# WeatherBet — Changelog

This file is the authoritative history of the bot's strategy and architecture.
It is designed to be both human-readable and fed as context to an AI assistant.

Each version entry documents: **what changed**, **why it changed**, and **what the
current state of the system is**. The most recent version at the top is the
canonical description of how the bot currently works.

---

## v4.5 — Cycle-Based Convergence Strategy + True Trailing Stop (2026-04-16)

### Why This Change Was Made

Live data from the first v4.3/v4.4 run (9 markets scanned, 1 entry, 2 stop-losses) surfaced two
problems: a gate configuration that blocked valid entries, and a trailing stop that ejected
profitable positions on normal price noise.

**Problem 1 — Gate over-tightness (1 of 9 markets entered).**

Post-scan gate analysis found three gates blocking 8 valid candidates:

| Gate | Old value | Blocked markets |
|------|-----------|-----------------|
| `min_entry_price` | 0.25 | Paris $0.20, Seattle $0.105, NYC $0.23, Chicago $0.089 |
| `min_edge` | 0.10 | London edge=0.096 (4bp short) |
| `kelly_fraction` | 0.30 | Atlanta $0.60, Miami $0.90, Munich $0.57 — all below $1.00 min_bet |

These were not marginal markets: Paris had P=60%, Seattle P=55%, London edge=9.6%. The gates
were calibrated for a different strategy and did not reflect actual opportunity characteristics.

**Problem 2 — Paris sold at $0.00 PnL despite being up +20%.**

The trailing stop triggered because it locked `stop_price` to exactly `entry_price` ($0.22)
when price hit `entry × 1.20 = $0.264`. Thin-market liquidity noise brought price back to
$0.22 in the next monitor cycle — triggering a stop at breakeven with zero gain.

Root cause: "lock to breakeven at +20%" is too tight for prediction markets. A 10–20% intraday
price swing is normal in thin-liquidity Polymarket books. The stop needed to be wider and
follow the price dynamically, not snap to a fixed level.

**Problem 3 — Strategy misalignment: edge-finder vs temperature predictor.**

Deeper analysis revealed the bot was functioning as an edge-finder (looking for market mispricing
vs model P) but the market also uses ECMWF/GFS data — so the "edge" on any given day is close
to zero by construction. Polymarket resolves on Wunderground station data (whole-integer degrees,
station-specific), not grid-cell averages. Grid models can be systematically cold- or warm-biased
vs the resolution station.

The real opportunity is **convergence**: temperature prediction markets are underpriced early
(0.10–0.35) when uncertainty is high and concentrate toward 0.80–0.95 as resolution approaches
and station-specific data confirms the forecast. The correct strategy is to enter early at a
cheap price, ride the convergence, take profit when the share price doubles, then re-enter for
another convergence cycle — up to 3× per market.

### Changes

#### Config gate relaxation (3 parameters)

| Parameter | Old | New | Reason |
|-----------|-----|-----|--------|
| `min_entry_price` | 0.25 | 0.10 | Valid-edge buckets exist at 0.10–0.24; low price alone is not a disqualifier when P ≥ min_confidence and edge ≥ min_edge |
| `min_edge` | 0.10 | 0.06 | London (edge=0.096) and comparable markets were blocked by 4bp; 0.06 requires meaningful gap without over-filtering |
| `kelly_fraction` | 0.30 | 0.40 | 30% Kelly produced sub-$1.00 bets (Polymarket floor), blocking entries with real edge |

#### `take_profit_roi` changed to 1.0 (cycle-based convergence)

**Old:** 0.35 — sell when price rises 35% above entry.

**New:** 1.0 — sell when share price doubles (+100% ROI).

Rationale: Prediction markets converge from 0.15–0.35 entry prices toward 0.80–0.95 at resolution
for correct buckets. A 35% exit captures $0.05–$0.12 on a $0.20 entry and leaves most of the
convergence gain on the table. A 100% ROI target captures the full convergence move while still
allowing re-entry for subsequent cycles on the same market.

Risk/reward improved from **0.7:1** (35% gain vs 50% loss, breakeven at 59% win rate) to
**2:1** (100% gain vs 50% loss, breakeven at 33.3% win rate).

#### True trailing stop (replaces lock-to-breakeven)

**Old behavior:** When `current_price >= entry × 1.20`, set `stop_price = entry_price`. This
locks the stop at exactly breakeven, so any price wobble back to entry triggers the stop.

**New behavior:** Track `peak_price` continuously. When `current_price >= entry × TRAILING_ACTIVATION`
(1.50 = +50%), the trailing stop activates and follows:

```
stop = peak_price × (1 − TRAILING_DISTANCE)   # TRAILING_DISTANCE = 0.30
```

The stop rises as price rises and never decreases. It only fires if price drops 30% from its
highest point — not 30% from entry, but 30% from the peak it reached.

Key constants:
- `TRAILING_ACTIVATION = 1.50` — stop activates only after price reaches entry × 1.50 (+50%)
- `TRAILING_DISTANCE = 0.30` — stop trails 30% below the peak price

At a $0.22 entry:
- Trailing activates when price reaches $0.33 (+50%)
- If price peaks at $0.40, stop locks at $0.28 (entry × 1.27 — above breakeven)
- If price continues to $0.50, stop rises to $0.35
- Price must drop 30% from its peak to trigger — normal noise (10–20%) does not fire the stop

The take-profit at +100% fires well before the trailing stop in most convergence scenarios, so
the trailing stop acts as a safety net for partial convergence or unexpected reversals, not as
the primary exit mechanism.

#### Cycle re-entry strategy

Up to `max_cycles_per_market = 3` re-entries per market. Each cycle:
1. Enters at cheap price (0.10–0.35) when P ≥ min_confidence and edge ≥ min_edge
2. Holds through convergence
3. Takes profit at +100% ROI (share price doubles)
4. Re-enters same bucket (if still hours remaining and conditions pass re-entry gates)
5. Stop-loss at −50% acts as backstop for each cycle independently

### Current State After v4.5

```
Strategy:    Cycle-based convergence. Enter when ECMWF and GFS agree (|delta| <= 2.0) AND
             P − market_price >= 0.06. Take profit when share price doubles (+100% ROI).
             Re-enter same market up to 3 cycles. Trailing stop activates at +50%, trails
             30% below peak. Stop-loss at −50%. No forecast-based exits.

min_entry_price:   0.10    (was 0.25)
min_edge:          0.06    (was 0.10)
kelly_fraction:    0.40    (was 0.30)
take_profit_roi:   1.00    (was 0.35)
stop_loss_pct:     0.50    (unchanged)
TRAILING_ACTIVATION: 1.50  (was 1.20 implicit)
TRAILING_DISTANCE:   0.30  (was 0.20)
```

---

## v4.4 — Sigma Decovariance + Market-Consensus Gate (2026-04-16)

### Why This Change Was Made

Post-mortem of the Munich D+2 2026-04-18 trade — the first trade executed by v4.3 — identified
three compounding failure modes that produced a bad entry despite all v4.3 gates passing.

**The trade:** Munich 19°C bucket, entry at $0.200, P=44%, edge=+0.24, model Δ=0.3°C.
- ECMWF: 19.1°C, GFS: 18.8°C → blended forecast: 18.9°C
- Market consensus (19°C: 18.5¢, 20°C: 23¢, **21°C: 28¢**) implied ~20.3°C — 1.4°C warmer
- At resolution, the market was correct and the position is a loser

**Root cause 1 — sigma inflation from blending (primary).**
`_blend_iv()` used the inverse-variance formula `σ = sqrt(1 / Σ(1/σᵢ²))` for blending sigma.
This formula is correct only when sources are **independent**. ECMWF and GFS are not: they share
physical equations, global observations, and fail in the same direction (~0.7–0.9 correlation).
Treating them as independent collapsed σ from 1.2°C to 0.849°C — a 29% reduction:

```
σ=0.849  →  P(19°C) = 44%  → passes min_confidence=0.38  →  trade entered   ✗
σ=1.200  →  P(19°C) = 32%  → below min_confidence=0.38   →  trade blocked   ✓
```

**Root cause 2 — price trend window too narrow.**
`is_price_stable_or_rising()` compared only the last 2 market snapshots (window=2). The 19°C
bucket's price fell 30% (0.235 → 0.160) between 12:32 and 16:23, then bounced slightly to 0.165
by 17:17. The two-snapshot window saw the bounce (+12%) and passed. A four-snapshot window would
have compared 14:32 (0.205) to 17:17 (0.165) — a 20% drop, below the 95% threshold, blocking entry.

**Root cause 3 — market consensus ignored.**
No gate compared the crowd's implied temperature to the model forecast. The market was pricing
the distribution 1.4°C warmer than the model. For EU 1°C buckets, that disagreement spans
more than one full bucket width and is a meaningful signal.

### Changes

#### `_blend_iv()` — sigma uses average variance, not IV formula (critical)

**Old:** `blended_sigma = sqrt(1 / Σ(1/σᵢ²))` — IV formula, assumes source independence.

**New:** `blended_sigma = sqrt(Σ(σᵢ²) / n)` — average variance, assumes ~0.5 correlation.

For two sources with equal σ, the old formula gives `σ/√2`; the new formula gives `σ`.
Temperature blending (the IV-weighted mean) is unchanged — only the sigma calculation changes.

Effect on sigma and P for the Munich trade:

| Formula | σ_blend | P(19°C) | Passes min_confidence=0.38 |
|---------|---------|---------|---------------------------|
| Old (IV — wrong) | 0.849°C | 44% | Yes → trade entered |
| New (avg variance) | 1.200°C | 32% | No → blocked |

METAR still provides genuine sigma reduction when present, because METAR is a real observation
(an independent measurement) and the IV formula is valid for independent sources. The fix applies
proportionally: two equal-sigma models produce no sigma reduction; adding METAR produces a modest
reduction (e.g. σ=1.2 + σ_metar=1.0 blends to 1.104 instead of 0.921).

#### `find_best_entry()` — trend window increased from 2 → 4 (gate 5)

The 2-snapshot window is too short to catch a drop-then-bounce pattern. A 4-snapshot window
compares the price 4 scans ago (approx. 4 hours) to the current price, making short-term
bounces harder to use as false confirmation.

Munich 19°C price history around entry:

| Time | Price |
|------|-------|
| 14:32 | 0.205 |
| 15:23 | 0.205 |
| 16:23 | **0.160** (−22%) |
| 17:17 | 0.165 |
| 17:45 (entry) | 0.185 |

Window=2 saw 0.165 → 0.185 (+12%) → passed. Window=4 sees 0.205 → 0.185 (−10%, below 95%) → blocked.

#### `market_implied_temp()` + gate 0b — crowd-vs-model gap check (new)

New function `market_implied_temp(outcomes)` computes the probability-weighted average of bucket
midpoints across all outcomes, using market prices as weights. Terminal buckets (`≤X°` or `≥X°`)
use their finite boundary as the midpoint proxy.

New gate 0b in `find_best_entry()`: if `|market_implied_temp − forecast_temp| > 2 × max_model_delta`,
block entry. At `max_model_delta=2.0°C`, the threshold is 4°C. This gate acts as a backstop for
cases where the crowd is pricing a dramatically different scenario than the models (e.g. a surprise
heatwave, local observation data not yet in the models, or a model initialization error).

For the Munich trade: market implied 20.3°C vs model 18.9°C = 1.4°C gap — below the 4.0°C threshold,
so gate 0b would not have blocked this trade independently. The gate becomes load-bearing for
larger divergences. Fix 1 (sigma) and Fix 2 (trend window) are each individually sufficient.

Updated gate order in `find_best_entry()`:

```
  0.  MODEL AGREEMENT:     |ECMWF − GFS| <= max_model_delta        → return None if fails
  0b. MARKET CONSENSUS:    |market_implied − forecast| <= 2×max_model_delta  → return None if fails
  1.  ZONE:                min_entry_price <= price <= max_entry_price
  2.  MIN EDGE:            P − market_price >= min_edge
  3.  CONFIDENCE:          P >= min_confidence
  4.  VOLUME:              >= min_volume                             (data gate — continue)
  5.  PRICE TREND:         flat or rising over last 4 scans          (data gate — continue)
  6.  BET SIZE:            Kelly size >= min_bet                     (data gate — continue)
```

### What Did Not Change

- Temperature blending formula (IV-weighted mean) — only the sigma formula changed
- All other entry gates, re-entry logic, stop-loss, take-profit, trailing stop
- Config parameters — no new parameters added (gate 0b threshold is derived from `max_model_delta`)
- Kelly sizing, calibration, auto-tuner, reconciliation, Polymarket API calls

---

## v4.3 — Clean Rewrite: Model Agreement Gate + Forecast Exit Removal (2026-04-16)

### Why This Change Was Made

Post-mortem analysis of 17 live trades (11.8% win rate, −27% drawdown, −$5.28 net PnL)
identified two dominant failure modes from the trade data:

**1. `forecast_changed` exits on correct predictions.**
Atlanta 2026-04-13: ECMWF=GFS=82°F, actual=82.1°F — the bucket was correct. A routine
forecast update triggered `forecast_changed` → sold at −$0.26 when it would have resolved YES.

**2. Entering markets where the two models disagree → wrong bucket selection.**
Every trade where `|ECMWF − GFS| ≥ 3°F` resulted in a loss because neither model
could be trusted to identify the right temperature bucket:

| Market | ECMWF–GFS delta | Exit | PnL |
|--------|----------------|------|-----|
| seattle_2026-04-13 | 3°F | stop_loss | −$0.51 (actual was 3.6°F off) |
| dallas_2026-04-14 | 4°F | forecast_changed | −$0.12 |
| dallas_2026-04-15 | 3°F | stop_loss | **−$1.46** (largest single loss) |
| miami_2026-04-15 | 3°F | forecast_changed | −$0.11 |

**3. `get_hrrr()` was US-only and misnamed.** The function called `models=gfs_seamless` (GFS,
not HRRR) and returned empty data for all EU cities — meaning London, Paris, Munich, and Ankara
had no second-model comparison and no model_delta check possible.

A backtest of the new gates against all 17 trades confirms:
```
BLOCKED 4 trades (all losses): PnL = −$2.20 (saved)
ALLOWED 12 trades:              PnL = −$3.08
Net:  −$3.08 vs actual −$5.28  (+$2.20 improvement, 42% fewer losses)
```
The remaining −$3.08 in allowed trades includes London (−$0.46) and Munich (−$1.60) which
had no GFS data in v3 (EU was US-only). With the fix in v4.3, those markets now have
model_delta values and may be blocked in future runs.

### File

`bot_v4.py` — clean rewrite. `bot_v3.py` is archived untouched.

### Changes

#### `forecast_changed` exit — removed entirely (critical)

The exit fired when the forecast moved ≥2° outside the bucket boundary AND the position was
losing. Even in its most conservative form, this caused the bot to sell correct predictions at
a loss before resolution. The Atlanta case (correct bucket, exited −$0.26) is the clearest proof.

**The market price already incorporates forecast updates.** If the forecast shifts adversarially,
the price will fall and the stop-loss will eventually fire. A separate forecast-tracking exit adds
no protective value and demonstrably creates losses.

**New exit rules — price only:**

| Trigger | Condition | Label |
|---------|-----------|-------|
| Take-profit | `current_price >= entry_price × (1 + take_profit_roi)` | `take_profit_roi` |
| Stop-loss | `current_price <= entry_price × stop_loss_pct` | `stop_loss` |
| Trailing stop | Once +20% above entry, stop rises to breakeven | `trailing_stop` |
| Resolution | Polymarket settles YES/NO | `resolved` |

#### Model agreement gate (new, critical)

Block entry for the entire market if `|ECMWF − GFS| > max_model_delta` (default 2.0°F/°C).

Applied before any bucket-level checks. When the two independent models disagree by more than
the gate threshold, neither can be trusted to identify the correct 1–2° bucket. All four
gated losses in the backtest had deltas of 3–4°F.

```
Gate order in find_best_entry():
  0. MODEL AGREEMENT: |ECMWF − GFS| <= max_model_delta  → return None if fails
  1. ZONE:            min_entry_price <= price <= max_entry_price
  2. MIN EDGE:        P − market_price >= min_edge
  3. CONFIDENCE:      P >= min_confidence
  4. VOLUME:          >= min_volume                        (data gate — continue)
  5. PRICE TREND:     flat or rising                       (data gate — continue)
  6. BET SIZE:        Kelly size >= min_bet                (data gate — continue)
```

#### Minimum edge gate (new, replaces min_ev)

`P − market_price >= min_edge` (default 0.10) replaces the old `EV >= min_ev` (0.05) gate.

The old EV gate required only that our model assigns 5% more probability than the price implies
(`p >= price × 1.05`). The new edge gate requires a 10 percentage-point absolute gap above the
market price (`p >= price + 0.10`). At price=0.40 this means p ≥ 0.50; at price=0.35, p ≥ 0.45.

EV is still computed and stored in the cycle record for diagnostics, but no longer gates entry.

#### `get_hrrr()` → `get_gfs()` (renamed + global)

The function was calling `models=gfs_seamless` while named `get_hrrr()`. It was also restricted
to US-only cities (`if loc["region"] != "us": return {}`), leaving EU markets with zero second-model data.

**Fix:** Renamed `get_gfs()`. Removed the US-only restriction. Uses the city's native temperature
unit (Fahrenheit for US, Celsius for EU). Forecast window extended from 3 to 7 days to match ECMWF.

Forecast snapshots still store GFS data under the key `"hrrr"` for backward compatibility with
`run_calibration()` and existing market files.

#### `get_metar()` — retry added

Added 3× retry with 2-second sleep between attempts. The function previously returned None on
the first timeout or empty response.

#### Cycle records — new diagnostic fields

Every cycle now stores:
- `ecmwf_at_entry`: ECMWF temperature at entry time
- `gfs_at_entry`: GFS temperature at entry time
- `model_delta`: `|ECMWF − GFS|` at entry time (for post-mortem analysis)
- `edge`: `P − market_price` at entry time

Every forecast snapshot now stores `model_delta` alongside the existing fields.

### New Config Parameters

| Parameter | Default | Description |
|---|---|---|
| `max_model_delta` | 2.0 | Block entry if \|ECMWF − GFS\| exceeds this (°F for US, °C for EU) |
| `min_edge` | 0.10 | Minimum (P − market_price) required to enter |

### Removed Config Parameters

`min_ev` — replaced by `min_edge`. EV is still computed and logged but no longer gates entry.

### What Did Not Change

- Core strategy: confidence-first, highest-probability bucket, opportunity zone [0.25, 0.65]
- Take-profit at 35% ROI, stop-loss at 25% loss, trailing stop activates at +20%
- Kelly sizing, calibration (Bayesian sigma), auto-tuner
- Reconciliation, auto-resolution, re-entry logic
- All Polymarket API calls
- `bucket_prob()`, `calc_kelly()`, `bet_size()`, `calc_ev()` math functions
- Scan regions: US + EU

---

## v4.2 — Forecast-Exit Signal Fix (2026-04-14)

### Why This Change Was Made

Post-v4.1 analysis found that `forecast_changed` was still exiting positions at near-entry
prices — buying at 0.27 and selling at 0.24–0.26 after just 2–5 hours. Root cause:

**The probability-based exit condition (`new_p < min_confidence`) fires on routine
forecast noise.** With sigma=1.414 (the inverse-variance blend of two sigma=2.0 sources),
a US 1°F bucket's probability ceiling is only ~0.49. A routine 2°F day-to-day forecast
update drops `new_p` from 0.49 to ~0.32 — below `min_confidence=0.38` — triggering the
exit on any 1-cent price dip.

The original v4.0 spec (never correctly implemented) called for:
> "Forecast moves ≥ 2° outside the bet bucket"

This is a calibration-independent, temperature-based condition. It fires only when the
forecast has genuinely moved beyond the bucket's edge, not when normal sigma uncertainty
happens to straddle the threshold.

### Bug Fixed

#### `forecast_changed` — probability threshold too sensitive (critical)

**Old condition:** `new_p < min_confidence AND current_price < entry_price`
With `min_confidence=0.38` and `p_max≈0.49` for US 1°F buckets, any ~2°F forecast
shift crossed the threshold. 7 of 15 exits were `forecast_changed` losses.

**New condition:** `forecast is ≥ 2° outside bucket edge AND current_price < entry_price`
- Bounded bucket `[t_low, t_high]`: triggers if `forecast < t_low − 2` or `forecast > t_high + 2`
- Terminal "X or higher": triggers if `forecast < t_low − 2`
- Terminal "X or below": triggers if `forecast > t_high + 2`

The 2° buffer is in native units (°F for US, °C for EU). `new_p` is still computed
and logged for diagnostics, but no longer drives the exit decision.

### What Did Not Change

- All other exit logic (take-profit, stop-loss, trailing stop, resolution)
- Entry gates, re-entry gates, Kelly sizing
- Config parameters

---

## v4.1 — Bug-Fix Pass + Config Rebalance (2026-04-13)

### Why This Change Was Made

Post-launch analysis of the first 15 live trades found that every closed position
was a loss or breakeven. Root-cause investigation identified eight bugs, all of
which caused the bot to enter lower-quality positions than the v4 strategy
intended and to exit them too aggressively.

### Bugs Fixed

#### 1. `find_best_entry` — `min_confidence` gate was never applied (critical)

The function checked `EV >= min_ev` (a v3 gate) but never checked
`p >= min_confidence` (the v4 gate). Ten of fifteen live trades were entered
with `p < 0.50` despite `min_confidence = 0.50` in the config.

**Fix:** Added `if p < min_conf: return None` as the second strategy gate,
immediately after the opportunity zone check.

#### 2. `find_best_entry` — fell back to lower-probability buckets (critical)

When the highest-probability bucket failed a gate, the loop used `continue`
instead of `return None`. The bot would silently try lower-probability buckets
to force a trade — the exact anti-pattern v4 was designed to prevent.

**Fix:** Strategy gates (zone, confidence, EV) now call `return None` to skip
the market entirely. Data/liquidity gates (volume, trend, bet size) still use
`continue` because the bucket itself is valid even if data is temporarily thin.

#### 3. `bucket_prob` — terminal buckets missing ±0.5 expansion (high)

`"X°F or higher"` and `"X°F or below"` buckets did not apply the same ±0.5
degree expansion used by bounded buckets. This caused severe probability
underestimation at the boundary: the London `[16°C or higher]` trade was
scored at `p = 0.500` when the correct value was `p = 0.662`.

**Fix:** Applied `t_high + 0.5` and `t_low - 0.5` to both terminal bucket
types in `bucket_prob()`, matching the bounded-bucket logic.

#### 4. `forecast_changed` exit — fired on winning positions (high)

The exit triggered whenever `new_p < entry_p * 0.70` — a 30% relative drop.
Normal forecast variation crossed this threshold, causing the bot to exit
profitable or near-breakeven positions before they reached the take-profit
target. Six of thirteen closed trades exited this way.

**Fix:** The forecast exit now only triggers when **both** conditions hold:
(a) `new_p < min_confidence` — the model has genuinely lost conviction below
the entry threshold; and (b) `current_price < entry_price` — the position is
already losing. Winning positions are never cut by forecast noise; the
take-profit handles them.

#### 5. `evaluate_reentry` — used `min_ev` instead of `min_confidence` (high)

The re-entry gate said *"Fresh P >= min_confidence on the same bucket"* in its
docstring but checked `calc_ev(p, price) < min_ev` in the code — the same v3
migration gap as Bug 1.

**Fix:** Replaced the EV check with `if p < _strategy["min_confidence"]: return None`.

#### 6. Tuner `min_confidence` bands — cumulative, converged to minimum (medium)

Each trade was added to **all** bands where `p >= threshold`. The 0.40 band
always accumulated the most data and dominated the signal, pushing the tuner
toward the lower bound regardless of which confidence level actually performed
best.

**Fix:** Replaced cumulative bands with exclusive ranges
`(0.35–0.40, 0.40–0.45, …)` so each trade lands in exactly one band. The
minimum required trades per band was reduced from 5 to 3 to account for the
smaller per-band sample sizes.

#### 7. Reconciled cycles — `p = None` silenced forecast exit (medium)

Reconciled cycles stored `"p": None`. In the forecast exit check,
`entry_p = pos.get("p") or 0.0` evaluated to `0.0`, making
`new_p < 0.0 * 0.70 = 0` always false. Reconciled positions could never be
exited via the relative-drop rule — only the hard floor `new_p < 0.10` applied.

**Fix:** Both reconcile blocks now compute and store the current
`bucket_prob()` as the cycle's `p`. The `monitor_positions` reconciler uses
the last closed cycle's `forecast_temp` and `sigma` as a best-effort estimate
until the next full scan refreshes it.

#### 8. `load_all_markets()` called 40× per scan (low)

Called inside the inner city/date loop (10 cities × 4 dates) for portfolio cap
checks — reading all market files from disk on every iteration.

**Fix:** Hoisted to a single call before the outer city loop. The cache is
updated when a new position opens so cap checks remain accurate within the scan.

### Config Changes

Three parameters were recalibrated to match the mathematical realities of the
markets being traded:

| Parameter | Old | New | Reason |
|---|---|---|---|
| `min_confidence` | 0.50 | **0.38** | A US 1°F bucket at `sigma=2°F` has a hard probability ceiling of 0.383. Setting the threshold at 0.50 made all US narrow-bucket entries mathematically impossible. EU 1°C buckets peak at ~0.60 and pass easily. |
| `min_ev` | 0.10 | **0.05** | At `min_ev=0.10` the maximum achievable EV for a US 1°F bucket (~9.4% at `price=0.35`) barely clears the threshold and fails entirely above `price=0.36`. Reducing to 0.05 admits entries with a small but real positive edge. The EV gate still acts as a sliding floor: at `price=0.55` it effectively requires `p >= 0.578`. |
| `stop_loss_pct` | 0.50 | **0.75** | `stop=0.50` means losing 50% to target a 35% gain — a negative R:R requiring a 58.8% win rate to break even. `stop=0.75` (25% max loss) gives a 35:25 R:R, breakeven at 41.7%, and positive EV at `p >= 0.45`. |

### Goal Update

The original 14-day deadline is removed. The goal remains doubling the balance
($30 → $60), but profitability is the priority over timeline. The reduced trade
frequency from the confidence gate (fewer but higher-quality entries) is
intentional and expected.

### What Did Not Change

- Entry logic structure: highest-probability bucket first, opportunity zone
  `0.25–0.65`, single `take_profit_roi = 0.35` exit rule
- Kelly sizing, trailing stop, stop-loss mechanics
- Calibration, reconciliation, and auto-resolution logic
- Scan regions: US + EU

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
