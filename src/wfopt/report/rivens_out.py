"""Riven estimates as CSV and a small self-contained HTML page."""

from __future__ import annotations

import csv
import html
from pathlib import Path
from typing import Any

from wfopt.engine.rivens import RivenEstimate
from wfopt.report.html_out import CSS

COLUMNS: list[tuple[str, Any]] = [
    ("Weapon", lambda e: e.weapon_name),
    ("Stats", lambda e: e.stats),
    ("Estimate", lambda e: "" if e.estimate is None else round(e.estimate)),
    ("Confidence", lambda e: e.confidence),
    ("Cheapest similar", lambda e: "" if e.cheapest is None else e.cheapest),
    ("Similar listed", lambda e: e.comps),
    ("Weapon floor", lambda e: "" if e.weapon_floor is None else e.weapon_floor),
    ("DE median", lambda e: "" if e.de_median is None else round(e.de_median)),
    ("Buff roll %", lambda e: "" if e.riven.buff_roll is None else round(100 * e.riven.buff_roll)),
    ("Curse roll %", lambda e: "" if e.riven.curse_roll is None else round(100 * e.riven.curse_roll)),
    ("Rerolls", lambda e: e.riven.rerolls),
    ("Rank", lambda e: e.riven.rank),
    ("MR", lambda e: "" if e.riven.mastery_req is None else e.riven.mastery_req),
    ("Polarity", lambda e: e.riven.polarity),
    ("Disposition", lambda e: "" if not e.weapon or e.weapon.disposition is None else e.weapon.disposition),
    ("Basis", lambda e: e.basis),
    ("Notes", lambda e: "; ".join(e.notes)),
    ("WFM search", lambda e: e.url),
]
NUMERIC = {"Estimate", "Cheapest similar", "Similar listed", "Weapon floor", "DE median", "Buff roll %",
           "Curse roll %", "Rerolls", "Rank", "MR", "Disposition"}


def write_csv(rows: list[RivenEstimate], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow([h for h, _ in COLUMNS])
        for e in rows:
            w.writerow([g(e) for _, g in COLUMNS])
    return path


def write_html(rows: list[RivenEstimate], path: Path, generated: str) -> Path:
    head = "".join(f'<th class="{"num" if h in NUMERIC else ""}">{html.escape(h)}</th>' for h, _ in COLUMNS)
    body = []
    for e in rows:
        tds = []
        for h, g in COLUMNS:
            v = g(e)
            s = "" if v is None else str(v)
            if h == "WFM search" and s:
                tds.append(f'<td><a href="{html.escape(s)}" target="_blank" rel="noopener">search</a></td>')
            elif h in ("Notes", "Basis"):
                tds.append(f'<td class="notes">{html.escape(s)}</td>')
            else:
                tds.append(f'<td class="{"num" if h in NUMERIC else ""}">{html.escape(s)}</td>')
        body.append("<tr>" + "".join(tds) + "</tr>")
    total = sum(e.estimate or 0 for e in rows)
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Riven Estimates</title><style>{CSS}</style></head>
<body><main><h1>Riven estimates</h1>
<p class="sub">Generated {html.escape(generated)} · {len(rows)} unveiled rivens · ≈{total:.0f}p total. Estimates come from
asking prices of similar rivens on warframe.market (not sales) and DE's weekly trade medians. Treat them as a starting point.</p>
<div class="wrap"><table><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table></div>
</main></body></html>"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(page, encoding="utf-8")
    return path
