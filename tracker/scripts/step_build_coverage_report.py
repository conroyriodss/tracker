"""Build an offline exploratory coverage report from hash-verified catalog exports.

Coverage flags describe what the compiler recorded, not FAIRness or whether a
capability is present. This report performs no source retrieval or adjudication.
"""
import argparse
import csv
import hashlib
import html
import io
import json
from pathlib import Path


def build_report(catalog: Path, output: Path, source_url: str = "") -> dict:
    manifest = json.loads((catalog / 'manifest.json').read_text())
    raw = (catalog / 'field_coverage.csv').read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    if sha != manifest['artifacts']['field_coverage.csv']['sha256']:
        raise ValueError('Coverage CSV does not match catalog manifest SHA-256')
    rows = list(csv.DictReader(io.StringIO(raw.decode('utf-8'))))
    if len(rows) != manifest['counts']['resources']:
        raise ValueError('Coverage denominator does not match catalog resource count')
    if not rows or len({r['repo_uid'] for r in rows}) != len(rows):
        raise ValueError('Coverage requires nonempty, unique resource IDs')
    fields = [k for k in rows[0] if k.endswith('_documented')]
    for row in rows:
        if any(row[f] not in {'0', '1'} for f in fields):
            raise ValueError('Coverage flags must be explicit 0 or 1; blanks are unresolved')
    esc = lambda x: html.escape(str(x), quote=True)
    labels = {f: f.removesuffix('_documented').replace('_', ' ').capitalize() for f in fields}
    counts = {f: sum(int(r[f]) for r in rows) for f in fields}
    n = len(rows)
    bars = ''.join(f'<div class="metric"><div class="metriclabel">{esc(labels[f])}<span>{counts[f]} / {n}</span></div>'
                   f'<div class="track" role="img" aria-label="{esc(labels[f])}: {counts[f]} of {n} recorded"><div style="width:{100*counts[f]/n:.4f}%"></div></div></div>'
                   for f in sorted(fields, key=lambda f: (counts[f], f)))
    options = ''.join(f'<option value="{esc(f)}">{esc(labels[f])}</option>' for f in fields)
    payload = json.dumps({'rows': rows, 'fields': fields, 'labels': labels}, ensure_ascii=False).replace('<', '\\u003c').replace('&', '\\u0026')
    link = f'<a href="{esc(source_url)}">Pinned source commit</a>' if source_url.startswith('https://github.com/') else 'Local catalog export'
    page = '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>NIH Tracker · Evidence coverage</title>
<style>:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;background:#0b1323;color:#e5edf8;font:16px/1.55 system-ui,sans-serif}main{max-width:1280px;margin:auto;padding:42px 30px}h1{font-size:42px;line-height:1.12;letter-spacing:-.035em;margin:14px 0}h2{font-size:23px;margin-top:36px}.eyebrow{font-size:12px;color:#7ce0cf;letter-spacing:.16em}.sub{color:#a9bbd5;max-width:850px}.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin:28px 0}.card{padding:22px;background:#15233a;border:1px solid #293e5c;border-radius:12px}.num{font-size:34px;font-weight:700}.small{color:#a9bbd5;font-size:13px}.note{background:#25283a;border-left:3px solid #e5b968;padding:16px 20px;margin:24px 0}.grid{display:grid;grid-template-columns:1fr 1fr;gap:20px 44px;padding:24px;background:#101d31;border:1px solid #283b58;border-radius:12px}.metriclabel{display:flex;justify-content:space-between;gap:15px;font-size:14px;margin-bottom:7px}.metriclabel span{color:#a9bbd5;white-space:nowrap}.track{height:9px;background:#34425a;border-radius:6px;overflow:hidden}.track div{height:100%;background:#70cfbf}.controls{display:flex;flex-wrap:wrap;gap:16px;align-items:end;margin:20px 0}label{font-size:13px;color:#b0c1d9;display:grid;gap:6px}input,select{font:inherit;background:#15233a;color:#eff5ff;border:1px solid #3b5275;border-radius:7px;padding:10px;max-width:100%}input{min-width:270px}table{border-collapse:collapse;width:100%;font-size:14px}td,th{text-align:left;padding:12px;border-bottom:1px solid #293e5c}th{color:#91d9cc;position:sticky;top:0;background:#101d31}td:first-child{width:80px}.tablewrap{max-height:540px;overflow:auto;border:1px solid #293e5c;border-radius:10px}.badge{padding:4px 9px;border-radius:12px;white-space:nowrap;background:#193c3b;color:#a3e8da}.missing{background:#303b50;color:#d4dfef}code{font-size:12px;overflow-wrap:anywhere}a{color:#9dcaff}.footer{margin-top:36px;color:#a9bbd5;font-size:13px}@media(max-width:700px){main{padding:25px 18px}h1{font-size:32px}.cards,.grid{grid-template-columns:1fr}.controls{display:block}label{margin:12px 0}input{min-width:0;width:100%}}</style></head><body><main>
<div class="eyebrow">NIH TRACKER / EXPLORATORY CATALOG DIAGNOSTIC</div><h1>What does the catalog document?</h1>
<p class="sub">An evidence-coverage view of the published resource inventory. Use it to prioritize extraction and curation, then inspect the underlying claims before drawing conclusions about capability.</p>
<div class="cards"><div class="card"><div class="num">__N__</div>Resources in this snapshot<div class="small">One distinct resource ID per row</div></div><div class="card"><div class="num">__FIELDS__</div>Documentation fields<div class="small">Separate indicators; no composite score</div></div><div class="card"><div class="num">__DATE__</div>Catalog as of<div class="small">Historical snapshot; not a live assessment</div></div></div>
<div class="note"><strong>“Not recorded” does not mean “not available.”</strong> A zero can reflect missing source data, extraction or mapping gaps, or an unresolved assessment. This export does not identify the cause. A documented field can describe a negative finding; it is not necessarily a positive capability.</div>
<h2>Recorded field coverage</h2><p class="small">Every bar uses all __N__ resources as its denominator. Teal = recorded; gray = not recorded in this export.</p><div class="grid">__BARS__</div>
<h2>Inspect the resources behind a field</h2><div class="controls"><label>Field<select id="field">__OPTIONS__</select></label><label>Recorded state<select id="state"><option value="all">All resources</option><option value="1">Recorded</option><option value="0">Not recorded</option></select></label><label>Find a resource<input id="search" type="search" placeholder="Name or resource ID"></label></div><p id="resultcount" class="small" aria-live="polite"></p>
<div class="tablewrap"><table><thead><tr><th>ID</th><th>Resource</th><th>Selected field</th></tr></thead><tbody id="rows"></tbody></table></div>
<h2>Before interpreting a gap</h2><p class="sub">Check the source record, the field mapping, and the assessment method. Resource type and lifecycle stage should determine applicability. This CSV does not supply an applicability denominator, so the view reports raw documentation coverage only.</p>
<div class="footer">__LINK__ · Catalog ID <code>__ID__</code><br>Coverage CSV SHA-256 <code>__SHA__</code><br>CSV hash and resource denominator verified against the supplied catalog manifest. No new repository claims or human decisions were generated.</div>
</main><script>const DATA=__PAYLOAD__;const field=document.getElementById('field'),state=document.getElementById('state'),search=document.getElementById('search'),body=document.getElementById('rows');
function render(){const q=search.value.toLocaleLowerCase();const rows=DATA.rows.filter(r=>(state.value==='all'||r[field.value]===state.value)&&(r.name+' '+r.repo_uid).toLocaleLowerCase().includes(q));body.replaceChildren();for(const r of rows){const tr=document.createElement('tr');for(const text of [r.repo_uid,r.name]){const td=document.createElement('td');td.textContent=text;tr.append(td)}const td=document.createElement('td'),s=document.createElement('span');s.className='badge'+(r[field.value]==='1'?'':' missing');s.textContent=r[field.value]==='1'?'Recorded':'Not recorded';td.append(s);tr.append(td);body.append(tr)}document.getElementById('resultcount').textContent=rows.length+' of '+DATA.rows.length+' resources · '+DATA.labels[field.value]}
field.value=DATA.fields.includes('data_format_documented')?'data_format_documented':DATA.fields[0];for(const el of [field,state,search])el.addEventListener('input',render);render();</script></body></html>'''
    for key, value in {'__N__':str(n), '__FIELDS__':str(len(fields)), '__DATE__':esc(manifest['catalog_as_of']),
                       '__BARS__':bars, '__OPTIONS__':options, '__LINK__':link, '__ID__':esc(manifest['catalog_id']),
                       '__SHA__':sha, '__PAYLOAD__':payload}.items():
        page = page.replace(key, value)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(page, encoding='utf-8')
    return {'resources': n, 'coverage_sha256': sha, 'fields': counts, 'output_sha256': hashlib.sha256(output.read_bytes()).hexdigest()}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--source-url', default='')
    args = parser.parse_args()
    print(json.dumps(build_report(args.catalog, args.out, args.source_url), indent=2))
