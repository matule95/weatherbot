#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bot_v4.py — WeatherBet Trading Bot (v4 Rewrite — 2026-04-16)
=============================================================
Clean rewrite of bot_v3.py. Targeted fixes based on post-mortem analysis of
17 live trades (11.8% win rate, -27% drawdown).

Root causes fixed:
  1. forecast_changed exit — removed entirely. The Atlanta 2026-04-13 trade
     (ECMWF=GFS=82°F, actual=82.1°F) was a correct prediction that exited at
     -$0.26 because a routine forecast update triggered the old exit. Price-only
     exits are now the sole mechanism.

  2. Model agreement gate (new) — blocks entry when |ECMWF - GFS| > max_model_delta
     (default 2°F/1.5°C). The three biggest losses (Seattle 3°F delta, Dallas 4°F
     and 3°F delta) would all have been skipped by this gate.

  3. get_hrrr() was calling models=gfs_seamless and was US-only — renamed to
     get_gfs(), now global and uses the city's native temperature unit.

  4. METAR now retries 3× before giving up (was no retry).

  5. Minimum edge gate (new) — requires P - market_price >= min_edge (default 0.10),
     replacing the weaker "any positive EV" filter.

Strategy:
  Entry:  Model agreement + opportunity zone + min edge + min confidence.
  Exit:   Take-profit at +35% ROI, stop-loss at -25%, trailing stop to breakeven.
          NO forecast-based exits.
  Cycles: Re-enter same bucket if: last cycle profitable, price below last exit,
          hours >= min_reentry_hours, fresh P >= min_confidence.

See CHANGELOG.md for full history.

Usage:
    python bot_v4.py              # main loop (live)
    python bot_v4.py status       # balance and open positions (live)
    python bot_v4.py report       # full report (live)
    python bot_v4.py --positions  # detailed per-position breakdown (live)
    python bot_v4.py --sim        # main loop (simulation — no real orders)
    python bot_v4.py --sim status # sim balance and open positions
    python bot_v4.py --sim report # sim full report
    python bot_v4.py --sim --positions  # detailed positions (sim)
    python bot_v4.py --sim sim-reset  # wipe sim state and market files
"""

import re
import sys
import json
import math
import time
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:
    try:
        from backports.zoneinfo import ZoneInfo
    except ImportError:
        ZoneInfo = None

from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import (
    MarketOrderArgs, OrderArgs, OrderType, ApiCreds,
    BalanceAllowanceParams, AssetType,
)
from py_clob_client_v2.order_builder.constants import BUY, SELL

# =============================================================================
# CONFIG
# =============================================================================

with open("weather_bot_config.json", encoding="utf-8") as f:
    _cfg = json.load(f)

BALANCE           = _cfg.get("balance", 30.0)
MAX_BET           = _cfg.get("max_bet", 10.0)
MIN_CONFIDENCE    = _cfg.get("min_confidence", 0.38)
MAX_ENTRY_PRICE   = _cfg.get("max_entry_price", 0.65)
MIN_ENTRY_PRICE   = _cfg.get("min_entry_price", 0.25)
MAX_REENTRY_PRICE = _cfg.get("max_reentry_price", 0.65)
MIN_REENTRY_HOURS = _cfg.get("min_reentry_hours", 12.0)
MIN_VOLUME        = _cfg.get("min_volume", 150)
MIN_HOURS         = _cfg.get("min_hours", 0.5)
MAX_HOURS         = _cfg.get("max_hours", 72.0)
MAX_SLIPPAGE      = _cfg.get("max_slippage", 0.06)
SCAN_INTERVAL     = _cfg.get("scan_interval", 3600)
VC_KEY            = _cfg.get("vc_key", "")
PRIOR_WEIGHT      = _cfg.get("prior_weight", 5)
TUNE_LOOKBACK     = _cfg.get("tune_lookback", 20)
TUNE_ENABLED      = _cfg.get("tune_enabled", True)
MAX_OPEN_POS      = _cfg.get("max_open_positions", 10)
MAX_POS_PER_DATE  = _cfg.get("max_positions_per_date", 5)
CITY_LOSS_LIMIT   = _cfg.get("city_loss_limit", 2)         # losses in window before pausing city
CITY_LOSS_WINDOW  = _cfg.get("city_loss_window_hours", 24) # rolling window for the loss count
MONITOR_INTERVAL  = _cfg.get("monitor_interval", 300)
STOP_LOSS_PCT     = _cfg.get("stop_loss_pct", 0.75)
TRAILING_DISTANCE   = _cfg.get("trailing_distance", 0.20)    # trail stop this far below the peak price once trailing activates
TRAILING_ACTIVATION = _cfg.get("trailing_activation", 1.25)  # trailing activates once price reaches entry × this factor
SCAN_REGIONS      = set(_cfg.get("scan_regions", ["us", "eu"]))
MAX_CYCLES        = _cfg.get("max_cycles_per_market", 3)
MIN_BET           = _cfg.get("min_bet", 0.50)

# v4 entry gates (unit-aware: F bucket = 1°F wide, C bucket = 1°C wide).
# Same number of "buckets-of-disagreement" should apply in both regions; a flat
# constant in raw degrees is 1.8× more permissive in C than in F, which leaks
# obviously-wrong entries (e.g. Munich 2026-04-29: crowd gap 0.8°C vs 4.0°C cap).
MAX_MODEL_DELTA_F   = _cfg.get("max_model_delta_f", 2.0)
MAX_MODEL_DELTA_C   = _cfg.get("max_model_delta_c", 1.1)
MAX_CROWD_GAP_BUCKS = _cfg.get("max_crowd_gap_buckets", 1.0)  # in bucket-widths
MAX_MODEL_DELTA     = MAX_MODEL_DELTA_F  # legacy alias for log lines; per-call value resolved by unit
MIN_EDGE            = _cfg.get("min_edge", 0.10)

def model_delta_cap(unit):
    return MAX_MODEL_DELTA_F if unit == "F" else MAX_MODEL_DELTA_C

def crowd_gap_cap(unit):
    # Bucket width is 1 unit (1°F or 1°C). Cap is expressed in bucket-widths.
    return MAX_CROWD_GAP_BUCKS

# CLOB credentials
POLYMARKET_HOST     = "https://clob.polymarket.com"
POLY_PRIVATE_KEY    = _cfg.get("polymarket_private_key", "")
POLY_API_KEY        = _cfg.get("polymarket_api_key", "")
POLY_API_SECRET     = _cfg.get("polymarket_api_secret", "")
POLY_API_PASSPHRASE = _cfg.get("polymarket_api_passphrase", "")
POLY_FUNDER         = _cfg.get("polymarket_funder", "")
POLY_CHAIN_ID       = _cfg.get("chain_id", 137)
POLY_SIG_TYPE       = _cfg.get("signature_type", 0)

SIGMA_F       = 1.5   # default forecast sigma in Fahrenheit (ECMWF 24h max-temp accuracy ~±1.5°F)
SIGMA_C       = 0.85  # default forecast sigma in Celsius
SIGMA_METAR_C = 1.0   # METAR is a real observation — tighter sigma
SIGMA_METAR_F = 1.5

WU_API_URL       = _cfg.get("wu_api_url", "http://localhost:3000")
SIGMA_WU_F_FINAL = 0.3   # WU finalized — the station reading is the resolution oracle
SIGMA_WU_C_FINAL = 0.2
SIGMA_WU_F       = 1.0   # Afternoon running_max — some afternoon swing still possible
SIGMA_WU_C       = 0.6
SIGMA_WU_F_EARLY = 1.5   # Morning running_max — large afternoon swing still possible
SIGMA_WU_C_EARLY = 1.0
BIAS_MIN_N       = 5     # minimum samples before bias correction is applied

try:
    WU_API_VALID = requests.get(f"{WU_API_URL}/health", timeout=5).ok
except Exception:
    WU_API_VALID = False

DATA_DIR         = Path("weather_bot_data")
DATA_DIR.mkdir(exist_ok=True)
STATE_FILE       = DATA_DIR / "state.json"
MARKETS_DIR      = DATA_DIR / "markets"
MARKETS_DIR.mkdir(exist_ok=True)
CALIBRATION_FILE = DATA_DIR / "calibration.json"
STRATEGY_FILE    = DATA_DIR / "strategy.json"

# --- Simulation mode ---
# Run with --sim to paper-trade without touching the chain.
# Separate data paths so sim runs never pollute live market files.
SIM_MODE        = "--sim" in sys.argv
SIM_BALANCE     = float(_cfg.get("sim_balance", 100.0))
SIM_MARKETS_DIR = DATA_DIR / "sim_markets"
SIM_STATE_FILE  = DATA_DIR / "sim_state.json"
if SIM_MODE:
    SIM_MARKETS_DIR.mkdir(exist_ok=True)

VC_KEY_VALID = bool(VC_KEY) and VC_KEY != "YOUR_KEY_HERE"

# Mutable strategy — overridden by strategy.json on startup and after each tune
_strategy = {
    "min_confidence":  _cfg.get("min_confidence", 0.38),
    "take_profit_roi": _cfg.get("take_profit_roi", 0.35),
    "kelly_fraction":  _cfg.get("kelly_fraction", 0.30),
}

def _load_strategy():
    if STRATEGY_FILE.exists():
        try:
            saved = json.loads(STRATEGY_FILE.read_text(encoding="utf-8"))
            for k in ("min_confidence", "take_profit_roi", "kelly_fraction"):
                if k in saved:
                    _strategy[k] = saved[k]
        except Exception:
            pass

_load_strategy()

LOCATIONS = {
    # --- Active markets (optimized 4-city selection) ---
    "nyc":          {"lat": 40.7772,  "lon":  -73.8726, "name": "New York City", "station": "KLGA", "unit": "F", "region": "us"},
    "miami":        {"lat": 25.7959,  "lon":  -80.2870, "name": "Miami",         "station": "KMIA", "unit": "F", "region": "us"},
    "atlanta":      {"lat": 33.6407,  "lon":  -84.4277, "name": "Atlanta",       "station": "KATL", "unit": "F", "region": "us"},
    "london":       {"lat": 51.5048,  "lon":    0.0495, "name": "London",        "station": "EGLC", "unit": "C", "region": "eu"},
    # --- Paused: opposite-direction model biases / historical delta losses ---
    "chicago":    {"lat": 41.9742,  "lon":  -87.9073, "name": "Chicago",       "station": "KORD", "unit": "F", "region": "us"},
    "dallas":     {"lat": 32.8471,  "lon":  -96.8518, "name": "Dallas",        "station": "KDAL", "unit": "F", "region": "us"},
    "seattle":    {"lat": 47.4502,  "lon": -122.3088, "name": "Seattle",       "station": "KSEA", "unit": "F", "region": "us"},
    "paris":      {"lat": 48.9962,  "lon":    2.5979, "name": "Paris",         "station": "LFPG", "unit": "C", "region": "eu"},
    "munich":     {"lat": 48.3537,  "lon":   11.7750, "name": "Munich",        "station": "EDDM", "unit": "C", "region": "eu"},
    "ankara":     {"lat": 40.1281,  "lon":   32.9951, "name": "Ankara",        "station": "LTAC", "unit": "C", "region": "eu"},
    # Kept for future use — not scanned unless added to scan_regions
    "seoul":        {"lat": 37.4691,  "lon":  126.4505, "name": "Seoul",         "station": "RKSI", "unit": "C", "region": "asia"},
    "tokyo":        {"lat": 35.7647,  "lon":  140.3864, "name": "Tokyo",         "station": "RJTT", "unit": "C", "region": "asia"},
    "shanghai":     {"lat": 31.1443,  "lon":  121.8083, "name": "Shanghai",      "station": "ZSPD", "unit": "C", "region": "asia"},
    "singapore":    {"lat":  1.3502,  "lon":  103.9940, "name": "Singapore",     "station": "WSSS", "unit": "C", "region": "asia"},
    "lucknow":      {"lat": 26.7606,  "lon":   80.8893, "name": "Lucknow",       "station": "VILK", "unit": "C", "region": "asia"},
    "toronto":      {"lat": 43.6772,  "lon":  -79.6306, "name": "Toronto",       "station": "CYYZ", "unit": "C", "region": "ca"},
    "sao-paulo":    {"lat": -23.4356, "lon":  -46.4731, "name": "Sao Paulo",     "station": "SBGR", "unit": "C", "region": "sa"},
    "buenos-aires": {"lat": -34.8222, "lon":  -58.5358, "name": "Buenos Aires",  "station": "SAEZ", "unit": "C", "region": "sa"},
    "wellington":   {"lat": -41.3272, "lon":  174.8052, "name": "Wellington",    "station": "NZWN", "unit": "C", "region": "oc"},
}

TIMEZONES = {
    "nyc": "America/New_York", "chicago": "America/Chicago",
    "miami": "America/New_York", "dallas": "America/Chicago",
    "seattle": "America/Los_Angeles", "atlanta": "America/New_York",
    "london": "Europe/London", "paris": "Europe/Paris",
    "munich": "Europe/Berlin", "ankara": "Europe/Istanbul",
    "seoul": "Asia/Seoul", "tokyo": "Asia/Tokyo",
    "shanghai": "Asia/Shanghai", "singapore": "Asia/Singapore",
    "lucknow": "Asia/Kolkata",
    "toronto": "America/Toronto", "sao-paulo": "America/Sao_Paulo",
    "buenos-aires": "America/Argentina/Buenos_Aires", "wellington": "Pacific/Auckland",
}

MONTHS = ["january","february","march","april","may","june",
          "july","august","september","october","november","december"]

# =============================================================================
# CLOB CLIENT
# =============================================================================

_clob_client = None

def get_clob_client() -> ClobClient:
    global _clob_client
    if _clob_client is None:
        if not POLY_PRIVATE_KEY:
            raise RuntimeError("polymarket_private_key not set in config")
        client = ClobClient(
            POLYMARKET_HOST,
            key=POLY_PRIVATE_KEY,
            chain_id=POLY_CHAIN_ID,
            signature_type=POLY_SIG_TYPE,
            funder=POLY_FUNDER or None,
        )
        if POLY_API_KEY and POLY_API_SECRET and POLY_API_PASSPHRASE:
            client.set_api_creds(ApiCreds(
                api_key=POLY_API_KEY,
                api_secret=POLY_API_SECRET,
                api_passphrase=POLY_API_PASSPHRASE,
            ))
        else:
            client.set_api_creds(client.create_or_derive_api_key())
        _clob_client = client
    return _clob_client

def get_real_balance() -> float | None:
    if SIM_MODE:
        if SIM_STATE_FILE.exists():
            try:
                return json.loads(SIM_STATE_FILE.read_text(encoding="utf-8")).get("balance", SIM_BALANCE)
            except Exception:
                pass
        return SIM_BALANCE
    try:
        resp = get_clob_client().get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        return float(resp["balance"]) / 1e6
    except Exception as e:
        print(f"  [BALANCE] Error fetching real balance: {e}")
        return None

def place_buy_order(token_id: str, cost: float) -> dict | None:
    """Place a market BUY order for `cost` USDC. Tries FOK then FAK."""
    if SIM_MODE:
        import uuid
        return {"status": "matched", "orderID": f"sim_{uuid.uuid4().hex[:8]}"}
    client = get_clob_client()
    mo = MarketOrderArgs(token_id=token_id, amount=cost, side=BUY)
    for order_type in (OrderType.FOK, OrderType.FAK):
        try:
            signed = client.create_market_order(mo)
            resp = client.post_order(signed, order_type)
            if resp and resp.get("status") in ("matched", "delayed"):
                return resp
        except Exception as e:
            print(f"  [BUY ERROR] {order_type}: {e}")
    return None

def place_sell_order(token_id: str, size: float, price: float, market_id: str = None) -> dict | None:
    """
    Place a limit FAK SELL order for `size` shares.
    Fetches actual on-chain balance first to prevent over-sell errors.
    Uses best bid for realistic fill pricing.
    """
    if SIM_MODE:
        return {"status": "matched"}
    client = get_clob_client()
    actual_size = round(size, 2)

    # Cap size to on-chain balance (partial fills on buy are common)
    try:
        bal_resp = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token_id)
        )
        available = float(bal_resp["balance"]) / 1e6
        if available < 1.0:
            print(f"  [SELL] Token balance too low ({available:.2f} shares), skipping")
            return None
        if available < actual_size:
            actual_size = math.floor(available * 100) / 100
            print(f"  [SELL] Capping size {size:.2f} → {actual_size:.2f} (partial fill)")
    except Exception as e:
        print(f"  [SELL] Could not check token balance: {e} — using recorded size")

    # Use best bid for realistic fill
    sell_price = price
    if market_id:
        try:
            r = requests.get(f"https://gamma-api.polymarket.com/markets/{market_id}", timeout=(3, 5))
            mdata = r.json()
            best_bid = mdata.get("bestBid")
            if best_bid is not None:
                sell_price = float(best_bid)
                if sell_price != price:
                    print(f"  [SELL] Using best bid ${sell_price:.3f} (last known ${price:.3f})")
        except Exception:
            pass

    if sell_price <= 0.005:
        print(f"  [SELL] Bid too low (${sell_price:.3f}), skipping")
        return None

    try:
        sell_args = OrderArgs(
            token_id=token_id,
            price=round(sell_price, 4),
            size=actual_size,
            side=SELL,
        )
        signed = client.create_order(sell_args)
        resp = client.post_order(signed, OrderType.FAK)
        if not resp or resp.get("status") not in ("matched", "delayed"):
            status = resp.get("status", "unknown") if resp else "no response"
            print(f"  [SELL] Order not filled (status: {status})")
            return None
        return resp
    except Exception as e:
        print(f"  [SELL ERROR] {e}")
        return None

def get_token_balance(token_id: str) -> float:
    if SIM_MODE or not token_id:
        return 0.0
    try:
        bal_resp = get_clob_client().get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token_id)
        )
        return float(bal_resp["balance"]) / 1e6
    except Exception as e:
        print(f"  [BALANCE] Error checking token balance: {e}")
        return 0.0

def get_real_entry_price(token_id: str) -> float | None:
    """Query Polymarket trade history to find actual average buy price."""
    if SIM_MODE:
        return None
    try:
        client = get_clob_client()
        try:
            from py_clob_client.clob_types import TradeParams
            trades = client.get_trades(TradeParams(asset_id=token_id))
        except (ImportError, TypeError, AttributeError):
            trades = client.get_trades({"asset_id": token_id})

        if not trades:
            return None

        total_cost, total_shares = 0.0, 0.0
        for t in trades:
            if (t.get("side") or "").upper() == "BUY":
                price = float(t.get("price", 0))
                size  = float(t.get("size", 0))
                if price > 0 and size > 0:
                    total_cost   += price * size
                    total_shares += size

        if total_shares > 0:
            return round(total_cost / total_shares, 6)
    except Exception as e:
        print(f"  [TRADES] Could not fetch trade history: {e}")
    return None

# =============================================================================
# MATH
# =============================================================================

def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def bucket_prob(forecast, t_low, t_high, sigma=None):
    """Gaussian CDF probability for all bucket types.

    Terminal buckets (t_low=-999 or t_high=999) use the raw boundary.
    All bounded buckets expand by ±0.5 degrees to account for integer
    temperature resolution: "72-73°F" resolves YES on any reading that
    rounds to 72 or 73, i.e., continuous range [71.5, 73.5].  The same
    logic applies to EU single-degree buckets (t_low == t_high).
    """
    s = sigma or 2.0
    if s <= 0:
        s = 0.01
    if t_low == -999:
        # Terminal lower bucket: "X°F or below" — continuous range (-∞, X+0.5)
        return norm_cdf((t_high + 0.5 - float(forecast)) / s)
    if t_high == 999:
        # Terminal upper bucket: "X°F or higher" — continuous range (X-0.5, +∞)
        return 1.0 - norm_cdf((t_low - 0.5 - float(forecast)) / s)
    # Bounded bucket — expand by ±0.5 for integer rounding
    return norm_cdf((t_high + 0.5 - float(forecast)) / s) - norm_cdf((t_low - 0.5 - float(forecast)) / s)

def city_recently_lost(city_slug, all_markets, now, window_hours=None, limit=None):
    """Return True if the city has accumulated >= `limit` negative-PnL exits in the
    past `window_hours`. Used as a per-city circuit breaker to block new entries
    after a loss cluster (e.g. Munich's 3 consecutive stops on 2026-04-29 cost
    the sim ~$26 — a single-city block would have stopped at the second one).

    Cycles span all markets for the city, not just one resolution date.
    """
    window  = window_hours if window_hours is not None else CITY_LOSS_WINDOW
    cap     = limit if limit is not None else CITY_LOSS_LIMIT
    cutoff  = now - timedelta(hours=window)
    losses  = 0
    for m in all_markets:
        if m.get("city") != city_slug:
            continue
        for c in m.get("cycles", []):
            if (c.get("pnl") or 0) >= 0:
                continue
            closed_at = c.get("closed_at")
            if not closed_at:
                continue
            try:
                ts = datetime.fromisoformat(closed_at.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                continue
            if ts >= cutoff:
                losses += 1
                if losses >= cap:
                    return True
    return False


def forecast_diverged(forecast_temp, t_low, t_high, tolerance=0.5):
    """Return True when the forecast has moved outside the bucket's rounding window.

    Used as the primary loss-side exit signal. Price-based stops do not reliably
    cap loss in weather markets — prices gap toward zero on resolution rather
    than declining smoothly. Exiting on a model update is the actual recoverable
    signal: by the time the corrected forecast clears the rounding window, the
    bucket is unlikely to win and price will continue to decay.

    Terminal lower bucket (-999, X): diverged if forecast > X + tolerance
    Terminal upper bucket (X, 999): diverged if forecast < X - tolerance
    Bounded bucket [X, Y]: diverged if forecast < X - tolerance or > Y + tolerance
    """
    if forecast_temp is None or t_low is None or t_high is None:
        return False
    if t_low == -999:
        return forecast_temp > t_high + tolerance
    if t_high == 999:
        return forecast_temp < t_low - tolerance
    return forecast_temp < t_low - tolerance or forecast_temp > t_high + tolerance


def calc_ev(p, price):
    """Expected value per dollar risked: p/price - 1 (positive = model has edge over market)."""
    if price <= 0 or price >= 1:
        return 0.0
    return round(p * (1.0 / price - 1.0) - (1.0 - p), 4)

def calc_kelly(p, price):
    if price <= 0 or price >= 1:
        return 0.0
    b = 1.0 / price - 1.0
    f = (p * b - (1.0 - p)) / b
    return round(min(max(0.0, f) * _strategy["kelly_fraction"], 1.0), 4)

def bet_size(kelly, balance):
    return round(min(kelly * balance, MAX_BET), 2)

# =============================================================================
# CALIBRATION
# =============================================================================

_cal: dict = {}

def load_cal():
    if CALIBRATION_FILE.exists():
        return json.loads(CALIBRATION_FILE.read_text(encoding="utf-8"))
    return {}

def get_sigma(city_slug, source="ecmwf", horizon=None):
    if horizon is not None:
        h_key = f"{city_slug}_{source}_d{horizon}"
        if h_key in _cal:
            return _cal[h_key]["sigma"]
    base_key = f"{city_slug}_{source}"
    if base_key in _cal:
        return _cal[base_key]["sigma"]
    return SIGMA_F if LOCATIONS[city_slug]["unit"] == "F" else SIGMA_C

def get_bias(city_slug, source, horizon=None):
    """Calibrated model bias (actual_wu − forecast). Positive = model runs cold.
    Returns 0.0 until BIAS_MIN_N samples are available."""
    if horizon is not None:
        h_key = f"{city_slug}_{source}_bias_d{horizon}"
        entry = _cal.get(h_key)
        if entry and entry.get("n", 0) >= BIAS_MIN_N:
            return entry["bias"]
    base_key = f"{city_slug}_{source}_bias"
    entry = _cal.get(base_key)
    if entry and entry.get("n", 0) >= BIAS_MIN_N:
        return entry["bias"]
    return 0.0

def run_calibration(markets):
    resolved = [m for m in markets if m.get("status") == "resolved" and m.get("actual_temp") is not None]
    if not resolved:
        return load_cal()

    cal     = load_cal()
    updated = []
    now_str = datetime.now(timezone.utc).isoformat()

    # Note: "hrrr" source name kept for backward compatibility with existing calibration data.
    # The get_gfs() function populates the "hrrr" key in forecast snapshots.
    for source in ["ecmwf", "hrrr"]:
        for city in set(m["city"] for m in resolved):
            if city not in LOCATIONS:
                continue
            group       = [m for m in resolved if m["city"] == city]
            prior_sigma = SIGMA_F if LOCATIONS[city]["unit"] == "F" else SIGMA_C

            horizon_errors:    dict[str, list] = {}
            horizon_residuals: dict[str, list] = {}
            all_errors:    list[float] = []
            all_residuals: list[float] = []

            for m in group:
                for snap in reversed(m.get("forecast_snapshots", [])):
                    temp_val = snap.get(source)
                    if temp_val is None:
                        continue
                    err      = abs(temp_val - m["actual_temp"])
                    residual = m["actual_temp"] - temp_val   # positive = model runs cold
                    all_errors.append(err)
                    all_residuals.append(residual)
                    h = snap.get("horizon", "")
                    h_num = h.replace("D+", "") if h.startswith("D+") else None
                    if h_num is not None:
                        horizon_errors.setdefault(h_num, []).append(err)
                        horizon_residuals.setdefault(h_num, []).append(residual)
                    break

            def bayesian_sigma(errors, prior_s):
                n = len(errors)
                if n == 0:
                    return None
                mae = sum(errors) / n
                return round((PRIOR_WEIGHT * prior_s + n * mae) / (PRIOR_WEIGHT + n), 3)

            for h_num, h_errs in horizon_errors.items():
                key = f"{city}_{source}_d{h_num}"
                new = bayesian_sigma(h_errs, prior_sigma)
                if new is None:
                    continue
                old = cal.get(key, {}).get("sigma", prior_sigma)
                cal[key] = {"sigma": new, "n": len(h_errs), "updated_at": now_str}
                if abs(new - old) > 0.05:
                    updated.append(f"{LOCATIONS[city]['name']} {source} D+{h_num}: {old:.2f}->{new:.2f}")

                # Bias update for this horizon
                h_resids = horizon_residuals.get(h_num, [])
                if h_resids:
                    bias_key = f"{city}_{source}_bias_d{h_num}"
                    new_bias = round(sum(h_resids) / len(h_resids), 3)
                    old_bias = cal.get(bias_key, {}).get("bias", 0.0)
                    cal[bias_key] = {"bias": new_bias, "n": len(h_resids), "updated_at": now_str}
                    if abs(new_bias - old_bias) > 0.1:
                        updated.append(f"{LOCATIONS[city]['name']} {source} D+{h_num} bias: {old_bias:+.2f}->{new_bias:+.2f}")

            if all_errors:
                key = f"{city}_{source}"
                new = bayesian_sigma(all_errors, prior_sigma)
                if new is not None:
                    old = cal.get(key, {}).get("sigma", prior_sigma)
                    cal[key] = {"sigma": new, "n": len(all_errors), "updated_at": now_str}
                    if abs(new - old) > 0.05:
                        updated.append(f"{LOCATIONS[city]['name']} {source}: {old:.2f}->{new:.2f}")

                # Bias update overall
                if all_residuals:
                    bias_key = f"{city}_{source}_bias"
                    new_bias = round(sum(all_residuals) / len(all_residuals), 3)
                    old_bias = cal.get(bias_key, {}).get("bias", 0.0)
                    cal[bias_key] = {"bias": new_bias, "n": len(all_residuals), "updated_at": now_str}
                    if abs(new_bias - old_bias) > 0.1:
                        updated.append(f"{LOCATIONS[city]['name']} {source} bias: {old_bias:+.2f}->{new_bias:+.2f}")

    CALIBRATION_FILE.write_text(json.dumps(cal, indent=2), encoding="utf-8")
    if updated:
        print(f"  [CAL] {', '.join(updated)}")
    return cal

# =============================================================================
# FORECASTS
# =============================================================================

def get_ecmwf(city_slug, dates):
    loc       = LOCATIONS[city_slug]
    unit      = loc["unit"]
    temp_unit = "fahrenheit" if unit == "F" else "celsius"
    result    = {}
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={loc['lat']}&longitude={loc['lon']}"
        f"&daily=temperature_2m_max&temperature_unit={temp_unit}"
        f"&forecast_days=7&timezone={TIMEZONES.get(city_slug, 'UTC')}"
        f"&models=ecmwf_ifs025&bias_correction=true"
    )
    for attempt in range(3):
        try:
            data = requests.get(url, timeout=(5, 10)).json()
            if "error" not in data:
                for date, temp in zip(data["daily"]["time"], data["daily"]["temperature_2m_max"]):
                    if date in dates and temp is not None:
                        result[date] = round(temp, 1) if unit == "C" else round(temp)
            break
        except Exception as e:
            if attempt < 2:
                time.sleep(3)
            else:
                print(f"  [ECMWF] {city_slug}: {e}")
    return result

def get_gfs(city_slug, dates):
    """
    GFS seamless model forecast via Open-Meteo. Global coverage (US and EU).
    Previously misnamed get_hrrr() and restricted to US only; now correctly
    named and available for all scan regions. Uses the city's native unit.

    Note: forecast_snapshots still store this under the key "hrrr" for backward
    compatibility with existing calibration data (run_calibration reads "hrrr").
    """
    loc       = LOCATIONS[city_slug]
    unit      = loc["unit"]
    temp_unit = "fahrenheit" if unit == "F" else "celsius"
    result    = {}
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={loc['lat']}&longitude={loc['lon']}"
        f"&daily=temperature_2m_max&temperature_unit={temp_unit}"
        f"&forecast_days=7&timezone={TIMEZONES.get(city_slug, 'UTC')}"
        f"&models=gfs_seamless"
    )
    for attempt in range(3):
        try:
            data = requests.get(url, timeout=(5, 10)).json()
            if "error" not in data:
                for date, temp in zip(data["daily"]["time"], data["daily"]["temperature_2m_max"]):
                    if date in dates and temp is not None:
                        result[date] = round(temp, 1) if unit == "C" else round(temp)
            break
        except Exception as e:
            if attempt < 2:
                time.sleep(3)
            else:
                print(f"  [GFS] {city_slug}: {e}")
    return result

def get_metar(city_slug):
    """Current observed temperature from METAR. D+0 only. Retries 3x."""
    loc     = LOCATIONS[city_slug]
    station = loc["station"]
    unit    = loc["unit"]
    url     = f"https://aviationweather.gov/api/data/metar?ids={station}&format=json"
    for attempt in range(3):
        try:
            resp = requests.get(url, timeout=(5, 8))
            if not resp.text or not resp.text.strip():
                if attempt < 2:
                    time.sleep(2)
                    continue
                return None
            data = resp.json()
            if data and isinstance(data, list):
                temp_c = data[0].get("temp")
                if temp_c is not None:
                    if unit == "F":
                        return round(float(temp_c) * 9 / 5 + 32)
                    return round(float(temp_c), 1)
            return None
        except Exception as e:
            if attempt < 2:
                time.sleep(2)
            else:
                print(f"  [METAR] {city_slug}: {e}")
    return None

def get_wu_running_max(city_slug, date_str):
    """Fetch WU running_max for D+0 via the hourly endpoint.

    Returns dict with running_max (in station's native unit), is_finalized,
    local_hour (of the running_max observation), and station_timezone.
    Returns None if WU API is down, no observations yet, or too early to use.
    """
    if not WU_API_VALID:
        return None
    loc     = LOCATIONS[city_slug]
    station = loc["station"]
    unit    = loc["unit"]
    url     = f"{WU_API_URL}/weather/{station}/hourly?date={date_str}"
    try:
        resp = requests.get(url, timeout=(5, 10))
        if not resp.ok:
            return None
        data        = resp.json()
        running_max = data.get("running_max")
        if running_max is None:
            return None
        running_max = float(running_max)

        # Determine local hour of the running_max observation.
        # Fallback is -1 (not 12) so that a timezone conversion failure causes WU to be
        # skipped entirely rather than included at the wrong sigma tier. On Windows,
        # ZoneInfo(tz_name) raises ZoneInfoNotFoundError without the tzdata package, so we
        # must also try pytz when ZoneInfo fails at runtime (not just when it fails to import).
        tz_name  = data.get("station_timezone") or TIMEZONES.get(city_slug, "UTC")
        rmt      = data.get("running_max_time")
        local_hour = -1  # skip WU when timezone conversion fails — avoids overnight low corrupting forecast
        if rmt:
            try:
                utc_dt    = datetime.fromisoformat(rmt.replace("Z", "+00:00"))
                converted = False
                if ZoneInfo is not None:
                    try:
                        local_dt   = utc_dt.astimezone(ZoneInfo(tz_name))
                        local_hour = local_dt.hour
                        converted  = True
                    except Exception:
                        pass  # ZoneInfo available but tz lookup failed (e.g. tzdata not installed)
                if not converted:
                    import pytz
                    local_dt   = utc_dt.astimezone(pytz.timezone(tz_name))
                    local_hour = local_dt.hour
            except Exception:
                pass  # local_hour stays -1 → WU skipped

        running_max = round(running_max) if unit == "F" else round(running_max, 1)
        return {
            "running_max":      running_max,
            "is_finalized":     bool(data.get("is_finalized")),
            "local_hour":       local_hour,
            "station_timezone": tz_name,
        }
    except Exception as e:
        print(f"  [WU-MAX] {city_slug} {date_str}: {e}")
        return None

def get_actual_temp(city_slug, date_str):
    """Deprecated: use get_wu_actual() instead. Kept for fallback."""
    loc     = LOCATIONS[city_slug]
    station = loc["station"]
    unit    = loc["unit"]
    vc_unit = "us" if unit == "F" else "metric"
    url = (
        f"https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/timeline"
        f"/{station}/{date_str}/{date_str}"
        f"?unitGroup={vc_unit}&key={VC_KEY}&include=days&elements=tempmax"
    )
    try:
        data = requests.get(url, timeout=(5, 8)).json()
        days = data.get("days", [])
        if days and days[0].get("tempmax") is not None:
            return round(float(days[0]["tempmax"]), 1)
    except Exception as e:
        print(f"  [VC] {city_slug} {date_str}: {e}")
    return None

def get_wu_actual(city_slug, date_str):
    """Fetch the WU daily high for a finalized date. Used for calibration/resolution.

    This is the authoritative source — Polymarket markets resolve on WU station
    readings, so calibration must use WU data (not Visual Crossing) to be meaningful.
    Returns None if WU is unavailable, day is not yet finalized, or no data.
    """
    if not WU_API_VALID:
        return None
    loc           = LOCATIONS[city_slug]
    station       = loc["station"]
    expected_unit = loc["unit"]
    url           = f"{WU_API_URL}/weather/{station}/daily?date={date_str}"
    for attempt in range(2):
        try:
            resp = requests.get(url, timeout=(5, 10))
            if not resp.ok:
                return None
            data = resp.json()
            if not data.get("is_finalized"):
                return None
            high = data.get("high")
            if high is None:
                return None
            high    = float(high)
            wu_unit = data.get("unit", expected_unit)
            if wu_unit != expected_unit:
                if expected_unit == "F":
                    high = round(high * 9 / 5 + 32)
                else:
                    high = round((high - 32) * 5 / 9, 1)
            else:
                high = round(high) if expected_unit == "F" else round(high, 1)
            return high
        except Exception as e:
            if attempt == 0:
                time.sleep(1)
            else:
                print(f"  [WU-ACTUAL] {city_slug} {date_str}: {e}")
    return None

def _blend_iv(temps_and_sigmas):
    """Inverse-variance weighted blend of (temp, sigma) tuples.

    Temperature: IV-weighted mean (correct — down-weights noisier sources).
    Sigma: average variance (NOT IV). IV assumes independence, but ECMWF and GFS
    are heavily correlated (~0.7-0.9) and share physics/observations. Treating them
    as independent collapses sigma from 1.2 → 0.85, inflating P by ~12pp and pushing
    marginal trades over the min_confidence gate. Average variance (ρ≈0.5 assumption)
    is conservative and avoids false-precision: two identical-sigma sources blend to
    the same sigma, not σ/√2.
    """
    if not temps_and_sigmas:
        return None, None
    if len(temps_and_sigmas) == 1:
        return temps_and_sigmas[0]
    weights       = [1.0 / (s ** 2) for _, s in temps_and_sigmas]
    total_w       = sum(weights)
    blended_temp  = sum(t * w for (t, _), w in zip(temps_and_sigmas, weights)) / total_w
    blended_sigma = math.sqrt(sum(s ** 2 for _, s in temps_and_sigmas) / len(temps_and_sigmas))
    return round(blended_temp, 1), round(blended_sigma, 3)

def take_forecast_snapshot(city_slug, dates, horizon_map=None):
    """
    Fetches ECMWF and GFS forecasts, blends them, and returns per-date snapshots.

    For D+0, replaces the old METAR (instantaneous) with the WU running_max
    (the highest reading recorded so far today). The running_max is what the
    Polymarket market will resolve on, making it the correct D+0 signal.

    WU sigma is tiered by time of day:
      - Finalized (day over): 0.3F/0.2C — station reading is the resolution value
      - Afternoon (local hour >= 14): 1.0F/0.6C — most of the day observed
      - Morning (local hour >= 8): 1.5F/1.0C — significant afternoon swing still possible
      - Early (<8h local): WU observation skipped (too early, high uncertainty)

    Snapshots store GFS data under the key "hrrr" for backward compatibility
    with run_calibration() and existing market files.
    """
    now_str = datetime.now(timezone.utc).isoformat()
    ecmwf   = get_ecmwf(city_slug, dates)
    gfs     = get_gfs(city_slug, dates)     # stored as "hrrr" in snapshots
    today   = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    loc     = LOCATIONS[city_slug]

    snapshots = {}
    for date in dates:
        h = horizon_map.get(date) if horizon_map else None

        # WU running_max for D+0 (replaces METAR)
        wu_obs   = None
        wu_sigma = None
        if date == today and WU_API_VALID:
            obs = get_wu_running_max(city_slug, date)
            if obs and obs["running_max"] is not None:
                is_us = loc["unit"] == "F"
                if obs["is_finalized"]:
                    sig = SIGMA_WU_F_FINAL if is_us else SIGMA_WU_C_FINAL
                elif obs["local_hour"] >= 14:
                    sig = SIGMA_WU_F if is_us else SIGMA_WU_C
                elif obs["local_hour"] >= 8:
                    sig = SIGMA_WU_F_EARLY if is_us else SIGMA_WU_C_EARLY
                else:
                    sig = None   # too early; skip
                if sig is not None:
                    wu_obs   = obs
                    wu_sigma = sig

        snap = {
            "ts":             now_str,
            "ecmwf":          ecmwf.get(date),
            "hrrr":           gfs.get(date),    # stored as "hrrr" for backward compat
            "metar":          None,              # deprecated — kept for schema compat
            "wu_running_max": wu_obs["running_max"] if wu_obs else None,
            "wu_sigma":       wu_sigma,
        }

        # model_delta: how far apart are ECMWF and GFS?
        # Used by find_best_entry() to block entries when models disagree.
        if snap["ecmwf"] is not None and snap["hrrr"] is not None:
            snap["model_delta"] = round(abs(snap["ecmwf"] - snap["hrrr"]), 1)
        else:
            snap["model_delta"] = None

        sources_for_blend = []
        source_names      = []
        if snap["ecmwf"] is not None:
            s = get_sigma(city_slug, "ecmwf", horizon=h)
            sources_for_blend.append((snap["ecmwf"], s))
            source_names.append("ecmwf")
        if snap["hrrr"] is not None:
            s = get_sigma(city_slug, "hrrr", horizon=h)
            sources_for_blend.append((snap["hrrr"], s))
            source_names.append("gfs")
        if wu_obs is not None:
            sources_for_blend.append((wu_obs["running_max"], wu_sigma))
            source_names.append("wu_running_max")

        if sources_for_blend:
            bt, bs = _blend_iv(sources_for_blend)
            snap["best"]         = bt
            snap["best_sigma"]   = bs
            snap["best_source"]  = "blend" if len(source_names) > 1 else source_names[0]
            snap["sources_used"] = source_names
        else:
            snap["best"]         = None
            snap["best_sigma"]   = None
            snap["best_source"]  = None
            snap["sources_used"] = []

        snapshots[date] = snap
    return snapshots

# =============================================================================
# POLYMARKET
# =============================================================================

def get_polymarket_event(city_slug, month, day, year):
    slug = f"highest-temperature-in-{city_slug}-on-{month}-{day}-{year}"
    try:
        r    = requests.get(f"https://gamma-api.polymarket.com/events?slug={slug}", timeout=(5, 8))
        data = r.json()
        if data and isinstance(data, list) and len(data) > 0:
            return data[0]
    except Exception:
        pass
    return None

def get_market_price(market_id):
    try:
        r      = requests.get(f"https://gamma-api.polymarket.com/markets/{market_id}", timeout=(3, 5))
        prices = json.loads(r.json().get("outcomePrices", "[0.5,0.5]"))
        return float(prices[0])
    except Exception:
        return None

def check_market_resolved(market_id):
    """
    Returns (True, closed_at) for YES win, (False, closed_at) for loss,
    (None, closed_at) if still open or indeterminate.
    """
    try:
        r    = requests.get(f"https://gamma-api.polymarket.com/markets/{market_id}", timeout=(5, 8))
        data = r.json()
        closed    = data.get("closed", False)
        closed_at = data.get("endDate") or data.get("closedTime") or ""
        if not closed:
            return None, ""
        prices    = json.loads(data.get("outcomePrices", "[0.5,0.5]"))
        yes_price = float(prices[0])
        if yes_price >= 0.95:
            return True, closed_at
        elif yes_price <= 0.05:
            return False, closed_at
        return None, closed_at
    except Exception as e:
        print(f"  [RESOLVE] {market_id}: {e}")
    return None, ""

def parse_temp_range(question):
    if not question:
        return None
    num = r'(-?\d+(?:\.\d+)?)'
    if re.search(r'or below', question, re.IGNORECASE):
        m = re.search(num + r'[°]?[FC] or below', question, re.IGNORECASE)
        if m:
            return (-999.0, float(m.group(1)))
    if re.search(r'or higher', question, re.IGNORECASE):
        m = re.search(num + r'[°]?[FC] or higher', question, re.IGNORECASE)
        if m:
            return (float(m.group(1)), 999.0)
    m = re.search(r'between ' + num + r'-' + num + r'[°]?[FC]', question, re.IGNORECASE)
    if m:
        return (float(m.group(1)), float(m.group(2)))
    m = re.search(r'be ' + num + r'[°]?[FC] on', question, re.IGNORECASE)
    if m:
        v = float(m.group(1))
        return (v, v)
    return None

def hours_to_resolution(end_date_str):
    try:
        end = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        return max(0.0, (end - datetime.now(timezone.utc)).total_seconds() / 3600)
    except Exception:
        return 999.0

def in_bucket(forecast, t_low, t_high):
    if t_low == t_high:
        return round(float(forecast)) == round(t_low)
    return t_low <= float(forecast) <= t_high

# =============================================================================
# MARKET DATA STORAGE
# =============================================================================

def market_path(city_slug, date_str):
    d = SIM_MARKETS_DIR if SIM_MODE else MARKETS_DIR
    return d / f"{city_slug}_{date_str}.json"

def load_market(city_slug, date_str):
    p = market_path(city_slug, date_str)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return None

def save_market(market):
    p = market_path(market["city"], market["date"])
    p.write_text(json.dumps(market, indent=2, ensure_ascii=False), encoding="utf-8")

def load_all_markets():
    d = SIM_MARKETS_DIR if SIM_MODE else MARKETS_DIR
    markets = []
    for f in d.glob("*.json"):
        try:
            markets.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass
    return markets

def new_market(city_slug, date_str, event, hours):
    loc = LOCATIONS[city_slug]
    return {
        "city":               city_slug,
        "city_name":          loc["name"],
        "date":               date_str,
        "unit":               loc["unit"],
        "station":            loc["station"],
        "event_end_date":     event.get("endDate", ""),
        "hours_at_discovery": round(hours, 1),
        "status":             "open",
        "cycles":             [],
        "actual_temp":        None,
        "resolved_outcome":   None,
        "pnl":                None,
        "forecast_snapshots": [],
        "market_snapshots":   [],
        "all_outcomes":       [],
        "created_at":         datetime.now(timezone.utc).isoformat(),
    }

def get_active_cycle(market_data):
    """Return the currently open cycle dict, or None."""
    for cycle in market_data.get("cycles", []):
        if cycle.get("status") == "open":
            return cycle
    return None

# =============================================================================
# STATE
# =============================================================================

def load_state():
    sf = SIM_STATE_FILE if SIM_MODE else STATE_FILE
    if sf.exists():
        return json.loads(sf.read_text(encoding="utf-8"))
    if SIM_MODE:
        print(f"  [SIM] Starting sim with ${SIM_BALANCE:.2f} virtual balance")
        return {
            "balance":          SIM_BALANCE,
            "starting_balance": SIM_BALANCE,
            "net_pnl":          0.0,
            "total_trades":     0,
            "profitable_exits": 0,
            "losing_exits":     0,
            "resolved_wins":    0,
            "resolved_losses":  0,
            "peak_balance":     SIM_BALANCE,
        }
    # Fresh start: use actual on-chain balance, fall back to config value
    real_bal = get_real_balance()
    starting = round(real_bal, 2) if real_bal is not None else BALANCE
    if real_bal is not None:
        print(f"  [INIT] On-chain balance detected: ${starting:.2f} — using as starting balance")
    else:
        print(f"  [INIT] Could not fetch on-chain balance — using config default ${starting:.2f}")
    return {
        "balance":          starting,
        "starting_balance": starting,
        "net_pnl":          0.0,
        "total_trades":     0,
        "profitable_exits": 0,
        "losing_exits":     0,
        "resolved_wins":    0,
        "resolved_losses":  0,
        "peak_balance":     starting,
    }

def save_state(state):
    sf = SIM_STATE_FILE if SIM_MODE else STATE_FILE
    sf.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

# =============================================================================
# ENTRY EVALUATION
# =============================================================================

def is_price_stable_or_rising(market_record, token_id, window=2):
    """
    Returns True if the bucket's price is flat or rising over recent scans.
    Returns True when no history exists (first scan — allow entry).
    A price is "falling" if it dropped more than 5% over the last N snapshots.
    """
    snaps_with_prices = [
        s for s in market_record.get("market_snapshots", [])
        if isinstance(s.get("prices"), dict)
    ]
    if len(snaps_with_prices) < 2:
        return True  # no price history yet — allow entry

    recent = snaps_with_prices[-window:]
    prices = [s["prices"].get(token_id) for s in recent]
    prices = [p for p in prices if p is not None]

    if len(prices) < 2:
        return True  # token not in history yet

    return prices[-1] >= prices[0] * 0.95


def market_implied_temp(outcomes):
    """
    Compute the market-implied temperature as a probability-weighted average of
    bucket midpoints. Terminal buckets use their finite boundary as the midpoint
    proxy (e.g. "≤14°C" → 14, "≥24°C" → 24). Used as a cross-check against the
    model forecast — large disagreements signal that the crowd knows something
    the models don't.
    """
    total_price = 0.0
    weighted    = 0.0
    for o in outcomes:
        t_low, t_high = o["range"]
        price         = o.get("price", 0.0)
        if t_low == -999:
            midpoint = t_high
        elif t_high == 999:
            midpoint = t_low
        else:
            midpoint = (t_low + t_high) / 2.0
        weighted    += midpoint * price
        total_price += price
    if total_price <= 0:
        return None
    return round(weighted / total_price, 1)


def find_best_entry(outcomes, forecast_temp, sigma, balance, market_record,
                    model_delta=None):
    """
    Find the best entry candidate using the v4 confidence-first strategy.

    Scores all buckets by probability descending. Checks the highest-probability
    bucket against all strategy gates. Returns an entry dict or None.

    Strategy gates (failures skip the market entirely — no fallback):
      0. Model agreement: |ECMWF - GFS| <= unit-aware cap — block market when
         models disagree. Cap is 2.0°F or 1.1°C (≈ same number of buckets).
      0b. Market-vs-model gap: if the crowd's implied temperature differs from
          our forecast by > MAX_CROWD_GAP_BUCKS bucket-widths, block entry. The
          crowd is pricing a different scenario — the model is likely wrong.
      1. Price in opportunity zone: MIN_ENTRY_PRICE..MAX_ENTRY_PRICE
      2. Minimum edge: P - market_price >= MIN_EDGE (not just any positive EV)
      3. Confidence: P >= min_confidence

    Data/liquidity gates (use continue — bucket valid, data may improve):
      4. Volume >= MIN_VOLUME
      5. Price trend: flat or rising vs last scan
      6. Bet size >= MIN_BET after Kelly sizing
    """
    if not outcomes or forecast_temp is None:
        return None

    unit = market_record.get("unit") or LOCATIONS.get(market_record.get("city"), {}).get("unit", "F")

    # Gate 0: model agreement — block entire market before inspecting any bucket.
    # Cap differs by unit because 1°F and 1°C are the same number of buckets but
    # very different magnitudes; a flat constant lets too much through in C.
    if model_delta is not None and model_delta > model_delta_cap(unit):
        return None

    # Gate 0b: market-vs-model gap — expressed in bucket-widths, so 1.0 means
    # "the crowd is pricing one whole bucket away from us."  Munich 2026-04-29:
    # market_temp ≈ 17.4, forecast 16.6 → gap 0.8 < 1.0 (just under the bar).
    # If you tighten this to 0.7, that case would have been blocked.
    market_temp = market_implied_temp(outcomes)
    if market_temp is not None:
        crowd_gap = abs(market_temp - forecast_temp)
        if crowd_gap > crowd_gap_cap(unit):
            return None

    min_conf = _strategy["min_confidence"]

    # Score all buckets by probability descending
    scored = []
    for o in outcomes:
        t_low, t_high = o["range"]
        p = bucket_prob(forecast_temp, t_low, t_high, sigma)
        scored.append((p, o))
    scored.sort(key=lambda x: x[0], reverse=True)

    for p, o in scored:
        # Hard longshot floor — sorted desc, so all remaining also fail
        if p < 0.10:
            break

        yes_price = o["price"]

        # --- STRATEGY GATES: top-bucket failure = skip market entirely ---

        # Gate 1: opportunity zone
        if yes_price < MIN_ENTRY_PRICE or yes_price > MAX_ENTRY_PRICE:
            return None

        # Gate 2: minimum edge above market price.
        # Requires our model to be meaningfully above the market-implied probability.
        # Stricter than a simple EV > 0 check. Replaces the old min_ev gate.
        edge = p - yes_price
        if edge < MIN_EDGE:
            return None

        # Gate 3: confidence-first — must clear the min_confidence threshold.
        # List is sorted descending, so all remaining buckets are also below threshold.
        if p < min_conf:
            return None

        # --- DATA / LIQUIDITY GATES: use continue, not return None ---

        # Gate 4: minimum volume
        if o.get("volume", 0) < MIN_VOLUME:
            continue

        # Gate 5: price trend (flat or rising — falling prices = market moving against us)
        if not is_price_stable_or_rising(market_record, o.get("token_id", ""), window=4):
            continue

        # Gate 6: minimum bet size after Kelly sizing
        kelly = calc_kelly(p, yes_price)
        size  = bet_size(kelly, balance)
        if size < MIN_BET:
            continue

        ev = calc_ev(p, yes_price)
        return {
            "market_id":    o["market_id"],
            "token_id":     o["token_id"],
            "question":     o["question"],
            "bucket_low":   o["range"][0],
            "bucket_high":  o["range"][1],
            "entry_price":  yes_price,
            "shares":       round(size / yes_price, 2),
            "cost":         size,
            "p":            round(p, 4),
            "edge":         round(edge, 4),
            "ev":           round(ev, 4),
            "kelly":        round(kelly, 4),
        }

    return None


def evaluate_reentry(cycles, outcomes, forecast_temp, sigma, hours):
    """
    Evaluate whether re-entry on the same market is allowed.

    Returns a partial entry dict for the same bucket, or None.

    Re-entry gates (all must pass):
      1. Last cycle was profitable (pnl > 0)
      2. Current price < last exit price (not chasing)
      3. Current price <= MAX_REENTRY_PRICE (still in opportunity zone)
      4. hours >= MIN_REENTRY_HOURS
      5. Fresh P >= min_confidence on the same bucket
    """
    if not cycles:
        return None

    last = cycles[-1]
    if (last.get("pnl") or 0) <= 0:
        return None  # last cycle was not profitable

    if hours < MIN_REENTRY_HOURS:
        return None

    last_exit_price = last.get("exit_price") or 1.0
    last_token      = last.get("token_id")
    t_low           = last.get("bucket_low")
    t_high          = last.get("bucket_high")

    if not last_token or t_low is None or t_high is None:
        return None

    # Find the same bucket in current outcomes
    current_outcome = next((o for o in outcomes if o.get("token_id") == last_token), None)
    if current_outcome is None:
        return None

    current_price = current_outcome["price"]

    # Gate: price must have come back down below where we sold
    if current_price >= last_exit_price:
        return None

    # Gate: still in opportunity zone
    if current_price > MAX_REENTRY_PRICE or current_price < MIN_ENTRY_PRICE:
        return None

    # Gate: fresh confidence check on the same bucket using current forecast.
    if forecast_temp is None:
        return None
    p = bucket_prob(forecast_temp, t_low, t_high, sigma)
    if p < _strategy["min_confidence"]:
        return None

    return {
        "market_id":   last["market_id"],
        "token_id":    last_token,
        "question":    last.get("question"),
        "bucket_low":  t_low,
        "bucket_high": t_high,
        "price":       current_price,
        "p":           round(p, 4),
        "volume":      current_outcome.get("volume", 0),
    }

# =============================================================================
# CORE SCAN LOOP
# =============================================================================

def scan_and_update():
    """Full scan: update forecasts, open/close positions, calibrate, tune."""
    global _cal
    now   = datetime.now(timezone.utc)
    state = load_state()

    real_bal = get_real_balance()
    balance  = real_bal if real_bal is not None else state["balance"]

    new_pos  = 0
    closed   = 0
    resolved = 0

    # Load all markets once before the scan loop for portfolio cap checks.
    all_mkts_cache = load_all_markets()

    for city_slug, loc in LOCATIONS.items():
        if loc.get("region") not in SCAN_REGIONS:
            continue
        unit     = loc["unit"]
        unit_sym = "F" if unit == "F" else "C"
        print(f"  -> {loc['name']}...", end=" ", flush=True)

        try:
            dates       = [(now + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(4)]
            horizon_map = {d: i for i, d in enumerate(dates)}

            snapshots = take_forecast_snapshot(city_slug, dates, horizon_map=horizon_map)
            time.sleep(0.3)
        except Exception as e:
            print(f"skipped ({e})")
            continue

        for i, date in enumerate(dates):
            dt    = datetime.strptime(date, "%Y-%m-%d")
            event = get_polymarket_event(city_slug, MONTHS[dt.month - 1], dt.day, dt.year)
            if not event:
                continue

            end_date = event.get("endDate", "")
            hours    = hours_to_resolution(end_date) if end_date else 0
            horizon  = f"D+{i}"

            mkt = load_market(city_slug, date)
            if mkt is None:
                if hours < MIN_HOURS or hours > MAX_HOURS:
                    continue
                mkt = new_market(city_slug, date, event, hours)

            if mkt["status"] == "resolved":
                continue

            # Parse all outcome buckets for this event date
            outcomes = []
            for market in event.get("markets", []):
                question = market.get("question", "")
                mid      = str(market.get("id", ""))
                volume   = float(market.get("volume", 0))
                rng      = parse_temp_range(question)
                if not rng:
                    continue
                try:
                    prices    = json.loads(market.get("outcomePrices", "[0.5,0.5]"))
                    yes_price = float(prices[0])
                except Exception:
                    continue
                token_id = ""
                try:
                    clob_ids = market.get("clobTokenIds")
                    if isinstance(clob_ids, str):
                        clob_ids = json.loads(clob_ids)
                    if clob_ids:
                        token_id = str(clob_ids[0])
                except Exception:
                    pass
                outcomes.append({
                    "question":  question,
                    "market_id": mid,
                    "token_id":  token_id,
                    "range":     rng,
                    "price":     round(yes_price, 4),
                    "volume":    round(volume, 0),
                })
            outcomes.sort(key=lambda x: x["range"][0])
            mkt["all_outcomes"] = outcomes

            # Forecast snapshot for this date
            snap          = snapshots.get(date, {})
            forecast_temp = snap.get("best")
            best_source   = snap.get("best_source")
            sigma         = snap.get("best_sigma") or get_sigma(city_slug, best_source or "ecmwf", horizon=i)
            model_delta   = snap.get("model_delta")

            # Bias-correct individual model forecasts and re-blend for decision-making.
            # Raw model values stored in snap are preserved in the market file for calibration.
            # Bias corrections always apply when calibration data exists (independent of WU).
            # WU running_max is added only when the WU API is available.
            ecmwf_raw       = snap.get("ecmwf")
            gfs_raw         = snap.get("hrrr")
            corrected       = []
            ecmwf_corrected = None
            gfs_corrected   = None
            if ecmwf_raw is not None:
                bias_e          = get_bias(city_slug, "ecmwf", horizon=i)
                ecmwf_corrected = round(ecmwf_raw + bias_e, 1)
                corrected.append((ecmwf_corrected, get_sigma(city_slug, "ecmwf", horizon=i)))
            if gfs_raw is not None:
                bias_g        = get_bias(city_slug, "hrrr", horizon=i)
                gfs_corrected = round(gfs_raw + bias_g, 1)
                corrected.append((gfs_corrected, get_sigma(city_slug, "hrrr", horizon=i)))
            # Recompute model_delta from corrected temps before WU is added.
            # The gate must use the same temperatures that drive the forecast decision.
            if ecmwf_corrected is not None and gfs_corrected is not None:
                model_delta = round(abs(ecmwf_corrected - gfs_corrected), 1)
            if WU_API_VALID:
                wu_max = snap.get("wu_running_max")
                wu_sig = snap.get("wu_sigma")
                if wu_max is not None and wu_sig is not None:
                    corrected.append((wu_max, wu_sig))
            if corrected:
                bt, bs = _blend_iv(corrected)
                if bt is not None:
                    forecast_temp = bt
                    sigma         = bs or sigma

            mkt["forecast_snapshots"].append({
                "ts":             snap.get("ts"),
                "horizon":        horizon,
                "hours_left":     round(hours, 1),
                "ecmwf":          snap.get("ecmwf"),
                "hrrr":           snap.get("hrrr"),
                "model_delta":    model_delta,
                "metar":          None,
                "wu_running_max": snap.get("wu_running_max"),
                "best":           snap.get("best"),
                "best_source":    snap.get("best_source"),
            })

            # Market snapshot: store per-token prices for trend detection
            mkt["market_snapshots"].append({
                "ts":     snap.get("ts"),
                "prices": {o["token_id"]: o["price"] for o in outcomes if o.get("token_id")},
            })

            ts_now = snap.get("ts") or now.isoformat()

            # ---- RECONCILE: detect orphaned on-chain positions ----
            pos = get_active_cycle(mkt)
            if not SIM_MODE and pos is None and mkt["status"] != "resolved":
                cycles = mkt.get("cycles", [])
                can_reconcile = (
                    len(cycles) < MAX_CYCLES and
                    not (cycles and cycles[-1].get("closed_at") and
                         (now - datetime.fromisoformat(
                             cycles[-1]["closed_at"].replace("Z", "+00:00")
                         )).total_seconds() < 120)
                )
                if can_reconcile and outcomes:
                    last_cycle = cycles[-1] if cycles else None
                    candidates_to_check = []
                    if last_cycle and last_cycle.get("token_id"):
                        candidates_to_check.append((last_cycle["token_id"], last_cycle["bucket_low"], last_cycle["bucket_high"]))
                    for o in outcomes:
                        if o.get("token_id") and (not candidates_to_check or o["token_id"] != candidates_to_check[0][0]):
                            candidates_to_check.append((o["token_id"], o["range"][0], o["range"][1]))

                    for tid, t_low, t_high in candidates_to_check:
                        onchain = get_token_balance(tid)
                        if onchain >= 1.0:
                            matching   = next((o for o in outcomes if o.get("token_id") == tid), None)
                            cp         = matching["price"] if matching else (last_cycle["entry_price"] if last_cycle else 0.5)
                            real_entry = get_real_entry_price(tid)
                            entry      = real_entry if real_entry is not None else cp
                            cycle_num  = len(cycles) + 1
                            recon_p    = round(bucket_prob(forecast_temp, t_low, t_high, sigma), 4) \
                                         if forecast_temp is not None else None
                            print(f"  [RECONCILE] {loc['name']} {date} [C{cycle_num}] — "
                                  f"{onchain:.1f} orphaned shares ({t_low}-{t_high}{unit_sym}) "
                                  f"entry ${entry:.4f}, market ${cp:.3f}")
                            mkt["cycles"].append({
                                "cycle_num":          cycle_num,
                                "market_id":          last_cycle["market_id"] if last_cycle else (matching["market_id"] if matching else ""),
                                "token_id":           tid,
                                "question":           last_cycle.get("question") if last_cycle else (matching["question"] if matching else ""),
                                "bucket_low":         t_low,
                                "bucket_high":        t_high,
                                "entry_price":        entry,
                                "shares":             round(onchain, 2),
                                "cost":               round(onchain * entry, 2),
                                "p":                  recon_p,
                                "edge":               None,
                                "ev":                 None,
                                "kelly":              None,
                                "forecast_temp":      forecast_temp,
                                "forecast_src":       best_source,
                                "sigma":              sigma,
                                "ecmwf_at_entry":     snap.get("ecmwf"),
                                "gfs_at_entry":       snap.get("hrrr"),
                                "model_delta":        model_delta,
                                "opened_at":          ts_now,
                                "status":             "open",
                                "pnl":                None,
                                "exit_price":         None,
                                "close_reason":       None,
                                "closed_at":          None,
                                "order_id":           None,
                                "stop_price":         round(entry * STOP_LOSS_PCT, 4),
                                "trailing_activated": False,
                                "peak_price":         None,
                                "reconciled":         True,
                            })
                            break
                        time.sleep(0.05)

            # ---- EXIT CHECKS ----
            # Take-profit and trailing-stop are price-based; loss-side exit is
            # forecast-based (forecast_diverged) because price-based stops
            # don't cap loss in weather markets — prices gap to zero on resolution.
            pos = get_active_cycle(mkt)
            if pos is not None:
                current_price = next(
                    (o["price"] for o in outcomes if o["market_id"] == pos["market_id"]),
                    None
                )
                if current_price is not None:
                    entry         = pos["entry_price"]
                    stop          = pos.get("stop_price", entry * STOP_LOSS_PCT)
                    roi_threshold = entry * (1.0 + _strategy["take_profit_roi"])

                    # True trailing stop: once up 25%, trail TRAILING_DISTANCE below the peak.
                    # Never locks to breakeven — stop follows the peak price downward at a distance,
                    # so a small price wobble back to entry does not eject the position.
                    if current_price >= entry * TRAILING_ACTIVATION or pos.get("trailing_activated"):
                        pos["trailing_activated"] = True
                        peak     = max(current_price, pos.get("peak_price") or 0)
                        pos["peak_price"] = peak
                        new_stop = round(peak * (1 - TRAILING_DISTANCE), 4)
                        if new_stop > stop:
                            pos["stop_price"] = new_stop
                            stop              = new_stop

                    take_triggered     = current_price >= roi_threshold and current_price > entry
                    trailing_triggered = pos.get("trailing_activated") and current_price <= stop
                    diverged_triggered = forecast_diverged(forecast_temp,
                                                           pos.get("bucket_low"),
                                                           pos.get("bucket_high"))

                    if take_triggered or trailing_triggered or diverged_triggered:
                        onchain = pos["shares"] if SIM_MODE else get_token_balance(pos["token_id"])
                        if onchain < 1.0:
                            # Already sold externally
                            reason = "sold_externally"
                            pnl    = round((current_price - entry) * pos["shares"], 2)
                            pos["exit_price"]   = current_price
                            pos["pnl"]          = pnl
                            pos["status"]       = "closed"
                            pos["close_reason"] = reason
                            pos["closed_at"]    = ts_now
                            closed += 1
                            state["net_pnl"] = round(state.get("net_pnl", 0.0) + pnl, 2)
                            if pnl > 0:
                                state["profitable_exits"] = state.get("profitable_exits", 0) + 1
                            else:
                                state["losing_exits"] = state.get("losing_exits", 0) + 1
                            print(f"  [CLOSED] {loc['name']} {date} — 0 on-chain, already sold")
                        else:
                            resp = place_sell_order(pos["token_id"], pos["shares"], current_price, market_id=pos["market_id"])
                            if resp is not None:
                                if take_triggered:
                                    reason = "take_profit_roi"
                                    label  = "TAKE ROI"
                                elif diverged_triggered:
                                    reason = "forecast_diverged"
                                    label  = "FORECAST EXIT"
                                else:
                                    reason = "trailing_stop"
                                    label  = "TRAILING"
                                pnl      = round((current_price - entry) * pos["shares"], 2)
                                balance += pos["cost"] + pnl
                                pos["exit_price"]   = current_price
                                pos["pnl"]          = pnl
                                pos["status"]       = "closed"
                                pos["close_reason"] = reason
                                pos["closed_at"]    = ts_now
                                state["net_pnl"] = round(state.get("net_pnl", 0.0) + pnl, 2)
                                if pnl > 0:
                                    state["profitable_exits"] = state.get("profitable_exits", 0) + 1
                                else:
                                    state["losing_exits"] = state.get("losing_exits", 0) + 1
                                closed += 1
                                print(f"  [{label}] {loc['name']} {date} [C{pos['cycle_num']}] | "
                                      f"entry ${entry:.3f} exit ${current_price:.3f} | "
                                      f"PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")
                            else:
                                print(f"  [SELL FAIL] {loc['name']} {date} — will retry next cycle")

            # ---- OPEN POSITION ----
            pos        = get_active_cycle(mkt)
            cycles_all = mkt.get("cycles", [])

            if pos is None and len(cycles_all) < MAX_CYCLES and hours >= MIN_HOURS:
                # Portfolio-level caps
                total_open = sum(1 for m in all_mkts_cache if get_active_cycle(m) is not None)
                date_open  = sum(1 for m in all_mkts_cache if get_active_cycle(m) is not None and m["date"] == date)
                if total_open >= MAX_OPEN_POS or date_open >= MAX_POS_PER_DATE:
                    save_market(mkt)
                    time.sleep(0.1)
                    continue

                # City-level circuit breaker: if this city just took multiple
                # losses in the rolling window, pause new entries on it. Blocks
                # loss clusters when one city's model is acutely off-calibration.
                if city_recently_lost(city_slug, all_mkts_cache, now):
                    save_market(mkt)
                    time.sleep(0.1)
                    continue

                candidate = None

                if not cycles_all:
                    # First entry: find highest-probability bucket
                    candidate = find_best_entry(
                        outcomes, forecast_temp, sigma, balance, mkt,
                        model_delta=model_delta
                    )
                else:
                    # Re-entry: evaluate risk gates on the same bucket.
                    # Gate 0 also applies: model disagreement predicts wrong-bucket re-entry.
                    reentry = evaluate_reentry(cycles_all, outcomes, forecast_temp, sigma, hours) \
                              if model_delta is None or model_delta <= model_delta_cap(loc["unit"]) else None
                    if reentry is not None and reentry["volume"] >= MIN_VOLUME:
                        price    = reentry["price"]
                        p        = reentry["p"]
                        kelly    = calc_kelly(p, price)
                        # Cap size at cycle 1's original cost (no escalating bets)
                        max_size = cycles_all[0].get("cost", MAX_BET)
                        size     = min(bet_size(kelly, balance), max_size)
                        if size >= MIN_BET:
                            edge = p - price
                            candidate = {
                                "market_id":    reentry["market_id"],
                                "token_id":     reentry["token_id"],
                                "question":     reentry["question"],
                                "bucket_low":   reentry["bucket_low"],
                                "bucket_high":  reentry["bucket_high"],
                                "entry_price":  price,
                                "shares":       round(size / price, 2),
                                "cost":         size,
                                "p":            p,
                                "edge":         round(edge, 4),
                                "ev":           round(calc_ev(p, price), 4),
                                "kelly":        round(kelly, 4),
                            }

                if candidate is not None:
                    # Confirm real ask / spread before placing
                    skip = False
                    try:
                        r     = requests.get(
                            f"https://gamma-api.polymarket.com/markets/{candidate['market_id']}",
                            timeout=(3, 5)
                        )
                        mdata = r.json()
                        real_ask    = float(mdata.get("bestAsk", candidate["entry_price"]))
                        real_bid    = float(mdata.get("bestBid", candidate["entry_price"]))
                        real_spread = round(real_ask - real_bid, 4)
                        if real_spread > MAX_SLIPPAGE or real_ask > MAX_ENTRY_PRICE or real_ask < MIN_ENTRY_PRICE:
                            print(f"  [SKIP] {loc['name']} {date} — ask ${real_ask:.3f} spread ${real_spread:.3f}")
                            skip = True
                        else:
                            candidate["entry_price"] = real_ask
                            candidate["shares"]      = round(candidate["cost"] / real_ask, 2)
                            candidate["ev"]          = round(calc_ev(candidate["p"], real_ask), 4)
                            candidate["edge"]        = round(candidate["p"] - real_ask, 4)
                    except Exception as e:
                        print(f"  [WARN] Could not fetch real ask: {e}")

                    if not skip:
                        if not candidate.get("token_id"):
                            print(f"  [SKIP] {loc['name']} {date} — no token_id")
                        else:
                            existing = get_token_balance(candidate["token_id"])
                            if existing >= 1.0:
                                print(f"  [SKIP] {loc['name']} {date} — already hold {existing:.1f} shares")
                            else:
                                resp = place_buy_order(candidate["token_id"], candidate["cost"])
                                if resp is not None:
                                    cycle_num    = len(mkt["cycles"]) + 1
                                    bucket_label = f"{candidate['bucket_low']}-{candidate['bucket_high']}{unit_sym}"
                                    entry_type   = "RE-ENTRY" if cycles_all else "BUY"
                                    delta_str    = f" d={model_delta:.1f}{unit_sym}" if model_delta is not None else ""
                                    mkt["cycles"].append({
                                        "cycle_num":          cycle_num,
                                        "market_id":          candidate["market_id"],
                                        "token_id":           candidate["token_id"],
                                        "question":           candidate["question"],
                                        "bucket_low":         candidate["bucket_low"],
                                        "bucket_high":        candidate["bucket_high"],
                                        "entry_price":        candidate["entry_price"],
                                        "shares":             candidate["shares"],
                                        "cost":               candidate["cost"],
                                        "p":                  candidate["p"],
                                        "edge":               candidate.get("edge"),
                                        "ev":                 candidate["ev"],
                                        "kelly":              candidate["kelly"],
                                        "forecast_temp":      forecast_temp,
                                        "forecast_src":       best_source,
                                        "sigma":              sigma,
                                        "ecmwf_at_entry":     snap.get("ecmwf"),
                                        "gfs_at_entry":       snap.get("hrrr"),
                                        "model_delta":        model_delta,
                                        "opened_at":          ts_now,
                                        "status":             "open",
                                        "pnl":                None,
                                        "exit_price":         None,
                                        "close_reason":       None,
                                        "closed_at":          None,
                                        "order_id":           resp.get("orderID", resp.get("orderId", "")),
                                        "stop_price":         round(candidate["entry_price"] * STOP_LOSS_PCT, 4),
                                        "trailing_activated": False,
                                        "peak_price":         None,
                                        "reconciled":         False,
                                    })
                                    balance -= candidate["cost"]
                                    state["total_trades"] += 1
                                    new_pos += 1
                                    if mkt not in all_mkts_cache:
                                        all_mkts_cache.append(mkt)
                                    print(f"  [{entry_type}] {loc['name']} {horizon} {date} [C{cycle_num}] | "
                                          f"{bucket_label} | ${candidate['entry_price']:.3f} | "
                                          f"P={candidate['p']:.0%} edge={candidate.get('edge', 0):+.2f}{delta_str} | "
                                          f"${candidate['cost']:.2f} ({(best_source or 'blend').upper()})")
                                else:
                                    print(f"  [ORDER FAIL] {loc['name']} {date}")

            if hours < 0.5 and mkt["status"] == "open":
                mkt["status"] = "closed"

            save_market(mkt)
            time.sleep(0.1)

        print("ok")

    # ---- AUTO-RESOLUTION ----
    for mkt in load_all_markets():
        if mkt["status"] == "resolved":
            continue
        pos = get_active_cycle(mkt)
        if pos is None:
            continue
        market_id = pos.get("market_id")
        if not market_id:
            continue

        won, closed_at_str = check_market_resolved(market_id)
        if won is None:
            if closed_at_str:
                try:
                    closed_dt = datetime.fromisoformat(closed_at_str.replace("Z", "+00:00"))
                    if (now - closed_dt).total_seconds() > 48 * 3600:
                        print(f"  [TIMEOUT] {mkt['city_name']} {mkt['date']} — indeterminate after 48h")
                        mkt["status"] = "unresolvable"
                        save_market(mkt)
                except Exception:
                    pass
            continue

        price  = pos["entry_price"]
        shares = pos["shares"]
        cost   = pos["cost"]
        pnl    = round(shares * (1 - price), 2) if won else round(-cost, 2)

        pos["exit_price"]   = 1.0 if won else 0.0
        pos["pnl"]          = pnl
        pos["close_reason"] = "resolved"
        pos["closed_at"]    = now.isoformat()
        pos["status"]       = "closed"
        mkt["pnl"]          = round(sum(c.get("pnl") or 0 for c in mkt["cycles"]), 2)
        mkt["status"]       = "resolved"
        mkt["resolved_outcome"] = "win" if won else "loss"

        if WU_API_VALID:
            actual = get_wu_actual(mkt["city"], mkt["date"])
            if actual is not None:
                mkt["actual_temp"] = actual

        if won:
            state["resolved_wins"]   = state.get("resolved_wins", 0) + 1
        else:
            state["resolved_losses"] = state.get("resolved_losses", 0) + 1
        state["net_pnl"] = round(state.get("net_pnl", 0.0) + pnl, 2)
        if pnl > 0:
            state["profitable_exits"] = state.get("profitable_exits", 0) + 1
        else:
            state["losing_exits"] = state.get("losing_exits", 0) + 1

        result = "WIN" if won else "LOSS"
        print(f"  [{result}] {mkt['city_name']} {mkt['date']} | PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")
        resolved += 1
        save_market(mkt)
        time.sleep(0.3)

    # ---- BACKFILL actual_temp for calibration ----
    if WU_API_VALID:
        for mkt in load_all_markets():
            if mkt.get("actual_temp") is not None:
                continue
            try:
                market_date = datetime.strptime(mkt["date"], "%Y-%m-%d").date()
            except Exception:
                continue
            if market_date < now.date() and mkt["city"] in LOCATIONS:
                actual = get_wu_actual(mkt["city"], mkt["date"])
                if actual is not None:
                    mkt["actual_temp"] = actual
                    save_market(mkt)
                time.sleep(0.2)

    # Sync balance — use tracked sim balance in sim mode, on-chain otherwise
    if SIM_MODE:
        state["balance"] = round(balance, 2)
    else:
        real_bal = get_real_balance()
        state["balance"] = round(real_bal if real_bal is not None else balance, 2)
    state["peak_balance"] = max(state.get("peak_balance", balance), state["balance"])
    save_state(state)

    _cal = run_calibration(load_all_markets())
    if TUNE_ENABLED:
        tune_strategy(load_all_markets())

    return new_pos, closed, resolved

# =============================================================================
# MONITOR POSITIONS  (quick between-scan check)
# =============================================================================

def monitor_positions():
    """
    Quick check of open positions for stop-loss and take-profit.
    Also reconciles orphaned on-chain shares between full scans.
    Exits are price-only — no forecast reads here.
    """
    markets  = load_all_markets()
    open_pos = [m for m in markets if get_active_cycle(m) is not None]

    # Reconcile: check markets with closed cycles but no active position (live only)
    for mkt in ([] if SIM_MODE else markets):
        cycles = mkt.get("cycles", [])
        if not cycles or get_active_cycle(mkt) is not None or mkt["status"] == "resolved":
            continue
        if len(cycles) >= MAX_CYCLES:
            continue
        last_cycle = cycles[-1]
        if not last_cycle.get("token_id"):
            continue
        if last_cycle.get("closed_at"):
            try:
                _lc = datetime.fromisoformat(last_cycle["closed_at"].replace("Z", "+00:00"))
                if (datetime.now(timezone.utc) - _lc).total_seconds() < 120:
                    continue
            except Exception:
                pass
        onchain = get_token_balance(last_cycle["token_id"])
        if onchain >= 1.0:
            city_name     = LOCATIONS.get(mkt["city"], {}).get("name", mkt["city"])
            current_price = last_cycle.get("exit_price", last_cycle["entry_price"])
            try:
                r     = requests.get(f"https://gamma-api.polymarket.com/markets/{last_cycle['market_id']}", timeout=(3, 5))
                mdata = r.json()
                prices = json.loads(mdata.get("outcomePrices", "[]"))
                if prices:
                    current_price = float(prices[0])
            except Exception:
                pass
            real_entry = get_real_entry_price(last_cycle["token_id"])
            entry      = real_entry if real_entry is not None else current_price
            cycle_num  = len(cycles) + 1
            last_fc    = last_cycle.get("forecast_temp")
            last_sigma = last_cycle.get("sigma")
            t_low_r    = last_cycle["bucket_low"]
            t_high_r   = last_cycle["bucket_high"]
            recon_p    = round(bucket_prob(last_fc, t_low_r, t_high_r, last_sigma), 4) \
                         if last_fc is not None and last_sigma is not None else None
            print(f"  [RECONCILE] {city_name} {mkt['date']} [C{cycle_num}] — "
                  f"{onchain:.1f} orphaned shares (entry ${entry:.4f}, market ${current_price:.3f})")
            mkt["cycles"].append({
                "cycle_num":          cycle_num,
                "market_id":          last_cycle["market_id"],
                "token_id":           last_cycle["token_id"],
                "question":           last_cycle.get("question"),
                "bucket_low":         last_cycle["bucket_low"],
                "bucket_high":        last_cycle["bucket_high"],
                "entry_price":        entry,
                "shares":             round(onchain, 2),
                "cost":               round(onchain * entry, 2),
                "p":                  recon_p,
                "edge":               None,
                "ev":                 None,
                "kelly":              None,
                "forecast_temp":      last_fc,
                "forecast_src":       last_cycle.get("forecast_src"),
                "sigma":              last_sigma,
                "ecmwf_at_entry":     last_cycle.get("ecmwf_at_entry"),
                "gfs_at_entry":       last_cycle.get("gfs_at_entry"),
                "model_delta":        last_cycle.get("model_delta"),
                "opened_at":          datetime.now(timezone.utc).isoformat(),
                "status":             "open",
                "pnl":                None,
                "exit_price":         None,
                "close_reason":       None,
                "closed_at":          None,
                "order_id":           None,
                "stop_price":         round(entry * STOP_LOSS_PCT, 4),
                "trailing_activated": False,
                "peak_price":         None,
                "reconciled":         True,
            })
            save_market(mkt)
            open_pos.append(mkt)
        time.sleep(0.05)

    if not open_pos:
        return 0

    state   = load_state()
    balance = state["balance"]
    closed  = 0

    for mkt in open_pos:
        pos = get_active_cycle(mkt)
        if pos is None:
            continue
        mid     = pos["market_id"]
        mutated = False

        # Fetch current best bid
        current_price = None
        try:
            r     = requests.get(f"https://gamma-api.polymarket.com/markets/{mid}", timeout=(3, 5))
            mdata = r.json()
            best_bid = mdata.get("bestBid")
            if best_bid is not None:
                current_price = float(best_bid)
        except Exception:
            pass

        if current_price is None:
            for o in mkt.get("all_outcomes", []):
                if o["market_id"] == mid:
                    current_price = o["price"]
                    break

        if current_price is None:
            continue

        entry         = pos["entry_price"]
        stop          = pos.get("stop_price", entry * STOP_LOSS_PCT)
        roi_threshold = entry * (1.0 + _strategy["take_profit_roi"])
        city_name     = LOCATIONS.get(mkt["city"], {}).get("name", mkt["city"])
        ts_now        = datetime.now(timezone.utc).isoformat()

        # True trailing stop: once up 25%, trail TRAILING_DISTANCE below the peak.
        # Never locks to breakeven — stop follows the peak price downward at a distance,
        # so a small price wobble back to entry does not eject the position.
        if current_price >= entry * TRAILING_ACTIVATION or pos.get("trailing_activated"):
            pos["trailing_activated"] = True
            peak     = max(current_price, pos.get("peak_price") or 0)
            pos["peak_price"] = peak
            new_stop = round(peak * (1 - TRAILING_DISTANCE), 4)
            if new_stop > stop:
                pos["stop_price"] = new_stop
                stop              = new_stop
                mutated = True
                print(f"  [TRAILING] {city_name} {mkt['date']} — stop raised to ${new_stop:.3f} (peak ${peak:.3f})")

        # Monitor loop has no fresh forecast — only price-based exits run here.
        # Trailing stop fires only AFTER trailing has activated (i.e., we were profitable).
        # Forecast-divergence exit runs in the scan loop where the forecast is fresh.
        take_triggered     = current_price >= roi_threshold and current_price > entry
        trailing_triggered = pos.get("trailing_activated") and current_price <= stop

        if take_triggered or trailing_triggered:
            onchain = pos["shares"] if SIM_MODE else get_token_balance(pos["token_id"])
            if onchain < 1.0:
                pnl = round((current_price - entry) * pos["shares"], 2)
                pos["exit_price"]   = current_price
                pos["pnl"]          = pnl
                pos["status"]       = "closed"
                pos["close_reason"] = "sold_externally"
                pos["closed_at"]    = ts_now
                closed  += 1
                mutated  = True
                state["net_pnl"] = round(state.get("net_pnl", 0.0) + pnl, 2)
                if pnl > 0:
                    state["profitable_exits"] = state.get("profitable_exits", 0) + 1
                else:
                    state["losing_exits"] = state.get("losing_exits", 0) + 1
                print(f"  [CLOSED] {city_name} {mkt['date']} — 0 on-chain, already sold")
            else:
                resp = place_sell_order(pos["token_id"], pos["shares"], current_price, market_id=mid)
                if resp is not None:
                    pnl     = round((current_price - entry) * pos["shares"], 2)
                    balance += pos["cost"] + pnl
                    if take_triggered:
                        reason = "take_profit_roi"
                        label  = "TAKE ROI"
                    else:
                        reason = "trailing_stop"
                        label  = "TRAILING"
                    pos["exit_price"]   = current_price
                    pos["pnl"]          = pnl
                    pos["status"]       = "closed"
                    pos["close_reason"] = reason
                    pos["closed_at"]    = ts_now
                    closed  += 1
                    mutated  = True
                    state["net_pnl"] = round(state.get("net_pnl", 0.0) + pnl, 2)
                    if pnl > 0:
                        state["profitable_exits"] = state.get("profitable_exits", 0) + 1
                    else:
                        state["losing_exits"] = state.get("losing_exits", 0) + 1
                    print(f"  [{label}] {city_name} {mkt['date']} [C{pos.get('cycle_num')}] | "
                          f"entry ${entry:.3f} exit ${current_price:.3f} | "
                          f"PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")
                else:
                    print(f"  [SELL FAIL] {city_name} {mkt['date']} — will retry")

        if mutated:
            save_market(mkt)

    if closed:
        if SIM_MODE:
            state["balance"] = round(balance, 2)
        else:
            real_bal = get_real_balance()
            state["balance"] = round(real_bal if real_bal is not None else balance, 2)
        state["peak_balance"] = max(state.get("peak_balance", balance), state["balance"])
        save_state(state)

    return closed

# =============================================================================
# STRATEGY TUNING
# =============================================================================

_TUNE_BOUNDS = {
    "kelly_fraction":  (0.10, 0.60),
    "min_confidence":  (0.33, 0.70),
    "take_profit_roi": (0.20, 0.50),
}
_TUNE_MAX_STEP = 0.10

def tune_strategy(markets):
    """
    Adjust strategy parameters based on recent closed cycles.
    Requires at least 20 closed cycles to activate.

    Tunes:
      kelly_fraction  — actual win rate vs predicted probability (any closed cycle
                        with realised PnL; not restricted to resolved-to-outcome).
      min_confidence  — which confidence band has the best avg PnL.
      take_profit_roi — only ratchets UP when results are strong. The down-branch
                        was removed: high stop rate is caused by bad entries, not
                        by TP being too high, and lowering TP just shrinks wins.
    """
    all_closed = [
        (m, c) for m in markets
        for c in m.get("cycles", [])
        if c.get("pnl") is not None
    ]
    all_closed.sort(key=lambda x: x[1].get("closed_at", ""))
    recent_pairs = all_closed[-TUNE_LOOKBACK:] if len(all_closed) >= 20 else []
    if not recent_pairs:
        return

    old = dict(_strategy)

    # --- kelly_fraction (signal: model accuracy across all closed cycles) ---
    kelly_wins      = sum(1 for _, c in recent_pairs if (c.get("pnl") or 0) > 0)
    actual_wr       = kelly_wins / len(recent_pairs)
    avg_predicted_p = sum(c.get("p") or 0.5 for _, c in recent_pairs) / len(recent_pairs)

    if actual_wr > avg_predicted_p + 0.05:
        adj = min(0.02, _TUNE_MAX_STEP * _strategy["kelly_fraction"])
        _strategy["kelly_fraction"] = min(_strategy["kelly_fraction"] + adj, _TUNE_BOUNDS["kelly_fraction"][1])
    elif actual_wr < avg_predicted_p - 0.05:
        adj = min(0.02, _TUNE_MAX_STEP * _strategy["kelly_fraction"])
        _strategy["kelly_fraction"] = max(_strategy["kelly_fraction"] - adj, _TUNE_BOUNDS["kelly_fraction"][0])

    # --- min_confidence (signal: which exclusive confidence band has best avg PnL) ---
    conf_band_ranges = [(0.35, 0.40), (0.40, 0.45), (0.45, 0.50), (0.50, 0.55), (0.55, 0.60), (0.60, 1.01)]
    band_pnls = {lo: [] for lo, _ in conf_band_ranges}

    for _, c in recent_pairs:
        pos_p = c.get("p")
        if pos_p is None:
            continue
        for lo, hi in conf_band_ranges:
            if lo <= pos_p < hi:
                band_pnls[lo].append(c.get("pnl") or 0)
                break

    best_conf  = _strategy["min_confidence"]
    best_ratio = None
    for lo, pnls in band_pnls.items():
        if len(pnls) >= 3:
            ratio = sum(pnls) / len(pnls)
            if best_ratio is None or ratio > best_ratio:
                best_ratio = ratio
                best_conf  = lo

    current = _strategy["min_confidence"]
    delta   = best_conf - current
    capped  = max(-_TUNE_MAX_STEP * current, min(_TUNE_MAX_STEP * current, delta))
    _strategy["min_confidence"] = round(
        max(_TUNE_BOUNDS["min_confidence"][0], min(_TUNE_BOUNDS["min_confidence"][1], current + capped)), 4
    )

    # --- take_profit_roi (one-way ratchet: only raise when results are strong) ---
    # The previous down-branch lowered TP whenever stop_rate was high, but that
    # response is incorrect for this market: high stop rate is an entry-quality
    # problem, not a TP-too-high problem. Lowering TP just shrinks wins while
    # losses keep their full size and breaks the R:R math.
    total_recent = len(recent_pairs)
    profitable_n = sum(1 for _, c in recent_pairs if (c.get("pnl") or 0) > 0)
    stop_n       = sum(1 for _, c in recent_pairs
                       if c.get("close_reason") in ("stop_loss", "trailing_stop", "forecast_diverged"))

    if total_recent > 0:
        profit_rate = profitable_n / total_recent
        stop_rate   = stop_n / total_recent

        if profit_rate > 0.60 and stop_rate < 0.20:
            adj = min(0.01, _TUNE_MAX_STEP * _strategy["take_profit_roi"])
            _strategy["take_profit_roi"] = min(_strategy["take_profit_roi"] + adj, _TUNE_BOUNDS["take_profit_roi"][1])

    changes = []
    for k in ("min_confidence", "take_profit_roi", "kelly_fraction"):
        if abs(_strategy[k] - old[k]) > 0.001:
            changes.append(f"{k}: {old[k]:.3f}->{_strategy[k]:.3f}")

    if changes:
        print(f"  [TUNE] {', '.join(changes)}")
        try:
            STRATEGY_FILE.write_text(json.dumps(_strategy, indent=2), encoding="utf-8")
        except Exception as e:
            print(f"  [TUNE] Failed to save: {e}")

# =============================================================================
# REPORT / STATUS
# =============================================================================

def print_status():
    state    = load_state()
    markets  = load_all_markets()
    open_pos = [m for m in markets if get_active_cycle(m) is not None]

    bal      = state["balance"]
    start    = state["starting_balance"]
    ret_pct  = (bal - start) / start * 100
    prof     = state.get("profitable_exits", 0)
    loss_e   = state.get("losing_exits", 0)
    r_wins   = state.get("resolved_wins", 0)
    r_loss   = state.get("resolved_losses", 0)
    net_pnl  = state.get("net_pnl", 0.0)
    total    = prof + loss_e
    r_total  = r_wins + r_loss

    real_bal = get_real_balance()
    real_str = f"  On-chain USDC:  ${real_bal:,.2f}" if real_bal is not None else "  On-chain USDC:  (unavailable)"

    print(f"\n{'='*60}")
    print(f"  WEATHERBET — STATUS  (v4 Rewrite)")
    print(f"{'='*60}")
    print(f"  Balance:        ${bal:,.2f}  (start ${start:,.2f}, {'+'if ret_pct>=0 else ''}{ret_pct:.1f}%)")
    print(real_str)
    if total:
        wr_str  = f"{prof/total:.0%}"
        pnl_str = f"{'+'if net_pnl>=0 else ''}{net_pnl:.2f}"
        print(f"  Exits:          {total} | Profitable: {prof} | Losing: {loss_e} | WR: {wr_str} | Net PnL: {pnl_str}")
    else:
        print(f"  Exits:          none yet")
    if r_total:
        print(f"  Resolved:       {r_total} | W: {r_wins} | L: {r_loss} | WR: {r_wins/r_total:.0%}")
    else:
        print(f"  Resolved:       none yet")
    print(f"  Open positions: {len(open_pos)}")
    print(f"\n  Strategy (live): confidence>={_strategy['min_confidence']:.2f}  "
          f"take_profit={_strategy['take_profit_roi']:.0%}  kelly={_strategy['kelly_fraction']:.2f}")
    print(f"  Gates:           model_delta<={MAX_MODEL_DELTA_F}°F/{MAX_MODEL_DELTA_C}°C  "
          f"min_edge>={MIN_EDGE:.2f}  crowd_gap<={MAX_CROWD_GAP_BUCKS:.1f} bucket(s)  "
          f"zone=[{MIN_ENTRY_PRICE:.2f},{MAX_ENTRY_PRICE:.2f}]")

    if open_pos:
        print(f"\n  Open positions:")
        total_unrealized = 0.0
        for m in open_pos:
            pos      = get_active_cycle(m)
            unit_sym = "F" if m["unit"] == "F" else "C"
            label    = f"{pos['bucket_low']}-{pos['bucket_high']}{unit_sym} [C{pos['cycle_num']}]"

            current_price = pos["entry_price"]
            for o in m.get("all_outcomes", []):
                if o["market_id"] == pos["market_id"]:
                    current_price = o["price"]
                    break

            unrealized = round((current_price - pos["entry_price"]) * pos["shares"], 2)
            total_unrealized += unrealized
            pnl_str  = f"{'+'if unrealized>=0 else ''}{unrealized:.2f}"
            delta_str = f" d={pos.get('model_delta'):.1f}{unit_sym}" if pos.get("model_delta") is not None else ""
            print(f"    {m['city_name']:<16} {m['date']} | {label:<20} | "
                  f"entry ${pos['entry_price']:.3f} -> ${current_price:.3f} | "
                  f"P={pos.get('p', 0):.0%}{delta_str} | PnL: {pnl_str}")

        sign = "+" if total_unrealized >= 0 else ""
        print(f"\n  Unrealized PnL: {sign}{total_unrealized:.2f}")

    print(f"{'='*60}\n")


def print_positions():
    """Detailed view of every open position: shares, cost, live P&L, stops."""
    markets  = load_all_markets()
    open_pos = [(m, get_active_cycle(m)) for m in markets]
    open_pos = [(m, c) for m, c in open_pos if c is not None]

    if not open_pos:
        print("\n  No open positions.\n")
        return

    print(f"\n{'='*70}")
    mode_label = " [SIM]" if SIM_MODE else ""
    print(f"  WEATHERBET — OPEN POSITIONS{mode_label}  ({len(open_pos)} total)")
    print(f"{'='*70}")

    total_cost       = 0.0
    total_value      = 0.0
    total_unrealized = 0.0

    for m, pos in sorted(open_pos, key=lambda x: (x[0]["date"], x[0]["city"])):
        unit_sym    = "F" if m["unit"] == "F" else "C"
        city_name   = m["city_name"]
        date        = m["date"]
        bucket      = f"{pos['bucket_low']}-{pos['bucket_high']}{unit_sym}"
        cycle_num   = pos["cycle_num"]

        entry_price = pos["entry_price"]
        shares      = pos["shares"]
        cost        = pos["cost"]
        stop_price  = pos.get("stop_price", round(entry_price * STOP_LOSS_PCT, 4))
        tp_price    = round(entry_price * (1.0 + _strategy["take_profit_roi"]), 4)
        trailing    = pos.get("trailing_activated", False)
        peak        = pos.get("peak_price")
        p           = pos.get("p", 0)
        edge        = pos.get("edge")
        model_delta = pos.get("model_delta")
        opened_at   = pos.get("opened_at", "")

        # Fetch live price from Gamma API; fall back to cached all_outcomes price
        current_price = None
        if not SIM_MODE:
            current_price = get_market_price(pos["market_id"])
        if current_price is None:
            for o in m.get("all_outcomes", []):
                if o["market_id"] == pos["market_id"]:
                    current_price = o["price"]
                    break
        if current_price is None:
            current_price = entry_price

        current_value = round(current_price * shares, 2)
        unrealized    = round((current_price - entry_price) * shares, 2)
        roi_pct       = (current_price - entry_price) / entry_price * 100 if entry_price else 0.0

        total_cost       += cost
        total_value      += current_value
        total_unrealized += unrealized

        pnl_sign   = "+" if unrealized >= 0 else ""
        roi_sign   = "+" if roi_pct >= 0 else ""

        if trailing:
            stop_label = f"${stop_price:.4f}  [TRAILING — peak ${peak:.3f}]"
        else:
            stop_label = f"${stop_price:.4f}"

        delta_str = f"  model delta={model_delta:.1f}{unit_sym}" if model_delta is not None else ""
        edge_str  = f"  edge={edge:+.3f}" if edge is not None else ""

        try:
            dt = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
            opened_str = dt.strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            opened_str = opened_at or "unknown"

        up_down = "UP" if unrealized >= 0 else "DOWN"

        print(f"\n  {'─'*66}")
        print(f"  {city_name:<18} {date}   {bucket}   [Cycle {cycle_num}]  [{up_down}]")
        print(f"  {'─'*66}")
        print(f"  Shares:        {shares:.2f}")
        print(f"  Cost:          ${cost:.2f}")
        print(f"  Entry price:   ${entry_price:.4f}")
        print(f"  Current price: ${current_price:.4f}")
        print(f"  Current value: ${current_value:.2f}   ({roi_sign}{roi_pct:.1f}% ROI)")
        print(f"  Unrealized:    {pnl_sign}${unrealized:.2f}")
        print(f"  Stop price:    {stop_label}")
        print(f"  Take-profit:   ${tp_price:.4f}  (+{_strategy['take_profit_roi']:.0%})")
        print(f"  P (model):     {p:.0%}{delta_str}{edge_str}")
        print(f"  Opened:        {opened_str}")

    total_sign = "+" if total_unrealized >= 0 else ""
    print(f"\n  {'─'*66}")
    print(f"  TOTALS   {len(open_pos)} positions   "
          f"Cost: ${total_cost:.2f}   "
          f"Value: ${total_value:.2f}   "
          f"Unrealized: {total_sign}${total_unrealized:.2f}")
    print(f"{'='*70}\n")


def print_report():
    markets  = load_all_markets()
    resolved = [m for m in markets if m["status"] == "resolved" and m.get("pnl") is not None]

    print(f"\n{'='*60}")
    print(f"  WEATHERBET — FULL REPORT  (v4 Rewrite)")
    print(f"{'='*60}")

    if not resolved:
        print("  No resolved markets yet.")
        return

    total_pnl = sum(m["pnl"] for m in resolved)
    wins      = [m for m in resolved if m["resolved_outcome"] == "win"]
    losses    = [m for m in resolved if m["resolved_outcome"] == "loss"]

    print(f"\n  Total resolved: {len(resolved)}")
    print(f"  Wins:           {len(wins)} | Losses: {len(losses)}")
    print(f"  Win rate:       {len(wins)/len(resolved):.0%}")
    print(f"  Total PnL:      {'+'if total_pnl>=0 else ''}{total_pnl:.2f}")

    print(f"\n  By city:")
    for city in sorted(set(m["city"] for m in resolved)):
        group = [m for m in resolved if m["city"] == city]
        w     = len([m for m in group if m["resolved_outcome"] == "win"])
        pnl   = sum(m["pnl"] for m in group)
        name  = LOCATIONS[city]["name"]
        print(f"    {name:<16} {w}/{len(group)} ({w/len(group):.0%})  PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")

    print(f"\n  Market details:")
    for m in sorted(resolved, key=lambda x: x["date"]):
        unit_sym = "F" if m["unit"] == "F" else "C"
        snaps    = m.get("forecast_snapshots", [])
        first_fc = snaps[0]["best"] if snaps else None
        last_fc  = snaps[-1]["best"] if snaps else None
        result   = m["resolved_outcome"].upper()
        mkt_pnl  = f"{'+'if (m['pnl'] or 0)>=0 else ''}{m['pnl']:.2f}" if m["pnl"] is not None else "-"
        fc_str   = f"forecast {first_fc}->{last_fc}{unit_sym}" if first_fc else "no forecast"
        actual   = f"actual {m['actual_temp']}{unit_sym}" if m.get("actual_temp") else ""
        cycles   = m.get("cycles", [])
        if cycles:
            for c in cycles:
                delta_str = f" d={c.get('model_delta'):.1f}" if c.get("model_delta") is not None else ""
                label  = f"{c.get('bucket_low')}-{c.get('bucket_high')}{unit_sym} C{c['cycle_num']}{delta_str}"
                c_pnl  = f"{'+'if (c.get('pnl') or 0)>=0 else ''}{(c.get('pnl') or 0):.2f}"
                reason = c.get("close_reason", "?")
                print(f"    {m['city_name']:<16} {m['date']} | {label:<22} | {fc_str} | {actual} | {result} {c_pnl} ({reason})")
        else:
            print(f"    {m['city_name']:<16} {m['date']} | no cycles       | {fc_str} | {actual} | {result} {mkt_pnl}")

    print(f"{'='*60}\n")

# =============================================================================
# BOOTSTRAP CALIBRATION
# =============================================================================

def bootstrap_wu_calibration(months=3):
    """Pre-seed bias data from historical WU monthly summaries vs Open-Meteo archive hindcasts.

    Runs on startup when any active city has fewer than BIAS_MIN_N bias samples.
    Computes mean(wu_high - model_forecast) per city/source using the last `months`
    months of WU data matched to ECMWF/GFS archive hindcasts.
    """
    global _cal
    _cal = load_cal()

    needs = any(
        _cal.get(f"{city}_ecmwf_bias", {}).get("n", 0) < BIAS_MIN_N
        for city in LOCATIONS
        if LOCATIONS[city].get("region") in SCAN_REGIONS
    )
    if not needs:
        return

    print(f"  [BOOTSTRAP] Seeding bias calibration from {months} months of WU history...")
    now     = datetime.now(timezone.utc)
    now_str = now.isoformat()

    for city_slug, loc in LOCATIONS.items():
        if loc.get("region") not in SCAN_REGIONS:
            continue
        if _cal.get(f"{city_slug}_ecmwf_bias", {}).get("n", 0) >= BIAS_MIN_N:
            continue

        station   = loc["station"]
        unit      = loc["unit"]
        temp_unit = "fahrenheit" if unit == "F" else "celsius"
        lat, lon  = loc["lat"], loc["lon"]
        tz        = TIMEZONES.get(city_slug, "UTC")

        # Fetch WU monthly data for last `months` months
        wu_days = {}
        for m_offset in range(1, months + 1):
            target_month = now.month - m_offset
            target_year  = now.year
            while target_month <= 0:
                target_month += 12
                target_year  -= 1
            month_str = f"{target_year}-{target_month:02d}"
            url = f"{WU_API_URL}/weather/{station}?month={month_str}"
            try:
                resp = requests.get(url, timeout=(5, 15))
                if not resp.ok:
                    continue
                data = resp.json()
                for rec in data.get("daily_records", []):
                    d   = rec.get("date")
                    h   = rec.get("high_temp")
                    cnt = rec.get("observation_count", 0)
                    if d and h is not None and cnt >= 12:
                        wu_days[d] = float(h)
            except Exception as e:
                print(f"  [BOOTSTRAP] {city_slug} {month_str}: {e}")

        if len(wu_days) < BIAS_MIN_N:
            continue

        dates_sorted = sorted(wu_days.keys())
        start_date   = dates_sorted[0]
        end_date     = dates_sorted[-1]

        # Fetch ECMWF and GFS archive hindcasts
        archives = {}
        for model_api, source_key in [("ecmwf_ifs", "ecmwf"), ("gfs_seamless", "hrrr")]:
            archive_url = (
                f"https://archive-api.open-meteo.com/v1/archive"
                f"?latitude={lat}&longitude={lon}"
                f"&start_date={start_date}&end_date={end_date}"
                f"&daily=temperature_2m_max&temperature_unit={temp_unit}"
                f"&timezone={tz}&models={model_api}"
            )
            try:
                resp = requests.get(archive_url, timeout=(10, 30))
                data = resp.json()
                if "error" in data:
                    print(f"  [BOOTSTRAP] {city_slug} {model_api}: {data.get('reason', 'API error')}")
                    continue
                daily    = data.get("daily", {})
                day_map  = {}
                for d, t in zip(daily.get("time", []), daily.get("temperature_2m_max", [])):
                    if t is not None:
                        day_map[d] = round(t) if unit == "F" else round(t, 1)
                archives[source_key] = day_map
                time.sleep(0.5)
            except Exception as e:
                print(f"  [BOOTSTRAP] {city_slug} {model_api} archive: {e}")

        # Compute mean bias per source
        for source_key, archive in archives.items():
            residuals = [wu_days[d] - archive[d] for d in wu_days if d in archive]
            if len(residuals) < BIAS_MIN_N:
                continue
            mean_bias = round(sum(residuals) / len(residuals), 3)
            bias_key  = f"{city_slug}_{source_key}_bias"
            _cal[bias_key] = {"bias": mean_bias, "n": len(residuals), "updated_at": now_str}
            print(f"  [BOOTSTRAP] {loc['name']} {source_key}: bias={mean_bias:+.2f} (n={len(residuals)})")

    CALIBRATION_FILE.write_text(json.dumps(_cal, indent=2), encoding="utf-8")
    print(f"  [BOOTSTRAP] Done.")

# =============================================================================
# MAIN LOOP
# =============================================================================

def main():
    # Strip --sim flag so remaining positional args are the subcommand
    positional = [a for a in sys.argv[1:] if not a.startswith("--")]
    cmd = positional[0].lower() if positional else None

    if cmd == "status":
        print_status()
        return
    if cmd == "report":
        print_report()
        return
    if cmd == "positions" or "--positions" in sys.argv:
        print_positions()
        return
    if cmd == "sim-reset":
        if not SIM_MODE:
            print("sim-reset requires --sim flag: python bot_v4.py --sim sim-reset")
            return
        if SIM_STATE_FILE.exists():
            SIM_STATE_FILE.unlink()
        wiped = 0
        for f in SIM_MARKETS_DIR.glob("*.json"):
            f.unlink()
            wiped += 1
        print(f"[SIM] Reset complete — removed {wiped} market file(s) and state. Run with --sim to start fresh.")
        return
    if cmd is not None:
        print(f"Unknown command: {cmd}")
        print("Usage: python bot_v4.py [--sim] [status|report|positions|sim-reset]")
        return

    sim_label = " [SIM MODE]" if SIM_MODE else ""
    print(f"\n{'='*60}")
    print(f"  WEATHERBET — Starting (v4 Rewrite){sim_label}")
    print(f"  Regions: {sorted(SCAN_REGIONS)}")
    print(f"  WU API: {'OK' if WU_API_VALID else 'UNAVAILABLE'} ({WU_API_URL})")
    print(f"  Strategy: take_profit={_strategy['take_profit_roi']:.0%}  "
          f"kelly={_strategy['kelly_fraction']:.2f}  confidence>={_strategy['min_confidence']:.2f}")
    print(f"  Entry gates: model_delta<={MAX_MODEL_DELTA_F}°F/{MAX_MODEL_DELTA_C}°C  "
          f"min_edge>={MIN_EDGE:.2f}  crowd_gap<={MAX_CROWD_GAP_BUCKS:.1f} bucket(s)  "
          f"zone=[{MIN_ENTRY_PRICE:.2f},{MAX_ENTRY_PRICE:.2f}]")
    print(f"  Loss exit: forecast-divergence (>0.5 units outside bucket)  "
          f"Trailing: +25% activation, 30% below peak")
    print(f"  Scan: every {SCAN_INTERVAL//60}min  Monitor: every {MONITOR_INTERVAL//60}min")
    if SIM_MODE:
        print(f"  ** No real orders placed — virtual balance ${SIM_BALANCE:.2f} **")
    print(f"  No forecast-based exits — price-only stop/take-profit")
    print(f"{'='*60}\n")

    global _cal
    _cal = load_cal()

    if WU_API_VALID:
        bootstrap_wu_calibration(months=3)

    last_scan    = 0.0
    last_monitor = 0.0

    while True:
        now_ts = time.time()

        if now_ts - last_monitor >= MONITOR_INTERVAL:
            last_monitor = now_ts
            print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Monitoring positions...")
            n = monitor_positions()
            if n:
                print(f"  Closed {n} position(s)")

        if now_ts - last_scan >= SCAN_INTERVAL:
            last_scan = now_ts
            print(f"\n[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Full scan starting...")
            new_pos, closed, resolved = scan_and_update()
            print(f"  Scan complete — opened: {new_pos}, closed: {closed}, resolved: {resolved}")

            # Print brief balance summary after each scan
            state     = load_state()
            bal       = state["balance"]
            start     = state["starting_balance"]
            roi       = (bal - start) / start * 100
            bal_label = "[SIM] Balance" if SIM_MODE else "Balance"
            print(f"  {bal_label}: ${bal:.2f} ({'+'if roi>=0 else ''}{roi:.1f}% vs start)\n")

        time.sleep(30)


if __name__ == "__main__":
    main()
