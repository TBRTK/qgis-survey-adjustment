# SPDX-License-Identifier: GPL-3.0-only
"""Local RTKLIB EX adapter. Raw jobs never insert or average baseline observations.

The external engine is BSD-2-Clause, not distributed by this GPL plugin.
Reviewed upstream EX and separately identified experimental Survey extensions
have explicit binary/configuration contracts. Third-party notices are retained.
"""
from copy import deepcopy
from dataclasses import dataclass,asdict
from datetime import datetime,timezone,timedelta
from decimal import Decimal,InvalidOperation
from pathlib import Path
from uuid import uuid4
import csv,gzip,hashlib,io,json,math,os,re,subprocess
from bisect import bisect_left
from . import network as n,rinex as r,baselines as b,engine,antenna

ENGINE_SHA256='e3dcb3d7ebc57ed8fb3d48f6be2de70c0744b6ad56913846e4b303297a348926'
ENGINE_VERSION='RTKLIB EX 2.5.1'
ENGINE_RELEASE='https://github.com/rtklibexplorer/RTKLIB/releases/tag/v2.5.1'
ENGINE_LICENSE='https://github.com/rtklibexplorer/RTKLIB/blob/v2.5.1/license.txt'
PROFILE='rtklib-ex251-gps-relative-v1'
MULTI_PROFILE='rtklib-ex251-gec-relative-v2'
SYSTEM_PROFILE='rtklib-survey-sa1-relative-v3'
SYSTEM_ENGINE_SHA256='744273e50daaf8d7653dfe67b67613c36f0abcf2eb64adb12ddafa278e5230d1'
SYSTEM_ENGINE_VERSION='RTKLIB EX 2.5.1-SA1 — experimental Survey PCV extension'
AZIMUTH_PROFILE='rtklib-survey-sa2-relative-v4'
AZIMUTH_ENGINE_SHA256='81054215ea966854f79b6ca8ecb802f189eff6e99ee934d65961c3ddd9e4e780'
AZIMUTH_ENGINE_VERSION='RTKLIB EX 2.5.1-SA2 — experimental azimuth and band selection'
STATIC_ATMOSPHERE='estimate-iono/wet-delay'
STATIC_PROFILE='rtklib-ex251-static-atmosphere-v5'
ITRF_PROFILE='rtklib-ex251-itrf2020-static-clock-v6'
ITRF_FLOAT_PROFILE='rtklib-ex251-itrf2020-static-clock-float-v7'
NETWORK_FLOAT_PROFILE='rtklib-ex251-network-float-static-v8'
CLOCK_MODEL='pntpos-gps-clock-ms-output-v1'
CLOCK_PROFILES=(ITRF_PROFILE,ITRF_FLOAT_PROFILE,NETWORK_FLOAT_PROFILE)
MODERN_PROFILES=(MULTI_PROFILE,SYSTEM_PROFILE,AZIMUTH_PROFILE,STATIC_PROFILE,*CLOCK_PROFILES)
SYSTEMS={'G':('GPS',1,('1','2')),'E':('Galileo',8,('1','7')),'C':('BeiDou B1I/B2I',32,('2','7')),'R':('GLONASS G1/G2',4,('1','2'))}
AZIMUTH_SIGNALS={'G':(('1','CPWLSX'),('2','WPCSLX')),'R':(('1','CP'),('2','CP')),
    'E':(('1','CBX'),('5','QIX')),'C':(('1','PDX'),('6','IQX'))}
CONTEXT_KEYS={'profile','job_id','project_id','occupation_sha256','engine_sha256','configuration_sha256',
    'inputs','base','rover','session','frame','epoch','mode','base_xyz','antenna_note','model_note','job_directory'}
WARNINGS=[
    'External engine result, not an independent validation or survey approval. FIX is not proof of correctness.',
    'Formal covariance is conditional on a base held exact. Base/frame/antenna/multipath/shared errors are not included.',
    'No precise ephemerides, satellite antenna calibration, tides, frame or coordinate-epoch transformation.',
    'Kinematic epochs and static filter states from one occupation are correlated; no averaging or automatic independence.',
    'Only explicitly selected GPS/Galileo/BeiDou signals are enabled. GLONASS, QZSS, SBAS and NavIC are excluded in this reviewed profile.'
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
    systems: tuple=('G',)
    calibration: str='none'
    antex_path: str=''
    calibration_note: str=''
    engine_variant: str='upstream'
    base_azimuth_deg: float|None=None
    rover_azimuth_deg: float|None=None
    orientation_reviewed: bool=False
    orientation_mode: str=''
    ambiguity_mode: str='continuous'
    reference_network_path: str=''
    network_kernel_path: str=''


def processing_profile(options):
    if options.reference_network_path:return NETWORK_FLOAT_PROFILE
    return (ITRF_FLOAT_PROFILE if options.ambiguity_mode=='off' else ITRF_PROFILE) if options.frame=='EPSG:9988' else STATIC_PROFILE if options.atmosphere==STATIC_ATMOSPHERE else {'upstream':MULTI_PROFILE,'survey-sa1':SYSTEM_PROFILE,'survey-sa2':AZIMUTH_PROFILE}.get(options.engine_variant)


def reference_network_review(project, references, target_xyz, cancelled=lambda:False):
    """Audit a three-station VRS candidate using existing raw-catalogue evidence.

    This is geometry/availability, not a network atmosphere/ambiguity estimator.
    The rover's observations and known coordinate never enter the reference fit.
    Negative affine weights are retained as extrapolation, never clipped to make
    the network appear to surround the target. No virtual RINEX is fabricated.
    """
    import numpy as np
    from .coordinate_job import llh
    n.validate_project(project);b.ecef(list(target_xyz))
    if not isinstance(references,list) or len(references)!=3:
        raise ValueError('Select exactly three distinct existing physical BASE sources for this initial network review.')
    sources={row['id']:row for row in project.get('raw_sources',[])}
    seen=set();points=set();rows=[];issues=[];notes=[];epochs=[];signatures=[]
    lat,lon,_=llh(target_xyz,'EPSG:9989');lat=math.radians(lat);lon=math.radians(lon)
    east=(-math.sin(lon),math.cos(lon),0.)
    north=(-math.sin(lat)*math.cos(lon),-math.sin(lat)*math.sin(lon),math.cos(lat))
    for declared in references:
        n._keys(declared,{'source_id','xyz','frame','coordinate_epoch_decimal_year','evidence'},'Reference station review')
        key=declared['source_id']
        if key not in sources or key in seen:raise ValueError('Reference sources must be distinct existing catalogue links.')
        seen.add(key);source=sources[key];meta=source['metadata']
        if source['role']!='BASE' or meta['file_type']!='OBS' or source['point'] in points:
            raise ValueError('Select distinct physical BASE points, not a rover, virtual station or a second file for the same point.')
        points.add(source['point']);b.ecef(list(declared['xyz']))
        if declared['frame']!='EPSG:9988':raise ValueError('This initial geometry review requires explicitly documented ITRF2020 ECEF for all references and target.')
        epoch=declared['coordinate_epoch_decimal_year'];n._number(epoch,'Reference coordinate epoch')
        if not 2000<=epoch<=2050:raise ValueError('Document each reference coordinate epoch as decimal year, 2000-2050.')
        n._text(declared['evidence'],'Reference coordinate/frame/epoch evidence',required=True);epochs.append(epoch)
        entry,unchanged=r.inspect_source(project,source,cancelled)
        if not unchanged:raise ValueError('Reference source requires review: '+'; '.join(entry['issues']))
        if meta['time_system']!='GPS' or source['utc_offset_seconds'] is None:
            raise ValueError('Each reference needs reviewed GPS time and source-minus-UTC evidence.')
        if source['session']!=sources[references[0]['source_id']]['session']:
            raise ValueError('Reference sources must belong to the same acquisition session.')
        if any(meta['events'].get(flag,0) for flag in ('2','3','4')):
            issues.append(source['point']+': moving/site/header-change events require separate continuous occupations.')
        if meta['receiver_clock_offset_epochs']:
            issues.append(source['point']+': recorded nonzero receiver clock offsets need a reviewed NETWORK clock model; do not round or silently shift them.')
        signatures.append({sample[0]-source['utc_offset_seconds']*r.TICK:set(sample[2].split()) for sample in meta['samples']})
        difference=[x-y for x,y in zip(declared['xyz'],target_xyz)]
        rows.append(dict(source_id=key,point=source['point'],source_sha256=source['source_sha256'],fingerprint=source['fingerprint'],
            xyz=list(declared['xyz']),frame=declared['frame'],coordinate_epoch_decimal_year=epoch,evidence=declared['evidence'],
            east_from_target_m=sum(x*y for x,y in zip(difference,east)),north_from_target_m=sum(x*y for x,y in zip(difference,north)),
            chord_to_target_m=math.dist(declared['xyz'],target_xyz),antenna_hen=meta['antenna_hen'],antenna_type=meta['antenna'],
            receiver_clock_offset_epochs=meta['receiver_clock_offset_epochs']))
    edges=[dict(points=[a['point'],c['point']],chord_m=math.dist(a['xyz'],c['xyz'])) for i,a in enumerate(rows) for c in rows[i+1:]]
    if min(edge['chord_m'] for edge in edges)<1:raise ValueError('Distinct reference names cannot represent the same physical coordinate.')
    matrix=np.array([[1.,1.,1.],[row['east_from_target_m']/1000 for row in rows],[row['north_from_target_m']/1000 for row in rows]])
    condition=float(np.linalg.cond(matrix))
    if not math.isfinite(condition) or condition>1e8:raise ValueError('Degenerate or nearly collinear reference triangle; a two-dimensional network cannot be inferred.')
    weights=np.linalg.solve(matrix,np.array([1.,0.,0.]))
    if not np.all(np.isfinite(weights)) or float(np.max(np.abs(matrix@weights-[1.,0.,0.])))>1e-8:
        raise ValueError('Invalid affine geometry solution.')
    for row,weight in zip(rows,weights):row['affine_weight']=float(weight)
    inside=bool(np.all(weights>=-1e-10) and np.all(weights<=1+1e-10))
    common=sorted(set.intersection(*(set(value) for value in signatures)))
    counts=[];supported=[]
    for tick in common:
        if cancelled():raise ValueError('Reference-network review cancelled.')
        signals=set.intersection(*(value[tick] for value in signatures))
        by_sat={}
        for signal in signals:
            sat,band=signal.split('/')
            if sat.startswith('G'):by_sat.setdefault(sat,set()).add(band)
        sats=sorted(sat for sat,bands in by_sat.items() if any(code.startswith('1') for code in bands) and any(code.startswith('2') for code in bands))
        counts.append(len(sats))
        if len(sats)>=4:supported.append(tick)
    if len(supported)<2:issues.append('Fewer than two exact common reference epochs with four matching GPS L1/L2 code/phase satellites.')
    types=[sources[row['source_id']]['metadata']['observation_types'].get('G',[]) for row in rows]
    common_codes=set.intersection(*(set(value) for value in types))
    plan={band:sorted(code[1:] for code in common_codes if code.startswith('C'+band) and 'L'+code[1:] in common_codes) for band in ('1','2')}
    if not all(plan.values()):issues.append('References lack a common exact GPS code/phase signal in both L1/L2 bands; no tracking-code substitution.')
    if not inside:notes.append('EXTRAPOLATION: target outside reference triangle. Negative weights amplify network/model errors; shorter virtual baseline does not remove distant-reference errors.')
    notes+=['Three references give zero spatial residual degrees of freedom for an affine plane; curvature and extrapolation accuracy cannot be checked internally.',
        'Equal-independent-error gain is a geometric diagnostic only; real reference errors are correlated and no VRS covariance or accuracy is estimated.',
        'Weights describe geometry only. They are not weights for averaging receiver carrier phases, baseline coordinates or independent positions.',
        'Independent pairwise RTKLIB ION/TROP status states lack a joint network ambiguity datum/covariance and must not be treated as VRS corrections.',
        'Reference coordinate epochs are retained individually; no propagation or inferred common frame realisation. Geometry is not precise coordinate acceptance.']
    return dict(format='survey-adjustment-reference-network-review',version=1,status='NETWORK_INPUT_REVIEW_REQUIRED' if issues else 'NETWORK_MODEL_REQUIRED',
        project_sha256=r.content_fingerprint(project),reference_stations=rows,target_ecef_itrf2020=list(target_xyz),reference_edges=edges,
        geometry=dict(mode='INTERPOLATION' if inside else 'EXTRAPOLATION',inside_triangle=inside,condition_number=condition,
            affine_weights=[float(v) for v in weights],equal_independent_error_gain=float(np.linalg.norm(weights)),spatial_residual_degrees_of_freedom=0),
        availability=dict(common_recorded_utc_epochs=len(common),gps_l1_l2_epochs_at_least_four_satellites=len(supported),
            minimum_common_two_band_satellites=min(counts) if counts else 0,maximum_common_two_band_satellites=max(counts) if counts else 0,
            first_utc=r.tick_text(common[0])+'Z' if common else '',last_utc=r.tick_text(common[-1])+'Z' if common else '',common_exact_signals=plan),
        issues=issues,notes=notes,rover_observations_used=False,virtual_observations_generated=False,
        synthesis_blocker='A reviewed joint network clock, antenna, code/phase bias, ambiguity and atmosphere estimator plus virtual-observation/readback validation is required. The installed pairwise engine is not a VRS generator.')


def warnings(options):
    notes=list(WARNINGS)
    if options.get('ambiguity_mode','continuous')=='off':
        notes.append('FLOAT ONLY: integer ambiguity resolution is disabled. For derived network/VRS inputs, shared network ambiguity/model/base uncertainty is excluded from the raw engine covariance; no FIX or independent-baseline claim.')
    if options.get('atmosphere')==STATIC_ATMOSPHERE:
        notes+=['Experimental stationary GPS L1/L2 atmosphere estimation: baseline-scaled ionosphere states and receiver zenith wet-delay states; not independent atmospheric measurements.',
            'Broadcast orbits remain selected. This is not complete 30–100 km field validation, precise-product processing, PPP or VRS generation. Base coordinates/frame/epoch, antenna model and independent checks remain essential.']
    if options.get('engine_variant') in ('survey-sa1','survey-sa2'):
        notes[-1]='Selected GPS/Galileo/BeiDou/GLONASS signals only. GLONASS ambiguity fixing is disabled; FDMA channels come from reviewed navigation. QZSS/SBAS/NavIC excluded.'
        notes+=['Experimental local engine extension, not an official RTKLIB release. Separate per-constellation/frequency receiver arrays; no GPS-pattern fallback.']
    if options.get('engine_variant')=='survey-sa2':
        notes+=['SA2: Galileo E1/E5a (F/NAV), BeiDou B1C/B3I. BeiDou excluded from coarse absolute SPP because RINEX-3 D1/D2 does not supply B1C code biases; relative same-signal differences retained.',
            'Fixed, level antenna orientation only; heading is the antenna NRP clockwise from true/geodetic north. No changing yaw, tilt or IMU corrections. Provider copied/unverified model notes must be retained.']
    return notes+(['Receiver PCO/PCV from the exact selected ANTEX constellation/frequency entries; header H/E/N remains a separate marker-to-ARP offset.']
        if options['calibration']=='antex' else ['No receiver PCO/PCV calibration applied. Header H/E/N alone does not calibrate an antenna.'])


def calibration_review(options,base,rover):
    if options.calibration=='none':return None
    catalogue=antenna.read(options.antex_path)
    return dict(original_sha256=catalogue['sha256'],evidence=options.calibration_note,
        base=antenna.select(catalogue,base['metadata'],options.frequencies,options.systems,options.engine_variant=='survey-sa2'),
        rover=antenna.select(catalogue,rover['metadata'],options.frequencies,options.systems,options.engine_variant=='survey-sa2'),
        engine_file='receiver.atx',engine_sha256='')


def program(path):
    path=Path(path).expanduser().resolve()
    if path.name.lower()!='rnx2rtkp.exe' or not path.is_file() or path.is_symlink():
        raise ValueError('Select the separately installed, reviewed RTKLIB EX 2.5.1 rnx2rtkp.exe.')
    if path.stat().st_size>20*1024*1024 or engine.sha256(path) not in (ENGINE_SHA256,SYSTEM_ENGINE_SHA256,AZIMUTH_ENGINE_SHA256):
        raise ValueError('Unreviewed engine binary. Select the exact reviewed upstream EX 2.5.1 or separate experimental Survey SA1/SA2 executable.')
    return path


def _navigation_check(source,systems=('G',),azimuth=False):
    # A bounded plausibility screen, never an orbit/health/fit certificate.
    lines=source['metadata']['header']; count=len(lines)
    path=Path(source['path']); payload=path.read_bytes()
    if payload.startswith(b'\x1f\x8b'): payload=gzip.decompress(payload)
    body=payload.decode('ascii').splitlines()[count:]; counts={s:0 for s in systems}; glo={}; i=0; v2=source['metadata']['version']=='2.11'
    while i<len(body):
        line=body[i]; i+=1
        if not line.strip(): continue
        sat='G' if v2 else line[0]; length=(5 if source['metadata']['version']=='3.05' else 4) if sat=='R' else 4 if sat=='S' else 8
        records=body[i:i+length-1]; i+=length-1
        if sat not in systems: continue
        prefix=3 if v2 else 4
        values=[[r.real(row[prefix+j*19:prefix+(j+1)*19]) if row[prefix+j*19:prefix+(j+1)*19].strip() else 0. for j in range(4)] for row in records]
        if sat=='R':
            if v2:raise ValueError('Reviewed GLONASS processing requires mixed RINEX 3 navigation.')
            if not records[1][prefix+3*19:prefix+4*19].strip():
                raise ValueError('Missing GLONASS FDMA channel; a blank field must not become channel zero.')
            position=[values[j][0]*1000 for j in range(3)];velocity=[values[j][1]*1000 for j in range(3)]
            channel=values[1][3]
            if not 19000000<math.sqrt(sum(v*v for v in position))<30000000 or math.sqrt(sum(v*v for v in velocity))>6000:
                raise ValueError('Nonphysical GLONASS position/velocity.')
            if channel!=int(channel) or not -7<=channel<=6:raise ValueError('GLONASS FDMA channel must be an explicit integer -7 through +6 in navigation; no wavelength is inferred.')
            tick=r.epoch(line[4:23].split());satellite=line[:3]
            records=glo.setdefault(satellite,{})
            if tick in records and records[tick]!=int(channel):raise ValueError('Conflicting GLONASS channels at one navigation epoch.')
            records[tick]=int(channel)
            counts[sat]+=1;continue
        eccentricity,sqrt_a=values[1][1],values[1][3]
        if azimuth and sat=='E' and not int(values[4][1])&256:continue
        if not 0<=eccentricity<.2 or not 4500<sqrt_a<(7000 if sat=='C' else 6000):
            raise ValueError(SYSTEMS[sat][0]+' navigation contains nonphysical/unsupported orbit parameters. The 0.9 parser fixtures cannot be used for positioning.')
        counts[sat]+=1
    if sum(counts.values())<4 or any(v==0 for v in counts.values()):
        raise ValueError('At least four plausible broadcast records in total and navigation for every selected system are required; availability/health/fit are still assessed by the engine.')
    return glo


def selected_signals(options,base,rover):
    """Same explicit code/phase signal at BOTH receivers; no band substitution."""
    plan={}
    for system in options.systems:
        plan[system]=[]
        for band,priority in AZIMUTH_SIGNALS[system][:options.frequencies]:
            common=[]
            for suffix in priority:
                code=band+suffix
                if all('C'+code in row['metadata']['observation_types'].get(system,[]) and
                    'L'+code in row['metadata']['observation_types'].get(system,[]) for row in (base,rover)):common.append(code)
            if not common:raise ValueError(system+' band '+band+' needs a common reviewed code/phase signal at both receivers. No E5b/E5a or B1I/B1C substitution.')
            plan[system].append(common[0])
    return plan


def validate(project,options,cancelled=lambda:False,preflight=True):
    n.validate_project(project)
    if not isinstance(options,ProcessingOptions): raise ValueError('Unsupported raw-processing options.')
    if not all(type(v) is bool and v for v in (options.base_reviewed,options.antenna_reviewed,options.model_reviewed,options.license_reviewed)):
        raise ValueError('Review the fixed base/frame/epoch, antenna endpoints, model/dependence and external-engine licence.')
    if options.mode not in ('static','kinematic') or type(options.frequencies) is not int or options.frequencies not in (1,2):
        raise ValueError('Choose static or PPK kinematic, one or two reviewed frequency slots.')
    if options.engine_variant not in ('upstream','survey-sa1','survey-sa2'):raise ValueError('Select a reviewed engine profile.')
    azimuth=options.engine_variant=='survey-sa2'
    if azimuth:
        if options.orientation_mode!='fixed-level':raise ValueError('SA2 requires fixed, level antenna orientation throughout the occupation. Changing yaw/tilt needs time-tagged orientation data and another reviewed model; it is not corrected here.')
        for value in (options.base_azimuth_deg,options.rover_azimuth_deg):
            n._number(value,'Antenna NRP azimuth')
            if not 0<=value<360:raise ValueError('Antenna NRP azimuth must be 0 <= degrees < 360, clockwise from true/geodetic north.')
        if options.orientation_reviewed is not True:raise ValueError('Review fixed, level antenna orientation, heading reference and provider calibration notes.')
    elif options.base_azimuth_deg is not None or options.rover_azimuth_deg is not None or options.orientation_reviewed or options.orientation_mode:
        raise ValueError('Explicit orientation requires Survey SA2; legacy engines must not silently ignore it.')
    if (not isinstance(options.systems,(list,tuple)) or not options.systems or len(set(options.systems))!=len(options.systems)
            or any(s not in SYSTEMS for s in options.systems) or 'G' not in options.systems):
        raise ValueError('Select GPS, optionally Galileo E1/E5b, BeiDou B1I/B2I and GLONASS G1/G2. GPS is required by this initialisation profile.')
    if 'R' in options.systems and options.engine_variant not in ('survey-sa1','survey-sa2'):raise ValueError('GLONASS requires the separately reviewed Survey SA1 or SA2 engine profile; legacy jobs remain unchanged.')
    if options.calibration not in ('none','antex'):raise ValueError('Choose explicit uncalibrated or receiver ANTEX processing.')
    if azimuth and options.calibration!='antex':raise ValueError('Survey SA2 requires an explicit receiver ANTEX model. Use the existing upstream/SA1 profile for uncalibrated processing.')
    if options.calibration=='antex':
        if tuple(options.systems)!=('G',) and options.engine_variant not in ('survey-sa1','survey-sa2'):
            raise ValueError('RTKLIB EX 2.5.1 reads GPS receiver calibration only. ANTEX plus multiple systems is blocked to prevent applying GPS patterns to other signals. Select GPS only or explicitly uncalibrated multi-GNSS.')
        n._text(options.calibration_note,'ANTEX source/version, usage rights and antenna installation evidence',required=True)
        if not options.antex_path:raise ValueError('Choose a local receiver ANTEX file.')
    elif options.antex_path or options.calibration_note:
        raise ValueError('Clear the ANTEX file/evidence or enable calibration; a selected file must not be silently ignored.')
    if options.atmosphere not in ('broadcast/saas','ideal-synthetic',STATIC_ATMOSPHERE) or (options.atmosphere=='ideal-synthetic' and not project['synthetic']):
        raise ValueError('Atmosphere-off is restricted to explicitly constructed synthetic examples.')
    if options.atmosphere==STATIC_ATMOSPHERE and (options.mode!='static' or options.frequencies!=2 or tuple(options.systems)!=('G',) or options.engine_variant!='upstream'):
        raise ValueError('The initial estimated-atmosphere profile requires stationary Static, GPS L1/L2 and the reviewed upstream EX engine. Other profiles remain unchanged.')
    if options.frame=='EPSG:9988' and (options.mode!='static' or options.frequencies!=2 or tuple(options.systems)!=('G',) or options.engine_variant!='upstream'):
        raise ValueError('The initial ITRF2020 clock-audited profile requires upstream EX, static GPS L1/L2.')
    if options.ambiguity_mode not in ('continuous','off') or (options.ambiguity_mode=='off' and options.frame!='EPSG:9988'):
        raise ValueError('Explicit FLOAT-only processing requires the ITRF2020 static GPS L1/L2 clock-audited profile.')
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
    from . import network_vrs
    derived=network_vrs.requires_float(base)
    if derived!=bool(options.reference_network_path):raise ValueError('A generated FLOAT VRS requires its original reviewed network product; do not discard or relabel derivation.')
    if derived:
        if options.ambiguity_mode!='off' or options.frame!='EPSG:9988' or options.calibration!='antex' or not options.network_kernel_path:
            raise ValueError('Derived network VRS requires FLOAT ONLY, ITRF2020, explicit receiver/virtual calibration and the reviewed network kernel.')
        if preflight:
            product=network_vrs.load_product(options.reference_network_path,options.network_kernel_path,cancelled=cancelled)
            if product['report']['virtual_sha256']!=base['source_sha256'] or product['manifest']['navigation']['sha256']!=nav['source_sha256'] or math.dist(options.base_xyz,product['report']['target_xyz'])>.0002:
                raise ValueError('VRS input, fixed virtual coordinate or navigation differs from its reviewed network product.')
    if azimuth:
        selected_signals(options,base,rover)
        for row,heading in ((base,options.base_azimuth_deg),(rover,options.rover_azimuth_deg)):
            if 'C' in options.systems and row['metadata']['version'] not in ('3.03','3.04','3.05'):
                raise ValueError('SA2 BeiDou B1C requires RINEX 3.03–3.05; older B1 identifiers are ambiguous.')
            if any(row['metadata']['events'].get(flag,0) for flag in ('2','3')):
                raise ValueError('RINEX declares antenna motion or a new site; split and review occupations/orientation before fixed-level SA2 processing.')
            for line in row['metadata']['header']:
                label=line[60:80].strip()
                if label=='SYS / PCVS APPLIED' and line[:60].strip():raise ValueError('RINEX declares antenna PCV already applied; do not apply a second model.')
                if label in ('ANTENNA: B.SIGHT XYZ','ANTENNA: ZERODIR XYZ') and line[:60].strip():
                    raise ValueError('RINEX declares body-frame antenna orientation; SA2 requires a reviewed level geodetic NRP heading, not an implicit body-frame transform.')
                if label=='ANTENNA: ZERODIR AZI' and line[:14].strip():
                    declared=r.real(line[:14])
                    if abs((declared-heading+180)%360-180)>1e-6:raise ValueError('Explicit antenna azimuth differs from the RINEX zero-direction header.')
    for row in (base,rover):
        if options.atmosphere==STATIC_ATMOSPHERE and any(row['metadata']['events'].get(flag,0) for flag in ('2','3','4')):
            raise ValueError('Static atmosphere estimation cannot ignore moving/new-site/header-change events. Split and review the physical occupation/reference station first.')
        if row['metadata']['time_system']!='GPS' or row['utc_offset_seconds'] is None:
            raise ValueError('This engine profile accepts explicitly documented GPS observation time only, including mixed-system RINEX.')
        for system in options.systems:
            codes=row['metadata']['observation_types'].get(system,row['metadata']['observation_types'].get('*',[]) if system=='G' else [])
            needed=tuple(pair[0] for pair in AZIMUTH_SIGNALS[system][:options.frequencies]) if azimuth else SYSTEMS[system][2][:options.frequencies]
            if any(not any(c.startswith(('C'+freq,'P'+freq)) for c in codes) or not any(c.startswith('L'+freq) for c in codes) for freq in needed):
                raise ValueError(SYSTEMS[system][0]+' needs code and carrier phase in each reviewed frequency slot at both base and rover. E5a and B1C are not substituted for E5b and B1I.')
    if not preflight:return base,rover,nav,None
    report=r.inspect_catalogue(project,cancelled)
    if report['blockers']: raise ValueError('Raw preflight requires review: '+'; '.join(report['blockers']))
    glo=_navigation_check(nav,options.systems,azimuth)
    if 'R' in options.systems:
        observed={s for row in (base,rover) for s in row['metadata']['satellites'] if s.startswith('R')}
        if not observed or observed-set(nav['metadata']['satellites']):
            raise ValueError('Every observed GLONASS satellite needs its own navigation/FDMA channel record; no default channel or wavelength is accepted.')
        times={sat:sorted(records) for sat,records in glo.items()}
        for row in (base,rover):
            for sample in row['metadata']['samples']:
                utc=sample[0]-row['utc_offset_seconds']*r.TICK
                for sat in (s for s in sample[1].split() if s.startswith('R')):
                    available=times.get(sat,[]);index=bisect_left(available,utc)
                    nearest=available[max(0,index-1):index+1]
                    if not nearest or min(abs(t-utc) for t in nearest)>1800*r.TICK:
                        raise ValueError('GLONASS '+sat+' lacks navigation/FDMA channel coverage within 30 minutes at an observed epoch. No default wavelength is accepted.')
    calibration_review(options,base,rover)
    return base,rover,nav,report


def configuration(options,base,rover):
    synthetic=options.atmosphere=='ideal-synthetic'
    values={
        'pos1-posmode':options.mode,'pos1-frequency':'l1' if options.frequencies==1 else 'l1+l2',
        'pos1-soltype':'forward','pos1-elmask':options.elevation,'pos1-dynamics':'off','pos1-tidecorr':0,
        'pos1-ionoopt':'off' if synthetic else 'brdc','pos1-tropopt':'off' if synthetic else 'saas',
        'pos1-sateph':'brdc','pos1-navsys':sum(SYSTEMS[s][1] for s in options.systems),'pos2-armode':options.ambiguity_mode,'pos2-gloarmode':'off',
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
    if options.engine_variant=='survey-sa2':
        plan=selected_signals(options,base,rover)
        signal_options=' '.join('-'+system+'L'+code for system,codes in plan.items() for code in codes)
        values.update({'ant1-sa-azimuth':options.rover_azimuth_deg,'ant2-sa-azimuth':options.base_azimuth_deg,
            'misc-rnxopt1':signal_options,'misc-rnxopt2':signal_options})
    # Explicitly disable every model not supplied in this profile; no engine defaults.
    values.update({'pos1-posopt1':'off','pos1-posopt2':'on' if options.calibration=='antex' else 'off',
        'ant1-anttype':'SA_ROVER        NONE' if options.calibration=='antex' else '',
        'ant2-anttype':'SA_BASE         NONE' if options.calibration=='antex' else '',
        'file-rcvantfile':'receiver.atx' if options.calibration=='antex' else '', 'file-satantfile':''})
    if options.atmosphere==STATIC_ATMOSPHERE:
        # Exact pinned-engine state model and explicit stochastic assumptions.
        # These are model settings, never derived survey accuracy or sigmas.
        values.update({'pos1-ionoopt':'est-stec','pos1-tropopt':'est-ztd',
            'stats-stdiono':.03,'stats-stdtrop':.3,'stats-prniono':.001,'stats-prntrop':.0001})
    return ''.join(f'{key}={value}\n' for key,value in values.items())


def prepare(folder,project,options,executable,cancelled=lambda:False):
    executable=program(executable); base,rover,nav,preflight=validate(project,options,cancelled)
    expected={'upstream':ENGINE_SHA256,'survey-sa1':SYSTEM_ENGINE_SHA256,'survey-sa2':AZIMUTH_ENGINE_SHA256}[options.engine_variant]
    if engine.sha256(executable)!=expected:raise ValueError('Selected executable differs from the chosen engine profile.')
    if cancelled():raise ValueError('Job preparation cancelled.')
    folder=Path(folder).resolve(); folder.mkdir(parents=True,exist_ok=False)
    context=dict(profile=processing_profile(options),job_id=str(uuid4()),project_id=project['project_id'],
        occupation_sha256=b.content_fingerprint([base['fingerprint'],rover['fingerprint']]),
        engine_sha256=expected,configuration_sha256='',inputs=[],base=base['point'],rover=rover['point'],
        session=base['session'],frame=options.frame,epoch=options.epoch,mode=options.mode,
        base_xyz=list(options.base_xyz),antenna_note=options.antenna_note,model_note=options.model_note,job_directory=str(folder))
    context.update(systems=list(options.systems),frequencies=options.frequencies,calibration=calibration_review(options,base,rover))
    if context['profile']==STATIC_PROFILE:context['atmosphere_model']=STATIC_ATMOSPHERE
    if context['profile'] in CLOCK_PROFILES:context.update(atmosphere_model=options.atmosphere,solution_time_model=CLOCK_MODEL)
    if context['profile'] in (ITRF_FLOAT_PROFILE,NETWORK_FLOAT_PROFILE):context['ambiguity_mode']='off'
    if context['profile']==NETWORK_FLOAT_PROFILE:
        from . import network_vrs
        product=network_vrs.snapshot_product(options.reference_network_path,folder/'reference_network',options.network_kernel_path,cancelled)
        context['reference_network']=dict(folder='reference_network',manifest_sha256=engine.sha256(folder/'reference_network/vrs.json'),kernel_sha256=network_vrs.KERNEL_SHA256,virtual_sha256=product['report']['virtual_sha256'])
    if options.engine_variant=='survey-sa2':context.update(signal_plan=selected_signals(options,base,rover),antenna_azimuth_deg={'base':options.base_azimuth_deg,'rover':options.rover_azimuth_deg},orientation_mode=options.orientation_mode)
    if context['calibration']:
        model=context['calibration']
        payload=antenna.subset([('SA_BASE         NONE',model['base']),('SA_ROVER        NONE',model['rover'])])
        # Fail rather than round unusual precision or silently change a model.
        (folder/'receiver.atx').write_bytes(payload)
        reread=antenna.read(folder/'receiver.atx')
        for index,key in enumerate(('base','rover')):
            if reread['entries'][index]['frequencies']!=model[key]['frequencies']:
                raise ValueError('ANTEX correction precision exceeds the reviewed engine subset; no rounding is accepted.')
        model['engine_sha256']=reread['sha256']
        original=Path(options.antex_path).read_bytes()
        if hashlib.sha256(original).hexdigest()!=model['original_sha256']:raise ValueError('Original ANTEX changed during preparation.')
        (folder/'original.atx').write_bytes(original)
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
    if context['profile']==NETWORK_FLOAT_PROFILE and engine.sha256(Path(folder)/'reference_network/vrs.json')!=context['reference_network']['manifest_sha256']:
        raise ValueError('Recorded reference-network product changed.')
    if engine.sha256(Path(folder)/'processing.conf')!=context['configuration_sha256']: raise ValueError('Processing configuration changed.')
    for row in context['inputs']:
        if engine.sha256(Path(folder)/row['name'])!=row['sha256']: raise ValueError('Job input changed: '+row['name'])
    if context.get('calibration'):
        for name,key in (('receiver.atx','engine_sha256'),('original.atx','original_sha256')):
            if engine.sha256(Path(folder)/name)!=context['calibration'][key]:raise ValueError('Job antenna calibration changed: '+name)
    if context['profile'] in MODERN_PROFILES:
        try: options=ProcessingOptions(**result['options'])
        except (TypeError,ValueError):raise ValueError('Invalid recorded processing options.') from None
        if processing_profile(options)!=context['profile']:raise ValueError('Recorded engine profile differs from options.')
        if context['profile'] in CLOCK_PROFILES and context['atmosphere_model']!=options.atmosphere:raise ValueError('Recorded ITRF2020 atmosphere model differs from options.')
        azimuth=context['profile']==AZIMUTH_PROFILE
        project=n.load_project(Path(folder)/'network.sanet.json')
        base,rover,_,_=validate(project,options,preflight=False)
        expected=dict(project_id=project['project_id'],base=base['point'],rover=rover['point'],session=base['session'],
            mode=options.mode,frame=options.frame,epoch=options.epoch,base_xyz=list(options.base_xyz),
            antenna_note=options.antenna_note,model_note=options.model_note)
        if any(context[key]!=value for key,value in expected.items()):raise ValueError('Processing provenance differs from the submitted network/options.')
        if azimuth and (context['orientation_mode']!=options.orientation_mode or context['signal_plan']!=selected_signals(options,base,rover) or context['antenna_azimuth_deg']!={'base':options.base_azimuth_deg,'rover':options.rover_azimuth_deg}):
            raise ValueError('Recorded signals/orientation differ from the submitted options.')
        if (list(options.systems),options.frequencies)!=(context['systems'],context['frequencies']):raise ValueError('Satellite selection differs from the processing audit.')
        if configuration(options,base,rover).encode('ascii')!=(Path(folder)/'processing.conf').read_bytes():raise ValueError('Recorded options differ from the submitted configuration.')
        if bool(context['calibration'])!=(options.calibration=='antex'):raise ValueError('Antenna calibration audit differs from options.')
        if context['calibration']:
            model=context['calibration'];catalogue=antenna.read(Path(folder)/'original.atx')
            for key,row in (('base',base),('rover',rover)):
                if antenna.select(catalogue,row['metadata'],options.frequencies,options.systems,azimuth)!=model[key]:raise ValueError('Recorded antenna selection differs from the original calibration.')
            subset=antenna.read(Path(folder)/'receiver.atx')['entries']
            if len(subset)!=2 or model['evidence']!=options.calibration_note:raise ValueError('Engine antenna subset differs from the recorded selection.')
            for entry,key,alias in zip(subset,('base','rover'),('SA_BASE         NONE','SA_ROVER        NONE')):
                if (entry['type']!=alias or entry['serial'] or entry['dazi']!=model[key]['dazi'] or entry['zenith']!=[0.,90.,5.]
                        or entry['start'] is not None or entry['end'] is not None
                        or entry['count']!=len(antenna.required(options.systems,options.frequencies,azimuth)) or entry['frequencies']!=model[key]['frequencies']):
                    raise ValueError('Engine antenna subset differs from the recorded selection.')
    return result


def validate_context(context):
    if not isinstance(context,dict):raise ValueError('Invalid processing provenance.')
    modern=context.get('profile') in MODERN_PROFILES
    azimuth=context.get('profile')==AZIMUTH_PROFILE
    estimated=context.get('profile')==STATIC_PROFILE
    clocked=context.get('profile') in CLOCK_PROFILES
    float_only=context.get('profile') in (ITRF_FLOAT_PROFILE,NETWORK_FLOAT_PROFILE)
    network_float=context.get('profile')==NETWORK_FLOAT_PROFILE
    n._keys(context,CONTEXT_KEYS|({'systems','frequencies','calibration'} if modern else set())|({'signal_plan','antenna_azimuth_deg','orientation_mode'} if azimuth else set())|({'atmosphere_model'} if estimated or clocked else set())|({'solution_time_model'} if clocked else set())|({'ambiguity_mode'} if float_only else set())|({'reference_network'} if network_float else set()),'Raw processing provenance')
    expected={PROFILE:ENGINE_SHA256,MULTI_PROFILE:ENGINE_SHA256,SYSTEM_PROFILE:SYSTEM_ENGINE_SHA256,AZIMUTH_PROFILE:AZIMUTH_ENGINE_SHA256,STATIC_PROFILE:ENGINE_SHA256,ITRF_PROFILE:ENGINE_SHA256,ITRF_FLOAT_PROFILE:ENGINE_SHA256,NETWORK_FLOAT_PROFILE:ENGINE_SHA256}
    if context['profile'] not in expected or context['engine_sha256']!=expected[context['profile']] or context['mode'] not in ('static','kinematic'):
        raise ValueError('Unsupported processing profile or engine identity.')
    if modern:
        systems=context['systems']
        if not isinstance(systems,list) or not systems or 'G' not in systems or len(set(systems))!=len(systems) or any(s not in SYSTEMS for s in systems):raise ValueError('Invalid recorded satellite systems.')
        if type(context['frequencies']) is not int or context['frequencies'] not in (1,2):raise ValueError('Invalid recorded frequencies.')
        if estimated and (context['atmosphere_model']!=STATIC_ATMOSPHERE or context['mode']!='static' or systems!=['G'] or context['frequencies']!=2):raise ValueError('Unsupported recorded static atmosphere model.')
        if clocked and (context['solution_time_model']!=CLOCK_MODEL or context['atmosphere_model'] not in ('broadcast/saas','ideal-synthetic',STATIC_ATMOSPHERE) or context['frame']!='EPSG:9988' or context['mode']!='static' or systems!=['G'] or context['frequencies']!=2):raise ValueError('Unsupported recorded ITRF2020 clock model.')
        if float_only and context['ambiguity_mode']!='off':raise ValueError('Recorded FLOAT-only profile must disable integer ambiguity resolution.')
        if network_float:
            from . import network_vrs
            item=context['reference_network'];n._keys(item,{'folder','manifest_sha256','kernel_sha256','virtual_sha256'},'Network VRS provenance')
            if item['folder']!='reference_network' or item['kernel_sha256']!=network_vrs.KERNEL_SHA256 or not re.fullmatch(r'[a-f0-9]{64}',item['manifest_sha256']) or not re.fullmatch(r'[a-f0-9]{64}',item['virtual_sha256']):raise ValueError('Invalid network VRS snapshot identity.')
        if context['frame']=='EPSG:9988' and not clocked:raise ValueError('ITRF2020 requires its explicit clock-audited processing profile.')
        if 'R' in systems and context['profile'] not in (SYSTEM_PROFILE,AZIMUTH_PROFILE):raise ValueError('GLONASS is not a legacy processing profile.')
        if azimuth:
            if context['orientation_mode']!='fixed-level':raise ValueError('Unsupported recorded antenna orientation model.')
            plan=context['signal_plan'];heading=context['antenna_azimuth_deg']
            n._keys(plan,set(systems),'Recorded signal plan');n._keys(heading,{'base','rover'},'Recorded antenna heading')
            for value in heading.values():
                n._number(value,'Antenna azimuth')
                if not 0<=value<360:raise ValueError('Invalid recorded antenna azimuth.')
            for system in systems:
                if not isinstance(plan[system],list) or len(plan[system])!=context['frequencies']:raise ValueError('Invalid recorded signal plan.')
                for code,(band,types) in zip(plan[system],AZIMUTH_SIGNALS[system]):
                    if not isinstance(code,str) or len(code)!=2 or code[0]!=band or code[1] not in types:raise ValueError('Unsupported recorded signal/band.')
        model=context['calibration']
        if model is not None:
            n._keys(model,{'original_sha256','evidence','base','rover','engine_file','engine_sha256'},'Receiver calibration')
            if (systems!=['G'] and context['profile'] not in (SYSTEM_PROFILE,AZIMUTH_PROFILE)) or model['engine_file']!='receiver.atx':raise ValueError('Unsupported receiver calibration profile.')
            for key in ('original_sha256','engine_sha256'):
                if not re.fullmatch('[0-9a-f]{64}',model[key]):raise ValueError('Invalid ANTEX hash.')
            n._text(model['evidence'],'Calibration evidence',required=True)
            for key in ('base','rover'):
                m=model[key]
                n._keys(m,{'type','serial','method','dazi','zenith','start','end','count','frequencies','selection'}|({'comments','sinex_code'} if azimuth else set()),'Selected receiver model')
                if m['selection'] not in ('generic type/radome','serial-specific') or (not azimuth and m['dazi']!=0) or m['zenith']!=[0.,90.,5.]:raise ValueError('Unsupported recorded antenna model.')
                if set(m['frequencies'])!=set(antenna.required(systems,context['frequencies'],azimuth)):raise ValueError('Invalid recorded antenna frequencies.')
                if azimuth:
                    antenna.validate_grid(m)
                    if not isinstance(m['comments'],list) or len(m['comments'])>500:raise ValueError('Invalid recorded calibration comments.')
                    for comment in m['comments']:n._text(comment,'Calibration provider comment')
                    n._text(m['sinex_code'],'Calibration SINEX code')
                for values in m['frequencies'].values():
                    n._keys(values,{'neu_mm','noazi_mm'}|({'azi_mm'} if azimuth and m['dazi'] else set()),'Antenna frequency')
                    for name,count,limit in (('neu_mm',3,10000),('noazi_mm',19,1000)):
                        if not isinstance(values[name],list) or len(values[name])!=count or any(type(v) not in (int,float) or not math.isfinite(v) or abs(v)>limit for v in values[name]):raise ValueError('Invalid recorded antenna corrections.')
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
    expected_header='% program   : RTKLIB ver.EX '+{SYSTEM_PROFILE:'2.5.1-SA1',AZIMUTH_PROFILE:'2.5.1-SA2'}.get(context['profile'],'2.5.1')
    if expected_header not in text.splitlines(): raise ValueError('Unexpected output engine/version header.')
    project=n.load_project(folder/'network.sanet.json'); sources={row['id']:row for row in project['raw_sources']}
    base,rover=[sources[row['source_id']] for row in context['inputs'][:2]]
    low=max(row['metadata']['first_tick'] for row in (base,rover)); high=min(row['metadata']['last_tick'] for row in (base,rover))
    if context['profile'] in (ITRF_FLOAT_PROFILE,NETWORK_FLOAT_PROFILE) and any(row['quality']=='1' or row['eligible'] for row in source['rows']):
        raise ValueError('FLOAT-only engine contract cannot contain FIX or baseline-eligible epochs.')
    if context['profile']==NETWORK_FLOAT_PROFILE:
        from . import network_vrs
        product=network_vrs.load_product(folder/'reference_network',manifest['options']['network_kernel_path'])
        if product['report']['virtual_sha256']!=context['reference_network']['virtual_sha256'] or engine.sha256(folder/'base.rnx')!=product['report']['virtual_sha256'] or math.dist(product['report']['target_xyz'],context['base_xyz'])>.0002:
            raise ValueError('Static VRS processing differs from its original network derivation.')
    if context['profile'] in CLOCK_PROFILES:
        stat=folder/'solution.pos.stat'
        if not stat.is_file() or stat.stat().st_size>32*1024*1024:raise ValueError('Missing or oversized engine receiver-clock evidence.')
        source['clock_epoch_audit']=clock_epoch_audit(stat.read_text(encoding='ascii'),source['rows'],rover['metadata']['samples'])
        if any(not low<=entry['raw_tick']<=high for entry in source['clock_epoch_audit']['rows']):raise ValueError('Clock-reconstructed source epoch falls outside the raw overlap.')
    else:
        for row in source['rows']:
            stamp=datetime.fromisoformat(row['time']); tick=r.epoch([str(stamp.year),str(stamp.month),str(stamp.day),str(stamp.hour),str(stamp.minute),f'{stamp.second+stamp.microsecond/1e6:.6f}'])
            if not low<=tick<=high: raise ValueError('Solution epoch falls outside the selected raw overlap.')
    source['processing']=deepcopy(context)
    source['marked_synthetic']=project['synthetic']
    return source


def verify_azimuth_audit(console,manifest):
    """Check ALL parsed grid values and a real correction against independent math."""
    context=manifest['context'];project=n.load_project(Path(context['job_directory'])/'network.sanet.json')
    sources={row['id']:row for row in project['raw_sources']}
    records={tag:{} for tag in ('SA_ORIENT','SA_AZI','SA_APPLY')}
    for line in console.splitlines():
        values=line.split(',');tag=values[0]
        if tag not in records:continue
        try:
            alias,system,slot=values[1],values[2],int(values[3])
            key=(alias,system,slot,int(values[4])) if tag=='SA_AZI' else (alias,system,slot)
            if tag=='SA_APPLY':data=[values[4],*[float(v) for v in values[5:]]]
            else:data=[float(v) for v in values[5:] if v!=''] if tag=='SA_AZI' else [float(v) for v in values[4:]]
        except (ValueError,IndexError):raise ValueError('Malformed engine azimuth application audit.') from None
        if key in records[tag]:raise ValueError('Duplicate engine azimuth audit record.')
        records[tag][key]=data
    def compare(actual,expected,label,tolerance=1e-9):
        if len(actual)!=len(expected) or any(not math.isfinite(a) or abs(a-b)>tolerance for a,b in zip(actual,expected)):
            raise ValueError('Engine did not confirm '+label+'.')
    for receiver,alias,index in (('base','SA_BASE         NONE',0),('rover','SA_ROVER        NONE',1)):
        model=context['calibration'][receiver];heading=context['antenna_azimuth_deg'][receiver]
        raw=sources[context['inputs'][index]['source_id']];h,e,north=raw['metadata']['antenna_hen']
        for system in context['systems']:
            for slot,code in enumerate(antenna.AZIMUTH_FREQUENCIES[system][:context['frequencies']]):
                key=(alias,system,slot)
                compare(records['SA_ORIENT'].get(key,[]),[heading,model['dazi']],'antenna orientation/grid')
                for row_index,row in enumerate(model['frequencies'][code].get('azi_mm',[])):
                    compare(records['SA_AZI'].get((*key,row_index),[]),[v/1000 for v in row[1:]],'every azimuth grid sample')
                applied=records['SA_APPLY'].get(key,[])
                if len(applied)!=4 or applied[0]!=context['signal_plan'][system][slot]:raise ValueError('Engine used a different signal/band.')
                _,az,zen,actual=applied
                if not all(math.isfinite(v) for v in (az,zen,actual)) or not 0<=az<=360 or not 0<=zen<=90:raise ValueError('Invalid engine correction direction.')
                ar,zr=math.radians(az),math.radians(zen)
                correction=antenna.correction(model,code,az,zen,heading)-(e*math.sin(ar)*math.sin(zr)+north*math.cos(ar)*math.sin(zr)+h*math.cos(zr))
                compare([actual],[correction],'independently calculated applied antenna correction',2e-8)


def static_atmosphere_audit(text,source_rows):
    """Exact-epoch engine-state evidence, not independent atmosphere/accuracy."""
    if len(text)>32*1024*1024:raise ValueError('Oversized atmosphere-state audit.')
    expected={r._utc_tick(datetime.fromisoformat(row['time']).replace(tzinfo=timezone.utc)):row['quality'] for row in source_rows}
    epoch0=r.epoch(['1980','1','6','0','0','0']);iono={};trop={};used={};rows=[];seen=set()
    for line in text.splitlines():
        values=line.split(',');kind=values[0]
        if kind not in ('$ION','$TROP','$SAT'):continue
        try:
            week=int(values[1]);tow=Decimal(values[2]);ticks=tow*r.TICK
            if not 0<=week<=10000 or not tow.is_finite() or not 0<=tow<604800 or ticks!=ticks.to_integral_value():raise ValueError()
            tick=epoch0+week*604800*r.TICK+int(ticks)
            if tick not in expected:raise ValueError()
            if kind=='$SAT':
                if len(values)<10:raise ValueError()
                satellite,slot=values[3],int(values[4])
                if values[9]=='1' and re.fullmatch(r'G(?:0[1-9]|[12][0-9]|3[0-2])',satellite):used.setdefault(tick,{}).setdefault(satellite,set()).add(slot)
                continue
            if len(values)!=(9 if kind=='$ION' else 7) or values[3]!=expected[tick]:raise ValueError()
            identifier=values[4]
            if (kind=='$ION' and not re.fullmatch(r'G(?:0[1-9]|[12][0-9]|3[0-2])',identifier)) or (kind=='$TROP' and identifier not in ('1','2')):raise ValueError()
            floating,fixed=map(float,values[-2:])
            if not all(math.isfinite(v) for v in (floating,fixed)):raise ValueError()
            key=(kind,tick,identifier)
            if key in seen:raise ValueError()
            seen.add(key)
        except (ValueError,IndexError,InvalidOperation,OverflowError):raise ValueError('Malformed, duplicate or unmatched engine atmosphere-state record.') from None
        if kind=='$ION':iono.setdefault(tick,set()).add(identifier)
        else:trop.setdefault(tick,set()).add(identifier)
        rows.append(dict(time=r.tick_text(tick),kind='GPS ionosphere state' if kind=='$ION' else 'Zenith wet-delay state',
            identifier=identifier,float_m=floating,fixed_m=fixed,quality=values[3]))
    matched=[];satellites=set()
    for tick in expected:
        common={sat for sat,slots in used.get(tick,{}).items() if {1,2}<=slots}&iono.get(tick,set())
        if trop.get(tick)!=set(('1','2')) or not common:raise ValueError('Missing engine ionosphere/wet-delay states or matching two-frequency GPS residuals at a solution epoch.')
        matched.append(tick);satellites.update(common)
    if not matched:raise ValueError('No solution epochs with checked atmosphere-state evidence.')
    wet=[row['float_m'] for row in rows if row['kind']=='Zenith wet-delay state']
    return dict(profile='static-gps-atmosphere-state-audit-v1',epochs=len(matched),ionosphere_records=sum(len(v) for v in iono.values()),
        wet_delay_records=sum(len(v) for v in trop.values()),gps_satellites=sorted(satellites),float_wet_delay_min_m=min(wet),float_wet_delay_max_m=max(wet),
        note='Engine conditional states only. Receiver 1 is rover, 2 is base; wet delay is not total zenith delay or survey uncertainty. Original full status/residual file retained.',rows=rows)


def clock_epoch_audit(text,source_rows,raw_samples):
    """Pinned pntpos.c: t_solution=t_observation-dtr[0]; $CLK gives dtr in ns.

    Match one original recorded epoch using that explicit correction and only
    the documented 0.001 s POS/stat output rounding (+0.0005 ns CLK rounding).
    This is not an arbitrary nearest-epoch tolerance or a correction of RINEX.
    """
    if len(text)>32*1024*1024:raise ValueError('Oversized receiver-clock audit.')
    tick=lambda value:r.epoch(datetime.fromisoformat(value).strftime('%Y %m %d %H %M %S.%f').split())
    expected={tick(row['time']):row['quality'] for row in source_rows};clocks={};origin=r.epoch(['1980','1','6','0','0','0'])
    for line in text.splitlines():
        if not line.startswith('$CLK,'):continue
        values=line.split(',')
        try:
            week=int(values[1]);tow=Decimal(values[2]);bias=Decimal(values[5])
            if len(values)!=11 or not 0<=week<=10000 or not tow.is_finite() or not 0<=tow<604800 or not bias.is_finite() or abs(bias)>100000000 or values[4]!='1':raise ValueError()
            instant=origin+week*604800*r.TICK+tow*r.TICK
            if instant!=instant.to_integral_value() or instant not in expected or values[3]!=expected[instant] or instant in clocks:raise ValueError()
            clocks[int(instant)]=bias
        except (ValueError,IndexError,InvalidOperation,OverflowError):raise ValueError('Malformed, duplicate or unmatched GPS receiver-clock record.') from None
    if set(clocks)!=set(expected):raise ValueError('Missing GPS receiver-clock record at a solution epoch.')
    raw=sorted(sample[0] for sample in raw_samples);used=set();rows=[];tolerance=Decimal('5000.00001')
    for instant,bias in clocks.items():
        reconstructed=Decimal(instant)+bias*r.TICK/Decimal(1000000000)
        index=bisect_left(raw,reconstructed-tolerance)
        if index>=len(raw) or abs(Decimal(raw[index])-reconstructed)>tolerance or (index+1<len(raw) and abs(Decimal(raw[index+1])-reconstructed)<=tolerance) or raw[index] in used:
            raise ValueError('Receiver-clock model does not uniquely reproduce a recorded source epoch within output quantization.')
        used.add(raw[index]);rows.append(dict(time_gpst=r.tick_text(instant),raw_time_gpst=r.tick_text(raw[index]),raw_tick=raw[index],gps_clock_bias_ns=str(bias),
            rounding_residual_s=float((Decimal(raw[index])-reconstructed)/r.TICK),quality=expected[instant]))
    if not rows:raise ValueError('No clock-audited solution epochs.')
    return dict(model=CLOCK_MODEL,epochs=len(rows),max_abs_clock_bias_s=float(max(abs(value) for value in clocks.values())/1000000000),
        note='Original RINEX epochs retained. Engine SPP receiver clock explains solution timestamps; millisecond output quantization only. Not independent physical time synchronization.',rows=rows)


def _clock_csv(audit):
    stream=io.StringIO(newline='');writer=csv.writer(stream,lineterminator='\n')
    writer.writerow(['Solution GPST','Recorded observation GPST','Engine GPS clock bias ns','Output rounding residual s','Q'])
    for row in audit['rows']:writer.writerow([row[key] for key in ('time_gpst','raw_time_gpst','gps_clock_bias_ns','rounding_residual_s','quality')])
    return stream.getvalue().encode('utf-8')


def _atmosphere_csv(audit):
    stream=io.StringIO(newline='');writer=csv.writer(stream,lineterminator='\n')
    writer.writerow(['Time GPST','Engine state (not independent measurement)','Receiver 1=rover 2=base / satellite','Float state m','Fixed state m','Solution Q'])
    for row in audit['rows']:writer.writerow([row[k] for k in ('time','kind','identifier','float_m','fixed_m','quality')])
    return stream.getvalue().encode('utf-8')


def inspect_result(folder,exit_code,executable):
    folder=Path(folder); manifest=load_manifest(folder)
    if manifest['state']!='PREPARED' or exit_code!=0: raise ValueError('Raw processing failed or this run is no longer pending. Partial results are not accepted.')
    program(executable)
    if engine.sha256(executable)!=manifest['context']['engine_sha256']:raise ValueError('Engine changed during processing.')
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
        engine_version={SYSTEM_PROFILE:SYSTEM_ENGINE_VERSION,AZIMUTH_PROFILE:AZIMUTH_ENGINE_VERSION}.get(manifest['context']['profile'],ENGINE_VERSION),
        eligible_fix=sum(row['eligible'] for row in source['rows']),warnings=warnings(manifest['options']) if manifest['context']['profile'] in MODERN_PROFILES else WARNINGS,
        first_time=source['rows'][0]['time'],last_time=source['rows'][-1]['time'],time_system='GPST')
    if manifest['context']['profile'] in MODERN_PROFILES:
        # Engine residual validity flag proves a constellation contributed; input
        # availability or a navsys mask alone is not evidence of use.
        used={s:set() for s in manifest['context']['systems']}
        stat=folder/'solution.pos.stat'
        if stat.is_file():
            if stat.stat().st_size>32*1024*1024:raise ValueError('Oversized residual audit.')
            for line in stat.read_text(encoding='ascii').splitlines():
                values=line.split(',')
                if len(values)>10 and values[0]=='$SAT' and values[3][:1] in used and values[9]=='1':used[values[3][0]].add(values[3])
        if any(not ids for ids in used.values()):raise ValueError('A selected satellite system contributed no valid residuals. Review navigation, signals, time overlap and engine log; no silent exclusion is accepted.')
        result.update(systems=manifest['context']['systems'],used_satellites={s:sorted(ids) for s,ids in used.items()},calibration=manifest['context']['calibration'])
        azimuth=manifest['context']['profile']==AZIMUTH_PROFILE
        if azimuth:
            result.update(signal_plan=manifest['context']['signal_plan'],antenna_azimuth_deg=manifest['context']['antenna_azimuth_deg'])
            used_slots={(line.split(',')[3][0],int(line.split(',')[4])) for line in stat.read_text(encoding='ascii').splitlines() if line.startswith('$SAT,') and line.split(',')[9]=='1'}
            if any((s,f+1) not in used_slots for s in used for f in range(manifest['context']['frequencies'])):raise ValueError('A selected system/frequency contributed no valid residuals.')
        if manifest['context']['profile'] in (SYSTEM_PROFILE,AZIMUTH_PROFILE) and manifest['context']['calibration']:
            console=(folder/'console.txt').read_text(encoding='utf-8',errors='replace')
            audit={}
            for match in re.finditer(r'SA_PCV,([^,\r\n]+),([GREC]),([01]),([^\r\n]+)',console):
                try: values=[float(v) for v in match[4].split(',')]
                except ValueError:raise ValueError('Invalid engine receiver-calibration audit.') from None
                audit[(match[1],match[2],int(match[3]))]=values
            for receiver,alias in (('base','SA_BASE         NONE'),('rover','SA_ROVER        NONE')):
                for system in manifest['context']['systems']:
                    bands=antenna.AZIMUTH_FREQUENCIES if azimuth else antenna.SYSTEM_FREQUENCIES
                    for slot,code in enumerate(bands[system][:manifest['context']['frequencies']]):
                        model=manifest['context']['calibration'][receiver]['frequencies'][code]
                        expected=[model['neu_mm'][1]/1000,model['neu_mm'][0]/1000,model['neu_mm'][2]/1000,model['noazi_mm'][0]/1000,model['noazi_mm'][-1]/1000]
                        actual=audit.get((alias,system,slot),[])
                        if len(actual)!=5 or any(not math.isfinite(a) or abs(a-b)>1e-9 for a,b in zip(actual,expected)):
                            raise ValueError('Engine did not confirm the selected '+receiver+' '+code+' receiver calibration. No fallback is accepted.')
            result['receiver_calibration_audit']='Every selected receiver/system/frequency confirmed by the engine; original NOAZI values and input hashes retained.'
            if azimuth:
                verify_azimuth_audit(console,manifest)
                result['receiver_calibration_audit']='Every selected azimuth grid sample, orientation, actual signal and independently recalculated applied correction confirmed; original provider notes and hashes retained.'
    if manifest['options'].get('atmosphere')==STATIC_ATMOSPHERE:
        stat=folder/'solution.pos.stat'
        if not stat.is_file() or stat.stat().st_size>32*1024*1024:raise ValueError('Missing or oversized atmosphere-state evidence.')
        audit=static_atmosphere_audit(stat.read_text(encoding='ascii'),source['rows'])
        (folder/'atmosphere_states.csv').write_bytes(_atmosphere_csv(audit))
        result['atmosphere_audit']={key:value for key,value in audit.items() if key!='rows'}
        distances=[math.dist(row['values'],manifest['context']['base_xyz']) for row in source['rows']]
        result['computed_baseline_length_m']=dict(min=min(distances),max=max(distances),last=distances[-1])
        result['atmosphere_model']=STATIC_ATMOSPHERE
    if manifest['context']['profile'] in CLOCK_PROFILES:
        audit=source['clock_epoch_audit'];(folder/'clock_epoch_audit.csv').write_bytes(_clock_csv(audit))
        result['clock_epoch_audit']={key:value for key,value in audit.items() if key!='rows'}
    with (folder/'epochs.csv').open('w',encoding='utf-8',newline='') as handle:
        from .coordinate_job import llh
        geographic=b.FRAMES[manifest['context']['frame']]
        latitude,height=('Latitude ITRF2020 / GRS80 deg','h ITRF2020 / GRS80 m') if geographic=='EPSG:9989' else ('Latitude WGS84 ellipsoid deg','h WGS84 ellipsoid m')
        # Preserve historical export columns/models for legacy frames.
        export_frame=geographic if geographic=='EPSG:9989' else 'EPSG:4979'
        writer=csv.writer(handle);writer.writerow(['Time GPST','X ECEF m','Y ECEF m','Z ECEF m',latitude,'Longitude deg',height,'Q','Baseline eligible','Reason'])
        for row in source['rows']: writer.writerow([row['time'],*row['values'],*llh(row['values'],export_frame),row['quality'],row['eligible'],row['reason']])
    (folder/'report.txt').write_text(result['engine_version']+' — '+result['mode']+'\n'+json.dumps(result,indent=2)+'\n\n'+
        'Original engine solution and covariance retained in solution.pos. No averaged coordinate or automatic baseline import.\n'+
        'Input originals, copied inputs, configuration and engine SHA256 recorded in processing.json. Hashes establish integrity, not authenticity.\n',encoding='utf-8')
    manifest['state']='COMPLETED';manifest['outputs']={name:engine.sha256(folder/name) for name in ('solution.pos','solution.pos.stat','epochs.csv','report.txt','atmosphere_states.csv','clock_epoch_audit.csv') if (folder/name).exists()}
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
        allowed=('solution.pos','solution.pos.stat','epochs.csv','report.txt')+(('atmosphere_states.csv',) if manifest['options'].get('atmosphere')==STATIC_ATMOSPHERE else ())
        if manifest['context']['profile'] in CLOCK_PROFILES:allowed+=('clock_epoch_audit.csv',)
        if name not in allowed or engine.sha256(path.parent/name)!=digest:
            raise ValueError('Processing result package changed.')
    source=_decode(path.parent,manifest)
    if manifest['context']['profile'] in CLOCK_PROFILES:
        if not {'solution.pos.stat','clock_epoch_audit.csv'}<=set(manifest['outputs']) or (path.parent/'clock_epoch_audit.csv').read_bytes()!=_clock_csv(source['clock_epoch_audit']):raise ValueError('Receiver-clock table differs from recalculated engine/source evidence.')
    if manifest['options'].get('atmosphere')==STATIC_ATMOSPHERE:
        if not {'solution.pos.stat','atmosphere_states.csv'}<=set(manifest['outputs']):raise ValueError('Missing recorded atmosphere-state outputs.')
        stat=path.parent/'solution.pos.stat'
        if stat.stat().st_size>32*1024*1024:raise ValueError('Oversized atmosphere-state audit.')
        audit=static_atmosphere_audit(stat.read_text(encoding='ascii'),source['rows'])
        if (path.parent/'atmosphere_states.csv').read_bytes()!=_atmosphere_csv(audit):raise ValueError('Atmosphere-state table differs from recalculated engine evidence.')
    return source


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
