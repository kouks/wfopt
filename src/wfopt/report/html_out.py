"""Single self-contained HTML report (no CDN): sortable, filterable table plus side sections."""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from wfopt.report.table import COLUMNS, NUMERIC, Report, row_values

CSS = """
:root{--bg:#fbfbfa;--fg:#1d1d1b;--muted:#6b6b66;--line:#e4e3df;--card:#fff;--accent:#2f5d8a;
--list:#e7f0e6;--sell:#dcebf7;--fuse:#efe6f5;--max:#f7eedc;--duc:#f6f1d6;--hold:#efefec}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#e8e7e3;--muted:#9a9993;--line:#2e2e2b;--card:#1e1e1c;
--accent:#8cb4dc;--list:#22331f;--sell:#1d2c3a;--fuse:#2e2338;--max:#3a3020;--duc:#36321b;--hold:#262624}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1600px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:32px 0 8px}
.sub{color:var(--muted);margin:0 0 16px}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:8px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.card b{display:block;font-size:18px;font-variant-numeric:tabular-nums}.card span{color:var(--muted);font-size:12px}
.controls{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0}
input,select{font:inherit;padding:6px 8px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg)}
.wrap{overflow-x:auto;border:1px solid var(--line);border-radius:8px;background:var(--card)}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{padding:5px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top;white-space:nowrap}
th{position:sticky;top:0;background:var(--card);cursor:pointer;user-select:none;font-weight:600}
th.num,td.num{text-align:right}td.notes{white-space:normal;min-width:320px;color:var(--muted)}
td.whisper{white-space:normal;min-width:260px;font-family:ui-monospace,monospace;font-size:12px}
tr.a-List td:nth-child(4){background:var(--list)}tr.a-Sell td:nth-child(4){background:var(--sell)}
tr.a-Fuse td:nth-child(4){background:var(--fuse)}tr.a-Max td:nth-child(4){background:var(--max)}
tr.a-Ducats td:nth-child(4){background:var(--duc)}tr.a-Hold{color:var(--muted)}
a{color:var(--accent)}details{margin:8px 0}summary{cursor:pointer;font-weight:600}
.count{color:var(--muted);font-weight:400}.warn{color:#b5562a}
button.copy{font:inherit;font-size:11px;padding:1px 6px;margin-left:4px;border:1px solid var(--line);border-radius:4px;
background:var(--bg);color:var(--fg);cursor:pointer}
"""

JS = r"""
const D = JSON.parse(document.getElementById('data').textContent);
const cols = D.columns, numeric = new Set(D.numeric);
let sortKey = 'priority', sortDir = -1;
const $ = s => document.querySelector(s);
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function cell(c, v){
  const k = c.key;
  if (k === 'url') return `<td><a href="${esc(v)}" target="_blank" rel="noopener">open</a></td>`;
  if (k === 'whisper') return v ? `<td class="whisper">${esc(v)}<button class="copy" data-w="${esc(v)}">copy</button></td>` : '<td></td>';
  if (k === 'notes') return `<td class="notes">${esc(v)}</td>`;
  return `<td class="${numeric.has(k) ? 'num' : ''}">${esc(v)}</td>`;
}
function render(){
  const q = $('#q').value.toLowerCase(), cat = $('#cat').value, act = $('#act').value, hide = $('#hide').checked;
  const idx = Object.fromEntries(cols.map((c, i) => [c.key, i]));
  let rows = D.rows.filter(r => (!q || r[idx.name].toLowerCase().includes(q) || r[idx.notes].toLowerCase().includes(q))
    && (!cat || r[idx.category] === cat) && (!act || r[idx.action] === act) && (!hide || r[idx.action] !== 'Hold'));
  const si = idx[sortKey];
  rows.sort((a, b) => {
    let x = a[si], y = b[si];
    if (numeric.has(sortKey)) { x = x === '' ? -Infinity : +x; y = y === '' ? -Infinity : +y; }
    return (x < y ? -1 : x > y ? 1 : 0) * sortDir;
  });
  $('#n').textContent = `${rows.length} of ${D.rows.length} rows`;
  $('#tb').innerHTML = rows.map(r => `<tr class="a-${esc(r[idx.action].split(' ')[0])}">${cols.map((c, i) => cell(c, r[i])).join('')}</tr>`).join('');
  document.querySelectorAll('#main th').forEach(th => th.textContent = th.dataset.h + (th.dataset.k === sortKey ? (sortDir > 0 ? ' ▲' : ' ▼') : ''));
}
$('#main thead').innerHTML = '<tr>' + cols.map(c => `<th data-k="${c.key}" data-h="${esc(c.header)}" class="${numeric.has(c.key) ? 'num' : ''}"></th>`).join('') + '</tr>';
$('#main thead').addEventListener('click', e => {
  const k = e.target.dataset.k; if (!k) return;
  if (k === sortKey) sortDir = -sortDir; else { sortKey = k; sortDir = numeric.has(k) ? -1 : 1; }
  render();
});
for (const [id, key] of [['cat', 'category'], ['act', 'action']]) {
  const i = cols.findIndex(c => c.key === key);
  [...new Set(D.rows.map(r => r[i]))].sort().forEach(v => $('#' + id).insertAdjacentHTML('beforeend', `<option>${esc(v)}</option>`));
}
['#q', '#cat', '#act', '#hide'].forEach(s => $(s).addEventListener('input', render));
document.addEventListener('click', e => {
  if (e.target.matches('button.copy')) { navigator.clipboard?.writeText(e.target.dataset.w); e.target.textContent = 'copied'; }
});
render();
"""


def _table(headers: list[str], rows: list[list[Any]], numeric: set[int] = frozenset()) -> str:
    th = "".join(f'<th class="{"num" if i in numeric else ""}">{html.escape(h)}</th>' for i, h in enumerate(headers))
    body = []
    for r in rows:
        tds = []
        for i, v in enumerate(r):
            s = "" if v is None else str(v)
            if s.startswith("https://"):
                tds.append(f'<td><a href="{html.escape(s)}" target="_blank" rel="noopener">open</a></td>')
            else:
                tds.append(f'<td class="{"num" if i in numeric else ""}">{html.escape(s)}</td>')
        body.append("<tr>" + "".join(tds) + "</tr>")
    return f'<div class="wrap"><table><thead><tr>{th}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'


def render_html(report: Report) -> str:
    s = report.summary()
    cards = "".join(f'<div class="card"><b>{html.escape(str(v))}</b><span>{html.escape(k)}</span></div>' for k, v in s.items())
    age_warn = ""
    if report.inventory_age_hours > report.cfg.inventory.max_age_hours:
        age_warn = (
            f'<p class="warn">Inventory file is {report.inventory_age_hours:.1f} h old. AlecaFrame refreshes it at login '
            "and loading screens.</p>"
        )
    plan_rows = [
        [e.row.name, e.row.action, e.qty, "" if e.row.rank is None else e.row.rank, e.row.price, e.trades, round(e.net, 1), e.row.whisper]
        for e in report.plan.entries
    ]
    plan_html = _table(["Item", "Action", "Qty", "Rank", "Price", "Trades", "Net", "Whisper top buyer"], plan_rows, {2, 3, 4, 5, 6})
    kept = report.kept_rows()
    kept_html = _table(
        ["Item", "Category", "Kept copies", "Status", "WFM"],
        [[k["name"], k["category"], k["kept"], k["status"], k["url"]] for k in kept],
    )
    short = [k for k in kept if k["short"]]
    c = report.classified
    skipped_html = _table(["Item", "Reason", "Units"], [[x.name, x.reason, x.units] for x in sorted(c.skipped, key=lambda x: x.name)], {2})
    unmapped_html = _table(
        ["gameRef", "Array", "Count"],
        [[h.game_ref, h.source, h.count] for h in sorted(c.unmapped, key=lambda h: (h.source, h.game_ref))],
        {2},
    )
    nm = report.no_market_rows()
    nm_html = _table(["Item", "Category", "Qty", "Rank"], [[r.name, r.category, r.qty, r.rank] for r in nm], {2, 3})
    cfg_html = _table(["Setting", "Value"], [[k, v] for k, v in report.cfg.as_flat_dict().items()])

    data = {
        "columns": [{"header": h, "key": k} for h, k, _ in COLUMNS],
        "numeric": sorted(NUMERIC),
        "rows": [row_values(r) for r in report.rows],
    }
    data_json = json.dumps(data).replace("</", "<\\/")
    gen = report.generated_at.strftime("%Y-%m-%d %H:%M UTC")
    listings_note = (
        f"{len(report.listings)} live sell listings for {html.escape(report.cfg.wfm.ingame_name)}"
        if report.listings is not None
        else "no ingame_name set: current listings not shown"
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Warframe Trade Report</title><style>{CSS}</style></head><body><main>
<h1>Warframe trade report</h1>
<p class="sub">Generated {gen} · inventory {html.escape(report.inventory_path)} · {listings_note} · {report.network_calls} network calls</p>
{age_warn}
<div class="cards">{cards}</div>
<h2>Today's plan <span class="count">({report.plan.trades} of {report.plan.budget} trades, ≈{report.plan.plat:.0f}p)</span></h2>
{plan_html}
<h2>All recommendations</h2>
<div class="controls"><input id="q" placeholder="Search item or notes" size="28">
<select id="cat"><option value="">All categories</option></select>
<select id="act"><option value="">All actions</option></select>
<label><input type="checkbox" id="hide" checked> Hide “Hold”</label> <span id="n" class="count"></span></div>
<div class="wrap" style="max-height:75vh"><table id="main"><thead></thead><tbody id="tb"></tbody></table></div>
<h2>Kept items</h2>
<details{" open" if short else ""}><summary>Arcanes still short of a keeper <span class="count">({len(short)})</span></summary>
{_table(["Item", "Status"], [[k["name"], k["status"]] for k in short])}</details>
<details><summary>All keeper copies <span class="count">({len(kept)})</span></summary>{kept_html}</details>
<h2>Debug</h2>
<details><summary>No market <span class="count">({len(nm)})</span></summary>{nm_html}</details>
<details><summary>Skipped (out of scope) <span class="count">({len(c.skipped)})</span></summary>{skipped_html}</details>
<details><summary>Unmapped holdings <span class="count">({len(c.unmapped)})</span></summary>{unmapped_html}</details>
<details><summary>Config used</summary>{cfg_html}</details>
<script type="application/json" id="data">{data_json}</script>
<script>{JS}</script>
</main></body></html>
"""


def write_html(report: Report, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_html(report), encoding="utf-8")
    return path
