# SPDX-License-Identifier: GPL-3.0-only
import csv
from datetime import datetime,timezone
import hashlib
import html
import json
from pathlib import Path


def text_report(report):
    options = report['options']
    lines = ['Survey Adjustment 0.1.0 – gjentakskontroll',
             'SYNTETISKE EKSEMPELDATA' if report.get('synthetic') else 'Koordinatkontroll – ikke råobservasjonsutjevning',
             report['method'],
             f"{options['crs']}, {options['height']}, koordinater og grenser i meter.",
             f"Brukt {report['used_count']} av {report['input_count']} målinger. Utelatt {len(report['excluded'])}.",
             f"Valgte grenser: plan {options['horizontal_tolerance']:g} m, høyde {options['vertical_tolerance']:g} m.",
             'Spredning = største avstand fra uvektet middel. Standardavvik = utvalgsstandardavvik (n−1).',
             'Både enkeltmålinger og middel kontrolleres mot referansen.']
    for group in report['groups']:
        lines += ['',f"{group['group']}: {group['status']} – {group['count']} målinger",
                  'Middel E/N/Z: '+', '.join(f'{v:.4f}' for v in group['mean']),
                  f"Største restavvik: plan {group['max_horizontal_residual']:.4f} m, høyde {group['max_vertical_residual']:.4f} m."]
        if group['reference_delta']:
            lines.append('Middel minus referanse E/N/Z: '+', '.join(f'{v:+.4f}' for v in group['reference_delta']))
        lines += group['reasons']
    lines += ['','Utelatte målinger:']
    lines += [f"Rad {o['row']}: {o['name']} ({o['group']}) – {o['reason']}" for o in report['excluded']]
    lines += ['','Merknader:']+report['warnings']+report['limitations']
    lines += ['','Kildefiler:']
    for source in report.get('sources',[]):
        lines += [source['path'],'SHA256: '+source['sha256']]
    return '\n'.join(lines)+'\n'


def save_bundle(parent,report):
    folder = Path(parent)/('Survey_Adjustment_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    folder.mkdir(parents=False,exist_ok=False)
    (folder/'kontroll.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    (folder/'kontroll.txt').write_text(text_report(report),encoding='utf-8')
    (folder/'kontroll.html').write_text('<!doctype html><html lang="nb"><meta charset="utf-8"><title>Gjentakskontroll</title>'
        '<style>body{font:16px system-ui;max-width:1000px;margin:2em auto;padding:0 1em}pre{white-space:pre-wrap;line-height:1.5}</style><h1>Survey Adjustment</h1><pre>'+
        html.escape(text_report(report))+'</pre></html>',encoding='utf-8')
    with (folder/'middelverdier.csv').open('w',encoding='utf-8-sig',newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(['Control ID','Easting','Northing','Elevation','Count','Status','CRS','Height datum'])
        for g in report['groups']:
            writer.writerow([g['group'],*(f'{v:.9f}' for v in g['mean']),g['count'],g['status'],report['options']['crs'],report['options']['height']])
    with (folder/'restavvik.csv').open('w',encoding='utf-8-sig',newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(['Row','Name','Control ID','Easting','Northing','Elevation','dE','dN','dZ','Exceed'])
        for o in report['observations']:
            writer.writerow([o['row'],o['name'],o['group'],o['e'],o['n'],o['z'],*o['delta'],o['exceed']])
    hashes = {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir() if p.is_file()}
    (folder/'sha256.json').write_text(json.dumps(hashes,indent=2),encoding='utf-8')
    return folder
