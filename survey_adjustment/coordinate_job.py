# SPDX-License-Identifier: GPL-3.0-only
"""Independent absolute-coordinate observations, expressed in SALSA's DEL model.

The fixed computational origin is just a numeric translation, not a GNSS base.
Input covariance is diagonal in geodetic ENU, rotated in full into ECEF XYZ.
No raw baseline, geoid, antenna, datum or epoch correction is inferred.
"""
from collections import defaultdict
from dataclasses import dataclass,asdict
from datetime import date
import csv
import hashlib
import json
import math
from pathlib import Path

from .core import key,number


FIELDS = {
    'name': ('Målingens navn', ['Name','Point','Punktnavn']),
    'group': ('Kontrollpunkt-ID', ['Control ID','Kontrollpunkt','Point ID','Name']),
    'lat': ('Breddegrad (desimalgrader)', ['Latitude','Breddegrad']),
    'lon': ('Lengdegrad (desimalgrader)', ['Longitude','Lengdegrad']),
    'h': ('Ellipsoidisk punkthøyde (m)', ['Ellipsoidal height','Ellipsoid height']),
    'status': ('Løsningsstatus (bare FIX brukes)', ['Solution status','RTK status','Status']),
    'time': ('Starttid for målingen', ['Averaging start','Timestamp','Tid']),
    'se': ('1σ geodetisk øst (m)', ['Sigma E','Sigma East','sde']),
    'sn': ('1σ geodetisk nord (m)', ['Sigma N','Sigma North','sdn']),
    'su': ('1σ opp (m)', ['Sigma U','Sigma Up','sdu']),
}
FRAMES = {'EPSG:4937': ('ETRS89 / GRS80',298.257222101),
          'EPSG:4979': ('WGS84 / WGS84-ellipsoide',298.257223563)}


@dataclass
class CoordinateOptions:
    synthetic: bool = False
    frame: str = ''
    epoch: str = ''
    ellipsoid_confirmed: bool = False
    independent_confirmed: bool = False
    uncertainty_confirmed: bool = False
    sigma_mode: str = 'constant'
    sigma_e: float = 0.
    sigma_n: float = 0.
    sigma_u: float = 0.
    uncertainty_source: str = ''
    correction_source: str = ''
    antenna_note: str = ''


def mapping_for(headers):
    return {f:next((h for a in aliases for h in headers if key(h)==key(a)),'')
            for f,(_,aliases) in FIELDS.items()}


def xyz(lat,lon,h,frame):
    invf=FRAMES[frame][1]
    phi,lam=math.radians(lat),math.radians(lon)
    f=1/invf; e2=f*(2-f); a=6378137.
    n=a/math.sqrt(1-e2*math.sin(phi)**2)
    return [(n+h)*math.cos(phi)*math.cos(lam),(n+h)*math.cos(phi)*math.sin(lam),
            (n*(1-e2)+h)*math.sin(phi)]


def llh(values,frame):
    x,y,z=values; a=6378137.; f=1/FRAMES[frame][1]; e2=f*(2-f)
    p=math.hypot(x,y)
    if p<1: raise ValueError('Punktet ligger for nær polaksen for denne importprofilen.')
    phi=math.atan2(z,p*(1-e2))
    for _ in range(15):
        n=a/math.sqrt(1-e2*math.sin(phi)**2)
        next_phi=math.atan2(z+e2*n*math.sin(phi),p)
        if abs(next_phi-phi)<1e-15: break
        phi=next_phi
    n=a/math.sqrt(1-e2*math.sin(phi)**2)
    return [math.degrees(phi),math.degrees(math.atan2(y,x)),p/math.cos(phi)-n]


def transpose(m): return [list(row) for row in zip(*m)]
def matmul(a,b): return [[math.fsum(x*y for x,y in zip(row,col)) for col in zip(*b)] for row in a]
def matvec(a,v): return [math.fsum(x*y for x,y in zip(row,v)) for row in a]


def inverse_spd(a):
    """Small Cholesky solve; reject indefinite/non-finite input, no regularization."""
    l=[[0.]*3 for _ in range(3)]
    for i in range(3):
        for j in range(i+1):
            value=a[i][j]-math.fsum(l[i][k]*l[j][k] for k in range(j))
            if i==j:
                if not math.isfinite(value) or value<=0: raise ValueError('Ugyldig kovarians/normalmatrise.')
                l[i][j]=math.sqrt(value)
            else: l[i][j]=value/l[j][j]
    columns=[]
    for col in range(3):
        y=[0.]*3; x=[0.]*3
        for i in range(3): y[i]=((1. if i==col else 0.)-math.fsum(l[i][k]*y[k] for k in range(i)))/l[i][i]
        for i in reversed(range(3)): x[i]=(y[i]-math.fsum(l[k][i]*x[k] for k in range(i+1,3)))/l[i][i]
        columns.append(x)
    return transpose(columns)


def covariance(lat,lon,sigmas):
    phi,lam=math.radians(lat),math.radians(lon)
    # Columns are geodetic East, North, Up unit vectors in XYZ.
    r=[[-math.sin(lam),-math.sin(phi)*math.cos(lam),math.cos(phi)*math.cos(lam)],
       [math.cos(lam),-math.sin(phi)*math.sin(lam),math.cos(phi)*math.sin(lam)],
       [0.,math.cos(phi),math.sin(phi)]]
    d=[[sigmas[i]**2 if i==j else 0. for j in range(3)] for i in range(3)]
    return matmul(matmul(r,d),transpose(r))


def sigma(value,line):
    value=number(value,'1σ',line)
    if not .0001<=value<=100:
        raise ValueError(f'Rad {line}: 1σ må være mellom 0.0001 og 100 meter.')
    return value


def coordinate_job(table,mapping,options):
    if options.frame not in FRAMES:
        raise ValueError('Velg og dokumenter ETRS89 eller WGS84 for bredde/lengde/ellipsoidisk høyde.')
    if not all((options.ellipsoid_confirmed,options.independent_confirmed,options.uncertainty_confirmed)):
        raise ValueError('Bekreft ellipsoidisk punkthøyde, usikkerhetsmodell og uavhengige målinger før SALSA.')
    try: date.fromisoformat(options.epoch)
    except (ValueError,TypeError): raise ValueError('Oppgi måleepoke som dato ÅÅÅÅ-MM-DD. Ingen epoketransformasjon utføres.') from None
    if not all(s.strip() for s in (options.uncertainty_source,options.correction_source,options.antenna_note)):
        raise ValueError('Dokumenter kilde for 1σ, korreksjonstjeneste/base og antenne-/punkthøyde.')
    if options.sigma_mode not in ('constant','columns'): raise ValueError('Velg usikkerhetsmodell.')
    required=['name','group','lat','lon','h','status']
    if options.sigma_mode=='columns': required+=['se','sn','su']
    for field in required:
        if mapping.get(field) not in table['headers']: raise ValueError('Velg kildekolonne for '+FIELDS[field][0]+'.')
    if len({mapping[f] for f in ('lat','lon','h')})!=3:
        raise ValueError('Breddegrad, lengdegrad og ellipsoidisk høyde må bruke ulike kolonner.')
    if options.sigma_mode=='columns' and any('rms' in key(mapping[f]) for f in ('se','sn','su')):
        raise ValueError('Emlids RMS-kolonner tolkes ikke automatisk som 1σ. Bruk dokumenterte sigmakolonner eller egne vurderte verdier.')
    constants=[sigma(v,0) for v in (options.sigma_e,options.sigma_n,options.sigma_u)] if options.sigma_mode=='constant' else None
    if len(table['rows'])>500: raise ValueError('Første profil støtter høyst 500 koordinatobservasjoner per kjøring.')
    groups=defaultdict(list); excluded=[]; times=set(); warnings=[]
    for row in table['rows']:
        line=row['__line__']; status=row[mapping['status']].upper().replace(' ','_')
        if status not in ('FIX','FIXED','RTK_FIX','RTK_FIXED'):
            excluded.append(dict(row=line,name=row[mapping['name']],reason='Ikke dokumentert FIX: '+(status or '(tom)')))
            continue
        name,group=row[mapping['name']].strip(),row[mapping['group']].strip()
        if not name or not group: raise ValueError(f'Rad {line}: målingsnavn og kontrollpunkt-ID må være fylt inn.')
        try: lat,lon,h=[number(row[mapping[f]],f,line) for f in ('lat','lon','h')]
        except ValueError:
            raise ValueError(f'Rad {line}: bredde/lengde må være endelige desimalgrader, høyden et endelig tall i meter.') from None
        if not (-89<=lat<=89 and -180<=lon<=180 and -1000<=h<=10000):
            raise ValueError(f'Rad {line}: ugyldig bredde/lengde/ellipsoidisk høyde; bruk desimalgrader og meter.')
        for header in table['headers']:
            if key(header)=='geodeticcrs' and row[header] and row[header]!=options.frame:
                raise ValueError(f'Rad {line}: Geodetic CRS stemmer ikke med valgt grunnlag.')
            if key(header)=='heightdatum' and row[header] and row[header].upper() not in ('ELLIP','ELLIPSOIDAL') and key(mapping['h']) in ('elevation','height','z'):
                raise ValueError(f'Rad {line}: ortometrisk Elevation kan ikke brukes som ellipsoidisk høyde.')
        time=row.get(mapping.get('time'),'').strip()
        if time:
            stamp=(group,time)
            if stamp in times: raise ValueError(f'Rad {line}: samme kontrollpunkt og måletid forekommer flere ganger. Kontroller dobbeltimport.')
            times.add(stamp)
        sigmas=[sigma(row[mapping[f]],line) for f in ('se','sn','su')] if constants is None else constants
        cov=covariance(lat,lon,sigmas)
        groups[group].append(dict(row=line,name=name,group=group,time=time,lat=lat,lon=lon,h=h,
            xyz=xyz(lat,lon,h,options.frame),sigma_enu=list(sigmas),covariance_xyz=cov))
    if not groups: raise ValueError('Ingen FIX-målinger å utjevne.')
    for group,values in groups.items():
        if len(values)<2: raise ValueError('Minst to selvstendige målinger kreves per kontrollpunkt i denne profilen: '+group)
    used=[o for name in sorted(groups) for o in groups[name]]
    origin=[round(v/1000)*1000 for v in used[0]['xyz']]
    lines=['# ABSOLUTE COORDINATE OBSERVATIONS - NOT MEASURED GNSS BASELINES',
        '# COORDORIGIN is a fixed computational translation, not a surveyed base.',
        'TITLE Survey Adjustment independent coordinate observations',
        'DIM 3','PREC 0.000001 M','CONV 15 ITER 0.000000001 CONV','CONFIDENCE 0.05','OUT NOAPV',
        'POS COORDORIGIN '+' '.join(f'{v:.9f}' for v in origin)+' M FIX']
    expected={}; identifiers={}; model_covariances={}; residuals=[]
    for index,(group,values) in enumerate(sorted(groups.items()),1):
        label=f'P{index:05d}'; identifiers[label]=group
        normal=[[0.]*3 for _ in range(3)]; rhs=[0.]*3
        for obs in values:
            inv=inverse_spd(obs['covariance_xyz'])
            delta=[v-o for v,o in zip(obs['xyz'],origin)]
            product=matvec(inv,delta)
            for i in range(3):
                rhs[i]+=product[i]
                for j in range(3): normal[i][j]+=inv[i][j]
        covmean=inverse_spd(normal)
        estimate=[a+b for a,b in zip(origin,matvec(covmean,rhs))]
        expected[label]=estimate; model_covariances[label]=covmean
        lines.append('POS '+label+' '+' '.join(f'{v:.9f}' for v in values[0]['xyz'])+' M EST')
        for obs in values:
            delta=[v-o for v,o in zip(obs['xyz'],origin)]; c=obs['covariance_xyz']
            upper=[c[0][0],c[0][1],c[0][2],c[1][1],c[1][2],c[2][2]]
            tag=f'O{len(residuals)+1:05d}'
            lines.append('DEL COORDORIGIN '+label+' '+' '.join(f'{v:.9f}' for v in delta)+' '+
                         ' '.join(f'{v:.16g}' for v in upper)+' M tag='+tag)
            residuals.append(dict(tag=tag,**obs,residual_xyz=[v-m for v,m in zip(obs['xyz'],estimate)]))
    warnings+=['Modellen antar uavhengige målinger og diagonal 1σ-kovarians i geodetisk ENU. Felles base-/datumfeil er ikke modellert.',
               'Ingen RINEX-prosessering, baselinjemåling, antennehøyde-, geoid-, datum- eller epoketransformasjon utføres.']
    if any(not o['time'] for o in used): warnings.append('Måletid mangler for én eller flere målinger; selvstendighet må dokumenteres utenfor CSV.')
    meta=dict(kind='coordinate_observations',synthetic=options.synthetic,frame=options.frame,frame_name=FRAMES[options.frame][0],height='ELLIP',
        epoch=options.epoch,expected_positions=expected,identifiers=identifiers,model_covariance_xyz=model_covariances,
        computational_origin_xyz=origin,observations=residuals,excluded=excluded,assumptions=warnings,
        uncertainty_source=options.uncertainty_source,correction_source=options.correction_source,antenna_note=options.antenna_note,
        mapping=dict(mapping),source={k:table[k] for k in ('path','sha256','encoding','delimiter')},
        options=asdict(options),
        independent_method='Vektet absolutt XYZ-middel med full rotert kovarians; bare den valgte lineære modellen kontrolleres.')
    return '\n'.join(lines)+'\n',meta


def prepare_coordinate(folder,table,mapping,options):
    data,meta=coordinate_job(table,mapping,options)
    raw=Path(table['path']).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=table['sha256']:
        raise ValueError('Kilde-CSV er endret etter innlesing. Åpne filen på nytt.')
    folder=Path(folder); folder.mkdir(parents=True,exist_ok=False)
    meta['input_dat_sha256']=hashlib.sha256(data.encode('ascii')).hexdigest()
    (folder/'input.dat').write_text(data,encoding='ascii',newline='\n')
    (folder/'source.csv').write_bytes(raw)
    (folder/'input_metadata.json').write_text(json.dumps(meta,ensure_ascii=False,indent=2),encoding='utf-8')
    return folder


def write_coordinate_results(folder,meta,positions):
    def cell(value):
        # Keep untrusted identifiers as text when CSV is opened in a spreadsheet.
        return "'"+value if isinstance(value,str) and value.lstrip().startswith(('=','+','-','@')) else value
    adjusted={name:llh(values,meta['frame']) for name,values in positions.items()}
    with (Path(folder)/'adjusted_coordinates.csv').open('w',encoding='utf-8-sig',newline='') as file:
        writer=csv.writer(file)
        writer.writerow(['Control ID','Latitude','Longitude','Ellipsoidal height','ECEF X','ECEF Y','ECEF Z','Geodetic CRS','Epoch','Assessment'])
        for name,values in positions.items():
            lat,lon,h=adjusted[name]
            writer.writerow([cell(meta['identifiers'][name]),f'{lat:.12f}',f'{lon:.12f}',f'{h:.6f}',*[f'{v:.6f}' for v in values],meta['frame'],meta['epoch'],'MÅ VURDERES'])
    with (Path(folder)/'coordinate_residuals.csv').open('w',encoding='utf-8-sig',newline='') as file:
        writer=csv.writer(file)
        writer.writerow(['Source row','Name','Control ID','Time','Residual X (m)','Residual Y (m)','Residual Z (m)'])
        for obs in meta['observations']:
            label=next(k for k,v in meta['identifiers'].items() if v==obs['group'])
            writer.writerow([obs['row'],cell(obs['name']),cell(obs['group']),cell(obs['time']),*[f'{v-m:.9f}' for v,m in zip(obs['xyz'],positions[label])]])
    return adjusted
