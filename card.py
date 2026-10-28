import re
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

THEMES = {
    "dark": {"bg1": "#1a1025", "bg2": "#2d1747", "fg": "#f3ecff", "dim": "#a99cc4", "track": "#3b2a55", "border": "#5b3d86"},
    "light": {"bg1": "#ffffff", "bg2": "#f1e8ff", "fg": "#24123d", "dim": "#6b5a87", "track": "#e4d8f7", "border": "#c9b3ee"},
}
PURPLES = ["#8A2BE2", "#A35BEB", "#B97CF0", "#6A1FB5", "#9D4EDD"]
WEEKS = 12
POSSUM = re.sub(r"</?svg[^>]*>", "", (Path(__file__).parent / "possum.svg").read_text()).strip()


def stats(podcasts, state, data, today):
    category = {p["id"]: p["category"] for p in podcasts}
    delivered = [(pid, e) for pid, episodes in state.items() for e in episodes.values() if e.get("delivered")]
    per_category = Counter({c: 0 for c in category.values()})
    per_category.update(category[pid] for pid, _ in delivered if pid in category)
    def monday(day):
        return day - timedelta(days=day.weekday())

    per_week = Counter((monday(today) - monday(date.fromisoformat(e["delivered"]))).days // 7 for _, e in delivered)
    verdicts = Counter(
        m.group(1)
        for f in (data / "summaries").glob("*/*.md")
        if (m := re.search(r"Verdict: (listen|skim|skip)", f.read_text()))
    )
    minutes = sum(int(m.group(1)) for _, e in delivered if (m := re.search(r"Length: (\d+) min", e.get("header", ""))))
    first = min((e["delivered"] for _, e in delivered), default=today.isoformat())
    return {
        "episodes": len(delivered),
        "hours": minutes / 60,
        "since": date.fromisoformat(first).strftime("%b %Y"),
        "categories": per_category.most_common(),
        "weeks": [per_week[WEEKS - 1 - i] for i in range(WEEKS)],
        "verdicts": {v: verdicts[v] for v in ("listen", "skim", "skip")},
    }


def svg(s, theme):
    t = THEMES[theme]
    rows_end = 132 + 26 * len(s["categories"])
    width, height = 560, rows_end + 64
    top = max((n for _, n in s["categories"]), default=0) or 1
    bars = []
    for i, (name, n) in enumerate(s["categories"]):
        y = 132 + i * 26
        w = 300 * n / top
        bars.append(
            f'<text x="28" y="{y + 11}" class="label">{name}</text>'
            f'<rect x="110" y="{y}" width="300" height="14" rx="7" fill="{t["track"]}"/>'
            f'<rect x="110" y="{y}" width="{w:.1f}" height="14" rx="7" fill="{PURPLES[i % len(PURPLES)]}" class="bar" style="animation-delay:{0.15 * i:.2f}s"/>'
            f'<text x="{110 + w + 8:.1f}" y="{y + 11}" class="count">{n}</text>'
        )
    peak = max(s["weeks"]) or 1
    baseline = rows_end - 4
    weeks = "".join(
        f'<rect x="{440 + i * 8}" y="{baseline - 40 * n / peak:.1f}" width="5" height="{max(40 * n / peak, 1.5):.1f}" rx="1.5" fill="#8A2BE2" opacity="{1 if n else 0.35}"/>'
        for i, n in enumerate(s["weeks"])
    )
    v = s["verdicts"]
    text_x = 30 + 31 * len(str(s["episodes"]))
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-label="Podcassum: {s['episodes']} episodes summarised">
<defs>
  <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{t['bg1']}"/><stop offset="1" stop-color="{t['bg2']}"/></linearGradient>
  <linearGradient id="num" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#8A2BE2"/><stop offset="1" stop-color="#C77DFF"/></linearGradient>
</defs>
<style>
  text {{ font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; fill: {t['fg']}; }}
  .title {{ font-size: 15px; font-weight: 700; letter-spacing: 2px; }}
  .dim {{ font-size: 12px; fill: {t['dim']}; }}
  .big {{ font-size: 52px; font-weight: 800; fill: url(#num); }}
  .label {{ font-size: 13px; fill: {t['dim']}; }}
  .count {{ font-size: 13px; font-weight: 700; }}
  .bar {{ transform-box: fill-box; transform-origin: left; animation: grow 0.9s cubic-bezier(.2,.8,.2,1) both; }}
  @keyframes grow {{ from {{ transform: scaleX(0); }} to {{ transform: scaleX(1); }} }}
</style>
<rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="14" fill="url(#bg)" stroke="{t['border']}"/>
<g transform="translate(20,8) scale(0.42)">{POSSUM}</g>
<text x="68" y="38" class="title">PODCASSUM</text>
<text x="{width - 28}" y="38" class="dim" text-anchor="end">since {s['since']}</text>
<text x="26" y="104" class="big">{s['episodes']}</text>
<text x="{text_x}" y="84" class="count">episodes summarised</text>
<text x="{text_x}" y="103" class="dim">{s['hours']:.1f} hours of audio you did not have to hear</text>
{''.join(bars)}
{weeks}
<text x="440" y="{rows_end + 14}" class="dim">last {WEEKS} weeks</text>
<text x="28" y="{height - 22}" class="dim">listen <tspan class="count">{v['listen']}</tspan>  ·  skim <tspan class="count">{v['skim']}</tspan>  ·  skip <tspan class="count">{v['skip']}</tspan></text>
</svg>
"""


def write(podcasts, state, data, out, today):
    s = stats(podcasts, state, data, today)
    changed = False
    for theme in THEMES:
        path = out / f"card-{theme}.svg"
        new = svg(s, theme)
        if not path.exists() or path.read_text() != new:
            path.write_text(new)
            changed = True
    return changed
