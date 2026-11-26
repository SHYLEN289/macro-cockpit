#!/usr/bin/env python3
"""Macro Cockpit data builder.

Pulls free data (FRED + Yahoo Finance), computes the regime read, and writes
docs/data.json for the dashboard page. Standard library only.

    python scripts/fetch_data.py            # live data (needs internet)
    python scripts/fetch_data.py --demo     # synthetic data, no internet

Environment:
    FRED_API_KEY   optional. Without it the script uses FRED's public CSV
                   endpoint, which works but is less official.
"""
import argparse
import csv
import datetime as dt
import json
import math
import os
import random
import statistics
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.json"
OUT = ROOT / "docs" / "data.json"
HIST = ROOT / "data" / "history.csv"
UA = "Mozilla/5.0 (compatible; macro-cockpit/1.0)"
TODAY = dt.date.today()


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------
def http_get(url, tries=3, timeout=30):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"GET failed: {last}")


def fetch_fred(sid, start, api_key):
    if api_key:
        q = urllib.parse.urlencode({"series_id": sid, "api_key": api_key,
                                    "file_type": "json", "observation_start": start})
        data = json.loads(http_get("https://api.stlouisfed.org/fred/series/observations?" + q))
        return [(o["date"], float(o["value"])) for o in data["observations"]
                if o["value"] not in (".", "")]
    q = urllib.parse.urlencode({"id": sid, "cosd": start})
    rows = list(csv.reader(http_get("https://fred.stlouisfed.org/graph/fredgraph.csv?" + q).splitlines()))
    return [(r[0], float(r[1])) for r in rows[1:] if len(r) >= 2 and r[1] not in (".", "")]


def fetch_yahoo(sym, rng):
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           + urllib.parse.quote(sym, safe="") + f"?range={rng}&interval=1d")
    res = json.loads(http_get(url))["chart"]["result"][0]
    out = {}
    for t, c in zip(res["timestamp"], res["indicators"]["quote"][0]["close"]):
        if c is not None:
            day = dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%d")
            out[day] = float(c)
    return sorted(out.items())


def load_live(cfg):
    key = os.environ.get("FRED_API_KEY", "").strip()
    raw, errors = {}, []
    hy = cfg.get("history_years", {})
    for k, sid in cfg["fred"].items():
        years = hy.get(k, hy.get("default", 3))
        start = (TODAY - dt.timedelta(days=int(365.25 * years))).isoformat()
        try:
            s = fetch_fred(sid, start, key)
            if not s:
                raise RuntimeError("empty series")
            raw[k] = s
        except Exception as e:  # noqa: BLE001
            errors.append(f"FRED {sid}: {e}")
    yr = cfg.get("yahoo_range", {})
    symbols = dict(cfg["yahoo"])
    for inst in cfg["instruments"]:
        symbols["inst_" + inst["id"]] = inst["yahoo"]
    for k, sym in symbols.items():
        try:
            s = fetch_yahoo(sym, yr.get(k, yr.get("default", "2y")))
            if not s:
                raise RuntimeError("empty series")
            raw[k] = s
        except Exception as e:  # noqa: BLE001
            errors.append(f"Yahoo {sym}: {e}")
        time.sleep(0.4)
    return raw, errors


# --------------------------------------------------------------------------
# Demo data (synthetic, clearly flagged in the output)
# --------------------------------------------------------------------------
def bdays(n):
    d, out = TODAY, []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= dt.timedelta(days=1)
    return out[::-1]


def weekly(n):
    d, out = TODAY, []
    while len(out) < n:
        out.append(d.isoformat())
        d -= dt.timedelta(days=7)
    return out[::-1]


def monthly(n):
    y, m, out = TODAY.year, TODAY.month, []
    while len(out) < n:
        out.append(dt.date(y, m, 1).isoformat())
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out[::-1]


def path(n, anchors, vol, rnd):
    """Smooth anchor path plus noise bridged so start and end stay exact."""
    base = []
    for i in range(n):
        f = i / (n - 1)
        for (f0, v0), (f1, v1) in zip(anchors, anchors[1:]):
            if f0 <= f <= f1:
                base.append(v0 + (v1 - v0) * (f - f0) / ((f1 - f0) or 1))
                break
    w, acc = [], 0.0
    for _ in range(n):
        acc += rnd.gauss(0, vol)
        w.append(acc)
    return [base[i] + (w[i] - w[0]) - (w[-1] - w[0]) * i / (n - 1) for i in range(n)]


def load_demo(cfg, scenario="calm"):
    rnd = random.Random(7)
    raw = {}
    S = scenario == "stress"

    def E(calm, stress):
        return stress if S else calm

    def put(k, dates, vals):
        raw[k] = [(d, float(v)) for d, v in zip(dates, vals)]

    D, N = bdays(780), 780
    for k, a, b, v in [("dgs3mo", 4.6, 4.28, .01), ("dgs2", 4.1, E(3.85, 4.30), .02), ("dgs5", 4.0, 3.92, .02),
                       ("dgs7", 4.1, 4.05, .02), ("dgs10", 4.3, E(4.21, 4.63), .02), ("dgs20", 4.6, 4.48, .02),
                       ("dgs30", 4.6, 4.55, .02), ("real10", 1.9, E(1.82, 2.25), .015), ("be10", 2.4, 2.37, .01),
                       ("hy", 3.1, E(3.42, 5.35), .03), ("ig", .85, E(.94, 1.35), .008), ("bbb", 1.05, E(1.18, 1.75), .01),
                       ("bb", 1.9, E(2.21, 3.4), .02), ("b", 3.1, E(3.56, 5.6), .03), ("ccc", 8.0, E(9.12, 13.8), .08),
                       ("vix_f", 15, E(16.2, 27.4), .5), ("vix3m_f", 17, E(17.9, 25.1), .4), ("wti_f", 74, E(78.4, 112.3), .8),
                       ("brent_f", 78, E(82.1, 116.0), .8)]:
        put(k, D, path(N, [(0, a), (1, b)], v, rnd))
    # 2s10s: inverted for a long stretch, un-inverted since (keeps the re-inversion logic honest)
    D4 = bdays(1040)
    put("t10y2y", D4, path(1040, [(0, -.3), (.35, -.45), (.62, -.05), (.75, .15), (1, E(.36, -.12))], .002, rnd))
    put("t10y3m", D, [dgs10 - dgs3 for (_, dgs10), (_, dgs3) in zip(raw["dgs10"], raw["dgs3mo"])])
    # policy rate: a few cuts, last one roughly five months ago
    hi = [5.5 if i < .45 * N else 5.25 if i < .6 * N else 5.0 if i < .75 * N else 4.75 if i < .85 * N else 4.5
          for i in range(N)]
    put("tar_hi", D, hi)
    put("tar_lo", D, [h - .25 for h in hi])
    put("effr", D, [h - .17 for h in hi])
    put("sofr", D, [h - .19 + rnd.gauss(0, .01) for h in hi])
    put("iorb", D, [h - .10 for h in hi])
    put("rrp_rate", D, [h - .25 for h in hi])
    # Yahoo-style
    D5 = bdays(1300)
    put("vix", D5, path(1300, [(0, 17), (.5, 19), (1, E(16.2, 27.4))], .6, rnd))
    put("vix9d", D5, path(1300, [(0, 16), (.5, 18), (1, E(14.9, 31.0))], .7, rnd))
    put("vix3m", D5, path(1300, [(0, 19), (.5, 20.5), (1, E(17.9, 25.1))], .45, rnd))
    put("vix6m", D5, path(1300, [(0, 20), (.5, 21), (1, E(19.0, 23.5))], .3, rnd))
    put("vvix", D, path(N, [(0, 90), (1, E(88.4, 118.0))], 1.2, rnd))
    spx = path(1300, [(0, 4300), (1, 6600)], E(28, 80), rnd)
    put("spx", D5, spx)
    put("dxy", D, path(N, [(0, 104), (.5, 106), (1, E(101.5, 108.9))], .25, rnd))
    put("wti", D, path(N, [(0, 74), (1, E(78.4, 112.3))], .8, rnd))
    put("brent", D, path(N, [(0, 78), (1, E(82.1, 116.0))], .8, rnd))
    for inst in cfg["instruments"]:
        end = 25100 if inst["id"] == "NQ" else 6600 if inst["id"] == "ES" else 1000
        put("inst_" + inst["id"], D, path(N, [(0, end * .78), (1, end)], end * .004, rnd))
    # weekly / monthly
    W = weekly(156)
    put("claims", W, path(156, [(0, 215000), (1, E(228000, 262000))], 3000, rnd))
    put("nfci", W, path(156, [(0, -.45), (1, E(-.42, .18))], .01, rnd))
    put("gdpnow", W, path(156, [(0, 1.5), (1, E(2.4, .4))], .15, rnd))
    M = monthly(48)
    for k, g in [("cpi", .0025), ("core_cpi", .0026), ("core_pce", .0022), ("pce", .002)]:
        v, out = 300.0, []
        for _ in range(48):
            v *= 1 + g + rnd.gauss(0, .0004)
            out.append(v)
        put(k, M, out)
    put("unrate", M, path(48, [(0, 3.9), (1, 4.2)], .03, rnd))
    put("sahm", M, path(48, [(0, .05), (1, E(.2, .43))], .02, rnd))
    return raw, [f"Demo mode ({scenario}): all numbers are synthetic."]


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def D(s):
    return dt.date.fromisoformat(s)


def val(s, back=0):
    if not s or len(s) <= back:
        return None
    return s[-1 - back][1]


def chg(s, n=1):
    a, b = val(s), val(s, n)
    return None if a is None or b is None else a - b


def pchg(s, n=1):
    a, b = val(s), val(s, n)
    return None if a is None or not b else (a / b - 1) * 100


def spark(s, n=60):
    return [round(v, 4) for _, v in (s or [])[-n:]]


def interp(points, x):
    pts = sorted(points)
    if x <= pts[0][0]:
        return pts[0][1]
    if x >= pts[-1][0]:
        return pts[-1][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= x <= x1:
            return y0 + (y1 - y0) * (x - x0) / ((x1 - x0) or 1)


def month_shift(s):
    """How many observations back is roughly one month for this series."""
    if len(s) < 3:
        return 1
    gaps = [(D(s[i][0]) - D(s[i - 1][0])).days for i in range(max(1, len(s) - 20), len(s))]
    med = statistics.median(gaps)
    return 1 if med >= 25 else 4 if med >= 5 else 21


def trunc(s, month_ago):
    return s[:len(s) - month_shift(s)] if month_ago else s


def yoy(s):
    if not s or len(s) < 13:
        return None
    return (s[-1][1] / s[-13][1] - 1) * 100


def r(x, nd=2):
    return None if x is None else round(x, nd)


def sgn(x, nd=0):
    return None if x is None else f"{x:+.{nd}f}"


# --------------------------------------------------------------------------
# Regime score: five dimensions, 0-100, higher is friendlier to risk
# --------------------------------------------------------------------------
def regime_dims(raw, A, month_ago):
    out = {}

    def pick(*keys):
        for k in keys:
            if raw.get(k):
                return trunc(raw[k], month_ago)
        return None

    def blend(parts):
        parts = [(w, v) for w, v in parts if v is not None]
        tw = sum(w for w, _ in parts)
        return None if not tw else sum(w * v for w, v in parts) / tw

    # Inflation: average core YoY
    ys = [y for y in (yoy(pick("core_pce")), yoy(pick("core_cpi"))) if y is not None]
    if ys:
        x = sum(ys) / len(ys)
        out["inflation"] = (interp(A["inflation"], x), f"Core inflation {x:.2f}% YoY")
    # Employment: Sahm rule + claims trend
    sahm, claims = pick("sahm"), pick("claims")
    sc = interp(A["sahm"], val(sahm)) if sahm else None
    cs, cdet = None, ""
    if claims and len(claims) >= 56:
        vals = [v for _, v in claims]
        ma = [sum(vals[i - 3:i + 1]) / 4 for i in range(3, len(vals))]
        ratio = ma[-1] / min(ma[-52:])
        cs, cdet = interp(A["claims_ratio"], ratio), f"claims {ratio:.2f}x 52w low"
    e = blend([(.6, sc), (.4, cs)])
    if e is not None:
        det = ", ".join(x for x in [f"Sahm {val(sahm):.2f}" if sahm else "", cdet] if x)
        out["employment"] = (e, det)
    # Growth: GDPNow + 3m10y slope
    g = pick("gdpnow")
    cv = pick("t10y3m")
    gs = interp(A["gdpnow"], val(g)) if g else None
    cus = interp(A["curve_3m10y"], val(cv)) if cv else None
    gr = blend([(.7, gs), (.3, cus)])
    if gr is not None:
        det = ", ".join(x for x in [f"GDPNow {val(g):.1f}%" if g else "", f"10Y-3M {val(cv):+.2f}" if cv else ""] if x)
        out["growth"] = (gr, det)
    # Credit
    hy = pick("hy")
    if hy:
        bp = val(hy) * 100
        out["credit"] = (interp(A["hy_bp"], bp), f"HY spread {bp:.0f} bp")
    # Financial conditions: NFCI + VIX
    nf, vx = pick("nfci"), pick("vix", "vix_f")
    ns = interp(A["nfci"], val(nf)) if nf else None
    vs = interp(A["vix"], val(vx)) if vx else None
    fc = blend([(.6, ns), (.4, vs)])
    if fc is not None:
        det = ", ".join(x for x in [f"NFCI {val(nf):+.2f}" if nf else "", f"VIX {val(vx):.1f}" if vx else ""] if x)
        out["financial"] = (fc, det)
    return out


def build_regime(raw, cfg):
    R = cfg["regime"]
    now, prev = regime_dims(raw, R["anchors"], False), regime_dims(raw, R["anchors"], True)
    labels = {"inflation": "Inflation", "employment": "Jobs", "growth": "Growth",
              "credit": "Credit", "financial": "Financial conditions"}
    dims = {}
    for k, name in labels.items():
        if k in now:
            dims[k] = {"name": name, "now": round(now[k][0]), "prev": round(prev[k][0]) if k in prev else None,
                       "detail": now[k][1]}
    if not dims:
        return None
    avg = sum(d["now"] for d in dims.values()) / len(dims)
    pv = [d["prev"] for d in dims.values() if d["prev"] is not None]
    avg_prev = sum(pv) / len(pv) if pv else None
    label = "risk-on" if avg >= R["risk_on_at"] else "risk-off" if avg <= R["risk_off_at"] else "mixed"
    direction = "stable"
    if avg_prev is not None:
        dlt = avg - avg_prev
        direction = "improving" if dlt > R["direction_band"] else "deteriorating" if dlt < -R["direction_band"] else "stable"
    weakest = sorted(dims.values(), key=lambda d: d["now"])[:2]
    return {"avg": round(avg), "avg_prev": None if avg_prev is None else round(avg_prev), "label": label,
            "direction": direction, "dims": dims, "weakest": [w["name"] for w in weakest],
            "spread": max(d["now"] for d in dims.values()) - min(d["now"] for d in dims.values())}


# --------------------------------------------------------------------------
# Triggers
# --------------------------------------------------------------------------
def special_cpi(raw):
    s = raw.get("cpi")
    if not s or len(s) < 15:
        return None
    ys = [(s[i][1] / s[i - 12][1] - 1) * 100 for i in (len(s) - 3, len(s) - 2, len(s) - 1)]
    return {"hit": all(y > 3.5 for y in ys), "near": ys[-1] > 3.2, "text": f"{ys[-1]:.1f}% YoY"}


def special_reinversion(raw):
    """True when 2s10s is below zero after a confirmed un-inversion.

    Confirmed = a run of 20+ negative days followed later by a run of 20+
    non-negative days. Runs are used so noise around zero does not reset it.
    """
    s = raw.get("t10y2y")
    if not s:
        return None
    runs = []
    for _, v in s:
        neg = v < 0
        if runs and runs[-1][0] == neg:
            runs[-1][1] += 1
        else:
            runs.append([neg, 1])
    seen_inv, confirmed = False, False
    for neg, n in runs:
        if neg and n >= 20:
            seen_inv = True
        elif (not neg) and n >= 20 and seen_inv:
            confirmed = True
    last = s[-1][1]
    return {"hit": confirmed and last < 0, "near": confirmed and 0 <= last < 0.15,
            "text": f"{last * 100:+.0f} bp"}


def build_triggers(raw, vals, cfg):
    out = []
    for t in cfg["triggers"]:
        item = {"id": t["id"], "label": t["label"], "why": t["why"], "level": t["status"]}
        if t.get("special") == "cpi_sustained":
            sp = special_cpi(raw)
        elif t.get("special") == "reinversion":
            sp = special_reinversion(raw)
        else:
            sp = None
            v = vals.get(t["val"])
            if v is not None:
                hit = v > t["level"] if t["op"] == ">" else v < t["level"]
                sp = {"hit": hit, "near": (not hit) and abs(v - t["level"]) <= t["near"],
                      "text": t["fmt"].format(v)}
        if sp is None:
            item.update(status="na", near=False, value="n/a")
        else:
            item.update(status=t["status"] if sp["hit"] else "ok", near=bool(sp["near"]), value=sp["text"])
        out.append(item)
    return out


# --------------------------------------------------------------------------
# Rates
# --------------------------------------------------------------------------
def rate_cycle(raw):
    s = raw.get("tar_hi")
    if not s:
        return None
    last_change = None
    for i in range(1, len(s)):
        d = s[i][1] - s[i - 1][1]
        if abs(d) >= 0.2:
            last_change = (s[i][0], 1 if d > 0 else -1, d)
    upper = s[-1][1]
    months = None if not last_change else (TODAY - D(last_change[0])).days / 30.44
    if upper < 0.5:
        phase = "ZERO"
    elif last_change and last_change[1] > 0:
        phase = "HIKING" if months <= 6 else "PEAK"
    elif last_change and last_change[1] < 0:
        phase = "CUTTING" if months <= 12 else "HOLD"
    else:
        phase = "PEAK" if upper >= 3 else "HOLD"
    notes = {
        "HIKING": "Every inflation print is a policy print. Data days own the session.",
        "PEAK": "The argument shifts to timing the first cut.",
        "CUTTING": "Why the Fed is cutting matters more than how much. Check credit and jobs.",
        "HOLD": "Rates are on hold after cuts. Watch for the next change in direction.",
        "ZERO": "Rates are pinned near zero, usually after a crisis.",
    }
    return {"phase": phase, "note": notes[phase], "months_since": r(months, 1),
            "last_change": None if not last_change else {"date": last_change[0], "bp": round(last_change[2] * 100)}}


def build_rates(raw, cfg):
    g = lambda k: raw.get(k)  # noqa: E731
    mats = [("3M", "dgs3mo"), ("2Y", "dgs2"), ("5Y", "dgs5"), ("7Y", "dgs7"),
            ("10Y", "dgs10"), ("20Y", "dgs20"), ("30Y", "dgs30")]
    curve = []
    for lab, k in mats:
        s = g(k)
        if s:
            curve.append({"m": lab, "now": r(val(s), 3), "wk": r(val(s, 5), 3), "mo": r(val(s, 21), 3),
                          "d1": r(None if chg(s) is None else chg(s) * 100, 1)})
    sp = {}
    for key, lab, s in [("s2s10s", "2s10s", g("t10y2y")), ("s3m10y", "10Y minus 3M", g("t10y3m"))]:
        if s:
            sp[key] = {"label": lab, "bp": r(val(s) * 100, 0), "mo_bp": r(None if chg(s, 21) is None else chg(s, 21) * 100, 0),
                       "spark": spark(s, 130)}
    if g("dgs30") and g("dgs10"):
        n = min(len(g("dgs30")), len(g("dgs10")))
        diff = [(a[0], a[1] - b[1]) for a, b in zip(g("dgs30")[-n:], g("dgs10")[-n:])]
        sp["s10s30s"] = {"label": "30Y minus 10Y", "bp": r(diff[-1][1] * 100, 0),
                         "mo_bp": r((diff[-1][1] - diff[-22][1]) * 100, 0) if len(diff) > 22 else None,
                         "spark": spark(diff, 130)}
    # curve movement type over the last month
    move = None
    d2, d10 = chg(g("dgs2"), 21), chg(g("dgs10"), 21)
    if d2 is not None and d10 is not None:
        ds = d10 - d2
        if ds > 0.02:
            move = "Bull steepener" if d2 < 0 else "Bear steepener"
        elif ds < -0.02:
            move = "Bull flattener" if d10 < 0 else "Bear flattener"
        else:
            move = "Little change in slope"
    real = None
    if g("real10"):
        real = {"real": r(val(g("real10")), 2), "real_mo_bp": r(None if chg(g("real10"), 21) is None else chg(g("real10"), 21) * 100, 0),
                "be": r(val(g("be10")), 2) if g("be10") else None, "spark": spark(g("real10"), 130)}
    fed = None
    if g("effr"):
        sofr, up = val(g("sofr")), val(g("tar_hi"))
        fed = {"effr": r(val(g("effr")), 2), "lo": r(val(g("tar_lo")), 2), "hi": r(up, 2),
               "sofr": r(sofr, 2), "iorb": r(val(g("iorb")), 2), "rrp": r(val(g("rrp_rate")), 2),
               "stress": bool(sofr is not None and up is not None and sofr > up + 0.001)}
    cuts = cfg["manual"].get("priced_cuts_12m")
    proxy = False
    if cuts is None and g("effr") and g("dgs2"):
        cuts, proxy = max(0.0, (val(g("effr")) - val(g("dgs2"))) / 0.25), True
    return {"cycle": rate_cycle(raw), "curve": curve, "spreads": sp, "move": move, "real": real, "fed": fed,
            "priced_cuts": None if cuts is None else {"value": r(cuts, 1), "proxy": proxy}}


# --------------------------------------------------------------------------
# Credit
# --------------------------------------------------------------------------
HY_BANDS = [
    (300, "Euphoria", "Neutral to cautious", "Complacency. Protection is cheap. Not a sell signal alone."),
    (450, "Normal", "Neutral or better", "Standard positioning. Credit is not the constraint."),
    (600, "Concern", "Vulnerable", "Treat rallies with suspicion and tighten risk."),
    (800, "Recession", "Often down 20% or more", "Defensive. Yesterday's map may not describe today's market."),
    (10 ** 9, "Crisis", "Bear market", "Fully defensive."),
]


def build_credit(raw, vix_now, vix_chg5):
    hy = raw.get("hy")
    if not hy:
        return None
    bp = val(hy) * 100
    idx = next(i for i, b in enumerate(HY_BANDS) if bp < b[0])
    b = HY_BANDS[idx]
    mo = chg(hy, 21)
    tiers = []
    for lab, k in [("IG", "ig"), ("BBB", "bbb"), ("BB", "bb"), ("B", "b"), ("CCC", "ccc"), ("HY", "hy")]:
        s = raw.get(k)
        if s:
            tiers.append({"tier": lab, "bp": r(val(s) * 100, 0),
                          "mo_bp": r(None if chg(s, 21) is None else chg(s, 21) * 100, 0), "spark": spark(s, 126)})
    note = None
    t = {x["tier"]: x for x in tiers}
    if "CCC" in t and "IG" in t and t["CCC"]["mo_bp"] is not None and t["IG"]["mo_bp"] is not None:
        if t["CCC"]["mo_bp"] >= 60 and t["IG"]["mo_bp"] <= 10:
            note = "CCC is widening while investment grade holds: stress at the weakest borrowers first."
        elif all((x["mo_bp"] or 0) >= 25 for x in tiers):
            note = "Every tier is widening together: broad credit stress."
    vs_vix = None
    if mo is not None and vix_now is not None:
        if mo * 100 >= 50 and vix_now < 18:
            vs_vix = "Credit is widening while VIX is calm. Trust credit."
        elif vix_chg5 is not None and vix_chg5 >= 5 and mo * 100 < 10:
            vs_vix = "VIX spiked but credit barely moved. The fear is probably short-term."
    return {"bp": round(bp), "band": b[1], "band_idx": idx, "equity": b[2], "action": b[3],
            "mo_bp": r(None if mo is None else mo * 100, 0), "d5_bp": r(None if chg(hy, 5) is None else chg(hy, 5) * 100, 0),
            "tiers": tiers, "note": note, "vs_vix": vs_vix, "spark": spark(hy, 252),
            "dates": [d for d, _ in hy[-252:]],
            "bands": [{"max": x[0] if x[0] < 10 ** 8 else None, "name": x[1]} for x in HY_BANDS]}


# --------------------------------------------------------------------------
# Volatility
# --------------------------------------------------------------------------
def rv_map(closes, win):
    rets = [math.log(closes[i][1] / closes[i - 1][1]) for i in range(1, len(closes))]
    out = {}
    for i in range(win - 1, len(rets)):
        out[closes[i + 1][0]] = statistics.stdev(rets[i - win + 1:i + 1]) * math.sqrt(252) * 100
    return out


def pctile(xs, v):
    return 100.0 * sum(1 for x in xs if x <= v) / len(xs)


def quant(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * (len(xs) - 1) + .5))]


def build_vol(raw, cfg):
    vix = raw.get("vix") or raw.get("vix_f")
    if not vix:
        return None
    v30 = val(vix)
    v9 = val(raw.get("vix9d"))
    v3m = val(raw.get("vix3m") or raw.get("vix3m_f"))
    v6 = val(raw.get("vix6m"))
    pts = [(lab, v) for lab, v in [("9D", v9), ("30D", v30), ("3M", v3m), ("6M", v6)] if v is not None]
    front = v9 if v9 is not None else v30
    state, slope = "unknown", None
    if v3m is not None:
        slope = v3m - v30
        if front > v3m:
            state = "backwardation"
        elif slope < cfg["vix_curve"]["contango_min_slope"]:
            state = "flat"
        else:
            state = "contango"
    level = ("calm" if v30 < 15 else "normal" if v30 < 20 else "elevated" if v30 < 30 else "stress")
    d5 = chg(vix, 5)
    # VRP at three tenors
    vrp, cone = {}, {}
    spx = raw.get("spx")
    if spx and len(spx) > 80:
        for tkey, ivkey, win in [("w1", "vix9d", 5), ("m1", "vix", 21), ("m3", "vix3m", 63)]:
            iv = raw.get(ivkey)
            if not iv:
                continue
            rvm = rv_map(spx, win)
            ivm = dict(iv)
            series = [(d, ivm[d] - rv) for d, rv in sorted(rvm.items()) if d in ivm]
            if len(series) > 60:
                cur = series[-1][1]
                xs = [x for _, x in series]
                vrp[tkey] = {"vrp": r(cur, 2), "iv": r(ivm[series[-1][0]], 2), "rv": r(rvm[series[-1][0]], 2)}
                if tkey == "m1":
                    cone = {"pct": round(pctile(xs, cur)), "p10": r(quant(xs, .1), 2), "p25": r(quant(xs, .25), 2),
                            "p50": r(quant(xs, .5), 2), "p75": r(quant(xs, .75), 2), "p90": r(quant(xs, .9), 2),
                            "n": len(xs), "spark": spark(series, 90)}
    regime = None
    if "m1" in vrp:
        m1 = vrp["m1"]["vrp"]
        regime = ("Stress: the market is moving more than options priced." if m1 < 0 else
                  "Options are rich against realized movement." if m1 > 5 else "Ordinary premium.")
    event = bool("w1" in vrp and "m1" in vrp and vrp["w1"]["vrp"] - vrp["m1"]["vrp"] >= 4)
    return {"vix": r(v30, 2), "d1": r(chg(vix), 2), "d5": r(d5, 2), "level": level, "state": state,
            "slope": r(slope, 2), "curve": [{"t": a, "v": r(b, 2)} for a, b in pts],
            "vvix": r(val(raw.get("vvix")), 1), "spark": spark(vix, 120), "vrp": vrp, "cone": cone,
            "vrp_read": regime, "event_priced": event}


# --------------------------------------------------------------------------
# Oil and dollar
# --------------------------------------------------------------------------
def build_energy(raw, wti_s):
    if not wti_s:
        return None
    w = val(wti_s)
    scenario = "supply_shock" if w > 110 else "demand_collapse" if w < 70 else "none"
    return {"wti": r(w, 2), "d1_pct": r(pchg(wti_s), 2), "d5_pct": r(pchg(wti_s, 5), 2),
            "brent": r(val(raw.get("brent") or raw.get("brent_f")), 2), "scenario": scenario,
            "to_110": r(110 - w, 1), "to_70": r(w - 70, 1), "spark": spark(wti_s, 120),
            "playbooks": {
                "supply_shock": {"sell": ["Airlines", "Consumer discretionary", "Transports"], "hold": ["Energy (XLE)", "Defensives"]},
                "demand_collapse": {"sell": ["Energy (XLE)", "Oil services"], "buy": ["Airlines", "Consumer discretionary", "Transports"]}}}


def build_dollar(dxy_s):
    if not dxy_s:
        return None
    v = val(dxy_s)
    return {"dxy": r(v, 2), "d1_pct": r(pchg(dxy_s), 2), "d5_pct": r(pchg(dxy_s, 5), 2),
            "above_108": v > 108, "to_108": r(108 - v, 2), "spark": spark(dxy_s, 120)}


# --------------------------------------------------------------------------
# Four-market check and instruments
# --------------------------------------------------------------------------
def four_market(raw, vals, vol, credit, cfg):
    T = cfg["four_market"]
    rows = []
    y = chg(raw.get("dgs10"))
    if y is not None:
        bp = y * 100
        st = "stress" if bp >= T["yield_jump_bp"] or bp <= T["yield_collapse_bp"] else "support" if bp <= 3 else "neutral"
        why = {"support": "Yields steady or falling.", "neutral": "Yields drifting up.",
               "stress": "Yields jumping." if bp > 0 else "Yields collapsing: a growth scare."}[st]
        rows.append({"id": "yields", "label": "Bonds (10Y)", "text": f"{bp:+.1f} bp", "state": st, "why": why})
    dx = pchg(raw.get("dxy"))
    if dx is not None:
        st = "stress" if dx >= T["dollar_stress_pct"] else "support" if dx <= T["dollar_flat_pct"] else "neutral"
        why = {"support": "Dollar flat or weaker.", "neutral": "Dollar firming.", "stress": "Dollar rising: money seeking safety."}[st]
        rows.append({"id": "dollar", "label": "Dollar (DXY)", "text": f"{dx:+.2f}%", "state": st, "why": why})
    if vol:
        up = vol["d1"] is not None and vol["d1"] >= T["vix_up_pts"]
        if vol["state"] == "backwardation" or up:
            st, why = "stress", "VIX rising or the curve is inverted."
        elif (vol["d1"] or 0) <= 0 and vol["state"] in ("contango", "unknown"):
            st, why = "support", "VIX drifting lower, curve in contango."
        else:
            st, why = "neutral", "VIX steady, curve flattening." if vol["state"] == "flat" else "VIX steady."
        rows.append({"id": "vol", "label": "Volatility (VIX)", "text": f"{vol['vix']:.1f} ({vol['d1'] or 0:+.1f})", "state": st, "why": why})
    if credit and credit.get("d5_bp") is not None:
        d = credit["d5_bp"]
        st = "stress" if d >= T["credit_stress_bp"] else "support" if d <= T["credit_ok_bp"] else "neutral"
        why = {"support": "Spreads tight or tightening.", "neutral": "Spreads edging wider.", "stress": "Spreads widening."}[st]
        rows.append({"id": "credit", "label": "Credit (HY)", "text": f"{credit['bp']} bp ({d:+.0f} 5d)", "state": st, "why": why})
    stress = sum(1 for x in rows if x["state"] == "stress")
    support = sum(1 for x in rows if x["state"] == "support")
    if len(rows) < 3:
        verdict, vtxt = "unknown", "Not enough markets available."
    elif support == len(rows):
        verdict, vtxt = "confirms", "All markets back the move. Hold runners longer and do not fade it."
    elif stress >= 3:
        verdict, vtxt = "stress", "Stress confirmed across markets. Do not fight a four-market move."
    elif stress >= 2:
        verdict, vtxt = "diverging", "Two or more markets flag stress. Somebody is wrong, often the stock market."
    elif stress == 0 and support >= 2:
        verdict, vtxt = "mostly_confirms", "Mostly confirming. No stress flags."
    else:
        verdict, vtxt = "mixed", "Mixed signals. Wait for the picture to resolve."
    return {"rows": rows, "verdict": verdict, "text": vtxt, "stress": stress, "support": support}


def build_instruments(raw, cfg, fm, vol, credit, rates, dollar):
    out = []
    rate_state = "neutral"
    d10 = chg(raw.get("dgs10"), 5)
    rl = rates["real"]["real_mo_bp"] if rates.get("real") else None
    y10 = val(raw.get("dgs10"))
    if (rl is not None and rl >= 15) or (d10 is not None and d10 * 100 >= 10) or (y10 is not None and y10 > 4.5):
        rate_state = "headwind"
    elif d10 is not None and d10 * 100 <= -10:
        rate_state = "tailwind"
    dol_state = "neutral"
    if dollar:
        dol_state = "headwind" if (dollar["d5_pct"] or 0) >= 1 else "tailwind" if (dollar["d5_pct"] or 0) <= -1 else "neutral"
    cr_state = "neutral"
    if credit:
        mo = credit["mo_bp"] or 0
        cr_state = "headwind" if mo >= 40 or credit["band_idx"] >= 2 else "tailwind" if mo <= -20 else "neutral"
    vol_state = "neutral"
    if vol:
        if vol["state"] == "backwardation" or vol["vix"] >= 25 or (vol["d5"] or 0) >= 3:
            vol_state = "headwind"
        elif vol["state"] == "contango" and vol["vix"] < 16:
            vol_state = "tailwind"
    states = {"rates": rate_state, "dollar": dol_state, "credit": cr_state, "vol": vol_state}
    names = {"rates": "Rates", "dollar": "Dollar", "credit": "Credit", "vol": "Volatility"}
    for inst in cfg["instruments"]:
        s = raw.get("inst_" + inst["id"])
        if not s:
            out.append({"id": inst["id"], "name": inst["name"], "price": None, "note": inst.get("note", "")})
            continue
        tilt, lens = 0.0, []
        for f, st in states.items():
            w = inst["sens"].get(f, 0.5)
            tilt += w * (-1 if st == "headwind" else 1 if st == "tailwind" else 0)
            lens.append({"factor": names[f], "state": st, "weight": w})
        c1 = pchg(s)
        warn = None
        if fm["verdict"] != "unknown" and c1 is not None:
            if c1 >= 0.5 and fm["stress"] >= 2:
                warn = "Rallying while other markets flag stress. Do not chase it."
            elif c1 <= -0.5 and fm["stress"] == 0:
                warn = "Selling off without macro stress. Dips often get bought."
        out.append({"id": inst["id"], "name": inst["name"], "note": inst.get("note", ""), "price": r(val(s), 2),
                    "d1_pct": r(c1, 2), "d5_pct": r(pchg(s, 5), 2), "spark": spark(s, 60), "lens": lens,
                    "tilt": r(tilt, 2),
                    "tilt_label": "Headwind" if tilt <= -1 else "Tailwind" if tilt >= 1 else "Neutral", "warn": warn})
    pairs = []
    byid = {i["id"]: i for i in out}
    for a, b in cfg.get("pairs", []):
        if a in byid and b in byid and byid[a].get("d1_pct") is not None and byid[b].get("d1_pct") is not None:
            pairs.append({"a": a, "b": b, "d1": r(byid[a]["d1_pct"] - byid[b]["d1_pct"], 2),
                          "d5": r((byid[a]["d5_pct"] or 0) - (byid[b]["d5_pct"] or 0), 2)})
    return out, pairs


# --------------------------------------------------------------------------
# Posture
# --------------------------------------------------------------------------
GUIDE = {
    "Normal": "Full size on A setups. Trust the walls and fade the edges in positive gamma.",
    "Elevated": "Reduce size. Only take setups where structure, options and order flow all line up, and respect breakdowns more.",
    "Critical": "Much smaller size. Do not fade moves. Trade only the confirmed direction, and sitting out is fine.",
}


def posture(regime, triggers, vol):
    alerts = [t for t in triggers if t["status"] == "alert"]
    crises = [t for t in triggers if t["status"] == "crisis"]
    watches = [t for t in triggers if t["status"] == "watch"]
    crit, elev = [], []
    if regime:
        if regime["label"] == "risk-off" and regime["direction"] == "deteriorating":
            crit.append("Regime is risk-off and deteriorating")
        if regime["label"] == "risk-off":
            elev.append("Regime is risk-off")
        elif regime["direction"] == "deteriorating":
            elev.append("Regime is deteriorating")
    if len(alerts) >= 2:
        crit.append(f"{len(alerts)} alert triggers are firing")
    elif len(alerts) == 1:
        elev.append(f"Alert: {alerts[0]['label']}")
    for c in crises:
        crit.append(f"Crisis: {c['label']}")
    if len(watches) >= 3:
        elev.append(f"{len(watches)} watch triggers are firing")
    if vol:
        if vol["state"] == "backwardation":
            crit.append("VIX curve is in backwardation")
        elif vol["state"] == "flat" or (vol["d5"] is not None and vol["d5"] >= 2):
            elev.append("VIX is rising or the curve is flattening")
    level = "Critical" if crit else "Elevated" if elev else "Normal"
    reasons = crit + elev if level != "Normal" else ["Regime stable or improving, no alert triggers, VIX curve in contango"]
    return {"level": level, "reasons": reasons, "guidance": GUIDE[level],
            "counts": {"alert": len(alerts), "watch": len(watches), "crisis": len(crises)}}


# --------------------------------------------------------------------------
# Tiles and history
# --------------------------------------------------------------------------
def build_tiles(raw, vals, rates, wti_s, dxy_s):
    t = []
    fed = rates.get("fed")
    if fed:
        t.append({"id": "ffr", "label": "Fed funds", "value": f"{fed['lo']:.2f} to {fed['hi']:.2f}%", "sub": f"Effective {fed['effr']:.2f}%"})
    if raw.get("dgs10"):
        c = chg(raw["dgs10"])
        t.append({"id": "y10", "label": "10Y yield", "value": f"{val(raw['dgs10']):.2f}%", "chg": r(None if c is None else c * 100, 1), "unit": "bp",
                  "sub": "Watch 4.50% and 4.00%"})
    for k, lab in [("cpi", "CPI YoY"), ("pce", "PCE YoY")]:
        y = yoy(raw.get(k))
        if y is not None:
            t.append({"id": k, "label": lab, "value": f"{y:.1f}%", "sub": "Fed target is 2%"})
    if raw.get("unrate"):
        t.append({"id": "u3", "label": "Unemployment", "value": f"{val(raw['unrate']):.1f}%", "sub": "U-3"})
    if raw.get("hy"):
        c = chg(raw["hy"])
        t.append({"id": "hy", "label": "HY spread", "value": f"{val(raw['hy']) * 100:.0f} bp", "chg": r(None if c is None else c * 100, 0), "unit": "bp", "sub": "Alert above 500"})
    if wti_s:
        t.append({"id": "wti", "label": "WTI crude", "value": f"${val(wti_s):.2f}", "chg": r(pchg(wti_s), 2), "unit": "%", "sub": "Levels: $70 and $110"})
    if dxy_s:
        t.append({"id": "dxy", "label": "Dollar index", "value": f"{val(dxy_s):.2f}", "chg": r(pchg(dxy_s), 2), "unit": "%", "sub": "Watch 108"})
    return t


def append_history(row):
    HIST.parent.mkdir(parents=True, exist_ok=True)
    rows, header = [], list(row.keys())
    if HIST.exists():
        with HIST.open() as f:
            rd = csv.DictReader(f)
            rows = [x for x in rd if x["date"] != row["date"]]
    rows.append({k: ("" if v is None else v) for k, v in row.items()})
    with HIST.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        for x in rows:
            w.writerow({k: x.get(k, "") for k in header})


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", nargs="?", const="calm", choices=["calm", "stress"],
                    help="synthetic data, no internet (calm or stress)")
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--no-history", action="store_true")
    a = ap.parse_args()
    cfg = json.loads(CONFIG.read_text())
    raw, errors = load_demo(cfg, a.demo) if a.demo else load_live(cfg)
    if not a.demo and not any(k in raw for k in ("dgs10", "hy")):
        print("No usable FRED data. Leaving the previous data.json untouched.", file=sys.stderr)
        for e in errors[:10]:
            print(" ", e, file=sys.stderr)
        sys.exit(1)

    vix_s = raw.get("vix") or raw.get("vix_f")
    wti_s = raw.get("wti") or raw.get("wti_f")
    dxy_s = raw.get("dxy")
    vals = {"dgs10": val(raw.get("dgs10")), "hy_bp": None if not raw.get("hy") else val(raw["hy"]) * 100,
            "wti": val(wti_s), "vix": val(vix_s), "dxy": val(dxy_s)}

    regime = build_regime(raw, cfg)
    triggers = build_triggers(raw, vals, cfg)
    vol = build_vol(raw, cfg)
    credit = build_credit(raw, val(vix_s), chg(vix_s, 5))
    rates = build_rates(raw, cfg)
    energy = build_energy(raw, wti_s)
    dollar = build_dollar(dxy_s)
    fm = four_market(raw, vals, vol, credit, cfg)
    instruments, pairs = build_instruments(raw, cfg, fm, vol, credit, rates, dollar)
    post = posture(regime, triggers, vol)
    tiles = build_tiles(raw, vals, rates, wti_s, dxy_s)

    latest = max((s[-1][0] for k, s in raw.items() if k in ("dgs10", "hy", "vix", "dxy") and s), default=TODAY.isoformat())
    out = {
        "meta": {"mode": "demo" if a.demo else "live", "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                 "as_of": latest, "errors": errors, "series_loaded": len(raw)},
        "posture": post, "regime": regime, "triggers": triggers, "tiles": tiles, "four_market": fm,
        "instruments": instruments, "pairs": pairs, "rates": rates, "credit": credit, "vol": vol,
        "energy": energy, "dollar": dollar,
    }
    outp = Path(a.out)
    outp.parent.mkdir(parents=True, exist_ok=True)
    outp.write_text(json.dumps(out, separators=(",", ":"), allow_nan=False))
    print(f"Wrote {outp} ({outp.stat().st_size // 1024} KB), mode={out['meta']['mode']}, posture={post['level']}, "
          f"errors={len(errors)}")
    if not a.demo and not a.no_history:
        append_history({"date": latest, "posture": post["level"], "regime_avg": regime and regime["avg"],
                        "regime": regime and regime["label"], "vix": vals["vix"], "hy_bp": vals["hy_bp"],
                        "us10y": vals["dgs10"], "wti": vals["wti"], "dxy": vals["dxy"],
                        "alerts": post["counts"]["alert"], "watches": post["counts"]["watch"]})


if __name__ == "__main__":
    main()
