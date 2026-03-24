#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bot_v2.py — Weather Trading Bot for Polymarket
=====================================================
Tracks weather forecasts from 3 sources (ECMWF, HRRR, METAR),
compares with Polymarket markets, paper trades using Kelly criterion.

Usage:
    python bot_v2.py          # main loop
    python bot_v2.py report   # full report
    python bot_v2.py status   # balance and open positions
    python bot_v2.py health   # GOOD/WARNING/BAD + reasons
"""

import re
import sys
import json
import math
import time
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

# =============================================================================
# CONFIG
# =============================================================================

with open("config.json", encoding="utf-8") as f:
    _cfg = json.load(f)

BALANCE          = _cfg.get("balance", 10000.0)
MAX_BET          = _cfg.get("max_bet", 20.0)        # max bet per trade
MIN_EV           = _cfg.get("min_ev", 0.10)
MAX_PRICE        = _cfg.get("max_price", 0.45)
MIN_VOLUME       = _cfg.get("min_volume", 500)
MIN_HOURS        = _cfg.get("min_hours", 2.0)
MAX_HOURS        = _cfg.get("max_hours", 72.0)
KELLY_FRACTION   = _cfg.get("kelly_fraction", 0.25)
MAX_SLIPPAGE     = _cfg.get("max_slippage", 0.03)  # max allowed ask-bid spread
SCAN_INTERVAL    = _cfg.get("scan_interval", 3600)   # every hour
CALIBRATION_MIN  = _cfg.get("calibration_min", 30)
VC_KEY           = _cfg.get("vc_key", "")

SIGMA_F = 2.0
SIGMA_C = 1.2

DATA_DIR         = Path("data")
DATA_DIR.mkdir(exist_ok=True)
STATE_FILE       = DATA_DIR / "state.json"
SIM_EXPORT_FILE  = Path("simulation.json")  # aggregate for sim_dashboard_repost.html
MARKETS_DIR      = DATA_DIR / "markets"
MARKETS_DIR.mkdir(exist_ok=True)
CALIBRATION_FILE = DATA_DIR / "calibration.json"

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
# MATH
# =============================================================================

def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def bucket_prob(forecast, t_low, t_high, sigma=None):
    """Probability that realized temp lands inside a bucket."""
    s = sigma or 2.0
    mu = float(forecast)
    if s <= 0:
        return 1.0 if in_bucket(mu, t_low, t_high) else 0.0
    if t_low == -999:
        return norm_cdf((t_high - mu) / s)
    if t_high == 999:
        return 1.0 - norm_cdf((t_low - mu) / s)
    if t_low == t_high:
        lo, hi = t_low - 0.5, t_high + 0.5
    else:
        lo, hi = t_low, t_high
    p = norm_cdf((hi - mu) / s) - norm_cdf((lo - mu) / s)
    return max(0.0, min(1.0, p))

def calc_ev(p, price):
    if price <= 0 or price >= 1: return 0.0
    return round(p * (1.0 / price - 1.0) - (1.0 - p), 4)

def calc_kelly(p, price):
    if price <= 0 or price >= 1: return 0.0
    b = 1.0 / price - 1.0
    f = (p * b - (1.0 - p)) / b
    return round(min(max(0.0, f) * KELLY_FRACTION, 1.0), 4)

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

def get_sigma(city_slug, source="ecmwf"):
    key = f"{city_slug}_{source}"
    if key in _cal:
        return _cal[key]["sigma"]
    return SIGMA_F if LOCATIONS[city_slug]["unit"] == "F" else SIGMA_C

def run_calibration(markets):
    """Recalculates sigma from resolved markets."""
    resolved = [m for m in markets if m.get("status") == "resolved" and m.get("actual_temp") is not None]
    cal = load_cal()
    updated = []

    for source in ["ecmwf", "hrrr", "metar"]:
        for city in set(m["city"] for m in resolved):
            group = [m for m in resolved if m["city"] == city]
            errors = []
            for m in group:
                snap = next((s for s in reversed(m.get("forecast_snapshots", []))
                             if s.get("best_source") == source), None)
                if snap and snap.get("best") is not None:
                    errors.append(abs(snap["best"] - m["actual_temp"]))
            if len(errors) < CALIBRATION_MIN:
                continue
            mae  = sum(errors) / len(errors)
            key  = f"{city}_{source}"
            old  = cal.get(key, {}).get("sigma", SIGMA_F if LOCATIONS[city]["unit"] == "F" else SIGMA_C)
            new  = round(mae, 3)
            cal[key] = {"sigma": new, "n": len(errors), "updated_at": datetime.now(timezone.utc).isoformat()}
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
    try:
        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={loc['lat']}&longitude={loc['lon']}"
            f"&daily=temperature_2m_max&temperature_unit={temp_unit}"
            f"&forecast_days=7&timezone={TIMEZONES.get(city_slug, 'UTC')}"
            f"&models=ecmwf_ifs025&bias_correction=true"
        )
        data = requests.get(url, timeout=(5, 8)).json()
        if "error" not in data:
            for date, temp in zip(data["daily"]["time"], data["daily"]["temperature_2m_max"]):
                if date in dates and temp is not None:
                    result[date] = round(temp, 1) if unit == "C" else round(temp)
    except Exception as e:
        print(f"  [ECMWF] {city_slug}: {e}")
    return result

def get_hrrr(city_slug, dates):
    """HRRR via Open-Meteo. US cities only, up to 48h horizon."""
    loc = LOCATIONS[city_slug]
    if loc["region"] != "us":
        return {}
    result = {}
    try:
        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={loc['lat']}&longitude={loc['lon']}"
            f"&daily=temperature_2m_max&temperature_unit=fahrenheit"
            f"&forecast_days=3&timezone={TIMEZONES.get(city_slug, 'UTC')}"
            f"&models=gfs_seamless"  # HRRR+GFS seamless — best option for US
        )
        data = requests.get(url, timeout=(5, 8)).json()
        if "error" not in data:
            for date, temp in zip(data["daily"]["time"], data["daily"]["temperature_2m_max"]):
                if date in dates and temp is not None:
                    result[date] = round(temp)
    except Exception as e:
        print(f"  [HRRR] {city_slug}: {e}")
    return result

def get_metar(city_slug):
    """Current observed temperature from METAR station. D+0 only."""
    loc = LOCATIONS[city_slug]
    station = loc["station"]
    unit = loc["unit"]
    try:
        url = f"https://aviationweather.gov/api/data/metar?ids={station}&format=json"
        data = requests.get(url, timeout=(5, 8)).json()
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
    """Actual temperature via Visual Crossing for closed markets."""
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
    Returns: None (still open), True (YES won), False (NO won)
    """
    try:
        r = requests.get(f"https://gamma-api.polymarket.com/markets/{market_id}", timeout=(5, 8))
        data = r.json()
        closed = data.get("closed", False)
        if not closed:
            return None
        # Check YES price — if ~1.0 then WIN, if ~0.0 then LOSS
        prices = json.loads(data.get("outcomePrices", "[0.5,0.5]"))
        yes_price = float(prices[0])
        if yes_price >= 0.95:
            return True   # WIN
        elif yes_price <= 0.05:
            return False  # LOSS
        return None  # not yet determined
    except Exception as e:
        print(f"  [RESOLVE] {market_id}: {e}")
    return None

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

def _to_float(v):
    try:
        if v is None:
            return None
        return float(v)
    except Exception:
        return None

def extract_yes_quotes(market):
    """
    Returns YES-side executable quote tuple: (bid, ask, mid, spread, is_valid).
    Prefers explicit bestBid/bestAsk from Gamma.
    """
    best_bid = _to_float(market.get("bestBid"))
    best_ask = _to_float(market.get("bestAsk"))
    yes_mid = None
    try:
        prices = json.loads(market.get("outcomePrices", "[0.5,0.5]"))
        if prices and len(prices) > 0:
            yes_mid = _to_float(prices[0])
    except Exception:
        pass

    # Prefer real executable orderbook quotes when sane.
    if best_bid is not None and best_ask is not None:
        if 0.0 <= best_bid <= 1.0 and 0.0 <= best_ask <= 1.0 and best_ask >= best_bid:
            mid = yes_mid if yes_mid is not None else (best_bid + best_ask) / 2.0
            return best_bid, best_ask, mid, (best_ask - best_bid), True

    # Fallback to midpoint-only quote (non-executable). Mark invalid to skip trading.
    if yes_mid is not None and 0.0 <= yes_mid <= 1.0:
        return yes_mid, yes_mid, yes_mid, 0.0, False

    return None, None, None, None, False

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
# Each market is stored in a separate file: data/markets/{city}_{date}.json
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
        "status":             "open",           # open | closed | resolved
        "position":           None,             # filled when position opens
        "actual_temp":        None,             # filled after resolution
        "resolved_outcome":   None,             # win / loss / no_position
        "pnl":                None,
        "forecast_snapshots": [],               # list of forecast snapshots
        "market_snapshots":   [],               # list of market price snapshots
        "all_outcomes":       [],               # all market buckets
        "created_at":         datetime.now(timezone.utc).isoformat(),
    }

# =============================================================================
# STATE (balance and open positions)
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

def write_simulation_export():
    """Merge state.json + data/markets/*.json into one file for the HTML dashboard."""
    state = load_state()
    markets = load_all_markets()
    positions = {}
    trades = []

    for m in markets:
        pos = m.get("position")
        if not pos:
            continue
        key = f"{m['city']}_{m['date']}"
        city_name = m.get("city_name", m["city"])
        q = pos.get("question", "")
        kelly = pos.get("kelly", 0)

        if pos.get("status") == "open":
            current_price = pos["entry_price"]
            for o in m.get("all_outcomes", []):
                if o.get("market_id") == pos.get("market_id"):
                    current_price = o.get("price", current_price)
                    break
            unrealized = round((current_price - pos["entry_price"]) * pos["shares"], 2)
            positions[key] = {
                "question": q,
                "location": city_name,
                "kelly_pct": kelly,
                "ev": pos.get("ev", 0),
                "cost": pos.get("cost", 0),
                "current_price": current_price,
                "entry_price": pos.get("entry_price"),
                "pnl": unrealized,
            }
            trades.append({
                "type": "entry",
                "question": q,
                "location": city_name,
                "date": m.get("date"),
                "opened_at": pos.get("opened_at") or "",
                "kelly_pct": kelly,
                "ev": pos.get("ev", 0),
                "cost": pos.get("cost", 0),
                "entry_price": pos.get("entry_price"),
                "our_prob": pos.get("p", 0),
            })
        else:
            trades.append({
                "type": "entry",
                "question": q,
                "location": city_name,
                "date": m.get("date"),
                "opened_at": pos.get("opened_at") or "",
                "kelly_pct": kelly,
                "ev": pos.get("ev", 0),
                "cost": pos.get("cost", 0),
                "entry_price": pos.get("entry_price"),
                "our_prob": pos.get("p", 0),
            })
            trades.append({
                "type": "exit",
                "question": q,
                "location": city_name,
                "closed_at": pos.get("closed_at") or "",
                "pnl": pos.get("pnl", 0),
                "close_reason": pos.get("close_reason"),
            })

    trades.sort(key=lambda t: (t.get("opened_at") or t.get("closed_at") or ""))

    out = {
        "balance": state["balance"],
        "starting_balance": state["starting_balance"],
        "total_trades": state["total_trades"],
        "wins": state["wins"],
        "losses": state["losses"],
        "peak_balance": state.get("peak_balance", state["balance"]),
        "positions": positions,
        "trades": trades,
        "last_scan": state.get("last_scan"),
    }
    SIM_EXPORT_FILE.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

# =============================================================================
# CORE LOGIC
# =============================================================================

def take_forecast_snapshot(city_slug, dates):
    """Fetches forecasts from all sources and returns a snapshot."""
    now_str = datetime.now(timezone.utc).isoformat()
    ecmwf   = get_ecmwf(city_slug, dates)
    hrrr    = get_hrrr(city_slug, dates)
    today   = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    snapshots = {}
    for date in dates:
        snap = {
            "ts":    now_str,
            "ecmwf": ecmwf.get(date),
            "hrrr":  hrrr.get(date) if date <= (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%d") else None,
            "metar": get_metar(city_slug) if date == today else None,
        }
        # Best forecast: HRRR for US D+0/D+1, otherwise ECMWF
        loc = LOCATIONS[city_slug]
        if loc["region"] == "us" and snap["hrrr"] is not None:
            snap["best"] = snap["hrrr"]
            snap["best_source"] = "hrrr"
        elif snap["ecmwf"] is not None:
            snap["best"] = snap["ecmwf"]
            snap["best_source"] = "ecmwf"
        else:
            snap["best"] = None
            snap["best_source"] = None
        snapshots[date] = snap
    return snapshots

def scan_and_update():
    """Main function of one cycle: updates forecasts, opens/closes positions."""
    global _cal
    now      = datetime.now(timezone.utc)
    state    = load_state()
    balance  = state["balance"]
    new_pos  = 0
    closed   = 0
    resolved = 0
    diagnostics = {
        "eligible_markets": 0,
        "skipped_no_book": 0,
        "skipped_bad_quote": 0,
        "skipped_spread": 0,
        "skipped_price_or_volume": 0,
        "skipped_ev": 0,
    }

    for city_slug, loc in LOCATIONS.items():
        unit = loc["unit"]
        unit_sym = "F" if unit == "F" else "C"
        print(f"  -> {loc['name']}...", end=" ", flush=True)

        try:
            dates = [(now + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(4)]
            snapshots = take_forecast_snapshot(city_slug, dates)
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

            # Load or create market record
            mkt = load_market(city_slug, date)
            if mkt is None:
                if hours < MIN_HOURS or hours > MAX_HOURS:
                    continue
                mkt = new_market(city_slug, date, event, hours)

            # Skip if market already resolved
            if mkt["status"] == "resolved":
                continue

            # Update outcomes list — use explicit YES bestBid/bestAsk quotes.
            outcomes = []
            for market in event.get("markets", []):
                question = market.get("question", "")
                mid      = str(market.get("id", ""))
                volume   = float(market.get("volume", 0))
                rng      = parse_temp_range(question)
                if not rng:
                    continue
                bid, ask, mid_price, spread, has_book = extract_yes_quotes(market)
                if bid is None or ask is None or mid_price is None:
                    continue
                outcomes.append({
                    "question":  question,
                    "market_id": mid,
                    "range":     rng,
                    "bid":       round(bid, 4),
                    "ask":       round(ask, 4),
                    "price":     round(mid_price, 4),   # midpoint/mark
                    "spread":    round(spread, 4),
                    "has_book":  has_book,
                    "volume":    round(volume, 0),
                })

            outcomes.sort(key=lambda x: x["range"][0])
            mkt["all_outcomes"] = outcomes

            # Forecast snapshot
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

            # Market price snapshot
            top = max(outcomes, key=lambda x: x["price"]) if outcomes else None
            market_snap = {
                "ts":       snap.get("ts"),
                "top_bucket": f"{top['range'][0]}-{top['range'][1]}{unit_sym}" if top else None,
                "top_price":  top["price"] if top else None,
            }
            mkt["market_snapshots"].append(market_snap)

            forecast_temp = snap.get("best")
            best_source   = snap.get("best_source")

            # --- STOP-LOSS AND TRAILING STOP ---
            if mkt.get("position") and mkt["position"].get("status") == "open":
                pos = mkt["position"]
                current_price = None
                for o in outcomes:
                    if o["market_id"] == pos["market_id"]:
                        current_price = o["price"]
                        break

                if current_price is not None:
                    current_price = next(
                        (x.get("bid", current_price) for x in outcomes if x["market_id"] == pos["market_id"]),
                        current_price
                    )  # sell at bid
                    entry = pos["entry_price"]
                    stop  = pos.get("stop_price", entry * 0.80)  # 20% stop by default

                    # Trailing: if up 20%+ — move stop to breakeven
                    if current_price >= entry * 1.20 and stop < entry:
                        pos["stop_price"] = entry
                        pos["trailing_activated"] = True

                    # Check stop
                    if current_price <= stop:
                        pnl = round((current_price - entry) * pos["shares"], 2)
                        balance += pos["cost"] + pnl
                        pos["closed_at"]    = snap.get("ts")
                        pos["close_reason"] = "stop_loss" if current_price < entry else "trailing_stop"
                        pos["exit_price"]   = current_price
                        pos["pnl"]          = pnl
                        pos["status"]       = "closed"
                        closed += 1
                        reason = "STOP" if current_price < entry else "TRAILING BE"
                        print(f"  [{reason}] {loc['name']} {date} | entry ${entry:.3f} exit ${current_price:.3f} | PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")

            # --- CLOSE POSITION if forecast shifted 2+ degrees ---
            if mkt.get("position") and forecast_temp is not None:
                pos = mkt["position"]
                old_bucket_low  = pos["bucket_low"]
                old_bucket_high = pos["bucket_high"]
                # 2-degree buffer — avoid closing on small forecast fluctuations
                unit = loc["unit"]
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
                        current_price = next(
                            (x.get("bid", current_price) for x in outcomes if x["market_id"] == pos["market_id"]),
                            current_price
                        )  # sell at bid
                        pnl = round((current_price - pos["entry_price"]) * pos["shares"], 2)
                        balance += pos["cost"] + pnl
                        mkt["position"]["closed_at"]    = snap.get("ts")
                        mkt["position"]["close_reason"] = "forecast_changed"
                        mkt["position"]["exit_price"]   = current_price
                        mkt["position"]["pnl"]          = pnl
                        mkt["position"]["status"]       = "closed"
                        closed += 1
                        print(f"  [CLOSE] {loc['name']} {date} — forecast changed | PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")

            # --- OPEN POSITION ---
            if not mkt.get("position") and forecast_temp is not None and hours >= MIN_HOURS:
                sigma = get_sigma(city_slug, best_source or "ecmwf")
                best_signal = None

                for o in outcomes:
                    t_low, t_high = o["range"]
                    price = o["price"]
                    volume = o["volume"]
                    diagnostics["eligible_markets"] += 1

                    if not in_bucket(forecast_temp, t_low, t_high):
                        continue

                    bid    = o.get("bid", o["price"])
                    ask    = o.get("ask", o["price"])
                    spread = o.get("spread", 0)
                    has_book = o.get("has_book", False)

                    # Require executable orderbook quotes for realistic fills.
                    if not has_book:
                        diagnostics["skipped_no_book"] += 1
                        continue
                    if ask < bid or ask <= 0 or ask >= 1 or bid < 0 or bid > 1:
                        diagnostics["skipped_bad_quote"] += 1
                        continue

                    # Slippage filter
                    if spread > MAX_SLIPPAGE:
                        diagnostics["skipped_spread"] += 1
                        continue
                    if ask >= MAX_PRICE or volume < MIN_VOLUME:
                        diagnostics["skipped_price_or_volume"] += 1
                        continue

                    p  = bucket_prob(forecast_temp, t_low, t_high, sigma)
                    ev = calc_ev(p, ask)   # EV calculated from ask
                    if ev < MIN_EV:
                        diagnostics["skipped_ev"] += 1
                        continue

                    kelly = calc_kelly(p, ask)
                    size  = bet_size(kelly, balance)
                    if size < 0.50:
                        continue

                    best_signal = {
                        "market_id":    o["market_id"],
                        "question":     o["question"],
                        "bucket_low":   t_low,
                        "bucket_high":  t_high,
                        "entry_price":  ask,       # enter at ask
                        "bid_at_entry": bid,
                        "spread":       spread,
                        "shares":       round(size / ask, 2),
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
                    }
                    break

                if best_signal:
                    balance -= best_signal["cost"]
                    mkt["position"] = best_signal
                    state["total_trades"] += 1
                    new_pos += 1
                    bucket_label = f"{best_signal['bucket_low']}-{best_signal['bucket_high']}{unit_sym}"
                    print(f"  [BUY]  {loc['name']} {horizon} {date} | {bucket_label} | "
                          f"${best_signal['entry_price']:.3f} | EV {best_signal['ev']:+.2f} | "
                          f"${best_signal['cost']:.2f} ({best_signal['forecast_src'].upper()})")

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

        # Check if market closed on Polymarket
        won = check_market_resolved(market_id)
        if won is None:
            continue  # market still open

        # Market closed — record result
        price  = pos["entry_price"]
        size   = pos["cost"]
        shares = pos["shares"]
        pnl    = round(shares * (1 - price), 2) if won else round(-size, 2)
        if mkt.get("actual_temp") is None:
            actual_temp = get_actual_temp(mkt["city"], mkt["date"])
            if actual_temp is not None:
                mkt["actual_temp"] = actual_temp

        balance += size + pnl
        pos["exit_price"]   = 1.0 if won else 0.0
        pos["pnl"]          = pnl
        pos["close_reason"] = "resolved"
        pos["closed_at"]    = now.isoformat()
        pos["status"]       = "closed"
        mkt["pnl"]          = pnl
        mkt["status"]       = "resolved"
        mkt["resolved_outcome"] = "win" if won else "loss"

        if won:
            state["wins"] += 1
        else:
            state["losses"] += 1

        result = "WIN" if won else "LOSS"
        print(f"  [{result}] {mkt['city_name']} {mkt['date']} | PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")
        resolved += 1

        save_market(mkt)
        time.sleep(0.3)

    state["balance"]      = round(balance, 2)
    state["peak_balance"] = max(state.get("peak_balance", balance), balance)
    state["last_scan"]    = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "new": new_pos,
        "closed": closed,
        "resolved": resolved,
        "diagnostics": diagnostics,
    }
    save_state(state)
    write_simulation_export()

    # Run calibration if enough data collected
    all_mkts = load_all_markets()
    resolved_count = len([m for m in all_mkts if m["status"] == "resolved"])
    if resolved_count >= CALIBRATION_MIN:
        global _cal
        _cal = run_calibration(all_mkts)

    return new_pos, closed, resolved, diagnostics

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
    last_scan = state.get("last_scan", {})

    print(f"\n{'='*55}")
    print(f"  WEATHERBET — STATUS")
    print(f"{'='*55}")
    print(f"  Balance:     ${bal:,.2f}  (start ${start:,.2f}, {'+'if ret_pct>=0 else ''}{ret_pct:.1f}%)")
    print(f"  Trades:      {total} | W: {wins} | L: {losses} | WR: {wins/total:.0%}" if total else "  No trades yet")
    print(f"  Open:        {len(open_pos)}")
    print(f"  Resolved:    {len(resolved)}")
    if last_scan:
        d = last_scan.get("diagnostics", {})
        print(f"  Last scan:   {last_scan.get('ts', '-')}")
        print(f"  Quality:     no_book={d.get('skipped_no_book', 0)} | "
              f"bad_quote={d.get('skipped_bad_quote', 0)} | spread={d.get('skipped_spread', 0)}")

    if open_pos:
        print(f"\n  Open positions:")
        total_unrealized = 0.0
        for m in open_pos:
            pos      = m["position"]
            unit_sym = "F" if m["unit"] == "F" else "C"
            label    = f"{pos['bucket_low']}-{pos['bucket_high']}{unit_sym}"

            # Current price from latest market snapshot
            current_price = pos["entry_price"]
            snaps = m.get("market_snapshots", [])
            if snaps:
                # Find our bucket price in all_outcomes
                for o in m.get("all_outcomes", []):
                    if o["market_id"] == pos["market_id"]:
                        current_price = o["price"]
                        break

            unrealized = round((current_price - pos["entry_price"]) * pos["shares"], 2)
            total_unrealized += unrealized
            pnl_str = f"{'+'if unrealized>=0 else ''}{unrealized:.2f}"

            print(f"    {m['city_name']:<16} {m['date']} | {label:<14} | "
                  f"entry ${pos['entry_price']:.3f} -> ${current_price:.3f} | "
                  f"PnL: {pnl_str} | {pos['forecast_src'].upper()}")

        sign = "+" if total_unrealized >= 0 else ""
        print(f"\n  Unrealized PnL: {sign}{total_unrealized:.2f}")

    write_simulation_export()
    print(f"{'='*55}\n")

def print_report():
    markets  = load_all_markets()
    resolved = [m for m in markets if m["status"] == "resolved" and m.get("pnl") is not None]

    print(f"\n{'='*55}")
    print(f"  WEATHERBET — FULL REPORT")
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

def print_explain():
    print(f"\n{'='*55}")
    print(f"  WEATHERBET — EXPLAIN")
    print(f"{'='*55}")
    print("  What this bot does:")
    print("  1) Reads weather forecasts for each city/date.")
    print("  2) Reads Polymarket quotes (best bid/ask).")
    print("  3) Buys only when expected value and risk filters pass.")
    print("  4) Closes on stop-loss, forecast shift, or market resolution.")
    print("")
    print("  What to check first:")
    print("  - Run: python bot_v2.py status")
    print("  - Quality line should not show huge bad_quote counts.")
    print("  - Open positions should use realistic entry prices (ask side).")
    print("")
    print("  If older data was created before quote-fix update:")
    print("  - Backup then clear the data folder for a clean run.")
    print("  - PowerShell: Remove-Item -Recurse -Force .\\data")
    print("  - Then run bot again and let it collect fresh snapshots.")
    print("")
    print("  How to judge outcomes (simple):")
    print("  - First 1-2 weeks: focus on data quality and stability, not PnL.")
    print("  - After 50+ resolved trades: check win rate + average PnL per trade.")
    print("  - After calibration grows: watch if sigma values stabilize.")
    print("")
    print("  One-line system check:")
    print("  - Run: python bot_v2.py health")
    print(f"{'='*55}\n")


def probe_external_apis():
    """
    Quick live checks (short timeouts). Used by `health` only — not part of trading loop.
    """
    out = {}

    try:
        url = (
            "https://api.open-meteo.com/v1/forecast"
            "?latitude=40.78&longitude=-73.87&daily=temperature_2m_max&forecast_days=1"
        )
        r = requests.get(url, timeout=(3, 6))
        ok = r.ok and "daily" in r.json()
        out["open_meteo"] = {"ok": ok, "http": r.status_code}
    except Exception as e:
        out["open_meteo"] = {"ok": False, "http": None, "error": str(e)[:100]}

    try:
        r = requests.get(
            "https://gamma-api.polymarket.com/events?slug=highest-temperature-in-chicago-on-march-22-2026",
            timeout=(3, 6),
        )
        data = r.json() if r.ok else []
        ok = r.ok and isinstance(data, list) and len(data) > 0
        out["gamma"] = {"ok": ok, "http": r.status_code}
    except Exception as e:
        out["gamma"] = {"ok": False, "http": None, "error": str(e)[:100]}

    try:
        r = requests.get(
            "https://aviationweather.gov/api/data/metar?ids=KORD&format=json",
            timeout=(3, 6),
        )
        data = r.json()
        out["metar"] = {"ok": r.ok and isinstance(data, list), "http": r.status_code}
    except Exception as e:
        out["metar"] = {"ok": False, "http": None, "error": str(e)[:100]}

    k = (VC_KEY or "").strip()
    if not k or "YOUR" in k.upper():
        out["visual_crossing"] = {"ok": None, "note": "no key (resolution temps optional until set)"}
    else:
        try:
            day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            url = (
                f"https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/timeline"
                f"/KORD/{day}/{day}?unitGroup=us&key={k}&include=days&elements=tempmax"
            )
            r = requests.get(url, timeout=(3, 6))
            jd = r.json() if r.ok else {}
            ok = r.ok and bool(jd.get("days"))
            out["visual_crossing"] = {"ok": ok, "http": r.status_code}
            if not ok and r.ok:
                out["visual_crossing"]["error"] = str(jd.get("message", "no days"))[:80]
        except Exception as e:
            out["visual_crossing"] = {"ok": False, "http": None, "error": str(e)[:100]}

    return out


def print_health():
    """Single-screen GOOD / WARNING / BAD with reasons."""
    state = load_state()
    markets = load_all_markets()
    cal = load_cal()
    last_scan = state.get("last_scan") or {}
    d = last_scan.get("diagnostics", {}) if isinstance(last_scan, dict) else {}

    open_pos = [m for m in markets if m.get("position") and m["position"].get("status") == "open"]
    resolved = [m for m in markets if m["status"] == "resolved"]
    with_actual = [m for m in resolved if m.get("actual_temp") is not None]

    bad, warn, notes, good = [], [], [], []

    apis = probe_external_apis()
    if not apis.get("open_meteo", {}).get("ok"):
        bad.append(
            "Open-Meteo unreachable or bad response — forecasts will fail. "
            + (apis["open_meteo"].get("error") or f"HTTP {apis['open_meteo'].get('http')}")
        )
    else:
        good.append("Open-Meteo OK")

    if not apis.get("gamma", {}).get("ok"):
        bad.append(
            "Polymarket Gamma unreachable or empty — cannot load markets. "
            + (apis["gamma"].get("error") or f"HTTP {apis['gamma'].get('http')}")
        )
    else:
        good.append("Polymarket API OK")

    met = apis.get("metar", {})
    if met.get("ok"):
        good.append("METAR OK")
    else:
        warn.append(
            "METAR check failed — D+0 observation extras may be missing. "
            + (met.get("error") or f"HTTP {met.get('http')}")
        )

    vc = apis.get("visual_crossing", {})
    if vc.get("ok") is None:
        warn.append(
            "Visual Crossing key missing or placeholder — set vc_key in config.json for actual_temp after resolution."
        )
    elif vc.get("ok") is False:
        warn.append(
            "Visual Crossing key present but request failed — check quota/key. "
            + (vc.get("error") or f"HTTP {vc.get('http')}")
        )
    else:
        good.append("Visual Crossing OK")

    eligible = max(0, int(d.get("eligible_markets", 0)))
    no_book = int(d.get("skipped_no_book", 0))
    bad_quote = int(d.get("skipped_bad_quote", 0))
    if eligible > 30:
        badish = (no_book + bad_quote) / eligible
        if badish > 0.55:
            bad.append(
                f"Last scan quote quality very poor: {(badish*100):.0f}% of buckets had no book or bad quotes "
                f"({no_book}+{bad_quote} / {eligible}). Expect few or no trades."
            )
        elif badish > 0.30:
            warn.append(
                f"Last scan: many buckets skipped for quotes ({(badish*100):.0f}% no_book+bad_quote / eligible)."
            )

    ts = last_scan.get("ts")
    if ts:
        try:
            t_end = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if t_end.tzinfo is None:
                t_end = t_end.replace(tzinfo=timezone.utc)
            age_sec = (datetime.now(timezone.utc) - t_end).total_seconds()
            stale = SCAN_INTERVAL * 2 + 900
            if age_sec > stale:
                warn.append(
                    f"No recent full scan (last {int(age_sec/3600)}h ago). "
                    "If the bot should be running, check the terminal for errors or restart it."
                )
            else:
                good.append(f"Last full scan {int(age_sec/60)} min ago")
        except Exception:
            warn.append("Could not parse last_scan timestamp in state.json.")
    else:
        notes.append("No last_scan yet — run one full scan: python bot_v2.py")

    if len(resolved) < CALIBRATION_MIN:
        notes.append(
            f"Calibration still learning: {len(resolved)} resolved / {CALIBRATION_MIN}+ recommended for stable sigma."
        )
    else:
        good.append(f"Enough resolved markets for calibration runs ({len(resolved)}).")

    n_cal = len(cal) if isinstance(cal, dict) else 0
    if n_cal > 0:
        good.append(f"calibration.json has {n_cal} city/source sigmas.")
    elif len(resolved) >= CALIBRATION_MIN:
        warn.append("Many resolved markets but calibration.json empty — next full scan should populate it.")

    legacy_open = 0
    for m in open_pos:
        pos = m["position"]
        for o in m.get("all_outcomes", []):
            if str(o.get("market_id")) == str(pos.get("market_id")):
                if not o.get("has_book", False):
                    legacy_open += 1
                break
    if legacy_open:
        warn.append(
            f"{legacy_open} open position(s) tied to snapshots without has_book (old data). "
            "For clean metrics: close bot, backup data, delete data folder, restart."
        )

    level = "GOOD"
    if bad:
        level = "BAD"
    elif warn:
        level = "WARNING"

    print(f"\n{'='*55}")
    print(f"  WEATHERBET — HEALTH: {level}")
    print(f"{'='*55}")
    print(f"  Balance: ${state.get('balance', 0):,.2f} | Open: {len(open_pos)} | Resolved: {len(resolved)} "
          f"| With actual_temp: {len(with_actual)}")

    if last_scan and ts:
        print(f"  Last scan: {ts}")
        print(
            f"  Scan filters: eligible={eligible} | no_book={no_book} | bad_quote={bad_quote} | "
            f"spread={d.get('skipped_spread', 0)} | ev_below={d.get('skipped_ev', 0)}"
        )

    print(f"\n  APIs (live probe):")
    for name, info in apis.items():
        if info.get("ok") is True:
            print(f"    {name}: OK (HTTP {info.get('http', '-')})")
        elif info.get("ok") is None:
            print(f"    {name}: skipped — {info.get('note', '')}")
        else:
            err = info.get("error") or info.get("note") or f"HTTP {info.get('http')}"
            print(f"    {name}: FAIL — {err}")

    if bad:
        print(f"\n  Blockers:")
        for line in bad:
            print(f"    - {line}")
    if warn:
        print(f"\n  Warnings:")
        for line in warn:
            print(f"    - {line}")
    if notes:
        print(f"\n  Notes:")
        for line in notes:
            print(f"    - {line}")
    if good:
        print(f"\n  OK:")
        for line in good[:10]:
            print(f"    - {line}")

    print(f"\n  What this means:")
    if level == "BAD":
        print("    Fix blockers first; the bot cannot run correctly until APIs and quotes behave.")
    elif level == "WARNING":
        print("    Safe to experiment, but read warnings — results may be incomplete or noisy.")
    else:
        print("    Core checks passed; still monitor PnL only after many resolved trades.")
    print(f"{'='*55}\n")

    write_simulation_export()


# =============================================================================
# MAIN LOOP
# =============================================================================

MONITOR_INTERVAL = 600  # monitor positions every 10 minutes

def monitor_positions():
    """Quick stop check on open positions without full scan."""
    markets  = load_all_markets()
    open_pos = [m for m in markets if m.get("position") and m["position"].get("status") == "open"]
    if not open_pos:
        return 0

    state   = load_state()
    balance = state["balance"]
    closed  = 0

    for mkt in open_pos:
        pos = mkt["position"]
        mid = pos["market_id"]

        # Get current price from all_outcomes (no extra requests)
        current_price = None
        for o in mkt.get("all_outcomes", []):
            if o["market_id"] == mid:
                current_price = o.get("bid", o["price"])  # use bid — sell price
                break

        if current_price is None:
            continue

        entry = pos["entry_price"]
        stop  = pos.get("stop_price", entry * 0.80)

        # Trailing: if up 20%+ — move stop to breakeven
        if current_price >= entry * 1.20 and stop < entry:
            pos["stop_price"] = entry
            pos["trailing_activated"] = True
            city_name = LOCATIONS.get(mkt["city"], {}).get("name", mkt["city"])
            print(f"  [TRAILING] {city_name} {mkt['date']} — stop moved to breakeven ${entry:.3f}")

        # Check stop
        if current_price <= stop:
            pnl = round((current_price - entry) * pos["shares"], 2)
            balance += pos["cost"] + pnl
            pos["closed_at"]    = datetime.now(timezone.utc).isoformat()
            pos["close_reason"] = "stop_loss" if current_price < entry else "trailing_stop"
            pos["exit_price"]   = current_price
            pos["pnl"]          = pnl
            pos["status"]       = "closed"
            closed += 1
            reason = "STOP" if current_price < entry else "TRAILING BE"
            city_name = LOCATIONS.get(mkt["city"], {}).get("name", mkt["city"])
            print(f"  [{reason}] {city_name} {mkt['date']} | entry ${entry:.3f} exit ${current_price:.3f} | PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")
            save_market(mkt)

    if closed:
        state["balance"] = round(balance, 2)
        save_state(state)

    write_simulation_export()
    return closed


def run_loop():
    global _cal
    _cal = load_cal()

    print(f"\n{'='*55}")
    print(f"  WEATHERBET — STARTING")
    print(f"{'='*55}")
    print(f"  Cities:     {len(LOCATIONS)}")
    print(f"  Balance:    ${BALANCE:,.0f} | Max bet: ${MAX_BET}")
    print(f"  Scan:       {SCAN_INTERVAL//60} min | Monitor: {MONITOR_INTERVAL//60} min")
    print(f"  Sources:    ECMWF + HRRR(US) + METAR(D+0)")
    print(f"  Data:       {DATA_DIR.resolve()}")
    print(f"  Ctrl+C to stop\n")

    last_full_scan = 0

    while True:
        now_ts  = time.time()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Full scan once per hour
        if now_ts - last_full_scan >= SCAN_INTERVAL:
            print(f"[{now_str}] full scan...")
            try:
                new_pos, closed, resolved, diag = scan_and_update()
                state = load_state()
                print(f"  balance: ${state['balance']:,.2f} | "
                      f"new: {new_pos} | closed: {closed} | resolved: {resolved}")
                print(f"  quotes: no_book={diag['skipped_no_book']} | "
                      f"bad_quote={diag['skipped_bad_quote']} | spread={diag['skipped_spread']}")
                last_full_scan = time.time()
            except KeyboardInterrupt:
                print(f"\n  Stopping — saving state...")
                save_state(load_state())
                write_simulation_export()
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
            # Quick stop monitoring
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
            write_simulation_export()
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
    elif cmd == "explain":
        _cal = load_cal()
        print_explain()
    elif cmd == "health":
        _cal = load_cal()
        print_health()
    else:
        print("Usage: python bot_v2.py [run|status|report|explain|health]")
