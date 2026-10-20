# SPDX-License-Identifier: GPL-3.0-only
import csv
from datetime import datetime,timezone
import hashlib
import html
import json
from pathlib import Path
from .version import VERSION


def text_report(report):
    options = report['options']
    lines = [f'Survey Adjustment {VERSION} – repeatability check',
             'SYNTHETIC EXAMPLE DATA' if report.get('synthetic') else 'Coordinate check – not raw observation adjustment',
             report['method'],
             f"{options['crs']}, {options['height']}, coordinates and tolerances in metres.",
             f"Used {report['used_count']} of {report['input_count']} measurements. Excluded {len(report['excluded'])}.",
             f"Selected tolerances: horizontal {options['horizontal_tolerance']:g} m, vertical {options['vertical_tolerance']:g} m.",
             'Spread = maximum distance from the unweighted mean. Standard deviation = sample standard deviation (n−1).',
             'Both individual measurements and the mean are checked against the reference.']
    for group in report['groups']:
        lines += ['',f"{group['group']}: {group['status']} – {group['count']} measurements",
                  'Mean E/N/Z: '+', '.join(f'{v:.4f}' for v in group['mean']),
                  f"Maximum residual: horizontal {group['max_horizontal_residual']:.4f} m, vertical {group['max_vertical_residual']:.4f} m."]
        if group['reference_delta']:
            lines.append('Mean minus reference E/N/Z: '+', '.join(f'{v:+.4f}' for v in group['reference_delta']))
        lines += group['reasons']
    lines += ['','Excluded measurements:']
    lines += [f"Row {o['row']}: {o['name']} ({o['group']}) – {o['reason']}" for o in report['excluded']]
    lines += ['','Notes:']+report['warnings']+report['limitations']
    lines += ['','Source files:']
    for source in report.get('sources',[]):
        lines += [source['path'],'SHA256: '+source['sha256']]
    return '\n'.join(lines)+'\n'


def save_bundle(parent,report):
    folder = Path(parent)/('Survey_Adjustment_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    folder.mkdir(parents=False,exist_ok=False)
    (folder/'check.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    (folder/'check.txt').write_text(text_report(report),encoding='utf-8')
    (folder/'check.html').write_text('<!doctype html><html lang="en"><meta charset="utf-8"><title>Repeatability check</title>'
        '<style>body{font:16px system-ui;max-width:1000px;margin:2em auto;padding:0 1em}pre{white-space:pre-wrap;line-height:1.5}</style><h1>Survey Adjustment</h1><pre>'+
        html.escape(text_report(report))+'</pre></html>',encoding='utf-8')
    with (folder/'means.csv').open('w',encoding='utf-8-sig',newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(['Control ID','Easting','Northing','Elevation','Count','Status','CRS','Height datum'])
        for g in report['groups']:
            writer.writerow([g['group'],*(f'{v:.9f}' for v in g['mean']),g['count'],g['status'],report['options']['crs'],report['options']['height']])
    with (folder/'residuals.csv').open('w',encoding='utf-8-sig',newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(['Row','Name','Control ID','Easting','Northing','Elevation','dE','dN','dZ','Exceed'])
        for o in report['observations']:
            writer.writerow([o['row'],o['name'],o['group'],o['e'],o['n'],o['z'],*o['delta'],o['exceed']])
    hashes = {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir() if p.is_file()}
    (folder/'sha256.json').write_text(json.dumps(hashes,indent=2),encoding='utf-8')
    return folder
