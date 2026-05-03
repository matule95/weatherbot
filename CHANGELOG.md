# WeatherBet — Changelog

This file is the authoritative history of the bot's strategy and architecture.
It is designed to be both human-readable and fed as context to an AI assistant.

Each version entry documents: **what changed**, **why it changed**, and **what the
current state of the system is**. The most recent version at the top is the
canonical description of how the bot currently works.

---

## v4.15 — Sigma Audit + Per-City Calibration Bootstrap (2026-05-03)

### Why This Change Was Made

After 48 hours of v4.13/v4.14 sim data — 45 trades, 38 closed, win rate 47.4%, realized PnL **−$4.16** — the bot stopped showing edge. v4.13's first 24h was strongly profitable (+$19.04, WR 57%), but the next 24h was equally negative (−$23.20, WR 35%). Reversion to mean rather than a sustained edge.

A data audit cross-checking every closed cycle's forecast against actual WU observations revealed the root cause was deeper than entry gates:

#### Forecast errors are 3-7°, not 0.85-1.5°

| City | Date | Forecast | Actual | Error |
|---|---|---|---|---|
| Lucknow | 5/3 | 37.1°C | **32°C** | **5.1°C** |
| Shanghai | 5/4 | 23.1°C | **16°C** | **7.1°C** |
| Tokyo | 5/4 | 26.0°C | **21°C** | **5.0°C** |
| Atlanta | 5/2 | 65.2°F | **72°F** | **6.8°F** |
| Sao-paulo | 5/3 | 21.9°C | 19°C | 2.9°C |
| (15 C-city samples) | | | | RMSE = **3.06°C** |
| (4 F-city samples) | | | | RMSE = **3.59°F** |

Configured sigmas: `SIGMA_C = 0.85`, `SIGMA_F = 1.5`. **Actual forecast errors are roughly 3.5x larger than the bot believed.**

#### What this meant in practice

With sigma=0.85 for a 1°C bucket centered on the forecast, the bot computed bucket probability ≈ 0.44 (max possible). With true sigma=3.0, the actual probability of that same bucket resolving YES is closer to 0.13. The bot was systematically:

- **Inflating edge estimates** — believing +0.20 edge when actual edge was zero or negative
- **Oversizing Kelly bets** — high "edges" justified large bets, amplifying losses
- **Letting marginal trades through gates** — min_confidence=0.42 was easy to clear at the wrong sigma but mathematically appropriate at the right sigma

The 47% observed win rate is exactly what you'd expect when "edge" calculations are illusory: random-walk performance.

### Why Some Trades Still Worked

Tokyo's only big win was on **`≥25°C` (open-ended top bucket)** at +$22 — open-ended buckets cover wide temperature ranges and are robust to forecast errors of 3-5°. Single-bucket entries (1° wide) require sigma well below the bucket width to be reliably correct. Historical winners (Tokyo, atlanta exits-before-resolve) shared this pattern: either open-ended buckets, or fast forecast_diverged exits caught early movement.

### What Changed

#### Config Changes

| Parameter | Old | New | Reason |
|---|---|---|---|
| `sigma_f` | 1.5 (hardcoded) | **2.5** (config) | Match observed F-city forecast errors (~3.6° RMSE). Used as fallback prior; per-city calibration overrides. |
| `sigma_c` | 0.85 (hardcoded) | **2.0** (config) | Match observed C-city forecast errors (~3.0° RMSE). Used as fallback prior; per-city calibration overrides. |
| `min_confidence` | 0.42 | **0.50** | At new sigma_c=2.0, single-bucket entries cap at p≈0.20 — unreachable under 0.50. **Intentionally bans single-bucket trades**, the dominant loss source. Open-ended bucket entries still pass when forecast is at the market's extreme. |
| `kelly_fraction` | 0.50 | **0.25** | Safety damper while new calibration is validated. Raise back toward 0.50 once 30+ closed cycles confirm the recalibration produces real edge. |

#### Code Changes

**`bootstrap_wu_calibration()` now seeds per-city sigma alongside bias** ([bot_v4.py:2442](bot_v4.py#L2442)). Previously it computed only `<city>_<source>_bias` from the 90-day WU + Open-Meteo archive sample; now it also computes `<city>_<source>` (the sigma key already read by `get_sigma()`) using the same Bayesian-blended MAE formula as `run_calibration()`.

The bootstrap gate was upgraded to detect when bias or sigma is missing (previously checked bias only). On next startup, every active city will have its sigma computed from 90 days of historical forecast-vs-WU error.

`SIGMA_F` and `SIGMA_C` constants moved from hardcoded values to `_cfg.get("sigma_f", 2.5)` / `_cfg.get("sigma_c", 2.0)` ([bot_v4.py:134-135](bot_v4.py#L134-L135)). They're still used as Bayesian priors in calibration; with PRIOR_WEIGHT=3 and n=90 sample days, the prior contributes <5% to the final per-city sigma, so the constants are mostly safety nets.

### Why Existing Data Is Preserved

- **Open positions**: continue with their stored entry parameters (entry_price, sigma at entry). Exit logic doesn't depend on the live sigma, so behavior is unchanged for them.
- **`calibration.json`**: 90 days of bias data preserved. Bootstrap will *add* sigma keys without modifying existing bias entries.
- **`sim_state.json`**, **`sim_markets/*.json`**: untouched. Cumulative PnL, snapshots, and history all preserved.

The only thing that changes is the gate behavior for *new* entries on the next scan after restart.

### What Did NOT Change

- **Trailing logic** (v4.14: 0.20 distance, 1.25 activation) — working correctly per closed cycles.
- **`forecast_diverged()` tolerance** (0.5) — bounded-loss thesis still valid; the issue was upstream (entries that shouldn't have happened), not the exit.
- **`take_profit_roi`** (0.50) — TP wins still capture meaningful profit.
- **Entry gates other than min_confidence** — model_delta, crowd_gap, min_edge unchanged. Wider sigma already filters most marginal entries via the confidence floor.
- **Bias correction** — the residual analysis showed bias values are reasonable; bias correction itself isn't the problem. The problem was the bot believed its bias-corrected blend with too-tight uncertainty.

### Expected Impact

Entry volume drops sharply — single-bucket C-city trades (the majority of v4.13/v4.14 entries) are now blocked. Expect:

- 5-10 entries/day instead of 17-20
- Most entries on open-ended buckets where forecast lands at market extreme
- Average bet size halved by the kelly_fraction reduction
- WR should rise above 55% if the new calibration is honest
- Daily PnL swing reduced (smaller bets, fewer trades), but net PnL trajectory should turn positive

This trades *learning speed* (more trades = faster signal) for *capital preservation* (smaller bets, fewer false-edge trades). The bot will need 5-10 days to produce 30+ closed cycles for the next confidence-level reassessment.

### Followup To Watch

1. **Bootstrap output** — on first restart, look for `[BOOTSTRAP] {city} {source}: sigma={value} (n=...)` lines confirming per-city sigmas are seeded.
2. **Per-city sigma values** — should land in the 1.5-3.5 range for most cities. Lucknow specifically should show wider sigma reflecting its high observed errors.
3. **WR by entry type** — open-ended bucket entries should win >55%. If they don't, the issue is calibration depth (recency), not just sigma scale.
4. **Tuner re-enable** — keep `tune_enabled: false` until 30+ closed cycles under v4.15 confirm the recalibration is stable.

### Current System State

```python
# Entry gates (v4.15):
min_confidence       = 0.50    # was 0.42 — banned single-bucket entries by design
max_model_delta_f    = 2.0
max_model_delta_c    = 2.0
max_crowd_gap_buckets = 1.5
min_edge             = 0.10

# Forecast uncertainty (v4.15):
sigma_f              = 2.5     # was 1.5 hardcoded — fallback prior; per-city calibrated
sigma_c              = 2.0     # was 0.85 hardcoded — fallback prior; per-city calibrated
# Per-city sigma seeded from 90 days of WU + Open-Meteo archive on bootstrap

# Position sizing (v4.15):
kelly_fraction       = 0.25    # was 0.50 — safety damper during calibration validation
max_bet              = 40.0

# Exit logic (unchanged from v4.14):
forecast_diverged(forecast, t_low, t_high, tolerance=0.5)
take_profit_roi      = 0.50
trailing_activation  = 1.25
trailing_distance    = 0.20
```

---

## v4.14 — Trailing-Stop Tightening + Constants Move to Config (2026-05-02)

### Why This Change Was Made

After 24h of v4.13 sim data — 31 trades opened, 21 closed, win rate 57.1%, realized PnL +$19.04 — one specific failure pattern stood out: **all 3 trailing-stop exits were losers** (avg PnL −$2.45), totaling −$7.36 of drag.

Forensic detail on the trailing-stop closes:

| City | Entry | Peak | Calc'd trail (peak×0.70) | Actual exit | PnL |
|---|---|---|---|---|---|
| london 5/2 | $0.237 | $0.298 | $0.209 | $0.163 | −$3.07 |
| toronto 5/2 | $0.240 | $0.320 | $0.224 | $0.190 | −$2.72 |
| toronto 5/3 | $0.240 | $0.320 | $0.224 | $0.220 | −$1.57 |

Two issues:

**1. The math at 0.30 trail is structurally lossy**: Trailing arms at +25% (entry × 1.25). With a 30% trail, the stop sits at peak × 0.70 = entry × 0.875 → exit at **−12.5% net** in the worst case. A position can climb to +25%, the trail arms, a routine forecast wobble drops price back toward entry, and we exit at a guaranteed loss. We were converting would-be flat trades into structural losers.

**2. Polymarket bestBid step volatility amplifies the problem**: Two of three exits fired well below the calculated trail (15–22% below) because thinly-traded bucket markets have wide bid-ask spreads — bestBid can step from $0.21 to $0.16 on a single trade. This isn't fixable by tightening alone, but a tighter trail means the stop is closer to the peak when bid steps catch us.

### Why Sim and Live Now Look Identical (Sim-Mode Audit)

A side-investigation of why sim was producing these "exit-below-trail" cases ruled out a sim-fidelity issue: `monitor_positions()` already runs every 5 minutes in **both** sim and live modes. The only sim-specific skip is the orphan-reconcile section ([bot_v4.py:1911](bot_v4.py#L1911)), which is correct because sim has no on-chain state to reconcile. Sim fetches live `bestBid` from Polymarket every 5 minutes, fires exits on those prices, and behaves identically to live for entry/exit decision-making. The only deliberate sim/live divergences are:

- order placement (paper response vs CLOB call)
- token/wallet balance source (tracked JSON vs on-chain)
- orphan reconciliation (no chain state in sim)

Behaviorally, sim is faithful. The trailing-stop late-exits are real Polymarket microstructure, not simulation artifact, so they will happen in live too. Tightening trail addresses both.

### Config Changes

| Parameter | Old | New | Reason |
|---|---|---|---|
| `trailing_distance` | 0.30 (hardcoded) | **0.20** (config) | Locks in profit at peak instead of giving back to loss. At entry×1.25 peak: −12.5% → 0% (breakeven). At entry×1.40 peak: −2% → +12% locked |
| `trailing_activation` | 1.25 (hardcoded) | 1.25 (config, unchanged) | Moved to config for tunability without code change |

### What Did NOT Change

- **`take_profit_roi = 0.50`** — 3/3 TPs in v4.13 captured +$11.74 avg. Lowering would shrink the big wins; sample too small to act on.
- **`forecast_diverged()` tolerance (0.5°)** — 14 of 15 forecast_diverged exits were near-flat (+$0.24 net combined). Only the buenos-aires 5/2 outlier (−$9.07, gapped to $0.005 at exit) was a tail event. Tightening would produce more exits on noise; the central tendency is correct.
- **All entry gates from v4.13** — producing the right volume at the right edge distribution. Win rate 57% beats expected calibrated 43%, so the model is conservative. No reason to touch.
- **Bias correction logic** — Lucknow 2026-05-03 is currently −63.9% with bias-corrected forecast still in-bucket while raw ECMWF/GFS show 38°C. This will resolve Tuesday and tell us whether the bias correction is right or wrong. Don't touch until we have data.

### Expected Impact

On the v4.13 sample, replacing each trailing-stop exit at the new 0.20 trail:
- london 5/2: peak $0.298 → trail $0.238 → at minimum +0% (vs −31% actual). Probably exits at +5–10%.
- toronto 5/2: peak $0.320 → trail $0.256 → at minimum +6.7% (vs −20% actual). Probably +5–10%.
- toronto 5/3: peak $0.320 → trail $0.256 → at minimum +6.7% (vs −8% actual). Probably +5–10%.

Estimated swing: ~+$10–12 of PnL recovered on the ~3 trailing exits per 24h. At v4.13's pace of 21 closes/day, this is +$10/day improvement. Compounds with v4.13's existing +$19/day baseline.

### Current System State

```python
# Entry gates (unchanged from v4.13):
min_confidence       = 0.42
max_model_delta_f    = 2.0
max_model_delta_c    = 2.0
max_crowd_gap_buckets = 1.5
min_edge             = 0.10
min_entry_price      = 0.10
max_entry_price      = 0.65

# Exit logic (v4.14):
forecast_diverged(forecast, t_low, t_high, tolerance=0.5)
take_profit_roi      = 0.50
trailing_activation  = 1.25 × entry           # now in config
trailing_distance    = 0.20 of peak            # was 0.30, now in config

# Position sizing (unchanged):
kelly_fraction       = 0.50
max_bet              = 40.0
max_open_positions   = 15
```

---

## v4.13 — Entry-Gate Recalibration for Volume (2026-05-01)

### Why This Change Was Made

The v4.12 sim run that followed the strategy overhaul (200 → 160 starting balance, sim reset 2026-04-30) produced **1 trade in 22 hours** — Tokyo 2026-05-02, which is sitting at +29% unrealized. Every other market was rejected at one of the entry gates. With zero closed cycles to learn from, the v4.12 design (forecast_diverged exit, bounded loss) cannot be validated.

A full audit of all 56 active sim markets across the 21-snapshot history identified the dominant blocker as **a math conflict between `min_confidence = 0.50` and the bucket-probability ceiling**:

- `sigma_C = 0.85` and 1°C-wide single buckets → maximum achievable bucket probability is `norm_cdf(0.5/0.85) − norm_cdf(−0.5/0.85) ≈ 0.444`
- A `min_confidence` floor of 0.50 therefore mathematically banned every single-bucket entry
- Only open-ended top/bottom buckets (`X°C-or-higher`, `X°C-or-below`) could ever clear the floor — that's why Tokyo got in (open `≥25°C` bucket at p=0.83) and nothing else did

Two additional gates were over-tuned for the v4.11 loss model and not re-evaluated after v4.12 introduced bounded loss:

- `max_model_delta_c = 1.1` was blocking 29 of 56 markets (over half), most for routine ECMWF/GFS spread of 1.2-1.8°C — that's normal model uncertainty, already accounted for in sigma
- `max_crowd_gap_buckets = 1.0` was blocking 14 markets — but by construction, high crowd-gap = high mispricing = high edge. The blocked trades had edges of +0.25 to +0.33, the largest in the eligible set

### Config Changes

| Parameter | Old | New | Reason |
|---|---|---|---|
| `min_confidence` | 0.50 | **0.42** | Math fix — single-bucket entries cap at ~0.44, so 0.50 banned them entirely. v4.12's bounded-loss exit makes the old "67% breakeven" math obsolete. 0.42 sits just under the ceiling |
| `max_model_delta_c` | 1.1 | **2.0** | Conceptual match with F's 2.0 cap. Routine ECMWF/GFS spread is 1.2-1.8°C and isn't a "wrong forecast" signal. Blocked 29/56 markets at the old value |
| `max_crowd_gap_buckets` | 1.0 | **1.5** | The single biggest profit lever. Unblocks the highest-edge trades specifically (+0.25 to +0.33). v4.12's forecast_diverged() will exit at bounded loss if the crowd turns out to be right |

### Why These Three (and Not Others)

A 6-config historical sweep was run against every snapshot of every market. Results (number of distinct markets that would have produced an entry over the 21h sim window):

| Config | Entries | Avg edge |
|---|---|---|
| Current v4.12 (0.50 / 1.1 / 1.0 / 0.10) | 0 | — |
| min_conf 0.42 only | 14 | +0.174 |
| + Δc 1.1→2.0 | 16 | +0.173 |
| **v4.13 (0.42 / 2.0 / 1.5 / 0.10)** | **27** | **+0.211** |
| + min_edge 0.10→0.07 | 28 | +0.202 (diluted) |
| + min_conf 0.42→0.40 | 31 | +0.191 (diluted) |

Lowering `min_edge` to 0.07 only adds 1 marginal trade and waters down avg edge. Lowering `min_conf` to 0.40 adds 3 lower-confidence trades that risk extra losses without proportional volume. The 27-entry / +0.211-avg-edge config is the local optimum for entry quality × volume.

### What Did NOT Change

- **`min_edge = 0.10`** — kept. Lowering to 0.07 was tested and adds one trade for diluted avg edge.
- **`max_model_delta_f = 2.0`** — kept. Already at the conceptual equivalent of the new C value.
- **`forecast_diverged()` tolerance, trailing stop distance, take_profit_roi, kelly_fraction, MAX_BET, position caps** — all unchanged. v4.13 is purely a gate recalibration, not a strategy change.
- **Tuner stays disabled.** Re-enable after the v4.13 strategy has produced 30+ closed cycles to confirm calibration before letting the tuner adjust.

### Expected Impact

Capital constraints (sim balance $160, max_bet $40, ~6 concurrent $25 positions) cap actual entries at 5-8 open at a time, not all 27 historical eligibles. Expect the bot to fill its position book within the first scan cycle and then trickle-replace as positions close.

If the model is calibrated (55% win rate at edges +0.10 to +0.33), EV math: `0.55 × $12.50 (TP) + 0.45 × −$10 (forecast_diverged exit) ≈ +$2.40 per trade`. At ~25 entries/day → ~$60/day → ~2.5 days to double the sim bankroll ($160 → $320).

The central assumption being tested: **does `forecast_diverged()` actually bound losses as designed when forecasts move outside the bucket?** 18 of 57 markets had ≥1.5° forecast swings during the sim window, so this exit will fire often. If observed loss-per-stop is significantly worse than ~40% of bet size, the v4.12 thesis is wrong and we'll need to revisit min_confidence.

### Current System State

```python
# Entry gates (v4.13):
min_confidence       = 0.42    # was 0.50
max_model_delta_f    = 2.0
max_model_delta_c    = 2.0     # was 1.1
max_crowd_gap_buckets = 1.5    # was 1.0
min_edge             = 0.10
min_entry_price      = 0.10
max_entry_price      = 0.65
min_volume           = 100

# Exit logic (unchanged from v4.12):
forecast_diverged(forecast, t_low, t_high, tolerance=0.5)
take_profit_roi      = 0.50
trailing_activation  = 1.25 × entry
trailing_distance    = 0.30 of peak

# Position sizing (unchanged):
kelly_fraction       = 0.50
max_bet              = 40.0
max_open_positions   = 15
max_positions_per_date = 5
```

---

## v4.12 — Forecast-Divergence Exit + Unit-Aware Gates + Tuner Rewire (2026-05-01)

### Why This Change Was Made

The first ~50-trade sim run on the v4.11 strategy lost 53% of starting balance ($200 → $94.17, peak $239.55). Root-cause analysis on the closed cycles plus a deep trace of one stop-out (Munich 2026-04-29) identified four distinct architectural failures, each independently sufficient to make the strategy unprofitable.

**Failure 1 — `stop_loss_pct = 0.625` is fictional in this market (critical).**

The R:R math underpinning v4.11 (50% gain vs 37.5% loss → ~43% breakeven win rate) assumes orderly price decline. Weather-bucket prices on Polymarket do not decline orderly: they gap toward $0 at resolution if the actual temperature lands in a different bucket. Munich on 2026-04-29 entered the 17°C bucket at $0.31 with a ~$0.19 stop. The market priced 18°C at $0.58 throughout the day, the actual high was 18°C, and the bucket exit price snapped from $0.30 to $0.0005 between two scan snapshots — the "stop" captured a −99.8% loss, not −37.5%. Effective R:R is closer to **+50% : −100%**, which lifts the breakeven win rate to ~67%, not 43%. Sim observed 50% win rate and 50% stop rate, which guaranteed the drawdown.

**Failure 2 — auto-tuner crushed `take_profit_roi` from 0.50 to 0.22 (critical).**

`tune_strategy()` lowered TP whenever `stop_rate > 0.40`. Sim observed 50% stop rate, so the tuner ratcheted TP down ~10 times. With TP=0.22 and effective loss=−100%, breakeven jumps to **82% win rate** — mathematically unreachable. The down-branch was responding to a symptom (bad entries) by amplifying a different problem (shrinking wins).

The kelly_fraction branch was also blind: it filtered for `close_reason == "resolved"` and there were zero resolved-to-outcome cycles in the dataset (every position exited via take-profit or stop before resolution day). Kelly was effectively frozen.

**Failure 3 — `max_model_delta = 2.0` and `max_crowd_gap = 4.0` are unit-naive (high).**

The same constant was used for °F (1°F-wide buckets) and °C (1°C-wide buckets). 2°C is 80% larger in physical terms than 2°F. EU markets received a much more permissive disagreement gate than US markets despite using the same bucket structure. Munich at entry: market-implied temperature ≈ 17.4°C, model forecast 16.6°C → crowd gap 0.8°C. The gate threshold was 4.0°C. The crowd was screaming "18°C" through nearly a full bucket of disagreement and the gate let it through.

**Failure 4 — `min_confidence = 0.40` is below the actual profitable floor (high).**

13 of the recent 20 cycles had `p ∈ [0.40, 0.45)`. Avg PnL: **−$2.51 per cycle**. The [0.45, 0.50) band was barely positive (avg −$1.33). With effective loss-per-stop near 100%, the math says you need calibrated win rate ≥ 67%, which means model `p` must be well above 0.50 even when calibrated. Entries at 0.40 cannot win in expectation regardless of edge.

### What Changed

#### `forecast_diverged()` replaces price-based stop_loss as the loss-side exit (critical)

New helper at module scope:

```python
def forecast_diverged(forecast_temp, t_low, t_high, tolerance=0.5):
    if forecast_temp is None or t_low is None or t_high is None:
        return False
    if t_low == -999:
        return forecast_temp > t_high + tolerance
    if t_high == 999:
        return forecast_temp < t_low - tolerance
    return forecast_temp < t_low - tolerance or forecast_temp > t_high + tolerance
```

A position exits when the bias-corrected forecast (which already includes WU `running_max` blended at low sigma when WU is up) moves more than 0.5 units outside the bucket's rounding window. This is the actual recoverable signal: by the time the corrected forecast clears the window, the bucket is unlikely to win and price will continue to decay. Critically, the exit fires while liquidity still exists — not after a resolution gap.

**Scan-loop exit (`scan_and_update`):** three-way trigger — `take_profit_roi` (price ≥ entry × 1.50), `forecast_diverged`, or `trailing_stop` (price ≤ trailing level, only after trailing has activated). Pre-trailing positions can no longer fire a price-based stop. New `close_reason = "forecast_diverged"`.

**Monitor-loop exit (`monitor_positions`):** two-way trigger — `take_profit_roi` or `trailing_stop`. The monitor loop has no fresh forecast; the divergence check runs only in the scan loop where the corrected forecast is recomputed. The `stop_loss` reason path is removed entirely.

`STOP_LOSS_PCT` and the `stop_price` field are kept as the seed value for the trailing stop once a position has gone profitable. They are no longer load-bearing on the loss side.

#### Tuner rewire (critical)

Two changes inside `tune_strategy()`:

1. **TP-down branch removed.** `take_profit_roi` is now a one-way ratchet — it can only increase, and only when `profit_rate > 0.60 AND stop_rate < 0.20`. The previous "lower TP when stop_rate is high" response is documented in the function docstring as the wrong fix for this market.

2. **`resolved_only` filter dropped from kelly tuning.** Kelly now learns from any closed cycle with realised PnL, not only from cycles that resolved to outcome. The previous filter left kelly permanently inactive whenever the bot was take-profit / stop-active enough to never hold to resolution.

`stop_n` (used by the surviving TP-up branch) now also counts `forecast_diverged` exits alongside `stop_loss` and `trailing_stop`, so the signal stays meaningful under the new exit semantics.

`tune_enabled` is set to **false** in config until 30+ closed cycles accumulate under the new strategy. The damaged `strategy.json` was deleted as part of the reset.

#### Unit-aware entry gates

```python
MAX_MODEL_DELTA_F   = _cfg.get("max_model_delta_f", 2.0)
MAX_MODEL_DELTA_C   = _cfg.get("max_model_delta_c", 1.1)
MAX_CROWD_GAP_BUCKS = _cfg.get("max_crowd_gap_buckets", 1.0)
```

`find_best_entry()` resolves the cap from the market's unit at call time:

- Gate 0 (model agreement): `model_delta > model_delta_cap(unit)`. 1.1°C ≈ same number of buckets as 2.0°F.
- Gate 0b (crowd gap): expressed in bucket-widths instead of absolute degrees. `1.0` = "the crowd is pricing one full bucket away from us." Replaces the previous `2 × max_model_delta` formula that produced a 4-bucket-wide threshold.

Munich case re-evaluated under the new gates: crowd_gap 0.8 < 1.0 (still passes — Tier 2 alone does not block Munich). The combination of `forecast_diverged` exit + `min_confidence ≥ 0.50` + per-city circuit breaker (below) is what addresses the Munich loss profile.

The legacy `MAX_MODEL_DELTA` symbol is retained as an alias for `MAX_MODEL_DELTA_F` so log lines and existing references continue to import. Per-call decisions resolve through `model_delta_cap(unit)`.

#### Per-city circuit breaker

```python
def city_recently_lost(city_slug, all_markets, now, window_hours=None, limit=None):
    # Returns True if >= limit negative-PnL closures for this city in the
    # last window_hours, scanning across all that city's markets (not just
    # one resolution date).
```

Wired into the entry path immediately after the portfolio caps in `scan_and_update`. Default thresholds:

- `city_loss_limit = 2`
- `city_loss_window_hours = 24`

Munich on 2026-04-29 took three stop_losses within ~9 hours costing $25.98. Under this gate, the second loss pauses Munich entries for 24h — saving ~$13.

#### Config changes

| Parameter | v4.11 | v4.12 | Reason |
|-----------|-------|-------|--------|
| `min_confidence` | 0.40 | **0.50** | [0.40, 0.45) band averaged −$2.51/cycle in sim; model `p` must clear 0.50 to be profitable under the new R:R |
| `min_edge` | 0.07 | **0.10** | Compensates for ~100% effective loss-per-stop |
| `max_model_delta` | 2.0 | **removed** | Replaced by `max_model_delta_f` (2.0) + `max_model_delta_c` (1.1) |
| `max_model_delta_f` | — | **2.0** | New |
| `max_model_delta_c` | — | **1.1** | New — same number of buckets as °F |
| `max_crowd_gap_buckets` | — | **1.0** | New — replaces implicit `2 × max_model_delta` |
| `city_loss_limit` | — | **2** | New |
| `city_loss_window_hours` | — | **24** | New |
| `tune_enabled` | true | **false** | Tuner had pushed TP from 0.50 → 0.22; off until cycles accumulate |
| `take_profit_roi` | 0.50 | 0.50 (unchanged in config; restored from broken-tuner 0.22) |  |

`stop_loss_pct` is unchanged (0.625) but its `_notes` entry now describes it as legacy — the price-based stop is no longer the loss-side exit.

#### Reset

- `weather_bot_data/sim_state.json` — deleted.
- `weather_bot_data/strategy.json` — deleted (held the broken tuned values).
- `weather_bot_data/sim_markets/*.json` — 96 files deleted.
- `weather_bot_data/calibration.json` — **kept**. Bias data is independent of strategy and represents 89-90 samples per city.
- `weather_bot_data/markets/` — kept (real-money history).

### What Did Not Change

- Forecast pipeline: ECMWF + GFS bias correction, WU `running_max` blending, sigma calibration. Untouched.
- `bucket_prob()`, `calc_kelly()`, `calc_ev()`, `bet_size()`. Untouched.
- Trailing stop: still activates at +25%, still trails 30% below peak. The trailing logic correctly protects winners and the existing constants are unchanged.
- Re-entry gate structure (`evaluate_reentry`): still requires last cycle profitable, fresh `p ≥ min_confidence`, current price below last exit, etc. Now also gated by the new model-delta cap (unit-aware) and the new per-city circuit breaker.
- `min_confidence` tuner band-selection logic — kept intact, just no longer running while `tune_enabled = false`.
- All Polymarket CLOB API calls, reconciliation, sim-mode plumbing, scan/monitor cadence, scan regions.

### Current System State After v4.12

```
Loss-side exit:    forecast_diverged (>0.5 units outside bucket's rounding window)
                   No price-based stop. Trailing stop still active for winners.
Take-profit:       +50% ROI (entry × 1.50)
Trailing stop:     activates at +25%, trails 30% below peak
Entry gates:       model_delta <= 2.0°F / 1.1°C  (unit-aware)
                   crowd_gap   <= 1.0 bucket-widths  (unit-aware)
                   price       in [0.10, 0.65]
                   min_edge    >= 0.10
                   confidence  >= 0.50
                   volume      >= MIN_VOLUME
                   trend       flat or rising
                   bet size    >= $1.00
Re-entry:          all entry gates + last cycle profitable + price < last exit
City circuit:      pause city for 24h after 2 negative-PnL exits
Tuner:             disabled (re-enable after 30+ new cycles)
Sim balance:       reset to $200 (sim_state.json deleted)
```

The strategy is structurally honest about how this market behaves: losses are not capped at 37.5%; they are capped at "exit price when the model has changed its mind." Entry quality is the load-bearing variable. The remaining tuner branches (kelly, min_confidence band selection, TP-up ratchet) are non-destructive and can be re-enabled once new-strategy data exists.

---

## v4.11 — 50% ROI Target + Trailing Stop Reactivation + R:R Rebalance (2026-04-22)

### Why This Change Was Made

The previous `take_profit_roi = 0.65` configuration had two structural problems:

**Problem 1 — `max_entry_price = 0.75` was unreachable at 65% TP.**

Polymarket shares cap at $1.00. The take-profit condition is `entry × (1 + take_profit_roi)`. At 65% TP, any entry above `$1.00 / 1.65 = $0.606` can mathematically never reach the target. With `max_entry_price = 0.75`, the bot was permitted to enter trades at prices where the take-profit was structurally impossible.

**Problem 2 — `TRAILING_ACTIVATION = 1.50` was dead code at 65% TP.**

The trailing stop activates at `entry × 1.50`. The take-profit fires at `entry × 1.65`. Price must pass through the trailing activation level to reach the TP — which means the trailing stop always had a chance to fire first, making the hard TP ceiling unreachable in most scenarios. The two exits competed instead of cooperating.

Lowering TP to 50% fixes Problem 1 (new entry ceiling: $0.667) but creates an identical dead-code issue at the same activation level: both TP and trailing would fire at exactly `entry × 1.50`. The trailing stop would still be non-functional.

**R:R degradation at 50% TP with unchanged SL.**

Keeping `stop_loss_pct = 0.50` (50% max loss) at a 50% TP target produces a 1:1 R:R — breakeven requires a 50% win rate. The prior design at TP=65%/SL=50% gave a 1.3:1 R:R (breakeven at 43.5%). The stop-loss must tighten to restore a comparable R:R.

### What Changed

#### `take_profit_roi`: 0.65 → 0.50

Target ROI reduced. At 50%, a trade entered at $0.40 takes profit at $0.60. Maximum viable entry price is now $1.00 / 1.50 = $0.667.

#### `max_entry_price`: 0.75 → 0.65

Corrected to match the 50% TP ceiling. Entries above $0.667 cannot reach the take-profit target. Set to $0.65 with a small buffer.

#### `stop_loss_pct`: 0.50 → 0.625

Stop fires at 62.5% of entry price — a 37.5% max loss per cycle instead of 50%. This restores a comparable R:R to the original design:

| Config | TP | Max loss | R:R | Breakeven WR |
|--------|----|----------|-----|--------------|
| Previous | 65% | 50% | 1.30:1 | 43.5% |
| **v4.11** | **50%** | **37.5%** | **1.33:1** | **~43%** |

#### `min_edge`: 0.05 → 0.07

Raised from 5pp to 7pp. At 1.33:1 R:R with ~43% breakeven win rate, marginal edges are insufficient. Raising the floor filters entries where the bot's probability advantage over the market is too thin to support that win rate requirement.

#### `TRAILING_ACTIVATION` (code): 1.50 → 1.25

With TP firing at `entry × 1.50`, the trailing stop must activate *before* that level to be functional. Set to `1.25` (+25% above entry), giving the trail a working window between +25% and +50%.

Example at $0.40 entry:
- Trail activates at $0.50 (+25%)
- If price peaks at $0.58, trail stop locks at $0.406 (above breakeven)
- Take-profit fires at $0.60 (+50%)
- If price reaches $0.55 then reverses, trail stop at $0.385 captures a partial exit rather than a full stop-loss

#### `_notes` in config

All documentation strings updated to reflect the new TP, SL, entry ceiling, and min_edge values.

### What Did Not Change

- All entry gates, re-entry logic, Kelly sizing, calibration
- `TRAILING_DISTANCE = 0.30` — trail still follows 30% below peak
- Auto-tuner bounds — `take_profit_roi` tuner ceiling was already 0.50
- Scan regions, intervals, position limits, all other config parameters

### Current System State After v4.11

- **Take-profit**: +50% ROI (`entry × 1.50`)
- **Stop-loss**: −37.5% max loss (`entry × 0.625`)
- **Trailing stop**: activates at +25% (`entry × 1.25`), trails 30% below peak
- **Entry zone**: $0.10 – $0.65 (ceiling corrected from $0.75)
- **Min edge**: 7pp (was 5pp)
- **R:R**: ~1.33:1, breakeven win rate ~43%

---

## v4.10 — `--positions` Command (2026-04-22)

### Why This Change Was Made

`python bot_v4.py status` shows open positions but only as single-line summaries. There was
no way to inspect the full detail of an individual position — shares held, exact cost basis,
live current price, distance to stop and take-profit, or whether trailing stop had activated —
without reading the raw JSON market files.

### What Changed

#### New `print_positions()` function + CLI command

**Invocation (both forms supported):**
```
python bot_v4.py --positions        # live
python bot_v4.py --sim --positions  # sim mode
python bot_v4.py positions          # positional alias
```

**Per-position output:**

```
  ──────────────────────────────────────────────────────────────────
  New York City      2026-04-25   72-73F   [Cycle 1]  [UP]
  ──────────────────────────────────────────────────────────────────
  Shares:        14.29
  Cost:          $5.00
  Entry price:   $0.3500
  Current price: $0.4200
  Current value: $6.00   (+20.0% ROI)
  Unrealized:    +$1.00
  Stop price:    $0.2945  [TRAILING — peak $0.420]
  Take-profit:   $0.7000  (+100%)
  P (model):     52%  model delta=1.2F  edge=+0.170
  Opened:        2026-04-22 14:35 UTC
```

**Footer:** totals row across all open positions — total cost, current value, net unrealized P&L.

#### Price sourcing

Live price is fetched from the Gamma API (`get_market_price()`) for each position at call time.
Falls back to the cached `all_outcomes` price stored in the market file, then to `entry_price`
if neither is available (e.g. network error).

#### Trailing stop display

When `trailing_activated` is true, the stop line includes the current peak price:
```
  Stop price:    $0.2945  [TRAILING — peak $0.420]
```
When not yet activated, just the static stop price is shown.

### What Did Not Change

- All strategy logic, entry/exit mechanics, simulation mode
- `status` command output — unchanged (still the compact one-liner format)
- No new config parameters

### Current System State After v4.10

All v4.9 behavior is unchanged. The `--positions` command is a read-only diagnostic tool.

---

## v4.9 — Sigma Reduction + Bias Decoupling + Tuner Floor Fix (2026-04-20)

### Why This Change Was Made

Simulation mode produced zero buys across all cities and dates. Root-cause analysis
identified three compounding bugs that made entry mathematically impossible under the
current default parameters.

**Bug 1 — `min_confidence=0.40` exceeds the theoretical P ceiling (fatal)**

With `SIGMA_F=2.0°F` and the ±0.5° bucket expansion applied in `bucket_prob()`, the
maximum probability any US 2°F-wide bucket can receive — even when the forecast is
perfectly centered on it — is:

```
P_max = Φ(1/2.0) − Φ(−1/2.0) ≈ 0.383
```

`min_confidence` was set to 0.40 in the config. **0.383 < 0.40** → Gate 3 always
fails for every US market regardless of market prices or forecasts. The situation is
worse for EU: with `SIGMA_C=1.2°C` and 1°C-wide buckets, `P_max ≈ 0.324`. Both
regions were permanently blocked.

Note from the v4.1 changelog: when `min_confidence` was first introduced it was set
to 0.38 (just below 0.383) specifically because "A US 1°F bucket at sigma=2°F has a
hard probability ceiling of 0.383." This invariant was violated when the config was
subsequently raised to 0.40.

The tuner lower bound of `(0.40, 0.70)` compounded this — it could never tune below
the wall.

**Bug 2 — Bias correction blocked when WU API is down (silent, significant)**

The entire bias-correction block in `scan_and_update()` was wrapped in
`if WU_API_VALID:`. When the local WU server (`localhost:3000`) is unavailable —
including during most sim runs where WU isn't actively needed — the bot used raw
(uncorrected) model forecasts even though `calibration.json` contained valid bias
data with 89–90 samples per city.

Example for Atlanta at raw forecast=78.5°F vs bias-corrected forecast=79.6°F:
- Without bias: top bucket is 78-79°F (P≈0.37, price=0.47, edge=−0.10) → blocked
- With bias: top bucket shifts to 80-81°F (P≈0.43, price=0.24, edge=+0.19) → valid entry

The WU API is only needed to add a running_max observation (D+0 only). The existing
bias data from calibration should always be applied.

### What Changed

#### `SIGMA_F` 2.0 → 1.5°F, `SIGMA_C` 1.2 → 0.85°C

**Old:** `SIGMA_F = 2.0`, `SIGMA_C = 1.2`

**New:** `SIGMA_F = 1.5`, `SIGMA_C = 0.85`

With sigma=1.5°F, the US bucket P_max rises to:
```
P_max = Φ(1/1.5) − Φ(−1/1.5) ≈ 0.496
```
This clears `min_confidence=0.40` with meaningful headroom, and is more consistent
with published ECMWF 24h max-temperature forecast accuracy (~±1.5°F).

With sigma=0.85°C, the EU bucket P_max rises to:
```
P_max = Φ(0.5/0.85) − Φ(−0.5/0.85) ≈ 0.445
```
Also clears the threshold. Previously 0.324 at sigma=1.2.

Validation (Atlanta, bias-corrected forecast=79.63°F, sigma=1.5):

| Bucket | P | Price | Edge | Result |
|--------|---|-------|------|--------|
| 80-81°F | 0.428 | 0.240 | +0.188 | **ENTRY** |
| 78-79°F | 0.388 | 0.470 | −0.082 | blocked |
| 82-83°F | 0.101 | 0.065 | +0.036 | below min_edge |

#### Bias correction decoupled from `WU_API_VALID`

**Old:** entire bias correction block gated on `if WU_API_VALID:`

**New:** bias correction from calibration always runs; `WU_API_VALID` only gates
appending the WU running_max observation to the blend.

```python
# Before: both bias correction and WU obs blocked when WU is down
if WU_API_VALID:
    ecmwf_corrected = ecmwf_raw + get_bias(...)
    ...
    wu_max = snap.get("wu_running_max")  # also blocked
    corrected.append((wu_max, wu_sig))

# After: bias correction always applies
ecmwf_corrected = ecmwf_raw + get_bias(...)   # always
...
if WU_API_VALID:
    wu_max = snap.get("wu_running_max")        # WU obs only when available
    corrected.append((wu_max, wu_sig))
```

#### Tuner lower bound: `min_confidence` `(0.40, 0.70)` → `(0.33, 0.70)`

The lower bound was above the old theoretical ceiling (0.383), preventing the tuner
from discovering that a lower confidence threshold produces better outcomes. Updated
to 0.33 — safely below the new P_max for both US (0.496) and EU (0.445).

### Mathematical Summary

| Parameter | Before | After | US P_max | EU P_max | Viable? |
|-----------|--------|-------|---------|---------|---------|
| SIGMA_F/C | 2.0 / 1.2 | 1.5 / 0.85 | 0.383 → **0.496** | 0.324 → **0.445** | No → **Yes** |
| min_confidence | 0.40 | 0.40 (unchanged) | — | — | — |
| tuner lower bound | 0.40 | 0.33 | — | — | — |

### What Did Not Change

- `min_confidence` in config stays at 0.40 — now achievable with sigma=1.5/0.85
- All entry gates, re-entry logic, stop-loss, take-profit, trailing stop
- `find_best_entry()` gate structure (Gates 0–6 unchanged)
- Kelly sizing, calibration, reconciliation, Polymarket API calls
- Sigma values for WU observations (`SIGMA_WU_F`, `SIGMA_WU_F_FINAL`, etc.)
- `SIGMA_METAR_F = 1.5`, `SIGMA_METAR_C = 1.0` (unchanged — METAR is deprecated)

### Current System State After v4.9

- **Entry gates**: model agreement → market consensus → price zone → min edge (0.05) → confidence (0.40)
- **Forecast sigma**: 1.5°F (US), 0.85°C (EU) — calibrated Bayesian sigma overrides these once resolved markets accumulate
- **Bias correction**: always applied from calibration.json; WU running_max added on top when WU is up
- **Tuner**: can now adjust min_confidence down to 0.33 if resolved data supports it
- **Sim mode**: Atlanta 80-81°F fires with P=0.428, edge=+0.188 at bias-corrected forecast 79.6°F

---

## v4.8 — Simulation Mode (2026-04-20)

### Why This Change Was Made

After the three v4.7 bug fixes, confidence in the strategy logic needs to be rebuilt
before committing more real capital. Simulation mode runs the full strategy — scanning,
forecasting, entry/exit evaluation — against live Polymarket prices but with no on-chain
orders and a virtual balance. This allows validating profitability without financial risk.

### What Changed

**New `--sim` flag** (`SIM_MODE = "--sim" in sys.argv`)

All strategy logic runs identically. The only differences in sim mode:

| Component | Live | Sim |
|---|---|---|
| `place_buy_order()` | Polymarket FOK/FAK order | Returns mock `{"status": "matched", "orderID": "sim_..."}` |
| `place_sell_order()` | Polymarket limit FAK sell | Returns mock `{"status": "matched"}` |
| `get_real_balance()` | On-chain USDC balance | Reads from `sim_state.json` |
| `get_token_balance()` | On-chain conditional balance | Always returns 0.0 (bypasses chain) |
| `get_real_entry_price()` | Polymarket trade history | Always returns None |
| Reconciliation blocks | Checks for orphaned on-chain shares | Skipped entirely |
| Balance sync (post-scan) | Syncs from on-chain | Uses tracked in-memory balance |
| Market files | `weather_bot_data/markets/` | `weather_bot_data/sim_markets/` |
| State file | `weather_bot_data/state.json` | `weather_bot_data/sim_state.json` |

**Separate data paths** ensure sim runs never pollute live market or state files.

**Starting balance**: configurable via `sim_balance` in `weather_bot_config.json` (default $100.00).

### CLI Commands

```
python bot_v4.py --sim              # run main loop in sim mode
python bot_v4.py --sim status       # show sim balance and open positions
python bot_v4.py --sim report       # show sim resolved markets and PnL
python bot_v4.py --sim sim-reset    # wipe sim_state.json and sim_markets/
```

### Current System State After v4.8

- Live bot: v4.7 with three bug fixes applied. 3 trades, -$1.57 net PnL.
- Sim mode: ready. Run `python bot_v4.py --sim` to begin paper-trading.
- Calibration: 58-59 samples per city, bias correction active.
- WU API: running at localhost:3000. `pip install tzdata` recommended for timezone conversion.

---

## v4.7 — Three Bug Fixes: WU Timezone, Re-entry Gate, Corrected Model Delta (2026-04-20)

### Why This Change Was Made

Post-mortem of the first three v4.6 live trades (1 win +$0.05, 2 losses −$1.62, net −$1.57)
identified three bugs, each independently responsible for a bad entry.

**Bug 1 — WU timezone conversion silently fails on Windows → overnight low corrupts forecast (NYC)**

`get_wu_running_max()` used `local_hour = 12` as the fallback when timezone conversion failed.
On Windows, `from zoneinfo import ZoneInfo` succeeds (Python 3.9+ stdlib) but
`ZoneInfo("America/New_York")` raises `ZoneInfoNotFoundError` at runtime because the `tzdata`
package is not installed. The `except Exception: pass` swallowed this and kept `local_hour = 12`.

At NYC entry time (05:44 UTC = **01:44 am EDT**), the WU running_max was 44°F — the overnight
minimum. With `local_hour = 12` the code treated it as a "morning" observation (sigma=1.5°F),
blended it into the forecast with weight 1/σ² = 0.44, and dragged the forecast from the true
model consensus (~51°F) down to 47.8°F. This caused the bot to enter the 48–49°F bucket instead
of the 50–51°F bucket — both resolved NO (actual high: 47°F), but the bug caused a wrong-bucket
entry on a trade that would have otherwise been skipped (P at 51°F forecast would be below
min_confidence=0.38 for any 1°F bucket).

The `else` pytz branch was also unreachable: it only ran when `ZoneInfo is None` (import failed),
but since the import succeeded, pytz was never tried even when ZoneInfo's runtime lookup failed.

**Bug 2 — Re-entry path bypassed model agreement gate (Buenos Aires Cycle 2)**

`find_best_entry()` has Gate 0: `if model_delta > MAX_MODEL_DELTA: return None`. This gate was
only applied to first entries. `evaluate_reentry()` has no such check. Buenos Aires Cycle 2 was
entered with ECMWF=23.7°C, GFS=21.0°C, **model_delta=2.7°C** — above the 2.0°C gate that would
have blocked a first entry. Result: −$1.00 stop loss.

**Bug 3 — Model delta gate used raw (pre-bias) temperatures**

`model_delta = snap.get("model_delta")` was computed before bias correction ran. The forecast
decisions used bias-corrected values; the gate used uncorrected ones. For NYC at entry:
raw_delta=0°F (both models showed 50°F) → gate passed. Corrected: ECMWF+2.28=52.3°F,
GFS−0.10=49.9°F → corrected_delta=2.4°F → would have failed the 2.0°F gate. For Buenos Aires
both cycles: raw_delta≤2.0°C → passed; corrected delta was 3.7°C → both would have been blocked.

### What Changed

#### `get_wu_running_max()` — timezone fallback changed from 12 to -1 + ZoneInfo runtime fallback

**Old:** `local_hour = 12` — treated unknown timezone as "noon", triggering inclusion of WU with
sigma=1.5°F even at 1:44am local.

**New:** `local_hour = -1` — any conversion failure means "skip WU entirely". -1 < 8, so the
early-morning guard (`if sig is None`) blocks the observation.

Also restructured the try block so ZoneInfo is attempted first, and if `ZoneInfo(tz_name)` raises
at runtime (not just on import), the code falls through to pytz rather than silently keeping -1.
Both pytz and tzdata paths now work correctly.

#### `scan_and_update()` — model_delta recomputed from bias-corrected temperatures

After applying bias correction to ECMWF and GFS, `model_delta` is now updated:

```python
if ecmwf_corrected is not None and gfs_corrected is not None:
    model_delta = round(abs(ecmwf_corrected - gfs_corrected), 1)
```

This is computed before WU is appended to the blend, so it accurately reflects the
model disagreement used in the forecast decision. The stored `model_delta` in
`forecast_snapshots` will now show the corrected delta (more diagnostic value).

#### `scan_and_update()` — Gate 0 applied to re-entries

Re-entry candidate construction is now guarded by the same model agreement check as first entry:

```python
reentry = evaluate_reentry(...) if model_delta is None or model_delta <= MAX_MODEL_DELTA else None
```

Model disagreement that would block a first entry now also blocks a re-entry on the same market.

### Backtest Against Live Trades

| Trade | Bug | Would be blocked? |
|-------|-----|------------------|
| NYC C1 | Bug 1 + Bug 3 | Bug 3: corrected_delta=2.4°F > 2.0°F → blocked |
| Buenos Aires C1 | Bug 3 | corrected_delta=3.7°C > 2.0°C → blocked |
| Buenos Aires C2 | Bug 2 + Bug 3 | Both gates would block it |

All three live losses would have been avoided. The only live trade surviving the fixed gates is
Buenos Aires C1 Cycle 1 (+$0.05) — which would also be blocked by Bug 3's corrected delta.
Net: 0 trades, $0 PnL vs actual −$1.57.

### Current System State (v4.7)

- **Entry gates**: model agreement (corrected Δ ≤ 2°F/°C) → market consensus → price zone → min edge → confidence
- **Re-entry**: model agreement gate now applied (was missing in v4.6)
- **D+0 WU**: timezone failure skips WU entirely (safe fallback); ZoneInfo/pytz both tried correctly
- **Model delta**: computed from bias-corrected temperatures, consistent with forecast decision values
- All other behavior unchanged from v4.6

---

## v4.6 — Wunderground Integration + Station Bias Correction (2026-04-19)

### Why This Change Was Made

Post-mortem on two losses (Dallas Apr 17: 83°F actual, we bet 84-85°F; Paris Apr 17: 21°C actual,
we bet 20°C) confirmed a fundamental data mismatch: Polymarket markets resolve on specific airport
Wunderground station readings, but the bot was:

1. **Calibrating against Visual Crossing** — VC may return a different temperature than the WU
   station the market actually resolves on. Sigma built against VC data is calibrating against the
   wrong source.

2. **Using METAR for D+0** — METAR is an instantaneous reading (current temperature at time of
   poll). The market resolves on the **daily high**, which is the WU `running_max`. A falling
   afternoon temperature would suppress METAR below the actual peak, biasing bucket selection down.

3. **No station-level bias correction** — ECMWF/GFS grid forecasts (~25km) have systematic
   cold/warm biases at specific airport stations. Without tracking `actual_wu − forecast` per city,
   probability buckets are centered on the wrong temperature.

A WU scraper API was already built and running at `localhost:3000` with data for all 19 configured
stations. Bootstrap over 2 months of WU history vs Open-Meteo archives revealed real, significant
biases:

| City | ECMWF bias | GFS bias |
|------|-----------|---------|
| New York (KLGA) | +2.28°F | −0.10°F |
| Chicago (KORD) | +1.72°F | −0.50°F |
| Atlanta (KATL) | +1.71°F | +0.69°F |
| Buenos Aires (SAEZ) | +1.75°C | +0.06°C |
| Seoul (RKSI) | −0.95°C | +2.97°C |

These biases directly explain why buckets were consistently wrong: for NYC, every ECMWF forecast
was 2.28°F cold — we were centering probability on the wrong bucket without knowing it.

### What Changed

| Area | Before (v4.5) | After (v4.6) |
|------|--------------|--------------|
| Resolution source | `get_actual_temp()` → Visual Crossing | `get_wu_actual()` → WU `/daily` endpoint |
| D+0 observation | `get_metar()` → instantaneous temp | `get_wu_running_max()` → WU `/hourly` running_max |
| D+0 sigma | fixed 1.5°F | tiered: 0.3 (final), 1.0 (afternoon), 1.5 (morning), skip (<8h local) |
| Calibration | Bayesian sigma only | sigma + bias (`actual_wu − forecast`) per city/source |
| Bias correction | none | `forecast_corrected = forecast + get_bias(city, source, horizon)` before every entry decision |
| Startup | load calibration | load calibration → bootstrap bias from 3 months of WU history |
| Config | no `wu_api_url` | `wu_api_url: "http://localhost:3000"` |

### New Functions

- **`get_wu_actual(city_slug, date_str)`** — WU daily high for a finalized date. Used by
  calibration backfill and auto-resolution.
- **`get_wu_running_max(city_slug, date_str)`** — WU hourly running_max for D+0. Returns
  `{running_max, is_finalized, local_hour, station_timezone}`. Sigma tier selected by local hour.
- **`get_bias(city_slug, source, horizon=None)`** — returns calibrated mean bias or 0.0 if fewer
  than `BIAS_MIN_N=5` samples. Mirrors `get_sigma()`.
- **`bootstrap_wu_calibration(months=3)`** — fetches WU monthly summaries + Open-Meteo archive
  hindcasts, computes `mean(wu_high − model)` per city/source, writes to `calibration.json`.

### New Calibration Keys

`calibration.json` now stores bias entries alongside sigma entries:
```json
"nyc_ecmwf_bias": {"bias": 2.276, "n": 58, "updated_at": "..."},
"nyc_hrrr_bias":  {"bias": -0.10, "n": 58, "updated_at": "..."}
```
Bias keys: `{city}_{source}_bias` and `{city}_{source}_bias_d{N}` (per-horizon variants updated
by `run_calibration()` as positions resolve).

### How Bias Correction Works

In `scan_and_update()`, immediately after fetching the forecast snapshot and before calling
`find_best_entry()`, each model value is corrected and re-blended:
```python
ecmwf_corrected = ecmwf_raw + get_bias(city, "ecmwf", horizon=i)
gfs_corrected   = gfs_raw   + get_bias(city, "hrrr",  horizon=i)
forecast_temp, sigma = _blend_iv([(ecmwf_corrected, σ_e), (gfs_corrected, σ_g), ...])
```
Raw model values are preserved in market files for ongoing calibration.

### Current System State (v4.6)

- **Entry**: model agreement gate (Δ ≤ 2°F/1.5°C) → price zone → min edge → min confidence
- **D+0**: WU running_max with tiered sigma (0.3→1.0→1.5°F based on local hour)
- **Bias correction**: applied on every scan for all 19 cities (38 bias entries bootstrapped)
- **Calibration**: Bayesian sigma + running-mean bias per city/source/horizon
- **Resolution source**: WU `/daily` endpoint (authoritative, matches market resolution)
- **Exits**: stop-loss (50%), trailing stop (+50% activation, 30% trail), take-profit (100% ROI)
- **No forecast-based exits**

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
