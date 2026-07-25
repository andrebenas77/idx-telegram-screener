#!/usr/bin/env python3
"""
Score crowdedness from data/history.csv and render the dark HTML screener.

    py scripts/build_screener.py                 # newest date in history
    py scripts/build_screener.py --date 2026-07-24

Reads:  reference/config.json, reference/tickers.csv, data/history.csv,
        assets/template.html, build/mentions-<date>.json (optional, for header stats)
Writes: docs/index.html, docs/archive/<date>.html, docs/archive.html, docs/history.csv
"""
import argparse
import csv
import html
import json
import math
import statistics
import shutil
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "reference" / "config.json"
TICKERS = ROOT / "reference" / "tickers.csv"
HISTORY = ROOT / "data" / "history.csv"
TEMPLATE = ROOT / "assets" / "template.html"
BUILD = ROOT / "build"
DOCS = ROOT / "docs"
WIB = timezone(timedelta(hours=7))


def esc(s):
    return html.escape(str(s), quote=True)


def load_tickers():
    meta = {}
    with open(TICKERS, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if not r.get("ticker"):
                continue
            code = r["ticker"].strip().upper()
            meta[code] = {
                "company": (r.get("company") or "").strip(),
                "sector": (r.get("sector") or "").strip(),
                "liquid": str(r.get("liquid", "0")).strip() in ("1", "true", "True"),
            }
    return meta


def load_history():
    if not HISTORY.exists():
        sys.exit("No data/history.csv yet — run fetch_mentions.py first.")
    rows = []
    with open(HISTORY, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append({
                "date": r["date"], "ticker": r["ticker"].upper(),
                "posts": int(r["posts"]), "channels": int(r["channels"]),
            })
    return rows


def load_build_json(prefix, today):
    p = BUILD / f"{prefix}-{today}.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def sparkline(vals, w=90, h=22):
    if not vals:
        return ""
    mx = max(vals) or 1
    n = len(vals)
    bw = w / max(n, 1)
    bars = []
    for i, v in enumerate(vals):
        bh = (v / mx) * (h - 3)
        x = i * bw
        y = h - bh
        op = 0.45 + 0.55 * (v / mx)
        bars.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw*0.72:.1f}" height="{bh:.1f}" '
                    f'rx="1" fill="var(--accent)" opacity="{op:.2f}"/>')
    return f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" class="spark">{"".join(bars)}</svg>'


def heat_badge(z):
    if z is None:
        return '<span class="pill pill-mute">–</span>'
    if z >= 2:
        return f'<span class="pill pill-hot">▲▲ {z:.1f}</span>'
    if z >= 1:
        return f'<span class="pill pill-warm">▲ {z:.1f}</span>'
    if z <= -1:
        return f'<span class="pill pill-cool">▼ {z:.1f}</span>'
    return f'<span class="pill pill-mute">{z:.1f}</span>'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="session date YYYY-MM-DD (default: newest in history)")
    args = ap.parse_args()

    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    W = cfg["weights"]
    VWIN = cfg["validity_window_days"]
    HALF = cfg["decay_halflife_days"]
    STALE = cfg["stale_after_quiet_sessions"]
    RESET = cfg["theme_reset_gap"]
    UP = cfg.get("up_pct", 3.0)
    DOWN = cfg.get("down_pct", -3.0)
    RVOLHOT = cfg.get("rvol_hot", 1.8)
    EXT = cfg.get("ext_pct", 15.0)
    meta = load_tickers()
    rows = load_history()

    all_dates = sorted({r["date"] for r in rows})
    today = args.date or all_dates[-1]
    usable = [d for d in all_dates if d <= today]
    if not usable:
        sys.exit(f"No history on or before {today}")
    idx_asc = {d: i for i, d in enumerate(usable)}
    last_i = len(usable) - 1

    pdata = load_build_json("prices", today)
    prices = pdata.get("prices", {})
    price_date = pdata.get("price_date")
    news = load_build_json("news", today).get("news", {})

    def age(d):
        return last_i - idx_asc[d]

    series = defaultdict(dict)      # ticker -> {date: (posts, channels)}
    for r in rows:
        if r["date"] in idx_asc:
            series[r["ticker"]][r["date"]] = (r["posts"], r["channels"])

    posts_today_total = sum(v.get(today, (0, 0))[0] for v in series.values())

    def posts_on(t, d):
        return series[t].get(d, (0, 0))[0]

    def theme_age(t):
        if posts_on(t, today) == 0:
            return 0
        run_start = 0
        gap = 0
        for d in sorted(usable, reverse=True):
            a = age(d)
            p = posts_on(t, d)
            if a == 0:
                continue
            if p > 0:
                gap = 0
                run_start = a
            else:
                gap += 1
                if gap >= RESET:
                    break
        return run_start + 1

    recs = []
    for t, s in series.items():
        window_dates = [d for d in usable if age(d) < VWIN]
        decayed = sum(posts_on(t, d) * (0.5 ** (age(d) / HALF)) for d in window_dates)
        if decayed <= 0 and not meta.get(t, {}).get("liquid"):
            continue
        posts_today = posts_on(t, today)
        chans_today = s.get(today, (0, 0))[1]
        win_posts = [posts_on(t, d) for d in window_dates]
        mean = statistics.fmean(win_posts) if win_posts else 0.0
        sd = statistics.pstdev(win_posts) if len(win_posts) > 1 else 0.0
        z = (posts_today - mean) / sd if sd > 0 else None
        prev5 = [posts_on(t, d) for d in usable if 1 <= age(d) <= 5]
        peak5 = max(prev5) if prev5 else 0
        active_ages = [age(d) for d in usable if posts_on(t, d) > 0]
        last_active = min(active_ages) if active_ages else 999
        spark_dates = [d for d in usable if age(d) < cfg.get("spark_sessions", 10)]
        spark_vals = [posts_on(t, d) for d in sorted(spark_dates)]
        recent5_sum = sum(prev5) + posts_today
        recs.append({
            "ticker": t, "company": meta.get(t, {}).get("company", ""),
            "sector": meta.get(t, {}).get("sector", ""),
            "liquid": meta.get(t, {}).get("liquid", False),
            "posts": posts_today, "channels": chans_today, "decayed": decayed,
            "share": posts_today / posts_today_total if posts_today_total else 0.0,
            "z": z, "peak5": peak5, "last_active": last_active,
            "theme_age": theme_age(t), "spark": spark_vals, "recent5_sum": recent5_sum,
        })

    # Add liquid names never mentioned, so the Quiet/contrarian list can flag ignored blue-chips.
    present = {r["ticker"] for r in recs}
    for t, m in meta.items():
        if m["liquid"] and t not in present:
            recs.append({
                "ticker": t, "company": m["company"], "sector": m["sector"], "liquid": True,
                "posts": 0, "channels": 0, "decayed": 0.0, "share": 0.0, "z": None,
                "peak5": 0, "last_active": 999, "theme_age": 0, "spark": [], "recent5_sum": 0,
            })

    # Attach price/volume (Yahoo .JK) and news to each record (v2). Missing -> None/[].
    for r in recs:
        pr = prices.get(r["ticker"], {})
        r["chg1d"] = pr.get("chg1d")
        r["chg5d"] = pr.get("chg5d")
        r["rvol"] = pr.get("rvol")
        r["close"] = pr.get("close")
        r["news"] = news.get(r["ticker"], [])

    active = [r for r in recs if r["decayed"] > 0]
    max_dp = max((r["decayed"] for r in active), default=1) or 1
    max_ch = max((r["channels"] for r in active), default=1) or 1
    max_sh = max((r["share"] for r in active), default=1) or 1
    for r in active:
        r["crowd"] = 100 * (
            W["decayed_posts"] * (r["decayed"] / max_dp)
            + W["channels"] * (r["channels"] / max_ch)
            + W["share"] * (r["share"] / max_sh)
        )

    def not_expired(r):
        return r["last_active"] < STALE

    crowded = sorted([r for r in active if not_expired(r)],
                     key=lambda r: -r["crowd"])[:cfg["top_n_crowded"]]
    crowded_set = {r["ticker"] for r in crowded}
    expired = sorted([r for r in active if r["last_active"] >= STALE],
                     key=lambda r: -r["peak5"])[:12]
    heating = sorted([r for r in active if not_expired(r) and r["z"] is not None
                      and r["z"] >= cfg["heating_min_z"]],
                     key=lambda r: -r["z"])[:12]
    # Cooling takes precedence over Aging: a fading name is cooling, not "still crowded but old".
    cooling = sorted([r for r in active if not_expired(r) and r["peak5"] > 0
                      and r["posts"] < cfg["cooling_drop_ratio"] * r["peak5"]],
                     key=lambda r: r["posts"] - r["peak5"])[:12]
    cooling_set = {r["ticker"] for r in cooling}
    aging = sorted([r for r in crowded if r["theme_age"] >= cfg["aging_min_theme_age"]
                    and r["ticker"] not in cooling_set],
                   key=lambda r: -r["crowd"])
    # Quiet = liquid names with no recent chatter at all (excludes expired, which had activity).
    quiet = sorted([r for r in recs if r["liquid"] and r["decayed"] <= 0
                    and r["recent5_sum"] <= cfg["quiet_max_recent_posts"]],
                   key=lambda r: r["ticker"])[:24]

    # header stats from build detail if present
    detail_p = BUILD / f"mentions-{today}.json"
    chans_scanned = msgs_scanned = None
    if detail_p.exists():
        d = json.loads(detail_p.read_text(encoding="utf-8"))
        chans_scanned = len(d.get("channels_scanned", []))
        msgs_scanned = d.get("messages_scanned")

    # ---- render fragments ----
    def crowd_bar(v):
        return (f'<div class="bar"><span style="width:{max(3, min(100, v)):.0f}%"></span>'
                f'</div><span class="barval">{v:.0f}</span>')

    def pct_cell(v):
        if v is None:
            return '<span class="mut">–</span>'
        cls = "up" if v > 0 else ("down" if v < 0 else "flat")
        return f'<span class="{cls}">{v:+.1f}%</span>'

    def rvol_cell(v):
        if v is None:
            return '<span class="mut">–</span>'
        hot = " rv-hot" if v >= RVOLHOT else ""
        return f'<span class="rv{hot}">{v:.1f}x</span>'

    def signal_of(r):
        c, c5, rv = r.get("chg1d"), r.get("chg5d"), (r.get("rvol") or 0)
        hasnews = bool(r.get("news"))
        if c is None:
            return ("", "")
        if c <= DOWN and rv >= RVOLHOT:
            return ("Distribution", "sig-dist")
        if c >= UP and (hasnews or rv >= RVOLHOT):
            return ("Confirmed", "sig-conf")   # crowded + up + news/volume = sell-on-news risk
        if c5 is not None and c5 >= EXT:
            return ("Extended", "sig-ext")
        if abs(c) < UP and not hasnews:
            return ("Anticipatory", "sig-antic")
        return ("", "")

    def signal_cell(r):
        label, css = signal_of(r)
        return f'<span class="sig {css}">{label}</span>' if label else '<span class="mut">–</span>'

    def news_badge(r):
        items = r.get("news") or []
        if not items:
            return '<span class="mut">–</span>'
        top = items[0]
        return (f'<a class="newsdot" href="{esc(top.get("url","#"))}" target="_blank" rel="noopener" '
                f'title="{esc(top.get("headline",""))}">news ({len(items)})</a>')

    def price_snip(r):
        return " · " + pct_cell(r["chg1d"]) if r.get("chg1d") is not None else ""

    def newtag(r):
        return ' <span class="tag tag-new">NEW</span>' if r["theme_age"] <= cfg["heating_new_max_age"] else ""

    crowded_rows = []
    for i, r in enumerate(crowded, 1):
        crowded_rows.append(
            "<tr>"
            f'<td class="rank">{i}</td>'
            f'<td class="tk">{esc(r["ticker"])}{newtag(r)}</td>'
            f'<td class="co">{esc(r["company"])}<span class="sec">{esc(r["sector"])}</span></td>'
            f'<td class="crd">{crowd_bar(r["crowd"])}</td>'
            f'<td class="num">{r["posts"]}<span class="sub">{r["channels"]} ch</span></td>'
            f'<td class="num">{pct_cell(r.get("chg1d"))}</td>'
            f'<td class="num">{pct_cell(r.get("chg5d"))}</td>'
            f'<td class="num">{rvol_cell(r.get("rvol"))}</td>'
            f'<td class="ctr">{signal_cell(r)}</td>'
            f'<td class="ctr">{news_badge(r)}</td>'
            f'<td class="spk">{sparkline(r["spark"])}</td>'
            "</tr>")
    crowded_html = ("".join(crowded_rows)
                    or '<tr><td colspan="11" class="empty">No mentions in the active window yet.</td></tr>')

    def news_section():
        out = []
        for r in crowded[:cfg.get("news_top_n", 5)]:
            items = r.get("news") or []
            head = (f'<div class="nw-tk">{esc(r["ticker"])} '
                    f'<span class="nw-co">{esc(r["company"])}</span> {price_snip(r).replace(" · ", "")}</div>')
            if items:
                lis = "".join(
                    f'<li><a href="{esc(n.get("url","#"))}" target="_blank" rel="noopener">'
                    f'{esc(n.get("headline",""))}</a>'
                    f'<span class="nw-meta">{esc(n.get("outlet",""))}'
                    + (f' · {esc(n.get("date",""))}' if n.get("date") else "") + '</span>'
                    + (f'<div class="nw-sum">{esc(n.get("summary",""))}</div>' if n.get("summary") else "")
                    + '</li>'
                    for n in items[:3])
                body = f'<ul class="nw-list">{lis}</ul>'
            else:
                body = '<div class="nw-none">No fresh news found in the last ~48h.</div>'
            out.append(f'<div class="nw-card">{head}{body}</div>')
        return "".join(out) or '<p class="empty">Run the news scan on the top crowded names to populate.</p>'

    def cards(items, metric):
        if not items:
            return '<p class="empty">Nothing here yet — this fills in as daily history builds.</p>'
        out = []
        for r in items:
            out.append(
                '<div class="card">'
                f'<div class="card-tk">{esc(r["ticker"])}{newtag(r) if metric=="heat" else ""}</div>'
                f'<div class="card-co">{esc(r["company"])}</div>'
                f'<div class="card-m">{metric_val(r, metric)}</div>'
                f'<div class="card-spk">{sparkline(r["spark"], w=110, h=20)}</div>'
                '</div>')
        return "".join(out)

    def metric_val(r, metric):
        if metric == "heat":
            return f'{heat_badge(r["z"])} · {r["posts"]} posts · Day {r["theme_age"]}' + price_snip(r)
        if metric == "aging":
            return f'Day {r["theme_age"]} · crowd {r["crowd"]:.0f} · {r["posts"]} posts' + price_snip(r)
        if metric == "cool":
            return f'{r["posts"]} posts today · peak5 {r["peak5"]} ▼' + price_snip(r)
        if metric == "expired":
            return f'quiet {r["last_active"]} sessions'
        if metric == "quiet":
            return f'{r["recent5_sum"]} posts / 5 sessions'
        return ""

    template = TEMPLATE.read_text(encoding="utf-8")
    gen = datetime.now(WIB).strftime("%Y-%m-%d %H:%M WIB")
    stat_bits = [f"{posts_today_total} ticker-posts", f"{len(active)} tickers active"]
    if chans_scanned is not None:
        stat_bits.insert(0, f"{chans_scanned} channels")
    if msgs_scanned is not None:
        stat_bits.append(f"{msgs_scanned} messages scanned")
    if price_date:
        stat_bits.append(f"prices @ close {price_date}")
    page = (template
            .replace("{{DATE}}", esc(today))
            .replace("{{GENERATED}}", esc(gen))
            .replace("{{SUMMARY_STATS}}", esc(" · ".join(stat_bits)))
            .replace("{{CROWDED_ROWS}}", crowded_html)
            .replace("{{NEWS_CARDS}}", news_section())
            .replace("{{HEATING_CARDS}}", cards(heating, "heat"))
            .replace("{{AGING_CARDS}}", cards(aging, "aging"))
            .replace("{{COOLING_CARDS}}", cards(cooling, "cool"))
            .replace("{{EXPIRED_CARDS}}", cards(expired, "expired"))
            .replace("{{QUIET_CARDS}}", cards(quiet, "quiet"))
            .replace("{{ARCHIVE_LINK}}", "archive.html"))

    DOCS.mkdir(exist_ok=True)
    (DOCS / "archive").mkdir(exist_ok=True)
    (DOCS / "index.html").write_text(page, encoding="utf-8")
    (DOCS / "archive" / f"{today}.html").write_text(
        page.replace('href="archive.html"', 'href="../archive.html"'), encoding="utf-8")
    if HISTORY.exists():
        shutil.copyfile(HISTORY, DOCS / "history.csv")

    # regenerate archive index
    files = sorted((p.stem for p in (DOCS / "archive").glob("*.html")), reverse=True)
    links = "\n".join(f'<li><a href="archive/{d}.html">{d}</a></li>' for d in files)
    arch = template
    for a, b in [("{{DATE}}", "Archive"), ("{{GENERATED}}", esc(gen)),
                 ("{{SUMMARY_STATS}}", f"{len(files)} daily screens"),
                 ("{{CROWDED_ROWS}}", ""), ("{{NEWS_CARDS}}", ""), ("{{HEATING_CARDS}}", ""),
                 ("{{AGING_CARDS}}", ""), ("{{COOLING_CARDS}}", ""), ("{{EXPIRED_CARDS}}", ""),
                 ("{{QUIET_CARDS}}", ""), ("{{ARCHIVE_LINK}}", "index.html")]:
        arch = arch.replace(a, b)
    marker = '<main class="wrap">'
    arch = arch.replace(marker, marker + f'<section class="panel"><h2>Archived screens</h2>'
                        f'<ul class="arch">{links}</ul></section>', 1)
    (DOCS / "archive.html").write_text(arch, encoding="utf-8")

    print(f"Built docs/index.html for {today}")
    print(f"  Most crowded: {', '.join(r['ticker'] for r in crowded[:8]) or '(none)'}")
    print(f"  Heating: {', '.join(r['ticker'] for r in heating[:6]) or '(none)'}")
    print(f"  Aging: {', '.join(r['ticker'] for r in aging[:6]) or '(none)'}")
    print(f"  Cooling: {', '.join(r['ticker'] for r in cooling[:6]) or '(none)'}")
    print(f"  Expired: {', '.join(r['ticker'] for r in expired[:6]) or '(none)'}")
    print(f"  Quiet/contrarian: {', '.join(r['ticker'] for r in quiet[:8]) or '(none)'}")
    print(f"  Prices loaded: {len(prices)} tickers (close {price_date or 'n/a'}) | "
          f"News: {sum(len(v) for v in news.values())} items on {len(news)} tickers")


if __name__ == "__main__":
    main()
