# SPDX-License-Identifier: GPL-3.0-only
"""Local Emlid ETC review and explicit antenna-position to pole-tip conversion.

This is a coordinate supplement, not raw IMU fusion or dynamic carrier processing.
Never infer full receiver attitude, covariance, date, height convention or endpoint.
"""
from bisect import bisect_left
from dataclasses import dataclass,asdict
from datetime import datetime,timedelta,timezone
from pathlib import Path
import csv,hashlib,io,json,math,re
from . import coordinate_job as c,network as n,rinex as r,baselines as b

SPEC='https://files.emlid.com/docs/ETC_rev.3_RS4.pdf'
LIMITATIONS=[
    'Coordinate-only ETC supplement, not raw PPK/static orientation, IMU fusion or dynamic ANTEX/carrier-phase correction.',
    'Heading is preserved for review, not assumed to be antenna NRP, Euler yaw or a full attitude matrix.',
    'Exact UTC matching only; no interpolation, extrapolation, nearest-epoch correction or inferred date.',
    'Only matched GGA FIX, ETC state 30 and notification 0 produce accepted coordinates. All other epochs remain in the audit.',
    'ETC axis coefficients times antenna height are vendor tilt estimates in mm, not full covariance, angular sigmas or total accuracy.',
    'No covariance is synthesized or propagated; no independent network observations or baselines are created.',
    'Frame/height/endpoint are user declarations. No datum, epoch, geoid or automatic 95 mm correction is applied.',
    'Generated examples test software only, not Reach RS4 Pro field accuracy or full device support.',
]


@dataclass
class OrientationOptions:
    endpoint: str=''
    height_mode: str=''
    frame: str=''
    antenna_height_m: float|None=None
    evidence: str=''
    endpoint_reviewed: bool=False
    height_reviewed: bool=False
    time_reviewed: bool=False


def sentence(payload):
    checksum=0
    for char in payload:checksum^=ord(char)
    return '$'+payload+'*'+f'{checksum:02X}'


def _number(value):
    if not re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)',value):raise ValueError('Invalid NMEA decimal.')
    result=float(value)
    if not math.isfinite(result):raise ValueError('Nonfinite NMEA value.')
    return result


def _tod(value):
    if not re.fullmatch(r'\d{6}(?:\.\d{1,6})?',value):raise ValueError('UTC time needs hhmmss[.ffffff]; leap seconds are outside this profile.')
    hour,minute,second=int(value[:2]),int(value[2:4]),_number(value[4:])
    if hour>23 or minute>59 or second>=60:raise ValueError('Invalid UTC clock time or unsupported leap second.')
    return (hour*3600+minute*60)*1000000+round(second*1000000)


def _angle(value,maximum=360):
    result=_number(value)
    if not 0<=result<=maximum:raise ValueError('NMEA angle outside its documented range.')
    return result


def _coordinate(value,hemisphere,latitude):
    digits=2 if latitude else 3
    if not re.fullmatch(r'\d{'+str(digits+2)+r'}(?:\.\d+)?',value):raise ValueError('Invalid NMEA geographic coordinate.')
    deg=int(value[:digits]);minutes=_number(value[digits:]);limit=90 if latitude else 180
    if minutes>=60 or deg>limit or (deg==limit and minutes):raise ValueError('NMEA coordinate outside its range.')
    if hemisphere not in (('N','S') if latitude else ('E','W')):raise ValueError('Invalid NMEA hemisphere.')
    return (deg+minutes/60)*(-1 if hemisphere in 'SW' else 1)


def read(path,cancelled=lambda:False):
    path=Path(path).expanduser().resolve()
    if path.is_symlink() or not path.is_file() or path.stat().st_size>8*1024*1024:raise ValueError('Choose a local NMEA file of at most 8 MiB.')
    payload=path.read_bytes()
    if len(payload)>8*1024*1024:raise ValueError('NMEA file grew beyond 8 MiB.')
    try:lines=payload.decode('ascii').splitlines()
    except UnicodeDecodeError:raise ValueError('NMEA input must be ASCII text, without binary data or a BOM.') from None
    if len(lines)>100000:raise ValueError('NMEA profile allows at most 100000 lines.')
    entries=[];anchors=[];counts={};untimed=[]
    for index,line in enumerate(lines,1):
        if cancelled():raise ValueError('Orientation review cancelled.')
        if not line:continue
        if len(line)>512 or any(not 32<=ord(ch)<=126 for ch in line) or not re.fullmatch(r'\$[A-Z]{2}[A-Z0-9]{3},[^*\r\n]*\*[0-9A-Fa-f]{2}',line):raise ValueError('Malformed NMEA sentence on line '+str(index)+'.')
        if sentence(line[1:-3])[-2:]!=line[-2:].upper():raise ValueError('NMEA checksum failed on line '+str(index)+'.')
        fields=line[1:-3].split(',');tag=fields[0];counts[tag]=counts.get(tag,0)+1
        if tag not in ('GNETC','GPETC','GNGGA','GPGGA','GNZDA','GPZDA','GNRMC','GPRMC','GNGST','GPGST'):continue
        kind=tag[2:];date=None
        try:
            if kind=='ZDA':
                if len(fields)!=7:raise ValueError('ZDA field count.')
                date=datetime(int(fields[4]),int(fields[3]),int(fields[2]),tzinfo=timezone.utc)
                if not 2000<=date.year<=2079:raise ValueError('ZDA year outside 2000-2079 profile.')
            elif kind=='RMC':
                if len(fields)<10 or not re.fullmatch(r'\d{6}',fields[9]):raise ValueError('RMC needs a date.')
                dd,mm,yy=int(fields[9][:2]),int(fields[9][2:4]),int(fields[9][4:])
                if yy>=80:raise ValueError('RMC century ambiguous; use full-year ZDA.')
                date=datetime(2000+yy,mm,dd,tzinfo=timezone.utc)
            if kind in ('ZDA','RMC'):
                tod=_tod(fields[1]);anchors.append((index,date+timedelta(microseconds=tod),tod));continue
            if kind=='ETC':
                if len(fields)!=10:raise ValueError('ETC revision 1 needs exactly nine fields.')
                state=int(fields[2]) if fields[2] else None;notification=int(fields[3]) if fields[3] else None
                if state not in (None,0,10,20,30) or notification not in (None,0,10,20,30):raise ValueError('Unknown ETC state/notification.')
                values=[_number(v) if v else None for v in fields[4:]]
                if state==30:
                    if any(v is None for v in values) or notification is None:raise ValueError('Compensating ETC record is incomplete.')
                    _angle(fields[4]);_angle(fields[5]);_angle(fields[6],180)
                    if any(v<0 for v in values[3:]):raise ValueError('Negative ETC accuracy coefficient.')
                elif any(v is not None for v in values):raise ValueError('Orientation values outside compensating state.')
                row=dict(line=index,state=state,notification=notification,heading=values[0],direction=values[1],tilt=values[2],coeff_neu=values[3:])
                if not fields[1]:untimed.append(dict(**row,utc=None));continue
            elif kind=='GGA':
                if len(fields)!=15:raise ValueError('GGA field count.')
                quality=int(fields[6]);lat=lon=alt=separation=None
                if not 0<=quality<=8:raise ValueError('Unknown GGA quality.')
                if fields[2] or fields[4]:lat=_coordinate(fields[2],fields[3],True);lon=_coordinate(fields[4],fields[5],False)
                if fields[9]:
                    alt=_number(fields[9])
                    if fields[10]!='M' or not -15000<=alt<=100000:raise ValueError('Invalid GGA altitude/units.')
                if fields[11]:
                    separation=_number(fields[11])
                    if fields[12]!='M' or abs(separation)>200:raise ValueError('Invalid GGA separation/units.')
                if quality and (lat is None or alt is None):raise ValueError('Valid GGA solution lacks coordinates.')
                row=dict(line=index,quality=quality,lat=lat,lon=lon,altitude=alt,separation=separation)
            else:
                if len(fields)!=9:raise ValueError('GST field count.')
                values=[_number(v) if v else None for v in fields[2:]]
                if any(v is not None and v<0 for v in values):raise ValueError('Invalid GST value.')
                row=dict(line=index,values=values)
            entries.append((kind,index,_tod(fields[1]),row))
        except (ValueError,IndexError,OverflowError) as error:raise ValueError('NMEA line '+str(index)+': '+str(error)) from None
    if not counts.get('GNETC',0)+counts.get('GPETC',0):raise ValueError('No Emlid ETC messages. RINEX/ordinary CSV cannot reconstruct changing orientation.')
    if not anchors:raise ValueError('UTC dates need ZDA or RMC in the same log. File names and computer clock are not date evidence.')
    if any(after[1]<before[1] for before,after in zip(anchors,anchors[1:])):raise ValueError('NMEA date/time anchors run backwards or conflict. Split/reset streams before review.')
    anchor_lines=[v[0] for v in anchors];records={kind:{} for kind in ('ETC','GGA','GST')}
    for kind,index,tod,row in entries:
        position=bisect_left(anchor_lines,index);dates=set()
        for _,anchor,atod in anchors[max(0,position-1):position+1]:
            delta=(tod-atod+43200000000)%86400000000-43200000000
            if abs(delta)<=2000000:dates.add(anchor+timedelta(microseconds=delta))
        if len(dates)!=1:raise ValueError('UTC date is missing/conflicting within 2 seconds of NMEA line '+str(index)+'. Include continuous ZDA/RMC; no guessed day rollover.')
        utc=dates.pop().isoformat(timespec='microseconds').replace('+00:00','Z')
        if utc in records[kind]:raise ValueError('Duplicate '+kind+' epoch '+utc+'. Split repeated/mixed streams first.')
        row['utc']=utc;records[kind][utc]=row
    for kind,rows in records.items():
        sequence=list(rows)
        if sequence!=sorted(sequence):raise ValueError(kind+' UTC epochs are out of order.')
    rows=[dict(utc=utc,etc=records['ETC'].get(utc),gga=records['GGA'].get(utc),gst=records['GST'].get(utc)) for utc in sorted(set(records['ETC'])|set(records['GGA']))]
    rows.extend(dict(utc=None,etc=row,gga=None,gst=None) for row in untimed)
    if not rows:raise ValueError('No reviewable orientation/position records.')
    return dict(path=str(path),sha256=hashlib.sha256(payload).hexdigest(),counts=counts,rows=rows)


def delta_enu(height,direction,tilt):
    """Pole-tip displacement from the documented uncompensated antenna endpoint."""
    direction,tilt=math.radians(direction),math.radians(tilt)
    return [-height*math.sin(tilt)*math.sin(direction),-height*math.sin(tilt)*math.cos(direction),-height*math.cos(tilt)]


def compensate(lat,lon,height,delta,frame):
    shift=c.matvec(c.enu_rotation(lat,lon),delta)
    values=[a+b for a,b in zip(c.xyz(lat,lon,height,frame),shift)]
    return c.llh(values,frame),values


def _epoch_reasons(source):
    etc,gga=source['etc'],source['gga'];reason=[]
    if not source['utc']:reason.append('No UTC time')
    if not etc:reason.append('No exact ETC epoch')
    elif etc['state']!=30:reason.append('ETC not compensating')
    elif etc['notification']!=0:reason.append('ETC notification '+str(etc['notification']))
    if not gga:reason.append('No exact GGA epoch')
    elif gga['quality']!=4:reason.append('GGA is not RTK FIX')
    return reason


def rinex_coverage(catalogue,project,rover_id,cancelled=lambda:False,raw_path=None):
    """Exact recorded-epoch screening, not physical synchronization/attitude proof."""
    n.validate_project(project)
    saved=next((s for s in project.get('raw_sources',[]) if s['id']==rover_id),None)
    if not saved or saved['role']!='ROVER' or saved['metadata']['file_type']!='OBS':raise ValueError('Select an existing reviewed rover RINEX link; do not import it again.')
    entry,_=r.inspect_source(project,saved,cancelled,raw_path)
    if entry['status']!='CHECKED':raise ValueError('Rover source review: '+'; '.join(entry['issues']))
    meta=saved['metadata']
    if meta['receiver_clock_offset_epochs']:raise ValueError('RINEX receiver-clock-offset epochs require a separately reviewed time model; this check must not ignore clock corrections.')
    by_tick={}
    for row in catalogue['rows']:
        if cancelled():raise ValueError('Orientation/RINEX time review cancelled.')
        if row['utc']:
            time=datetime.fromisoformat(row['utc'].replace('Z','+00:00'))
            by_tick[r._utc_tick(time)]=row
    etc_ticks=sorted(t for t,row in by_tick.items() if row['etc'])
    offset=saved['utc_offset_seconds']*r.TICK;rows=[];raw_ticks=set()
    for tick,_,_ in meta['samples']:
        if cancelled():raise ValueError('Orientation/RINEX time review cancelled.')
        utc=tick-offset;raw_ticks.add(utc);source=by_tick.get(utc)
        exact=bool(source and source['etc']);reasons=_epoch_reasons(source) if source else ['No exact ETC/GGA epoch']
        position=bisect_left(etc_ticks,utc);near=etc_ticks[max(0,position-1):position+1]
        delta=min(near,key=lambda t:(abs(t-utc),t))-utc if near else None
        rows.append(dict(raw_tick=tick,raw_time=r.tick_text(tick),utc=r.tick_text(utc)+'Z',exact_etc=exact,
            usable=not reasons,etc_line=source['etc']['line'] if exact else None,
            nearest_etc_delta_ticks=delta,reason='; '.join(reasons)))
    usable=sum(row['usable'] for row in rows);matched=sum(row['exact_etc'] for row in rows)
    return dict(profile='rinex-etc-exact-time-v1',status='EXACT_USABLE_EPOCH_COVERAGE' if usable==len(rows) else 'INCOMPLETE_EPOCH_COVERAGE',
        project_sha256=b.content_fingerprint(project),rover_id=rover_id,point=saved['point'],session=saved['session'],
        raw_source_sha256=saved['source_sha256'],raw_fingerprint=saved['fingerprint'],time_system=meta['time_system'],
        source_minus_utc_seconds=saved['utc_offset_seconds'],time_evidence=saved['time_evidence'],
        nmea_sha256=catalogue['sha256'],raw_epochs=len(rows),exact_etc_epochs=matched,usable_epochs=usable,
        missing_etc_epochs=len(rows)-matched,unusable_etc_epochs=matched-usable,
        extra_nmea_epochs=sum(t not in raw_ticks for t in by_tick),rows=rows,warnings=entry['warnings']+[
            'Nearest ETC delta is diagnostic only; it never supplies a missing epoch or a guessed time shift.',
            'Matching recorded timestamps does not establish physical timing/latency, antenna-axis alignment, full attitude or dynamic carrier-phase corrections.',
            'This check does not compare positions, approve raw processing, replace base/navigation preflight or create independent observations.'])


def review(catalogue,options,project=None,rover_id='',cancelled=lambda:False,raw_path=None):
    if not isinstance(options,OrientationOptions):raise ValueError('Invalid orientation options.')
    if options.endpoint not in ('uncompensated-antenna','already-ground'):raise ValueError('Declare uncompensated antenna endpoint or already-compensated ground point. Unknown endpoints cannot be corrected.')
    if options.height_mode not in ('ellipsoid-altitude','msl-plus-separation'):raise ValueError('Declare the GGA height convention; standard MSL and vendor ellipsoidal altitude must not be confused.')
    if options.frame not in c.FRAMES:raise ValueError('Declare the geographic frame/ellipsoid; no transformation is applied.')
    if not all(v is True for v in (options.endpoint_reviewed,options.height_reviewed,options.time_reviewed)):raise ValueError('Review endpoint, height/frame and UTC synchronization evidence.')
    n._text(options.evidence,'Endpoint, frame/height, timing and pole/installation evidence',required=True)
    if options.endpoint=='uncompensated-antenna':
        n._number(options.antenna_height_m,'Pole tip to documented position endpoint distance')
        if not .1<=options.antenna_height_m<=10:raise ValueError('Pole-axis endpoint distance must be 0.1 to 10 metres; no automatic 95 mm addition.')
    elif options.antenna_height_m is not None:raise ValueError('Clear pole height for already-compensated ground coordinates; no second correction is allowed.')
    rows=[]
    for source in catalogue['rows']:
        if cancelled():raise ValueError('Orientation review cancelled.')
        etc,gga=source['etc'],source['gga'];reason=_epoch_reasons(source);original=ground=values=None;delta=None;estimate=None
        if gga:
            if gga['lat'] is not None and gga['altitude'] is not None:
                if options.height_mode=='msl-plus-separation' and gga['separation'] is None:reason.append('Missing GGA geoid separation')
                else:original=[gga['lat'],gga['lon'],gga['altitude']+(gga['separation'] if options.height_mode=='msl-plus-separation' else 0)]
            else:reason.append('Missing position')
        if not reason:
            if options.endpoint=='already-ground':ground=list(original);values=c.xyz(*ground,options.frame);delta=[0.,0.,0.]
            else:
                delta=delta_enu(options.antenna_height_m,etc['direction'],etc['tilt'])
                ground,values=compensate(*original,delta,options.frame)
                estimate=[v*options.antenna_height_m for v in etc['coeff_neu']]
        rows.append(dict(utc=source['utc'],etc=etc,gga=gga,gst=source['gst'],accepted=not reason,reason='; '.join(reason),
            original_llh=original,ground_llh=ground,ground_xyz=values,delta_enu_m=delta,tilt_estimate_neu_mm=estimate))
    result=dict(profile='emlid-etc-coordinate-v1',source_sha256=catalogue['sha256'],options=asdict(options),
        accepted=sum(row['accepted'] for row in rows),rows=rows,limitations=list(LIMITATIONS),specification=SPEC)
    if rover_id:
        result['profile']='emlid-etc-coordinate-v2-linked-time'
        result['rinex_coverage']=rinex_coverage(catalogue,project,rover_id,cancelled,raw_path)
    return result


def _csv(result,accepted_only=False):
    stream=io.StringIO(newline='');writer=csv.writer(stream,lineterminator='\n')
    writer.writerow(['Name','Timestamp','Latitude','Longitude','Ellipsoidal height','Solution status','Accepted','Reason',
        'Heading deg','Tilt direction deg','Tilt deg','Delta E m','Delta N m','Delta U m','Original latitude','Original longitude','Original ellipsoidal height',
        'Tilt N estimate mm (NOT covariance)','Tilt E estimate mm (NOT covariance)','Tilt U estimate mm (NOT covariance)'])
    for index,row in enumerate(result['rows'],1):
        if accepted_only and not row['accepted']:continue
        etc=row['etc'] or {};ground=row['ground_llh'] or ['']*3;original=row['original_llh'] or ['']*3
        writer.writerow([f'ETC_{index:06d}',row['utc'] or '',*ground,'FIX' if row['accepted'] else 'EXCLUDED',row['accepted'],row['reason'],
            *[etc.get(k,'') for k in ('heading','direction','tilt')],*(row['delta_enu_m'] or ['']*3),*original,*(row['tilt_estimate_neu_mm'] or ['']*3)])
    return stream.getvalue().encode('utf-8')


def _coverage_csv(coverage):
    stream=io.StringIO(newline='');writer=csv.writer(stream,lineterminator='\n')
    writer.writerow(['Recorded RINEX time','Declared time system','UTC from declared offset','Exact ETC','Usable status match','ETC source line','Nearest ETC delta ms (diagnostic only)','Reason'])
    for row in coverage['rows']:
        delta=row['nearest_etc_delta_ticks']
        writer.writerow([row['raw_time'],coverage['time_system'],row['utc'],row['exact_etc'],row['usable'],row['etc_line'],
            delta/(r.TICK/1000) if delta is not None else '',row['reason']])
    return stream.getvalue().encode('utf-8')


def export_snapshot(folder,catalogue,options,project=None,rover_id=''):
    # Re-read originals before export; stale UI results never override a changed log.
    fresh=read(catalogue['path'])
    if fresh!=catalogue:raise ValueError('NMEA source changed since review. Review it again.')
    result=review(fresh,options,project,rover_id)
    if not result['accepted']:raise ValueError('No accepted coordinate epochs to export.')
    payload=Path(fresh['path']).read_bytes()
    if hashlib.sha256(payload).hexdigest()!=fresh['sha256']:raise ValueError('NMEA source changed while copying.')
    folder=Path(folder).resolve();folder.mkdir(parents=True,exist_ok=False)
    (folder/'original.nmea').write_bytes(payload)
    (folder/'review.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    (folder/'epochs.csv').write_bytes(_csv(result))
    (folder/'ground_coordinates.csv').write_bytes(_csv(result,True))
    text='ETC orientation / pole-tip review\n\n'+str(result['accepted'])+' accepted epochs of '+str(len(result['rows']))+'.\n'+options.evidence+'\n\n'+'\n'.join(result['limitations'])+'\n\n'+SPEC+'\n'
    (folder/'REPORT.txt').write_text(text,encoding='utf-8')
    names=['original.nmea','review.json','epochs.csv','ground_coordinates.csv','REPORT.txt']
    if rover_id:
        saved=next(s for s in project['raw_sources'] if s['id']==rover_id)
        with Path(saved['path']).open('rb') as handle:raw=handle.read(r.MAX_BYTES+1)
        if len(raw)>r.MAX_BYTES or hashlib.sha256(raw).hexdigest()!=saved['source_sha256']:raise ValueError('Rover source changed while copying the linked review.')
        (folder/'raw_rover.rnx').write_bytes(raw)
        (folder/'network.sanet.json').write_text(n.dumps_project(project),encoding='utf-8')
        (folder/'rinex_coverage.csv').write_bytes(_coverage_csv(result['rinex_coverage']))
        names+=['raw_rover.rnx','network.sanet.json','rinex_coverage.csv']
        coverage=result['rinex_coverage']
        with (folder/'REPORT.txt').open('a',encoding='utf-8') as handle:
            handle.write('\nRINEX time coverage: '+coverage['status']+'; '+str(coverage['usable_epochs'])+'/'+str(coverage['raw_epochs'])+' usable exact status matches.\n'+'\n'.join(coverage['warnings'])+'\n')
    hashes={name:hashlib.sha256((folder/name).read_bytes()).hexdigest() for name in names}
    (folder/'checksums.json').write_text(json.dumps(hashes,indent=2)+'\n',encoding='utf-8')
    verify_snapshot(folder)
    return folder


def verify_snapshot(folder):
    folder=Path(folder)
    hashes=json.loads((folder/'checksums.json').read_text(encoding='utf-8'))
    basic={'original.nmea','review.json','epochs.csv','ground_coordinates.csv','REPORT.txt'}
    linked=basic|{'raw_rover.rnx','network.sanet.json','rinex_coverage.csv'}
    if set(hashes) not in (basic,linked):raise ValueError('Unexpected orientation package members.')
    for name,digest in hashes.items():
        if hashlib.sha256((folder/name).read_bytes()).hexdigest()!=digest:raise ValueError('Orientation package changed: '+name)
    result=json.loads((folder/'review.json').read_text(encoding='utf-8'))
    has_link=set(hashes)==linked
    if has_link!=('rinex_coverage' in result):raise ValueError('Recorded RINEX coverage differs from package members.')
    project=n.load_project(folder/'network.sanet.json') if has_link else None
    rover_id=result['rinex_coverage']['rover_id'] if has_link else ''
    expected=review(read(folder/'original.nmea'),OrientationOptions(**result['options']),project,rover_id,raw_path=folder/'raw_rover.rnx' if has_link else None)
    if result!=expected:raise ValueError('Recorded orientation review differs from original data/options.')
    if (folder/'epochs.csv').read_bytes()!=_csv(expected) or (folder/'ground_coordinates.csv').read_bytes()!=_csv(expected,True):raise ValueError('Orientation coordinate export differs from recalculated values.')
    if has_link and (folder/'rinex_coverage.csv').read_bytes()!=_coverage_csv(expected['rinex_coverage']):raise ValueError('RINEX time coverage CSV differs from the original recorded epochs.')
    return expected
