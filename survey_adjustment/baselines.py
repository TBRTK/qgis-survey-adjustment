# SPDX-License-Identifier: GPL-3.0-only
"""Import processed relative GNSS solutions; never process RINEX or average epochs.

RTKLIB field conventions reviewed against upstream solution.c (2026-10-19).
Independent implementation; no RTKLIB/Emlid code or receiver data is bundled.
"""
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timezone
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import re
from uuid import uuid4

from . import network as n
from .coordinate_job import xyz, matmul, transpose

MAX_SOURCE_BYTES = 20 * 1024 * 1024
MAX_ROWS = 10000
CSV_FIELDS = ('baseline_id', 'from_point', 'to_point', 'session', 'dx_m', 'dy_m', 'dz_m',
              'cxx_m2', 'cxy_m2', 'cxz_m2', 'cyy_m2', 'cyz_m2', 'czz_m2', 'status', 'time_utc')
FRAMES = {'EPSG:4936': 'EPSG:4937', 'EPSG:4978': 'EPSG:4979'}
LOCKED_FIELDS = ('id', 'kind', 'session', 'from_point', 'to_point', 'values', 'covariance',
                 'frame', 'instrument_height', 'target_height')
WARNINGS = ('Import only; no network adjustment or quality approval has been performed.',
            'Formal covariance is conditional on the processing base and model. Base, datum, antenna and shared errors are not added.',
            'Epochs from one occupation are correlated. No averaging, sigma/sqrt(n) reduction or automatic independence is assumed.',
            'No antenna, tilt, geoid, datum or epoch corrections are applied during import.')


@dataclass
class ImportOptions:
    frame: str = ''
    epoch: str = ''
    origin: str = ''
    target: str = ''
    session: str = ''
    identifier: str = ''
    use: str = 'ADJUSTMENT'
    antenna_note: str = ''
    uncertainty_note: str = ''
    relative_confirmed: bool = False
    endpoints_confirmed: bool = False
    covariance_confirmed: bool = False
    synthetic: bool = False
    stationary_confirmed: bool = False
    occupation_note: str = ''


def finite(text, line):
    try:
        value = float(text)
        if not math.isfinite(value):
            raise ValueError()
        return value
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f'Line {line}: invalid or non-finite numeric field; source contents withheld.') from None


def packed(matrix):
    return [matrix[0][0], matrix[0][1], matrix[0][2], matrix[1][1], matrix[1][2], matrix[2][2]]


def enu_to_ecef(lat, lon):
    phi, lam = math.radians(lat), math.radians(lon)
    sp, cp, sl, cl = math.sin(phi), math.cos(phi), math.sin(lam), math.cos(lam)
    return [[-sl, -sp*cl, cp*cl], [cl, -sp*sl, cp*sl], [0., cp, sp]]


def matrix(cov):
    xx, xy, xz, yy, yz, zz = cov
    return [[xx, xy, xz], [xy, yy, yz], [xz, yz, zz]]


def geodetic(values):
    n._numbers(values, 3, 'Processing reference / position')
    if not -90 < values[0] < 90 or not -180 <= values[1] <= 180 or not -12000 < values[2] < 100000:
        raise ValueError('Expected decimal latitude, longitude and ellipsoidal height in metres; polar/DMS/geoid profiles are not supported.')


def ecef(values):
    n._numbers(values, 3, 'ECEF position')
    if not 6_000_000 < math.sqrt(sum(v*v for v in values)) < 7_000_000:
        raise ValueError('Expected an Earth-centred absolute position in metres, not a delta or projected coordinate.')


def epoch_text(text, timescale, line):
    value = text.replace('/', '-').replace(' ', 'T')
    try:
        if not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|[+-]\d\d:\d\d)?', value):
            raise ValueError()
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if timescale == 'UTC':
            return stamp.replace(tzinfo=timezone.utc).isoformat().replace('+00:00', 'Z') if stamp.utcoffset() is None else stamp.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
        if stamp.utcoffset() is not None:
            raise ValueError()
        return stamp.isoformat()
    except (ValueError, TypeError):
        raise ValueError(f'Line {line}: expected calendar time with at most six fractional digits in the declared time system; GPS week/TOW and leap seconds are unsupported.') from None


def metadata_keys():
    return dict(representation='', mode='', time_system='', reference=None)


def validate_metadata(meta, fmt):
    n._keys(meta, metadata_keys(), 'Baseline source metadata')
    if fmt == 'RTKLIB_POS_1':
        if meta['representation'] not in ('LLH', 'XYZ', 'ENU') or meta['mode'] not in ('static', 'kinematic') or meta['time_system'] not in ('UTC', 'GPST'):
            raise ValueError('Only documented static/kinematic relative RTKLIB POS with calendar UTC/GPST is supported.')
        (ecef if meta['representation'] == 'XYZ' else geodetic)(meta['reference'])
    elif fmt == 'GNSS_BASELINE_CSV_1':
        if meta != dict(representation='DELTA_XYZ', mode='processed-relative-vector', time_system='UTC', reference=None):
            raise ValueError('Unsupported baseline CSV metadata.')
    else:
        raise ValueError('Unsupported measured-baseline source format.')


def to_ecef(values, cov, meta, frame):
    if frame not in FRAMES:
        raise ValueError('Select the documented ECEF frame; no datum is inferred from a receiver brand or POS label.')
    n._numbers(values, 3, 'Source vector / position')
    n.validate_covariance(cov)
    kind, ref = meta['representation'], meta['reference']
    if kind == 'DELTA_XYZ':
        return list(values), list(cov), None
    if kind == 'XYZ':
        ecef(values); ecef(ref)
        return [v-r for v, r in zip(values, ref)], list(cov), list(ref)
    geodetic(ref)
    # RTKLIB writes native LLH/ref-pos on its WGS84 ellipsoid even when the
    # geocentric processing coordinates belong to a separately documented frame.
    # Decode that representation; do not change ellipsoid from the output-frame label.
    reference = xyz(*ref, 'EPSG:4979')
    if kind == 'LLH':
        geodetic(values)
        result = [v-r for v, r in zip(xyz(*values, 'EPSG:4979'), reference)]
        rotation = enu_to_ecef(values[0], values[1])  # POS LLH covariance is local at the rover.
    elif kind == 'ENU':
        rotation = enu_to_ecef(ref[0], ref[1])  # ENU baseline/covariance are local at the base.
        result = [row[0] for row in matmul(rotation, [[v] for v in values])]
    else:
        raise ValueError('Unsupported vector representation.')
    converted = packed(matmul(matmul(rotation, matrix(cov)), transpose(rotation)))
    n.validate_covariance(converted)
    return result, converted, reference


def _pos(text, processing_mode=None):
    lines = text.splitlines()
    header, reference, mode, height_label = None, None, None, ''
    data_started = False
    for line in lines:
        if not line.lstrip().startswith('%'):
            if line.strip(): data_started = True
            continue
        clean = line.lstrip()[1:].strip()
        if data_started and (clean.lower().startswith(('pos mode','ref pos')) or 'lat/lon/height=' in clean or re.match(r'^(?:GPST|UTC|JST)\b', clean)):
            raise ValueError('POS structural headers must precede all solution data.')
        if clean.lower().startswith('pos mode'):
            if mode is not None or ':' not in clean:
                raise ValueError('Duplicate or invalid POS processing-mode header.')
            mode = clean.split(':', 1)[1].strip().lower()
        if clean.lower().startswith('ref pos'):
            if reference is not None or ':' not in clean:
                raise ValueError('Duplicate or invalid POS processing-base header.')
            reference = [finite(v, 0) for v in re.split(r'[\s,;]+', clean.split(':', 1)[1].strip())]
        if 'lat/lon/height=' in clean:
            if height_label:
                raise ValueError('Duplicate POS coordinate/height declaration.')
            height_label = clean
        if re.match(r'^(?:GPST|UTC|JST)\b', clean) and any(v in clean for v in ('latitude(', 'x-ecef(', 'e-baseline(')):
            if header is not None:
                raise ValueError('Multiple POS solution headers are unsupported; split files explicitly.')
            header = re.split(r'[\s,;]+', clean)
    if header is None:
        raise ValueError('No supported RTKLIB solution header. Preserve output headers; LLH, XYZ and ENU profiles require documented units and full covariance.')
    if 'latitude(deg)' in header:
        representation = 'LLH'
        expected = ['latitude(deg)', 'longitude(deg)', 'height(m)', 'Q', 'ns', 'sdn(m)', 'sde(m)', 'sdu(m)', 'sdne(m)', 'sdeu(m)', 'sdun(m)', 'age(s)', 'ratio']
        if 'lat/lon/height=WGS84/ellipsoidal' not in height_label:
            raise ValueError('LLH POS requires an explicit WGS84/ellipsoidal output declaration. Geoid/DMS/ambiguous heights are not converted.')
    elif 'x-ecef(m)' in header:
        representation = 'XYZ'
        expected = ['x-ecef(m)', 'y-ecef(m)', 'z-ecef(m)', 'Q', 'ns', 'sdx(m)', 'sdy(m)', 'sdz(m)', 'sdxy(m)', 'sdyz(m)', 'sdzx(m)', 'age(s)', 'ratio']
    else:
        representation = 'ENU'
        expected = ['e-baseline(m)', 'n-baseline(m)', 'u-baseline(m)', 'Q', 'ns', 'sde(m)', 'sdn(m)', 'sdu(m)', 'sden(m)', 'sdnu(m)', 'sdue(m)', 'age(s)', 'ratio']
    if header[1:] != expected:
        raise ValueError('Unsupported POS column order or missing covariance. Export the documented full RTKLIB header; values are not guessed.')
    if processing_mode is not None and processing_mode not in ('static','kinematic'):
        raise ValueError('Operator-declared POS processing mode must be static or kinematic.')
    if mode is None:
        if processing_mode is None:
            raise ValueError('POS header omits processing mode. Select the documented Static or Kinematic workflow explicitly; receiver motion alone does not establish processing mode.')
        mode = processing_mode
    elif processing_mode is not None and mode != processing_mode:
        raise ValueError('Operator-declared processing mode conflicts with the POS header; no header is overridden.')
    meta = dict(representation=representation, mode=mode, time_system=header[0], reference=reference)
    validate_metadata(meta, 'RTKLIB_POS_1')
    rows, times = [], set()
    for index, raw in enumerate(lines, 1):
        if not raw.strip() or raw.lstrip().startswith('%'):
            continue
        values = re.split(r'[\s,;]+', raw.strip())
        if len(values) != 15:
            raise ValueError(f'Line {index}: expected date, time and thirteen POS fields; no row was imported.')
        stamp = epoch_text(values[0]+' '+values[1], meta['time_system'], index)
        if stamp in times:
            raise ValueError(f'Line {index}: duplicate solution epoch; split/verify combined files before importing.')
        times.add(stamp)
        numbers = [finite(v, index) for v in values[2:]]
        q, ns = numbers[3:5]
        if q != int(q) or q not in range(7) or ns != int(ns) or ns < 0:
            raise ValueError(f'Line {index}: unsupported solution quality or satellite count.')
        source = numbers[:3]
        sd1, sd2, sd3 = numbers[5:8]
        cross1, cross2, cross3 = (v*abs(v) for v in numbers[8:11])
        # Signed square roots of cross-covariances, not correlation coefficients.
        if representation == 'LLH':
            cov = [sd2**2, cross1, cross2, sd1**2, cross3, sd3**2]
        else:
            cov = [sd1**2, cross1, cross3, sd2**2, cross2, sd3**2]
        eligible, reason = q == 1, '' if q == 1 else 'Not FIX (Q=1 required)'
        try:
            if min(sd1, sd2, sd3) <= 0:
                raise ValueError('Missing/non-positive standard deviations')
            n.validate_covariance(cov)
            if representation == 'LLH': geodetic(source)
            if representation == 'XYZ': ecef(source)
        except ValueError:
            eligible, reason = False, 'Invalid or rounded non-positive-definite covariance / coordinate'
        rows.append(dict(line=index, time=stamp, quality=str(int(q)), raw=raw,
                         values=source, covariance=cov, eligible=eligible, reason=reason,
                         identifier='', origin='', target='', session=''))
        if len(rows) > MAX_ROWS:
            raise ValueError('POS exceeds the 10000-epoch preview limit. Export a documented static/occupation subset.')
    return 'RTKLIB_POS_1', meta, rows


def _csv(text):
    first = text.splitlines()[0] if text.splitlines() else ''
    delimiters = [d for d in (',', ';', '\t') if first.split(d) == list(CSV_FIELDS)]
    if len(delimiters) != 1:
        raise ValueError('Baseline CSV requires the exact documented baseline_id/from_point/to_point/session/dx_m…covariance/status/time_utc header. Coordinate point lists are not baselines.')
    delimiter = delimiters[0]
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter, strict=True)
    rows, previous_line = [], 1
    raw_lines = text.splitlines(keepends=True)
    for record in reader:
        line = reader.line_num
        if None in record or any(v is None for v in record.values()):
            raise ValueError(f'Line {line}: wrong CSV field count.')
        def number(name):
            value = record[name]
            return finite(value.replace(',', '.') if delimiter != ',' else value, line)
        values = [number(name) for name in ('dx_m', 'dy_m', 'dz_m')]
        cov = [number(name) for name in ('cxx_m2', 'cxy_m2', 'cxz_m2', 'cyy_m2', 'cyz_m2', 'czz_m2')]
        n.validate_covariance(cov)
        stamp = record['time_utc']
        if not re.search(r'(?:Z|[+-]\d\d:\d\d)$', stamp):
            raise ValueError(f'Line {line}: time_utc requires Z or a UTC offset.')
        raw = ''.join(raw_lines[previous_line:line]).rstrip('\r\n')
        previous_line = line
        rows.append(dict(line=line, time=epoch_text(stamp, 'UTC', line), quality=record['status'],
                         raw=raw, values=values, covariance=cov,
                         eligible=record['status']=='FIX', reason='' if record['status']=='FIX' else 'Not FIX',
                         identifier=record['baseline_id'], origin=record['from_point'], target=record['to_point'], session=record['session']))
        if len(rows) > MAX_ROWS: raise ValueError('Baseline CSV exceeds 10000 rows.')
    return 'GNSS_BASELINE_CSV_1', dict(representation='DELTA_XYZ', mode='processed-relative-vector', time_system='UTC', reference=None), rows


def parse_source(payload, name, *, processing_mode=None):
    if not isinstance(payload, bytes) or len(payload) > MAX_SOURCE_BYTES:
        raise ValueError('Baseline input must be at most 20 MB of text.')
    try:
        text = payload.decode('utf-8-sig')
        if '\x00' in text:
            raise ValueError('Binary baseline input is unsupported.')
        is_pos = any(line.lstrip().startswith('%') for line in text.splitlines())
        if processing_mode is not None and not is_pos:
            raise ValueError('An operator-declared processing mode applies only to POS sources, not baseline CSV.')
        fmt, metadata, rows = _pos(text, processing_mode) if is_pos else _csv(text)
    except (UnicodeError, csv.Error, OverflowError):
        raise ValueError('Invalid baseline text encoding, numeric range or CSV structure; source contents withheld.') from None
    if not rows:
        raise ValueError('Baseline source contains no observations.')
    declared = (f'POS processing mode omitted from original header; operator explicitly declared {processing_mode} workflow.'
                if is_pos and not re.search(r'^\s*%\s*pos mode\b',text,re.MULTILINE|re.IGNORECASE) else '')
    return dict(format=fmt, source_name=Path(name).name, source_sha256=hashlib.sha256(payload).hexdigest(),
                metadata=metadata, rows=rows, marked_synthetic='SYNTHETIC' in text.upper(), mode_declaration=declared)


def read_source(path, *, processing_mode=None):
    from .gnss_processing import source_for_import
    processed=source_for_import(path)
    if processed is not None:
        if processing_mode and processing_mode!=processed['metadata']['mode']:
            raise ValueError('Processing mode conflicts with the verified raw job.')
        return processed
    with Path(path).open('rb') as handle:
        data = handle.read(MAX_SOURCE_BYTES+1)
    return parse_source(data, Path(path).name, processing_mode=processing_mode)


def content_fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def fingerprint(fmt, meta, time, values, cov):
    return content_fingerprint([fmt, meta, time, values, cov])


def ensure_unique_import(batches, source_sha256, identity, line=None):
    """Shared source/record identity gate for processed and raw import adapters."""
    for batch in batches:
        for old in batch.get('records', [batch]):
            if old['fingerprint']==identity or (batch['source_sha256']==source_sha256 and old.get('line')==line):
                raise ValueError('This source observation was already imported (including under another filename/session).')


def imported_snapshot(project, identifier):
    for batch in project['imports']:
        for row in batch['records']:
            if row['observation']['id'] == identifier:
                return row['observation']
    return None


def plan_import(project, source, line, options):
    """Return a new project; no source/project is modified, even on a late failure."""
    n.validate_project(project)
    from .gnss_processing import validate_handoff
    validate_handoff(project,source,options)
    if not isinstance(options, ImportOptions) or not all(type(v) is bool and v for v in (options.relative_confirmed, options.endpoints_confirmed, options.covariance_confirmed)):
        raise ValueError('Confirm the relative solution, physical endpoints/height treatment and covariance/dependence review.')
    for key in ('frame','epoch','origin','target','session','identifier','use'):
        n._text(getattr(options,key), key, required=True)
    if options.frame not in FRAMES:
        raise ValueError('Select the documented ECEF reference frame.')
    try:
        date.fromisoformat(options.epoch)
    except ValueError:
        raise ValueError('Enter the documented coordinate epoch as YYYY-MM-DD; no transformation is applied.') from None
    if type(options.synthetic) is not bool or project['synthetic'] != options.synthetic:
        raise ValueError('Keep synthetic demonstrations and real surveys in separate network projects.')
    if source['marked_synthetic'] and not options.synthetic:
        raise ValueError('This source is explicitly marked SYNTHETIC; use a separate synthetic network.')
    for text, label in ((options.antenna_note, 'Antenna / physical endpoint treatment'), (options.uncertainty_note, 'Covariance / processing assumptions')):
        n._text(text, label, required=True)
    declaration = source.get('mode_declaration','')
    n._text(declaration, 'POS processing-mode declaration')
    rows = [r for r in source['rows'] if r['line'] == line]
    if len(rows) != 1 or not rows[0]['eligible']:
        raise ValueError('Select exactly one eligible FIX observation with valid full covariance.')
    row, meta = rows[0], source['metadata']
    validate_metadata(meta, source['format'])
    for key in ('identifier', 'origin', 'target', 'session'):
        if row[key] and row[key] != getattr(options, key):
            raise ValueError('CSV point/session/observation IDs must match the source row; no implicit renaming.')
    known_sessions = {s['id']:s for s in project['sessions']}
    if options.session not in known_sessions or not known_sessions[options.session]['start']:
        raise ValueError('Select an existing documented acquisition session with a UTC start time.')
    session = known_sessions[options.session]
    if meta['time_system'] == 'UTC':
        time = n._time(row['time'], 'Source UTC time')
        if time < n._time(session['start'], 'Session start') or (session['end'] and time > n._time(session['end'], 'Session end')):
            raise ValueError('Selected UTC observation lies outside this acquisition session.')
    for old in project['observations']:
        if old['kind']=='GNSS_VECTOR' and old['session']==options.session and {old['from_point'],old['to_point']}=={options.origin,options.target}:
            raise ValueError('A GNSS baseline for these endpoints already exists in this session. Do not turn correlated epochs into repeats.')
    values, cov, reference = to_ecef(row['values'], row['covariance'], meta, options.frame)
    fp = fingerprint(source['format'], meta, row['time'], row['values'], row['covariance'])
    ensure_unique_import(project['imports'],source['source_sha256'],fp,line)
    for batch in project['imports']:
        for old in batch['records']:
            previous = old['observation']
            if (old['time']==row['time'] and batch['metadata']['time_system']==meta['time_system']
                    and batch['frame']==options.frame and {previous['from_point'],previous['to_point']}=={options.origin,options.target}):
                sign = 1 if previous['from_point']==options.origin else -1
                if math.dist(values,[sign*v for v in previous['values']]) < .001:
                    raise ValueError('An equivalent baseline at this source epoch was already imported. Re-exporting or renaming its session does not establish independence.')
        if reference is not None and batch['frame']==options.frame and batch['epoch']==options.epoch and batch['metadata']['reference'] is not None:
            previous_ref = to_ecef(batch['records'][0]['native_values'], batch['records'][0]['native_covariance'], batch['metadata'], batch['frame'])[2]
            if any(r['observation']['from_point']==options.origin for r in batch['records']) and math.dist(reference, previous_ref) > .001:
                raise ValueError('Processing-base coordinates changed by more than 1 mm for this point/frame/epoch. Review or reprocess before merging baselines.')
    obs = dict(n.observation(), id=options.identifier, kind='GNSS_VECTOR', session=options.session,
               from_point=options.origin, to_point=options.target, use=options.use, values=values,
               covariance=cov, frame=options.frame,
               notes='Imported processed relative GNSS observation; original values retained in import audit.')
    batch = dict(id=str(uuid4()), format=source['format'], source_name=source['source_name'],
                 source_sha256=source['source_sha256'], imported_at=datetime.now(timezone.utc).isoformat(),
                 frame=options.frame, epoch=options.epoch, antenna_note=options.antenna_note,
                 uncertainty_note=options.uncertainty_note+('\n'+declaration if declaration else ''), synthetic=options.synthetic,
                 metadata=deepcopy(meta), records=[dict(line=line, time=row['time'], quality=row['quality'], raw=row['raw'],
                 native_values=deepcopy(row['values']), native_covariance=deepcopy(row['covariance']), fingerprint=fp, observation=deepcopy(obs))])
    if source.get('processing'):
        batch['processing']=deepcopy(source['processing'])
        batch['uncertainty_note']+='\nOccupation review: '+options.occupation_note
    result = deepcopy(project)
    result['observations'].append(obs)
    result['imports'].append(batch)
    n.dumps_project(result)  # Also enforce the serializable 20 MB project limit before applying.
    return result


def validate_imports(project):
    imports = project['imports']
    if not isinstance(imports, list) or len(imports) > 10000:
        raise ValueError('Expected at most 10000 import audit records.')
    ids, fingerprints, observations, occupations = set(), set(), {}, set()
    template = dict(id='', format='', source_name='', source_sha256='', imported_at='', frame='', epoch='',
                    antenna_note='', uncertainty_note='', synthetic=False, metadata={}, records=[])
    row_template = dict(line=0, time='', quality='', raw='', native_values=[], native_covariance=[], fingerprint='', observation={})
    for batch in imports:
        expected=set(template)
        if isinstance(batch,dict) and 'processing' in batch: expected.add('processing')
        n._keys(batch, expected, 'Import audit')
        if 'processing' in batch:
            from .gnss_processing import validate_context
            validate_context(batch['processing'])
            context=batch['processing']
            if batch['format']!='RTKLIB_POS_1':raise ValueError('Raw-job provenance requires the original processed POS source.')
            validate_metadata(batch['metadata'],batch['format'])
            if context['occupation_sha256'] in occupations:raise ValueError('A raw occupation cannot appear as two independent imported baselines.')
            occupations.add(context['occupation_sha256'])
            if (batch['frame'],batch['epoch'])!=(context['frame'],context['epoch']):
                raise ValueError('Imported processing frame/epoch differs from its job.')
            if math.dist(batch['metadata']['reference'],context['base_xyz'])>.0002:
                raise ValueError('Processing base differs from its import provenance.')
            raw={row['id']:row for row in project.get('raw_sources',[])}
            if project['project_id']!=context['project_id'] or any(raw.get(row['source_id'],{}).get('fingerprint')!=row['fingerprint'] for row in context['inputs']):
                raise ValueError('Imported raw-processing provenance depends on its original catalogue links/project identity.')
            for entry,point in zip(context['inputs'][:2],(context['base'],context['rover'])):
                if (raw[entry['source_id']]['point'],raw[entry['source_id']]['session'])!=(point,context['session']):
                    raise ValueError('Imported processing point/session bindings cannot be relabelled through the raw catalogue.')
        for field in ('id', 'source_name', 'source_sha256', 'imported_at', 'frame', 'epoch', 'antenna_note', 'uncertainty_note'):
            n._text(batch[field], 'Import '+field, required=True)
        if batch['id'] in ids or '/' in batch['source_name'] or '\\' in batch['source_name'] or ':' in batch['source_name']:
            raise ValueError('Duplicate import ID or invalid source basename.')
        ids.add(batch['id'])
        if not re.fullmatch('[a-f0-9]{64}', batch['source_sha256']):
            raise ValueError('Import source SHA256 is invalid.')
        if batch['frame'] not in FRAMES or type(batch['synthetic']) is not bool or batch['synthetic'] != project['synthetic']:
            raise ValueError('Invalid import frame or mixed synthetic/real source.')
        date.fromisoformat(batch['epoch'])
        if n._time(batch['imported_at'], 'Import time') is None:
            raise ValueError('Import audit requires its timestamp.')
        validate_metadata(batch['metadata'], batch['format'])
        if not isinstance(batch['records'], list) or len(batch['records']) != 1:
            raise ValueError('This import profile records exactly one selected baseline per operation.')
        for row in batch['records']:
            n._keys(row, row_template, 'Imported source row')
            if type(row['line']) is not int or row['line'] < 1:
                raise ValueError('Import source line must be a positive integer.')
            n._text(row['raw'], 'Original source row', required=True)
            if row['quality'] not in ('1','FIX'):
                raise ValueError('Only FIX source rows may be imported.')
            if epoch_text(row['time'], batch['metadata']['time_system'], row['line']) != row['time']:
                raise ValueError('Import source timestamp is not canonical.')
            fp = fingerprint(batch['format'], batch['metadata'], row['time'], row['native_values'], row['native_covariance'])
            if fp != row['fingerprint'] or fp in fingerprints:
                raise ValueError('Import source fingerprint mismatch or duplicate source observation.')
            fingerprints.add(fp)
            obs = row['observation']
            if 'processing' in batch and (obs['from_point'],obs['to_point'],obs['session'])!=(context['base'],context['rover'],context['session']):
                raise ValueError('Imported processing endpoints/session differ from its job.')
            n._keys(obs, n.observation(), 'Imported original observation')
            if obs['id'] in observations:
                raise ValueError('Duplicate imported observation ID.')
            n._text(obs['id'], 'Imported observation ID', required=True)
            if obs['kind']!='GNSS_VECTOR' or obs['frame']!=batch['frame']:
                raise ValueError('Import snapshot must retain its GNSS vector and frame.')
            shadow = n.new_project()
            shadow['points'] = [dict(n.point(), id=obs[key]) for key in ('from_point','to_point')]
            shadow['sessions'] = [dict(n.session(), id=obs['session'])]
            shadow['observations'] = [obs]
            n.validate_project(shadow)
            values, cov, _ = to_ecef(row['native_values'], row['native_covariance'], batch['metadata'], batch['frame'])
            # Retain the stored numbers; tolerate libm rounding when opened on another OS.
            if (any(not math.isclose(a,c,rel_tol=1e-13,abs_tol=2e-8) for a,c in zip(obs['values'],values))
                    or any(not math.isclose(a,c,rel_tol=1e-12,abs_tol=1e-18) for a,c in zip(obs['covariance'],cov))):
                raise ValueError('Imported vector/covariance differs from its recorded conversion.')
            observations[obs['id']] = obs
    for obs in project['observations']:
        original = observations.get(obs['id'])
        if original and any(obs[key] != original[key] for key in LOCKED_FIELDS):
            raise ValueError('Imported measured vector, covariance, endpoints, session and ID are immutable. Correct a mapping/source in a separate or restored pre-import network, retaining this audit.')
