# WeatherBet — Polymarket Weather Trading Bot (v4 Strategy)

Automated weather market trading bot for Polymarket. Finds high-confidence
temperature outcomes using real forecast data from ECMWF and HRRR across
10 cities (US + EU), places real trades via the Polymarket CLOB API, and
manages positions autonomously.

See [CHANGELOG.md](CHANGELOG.md) for the full version history and the
reasoning behind each strategy change.

---

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [How a Trade is Decided](#how-a-trade-is-decided)
4. [Exit Conditions](#exit-conditions)
5. [Multi-Cycle Re-Entry](#multi-cycle-re-entry)
6. [Reconciliation](#reconciliation)
7. [Auto-Tuner](#auto-tuner)
8. [Calibration](#calibration)
9. [Data Model](#data-model)
10. [Config Reference](#config-reference)
11. [CLI Usage](#cli-usage)
12. [Audit Trail](#audit-trail)

---

## Overview

Polymarket runs binary markets like "Will the highest temperature in Chicago
be 56°F or higher on April 12?" This bot uses airport-station forecasts (the
same source Polymarket resolves on) to identify the **most probable temperature
outcome**, enters if the price offers a 35% ROI exit opportunity, then sells
early rather than waiting for resolution.

**Core principle:** bet on the *favorite*, not the *underdog with the best odds*.
A 65%-likely outcome at 50¢ is a better trade than a 30%-likely outcome at 15¢,
even though the second has higher expected value per dollar. The strategy is
designed to take profit at +35% ROI repeatedly, not to hold positions to
resolution.

**Key design principles:**
- Enter the highest-probability bucket, not the highest-EV bucket
- Price must be in the opportunity zone: 0.25 – 0.65 (room for +35% exit)
- Confidence must be ≥ 50% (`min_confidence`)
- Single take-profit rule: sell at +35% ROI (`take_profit_roi`)
- Position size via fractional Kelly Criterion
- Re-enter the same market only when risk conditions are favorable
- Parameters self-adjust over time via the built-in tuner

---

## Architecture

```
main loop
├── scan_markets()            — full scan every scan_interval seconds
│   ├── fetch forecasts       — ECMWF, HRRR, METAR per city per date
│   ├── fetch market data     — Polymarket Gamma API
│   ├── reconcile()           — detect orphaned on-chain positions
│   ├── stop/take-profit      — exit open positions if thresholds hit
│   ├── forecast-shift exit   — exit if model forecast moved 2+ degrees
│   ├── find_best_entry()     — enter if highest-P bucket passes gates
│   ├── evaluate_reentry()    — re-enter previous market if risk gates pass
│   ├── auto-resolve          — close markets that resolved on Polymarket
│   ├── run_calibration()     — update sigma estimates from resolved data
│   └── tune_strategy()       — adjust min_confidence, take_profit_roi, kelly_fraction
│
└── monitor_positions()       — quick check every monitor_interval seconds
    ├── reconcile()           — detect orphaned positions between scans
    └── stop/take-profit      — react to price moves without a full scan
```

The main loop alternates: run `monitor_positions()` every 5 minutes,
run a full `scan_markets()` every hour.

---

## How a Trade is Decided

### 1. Forecast Assembly

For each city/date combination the bot assembles the best available forecast:

- **ECMWF** (Open-Meteo, global, bias-corrected): primary source for all cities
- **HRRR/GFS** (Open-Meteo, US only, 48h horizon): secondary for US cities
- **METAR** (Aviation Weather API, real-time observation): blended for D+0

When both ECMWF and HRRR are available they are blended via
**inverse-variance weighting** — the model with lower historical error gets
more weight.

### 2. Sigma (Forecast Uncertainty)

Sigma is the standard deviation of the temperature forecast error. It
determines how wide the Gaussian distribution is spread around the forecast.

- Default: `1.2°C` / `2.0°F`
- Updated per city/source/horizon via Bayesian calibration as resolved data
  accumulates (see [Calibration](#calibration))

### 3. Bucket Probability

```
P(bucket) = Φ((high - forecast) / sigma) - Φ((low - forecast) / sigma)
```

Special cases:
- `"X or below"` buckets: `Φ((high - forecast) / sigma)`
- `"X or higher"` buckets: `1 - Φ((low - forecast) / sigma)`
- Single-degree Celsius buckets (e.g. `22°C`): treated as `[21.5, 22.5]`

### 4. Entry Decision (find_best_entry)

All buckets are scored by probability. The **highest-probability bucket** is
selected and then checked against all gates:

| Gate | Condition | Why |
|---|---|---|
| Opportunity zone | `0.25 ≤ price ≤ 0.65` | Below 0.25 = longshot; above 0.65 = no room for 35% ROI |
| Confidence | `P >= min_confidence (0.50)` | More likely right than wrong |
| Price trend | Price flat or rising vs last scan | Falling prices = market moving against us |
| Volume | `volume >= min_volume` | Minimum liquidity |
| Spread | `spread <= max_slippage` | Tight enough to fill at fair price |
| Hours | `min_hours <= h <= max_hours` | Valid time window |
| Portfolio cap | `open_positions < max_open_positions` | Overall exposure limit |
| Date cap | `positions_on_date < max_positions_per_date` | Concentration limit |
| Bet size | Kelly result `>= min_bet` | Worth placing |

If the top bucket fails any gate, the market is **skipped entirely** — the bot
does not fall back to a lower-probability bucket.

### 5. Kelly Criterion Sizing

```
full_kelly = (P × b - (1 - P)) / b      where b = (1/price - 1)
kelly      = full_kelly × kelly_fraction
bet_size   = min(kelly × balance, max_bet)
```

`kelly_fraction` default is 0.30. The tuner adjusts this based on win rate.

---

## Exit Conditions

Every open position is checked against five exit triggers on each monitor
and scan loop:

### 1. Take-Profit ROI
```
exit if: current_price >= entry_price × (1 + take_profit_roi)
         AND current_price > entry_price
```
Default `take_profit_roi = 0.35` (sell when up 35%).
Labeled `take_profit_roi` in the data.

### 2. Stop-Loss
```
exit if: current_price <= stop_price
stop_price = entry_price × stop_loss_pct   (set at entry, default 0.78)
```
Maximum loss: 22% of entry price. Labeled `stop_loss`.

### 3. Trailing Stop (Breakeven)
```
if current_price >= entry_price × 1.20:
    stop_price = entry_price   (raised to breakeven)
    trailing_activated = True
```
Once the position is up 20%, the stop moves to entry price. A subsequent
drop to entry triggers a `trailing_stop` exit at breakeven — locking in zero
loss on a position that was winning.

### 4. Forecast Shift Exit
```
exit if: forecast has moved outside the bucket by more than the bucket width + buffer
```
If the ECMWF forecast moves significantly away from the bucket the position
is in, the bot exits immediately. Model conviction has changed.
Labeled `forecast_changed`.

### 5. Market Resolution
After the market closes on Polymarket, the bot queries the final YES price:
- `YES >= 0.95` → WIN — shares pay out at $1.00 each
- `YES <= 0.05` → LOSS — position goes to $0

Labeled `resolved`.

---

## Multi-Cycle Re-Entry

After a position closes, the bot evaluates whether to re-enter the **same
bucket** in the same market. Re-entry requires all conditions to be true:

1. No currently open cycle on this market
2. Total cycles on this market < `max_cycles_per_market` (default: 3)
3. Last closed cycle was profitable (`pnl > 0`)
4. Current price is **below the last exit price** (not chasing a peaked price)
5. Current price is still in the opportunity zone (`≤ max_reentry_price = 0.65`)
6. At least `min_reentry_hours = 12` hours remain to resolution
7. Fresh forecast still gives `P >= min_confidence` on the same bucket
8. Position size capped at cycle 1's original cost (no escalating bets)

The "below last exit price" gate is critical: it prevents the bot from
buying back into a market that has already run up past where we just sold.

Re-entry always targets the **same bucket** as the original cycle. If the
bucket no longer passes the gates, no re-entry happens — the market is done.

---

## Reconciliation

Reconciliation handles on-chain shares that are not tracked locally (e.g.
after a crash or partial fill).

On each scan and monitor loop, for markets with no active open cycle:
1. Checks the on-chain token balance for the last known token
2. If shares ≥ 1, creates a new cycle record to track them

**Guards:**
- **Cycle limit**: skipped if `len(cycles) >= max_cycles_per_market`
- **Cooldown**: skipped if the last cycle closed within 120 seconds (settlement lag)

Reconciled cycles have `reconciled: true` and `order_id: null`.

---

## Auto-Tuner

The tuner runs at the end of every full scan (`tune_enabled: true`) and
adjusts three parameters based on recent performance. Requires at least
20 resolved cycles to activate.

**Data used:** all closed cycles, most recent `tune_lookback` cycles.

### What It Adjusts

**kelly_fraction** — based on win rate vs predicted probability:
```
if actual_win_rate > avg_predicted_p + 0.05: raise kelly_fraction (up to 0.60)
if actual_win_rate < avg_predicted_p - 0.05: lower kelly_fraction (down to 0.10)
max step: min(0.02, 10% of current kelly_fraction)
```
Uses only `close_reason == "resolved"` cycles — early exits don't dilute
the model accuracy signal.

**min_confidence** — finds the confidence band with the best avg PnL:
```
bands: ≥0.40, ≥0.45, ≥0.50, ≥0.55, ≥0.60, ≥0.65
max step: 10% of current min_confidence
```

**take_profit_roi** — adjusts target based on win rate and stop frequency:
```
if profit_rate > 60% AND stop_rate < 20%: raise take_profit_roi (up to 0.50)
if profit_rate < 45% OR stop_rate > 40%:  lower take_profit_roi (down to 0.20)
max step: 10% of current take_profit_roi
```

Tuned parameters are saved to `weather_bot_data/strategy.json`.
Delete this file to reset the tuner to config defaults.

---

## Calibration

The calibration system estimates forecast accuracy per city, per source,
per forecast horizon (D+0 through D+3).

After each full scan, for every resolved market with an `actual_temp`:
1. Computes the absolute error between each forecast snapshot and actual temp
2. Groups errors by city, source (ecmwf/hrrr), and horizon
3. Updates sigma using a Bayesian update:

```
sigma = (prior_weight × prior_sigma + n × MAE) / (prior_weight + n)
```

Calibrated sigmas are saved to `weather_bot_data/calibration.json`.

---

## Data Model

### `weather_bot_data/state.json`
```json
{
  "balance": 30.0,
  "starting_balance": 30.0,
  "net_pnl": 0.0,
  "total_trades": 0,
  "profitable_exits": 0,
  "losing_exits": 0,
  "resolved_wins": 0,
  "resolved_losses": 0,
  "peak_balance": 30.0
}
```

### `weather_bot_data/strategy.json`
Tuner output — overrides config for `min_confidence`, `take_profit_roi`,
and `kelly_fraction`. Delete to reset to config defaults.

### `weather_bot_data/calibration.json`
Sigma estimates per city/source/horizon.

### `weather_bot_data/markets/{city}_{date}.json`

**Market-level fields:**

| Field | Description |
|---|---|
| `city`, `date`, `unit` | Identifiers |
| `station` | METAR station for resolution |
| `event_end_date` | ISO timestamp of market resolution |
| `status` | `open`, `resolved`, or `unresolvable` |
| `cycles` | Array of trade cycles |
| `actual_temp` | Final temperature (from Visual Crossing after resolution) |
| `resolved_outcome` | `win`, `loss`, or null |
| `pnl` | Sum of PnL across all cycles |
| `forecast_snapshots` | Hourly forecast readings |
| `market_snapshots` | Hourly market prices per token (used for trend detection) |
| `all_outcomes` | All Polymarket buckets and latest prices |

**Cycle fields:**

| Field | Description |
|---|---|
| `cycle_num` | 1-indexed cycle number within this market |
| `market_id`, `token_id` | Polymarket identifiers |
| `bucket_low`, `bucket_high` | Temperature range bet on |
| `entry_price` | Price paid per share |
| `shares`, `cost` | Position size |
| `p` | Model probability at entry |
| `ev` | Expected value at entry (diagnostic; not the primary entry filter) |
| `kelly` | Kelly fraction at entry |
| `forecast_temp`, `sigma` | Forecast inputs at entry |
| `stop_price` | Current stop-loss price (may be raised by trailing stop) |
| `trailing_activated` | Whether trailing stop has fired |
| `exit_price`, `pnl` | Exit price and dollar PnL |
| `close_reason` | `stop_loss`, `trailing_stop`, `take_profit_roi`, `forecast_changed`, `resolved`, `sold_externally` |
| `reconciled` | `true` if created by reconciliation |

---

## Config Reference

| Parameter | Type | Description |
|---|---|---|
| `balance` | float | Current bankroll in USD |
| `max_bet` | float | Hard cap per trade in USD |
| `min_confidence` | float | Min P(win) to enter. Tuner adjusts this. |
| `max_entry_price` | float | Upper bound of opportunity zone (default 0.65) |
| `min_entry_price` | float | Lower bound of opportunity zone (default 0.25) |
| `max_reentry_price` | float | Max price allowed on cycle re-entry (default 0.65) |
| `min_reentry_hours` | float | Min hours remaining to allow re-entry (default 12) |
| `take_profit_roi` | float | Sell at this ROI above entry (default 0.35 = 35%). Tuner adjusts. |
| `stop_loss_pct` | float | Exit if price drops to this fraction of entry (default 0.78) |
| `kelly_fraction` | float | Fractional Kelly multiplier. Tuner adjusts. |
| `min_volume` | int | Minimum shares traded on the market |
| `min_hours` | float | Minimum hours to resolution |
| `max_hours` | float | Maximum hours to resolution |
| `max_slippage` | float | Max bid-ask spread allowed |
| `scan_interval` | int | Seconds between full scans |
| `monitor_interval` | int | Seconds between quick monitor checks |
| `max_open_positions` | int | Max concurrent open positions |
| `max_positions_per_date` | int | Max open positions on same resolution date |
| `max_cycles_per_market` | int | Max re-entries per market |
| `min_bet` | float | Minimum Kelly-sized bet in USD to place |
| `prior_weight` | int | Bayesian prior weight for sigma calibration |
| `tune_lookback` | int | Recent cycles used by the tuner |
| `tune_enabled` | bool | Enable/disable the auto-tuner |
| `scan_regions` | list | Regions to scan: `us`, `eu`, `asia`, `ca`, `sa`, `oc` |

---

## CLI Usage

```bash
# Start the bot — full scan every hour, monitor every 5 minutes
python bot_v3.py

# Print current balance, open positions, and unrealized PnL
python bot_v3.py status

# Full report — all resolved markets, cycle-level breakdown
python bot_v3.py report
```

The bot reads `weather_bot_config.json` on startup.

To start fresh (wipe all data):
```bash
rm -rf weather_bot_data/markets/
rm -f weather_bot_data/state.json
rm -f weather_bot_data/calibration.json
rm -f weather_bot_data/strategy.json
python bot_v3.py
```

---

## Audit Trail

Every trade decision is reconstructable from the market JSON file.

**Why the bot entered:**
1. Open `weather_bot_data/markets/{city}_{date}.json`
2. Find the cycle with the relevant `opened_at` timestamp
3. Read `entry_price`, `p`, `ev`, `kelly`, `forecast_temp`, `sigma`
4. Cross-reference `forecast_snapshots` at `opened_at`
5. Check `market_snapshots` — the `prices` dict shows per-token price history
   used for the trend check

**Why the bot exited:**
- `close_reason` tells you the trigger
- `take_profit_roi`: `exit_price >= entry_price × (1 + take_profit_roi)`
- `stop_loss`: `exit_price <= stop_price`
- `trailing_stop`: price fell back to breakeven after trailing stop activated
- `forecast_changed`: forecast snapshot at `closed_at` shows the shift

**Why the tuner changed a parameter:**
- `weather_bot_data/strategy.json` shows current tuned values
- Changes are printed as `[TUNE] param: old->new` after each full scan

---

## APIs Used

| API | Auth | Purpose |
|---|---|---|
| Open-Meteo | None | ECMWF + HRRR/GFS forecasts |
| Aviation Weather | None | METAR real-time station observations |
| Polymarket Gamma | None | Market data, prices, resolution status |
| Polymarket CLOB | Private key | Order placement and token balance |
| Visual Crossing | Free key | Historical temperatures for calibration |

---

## Versions

| File | Status | Description |
|---|---|---|
| `bot_v1.py` | Archive | Base bot, 6 US cities, no EV/Kelly, no real trades |
| `weatherbet.py` | Archive | Simulation bot, 20 cities, full EV/Kelly |
| `bot_v3.py` | **Current** | Real trades, v4 confidence-first strategy |

See [CHANGELOG.md](CHANGELOG.md) for full version history.

---

## Disclaimer

This is not financial advice. Prediction markets carry real financial risk.
All trades are executed with real funds. Understand the code before running
it with capital you cannot afford to lose.
