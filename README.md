# WeatherBet — Polymarket Weather Trading Bot (v3)

Automated weather market trading bot for Polymarket. Finds mispriced temperature outcomes using real forecast data from ECMWF and HRRR across 20 cities worldwide, places real trades via the Polymarket CLOB API, and manages positions autonomously.

---

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [How a Trade is Decided](#how-a-trade-is-decided)
4. [Multi-Cycle Re-Entry](#multi-cycle-re-entry)
5. [Exit Conditions](#exit-conditions)
6. [Reconciliation](#reconciliation)
7. [Auto-Tuner](#auto-tuner)
8. [Calibration](#calibration)
9. [Data Model](#data-model)
10. [Config Reference](#config-reference)
11. [CLI Usage](#cli-usage)
12. [Audit Trail — Tracing a Decision](#audit-trail--tracing-a-decision)

---

## Overview

Polymarket runs binary markets like "Will the highest temperature in Chicago be 56°F or higher on April 12?" These markets are frequently mispriced because retail participants use gut feel or city-center weather apps. This bot uses airport-station forecasts (the same source Polymarket resolves on) and a Gaussian probability model to find buckets where the true probability is meaningfully above the market price.

**Key design principles:**
- Every trade requires positive expected value (EV > `min_ev`)
- Position size is determined by fractional Kelly Criterion
- Positions are monitored every `monitor_interval` seconds for stop-loss and take-profit
- All data — forecasts, prices, trades, outcomes — is written to disk in JSON for auditability
- Parameters self-adjust over time via a built-in tuner

---

## Architecture

```
main loop
├── scan_markets()          — full scan every scan_interval seconds
│   ├── fetch forecasts     — ECMWF, HRRR, METAR per city per date
│   ├── fetch market data   — Polymarket Gamma API
│   ├── reconcile()         — detect orphaned on-chain positions
│   ├── stop/take-profit    — exit open positions if thresholds hit
│   ├── forecast-shift exit — exit if model forecast moved 2+ degrees
│   ├── open position       — enter new position if EV > min_ev
│   ├── auto-resolve        — close markets that resolved on Polymarket
│   ├── run_calibration()   — update sigma estimates from resolved data
│   └── tune_strategy()     — adjust min_ev, max_price, kelly_fraction
│
└── monitor_positions()     — quick check every monitor_interval seconds
    ├── reconcile()         — detect orphaned positions between scans
    └── stop/take-profit    — react to price moves without a full scan
```

The main loop alternates: run `monitor_positions()` every minute, run a full `scan_markets()` every hour.

---

## How a Trade is Decided

### 1. Forecast Assembly

For each city/date combination, the bot assembles the best available temperature forecast:

- **ECMWF** (Open-Meteo, global, bias-corrected): primary source for all cities
- **HRRR/GFS** (Open-Meteo, US only, 48h horizon): secondary source for US cities
- **METAR** (Aviation Weather API, real-time observation): used in a blend when <6h to resolution

When both ECMWF and HRRR are available, they are blended using **inverse-variance weighting** — the model with lower historical error (lower sigma) gets more weight.

### 2. Sigma (Forecast Uncertainty)

Sigma is the standard deviation of the temperature forecast error. It determines how wide the Gaussian distribution is spread around the forecast temperature.

- Default: `1.2°C` / `2.0°F`
- When resolved market data accumulates, sigma is updated per city, per source, per forecast horizon (D+0, D+1, D+2, D+3) via Bayesian update (see [Calibration](#calibration))
- Tighter sigma → higher confidence → sharper probability estimates

### 3. Bucket Probability

For each temperature bucket on Polymarket, the bot computes the probability using a Gaussian CDF:

```
P(bucket) = Φ((high - forecast) / sigma) - Φ((low - forecast) / sigma)
```

Special cases:
- `"X or below"` buckets: `Φ((high - forecast) / sigma)`
- `"X or higher"` buckets: `1 - Φ((low - forecast) / sigma)`
- Single-degree Celsius buckets (e.g. `22°C`): treated as `[21.5, 22.5]`

### 4. Expected Value

```
EV = P × (1/price - 1) - (1 - P)
```

This is the expected dollar return per dollar risked. A trade is only considered if `EV >= min_ev`.

### 5. Kelly Criterion

```
full_kelly = (P × b - (1 - P)) / b      where b = (1/price - 1)
kelly      = full_kelly × kelly_fraction
bet_size   = min(kelly × balance, max_bet)
```

`kelly_fraction` is the fractional Kelly multiplier (default 0.25). Full Kelly maximises long-run growth but is volatile — fractional Kelly trades some growth rate for stability.

### 6. Filters Applied Before Entry

| Filter | Description |
|---|---|
| `EV >= min_ev` | Minimum edge required |
| `market_price <= max_price` | Won't overpay on near-certain markets |
| `market_price >= min_price` | Avoids near-zero penny markets where spread exceeds edge |
| `volume >= min_volume` | Minimum liquidity |
| `spread <= max_slippage` | Bid-ask spread must be tight enough |
| `hours >= min_hours` | Market must have enough time left |
| `hours <= max_hours` | Market must not be too far out |
| `open_positions < max_open_positions` | Portfolio-level cap |
| `positions_on_date < max_positions_per_date` | Per-date concentration cap |
| `bet_size >= min_bet` | Kelly result must be large enough to be worth placing |

Among all buckets that pass filters, the bot selects the one with the **highest EV**.

---

## Multi-Cycle Re-Entry

After a position closes (stop-loss, take-profit, or forecast shift), the bot can re-enter the same market with a new position — called a **cycle**.

Re-entry conditions (all must be true):
1. No currently open cycle on this market
2. Total cycles on this market < `max_cycles_per_market`
3. The **last closed cycle was profitable** (`pnl > 0`)
4. A valid forecast is available
5. `hours >= min_hours`

The "last profitable" gate is intentional: re-entry is only allowed when the previous exit was a win. This prevents the bot from averaging down into a losing position repeatedly. If the last cycle lost, no further re-entries happen on that market.

Each cycle is tracked independently with its own `entry_price`, `shares`, `pnl`, `close_reason`, and timestamps. Each cycle is an independent data point for the tuner.

---

## Exit Conditions

Every open position is checked against five possible exit triggers on each monitor and scan loop:

### 1. Stop-Loss
```
exit if: current_price <= stop_price
stop_price = entry_price × stop_loss_pct   (set at entry)
```
Labeled `stop_loss` in the data. The position is sold at market.

### 2. Trailing Stop (Breakeven)
```
if current_price >= entry_price × 1.20:
    stop_price = entry_price   (raised to breakeven)
    trailing_activated = True
```
Once the position is up 20%, the stop moves to the entry price. A subsequent drop back to entry triggers a `trailing_stop` exit at breakeven.

### 3. Take-Profit (Time-Based)
```
if hours < 24:   threshold = take_profit_final   (default 0.50)
elif hours < 48: threshold = take_profit_short   (default 0.82)
else:            threshold = take_profit_long    (default 0.70)

exit if: current_price >= threshold AND current_price > entry_price
```
The `current_price > entry_price` guard ensures this never exits at a loss — if the threshold is below entry, the position holds until stop-loss or resolution.

Labeled `take_profit` in the data.

### 4. Take-Profit ROI
```
roi_threshold = entry_price × (1 + take_profit_roi)   (default 1.35×)
exit if: current_price >= roi_threshold AND current_price > entry_price
```
Always active regardless of time remaining. Locks a fixed ROI gain any time it's hit.
Labeled `take_profit_roi` in the data.

### 5. Forecast Shift Exit
```
exit if: |new_forecast - bucket_midpoint| > 2°C (or 2°F)
```
If the ECMWF forecast moves 2+ degrees away from the bucket the position is in, the bot exits immediately. The model's conviction has changed.
Labeled `forecast_changed` in the data.

### 6. Market Resolution
After the market closes on Polymarket, the bot queries the final YES price:
- `YES >= 0.95` → WIN — shares pay out at $1.00 each
- `YES <= 0.05` → LOSS — position goes to $0

Labeled `resolved` in the data. State `wins`/`losses` counters only increment on resolutions, not early exits.

---

## Reconciliation

Reconciliation handles the case where the bot has shares on-chain that are not tracked in its local state (e.g., after a crash, a partial fill, or an external buy).

On each scan and monitor loop, for markets with no active open cycle, the bot:
1. Checks the on-chain token balance for the last known token
2. If shares exist, creates a new cycle record to track them

**Guards (as of the current version):**
- **Cycle limit**: reconcile is skipped if `len(cycles) >= max_cycles_per_market` — orphaned shares above the limit go untracked rather than triggering unlimited cycles
- **Cooldown**: reconcile is skipped if the last cycle closed within 120 seconds — prevents the race condition where a fresh stop-loss still shows shares on-chain due to API settlement lag

Reconciled cycles have `reconciled: true` and `order_id: null` in the data. They may have `p: null` and `ev: null` if no forecast was available at reconcile time.

---

## Auto-Tuner

The tuner runs at the end of every full scan (`tune_enabled: true`) and adjusts three strategy parameters based on recent performance.

### Data Used
All closed cycles from resolved markets, sorted by `closed_at`, taking the most recent `tune_lookback` cycles. Requires at least 20 resolved cycles to fire.

### What It Adjusts

**kelly_fraction** — based on win rate vs predicted probability:
```
if actual_win_rate > avg_predicted_p + 0.05: raise kelly_fraction (up to 0.60)
if actual_win_rate < avg_predicted_p - 0.05: lower kelly_fraction (down to 0.10)
max step per tune: min(0.02, 10% of current kelly_fraction)
```

**min_ev** — finds the EV band with the best average PnL (requires ≥5 samples per band):
```
bands: ≥0.03, ≥0.05, ≥0.08, ≥0.10, ≥0.15, ≥0.20
max step: 10% of current min_ev
```

**max_price** — finds the price ceiling with the best total PnL (requires ≥5 samples per band):
```
bands: ≤0.25, ≤0.30, ≤0.35, ≤0.40, ≤0.45, ≤0.55, ≤0.65
max step: 10% of current max_price
```

Win rate counts any cycle with `pnl > 0` as a win, including `take_profit` and `take_profit_roi` exits (not just market resolutions).

Tuned parameters are saved to `weather_bot_data/strategy.json` and loaded on bot startup. Changes are printed as `[TUNE] param: old->new`.

---

## Calibration

The calibration system estimates forecast accuracy per city, per source, per forecast horizon.

After each full scan, for every resolved market with an `actual_temp`:
1. The bot computes the absolute error between each forecast snapshot and the actual temperature
2. It groups errors by city, source (ecmwf/hrrr), and horizon (D+0, D+1, D+2, D+3)
3. It updates sigma using a **Bayesian update**:

```
sigma = (prior_weight × prior_sigma + n × MAE) / (prior_weight + n)
```

`prior_weight` controls how many data points the prior is worth (default 5). With 5 real observations, the observed MAE has equal weight to the prior.

Calibrated sigmas are saved to `weather_bot_data/calibration.json`. They are loaded on each scan and used in `bucket_prob()` calculations. A city/source/horizon with no calibration data falls back to the city default (1.2°C or 2.0°F).

---

## Data Model

### `weather_bot_data/state.json`
Session-level accounting:
```json
{
  "balance": 5.46,
  "starting_balance": 5.46,
  "total_trades": 0,
  "wins": 0,
  "losses": 0,
  "peak_balance": 5.46
}
```
`wins`/`losses` count only market resolutions, not early exits. `balance` is updated on every trade open/close.

### `weather_bot_data/strategy.json`
Tuner output — overrides the config values for `min_ev`, `max_price`, and `kelly_fraction`:
```json
{
  "min_ev": 0.08,
  "max_price": 0.62,
  "kelly_fraction": 0.25
}
```
Delete this file to reset the tuner to config defaults.

### `weather_bot_data/calibration.json`
Sigma estimates per city/source/horizon:
```json
{
  "chicago_ecmwf_d1": { "sigma": 1.8, "n": 12, "updated_at": "..." },
  "chicago_ecmwf":    { "sigma": 1.9, "n": 30, "updated_at": "..." }
}
```

### `weather_bot_data/markets/{city}_{date}.json`
One file per city/date. Contains the full history:

| Field | Description |
|---|---|
| `city`, `date`, `unit` | Identifiers |
| `station` | METAR station used for resolution |
| `event_end_date` | ISO timestamp of market resolution |
| `status` | `open`, `resolved`, or `unresolvable` |
| `cycles` | Array of trade cycles (see below) |
| `actual_temp` | Final temperature from Visual Crossing (populated after resolution) |
| `resolved_outcome` | `win`, `loss`, or null |
| `pnl` | Sum of PnL across all cycles |
| `forecast_snapshots` | Hourly forecast readings (ECMWF, HRRR, METAR) |
| `market_snapshots` | Hourly market price and top-bucket readings |
| `all_outcomes` | All Polymarket buckets and their prices at last scan |

**Cycle fields:**

| Field | Description |
|---|---|
| `cycle_num` | 1-indexed cycle number within this market |
| `market_id`, `token_id` | Polymarket identifiers |
| `bucket_low`, `bucket_high` | Temperature range bet on |
| `entry_price` | Price paid per share |
| `shares`, `cost` | Position size |
| `p` | Model-estimated probability at entry |
| `ev`, `kelly` | Expected value and Kelly fraction at entry |
| `forecast_temp`, `forecast_src`, `sigma` | Forecast inputs used at entry |
| `stop_price` | Current stop-loss price (may be raised by trailing stop) |
| `trailing_activated` | Whether trailing stop has been triggered |
| `exit_price`, `pnl` | Exit price and dollar PnL |
| `close_reason` | `stop_loss`, `trailing_stop`, `take_profit`, `take_profit_roi`, `forecast_changed`, `resolved`, `sold_externally` |
| `reconciled` | `true` if this cycle was created by reconciliation, not a bot-initiated buy |

---

## Config Reference

| Parameter | Type | Description |
|---|---|---|
| `balance` | float | Current bankroll in USD. Update manually when restarting after a top-up. |
| `max_bet` | float | Hard cap per trade in USD. Kelly may suggest more — this is the ceiling. |
| `min_ev` | float | Minimum expected value to enter. Tuner adjusts this. |
| `max_price` | float | Maximum market price to buy. Tuner adjusts this. |
| `min_price` | float | Minimum market price to buy. Filters penny markets. Do not set below 0.08. |
| `min_volume` | int | Minimum shares traded on the market. Filters illiquid markets. |
| `min_hours` | float | Minimum hours to resolution. Skip markets resolving too soon. |
| `max_hours` | float | Maximum hours to resolution. Skip markets too far out. |
| `kelly_fraction` | float | Fractional Kelly multiplier (0–1). Tuner adjusts this. |
| `scan_interval` | int | Seconds between full scans. |
| `max_slippage` | float | Max bid-ask spread allowed. |
| `prior_weight` | int | Bayesian prior weight for calibration sigma. Lower = faster learning. |
| `tune_lookback` | int | Number of recent resolved cycles for the tuner. |
| `tune_enabled` | bool | Enable/disable the auto-tuner. |
| `max_open_positions` | int | Max concurrent open positions across all markets. |
| `max_positions_per_date` | int | Max open positions sharing the same resolution date. |
| `monitor_interval` | int | Seconds between quick monitor checks (stop-loss, take-profit). |
| `stop_loss_pct` | float | Exit if price drops to this fraction of entry (e.g. 0.72 = 28% loss). |
| `take_profit_short` | float | Sell target when 24–48h remain. |
| `take_profit_long` | float | Sell target when >48h remain. |
| `take_profit_final` | float | Sell target when <24h remain. Only fires above entry price. |
| `take_profit_roi` | float | Sell when price is this fraction above entry (e.g. 0.35 = +35%). |
| `max_cycles_per_market` | int | Max re-entries per market. Reconcile also respects this limit. |
| `min_bet` | float | Minimum Kelly-sized bet in USD to place an order. |
| `scan_regions` | list | City regions to scan: `eu`, `us`, `asia`, `ca`, `sa`, `oc`. |

---

## CLI Usage

```bash
# Start the bot — full scan every hour, monitor every minute
python bot_v3.py

# Print current balance, open positions, and unrealized PnL
python bot_v3.py status

# Full report — all resolved markets, cycle-level breakdown
python bot_v3.py report
```

The bot reads `weather_bot_config.json` on startup. To use the prod config:
```bash
cp weather_bot_config_prod.json weather_bot_config.json
python bot_v3.py
```

To start fresh (wipe all data):
```bash
rm -rf weather_bot_data/markets/
rm weather_bot_data/state.json
rm weather_bot_data/calibration.json
rm weather_bot_data/strategy.json
python bot_v3.py
```

---

## Audit Trail — Tracing a Decision

Every trade decision is reconstructable from the market JSON file. To understand why the bot entered a position:

1. Open `weather_bot_data/markets/{city}_{date}.json`
2. Find the cycle with the relevant `opened_at` timestamp
3. Read `entry_price`, `p`, `ev`, `kelly`, `forecast_temp`, `sigma`, `forecast_src`
4. Cross-reference `forecast_snapshots` for the snapshot at `opened_at` — this shows exactly what ECMWF, HRRR, and METAR were reporting at entry time
5. Cross-reference `market_snapshots` for the market price history

To understand why the bot exited:
- `close_reason` tells you the trigger
- `exit_price` and `pnl` show the outcome
- For `stop_loss`: compare `exit_price` to `stop_price` in the cycle
- For `take_profit`: compare `exit_price` to the relevant threshold given `hours` remaining at exit
- For `forecast_changed`: the forecast snapshot at `closed_at` will show the model had moved 2+ degrees

To trace tuner decisions:
- `weather_bot_data/strategy.json` shows current tuned values
- Tuner adjustments are printed to stdout as `[TUNE] param: old->new` after each full scan

---

## Versions

| File | Status | Description |
|---|---|---|
| `bot_v1.py` | Archive | Base bot, 6 US cities, no EV/Kelly, no real trades |
| `weatherbet.py` | Archive | Simulation bot, 20 cities, full EV/Kelly, no execution |
| `bot_v3.py` | **Current** | Full execution bot — real trades, multi-cycle, auto-tuner, reconciliation |

---

## APIs Used

| API | Auth | Purpose |
|---|---|---|
| Open-Meteo | None | ECMWF + HRRR/GFS forecasts |
| Aviation Weather | None | METAR real-time station observations |
| Polymarket Gamma | None | Market data, prices, resolution status |
| Polymarket CLOB | Private key | Order placement and token balance |
| Visual Crossing | Free key | Historical temperatures for resolution |

---

## Disclaimer

This is not financial advice. Prediction markets carry real financial risk. All trades are executed with real funds. Understand the code before running it with capital you cannot afford to lose.
