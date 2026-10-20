# SPDX-License-Identifier: GPL-3.0-only
"""Versioned network project storage. This module does not adjust observations."""
from copy import deepcopy
from datetime import date, datetime, timezone
import json
import math
import os
from pathlib import Path
from uuid import uuid4

FORMAT = 'survey-adjustment-network'
SCHEMA_VERSION = 2
MAX_BYTES = 20 * 1024 * 1024
ROLES = {'UNKNOWN': 'Unknown / estimate', 'CONSTRAINED': 'Known with uncertainty',
         'FIXED': 'Fixed / held exact', 'CHECK': 'Independent check only'}
KINDS = {'GNSS_VECTOR': 'GNSS vector (ECEF XYZ)', 'DISTANCE': 'Slope distance',
         'HEIGHT_DIFFERENCE': 'Height difference (to minus from)'}
USES = {'ADJUSTMENT': 'Adjustment', 'CHECK': 'Independent check only'}
CRS = {f'EPSG:258{z}': f'EUREF89 / UTM {z}N' for z in range(31, 37)}
HEIGHTS = ('NN2000', 'NN54', 'ELLIP', 'LOCAL')


def new_project():
    return dict(format=FORMAT, schema_version=SCHEMA_VERSION, project_id=str(uuid4()),
                name='Untitled network', crs='', height_datum='', epoch='', notes='', synthetic=False,
                points=[], sessions=[], observations=[], imports=[])


def upgrade_project(project):
    """Migrate schema 1 in memory only; never overwrite or discard old fields."""
    result = deepcopy(project)
    if isinstance(result, dict) and type(result.get('schema_version')) is int and result['schema_version']==1:
        _keys(result, set(new_project())-{'imports'}, 'Legacy schema-1 project')
        result.update(schema_version=SCHEMA_VERSION, imports=[])
    return result


def point():
    return dict(id='', role='UNKNOWN', coordinates=None, sigma=None, notes='')


def session():
    return dict(id='', start='', end='', instrument='', corrections='', notes='')


def observation():
    return dict(id='', session='', kind='DISTANCE', from_point='', to_point='', use='ADJUSTMENT',
                enabled=True, values=[0.0], covariance=[0.0004], frame='',
                instrument_height=None, target_height=None, notes='')


def _keys(value, expected, label):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise ValueError(f'{label}: unexpected or missing fields; use a supported project schema.')


def _text(value, label, required=False):
    if not isinstance(value, str) or len(value) > 10000 or '\x00' in value:
        raise ValueError(f'{label}: invalid text.')
    if required and (not value.strip() or value != value.strip()):
        raise ValueError(f'{label}: enter non-empty text without surrounding spaces.')


def _number(value, label, positive=False):
    if type(value) not in (int, float) or not math.isfinite(value) or (positive and value <= 0):
        raise ValueError(f'{label}: enter a finite {"positive " if positive else ""}number.')


def _numbers(value, count, label, positive=False):
    if not isinstance(value, list) or len(value) != count:
        raise ValueError(f'{label}: expected {count} numbers.')
    for v in value:
        _number(v, label, positive)


def _time(value, label):
    _text(value, label)
    if not value:
        return None
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.utcoffset() is None:
            raise ValueError()
        return result
    except ValueError:
        raise ValueError(f'{label}: use an ISO date and time with Z or a UTC offset.') from None


def covariance_from_sigmas(sigmas, correlations):
    _numbers(sigmas, 3, 'Vector standard deviations', positive=True)
    _numbers(correlations, 3, 'Vector correlations')
    a, b, d = sigmas
    ab, ad, bd = correlations
    result = [a*a, ab*a*b, ad*a*d, b*b, bd*b*d, d*d]
    validate_covariance(result)
    return result


def validate_covariance(value):
    _numbers(value, 6, 'Vector covariance (m²)')
    xx, xy, xz, yy, yz, zz = value
    if min(xx, yy, zz) <= 0:
        raise ValueError('Vector covariance must have positive variances.')
    a, b, d = math.sqrt(xx), math.sqrt(yy), math.sqrt(zz)
    ab, ad, bd = xy/a/b, xz/a/d, yz/b/d
    if max(abs(ab), abs(ad), abs(bd)) >= 1 or 1+2*ab*ad*bd-ab*ab-ad*ad-bd*bd <= 1e-12:
        raise ValueError('Vector covariance must be positive definite; check sigmas and correlations.')


def validate_project(project):
    """Reject damaged/ambiguous records; return draft/readiness notes, not approval."""
    expected=set(new_project())
    if isinstance(project,dict) and project.get('schema_version')==3: expected.add('raw_sources')
    _keys(project, expected, 'Project')
    if project['format'] != FORMAT or type(project['schema_version']) is not int or project['schema_version'] not in (SCHEMA_VERSION,3):
        raise ValueError('Unsupported network project format/schema version. The file was not changed.')
    for field in ('project_id', 'name'):
        _text(project[field], field, required=True)
    for field in ('notes', 'epoch'):
        _text(project[field], field)
    if project['epoch']:
        try:
            date.fromisoformat(project['epoch'])
        except ValueError:
            raise ValueError('Project epoch: use YYYY-MM-DD; no epoch transformation is applied.') from None
    if project['crs'] not in ('', *CRS) or project['height_datum'] not in ('', *HEIGHTS):
        raise ValueError('Unsupported point coordinate system or height datum.')
    if type(project['synthetic']) is not bool:
        raise ValueError('Synthetic example flag must be true or false.')
    for field in ('points', 'sessions', 'observations'):
        if not isinstance(project[field], list) or len(project[field]) > 10000:
            raise ValueError(f'{field}: expected a list of at most 10000 records.')
    records = {}
    for collection, template in (('points', point), ('sessions', session), ('observations', observation)):
        lookup = {}
        for record in project[collection]:
            _keys(record, template(), collection)
            _text(record['id'], f'{collection} ID', required=True)
            if len(record['id']) > 80 or any(ord(ch) < 32 for ch in record['id']):
                raise ValueError(f'{collection}: IDs must be at most 80 characters, without control characters.')
            if record['id'] in lookup:
                raise ValueError(f'{collection}: duplicate ID {record["id"]}.')
            _text(record['notes'], f'{record["id"]} notes')
            lookup[record['id']] = record
        records[collection] = lookup
    warnings = []
    if not project['crs'] or not project['height_datum']:
        warnings.append('Draft: select the point coordinate system and height datum before using coordinates.')
    for p in project['points']:
        if p['role'] not in ROLES:
            raise ValueError(f'{p["id"]}: unsupported point role.')
        if p['coordinates'] is not None:
            _numbers(p['coordinates'], 3, p['id']+' coordinates')
            if project['crs'] and not (-3000000 < p['coordinates'][0] < 3000000 and 0 < p['coordinates'][1] < 10000000):
                raise ValueError(f'{p["id"]}: expected UTM Easting, Northing and point height in metres.')
            if project['crs'] and not (100000 <= p['coordinates'][0] <= 900000):
                warnings.append(p['id']+': Easting is outside the nominal UTM zone range. Verify the documented zone; coordinates were preserved.')
        elif p['role'] != 'UNKNOWN':
            raise ValueError(f'{p["id"]}: known, fixed and independent check points require all three coordinates.')
        if p['sigma'] is not None:
            _numbers(p['sigma'], 3, p['id']+' coordinate standard deviations', positive=True)
        if p['role'] == 'CONSTRAINED' and p['sigma'] is None:
            raise ValueError(f'{p["id"]}: a known point with uncertainty requires positive σE, σN and σZ.')
        if p['role'] in ('UNKNOWN', 'FIXED') and p['sigma'] is not None:
            raise ValueError(f'{p["id"]}: coordinate sigmas are only stored for known-with-uncertainty or check points.')
        if p['role'] == 'FIXED':
            if not p['notes'].strip():
                raise ValueError(f'{p["id"]}: explain why this point is held exact in Notes.')
            warnings.append(f'{p["id"]}: fixed means held exact in a future model; it does not prove error-free field coordinates.')
    for s in project['sessions']:
        for field in ('instrument', 'corrections'):
            _text(s[field], s['id']+' '+field)
        start, end = _time(s['start'], s['id']+' start'), _time(s['end'], s['id']+' end')
        if end and (not start or end < start):
            raise ValueError(f'{s["id"]}: end requires a start and must not precede it.')
        if not start:
            warnings.append(s['id']+': session time is undocumented; independence cannot be inferred.')
    for o in project['observations']:
        label = o['id']
        if o['kind'] not in KINDS or o['use'] not in USES or type(o['enabled']) is not bool:
            raise ValueError(label+': unsupported observation type, use or enabled flag.')
        if o['session'] not in records['sessions']:
            raise ValueError(label+': select an existing session.')
        for end in ('from_point', 'to_point'):
            if o[end] not in records['points']:
                raise ValueError(label+': '+end+' must identify an existing point.')
            if o['enabled'] and o['use'] == 'ADJUSTMENT' and records['points'][o[end]]['role'] == 'CHECK':
                raise ValueError(label+': an independent check point must not enter adjustment; set this observation to Check only.')
        if o['from_point'] == o['to_point']:
            raise ValueError(label+': from and to must be different points.')
        for field in ('instrument_height', 'target_height'):
            if o[field] is not None:
                _number(o[field], label+' '+field)
                if o[field] < 0:
                    raise ValueError(label+': instrument/target heights must be non-negative or blank.')
        _text(o['frame'], label+' vector frame')
        if o['kind'] == 'GNSS_VECTOR':
            _numbers(o['values'], 3, label+' ECEF vector (m)')
            validate_covariance(o['covariance'])
            if o['frame'] not in ('EPSG:4936', 'EPSG:4978'):
                raise ValueError(label+': select the documented ECEF vector frame (EPSG:4936 or EPSG:4978).')
        else:
            _numbers(o['values'], 1, label+' value (m)', positive=o['kind']=='DISTANCE')
            _numbers(o['covariance'], 1, label+' variance (m²)', positive=True)
            if o['frame']:
                raise ValueError(label+': the ECEF vector frame only applies to GNSS vectors.')
        if not o['enabled'] and not o['notes'].strip():
            raise ValueError(label+': document the reason for disabling this observation in Notes.')
    from .baselines import validate_imports
    validate_imports(project)
    if project['schema_version']==3:
        from .rinex import validate_catalogue
        validate_catalogue(project)
        warnings.append('Raw RINEX catalogue only: original observations are external, hash-checked files; no new coordinate/baseline observation is created. Run raw preflight before using its timeline/map.')
    if project['imports']:
        warnings.append('Measured baseline import: formal precision is conditional on the processing model/base; shared errors and cross-observation covariance are not modelled. Import is not a network solution.')
    if not project['points'] or not project['observations']:
        warnings.append('Draft: add points, sessions and observations to describe a network.')
    warnings.append('Project structure only: no network adjustment, datum/height conversion, correction or quality approval has been performed.')
    return warnings


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON field: '+key)
        result[key] = value
    return result


def loads_project(text):
    if len(text.encode('utf-8')) > MAX_BYTES:
        raise ValueError('Network project exceeds the 20 MB draft limit.')
    try:
        project = upgrade_project(json.loads(text, object_pairs_hook=_pairs, parse_constant=lambda v: (_ for _ in ()).throw(ValueError('Non-finite JSON number: '+v))))
        validate_project(project)
        return project
    except (RecursionError, TypeError, OverflowError) as error:
        raise ValueError('Invalid network project structure.') from error


def dumps_project(project):
    validate_project(project)
    result = json.dumps(project, ensure_ascii=False, indent=2, allow_nan=False)+'\n'
    if len(result.encode('utf-8')) > MAX_BYTES:
        raise ValueError('Network project exceeds the 20 MB draft limit.')
    return result


def load_project(path):
    path = Path(path)
    if path.stat().st_size > MAX_BYTES:
        raise ValueError('Network project exceeds the 20 MB draft limit.')
    return loads_project(path.read_text(encoding='utf-8-sig'))


def save_project(path, project):
    """Validate first, then replace atomically without truncating the previous file."""
    payload = dumps_project(project)
    path = Path(path)
    temporary = path.parent / ('.'+path.name+'.'+uuid4().hex+'.tmp')
    created = False
    try:
        # Exclusive creation also fails promptly on a read-only Windows directory.
        with temporary.open('x', encoding='utf-8') as handle:
            created = True
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if created and temporary.exists():
            temporary.unlink()


def replace_record(project, collection, record, index=None):
    result = deepcopy(project)
    if collection=='observations' and index is not None:
        from .baselines import imported_snapshot
        if imported_snapshot(project, project[collection][index]['id']) and record['id']!=project[collection][index]['id']:
            raise ValueError('Imported measured observation IDs cannot be renamed.')
    if index is None:
        result[collection].append(deepcopy(record))
    else:
        result[collection][index] = deepcopy(record)
    validate_project(result)
    return result


def remove_record(project, collection, index):
    result = deepcopy(project)
    del result[collection][index]
    validate_project(result)
    return result


def synthetic_project():
    p = new_project()
    p.update(name='SYNTHETIC construction network inventory', crs='EPSG:25832', height_datum='ELLIP', epoch='2026-10-18', synthetic=True,
             notes='Constructed training inventory, not field data. Observations are stored only; no network has been adjusted.')
    p['points'] = [
        dict(point(), id='REF0', role='FIXED', coordinates=[291000.0, 6701000.0, 80.0], notes='SYNTHETIC anchor chosen exact only for this training inventory.'),
        dict(point(), id='REF1', role='CONSTRAINED', coordinates=[291100.0, 6701000.0, 80.0], sigma=[.015, .015, .025], notes='SYNTHETIC known coordinates with assessed uncertainty.'),
        dict(point(), id='P01', notes='SYNTHETIC unknown point; initial coordinates intentionally blank.'),
        dict(point(), id='CHECK1', role='CHECK', coordinates=[291050.0, 6701120.0, 81.23456789], sigma=[.02, .02, .03], notes='SYNTHETIC independent check; excluded from adjustment.')]
    p['sessions'] = [dict(session(), id='S01', start='2026-10-18T08:00:00Z', end='2026-10-18T09:00:00Z', instrument='SYNTHETIC instrument, no manufacturer', corrections='Synthetic data; no actual correction service.')]
    for oid, origin, target, kind, value, use in [
        ('O01','REF0','P01','DISTANCE',100.02,'ADJUSTMENT'),
        ('O02','REF1','P01','DISTANCE',100.02,'ADJUSTMENT'),
        ('O03','REF0','P01','HEIGHT_DIFFERENCE',2.0,'ADJUSTMENT'),
        ('C01','REF0','CHECK1','DISTANCE',130.005861,'CHECK')]:
        p['observations'].append(dict(observation(), id=oid, session='S01', from_point=origin, to_point=target,
            kind=kind, values=[value], use=use, instrument_height=0.0, target_height=0.0,
            notes='SYNTHETIC point-to-point inventory value; no field corrections applied.'))
    validate_project(p)
    return p
