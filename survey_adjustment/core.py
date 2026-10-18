# SPDX-License-Identifier: GPL-3.0-only
"""Strict coordinate CSV reader and descriptive repeatability checks.

These statistics are not a GNSS raw-observation/network adjustment. All distances
are in the input projected grid; no scale, datum or height correction is applied.
"""
from dataclasses import dataclass, asdict
from collections import defaultdict
import csv
import hashlib
import io
import math
import re
from pathlib import Path
import statistics


ALIASES = {
    'name': ['Name', 'Observation', 'Obs ID', 'Point name', 'Point', 'Punktnavn', 'Punkt'],
    'group': ['Control ID', 'Control Point', 'Kontrollpunkt', 'Point ID', 'Target ID', 'Name', 'Point name', 'Point'],
    'e': ['Easting', 'East', 'Øst', 'Oest', 'E', 'E_m'],
    'n': ['Northing', 'North', 'Nord', 'N', 'N_m'],
    'z': ['Elevation', 'Height', 'Høyde', 'Z', 'H_m', 'Z_m'],
    'status': ['Solution status', 'Solution', 'RTK status', 'Fix type', 'Status'],
    'time': ['Averaging start', 'Timestamp', 'Start time', 'Tid'],
}


def key(text):
    return ''.join(c for c in text.casefold() if c.isalnum())


def read_table(path):
    path = Path(path)
    data = path.read_bytes()
    if len(data) > 25_000_000:
        raise ValueError('CSV-filen er større enn utkastets grense på 25 MB.')
    encoding = 'utf-8-sig'
    try:
        text = data.decode(encoding)
    except UnicodeDecodeError:
        encoding = 'cp1252'
        text = data.decode(encoding)
    try:
        delimiter = csv.Sniffer().sniff(text[:32768], delimiters=',;\t').delimiter
    except csv.Error:
        raise ValueError('Kunne ikke bestemme CSV-skilletegn. Bruk komma, semikolon eller tabulator.') from None
    reader = csv.reader(io.StringIO(text), delimiter=delimiter, strict=True)
    try:
        headers = [h.strip() for h in next(reader)]
        if any(not h for h in headers) or len(set(map(key,headers))) != len(headers):
            raise ValueError('CSV har tomme eller dupliserte kolonnenavn.')
        if any(re.fullmatch(r'[+-]?(?:\d+(?:[.,]\d*)?|[.,]\d+)(?:[eE][+-]?\d+)?',h) for h in headers):
            raise ValueError('CSV må ha en overskriftsrad med tekstlige kolonnenavn. Første rad ser ut som måledata; legg til kolonnenavn før import.')
        rows = []
        for row in reader:
            if not row or all(not value.strip() for value in row):
                continue
            if len(row) != len(headers):
                raise ValueError(f'CSV-linje {reader.line_num}: feil antall kolonner.')
            rows.append(dict(zip(headers,[value.strip() for value in row]), __line__=reader.line_num))
    except (StopIteration,csv.Error) as error:
        raise ValueError('Tom eller ugyldig CSV: ' + str(error)) from None
    if not rows:
        raise ValueError('CSV-filen inneholder ingen målinger.')
    return dict(path=str(path.resolve()), sha256=hashlib.sha256(data).hexdigest(),
                encoding=encoding, delimiter=delimiter, headers=headers, rows=rows)


def default_mapping(headers):
    return {field: next((h for alias in aliases for h in headers if key(h) == key(alias)), '')
            for field,aliases in ALIASES.items()}


def number(text, field, line):
    try:
        value = float(str(text).strip().replace(',', '.'))
    except (TypeError,ValueError):
        raise ValueError(f'Linje {line}: {field} må være et tall i meter.') from None
    if not math.isfinite(value):
        raise ValueError(f'Linje {line}: {field} må være endelig; tomme verdier/NaN godtas ikke.')
    return value


@dataclass
class Observation:
    row: int
    name: str
    group: str
    e: float
    n: float
    z: float
    status: str = ''
    time: str = ''


@dataclass
class Options:
    crs: str = 'EPSG:25832'
    height: str = 'NN2000'
    confirmed: bool = False
    only_fix: bool = True
    horizontal_tolerance: float = 0.03
    vertical_tolerance: float = 0.05


def observations(table, mapping):
    for field in ('name','group','e','n','z'):
        if mapping.get(field) not in table['headers']:
            raise ValueError('Velg kildekolonne for ' + field + '.')
    result = []
    for row in table['rows']:
        line = row['__line__']
        name, group = (row[mapping[field]].strip() for field in ('name','group'))
        if not name or not group:
            raise ValueError(f'Linje {line}: punktnavn og kontrollpunkt-ID må være fylt inn.')
        result.append(Observation(line,name,group,*(number(row[mapping[a]],a,line) for a in ('e','n','z')),
                                  row.get(mapping.get('status'),''),row.get(mapping.get('time'),'')))
    return result


def references(table, mapping):
    result = {}
    for obs in observations(table,mapping):
        if obs.group in result:
            raise ValueError('Duplisert kontrollpunkt i referansefilen: ' + obs.group)
        result[obs.group] = (obs.e,obs.n,obs.z)
    return result


def analyze(items, refs, options):
    if options.crs not in {f'EPSG:258{z}' for z in range(31,37)} or not options.confirmed:
        raise ValueError('Velg og bekreft at begge filer bruker samme EUREF89/UTM og høydegrunnlag i meter.')
    if options.height not in ('NN2000','NN54','ELLIP','LOKAL'):
        raise ValueError('Velg høydegrunnlag.')
    for value in (options.horizontal_tolerance,options.vertical_tolerance):
        if not math.isfinite(value) or value <= 0:
            raise ValueError('Kontrollgrensene må være større enn null.')
    groups, excluded, warnings = defaultdict(list), [], []
    for obs in items:
        if not (0 <= obs.e <= 1_000_000 and 0 <= obs.n <= 10_000_000):
            raise ValueError(f'Rad {obs.row}: E/N er utenfor forventet UTM-område. Kontroller akserekkefølge, CRS og enhet.')
        if options.only_fix and obs.status.upper().replace(' ','_') not in ('FIX','FIXED','RTK_FIX','RTK_FIXED'):
            excluded.append(dict(row=obs.row,name=obs.name,group=obs.group,reason='Ikke dokumentert FIX: ' + (obs.status or '(tom status)')))
        else:
            groups[obs.group].append(obs)
    if not groups:
        raise ValueError('Ingen målinger igjen etter FIX-filteret. Kontroller statuskolonnen og filtervalget.')
    summaries, residuals = [], []
    for group, values in sorted(groups.items()):
        coords = [(o.e,o.n,o.z) for o in values]
        mean = tuple(math.fsum(c[a] for c in coords)/len(coords) for a in range(3))
        sd = tuple(statistics.stdev(c[a] for c in coords) if len(coords)>1 else None for a in range(3))
        max_h = max(math.hypot(c[0]-mean[0],c[1]-mean[1]) for c in coords)
        max_v = max(abs(c[2]-mean[2]) for c in coords)
        ref = refs.get(group)
        delta = tuple(mean[a]-ref[a] for a in range(3)) if ref else None
        ref_h = math.hypot(*delta[:2]) if delta else None
        reasons = []
        if len(values)<2: reasons.append('Bare én måling; gjentak kan ikke vurderes')
        if max_h > options.horizontal_tolerance: reasons.append('Spredning i plan over grensen')
        if max_v > options.vertical_tolerance: reasons.append('Spredning i høyde over grensen')
        if ref_h is not None and ref_h > options.horizontal_tolerance: reasons.append('Middelavvik mot kontroll i plan over grensen')
        if delta and abs(delta[2]) > options.vertical_tolerance: reasons.append('Middelavvik mot kontroll i høyde over grensen')
        if not ref: reasons.append('Ingen referanse; absolutt avvik kan ikke vurderes')
        if not options.only_fix and any(o.status.upper() not in ('FIX','FIXED','RTK FIX','RTK_FIXED','RTK_FIX') for o in values):
            reasons.append('Målinger uten FIX er med i middelverdien')
        times = [o.time for o in values if o.time]
        if len(times)!=len(values): warnings.append(group + ': måletid mangler for minst én måling')
        if len(set(times)) != len(times): warnings.append(group + ': samme måletid er registrert flere ganger')
        status = 'AVVIK' if any('over grensen' in r for r in reasons) else 'UAVKLART' if reasons else 'INNENFOR'
        summary = dict(group=group,count=len(values),mean=mean,sd=sd,max_horizontal_residual=max_h,
                       max_vertical_residual=max_v,reference=ref,reference_delta=delta,status=status,reasons=reasons)
        summaries.append(summary)
        for obs in values:
            d = (obs.e-mean[0],obs.n-mean[1],obs.z-mean[2])
            # Check every observation against the reference, not only a possibly cancelling mean.
            rd = tuple((obs.e,obs.n,obs.z)[a]-ref[a] for a in range(3)) if ref else None
            exceed = math.hypot(*d[:2])>options.horizontal_tolerance or abs(d[2])>options.vertical_tolerance
            if rd and (math.hypot(*rd[:2])>options.horizontal_tolerance or abs(rd[2])>options.vertical_tolerance):
                exceed = True
                if 'Enkeltmåling mot kontroll over grensen' not in reasons:
                    reasons.append('Enkeltmåling mot kontroll over grensen')
                    summary['status'] = 'AVVIK'
            residuals.append(dict(**asdict(obs),mean=mean,delta=d,reference_delta=rd,exceed=exceed))
    missing = sorted({o.group for o in items}-set(groups))
    for group in missing: warnings.append(group + ': alle målinger er utelatt av FIX-filteret')
    return dict(method='Uvektet gjentakskontroll av ferdige koordinater; ikke SALSA-utjevning',
                options=asdict(options),input_count=len(items),used_count=len(residuals),groups=summaries,
                observations=residuals,excluded=excluded,warnings=warnings,
                limitations=['Gjentak kan være korrelerte; spredning dokumenterer ikke absolutt nøyaktighet.',
                             'Ingen automatisk fjerning av uteliggere. FIX er et filter, ikke en kvalitetsgaranti.',
                             'RMS-felt brukes ikke som vekter. Ingen RINEX-/baselineprosessering.',
                             'Kontrollpunkter brukes til sammenligning, ikke til å flytte målingene.',
                             'Ingen datum-, geoid-, antennehøyde- eller koordinattransformasjon i gjentakskontrollen.',
                             'Kontrollgrenser er brukerens valg; ikke mottakerkrav eller en nasjonal standard.'])
