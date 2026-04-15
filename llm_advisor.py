#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
llm_advisor.py — LLM cycle advisor via OpenRouter
==================================================
Provides one structured assessment per scan cycle:
  - Market regime classification
  - News sentiment with directional score
  - Per-market action (bet_up / bet_down / skip) with p_up_adj and kelly_multiplier
  - Optional cycle veto for dangerous conditions

All failures return NEUTRAL_ASSESSMENT so the quantitative model runs unchanged.
"""

import json
import time
import math
import requests
from datetime import datetime, timezone
from typing import TypedDict, Optional

# ---------------------------------------------------------------------------
# Output types
# ---------------------------------------------------------------------------

class MarketDecision(TypedDict):
    action: str             # "bet_up" | "bet_down" | "skip"
    p_up_adj: float         # clamped to [-0.12, 0.12]
    kelly_multiplier: float # clamped to [0.5, 1.5]
    confidence: str         # "high" | "medium" | "low"
    skip_reason: Optional[str]
    reasoning: str

class CycleAssessment(TypedDict):
    regime: str             # "trending_up"|"trending_down"|"choppy"|"volatile"|"uncertain"
    regime_confidence: str  # "high"|"medium"|"low"
    news_sentiment: str     # "strongly_bullish"|"bullish"|"neutral"|"bearish"|"strongly_bearish"
    news_sentiment_score: float  # -0.15 to 0.15
    cycle_veto: bool
    cycle_veto_reason: Optional[str]
    markets: dict           # slug -> MarketDecision
    overall_reasoning: str

NEUTRAL_ASSESSMENT: CycleAssessment = {
    "regime": "uncertain",
    "regime_confidence": "low",
    "news_sentiment": "neutral",
    "news_sentiment_score": 0.0,
    "cycle_veto": False,
    "cycle_veto_reason": None,
    "markets": {},
    "overall_reasoning": "LLM unavailable — quantitative model only.",
}

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a trading signal advisor for Polymarket 1-hour binary outcome prediction markets.

HOW THESE MARKETS WORK:
Each market resolves "Up" if the coin price at market END >= price at market START,
measured by the Chainlink oracle at exact 1-hour boundaries.
You are not predicting the absolute price — only whether it will be HIGHER or LOWER
than it was when this specific 1-hour window opened.

YOUR ROLE:
A GBM-based quantitative model already handles drift, volatility, EV, and Kelly sizing.
You provide a qualitative overlay:
1. Classify the current market regime from technical signals + crowd behavior
2. Interpret news for its directional impact over the NEXT 1 HOUR ONLY
3. Read the Polymarket crowd signals: price trajectory, volume, spread, one_hour_price_change
4. Recommend per-market actions (bet_up / bet_down / skip) with probability adjustments
5. Optionally veto the entire cycle for genuinely dangerous conditions

POLYMARKET CROWD SIGNALS — HOW TO READ THEM:
- up_price: current crowd consensus probability that coin ends above start price
- one_hour_price_chg: how much the crowd shifted in the last hour (e.g. +0.15 = crowd moved 15% toward Up)
- crowd_momentum_1h: (up_price_now - up_price_60min_ago) — is the crowd accelerating in one direction?
- crowd_accelerating: whether the most recent crowd moves are speeding up
- price_trajectory: sampled history of the Up token price on Polymarket (crowd belief over time)
- volume_24hr: how much USDC traded today — higher = more efficient market, harder to beat
- spread: bid-ask spread — 0.01 is normal; >0.05 means thin liquidity, recommend skip
- competitive: market efficiency score (0-1) — near 1.0 = very efficient, crowd is well-informed
- open_interest: USDC currently committed to open positions

SIGNAL INTERPRETATION GUIDE:
- Crowd and quant model AGREE (same direction, high momentum): increase kelly_multiplier to 1.4-1.5
- Crowd CONTRADICTS quant model: trust the quant drift, use kelly_multiplier 0.9, small p_up_adj only
- Tight spread (<=0.01) + competitive >0.95: crowd is well-informed, weight their direction
- crowd_momentum_1h > 0.05 in the same direction as drift: strong confirming signal (+0.06 to +0.12 p_up_adj)
- one_hour_price_chg > 0.10 aligned with drift: push p_up_adj to the upper range
- volume_24hr < 100 USDC: thin market, skip
- spread > 0.05: skip — poor execution quality
- When drift and RSI and crowd all align: maximum confidence, kelly_multiplier 1.5, p_up_adj at maximum

CRITICAL CONSTRAINTS:
- Only the last 30-60 minutes matter for 1-hour direction. Macro narratives are irrelevant unless driving momentum RIGHT NOW.
- p_up_adj range: -0.12 to +0.12. Use the full range when signals are strong — don't be timid.
- kelly_multiplier: 1.0 = unchanged, 1.5 = max. DEFAULT to 1.2 when you have any directional conviction. Only go below 1.0 when signals clearly conflict.
- BIAS TOWARD BETTING: when signals are ambiguous, prefer action over skip. Missing a good bet is as costly as a bad one at this scale.
- If has_open_position is true for a market, always set action to "skip".
- cycle_veto ONLY for: confirmed exchange halt, Chainlink oracle failure, active flash crash in progress. Do NOT veto for news uncertainty, low confidence, or normal volatility.
- minutes_to_end < 10: skip — too late to enter, market is nearly resolved.
- Do NOT apply capital-preservation penalties based on recent losses. The quantitative model already adjusts for this. Your job is to find the best bet, not to be cautious.

Output ONLY valid JSON matching the schema below. No markdown fences. No text outside the JSON.

REQUIRED OUTPUT SCHEMA:
{
  "regime": "<trending_up|trending_down|choppy|volatile|uncertain>",
  "regime_confidence": "<high|medium|low>",
  "news_sentiment": "<strongly_bullish|bullish|neutral|bearish|strongly_bearish>",
  "news_sentiment_score": <float -0.15 to 0.15>,
  "cycle_veto": <true|false>,
  "cycle_veto_reason": <null or string>,
  "markets": {
    "<slug>": {
      "action": "<bet_up|bet_down|skip>",
      "p_up_adj": <float -0.12 to 0.12>,
      "kelly_multiplier": <float 0.5 to 1.5>,
      "confidence": "<high|medium|low>",
      "skip_reason": <null or string>,
      "reasoning": "<1-2 sentences max>"
    }
  },
  "overall_reasoning": "<2-3 sentences>"
}"""

# ---------------------------------------------------------------------------
# Context helpers
# ---------------------------------------------------------------------------

def _compute_account_stats(state: dict, resolved_history: list) -> dict:
    total     = state.get("total_bets", 0)
    wins      = state.get("wins", 0)
    losses    = state.get("losses", 0)
    total_pnl = state.get("total_pnl", 0.0)
    balance   = state.get("balance", 0.0)

    # Win rates over rolling windows from resolved history
    resolved_positions = [
        m["position"] for m in resolved_history
        if m.get("position") and m["position"].get("pnl") is not None
    ]
    recent_20 = resolved_positions[-20:] if len(resolved_positions) >= 20 else resolved_positions
    recent_5  = resolved_positions[-5:]  if len(resolved_positions) >= 5  else resolved_positions
    recent_10 = resolved_positions[-10:] if len(resolved_positions) >= 10 else resolved_positions

    def _wr(positions):
        if not positions:
            return None
        won = sum(1 for p in positions
                  if (p.get("outcome") is not None and p["outcome"] == p.get("bet_side"))
                  or (p.get("outcome") is None and (p.get("pnl") or 0) > 0))
        return round(won / len(positions), 3)

    pnl_last_10 = round(sum(p.get("pnl") or 0 for p in recent_10), 2)

    return {
        "balance_usdc":    round(balance, 2),
        "total_bets":      total,
        "wins":            wins,
        "losses":          losses,
        "win_rate_all":    round(wins / total, 3) if total > 0 else None,
        "win_rate_last_20": _wr(recent_20),
        "win_rate_last_5":  _wr(recent_5),
        "total_pnl":       round(total_pnl, 2),
        "pnl_last_10":     pnl_last_10,
    }


def _compute_candle_stats(candles: list) -> dict:
    if not candles or len(candles) < 2:
        return {}
    recent = candles[-5:] if len(candles) >= 5 else candles
    highs   = [c[1] for c in recent if c[1]]
    lows    = [c[2] for c in recent if c[2]]
    volumes = [c[4] for c in recent if c[4]]
    closes  = [c[3] for c in recent if c[3]]
    current_price = closes[-1] if closes else 0
    vol_usd = sum(v * current_price for v in volumes) if volumes and current_price else 0
    return {
        "candle_high_5m":  round(max(highs), 2)      if highs   else None,
        "candle_low_5m":   round(min(lows), 2)        if lows    else None,
        "volume_5m_usd":   round(vol_usd, 0)          if vol_usd else None,
    }


def _compute_crowd_signals(price_history: list, up_price_now: float) -> dict:
    """Derive crowd momentum and acceleration from CLOB price trajectory."""
    if not price_history or len(price_history) < 2:
        return {
            "crowd_momentum_1h": 0.0,
            "crowd_accelerating": False,
        }

    # Find point closest to 60 min ago
    p_60m_ago = price_history[0]["p"]
    for pt in price_history:
        if pt["minutes_ago"] >= 60:
            p_60m_ago = pt["p"]
        else:
            break

    crowd_momentum = round(up_price_now - p_60m_ago, 4)

    # Acceleration: compare velocity of last 3 points vs first 3 points
    accelerating = False
    if len(price_history) >= 6:
        first_half = price_history[:3]
        last_half  = price_history[-3:]
        vel_early = last_half[0]["p"] - first_half[0]["p"]
        vel_late  = last_half[-1]["p"] - last_half[0]["p"]
        # Accelerating if recent movement is larger in the same direction
        if abs(vel_late) > abs(vel_early) and (vel_late * crowd_momentum >= 0):
            accelerating = True

    return {
        "crowd_momentum_1h": crowd_momentum,
        "crowd_accelerating": accelerating,
    }


def _format_resolved_history(resolved_history: list) -> list:
    """Compact resolved market records for LLM context (no raw snapshots/token IDs)."""
    result = []
    for m in resolved_history:
        pos = m.get("position", {})
        if not pos or pos.get("pnl") is None:
            continue
        try:
            end_dt   = datetime.fromisoformat(m["event_end"].replace("Z", "+00:00"))
            hour_utc = end_dt.hour
        except Exception:
            hour_utc = None
        won = (pos.get("outcome") is not None and pos["outcome"] == pos.get("bet_side")) \
              or (pos.get("outcome") is None and (pos.get("pnl") or 0) > 0)
        result.append({
            "slug":              m.get("slug", ""),
            "hour_utc":          hour_utc,
            "bet_side":          pos.get("bet_side"),
            "outcome":           pos.get("outcome"),
            "won":               won,
            "pnl":               round(pos.get("pnl") or 0, 2),
            "drift_at_entry_pct": round((pos.get("drift_at_entry") or 0) * 100, 3),
            "p_final":           pos.get("p_final"),
            "ev":                pos.get("ev"),
            "rsi_at_entry":      pos.get("rsi_at_entry"),
            "fear_greed":        pos.get("fear_greed"),
            "up_price_at_entry": pos.get("up_price_at_entry"),
        })
    return result


def _format_open_positions(open_markets: list, active_market_map: dict) -> list:
    """Format open positions with current unrealized PnL."""
    result = []
    for m in open_markets:
        pos = m.get("position", {})
        if pos.get("status") != "open":
            continue
        slug = m.get("slug", "")
        active = active_market_map.get(slug, {})
        cur_up_price = active.get("up_price") or pos.get("up_price_at_entry") or pos.get("entry_price")
        entry = pos.get("entry_price", 0)
        shares = pos.get("shares", 0)
        cur_price = cur_up_price if pos.get("bet_side") == "Up" else (1.0 - (cur_up_price or 0.5))
        unrealized_pnl = round((cur_price - entry) * shares, 2) if entry and shares else None

        try:
            end_dt = datetime.fromisoformat(m["event_end"].replace("Z", "+00:00"))
            secs   = (end_dt - datetime.now(timezone.utc)).total_seconds()
        except Exception:
            secs = None

        result.append({
            "slug":             slug,
            "bet_side":         pos.get("bet_side"),
            "entry_price":      entry,
            "current_up_price": cur_up_price,
            "drift_at_entry_pct": round((pos.get("drift_at_entry") or 0) * 100, 3),
            "seconds_to_end":   int(secs) if secs is not None else None,
            "unrealized_pnl":   unrealized_pnl,
        })
    return result


# ---------------------------------------------------------------------------
# Main context builder
# ---------------------------------------------------------------------------

def build_context(
    coin: str,
    current_price: float,
    signals: dict,
    p_technical: float,
    fg_val: int,
    fg_class: str,
    news_articles: list,
    active_markets: list,
    price_histories: dict,      # slug -> list[{"minutes_ago": float, "p": float}]
    resolved_history: list,
    open_positions: list,
    state: dict,
    strategy: dict,
) -> dict:
    now = datetime.now(timezone.utc)

    # Account stats
    account = _compute_account_stats(state, resolved_history)

    # Candle stats from price_state (signals dict already has RSI/momentum; candles passed via signals)
    candle_stats = _compute_candle_stats(signals.get("_candles", []))

    # Map active market slugs for open-position lookup
    active_map = {m["slug"]: m for m in active_markets}

    # Format active markets with Polymarket crowd signals
    fmt_markets = []
    for m in active_markets:
        slug       = m["slug"]
        up_price   = m.get("up_price", 0.5)
        hist       = price_histories.get(slug, [])
        crowd      = _compute_crowd_signals(hist, up_price)
        has_open   = any(
            op.get("slug") == slug for op in open_positions
            if (op.get("position") or {}).get("status") == "open"
        )

        try:
            end_dt   = datetime.fromisoformat(m["event_end"].replace("Z", "+00:00"))
            secs_end = max(0, (end_dt - now).total_seconds())
        except Exception:
            secs_end = m.get("seconds_to_end", 0)

        start_price = m.get("coin_start_price") or current_price
        drift_pct   = round((current_price - start_price) / start_price * 100, 3) if start_price else 0.0

        fmt_markets.append({
            "slug":             slug,
            "seconds_to_end":   int(secs_end),
            "minutes_to_end":   round(secs_end / 60.0, 1),
            "up_price":         up_price,
            "down_price":       m.get("down_price", round(1.0 - up_price, 3)),
            "drift_from_start_pct": drift_pct,
            "coin_start_price": start_price,
            "has_open_position": has_open,
            "polymarket_crowd": {
                "volume_24hr_usdc":     m.get("volume_24hr", 0.0),
                "open_interest_usdc":   m.get("open_interest", 0.0),
                "spread":               m.get("spread", 0.0),
                "competitive":          round(m.get("competitive", 0.0), 4),
                "one_hour_price_chg":   m.get("one_hour_price_chg", 0.0),
                "last_trade_price":     m.get("last_trade_price", up_price),
                "best_bid":             m.get("best_bid", 0.0),
                "best_ask":             m.get("best_ask", 0.0),
                "crowd_momentum_1h":    crowd["crowd_momentum_1h"],
                "crowd_accelerating":   crowd["crowd_accelerating"],
                "price_trajectory":     hist,
            },
        })

    # Compact resolved history (last 25)
    fmt_resolved = _format_resolved_history(resolved_history[-25:])

    # Open positions
    fmt_open = _format_open_positions(open_positions, active_map)

    ctx = {
        "coin":          coin,
        "timestamp_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "hour_utc":      now.hour,
        "weekday":       now.strftime("%A"),

        "account": account,

        "strategy_params": {
            "annual_vol":     strategy.get("annual_vol"),
            "kelly_fraction": strategy.get("kelly_fraction"),
            "min_ev":         strategy.get("min_ev"),
            "min_drift":      strategy.get("min_drift"),
        },

        "binance_signals": {
            "current_price":    round(current_price, 2),
            "rsi_14":           round(signals.get("rsi_14", 50.0), 1),
            "momentum_1m_pct":  round(signals.get("momentum_1m", 0.0) * 100, 4),
            "momentum_5m_pct":  round(signals.get("momentum_5m", 0.0) * 100, 4),
            "ob_imbalance":     round(signals.get("ob_imbalance", 0.0), 4),
            "p_technical":      round(p_technical, 4),
            **candle_stats,
        },

        "sentiment": {
            "fear_greed_value": fg_val,
            "fear_greed_class": fg_class,
        },

        "news":             news_articles,
        "active_markets":   fmt_markets,
        "open_positions":   fmt_open,
        "resolved_history": fmt_resolved,
    }

    return ctx


# ---------------------------------------------------------------------------
# Response parser / validator
# ---------------------------------------------------------------------------

_VALID_REGIMES    = {"trending_up", "trending_down", "choppy", "volatile", "uncertain"}
_VALID_CONFS      = {"high", "medium", "low"}
_VALID_SENTIMENTS = {"strongly_bullish", "bullish", "neutral", "bearish", "strongly_bearish"}
_VALID_ACTIONS    = {"bet_up", "bet_down", "skip"}


def _parse_response(raw: dict) -> CycleAssessment:
    """Validate and clamp all fields; fall back to NEUTRAL on anything unexpected."""
    out = dict(NEUTRAL_ASSESSMENT)

    regime = raw.get("regime", "uncertain")
    out["regime"] = regime if regime in _VALID_REGIMES else "uncertain"

    rc = raw.get("regime_confidence", "low")
    out["regime_confidence"] = rc if rc in _VALID_CONFS else "low"

    sentiment = raw.get("news_sentiment", "neutral")
    out["news_sentiment"] = sentiment if sentiment in _VALID_SENTIMENTS else "neutral"

    try:
        score = float(raw.get("news_sentiment_score", 0.0))
        out["news_sentiment_score"] = round(max(-0.15, min(0.15, score)), 4)
    except (TypeError, ValueError):
        out["news_sentiment_score"] = 0.0

    out["cycle_veto"]        = bool(raw.get("cycle_veto", False))
    out["cycle_veto_reason"] = raw.get("cycle_veto_reason") or None
    out["overall_reasoning"] = str(raw.get("overall_reasoning", ""))[:500]

    # Per-market decisions
    markets_raw = raw.get("markets", {})
    if not isinstance(markets_raw, dict):
        markets_raw = {}

    parsed_markets = {}
    for slug, mkt_raw in markets_raw.items():
        if not isinstance(mkt_raw, dict):
            continue
        action = mkt_raw.get("action", "skip")
        if action not in _VALID_ACTIONS:
            action = "skip"

        try:
            p_adj = float(mkt_raw.get("p_up_adj", 0.0))
            p_adj = round(max(-0.12, min(0.12, p_adj)), 4)
        except (TypeError, ValueError):
            p_adj = 0.0

        try:
            km = float(mkt_raw.get("kelly_multiplier", 1.0))
            km = round(max(0.5, min(1.5, km)), 4)
        except (TypeError, ValueError):
            km = 1.0

        conf = mkt_raw.get("confidence", "low")
        if conf not in _VALID_CONFS:
            conf = "low"

        parsed_markets[slug] = {
            "action":           action,
            "p_up_adj":         p_adj,
            "kelly_multiplier": km,
            "confidence":       conf,
            "skip_reason":      mkt_raw.get("skip_reason") or None,
            "reasoning":        str(mkt_raw.get("reasoning", ""))[:300],
        }

    out["markets"] = parsed_markets
    return out


# ---------------------------------------------------------------------------
# OpenRouter call
# ---------------------------------------------------------------------------

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
LLM_TIMEOUT    = 12   # seconds


def assess_cycle(api_key: str, model: str, context: dict) -> CycleAssessment:
    """
    Call OpenRouter with the assembled context and return a parsed CycleAssessment.
    Returns NEUTRAL_ASSESSMENT on any failure — never raises.
    """
    if not api_key:
        return NEUTRAL_ASSESSMENT

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": json.dumps(context, separators=(",", ":"))},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
        "max_tokens":  32768,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
        "HTTP-Referer":  "crypto-bot",
        "X-Title":       "crypto-bot-llm-advisor",
    }

    try:
        resp = requests.post(
            OPENROUTER_URL,
            headers=headers,
            json=payload,
            timeout=LLM_TIMEOUT,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]

        # Strip leading/trailing whitespace — some Gemini variants prefix with newlines
        content = content.strip()

        # If the model added any preamble before the JSON object, find the first {
        brace = content.find("{")
        if brace > 0:
            content = content[brace:]

        raw = json.loads(content)
        return _parse_response(raw)

    except requests.exceptions.Timeout:
        print(f"  [LLM] Timeout after {LLM_TIMEOUT}s — using quantitative model only")
        return NEUTRAL_ASSESSMENT

    except requests.exceptions.HTTPError as e:
        print(f"  [LLM] HTTP {e.response.status_code} from OpenRouter — using quantitative model only")
        return NEUTRAL_ASSESSMENT

    except (json.JSONDecodeError, KeyError, IndexError) as e:
        content_preview = ""
        try:
            content_preview = resp.json()["choices"][0]["message"]["content"][:300]
        except Exception:
            try:
                content_preview = resp.text[:200]
            except Exception:
                pass
        print(f"  [LLM] Parse error ({e}) — content: {content_preview!r}")
        return NEUTRAL_ASSESSMENT

    except Exception as e:
        print(f"  [LLM] Unexpected error ({e}) — using quantitative model only")
        return NEUTRAL_ASSESSMENT
