# SPDX-License-Identifier: GPL-3.0-only
"""Local RTKLIB EX adapter. Raw jobs never insert or average baseline observations.

The external engine is BSD-2-Clause, not distributed by this GPL plugin.
Only the reviewed Windows EX 2.5.1 binary/configuration contract is enabled.
"""
from copy import deepcopy
from dataclasses import dataclass,asdict
from datetime import datetime,timezone,timedelta
from pathlib import Path
from uuid import uuid4
import csv,gzip,hashlib,io,json,math,os,re,subprocess
from . import network as n,rinex as r,baselines as b,engine

ENGINE_SHA256='e3dcb3d7ebc57ed8fb3d48f6be2de70c0744b6ad56913846e4b303297a348926'
ENGINE_VERSION='RTKLIB EX 2.5.1'
ENGINE_RELEASE='https://github.com/rtklibexplorer/RTKLIB/releases/tag/v2.5.1'
ENGINE_LICENSE='https://github.com/rtklibexplorer/RTKLIB/blob/v2.5.1/license.txt'
PROFILE='rtklib-ex251-gps-relative-v1'
CONTEXT_KEYS={'profile','job_id','project_id','occupation_sha256','engine_sha256','configuration_sha256',
    'inputs','base','rover','session','frame','epoch','mode','base_xyz','antenna_note','model_note','job_directory'}
WARNINGS=[
    'External engine result, not an independent validation or survey approval. FIX is not proof of correctness.',
    'Formal covariance is conditional on a base held exact. Base/frame/antenna/multipath/shared errors are not included.',
    'No receiver antenna PCV/PCO calibration, precise ephemerides, tides, frame or coordinate-epoch transformation in this first profile.',
    'Kinematic epochs and static filter states from one occupation are correlated; no averaging or automatic independence.',
    'GPS broadcast L1 or L1+L2 first profile; other constellations remain in original files but are excluded by the processor.'
]


@dataclass
class ProcessingOptions:
    base: str=''
    rover: str=''
    navigation: str=''
    mode: str='static'
    base_xyz: tuple=()
    frame: str=''
    epoch: str=''
    frequencies: int=2
    elevation: float=15.
    ratio: float=3.
    atmosphere: str='broadcast/saas'
    antenna_note: str=''
    model_note: str=''
    base_reviewed: bool=False
    antenna_reviewed: bool=False
    model_reviewed: bool=False
    license_reviewed: bool=False


def program(path):
    path=Path(path).expanduser().resolve()
    if path.name.lower()!='rnx2rtkp.exe' or not path.is_file() or path.is_symlink():
        raise ValueError('Select the separately installed, reviewed RTKLIB EX 2.5.1 rnx2rtkp.exe.')
    if path.stat().st_size>20*1024*1024 or engine.sha256(path)!=ENGINE_SHA256:
        raise ValueError('Unreviewed engine binary. This profile requires the exact RTKLIB EX 2.5.1 Windows executable; no other version is run.')
    return path


def _navigation_check(source):
    # A bounded plausibility screen, never an orbit/health/fit certificate.
    lines=source['metadata']['header']; count=len(lines)
    path=Path(source['path']); payload=path.read_bytes()
    if payload.startswith(b'\x1f\x8b'): payload=gzip.decompress(payload)
    body=payload.decode('ascii').splitlines()[count:]; gps=0; i=0; v2=source['metadata']['version']=='2.11'
    while i<len(body):
        line=body[i]; i+=1
        if not line.strip(): continue
        sat='G' if v2 else line[0]; length=(5 if source['metadata']['version']=='3.05' else 4) if sat=='R' else 4 if sat=='S' else 8
        records=body[i:i+length-1]; i+=length-1
        if sat!='G': continue
        prefix=3 if v2 else 4
        values=[[r.real(row[prefix+j*19:prefix+(j+1)*19]) if row[prefix+j*19:prefix+(j+1)*19].strip() else 0. for j in range(4)] for row in records]
        eccentricity,sqrt_a=values[1][1],values[1][3]
        if not 0<=eccentricity<.2 or not 4500<sqrt_a<6000:
            raise ValueError('GPS navigation contains nonphysical/unsupported orbit parameters. The 0.9 parser fixtures cannot be used for positioning.')
        gps+=1
    if gps<4: raise ValueError('At least four plausible GPS broadcast records are required; availability/health/fit are still assessed by the engine.')


def validate(project,options,cancelled=lambda:False,preflight=True):
    n.validate_project(project)
    if not isinstance(options,ProcessingOptions): raise ValueError('Unsupported raw-processing options.')
    if not all(type(v) is bool and v for v in (options.base_reviewed,options.antenna_reviewed,options.model_reviewed,options.license_reviewed)):
        raise ValueError('Review the fixed base/frame/epoch, antenna endpoints, model/dependence and external-engine licence.')
    if options.mode not in ('static','kinematic') or type(options.frequencies) is not int or options.frequencies not in (1,2):
        raise ValueError('Choose static or PPK kinematic, GPS L1 or L1+L2.')
    if options.atmosphere not in ('broadcast/saas','ideal-synthetic') or (options.atmosphere=='ideal-synthetic' and not project['synthetic']):
        raise ValueError('Atmosphere-off is restricted to explicitly constructed synthetic examples.')
    for value,low,high,label in ((options.elevation,10,45,'Elevation mask'),(options.ratio,3,10,'Ambiguity ratio threshold')):
        n._number(value,label)
        if not low<=value<=high: raise ValueError(label+' is outside this reviewed profile.')
    b.ecef(list(options.base_xyz))
    if options.frame not in b.FRAMES or options.epoch!=project['epoch']: raise ValueError('Document the ECEF frame and use the network coordinate epoch. No transformation is applied.')
    for key in ('antenna_note','model_note'): n._text(getattr(options,key),key,required=True)
    sources={s['id']:s for s in project.get('raw_sources',[])}
    try: base,rover,nav=[sources[key] for key in (options.base,options.rover,options.navigation)]
    except KeyError: raise ValueError('Select existing raw base, rover and navigation links.') from None
    if (base['role'],rover['role'],nav['role'])!=('BASE','ROVER','NAV') or base['point']==rover['point'] or base['session']!=rover['session']:
        raise ValueError('Select a base/rover pair of different physical points in the same acquisition session, plus navigation.')
    if base['utc_offset_seconds']!=rover['utc_offset_seconds']:raise ValueError('GPS observation sources must use the same documented GPS-minus-UTC offset for this profile.')
    for row in (base,rover):
        if row['metadata']['time_system']!='GPS' or row['utc_offset_seconds'] is None:
            raise ValueError('This first engine profile accepts explicitly documented GPS observation time only.')
        codes=row['metadata']['observation_types'].get('G',row['metadata']['observation_types'].get('*',[]))
        needed=('1','2')[:options.frequencies]
        if any(not any(c.startswith(('C'+freq,'P'+freq)) for c in codes) or not any(c.startswith('L'+freq) for c in codes) for freq in needed):
            raise ValueError('The selected GPS frequencies need both code and carrier-phase observations at base and rover.')
    if not preflight:return base,rover,nav,None
    report=r.inspect_catalogue(project,cancelled)
    if report['blockers']: raise ValueError('Raw preflight requires review: '+'; '.join(report['blockers']))
    _navigation_check(nav)
    return base,rover,nav,report


def configuration(options,base,rover):
    synthetic=options.atmosphere=='ideal-synthetic'
    values={
        'pos1-posmode':options.mode,'pos1-frequency':'l1' if options.frequencies==1 else 'l1+l2',
        'pos1-soltype':'forward','pos1-elmask':options.elevation,'pos1-dynamics':'off','pos1-tidecorr':0,
        'pos1-ionoopt':'off' if synthetic else 'brdc','pos1-tropopt':'off' if synthetic else 'saas',
        'pos1-sateph':'brdc','pos1-navsys':1,'pos2-armode':'continuous','pos2-gloarmode':'off',
        'pos2-bdsarmode':'off','pos2-arthres':options.ratio,'pos2-arthresmin':options.ratio,'pos2-arthresmax':options.ratio,
        'pos2-arminfix':10,'pos2-arlockcnt':0,'pos2-maxage':1,'misc-timeinterp':'off',
        'out-solformat':'xyz','out-outhead':'on','out-outopt':'on','out-outvel':'off','out-timesys':'gpst',
        'out-timeform':'hms','out-timendec':3,'out-outsingle':'off','out-solstatic':'all','out-outstat':'residual',
        'ant2-postype':'xyz','ant2-pos1':f'{options.base_xyz[0]:.8f}',
        'ant2-pos2':f'{options.base_xyz[1]:.8f}','ant2-pos3':f'{options.base_xyz[2]:.8f}'
    }
    for number,row in ((1,rover),(2,base)):
        h,e,north=row['metadata']['antenna_hen']
        values.update({f'ant{number}-antdele':e,f'ant{number}-antdeln':north,f'ant{number}-antdelu':h})
    return ''.join(f'{key}={value}\n' for key,value in values.items())


def prepare(folder,project,options,executable,cancelled=lambda:False):
    executable=program(executable); base,rover,nav,preflight=validate(project,options,cancelled)
    if cancelled():raise ValueError('Job preparation cancelled.')
    folder=Path(folder).resolve(); folder.mkdir(parents=True,exist_ok=False)
    context=dict(profile=PROFILE,job_id=str(uuid4()),project_id=project['project_id'],
        occupation_sha256=b.content_fingerprint([base['fingerprint'],rover['fingerprint']]),
        engine_sha256=ENGINE_SHA256,configuration_sha256='',inputs=[],base=base['point'],rover=rover['point'],
        session=base['session'],frame=options.frame,epoch=options.epoch,mode=options.mode,
        base_xyz=list(options.base_xyz),antenna_note=options.antenna_note,model_note=options.model_note,job_directory=str(folder))
    for name,row in (('base.rnx',base),('rover.rnx',rover),('navigation.rnx',nav)):
        fresh=r.read_source(row['path'],cancelled)
        if fresh['source_sha256']!=row['source_sha256']: raise ValueError('Source changed during job preparation; no job may run.')
        payload=Path(row['path']).read_bytes()
        if hashlib.sha256(payload).hexdigest()!=row['source_sha256']: raise ValueError('Source changed during copying.')
        if payload.startswith(b'\x1f\x8b'): payload=gzip.decompress(payload)
        (folder/name).write_bytes(payload)
        context['inputs'].append(dict(name=name,sha256=hashlib.sha256(payload).hexdigest(),
            original_sha256=row['source_sha256'],fingerprint=row['fingerprint'],source_id=row['id']))
    config=configuration(options,base,rover).encode('ascii');(folder/'processing.conf').write_bytes(config)
    context['configuration_sha256']=hashlib.sha256(config).hexdigest()
    (folder/'network.sanet.json').write_text(n.dumps_project(project),encoding='utf-8')
    manifest=dict(format='survey-adjustment-raw-job',version=1,state='PREPARED',context=context,
        options=asdict(options),network_sha256=engine.sha256(folder/'network.sanet.json'),
        engine_path=str(executable),command=command(executable),created_utc=datetime.now(timezone.utc).isoformat(),
        preflight=preflight,outputs={})
    if cancelled():raise ValueError('Job preparation cancelled; no engine started.')
    _write(folder,manifest)
    return manifest


def command(executable):
    return [str(executable),'-k','processing.conf','-o','solution.pos','rover.rnx','base.rnx','navigation.rnx']


def _write(folder,manifest):
    (Path(folder)/'processing.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')


def load_manifest(folder):
    path=Path(folder)/'processing.json'
    with path.open('rb') as handle: payload=handle.read(n.MAX_BYTES+1)
    if len(payload)>n.MAX_BYTES: raise ValueError('Raw job metadata exceeds the project-size limit.')
    result=json.loads(payload)
    n._keys(result,{'format','version','state','context','options','network_sha256','engine_path','command','created_utc','preflight','outputs'},'Raw job')
    if result['format']!='survey-adjustment-raw-job' or result['version']!=1: raise ValueError('Unsupported processing receipt.')
    validate_context(result['context'])
    if engine.sha256(Path(folder)/'network.sanet.json')!=result['network_sha256']: raise ValueError('Saved job network changed.')
    context=result['context']
    if engine.sha256(Path(folder)/'processing.conf')!=context['configuration_sha256']: raise ValueError('Processing configuration changed.')
    for row in context['inputs']:
        if engine.sha256(Path(folder)/row['name'])!=row['sha256']: raise ValueError('Job input changed: '+row['name'])
    return result


def validate_context(context):
    n._keys(context,CONTEXT_KEYS,'Raw processing provenance')
    if context['profile']!=PROFILE or context['engine_sha256']!=ENGINE_SHA256 or context['mode'] not in ('static','kinematic'):
        raise ValueError('Unsupported processing profile or engine identity.')
    for key in ('job_id','project_id','base','rover','session','epoch','antenna_note','model_note','job_directory'):
        n._text(context[key],key,required=True)
    if context['frame'] not in b.FRAMES: raise ValueError('Invalid processing frame.')
    b.ecef(context['base_xyz'])
    for key in ('occupation_sha256','configuration_sha256'):
        if not isinstance(context[key],str) or len(context[key])!=64 or any(c not in '0123456789abcdef' for c in context[key]): raise ValueError('Invalid processing identity.')
    if not isinstance(context['inputs'],list) or len(context['inputs'])!=3: raise ValueError('Expected three raw job input identities.')
    for index,row in enumerate(context['inputs']):
        n._keys(row,{'name','sha256','original_sha256','fingerprint','source_id'},'Raw job input')
        if row['name']!=('base.rnx','rover.rnx','navigation.rnx')[index]: raise ValueError('Invalid processing input basename/order.')
        for key in ('sha256','original_sha256','fingerprint'):
            if not isinstance(row[key],str) or len(row[key])!=64 or any(c not in '0123456789abcdef' for c in row[key]): raise ValueError('Invalid raw job hash.')
        n._text(row['source_id'],'Raw source ID',required=True)
    if b.content_fingerprint([row['fingerprint'] for row in context['inputs'][:2]])!=context['occupation_sha256']:
        raise ValueError('Raw occupation identity differs from source fingerprints.')


def _decode(folder,manifest):
    folder=Path(folder); source=b.parse_source((folder/'solution.pos').read_bytes(),'solution.pos')
    context=manifest['context']; meta=source['metadata']
    if (meta['representation'],meta['mode'],meta['time_system'])!=('XYZ',context['mode'],'GPST') or math.dist(meta['reference'],context['base_xyz'])>.0002:
        raise ValueError('Output mode/time/coordinates/base differ from the submitted processing job.')
    text=(folder/'solution.pos').read_text(encoding='ascii')
    if '% program   : RTKLIB ver.EX 2.5.1' not in text: raise ValueError('Unexpected output engine/version header.')
    project=n.load_project(folder/'network.sanet.json'); sources={row['id']:row for row in project['raw_sources']}
    base,rover=[sources[row['source_id']] for row in context['inputs'][:2]]
    low=max(row['metadata']['first_tick'] for row in (base,rover)); high=min(row['metadata']['last_tick'] for row in (base,rover))
    for row in source['rows']:
        stamp=datetime.fromisoformat(row['time']); tick=r.epoch([str(stamp.year),str(stamp.month),str(stamp.day),str(stamp.hour),str(stamp.minute),f'{stamp.second+stamp.microsecond/1e6:.6f}'])
        if not low<=tick<=high: raise ValueError('Solution epoch falls outside the selected raw overlap.')
    source['processing']=deepcopy(context)
    source['marked_synthetic']=project['synthetic']
    return source


def inspect_result(folder,exit_code,executable):
    folder=Path(folder); manifest=load_manifest(folder)
    if manifest['state']!='PREPARED' or exit_code!=0: raise ValueError('Raw processing failed or this run is no longer pending. Partial results are not accepted.')
    program(executable)
    if (folder/'console.txt').exists() and re.search(r'invalid option|error\s*:',(folder/'console.txt').read_text(encoding='utf-8',errors='replace'),re.I):
        raise ValueError('Engine reported an invalid option or processing error; review the local console log.')
    if not (folder/'solution.pos').is_file() or (folder/'solution.pos').stat().st_size>b.MAX_SOURCE_BYTES: raise ValueError('Missing or oversized engine solution.')
    source=_decode(folder,manifest)
    if not source['rows']: raise ValueError('The engine produced no solution epochs.')
    selected={row['source_id'] for row in manifest['context']['inputs']}
    for original in n.load_project(folder/'network.sanet.json')['raw_sources']:
        if original['id'] not in selected:continue
        if engine.sha256(original['path'])!=original['source_sha256']: raise ValueError('Original source changed during processing; do not use this result.')
    counts={quality:sum(row['quality']==quality for row in source['rows']) for quality in sorted({row['quality'] for row in source['rows']})}
    result=dict(mode=manifest['context']['mode'],epochs=len(source['rows']),quality_counts=counts,
        eligible_fix=sum(row['eligible'] for row in source['rows']),warnings=WARNINGS,
        first_time=source['rows'][0]['time'],last_time=source['rows'][-1]['time'],time_system='GPST')
    with (folder/'epochs.csv').open('w',encoding='utf-8',newline='') as handle:
        from .coordinate_job import llh
        writer=csv.writer(handle);writer.writerow(['Time GPST','X ECEF m','Y ECEF m','Z ECEF m','Latitude WGS84 ellipsoid deg','Longitude deg','h WGS84 ellipsoid m','Q','Baseline eligible','Reason'])
        for row in source['rows']: writer.writerow([row['time'],*row['values'],*llh(row['values'],'EPSG:4979'),row['quality'],row['eligible'],row['reason']])
    (folder/'report.txt').write_text(ENGINE_VERSION+' — '+result['mode']+'\n'+json.dumps(result,indent=2)+'\n\n'+
        'Original engine solution and covariance retained in solution.pos. No averaged coordinate or automatic baseline import.\n'+
        'Input originals, copied inputs, configuration and engine SHA256 recorded in processing.json. Hashes establish integrity, not authenticity.\n',encoding='utf-8')
    manifest['state']='COMPLETED';manifest['outputs']={name:engine.sha256(folder/name) for name in ('solution.pos','solution.pos.stat','epochs.csv','report.txt') if (folder/name).exists()}
    _write(folder,manifest)
    return result,source


def abort(folder,reason):
    manifest=load_manifest(folder);manifest['state']='ABORTED';_write(folder,manifest)
    (Path(folder)/'ABORTED.txt').write_text(str(reason)+'\nPartial files must not be used.\n',encoding='utf-8')


def source_for_import(path):
    """The existing POS reader attaches checked provenance to a completed job POS."""
    path=Path(path)
    if path.name!='solution.pos' or not (path.parent/'processing.json').is_file(): return None
    manifest=load_manifest(path.parent)
    if manifest['state']!='COMPLETED' or manifest['outputs'].get('solution.pos')!=engine.sha256(path):
        raise ValueError('Processing job is incomplete, stopped or its solution has changed.')
    for name,digest in manifest['outputs'].items():
        if name not in ('solution.pos','solution.pos.stat','epochs.csv','report.txt') or engine.sha256(path.parent/name)!=digest:
            raise ValueError('Processing result package changed.')
    return _decode(path.parent,manifest)


def validate_handoff(project,source,options):
    context=source.get('processing')
    if context is None: return
    validate_context(context)
    if options.stationary_confirmed is not True:
        raise ValueError('Confirm that the selected result represents the documented stationary physical point occupation. Moving trajectory epochs are not point baselines.')
    n._text(options.occupation_note,'Stationary occupation / selected epoch evidence',required=True)
    if (options.origin,options.target,options.session,options.frame,options.epoch)!=(context['base'],context['rover'],context['session'],context['frame'],context['epoch']):
        raise ValueError('Use the processed job physical points, acquisition session, frame and coordinate epoch. A new label does not establish a new occupation.')
    if project['project_id']!=context['project_id']: raise ValueError('Import this job into its original network project, preserving raw-source provenance.')
    identities={row['id']:row['fingerprint'] for row in project.get('raw_sources',[])}
    if any(identities.get(row['source_id'])!=row['fingerprint'] for row in context['inputs']): raise ValueError('Raw catalogue differs from the processing job.')
    for batch in project['imports']:
        if batch.get('processing',{}).get('occupation_sha256')==context['occupation_sha256']:
            raise ValueError('This raw occupation was already imported. Reprocessing, a second epoch or another mode is not an independent baseline.')


def run(folder,project,options,executable,timeout=120):
    """Integration tests; native UI uses cancellable nonblocking QProcess."""
    executable=program(executable);prepare(folder,project,options,executable)
    try:
        result=subprocess.run(command(executable),cwd=folder,env=engine.environment(Path(executable)),capture_output=True,
            timeout=timeout,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        (Path(folder)/'console.txt').write_bytes((result.stdout+b'\n'+result.stderr)[:5*1024*1024])
        return inspect_result(folder,result.returncode,executable)
    except Exception as error:
        abort(folder,str(error));raise
