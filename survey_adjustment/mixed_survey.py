# SPDX-License-Identifier: GPL-3.0-only
"""Explicit mixed-observation snapshots; independent adjustment and SALSA bridge.

Mathematical conventions follow SALSA's public measurement specification. No
manufacturer decoder, geoid, DOV, face reduction or atmospheric correction is
inferred. The original schema-2 GNSS network is embedded without alteration.
"""
from copy import deepcopy
from dataclasses import dataclass, asdict
import csv
import hashlib
import html
import io
import json
import math
from pathlib import Path
import re
import subprocess

import numpy as np
from scipy.stats import chi2, norm
from . import network as n, network_quality as q, baselines as b, engine
from .coordinate_job import llh
from .version import VERSION

FORMAT = 'survey-adjustment-mixed'
FIELDS = ('id','session','kind','from_point','to_point','backsight','value','unit','sigma',
          'instrument_height','target_height','backsight_height','height_basis','angle_reference',
          'use','enabled','notes')
KINDS = ('DISTANCE','HORIZONTAL_ANGLE','ZENITH_ANGLE','HEIGHT_DIFFERENCE')
ANGLES = ('HORIZONTAL_ANGLE','ZENITH_ANGLE')
RADIUS = 6378137.
SOA = math.pi / 648000.


@dataclass
class MixedOptions:
    profile: str = 'MIXED_3D'
    frame_reviewed: bool = False
    reductions_reviewed: bool = False
    independence_reviewed: bool = False
    model_note: str = ''
    alpha: float = .05


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_source(path):
    path = Path(path)
    data = path.read_bytes()
    if len(data) > 1000000:
        raise ValueError('Survey CSV exceeds the 1 MB limit.')
    text = data.decode('utf-8-sig')
    result = dict(name=path.name, sha256=digest(data), utf8=text,
                  bom=data.startswith(b'\xef\xbb\xbf'),synthetic=False)
    parse_source(result)
    return result


def source_bytes(source):
    n._keys(source, {'name','sha256','utf8','bom','synthetic'}, 'Survey source')
    n._text(source['name'],'Source basename',required=True)
    if any(c in source['name'] for c in '/\\:') or type(source['bom']) is not bool or type(source['synthetic']) is not bool:
        raise ValueError('Invalid source basename or encoding marker.')
    if not isinstance(source['utf8'],str) or len(source['utf8'].encode('utf-8')) > 1000000:
        raise ValueError('Invalid or oversized survey source.')
    data = (b'\xef\xbb\xbf' if source['bom'] else b'') + source['utf8'].encode('utf-8')
    if digest(data) != source['sha256']:
        raise ValueError('Survey source SHA256 mismatch. Original text must be preserved.')
    return data


def parse_source(source):
    source_bytes(source)
    text=source['utf8']
    if not text.strip(): raise ValueError('Survey source is empty.')
    delimiter=';' if text.splitlines()[0].count(';') > text.splitlines()[0].count(',') else ','
    reader=csv.DictReader(io.StringIO(text,newline=''),delimiter=delimiter)
    if reader.fieldnames != list(FIELDS):
        raise ValueError('Use the documented mixed-survey CSV template, including its exact column order. Unknown fields are not discarded.')
    result=[]; identifiers=set()
    for line,row in enumerate(reader,2):
        if None in row or any(v is None for v in row.values()):
            raise ValueError(f'CSV row {line}: incorrect number of fields.')
        o={k:v.strip() for k,v in row.items()}
        if not o['id'] or o['id'] in identifiers:
            raise ValueError(f'CSV row {line}: observation IDs must be non-empty and unique.')
        identifiers.add(o['id'])
        if o['kind'] not in KINDS or o['use'] not in n.USES or o['enabled'] not in ('true','false'):
            raise ValueError(f'CSV row {line}: unsupported kind, use or enabled flag.')
        def number(key, optional=False):
            value=o[key]
            if not value and optional: return None
            try:
                x=float(value.replace(',','.'))
                if not math.isfinite(x): raise ValueError()
                return x
            except ValueError:
                raise ValueError(f'CSV row {line}: {key} requires a finite number; no thousands separators.') from None
        for key in ('value','sigma'): o[key]=number(key)
        for key in ('instrument_height','target_height','backsight_height'):
            o[key]=number(key,True)
            if o[key] is not None and not 0<=o[key]<=20:
                raise ValueError(f'CSV row {line}: {key} must be 0-20 metres or blank.')
        if o['sigma']<=0: raise ValueError(f'CSV row {line}: sigma must be a positive assessed 1-sigma value.')
        if o['kind'] in ANGLES:
            if o['unit'] not in ('deg','gon') or o['angle_reference']!='ELLIPSOID_NORMAL':
                raise ValueError(f'CSV row {line}: angles require deg/gon and explicit ELLIPSOID_NORMAL reduction. Raw gravity-referenced angles are not silently corrected.')
            upper=360 if o['unit']=='deg' else 400
            if not 0<=o['value']<(upper if o['kind']=='HORIZONTAL_ANGLE' else upper/2+1e-12):
                raise ValueError(f'CSV row {line}: angle outside the declared range; face-II zenith angles must be reduced externally.')
        elif o['unit']!='m' or o['angle_reference']:
            raise ValueError(f'CSV row {line}: linear observations use m and no angle reference.')
        if o['kind']=='DISTANCE' and o['value']<=0:
            raise ValueError(f'CSV row {line}: a slope distance must be positive; grid/horizontal distances are not accepted.')
        if o['kind']=='HORIZONTAL_ANGLE':
            if not o['backsight'] or o['backsight_height'] is None or o['target_height'] is None:
                raise ValueError(f'CSV row {line}: horizontal angles require a backsight and explicit backsight/target heights.')
            if o['instrument_height'] is not None:
                raise ValueError(f'CSV row {line}: horizontal angles use backsight/target heights; instrument height is not applied by this angle model and must be blank.')
        else:
            if o['backsight'] or o['backsight_height'] is not None:
                raise ValueError(f'CSV row {line}: backsight applies only to horizontal angles.')
            if o['kind'] in ('DISTANCE','ZENITH_ANGLE') and (o['instrument_height'] is None or o['target_height'] is None):
                raise ValueError(f'CSV row {line}: instrument/target heights must be explicit, including 0 for reduced mark-to-mark values.')
        if o['kind']=='HEIGHT_DIFFERENCE':
            if o['height_basis'] not in n.HEIGHTS or o['instrument_height'] is not None or o['target_height'] is not None:
                raise ValueError(f'CSV row {line}: height differences require a height basis and blank instrument/target heights; use reduced point-to-point height differences.')
        elif o['height_basis']:
            raise ValueError(f'CSV row {line}: height_basis applies only to height differences.')
        if not o['notes']:
            raise ValueError(f'CSV row {line}: document reductions and uncertainty in notes (or the reason for disabling).')
        o.update(enabled=o['enabled']=='true',line=line)
        result.append(o)
    if not 1<=len(result)<=300:
        raise ValueError('Expected 1-300 survey observations.')
    return result


def snapshot(project, source, options):
    n.validate_project(project); parse_source(source)
    if source['synthetic']!=project['synthetic']:
        raise ValueError('Synthetic/real classification differs between the CSV and network. Do not mix training and field data.')
    if not isinstance(options,MixedOptions): raise ValueError('Expected mixed-survey options.')
    return dict(format=FORMAT,schema_version=1,network=deepcopy(project),source=deepcopy(source),options=asdict(options))


def dumps(job):
    n._keys(job,{'format','schema_version','network','source','options'},'Mixed project')
    if job['format']!=FORMAT or type(job['schema_version']) is not int or job['schema_version']!=1:
        raise ValueError('Unsupported mixed project format/version.')
    snapshot(job['network'],job['source'],MixedOptions(**job['options']))
    text=json.dumps(job,ensure_ascii=False,indent=2,allow_nan=False)+'\n'
    if len(text.encode('utf-8'))>n.MAX_BYTES: raise ValueError('Mixed project exceeds 20 MB.')
    return text


def loads(text):
    if len(text.encode('utf-8'))>n.MAX_BYTES: raise ValueError('Mixed project exceeds 20 MB.')
    job=json.loads(text,object_pairs_hook=n._pairs,parse_constant=lambda value:(_ for _ in ()).throw(ValueError('Non-finite JSON.')))
    dumps(job)
    return job


def _angular(o):
    return math.pi/(180 if o['unit']=='deg' else 200) if o['kind'] in ANGLES else 1.


def _geometry(position):
    # SALSA uses WGS84 ellipsoid geometry internally, including for ECEF inputs.
    latitude,longitude,height=llh(position,'EPSG:4979')
    rotation=np.array(b.enu_to_ecef(latitude,longitude)).T
    return math.radians(latitude),math.radians(longitude),height,rotation


def _prediction(o, positions, profile):
    a,z=positions[o['from_point']],positions[o['to_point']]
    if profile=='LEVEL_1D': return np.array([z[0]-a[0]])
    if o['kind']=='GNSS_VECTOR': return z-a
    delta=z-a; distance=float(np.linalg.norm(delta))
    if distance<.01: raise ValueError(o['id']+': endpoints are too close for a stable survey equation.')
    if o['kind']=='DISTANCE': return np.array([distance])
    if o['kind']=='HEIGHT_DIFFERENCE': return np.array([_geometry(z)[2]-_geometry(a)[2]])
    enu=_geometry(a)[3]@delta
    if np.linalg.norm(enu[:2])<.01: raise ValueError(o['id']+': near-vertical sight has no stable horizontal/zenith angle.')
    if o['kind']=='ZENITH_ANGLE': return np.array([math.atan2(np.linalg.norm(enu[:2]),enu[2])])
    back=_geometry(a)[3]@(positions[o['backsight']]-a)
    if np.linalg.norm(back[:2])<.01: raise ValueError(o['id']+': degenerate backsight.')
    return np.array([(math.atan2(enu[0],enu[1])-math.atan2(back[0],back[1]))%(2*math.pi)])


def _measured(o, positions, profile):
    if o['kind']=='GNSS_VECTOR': return np.array(o['values'])
    value=o['value']*_angular(o)
    if profile=='LEVEL_1D' or o['kind']=='HEIGHT_DIFFERENCE': return np.array([value])
    a,z=positions[o['from_point']],positions[o['to_point']]
    la,loa,ha,_=_geometry(a); lz,loz,hz,_=_geometry(z)
    if o['kind']=='DISTANCE':
        hi,ht=o['instrument_height'],o['target_height']
        reduced=(value*value-(hz+ht-ha-hi)**2)*(RADIUS+ha)*(RADIUS+hz)/((RADIUS+ha+hi)*(RADIUS+hz+ht))+(hz-ha)**2
        if reduced<=0: raise ValueError(o['id']+': slope distance is inconsistent with the stated instrument/target heights.')
        return np.array([math.sqrt(reduced)])
    if o['kind']=='ZENITH_ANGLE':
        distance=float(np.linalg.norm(z-a)); dh=hz-ha-distance*distance/(2*RADIUS)
        horizontal2=distance*distance-dh*dh
        if horizontal2<=0: raise ValueError(o['id']+': invalid zenith-height reduction geometry.')
        correction=(o['target_height']*math.cos(lz-la)*math.cos(loz-loa)-o['instrument_height'])/math.sqrt(horizontal2)
        return np.array([math.pi/2-math.atan(math.tan(math.pi/2-value)-correction)])
    back=positions[o['backsight']]; hb=_geometry(back)[2]; rot=_geometry(a)[3]
    t=rot@(z-a); f=rot@(back-a)
    az_t,az_f=math.atan2(t[0],t[1]),math.atan2(f[0],f[1])
    correction=SOA*.108*math.cos(la)**2*(math.sin(2*az_t)*(hz+o['target_height'])-math.sin(2*az_f)*(hb+o['backsight_height']))/1000.
    return np.array([value+correction])


def _difference(o, observed, calculated):
    value=observed-calculated
    if o['kind'] in ANGLES: value=(value+math.pi)%(2*math.pi)-math.pi
    return value


def _jacobian(o, positions, order, profile):
    dimension=1 if profile=='LEVEL_1D' else 3
    matrix=np.zeros((3 if o['kind']=='GNSS_VECTOR' else 1,dimension*len(order)))
    involved={o['from_point'],o['to_point'],o.get('backsight','')}
    for index,name in enumerate(order):
        if name not in involved: continue
        for axis in range(dimension):
            step=.02 if profile=='MIXED_3D' else .01
            plus={**positions,name:positions[name].copy()}; minus={**positions,name:positions[name].copy()}
            plus[name][axis]+=step; minus[name][axis]-=step
            matrix[:,dimension*index+axis]=_difference(o,_prediction(o,plus,profile),_prediction(o,minus,profile))/(2*step)
    return matrix


def _observations(job):
    project=job['network']; rows=parse_source(job['source']); options=MixedOptions(**job['options'])
    if options.profile not in ('MIXED_3D','LEVEL_1D') or not all(type(v) is bool and v for v in
            (options.frame_reviewed,options.reductions_reviewed,options.independence_reviewed)):
        raise ValueError('Select a profile and review frame/heights, reductions and independent uncertainty blocks.')
    n._text(options.model_note,'Survey model review',required=True)
    n._number(options.alpha,'Significance alpha')
    if not .001<=options.alpha<=.20: raise ValueError('Significance alpha must be 0.001-0.20.')
    if not project['crs'] or not project['epoch'] or not project['height_datum']:
        raise ValueError('Document project CRS, coordinate epoch and height datum.')
    if options.profile=='MIXED_3D' and project['height_datum']!='ELLIP':
        raise ValueError('Mixed 3D requires ELLIP heights. NN2000/NN54 levelling uses LEVEL_1D; no geoid conversion is inferred.')
    points={p['id']:p for p in project['points']}; sessions={s['id']:s for s in project['sessions']}
    if set(r['id'] for r in rows)&set(o['id'] for o in project['observations']):
        raise ValueError('Survey CSV IDs overlap existing network observation IDs.')
    existing=[]
    for original in project['observations']:
        if original['kind']!='GNSS_VECTOR' and original['enabled']:
            raise ValueError('Existing distance/height inventory records need explicit units and reductions: move them to the survey CSV and disable the inventory record with a reason. Nothing is silently omitted.')
        if original['kind']=='GNSS_VECTOR':
            if options.profile=='LEVEL_1D' and original['enabled']:
                raise ValueError('LEVEL_1D does not use GNSS vectors. Disable them with a reason or use a separate levelling network.')
            if original['frame']!='EPSG:4936' or any(x not in (None,0.) for x in (original['instrument_height'],original['target_height'])):
                if original['enabled']: raise ValueError('Mixed GNSS vectors require ETRS89 ECEF and already reduced physical endpoints.')
            existing.append(deepcopy(original))
    for o in rows:
        endpoints=[o['from_point'],o['to_point']]+([o['backsight']] if o['kind']=='HORIZONTAL_ANGLE' else [])
        if any(v not in points for v in endpoints) or len(set(endpoints))!=len(endpoints):
            raise ValueError(o['id']+': endpoints/backsight must be distinct, existing physical point IDs.')
        if o['session'] not in sessions or not sessions[o['session']]['start']:
            raise ValueError(o['id']+': select an existing acquisition session with documented UTC time.')
        if o['use']=='ADJUSTMENT' and o['enabled'] and any(points[v]['role']=='CHECK' for v in endpoints):
            raise ValueError(o['id']+': independent check points must stay outside the adjustment.')
        if o['kind']=='HEIGHT_DIFFERENCE' and o['height_basis']!=project['height_datum']:
            raise ValueError(o['id']+': height difference basis does not match the project height datum.')
        if options.profile=='LEVEL_1D' and o['enabled'] and o['kind']!='HEIGHT_DIFFERENCE':
            raise ValueError('LEVEL_1D accepts point-to-point height differences only; no active observations are dropped.')
    for batch in project['imports']:
        if batch['epoch']!=project['epoch']:
            raise ValueError('GNSS import epoch differs from the network epoch.')
    all_rows=existing+rows
    pairs=set()
    for o in existing:
        if not o['enabled'] or o['use']!='ADJUSTMENT': continue
        pair=(o['session'],*sorted((o['from_point'],o['to_point'])))
        if pair in pairs: raise ValueError('Repeated GNSS endpoint pair in one acquisition session; correlated epochs are not independent repeats.')
        pairs.add(pair)
    if len(points)>50 or len(all_rows)>300: raise ValueError('Mixed profile limit: 50 points and 300 observations.')
    return all_rows,options


def analyse(job):
    dumps(job)
    project=job['network']; all_rows,options=_observations(job); profile=options.profile
    active=[o for o in all_rows if o['enabled'] and o['use']=='ADJUSTMENT']
    checks=[o for o in all_rows if o['enabled'] and o['use']=='CHECK']
    if not active: raise ValueError('No active adjustment observations.')
    points={p['id']:p for p in project['points']}
    vertices=sorted({o[k] for o in active for k in ('from_point','to_point')}|{o['backsight'] for o in active if o.get('backsight')})
    for name in vertices:
        if points[name]['coordinates'] is None:
            raise ValueError(name+': enter documented approximate E/N/Z coordinates before a nonlinear mixed adjustment.')
        if points[name]['role'] in ('FIXED','CONSTRAINED') and not points[name]['notes'].strip():
            raise ValueError(name+': document the control source and fixed/weighted role.')
    graph=active+[dict(from_point=o['from_point'],to_point=o['backsight']) for o in active if o.get('backsight')]
    groups=q._components(vertices,graph)
    if any(not any(points[v]['role'] in ('FIXED','CONSTRAINED') for v in group) for group in groups):
        raise ValueError('An unanchored component has no documented fixed/weighted control.')
    order=[v for v in vertices if points[v]['role']!='FIXED']; dimension=1 if profile=='LEVEL_1D' else 3
    if not order: raise ValueError('At least one adjustable point is required.')
    grid=q.GridCoordinates(project['crs'])
    initial={v:np.array([points[v]['coordinates'][2]]) if dimension==1 else grid.ecef(points[v]['coordinates']) for v in vertices}
    positions={v:p.copy() for v,p in initial.items()}
    blocks=[]
    for o in active:
        covariance=np.array(b.matrix(o['covariance'])) if o['kind']=='GNSS_VECTOR' else np.array([[(o['sigma']*_angular(o))**2]])
        blocks.append((o,covariance))
    for name in order:
        if points[name]['role']=='CONSTRAINED':
            mean=initial[name]; covariance=np.array([[points[name]['sigma'][2]**2]]) if dimension==1 else grid.prior_covariance(points[name])
            blocks.append((dict(id='Prior: '+name,kind='COORDINATE_PRIOR',from_point='',to_point=name,mean=mean),covariance))
    columns=dimension*len(order)
    def linearize(current):
        designs=[]; residuals=[]; raw=[]
        for o,covariance in blocks:
            if o['kind']=='COORDINATE_PRIOR':
                matrix=np.zeros((dimension,columns)); idx=order.index(o['to_point']); matrix[:,dimension*idx:dimension*(idx+1)]=np.eye(dimension)
                residual=o['mean']-current[o['to_point']]
            else:
                matrix=_jacobian(o,current,order,profile)
                residual=_difference(o,_measured(o,current,profile),_prediction(o,current,profile))
            lower=np.linalg.cholesky(covariance)
            designs.append(np.linalg.solve(lower,matrix)); residuals.append(np.linalg.solve(lower,residual)); raw.append((o,covariance,matrix,residual))
        return np.vstack(designs),np.concatenate(residuals),raw
    converged=False
    for iteration in range(40):
        design,residual,raw=linearize(positions)
        u,s,vt=np.linalg.svd(design,full_matrices=False)
        rank=int(np.sum(s>s[0]*1e-10))
        if rank!=columns or s[0]/s[-1]>1e8:
            raise ValueError(f'Rank-deficient/ill-conditioned mixed network: {rank} of {columns} parameters. Add geometry/control, not invented observations.')
        step=vt.T@((u.T@residual)/s)
        if not np.isfinite(step).all() or np.max(abs(step))>1000:
            raise ValueError('Unstable mixed adjustment; review approximate coordinates and observation conventions.')
        for i,v in enumerate(order): positions[v]+=step[dimension*i:dimension*(i+1)]
        if max(abs(step))<2e-7:
            converged=True; break
    if not converged: raise ValueError('Independent mixed model did not converge within 40 iterations.')
    design,residual,raw=linearize(positions)
    _,s,vt=np.linalg.svd(design,full_matrices=False)
    covariance=(vt.T/np.square(s))@vt
    dof=len(residual)-columns; statistic=float(residual@residual)
    lower,upper=map(float,chi2.ppf([options.alpha/2,1-options.alpha/2],dof)) if dof else (None,None)
    global_test=dict(degrees_of_freedom=dof,statistic=statistic,apv=statistic/dof if dof else None,
        lower=lower,upper=upper,status='NOT_TESTABLE' if not dof else 'EXCESS_SCATTER' if statistic>upper else 'LOW_SCATTER' if statistic<lower else 'WITHIN_MODEL_INTERVAL')
    diagnostics=[]
    for o,cov,matrix,res in raw:
        qv=(cov-matrix@covariance@matrix.T); variances=np.maximum(np.diag(qv),0.)
        standardized=[float(v/math.sqrt(var)) if var>max(float(np.max(np.diag(cov)))*1e-8,1e-24) else None for v,var in zip(res,variances)]
        red=float(np.trace(np.linalg.solve(cov,qv))); fraction=max(0.,min(1.,red/len(res)))
        factor=1. if o['kind'] in ('GNSS_VECTOR','COORDINATE_PRIOR') else _angular(o)
        unit='m' if o['kind'] in ('GNSS_VECTOR','COORDINATE_PRIOR') else o['unit']
        flags=['NO_REDUNDANCY'] if fraction<1e-8 else ['LOW_REDUNDANCY'] if fraction<.1 else []
        if any(v is not None and 2*norm.sf(abs(v))*len(residual)<options.alpha for v in standardized): flags.append('RESIDUAL_REVIEW')
        diagnostics.append(dict(id=o['id'],kind=o['kind'],unit=unit,residual=(res/factor).tolist(),
            standardized=standardized,redundancy_fraction=fraction,flags=flags,
            corrected_observation=None if o['kind']=='COORDINATE_PRIOR' else (_measured(o,positions,profile)/factor).tolist()))
    check_results=[]
    for o in checks:
        endpoints=[o['from_point'],o['to_point']]+([o['backsight']] if o.get('backsight') else [])
        extra={v for v in endpoints if v not in positions}; local=dict(positions); missing=False
        for v in extra:
            p=points[v]
            if p['coordinates'] is None or p['role'] not in ('CHECK','FIXED'): missing=True; continue
            local[v]=np.array([p['coordinates'][2]]) if dimension==1 else grid.ecef(p['coordinates'])
            if p['role']=='CHECK' and p['sigma'] is None: missing=True
        r=dict(id=o['id'],status='INCONCLUSIVE',unit='m' if o['kind']=='GNSS_VECTOR' else o['unit'],residual=None,pvalue=None)
        if not missing:
            res=_difference(o,_measured(o,local,profile),_prediction(o,local,profile))
            jac=_jacobian(o,local,order,profile)
            cov=np.array(b.matrix(o['covariance'])) if o['kind']=='GNSS_VECTOR' else np.array([[(o['sigma']*_angular(o))**2]])
            cov=cov+jac@covariance@jac.T
            for v in extra:
                if points[v]['role']=='CHECK':
                    j=_jacobian(o,local,[v],profile)
                    prior=np.array([[points[v]['sigma'][2]**2]]) if dimension==1 else grid.prior_covariance(points[v])
                    cov+=j@prior@j.T
            score=float(res@np.linalg.solve(cov,res)); probability=float(chi2.sf(score,len(res)))
            r.update(residual=(res/(1 if o['kind']=='GNSS_VECTOR' else _angular(o))).tolist(),pvalue=probability,
                status='CHECK_REVIEW' if probability*len(checks)<options.alpha else 'WITHIN_MODEL_TEST')
        check_results.append(r)
    point_results=[]
    for p in project['points']:
        name=p['id']; row=dict(id=name,role=p['role'],position_grid=None,sigma=None,flags=[])
        if name in positions:
            block=covariance[dimension*order.index(name):dimension*(order.index(name)+1),dimension*order.index(name):dimension*(order.index(name)+1)] if name in order else np.zeros((dimension,dimension))
            if dimension==1:
                row.update(position_grid=p['coordinates'][:2]+[float(positions[name][0])],sigma=[None,None,float(np.sqrt(block[0,0]))])
            else:
                lat,lon,_=llh(positions[name],'EPSG:4937'); rot=np.array(b.enu_to_ecef(lat,lon)).T
                row.update(position_grid=grid.grid(positions[name]),sigma=np.sqrt(np.maximum(np.diag(rot@block@rot.T),0.)).tolist())
            if p['role']=='FIXED': row['flags'].append('FIXED_MODEL_ASSUMPTION')
        else: row['flags'].append('HELD_OUT_OR_UNOBSERVED')
        point_results.append(row)
    warnings=['Conditional independent-block uncertainty model; shared setup/base errors and cross-observation covariance are not modelled.',
        'No geoid/DOV, atmospheric, face, prism or datum/epoch correction is inferred. No variance rescaling or automatic outlier removal.',
        'Sigma must include assessed centring, instrument-height, target-height and prior reduction uncertainty; these are not guessed.',
        'Statistical diagnostics are conditional model tests, not survey/delivery approval.']
    if profile=='LEVEL_1D': warnings.append('HEIGHT ONLY: E/N are retained, not adjusted or quality-tested. SALSA uses a computational radial axis, not georeferenced ECEF coordinates; the original height datum is preserved.')
    else: warnings.append('Angles must already refer to the ellipsoid normal; raw gravity verticals require external documented reduction. Orthometric levelling cannot enter this ELLIP profile directly.')
    if not dof: warnings.append('ZERO DEGREES OF FREEDOM: fit test NOT_TESTABLE.')
    if global_test['status']!='WITHIN_MODEL_INTERVAL': warnings.append('Global model test: '+global_test['status']+'.')
    return dict(kind='mixed_survey',software_version=VERSION,profile=profile,synthetic=project['synthetic'],
        job_sha256=digest(dumps(job).encode('utf-8')),height=project['height_datum'],point_crs=project['crs'],epoch=project['epoch'],
        positions={v:x.tolist() for v,x in positions.items()},initial={v:x.tolist() for v,x in initial.items()},
        parameter_order=order,parameter_covariance=covariance.tolist(),points=point_results,
        observations=diagnostics,checks=check_results,excluded=[dict(id=o['id'],reason=o['notes']) for o in all_rows if not o['enabled']],
        global_test=global_test,iterations=iteration+1,warnings=warnings,labels={v:f'P{i:04d}' for i,v in enumerate(vertices,1)})


def solver_input(job, report):
    project=job['network']; options=MixedOptions(**job['options']); labels=report['labels']; profile=options.profile
    num=lambda values:' '.join(format(float(v),'.17g') for v in values)
    lines=['# SYNTHETIC TRAINING DATA' if project['synthetic'] else '# PRIVATE SURVEY DATA - KEEP LOCAL',
        'TITLE Survey Adjustment mixed survey','DIM 3','PREC 0.000000001 M',
        'CONV 40 ITER 0.000000001 CONV',f'CONFIDENCE {options.alpha/2:.17g}','OUT NOAPV']
    points={p['id']:p for p in project['points']}; level=profile=='LEVEL_1D'
    for name,label in labels.items():
        initial=report['initial'][name]
        coords=[RADIUS+initial[0],0.,0.] if level else initial
        suffix='FIX' if points[name]['role']=='FIXED' else 'EST'+(' CONS YZ' if level else '')
        lines.append('POS '+label+' '+num(coords)+' M '+suffix)
    priors=[v for v in labels if points[v]['role']=='CONSTRAINED']
    if priors:
        origin=[RADIUS,0.,0.] if level else report['initial'][priors[0]]
        lines.append('POS PRIOR_ORIGIN '+num(origin)+' M FIX')
        grid=q.GridCoordinates(project['crs'])
        for index,v in enumerate(priors):
            if level:
                lines.append(f'HGT PRIOR_ORIGIN {labels[v]} '+num([points[v]['coordinates'][2],points[v]['sigma'][2]])+f' M tag=PRIOR_{index:04d}')
            else:
                delta=np.array(report['initial'][v])-origin; cov=b.packed(grid.prior_covariance(points[v]))
                lines.append(f'DEL PRIOR_ORIGIN {labels[v]} '+num(list(delta)+cov)+f' M tag=PRIOR_{index:04d}')
    rows,_=_observations(job)
    for index,o in enumerate([o for o in rows if o['enabled'] and o['use']=='ADJUSTMENT']):
        a,z=labels[o['from_point']],labels[o['to_point']]; tag=f' tag=OBS_{index:04d}'
        if o['kind']=='GNSS_VECTOR': line=f'DEL {a} {z} '+num(o['values']+o['covariance'])+' M'
        elif o['kind']=='HEIGHT_DIFFERENCE': line=f'HGT {a} {z} '+num([o['value'],o['sigma']])+' M'
        elif o['kind']=='DISTANCE':
            line=f'DIS {a} {z} '+num([o['value'],o['sigma']])+' M CORR '+num([o['instrument_height'],o['target_height'],0.,0.])+' M'
        else:
            value=o['value']*_angular(o)*180/math.pi; sigma=o['sigma']*_angular(o)/SOA
            if o['kind']=='ZENITH_ANGLE':
                line=f'ZAN {a} {z} '+num([value])+' DEG '+num([sigma])+' SOA CORR '+num([o['instrument_height'],o['target_height'],0.,0.])+' M 0'
            else:
                line=f'HAN {labels[o["backsight"]]} {a} {z} '+num([value])+' DEG '+num([sigma])+' SOA CORR '+num([o['backsight_height'],o['target_height'],0.,0.])+' M'
        # SALSA treats everything following tag= as the label: tag MUST be last.
        lines.append(line+tag)
    return '\n'.join(lines)+'\n'


def prepare(folder, job, program):
    report=analyse(job); program=engine.solver_program(program)
    folder=Path(folder); folder.mkdir(parents=True,exist_ok=False)
    data=solver_input(job,report).encode('ascii'); source=dumps(job).encode('utf-8')
    (folder/'mixed.samix.json').write_bytes(source); (folder/'source.csv').write_bytes(source_bytes(job['source']))
    (folder/'input.dat').write_bytes(data)
    meta=dict(job_sha256=digest(source),input_sha256=digest(data),engine_sha256=engine.sha256(program))
    (folder/'input_metadata.json').write_text(json.dumps(meta,indent=2)+'\n',encoding='utf-8')
    return report


def inspect_result(folder, exit_code, program):
    folder=Path(folder); program=engine.solver_program(program)
    source=(folder/'mixed.samix.json').read_bytes(); data=(folder/'input.dat').read_bytes()
    metadata=json.loads((folder/'input_metadata.json').read_text(encoding='utf-8'))
    if metadata!=dict(job_sha256=digest(source),input_sha256=digest(data),engine_sha256=engine.sha256(program)):
        raise ValueError('Mixed snapshot, SALSA input or engine changed; result rejected.')
    job=loads(source.decode('utf-8')); report=analyse(job)
    if data!=solver_input(job,report).encode('ascii') or (folder/'source.csv').read_bytes()!=source_bytes(job['source']):
        raise ValueError('Survey source/input no longer reproduces the recorded model.')
    output=folder/'solution.out'
    if not output.is_file() or output.stat().st_size>50000000: raise ValueError('Missing or oversized SALSA output.')
    text=output.read_text(encoding='utf-8',errors='replace')
    if exit_code!=0 or not (folder/'solution.bin').is_file() or re.search(r'^.*\b(?:Error|Fatal|Exception)\b.*$',text,re.M|re.I):
        raise ValueError('SALSA mixed calculation failed; review the local output.')
    if 'Convergence reached' not in text or text.count('Final Adjusted Positions XYZ')!=1:
        raise ValueError('SALSA did not confirm a unique converged mixed solution.')
    stats=re.findall(r'Degrees of freedom\s*=\s*(\d+);.*?APV\s*=\s*([\d.eE+\-]+)\s*\(Final\)',text)
    dof=report['global_test']['degrees_of_freedom']; apv=report['global_test']['apv']
    if len(stats)!=1 or int(stats[0][0])!=dof or (apv is not None and not math.isclose(float(stats[0][1]),apv,rel_tol=2e-4,abs_tol=1e-5)):
        raise ValueError('SALSA degrees of freedom/variance statistic disagree with the independent mixed model.')
    section=text.split('Final Adjusted Positions XYZ')[1].split('Final Adjusted Positions LLH')[0]
    reverse={label:name for name,label in report['labels'].items()}; found={}; maximum=0.
    for line in section.splitlines():
        fields=line.split()
        if not fields or fields[0] not in reverse: continue
        name=reverse[fields[0]]
        if name in found: raise ValueError('Duplicate SALSA point.')
        values=np.array([float(v) for v in fields[1:4]])
        expected=report['positions'][name]
        if report['profile']=='LEVEL_1D': expected=[RADIUS+expected[0],0.,0.]
        if len(values)!=3 or not np.isfinite(values).all(): raise ValueError('Invalid SALSA coordinates.')
        maximum=max(maximum,float(np.max(abs(values-np.array(expected)))))
        if name in report['parameter_order']:
            index=report['parameter_order'].index(name); dimension=1 if report['profile']=='LEVEL_1D' else 3
            cov=np.array(report['parameter_covariance'])[dimension*index:dimension*(index+1),dimension*index:dimension*(index+1)]
            actual=[float(v) for v in fields[7:13]]
            expected_cov=[float(cov[0,0]),0.,0.,0.,0.,0.] if dimension==1 else b.packed(cov)
            if len(actual)!=6 or any(not math.isfinite(x) or abs(x-y)>max(1e-11,.01*abs(y)) for x,y in zip(actual,expected_cov)):
                raise ValueError('SALSA covariance disagrees with the independent mixed model.')
        found[name]=values
    if set(found)!=set(reverse.values()) or maximum>2e-5:
        raise ValueError('SALSA positions differ from the independent mixed solution by more than 0.02 mm.')
    version=re.search(r'SALSA Ver\.\s+(\S+)',text)
    report['engine_confirmation']=dict(status='INDEPENDENT_MIXED_MODEL_COMPARISON_PASSED',version=version[1] if version else 'unknown',
        maximum_position_difference_m=maximum,covariance_comparison='Rounded SALSA text checked within 1% or 1e-11 m2; no APV rescaling.',
        source_sha256=digest(source),input_sha256=digest(data),engine_sha256=engine.sha256(program),output_sha256=engine.sha256(output))
    report['engine_warnings']=[line.strip() for line in text.splitlines() if re.match(r'^\s*Warning\b',line,re.I)]
    write_results(folder,report)
    return report


def run(program, folder, job):
    prepare(folder,job,program)
    completed=subprocess.run(q.network_command(program),cwd=folder,env=engine.environment(program),capture_output=True,timeout=60)
    (Path(folder)/'console.txt').write_bytes(completed.stdout+b'\n'+completed.stderr)
    return inspect_result(folder,completed.returncode,program)


def report_text(report):
    lines=[f'Survey Adjustment {VERSION} - mixed survey', 'SYNTHETIC TRAINING DATA' if report['synthetic'] else 'PRIVATE SURVEY DATA - KEEP LOCAL',
        report['profile']+'; '+report['point_crs']+'; height '+report['height']+'; epoch '+report['epoch'],
        'SALSA VERIFIED' if report.get('engine_confirmation') else 'INDEPENDENT PREVIEW - SALSA HAS NOT RUN',
        'Observed minus calculated residuals in the explicitly stated units; uncertainty is not rescaled by APV.',
        'Global model test: '+json.dumps(report['global_test']), '', 'POINTS']
    for p in report['points']: lines.append(f"{p['id']} | {p['role']} | E/N/Z: {p['position_grid']} | sigma: {p['sigma']}")
    lines+=['','OBSERVATIONS / PRIORS']
    for o in report['observations']: lines.append(f"{o['id']} | {o['kind']} | {o['residual']} {o['unit']} | standardized {o['standardized']} | redundancy {o['redundancy_fraction']:.6g} | {o['flags']}")
    lines+=['','HELD-OUT CHECKS']+[json.dumps(r,ensure_ascii=False) for r in report['checks']]
    lines+=['','EXCLUDED']+[json.dumps(r,ensure_ascii=False) for r in report['excluded']]
    lines+=['','WARNINGS']+report['warnings']+report.get('engine_warnings',[])
    lines+=['','The software is provided AS IS, without warranty. Disclaimers and limitations of liability apply to the extent permitted by GPLv3 sections 15-17 and applicable law. See LICENSE.']
    return '\n'.join(lines)+'\n'


def write_results(folder, report):
    folder=Path(folder); text=report_text(report)
    (folder/'quality.json').write_text(json.dumps(report,indent=2,ensure_ascii=False,allow_nan=False)+'\n',encoding='utf-8')
    (folder/'quality.txt').write_text(text,encoding='utf-8')
    (folder/'quality.html').write_text('<!doctype html><html lang="en"><meta charset="utf-8"><title>Mixed survey</title><style>body{font:16px system-ui;max-width:1100px;margin:2em auto}pre{white-space:pre-wrap}</style><pre>'+html.escape(text)+'</pre></html>',encoding='utf-8')
    def safe(value):
        value=str(value); return "'"+value if value.startswith(('=','+','-','@')) else value
    with (folder/'points.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.writer(f); writer.writerow(['ID','Role','Easting','Northing','Point height','Sigma E','Sigma N','Sigma height','CRS','Height datum','Assessment'])
        for p in report['points']: writer.writerow([safe(p['id']),p['role'],*(p['position_grid'] or [None]*3),*(p['sigma'] or [None]*3),report['point_crs'],report['height'],'REVIEW REQUIRED'])
    with (folder/'residuals.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.writer(f); writer.writerow(['ID','Kind','Unit','Observed-minus-calculated','Standardized residuals','Redundancy fraction','Review'])
        for o in report['observations']: writer.writerow([safe(o['id']),o['kind'],o['unit'],json.dumps(o['residual']),json.dumps(o['standardized']),o['redundancy_fraction'],', '.join(o['flags'])])
    (folder/'sha256.json').write_text(json.dumps({p.name:engine.sha256(p) for p in folder.iterdir() if p.is_file() and p.name!='sha256.json'},indent=2)+'\n',encoding='utf-8')


def synthetic_job(profile='MIXED_3D'):
    """Constructed geometry and independent seeded noise; never receiver data."""
    project=n.new_project()
    project.update(project_id='00000000-0000-4000-8000-000000000070',name='SYNTHETIC mixed survey',
        crs='EPSG:25832',height_datum='NN2000' if profile=='LEVEL_1D' else 'ELLIP',epoch='2026-10-18',synthetic=True,
        notes='Constructed training geometry; no actual field data or correction service.')
    truth={'REF0':[291000.,6701000.,80.], 'REF1':[291120.,6701000.,81.],
           'P01':[291060.,6701060.,82.], 'P02':[291150.,6701100.,84.], 'CHECK1':[291100.,6701080.,83.]}
    for name,coords in truth.items():
        role='FIXED' if name=='REF0' else 'CONSTRAINED' if name=='REF1' else 'CHECK' if name=='CHECK1' else 'UNKNOWN'
        approximate=list(coords)
        if role=='UNKNOWN': approximate=[coords[0]+.08,coords[1]-.06,coords[2]+.10]
        project['points'].append(dict(n.point(),id=name,role=role,coordinates=approximate,
            sigma=[.01,.01,.004] if role in ('CONSTRAINED','CHECK') else None,
            notes='SYNTHETIC training control/approximation; fixed is a model choice only.'))
    for i,name in enumerate(('G01','T01','L01','CHECK')):
        project['sessions'].append(dict(n.session(),id=name,start=f'2026-10-18T{8+i:02}:00:00Z',end=f'2026-10-18T{8+i:02}:59:00Z',
            instrument='SYNTHETIC instrument',corrections='Synthetic independent Gaussian errors; no field correction service.'))
    rng=np.random.default_rng(705)
    grid=q.GridCoordinates(project['crs'])
    positions={v:np.array([xyz[2]]) if profile=='LEVEL_1D' else grid.ecef(xyz) for v,xyz in truth.items()}
    if profile=='MIXED_3D':
        cov=[.0001,.00001,0.,.000144,.000005,.000225]
        for i,(a,z) in enumerate((('REF0','P01'),('REF1','P02'))):
            values=positions[z]-positions[a]+rng.multivariate_normal(np.zeros(3),b.matrix(cov))
            project['observations'].append(dict(n.observation(),id=f'G{i+1}',kind='GNSS_VECTOR',session='G01',from_point=a,to_point=z,
                values=values.tolist(),covariance=cov.copy(),frame='EPSG:4936',instrument_height=0.,target_height=0.,
                notes='SYNTHETIC independent ECEF vector; point-to-point, full covariance known.'))
    rows=[]
    def add(kind,a,z,back='',unit='m',use='ADJUSTMENT',enabled=True):
        o={key:'' for key in FIELDS}
        sigma=.003 if unit=='m' else .0005 if unit=='gon' else .00045
        o.update(id=f'T{len(rows)+1:03d}',session='CHECK' if use=='CHECK' else 'L01' if kind=='HEIGHT_DIFFERENCE' else 'T01',
            kind=kind,from_point=a,to_point=z,backsight=back,unit=unit,sigma=sigma,use=use,enabled='true' if enabled else 'false',
            notes='SYNTHETIC independent training observation; no actual geoid/atmosphere; assessed sigma includes reductions.')
        if kind in ANGLES: o['angle_reference']='ELLIPSOID_NORMAL'
        if kind=='HEIGHT_DIFFERENCE': o['height_basis']=project['height_datum']
        elif kind=='HORIZONTAL_ANGLE': o.update(target_height=1.8,backsight_height=1.7)
        else: o.update(instrument_height=1.5,target_height=1.8)
        numeric=dict(o,value=0.,enabled=enabled,line=len(rows)+2)
        for key in ('instrument_height','target_height','backsight_height'):
            if numeric[key]=='': numeric[key]=None
        nominal=float(_prediction(numeric,positions,profile)[0]); value=nominal
        if kind=='DISTANCE':
            ha,hz=_geometry(positions[a])[2],_geometry(positions[z])[2]
            ratio=(RADIUS+ha)*(RADIUS+hz)/((RADIUS+ha+1.5)*(RADIUS+hz+1.8))
            value=math.sqrt((nominal**2-(hz-ha)**2)/ratio+(hz+1.8-ha-1.5)**2)
        elif kind=='ZENITH_ANGLE':
            la,loa,ha,_=_geometry(positions[a]); lz,loz,hz,_=_geometry(positions[z]); distance=np.linalg.norm(positions[z]-positions[a])
            horizontal=math.sqrt(distance**2-(hz-ha-distance**2/(2*RADIUS))**2)
            value=math.pi/2-math.atan(math.tan(math.pi/2-nominal)+(1.8*math.cos(lz-la)*math.cos(loz-loa)-1.5)/horizontal)
        elif kind=='HORIZONTAL_ANGLE':
            numeric['value']=nominal/_angular(numeric)
            correction=_measured(numeric,positions,profile)[0]-nominal
            value=(nominal-correction)%(2*math.pi)
        o['value']=value/_angular(numeric)+rng.normal(0.,sigma)
        rows.append(o)
    if profile=='MIXED_3D':
        for a,z in (('REF0','P01'),('REF1','P01'),('REF0','P02'),('REF1','P02'),('P01','P02')): add('DISTANCE',a,z)
        add('HORIZONTAL_ANGLE','REF0','P01','REF1','gon')
        add('HORIZONTAL_ANGLE','REF1','P02','REF0','deg')
        add('HORIZONTAL_ANGLE','P01','P02','REF0','gon')
        add('ZENITH_ANGLE','REF0','P01',unit='gon')
        add('ZENITH_ANGLE','REF1','P02',unit='deg')
    for a,z in (('REF0','P01'),('REF1','P01'),('P01','P02'),('REF1','P02'),('REF0','P02')): add('HEIGHT_DIFFERENCE',a,z)
    add('HEIGHT_DIFFERENCE' if profile=='LEVEL_1D' else 'DISTANCE','P01','CHECK1',use='CHECK')
    add('HEIGHT_DIFFERENCE' if profile=='LEVEL_1D' else 'DISTANCE','REF0','P02',enabled=False)
    rows[-1]['notes']='SYNTHETIC disabled training observation; retained to demonstrate exclusion audit.'
    output=io.StringIO(newline=''); writer=csv.DictWriter(output,fieldnames=FIELDS,lineterminator='\n'); writer.writeheader(); writer.writerows(rows)
    content=output.getvalue(); source=dict(name='synthetic_levelling.csv' if profile=='LEVEL_1D' else 'synthetic_mixed_survey.csv',
        sha256=digest(content.encode('utf-8')),utf8=content,bom=False,synthetic=True)
    return snapshot(project,source,MixedOptions(profile=profile))
