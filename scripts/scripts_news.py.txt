#!/usr/bin/env python3
"""Builds docs/news.json: free headlines (titles + links only) grouped by topic,
then an optional structured summary written by the free Gemini API.

Never raises: if anything fails, it writes what it has plus a list of errors,
so the data workflow keeps working. Standard library only.

Usage:  python scripts/news.py            (live; summary only if GEMINI_API_KEY is set)
        python scripts/news.py --demo     (sample content, no network)
"""
import datetime as dt
import email.utils
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "news.json"
UA = {"User-Agent": "Mozilla/5.0 (macro-cockpit news bot)"}
MODELS = ["gemini-3.6-flash", "gemini-3.8-flash", "gemini-flash-latest"]
MAX_AGE_H = 40
PER_SECTION = 14

GN = "https://news.google.com/rss/search?hl=en-US&gl=US&ceid=US:en&q="


def gnews(q):
    return GN + urllib.parse.quote(q + " when:1d")


# section id -> (title, feeds[(label, url)])
SECTIONS = {
    "fed": ("Fed and rates", [
        ("Federal Reserve", "https://www.federalreserve.gov/feeds/press_all.xml"),
        ("Google News", gnews("Federal Reserve OR FOMC OR Treasury yields OR rate cut")),
    ]),
    "econ": ("Economy and data", [
        ("CNBC economy", "https://www.cnbc.com/id/20910258/device/rss/rss.html"),
        ("Google News", gnews("US economy inflation OR jobs OR GDP OR retail sales OR PCE")),
    ]),
    "mega": ("Index heavyweights", [
        ("Google News", gnews("Nvidia OR Microsoft OR Apple OR Amazon stock")),
        ("Google News", gnews("Alphabet OR Meta OR Tesla OR Broadcom stock")),
        ("Yahoo Finance", "https://finance.yahoo.com/news/rssindex"),
    ]),
    "macro": ("Policy, geopolitics and oil", [
        ("Google News", gnews("tariffs OR sanctions OR geopolitics markets")),
        ("Google News", gnews("oil prices OPEC OR crude")),
    ]),
    "mkts": ("Market wrap", [
        ("CNBC markets", "https://www.cnbc.com/id/15839069/device/rss/rss.html"),
        ("Google News", gnews("Wall Street stocks Nasdaq S&P 500 futures")),
    ]),
}

MEGA = ["NVDA", "MSFT", "AAPL", "AMZN", "GOOGL", "META", "TSLA", "AVGO"]


def get(url, timeout=25):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def clean(s):
    s = re.sub(r"<[^>]+>", "", s or "")
    return re.sub(r"\s+", " ", s).strip()


def parse_feed(raw, label):
    out = []
    root = ET.fromstring(raw)
    for it in root.iter():
        tag = it.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        title = link = pub = src = ""
        for ch in it:
            t = ch.tag.split("}")[-1]
            if t == "title":
                title = clean(ch.text)
            elif t == "link":
                link = (ch.attrib.get("href") or ch.text or "").strip()
            elif t in ("pubDate", "published", "updated", "date") and not pub:
                pub = (ch.text or "").strip()
            elif t == "source":
                src = clean(ch.text)
        if not title or not link:
            continue
        ts = None
        try:
            ts = email.utils.parsedate_to_datetime(pub)
        except Exception:
            try:
                ts = dt.datetime.fromisoformat(pub.replace("Z", "+00:00"))
            except Exception:
                ts = None
        if ts is not None and ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.timezone.utc)
        out.append({"title": title, "url": link, "source": src or label, "ts": ts})
    return out


def collect(errors):
    now = dt.datetime.now(dt.timezone.utc)
    secs = {}
    for sid, (title, feeds) in SECTIONS.items():
        seen, items = set(), []
        for label, url in feeds:
            try:
                got = parse_feed(get(url), label)
            except Exception as e:
                errors.append(f"News feed failed ({sid}/{label}): {type(e).__name__} {str(e)[:80]}")
                continue
            for g in got:
                if g["ts"] is not None and (now - g["ts"]).total_seconds() > MAX_AGE_H * 3600:
                    continue
                key = re.sub(r"\W+", "", g["title"].lower())[:60]
                if key in seen:
                    continue
                seen.add(key)
                items.append(g)
        items.sort(key=lambda g: g["ts"] or dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc), reverse=True)
        secs[sid] = items[:PER_SECTION]
    return secs


PROMPT = """You are a macro desk analyst writing a daily briefing for a futures day trader who trades NQ (Nasdaq-100) and ES (S&P 500). He does not want to read articles. Use ONLY the numbered headlines below. Do not invent figures, quotes or events that are not in the headlines. If the headlines do not show something, say it is unclear. This is context, not trade advice.

Return JSON only, with this shape:
{
 "big_picture": "two short sentences: the single main story today and the overall tone for NQ/ES",
 "sections": [
  {"id": "<one of: fed, econ, mega, macro, mkts>",
   "items": [
    {"title": "<6-12 words>",
     "what_changed": "<one sentence>",
     "nq_es": "<one sentence: likely effect on NQ and ES, and which is more sensitive>",
     "approach": "<one sentence: how to approach it as an intraday trader, e.g. size, levels to respect, what to wait for>",
     "horizon": "short" | "long" | "both",
     "tone": "bullish" | "bearish" | "mixed" | "neutral",
     "refs": [<headline numbers that support it>]}
   ]}
 ]
}
Rules: merge headlines about the same event into one item. Keep 2 to 5 items per section, the most market-relevant first. In "mega" give each company that has real news its own item and name the ticker in the title (watch list: %s). Skip sections with nothing useful. "short" means hours to days, "long" means weeks or more. Plain words, no jargon.

HEADLINES:
%s
"""


def build_prompt(secs):
    lines, idx = [], {}
    n = 0
    for sid, items in secs.items():
        lines.append(f"[{sid}] {SECTIONS[sid][0]}")
        for g in items:
            n += 1
            idx[n] = g
            lines.append(f"{n}. {g['title']} ({g['source']})")
    return PROMPT % (", ".join(MEGA), "\n".join(lines)), idx


def call_gemini(prompt, key, errors):
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.3},
    }).encode()
    for model in MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        req = urllib.request.Request(url, data=body, headers={
            "Content-Type": "application/json", "x-goog-api-key": key})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                data = json.loads(r.read())
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            text = re.sub(r"^```(?:json)?|```$", "", text.strip()).strip()
            return json.loads(text), model
        except urllib.error.HTTPError as e:
            errors.append(f"Gemini {model}: HTTP {e.code} {e.read()[:140].decode('utf-8', 'ignore')}")
        except Exception as e:
            errors.append(f"Gemini {model}: {type(e).__name__} {str(e)[:100]}")
    return None, None


def shape_summary(raw, idx):
    secs = []
    for s in raw.get("sections", []):
        sid = s.get("id")
        if sid not in SECTIONS:
            continue
        items = []
        for it in s.get("items", []):
            refs = [r for r in it.get("refs", []) if isinstance(r, int) and r in idx][:3]
            items.append({
                "title": str(it.get("title", ""))[:140],
                "what_changed": str(it.get("what_changed", ""))[:400],
                "nq_es": str(it.get("nq_es", ""))[:400],
                "approach": str(it.get("approach", ""))[:400],
                "horizon": it.get("horizon") if it.get("horizon") in ("short", "long", "both") else "short",
                "tone": it.get("tone") if it.get("tone") in ("bullish", "bearish", "mixed", "neutral") else "neutral",
                "links": [{"title": idx[r]["title"], "url": idx[r]["url"], "source": idx[r]["source"]} for r in refs],
            })
        if items:
            secs.append({"id": sid, "title": SECTIONS[sid][0], "items": items})
    return {"big_picture": str(raw.get("big_picture", ""))[:500], "sections": secs}


def headline_view(secs):
    return [{"id": sid, "title": SECTIONS[sid][0],
             "headlines": [{"title": g["title"], "url": g["url"], "source": g["source"],
                            "ts": g["ts"].isoformat() if g["ts"] else None} for g in items]}
            for sid, items in secs.items() if items]


def demo():
    return {
        "mode": "demo", "model": None, "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "errors": [],
        "summary": {
            "big_picture": "Sample text only. Rate-cut hopes are holding up chips, while oil is the main risk to the calm.",
            "sections": [
                {"id": "fed", "title": "Fed and rates", "items": [{
                    "title": "Fed speakers lean toward a December pause (sample)",
                    "what_changed": "Two officials said more data is needed before another cut.",
                    "nq_es": "Higher yields hurt NQ more than ES because growth stocks are rate-sensitive.",
                    "approach": "Respect the 10-year yield level. Trade smaller into speeches and wait for the first reaction.",
                    "horizon": "short", "tone": "bearish", "links": []}]},
                {"id": "mega", "title": "Index heavyweights", "items": [{
                    "title": "NVDA: supply chain chatter (sample)",
                    "what_changed": "Reports point to strong chip demand heading into next quarter.",
                    "nq_es": "NVDA is a big NQ weight, so a move here drags NQ first and ES second.",
                    "approach": "Watch NVDA as a lead for NQ. If it fades while NQ holds, be cautious on longs.",
                    "horizon": "both", "tone": "bullish", "links": []}]},
            ]},
        "headlines": [],
    }


def main():
    if "--demo" in sys.argv:
        OUT.write_text(json.dumps(demo(), indent=1))
        print("wrote demo news.json")
        return
    errors = []
    out = {"mode": "headlines", "model": None,
           "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
           "errors": errors, "summary": None, "headlines": []}
    try:
        secs = collect(errors)
        out["headlines"] = headline_view(secs)
        total = sum(len(v) for v in secs.values())
        if total == 0:
            errors.append("No headlines were collected, so the previous news was kept.")
            if OUT.exists():
                try:
                    old = json.loads(OUT.read_text())
                    old["errors"] = errors
                    out = old
                except Exception:
                    pass
        else:
            key = os.environ.get("GEMINI_API_KEY", "").strip()
            if not key:
                errors.append("No GEMINI_API_KEY secret found, so this shows grouped headlines only.")
            else:
                prompt, idx = build_prompt(secs)
                raw, model = call_gemini(prompt, key, errors)
                if raw:
                    summ = shape_summary(raw, idx)
                    if summ["sections"]:
                        out["summary"], out["model"], out["mode"] = summ, model, "summary"
                    else:
                        errors.append("Gemini replied but the summary had no usable sections.")
    except Exception as e:
        errors.append(f"News step failed: {type(e).__name__} {str(e)[:100]}")
    OUT.write_text(json.dumps(out, indent=1))
    print(f"news.json: mode={out['mode']} errors={len(errors)}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never break the workflow
        print("news step crashed:", e)
