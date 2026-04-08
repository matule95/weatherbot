#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
weatherbet.py — Weather Trading Bot for Polymarket (v3 — REAL TRADES)
======================================================================
Identical logic to v2 but executes real trades via py-clob-client.

Requires in config.json:
    polymarket_private_key   — 0x-prefixed private key
    polymarket_api_key       — CLOB API key (optional — derived if omitted)
    polymarket_api_secret    — CLOB API secret (optional)
    polymarket_api_passphrase— CLOB passphrase (optional)
    polymarket_funder        — funder address (for proxy/gnosis wallets)
    chain_id                 — 137 (Polygon mainnet, default)
    signature_type           — 0=EOA, 1=Magic, 2=Gnosis (default 0)

Usage:
    python bot_v3.py          # main loop
    python bot_v3.py report   # full report
    python bot_v3.py status   # balance and open positions
"""

import re
import sys
import json
import math
import time
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    MarketOrderArgs, OrderArgs, OrderType, ApiCreds,
    BalanceAllowanceParams, AssetType,
)
from py_clob_client.order_builder.constants import BUY, SELL

# =============================================================================
# CONFIG
# =============================================================================

with open("weather_bot_config.json", encoding="utf-8") as f:
    _cfg = json.load(f)

BALANCE          = _cfg.get("balance", 10000.0)
MAX_BET          = _cfg.get("max_bet", 20.0)
MIN_VOLUME       = _cfg.get("min_volume", 500)
MIN_HOURS        = _cfg.get("min_hours", 2.0)
MAX_HOURS        = _cfg.get("max_hours", 72.0)
MAX_SLIPPAGE     = _cfg.get("max_slippage", 0.03)
SCAN_INTERVAL    = _cfg.get("scan_interval", 3600)
CALIBRATION_MIN  = _cfg.get("calibration_min", 30)
VC_KEY           = _cfg.get("vc_key", "")
PRIOR_WEIGHT     = _cfg.get("prior_weight", 10)
MAX_POS_PER_EVENT= _cfg.get("max_positions_per_event", 1)
TUNE_LOOKBACK    = _cfg.get("tune_lookback", 100)
TUNE_ENABLED     = _cfg.get("tune_enabled", True)
MAX_OPEN_POS     = _cfg.get("max_open_positions", 15)
MAX_POS_PER_DATE = _cfg.get("max_positions_per_date", 6)
MONITOR_INTERVAL = _cfg.get("monitor_interval", 600)
STOP_LOSS_PCT    = _cfg.get("stop_loss_pct", 0.80)
TAKE_PROFIT_SHORT= _cfg.get("take_profit_short", 0.85)  # 24-48h remaining
TAKE_PROFIT_LONG = _cfg.get("take_profit_long", 0.75)   # >48h remaining
TAKE_PROFIT_FINAL= _cfg.get("take_profit_final", 0.50)  # <24h remaining
TAKE_PROFIT_ROI  = _cfg.get("take_profit_roi", 0.40)    # sell when price >= entry * (1 + ROI)
SCAN_REGIONS     = set(_cfg.get("scan_regions", ["eu"]))

# CLOB credentials
POLYMARKET_HOST    = "https://clob.polymarket.com"
POLY_PRIVATE_KEY   = _cfg.get("polymarket_private_key", "")
POLY_API_KEY       = _cfg.get("polymarket_api_key", "")
POLY_API_SECRET    = _cfg.get("polymarket_api_secret", "")
POLY_API_PASSPHRASE= _cfg.get("polymarket_api_passphrase", "")
POLY_FUNDER        = _cfg.get("polymarket_funder", "")
POLY_CHAIN_ID      = _cfg.get("chain_id", 137)
POLY_SIG_TYPE      = _cfg.get("signature_type", 0)

SIGMA_F = 2.0
SIGMA_C = 1.2

DATA_DIR         = Path("data_v3")
DATA_DIR.mkdir(exist_ok=True)
STATE_FILE       = DATA_DIR / "state.json"
MARKETS_DIR      = DATA_DIR / "markets"
MARKETS_DIR.mkdir(exist_ok=True)
CALIBRATION_FILE = DATA_DIR / "calibration.json"
STRATEGY_FILE    = DATA_DIR / "strategy.json"

VC_KEY_VALID = bool(VC_KEY) and VC_KEY != "YOUR_KEY_HERE"

# Mutable strategy parameters — overridden by strategy.json when present
_strategy = {
    "min_ev":         _cfg.get("min_ev", 0.10),
    "max_price":      _cfg.get("max_price", 0.45),
    "kelly_fraction": _cfg.get("kelly_fraction", 0.25),
}

def _load_strategy():
    if STRATEGY_FILE.exists():
        try:
            saved = json.loads(STRATEGY_FILE.read_text(encoding="utf-8"))
            for k in ("min_ev", "max_price", "kelly_fraction"):
                if k in saved:
                    _strategy[k] = saved[k]
        except Exception:
            pass

_load_strategy()

LOCATIONS = {
    "nyc":          {"lat": 40.7772,  "lon":  -73.8726, "name": "New York City", "station": "KLGA", "unit": "F", "region": "us"},
    "chicago":      {"lat": 41.9742,  "lon":  -87.9073, "name": "Chicago",       "station": "KORD", "unit": "F", "region": "us"},
    "miami":        {"lat": 25.7959,  "lon":  -80.2870, "name": "Miami",         "station": "KMIA", "unit": "F", "region": "us"},
    "dallas":       {"lat": 32.8471,  "lon":  -96.8518, "name": "Dallas",        "station": "KDAL", "unit": "F", "region": "us"},
    "seattle":      {"lat": 47.4502,  "lon": -122.3088, "name": "Seattle",       "station": "KSEA", "unit": "F", "region": "us"},
    "atlanta":      {"lat": 33.6407,  "lon":  -84.4277, "name": "Atlanta",       "station": "KATL", "unit": "F", "region": "us"},
    "london":       {"lat": 51.5048,  "lon":    0.0495, "name": "London",        "station": "EGLC", "unit": "C", "region": "eu"},
    "paris":        {"lat": 48.9962,  "lon":    2.5979, "name": "Paris",         "station": "LFPG", "unit": "C", "region": "eu"},
    "munich":       {"lat": 48.3537,  "lon":   11.7750, "name": "Munich",        "station": "EDDM", "unit": "C", "region": "eu"},
    "ankara":       {"lat": 40.1281,  "lon":   32.9951, "name": "Ankara",        "station": "LTAC", "unit": "C", "region": "eu"},
    "seoul":        {"lat": 37.4691,  "lon":  126.4505, "name": "Seoul",         "station": "RKSI", "unit": "C", "region": "asia"},
    "tokyo":        {"lat": 35.7647,  "lon":  140.3864, "name": "Tokyo",         "station": "RJTT", "unit": "C", "region": "asia"},
    "shanghai":     {"lat": 31.1443,  "lon":  121.8083, "name": "Shanghai",      "station": "ZSPD", "unit": "C", "region": "asia"},
    "singapore":    {"lat":  1.3502,  "lon":  103.9940, "name": "Singapore",     "station": "WSSS", "unit": "C", "region": "asia"},
    "lucknow":      {"lat": 26.7606,  "lon":   80.8893, "name": "Lucknow",       "station": "VILK", "unit": "C", "region": "asia"},
    "tel-aviv":     {"lat": 32.0114,  "lon":   34.8867, "name": "Tel Aviv",      "station": "LLBG", "unit": "C", "region": "asia"},
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
    "lucknow": "Asia/Kolkata", "tel-aviv": "Asia/Jerusalem",
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
            raise RuntimeError("polymarket_private_key not set in config.json")
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
            client.set_api_creds(client.create_or_derive_api_creds())
        _clob_client = client
    return _clob_client

def get_real_balance() -> float | None:
    """Fetch available USDC balance from Polymarket wallet."""
    try:
        resp = get_clob_client().get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        return float(resp["balance"]) / 1e6
    except Exception as e:
        print(f"  [BALANCE] Error fetching real balance: {e}")
        return None

def place_buy_order(token_id: str, cost: float) -> dict | None:
    """
    Place a market BUY order for `cost` USDC worth of YES tokens.
    Tries FOK first, falls back to FAK.
    Returns the response dict on success, None on failure.
    """
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
    Uses best bid from Polymarket for pricing to ensure fills.
    Falls back to `price` if best bid unavailable.
    Queries actual conditional token balance first and caps size to what's
    available, preventing "not enough balance" errors from partial fills.
    Returns the response dict on success, None on failure.
    """
    client = get_clob_client()

    # Check actual on-chain token balance before attempting sell
    actual_size = round(size, 2)
    try:
        bal_resp = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token_id)
        )
        available = float(bal_resp["balance"]) / 1e6
        if available < 1.0:
            print(f"  [SELL] Token balance too low ({available:.2f} shares), skipping")
            return None
        if available < actual_size:
            floored = math.floor(available * 100) / 100
            print(f"  [SELL] Capping size {actual_size:.2f} → {floored:.2f} (partial fill on buy)")
            actual_size = floored
    except Exception as e:
        print(f"  [SELL] Could not check token balance: {e} — using recorded size")

    # Fetch best bid for realistic sell pricing
    sell_price = price
    if market_id:
        try:
            r = requests.get(f"https://gamma-api.polymarket.com/markets/{market_id}", timeout=(3, 5))
            mdata = r.json()
            best_bid = mdata.get("bestBid")
            if best_bid is not None:
                sell_price = float(best_bid)
                if sell_price != price:
                    print(f"  [SELL] Using best bid ${sell_price:.3f} (market ${price:.3f})")
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
    """Query on-chain conditional token balance (in shares)."""
    if not token_id:
        return 0.0
    try:
        client = get_clob_client()
        bal_resp = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token_id)
        )
        return float(bal_resp["balance"]) / 1e6
    except Exception as e:
        print(f"  [BALANCE] Error checking token balance: {e}")
        return 0.0

def get_real_entry_price(token_id: str) -> float | None:
    """Query Polymarket trade history to find actual average buy price."""
    try:
        client = get_clob_client()
        try:
            from py_clob_client.clob_types import TradeParams
            trades = client.get_trades(TradeParams(asset_id=token_id))
        except (ImportError, TypeError, AttributeError):
            trades = client.get_trades({"asset_id": token_id})

        if not trades:
            return None

        total_cost = 0.0
        total_shares = 0.0
        for t in trades:
            side = (t.get("side") or "").upper()
            if side == "BUY":
                price = float(t.get("price", 0))
                size = float(t.get("size", 0))
                if price > 0 and size > 0:
                    total_cost += price * size
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
    """CDF-based probability for all bucket types including regular buckets."""
    s = sigma or 2.0
    if s <= 0:
        s = 0.01
    if t_low == -999:
        return norm_cdf((t_high - float(forecast)) / s)
    if t_high == 999:
        return 1.0 - norm_cdf((t_low - float(forecast)) / s)
    return norm_cdf((t_high - float(forecast)) / s) - norm_cdf((t_low - float(forecast)) / s)

def calc_ev(p, price):
    if price <= 0 or price >= 1: return 0.0
    return round(p * (1.0 / price - 1.0) - (1.0 - p), 4)

def calc_kelly(p, price):
    if price <= 0 or price >= 1: return 0.0
    b = 1.0 / price - 1.0
    f = (p * b - (1.0 - p)) / b
    return round(min(max(0.0, f) * _strategy["kelly_fraction"], 1.0), 4)

def bet_size(kelly, balance):
    raw = kelly * balance
    return round(min(raw, MAX_BET), 2)

# =============================================================================
# CALIBRATION
# =============================================================================

_cal: dict = {}

def load_cal():
    if CALIBRATION_FILE.exists():
        return json.loads(CALIBRATION_FILE.read_text(encoding="utf-8"))
    return {}

def get_sigma(city_slug, source="ecmwf", horizon=None):
    """Lookup calibrated sigma with fallback: city_source_d{h} -> city_source -> default."""
    if horizon is not None:
        h_key = f"{city_slug}_{source}_d{horizon}"
        if h_key in _cal:
            return _cal[h_key]["sigma"]
    base_key = f"{city_slug}_{source}"
    if base_key in _cal:
        return _cal[base_key]["sigma"]
    return SIGMA_F if LOCATIONS[city_slug]["unit"] == "F" else SIGMA_C

def run_calibration(markets):
    """Recalculates sigma from resolved markets using Bayesian update, per horizon."""
    resolved = [m for m in markets if m.get("status") == "resolved" and m.get("actual_temp") is not None]
    if not resolved:
        return load_cal()

    cal = load_cal()
    updated = []

    for source in ["ecmwf", "hrrr"]:
        for city in set(m["city"] for m in resolved):
            if city not in LOCATIONS:
                continue
            group = [m for m in resolved if m["city"] == city]
            prior_sigma = SIGMA_F if LOCATIONS[city]["unit"] == "F" else SIGMA_C

            horizon_errors: dict[str, list] = {}
            all_errors: list[float] = []

            for m in group:
                for snap in reversed(m.get("forecast_snapshots", [])):
                    temp_val = snap.get(source)
                    if temp_val is None:
                        continue
                    err = abs(temp_val - m["actual_temp"])
                    all_errors.append(err)
                    h = snap.get("horizon", "")
                    h_num = h.replace("D+", "") if h.startswith("D+") else None
                    if h_num is not None:
                        horizon_errors.setdefault(h_num, []).append(err)
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
                cal[key] = {"sigma": new, "n": len(h_errs), "updated_at": datetime.now(timezone.utc).isoformat()}
                if abs(new - old) > 0.05:
                    updated.append(f"{LOCATIONS[city]['name']} {source} D+{h_num}: {old:.2f}->{new:.2f}")

            if all_errors:
                key = f"{city}_{source}"
                new = bayesian_sigma(all_errors, prior_sigma)
                if new is not None:
                    old = cal.get(key, {}).get("sigma", prior_sigma)
                    cal[key] = {"sigma": new, "n": len(all_errors), "updated_at": datetime.now(timezone.utc).isoformat()}
                    if abs(new - old) > 0.05:
                        updated.append(f"{LOCATIONS[city]['name']} {source}: {old:.2f}->{new:.2f}")

    CALIBRATION_FILE.write_text(json.dumps(cal, indent=2), encoding="utf-8")
    if updated:
        print(f"  [CAL] {', '.join(updated)}")
    return cal

# =============================================================================
# FORECASTS
# =============================================================================

def get_ecmwf(city_slug, dates):
    """ECMWF via Open-Meteo with bias correction. For all cities."""
    loc = LOCATIONS[city_slug]
    unit = loc["unit"]
    temp_unit = "fahrenheit" if unit == "F" else "celsius"
    result = {}
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

def get_hrrr(city_slug, dates):
    """HRRR via Open-Meteo. US cities only, up to 48h horizon."""
    loc = LOCATIONS[city_slug]
    if loc["region"] != "us":
        return {}
    result = {}
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={loc['lat']}&longitude={loc['lon']}"
        f"&daily=temperature_2m_max&temperature_unit=fahrenheit"
        f"&forecast_days=3&timezone={TIMEZONES.get(city_slug, 'UTC')}"
        f"&models=gfs_seamless"  # HRRR+GFS seamless — best option for US
    )
    for attempt in range(3):
        try:
            data = requests.get(url, timeout=(5, 10)).json()
            if "error" not in data:
                for date, temp in zip(data["daily"]["time"], data["daily"]["temperature_2m_max"]):
                    if date in dates and temp is not None:
                        result[date] = round(temp)
            break
        except Exception as e:
            if attempt < 2:
                time.sleep(3)
            else:
                print(f"  [HRRR] {city_slug}: {e}")
    return result

def get_metar(city_slug):
    """Current observed temperature from METAR station. D+0 only."""
    loc = LOCATIONS[city_slug]
    station = loc["station"]
    unit = loc["unit"]
    try:
        url = f"https://aviationweather.gov/api/data/metar?ids={station}&format=json"
        resp = requests.get(url, timeout=(5, 8))
        if not resp.text or not resp.text.strip():
            return None
        data = resp.json()
        if data and isinstance(data, list):
            temp_c = data[0].get("temp")
            if temp_c is not None:
                if unit == "F":
                    return round(float(temp_c) * 9/5 + 32)
                return round(float(temp_c), 1)
    except Exception as e:
        print(f"  [METAR] {city_slug}: {e}")
    return None

def get_actual_temp(city_slug, date_str):
    loc = LOCATIONS[city_slug]
    station = loc["station"]
    unit = loc["unit"]
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

def check_market_resolved(market_id):
    """
    Checks if the market closed on Polymarket and who won.
    Returns: (None, closed_at_str) if still open/indeterminate,
             (True, closed_at_str) if YES won, (False, closed_at_str) if NO won.
    """
    try:
        r = requests.get(f"https://gamma-api.polymarket.com/markets/{market_id}", timeout=(5, 8))
        data = r.json()
        closed = data.get("closed", False)
        closed_at = data.get("endDate") or data.get("closedTime") or ""
        if not closed:
            return None, ""
        prices = json.loads(data.get("outcomePrices", "[0.5,0.5]"))
        yes_price = float(prices[0])
        if yes_price >= 0.95:
            return True, closed_at
        elif yes_price <= 0.05:
            return False, closed_at
        return None, closed_at
    except Exception as e:
        print(f"  [RESOLVE] {market_id}: {e}")
    return None, ""

def blend_forecasts(temps_and_sigmas):
    """Inverse-variance weighted blend of (temp, sigma) tuples. ECMWF + HRRR only."""
    if not temps_and_sigmas:
        return None, None
    if len(temps_and_sigmas) == 1:
        return temps_and_sigmas[0]
    weights = [1.0 / (s ** 2) for _, s in temps_and_sigmas]
    total_w = sum(weights)
    blended_temp = sum(t * w for (t, _), w in zip(temps_and_sigmas, weights)) / total_w
    blended_sigma = math.sqrt(1.0 / total_w)
    return round(blended_temp, 1), round(blended_sigma, 3)

# =============================================================================
# POLYMARKET
# =============================================================================

def get_polymarket_event(city_slug, month, day, year):
    slug = f"highest-temperature-in-{city_slug}-on-{month}-{day}-{year}"
    try:
        r = requests.get(f"https://gamma-api.polymarket.com/events?slug={slug}", timeout=(5, 8))
        data = r.json()
        if data and isinstance(data, list) and len(data) > 0:
            return data[0]
    except Exception:
        pass
    return None

def get_market_price(market_id):
    try:
        r = requests.get(f"https://gamma-api.polymarket.com/markets/{market_id}", timeout=(3, 5))
        prices = json.loads(r.json().get("outcomePrices", "[0.5,0.5]"))
        return float(prices[0])
    except Exception:
        return None

def parse_temp_range(question):
    if not question: return None
    num = r'(-?\d+(?:\.\d+)?)'
    if re.search(r'or below', question, re.IGNORECASE):
        m = re.search(num + r'[°]?[FC] or below', question, re.IGNORECASE)
        if m: return (-999.0, float(m.group(1)))
    if re.search(r'or higher', question, re.IGNORECASE):
        m = re.search(num + r'[°]?[FC] or higher', question, re.IGNORECASE)
        if m: return (float(m.group(1)), 999.0)
    m = re.search(r'between ' + num + r'-' + num + r'[°]?[FC]', question, re.IGNORECASE)
    if m: return (float(m.group(1)), float(m.group(2)))
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
    return MARKETS_DIR / f"{city_slug}_{date_str}.json"

def load_market(city_slug, date_str):
    p = market_path(city_slug, date_str)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return None

def save_market(market):
    p = market_path(market["city"], market["date"])
    p.write_text(json.dumps(market, indent=2, ensure_ascii=False), encoding="utf-8")

def load_all_markets():
    markets = []
    for f in MARKETS_DIR.glob("*.json"):
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
        "position":           None,
        "actual_temp":        None,
        "resolved_outcome":   None,
        "pnl":                None,
        "forecast_snapshots": [],
        "market_snapshots":   [],
        "all_outcomes":       [],
        "created_at":         datetime.now(timezone.utc).isoformat(),
    }

# =============================================================================
# STATE
# =============================================================================

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {
        "balance":          BALANCE,
        "starting_balance": BALANCE,
        "total_trades":     0,
        "wins":             0,
        "losses":           0,
        "peak_balance":     BALANCE,
    }

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

# =============================================================================
# CORE LOGIC
# =============================================================================

def take_forecast_snapshot(city_slug, dates, horizon_map=None):
    """Fetches forecasts from all sources and returns blended snapshots."""
    now_str = datetime.now(timezone.utc).isoformat()
    ecmwf   = get_ecmwf(city_slug, dates)
    hrrr    = get_hrrr(city_slug, dates)
    today   = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    snapshots = {}
    for date in dates:
        h = horizon_map.get(date) if horizon_map else None
        snap = {
            "ts":    now_str,
            "ecmwf": ecmwf.get(date),
            "hrrr":  hrrr.get(date) if date <= (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%d") else None,
            "metar": get_metar(city_slug) if date == today else None,
        }

        loc = LOCATIONS[city_slug]
        sources_for_blend = []
        if snap["ecmwf"] is not None:
            s = get_sigma(city_slug, "ecmwf", horizon=h)
            sources_for_blend.append((snap["ecmwf"], s))
        if snap["hrrr"] is not None:
            s = get_sigma(city_slug, "hrrr", horizon=h)
            sources_for_blend.append((snap["hrrr"], s))

        if sources_for_blend:
            bt, bs = blend_forecasts(sources_for_blend)
            snap["best"] = bt
            snap["best_sigma"] = bs
            snap["best_source"] = "blend" if len(sources_for_blend) > 1 else ("hrrr" if snap["hrrr"] is not None and loc["region"] == "us" else "ecmwf")
            snap["sources_used"] = [("ecmwf" if t == snap.get("ecmwf") else "hrrr") for t, _ in sources_for_blend]
        else:
            snap["best"] = None
            snap["best_sigma"] = None
            snap["best_source"] = None
            snap["sources_used"] = []

        snapshots[date] = snap
    return snapshots

def scan_and_update():
    """Main function of one cycle: updates forecasts, opens/closes positions."""
    global _cal
    now      = datetime.now(timezone.utc)
    state    = load_state()

    # Use real on-chain balance for Kelly sizing
    real_bal = get_real_balance()
    balance  = real_bal if real_bal is not None else state["balance"]

    new_pos  = 0
    closed   = 0
    resolved = 0

    for city_slug, loc in LOCATIONS.items():
        if loc.get("region") not in SCAN_REGIONS:
            continue
        unit = loc["unit"]
        unit_sym = "F" if unit == "F" else "C"
        print(f"  -> {loc['name']}...", end=" ", flush=True)

        try:
            dates = [(now + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(4)]
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

            outcomes = []
            for market in event.get("markets", []):
                question = market.get("question", "")
                mid      = str(market.get("id", ""))
                volume   = float(market.get("volume", 0))
                rng      = parse_temp_range(question)
                if not rng:
                    continue
                try:
                    prices = json.loads(market.get("outcomePrices", "[0.5,0.5]"))
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

            snap = snapshots.get(date, {})
            forecast_snap = {
                "ts":          snap.get("ts"),
                "horizon":     horizon,
                "hours_left":  round(hours, 1),
                "ecmwf":       snap.get("ecmwf"),
                "hrrr":        snap.get("hrrr"),
                "metar":       snap.get("metar"),
                "best":        snap.get("best"),
                "best_source": snap.get("best_source"),
            }
            mkt["forecast_snapshots"].append(forecast_snap)

            top = max(outcomes, key=lambda x: x["price"]) if outcomes else None
            market_snap = {
                "ts":         snap.get("ts"),
                "top_bucket": f"{top['range'][0]}-{top['range'][1]}{unit_sym}" if top else None,
                "top_price":  top["price"] if top else None,
            }
            mkt["market_snapshots"].append(market_snap)

            forecast_temp = snap.get("best")
            best_source   = snap.get("best_source")

            # --- RECONCILE: detect on-chain positions not tracked locally ---
            if mkt["status"] != "resolved":
                pos = mkt.get("position")
                need_reconcile = pos is None or pos.get("status") != "open"
                if need_reconcile and outcomes:
                    if pos is not None and pos.get("token_id"):
                        onchain = get_token_balance(pos["token_id"])
                        if onchain >= 1.0:
                            matching = next((o for o in outcomes if o.get("token_id") == pos["token_id"]), None)
                            cp = matching["price"] if matching else pos.get("exit_price", pos["entry_price"])
                            real_entry = get_real_entry_price(pos["token_id"])
                            entry = real_entry if real_entry is not None else cp
                            print(f"  [RECONCILE] {loc['name']} {date} — {onchain:.1f} orphaned shares on-chain "
                                  f"(entry ${entry:.4f}, market ${cp:.3f})")
                            pos["shares"]       = round(onchain, 2)
                            pos["entry_price"]  = entry
                            pos["cost"]         = round(onchain * entry, 2)
                            pos["status"]       = "open"
                            pos["pnl"]          = None
                            pos["exit_price"]   = None
                            pos["close_reason"] = None
                            pos["closed_at"]    = None
                            pos["reconciled"]   = True
                    else:
                        for o in outcomes:
                            tid = o.get("token_id")
                            if not tid:
                                continue
                            onchain = get_token_balance(tid)
                            if onchain >= 1.0:
                                cp = o["price"]
                                t_low, t_high = o["range"]
                                real_entry = get_real_entry_price(tid)
                                entry = real_entry if real_entry is not None else cp
                                print(f"  [RECONCILE] {loc['name']} {date} — {onchain:.1f} on-chain shares "
                                      f"({t_low}-{t_high}{unit_sym}) entry ${entry:.4f}, market ${cp:.3f}")
                                mkt["position"] = {
                                    "market_id":    o["market_id"],
                                    "token_id":     tid,
                                    "question":     o["question"],
                                    "bucket_low":   t_low,
                                    "bucket_high":  t_high,
                                    "entry_price":  entry,
                                    "shares":       round(onchain, 2),
                                    "cost":         round(onchain * entry, 2),
                                    "p":            None,
                                    "ev":           None,
                                    "kelly":        None,
                                    "forecast_temp": forecast_temp,
                                    "forecast_src":  best_source,
                                    "sigma":        None,
                                    "opened_at":    snap.get("ts"),
                                    "status":       "open",
                                    "pnl":          None,
                                    "exit_price":   None,
                                    "close_reason": None,
                                    "closed_at":    None,
                                    "order_id":     None,
                                    "reconciled":   True,
                                }
                                break
                            time.sleep(0.05)

            # --- STOP-LOSS, TRAILING STOP, AND TAKE-PROFIT ---
            if mkt.get("position") and mkt["position"].get("status") == "open":
                pos = mkt["position"]
                current_price = None
                for o in outcomes:
                    if o["market_id"] == pos["market_id"]:
                        current_price = o["price"]
                        break

                if current_price is not None:
                    entry = pos["entry_price"]
                    stop  = pos.get("stop_price", entry * STOP_LOSS_PCT)

                    if current_price >= entry * 1.20 and stop < entry:
                        pos["stop_price"] = entry
                        pos["trailing_activated"] = True

                    if hours < 24:
                        take_profit = TAKE_PROFIT_FINAL
                    elif hours < 48:
                        take_profit = TAKE_PROFIT_SHORT
                    else:
                        take_profit = TAKE_PROFIT_LONG

                    roi_threshold = entry * (1.0 + TAKE_PROFIT_ROI)
                    take_triggered = current_price >= take_profit or current_price >= roi_threshold
                    stop_triggered = current_price <= stop

                    if take_triggered or stop_triggered:
                        onchain = get_token_balance(pos["token_id"])
                        if onchain < 1.0:
                            pnl = round((current_price - entry) * pos["shares"], 2)
                            pos["exit_price"]   = current_price
                            pos["pnl"]          = pnl
                            pos["status"]       = "closed"
                            pos["close_reason"] = "sold_externally"
                            pos["closed_at"]    = snap.get("ts")
                            closed += 1
                            print(f"  [CLOSED] {loc['name']} {date} — 0 shares on-chain, position already sold/resolved")
                        else:
                            resp = place_sell_order(pos["token_id"], pos["shares"], current_price, market_id=pos["market_id"])
                            if resp is not None:
                                pnl = round((current_price - entry) * pos["shares"], 2)
                                balance += pos["cost"] + pnl
                                pos["closed_at"]    = snap.get("ts")
                                if current_price >= roi_threshold and current_price < take_profit:
                                    pos["close_reason"] = "take_profit_roi"
                                    reason = "TAKE ROI"
                                elif take_triggered:
                                    pos["close_reason"] = "take_profit"
                                    reason = "TAKE"
                                elif current_price < entry:
                                    pos["close_reason"] = "stop_loss"
                                    reason = "STOP"
                                else:
                                    pos["close_reason"] = "trailing_stop"
                                    reason = "TRAILING BE"
                                pos["exit_price"]   = current_price
                                pos["pnl"]          = pnl
                                pos["status"]       = "closed"
                                closed += 1
                                print(f"  [{reason}] {loc['name']} {date} | entry ${entry:.3f} exit ${current_price:.3f} | PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")
                            else:
                                print(f"  [SELL FAIL] {loc['name']} {date} — will retry next cycle")

            # --- CLOSE POSITION if forecast shifted 2+ degrees ---
            if mkt.get("position") and mkt["position"].get("status") == "open" and forecast_temp is not None:
                pos = mkt["position"]
                old_bucket_low  = pos["bucket_low"]
                old_bucket_high = pos["bucket_high"]
                buffer = 2.0 if unit == "F" else 1.0
                mid_bucket = (old_bucket_low + old_bucket_high) / 2 if old_bucket_low != -999 and old_bucket_high != 999 else forecast_temp
                forecast_far = abs(forecast_temp - mid_bucket) > (abs(mid_bucket - old_bucket_low) + buffer)
                if not in_bucket(forecast_temp, old_bucket_low, old_bucket_high) and forecast_far:
                    current_price = None
                    for o in outcomes:
                        if o["market_id"] == pos["market_id"]:
                            current_price = o["price"]
                            break
                    if current_price is not None:
                        resp = place_sell_order(pos["token_id"], pos["shares"], current_price, market_id=pos["market_id"])
                        if resp is not None:
                            pnl = round((current_price - pos["entry_price"]) * pos["shares"], 2)
                            balance += pos["cost"] + pnl
                            mkt["position"]["closed_at"]    = snap.get("ts")
                            mkt["position"]["close_reason"] = "forecast_changed"
                            mkt["position"]["exit_price"]   = current_price
                            mkt["position"]["pnl"]          = pnl
                            mkt["position"]["status"]       = "closed"
                            closed += 1
                            print(f"  [CLOSE] {loc['name']} {date} — forecast changed | PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")
                        else:
                            print(f"  [SELL FAIL] {loc['name']} {date} — will retry next cycle")

            # --- OPEN POSITION (multi-bucket scan) ---
            best_signal = None
            if not mkt.get("position") and forecast_temp is not None and hours >= MIN_HOURS:
                sigma = snap.get("best_sigma") or get_sigma(city_slug, best_source or "ecmwf", horizon=i)

                all_open = load_all_markets()
                total_open = sum(1 for m in all_open if m.get("position") and m["position"].get("status") == "open")
                date_open = sum(1 for m in all_open if m.get("position") and m["position"].get("status") == "open" and m["date"] == date)

                skip_portfolio = False
                if total_open >= MAX_OPEN_POS:
                    skip_portfolio = True
                elif date_open >= MAX_POS_PER_DATE:
                    skip_portfolio = True

                if not skip_portfolio:
                    candidates = []
                    for o in outcomes:
                        t_low, t_high = o["range"]
                        if o["volume"] < MIN_VOLUME:
                            continue
                        p = bucket_prob(forecast_temp, t_low, t_high, sigma)
                        if p < 0.01:
                            continue
                        yes_price = o["price"]
                        ev = calc_ev(p, yes_price)
                        if ev >= _strategy["min_ev"]:
                            kelly = calc_kelly(p, yes_price)
                            size = bet_size(kelly, balance)
                            if size >= 1.00:
                                candidates.append({
                                    "market_id":    o["market_id"],
                                    "token_id":     o["token_id"],
                                    "question":     o["question"],
                                    "bucket_low":   t_low,
                                    "bucket_high":  t_high,
                                    "entry_price":  yes_price,
                                    "shares":       round(size / yes_price, 2),
                                    "cost":         size,
                                    "p":            round(p, 4),
                                    "ev":           round(ev, 4),
                                    "kelly":        round(kelly, 4),
                                    "forecast_temp":forecast_temp,
                                    "forecast_src": best_source,
                                    "sigma":        sigma,
                                    "opened_at":    snap.get("ts"),
                                    "status":       "open",
                                    "pnl":          None,
                                    "exit_price":   None,
                                    "close_reason": None,
                                    "closed_at":    None,
                                    "order_id":     None,
                                })

                    candidates.sort(key=lambda c: c["ev"], reverse=True)
                    best_signal = candidates[0] if candidates else None

                if best_signal:
                    skip_position = False
                    try:
                        r = requests.get(f"https://gamma-api.polymarket.com/markets/{best_signal['market_id']}", timeout=(3, 5))
                        mdata = r.json()
                        real_ask = float(mdata.get("bestAsk", best_signal["entry_price"]))
                        real_bid = float(mdata.get("bestBid", best_signal["entry_price"]))
                        real_spread = round(real_ask - real_bid, 4)
                        if real_spread > MAX_SLIPPAGE or real_ask >= _strategy["max_price"]:
                            print(f"  [SKIP] {loc['name']} {date} — real ask ${real_ask:.3f} spread ${real_spread:.3f}")
                            skip_position = True
                        else:
                            best_signal["entry_price"]  = real_ask
                            best_signal["shares"]       = round(best_signal["cost"] / real_ask, 2)
                            best_signal["ev"]           = round(calc_ev(best_signal["p"], real_ask), 4)
                    except Exception as e:
                        print(f"  [WARN] Could not fetch real ask for {best_signal['market_id']}: {e}")

                    if not skip_position and best_signal["entry_price"] < _strategy["max_price"]:
                        if not best_signal["token_id"]:
                            print(f"  [SKIP] {loc['name']} {date} — no token_id, cannot place order")
                        else:
                            existing = get_token_balance(best_signal["token_id"])
                            if existing >= 1.0:
                                print(f"  [SKIP] {loc['name']} {date} — already hold {existing:.1f} shares on-chain")
                            else:
                                resp = place_buy_order(best_signal["token_id"], best_signal["cost"])
                                if resp is not None:
                                    best_signal["order_id"] = resp.get("orderID", resp.get("orderId", ""))
                                    balance -= best_signal["cost"]
                                    mkt["position"] = best_signal
                                    state["total_trades"] += 1
                                    new_pos += 1
                                    bucket_label = f"{best_signal['bucket_low']}-{best_signal['bucket_high']}{unit_sym}"
                                    print(f"  [BUY]  {loc['name']} {horizon} {date} | {bucket_label} | "
                                          f"${best_signal['entry_price']:.3f} | EV {best_signal['ev']:+.2f} | "
                                          f"${best_signal['cost']:.2f} ({(best_signal['forecast_src'] or 'blend').upper()})")
                                else:
                                    print(f"  [ORDER FAIL] {loc['name']} {date} — order not filled")

            # Market closed by time
            if hours < 0.5 and mkt["status"] == "open":
                mkt["status"] = "closed"

            save_market(mkt)
            time.sleep(0.1)

        print("ok")

    # --- AUTO-RESOLUTION ---
    for mkt in load_all_markets():
        if mkt["status"] == "resolved":
            continue

        pos = mkt.get("position")
        if not pos or pos.get("status") != "open":
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
                        print(f"  [TIMEOUT] {mkt['city_name']} {mkt['date']} — indeterminate after 48h, skipping")
                        mkt["status"] = "unresolvable"
                        save_market(mkt)
                except Exception:
                    pass
            continue

        price  = pos["entry_price"]
        size   = pos["cost"]
        shares = pos["shares"]
        pnl    = round(shares * (1 - price), 2) if won else round(-size, 2)

        pos["exit_price"]   = 1.0 if won else 0.0
        pos["pnl"]          = pnl
        pos["close_reason"] = "resolved"
        pos["closed_at"]    = now.isoformat()
        pos["status"]       = "closed"
        mkt["pnl"]          = pnl
        mkt["status"]       = "resolved"
        mkt["resolved_outcome"] = "win" if won else "loss"

        if VC_KEY_VALID:
            actual = get_actual_temp(mkt["city"], mkt["date"])
            if actual is not None:
                mkt["actual_temp"] = actual

        if won:
            state["wins"] += 1
        else:
            state["losses"] += 1

        result = "WIN" if won else "LOSS"
        print(f"  [{result}] {mkt['city_name']} {mkt['date']} | PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")
        resolved += 1

        save_market(mkt)
        time.sleep(0.3)

    # --- BACKFILL actual_temp FOR ALL PAST MARKETS (calibration needs this) ---
    if VC_KEY_VALID:
        for mkt in load_all_markets():
            if mkt.get("actual_temp") is not None:
                continue
            try:
                market_date = datetime.strptime(mkt["date"], "%Y-%m-%d").date()
            except Exception:
                continue
            if market_date < now.date() and mkt["city"] in LOCATIONS:
                actual = get_actual_temp(mkt["city"], mkt["date"])
                if actual is not None:
                    mkt["actual_temp"] = actual
                    save_market(mkt)
                time.sleep(0.2)

    # Sync balance from on-chain
    real_bal = get_real_balance()
    state["balance"]      = round(real_bal if real_bal is not None else balance, 2)
    state["peak_balance"] = max(state.get("peak_balance", balance), state["balance"])
    save_state(state)

    all_mkts = load_all_markets()
    global _cal
    _cal = run_calibration(all_mkts)

    if TUNE_ENABLED:
        tune_strategy(all_mkts, state)

    return new_pos, closed, resolved

# =============================================================================
# STRATEGY TUNING
# =============================================================================

_TUNE_BOUNDS = {
    "kelly_fraction": (0.10, 0.60),
    "min_ev":         (0.03, 0.30),
    "max_price":      (0.25, 0.65),
}
_TUNE_MAX_STEP = 0.10

def tune_strategy(markets, state):
    """Adjust strategy parameters based on recent resolved trades."""
    resolved = sorted(
        [m for m in markets if m.get("status") == "resolved" and m.get("position")],
        key=lambda m: m.get("position", {}).get("closed_at", ""),
    )
    recent = resolved[-TUNE_LOOKBACK:] if len(resolved) >= 20 else []
    if not recent:
        return

    old = dict(_strategy)
    positions = [m["position"] for m in recent if m.get("position")]

    wins = sum(1 for m in recent if m.get("resolved_outcome") == "win")
    actual_wr = wins / len(recent) if recent else 0.5
    avg_predicted_p = sum(p.get("p", 0.5) for p in positions) / len(positions) if positions else 0.5

    if actual_wr > avg_predicted_p + 0.05:
        adj = min(0.02, _TUNE_MAX_STEP * _strategy["kelly_fraction"])
        _strategy["kelly_fraction"] = min(_strategy["kelly_fraction"] + adj, _TUNE_BOUNDS["kelly_fraction"][1])
    elif actual_wr < avg_predicted_p - 0.05:
        adj = min(0.02, _TUNE_MAX_STEP * _strategy["kelly_fraction"])
        _strategy["kelly_fraction"] = max(_strategy["kelly_fraction"] - adj, _TUNE_BOUNDS["kelly_fraction"][0])

    ev_bands = [(0.03, []), (0.05, []), (0.08, []), (0.10, []), (0.15, []), (0.20, [])]
    for m in recent:
        pos = m.get("position", {})
        pos_ev = pos.get("ev", 0)
        for threshold, group in ev_bands:
            if pos_ev >= threshold:
                group.append(m.get("pnl", 0) or 0)

    best_ev_threshold = _strategy["min_ev"]
    best_ev_pnl_ratio = 0
    for threshold, pnls in ev_bands:
        if len(pnls) >= 5:
            ratio = sum(pnls) / len(pnls) if pnls else 0
            if ratio > best_ev_pnl_ratio:
                best_ev_pnl_ratio = ratio
                best_ev_threshold = threshold

    current = _strategy["min_ev"]
    delta = best_ev_threshold - current
    capped = max(-_TUNE_MAX_STEP * current, min(_TUNE_MAX_STEP * current, delta))
    _strategy["min_ev"] = round(max(_TUNE_BOUNDS["min_ev"][0], min(_TUNE_BOUNDS["min_ev"][1], current + capped)), 4)

    price_bands = [(0.25, []), (0.30, []), (0.35, []), (0.40, []), (0.45, []), (0.55, []), (0.65, [])]
    for m in recent:
        pos = m.get("position", {})
        ep = pos.get("entry_price", 0)
        for ceiling, group in price_bands:
            if ep <= ceiling:
                group.append(m.get("pnl", 0) or 0)

    best_price_ceil = _strategy["max_price"]
    best_price_pnl = 0
    for ceiling, pnls in price_bands:
        if len(pnls) >= 5:
            total = sum(pnls)
            if total > best_price_pnl:
                best_price_pnl = total
                best_price_ceil = ceiling

    current = _strategy["max_price"]
    delta = best_price_ceil - current
    capped = max(-_TUNE_MAX_STEP * current, min(_TUNE_MAX_STEP * current, delta))
    _strategy["max_price"] = round(max(_TUNE_BOUNDS["max_price"][0], min(_TUNE_BOUNDS["max_price"][1], current + capped)), 4)

    changes = []
    for k in ("min_ev", "max_price", "kelly_fraction"):
        if abs(_strategy[k] - old[k]) > 0.001:
            changes.append(f"{k}: {old[k]:.3f}->{_strategy[k]:.3f}")

    if changes:
        print(f"  [TUNE] {', '.join(changes)}")
        try:
            STRATEGY_FILE.write_text(json.dumps(_strategy, indent=2), encoding="utf-8")
        except Exception as e:
            print(f"  [TUNE] Failed to save: {e}")

# =============================================================================
# REPORT
# =============================================================================

def print_status():
    state    = load_state()
    markets  = load_all_markets()
    open_pos = [m for m in markets if m.get("position") and m["position"].get("status") == "open"]
    resolved = [m for m in markets if m["status"] == "resolved" and m.get("pnl") is not None]

    bal     = state["balance"]
    start   = state["starting_balance"]
    ret_pct = (bal - start) / start * 100
    wins    = state["wins"]
    losses  = state["losses"]
    total   = wins + losses

    # Also show live on-chain balance
    real_bal = get_real_balance()
    real_str = f"  On-chain USDC: ${real_bal:,.2f}" if real_bal is not None else "  On-chain USDC: (unavailable)"

    print(f"\n{'='*55}")
    print(f"  WEATHERBET v3 — STATUS (REAL TRADES)")
    print(f"{'='*55}")
    print(f"  Balance:     ${bal:,.2f}  (start ${start:,.2f}, {'+'if ret_pct>=0 else ''}{ret_pct:.1f}%)")
    print(real_str)
    print(f"  Trades:      {total} | W: {wins} | L: {losses} | WR: {wins/total:.0%}" if total else "  No trades yet")
    print(f"  Open:        {len(open_pos)}")
    print(f"  Resolved:    {len(resolved)}")

    if open_pos:
        print(f"\n  Open positions:")
        total_unrealized = 0.0
        for m in open_pos:
            pos      = m["position"]
            unit_sym = "F" if m["unit"] == "F" else "C"
            label    = f"{pos['bucket_low']}-{pos['bucket_high']}{unit_sym}"

            current_price = pos["entry_price"]
            for o in m.get("all_outcomes", []):
                if o["market_id"] == pos["market_id"]:
                    current_price = o["price"]
                    break

            unrealized = round((current_price - pos["entry_price"]) * pos["shares"], 2)
            total_unrealized += unrealized
            pnl_str = f"{'+'if unrealized>=0 else ''}{unrealized:.2f}"

            print(f"    {m['city_name']:<16} {m['date']} | {label:<14} | "
                  f"entry ${pos['entry_price']:.3f} -> ${current_price:.3f} | "
                  f"PnL: {pnl_str} | {(pos.get('forecast_src') or 'blend').upper()}")

        sign = "+" if total_unrealized >= 0 else ""
        print(f"\n  Unrealized PnL: {sign}{total_unrealized:.2f}")

    print(f"{'='*55}\n")

def print_report():
    markets  = load_all_markets()
    resolved = [m for m in markets if m["status"] == "resolved" and m.get("pnl") is not None]

    print(f"\n{'='*55}")
    print(f"  WEATHERBET v3 — FULL REPORT")
    print(f"{'='*55}")

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
        pos      = m.get("position", {})
        unit_sym = "F" if m["unit"] == "F" else "C"
        snaps    = m.get("forecast_snapshots", [])
        first_fc = snaps[0]["best"] if snaps else None
        last_fc  = snaps[-1]["best"] if snaps else None
        label    = f"{pos.get('bucket_low')}-{pos.get('bucket_high')}{unit_sym}" if pos else "no position"
        result   = m["resolved_outcome"].upper()
        pnl_str  = f"{'+'if m['pnl']>=0 else ''}{m['pnl']:.2f}" if m["pnl"] is not None else "-"
        fc_str   = f"forecast {first_fc}->{last_fc}{unit_sym}" if first_fc else "no forecast"
        actual   = f"actual {m['actual_temp']}{unit_sym}" if m["actual_temp"] else ""
        print(f"    {m['city_name']:<16} {m['date']} | {label:<14} | {fc_str} | {actual} | {result} {pnl_str}")

    print(f"{'='*55}\n")

# =============================================================================
# MAIN LOOP
# =============================================================================

# MONITOR_INTERVAL is now read from config above

def monitor_positions():
    """Quick stop check on open positions without full scan."""
    markets  = load_all_markets()
    open_pos = [m for m in markets if m.get("position") and m["position"].get("status") == "open"]

    # Reconcile: check closed positions for orphaned on-chain shares
    closed_mkts = [m for m in markets if m.get("position") and m["position"].get("status") == "closed"
                   and m["status"] != "resolved"]
    for mkt in closed_mkts:
        pos = mkt["position"]
        if not pos.get("token_id"):
            continue
        onchain = get_token_balance(pos["token_id"])
        if onchain >= 1.0:
            city_name = LOCATIONS.get(mkt["city"], {}).get("name", mkt["city"])
            current_price = pos.get("exit_price", pos["entry_price"])
            try:
                r = requests.get(f"https://gamma-api.polymarket.com/markets/{pos['market_id']}", timeout=(3, 5))
                mdata = r.json()
                prices = json.loads(mdata.get("outcomePrices", "[]"))
                if prices:
                    current_price = float(prices[0])
            except Exception:
                pass
            real_entry = get_real_entry_price(pos["token_id"])
            entry = real_entry if real_entry is not None else current_price
            print(f"  [RECONCILE] {city_name} {mkt['date']} — {onchain:.1f} orphaned shares "
                  f"(entry ${entry:.4f}, market ${current_price:.3f})")
            pos["shares"]       = round(onchain, 2)
            pos["entry_price"]  = entry
            pos["cost"]         = round(onchain * entry, 2)
            pos["status"]       = "open"
            pos["pnl"]          = None
            pos["exit_price"]   = None
            pos["close_reason"] = None
            pos["closed_at"]    = None
            pos["reconciled"]   = True
            save_market(mkt)
            open_pos.append(mkt)
        time.sleep(0.05)

    if not open_pos:
        return 0

    state   = load_state()
    balance = state["balance"]
    closed  = 0

    for mkt in open_pos:
        pos = mkt["position"]
        mid = pos["market_id"]
        mutated = False

        current_price = None
        try:
            r = requests.get(f"https://gamma-api.polymarket.com/markets/{mid}", timeout=(3, 5))
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

        entry = pos["entry_price"]
        stop  = pos.get("stop_price", entry * STOP_LOSS_PCT)
        city_name = LOCATIONS.get(mkt["city"], {}).get("name", mkt["city"])

        end_date = mkt.get("event_end_date", "")
        hours_left = hours_to_resolution(end_date) if end_date else 999.0

        if hours_left < 24:
            take_profit = TAKE_PROFIT_FINAL
        elif hours_left < 48:
            take_profit = TAKE_PROFIT_SHORT
        else:
            take_profit = TAKE_PROFIT_LONG

        if current_price >= entry * 1.20 and stop < entry:
            pos["stop_price"] = entry
            pos["trailing_activated"] = True
            mutated = True
            print(f"  [TRAILING] {city_name} {mkt['date']} — stop moved to breakeven ${entry:.3f}")

        roi_threshold = entry * (1.0 + TAKE_PROFIT_ROI)
        take_triggered = current_price >= take_profit or current_price >= roi_threshold
        stop_triggered = current_price <= stop

        if take_triggered or stop_triggered:
            onchain = get_token_balance(pos["token_id"])
            if onchain < 1.0:
                pnl = round((current_price - entry) * pos["shares"], 2)
                pos["exit_price"]   = current_price
                pos["pnl"]          = pnl
                pos["status"]       = "closed"
                pos["close_reason"] = "sold_externally"
                pos["closed_at"]    = datetime.now(timezone.utc).isoformat()
                closed += 1
                mutated = True
                print(f"  [CLOSED] {city_name} {mkt['date']} — 0 shares on-chain, position already sold/resolved")
            else:
                resp = place_sell_order(pos["token_id"], pos["shares"], current_price, market_id=pos["market_id"])
                if resp is not None:
                    pnl = round((current_price - entry) * pos["shares"], 2)
                    balance += pos["cost"] + pnl
                    pos["closed_at"] = datetime.now(timezone.utc).isoformat()
                    if current_price >= roi_threshold and current_price < take_profit:
                        pos["close_reason"] = "take_profit_roi"
                        reason = "TAKE ROI"
                    elif take_triggered:
                        pos["close_reason"] = "take_profit"
                        reason = "TAKE"
                    elif current_price < entry:
                        pos["close_reason"] = "stop_loss"
                        reason = "STOP"
                    else:
                        pos["close_reason"] = "trailing_stop"
                        reason = "TRAILING BE"
                    pos["exit_price"]   = current_price
                    pos["pnl"]          = pnl
                    pos["status"]       = "closed"
                    closed += 1
                    mutated = True
                    print(f"  [{reason}] {city_name} {mkt['date']} | entry ${entry:.3f} exit ${current_price:.3f} | {hours_left:.0f}h left | PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")
                else:
                    print(f"  [SELL FAIL] {city_name} {mkt['date']} — will retry next cycle")

        if mutated:
            save_market(mkt)

    if closed:
        real_bal = get_real_balance()
        state["balance"] = round(real_bal if real_bal is not None else balance, 2)
        save_state(state)

    return closed


def run_loop():
    global _cal
    _cal = load_cal()

    print(f"\n{'='*55}")
    print(f"  WEATHERBET v3 — STARTING (REAL TRADES)")
    print(f"{'='*55}")
    print(f"  Cities:     {len(LOCATIONS)}")
    print(f"  Max bet:    ${MAX_BET} | Min EV: {_strategy['min_ev']} | Max price: {_strategy['max_price']}")
    print(f"  Kelly:      {_strategy['kelly_fraction']} | Tuning: {'ON' if TUNE_ENABLED else 'OFF'}")
    print(f"  Scan:       {SCAN_INTERVAL//60} min | Monitor: {MONITOR_INTERVAL//60} min")
    print(f"  Sources:    ECMWF + HRRR(US) ensemble | METAR(ref only)")
    print(f"  Limits:     {MAX_OPEN_POS} max positions | {MAX_POS_PER_DATE}/date")
    print(f"  Data:       {DATA_DIR.resolve()}")

    if not VC_KEY_VALID:
        print(f"  WARNING: vc_key not configured — calibration disabled (no actual temps)")

    real_bal = get_real_balance()
    if real_bal is None:
        print(f"  WARNING: Could not fetch on-chain balance — check your keys in config.json")
    else:
        print(f"  Wallet USDC: ${real_bal:,.2f}")
    print(f"  Ctrl+C to stop\n")

    last_full_scan = 0

    while True:
        now_ts  = time.time()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if now_ts - last_full_scan >= SCAN_INTERVAL:
            print(f"[{now_str}] full scan...")
            try:
                new_pos, closed, resolved = scan_and_update()
                state = load_state()
                print(f"  balance: ${state['balance']:,.2f} | "
                      f"new: {new_pos} | closed: {closed} | resolved: {resolved}")
                last_full_scan = time.time()
            except KeyboardInterrupt:
                print(f"\n  Stopping — saving state...")
                save_state(load_state())
                print(f"  Done. Bye!")
                break
            except requests.exceptions.ConnectionError:
                print(f"  Connection lost — waiting 60 sec")
                time.sleep(60)
                continue
            except Exception as e:
                print(f"  Error: {e} — waiting 60 sec")
                time.sleep(60)
                continue
        else:
            print(f"[{now_str}] monitoring positions...")
            try:
                stopped = monitor_positions()
                if stopped:
                    state = load_state()
                    print(f"  balance: ${state['balance']:,.2f}")
            except Exception as e:
                print(f"  Monitor error: {e}")

        try:
            time.sleep(MONITOR_INTERVAL)
        except KeyboardInterrupt:
            print(f"\n  Stopping — saving state...")
            save_state(load_state())
            print(f"  Done. Bye!")
            break

# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run":
        run_loop()
    elif cmd == "status":
        _cal = load_cal()
        print_status()
    elif cmd == "report":
        _cal = load_cal()
        print_report()
    else:
        print("Usage: python bot_v3.py [run|status|report]")
