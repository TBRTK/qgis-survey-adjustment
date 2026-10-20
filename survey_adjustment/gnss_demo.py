# SPDX-License-Identifier: GPL-3.0-only
"""Construct GNSS observations from invented circular orbits, not receiver data.

Software integration fixture only: no atmosphere or multipath. Optional invented
receiver patterns. Kepler circular orbit, signal travel and Earth-rotation model.
No RTKLIB source is copied. All carrier ambiguities are constructed integers.
"""
from datetime import datetime,timedelta
from pathlib import Path
import math,random,json
from functools import lru_cache
from .coordinate_job import xyz
from . import network as n,rinex as r,antenna

C=299792458.; OMEGA=7.2921151467e-5; MU=3.986005e14; A=26560000.
START=datetime(2026,10,18,8,0,18)
GPS0=datetime(1980,1,6)
WEEK=int((START-GPS0).total_seconds()//604800)
TOE=(START-GPS0).total_seconds()-WEEK*604800
BASE=xyz(60.4,5.2,80.,'EPSG:4979')


def glo_initial(prn):
    plane,k=divmod(prn-1,8);angle=k*math.pi/4+plane*.2;node=plane*2*math.pi/3+.3
    radius=25471000.;speed=math.sqrt(3.9860044e14/radius);inc=math.radians(64.8)
    rotate=lambda x,y:[x*math.cos(node)-y*math.cos(inc)*math.sin(node),x*math.sin(node)+y*math.cos(inc)*math.cos(node),y*math.sin(inc)]
    pos=rotate(radius*math.cos(angle),radius*math.sin(angle));vel=rotate(-speed*math.sin(angle),speed*math.cos(angle))
    return tuple(pos+[vel[0]+OMEGA*pos[1],vel[1]-OMEGA*pos[0],vel[2]])


def glo_step(state,seconds):
    """Independent J2 + rotating-Earth dynamics; fourth-order integration."""
    def derivative(s):
        x,y,z,vx,vy,vz=s;r2=x*x+y*y+z*z;radius=math.sqrt(r2)
        gravity=3.9860044e14/(radius**3);j2=1.5*1.0826257e-3*3.9860044e14*6378136.**2/(radius**5)
        flatten=5*z*z/r2;w=7.292115e-5
        return [vx,vy,vz,(-gravity-j2*(1-flatten)+w*w)*x+2*w*vy,
            (-gravity-j2*(1-flatten)+w*w)*y-2*w*vx,(-gravity-j2*(3-flatten))*z]
    a=derivative(state);b=derivative([v+seconds*q/2 for v,q in zip(state,a)])
    c=derivative([v+seconds*q/2 for v,q in zip(state,b)]);e=derivative([v+seconds*q for v,q in zip(state,c)])
    return tuple(v+seconds*(aa+2*bb+2*cc+ee)/6 for v,aa,bb,cc,ee in zip(state,a,b,c,e))


@lru_cache(maxsize=4096)
def glo_grid(prn,index):
    return glo_initial(prn) if index==0 else glo_step(glo_grid(prn,index-1),15.)


def glo_channel(prn):return (prn-1)%14-7


def orbit(system):
    return {'G':(A,MU,OMEGA,TOE,0.,0.),'E':(29600000.,3.986004418e14,OMEGA,TOE-60,60.,.4),
            'C':(27800000.,3.986004418e14,7.292115e-5,TOE-74,60.,.8)}[system]


def position(prn,seconds,system='G'):
    if system=='R':
        index=max(0,int(seconds//15));return list(glo_step(glo_grid(prn,index),seconds-index*15)[:3])
    a,mu,omega,toe,lead,offset=orbit(system)
    plane,index=divmod(prn-(6 if system=='C' else 1),4)
    u=index*math.pi/2+plane*.23+math.sqrt(mu/a**3)*(seconds+lead)
    node=plane*math.pi/3+offset-omega*(toe+seconds+lead); inc=math.radians(55)
    x,y=a*math.cos(u),a*math.sin(u)
    return [x*math.cos(node)-y*math.cos(inc)*math.sin(node),
            x*math.sin(node)+y*math.cos(inc)*math.cos(node),y*math.sin(inc)]


def distance(prn,seconds,receiver,system='G'):
    travel=.08
    for _ in range(5):
        sat=position(prn,seconds-travel,system); angle=OMEGA*travel
        rotated=[math.cos(angle)*sat[0]+math.sin(angle)*sat[1],-math.sin(angle)*sat[0]+math.cos(angle)*sat[1],sat[2]]
        travel=math.dist(rotated,receiver)/C
    return travel*C


def rover_at(seconds,moving):
    return [BASE[0]+25.+(.02*seconds if moving else 0.),BASE[1]+30.,BASE[2]+12.]


def header(value,label): return f'{value:<60}{label:<20}\n'


def synthetic_model(base):
    # Invented values, not a calibration of any real antenna or receiver.
    return dict(frequencies={f:dict(neu_mm=[10. if base else -20.,5. if base else 15.,100. if base else 350.],
        noazi_mm=[round((5. if base else 15.)*(z/90)**2,4) for z in range(0,91,5)]) for f in ('G01','G02')})


def multi_model(base,systems,azimuth=False):
    model=synthetic_model(base)
    for system in systems:
        if system=='G':continue
        for index,freq in enumerate((antenna.AZIMUTH_FREQUENCIES if azimuth else antenna.SYSTEM_FREQUENCIES)[system]):
            offset={'E':80.,'C':-65.,'R':120.}[system]*(1 if base else -1)*(1+index*.2)
            model['frequencies'][freq]=dict(neu_mm=[20.+offset/3,-30.+offset/4,200.+offset],
                noazi_mm=[round((30.+offset/5)*(z/90)**2,4) for z in range(0,91,5)])
    if azimuth:
        model.update(dazi=5.)
        for index,values in enumerate(model['frequencies'].values()):
            amplitude=(40.+index*8)*(1 if base else -1)
            values['azi_mm']=[[float(a),*[round(v+amplitude*math.sin(math.radians(a if a<360 else 0))*math.sin(math.radians(z)),4)
                for z,v in zip(range(0,91,5),values['noazi_mm'])]] for a in range(0,361,5)]
    return model


def observations(moving,base=False,systems=('G',),calibrated=False,azimuth=False):
    rng=random.Random(100 if base else 101)
    model_set=multi_model(base,systems,azimuth) if calibrated else None
    receiver=BASE if base else rover_at(0,moving)
    text=header(f'{"3.05":>9}           O                   '+('M' if len(systems)>1 else 'G'),'RINEX VERSION / TYPE')
    text+=header('SYNTHETIC IDEAL ORBITS / CODE / PHASE - NO FIELD DATA','COMMENT')
    text+=header('SIM_BASE' if base else 'SIM_ROVER','MARKER NAME')
    text+=header(f'{"SYNTHETIC":<20}{"IDEAL":<20}{"1":<20}','REC # / TYPE / VERS')
    name=('SIM_BASEANT     NONE' if base else 'SIM_ROVERANT    NONE') if calibrated else 'ZERO-PATTERN'
    text+=header(f'{"SYNTHETIC":<20}{name:<20}','ANT # / TYPE')
    text+=header(''.join(f'{v:14.4f}' for v in receiver),'APPROX POSITION XYZ')
    text+=header(''.join(f'{v:14.4f}' for v in ((1. if base else 2.,0.,0.) if calibrated else (0.,0.,0.))),'ANTENNA: DELTA H/E/N')
    for system in systems:
        types={'G':'G    4 C1C L1C C2W L2W','E':'E    4 C1C L1C C7Q L7Q','C':'C    4 C2I L2I C7I L7I','R':'R    4 C1C L1C C2C L2C'}
        if azimuth:types.update(E='E    4 C1C L1C C5Q L5Q',C='C    4 C1P L1P C6I L6I')
        text+=header(types[system],'SYS / # / OBS TYPES')
    if azimuth:text+=header(f'{(0. if base else 37.):14.4f}','ANTENNA: ZERODIR AZI')
    text+=header(f'{5.:10.3f}','INTERVAL')
    for stamp,label in ((START,'TIME OF FIRST OBS'),(START+timedelta(seconds=600),'TIME OF LAST OBS')):
        text+=header(f'{stamp.year:6d}{stamp.month:6d}{stamp.day:6d}{stamp.hour:6d}{stamp.minute:6d}{stamp.second:13.7f}     GPS',label)
    text+=header('','END OF HEADER')
    phi,lam=math.radians(60.4),math.radians(5.2)
    up=[math.cos(phi)*math.cos(lam),math.cos(phi)*math.sin(lam),math.sin(phi)]
    for seconds in range(0,601,5):
        receiver=BASE if base else rover_at(seconds,moving); stamp=START+timedelta(seconds=seconds)
        sats=[]
        for system in systems:
            for prn in range(19 if azimuth and system=='C' else 6 if system=='C' else 1,43 if azimuth and system=='C' else 30 if system=='C' else 25):
                sat=position(prn,seconds,system); vector=[a-b for a,b in zip(sat,receiver)]
                elevation=math.asin(sum(a*b for a,b in zip(vector,up))/math.sqrt(sum(a*a for a in vector)))
                if elevation>math.radians(15): sats.append((system,prn))
        text+=f'> {stamp:%Y %m %d %H %M} {stamp.second:10.7f}  0{len(sats):3d}\n'
        for system,prn in sats:
            rho=distance(prn,seconds,receiver,system)
            corrections=[0.,0.]
            if calibrated:
                sat=position(prn,seconds,system); los=[a-b for a,b in zip(sat,receiver)]; norm=math.sqrt(sum(v*v for v in los)); los=[v/norm for v in los]
                east=[-math.sin(lam),math.cos(lam),0.];north=[-math.sin(phi)*math.cos(lam),-math.sin(phi)*math.sin(lam),math.cos(phi)]
                zen=math.degrees(math.acos(sum(a*b for a,b in zip(los,up))));i=min(17,int(zen/5));f=zen/5-i
                for slot,freq in enumerate((antenna.AZIMUTH_FREQUENCIES if azimuth else antenna.SYSTEM_FREQUENCIES)[system]):
                    entire=model_set;model=entire['frequencies'][freq];nn,ee,uu=[v/1000 for v in model['neu_mm']]
                    offset=[nn*a+ee*b+(uu+(1. if base else 2.))*c for a,b,c in zip(north,east,up)]
                    corrections[slot]=-sum(a*b for a,b in zip(offset,los))+(model['noazi_mm'][i]*(1-f)+model['noazi_mm'][i+1]*f)/1000
                    if azimuth:
                        # Independent generation: rotate LOS into antenna axes;
                        # sum four grid corners (not the adapter review helper).
                        heading=0. if base else 37.;angle=math.radians(heading)
                        n_rot=nn*math.cos(angle)-ee*math.sin(angle);e_rot=nn*math.sin(angle)+ee*math.cos(angle)
                        geometric_az=math.degrees(math.atan2(sum(a*b for a,b in zip(los,east)),sum(a*b for a,b in zip(los,north))))%360
                        column=((geometric_az-heading)%360)/5;lower=int(column);w=column-lower
                        grid=entire['frequencies'][freq]['azi_mm']
                        pcv=sum(grid[a][z+1]*wa*wz for a,wa in ((lower,1-w),(lower+1,w)) for z,wz in ((i,1-f),(i+1,f)))/1000
                        corrections[slot]=-sum((n_rot*a+e_rot*b+(uu+(1. if base else 2.))*c)*v for a,b,c,v in zip(north,east,up,los))+pcv
            f1,f2=(1602e6+glo_channel(prn)*.5625e6,1246e6+glo_channel(prn)*.4375e6) if system=='R' else {'G':(1575.42e6,1227.60e6),'E':(1575.42e6,1207.14e6),'C':(1561.098e6,1207.14e6)}[system]
            if azimuth and system in ('E','C'):f1,f2={'E':(1575.42e6,1176.45e6),'C':(1575.42e6,1268.52e6)}[system]
            code1=rho+corrections[0]+rng.gauss(0,.02); code2=rho+corrections[1]+rng.gauss(0,.02)
            phase1=(rho+corrections[0]+rng.gauss(0,.001))/(C/f1)+100000+prn*100+(0 if base else 50)
            phase2=(rho+corrections[1]+rng.gauss(0,.001))/(C/f2)+80000+prn*80+(0 if base else 30)
            text+=f'{system}{prn:02d}'+''.join(f'{v:14.3f}07' for v in (code1,phase1,code2,phase2))+'\n'
    return text


def navigation(systems=('G',),azimuth=False):
    text=header(f'{"3.05":>9}           N                   '+('M' if len(systems)>1 else 'G'),'RINEX VERSION / TYPE')
    text+=header('SYNTHETIC CIRCULAR ORBITS - NOT REAL EPHEMERIDES','COMMENT')+header('','END OF HEADER')
    for system in systems:
        if system=='R':
            for prn in range(1,25):
                state=glo_initial(prn);stamp=START-timedelta(seconds=18)
                text+=f'R{prn:02d} {stamp:%Y %m %d %H %M %S}'+''.join(f'{v:19.12E}' for v in (0.,0.,28800.))+'\n'
                for axis in range(3):
                    row=[state[axis]/1000,state[axis+3]/1000,0.,glo_channel(prn) if axis==1 else 0.]
                    text+='    '+''.join(f'{v:19.12E}' for v in row)+'\n'
                text+='    '+''.join(f'{0.:19.12E}' for _ in range(4))+'\n'
            continue
        a,mu,omega,toe,lead,offset=orbit(system)
        for prn in range(19 if azimuth and system=='C' else 6 if system=='C' else 1,43 if azimuth and system=='C' else 30 if system=='C' else 25):
            plane,index=divmod(prn-(6 if system=='C' else 1),4)
            stamp=START-timedelta(seconds=lead+(14 if system=='C' else 0))
            week=int((stamp-datetime(2006,1,1)).total_seconds()//604800) if system=='C' else WEEK
            text+=f'{system}{prn:02d} {stamp:%Y %m %d %H %M %S}'+''.join(f'{0.:19.12E}' for _ in range(3))+'\n'
            rows=[[1,0,0,index*math.pi/2+plane*.23],[0,0,0,math.sqrt(a)],
                [toe,0,plane*math.pi/3+offset,0],[math.radians(55),0,0,0],
                [0,(258 if azimuth else 517) if system=='E' else 1,week,0],[1,0,0,0 if system=='C' else 1],[toe,4,0,0]]
            for row in rows: text+='    '+''.join(f'{v:19.12E}' for v in row)+'\n'
    return text


def create(folder,moving=False,systems=('G',),calibrated=False,azimuth=False):
    """Create a new explicit synthetic project; never append to a user's network."""
    if not systems or 'G' not in systems or any(s not in ('G','E','C','R') for s in systems) or (azimuth and not calibrated):raise ValueError('Unsupported synthetic profile.')
    folder=Path(folder); folder.mkdir(parents=True,exist_ok=False)
    project=n.new_project(); project.update(name='SYNTHETIC '+('moving PPK' if moving else 'static')+' '+''.join(systems)+(' ANTEX' if calibrated else '')+' integration example',
        synthetic=True,crs='EPSG:25832',height_datum='ELLIP',epoch='2026-10-18',
        notes='Constructed circular '+''.join(systems)+' orbits; no atmosphere. '+('Invented ANTEX PCO/PCV and H/E/N offsets.' if calibrated else 'Zero antenna patterns and offsets.')+' Software test only; coordinates are not national controls.')
    project['points']=[dict(n.point(),id='SIM_BASE'),dict(n.point(),id='SIM_ROVER')]
    project['sessions']=[dict(n.session(),id='SIM01',start='2026-10-18T08:00:00Z',end='2026-10-18T08:10:00Z',instrument='SYNTHETIC GNSS',corrections='Constructed GPS-UTC = 18 s, BDT = GPST - 14 s; no atmosphere.')]
    for name,text,role,point in (('base',observations(moving,True,systems,calibrated,azimuth),'BASE','SIM_BASE'),('rover',observations(moving,False,systems,calibrated,azimuth),'ROVER','SIM_ROVER'),('navigation',navigation(systems,azimuth),'NAV','')):
        path=folder/(name+'.rnx'); path.write_text(text,encoding='ascii',newline='\n')
        project=r.plan_import(project,r.read_source(path),r.ImportReview(role=role,point=point,session='SIM01' if point else '',
            utc_offset_seconds=18 if point else None,time_evidence='Constructed GPS minus UTC = 18 seconds.',
            antenna_evidence='SYNTHETIC invented ANTEX and marker-to-ARP offsets.' if calibrated else 'SYNTHETIC zero antenna offsets/patterns; simulated electrical endpoints.',
            note='Ideal constructed software test, not field measurements.',synthetic=True,confirmed=True))
    n.save_project(folder/'example.sanet.json',project)
    if calibrated:
        payload=antenna.subset([('SIM_BASEANT     NONE',multi_model(True,systems,azimuth)),('SIM_ROVERANT    NONE',multi_model(False,systems,azimuth))])
        (folder/'synthetic.atx').write_bytes(payload.replace(header('','END OF HEADER').encode('ascii'),(header('SYNTHETIC INVENTED MODELS - NOT REAL ANTENNA CALIBRATION','COMMENT')+header('','END OF HEADER')).encode('ascii'),1))
    (folder/'truth.json').write_text(json.dumps(dict(synthetic=True,moving=moving,base_xyz=BASE,
        rover_start_xyz=rover_at(0,moving),rover_end_xyz=rover_at(600,moving),systems=list(systems),calibrated=calibrated,azimuth=azimuth,
        caution='Ideal synthetic integration fixture; cannot establish field accuracy.'),indent=2)+'\n',encoding='utf-8')
    return project
