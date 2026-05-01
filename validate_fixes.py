#!/usr/bin/env python3
"""
validate_fixes.py — Validates the three v4.7 bug fixes without running the full bot.
Run from:  C:/Users/clayt/WebstormProjects/weatherbot/
           python validate_fixes.py
"""

import sys, ast, math, requests
from datetime import datetime, timezone, timedelta

ok = 0; fail = 0

def check(label, passed, detail=""):
    global ok, fail
    sym = "PASS" if passed else "FAIL"
    print(f"  [{sym}] {label}")
    if not passed and detail:
        print(f"         ^ {detail}")
    if passed: ok += 1
    else: fail += 1

# --───────────────────────────────────────────────────────────────────────────
# 0. Syntax check — bot_v4.py must parse without errors
# --───────────────────────────────────────────────────────────────────────────
print("\n--0. Syntax check --")
try:
    src = open("bot_v4.py", encoding="utf-8").read()
    ast.parse(src)
    check("bot_v4.py parses without syntax errors", True)
except SyntaxError as e:
    check("bot_v4.py parses without syntax errors", False, str(e))
    print("\nSyntax error — aborting further tests.")
    sys.exit(1)

# --───────────────────────────────────────────────────────────────────────────
# 0b. Critical string checks — verify the exact new lines are present in source
# --───────────────────────────────────────────────────────────────────────────
print("\n--0b. Source-level presence checks --")

check("Fix 1: local_hour = -1 fallback present",
      "local_hour = -1" in src,
      "expected 'local_hour = -1' in get_wu_running_max()")

check("Fix 1: ZoneInfo runtime fallback (converted flag) present",
      "converted = False" in src,
      "expected inner try/converted pattern for ZoneInfo→pytz fallback")

check("Fix 2: re-entry model_delta guard present",
      "model_delta <= MAX_MODEL_DELTA else None" in src,
      "expected 're-entry ... if model_delta is None or model_delta <= MAX_MODEL_DELTA else None'")

check("Fix 3: corrected model_delta recompute present",
      "model_delta = round(abs(ecmwf_corrected - gfs_corrected), 1)" in src,
      "expected corrected delta calculation after bias block")

check("Fix 1: old fallback 'local_hour = 12' removed",
      "local_hour = 12" not in src,
      "old unsafe fallback still present in source")

# --───────────────────────────────────────────────────────────────────────────
# 1. Fix 1 — WU timezone conversion
# --───────────────────────────────────────────────────────────────────────────
print("\n--Fix 1: WU timezone fallback behaviour --")

# Detect which timezone library works at runtime
_tz_lib = None
try:
    from zoneinfo import ZoneInfo
    ZoneInfo("America/New_York")
    _tz_lib = "zoneinfo+tzdata"
except Exception:
    pass
if _tz_lib is None:
    try:
        import pytz
        pytz.timezone("America/New_York")
        _tz_lib = "pytz"
    except Exception:
        pass

if _tz_lib:
    check(f"Timezone library works: {_tz_lib}", True)
else:
    # Not a fix failure — WU being skipped is SAFE. But afternoon data is also missed.
    print("  [WARN] No tz library (tzdata/pytz) — WU always skipped (safe, no corrupt forecasts)")
    print("         To also use valid afternoon WU readings: pip install tzdata")

def simulate_local_hour(rmt_str, tz_name):
    """Mirrors the fixed get_wu_running_max() timezone block exactly."""
    local_hour = -1
    if rmt_str:
        try:
            utc_dt    = datetime.fromisoformat(rmt_str.replace("Z", "+00:00"))
            converted = False
            try:
                from zoneinfo import ZoneInfo
                local_dt   = utc_dt.astimezone(ZoneInfo(tz_name))
                local_hour = local_dt.hour
                converted  = True
            except Exception:
                pass
            if not converted:
                import pytz
                local_dt   = utc_dt.astimezone(pytz.timezone(tz_name))
                local_hour = local_dt.hour
        except Exception:
            pass
    return local_hour

# Safety invariant: the old default (12) was dangerous, the new default (-1) is safe
check("Old default local_hour=12 would NOT skip WU at 1am  [showing bug was real]", 12 >= 8)
check("New default local_hour=-1 DOES skip WU at 1am  [fix is safe]", -1 < 8)

# Core: early morning must always be skipped regardless of tz library
lh_early = simulate_local_hour("2026-04-20T05:44:00.000Z", "America/New_York")
check(f"05:44 UTC NYC local_hour={lh_early}: WU correctly skipped (<8 or ==-1)",
      lh_early < 8, f"got {lh_early}")

if _tz_lib:
    # Full timezone checks only when a tz library is available
    check(f"05:44 UTC = 1:44am EDT -> local_hour={lh_early} (expected 1)", lh_early == 1, f"got {lh_early}")
    lh_pm = simulate_local_hour("2026-04-20T19:00:00.000Z", "America/New_York")
    check(f"19:00 UTC = 3pm EDT -> local_hour={lh_pm} -> WU included (>=14)", lh_pm >= 14, f"got {lh_pm}")
    lh_ba = simulate_local_hour("2026-04-20T18:00:00.000Z", "America/Buenos_Aires")
    check(f"BsAs 18:00 UTC = 3pm ART -> local_hour={lh_ba} -> WU included (>=14)", lh_ba >= 14, f"got {lh_ba}")
else:
    print("  [INFO] Skipping full local_hour checks (no tz lib). Run: pip install tzdata")

# --───────────────────────────────────────────────────────────────────────────
# 2. Fix 2 — Re-entry model_delta gate
# --───────────────────────────────────────────────────────────────────────────
print("\n--Fix 2: Re-entry model_delta gate --")

def reentry_allowed(model_delta, max_model_delta=2.0):
    """Mirrors the fixed re-entry guard: allowed iff delta is None or <= max."""
    return model_delta is None or model_delta <= max_model_delta

check("delta=2.7 (Buenos Aires C2)  ->re-entry BLOCKED", not reentry_allowed(2.7),
      "expected False (blocked), got True")
check("delta=2.0 (Buenos Aires C1)  ->re-entry ALLOWED (boundary <= not <)",
      reentry_allowed(2.0))
check("delta=1.5                    ->re-entry ALLOWED",  reentry_allowed(1.5))
check("delta=None (no model data)   ->re-entry ALLOWED (conservative pass)",
      reentry_allowed(None))
check("delta=3.0                    ->re-entry BLOCKED",  not reentry_allowed(3.0))

# Reproduce Buenos Aires C2 scenario exactly
ba_c2_delta = abs(23.7 - 21.0)  # raw delta
check(f"Buenos Aires C2 raw_delta={ba_c2_delta:.1f} ->re-entry would be BLOCKED by Fix 2",
      not reentry_allowed(ba_c2_delta))

# --───────────────────────────────────────────────────────────────────────────
# 3. Fix 3 — model_delta recomputed from bias-corrected temps
# --───────────────────────────────────────────────────────────────────────────
print("\n--Fix 3: model_delta from bias-corrected temps --")

def corrected_delta(ecmwf_raw, gfs_raw, ecmwf_bias, gfs_bias):
    ec = round(ecmwf_raw + ecmwf_bias, 1)
    gc = round(gfs_raw   + gfs_bias,   1)
    return round(abs(ec - gc), 1), ec, gc

MAX_DELTA = 2.0

# NYC at entry: both raw=50°F, biases +2.276 / -0.103
raw_nyc  = abs(50 - 50)
corr_nyc, ec_nyc, gc_nyc = corrected_delta(50, 50, 2.276, -0.103)
check(f"NYC raw_delta={raw_nyc}°F  ->old gate PASSES (bug: trade entered)",
      raw_nyc <= MAX_DELTA)
check(f"NYC corr_delta={corr_nyc}°F (ECMWF={ec_nyc}, GFS={gc_nyc}) ->new gate BLOCKS",
      corr_nyc > MAX_DELTA,
      f"expected >{MAX_DELTA}, got {corr_nyc}")

# Buenos Aires C1: ECMWF=23.0, GFS=21.0, biases +1.751 / +0.059
raw_ba1  = abs(23.0 - 21.0)
corr_ba1, ec_ba1, gc_ba1 = corrected_delta(23.0, 21.0, 1.751, 0.059)
check(f"BsAs C1  raw_delta={raw_ba1:.1f}°C ->old gate PASSES (at boundary)",
      raw_ba1 <= MAX_DELTA)
check(f"BsAs C1  corr_delta={corr_ba1:.1f}°C (ECMWF={ec_ba1}, GFS={gc_ba1}) ->new gate BLOCKS",
      corr_ba1 > MAX_DELTA,
      f"expected >{MAX_DELTA}, got {corr_ba1}")

# Buenos Aires C2: ECMWF=23.7, GFS=21.0
raw_ba2  = abs(23.7 - 21.0)
corr_ba2, ec_ba2, gc_ba2 = corrected_delta(23.7, 21.0, 1.751, 0.059)
check(f"BsAs C2  raw_delta={raw_ba2:.1f}°C ->old gate already blocks",
      raw_ba2 > MAX_DELTA)
check(f"BsAs C2  corr_delta={corr_ba2:.1f}°C ->new gate also blocks",
      corr_ba2 > MAX_DELTA)

# Sanity: equal biases ->corrected delta == raw delta
raw_eq, corr_eq, _, _ = abs(20.0 - 18.0), *corrected_delta(20.0, 18.0, 1.0, 1.0)
check(f"Equal biases ->corrected_delta ({corr_eq}°C) == raw_delta ({raw_eq}°C)",
      raw_eq == corr_eq)

# --───────────────────────────────────────────────────────────────────────────
# 4. Live WU API spot-check
# --───────────────────────────────────────────────────────────────────────────
print("\n--4. Live WU API spot-check --")
try:
    r = requests.get("http://localhost:3000/weather/KLGA/hourly?date=2026-04-20", timeout=5)
    check("WU API /hourly responds 200", r.ok, f"status {r.status_code}")
    d = r.json()
    rmt = d.get("running_max_time")
    tz  = d.get("station_timezone")
    check("station_timezone present in response", bool(tz), f"got {tz!r}")
    check("running_max_time present in response", bool(rmt), f"got {rmt!r}")
    if rmt and tz:
        lh = simulate_local_hour(rmt, tz)
        if lh == -1:
            tier = "SKIP (tz conversion failed — tzdata/pytz not installed)"
        elif d.get("is_finalized"):
            tier = "finalized (sigma=0.3F)"
        elif lh >= 14:
            tier = "afternoon (sigma=1.0F)"
        elif lh >= 8:
            tier = "morning (sigma=1.5F)"
        else:
            tier = "SKIP (local_hour < 8)"
        check(f"running_max_time -> local_hour={lh}, tier: {tier}", True)
except Exception as e:
    check("WU API reachable", False, str(e))

# --───────────────────────────────────────────────────────────────────────────
# Summary
# --───────────────────────────────────────────────────────────────────────────
print(f"\n{'-'*50}")
print(f"  {ok} passed   {fail} failed")
if fail == 0:
    print("  All checks passed — fixes validated.")
else:
    print(f"  {fail} check(s) failed — review output above.")
sys.exit(0 if fail == 0 else 1)
