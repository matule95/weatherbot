# 🌤 WeatherBet — Polymarket Weather Trading Bot

Research-oriented **paper trading** bot for Polymarket weather markets. It finds temperature buckets using real forecasts (airport-matched coordinates), compares them to YES prices, and simulates position sizing with Kelly-style rules. **It does not place on-chain Polymarket orders** — balance and PnL are tracked locally in JSON.

No SDK. No black box. Pure Python.

---

## Versions

### `bot_v1.py` — Base Bot

The foundation. Scans 6 US cities, fetches forecasts from NWS using airport station coordinates, finds matching temperature buckets on Polymarket, and flags entries when the market price is below a threshold. Optional `--live` only updates a **local** simulation ledger.

No EV/Kelly; good for understanding the core matching idea.

### `bot_v2.py` — Full Bot (current)

Everything above, scaled up:

- **20 cities** across several regions (US, Europe, Asia, Canada, South America, Oceania)
- **Forecast stack** — ECMWF (global), **US daily max** from Open-Meteo `gfs_seamless` (GFS-oriented blend; not literal HRRR grid), METAR where applicable
- **Expected value** — skips trades where the model says the edge is negative
- **Fractional Kelly** — caps position size (`max_bet`, balance)
- **Stop-loss + trailing stop** — default 20% stop; can move stop to breakeven after +20% on the bid
- **Stops between scans** — every monitor cycle refreshes **live bid/ask** from Gamma for open positions so 10‑minute checks are not stuck on stale hourly quotes
- **Slippage filter** — skips wide spreads (`max_slippage`)
- **Self-calibration** — learns a per-city spread heuristic from resolved markets (see docstring in `run_calibration` for limits)
- **Persistence** — forecasts, quotes, and positions in `data/markets/`; `simulation.json` feeds `sim_dashboard_repost.html`

---

## How It Works

Polymarket runs markets like “Will the highest temperature in Chicago be between 46–47°F on March 7?”. The bot:

1. Pulls ECMWF and (for US cities) `gfs_seamless` daily max from [Open-Meteo](https://open-meteo.com/)
2. Pulls METAR observations for the **same ICAO station** the market resolves on
3. Loads buckets and **best bid / best ask** from Polymarket Gamma
4. Computes a Gaussian bucket probability, EV vs ask, and Kelly-based size
5. Runs a **full scan** on `scan_interval` (default 1 hour) and **monitors** open positions on a shorter interval (default 10 minutes) with **fresh quotes**
6. Resolves paper PnL when Gamma marks the market closed; can attach **Visual Crossing** `tempmax` for calibration

---

## Why Airport Coordinates Matter

Every Polymarket weather market resolves on a specific airport station. NYC uses LaGuardia (KLGA), Dallas uses Love Field (KDAL), not necessarily the city centroid. The bot uses those coordinates for forecasts and METAR.

| City | Station | Airport |
|------|---------|---------|
| NYC | KLGA | LaGuardia |
| Chicago | KORD | O'Hare |
| Miami | KMIA | Miami Intl |
| Dallas | KDAL | Love Field |
| Seattle | KSEA | Sea-Tac |
| Atlanta | KATL | Hartsfield |
| London | EGLC | London City |
| Tokyo | RJTT | Haneda |
| ... | ... | ... |

---

## Installation

```bash
git clone https://github.com/alteregoeth-ai/weatherbot
cd weatherbot
pip install -r requirements.txt
```

Configuration:

1. Copy `config.example.json` → `config.json` (or set `WEATHERBOT_CONFIG` to a JSON file path).
2. Set Visual Crossing in **`config.json`** (`vc_key`) **or** environment variable `VC_KEY` / `WEATHERBOT_VC_KEY` (env wins). Used for historical `tempmax` after resolution.

`config.json`, `data/`, and `simulation.json` are gitignored — keep secrets and generated JSON out of version control.

---

## Usage (`bot_v2.py`)

```bash
python bot_v2.py              # default: run main loop (alias for same as below)
python bot_v2.py run          # main loop: hourly scan + 10‑minute monitor
python bot_v2.py status       # balance and open positions
python bot_v2.py report       # resolved markets breakdown
python bot_v2.py explain      # operational notes and data-quality tips
python bot_v2.py health       # API probes + GOOD / WARNING / BAD summary
```

`bot_v1.py` keeps its own CLI (`--live`, `--reset`, `--positions`).

---

## Data storage

- `data/markets/{city}_{date}.json` — per-event snapshots (forecasts, quotes, optional position)
- `data/state.json` — simulated balance and scan metadata
- `data/calibration.json` — per-city sigma heuristics
- `simulation.json` — aggregate export for the HTML dashboard

---

## APIs

| API | Auth | Purpose |
|-----|------|---------|
| Open-Meteo | None | ECMWF + US `gfs_seamless` |
| Aviation Weather (METAR) | None | Station observations |
| Polymarket Gamma | None | Markets and quotes |
| Visual Crossing | Free key (`VC_KEY` or `vc_key` in config) | Resolution / calibration temps |

---

## Disclaimer

This is not financial advice. Prediction markets carry real risk. This repo’s v2 path is **simulation-only** until you add a separate execution layer.
