# SPDX-License-Identifier: GPL-3.0-only
"""Independent, full-covariance ETRS89 GNSS network model and SALSA verification.

No raw GNSS processing, geoid/antenna correction, robust deletion or variance
rescaling. All probabilities are conditional on the documented independent-block
Gaussian model. Coordinate constraints are distinct from measured baselines.
"""
from dataclasses import dataclass, asdict
from copy import deepcopy
import csv
import hashlib
import html
import json
import math
from pathlib import Path
import re
import subprocess

import numpy as np
from scipy.stats import chi2, norm

from . import network as n, baselines as b
from .coordinate_job import xyz, llh
from . import engine
from .version import VERSION


@dataclass
class QualityOptions:
    frame_epoch_reviewed: bool = False
    endpoints_reviewed: bool = False
    independence_reviewed: bool = False
    model_note: str = ''
    alpha: float = .05
    low_redundancy: float = .10
    horizontal_95_limit: float | None = None
    vertical_sigma_limit: float | None = None


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _validate_options(options):
    if not isinstance(options, QualityOptions) or not all(type(v) is bool and v for v in
            (options.frame_epoch_reviewed, options.endpoints_reviewed, options.independence_reviewed)):
        raise ValueError('Review frame/epoch, physical endpoints and the independent-block uncertainty model before calculating.')
    n._text(options.model_note, 'Network uncertainty / dependence review', required=True)
    for value, label, lower, upper in ((options.alpha,'Significance level',.001,.20),
                                      (options.low_redundancy,'Redundancy warning fraction',0.,1.)):
        n._number(value,label)
        if not lower <= value <= upper:
            raise ValueError(f'{label}: expected a value between {lower:g} and {upper:g}.')
    for value in (options.horizontal_95_limit, options.vertical_sigma_limit):
        if value is not None:
            n._number(value,'Optional uncertainty warning limit',positive=True)


class GridCoordinates:
    """Same-frame grid/ECEF conversion. No WGS84/ETRS89 datum transformation."""
    def __init__(self, crs):
        from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsProject
        self.forward = QgsCoordinateTransform(QgsCoordinateReferenceSystem(crs),QgsCoordinateReferenceSystem('EPSG:4937'),QgsProject.instance())
        self.backward = QgsCoordinateTransform(QgsCoordinateReferenceSystem('EPSG:4937'),QgsCoordinateReferenceSystem(crs),QgsProject.instance())

    def ecef(self, coordinates):
        from qgis.core import QgsPointXY
        east,north,height = coordinates
        p = self.forward.transform(QgsPointXY(east,north))
        return np.array(xyz(p.y(),p.x(),height,'EPSG:4937'))

    def grid(self, position):
        from qgis.core import QgsPointXY
        lat,lon,height = llh(position,'EPSG:4937')
        p = self.backward.transform(QgsPointXY(lon,lat))
        return [p.x(),p.y(),height]

    def prior_covariance(self, point):
        # Input sigmas belong to PROJECT GRID E/N/H, not geodetic ENU. A
        # same-frame Jacobian includes grid convergence and projection scale.
        values = np.array(point['coordinates'],dtype=float)
        columns = []
        for axis in range(3):
            offset = np.zeros(3); offset[axis] = 1.
            columns.append((self.ecef(values+offset)-self.ecef(values-offset))/2.)
        jacobian = np.column_stack(columns)
        result = jacobian @ np.diag(np.square(point['sigma'])) @ jacobian.T
        if not np.isfinite(result).all():
            raise ValueError('Non-finite coordinate uncertainty; review the input standard deviations.')
        np.linalg.cholesky(result)
        return result


def _covariance(values):
    return np.array(b.matrix(values),dtype=float)


def _components(vertices, observations):
    neighbours = {v:set() for v in vertices}
    for o in observations:
        neighbours[o['from_point']].add(o['to_point']); neighbours[o['to_point']].add(o['from_point'])
    result=[]; remaining=set(vertices)
    while remaining:
        todo=[min(remaining)]; group=set()
        while todo:
            name=todo.pop()
            if name in group: continue
            group.add(name); todo.extend(neighbours[name]-group)
        remaining-=group; result.append(sorted(group))
    return result


def _block_diagnostics(record, residual, covariance, residual_covariance):
    qv=(residual_covariance+residual_covariance.T)/2.
    tolerance=max(float(np.max(np.diag(covariance)))*1e-9,1e-24)
    eigenvalues,eigenvectors=np.linalg.eigh(qv)
    if eigenvalues.min() < -tolerance:
        raise ValueError('Unstable residual covariance; review network conditioning and uncertainty weights.')
    eigenvalues=np.maximum(eigenvalues,0.)
    qv=(eigenvectors*eigenvalues)@eigenvectors.T
    valid=eigenvalues>tolerance
    rank=int(valid.sum())
    stat=float(np.sum(np.square(eigenvectors[:,valid].T@residual)/eigenvalues[valid])) if rank else None
    lower=np.linalg.cholesky(covariance)
    whitened=np.linalg.solve(lower,np.linalg.solve(lower,qv).T).T
    redundancy_eigenvalues=np.clip(np.linalg.eigvalsh((whitened+whitened.T)/2),0.,1.)
    standardized=[float(value/math.sqrt(variance)) if variance>tolerance else None for value,variance in zip(residual,np.diag(qv))]
    result=dict(record, residual_xyz_m=residual.tolist(),residual_covariance_xyz=qv.tolist(),
        standardized_xyz=standardized,component_pvalues=[float(2*norm.sf(abs(z))) if z is not None else None for z in standardized],
        residual_test_rank=rank,block_statistic=stat,block_pvalue=float(chi2.sf(stat,rank)) if rank else None,
        redundancy_number=float(redundancy_eigenvalues.sum()),redundancy_fraction=float(redundancy_eigenvalues.mean()),
        minimum_direction_redundancy=float(redundancy_eigenvalues.min()),flags=[])
    return result


def analyse(project, options):
    """Pure snapshot analysis; never edit points, observations, sources or audits."""
    n.validate_project(project); _validate_options(options)
    if project['crs'] not in n.CRS or project['height_datum']!='ELLIP' or not project['epoch']:
        raise ValueError('This GNSS profile requires EUREF89/UTM, ellipsoidal point heights and a documented coordinate epoch. NN2000/NN54/local heights are not converted.')
    active=[o for o in project['observations'] if o['enabled'] and o['use']=='ADJUSTMENT']
    checks=[o for o in project['observations'] if o['enabled'] and o['use']=='CHECK']
    if not active:
        raise ValueError('No enabled adjustment observations. Held-out checks are never used to solve the network.')
    if any(o['kind']!='GNSS_VECTOR' for o in active):
        raise ValueError('This first network solver profile accepts GNSS vectors only. Unsupported active observations must be disabled with a reason or handled in a later mixed-observation profile.')
    if any(o['frame']!='EPSG:4936' for o in active):
        raise ValueError('EUREF89 point coordinates require documented ETRS89 ECEF vectors (EPSG:4936). No WGS84/ETRS89 datum transformation is inferred.')
    if len(project['points'])>50 or len(active)>200:
        raise ValueError('This first quality profile supports at most 50 points and 200 active GNSS vectors.')
    if any(value not in (None,0.) for o in active for value in (o['instrument_height'],o['target_height'])):
        raise ValueError('Nonzero instrument/target heights are not applied by this solver adapter. Supply already reduced point-to-point vectors and document the endpoint treatment.')
    points={p['id']:p for p in project['points']}
    vertices=sorted({o[key] for o in active for key in ('from_point','to_point')})
    groups=_components(vertices,active)
    for group in groups:
        if not any(points[k]['role'] in ('FIXED','CONSTRAINED') for k in group):
            raise ValueError('Unanchored network component: '+', '.join(group)+'. Add documented fixed or weighted control; approximate coordinates alone do not define the datum.')
    for name in vertices:
        if points[name]['role'] in ('FIXED','CONSTRAINED') and not points[name]['notes'].strip():
            raise ValueError(name+': document the control-coordinate source and reason for its fixed/weighted role.')
    pairs=set()
    imported={row['observation']['id']:batch for batch in project['imports'] for row in batch['records']}
    for o in active:
        pair=(o['session'],*sorted((o['from_point'],o['to_point'])))
        if pair in pairs:
            raise ValueError('Repeated endpoint pair in one acquisition session: '+o['id']+'. Correlated epochs are not independent repeats.')
        pairs.add(pair)
        if o['id'] in imported and imported[o['id']]['epoch']!=project['epoch']:
            raise ValueError(o['id']+': import coordinate epoch differs from the network epoch. Reconcile the reference information before adjustment.')
    grid=GridCoordinates(project['crs'])
    initial={name:grid.ecef(points[name]['coordinates']) for name in vertices if points[name]['coordinates'] is not None}
    while len(initial)<len(vertices):
        count=len(initial)
        for o in active:
            a,z=o['from_point'],o['to_point']; vector=np.array(o['values'])
            if a in initial and z not in initial: initial[z]=initial[a]+vector
            elif z in initial and a not in initial: initial[a]=initial[z]-vector
        if len(initial)==count:
            raise ValueError('Cannot derive approximate coordinates from the documented control network.')
    for position in initial.values(): b.ecef(position.tolist())
    adjustable=[name for name in vertices if points[name]['role']!='FIXED']
    if not adjustable:
        raise ValueError('No adjustable points in this profile. At least one unknown or weighted-control point is required.')
    indices={name:slice(3*i,3*i+3) for i,name in enumerate(adjustable)}
    columns=3*len(adjustable)
    blocks=[]; priors=[]
    for o in active:
        design=np.zeros((3,columns)); a,z=o['from_point'],o['to_point']
        if a in indices: design[:,indices[a]]=-np.eye(3)
        if z in indices: design[:,indices[z]]=np.eye(3)
        blocks.append(dict(id=o['id'],kind='GNSS_VECTOR',from_point=a,to_point=z,session=o['session'],
            observed=np.array(o['values']),covariance=_covariance(o['covariance']),design=design,
            rhs=np.array(o['values'])-(initial[z]-initial[a])))
    for name in adjustable:
        if points[name]['role']!='CONSTRAINED': continue
        design=np.zeros((3,columns)); design[:,indices[name]]=np.eye(3)
        covariance=grid.prior_covariance(points[name]); mean=grid.ecef(points[name]['coordinates'])
        blocks.append(dict(id='Coordinate prior: '+name,kind='COORDINATE_PRIOR',from_point=None,to_point=name,session=None,
            observed=mean,covariance=covariance,design=design,rhs=mean-initial[name]))
        priors.append(dict(point=name,mean_ecef=mean.tolist(),covariance_xyz=covariance.tolist()))
    white_design=[]; white_rhs=[]
    for block in blocks:
        lower=np.linalg.cholesky(block['covariance'])
        white_design.append(np.linalg.solve(lower,block['design']))
        white_rhs.append(np.linalg.solve(lower,block['rhs']))
    design=np.vstack(white_design); rhs=np.concatenate(white_rhs)
    if not np.isfinite(design).all() or not np.isfinite(rhs).all():
        raise ValueError('Non-finite weighted network; review the input magnitudes and uncertainty weights.')
    u,s,v=np.linalg.svd(design,full_matrices=False)
    rank=int(np.sum(s>s[0]*1e-11))
    if rank!=columns:
        raise ValueError(f'Rank-deficient or numerically unstable network: rank {rank} of {columns}. No uncertainty or quality approval can be reported.')
    condition=float(s[0]/s[-1])
    if condition>1e8:
        raise ValueError('Network weights/geometry are too ill-conditioned for reliable quality diagnostics.')
    increment=v.T@((u.T@rhs)/s)
    parameter_covariance=(v.T/np.square(s))@v
    solution={name:initial[name]+increment[indices[name]] if name in indices else initial[name] for name in vertices}
    for position in solution.values(): b.ecef(position.tolist())
    dof=len(rhs)-columns
    white_residual=rhs-design@increment
    statistic=float(white_residual@white_residual)
    global_test=dict(degrees_of_freedom=dof,statistic=statistic,apv=statistic/dof if dof else None,
        alpha=options.alpha,status='NOT_TESTABLE',lower=None,upper=None,p_two_sided=None)
    if dof:
        lower,upper=map(float,chi2.ppf([options.alpha/2,1-options.alpha/2],dof))
        global_test.update(lower=lower,upper=upper,
            p_two_sided=float(min(1.,2*min(chi2.cdf(statistic,dof),chi2.sf(statistic,dof)))),
            status='EXCESS_SCATTER' if statistic>upper else 'LOW_SCATTER' if statistic<lower else 'WITHIN_MODEL_INTERVAL')
    diagnostics=[]
    for block in blocks:
        residual=block['rhs']-block['design']@increment
        qv=block['covariance']-block['design']@parameter_covariance@block['design'].T
        record={k:block[k] for k in ('id','kind','from_point','to_point','session')}
        diagnostics.append(_block_diagnostics(record,residual,block['covariance'],qv))
    local_tests=sum(r['block_pvalue'] is not None for r in diagnostics)+sum(p is not None for r in diagnostics for p in r['component_pvalues'])
    for r in diagnostics:
        if r['residual_test_rank']==0: r['flags'].append('NO_REDUNDANCY')
        elif r['minimum_direction_redundancy']<options.low_redundancy: r['flags'].append('LOW_REDUNDANCY')
        if ((r['block_pvalue'] is not None and r['block_pvalue']*local_tests<options.alpha) or
                any(p is not None and p*local_tests<options.alpha for p in r['component_pvalues'])):
            r['flags'].append('RESIDUAL_REVIEW')
    check_results=[]
    for o in checks:
        result=dict(id=o['id'],status='INCONCLUSIVE',residual_xyz_m=None,pvalue=None,reason='')
        if o['kind']!='GNSS_VECTOR' or o['frame']!='EPSG:4936' or any(x not in (None,0.) for x in (o['instrument_height'],o['target_height'])):
            result['reason']='Held-out observation uses an unsupported type/frame/height reduction.'
            check_results.append(result); continue
        prediction=np.zeros(3); jacobian=np.zeros((3,columns)); reference_covariance=np.zeros((3,3)); missing=False
        for name,sign in ((o['from_point'],-1.),(o['to_point'],1.)):
            p=points[name]
            if name in solution:
                prediction+=sign*solution[name]
                if name in indices: jacobian[:,indices[name]]+=sign*np.eye(3)
            elif p['role'] in ('CHECK','FIXED') and p['coordinates'] is not None:
                prediction+=sign*grid.ecef(p['coordinates'])
                if p['role']=='CHECK':
                    if p['sigma'] is None: missing=True
                    else: reference_covariance+=grid.prior_covariance(p)
            else:
                missing=True; result['reason']='A check endpoint is not estimated or a documented independent reference.'
        residual=np.array(o['values'])-prediction
        if not result['reason']: result['residual_xyz_m']=residual.tolist()
        if missing:
            if not result['reason']: result['reason']='Independent check-coordinate uncertainty is undocumented; no zero uncertainty is inferred.'
        else:
            covariance=_covariance(o['covariance'])+jacobian@parameter_covariance@jacobian.T+reference_covariance
            score=float(residual@np.linalg.solve(covariance,residual))
            result.update(status='WITHIN_MODEL_TEST',pvalue=float(chi2.sf(score,3)),statistic=score,prediction_covariance_xyz=covariance.tolist(),reason='Independent held-out test; never used in adjustment.')
        check_results.append(result)
    count=sum(r['pvalue'] is not None for r in check_results)
    for r in check_results:
        if r['pvalue'] is not None and r['pvalue']*count<options.alpha: r['status']='CHECK_REVIEW'
    point_results=[]
    for p in project['points']:
        name=p['id']; result=dict(id=name,role=p['role'],position_ecef=None,position_grid=None,covariance_xyz=None,
            sigma_enu_m=None,horizontal_95_axes_m=None,vertical_95_halfwidth_m=None,flags=[])
        if name not in solution:
            result['flags']=['INDEPENDENT_REFERENCE_NOT_ADJUSTED' if p['role']=='CHECK' else 'UNOBSERVED_NOT_ADJUSTED']
        else:
            position=solution[name]
            covariance=parameter_covariance[indices[name],indices[name]] if name in indices else np.zeros((3,3))
            lat,lon,_=llh(position,'EPSG:4937'); rotation=np.array(b.enu_to_ecef(lat,lon)).T
            enu=rotation@covariance@rotation.T
            sigmas=np.sqrt(np.maximum(np.diag(enu),0.))
            axes=np.sqrt(np.maximum(np.linalg.eigvalsh(enu[:2,:2]),0.)*chi2.ppf(.95,2))[::-1]
            result.update(position_ecef=position.tolist(),position_grid=grid.grid(position),covariance_xyz=covariance.tolist(),
                sigma_enu_m=sigmas.tolist(),horizontal_95_axes_m=axes.tolist(),vertical_95_halfwidth_m=float(norm.ppf(.975)*sigmas[2]))
            if p['role']=='FIXED': result['flags'].append('FIXED_MODEL_ASSUMPTION')
            else:
                incident=[r for r in diagnostics if name in (r['from_point'],r['to_point'])]
                if any('NO_REDUNDANCY' in r['flags'] or 'LOW_REDUNDANCY' in r['flags'] for r in incident): result['flags'].append('WEAK_CONTROL')
                if options.horizontal_95_limit is not None and axes[0]>options.horizontal_95_limit: result['flags'].append('USER_HORIZONTAL_LIMIT')
                if options.vertical_sigma_limit is not None and sigmas[2]>options.vertical_sigma_limit: result['flags'].append('USER_VERTICAL_LIMIT')
        point_results.append(result)
    warnings=['Conditional independent-block Gaussian model: common-base/datum/antenna errors and cross-observation covariances are not modelled.',
        'Fixed coordinates are held exact by choice; zero model uncertainty does not verify the control point.',
        'No variance rescaling, automatic outlier removal or survey/delivery approval. Horizontal 95% ellipse and vertical 95% interval are separate marginal regions.']
    if global_test['status']=='NOT_TESTABLE': warnings.append('ZERO DEGREES OF FREEDOM: coordinates may be determined, but fit/variance tests cannot validate this network.')
    elif global_test['status']!='WITHIN_MODEL_INTERVAL': warnings.append('Global two-sided chi-square test: '+global_test['status']+'. Review errors, dependence and the stated uncertainty model.')
    if len(groups)>1: warnings.append('Multiple independently anchored components; their connection has not been observed.')
    if any(r['flags'] for r in diagnostics): warnings.append('Some observations have weak/no redundancy or significant residuals. A small residual does not prove correctness.')
    if any(r['status']!='WITHIN_MODEL_TEST' for r in check_results): warnings.append('Some held-out checks require review or have insufficient uncertainty information.')
    if condition>1e6: warnings.append('High condition number: results are sensitive to the chosen weights.')
    return dict(kind='gnss_network_quality',software_version=VERSION,synthetic=project['synthetic'],
        project_id=project['project_id'],project_sha256=_hash(n.dumps_project(project).encode('utf-8')),
        options=asdict(options),frame='EPSG:4936',point_crs=project['crs'],height='ELLIP',epoch=project['epoch'],
        global_test=global_test,network=dict(active_vectors=len(active),coordinate_priors=len(priors),
            components=groups,adjustable_points=len(adjustable),unknown_parameters=columns,rank=rank,condition_number=condition,
            scalar_observations=len(rhs),local_tests=local_tests),points=point_results,observations=diagnostics,checks=check_results,
        excluded=[dict(id=o['id'],reason='Disabled: '+o['notes']) for o in project['observations'] if not o['enabled']],
        warnings=warnings,positions_ecef={k:v.tolist() for k,v in solution.items()},initial_ecef={k:v.tolist() for k,v in initial.items()},
        parameter_order=adjustable,parameter_covariance_xyz=parameter_covariance.tolist(),coordinate_priors=priors,
        labels={name:f'P{i:04d}' for i,name in enumerate(vertices,1)})


def solver_input(project, analysis):
    """Generated labels only; user IDs/notes never become solver syntax."""
    labels=analysis['labels']; points={p['id']:p for p in project['points']}
    numeric=lambda values:' '.join(format(float(v),'.17g') for v in values)
    lines=['# Independently generated GNSS network; coordinate priors are not measured baselines.',
        '# SYNTHETIC TRAINING DATA' if project['synthetic'] else '# PRIVATE FIELD DATA - KEEP LOCAL',
        'TITLE Survey Adjustment GNSS network quality','DIM 3','PREC 0.000000001 M',
        'CONV 20 ITER 0.000000001 CONV',f"CONFIDENCE {analysis['options']['alpha']/2:.17g}",'OUT NOAPV']
    for name,label in labels.items():
        lines.append('POS '+label+' '+numeric(analysis['initial_ecef'][name])+' M '+('FIX' if points[name]['role']=='FIXED' else 'EST'))
    if analysis['coordinate_priors']:
        origin=analysis['coordinate_priors'][0]['mean_ecef']
        lines.append('# Numerical translation origin for independent coordinate constraints only.')
        lines.append('POS PRIOR_ORIGIN '+numeric(origin)+' M FIX')
        for index,prior in enumerate(analysis['coordinate_priors'],1):
            cov=b.packed(prior['covariance_xyz'])
            delta=[v-r for v,r in zip(prior['mean_ecef'],origin)]
            lines.append('DEL PRIOR_ORIGIN '+labels[prior['point']]+' '+numeric(delta+cov)+f' M tag=PRIOR_{index:04d}')
    active=[o for o in project['observations'] if o['enabled'] and o['use']=='ADJUSTMENT']
    for index,o in enumerate(active,1):
        lines.append('DEL '+labels[o['from_point']]+' '+labels[o['to_point']]+' '+numeric(o['values']+o['covariance'])+f' M tag=OBS_{index:04d}')
    return '\n'.join(lines)+'\n'


def prepare_network(folder, project, options, program):
    analysis=analyse(project,options)
    folder=Path(folder); folder.mkdir(parents=True,exist_ok=False)
    source=n.dumps_project(project).encode('utf-8'); data=solver_input(project,analysis).encode('ascii')
    (folder/'network.sanet.json').write_bytes(source)
    (folder/'input.dat').write_bytes(data)
    metadata=dict(kind='gnss_network_quality',project_sha256=_hash(source),input_dat_sha256=_hash(data),
        solver_sha256=engine.sha256(engine.solver_program(program)),options=asdict(options))
    (folder/'input_metadata.json').write_text(json.dumps(metadata,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    (folder/'preflight.json').write_text(json.dumps(analysis,indent=2,ensure_ascii=False,allow_nan=False)+'\n',encoding='utf-8')
    return analysis


def network_command(program):
    return [str(engine.solver_program(program)),'--input','input.dat','--out','solution.out','--bin','solution.bin',
            '--forcestable','--linprecP','9','--linprecM','9']


def inspect_network_result(folder, exit_code, program, write=True):
    folder=Path(folder); program=engine.solver_program(program)
    metadata=json.loads((folder/'input_metadata.json').read_text(encoding='utf-8'))
    if metadata.get('solver_sha256')!=engine.sha256(program):
        raise ValueError('The SALSA program changed since preparation; do not use the result.')
    source=(folder/'network.sanet.json').read_bytes(); data=(folder/'input.dat').read_bytes()
    if (metadata.get('kind')!='gnss_network_quality' or _hash(source)!=metadata.get('project_sha256') or
            _hash(data)!=metadata.get('input_dat_sha256')):
        raise ValueError('Network snapshot or SALSA input changed since preparation; do not use the result.')
    project=n.loads_project(source.decode('utf-8')); options=QualityOptions(**metadata['options'])
    analysis=analyse(project,options)
    if data!=solver_input(project,analysis).encode('ascii'):
        raise ValueError('SALSA input does not reproduce the recorded network model/options.')
    path=folder/'solution.out'
    if not path.is_file() or path.stat().st_size>50*1024*1024:
        raise ValueError('Missing or oversized SALSA result.')
    text=path.read_text(encoding='utf-8',errors='replace')
    if exit_code!=0 or not (folder/'solution.bin').is_file() or re.search(r'^.*\b(?:Error|Fatal|Exception)\b.*$',text,re.M|re.I):
        raise ValueError('SALSA network calculation failed. Review the local console/result files.')
    if 'Convergence reached' not in text or text.count('Final Adjusted Positions XYZ')!=1:
        raise ValueError('SALSA did not confirm a unique converged final solution.')
    found=re.findall(r'Degrees of freedom\s*=\s*(\d+);.*?APV\s*=\s*([\d.eE+\-]+)\s*\(Final\)',text)
    if len(found)!=1 or int(found[0][0])!=analysis['global_test']['degrees_of_freedom']:
        raise ValueError('SALSA degrees of freedom disagree with the independent network model.')
    apv=float(found[0][1]); expected_apv=analysis['global_test']['apv']
    if not math.isfinite(apv) or (expected_apv is not None and not math.isclose(apv,expected_apv,rel_tol=1e-5,abs_tol=1e-6)):
        raise ValueError('SALSA variance statistic disagrees with the independent network model.')
    section=text.split('Final Adjusted Positions XYZ')[1].split('Final Adjusted Positions LLH')[0]
    reverse={label:name for name,label in analysis['labels'].items()}; found_positions={}; maximum=0.
    report_points={p['id']:p for p in analysis['points']}
    for line in section.splitlines():
        fields=line.split()
        if not fields or fields[0] not in reverse: continue
        name=reverse[fields[0]]
        if name in found_positions: raise ValueError('Duplicate SALSA final position.')
        try:
            position=[float(v) for v in fields[1:4]]
            if len(position)!=3 or not all(math.isfinite(v) for v in position): raise ValueError()
            maximum=max(maximum,max(abs(a-c) for a,c in zip(position,analysis['positions_ecef'][name])))
            if report_points[name]['role']!='FIXED':
                covariance=[float(v) for v in fields[7:13]]
                expected=b.packed(report_points[name]['covariance_xyz'])
                if len(covariance)!=6 or any(not math.isfinite(a) or abs(a-c)>max(1e-12,.006*abs(c)) for a,c in zip(covariance,expected)):
                    raise ValueError()
            found_positions[name]=position
        except (ValueError,IndexError):
            raise ValueError('Invalid or inconsistent SALSA final coordinate covariance.') from None
    if set(found_positions)!=set(reverse.values()) or maximum>2e-6:
        raise ValueError('SALSA coordinates disagree with the independent full-covariance network solution.')
    version=re.search(r'SALSA Ver\.\s+(\S+)',text)
    analysis['engine_confirmation']=dict(engine='SALSA',version=version[1] if version else 'unknown',
        status='INDEPENDENT_MODEL_COMPARISON_PASSED',maximum_position_difference_m=maximum,
        covariance_comparison='SALSA text covariance is rounded to 3 significant digits; checked within 0.6% or 1e-12 m2.',
        solver_sha256=engine.sha256(program),input_sha256=_hash(data),source_sha256=_hash(source),output_sha256=_hash(path.read_bytes()))
    analysis['engine_warnings']=[line.strip() for line in text.splitlines() if re.match(r'^\s*Warning\b',line,re.I)]
    if write: write_results(folder,analysis)
    return analysis


def run_network(program, folder, project, options, timeout=60):
    """Synchronous test/CLI entry; the QGIS dialog uses nonblocking QProcess."""
    program=engine.solver_program(program); prepare_network(folder,project,options,program)
    result=subprocess.run(network_command(program),cwd=folder,env=engine.environment(program),capture_output=True,timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW if __import__('os').name=='nt' else 0)
    (Path(folder)/'console.txt').write_bytes(result.stdout+b'\n'+result.stderr)
    return inspect_network_result(folder,result.returncode,program)


def report_text(report):
    global_test=report['global_test']; network=report['network']
    lines=[f'Survey Adjustment {VERSION} - GNSS network quality',
        'SYNTHETIC TRAINING DATA' if report['synthetic'] else 'PRIVATE MEASUREMENTS - KEEP LOCAL',
        'Conditional statistical analysis, not survey/delivery approval.',
        f"{report['point_crs']}; ETRS89 ECEF vectors; ELLIP; coordinate epoch {report['epoch']}.",
        f"{network['active_vectors']} GNSS vectors; {network['coordinate_priors']} coordinate priors; {network['unknown_parameters']} unknown components.",
        f"Global two-sided chi-square: {global_test['status']}; dof={global_test['degrees_of_freedom']}; statistic={global_test['statistic']:.8g}; APV={global_test['apv']}.",
        f"Significance alpha={global_test['alpha']:g}; local block/component tests use Bonferroni over {network['local_tests']} tests.",
        'Covariance is a priori-model covariance, never rescaled by observed scatter. XYZ residual sign: observed minus calculated.',
        'Horizontal 95% semi-axes and vertical 95% intervals are separate marginal confidence regions.']
    confirmation=report.get('engine_confirmation')
    if confirmation: lines.append('SALSA '+confirmation['version']+': '+confirmation['status'])
    lines+=['','POINTS']
    for p in report['points']:
        lines.append(p['id']+' | '+p['role']+' | sigma ENU (m): '+str(p['sigma_enu_m'])+' | H95 semi-axes (m): '+str(p['horizontal_95_axes_m'])+' | '+', '.join(p['flags']))
    lines+=['','OBSERVATIONS / COORDINATE PRIORS']
    for r in report['observations']:
        lines.append(r['id']+' | '+r['kind']+' | residual XYZ (m): '+str(r['residual_xyz_m'])+
            ' | standardized: '+str(r['standardized_xyz'])+f" | redundancy fraction={r['redundancy_fraction']:.6g} | "+', '.join(r['flags']))
    lines+=['','HELD-OUT CHECKS (NEVER USED IN FIT)']
    for r in report['checks']: lines.append(r['id']+' | '+r['status']+' | '+r['reason']+' | residual XYZ (m): '+str(r['residual_xyz_m']))
    lines+=['','MODEL REVIEW',report['options']['model_note'],'','WARNINGS']+report['warnings']+report.get('engine_warnings',[])
    lines+=['','The software is provided AS IS, without warranty. Disclaimers and limitations of liability apply to the extent permitted by GPLv3 sections 15-17 and applicable law. See LICENSE.']
    return '\n'.join(lines)+'\n'


def write_results(folder, report):
    folder=Path(folder); text=report_text(report)
    (folder/'quality.json').write_text(json.dumps(report,indent=2,ensure_ascii=False,allow_nan=False)+'\n',encoding='utf-8')
    (folder/'quality.txt').write_text(text,encoding='utf-8')
    (folder/'quality.html').write_text('<!doctype html><html lang="en"><meta charset="utf-8"><title>GNSS network quality</title>'
        '<style>body{font:16px system-ui;max-width:1100px;margin:2em auto;padding:1em}pre{white-space:pre-wrap}</style><pre>'+html.escape(text)+'</pre></html>',encoding='utf-8')
    def label(value):
        value=str(value)
        return "'"+value if value.startswith(('=','+','-','@')) else value
    with (folder/'points.csv').open('w',encoding='utf-8-sig',newline='') as handle:
        writer=csv.writer(handle); writer.writerow(['ID','Role','Easting','Northing','Ellipsoidal height','Sigma geodetic E','Sigma geodetic N','Sigma U','Horizontal 95 major semi-axis','Horizontal 95 minor semi-axis','Vertical 95 halfwidth','CRS','Flags'])
        for p in report['points']:
            writer.writerow([label(p['id']),p['role'],*(p['position_grid'] or [None]*3),*(p['sigma_enu_m'] or [None]*3),*(p['horizontal_95_axes_m'] or [None]*2),p['vertical_95_halfwidth_m'],report['point_crs'],'; '.join(p['flags'])])
    with (folder/'residuals.csv').open('w',encoding='utf-8-sig',newline='') as handle:
        writer=csv.writer(handle); writer.writerow(['ID','Kind','dX m','dY m','dZ m','Standardized X','Standardized Y','Standardized Z','Redundancy fraction','Minimum direction redundancy','Block pvalue','Flags'])
        for r in report['observations']:
            writer.writerow([label(r['id']),r['kind'],*r['residual_xyz_m'],*r['standardized_xyz'],r['redundancy_fraction'],r['minimum_direction_redundancy'],r['block_pvalue'],'; '.join(r['flags'])])
    with (folder/'checks.csv').open('w',encoding='utf-8-sig',newline='') as handle:
        writer=csv.writer(handle); writer.writerow(['ID','Status','dX m','dY m','dZ m','Pvalue','Reason'])
        for r in report['checks']: writer.writerow([label(r['id']),r['status'],*(r['residual_xyz_m'] or [None]*3),r['pvalue'],r['reason']])
    hashes={p.name:engine.sha256(p) for p in folder.iterdir() if p.is_file() and p.name!='sha256.json'}
    (folder/'sha256.json').write_text(json.dumps(hashes,indent=2)+'\n',encoding='utf-8')


def synthetic_quality_project():
    """Constructed training network only; no field/customer observations."""
    project=n.new_project()
    project.update(project_id='00000000-0000-4000-8000-000000000050',name='SYNTHETIC GNSS network quality',
        crs='EPSG:25832',height_datum='ELLIP',epoch='2026-10-18',synthetic=True,
        notes='SYNTHETIC mathematically constructed training network. Independent Gaussian vector blocks with full covariance; no actual receiver/base data.')
    truth={'REF0':[291000.,6701000.,80.], 'REF1':[291120.,6701000.,79.98],
           'P01':[291030.,6701050.,82.], 'P02':[291090.,6701080.,82.5], 'CHECK1':[291070.,6701090.,81.25]}
    project['points']=[dict(n.point(),id='REF0',role='FIXED',coordinates=truth['REF0'],notes='SYNTHETIC exact anchor for this mathematical example only.'),
        dict(n.point(),id='REF1',role='CONSTRAINED',coordinates=[291120.006,6701000.009,79.992],sigma=[.02,.02,.03],notes='SYNTHETIC independent coordinate prior; diagonal covariance in project grid E/N/H.'),
        dict(n.point(),id='P01',coordinates=[291030.05,6701049.97,82.02],notes='SYNTHETIC approximate initial position; not a constraint.'),
        dict(n.point(),id='P02',notes='SYNTHETIC unknown; initial position derived from measured vectors.'),
        dict(n.point(),id='CHECK1',role='CHECK',coordinates=truth['CHECK1'],sigma=[.015,.02,.03],notes='SYNTHETIC independent reference kept outside the fit.')]
    project['sessions']=[dict(n.session(),id=key,start=time,instrument='SYNTHETIC mathematical source',corrections='No real receiver or correction service.',
        notes='SYNTHETIC independent errors by construction; session labels alone do not establish field independence.')
        for key,time in (('S01','2026-10-18T08:00:00Z'),('S02','2026-10-18T10:00:00Z'),('CHECK','2026-10-18T12:00:00Z'))]
    grid=GridCoordinates(project['crs']); positions={key:grid.ecef(value) for key,value in truth.items()}
    covariance=n.covariance_from_sigmas([.012,.015,.025],[.2,-.1,.15])
    rng=np.random.default_rng(503)
    pairs=[('REF0','P01','S01'),('REF1','P01','S01'),('P01','P02','S01'),('REF0','P02','S01'),
           ('REF1','P02','S01'),('REF0','REF1','S01'),('REF0','P01','S02'),('REF0','CHECK1','CHECK')]
    for index,(origin,target,session) in enumerate(pairs,1):
        noise=rng.multivariate_normal(np.zeros(3),_covariance(covariance))
        project['observations'].append(dict(n.observation(),id=f'V{index:02d}',kind='GNSS_VECTOR',session=session,
            from_point=origin,to_point=target,use='CHECK' if session=='CHECK' else 'ADJUSTMENT',
            values=(positions[target]-positions[origin]+noise).tolist(),covariance=list(covariance),frame='EPSG:4936',
            instrument_height=0.,target_height=0.,notes='SYNTHETIC independent processed point-to-point vector, not a receiver export.'))
    n.validate_project(project)
    return project
