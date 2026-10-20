# SPDX-License-Identifier: GPL-3.0-only
"""Construct ideal GPS observations from invented circular orbits, not receiver data.

Software integration fixture only: no atmosphere, multipath or antenna phase
patterns. Kepler circular orbit, signal travel and Earth-rotation range model.
No RTKLIB source is copied. All carrier ambiguities are constructed integers.
"""
from datetime import datetime,timedelta
from pathlib import Path
import math,random,json
from .coordinate_job import xyz
from . import network as n,rinex as r

C=299792458.; OMEGA=7.2921151467e-5; MU=3.986005e14; A=26560000.
START=datetime(2026,10,18,8,0,18)
GPS0=datetime(1980,1,6)
WEEK=int((START-GPS0).total_seconds()//604800)
TOE=(START-GPS0).total_seconds()-WEEK*604800
BASE=xyz(60.4,5.2,80.,'EPSG:4979')


def position(prn,seconds):
    plane,index=divmod(prn-1,4)
    u=index*math.pi/2+plane*.23+math.sqrt(MU/A**3)*seconds
    node=plane*math.pi/3-OMEGA*(TOE+seconds); inc=math.radians(55)
    x,y=A*math.cos(u),A*math.sin(u)
    return [x*math.cos(node)-y*math.cos(inc)*math.sin(node),
            x*math.sin(node)+y*math.cos(inc)*math.cos(node),y*math.sin(inc)]


def distance(prn,seconds,receiver):
    travel=.08
    for _ in range(5):
        sat=position(prn,seconds-travel); angle=OMEGA*travel
        rotated=[math.cos(angle)*sat[0]+math.sin(angle)*sat[1],-math.sin(angle)*sat[0]+math.cos(angle)*sat[1],sat[2]]
        travel=math.dist(rotated,receiver)/C
    return travel*C


def rover_at(seconds,moving):
    return [BASE[0]+25.+(.02*seconds if moving else 0.),BASE[1]+30.,BASE[2]+12.]


def header(value,label): return f'{value:<60}{label:<20}\n'


def observations(moving,base=False):
    rng=random.Random(100 if base else 101)
    receiver=BASE if base else rover_at(0,moving)
    text=header(f'{"3.05":>9}           O                   G','RINEX VERSION / TYPE')
    text+=header('SYNTHETIC IDEAL ORBITS / CODE / PHASE - NO FIELD DATA','COMMENT')
    text+=header('SIM_BASE' if base else 'SIM_ROVER','MARKER NAME')
    text+=header(f'{"SYNTHETIC":<20}{"IDEAL":<20}{"1":<20}','REC # / TYPE / VERS')
    text+=header(f'{"SYNTHETIC":<20}{"ZERO-PATTERN":<20}','ANT # / TYPE')
    text+=header(''.join(f'{v:14.4f}' for v in receiver),'APPROX POSITION XYZ')
    text+=header(''.join(f'{v:14.4f}' for v in (0.,0.,0.)),'ANTENNA: DELTA H/E/N')
    text+=header('G    4 C1C L1C C2W L2W','SYS / # / OBS TYPES')
    text+=header(f'{5.:10.3f}','INTERVAL')
    for stamp,label in ((START,'TIME OF FIRST OBS'),(START+timedelta(seconds=600),'TIME OF LAST OBS')):
        text+=header(f'{stamp.year:6d}{stamp.month:6d}{stamp.day:6d}{stamp.hour:6d}{stamp.minute:6d}{stamp.second:13.7f}     GPS',label)
    text+=header('','END OF HEADER')
    phi,lam=math.radians(60.4),math.radians(5.2)
    up=[math.cos(phi)*math.cos(lam),math.cos(phi)*math.sin(lam),math.sin(phi)]
    for seconds in range(0,601,5):
        receiver=BASE if base else rover_at(seconds,moving); stamp=START+timedelta(seconds=seconds)
        sats=[]
        for prn in range(1,25):
            sat=position(prn,seconds); vector=[a-b for a,b in zip(sat,receiver)]
            elevation=math.asin(sum(a*b for a,b in zip(vector,up))/math.sqrt(sum(a*a for a in vector)))
            if elevation>math.radians(15): sats.append(prn)
        text+=f'> {stamp:%Y %m %d %H %M} {stamp.second:10.7f}  0{len(sats):3d}\n'
        for prn in sats:
            rho=distance(prn,seconds,receiver)
            code1=rho+rng.gauss(0,.02); code2=rho+rng.gauss(0,.02)
            phase1=(rho+rng.gauss(0,.001))/(C/1575.42e6)+100000+prn*100+(0 if base else 50)
            phase2=(rho+rng.gauss(0,.001))/(C/1227.60e6)+80000+prn*80+(0 if base else 30)
            text+=f'G{prn:02d}'+''.join(f'{v:14.3f}07' for v in (code1,phase1,code2,phase2))+'\n'
    return text


def navigation():
    text=header(f'{"3.05":>9}           N                   G','RINEX VERSION / TYPE')
    text+=header('SYNTHETIC CIRCULAR GPS ORBITS - NOT REAL EPHEMERIDES','COMMENT')+header('','END OF HEADER')
    for prn in range(1,25):
        plane,index=divmod(prn-1,4)
        text+=f'G{prn:02d} {START:%Y %m %d %H %M %S}'+''.join(f'{0.:19.12E}' for _ in range(3))+'\n'
        rows=[[1,0,0,index*math.pi/2+plane*.23],[0,0,0,math.sqrt(A)],
            [TOE,0,plane*math.pi/3,0],[math.radians(55),0,0,0],
            [0,1,WEEK,0],[1,0,0,1],[TOE,4,0,0]]
        for row in rows: text+='    '+''.join(f'{v:19.12E}' for v in row)+'\n'
    return text


def create(folder,moving=False):
    """Create a new explicit synthetic project; never append to a user's network."""
    folder=Path(folder); folder.mkdir(parents=True,exist_ok=False)
    project=n.new_project(); project.update(name='SYNTHETIC '+('moving PPK' if moving else 'static GPS')+' integration example',
        synthetic=True,crs='EPSG:25832',height_datum='ELLIP',epoch='2026-10-18',
        notes='Constructed circular GPS orbits and ideal signals; no atmosphere or antenna patterns. Software test only. Coordinates are not national controls.')
    project['points']=[dict(n.point(),id='SIM_BASE'),dict(n.point(),id='SIM_ROVER')]
    project['sessions']=[dict(n.session(),id='SIM01',start='2026-10-18T08:00:00Z',end='2026-10-18T08:10:00Z',instrument='SYNTHETIC GPS',corrections='Constructed GPS-UTC = 18 s; no atmosphere.')]
    for name,text,role,point in (('base',observations(moving,True),'BASE','SIM_BASE'),('rover',observations(moving),'ROVER','SIM_ROVER'),('navigation',navigation(),'NAV','')):
        path=folder/(name+'.rnx'); path.write_text(text,encoding='ascii',newline='\n')
        project=r.plan_import(project,r.read_source(path),r.ImportReview(role=role,point=point,session='SIM01' if point else '',
            utc_offset_seconds=18 if point else None,time_evidence='Constructed GPS minus UTC = 18 seconds.',
            antenna_evidence='SYNTHETIC zero antenna offsets/patterns; simulated electrical endpoints.',
            note='Ideal constructed software test, not field measurements.',synthetic=True,confirmed=True))
    n.save_project(folder/'example.sanet.json',project)
    (folder/'truth.json').write_text(json.dumps(dict(synthetic=True,moving=moving,base_xyz=BASE,
        rover_start_xyz=rover_at(0,moving),rover_end_xyz=rover_at(600,moving),
        caution='Ideal synthetic integration fixture; cannot establish field accuracy.'),indent=2)+'\n',encoding='utf-8')
    return project
