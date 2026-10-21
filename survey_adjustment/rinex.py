# SPDX-License-Identifier: GPL-3.0-only
"""Bounded, read-only RINEX catalogue/preflight. No positioning or baseline synthesis.

Independent implementation from IGS RINEX 2.11 and 3.05 record specifications.
Original bytes remain local. Source timestamps retain their declared time system.
"""
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from collections import Counter
from uuid import uuid4
import gzip, hashlib, io, math, re
from . import network as n
from .baselines import content_fingerprint, ensure_unique_import

TICK=10_000_000
MAX_BYTES=32*1024*1024
MAX_EPOCHS=50000
VERSIONS=('2.11','3.02','3.03','3.04','3.05')
SYSTEMS='GRECJSI'
TIMES=('GPS','GLO','GAL','QZS','BDT','IRN','UTC')
ORIGIN=datetime(1970,1,1)


def tick_text(tick):
    seconds,fraction=divmod(tick,TICK)
    return (ORIGIN+timedelta(seconds=seconds)).isoformat(timespec='seconds')+f'.{fraction:07d}'


def epoch(values, two_digit=False):
    try:
        y,mo,day,h,minute=map(int,values[:5]); seconds=Decimal(values[5])
        if two_digit: y+=1900 if y>=80 else 2000
        if not seconds.is_finite() or not 0<=seconds<60 or seconds*TICK!=(seconds*TICK).to_integral_value():
            raise ValueError()
        delta=datetime(y,mo,day,h,minute)-ORIGIN
        return (delta.days*86400+delta.seconds)*TICK+int(seconds*TICK)
    except (ValueError,IndexError,InvalidOperation,OverflowError):
        raise ValueError('Invalid/unsupported RINEX epoch (up to seven fractional digits; leap-second epochs require a separately reviewed profile).') from None


def real(text):
    try: value=float(text.replace('D','E').replace('d','e'))
    except (ValueError,OverflowError): raise ValueError('Invalid RINEX numeric field.') from None
    if not math.isfinite(value): raise ValueError('Non-finite RINEX numeric field.')
    return value


def read_source(path, cancelled=lambda:False):
    if cancelled(): raise ValueError('RINEX inspection cancelled; nothing imported.')
    path=Path(path)
    if path.is_symlink() or not path.is_file(): raise ValueError('Choose an existing, unlinked local RINEX file.')
    with path.open('rb') as handle: payload=handle.read(MAX_BYTES+1)
    if len(payload)>MAX_BYTES: raise ValueError('RINEX profile limit: 32 MiB per file before/after gzip decompression.')
    if cancelled(): raise ValueError('RINEX inspection cancelled; nothing imported.')
    result=parse_source(payload,path.name,cancelled)
    result['path']=str(path.resolve())
    return result


def parse_source(payload,name,cancelled=lambda:False):
    if not isinstance(payload,bytes) or len(payload)>MAX_BYTES: raise ValueError('RINEX file exceeds the 32 MiB profile limit.')
    original=payload
    if payload.startswith(b'\x1f\x8b'):
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(payload)) as stream: payload=stream.read(MAX_BYTES+1)
        except (OSError,EOFError): raise ValueError('Invalid gzip RINEX source.') from None
    if len(payload)>MAX_BYTES: raise ValueError('Decompressed RINEX exceeds the 32 MiB limit.')
    try: text=payload.decode('ascii')
    except UnicodeError: raise ValueError('Expected ASCII RINEX, optionally gzip. Convert compact/Hatanaka RINEX separately; no converter is run.') from None
    if any(ord(ch)<32 and ch not in '\r\n\t' for ch in text): raise ValueError('Binary/control content is not a supported RINEX source.')
    lines=text.splitlines()
    if not lines or lines[0][60:80].strip()!='RINEX VERSION / TYPE':
        raise ValueError('Not a supported RINEX header. Compact/Hatanaka, ZIP and receiver binaries are not decoded.')
    version=lines[0][:9].strip(); kind=lines[0][20:21]; system=lines[0][40:41].strip()
    if version not in VERSIONS: raise ValueError('Supported RINEX versions: 2.11 and 3.02-3.05. Other versions are retained outside this profile.')
    if kind not in ('O','N','G') or (kind=='G' and version!='2.11'): raise ValueError('Only observation and documented navigation catalogue files are supported.')
    header=[]; values={}; types={}; counts={}; current=None
    for index,line in enumerate(lines[:5000]):
        if len(line)>80 and line[80:].strip(): raise ValueError('Oversized RINEX header record.')
        label=line[60:80].strip(); value=line[:60]; header.append(line)
        if label=='END OF HEADER': break
        if label=='CRINEX VERS   / TYPE': raise ValueError('Compact RINEX needs an external reviewed conversion first.')
        if label=='SYS / # / OBS TYPES':
            key=value[:1].strip() or current
            if not key or key not in SYSTEMS: raise ValueError('Invalid observation-system header.')
            if value[3:6].strip():
                if key in counts: raise ValueError('Repeated observation-type definition.')
                counts[key]=int(value[3:6]); types[key]=[]
            if key not in types: raise ValueError('Observation type continuation has no first record.')
            types[key].extend(value[7:60].split()); current=key
        elif label=='# / TYPES OF OBSERV':
            if value[:6].strip():
                if '*' in counts: raise ValueError('Repeated observation-type definition.')
                counts['*']=int(value[:6]); types['*']=[]
            if '*' not in types: raise ValueError('Observation type continuation has no first record.')
            types['*'].extend(value[6:60].split())
        elif label not in ('COMMENT',''):
            if label in values and label in ('MARKER NAME','MARKER NUMBER','APPROX POSITION XYZ','ANTENNA: DELTA H/E/N','ANT # / TYPE','REC # / TYPE / VERS','INTERVAL','TIME OF FIRST OBS','TIME OF LAST OBS'):
                raise ValueError('Ambiguous repeated RINEX header: '+label)
            values.setdefault(label,value)
    else: raise ValueError('Missing END OF HEADER within the header-size limit.')
    if not header or header[-1][60:80].strip()!='END OF HEADER': raise ValueError('Missing END OF HEADER.')
    if any(not 1<=count<=96 or len(types[key])!=count or len(set(types[key]))!=count for key,count in counts.items()):
        raise ValueError('Observation-type count/continuation differs from the header.')
    pattern=r'[A-Z][1-9]' if version=='2.11' else r'[A-Z][1-9][A-Z]'
    if any(not re.fullmatch(pattern,code) for codes in types.values() for code in codes): raise ValueError('Invalid observation type code.')
    if kind=='O' and (not types or (version=='2.11')!=('*' in types)): raise ValueError('Missing or incompatible observation-type header.')
    def triplet(label):
        if label not in values: return None
        if len(values[label].split())!=3: raise ValueError('Expected three header values: '+label)
        return [real(v) for v in values[label].split()]
    first=values.get('TIME OF FIRST OBS',''); last=values.get('TIME OF LAST OBS','')
    time_system=first[48:51].strip()
    if time_system and time_system not in TIMES: raise ValueError('Unsupported RINEX time system.')
    metadata=dict(version=version,file_type='OBS' if kind=='O' else 'NAV',system=system,
        marker=values.get('MARKER NAME','').strip(),marker_number=values.get('MARKER NUMBER','').strip(),
        antenna=values.get('ANT # / TYPE','')[20:40].strip(),antenna_serial=values.get('ANT # / TYPE','')[:20].strip(),
        receiver=values.get('REC # / TYPE / VERS','')[20:40].strip(),
        approximate_xyz=triplet('APPROX POSITION XYZ'),antenna_hen=triplet('ANTENNA: DELTA H/E/N'),
        time_system=time_system,declared_first=epoch(first[:43].split()) if first.strip() else None,
        declared_last=epoch(last[:43].split()) if last.strip() else None,
        interval_s=real(values['INTERVAL'].strip()) if 'INTERVAL' in values else None,
        observation_types=types,header=header)
    if metadata['interval_s'] is not None and metadata['interval_s']<=0: raise ValueError('INTERVAL must be positive.')
    body=lines[index+1:]
    metadata.update(_observations(body,metadata,cancelled) if kind=='O' else _navigation(body,version,kind,cancelled))
    return dict(source_name=Path(name).name,source_sha256=hashlib.sha256(original).hexdigest(),
        fingerprint=content_fingerprint([version,kind,system,types,time_system,metadata['approximate_xyz'],
            metadata['antenna_hen'],[line.rstrip() for line in body]]),
        size_bytes=len(original),expanded_bytes=len(payload),metadata=metadata,
        marked_synthetic=any('SYNTHETIC' in line.upper() for line in header))


def _satellite(value,default='G'):
    value=value.strip()
    if value.isdigit(): value=default+value.zfill(2)
    if not re.fullmatch('['+SYSTEMS+'][0-9]{2}',value): raise ValueError('Invalid satellite identifier.')
    return value


def _fields(record,codes,system):
    if len(record)>16*len(codes) and record[16*len(codes):].strip(): raise ValueError('Extra RINEX observation fields.')
    record=record.ljust(16*len(codes)); available=set(); loss=half=boc=0; nonmissing=[]
    for i,code in enumerate(codes):
        cell=record[i*16:(i+1)*16]; number=real(cell[:14]) if cell[:14].strip() else 0.
        if cell[14:15] not in ' 01234567' or cell[15:16] not in ' 0123456789': raise ValueError('Invalid LLI/SSI digit.')
        if number!=0:
            available.add(code); nonmissing.append(system+':'+code)
        if code.startswith('L'):
            lli=int(cell[14]) if cell[14].strip() else 0
            loss+=bool(lli&1); half+=bool(lli&2); boc+=bool(lli&4)
    frequencies={code[1:] for code in available if code.startswith('L') and ('C'+code[1:] in available or (len(code)==2 and 'P'+code[1:] in available))}
    return frequencies,nonmissing,loss,half,boc


def _observations(lines,meta,cancelled):
    v2=meta['version']=='2.11'; samples=[]; signal_counts=Counter(); satellites=set(); events=Counter()
    loss=half=boc=power=clock_epochs=0; i=0; previous=None
    while i<len(lines):
        if cancelled(): raise ValueError('RINEX inspection cancelled; nothing imported.')
        line=lines[i]; i+=1
        if not line.strip(): continue
        try:
            if v2:
                flag=int(line[28:29]); count=int(line[29:32]); parts=line[:26].split()
            else:
                if not line.startswith('>'): raise ValueError('Expected a RINEX 3 epoch record; folded satellite records are not supported.')
                parts=line[1:29].split(); flag=int(line[31:32]); count=int(line[32:35])
        except (ValueError,IndexError): raise ValueError('Invalid RINEX epoch/satellite count.') from None
        if flag not in range(7) or not 0<=count<=999: raise ValueError('Unsupported RINEX epoch flag or count.')
        if flag in (2,3,4,5):
            if i+count>len(lines): raise ValueError('Truncated event records.')
            if flag==4: raise ValueError('Mid-file header changes require splitting/review outside the 0.9 profile.')
            events[str(flag)]+=1; i+=count; continue
        time=epoch(parts,v2)
        clock=line[68:80] if v2 else line[35:]
        if clock.strip() and real(clock.strip())!=0: clock_epochs+=1
        if flag!=6:
            if previous is not None and time<=previous: raise ValueError('Duplicate or non-monotonic observation epoch.')
            previous=time
        power+=flag==1; names=[]
        if v2:
            names=[line[j:j+3] for j in range(32,min(68,32+count*3),3)]
            while len(names)<count:
                if i>=len(lines) or lines[i][:32].strip(): raise ValueError('Missing satellite-list continuation.')
                continuation=lines[i]; i+=1
                names.extend(continuation[j:j+3] for j in range(32,min(68,32+(count-len(names))*3),3))
        epoch_sats=set(); phase=set()
        for sat_index in range(count):
            if i>=len(lines): raise ValueError('Truncated satellite observations.')
            if v2:
                sat=_satellite(names[sat_index],meta['system'] if meta['system'] in SYSTEMS else 'G'); codes=meta['observation_types']['*']
                chunks=[]
                for j in range((len(codes)+4)//5):
                    if i>=len(lines): raise ValueError('Truncated RINEX 2 observation continuation.')
                    part=lines[i]; i+=1
                    if len(part)>80 and part[80:].strip(): raise ValueError('Oversized RINEX 2 observation record.')
                    needed=min(5,len(codes)-j*5)
                    if part[needed*16:].strip(): raise ValueError('Extra RINEX 2 observation fields.')
                    chunks.append(part[:80].ljust(80))
                record=''.join(chunks)[:16*len(codes)]
            else:
                part=lines[i]; i+=1; sat=_satellite(part[:3]); codes=meta['observation_types'].get(sat[0])
                if codes is None: raise ValueError('Satellite system has no observation-type definition.')
                record=part[3:]
            if sat in epoch_sats: raise ValueError('Duplicate satellite within an epoch.')
            epoch_sats.add(sat)
            frequencies,signals,a,b,c=_fields(record,codes,sat[0])
            if flag!=6:
                signal_counts.update(signals); loss+=a; half+=b; boc+=c
                phase.update(sat+'/'+frequency for frequency in frequencies)
        if flag==6: events['6']+=1; continue
        satellites.update(epoch_sats)
        samples.append([time,' '.join(sorted(epoch_sats)),' '.join(sorted(phase))])
        if len(samples)>MAX_EPOCHS: raise ValueError('RINEX catalogue limit: 50000 observation epochs per file.')
    if not samples: raise ValueError('No normal observation epochs in RINEX source.')
    deltas=[b[0]-a[0] for a,b in zip(samples,samples[1:])]
    step=meta['interval_s'] if meta['interval_s'] is not None else (min(deltas)/TICK if deltas else None)
    gaps=[[a[0],b[0],(b[0]-a[0])/TICK] for a,b in zip(samples,samples[1:]) if step and (b[0]-a[0])/TICK>1.5*step]
    return dict(samples=samples,first_tick=samples[0][0],last_tick=samples[-1][0],epochs=len(samples),
        satellites=sorted(satellites),signal_counts=dict(sorted(signal_counts.items())),lli_loss_of_lock=loss,
        lli_half_cycle=half,lli_boc_tracking=boc,power_failure_epochs=power,receiver_clock_offset_epochs=clock_epochs,events=dict(events),gaps=gaps,
        gap_step_s=step,gap_basis='Header INTERVAL' if meta['interval_s'] is not None else 'Minimum observed step; not a declared sampling interval')


def _navigation(lines,version,kind,cancelled):
    """Frame common broadcast records for catalogue only, never assess orbit validity."""
    i=0; epochs=[]; satellites=set()
    while i<len(lines):
        if cancelled(): raise ValueError('RINEX inspection cancelled; nothing imported.')
        line=lines[i]; i+=1
        if not line.strip(): continue
        v2=version=='2.11'
        try:
            if v2:
                sat=_satellite(line[:2],'R' if kind=='G' else 'G'); time=epoch(line[2:22].split(),True); prefix=22; continuation=3
            else:
                sat=_satellite(line[:3]); time=epoch(line[3:23].split()); prefix=23; continuation=4
            length=(5 if version=='3.05' else 4) if sat[0]=='R' else 4 if sat[0]=='S' else 8
            if len(line)<prefix+57: raise ValueError('Truncated navigation first record.')
            for j in range(3): real(line[prefix+j*19:prefix+(j+1)*19])
            for j in range(length-1):
                if i>=len(lines) or lines[i][:continuation].strip(): raise ValueError('Truncated navigation continuation.')
                row=lines[i]; i+=1
                for k in range(4):
                    cell=row[continuation+k*19:continuation+(k+1)*19]
                    if cell.strip(): real(cell)
            epochs.append(time); satellites.add(sat)
            if len(epochs)>MAX_EPOCHS: raise ValueError('Navigation catalogue record limit exceeded.')
        except (ValueError,IndexError): raise ValueError('Unsupported, damaged or truncated broadcast navigation record.') from None
    if not epochs: raise ValueError('Navigation source has no framed broadcast records.')
    return dict(first_tick=min(epochs),last_tick=max(epochs),records=len(epochs),satellites=sorted(satellites),
        navigation_scope='Record framing/numbers only; orbit, health, issue-of-data, fit intervals and observation coverage are NOT validated.')


@dataclass
class ImportReview:
    role: str = 'ROVER'
    point: str = ''
    session: str = ''
    utc_offset_seconds: int | None = None
    time_evidence: str = ''
    antenna_evidence: str = ''
    note: str = ''
    synthetic: bool = False
    confirmed: bool = False


def plan_import(project,source,review):
    n.validate_project(project)
    if not isinstance(review,ImportReview) or review.confirmed is not True: raise ValueError('Review and confirm the raw-file metadata before adding it.')
    if type(review.synthetic) is not bool or review.synthetic!=project['synthetic'] or (source['marked_synthetic'] and not review.synthetic):
        raise ValueError('Keep synthetic raw examples and real projects separate; explicitly select the source classification.')
    obs=source['metadata']['file_type']=='OBS'
    if review.role not in (('BASE','ROVER') if obs else ('NAV',)): raise ValueError('Choose a role compatible with the RINEX file type.')
    for field in ('note','time_evidence')+('antenna_evidence',) if obs else ('note',):
        n._text(getattr(review,field),field,required=True)
    if obs and (review.point not in {p['id'] for p in project['points']} or review.session not in {s['id'] for s in project['sessions']}):
        raise ValueError('Link observations to an existing physical point and acquisition session; header XYZ does not create controls.')
    offset=review.utc_offset_seconds
    if offset is not None and (type(offset) is not int or abs(offset)>86400): raise ValueError('UTC offset must be an explicitly documented integer number of seconds (source minus UTC).')
    result=deepcopy(project); result.setdefault('raw_sources',[]); result['schema_version']=3
    ensure_unique_import(result['raw_sources'],source['source_sha256'],source['fingerprint'])
    row=dict(id=str(uuid4()),path=source['path'],source_name=source['source_name'],
        source_sha256=source['source_sha256'],fingerprint=source['fingerprint'],size_bytes=source['size_bytes'],
        expanded_bytes=source['expanded_bytes'],metadata=deepcopy(source['metadata']),
        role=review.role,point=review.point if obs else '',session=review.session if obs else '',
        utc_offset_seconds=offset if obs else None,time_evidence=review.time_evidence if obs else '',
        antenna_evidence=review.antenna_evidence if obs else '',note=review.note,synthetic=review.synthetic)
    result['raw_sources'].append(row); n.dumps_project(result)
    return result


def validate_catalogue(project):
    rows=project['raw_sources']
    if not isinstance(rows,list) or len(rows)>100: raise ValueError('Raw catalogue: expected at most 100 sources.')
    seen=set(); previous=[]
    fields={'id','path','source_name','source_sha256','fingerprint','size_bytes','expanded_bytes','metadata','role','point','session',
            'utc_offset_seconds','time_evidence','antenna_evidence','note','synthetic'}
    for row in rows:
        n._keys(row,fields,'Raw source')
        for key in ('id','path','source_name','note'): n._text(row[key],key,required=True)
        if row['id'] in seen: raise ValueError('Duplicate raw-source ID.')
        seen.add(row['id'])
        for key in ('source_sha256','fingerprint'):
            if not isinstance(row[key],str) or not re.fullmatch('[0-9a-f]{64}',row[key]): raise ValueError('Invalid raw-source identity.')
        if type(row['synthetic']) is not bool or row['synthetic']!=project['synthetic']: raise ValueError('Raw source/project synthetic classification differs.')
        for key in ('size_bytes','expanded_bytes'):
            if type(row[key]) is not int or not 0<row[key]<=MAX_BYTES: raise ValueError('Invalid raw-source size.')
        meta=row['metadata']
        if not isinstance(meta,dict) or meta.get('version') not in VERSIONS or meta.get('file_type') not in ('OBS','NAV'): raise ValueError('Unsupported raw metadata.')
        obs=meta['file_type']=='OBS'
        base={'version','file_type','system','marker','marker_number','antenna','antenna_serial','receiver',
              'approximate_xyz','antenna_hen','time_system','declared_first','declared_last','interval_s','observation_types','header'}
        extra={'samples','first_tick','last_tick','epochs','satellites','signal_counts','lli_loss_of_lock','lli_half_cycle',
               'lli_boc_tracking','power_failure_epochs','receiver_clock_offset_epochs','events','gaps','gap_step_s','gap_basis'} if obs else {'first_tick','last_tick','records','satellites','navigation_scope'}
        n._keys(meta,base|extra,'Raw metadata')
        for key in ('system','marker','marker_number','antenna','antenna_serial','receiver','time_system'): n._text(meta[key],key)
        if meta['time_system'] not in ('',*TIMES): raise ValueError('Invalid raw time system.')
        for key in ('approximate_xyz','antenna_hen'):
            if meta[key] is not None: n._numbers(meta[key],3,key)
        for key in ('first_tick','last_tick','declared_first','declared_last'):
            if meta[key] is not None:
                if type(meta[key]) is not int: raise ValueError('Invalid raw time tick.')
                tick_text(meta[key])
        if meta['first_tick']>meta['last_tick']: raise ValueError('Invalid raw time extent.')
        if not isinstance(meta['header'],list) or len(meta['header'])>5000: raise ValueError('Invalid raw header.')
        for line in meta['header']: n._text(line,'Original raw header')
        if not isinstance(meta['observation_types'],dict): raise ValueError('Invalid raw observation types.')
        for key,codes in meta['observation_types'].items():
            if key not in ('*',*SYSTEMS) or not isinstance(codes,list) or not 1<=len(codes)<=96 or any(not isinstance(code,str) or not re.fullmatch('[A-Z][1-9][A-Z]?',code) for code in codes):
                raise ValueError('Invalid raw observation-type catalogue.')
        if meta['interval_s'] is not None: n._number(meta['interval_s'],'Raw header interval',positive=True)
        if not isinstance(meta['satellites'],list) or any(_satellite(s)!=s for s in meta['satellites']): raise ValueError('Invalid raw satellite list.')
        if row['role'] not in (('BASE','ROVER') if obs else ('NAV',)): raise ValueError('Invalid raw source role.')
        if obs:
            if row['point'] not in {p['id'] for p in project['points']} or row['session'] not in {s['id'] for s in project['sessions']}:
                raise ValueError('Raw source references a missing point/session.')
            for key in ('time_evidence','antenna_evidence'): n._text(row[key],key,required=True)
            if row['utc_offset_seconds'] is not None and (type(row['utc_offset_seconds']) is not int or abs(row['utc_offset_seconds'])>86400): raise ValueError('Invalid declared UTC offset.')
            if not isinstance(meta.get('samples'),list) or not 1<=len(meta['samples'])<=MAX_EPOCHS: raise ValueError('Invalid raw epoch catalogue.')
            previous_tick=None
            for sample in meta['samples']:
                if not isinstance(sample,list) or len(sample)!=3 or type(sample[0]) is not int or (previous_tick is not None and sample[0]<=previous_tick):
                    raise ValueError('Invalid raw epoch sequence.')
                previous_tick=sample[0]
                for value in sample[1:]: n._text(value,'Raw availability')
                if any(_satellite(s)!=s for s in sample[1].split()): raise ValueError('Invalid availability satellite.')
                if any(not re.fullmatch('['+SYSTEMS+'][0-9]{2}/[1-9][A-Z]?',s) for s in sample[2].split()): raise ValueError('Invalid code/phase availability.')
            if meta['epochs']!=len(meta['samples']) or meta['first_tick']!=meta['samples'][0][0] or meta['last_tick']!=meta['samples'][-1][0]:
                raise ValueError('Raw summary differs from the epoch catalogue.')
            for key in ('epochs','lli_loss_of_lock','lli_half_cycle','lli_boc_tracking','power_failure_epochs','receiver_clock_offset_epochs'):
                if type(meta[key]) is not int or meta[key]<0: raise ValueError('Invalid raw counter.')
            n._text(meta['gap_basis'],'Gap detection basis')
            if meta['gap_step_s'] is not None: n._number(meta['gap_step_s'],'Gap step',positive=True)
            if not isinstance(meta['gaps'],list) or len(meta['gaps'])>MAX_EPOCHS: raise ValueError('Invalid raw gap list.')
            for gap in meta['gaps']:
                if not isinstance(gap,list) or len(gap)!=3 or type(gap[0]) is not int or type(gap[1]) is not int or not meta['first_tick']<=gap[0]<gap[1]<=meta['last_tick']:
                    raise ValueError('Invalid raw gap extent.')
                n._number(gap[2],'Gap duration',positive=True)
            for key in ('signal_counts','events'):
                if not isinstance(meta[key],dict): raise ValueError('Invalid raw availability/event counters.')
                for name,count in meta[key].items():
                    n._text(name,'Availability key',required=True)
                    if type(count) is not int or count<0: raise ValueError('Invalid raw availability count.')
        elif any(row[key] not in ('',None) for key in ('point','session','utc_offset_seconds','time_evidence','antenna_evidence')):
            raise ValueError('Navigation catalogue entries do not bind point/session/time declarations.')
        else:
            if type(meta['records']) is not int or not 1<=meta['records']<=MAX_EPOCHS: raise ValueError('Invalid navigation record count.')
            n._text(meta['navigation_scope'],'Navigation scope',required=True)
        ensure_unique_import(previous,row['source_sha256'],row['fingerprint']); previous.append(row)


def _utc_tick(time):
    t=time.astimezone(timezone.utc).replace(tzinfo=None)-ORIGIN
    return (t.days*86400+t.seconds)*TICK+t.microseconds*10


def inspect_source(project,saved,cancelled=lambda:False,path=None):
    """Shared exact-byte/metadata and review checks on a validated catalogue row.

    A private snapshot may supply its copied path; the original identity and
    every metadata value still have to agree. This never changes the saved link.
    """
    if cancelled(): raise ValueError('RINEX preflight cancelled.')
    try:
        fresh=read_source(saved['path'] if path is None else path,cancelled)
        if any(fresh[key]!=saved[key] for key in ('source_sha256','fingerprint','size_bytes','expanded_bytes','metadata')):
            raise ValueError('Source changed since import; retain the original or remove/review a replacement explicitly.')
        meta=saved['metadata']; obs=meta['file_type']=='OBS';issues=[];notes=[]
        if obs:
            if not meta['time_system']: issues.append('Source time system is undocumented; no UTC/GPS default inferred.')
            if saved['utc_offset_seconds'] is None: issues.append('Source-minus-UTC offset is undocumented.')
            if not meta['antenna'] or meta['antenna_hen'] is None: issues.append('Antenna type or H/E/N offset header is missing.')
            if not meta['marker']: issues.append('Marker name is missing.')
            if not project['crs'] or not project['height_datum'] or not project['epoch']: issues.append('Network reference/height/coordinate epoch is incomplete.')
            if meta['declared_first'] is not None and meta['first_tick']!=meta['declared_first']: issues.append('First epoch differs from TIME OF FIRST OBS.')
            if meta['declared_last'] is not None and meta['last_tick']!=meta['declared_last']: issues.append('Last epoch differs from TIME OF LAST OBS.')
            if meta['approximate_xyz'] is None: notes.append('No header approximate location; source remains listed but is not mapped.')
            elif not 5_000_000<math.sqrt(sum(v*v for v in meta['approximate_xyz']))<8_000_000 or math.hypot(*meta['approximate_xyz'][:2])<1:
                notes.append('Header approximate XYZ is unsuitable for the display profile; source remains listed but is not mapped.')
            if meta['gaps']: notes.append(str(len(meta['gaps']))+' sampling gaps; timeline does not bridge them as continuous coverage.')
            if meta['lli_loss_of_lock'] or meta['lli_half_cycle']: notes.append('Phase LLI loss/half-cycle flags present; counts are header/data indicators, not independent cycle-slip detection.')
            if meta['power_failure_epochs'] or meta['events']: notes.append('Power/event flags require review; no corrections applied.')
            if meta['receiver_clock_offset_epochs']: notes.append('Receiver clock offsets are reported; times are displayed as recorded. Clock corrections are not reproduced by this catalogue.')
            if saved['utc_offset_seconds'] is not None:
                session=next(s for s in project['sessions'] if s['id']==saved['session'])
                start=n._time(session['start'],'Session start'); end=n._time(session['end'],'Session end')
                offset=saved['utc_offset_seconds']*TICK
                if not start or not end: issues.append('Linked acquisition session needs documented UTC start and end.')
                elif meta['first_tick']-offset<_utc_tick(start) or meta['last_tick']-offset>_utc_tick(end): issues.append('Source observations fall outside the declared UTC acquisition session.')
        else: notes.append(meta['navigation_scope'])
        return dict(id=saved['id'],status='BLOCKED' if issues else 'CHECKED',issues=issues,warnings=notes),True
    except (ValueError,OSError,OverflowError) as error:
        return dict(id=saved['id'],status='SOURCE_UNAVAILABLE_OR_CHANGED',issues=[str(error)],warnings=[]),False


def inspect_catalogue(project,cancelled=lambda:False):
    """Re-read every source; compare exact catalogue metadata before map/timeline use."""
    n.validate_project(project); entries=[]; blocked=[]; warnings=[]; verified=[]
    for saved in project.get('raw_sources',[]):
        entry,unchanged=inspect_source(project,saved,cancelled)
        if unchanged:verified.append(saved['id'])
        entries.append(entry)
        blocked.extend(saved['source_name']+': '+s for s in entry['issues'])
        warnings.extend(saved['source_name']+': '+s for s in entry['warnings'])
    pairs=[]
    sources=[r for r in project.get('raw_sources',[]) if r['id'] in verified and r['metadata']['file_type']=='OBS' and r['utc_offset_seconds'] is not None and r['metadata']['time_system']]
    for base in (r for r in sources if r['role']=='BASE'):
        for rover in (r for r in sources if r['role']=='ROVER'):
            if base['session']!=rover['session']: continue
            if base['point']==rover['point']: blocked.append('Base and rover must identify different physical points.'); continue
            a={s[0]-base['utc_offset_seconds']*TICK:set(s[2].split()) for s in base['metadata']['samples']}
            b={s[0]-rover['utc_offset_seconds']*TICK:set(s[2].split()) for s in rover['metadata']['samples']}
            common=sorted(set(a)&set(b)); counts=[len({signal.split('/')[0] for signal in a[t]&b[t]}) for t in common]
            pair=dict(base=base['id'],rover=rover['id'],matched_epochs=len(common),
                first_utc=tick_text(common[0])+'Z' if common else '',last_utc=tick_text(common[-1])+'Z' if common else '',
                minimum_common_code_phase_satellites=min(counts) if counts else 0,maximum_common_code_phase_satellites=max(counts) if counts else 0)
            pairs.append(pair)
            if len(common)<2 or not counts or min(counts)<4: blocked.append('Base/rover pair lacks at least two exactly aligned UTC epochs with four matching code/phase satellites; review time/signal coverage.')
    if not pairs: blocked.append('No verified base/rover pair with explicitly reviewed time offsets.')
    if not any(r['role']=='NAV' and r['id'] in verified for r in project.get('raw_sources',[])): blocked.append('No unchanged broadcast navigation file catalogued. Orbit/fit suitability still requires a processing engine.')
    warnings.append('Preflight/catalogue only: no PPK/static positioning, ambiguity resolution, antenna calibration, frame transformation or independent cycle-slip detection. Four common satellites is only a screening floor, never proof of solution quality.')
    return dict(status='BLOCKED' if blocked else 'RAW_CATALOGUE_CHECKED',project_sha256=content_fingerprint(project),
        verified_sources=verified,entries=entries,pairs=pairs,blockers=blocked,warnings=warnings)
