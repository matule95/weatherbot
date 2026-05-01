"""
Bot health check — works for both sim and real modes.

Usage:
  python health_check.py          # auto-detects mode (defaults to sim if sim_state.json exists)
  python health_check.py --sim    # force sim mode
  python health_check.py --real   # force real mode

Reports realized PnL, equity (cash + open positions at last-known prices),
trade quality, exit-reason breakdown, position aging, and red-flag signals.
"""
import json, sys, glob, os
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter, defaultdict

# --- mode detection ---
DATA_DIR = Path("weather_bot_data")
force_sim = "--sim" in sys.argv
force_real = "--real" in sys.argv

sim_state = DATA_DIR / "sim_state.json"
real_state = DATA_DIR / "state.json"

if force_real:
    SIM = False
elif force_sim:
    SIM = True
elif sim_state.exists() and not real_state.exists():
    SIM = True
elif real_state.exists() and not sim_state.exists():
    SIM = False
else:
    # Both exist — prefer sim by default
    SIM = True

state_file = sim_state if SIM else real_state
markets_dir = DATA_DIR / ("sim_markets" if SIM else "markets")
mode_label = "SIM" if SIM else "LIVE"

if not state_file.exists():
    print(f"No {mode_label.lower()} state file at {state_file}")
    sys.exit(1)

state = json.loads(state_file.read_text())
now = datetime.now(timezone.utc)

# --- load all markets, classify ---
markets = []
for f in sorted(glob.glob(str(markets_dir / "*.json"))):
    try:
        markets.append(json.loads(Path(f).read_text()))
    except Exception:
        pass

open_positions = []
closed_cycles = []
for m in markets:
    for c in m.get("cycles", []):
        c_full = {**c, "city": m["city"], "city_name": m.get("city_name", m["city"]),
                  "date": m["date"], "unit": m.get("unit", "F"),
                  "market_snapshots": m.get("market_snapshots", []),
                  "all_outcomes": m.get("all_outcomes", []),
                  "event_end_date": m.get("event_end_date")}
        if c.get("status") == "open":
            open_positions.append(c_full)
        else:
            closed_cycles.append(c_full)

# --- equity: cash + sum(open position cost × current_price/entry_price) ---
def latest_price(c):
    """Get the latest snapshot price for this cycle's token."""
    snaps = c.get("market_snapshots", [])
    tok = c.get("token_id")
    if not snaps or not tok: return None
    for s in reversed(snaps):
        px = s.get("prices", {}).get(tok)
        if px is not None:
            return px
    return None

cash = state["balance"]
deployed_value = 0.0
deployed_cost = 0.0
for c in open_positions:
    px = latest_price(c)
    cost = c.get("cost", 0)
    if px is None:
        deployed_value += cost  # mark to entry if no price
    else:
        deployed_value += (cost / c["entry_price"]) * px  # shares × current price
    deployed_cost += cost

equity = cash + deployed_value
unrealized = deployed_value - deployed_cost
starting = state.get("starting_balance", cash)
total_pnl = equity - starting
realized_pnl = state.get("net_pnl", 0)

# --- print header ---
print("=" * 78)
print(f"  WEATHERBOT HEALTH CHECK — {mode_label} MODE — {now.strftime('%Y-%m-%d %H:%M UTC')}")
print("=" * 78)

# --- top-line numbers ---
print(f"\n  STARTING BANKROLL:   ${starting:>8.2f}")
print(f"  CURRENT EQUITY:      ${equity:>8.2f}   ({total_pnl:+.2f}  {(total_pnl/starting*100 if starting else 0):+.1f}%)")
print(f"    cash:              ${cash:>8.2f}")
print(f"    open positions:    ${deployed_value:>8.2f}   ({len(open_positions)} positions, ${deployed_cost:.2f} cost basis)")
print(f"    unrealized:        ${unrealized:>+8.2f}")
print(f"  REALIZED PnL:        ${realized_pnl:>+8.2f}   (net of all closed cycles)")
print(f"  PEAK BALANCE:        ${state.get('peak_balance', starting):>8.2f}")

# --- trade counts and win rate ---
n_total = state.get("total_trades", 0)
n_profit = state.get("profitable_exits", 0)
n_loss = state.get("losing_exits", 0)
n_closed = n_profit + n_loss
wr = (n_profit / n_closed * 100) if n_closed else 0
print(f"\n  TRADES OPENED:       {n_total}")
print(f"  CYCLES CLOSED:       {n_closed}   (profit: {n_profit}, loss: {n_loss})")
if n_closed:
    print(f"  WIN RATE:            {wr:.1f}%   (need >50% for positive EV at current TP/loss ratio)")
print(f"  RESOLVES:            wins {state.get('resolved_wins', 0)}, losses {state.get('resolved_losses', 0)}")

# --- exit reason breakdown (from closed cycles) ---
if closed_cycles:
    reasons = Counter(c.get("close_reason") for c in closed_cycles)
    print(f"\n  EXIT REASONS (last {len(closed_cycles)} closed cycles):")
    for r, n in reasons.most_common():
        avg_pnl = sum(c.get("pnl", 0) or 0 for c in closed_cycles if c.get("close_reason") == r) / n
        print(f"    {str(r):<22s} {n:>3d}   avg PnL ${avg_pnl:+.2f}")

# --- open position detail ---
if open_positions:
    print(f"\n  OPEN POSITIONS (sorted by unrealized):")
    print(f"    {'city':<14s} {'date':<12s} {'bucket':<14s} {'entry':>6s} {'now':>6s} {'roi':>7s} {'cost':>6s} {'age':>5s}")
    rows = []
    for c in open_positions:
        px = latest_price(c)
        roi = ((px / c["entry_price"]) - 1) * 100 if px else None
        opened = c.get("opened_at", "")
        try:
            age_h = (now - datetime.fromisoformat(opened)).total_seconds() / 3600
        except Exception:
            age_h = 0
        bkt = f"{c['bucket_low']}-{c['bucket_high']}{c['unit']}"
        rows.append((roi if roi is not None else -999, c["city"], c["date"], bkt,
                     c["entry_price"], px, roi, c.get("cost", 0), age_h, c.get("trailing_activated"), c.get("stop_price")))
    rows.sort(key=lambda r: r[0], reverse=True)
    for r in rows:
        _, city, date, bkt, entry, px, roi, cost, age, trail, stop = r
        px_s = f"${px:.3f}" if px is not None else "  --"
        roi_s = f"{roi:+.1f}%" if roi is not None else "   --"
        flag = " *" if trail else ""
        print(f"    {city:<14s} {date:<12s} {bkt:<14s} ${entry:>.3f} {px_s:>6s} {roi_s:>7s} ${cost:>5.2f} {age:>4.1f}h{flag}")
    print(f"    (* = trailing stop armed)")

# --- recent activity / "is the bot alive" check ---
recent_opens = [c for c in open_positions + closed_cycles
                if c.get("opened_at") and (now - datetime.fromisoformat(c["opened_at"])).total_seconds() < 3*3600]
recent_closes = [c for c in closed_cycles
                 if c.get("closed_at") and (now - datetime.fromisoformat(c["closed_at"])).total_seconds() < 3*3600]
print(f"\n  ACTIVITY (last 3h):  {len(recent_opens)} new opens, {len(recent_closes)} closes")

# --- forecast snapshot freshness (proxy for "scan is running") ---
latest_snap_ts = None
for m in markets:
    snaps = m.get("forecast_snapshots", [])
    if snaps:
        try:
            ts = datetime.fromisoformat(snaps[-1]["ts"])
            if latest_snap_ts is None or ts > latest_snap_ts:
                latest_snap_ts = ts
        except Exception:
            pass
if latest_snap_ts:
    age_min = (now - latest_snap_ts).total_seconds() / 60
    flag = "   STALE — scan may be down" if age_min > 90 else ""
    print(f"  LAST SCAN:           {latest_snap_ts.strftime('%H:%M UTC')} ({age_min:.0f} min ago){flag}")

# --- per-city PnL (only meaningful with closed cycles) ---
if closed_cycles:
    by_city = defaultdict(lambda: {"n": 0, "pnl": 0.0, "wins": 0})
    for c in closed_cycles:
        by_city[c["city"]]["n"] += 1
        by_city[c["city"]]["pnl"] += c.get("pnl", 0) or 0
        if (c.get("pnl") or 0) > 0:
            by_city[c["city"]]["wins"] += 1
    print(f"\n  PER-CITY (closed cycles):")
    for city, d in sorted(by_city.items(), key=lambda x: -x[1]["pnl"]):
        wr_c = (d["wins"] / d["n"] * 100) if d["n"] else 0
        print(f"    {city:<14s} n={d['n']:<3d} pnl ${d['pnl']:>+6.2f}   wr {wr_c:.0f}%")

# --- red-flag analysis ---
print(f"\n  RED FLAGS:")
flags = []

if realized_pnl < -50:
    flags.append(f"  [!] Realized PnL ${realized_pnl:+.2f} (>$50 loss) — RECALIBRATION FAILED, stop and report")
if n_loss >= 7 and (now - datetime.fromisoformat(closed_cycles[0].get("opened_at", now.isoformat()))).total_seconds() < 12*3600:
    flags.append(f"  [!] {n_loss} losing exits within 12h — bounded-loss thesis not working")
if latest_snap_ts and (now - latest_snap_ts).total_seconds() > 3*3600:
    flags.append(f"  [!] No scan activity for {(now-latest_snap_ts).total_seconds()/3600:.1f}h — bot may have crashed")
if n_closed >= 5 and wr < 35:
    flags.append(f"  [!] Win rate {wr:.0f}% across {n_closed} closes — model edge not materializing")
if cash < 5 and len(open_positions) < 3:
    flags.append(f"  [!] Cash ${cash:.2f} but only {len(open_positions)} open positions — capital may be stuck")

if not flags:
    avg_age = sum(((now - datetime.fromisoformat(c["opened_at"])).total_seconds()/3600 for c in open_positions if c.get("opened_at")), 0) / max(len(open_positions), 1)
    print(f"  None. Bot looks healthy.")
    print(f"  Avg open-position age: {avg_age:.1f}h — D+2 markets resolve within 48h, expect closes by then.")
else:
    for f in flags:
        print(f)

# --- summary verdict ---
print(f"\n  VERDICT:")
if realized_pnl < -50 or (n_closed >= 5 and wr < 35):
    print(f"    STOP. Recalibration not working. Report to AI for diagnosis.")
elif n_total < 3 and (latest_snap_ts is None or (now - latest_snap_ts).total_seconds() > 6*3600):
    print(f"    BOT MAY BE STUCK. Check logs, possibly restart.")
elif n_closed == 0:
    print(f"    TOO EARLY. Wait for at least 3-5 closed cycles before judging.")
elif total_pnl > 10:
    print(f"    HEALTHY. Up ${total_pnl:+.2f} on equity. Let it keep running.")
elif total_pnl > -10:
    print(f"    NEUTRAL. Equity within ±$10 of starting. Keep observing.")
else:
    print(f"    WATCH CAREFULLY. Down ${total_pnl:.2f} on equity but not at red-flag threshold.")

print("=" * 78)
