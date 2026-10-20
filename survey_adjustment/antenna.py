# SPDX-License-Identifier: GPL-3.0-only
"""Strict receiver ANTEX selection before the external engine's permissive reader.

ANTEX 1.4 absolute, 0:5:90 degree receiver patterns. Survey SA2 adds azimuth
grids and explicit E1/E5a, B1C/B3I bands; historical selections stay unchanged.
Original files stay local; a minimal, uniquely named engine subset prevents its
radome/serial/validity fallback and preserves the selected numerical values.
"""
from pathlib import Path
from copy import deepcopy
import hashlib,math,re
from . import rinex as r

MAX_BYTES=32*1024*1024
SYSTEM_FREQUENCIES={'G':('G01','G02'),'R':('R01','R02'),'E':('E01','E07'),'C':('C02','C07')}
AZIMUTH_FREQUENCIES={**SYSTEM_FREQUENCIES,'E':('E01','E05'),'C':('C01','C06')}


def required(systems,frequencies,azimuth=False):
    bands=AZIMUTH_FREQUENCIES if azimuth else SYSTEM_FREQUENCIES
    return tuple(code for system in systems for code in bands[system][:frequencies])


def _samples(text,count):
    try: values=_numbers(text,count)
    except ValueError:
        if len(text)!=count*8:raise ValueError('Invalid ANTEX PCV sample count.') from None
        try: values=[float(text[i:i+8]) for i in range(0,len(text),8)]
        except ValueError:raise ValueError('Invalid ANTEX PCV sample.') from None
    if not all(math.isfinite(v) for v in values):raise ValueError('Nonfinite ANTEX PCV sample.')
    return values


def _numbers(text,count):
    try: values=[float(x) for x in text.split()]
    except ValueError: raise ValueError('Invalid ANTEX numeric field.') from None
    if len(values)!=count or not all(math.isfinite(x) for x in values):
        raise ValueError('Invalid ANTEX numeric field/count.')
    return values


def read(path):
    path=Path(path)
    if not path.is_file() or path.is_symlink() or path.stat().st_size>MAX_BYTES:
        raise ValueError('Select a local ANTEX file within 32 MiB; symbolic links are not accepted.')
    with path.open('rb') as handle: payload=handle.read(MAX_BYTES+1)
    if len(payload)>MAX_BYTES: raise ValueError('ANTEX file exceeds 32 MiB.')
    # Comments in older official files can contain Latin-1 attribution.
    lines=payload.decode('latin-1').splitlines()
    if not lines or len(lines)>300000 or any(len(line)>4096 for line in lines):
        raise ValueError('ANTEX file exceeds the supported line limits.')
    if lines[0][60:80].strip()!='ANTEX VERSION / SYST' or lines[0][:8].strip()!='1.4':
        raise ValueError('This profile requires ANTEX 1.4. Other versions require a separately tested adapter.')
    absolute=False; in_header=True; entries=[]; current=None; frequency=None; rms=False
    for line in lines[1:]:
        label=line[60:80].strip()
        if label=='COMMENT':
            if current is not None:current.setdefault('comments',[]).append(line[:60].rstrip())
            continue
        if in_header:
            if label=='PCV TYPE / REFANT': absolute=line[:1]=='A'
            if label=='END OF HEADER': in_header=False
            continue
        if label=='START OF ANTENNA':
            if current is not None: raise ValueError('Nested ANTEX antenna block.')
            current=dict(type='',serial='',method='',dazi=None,zenith=None,start=None,end=None,count=None,frequencies={})
        elif label=='END OF ANTENNA':
            if current is None or frequency is not None: raise ValueError('Unbalanced ANTEX antenna/frequency block.')
            entries.append(current);current=None
            if len(entries)>10000: raise ValueError('Too many ANTEX entries.')
        elif current is not None:
            if label=='TYPE / SERIAL NO': current.update(type=line[:20].rstrip(),serial=line[20:40].strip())
            elif label=='METH / BY / # / DATE': current['method']=line[:60].rstrip()
            elif label=='SINEX CODE': current['sinex_code']=line[:60].rstrip()
            elif label=='DAZI': current['dazi']=_numbers(line[:8],1)[0]
            elif label=='ZEN1 / ZEN2 / DZEN': current['zenith']=_numbers(line[:20],3)
            elif label=='# OF FREQUENCIES':
                try: current['count']=int(line[:6])
                except ValueError: raise ValueError('Invalid ANTEX frequency count.') from None
            elif label in ('VALID FROM','VALID UNTIL'):
                current['start' if label=='VALID FROM' else 'end']=r.epoch(line[:43].split())
            elif label=='START OF FREQ RMS': rms=True
            elif label=='END OF FREQ RMS': rms=False
            elif rms: continue
            elif label=='START OF FREQUENCY':
                frequency=line[3:6]
                if not re.fullmatch('[GRECJSI][0-9]{2}',frequency) or frequency in current['frequencies']:
                    raise ValueError('Invalid or duplicate ANTEX frequency.')
                current['frequencies'][frequency]={}
            elif label=='END OF FREQUENCY':
                if frequency!=line[3:6]: raise ValueError('Unbalanced ANTEX frequency.')
                frequency=None
            elif frequency and label=='NORTH / EAST / UP':
                if 'neu_mm' in current['frequencies'][frequency]:raise ValueError('Duplicate ANTEX PCO record.')
                current['frequencies'][frequency]['neu_mm']=_numbers(line[:60],3)
            elif frequency and line[:8].strip()=='NOAZI':
                if 'noazi_mm' in current['frequencies'][frequency]:raise ValueError('Duplicate ANTEX NOAZI record.')
                try:
                    grid=current['zenith'];count=round((grid[1]-grid[0])/grid[2])+1
                    if not 2<=count<=500:raise ValueError()
                    vals=_samples(line[8:],count)
                except (ValueError,TypeError,ZeroDivisionError):raise ValueError('Invalid ANTEX NOAZI values/grid.') from None
                current['frequencies'][frequency]['noazi_mm']=vals
            elif frequency and current['dazi']:
                try:
                    angle=_numbers(line[:8],1)[0]
                    grid=current['zenith'];count=round((grid[1]-grid[0])/grid[2])+1
                    if not 2<=count<=500:raise ValueError()
                    values=[angle,*_samples(line[8:],count)]
                except (ValueError,TypeError,ZeroDivisionError):raise ValueError('Invalid ANTEX azimuth record/grid.') from None
                rows=current['frequencies'][frequency].setdefault('azi_mm',[])
                if len(rows)>=361:raise ValueError('Too many ANTEX azimuth rows.')
                rows.append(values)
    if in_header or current is not None or not absolute:
        raise ValueError('Incomplete ANTEX or relative calibration. Absolute ANTEX is required.')
    return dict(sha256=hashlib.sha256(payload).hexdigest(),entries=entries)


def select(catalogue,metadata,frequencies,systems=('G',),azimuth=False):
    """Exact type+radome, serial-specific before generic, entire occupation valid."""
    name=metadata['antenna']; serial=metadata['antenna_serial']
    if len(name)<20 or not name[16:20].strip():
        raise ValueError('RINEX antenna must contain the exact 16-character model and 4-character radome (for example NONE). No radome fallback is allowed.')
    candidates=[e for e in catalogue['entries'] if e['type']==name and
        (e['serial']==serial or not e['serial']) and
        (e['start'] is None or e['start']<=metadata['first_tick']) and
        (e['end'] is None or e['end']>=metadata['last_tick'])]
    specific=[e for e in candidates if e['serial'] and e['serial']==serial]
    candidates=specific or [e for e in candidates if not e['serial']]
    if len(candidates)!=1:
        raise ValueError('ANTEX needs one exact antenna/radome and serial or generic model valid for the entire occupation; missing or ambiguous matches are blocked.')
    chosen=deepcopy(candidates[0])
    if (not azimuth and chosen['dazi']!=0) or chosen['zenith']!=[0.,90.,5.]:
        raise ValueError('Reviewed engine profile supports only DAZI=0 and ZEN1/ZEN2/DZEN=0/90/5. Azimuth patterns or another grid are not silently approximated.')
    if chosen['count']!=len(chosen['frequencies']): raise ValueError('ANTEX frequency count differs from its blocks.')
    if azimuth:
        validate_grid(chosen)
        chosen.setdefault('comments',[]);chosen.setdefault('sinex_code','')
    else:
        chosen.pop('comments',None);chosen.pop('sinex_code',None)
    needed=required(systems,frequencies,azimuth)
    for freq in needed:
        values=chosen['frequencies'].get(freq,{})
        keys={'neu_mm','noazi_mm'}|({'azi_mm'} if chosen['dazi'] else set())
        if set(values)!=keys or len(values['noazi_mm'])!=19:
            raise ValueError('Selected antenna lacks complete '+freq+' PCO and 19 NOAZI PCV samples.')
        if any(not math.isfinite(v) or abs(v)>limit for key,limit in (('neu_mm',10000),('noazi_mm',1000)) for v in values[key]):
            raise ValueError('Nonfinite or implausible ANTEX correction.')
    chosen['frequencies']={f:chosen['frequencies'][f] for f in needed}
    chosen['selection']='serial-specific' if specific else 'generic type/radome'
    return chosen


def validate_grid(model):
    """Bounded complete clockwise grid, including a consistent 0/360 seam."""
    step=model['dazi']
    if type(step) not in (int,float) or not math.isfinite(step) or (step!=0 and (not 5<=step<=90 or abs(360/step-round(360/step))>1e-10)):
        raise ValueError('Azimuth DAZI must be zero or a 5–90 degree step dividing 360.')
    if model['zenith']!=[0.,90.,5.]:raise ValueError('Supported zenith grid is 0/90/5.')
    if not step:return
    count=round(360/step)+1
    for values in model['frequencies'].values():
        rows=values.get('azi_mm',[])
        if not isinstance(rows,list) or len(rows)!=count or any(not isinstance(row,list) or len(row)!=20 or
            any(type(v) not in (int,float) or not math.isfinite(v) for v in row) or abs(row[0]-i*step)>1e-8 or
            any(abs(v)>1000 for v in row[1:]) for i,row in enumerate(rows)):
            raise ValueError('Incomplete, duplicated, nonfinite or unordered ANTEX azimuth grid.')
        if rows[0][1:]!=rows[-1][1:]:raise ValueError('ANTEX azimuth 0/360 seam differs.')


def correction(model,code,azimuth_deg,zenith_deg,heading_deg=0.):
    """Independent review calculation in metres, antenna NRP clockwise from north."""
    values=model['frequencies'][code];z=max(0.,min(18.,zenith_deg/5));lo=min(17,int(z));part=z-lo
    interpolate=lambda row:row[lo]*(1-part)+row[lo+1]*part
    if model['dazi']:
        a=((azimuth_deg-heading_deg)%360)/model['dazi'];index=int(a);weight=a-index
        rows=values['azi_mm'];pcv=interpolate(rows[index][1:])*(1-weight)+interpolate(rows[index+1][1:])*weight
    else:pcv=interpolate(values['noazi_mm'])
    angle=math.radians(heading_deg);north,east,up=values['neu_mm']
    n_rot=north*math.cos(angle)-east*math.sin(angle);e_rot=north*math.sin(angle)+east*math.cos(angle)
    az=math.radians(azimuth_deg);zen=math.radians(zenith_deg)
    return (-e_rot*math.sin(az)*math.sin(zen)-n_rot*math.cos(az)*math.sin(zen)-up*math.cos(zen)+pcv)/1000


def subset(models):
    """Keep actual constellation/frequency IDs under unique receiver aliases."""
    line=lambda value,label:f'{value:<60}{label:<20}\n'
    systems={f[0] for _,model in models for f in model['frequencies']}
    text=line('     1.4            '+('G' if systems=={'G'} else 'M'),'ANTEX VERSION / SYST')+line('A','PCV TYPE / REFANT')+line('','END OF HEADER')
    for alias,model in models:
        text+=line('','START OF ANTENNA')+line(f'{alias:<20}','TYPE / SERIAL NO')
        text+=line(f'{model.get("dazi",0.):8.1f}','DAZI')+line('     0.0  90.0   5.0','ZEN1 / ZEN2 / DZEN')
        text+=line(f'{len(model["frequencies"]):6d}','# OF FREQUENCIES')
        for freq,values in model['frequencies'].items():
            text+=line('   '+freq,'START OF FREQUENCY')
            text+=line(''.join(f'{v:16.4f}' for v in values['neu_mm']),'NORTH / EAST / UP')
            # Stay within the pinned reader's 256-byte line buffer while keeping
            # whitespace between every value, including negative/full-width data.
            text+='   NOAZI'+''.join(f'{v:12.4f}' for v in values['noazi_mm'])+'\n'
            for row in values.get('azi_mm',[]):text+=f'{row[0]:8.1f}'+''.join(f'{v:12.4f}' for v in row[1:])+'\n'
            text+=line('   '+freq,'END OF FREQUENCY')
        text+=line('','END OF ANTENNA')
    return text.encode('ascii')
