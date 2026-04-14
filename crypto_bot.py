#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
crypto_bot.py — Multi-coin Up/Down Polymarket Bot
===================================================
Trades "Coin Up or Down - 15 Minutes" markets on Polymarket for:
  BTC, ETH, SOL, XRP

Resolves Up if Chainlink price at market END >= price at START.

Usage:
    python crypto_bot.py --coin SOL              # live trading
    python crypto_bot.py --coin ETH --dry-run    # no orders placed
    python crypto_bot.py --coin XRP find         # show active markets
    python crypto_bot.py --coin BTC signals      # show current signals
    python crypto_bot.py --coin SOL status       # balance + open positions

Per-coin config in config.json under "coins": { "SOL": { ... }, "ETH": { ... } }
Shared credentials (polymarket_private_key, etc.) remain at the top level.
"""

import sys
import json
import math
import time
import signal
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
# PER-COIN DEFAULTS
# Adjust annual_vol priors to reflect each asset's typical realized volatility.
# These are starting points — the calibrator updates them from real outcomes.
# =============================================================================

COIN_DEFAULTS = {
    "BTC": {
        "slug_prefix":      "btc",
        "binance_symbol":   "BTCUSDT",
        "news_category":    "BTC",
        "annual_vol":       0.60,
        "min_ev":           0.06,
        "min_drift":        0.001,
        "kelly_fraction":   0.25,
        "max_bet":          25.0,
        "min_liquidity":    300.0,
    },
    "ETH": {
        "slug_prefix":      "eth",
        "binance_symbol":   "ETHUSDT",
        "news_category":    "ETH",
        "annual_vol":       0.80,   # ETH is more volatile than BTC
        "min_ev":           0.06,
        "min_drift":        0.001,
        "kelly_fraction":   0.25,
        "max_bet":          25.0,
        "min_liquidity":    300.0,
    },
    "SOL": {
        "slug_prefix":      "sol",
        "binance_symbol":   "SOLUSDT",
        "news_category":    "SOL",
        "annual_vol":       1.20,   # SOL is significantly more volatile
        "min_ev":           0.06,
        "min_drift":        0.002,  # needs a larger drift given higher vol
        "kelly_fraction":   0.20,   # slightly conservative for higher vol
        "max_bet":          25.0,
        "min_liquidity":    200.0,
    },
    "XRP": {
        "slug_prefix":      "xrp",
        "binance_symbol":   "XRPUSDT",
        "news_category":    "XRP",
        "annual_vol":       0.90,
        "min_ev":           0.06,
        "min_drift":        0.001,
        "kelly_fraction":   0.25,
        "max_bet":          25.0,
        "min_liquidity":    200.0,
    },
}

# =============================================================================
# PARSE CLI ARGS
# =============================================================================

def _parse_args():
    args      = sys.argv[1:]
    coin      = "BTC"
    dry_run   = False
    positional = []
    i = 0
    while i < len(args):
        if args[i] == "--coin" and i + 1 < len(args):
            coin = args[i + 1].upper()
            i += 2
        elif args[i] == "--dry-run":
            dry_run = True
            i += 1
        elif not args[i].startswith("--"):
            positional.append(args[i])
            i += 1
        else:
            i += 1
    cmd = positional[0] if positional else "run"
    return coin, cmd, dry_run

COIN, CMD, DRY_RUN = _parse_args()

if COIN not in COIN_DEFAULTS:
    print(f"Unknown coin '{COIN}'. Supported: {', '.join(COIN_DEFAULTS)}")
    sys.exit(1)

_defaults = COIN_DEFAULTS[COIN]

# =============================================================================
# CONFIG
# =============================================================================

with open("config.json", encoding="utf-8") as _f:
    _cfg = json.load(_f)

# Per-coin config overrides from config.json "coins" section
_coin_cfg = _cfg.get("coins", {}).get(COIN, {})

def _cv(key, default):
    """Read per-coin config with fallback to COIN_DEFAULTS."""
    return _coin_cfg.get(key, _defaults.get(key, default))

# Polymarket credentials (shared across all coins)
POLYMARKET_HOST     = "https://clob.polymarket.com"
POLY_PRIVATE_KEY    = _cfg.get("polymarket_private_key", "")
POLY_API_KEY        = _cfg.get("polymarket_api_key", "")
POLY_API_SECRET     = _cfg.get("polymarket_api_secret", "")
POLY_API_PASSPHRASE = _cfg.get("polymarket_api_passphrase", "")
POLY_FUNDER         = _cfg.get("polymarket_funder", "")
POLY_CHAIN_ID       = _cfg.get("chain_id", 137)
POLY_SIG_TYPE       = _cfg.get("signature_type", 0)

# Per-coin fixed params
SLUG_PREFIX    = _cv("slug_prefix",    _defaults["slug_prefix"])
BINANCE_SYMBOL = _cv("binance_symbol", _defaults["binance_symbol"])
NEWS_CATEGORY  = _cv("news_category",  _defaults["news_category"])
MAX_BET        = _cv("max_bet",        _defaults["max_bet"])
MIN_LIQUIDITY  = _cv("min_liquidity",  _defaults["min_liquidity"])
ENTRY_MIN_SEC  = _cfg.get("btc_entry_window_min_sec", 90)   # shared timing
ENTRY_MAX_SEC  = _cfg.get("btc_entry_window_max_sec", 660)
SCAN_INTERVAL  = 60
TUNE_LOOKBACK  = _cv("tune_lookback",  _cfg.get("btc_tune_lookback", 30))
PRIOR_WEIGHT   = _cv("prior_weight",   _cfg.get("btc_prior_weight", 10))
MONITOR_INTERVAL    = _cfg.get("monitor_interval", 15)
MAX_OPEN_POSITIONS  = _cv("max_open_positions", _cfg.get("max_open_positions", 2))
STOP_LOSS_PCT       = _cfg.get("stop_loss_pct", 0.80)
TAKE_PROFIT_SHORT= _cfg.get("take_profit_short", 0.85)
TAKE_PROFIT_LONG = _cfg.get("take_profit_long", 0.75)
TAKE_PROFIT_FINAL= _cfg.get("take_profit_final", 0.50)
TAKE_PROFIT_ROI  = _cfg.get("take_profit_roi", 0.40)
# Minimum allowed market price for either side before we consider the crowd's
# consensus too strong to bet against. E.g. 0.10 means skip if Up > 0.90.
MARKET_MIN_PRICE = _cfg.get("market_min_price", 0.10)

# Data dirs — one per coin
DATA_DIR         = Path(f"data_{COIN.lower()}")
MARKETS_DIR      = DATA_DIR / "markets"
STATE_FILE       = DATA_DIR / "state.json"
CALIBRATION_FILE = DATA_DIR / "calibration.json"
STRATEGY_FILE    = DATA_DIR / "strategy.json"
DATA_DIR.mkdir(exist_ok=True)
MARKETS_DIR.mkdir(exist_ok=True)

GAMMA_API   = "https://gamma-api.polymarket.com"
BINANCE_API = "https://api.binance.com/api/v3"

# ---------------------------------------------------------------------------
# Mutable strategy — persisted to strategy.json, updated by tune_strategy()
# ---------------------------------------------------------------------------
_strategy = {
    "annual_vol":     _cv("annual_vol",     _defaults["annual_vol"]),
    "kelly_fraction": _cv("kelly_fraction", _defaults["kelly_fraction"]),
    "min_ev":         _cv("min_ev",         _defaults["min_ev"]),
    "min_drift":      _cv("min_drift",      _defaults["min_drift"]),
}

_TUNE_BOUNDS = {
    "annual_vol":     (0.20, 3.00),
    "kelly_fraction": (0.05, 0.40),
    "min_ev":         (0.03, 0.25),
    "min_drift":      (0.0005, 0.015),
}
_TUNE_MAX_STEP = 0.15


def _load_strategy() -> None:
    if STRATEGY_FILE.exists():
        try:
            saved = json.loads(STRATEGY_FILE.read_text(encoding="utf-8"))
            for k in _strategy:
                if k in saved:
                    _strategy[k] = saved[k]
        except Exception:
            pass


_load_strategy()

# =============================================================================
# STATE
# =============================================================================

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"balance": _cfg.get("balance", 30.0), "total_bets": 0,
            "wins": 0, "losses": 0, "total_pnl": 0.0}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


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
    try:
        resp = get_clob_client().get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        return float(resp["balance"]) / 1e6
    except Exception as e:
        print(f"  [BALANCE] Error: {e}")
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


def place_buy_order(token_id: str, cost: float) -> dict | None:
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
    """
    client = get_clob_client()

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

    sell_price = price
    if market_id:
        try:
            r = requests.get(f"{GAMMA_API}/markets/{market_id}", timeout=(3, 5))
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
            token_id=token_id, price=round(sell_price, 4),
            size=actual_size, side=SELL,
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


# =============================================================================
# MATH
# =============================================================================

def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def calc_ev(p: float, price: float) -> float:
    if price <= 0 or price >= 1:
        return 0.0
    return round(p * (1.0 / price - 1.0) - (1.0 - p), 4)


def calc_kelly(p: float, price: float) -> float:
    if price <= 0 or price >= 1:
        return 0.0
    b = 1.0 / price - 1.0
    f = (p * b - (1.0 - p)) / b
    return round(min(max(0.0, f) * _strategy["kelly_fraction"], 1.0), 4)


def bet_size(kelly: float, balance: float) -> float:
    return round(min(kelly * balance, MAX_BET), 2)


# =============================================================================
# MARKET DISCOVERY
# =============================================================================

def _parse_json_field(raw, default):
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return default
    return raw if raw is not None else default


def _upcoming_slugs(count: int = 4) -> list[str]:
    """Construct upcoming slugs from 15-min boundary timestamps."""
    now_ts  = int(datetime.now(timezone.utc).timestamp())
    current = (now_ts // 900) * 900
    return [f"{SLUG_PREFIX}-updown-15m-{current + i * 900}" for i in range(count)]


def find_active_markets() -> list[dict]:
    now   = datetime.now(timezone.utc)
    slugs = _upcoming_slugs(4)
    result = []

    for slug in slugs:
        try:
            r = requests.get(f"{GAMMA_API}/events", params={"slug": slug}, timeout=10)
            r.raise_for_status()
            events = r.json()
        except Exception as e:
            print(f"  [GAMMA] Error fetching {slug}: {e}")
            continue

        if not events:
            continue

        ev = events[0]
        if not ev.get("active") or ev.get("closed"):
            continue

        markets = ev.get("markets") or []
        if not markets:
            continue
        mkt = markets[0]

        token_ids  = _parse_json_field(mkt.get("clobTokenIds"), [])
        prices_raw = _parse_json_field(mkt.get("outcomePrices"), ["0.5", "0.5"])

        if len(token_ids) < 2:
            continue
        try:
            up_price   = float(prices_raw[0])
            down_price = float(prices_raw[1])
        except Exception:
            continue
        if not (0.01 <= up_price <= 0.99 and 0.01 <= down_price <= 0.99):
            continue

        start_str = mkt.get("eventStartTime") or ev.get("startTime") or ""
        end_str   = mkt.get("endDate") or ev.get("endDate") or ""
        if not end_str:
            continue

        try:
            end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        except Exception:
            continue

        if start_str:
            try:
                start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            except Exception:
                start_dt = end_dt - timedelta(seconds=900)
        else:
            try:
                start_dt = datetime.fromtimestamp(int(slug.split("-")[-1]), tz=timezone.utc)
            except Exception:
                start_dt = end_dt - timedelta(seconds=900)

        seconds_to_end = (end_dt - now).total_seconds()
        if seconds_to_end < 0:
            continue

        result.append({
            "slug":           slug,
            "event_start":    start_dt.isoformat(),
            "event_end":      end_dt.isoformat(),
            "seconds_to_end": seconds_to_end,
            "up_token_id":    str(token_ids[0]),
            "down_token_id":  str(token_ids[1]),
            "up_price":       up_price,
            "down_price":     down_price,
            "liquidity":      float(mkt.get("liquidityNum") or ev.get("liquidity") or 0),
            "market_id":      str(mkt.get("id", "")),
        })

    return result


# =============================================================================
# PRICE DATA  (Binance public API — no auth)
# =============================================================================

def fetch_historical_open(epoch_sec: int) -> float | None:
    """Return the Binance 1-min open price at epoch_sec (closest market-start reference)."""
    try:
        r = requests.get(
            f"{BINANCE_API}/klines",
            params={"symbol": BINANCE_SYMBOL, "interval": "1m",
                    "startTime": epoch_sec * 1000,
                    "endTime":   (epoch_sec + 120) * 1000,
                    "limit": 1},
            timeout=5,
        )
        candles = r.json()
        if candles:
            return float(candles[0][1])   # index 1 = open price
    except Exception as e:
        print(f"  [BINANCE] Historical open fetch failed: {e}")
    return None


def fetch_price_state() -> dict:
    state = {"current_price": None, "candles": [], "bids": [], "asks": []}
    try:
        r = requests.get(f"{BINANCE_API}/ticker/price",
                         params={"symbol": BINANCE_SYMBOL}, timeout=5)
        state["current_price"] = float(r.json()["price"])
    except Exception as e:
        print(f"  [BINANCE] Price fetch failed: {e}")
        return state
    try:
        r = requests.get(f"{BINANCE_API}/klines",
                         params={"symbol": BINANCE_SYMBOL, "interval": "1m", "limit": 30},
                         timeout=5)
        state["candles"] = [
            [float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])]
            for c in r.json()
        ]
    except Exception as e:
        print(f"  [BINANCE] Candles fetch failed: {e}")
    try:
        r = requests.get(f"{BINANCE_API}/depth",
                         params={"symbol": BINANCE_SYMBOL, "limit": 20}, timeout=5)
        data = r.json()
        state["bids"] = [[float(b[0]), float(b[1])] for b in data.get("bids", [])]
        state["asks"] = [[float(a[0]), float(a[1])] for a in data.get("asks", [])]
    except Exception as e:
        print(f"  [BINANCE] Depth fetch failed: {e}")
    return state


def _compute_rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(0.0, delta))
        losses.append(max(0.0, -delta))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    return round(100.0 - 100.0 / (1.0 + avg_gain / avg_loss), 2)


def compute_technical_signals(price_state: dict) -> dict:
    candles = price_state["candles"]
    signals = {"momentum_1m": 0.0, "momentum_5m": 0.0, "rsi_14": 50.0, "ob_imbalance": 0.0}

    if len(candles) >= 2:
        closes = [c[3] for c in candles]
        if closes[-2]:
            signals["momentum_1m"] = (closes[-1] - closes[-2]) / closes[-2]
    if len(candles) >= 6:
        closes = [c[3] for c in candles]
        if closes[-6]:
            signals["momentum_5m"] = (closes[-1] - closes[-6]) / closes[-6]
    if len(candles) >= 15:
        signals["rsi_14"] = _compute_rsi([c[3] for c in candles])

    bids = price_state["bids"]
    asks = price_state["asks"]
    if bids and asks:
        bid_vol = sum(b[1] for b in bids)
        ask_vol = sum(a[1] for a in asks)
        total = bid_vol + ask_vol
        if total > 0:
            signals["ob_imbalance"] = (bid_vol - ask_vol) / total
    return signals


def estimate_p_up_technical(signals: dict) -> float:
    p = 0.50
    p += max(-0.08, min(0.08, signals["momentum_5m"] * 8.0))
    p += max(-0.07, min(0.07, (signals["rsi_14"] - 50.0) / 600.0))
    p += max(-0.05, min(0.05, signals["ob_imbalance"] * 0.05))
    return max(0.10, min(0.90, p))


# =============================================================================
# DRIFT / GBM PROBABILITY MODEL
# =============================================================================

def p_up_given_drift(drift: float, seconds_remaining: float) -> float:
    """
    GBM P(Up | drift, time_remaining).
    sigma_15min = annual_vol / sqrt(365 * 96)  — coin trades 24/7.
    Higher annual_vol (e.g. SOL at 1.2) means we need a larger drift
    to get high confidence — correctly making the model more selective.
    """
    if seconds_remaining <= 0:
        return 1.0 if drift >= 0 else 0.0
    sigma_15min     = _strategy["annual_vol"] / math.sqrt(365 * 96)
    sigma_remaining = sigma_15min * math.sqrt(seconds_remaining / 900.0)
    if sigma_remaining <= 0:
        return 1.0 if drift >= 0 else 0.0
    return norm_cdf(drift / sigma_remaining)


def blend_probabilities(p_drift: float, p_technical: float, drift_abs: float) -> float:
    if drift_abs > 0.003:
        return p_drift * 0.85 + p_technical * 0.15
    elif drift_abs > 0.001:
        return p_drift * 0.60 + p_technical * 0.40
    else:
        return p_drift * 0.30 + p_technical * 0.70


# =============================================================================
# NEWS / SENTIMENT
# =============================================================================

_news_cache: dict = {"ts": 0.0, "clear": True}
_NEWS_TTL = 300

_FUD_KEYWORDS  = {
    "hack", "hacked", "exploit", "fraud", "scam", "sec", "ban", "banned",
    "shutdown", "suspend", "suspended", "crash", "collapse", "liquidation",
}
_PUMP_KEYWORDS = {"etf approved", "etf approval", "all-time high"}


def check_news_clear() -> bool:
    now = time.time()
    if now - _news_cache["ts"] < _NEWS_TTL:
        return _news_cache["clear"]
    try:
        r = requests.get(
            "https://min-api.cryptocompare.com/data/v2/news/",
            params={"lang": "EN", "categories": NEWS_CATEGORY, "sortOrder": "latest"},
            timeout=8,
        )
        articles = r.json().get("Data", [])
        cutoff = now - 1800
        for article in articles:
            if (article.get("published_on") or 0) < cutoff:
                break
            text = ((article.get("title") or "") + " " + (article.get("body") or "")[:200]).lower()
            for kw in _PUMP_KEYWORDS:
                if kw in text:
                    print(f"  [NEWS] Bullish event ({kw!r}): {article.get('title', '')[:70]} — continuing")
            for kw in _FUD_KEYWORDS:
                if kw in text:
                    print(f"  [NEWS] FUD detected ({kw!r}): {article.get('title', '')[:70]}")
                    _news_cache.update({"ts": now, "clear": False})
                    return False
        _news_cache.update({"ts": now, "clear": True})
        return True
    except Exception as e:
        print(f"  [NEWS] Fetch failed ({e}) — assuming clear")
        _news_cache.update({"ts": now, "clear": True})
        return True


def get_fear_greed() -> tuple[int, str]:
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=5)
        d = r.json()["data"][0]
        return int(d["value"]), d["value_classification"]
    except Exception:
        return 50, "Neutral"


# =============================================================================
# MARKET FILE HELPERS
# =============================================================================

def _mkt_path(slug: str) -> Path:
    return MARKETS_DIR / f"{slug}.json"


def load_market(slug: str) -> dict | None:
    p = _mkt_path(slug)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def save_market(data: dict) -> None:
    _mkt_path(data["slug"]).write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_all_markets() -> list[dict]:
    result = []
    for f in sorted(MARKETS_DIR.glob("*.json")):
        try:
            result.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass
    return result


def list_open_slugs() -> set[str]:
    slugs = set()
    for f in MARKETS_DIR.glob("*.json"):
        try:
            m   = json.loads(f.read_text(encoding="utf-8"))
            pos = m.get("position")
            if pos and pos.get("status") == "open":
                slugs.add(m["slug"])
        except Exception:
            pass
    return slugs


# =============================================================================
# RESOLUTION
# =============================================================================

def check_resolved_markets(state: dict) -> dict:
    now = datetime.now(timezone.utc)
    for f in sorted(MARKETS_DIR.glob("*.json")):
        try:
            m = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        pos = m.get("position")
        if not pos or pos.get("status") != "open":
            continue
        try:
            end_dt = datetime.fromisoformat(m["event_end"].replace("Z", "+00:00"))
        except Exception:
            continue
        if (now - end_dt).total_seconds() < 60:
            continue

        try:
            r = requests.get(f"{GAMMA_API}/events", params={"slug": m["slug"]}, timeout=10)
            events = r.json()
            if not events:
                continue
            mkt_data     = (events[0].get("markets") or [{}])[0]
            prices_raw   = _parse_json_field(mkt_data.get("outcomePrices"), ["0.5", "0.5"])
            up_price_now = float(prices_raw[0])
        except Exception as e:
            print(f"  [RESOLVE] Error for {m['slug']}: {e}")
            continue

        if up_price_now >= 0.95:
            outcome = "Up"
        elif up_price_now <= 0.05:
            outcome = "Down"
        else:
            continue

        won = (pos["bet_side"] == outcome)
        pnl = round((1.0 - pos["entry_price"]) * pos["shares"], 4) if won else round(-pos["cost"], 4)

        end_price = m["snapshots"][-1].get("price") if m.get("snapshots") else None
        pos["status"]      = "resolved"
        pos["outcome"]     = outcome
        pos["pnl"]         = pnl
        m["coin_end_price"] = end_price
        save_market(m)

        state["total_bets"] += 1
        state["wins" if won else "losses"] += 1
        state["total_pnl"] = round(state["total_pnl"] + pnl, 4)

        tag = "WIN " if won else "LOSS"
        print(f"  [RESOLVED] {m['slug']} → {outcome} ({tag}) PnL={pnl:+.2f}")

    return state


# =============================================================================
# CALIBRATION & TUNING
# =============================================================================

def run_calibration(markets: list[dict]) -> None:
    """
    Bayesian update of realized annualized volatility from resolved markets.
    Uses actual coin start/end prices recorded in market files.
    """
    resolved = [
        m for m in markets
        if m.get("coin_start_price") and m.get("coin_end_price")
    ]
    if len(resolved) < 3:
        return

    realized_returns = []
    for m in resolved:
        start = m["coin_start_price"]
        end   = m["coin_end_price"]
        if start and end and start > 0:
            realized_returns.append(abs(end - start) / start)

    if not realized_returns:
        return

    mean_abs  = sum(realized_returns) / len(realized_returns)
    # E[|X|] = sigma * sqrt(2/pi) for N(0,sigma), so sigma = mean_abs * sqrt(pi/2)
    realized_annual = mean_abs * math.sqrt(math.pi / 2.0) * math.sqrt(365 * 96)

    prior_vol = _strategy["annual_vol"]
    n         = len(realized_returns)
    new_vol   = (PRIOR_WEIGHT * prior_vol + n * realized_annual) / (PRIOR_WEIGHT + n)
    new_vol   = round(max(_TUNE_BOUNDS["annual_vol"][0], min(_TUNE_BOUNDS["annual_vol"][1], new_vol)), 4)

    changed = abs(new_vol - prior_vol) > 0.005
    _strategy["annual_vol"] = new_vol

    cal = {}
    if CALIBRATION_FILE.exists():
        try:
            cal = json.loads(CALIBRATION_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    cal["annual_vol"] = {
        "value":        new_vol,
        "n":            n,
        "mean_15m_abs": round(mean_abs * 100, 4),
        "updated_at":   datetime.now(timezone.utc).isoformat(),
    }
    CALIBRATION_FILE.write_text(json.dumps(cal, indent=2), encoding="utf-8")
    if changed:
        print(f"  [CAL] {COIN} annual_vol: {prior_vol:.3f} → {new_vol:.3f}  (n={n}, mean_15m={mean_abs*100:.3f}%)")


def _position_won(pos: dict) -> bool:
    """Return True if the position was a win.
    For resolved markets, compare outcome to bet_side.
    For early-closed positions (outcome=None), treat pnl > 0 as a win."""
    if pos.get("outcome") is not None:
        return pos["outcome"] == pos["bet_side"]
    return (pos.get("pnl") or 0) > 0


def tune_strategy(markets: list[dict]) -> None:
    """
    Adjust strategy parameters from closed/resolved bets:
      1. kelly_fraction — based on actual vs predicted win rate
      2. min_ev         — find EV threshold band with best mean PnL/bet
      3. min_drift      — find drift threshold that predicts wins most reliably
    """
    resolved_bets = [
        m for m in markets
        if m.get("position")
           and m["position"].get("status") in ("resolved", "closed")
           and m["position"].get("pnl") is not None
    ]
    if len(resolved_bets) < TUNE_LOOKBACK:
        return

    recent = resolved_bets[-TUNE_LOOKBACK:]
    old    = dict(_strategy)

    # 1. Kelly vs win rate
    wins      = sum(1 for m in recent if _position_won(m["position"]))
    actual_wr = wins / len(recent)
    avg_p     = sum(m["position"].get("p_final", 0.5) for m in recent) / len(recent)
    kf        = _strategy["kelly_fraction"]
    if actual_wr > avg_p + 0.05:
        kf = min(kf * (1 + _TUNE_MAX_STEP), _TUNE_BOUNDS["kelly_fraction"][1])
    elif actual_wr < avg_p - 0.05:
        kf = max(kf * (1 - _TUNE_MAX_STEP), _TUNE_BOUNDS["kelly_fraction"][0])
    _strategy["kelly_fraction"] = round(kf, 4)

    # 2. min_ev — best-performing EV band
    # Build synthetic near-miss records from skipped_ev + resolved outcome so the
    # tuner can evaluate thresholds below the current min_ev (survivorship fix).
    skipped_resolved = []
    for m in markets:
        sk = m.get("skipped_ev")
        if not sk or m.get("position"):
            continue
        try:
            end_dt = datetime.fromisoformat(m["event_end"].replace("Z", "+00:00"))
        except Exception:
            continue
        if (datetime.now(timezone.utc) - end_dt).total_seconds() < 60:
            continue
        snapshots = m.get("snapshots", [])
        if not snapshots:
            continue
        end_price_snap = snapshots[-1].get("up_price")
        if end_price_snap is None:
            continue
        # Infer outcome from resolved market price
        if end_price_snap >= 0.95:
            outcome = "Up"
        elif end_price_snap <= 0.05:
            outcome = "Down"
        else:
            continue
        won  = (sk["side"] == outcome)
        pnl  = round((1.0 - sk["price"]) * (sk["ev"] * 1.0), 4) if won else round(-sk["ev"], 4)
        skipped_resolved.append({"ev": sk["ev"], "pnl": pnl})

    ev_thresholds = [0.03, 0.05, 0.07, 0.10, 0.15, 0.20]
    best_ev, best_ev_score = _strategy["min_ev"], -999.0
    for thresh in ev_thresholds:
        group = [m for m in recent if m["position"].get("ev", 0) >= thresh]
        # Augment with near-miss records that fall in this threshold band
        shadow = [s for s in skipped_resolved if s["ev"] >= thresh]
        combined = [m["position"]["pnl"] for m in group] + [s["pnl"] for s in shadow]
        if len(combined) >= 5:
            mean_pnl = sum(combined) / len(combined)
            if mean_pnl > best_ev_score:
                best_ev_score = mean_pnl
                best_ev       = thresh
    current = _strategy["min_ev"]
    delta   = best_ev - current
    capped  = max(-_TUNE_MAX_STEP * current, min(_TUNE_MAX_STEP * current, delta))
    _strategy["min_ev"] = round(
        max(_TUNE_BOUNDS["min_ev"][0], min(_TUNE_BOUNDS["min_ev"][1], current + capped)), 4)

    # 3. min_drift — best-performing drift band at entry
    drift_thresholds = [0.0005, 0.001, 0.002, 0.003, 0.005, 0.008]
    best_drift, best_drift_score = _strategy["min_drift"], -999.0
    for thresh in drift_thresholds:
        group = [m for m in recent if abs(m["position"].get("drift_at_entry", 0)) >= thresh]
        if len(group) >= 5:
            mean_pnl = sum(m["position"]["pnl"] for m in group) / len(group)
            if mean_pnl > best_drift_score:
                best_drift_score = mean_pnl
                best_drift       = thresh
    current = _strategy["min_drift"]
    delta   = best_drift - current
    capped  = max(-_TUNE_MAX_STEP * current, min(_TUNE_MAX_STEP * current, delta))
    _strategy["min_drift"] = round(
        max(_TUNE_BOUNDS["min_drift"][0], min(_TUNE_BOUNDS["min_drift"][1], current + capped)), 6)

    changes = [f"{k}: {old[k]:.4f}→{_strategy[k]:.4f}"
               for k in ("annual_vol", "kelly_fraction", "min_ev", "min_drift")
               if abs(_strategy[k] - old[k]) > 0.0001]
    if changes:
        print(f"  [TUNE] {COIN}: {', '.join(changes)}")
        STRATEGY_FILE.write_text(json.dumps(_strategy, indent=2), encoding="utf-8")


# =============================================================================
# SCAN CYCLE
# =============================================================================

def run_scan(state: dict, dry_run: bool = False, paper_mode: bool = False) -> dict:
    now = datetime.now(timezone.utc)

    real_bal = get_real_balance()
    if real_bal is not None:
        state["balance"] = real_bal
    balance = state["balance"]

    if dry_run:
        mode_tag = "[DRY-RUN]"
    elif paper_mode:
        mode_tag = "[PAPER]"
    else:
        mode_tag = ""

    print(f"\n{mode_tag + ' ' if mode_tag else ''}[{COIN}] {now.strftime('%Y-%m-%d %H:%M:%S UTC')}  balance=${balance:.2f}")

    markets = find_active_markets()
    if not markets:
        print(f"  No active {COIN} 15-min markets found.")
        return state

    price_state = fetch_price_state()
    if not price_state["current_price"]:
        print("  Could not fetch price — skipping cycle.")
        return state

    current_price = price_state["current_price"]
    signals       = compute_technical_signals(price_state)
    p_technical   = estimate_p_up_technical(signals)
    print(
        f"  {COIN}: ${current_price:,.4f} | "
        f"RSI={signals['rsi_14']:.1f} | "
        f"mom5m={signals['momentum_5m']*100:+.3f}% | "
        f"OB={signals['ob_imbalance']:+.3f} | "
        f"p_tech={p_technical:.3f}"
    )

    news_clear = check_news_clear()
    if not news_clear:
        print(f"  [NEWS] Breaking {COIN} news detected — no bets this cycle.")

    fg_val, fg_class = get_fear_greed()
    fg_adj = 0.02 if fg_val < 10 else (-0.02 if fg_val > 90 else 0.0)
    if fg_adj:
        print(f"  F&G={fg_val} ({fg_class}) → prior adj={fg_adj:+.2f}")

    open_slugs = list_open_slugs()
    new_this_cycle = 0

    for mkt in markets:
        slug           = mkt["slug"]
        seconds_to_end = mkt["seconds_to_end"]

        mdata = load_market(slug)
        if mdata is None:
            mdata = {
                "slug":             slug,
                "coin":             COIN,
                "event_start":      mkt["event_start"],
                "event_end":        mkt["event_end"],
                "up_token_id":      mkt["up_token_id"],
                "down_token_id":    mkt["down_token_id"],
                "coin_start_price": None,
                "coin_end_price":   None,
                "position":         None,
                "snapshots":        [],
            }

        try:
            start_dt = datetime.fromisoformat(mkt["event_start"].replace("Z", "+00:00"))
        except Exception:
            start_dt = now

        if mdata["coin_start_price"] is None and now >= start_dt:
            try:
                start_epoch = int(slug.split("-")[-1])
                hist = fetch_historical_open(start_epoch)
            except Exception:
                hist = None
            mdata["coin_start_price"] = hist if hist else current_price
            src = "historical" if hist else "current"
            print(f"  [START] {slug} — {COIN} start: ${mdata['coin_start_price']:,.4f} ({src})")

        drift = 0.0
        if mdata["coin_start_price"]:
            drift = (current_price - mdata["coin_start_price"]) / mdata["coin_start_price"]

        mdata["snapshots"].append({
            "ts":         now.isoformat(),
            "price":      current_price,
            "drift":      round(drift, 6),
            "up_price":   mkt["up_price"],
            "down_price": mkt["down_price"],
        })
        save_market(mdata)

        # --- RECONCILE: detect on-chain positions not tracked locally ---
        pos = mdata.get("position")
        need_reconcile = pos is None or pos.get("status") != "open"
        if need_reconcile:
            token_ids_to_check = []
            if pos is not None and pos.get("token_id"):
                token_ids_to_check = [(pos["token_id"], pos.get("bet_side", "?"))]
            else:
                token_ids_to_check = [
                    (mkt["up_token_id"], "Up"),
                    (mkt["down_token_id"], "Down"),
                ]
            for tid, side in token_ids_to_check:
                if not tid:
                    continue
                onchain = get_token_balance(tid)
                if onchain >= 1.0:
                    real_entry = get_real_entry_price(tid)
                    cp = mkt["up_price"] if side == "Up" else mkt["down_price"]
                    entry = real_entry if real_entry is not None else cp
                    print(f"  [RECONCILE] {slug} — {onchain:.1f} orphaned shares ({side}) "
                          f"entry ${entry:.4f}, market ${cp:.3f}")
                    mdata["position"] = {
                        "bet_side":     side,
                        "token_id":     tid,
                        "market_id":    mkt["market_id"],
                        "entry_price":  entry,
                        "shares":       round(onchain, 2),
                        "cost":         round(onchain * entry, 2),
                        "status":       "open",
                        "outcome":      None,
                        "pnl":          None,
                        "reconciled":   True,
                    }
                    save_market(mdata)
                    break
                time.sleep(0.05)

        # --- STOP-LOSS, TRAILING STOP, AND TAKE-PROFIT for open positions ---
        if mdata.get("position") and mdata["position"].get("status") == "open":
            pos = mdata["position"]
            tid = pos.get("token_id") or (mkt["up_token_id"] if pos["bet_side"] == "Up" else mkt["down_token_id"])
            cp = mkt["up_price"] if pos["bet_side"] == "Up" else mkt["down_price"]

            if cp is not None:
                entry = pos["entry_price"]
                stop  = pos.get("stop_price", entry * STOP_LOSS_PCT)

                if cp >= entry * 1.20 and stop < entry:
                    pos["stop_price"] = entry
                    pos["trailing_activated"] = True

                minutes_left = seconds_to_end / 60.0
                if minutes_left < 5:
                    take_profit = TAKE_PROFIT_FINAL
                elif minutes_left < 8:
                    take_profit = TAKE_PROFIT_SHORT
                else:
                    take_profit = TAKE_PROFIT_LONG

                roi_threshold = entry * (1.0 + TAKE_PROFIT_ROI)
                take_triggered = cp >= max(take_profit, entry) or cp >= roi_threshold
                stop_triggered = cp <= stop

                if take_triggered or stop_triggered:
                    mid = pos.get("market_id", mkt["market_id"])
                    # Use actual on-chain balance to correct for partial fills at entry
                    actual_shares = get_token_balance(tid)
                    if actual_shares >= 1.0:
                        pos["shares"] = round(actual_shares, 2)
                    resp = place_sell_order(tid, pos["shares"], cp, market_id=mid)
                    if resp is not None:
                        pnl = round((cp - entry) * pos["shares"], 2)
                        if cp >= roi_threshold and cp < take_profit:
                            pos["close_reason"] = "take_profit_roi"
                            reason = "TAKE ROI"
                        elif take_triggered:
                            pos["close_reason"] = "take_profit"
                            reason = "TAKE"
                        elif cp < entry:
                            pos["close_reason"] = "stop_loss"
                            reason = "STOP"
                        else:
                            pos["close_reason"] = "trailing_stop"
                            reason = "TRAILING BE"
                        pos["exit_price"] = cp
                        pos["pnl"]        = pnl
                        pos["status"]     = "closed"
                        pos["closed_at"]  = now.isoformat()
                        save_market(mdata)
                        state["total_bets"] += 1
                        if pnl >= 0:
                            state["wins"] += 1
                        else:
                            state["losses"] += 1
                        state["total_pnl"] = round(state["total_pnl"] + pnl, 4)
                        print(f"  [{reason}] {slug} | entry ${entry:.3f} exit ${cp:.3f} | "
                              f"{minutes_left:.0f}m left | PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")
                    else:
                        print(f"  [SELL FAIL] {slug} — will retry next cycle")
            continue

        # --- Skip conditions for new entries ---
        if len(open_slugs) + new_this_cycle >= MAX_OPEN_POSITIONS:
            print(f"  [SKIP] {slug} — max open positions ({MAX_OPEN_POSITIONS}) reached")
            continue
        if not (ENTRY_MIN_SEC <= seconds_to_end <= ENTRY_MAX_SEC):
            continue
        if mdata["coin_start_price"] is None:
            print(f"  [SKIP] {slug} — start price not yet recorded")
            continue
        if not news_clear:
            continue
        if mkt["liquidity"] < MIN_LIQUIDITY:
            print(f"  [SKIP] {slug} — liquidity ${mkt['liquidity']:.0f} < ${MIN_LIQUIDITY:.0f}")
            continue

        # --- Skip if market is already near-terminal (crowd consensus > 90%) ---
        # When Up is priced at 0.95, the crowd has watched the full trajectory and
        # is almost certainly right. Our GBM model would generate huge fake EV on
        # the losing side — filter it out to avoid value traps.
        if not (MARKET_MIN_PRICE <= mkt["up_price"] <= 1.0 - MARKET_MIN_PRICE):
            print(f"  [SKIP] {slug} — market one-sided (Up={mkt['up_price']:.3f}), crowd consensus too strong")
            continue

        # --- Probability estimation ---
        p_drift   = p_up_given_drift(drift, seconds_to_end)
        p_blended = blend_probabilities(p_drift, p_technical, abs(drift))
        p_blended = max(0.10, min(0.90, p_blended + fg_adj))

        up_price   = mkt["up_price"]
        down_price = mkt["down_price"]
        ev_up      = calc_ev(p_blended,       up_price)
        ev_down    = calc_ev(1.0 - p_blended, down_price)

        if abs(drift) < _strategy["min_drift"]:
            print(
                f"  [SKIP] {slug} | drift={drift*100:+.3f}% < "
                f"min_drift={_strategy['min_drift']*100:.3f}% — insufficient drift"
            )
            continue

        min_ev = _strategy["min_ev"]
        model_favours_up = p_blended >= 0.5
        if ev_up >= min_ev and model_favours_up:
            bet_side, token_id = "Up",   mkt["up_token_id"]
            entry_price, p_win, ev = up_price,   p_blended,       ev_up
        elif ev_down >= min_ev and not model_favours_up:
            bet_side, token_id = "Down", mkt["down_token_id"]
            entry_price, p_win, ev = down_price, 1.0 - p_blended, ev_down
        else:
            # Determine why we're skipping for the log
            if model_favours_up and ev_up < min_ev:
                reason = f"EV_up={ev_up:.3f} < min_ev — no edge"
            elif not model_favours_up and ev_down < min_ev:
                reason = f"EV_down={ev_down:.3f} < min_ev — no edge"
            else:
                reason = f"model/EV mismatch (p_up={p_blended:.3f}, EV_up={ev_up:.3f}, EV_down={ev_down:.3f})"
            print(f"  [SKIP] {slug} | drift={drift*100:+.3f}% | {reason}")
            # Record near-miss so the EV tuner can evaluate lower thresholds
            best_ev_side = "Up" if model_favours_up else "Down"
            best_ev_val  = ev_up if model_favours_up else ev_down
            mdata["skipped_ev"] = {
                "ev":       round(best_ev_val, 4),
                "ev_up":    round(ev_up, 4),
                "ev_down":  round(ev_down, 4),
                "p":        round(p_blended, 4),
                "side":     best_ev_side,
                "price":    up_price if best_ev_side == "Up" else down_price,
                "drift":    round(drift, 6),
            }
            save_market(mdata)
            continue

        kelly      = calc_kelly(p_win, entry_price)
        if kelly <= 0:
            continue
        cost       = bet_size(kelly, balance)
        if cost < 1.0:
            print(f"  [SKIP] {slug} — bet ${cost:.2f} < $1 minimum")
            continue
        shares_est = round(cost / entry_price, 2)

        print(
            f"  [BET] {slug}\n"
            f"        Side={bet_side} | Price={entry_price:.3f} | "
            f"Drift={drift*100:+.3f}% | {seconds_to_end:.0f}s left\n"
            f"        p_drift={p_drift:.3f}  p_tech={p_technical:.3f}  p_final={p_blended:.3f}\n"
            f"        EV={ev:.3f} | Kelly={kelly:.4f} | Cost=${cost:.2f} | Shares≈{shares_est}"
        )

        if dry_run:
            print("        [DRY-RUN] Order NOT placed.")
            continue

        existing = get_token_balance(token_id)
        if existing >= 1.0:
            print(f"  [SKIP] {slug} — already hold {existing:.1f} shares on-chain")
            continue

        position = {
            "bet_side":              bet_side,
            "token_id":             token_id,
            "market_id":            mkt["market_id"],
            "entry_price":           entry_price,
            "shares":                shares_est,
            "cost":                  cost,
            "up_price_at_entry":     up_price,
            "down_price_at_entry":   down_price,
            "p_final":               round(p_blended, 4),
            "p_drift":               round(p_drift, 4),
            "p_technical":           round(p_technical, 4),
            "drift_at_entry":        round(drift, 6),
            "momentum_5m_at_entry":  round(signals["momentum_5m"], 6),
            "rsi_at_entry":          round(signals["rsi_14"], 1),
            "ob_imbalance_at_entry": round(signals["ob_imbalance"], 4),
            "fear_greed":            fg_val,
            "ev":                    ev,
            "kelly":                 kelly,
            "paper":                 paper_mode,
            "status":                "open",
            "outcome":               None,
            "pnl":                   None,
        }

        if paper_mode:
            position["order_id"] = "paper"
            mdata["position"]    = position
            save_market(mdata)
            print("        [PAPER] Phantom bet recorded (no real order).")
            continue

        resp = place_buy_order(token_id, cost)
        if not resp:
            print("        [FAILED] Order not filled.")
            continue

        position["order_id"] = resp.get("orderID") or resp.get("id", "")
        mdata["position"]    = position
        save_market(mdata)
        new_this_cycle += 1
        print(f"        [OK] OrderID={position['order_id']}")

    return state


# =============================================================================
# MONITOR POSITIONS  (between full scans)
# =============================================================================

def monitor_positions() -> int:
    """Quick stop-loss / take-profit check on open positions without full scan."""
    now = datetime.now(timezone.utc)
    markets = load_all_markets()
    open_pos = [m for m in markets if m.get("position") and m["position"].get("status") == "open"]

    closed_mkts = [m for m in markets if m.get("position") and m["position"].get("status") == "closed"
                   and m.get("position", {}).get("outcome") is None]
    for mkt in closed_mkts:
        pos = mkt["position"]
        tid = pos.get("token_id")
        if not tid:
            continue
        onchain = get_token_balance(tid)
        if onchain >= 1.0:
            try:
                r = requests.get(f"{GAMMA_API}/markets/{pos.get('market_id', '')}", timeout=(3, 5))
                mdata = r.json()
                prices_raw = _parse_json_field(mdata.get("outcomePrices"), ["0.5", "0.5"])
                cp = float(prices_raw[0]) if pos.get("bet_side") == "Up" else float(prices_raw[1])
            except Exception:
                cp = pos.get("exit_price", pos["entry_price"])
            real_entry = get_real_entry_price(tid)
            entry = real_entry if real_entry is not None else cp
            print(f"  [RECONCILE] {mkt['slug']} — {onchain:.1f} orphaned shares "
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
        mid = pos.get("market_id", "")
        mutated = False

        current_price = None
        try:
            r = requests.get(f"{GAMMA_API}/markets/{mid}", timeout=(3, 5))
            mdata = r.json()
            best_bid = mdata.get("bestBid")
            if best_bid is not None:
                current_price = float(best_bid)
        except Exception:
            pass

        if current_price is None:
            continue

        entry = pos["entry_price"]
        stop  = pos.get("stop_price", entry * STOP_LOSS_PCT)

        try:
            end_dt = datetime.fromisoformat(mkt["event_end"].replace("Z", "+00:00"))
            seconds_left = max(0, (end_dt - now).total_seconds())
        except Exception:
            seconds_left = 900.0
        minutes_left = seconds_left / 60.0

        if current_price >= entry * 1.20 and stop < entry:
            pos["stop_price"] = entry
            pos["trailing_activated"] = True
            mutated = True
            print(f"  [TRAILING] {mkt['slug']} — stop moved to breakeven ${entry:.3f}")

        if minutes_left < 5:
            take_profit = TAKE_PROFIT_FINAL
        elif minutes_left < 8:
            take_profit = TAKE_PROFIT_SHORT
        else:
            take_profit = TAKE_PROFIT_LONG

        roi_threshold = entry * (1.0 + TAKE_PROFIT_ROI)
        take_triggered = current_price >= max(take_profit, entry) or current_price >= roi_threshold
        stop_triggered = current_price <= stop

        if take_triggered or stop_triggered:
            tid = pos.get("token_id")
            if not tid:
                tid = mkt.get("up_token_id") if pos["bet_side"] == "Up" else mkt.get("down_token_id")
            # Use actual on-chain balance to correct for partial fills at entry
            actual_shares = get_token_balance(tid)
            if actual_shares >= 1.0:
                pos["shares"] = round(actual_shares, 2)
            resp = place_sell_order(tid, pos["shares"], current_price, market_id=mid)
            if resp is not None:
                pnl = round((current_price - entry) * pos["shares"], 2)
                balance += pos["cost"] + pnl
                pos["closed_at"] = now.isoformat()
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
                pos["exit_price"] = current_price
                pos["pnl"]        = pnl
                pos["status"]     = "closed"
                state["total_bets"] += 1
                if pnl >= 0:
                    state["wins"] += 1
                else:
                    state["losses"] += 1
                state["total_pnl"] = round(state["total_pnl"] + pnl, 4)
                closed += 1
                mutated = True
                print(f"  [{reason}] {mkt['slug']} | entry ${entry:.3f} exit ${current_price:.3f} | "
                      f"{minutes_left:.0f}m left | PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")
            else:
                print(f"  [SELL FAIL] {mkt['slug']} — will retry next cycle")

        if mutated:
            save_market(mkt)

    if closed:
        real_bal = get_real_balance()
        state["balance"] = round(real_bal if real_bal is not None else balance, 2)
        save_state(state)

    return closed


# =============================================================================
# CLI COMMANDS
# =============================================================================

def cmd_find() -> None:
    price_state = fetch_price_state()
    price       = price_state["current_price"]
    mkts        = find_active_markets()
    print(f"\n{COIN}: ${price:,.4f}")
    print(f"Active 15-min markets: {len(mkts)}\n")
    for m in mkts:
        mdata     = load_market(m["slug"]) or {}
        start_p   = mdata.get("coin_start_price")
        drift_str = ""
        if start_p and price:
            drift     = (price - start_p) / start_p * 100
            p_d       = p_up_given_drift(drift / 100, m["seconds_to_end"])
            drift_str = f"  drift={drift:+.3f}%  p_drift={p_d:.3f}"
        in_window  = ENTRY_MIN_SEC <= m["seconds_to_end"] <= ENTRY_MAX_SEC
        window_str = " [IN WINDOW]" if in_window else ""
        print(
            f"  {m['slug']}\n"
            f"    Up={m['up_price']:.3f}  Down={m['down_price']:.3f}  "
            f"liq=${m['liquidity']:.0f}  {m['seconds_to_end']:.0f}s left"
            f"{drift_str}{window_str}"
        )


def cmd_signals() -> None:
    price_state = fetch_price_state()
    sigs        = compute_technical_signals(price_state)
    p_up        = estimate_p_up_technical(sigs)
    fg_val, fg_class = get_fear_greed()
    clear       = check_news_clear()
    print(f"\n{COIN}: ${price_state['current_price']:,.4f}")
    print(f"  momentum_1m  = {sigs['momentum_1m']*100:+.4f}%")
    print(f"  momentum_5m  = {sigs['momentum_5m']*100:+.4f}%")
    print(f"  rsi_14       = {sigs['rsi_14']:.2f}")
    print(f"  ob_imbalance = {sigs['ob_imbalance']:+.4f}")
    print(f"  P(Up) tech   = {p_up:.4f}")
    print(f"  Fear & Greed = {fg_val} ({fg_class})")
    print(f"  News clear   = {clear}")


def cmd_status(state: dict) -> None:
    real_bal = get_real_balance()
    print(f"\n=== {COIN} Bot Status ===")
    if real_bal is not None:
        print(f"Balance (wallet): ${real_bal:.2f}")
    else:
        print(f"Balance (wallet): unavailable (last known: ${state['balance']:.2f})")
    print(f"Total bets: {state['total_bets']}  W={state['wins']}  L={state['losses']}")
    wr = state["wins"] / state["total_bets"] * 100 if state["total_bets"] else 0
    print(f"Win rate:   {wr:.1f}%")
    print(f"Total PnL:  ${state['total_pnl']:+.2f}")
    print(f"\nLearned strategy:")
    for k, v in _strategy.items():
        print(f"  {k:<18} = {v:.4f}")

    open_pos = []
    for f in sorted(MARKETS_DIR.glob("*.json")):
        try:
            m   = json.loads(f.read_text(encoding="utf-8"))
            pos = m.get("position")
            if pos and pos.get("status") == "open":
                open_pos.append((m["slug"], pos))
        except Exception:
            pass
    print(f"\nOpen positions: {len(open_pos)}")
    for slug, pos in open_pos:
        paper = " [PAPER]" if pos.get("paper") else ""
        print(
            f"  {slug}{paper}\n"
            f"    {pos['bet_side']} @ {pos['entry_price']:.3f} | "
            f"cost=${pos['cost']:.2f} | EV={pos['ev']:.3f} | "
            f"drift_entry={pos['drift_at_entry']*100:+.3f}%"
        )


# =============================================================================
# MAIN LOOP
# =============================================================================

def run_loop() -> None:
    state     = load_state()
    cycle_num = 0

    _stop = {"flag": False}

    def _shutdown(signum, frame):
        print(f"\n[{COIN}] Signal {signum} — saving state and exiting.")
        save_state(state)
        _stop["flag"] = True

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT,  _shutdown)

    MIN_TRADEABLE = 1.0
    print(
        f"{COIN} Bot starting.  DRY_RUN={DRY_RUN}\n"
        f"  data dir: {DATA_DIR}\n"
        f"  min_ev={_strategy['min_ev']}  min_drift={_strategy['min_drift']*100:.3f}%  "
        f"Kelly={_strategy['kelly_fraction']}  annual_vol={_strategy['annual_vol']:.2f}\n"
        f"  Entry window: [{ENTRY_MIN_SEC}s, {ENTRY_MAX_SEC}s] before close | "
        f"max_bet=${MAX_BET}\n"
        f"  Scan: {SCAN_INTERVAL}s | Monitor: {MONITOR_INTERVAL}s | "
        f"SL={STOP_LOSS_PCT} | TP_final={TAKE_PROFIT_FINAL} | TP_ROI={TAKE_PROFIT_ROI}"
    )

    real_bal = get_real_balance()
    if real_bal is None:
        print(f"  WARNING: Could not fetch on-chain balance — check your keys in config.json")
    else:
        print(f"  Wallet USDC: ${real_bal:,.2f}")
    print(f"  Ctrl+C to stop\n")

    last_full_scan = 0

    while not _stop["flag"]:
        now_ts  = time.time()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if now_ts - last_full_scan >= SCAN_INTERVAL:
            print(f"\n[{now_str}] [{COIN}] full scan...")
            try:
                state = check_resolved_markets(state)

                balance    = state["balance"]
                paper_mode = (not DRY_RUN) and (balance < MIN_TRADEABLE)
                if paper_mode:
                    print(f"  [{COIN} PAPER MODE] Wallet ${balance:.2f} < ${MIN_TRADEABLE:.2f} — phantom bets only")

                state = run_scan(state, dry_run=DRY_RUN, paper_mode=paper_mode)
                save_state(state)

                cycle_num += 1
                if cycle_num % 10 == 0:
                    all_markets = load_all_markets()
                    run_calibration(all_markets)
                    tune_strategy(all_markets)

                last_full_scan = time.time()

            except KeyboardInterrupt:
                _shutdown(signal.SIGINT, None)
            except requests.exceptions.ConnectionError:
                print(f"  Connection lost — waiting 60 sec")
                time.sleep(60)
                continue
            except Exception as e:
                print(f"[{COIN} LOOP ERROR] {e}")
        else:
            print(f"[{now_str}] [{COIN}] monitoring positions...")
            try:
                stopped = monitor_positions()
                if stopped:
                    state = load_state()
                    print(f"  balance: ${state['balance']:,.2f}")
            except Exception as e:
                print(f"  Monitor error: {e}")

        if not _stop["flag"]:
            try:
                time.sleep(MONITOR_INTERVAL)
            except KeyboardInterrupt:
                _shutdown(signal.SIGINT, None)


# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    if CMD == "find":
        cmd_find()
    elif CMD == "signals":
        cmd_signals()
    elif CMD == "status":
        cmd_status(load_state())
    else:
        run_loop()
