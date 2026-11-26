"""Weekly panels, calendar, positioning, history and the daily regime tag.

Everything here uses only the standard library and the same raw series that
fetch_data.py already loads. fetch_data.py calls build_all() and, in demo mode,
demo_raw() to add synthetic series.
"""
import bisect
import csv
import datetime as dt
import json
import statistics
import urllib.parse
from collections import Counter
from pathlib import Path


# --------------------------------------------------------------------------
# Tiny helpers
# --------------------------------------------------------------------------
def D(s):
    return dt.date.fromisoformat(s)


def val(s, back=0):
    if not s or len(s) <= back:
        return None
    return s[-1 - back][1]


def r(x, nd=2):
    return None if x is None else round(x, nd)


def pct(a, b):
    return None if a is None or not b else (a / b - 1) * 100


def corr(x, y):
    try:
        return statistics.correlation(x, y)
    except (statistics.StatisticsError, ZeroDivisionError):
        return None


def at_or_before(s, day):
    ds = [d for d, _ in s]
    i = bisect.bisect_right(ds, day) - 1
    return s[i][1] if i >= 0 else None


def yoy_series(s):
    return [(s[i][0], (s[i][1] / s[i - 12][1] - 1) * 100) for i in range(12, len(s))]


def pack(s, n):
    s = s[-n:]
    return {"d": [d for d, _ in s], "v": [round(v, 4) for _, v in s]}


# --------------------------------------------------------------------------
# Labor and inflation
# --------------------------------------------------------------------------
def build_labor(raw):
    un, cl, sa, pr = raw.get("unrate"), raw.get("claims"), raw.get("sahm"), raw.get("payems")
    if not (un or cl or sa or pr):
        return None
    tiles, flags = [], []
    if un:
        lo12 = min(v for _, v in un[-12:])
        up = val(un) - lo12
        tiles.append({"label": "Unemployment", "value": f"{val(un):.1f}%", "sub": f"{up:+.1f} pt above its 12-month low",
                      "state": "alert" if up >= 0.5 else "watch" if up >= 0.3 else "ok"})
        if up >= 0.3:
            flags.append("Unemployment is climbing")
    if sa:
        v = val(sa)
        tiles.append({"label": "Sahm rule", "value": f"{v:.2f}", "sub": "Recession signal at 0.50",
                      "state": "alert" if v >= 0.5 else "watch" if v >= 0.3 else "ok"})
        if v >= 0.3:
            flags.append("Sahm rule is rising")
    if cl:
        ma4 = sum(v for _, v in cl[-4:]) / 4
        low = min(v for _, v in cl[-52:])
        ratio = ma4 / low
        tiles.append({"label": "Jobless claims, 4-week", "value": f"{ma4 / 1000:.0f}k", "sub": f"{ratio:.2f}x the 52-week low",
                      "state": "alert" if ratio >= 1.35 else "watch" if ratio >= 1.2 else "ok"})
        if ratio >= 1.2:
            flags.append("Claims are lifting off their lows")
    pay = None
    if pr and len(pr) > 4:
        diffs = [(pr[i][0], pr[i][1] - pr[i - 1][1]) for i in range(1, len(pr))]
        avg3 = sum(v for _, v in diffs[-3:]) / 3
        pay = diffs
        tiles.append({"label": "Payrolls, 3-month avg", "value": f"{avg3:+.0f}k", "sub": "Jobs added per month",
                      "state": "alert" if avg3 < 0 else "watch" if avg3 < 75 else "ok"})
        if avg3 < 75:
            flags.append("Hiring is slowing")
    state = "cracking" if any(t["state"] == "alert" for t in tiles) else "softening" if flags else "healthy"
    read = {"healthy": "Labor market is holding. Bad news is not coming from jobs.",
            "softening": "Labor is softening. If the story turns to growth fear, this data gets promoted to Tier 1.",
            "cracking": "Labor is cracking. Expect weak data to matter more than strong data."}[state]
    return {"state": state, "read": read, "flags": flags, "tiles": tiles,
            "unrate": pack(un, 48) if un else None, "claims": pack(cl, 104) if cl else None,
            "payrolls": pack(pay, 24) if pay else None}


def build_inflation(raw):
    out = {"tiles": [], "series": {}}
    names = [("cpi", "CPI"), ("core_cpi", "Core CPI"), ("core_pce", "Core PCE"), ("pce", "PCE")]
    for k, lab in names:
        s = raw.get(k)
        if not s or len(s) < 14:
            continue
        ys = yoy_series(s)
        now = ys[-1][1]
        prev = ys[-4][1] if len(ys) > 4 else now
        ann3 = ((s[-1][1] / s[-4][1]) ** 4 - 1) * 100
        out["tiles"].append({"id": k, "label": lab + " YoY", "value": f"{now:.1f}%",
                             "sub": f"3-month pace {ann3:.1f}%", "trend": "rising" if now - prev >= 0.2 else "falling" if now - prev <= -0.2 else "flat",
                             "pace": r(ann3, 1), "yoy": r(now, 2)})
        if k in ("cpi", "core_cpi", "core_pce"):
            out["series"][k] = pack(ys, 36)
    core = next((t for t in out["tiles"] if t["id"] == "core_cpi"), None) or next((t for t in out["tiles"] if t["id"] == "core_pce"), None)
    if not core:
        return out if out["tiles"] else None
    hot = core["pace"] - core["yoy"] >= 0.3
    cool = core["yoy"] - core["pace"] >= 0.3
    out["read"] = ("Core inflation is re-accelerating: the recent pace is above the yearly rate." if hot else
                   "Core inflation is cooling: the recent pace is below the yearly rate." if cool else
                   "Core inflation pace matches the yearly rate. No change in the story.")
    out["state"] = "hot" if hot or core["yoy"] >= 3.5 else "cool" if cool else "steady"
    if raw.get("be10"):
        out["breakeven"] = r(val(raw["be10"]), 2)
    if raw.get("real10"):
        out["real_yield"] = r(val(raw["real10"]), 2)
    return out


# --------------------------------------------------------------------------
# Net liquidity = Fed balance sheet - Treasury cash account - reverse repo
# --------------------------------------------------------------------------
def build_liquidity(raw):
    w, t, rr = raw.get("walcl"), raw.get("tga"), raw.get("rrp_on")
    if not (w and t):
        return None
    pts = []
    for d, v in w:
        tg = at_or_before(t, d)
        rp = at_or_before(rr, d) * 1000 if rr else 0.0  # billions to millions
        if tg is not None and rp is not None:
            pts.append((d, (v - tg - rp) / 1e6))  # trillions
    if len(pts) < 14:
        return None
    now = pts[-1][1]
    c4, c13 = now - pts[-5][1], now - pts[-14][1]
    direction = "improving" if c4 >= 0.05 else "tightening" if c4 <= -0.05 else "flat"
    read = {"improving": "Liquidity is rising. A supportive tailwind for risk assets, especially NQ.",
            "tightening": "Liquidity is draining. A headwind that tends to show up in long-duration stocks first.",
            "flat": "Liquidity is flat. Not the driver right now."}[direction]
    return {"net_tn": r(now, 3), "chg4_bn": r(c4 * 1000, 0), "chg13_bn": r(c13 * 1000, 0), "direction": direction, "read": read,
            "fed_tn": r(w[-1][1] / 1e6, 2), "tga_bn": r(t[-1][1] / 1e3, 0), "rrp_bn": r(rr[-1][1], 0) if rr else None,
            "series": pack(pts, 104)}


# --------------------------------------------------------------------------
# Correlations
# --------------------------------------------------------------------------
def returns(s, diff=False):
    out = []
    for i in range(1, len(s)):
        a, b = s[i][1], s[i - 1][1]
        if diff:
            out.append((s[i][0], a - b))
        elif b:
            out.append((s[i][0], a / b - 1))
    return out


def build_correlations(raw, cfg):
    cc = cfg.get("correlation")
    if not cc:
        return None
    W, B, bd = cc["window"], cc["baseline"], cc["break_delta"]
    rows = []
    for p in cc["pairs"]:
        a, b = raw.get(p["a"]), raw.get(p["b"])
        if not a or not b:
            continue
        ra, rb = dict(returns(a)), dict(returns(b, p.get("b_diff")))
        days = sorted(set(ra) & set(rb))
        if len(days) < W + 30:
            continue
        xa, xb = [ra[d] for d in days], [rb[d] for d in days]
        cur, base = corr(xa[-W:], xb[-W:]), corr(xa[-B:], xb[-B:])
        if cur is None or base is None:
            continue
        roll = [corr(xa[i - W:i], xb[i - W:i]) for i in range(max(W, len(days) - 150), len(days) + 1, 5)]
        roll = [round(x, 3) for x in roll if x is not None]
        delta = cur - base
        flip = cur * base < 0 and abs(cur) >= 0.25 and abs(base) >= 0.25
        status = "break" if abs(delta) >= bd or flip else "watch" if abs(delta) >= bd * 0.6 else "normal"
        note = ""
        if p["id"] == "spx_tlt" and cur > 0.2:
            note = "Positive: stocks and bonds fall together. Inflation and rates are the main fear."
        rows.append({"id": p["id"], "label": p["label"], "now": r(cur, 2), "base": r(base, 2), "delta": r(delta, 2),
                     "status": status, "note": note, "spark": roll})
    if not rows:
        return None
    breaks = [x for x in rows if x["status"] == "break"]
    read = ("Relationships are breaking. Volatility of volatility tends to rise. Reduce size and do not sell premium."
            if len(breaks) >= 2 else
            "One relationship is changing. Find out why before trusting the other readings." if breaks else
            "Relationships are behaving as usual.")
    return {"rows": rows, "breaks": len(breaks), "read": read, "window": W, "baseline": B}


# --------------------------------------------------------------------------
# Sector rotation, factors, and the rotation guide
# --------------------------------------------------------------------------
# For each macro condition: sectors that should outperform / underperform (lesson 22 rotation guide)
GUIDE = {
    "rates_up": {"label": "Rates rising", "out": ["XLF", "XLE", "XLI"], "under": ["XLU", "XLRE", "XLK"]},
    "rates_dn": {"label": "Rates falling", "out": ["XLU", "XLRE"], "under": ["XLF", "XLE"]},
    "credit_wide": {"label": "Credit widening", "out": ["XLP", "XLU", "XLV"], "under": ["XLY"]},
    "credit_tight": {"label": "Credit tightening", "out": ["XLY"], "under": ["XLU", "XLP"]},
    "oil_up": {"label": "Oil spiking", "out": ["XLE"], "under": ["XLY"]},
}


def build_rotation(raw, cfg):
    spy = raw.get("e_SPY")
    secs = cfg.get("sectors", {})
    rows = []
    for tk, name in secs.items():
        s = raw.get("s_" + tk)
        if not s or len(s) < 64:
            continue
        row = {"t": tk, "name": name}
        for lab, n in (("w1", 5), ("m1", 21), ("m3", 63)):
            row[lab] = r(pct(val(s), val(s, n)), 2)
            if spy and len(spy) > n:
                row[lab + "_rel"] = r(pct(val(s), val(s, n)) - pct(val(spy), val(spy, n)), 2)
        rows.append(row)
    if len(rows) < 6:
        return None
    rows.sort(key=lambda x: x.get("m1_rel", x["m1"]), reverse=True)
    rank = {x["t"]: i for i, x in enumerate(rows)}
    half = len(rows) / 2
    # Macro conditions from the last month of data
    conds = []

    def d21(k, pc=False):
        s = raw.get(k)
        if not s or len(s) < 22:
            return None
        return pct(val(s), val(s, 21)) if pc else val(s) - val(s, 21)

    y, hy, wti = d21("dgs10"), d21("hy"), d21("wti", True)
    if y is not None and y >= 0.15:
        conds.append("rates_up")
    if y is not None and y <= -0.15:
        conds.append("rates_dn")
    if hy is not None and hy >= 0.25:
        conds.append("credit_wide")
    if hy is not None and hy <= -0.25:
        conds.append("credit_tight")
    if wti is not None and wti >= 10:
        conds.append("oil_up")
    checks = []
    for c in conds:
        g = GUIDE[c]
        okc = [t for t in g["out"] if t in rank and rank[t] < half]
        oku = [t for t in g["under"] if t in rank and rank[t] >= half]
        total = len([t for t in g["out"] if t in rank]) + len([t for t in g["under"] if t in rank])
        hit = len(okc) + len(oku)
        checks.append({"cond": g["label"], "expect_out": g["out"], "expect_under": g["under"],
                       "match": hit / total if total else None, "hit": hit, "total": total})
    scored = [c for c in checks if c["match"] is not None]
    if scored:
        avg = sum(c["match"] for c in scored) / len(scored)
        verdict = "matches" if avg >= 0.6 else "disagrees" if avg <= 0.4 else "mixed"
    else:
        verdict = "quiet"
    read = {"matches": "Rotation agrees with the macro story. The market is voting the same way.",
            "disagrees": "Rotation disagrees with the macro story. The lesson says trust the rotation.",
            "mixed": "Rotation only partly matches the macro story. Wait for a clearer pattern.",
            "quiet": "No strong macro condition this month, so there is no specific rotation to expect."}[verdict]
    ratios = []
    for rc in cfg.get("ratios", []):
        a, b = raw.get("s_" + rc["num"]) or raw.get("e_" + rc["num"]), raw.get("s_" + rc["den"]) or raw.get("e_" + rc["den"])
        if not a or not b:
            continue
        da, db = dict(a), dict(b)
        days = sorted(set(da) & set(db))
        if len(days) < 64:
            continue
        rs = [da[d] / db[d] for d in days]
        ratios.append({"id": rc["id"], "label": rc["label"], "read": rc["read"], "m1": r(pct(rs[-1], rs[-22]), 2),
                       "m3": r(pct(rs[-1], rs[-64]), 2), "spark": [round(x / rs[-64] * 100, 2) for x in rs[-64:]]})
    nq, es = raw.get("inst_NQ"), raw.get("inst_ES")
    nqes = None
    if nq and es and len(nq) > 22 and len(es) > 22:
        nqes = {"w1": r(pct(val(nq), val(nq, 5)) - pct(val(es), val(es, 5)), 2),
                "m1": r(pct(val(nq), val(nq, 21)) - pct(val(es), val(es, 21)), 2)}
        nqes["read"] = ("NQ is leading ES: growth and duration are favoured, risk appetite is on." if nqes["w1"] > 0.3 else
                        "ES is leading NQ: investors are leaning defensive or rates are weighing on growth." if nqes["w1"] < -0.3 else
                        "NQ and ES are moving together. No rotation signal.")
    return {"sectors": rows, "leaders": [x["name"] for x in rows[:2]], "laggards": [x["name"] for x in rows[-2:]],
            "checks": checks, "verdict": verdict, "read": read, "ratios": ratios, "nq_es": nqes}


# --------------------------------------------------------------------------
# CFTC positioning (Commitments of Traders, financial futures report)
# --------------------------------------------------------------------------
def _pick(row, prefix):
    for k in row:
        if k.startswith(prefix):
            try:
                return int(float(row[k]))
            except (TypeError, ValueError):
                return None
    return None


def fetch_cot(cfg, http_get, errors):
    cc = cfg.get("cot") or {}
    if not cc.get("enabled"):
        return None
    base = f"https://publicreporting.cftc.gov/resource/{cc['dataset']}.json"
    out = {}
    for inst, pat in cc["markets"].items():
        try:
            q = urllib.parse.urlencode({"$where": f"market_and_exchange_names like '{pat}'",
                                        "$order": "report_date_as_yyyy_mm_dd DESC", "$limit": str(cc["weeks"] * 3)})
            rows = json.loads(http_get(base + "?" + q))
            if not rows:
                raise RuntimeError("no rows")
            name = Counter(x["market_and_exchange_names"] for x in rows).most_common(1)[0][0]
            rows = sorted([x for x in rows if x["market_and_exchange_names"] == name], key=lambda x: x["report_date_as_yyyy_mm_dd"])
            rows = rows[-cc["weeks"]:]
            lev, am = [], []
            for x in rows:
                day = x["report_date_as_yyyy_mm_dd"][:10]
                ll, ls = _pick(x, "lev_money_positions_long"), _pick(x, "lev_money_positions_short")
                al, as_ = _pick(x, "asset_mgr_positions_long"), _pick(x, "asset_mgr_positions_short")
                if None not in (ll, ls):
                    lev.append((day, ll - ls))
                if None not in (al, as_):
                    am.append((day, al - as_))
            if not lev:
                raise RuntimeError("leveraged-fund columns not found")
            out[inst] = {"market": name, "lev": lev, "am": am}
        except Exception as e:  # noqa: BLE001
            errors.append(f"CFTC {inst}: {e}")
    return out or None


def cot_index(s):
    vals = [v for _, v in s]
    lo, hi = min(vals), max(vals)
    return 50.0 if hi == lo else (vals[-1] - lo) / (hi - lo) * 100


def build_positioning(cot):
    if not cot:
        return None
    res = {}
    for inst, c in cot.items():
        lev, am = c["lev"], c.get("am") or []
        idx = cot_index(lev)
        flag = "crowded long" if idx >= 90 else "crowded short" if idx <= 10 else None
        res[inst] = {"market": c["market"], "as_of": lev[-1][0], "lev_net": lev[-1][1], "lev_chg_1w": lev[-1][1] - lev[-2][1] if len(lev) > 1 else None,
                     "lev_index": r(idx, 0), "am_net": am[-1][1] if am else None, "am_index": r(cot_index(am), 0) if am else None,
                     "flag": flag, "weeks": len(lev), "spark": [v for _, v in lev[-52:]],
                     "read": ("Leveraged funds are at an extreme long. One more buyer is hard to find, so downside gaps get bigger." if flag == "crowded long" else
                              "Leveraged funds are at an extreme short. A squeeze higher is possible if news turns." if flag == "crowded short" else
                              "Positioning is not at an extreme.")}
    return res


# --------------------------------------------------------------------------
# Economic calendar
# --------------------------------------------------------------------------
def second_saturday_before(start):
    s1 = start - dt.timedelta(days=(start.weekday() - 5) % 7 or 7)
    return s1 - dt.timedelta(days=7)


def nth_weekday(y, m, wd, n):
    d = dt.date(y, m, 1)
    d += dt.timedelta(days=(wd - d.weekday()) % 7)
    return d + dt.timedelta(days=7 * (n - 1))


def business_day(y, m, n, holidays):
    d, k = dt.date(y, m, 1), 0
    while True:
        if d.weekday() < 5 and d.isoformat() not in holidays:
            k += 1
            if k == n:
                return d
        d += dt.timedelta(days=1)


def build_calendar(path, today, back=3, horizon=45):
    p = Path(path)
    if not p.exists():
        return None
    cal = json.loads(p.read_text())
    hol = {h["date"]: h for h in cal.get("holidays", [])}
    closed = {d for d, h in hol.items() if h.get("close") == "full"}
    ev = []

    def add(day, time, name, tier, kind, approx=False, note=""):
        ev.append({"date": day.isoformat(), "time": time, "name": name, "tier": tier, "kind": kind, "approx": approx, "note": note})

    lo, hi = today - dt.timedelta(days=back), today + dt.timedelta(days=horizon)
    for m in cal.get("fomc", []):
        s, e = D(m["start"]), D(m["end"])
        dec = m.get("decision", True)
        add(s, "", "FOMC meeting, day 1", 3, "fomc")
        if dec:
            add(e, "2:00 pm", "FOMC decision", 1, "fomc", note="Press conference 2:30 pm" + (", new projections (dots)" if m.get("sep") else ""))
            add(e + dt.timedelta(days=21), "2:00 pm", "FOMC minutes", 2, "fomc")
        else:
            add(e, "", "FOMC strategy meeting, no rate decision", 2, "fomc")
    for d in cal.get("cpi", []):
        add(D(d), "8:30 am", "CPI", 1, "data", note="Inflation")
    for d in cal.get("nfp", []):
        add(D(d), "8:30 am", "Jobs report (NFP)", 1, "data", note="Labor")
    for d in cal.get("ppi", []):
        add(D(d), "8:30 am", "PPI", 2, "data")
    for d in cal.get("retail_sales_approx", []):
        add(D(d), "8:30 am", "Retail sales", 2, "data", approx=True)
    for d in cal.get("pce_approx", []):
        add(D(d), "8:30 am", "PCE inflation", 2, "data", approx=True, note="The Fed's preferred inflation measure")
    for x in cal.get("extra", []):
        add(D(x["date"]), x.get("time", ""), x["name"], x.get("tier", 3), "extra", note=x.get("note", ""))
    d = lo
    while d <= hi:
        if d.weekday() == 3 and d.isoformat() not in closed:
            add(d, "8:30 am", "Jobless claims", 3, "claims")
        d += dt.timedelta(days=1)
    for y, m in sorted({(lo.year, lo.month), (hi.year, hi.month), (today.year, today.month)}):
        o = nth_weekday(y, m, 4, 3)
        add(o, "", "Monthly options expiry (OPEX)", 2, "opex", note="Dealer hedging unwinds after expiry")
        add(business_day(y, m, 1, closed), "10:00 am", "ISM manufacturing", 2, "data")
        add(business_day(y, m, 3, closed), "10:00 am", "ISM services", 2, "data")
    for dstr, h in hol.items():
        add(D(dstr), "", ("US markets closed: " if h.get("close") == "full" else "Early close 1:00 pm: ") + h["name"], 3, "holiday")
    ev = [e for e in ev if lo.isoformat() <= e["date"] <= hi.isoformat()]
    seen, uniq = set(), []
    for e in sorted(ev, key=lambda e: (e["date"], e["tier"], e["time"], e["name"])):
        k = (e["date"], e["name"])
        if k not in seen:
            seen.add(k)
            uniq.append(e)
    black = None
    for m in cal.get("fomc", []):
        s, e = D(m["start"]), D(m["end"])
        b0, b1 = second_saturday_before(s), e + dt.timedelta(days=1)
        if b1 >= today and b0 <= today + dt.timedelta(days=30):
            black = {"start": b0.isoformat(), "end": b1.isoformat(), "meeting": f"{m['start']} to {m['end']}",
                     "active": b0 <= today <= b1, "days_to_start": (b0 - today).days}
            break
    nxt = next((e for e in uniq if e["tier"] == 1 and e["date"] >= today.isoformat()), None)
    if nxt:
        nxt = dict(nxt, days=(D(nxt["date"]) - today).days)
    return {"today": today.isoformat(), "events": uniq, "blackout": black, "next_tier1": nxt,
            "note": "Times are US Eastern. FOMC, CPI and jobs dates come from the Fed and BLS schedules. Dates marked approximate are estimates; check the official release."}


# --------------------------------------------------------------------------
# Release scoreboard: previous, actual, revision (expected is typed in the page)
# --------------------------------------------------------------------------
# id, group, label, series key, kind, unit, decimals, in-line band, scale, word if above, word if below, calendar name
REL = [
    ("nfp", "Jobs", "Payrolls change", "payems", "chg", "k", 0, 25, 1, "Stronger jobs", "Weaker jobs", "Jobs report (NFP)"),
    ("unrate", "Jobs", "Unemployment rate", "unrate", "lvl", "%", 1, 0.05, 1, "Weaker jobs", "Stronger jobs", "Jobs report (NFP)"),
    ("claims", "Jobs", "Initial jobless claims", "claims", "lvl", "k", 0, 8, 0.001, "Weaker jobs", "Stronger jobs", "Jobless claims"),
    ("cpi_yoy", "Inflation", "CPI, year over year", "cpi", "yoy", "%", 1, 0.05, 1, "Hotter, hawkish", "Cooler, dovish", "CPI"),
    ("core_cpi_mom", "Inflation", "Core CPI, month over month", "core_cpi", "mom", "%", 1, 0.05, 1, "Hotter, hawkish", "Cooler, dovish", "CPI"),
    ("core_pce_mom", "Inflation", "Core PCE, month over month", "core_pce", "mom", "%", 1, 0.05, 1, "Hotter, hawkish", "Cooler, dovish", "PCE inflation"),
    ("ppi_mom", "Inflation", "PPI, month over month", "ppi", "mom", "%", 1, 0.1, 1, "Hotter, hawkish", "Cooler, dovish", "PPI"),
    ("retail_mom", "Spending", "Retail sales, month over month", "retail", "mom", "%", 1, 0.2, 1, "Stronger spending", "Weaker spending", "Retail sales"),
]


def fetch_initial(sid, api_key, http_get, start):
    """First-reported values: take every vintage and keep the earliest one for each date."""
    q = urllib.parse.urlencode({"series_id": sid, "api_key": api_key, "file_type": "json", "observation_start": start,
                                "realtime_start": "1776-07-04", "realtime_end": "9999-12-31", "limit": 100000})
    data = json.loads(http_get("https://api.stlouisfed.org/fred/series/observations?" + q))
    best = {}
    for o in data["observations"]:
        if o["value"] in (".", ""):
            continue
        d, rs = o["date"], o["realtime_start"]
        if d not in best or rs < best[d][0]:
            best[d] = (rs, float(o["value"]))
    return {d: v for d, (rs, v) in best.items()}


def fetch_initials(cfg, today, http_get, errors, api_key):
    out = {}
    if not api_key:
        errors.append("Revisions: FRED API key not set, so first-reported values are skipped.")
        return out
    start = (today - dt.timedelta(days=3 * 366)).isoformat()
    for key in sorted({r[3] for r in REL}):
        sid = (cfg.get("fred") or {}).get(key)
        if not sid:
            continue
        try:
            out[key] = fetch_initial(sid, api_key, http_get, start)
        except Exception as e:  # noqa: BLE001
            errors.append(f"Revisions ({sid}): {str(e)[:90]}")
    return out


# Expected (consensus) numbers: the free public Forex Factory weekly JSON feed (unofficial).
FF_URLS = ["https://nfs.faireconomy.media/ff_calendar_thisweek.json", "https://nfs.faireconomy.media/ff_calendar_nextweek.json"]
FF_TITLES = {"nfp": "non-farm employment change", "unrate": "unemployment rate", "claims": "unemployment claims", "cpi_yoy": "cpi y/y",
             "core_cpi_mom": "core cpi m/m", "core_pce_mom": "core pce price index m/m", "ppi_mom": "ppi m/m", "retail_mom": "retail sales m/m"}


def _num(x):
    if x is None:
        return None
    t = str(x).strip().replace(",", "").replace("%", "")
    if not t:
        return None
    mult = 1.0
    if t[-1] in "kK":
        t = t[:-1]
    elif t[-1] in "mM":
        t, mult = t[:-1], 1000.0
    try:
        return float(t) * mult
    except ValueError:
        return None


def ref_period(rid, event_date):
    """Which data period a release on this date covers (matches the series dates)."""
    e = D(event_date)
    if rid == "claims":
        return (e - dt.timedelta(days=5)).isoformat()
    last = e.replace(day=1) - dt.timedelta(days=1)
    return last.replace(day=1).isoformat()


def fetch_consensus(http_get, errors):
    items = []
    for u in FF_URLS:
        try:
            for it in json.loads(http_get(u)):
                if str(it.get("country", "")).upper() == "USD":
                    items.append(it)
        except Exception as e:  # noqa: BLE001
            if "404" in str(e) and "nextweek" in u:
                continue  # next week's file is only published late in the week
            errors.append(f"Expected numbers ({u.rsplit('/', 1)[-1]}): {str(e)[:80]}")
    return items


def update_store(store, items, today):
    for rid, title in FF_TITLES.items():
        for it in items:
            if str(it.get("title", "")).strip().lower() != title:
                continue
            f = _num(it.get("forecast"))
            ed = str(it.get("date", ""))[:10]
            if f is None or len(ed) != 10:
                continue
            key = f"{rid}|{ref_period(rid, ed)}"
            old = store.get(key)
            if old and old["event_date"] <= today.isoformat():
                continue  # frozen once the release day has passed
            store[key] = {"forecast": f, "event_date": ed}
    cutoff = (today - dt.timedelta(days=150)).isoformat()
    return {k: v for k, v in store.items() if v["event_date"] >= cutoff}



def _next_period(d, weekly):
    x = D(d)
    if weekly:
        return (x + dt.timedelta(days=7)).isoformat()
    y, m = (x.year + 1, 1) if x.month == 12 else (x.year, x.month + 1)
    return dt.date(y, m, 1).isoformat()


def build_releases(raw, init, cal, store=None):
    store = store or {}
    rows = []
    evs = (cal or {}).get("events", [])
    today = (cal or {}).get("today", "")
    for rid, grp, label, key, kind, unit, dec, inline, scale, up, dn, evname in REL:
        s = raw.get(key)
        need = {"yoy": 14, "mom": 3, "chg": 3, "lvl": 3}[kind]
        if not s or len(s) < need:
            continue
        i0 = len(s) - 1
        first_map = init.get(key, {})

        def metric(i, first=False):
            v = first_map.get(s[i][0]) if first else s[i][1]
            if v is None:
                return None
            if kind == "yoy":
                return (v / s[i - 12][1] - 1) * 100
            if kind == "mom":
                return (v / s[i - 1][1] - 1) * 100
            if kind == "chg":
                return v - s[i - 1][1]
            return v * scale

        act, prev = metric(i0), metric(i0 - 1)
        pf = metric(i0 - 1, True)
        rev = None if pf is None or prev is None else prev - pf
        weekly = key == "claims"
        nxt = next((e["date"] for e in evs if e["name"] == evname and e["date"] >= today), None)
        rows.append({"id": rid, "group": grp, "label": label, "unit": unit, "dec": dec, "inline": inline, "up": up, "dn": dn,
                     "period": s[i0][0], "weekly": weekly, "next_period": _next_period(s[i0][0], weekly),
                     "actual": None if act is None else round(act, 3), "prev": None if prev is None else round(prev, 3),
                     "prev_first": None if pf is None else round(pf, 3), "revision": None if rev is None else round(rev, 3),
                     "next_date": nxt,
                     "expected": (store.get(f"{rid}|{s[i0][0]}") or {}).get("forecast"),
                     "next_expected": (store.get(f"{rid}|{_next_period(s[i0][0], weekly)}") or {}).get("forecast")})
    return rows



# --------------------------------------------------------------------------
# Daily regime tag and history
# --------------------------------------------------------------------------
def daily_tag(day, post, regime, vol, rates, credit, fm):
    p = [day, post["level"]]
    if regime:
        p.append(f"{regime['label']} {regime['avg']} {regime['direction']}")
    if credit:
        p.append(f"HY {credit['bp']}bp {credit['band']}")
    if vol:
        p.append(f"VIX {vol['vix']:.1f} {vol['state']}")
    if rates and rates.get("cycle"):
        p.append(f"Fed {rates['cycle']['phase']}")
    if fm:
        p.append(f"4mkt {fm['verdict']}")
    return " | ".join(p)


def load_history(path, limit=500):
    p = Path(path)
    if not p.exists():
        return []
    out = []
    with p.open() as f:
        for x in csv.DictReader(f):
            row = {}
            for k, v in x.items():
                try:
                    row[k] = float(v) if v not in ("", None) and k not in ("date", "posture", "regime", "tag", "four_market", "vix_state", "rate_phase") else v
                except ValueError:
                    row[k] = v
            out.append(row)
    return out[-limit:]


def demo_history(raw, post_level, regime, S):
    days = [d for d, _ in raw["vix"][-70:]]
    n = len(days)
    avg0, avg1 = regime["avg_prev"], regime["avg"]
    out = []
    for i, d in enumerate(days):
        f = i / (n - 1)
        a = round(avg0 + 6 * (1 - f) * (1 if S else 0) + (avg1 - avg0) * f)
        lvl = "Normal"
        if S and i > n - 14:
            lvl = "Elevated"
        if S and i > n - 5:
            lvl = "Critical"
        out.append({"date": d, "posture": lvl, "regime_avg": a if not S else round(70 - 35 * f * f), "vix": round(raw["vix"][-70 + i][1], 1),
                    "hy_bp": round(at_or_before(raw["hy"], d) * 100), "us10y": round(at_or_before(raw["dgs10"], d), 2)})
    return out


# --------------------------------------------------------------------------
# Synthetic series for demo mode
# --------------------------------------------------------------------------
def demo_raw(raw, cfg, S, path, bdays, weekly, monthly, rnd):
    def put(k, dates, vals):
        raw[k] = [(d, float(v)) for d, v in zip(dates, vals)]

    # 1M and the earlier move for each ETF (percent over the last ~21 days), calm and stress
    calm = {"XLK": 4, "XLC": 2, "XLY": 3, "XLF": 2, "XLI": 1.5, "XLE": -2, "XLB": 0, "XLV": -1, "XLP": -1.5, "XLU": -2.5, "XLRE": -1.5,
            "SPY": 1.5, "IWM": 2.5, "QQQ": 2.5, "RSP": 1.0, "MTUM": 3.0, "USMV": -1.0, "VLUE": 0.5, "GLD": -0.5, "TLT": 0.5}
    strs = {"XLK": -6, "XLC": -5, "XLY": -7, "XLF": -4, "XLI": -4, "XLE": 8, "XLB": -2, "XLV": 1.5, "XLP": 2.5, "XLU": 3, "XLRE": -1,
            "SPY": -4.5, "IWM": -8, "QQQ": -6.5, "RSP": -5, "MTUM": -6, "USMV": 1, "VLUE": -2.5, "GLD": 4, "TLT": -3}
    moves = strs if S else calm
    D5, n5 = bdays(780), 780
    for k in list(cfg.get("sectors", {})) + ["SPY", "IWM", "QQQ", "RSP", "MTUM", "USMV", "VLUE", "GLD"]:
        key = ("s_" if k.startswith("XL") else "e_") + k
        base = 100 + rnd.uniform(-5, 5)
        put(key, D5, path(n5, [(0, 100), (.97, base * 1.25), (1, base * 1.25 * (1 + moves[k] / 100))], .5, rnd))
    # Stocks and bonds: negative link normally, positive in the stress demo's last 60 days
    D6, n6 = bdays(1300), 1300
    spx = raw["spx"]
    tlt, p, rets = [], 90.0, []
    for i in range(n6):
        sr = 0 if i == 0 else spx[i][1] / spx[i - 1][1] - 1
        rho = (.9 if (S and i > n6 - 60) else -.5)
        p *= 1 + rho * sr + rnd.gauss(0, .004)
        tlt.append(p)
    put("e_TLT", D6, tlt)
    # Labor and liquidity
    M = monthly(48)
    lvl, out = 155000.0, []
    for i in range(48):
        lvl += (170 if not S else (170 if i < 40 else 20 - (i - 40) * 15)) + rnd.gauss(0, 15)
        out.append(lvl)
    put("payems", M, out)
    for k, base, step in (("ppi", 140.0, .25), ("retail", 600000.0, .4)):
        v, ser = base, []
        for i in range(48):
            v *= 1 + (step + rnd.gauss(0, .3)) / 100
            ser.append(v)
        put(k, M, ser)
    W = weekly(156)
    put("walcl", W, path(156, [(0, 7.2e6), (.5, 6.6e6), (1, 6.65e6 if not S else 6.3e6)], 8000, rnd))
    put("tga", W, path(156, [(0, 6.5e5), (1, 8.2e5 if not S else 9.5e5)], 15000, rnd))
    put("rrp_on", bdays(780), path(780, [(0, 800), (.5, 200), (1, 40 if not S else 8)], 10, rnd))
    return raw


def build_all(raw, cfg, today, http_get, errors, demo, S, post_level=None, regime=None, cal_path=None, cot_demo_rnd=None, api_key=None, store=None):
    out = {"labor": build_labor(raw), "inflation": build_inflation(raw), "liquidity": build_liquidity(raw),
           "correlations": build_correlations(raw, cfg), "rotation": build_rotation(raw, cfg)}
    if demo:
        rnd = cot_demo_rnd
        cot = {}
        for inst in (cfg.get("cot") or {}).get("markets", {}):
            weeks = cfg["cot"]["weeks"]
            base, ser = 20000.0, []
            for i in range(weeks):
                base += rnd.gauss(0, 2500)
                ser.append(base)
            lo, hi = min(ser), max(ser)
            if inst == "NQ":
                ser[-1] = lo + (hi - lo) * (0.95 if S else 0.55)
            days = [(today - dt.timedelta(days=7 * (weeks - 1 - i))).isoformat() for i in range(weeks)]
            lev = list(zip(days, [int(v) for v in ser]))
            cot[inst] = {"market": f"{inst} (demo)", "lev": lev, "am": [(d, int(v * .6)) for d, v in lev]}
    else:
        cot = fetch_cot(cfg, http_get, errors)
    out["positioning"] = build_positioning(cot)
    out["calendar"] = build_calendar(cal_path, today) if cal_path else None
    if demo:
        rnd = cot_demo_rnd
        init = {}
        for rid, grp, label, key, kind, unit, dec, inline, scale, up, dn, ev in REL:
            ser = raw.get(key)
            if not ser or len(ser) < 3:
                continue
            m = {}
            for d, v in ser[-3:-1]:
                m[d] = v + (rnd.gauss(0, 20000 if key == "payems" else 0) if key == "payems" else v * rnd.gauss(0, .0004))
            if key == "payems":
                m[ser[-2][0]] = ser[-2][1] - 14
            init[key] = m
    else:
        init = fetch_initials(cfg, today, http_get, errors, api_key)
    store = dict(store or {})
    if demo:
        store = {}
        for rid, grp, label, key, kind, unit, dec, inline, scale, up, dn, ev in REL:
            ser = raw.get(key)
            if not ser or len(ser) < 14:
                continue
            row = next((r for r in build_releases(raw, init, out["calendar"]) if r["id"] == rid), None)
            if row and row["actual"] is not None:
                off = (cot_demo_rnd.choice([-1, 0, 0, 1, 2])) * row["inline"] * 1.5
                store[f"{rid}|{row['period']}"] = {"forecast": round(row["actual"] - off, row["dec"]), "event_date": today.isoformat()}
                store[f"{rid}|{row['next_period']}"] = {"forecast": round(row["actual"], row["dec"]), "event_date": (today + dt.timedelta(days=9)).isoformat()}
    elif not demo:
        store = update_store(store, fetch_consensus(http_get, errors), today)
    out["consensus_store"] = store
    out["releases"] = build_releases(raw, init, out["calendar"], store) if out["calendar"] else []
    return out
