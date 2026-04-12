# WeatherBet — Changelog

This file is the authoritative history of the bot's strategy and architecture.
It is designed to be both human-readable and fed as context to an AI assistant.

Each version entry documents: **what changed**, **why it changed**, and **what the
current state of the system is**. The most recent version at the top is the
canonical description of how the bot currently works.

---

## v4.0 — Confidence-First Strategy Overhaul (2026-04-12)

### Why This Change Was Made

Testing with a small $5 balance revealed a systematic failure: the bot
consistently entered losing positions because it used **edge-hunting** as its
primary entry signal.

Edge-hunting means: scan all temperature buckets, find any where the model's
probability estimate exceeds the market price, enter the one with the highest
expected value (EV). This approach has a critical flaw — buckets with the
highest EV are almost always **low-probability longshots** (e.g. a 20¢ bucket
with 35% true probability). These buckets fail most of the time (65% loss rate)
and their price trends **downward** after entry because the market is right to
price them cheap.

The fix: stop looking for mispriced markets. Instead, identify the
**highest-probability outcome** (what the forecast says is most likely to happen)
and enter if the price is low enough to offer a 35% ROI exit before resolution.

### Core Philosophy Shift

| | v3 (old) | v4 (new) |
|---|---|---|
| Entry signal | Highest expected value (EV) | Highest probability bucket |
| Entry filter | `EV >= min_ev` | `P >= min_confidence` AND price in zone |
| Exit | Time-based tiers (24h/48h thresholds) | Single ROI target (35%) |
| Goal | Beat the market's pricing | Ride the favorite to a 35% gain |
| Re-entry | Last cycle profitable | Last cycle profitable + price zone + hours check |
| Tuner params | `min_ev`, `max_price`, `kelly_fraction` | `min_confidence`, `take_profit_roi`, `kelly_fraction` |

### New Entry Logic (find_best_entry)

1. Compute P(bucket resolves YES) for every bucket using the Gaussian forecast model
2. Sort buckets by probability **descending** — pick the highest-probability one
3. Apply **opportunity zone** filter: `min_entry_price (0.25) ≤ price ≤ max_entry_price (0.65)`
   - Below 0.25: almost certainly a longshot the market has correctly priced cheap
   - Above 0.65: not enough room for a 35% ROI exit (would need price > 0.877)
4. Apply **confidence** filter: `P >= min_confidence (0.50)` — more likely right than wrong
5. Apply **price trend** filter: the bucket's price must be flat or rising over recent
   market snapshots (not declining — falling prices mean the market is moving against us)
6. Size via Kelly Criterion (`kelly_fraction` × full Kelly), capped at `max_bet`

If the highest-probability bucket fails any gate, skip the market entirely.
Do **not** fall back to a lower-probability bucket just to make a trade.

### New Exit Logic

All time-based take-profit tiers are removed. One rule governs all exits:

| Trigger | Condition | Label |
|---|---|---|
| Take-profit | `current_price >= entry_price × (1 + take_profit_roi)` AND above entry | `take_profit_roi` |
| Stop-loss | `current_price <= entry_price × stop_loss_pct` | `stop_loss` |
| Trailing stop | Once up 20%, stop moves to breakeven; subsequent drop to entry triggers exit | `trailing_stop` |
| Forecast shift | Forecast moves ≥ 2° outside the bet bucket | `forecast_changed` |
| Resolution | Polymarket settles the market YES/NO | `resolved` |

The take-profit ROI default is **35%**. The tuner can adjust this between 20%–50%.

### New Re-Entry Logic (evaluate_reentry)

After a cycle closes profitably, the same market may be re-entered **only** if
all of the following pass:

1. Last cycle `pnl > 0` (profitable exit required)
2. Current price is **below the last exit price** (not chasing a peak)
3. Current price is still in the opportunity zone (`≤ max_reentry_price = 0.65`)
4. At least `min_reentry_hours = 12` hours remain to resolution
5. Fresh forecast probability `P >= min_confidence` on the **same bucket**
6. Position size capped at cycle 1's original cost (no escalating bets)
7. Total cycles < `max_cycles_per_market = 3`

Re-entry always targets the **same bucket** as cycle 1. This prevents chasing
a different outcome after taking profit on the original.

### Tuner Changes

The tuner now adjusts these three parameters (requires 20+ resolved cycles):

| Parameter | Bounds | Signal used |
|---|---|---|
| `kelly_fraction` | 0.10 – 0.60 | Actual win rate vs predicted probability on resolved markets |
| `min_confidence` | 0.40 – 0.70 | Which confidence band produces the best avg PnL |
| `take_profit_roi` | 0.20 – 0.50 | Profitable exit rate and stop-loss frequency |

The old `min_ev` and `max_price` tuner parameters are removed.

### Market Snapshot Change

`market_snapshots` now stores **per-token prices** in addition to the top bucket:

```json
{
  "ts": "2026-04-12T10:00:00Z",
  "prices": { "<token_id>": 0.52, "<token_id_2>": 0.31, ... }
}
```

This enables the price trend check in `find_best_entry()`.

### Scope

- **Active scan regions**: US + EU only (`scan_regions: ["us", "eu"]`)
- All 20 city definitions are kept; other regions can be re-enabled via config
- Starting balance: **$30**, target: **$60 in 14 days** (+100%)
- Starting balance: auto-detected from on-chain wallet on first run (falls back to config `balance` field if wallet is unreachable)
- Data directory wiped clean before first run

### New Config Parameters

| Parameter | Default | Description |
|---|---|---|
| `min_confidence` | 0.50 | Minimum P(win) required to enter |
| `max_entry_price` | 0.65 | Upper price ceiling for the opportunity zone |
| `min_entry_price` | 0.25 | Lower price floor for the opportunity zone |
| `max_reentry_price` | 0.65 | Price ceiling for cycle re-entry |
| `min_reentry_hours` | 12.0 | Minimum hours left to allow re-entry |
| `take_profit_roi` | 0.35 | Sell when price is 35% above entry cost |

### Removed Config Parameters

`take_profit_short`, `take_profit_long`, `take_profit_final` — replaced by single `take_profit_roi`.
`min_ev` — no longer a primary entry filter (EV is still computed for diagnostics).
`max_price` — replaced by `max_entry_price`.

---

## v3.0 — Real Trade Execution + Multi-Cycle (2026-03-xx)

### What Changed

- Real trade execution via Polymarket CLOB API (`py_clob_client`)
- Multi-cycle support: bot can re-enter the same market after a profitable exit
- Reconciliation: detects on-chain positions not tracked in local state (crash recovery)
- Auto-tuner: adjusts `min_ev`, `max_price`, `kelly_fraction` from resolved data
- Calibration: Bayesian sigma updates per city/source/horizon
- METAR observations blended into forecast for D+0 markets within 6h of resolution
- Take-profit tiers: different price targets at >48h, 24–48h, <24h remaining
- Trailing stop: stop moves to breakeven once position is up 20%

### What It Kept From v2

- Gaussian CDF probability model (`bucket_prob`)
- Expected value calculation (`calc_ev`)
- Kelly Criterion sizing (`calc_kelly`)
- ECMWF + HRRR inverse-variance blend
- 20-city coverage (US, EU, Asia, SA, Oceania)

### Known Issues (fixed in v4)

- Edge-hunting entry signal consistently selected low-probability longshots
- Time-based take-profit tiers added unnecessary complexity
- Min-EV filter prevented entry on fairly-priced but high-confidence opportunities
- `max_price` cap discarded profitable high-confidence buckets above the threshold

---

## v2.0 — Simulation Bot, Full EV/Kelly (filename: weatherbet.py)

- Simulation only (no real trades)
- Full expected-value and Kelly sizing logic
- 20 cities, all regions
- ECMWF + HRRR blend
- No execution, no position tracking

---

## v1.0 — Base Bot (filename: bot_v1.py)

- 6 US cities only
- No EV or Kelly calculation
- No real trade execution
- Fixed bet sizing
