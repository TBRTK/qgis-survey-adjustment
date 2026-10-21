# SPDX-License-Identifier: GPL-3.0-only
"""Experimental three-reference FLOAT observations in the existing GNSS flow.

Joint double-difference GLS with shared master/pivot noise. No integer fixing,
field accuracy or complete atmospheric model. Original reference files remain
separate. The rover is not read or fitted. Derived observations are dependent.
"""
from pathlib import Path
from datetime import datetime,timedelta,timezone
from copy import deepcopy
import csv,hashlib,json,math,subprocess
from decimal import Decimal
import re,tempfile
import numpy as np
from . import network as n,rinex as r,antenna,engine

KERNEL_SHA256='3364cdc0f76e674633f2f8193b2e065af03a73f0e3a8fd4ed67c59f541c50e69'
KERNEL_ID='SA_NETWORK_KERNEL_V1; RTKLIB EX 2.5.1; GPS 1C/2W; broadcast; receiver NOAZI; independent lambda search'
TAG='SURVEY_ADJUSTMENT_NETWORK_FLOAT_V1'
ANTENNA='SA_VRS_FLOAT'.ljust(16)+'NONE'
C=299792458.;F=np.array([1575.42e6,1227.60e6]);LAM=C/F;G=(F[0]/F[1])**2
LIMITS=[
    'EXPERIMENTAL FLOAT ONLY. Network ambiguities are not fixed; virtual phases contain non-integer biases. Downstream integer ambiguity resolution MUST remain OFF.',
    'Broadcast orbits and receiver NOAZI models only. Satellite patterns, precise products, tides and windup are not modeled by this initial kernel.',
    'Exactly three physical references and affine spatial transfer. Outside the triangle is extrapolation; curvature has zero internal spatial residual degrees of freedom.',
    'Per-block ambiguity GLS retains shared-master/pivot observation covariance. Conditional covariance assumes time-independent observation noise; temporal/multipath/base/frame/transfer-model uncertainties are excluded.',
    'Common receiver/satellite clock terms cancel algebraically in double differences. Original tags and observations remain unchanged. This initial profile only bounds small supplied/observed reference-clock offsets; no general clock correction or resampling.',
    'Conservative LLI/geometry-free screening is not exhaustive cycle-slip detection. Every block begins a new virtual ambiguity arc.',
    'Relative pivot gauge: master observation at the pivot is transferred geometrically, with no inferred absolute atmospheric correction. This is a relative DGNSS experiment, not a PPP reference.',
    'No rover observation or known control is used to fit the network. Generated observations share their parent references and are not independent survey repeats.',
    'A generated RINEX or successful static run does not establish accuracy. Retain all excluded blocks, FLOAT states and withheld control differences.'
]


def requires_float(source):
    return any(TAG in line for line in source['metadata']['header'])


def _inverse_spd(value):
    lower=np.linalg.cholesky(value)
    return np.linalg.solve(lower.T,np.linalg.solve(lower,np.eye(len(value))))


def design(k):
    """Y order: reference [master,aux1,aux2], [pivot,others], C1/C2/U1/U2."""
    if type(k) is not int or not 4<=k<=16:raise ValueError('Network block requires 4-16 common GPS satellites.')
    m=2*(k-1);difference=np.zeros((4*m,12*k))
    for auxiliary in range(2):
        for sat in range(1,k):
            index=auxiliary*(k-1)+sat-1
            for field in range(4):
                for receiver,satellite,sign in ((auxiliary+1,sat,1),(0,sat,-1),(auxiliary+1,0,-1),(0,0,1)):
                    difference[index*4+field,(receiver*k+satellite)*4+field]=sign
    combination=np.zeros((2*m,4*m))
    for index in range(m):
        combination[index*2,index*4:index*4+4]=[-(G+1)/(G-1)/LAM[0],2/(G-1)/LAM[0],1/LAM[0],0]
        combination[index*2+1,index*4:index*4+4]=[-2*G/(G-1)/LAM[1],(G+1)/(G-1)/LAM[1],0,1/LAM[1]]
    return difference,combination@difference


def fit_float_block(values,elevation,cancelled=lambda:False):
    values=np.asarray(values,dtype=float);elevation=np.asarray(elevation,dtype=float)
    if values.ndim!=4 or values.shape[1]!=3 or values.shape[3]!=4 or not 2<=len(values)<=120 or elevation.shape!=values.shape[:3] or not np.all(np.isfinite(values)) or not np.all(np.isfinite(elevation)):
        raise ValueError('Invalid bounded network block or nonfinite measurements.')
    if np.any(elevation<15) or np.any(elevation>90):raise ValueError('Network elevations must be 15-90 degrees.')
    samples,_,k,_=values.shape;difference,combination=design(k)
    normal=np.zeros((len(combination),len(combination)));rhs=np.zeros(len(combination));estimates=[];precisions=[]
    for index in range(samples):
        if cancelled():raise ValueError('Network estimation cancelled.')
        variance=((np.array([.35,.45,.004,.005])[None,None,:]/np.maximum(np.sin(np.radians(elevation[index])),.2)[:,:,None])**2).reshape(-1)
        covariance=(combination*variance)@combination.T;inverse=_inverse_spd(covariance)
        estimate=combination@values[index].reshape(-1);normal+=inverse;rhs+=inverse@estimate
        estimates.append(estimate);precisions.append(inverse)
    covariance=_inverse_spd(normal);ambiguities=covariance@rhs
    residual=sum(float((estimate-ambiguities)@precision@(estimate-ambiguities)) for estimate,precision in zip(estimates,precisions))
    dd=np.stack([(difference@row.reshape(-1)).reshape(2*(k-1),4) for row in values])
    phase=dd[:,:,2:]-ambiguities.reshape(1,-1,2)*LAM
    ionosphere=(phase[:,:,0]-phase[:,:,1])/(G-1)
    nondispersive=(G*phase[:,:,0]-phase[:,:,1])/(G-1)
    if not all(np.all(np.isfinite(value)) for value in (covariance,ambiguities,ionosphere,nondispersive)):
        raise ValueError('Nonfinite network estimate.')
    return dict(ambiguities=ambiguities,covariance=covariance,iono=ionosphere,nondispersive=nondispersive,
        nominal_reduced_residual=residual/(len(combination)*(samples-1)),sigma=np.sqrt(np.diag(covariance)))


def float_observations(reference,weights,cancelled=lambda:False):
    if len(reference)!=3 or len(weights)!=3 or not all(math.isfinite(v) for v in weights) or abs(sum(weights)-1)>1e-8 or sum(abs(v) for v in weights)>5:
        raise ValueError('Unsupported three-reference weights or excessive extrapolation gain.')
    times=sorted(set.intersection(*(set(row) for row in reference)))
    if len(times)>6000:raise ValueError('Too many network epochs for this initial bounded profile.')
    emitted=[];audit=[]
    for start in range(0,len(times),120):
        if cancelled():raise ValueError('Network generation cancelled.')
        ticks=times[start:start+120]
        if len(ticks)<30:
            audit.append(dict(start=ticks[0],state='EXCLUDED_SHORT_BLOCK',samples=len(ticks)));continue
        common=set.intersection(*(set(rows[tick]) for rows in reference for tick in ticks));accepted=[];excluded={}
        for sat in sorted(common):
            reason=None
            for rows in reference:
                flags=[int(rows[tick][sat]['lli1'])|int(rows[tick][sat]['lli2']) for tick in ticks]
                if any(flag&2 for flag in flags) or any(flag&1 for flag in flags[1:]):reason='LLI reset/half-cycle within block';break
                gf=np.array([LAM[0]*rows[tick][sat]['L1']-LAM[1]*rows[tick][sat]['L2'] for tick in ticks])
                if np.max(np.abs(np.diff(gf)))>.05:reason='geometry-free discontinuity/rapid-ionosphere screening';break
            if reason:excluded[sat]=reason
            else:accepted.append(sat)
        if not 4<=len(accepted)<=16:
            audit.append(dict(start=ticks[0],state='EXCLUDED_COMMON_ARCS',common=len(common),accepted=accepted,exclusions=excluded));continue
        pivot=max(accepted,key=lambda sat:min(rows[tick][sat]['elevation_deg'] for rows in reference for tick in ticks))
        satellites=[pivot]+[sat for sat in accepted if sat!=pivot];k=len(satellites)
        values=np.empty((len(ticks),3,k,4));elevation=np.empty((len(ticks),3,k))
        for time,tick in enumerate(ticks):
            for receiver,rows in enumerate(reference):
                for satellite,sat in enumerate(satellites):
                    row=rows[tick][sat]
                    values[time,receiver,satellite]=[row['P1']-row['model1'],row['P2']-row['model2'],LAM[0]*row['L1']-row['model1'],LAM[1]*row['L2']-row['model2']]
                    elevation[time,receiver,satellite]=row['elevation_deg']
        result=fit_float_block(values,elevation,cancelled)
        record=dict(start=ticks[0],end=ticks[-1],state='EXPERIMENTAL_FLOAT',pivot=pivot,satellites=satellites,exclusions=excluded,
            samples=len(ticks),integer_fix=False,nominal_reduced_residual=result['nominal_reduced_residual'],ambiguities=result['ambiguities'].tolist(),
            conditional_covariance_cycles2=result['covariance'].tolist(),conditional_sigma_min_cycles=float(min(result['sigma'])),conditional_sigma_max_cycles=float(max(result['sigma'])))
        if result['nominal_reduced_residual']>3 or np.max(np.abs(result['iono']))>50 or np.max(np.abs(result['nondispersive']))>50:
            record['state']='EXCLUDED_MODEL_REVIEW';audit.append(record);continue
        audit.append(record)
        for time,tick in enumerate(ticks):
            output=[]
            for satellite,sat in enumerate(satellites):
                nondispersive=ion=0.
                if satellite:
                    nondispersive=sum(weights[receiver]*result['nondispersive'][time,(receiver-1)*(k-1)+satellite-1] for receiver in (1,2))
                    ion=sum(weights[receiver]*result['iono'][time,(receiver-1)*(k-1)+satellite-1] for receiver in (1,2))
                row=reference[0][tick][sat];geometric=np.array([row['virtual_model']-row['model1'],row['virtual_model']-row['model2']])
                code=np.array([row['P1'],row['P2']])+geometric+nondispersive+np.array([1.,G])*ion
                phase=np.array([row['L1'],row['L2']])+(geometric+nondispersive-np.array([1.,G])*ion)/LAM
                output.append(dict(sat=sat,values=[float(code[0]),float(phase[0]),float(code[1]),float(phase[1])],lli=1 if time==0 else 0,
                    nondispersive_dd_m=float(nondispersive),iono_l1_dd_m=float(ion)))
            emitted.append(dict(tick=tick,observations=output))
    if not emitted:raise ValueError('No network block passed the limited FLOAT model/continuity screening; no VRS generated.')
    return emitted,audit


def rinex_bytes(epochs,target,synthetic):
    if not epochs:raise ValueError('No virtual observations.')
    line=lambda value,label:f'{value:<60}{label:<20}\n'
    date=lambda tick:datetime(1980,1,6)+timedelta(weeks=tick[0],microseconds=tick[1]/10)
    stamp=lambda value:''.join(f'{v:6d}' for v in (value.year,value.month,value.day,value.hour,value.minute))+f'{value.second+value.microsecond/1e6:13.7f}     GPS'
    text=line('     3.05           OBSERVATION DATA    G: GPS','RINEX VERSION / TYPE')
    # Fixed program header: regenerating the same audited inputs is byte-stable.
    text+=line('SurveyAdj NET FLOAT  Local research','PGM / RUN BY / DATE')+line('VRS_FLOAT','MARKER NAME')
    for value in (TAG,'SYNTHETIC constructed software experiment.' if synthetic else 'COMPUTED REAL-DERIVED; NO PHYSICAL RECEIVER',
        'FLOAT network; downstream integer fixing MUST remain OFF.','Affine network transfer; no accuracy or delivery approval.',
        'No rover observation/control used to fit reference model.','Relative pivot gauge; each block starts a new phase arc.'):
        text+=line(value,'COMMENT')
    text+=line(' '*20+'SA NETWORK FLOAT    EXPERIMENTAL','REC # / TYPE / VERS')+line(' '*20+ANTENNA,'ANT # / TYPE')
    text+=line(''.join(f'{value:14.4f}' for value in target),'APPROX POSITION XYZ')+line(''.join(f'{value:14.4f}' for value in (0,0,0)),'ANTENNA: DELTA H/E/N')
    text+=line('G    4 C1C L1C C2W L2W','SYS / # / OBS TYPES')+line(f'{30:10.3f}','INTERVAL')
    text+=line(stamp(date(epochs[0]['tick'])),'TIME OF FIRST OBS')+line(stamp(date(epochs[-1]['tick'])),'TIME OF LAST OBS')
    text+=line('     0','RCV CLOCK OFFS APPL')+line('G L1C  0.00000','SYS / PHASE SHIFT')+line('G L2W  0.00000','SYS / PHASE SHIFT')+line('','END OF HEADER')
    for entry in epochs:
        time=date(entry['tick']);observations=entry['observations']
        text+=f'> {time.year:4d} {time.month:2d} {time.day:2d} {time.hour:2d} {time.minute:2d} {time.second+time.microsecond/1e6:10.7f}  0{len(observations):3d}\n'
        for row in observations:
            if len(row['values'])!=4 or not all(math.isfinite(v) for v in row['values']):raise ValueError('Invalid virtual code/phase observation.')
            fields=[f'{value:14.3f}'+(str(row['lli']) if column in (1,3) and row['lli'] else ' ')+' ' for column,value in enumerate(row['values'])]
            if any(len(value)!=16 for value in fields):raise ValueError('Virtual RINEX numeric field exceeds its fixed width.')
            text+=row['sat']+''.join(fields)+'\n'
    return text.encode('ascii')


def _load_geometry(path,source):
    if path.stat().st_size>24*1024*1024:raise ValueError('Oversized network kernel output.')
    expected={'week','tow','sat','P1','L1','P2','L2','D1','D2','lli1','lli2','elevation_deg','model1','model2','virtual_model','spp_clock_s'}
    result={};original={sample[0] for sample in source['metadata']['samples']}
    with path.open(encoding='ascii',newline='') as handle:
        reader=csv.DictReader(handle)
        if set(reader.fieldnames or [])!=expected:raise ValueError('Network kernel output schema differs.')
        for count,row in enumerate(reader):
            if count>150000:raise ValueError('Too many network kernel records.')
            week=int(row['week']);tow=Decimal(row['tow']);tick=week*604800*r.TICK+int(tow*r.TICK)+r.epoch(['1980','1','6','0','0','0'])
            if tick not in original or tow*r.TICK!=int(tow*r.TICK) or not re.fullmatch(r'G(?:0[1-9]|[12][0-9]|3[0-2])',row['sat']):raise ValueError('Kernel epoch/satellite differs from original observations.')
            key=(week,int(tow*r.TICK));sat=row['sat'];values={name:(value if name=='sat' else float(value)) for name,value in row.items()}
            if not all(math.isfinite(value) for name,value in values.items() if name!='sat') or abs(values['spp_clock_s'])>1e-7:
                raise ValueError('Nonfinite kernel result or reference clock outside this initial 100 ns profile.')
            if sat in result.setdefault(key,{}):raise ValueError('Duplicate network kernel epoch/satellite.')
            result[key][sat]=values
    if not result:raise ValueError('No supported exact GPS 1C/2W observations from a reference.')
    return result


def _downstream_antex(catalogue):
    zero=dict(dazi=0.,frequencies={freq:dict(neu_mm=[0.,0.,0.],noazi_mm=[0.]*19) for freq in ('G01','G02')})
    selected=[(ANTENNA,zero)]
    for entry in catalogue['entries']:
        if entry['type']==ANTENNA:raise ValueError('Original ANTEX already contains a conflicting virtual endpoint.')
        if entry['dazi']==0 and all(code in entry['frequencies'] for code in ('G01','G02')):
            selected.append((entry['type'],dict(entry,frequencies={code:entry['frequencies'][code] for code in ('G01','G02')})))
    return antenna.subset(selected)


def generate(folder,project,references,target,kernel_path,antex_path,navigation_id,review_note,cancelled=lambda:False):
    """New local snapshot only. No project mutation, download, averaging or import."""
    from . import gnss_processing as processing
    folder=Path(folder);kernel=Path(kernel_path).resolve();n._text(review_note,'Network model/licence/uncertainty review',required=True)
    review=processing.reference_network_review(project,references,target,cancelled)
    if engine.sha256(kernel)!=KERNEL_SHA256:raise ValueError('Select the reviewed separately built network kernel; no binary fallback.')
    identity=subprocess.run([str(kernel),'identity'],capture_output=True,text=True,timeout=10)
    if identity.returncode or identity.stdout.strip()!=KERNEL_ID:raise ValueError('Unexpected network kernel identity.')
    source_by_id={row['id']:row for row in project.get('raw_sources',[])};nav=source_by_id.get(navigation_id)
    if not nav or nav['role']!='NAV':raise ValueError('Select an existing GPS navigation source from this catalogue.')
    check,unchanged=r.inspect_source(project,nav,cancelled)
    if not unchanged:raise ValueError('Navigation source changed or requires review.')
    processing._navigation_check(nav,('G',))
    ordered=sorted(review['reference_stations'],key=lambda row:row['chord_to_target_m'])
    weights=[row['affine_weight'] for row in ordered]
    if any(source_by_id[row['source_id']]['metadata']['interval_s']!=30 for row in ordered):raise ValueError('Initial network generation requires 30 s reference observations.')
    if (max(row['coordinate_epoch_decimal_year'] for row in ordered)-min(row['coordinate_epoch_decimal_year'] for row in ordered))*366*86400>60:
        raise ValueError('Reference coordinate epochs differ by more than 60 s; supply a reviewed common-epoch model first.')
    catalogue=antenna.read(antex_path)
    for row in ordered:
        source=source_by_id[row['source_id']];meta=source['metadata']
        if requires_float(source) or Path(source['path']).suffix.lower()=='.gz':raise ValueError('Use expanded physical reference RINEX, not a virtual product or gzip in this initial kernel profile.')
        if meta['version'] not in ('3.04','3.05') or any(meta['events'].get(flag,0) for flag in ('2','3','4')):raise ValueError('Use continuous reviewed RINEX 3.04/3.05 physical reference occupations.')
        antenna.select(catalogue,meta,2,('G',))
        if any(line[60:80].strip()=='SYS / PCVS APPLIED' and line[:60].strip() for line in meta['header']):
            raise ValueError('Reference PCV is already applied; do not remove the receiver model twice.')
        flags=[line[:60].strip() for line in meta['header'] if line[60:80].strip()=='RCV CLOCK OFFS APPL']
        if len(flags)>1 or (flags and flags[0]!='0'):raise ValueError('Initial DD clock profile requires uncorrected reference observables; no double clock correction.')
        with Path(source['path']).open(encoding='ascii') as handle:
            offsets=[abs(float(line[35:])) for line in handle if line.startswith('>') and line[35:].strip()]
        if offsets and (not all(math.isfinite(value) for value in offsets) or max(offsets)>1e-8):raise ValueError('Recorded reference clock offset exceeds 10 ns; a general correction/resampling model is not implemented.')
    if cancelled():raise ValueError('Network generation cancelled.')
    folder.mkdir(parents=True,exist_ok=False);n.save_project(folder/'network.sanet.json',project)
    paths=[];provenance=[]
    for index,row in enumerate(ordered):
        source=source_by_id[row['source_id']];payload=Path(source['path']).read_bytes()
        if hashlib.sha256(payload).hexdigest()!=source['source_sha256']:raise ValueError('Reference source changed during snapshot.')
        path=folder/('reference_'+str(index)+'.rnx');path.write_bytes(payload);paths.append(path)
        provenance.append(dict(source_id=source['id'],name=path.name,sha256=source['source_sha256'],fingerprint=source['fingerprint']))
    nav_path=folder/'navigation.rnx';nav_path.write_bytes(Path(nav['path']).read_bytes())
    if engine.sha256(nav_path)!=nav['source_sha256']:raise ValueError('Navigation changed during snapshot.')
    antex=folder/'reference_models.atx';antex.write_bytes(Path(antex_path).read_bytes())
    if engine.sha256(antex)!=catalogue['sha256']:raise ValueError('ANTEX changed during snapshot.')
    original=dict(format=TAG,version=1,state='PREPARING',kernel_sha256=KERNEL_SHA256,project_sha256=review['project_sha256'],
        references=provenance,navigation=dict(name=nav_path.name,sha256=nav['source_sha256']),antex_sha256=catalogue['sha256'],review_note=review_note)
    (folder/'vrs.json').write_text(json.dumps(original,indent=2)+'\n',encoding='utf-8')
    geometry=[]
    for index,row in enumerate(ordered):
        if cancelled():raise ValueError('Network generation cancelled.')
        output=folder/('reference_'+str(index)+'_model.csv')
        result=subprocess.run([str(kernel),'dump',str(paths[index]),str(nav_path),str(antex),*map(str,row['xyz']),*map(str,target),str(output)],capture_output=True,text=True,timeout=120)
        (folder/('reference_'+str(index)+'_kernel.log')).write_text(result.stdout+'\n'+result.stderr,encoding='utf-8')
        if result.returncode:raise ValueError('Network geometry kernel failed; retain the local partial snapshot/log.')
        geometry.append(_load_geometry(output,source_by_id[row['source_id']]))
    epochs,blocks=float_observations(geometry,weights,cancelled)
    virtual=folder/'virtual_float.rnx';virtual.write_bytes(rinex_bytes(epochs,target,project['synthetic']))
    source=r.read_source(virtual,cancelled)
    if source['metadata']['epochs']!=len(epochs):raise ValueError('Virtual RINEX readback epoch count differs.')
    # An ideal virtual endpoint is an explicit mathematical definition, not a
    # manufactured antenna calibration. Preserve other original receiver blocks.
    downstream=folder/'virtual_and_receivers.atx';downstream.write_bytes(_downstream_antex(catalogue))
    report=dict(format=TAG,version=1,status='EXPERIMENTAL_FLOAT_ONLY',epochs=len(epochs),blocks=blocks,
        reference_stations=ordered,weights=weights,target_xyz=list(target),geometry=review['geometry'],availability=review['availability'],
        input_review_issues=review['issues'],clock_model='DD common receiver clock cancellation; supplied offsets <=10 ns, observed SPP clocks <=100 ns; original tags/observations unchanged',
        coordinate_epoch_decimal_year=ordered[0]['coordinate_epoch_decimal_year'],reference_review=review_note,
        integer_fix=False,downstream_ambiguity_policy='off',synthetic=project['synthetic'],rover_observations_used=False,known_control_used_to_fit=False,
        limits=LIMITS,virtual_sha256=source['source_sha256'],virtual_fingerprint=source['fingerprint'])
    (folder/'network_model.json').write_text(json.dumps(report,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    (folder/'report.txt').write_text('EXPERIMENTAL THREE-REFERENCE FLOAT VRS\n'+json.dumps(report,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    original.update(state='COMPLETED',outputs={path.name:engine.sha256(path) for path in [virtual,downstream,folder/'network_model.json',folder/'report.txt',*[folder/('reference_'+str(i)+'_model.csv') for i in range(3)]]})
    (folder/'vrs.json').write_text(json.dumps(original,indent=2)+'\n',encoding='utf-8')
    return dict(folder=str(folder),virtual_path=str(virtual),antex_path=str(downstream),report=report,manifest=original)


def product_files(manifest):
    outputs={'virtual_float.rnx','virtual_and_receivers.atx','network_model.json','report.txt',*[f'reference_{i}_model.csv' for i in range(3)]}
    if set(manifest.get('outputs',{}))!=outputs:raise ValueError('Incomplete or unexpected VRS product outputs.')
    if len(manifest.get('references',[]))!=3 or [row['name'] for row in manifest['references']]!=[f'reference_{i}.rnx' for i in range(3)]:raise ValueError('Unexpected VRS reference source names.')
    return sorted(outputs|{'vrs.json','network.sanet.json','reference_models.atx','navigation.rnx',*[f'reference_{i}.rnx' for i in range(3)]})


def _equivalent(a,b):
    if isinstance(a,dict):return isinstance(b,dict) and a.keys()==b.keys() and all(_equivalent(a[key],b[key]) for key in a)
    if isinstance(a,(list,tuple)):return isinstance(b,(list,tuple)) and len(a)==len(b) and all(_equivalent(x,y) for x,y in zip(a,b))
    if type(a) in (float,int) and type(b) in (float,int):return math.isclose(a,b,rel_tol=1e-12,abs_tol=1e-8)
    return type(a)==type(b) and a==b


def load_product(folder,kernel_path,cancelled=lambda:False):
    folder=Path(folder);path=folder/'vrs.json'
    if path.stat().st_size>100000:raise ValueError('Oversized VRS manifest.')
    manifest=json.loads(path.read_text(encoding='utf-8'))
    n._keys(manifest,{'format','version','state','kernel_sha256','project_sha256','references','navigation','antex_sha256','review_note','outputs'},'VRS manifest')
    if manifest['format']!=TAG or manifest['version']!=1 or manifest['state']!='COMPLETED' or manifest['kernel_sha256']!=KERNEL_SHA256:raise ValueError('Incomplete or unsupported VRS product.')
    names=product_files(manifest)
    for name in names:
        selected=folder/name
        if selected.is_symlink() or not selected.is_file():raise ValueError('Missing/symlink VRS product member.')
    for name,digest in manifest['outputs'].items():
        if engine.sha256(folder/name)!=digest:raise ValueError('VRS output changed.')
    project=n.load_project(folder/'network.sanet.json')
    if r.content_fingerprint(project)!=manifest['project_sha256']:raise ValueError('VRS source-network snapshot changed.')
    sources={row['id']:row for row in project.get('raw_sources',[])}
    for row in manifest['references']:
        n._keys(row,{'source_id','name','sha256','fingerprint'},'VRS reference provenance')
        if engine.sha256(folder/row['name'])!=row['sha256'] or row['sha256']!=sources[row['source_id']]['source_sha256'] or row['fingerprint']!=sources[row['source_id']]['fingerprint']:raise ValueError('VRS parent reference changed.')
        check,unchanged=r.inspect_source(project,sources[row['source_id']],cancelled,path=folder/row['name'])
        if not unchanged:raise ValueError('VRS parent metadata/source differs from original catalogue.')
    if manifest['navigation']['name']!='navigation.rnx' or engine.sha256(folder/'navigation.rnx')!=manifest['navigation']['sha256'] or engine.sha256(folder/'reference_models.atx')!=manifest['antex_sha256']:
        raise ValueError('VRS navigation/calibration changed.')
    model_path=folder/'network_model.json'
    if model_path.stat().st_size>8*1024*1024:raise ValueError('Oversized VRS network model.')
    model=json.loads(model_path.read_text(encoding='utf-8'))
    if model['format']!=TAG or model['integer_fix'] is not False or model['downstream_ambiguity_policy']!='off' or model['rover_observations_used'] is not False or model['known_control_used_to_fit'] is not False:
        raise ValueError('VRS model lost its FLOAT-only/held-out contract.')
    # Reuse the original catalogue audit with relocated parent copies only.
    # The rover may be offline and is never inspected for this reference fit.
    from . import gnss_processing as processing
    relocated=deepcopy(project)
    for item in relocated['raw_sources']:
        parent=next((row for row in manifest['references'] if row['source_id']==item['id']),None)
        if parent:item['path']=str((folder/parent['name']).resolve())
    declarations=[{key:row[key] for key in ('source_id','xyz','frame','coordinate_epoch_decimal_year','evidence')} for row in model['reference_stations']]
    review=processing.reference_network_review(relocated,declarations,model['target_xyz'],cancelled)
    expected=sorted(review['reference_stations'],key=lambda row:row['chord_to_target_m'])
    geometry=deepcopy(review['geometry']);recorded_geometry=deepcopy(model['geometry'])
    # Geometry-review input order may differ from distance-ordered model rows.
    geometry['affine_weights']=sorted(geometry['affine_weights']);recorded_geometry['affine_weights']=sorted(recorded_geometry['affine_weights'])
    if not all(_equivalent(a,b) for a,b in ((expected,model['reference_stations']),(geometry,recorded_geometry),(review['availability'],model['availability']),([row['affine_weight'] for row in expected],model['weights']))):
        raise ValueError('VRS reference geometry/weights/coverage differs from the original catalogue.')
    if project['synthetic']!=model['synthetic'] or model['limits']!=LIMITS or model['coordinate_epoch_decimal_year']!=expected[0]['coordinate_epoch_decimal_year'] or model['epochs']!=r.read_source(folder/'virtual_float.rnx')['metadata']['epochs']:
        raise ValueError('VRS source/model identity or limitations changed.')
    if _downstream_antex(antenna.read(folder/'reference_models.atx'))!=(folder/'virtual_and_receivers.atx').read_bytes():raise ValueError('VRS ideal endpoint/receiver calibration differs from the original models.')
    kernel=Path(kernel_path)
    if engine.sha256(kernel)!=KERNEL_SHA256:raise ValueError('Reviewed network kernel required for VRS source/model readback.')
    with tempfile.TemporaryDirectory(prefix='network_readback_',dir=folder) as temporary:
        temporary=Path(temporary)
        for index,row in enumerate(model['reference_stations']):
            if cancelled():raise ValueError('VRS readback cancelled.')
            output=temporary/(str(index)+'.csv')
            result=subprocess.run([str(kernel),'dump',str(folder/f'reference_{index}.rnx'),str(folder/'navigation.rnx'),str(folder/'reference_models.atx'),*map(str,row['xyz']),*map(str,model['target_xyz']),str(output)],capture_output=True,text=True,timeout=120)
            if result.returncode or engine.sha256(output)!=manifest['outputs'][f'reference_{index}_model.csv']:raise ValueError('VRS geometry does not reproduce original reference observations/calibration/navigation.')
    geometry=[_load_geometry(folder/f'reference_{i}_model.csv',sources[row['source_id']]) for i,row in enumerate(manifest['references'])]
    epochs,blocks=float_observations(geometry,model['weights'],cancelled)
    if rinex_bytes(epochs,model['target_xyz'],model['synthetic'])!=(folder/'virtual_float.rnx').read_bytes():raise ValueError('VRS observations differ from recalculated joint network model.')
    if len(blocks)!=len(model['blocks']):raise ValueError('VRS network block count differs.')
    for actual,recorded in zip(blocks,model['blocks']):
        if actual.keys()!=recorded.keys():raise ValueError('VRS model block fields differ.')
        for key in actual:
            if key in ('ambiguities','conditional_covariance_cycles2'):
                if not np.allclose(actual[key],recorded[key],rtol=1e-7,atol=1e-5):raise ValueError('VRS joint covariance/ambiguity model differs.')
            elif json.dumps(actual[key],sort_keys=True)!=json.dumps(recorded[key],sort_keys=True):raise ValueError('VRS network model differs from recalculation.')
    if r.read_source(folder/'virtual_float.rnx')['fingerprint']!=model['virtual_fingerprint']:raise ValueError('VRS fingerprint differs.')
    return dict(folder=str(folder),virtual_path=str(folder/'virtual_float.rnx'),antex_path=str(folder/'virtual_and_receivers.atx'),report=model,manifest=manifest)


def snapshot_product(source,target,kernel_path,cancelled=lambda:False):
    product=load_product(source,kernel_path,cancelled=cancelled);target=Path(target);target.mkdir(exist_ok=False)
    for name in product_files(product['manifest']):
        if cancelled():raise ValueError('VRS snapshot cancelled.')
        payload=(Path(source)/name).read_bytes()
        if len(payload)>32*1024*1024:raise ValueError('Oversized VRS snapshot member.')
        (target/name).write_bytes(payload)
    return load_product(target,kernel_path,cancelled=cancelled)


def attach_product(project,product,kernel_path,point_id):
    """Use the existing typed raw import, once, with no baseline/coordinate fit."""
    checked=load_product(product['folder'],kernel_path)
    if r.content_fingerprint(project)!=checked['manifest']['project_sha256']:raise ValueError('Source network changed; do not attach a VRS to another occupation/project snapshot.')
    n._text(point_id,'Virtual reference point ID',required=True)
    if point_id in {point['id'] for point in project['points']}:raise ValueError('Virtual point ID already exists; do not overwrite another point.')
    result=deepcopy(project)
    result['points'].append(dict(n.point(),id=point_id,notes='COMPUTED FLOAT virtual reference; original ITRF2020 network product retained separately. No physical control, independent repeat or accepted EUREF89 point.'))
    master=next(source for source in project['raw_sources'] if source['id']==checked['report']['reference_stations'][0]['source_id'])
    return r.plan_import(result,r.read_source(checked['virtual_path']),r.ImportReview(role='BASE',point=point_id,session=master['session'],
        utc_offset_seconds=master['utc_offset_seconds'],time_evidence='GPST tags inherited from the reviewed master. '+master['time_evidence'],
        antenna_evidence='Defined ideal virtual endpoint: zero PCO/PCV and H/E/N; physical master antenna removed once by the audited kernel. Rover calibration remains separate.',
        note='EXPERIMENTAL joint FLOAT VRS; shared physical references, no independent observation. Integer ambiguity resolution MUST remain OFF. Original network product and review are mandatory; no covariance/accuracy approval.',
        synthetic=project['synthetic'],confirmed=True))
