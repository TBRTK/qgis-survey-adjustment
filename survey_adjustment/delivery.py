# SPDX-License-Identifier: GPL-3.0-only
"""Local, explicit EUREF89 delivery snapshots. No frame/epoch inference or uploads."""
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from copy import deepcopy
import csv
import hashlib
import html
import io
import json
import math
import re

from . import network as n, network_quality as q, mixed_survey as m, engine
from .version import VERSION

REFERENCE_URL = 'https://www.kartverket.no/til-lands/posisjon/href-hoydereferansemodeller-og-transformasjonsmodeller'
MODEL_COMMIT = 'cdab44864a36a9f3c3e90a36637c5d8a86e624c6'
MODELS = {
    'NN2000:2018': ('no_kv_HREF2018B_NN2000_EUREF89.tif',
                    '112a42506e079a9c3f7a7a134729e4160494b101f5a9a8bcd6f5e99852b52275'),
    'NN2000:2025': ('no_kv_HREF2025A_NN2000_EUREF89.tif',
                    '6005c5bfb7c6b87e4dbb4c4f75fa589bd8fbceef71b711220bf173fab5fbb9f7'),
}
HEIGHTS = ('ELLIP', 'NN2000:2018', 'NN2000:2025', 'NN54', 'LOCAL')
OUTPUT_CRS = dict(n.CRS, **{f'EPSG:{5100+z}': f'EUREF89 / NTM {z}' for z in range(5,31)})
NOTICE = ('The software is provided "AS IS", without warranty. Disclaimers and limitations of liability '
          'apply only to the extent permitted by GPLv3 sections 15-17 and applicable law. '
          'This does not exclude statutory liability that cannot lawfully be excluded. See LICENSE.')
LIMITS = [
    'Reference declarations require external evidence; coordinates alone cannot establish frame, epoch or height datum.',
    'Same-frame EUREF89 projection and explicit HREF height conversion only. No ITRF/WGS84 frame shift, deformation or coordinate re-epoching.',
    'No delivery approval, digital signature or authenticity guarantee. Checksums detect changes relative to the supplied manifest.',
    'Original adjustment covariance is retained in the calculation report. Output-coordinate uncertainty is NOT propagated; HREF/model uncertainty is not inferred.',
    'HREF coverage is mainland Norway only, limited by the selected grid. No offshore/Svalbard extrapolation or missing-grid fallback.',
]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return (json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False)+'\n').encode('utf-8')


def _read(path, limit=50*1024*1024):
    path=Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size>limit:
        raise ValueError('Missing, linked or oversized local file: '+path.name)
    return path.read_bytes()


def _relative(name):
    if not isinstance(name,str) or not name or '\\' in name or ':' in name:
        raise ValueError('Unsafe package path.')
    parts=PurePosixPath(name).parts
    if PurePosixPath(name).is_absolute() or any(p in ('.','..') for p in parts) or str(PurePosixPath(name))!=name:
        raise ValueError('Unsafe package path.')
    return name


def _plain_file(path):
    path=Path(path)
    if any(p.is_symlink() for p in (path,*path.parents)):
        raise ValueError('Linked package paths are not accepted.')


def load_run(path, program):
    """Read and revalidate a completed SALSA run without executing or rewriting it."""
    path=Path(path)
    if path.name!='quality.json': raise ValueError('Select quality.json from a completed SALSA calculation folder.')
    folder=path.parent; _plain_file(folder)
    hashes=json.loads(_read(folder/'sha256.json',100000))
    if not isinstance(hashes,dict) or len(hashes)>20: raise ValueError('Invalid calculation checksum manifest.')
    allowed={'network.sanet.json','mixed.samix.json','source.csv','input.dat','input_metadata.json',
             'solution.out','solution.bin','console.txt','preflight.json','quality.json','quality.txt',
             'quality.html','points.csv','residuals.csv','checks.csv'}
    if not set(hashes)<=allowed: raise ValueError('Unexpected files in calculation manifest; only known run artifacts may be packaged.')
    required={'input.dat','input_metadata.json','solution.out','solution.bin','quality.json'}
    if not required<=set(hashes): raise ValueError('Incomplete calculation checksum manifest.')
    files={}
    for name,sha in hashes.items():
        _relative(name); data=_read(folder/name)
        if not isinstance(sha,str) or digest(data)!=sha: raise ValueError('Calculation file changed: '+name)
        files[name]=data
    if sum(len(v) for v in files.values())>100*1024*1024: raise ValueError('Calculation package exceeds the 100 MB profile limit.')
    saved=json.loads(files['quality.json'])
    if not saved.get('engine_confirmation'): raise ValueError('Delivery requires a completed, verified SALSA calculation, not a preview.')
    if saved.get('kind')=='mixed_survey':
        if not {'mixed.samix.json','source.csv'}<=set(files): raise ValueError('Missing mixed snapshot/source.')
        snapshot=m.loads(files['mixed.samix.json'].decode('utf-8')); project=snapshot['network']
        verified=m.inspect_result(folder,0,program,write=False)
    elif saved.get('kind')=='gnss_network_quality':
        if 'network.sanet.json' not in files: raise ValueError('Missing original network snapshot.')
        snapshot=n.loads_project(files['network.sanet.json'].decode('utf-8')); project=snapshot
        verified=q.inspect_network_result(folder,0,program,write=False)
    else: raise ValueError('This delivery workflow supports GNSS-network and mixed-survey calculation folders.')
    # Older runs may be reviewed by a newer plugin. Every result field except
    # the reporting software version must still reproduce exactly.
    if {k:v for k,v in saved.items() if k!='software_version'}!={k:v for k,v in verified.items() if k!='software_version'}:
        raise ValueError('Saved report differs from the reproducible calculation; do not package it.')
    for name,data in files.items():
        if _read(folder/name)!=data: raise ValueError('Calculation changed during verification: '+name)
    files['sha256.json']=_read(folder/'sha256.json',100000)
    return dict(project=project,report=saved,files=files,folder=folder,program=Path(program),
                identity=digest(files['quality.json']))


@dataclass
class ReferenceReview:
    source_height: str = ''
    target_height: str = ''
    target_crs: str = ''
    reviewer: str = ''
    recipient: str = ''
    purpose: str = ''
    frame_evidence: str = ''
    epoch_evidence: str = ''
    height_evidence: str = ''
    uncertainty_review: str = ''
    change_log: str = ''
    reviewed: bool = False


def validate_review(run, review):
    if not isinstance(review,ReferenceReview): raise ValueError('Expected a reference review.')
    if type(review.reviewed) is not bool or not review.reviewed: raise ValueError('Review and explicitly confirm the reference evidence before exporting.')
    for name in ('reviewer','recipient','purpose','frame_evidence','epoch_evidence','height_evidence','uncertainty_review','change_log'):
        n._text(getattr(review,name),name.replace('_',' ').title(),required=True)
    project=run['project']; n.validate_project(project)
    if not project['epoch']: raise ValueError('The calculation needs a documented coordinate epoch; observation date is not a substitute.')
    if review.source_height not in HEIGHTS or review.target_height not in HEIGHTS:
        raise ValueError('Choose explicit input/output height references; NN2000 needs its 2018/2025 version.')
    compatible=('NN2000:2018','NN2000:2025') if project['height_datum']=='NN2000' else (project['height_datum'],)
    if review.source_height not in compatible: raise ValueError('Input height declaration conflicts with the saved calculation.')
    if review.target_crs not in OUTPUT_CRS: raise ValueError('Choose a supported same-frame EUREF89 UTM/NTM projection.')
    if review.source_height!=review.target_height and any(h in ('NN54','LOCAL') for h in (review.source_height,review.target_height)):
        raise ValueError('NN54/LOCAL height changes are not supported in this profile. Retain the original datum.')
    sessions=[]
    for session in project['sessions']:
        start=n._time(session['start'],session['id']+' start')
        end=n._time(session['end'],session['id']+' end')
        if start is None: raise ValueError('Document acquisition-session times before delivery: '+session['id'])
        sessions.append(dict(id=session['id'],start_utc=start.astimezone(timezone.utc).isoformat(),
                             end_utc=end.astimezone(timezone.utc).isoformat() if end else None))
    if not sessions: raise ValueError('No documented acquisition sessions.')
    return sessions


def projection(crs):
    """Explicit projection formula, never a datum-operation selection."""
    if crs not in OUTPUT_CRS: raise ValueError('Unsupported EUREF89 projection.')
    code=int(crs.split(':')[1])
    if 25831<=code<=25836: return f'+proj=utm +zone={code-25800} +ellps=GRS80 +units=m'
    zone=code-5100
    return f'+proj=tmerc +lat_0=58 +lon_0={zone+.5} +k=1 +x_0=100000 +y_0=1000000 +ellps=GRS80 +units=m'


def model(path, height):
    if height not in MODELS: raise ValueError('No HREF model is defined for this height reference.')
    name,sha=MODELS[height]; path=Path(path)
    if digest(_read(path,2000000))!=sha:
        raise ValueError('HREF bytes do not match the reviewed official '+height+' GeoTIFF. A filename alone does not identify a model.')
    return dict(height=height,name=name,sha256=sha,path=path,
                source=f'https://raw.githubusercontent.com/OSGeo/PROJ-data/{MODEL_COMMIT}/no_kv/{name}',
                attribution='Kartverket (Norwegian Mapping Authority); GeoTIFF via OSGeo PROJ-data; CC BY 4.0',
                license='https://creativecommons.org/licenses/by/4.0/')


def _grid_offset(info,lon,lat):
    """Independent bilinear readback; reject every missing corner and edge extrapolation."""
    from osgeo import gdal
    ds=gdal.Open(str(info['path']),gdal.GA_ReadOnly)
    if ds is None: raise ValueError('Cannot read reviewed HREF grid.')
    gt=ds.GetGeoTransform()
    if gt[2]!=0 or gt[4]!=0: raise ValueError('Unsupported rotated HREF grid.')
    px=(lon-gt[0])/gt[1]-.5; py=(lat-gt[3])/gt[5]-.5
    x,y=math.floor(px),math.floor(py)
    if not (0<=x<ds.RasterXSize-1 and 0<=y<ds.RasterYSize-1): raise ValueError('Point outside HREF interpolation coverage.')
    values=ds.GetRasterBand(1).ReadAsArray(x,y,2,2).astype(float)
    nodata=ds.GetRasterBand(1).GetNoDataValue()
    if not all(math.isfinite(float(v)) and (nodata is None or math.isnan(nodata) or v!=nodata) for v in values.flat):
        raise ValueError('Point intersects a HREF no-data area. No extrapolation or fallback is allowed.')
    fx,fy=px-x,py-y
    return float((1-fy)*((1-fx)*values[0,0]+fx*values[0,1])+fy*((1-fx)*values[1,0]+fx*values[1,1]))


class CoordinateOperation:
    def __init__(self, source_crs, target_crs, source_height, target_height, paths):
        from pyproj import Transformer
        self.source_crs,self.target_crs=source_crs,target_crs
        self.source_height,self.target_height=source_height,target_height
        self.models={}; self.steps=[]
        for height in (source_height,target_height):
            if source_height!=target_height and height in MODELS and height not in self.models:
                if not paths.get(height): raise ValueError('Select the reviewed local HREF grid for '+height)
                self.models[height]=model(paths[height],height)
        self.to_geographic=Transformer.from_pipeline('+proj=pipeline +step +inv '+projection(source_crs)+
                                                     ' +step +proj=unitconvert +xy_in=rad +xy_out=deg')
        self.from_geographic=Transformer.from_pipeline('+proj=pipeline +step +proj=unitconvert +xy_in=deg +xy_out=rad +step '+projection(target_crs))
        self.grid_operations={}
        for height,info in self.models.items():
            path=info['path'].resolve().as_posix()
            if any(c in path for c in ('"','\n','\r')): raise ValueError('Unsupported model path characters.')
            pipeline=f'+proj=pipeline +step +proj=unitconvert +xy_in=deg +xy_out=rad +step +proj=vgridshift +grids="{path}" +multiplier=1 +step +proj=unitconvert +xy_in=rad +xy_out=deg'
            self.grid_operations[height]=Transformer.from_pipeline(pipeline)
            self.steps.append(dict(height=height,operation='Mandatory local vgridshift; h = H + N; H = h - N',
                                   grid=info['name'],sha256=info['sha256']))

    def transform(self, values):
        n._numbers(list(values),3,'Point coordinates')
        east,north,z=values; lon,lat=self.to_geographic.transform(east,north,errcheck=True)
        if not (4<=lon<=32 and 57<=lat<=72): raise ValueError('Point outside this mainland Norway delivery profile; check E/N order, units and projection.')
        separation={}
        for height,operation in self.grid_operations.items():
            offset=operation.transform(lon,lat,0.,errcheck=True)[2]
            check=_grid_offset(self.models[height],lon,lat)
            if not math.isfinite(offset) or abs(offset-check)>2e-6:
                raise ValueError('PROJ grid shift disagrees with independent HREF raster interpolation.')
            separation[height]=offset
        output_z=z
        if self.source_height!=self.target_height:
            output_z=z+separation.get(self.source_height,0.)-separation.get(self.target_height,0.)
        if self.source_crs==self.target_crs: output_e,output_n=east,north
        else: output_e,output_n=self.from_geographic.transform(lon,lat,errcheck=True)
        result=[float(output_e),float(output_n),float(output_z)]
        if not all(math.isfinite(v) for v in result): raise ValueError('Non-finite delivered coordinate.')
        return result,separation

    def metadata(self):
        return dict(horizontal='Explicit GRS80 inverse/forward projection; no frame shift',
                    source_projection=projection(self.source_crs),target_projection=projection(self.target_crs),
                    height_steps=self.steps,epoch_operation='NONE - coordinate epoch retained',fallback='NONE')


def preview(run, review, paths):
    sessions=validate_review(run,review)
    operation=CoordinateOperation(run['project']['crs'],review.target_crs,review.source_height,review.target_height,paths)
    records=[]; source={p['id']:p for p in run['project']['points']}
    for point in run['report']['points']:
        adjusted=point['position_grid']
        converted,offsets=operation.transform(adjusted) if adjusted is not None else (None,{})
        records.append(dict(id=point['id'],role=point['role'],source=source[point['id']]['coordinates'],
                            adjusted=deepcopy(adjusted),delivered=converted,separations_m=offsets,
                            source_crs=run['project']['crs'],target_crs=review.target_crs,
                            source_height=review.source_height,target_height=review.target_height,
                            coordinate_epoch=run['project']['epoch'],synthetic=run['project']['synthetic'],
                            output_uncertainty='NOT_PROPAGATED - see original calculation covariance'))
    if not any(r['delivered'] for r in records): raise ValueError('The calculation has no adjusted coordinates to deliver.')
    notes=list(LIMITS)
    if 'NN2000:2018' in (review.source_height,review.target_height):
        notes.append('HREF2018B / NN2000:2018 was explicitly selected. HREF2025A became official on 2026-10-14; keep project model continuity and document the recipient agreement.')
    if review.source_height==review.target_height: notes.append('Heights are retained exactly; no HREF is applied.')
    return dict(records=records,sessions=sessions,operation=operation.metadata(),warnings=notes),operation


def _csv(records):
    buf=io.StringIO(newline=''); writer=csv.writer(buf)
    writer.writerow(['ID','Role','Source E','Source N','Source Z','Adjusted E','Adjusted N','Adjusted Z',
                     'Delivered E','Delivered N','Delivered Z','Input CRS','Input height reference','Output CRS',
                     'Output height reference','Coordinate epoch','Synthetic','Output uncertainty'])
    for row in records:
        safe=lambda s:"'"+s if s.startswith(('=','+','-','@')) else s
        writer.writerow([safe(row['id']),row['role'],*(row['source'] or [None]*3),
                         *(row['adjusted'] or [None]*3),*(row['delivered'] or [None]*3),
                         row['source_crs'],row['source_height'],row['target_crs'],row['target_height'],
                         row['coordinate_epoch'],row['synthetic'],row['output_uncertainty']])
    return b'\xef\xbb\xbf'+buf.getvalue().encode('utf-8')


def _spatial_reference(crs,height):
    from pyproj import CRS
    from pyproj.crs import CompoundCRS
    from osgeo import osr
    horizontal=CRS(crs)
    if height=='ELLIP': definition=horizontal.to_3d().to_wkt()
    elif height in ('NN2000:2018','NN2000:2025','NN54'):
        if height=='NN2000:2025':
            # Explicit EPSG 11394 definition for QGIS installations with an older
            # proj.db. This defines metadata only; mandatory grid bytes determine
            # the actual vertical operation. No substitute datum is selected.
            vertical=CRS.from_wkt('VERTCRS["NN2000:2025 height",VDATUM["Norway Normal Null 2000:2025",ID["EPSG",1454]],'
                                 'CS[vertical,1],AXIS["gravity-related height (H)",up],LENGTHUNIT["metre",1],ID["EPSG",11394]]')
        else: vertical=CRS({'NN2000:2018':5941,'NN54':5776}[height])
        definition=CompoundCRS(crs+' + '+height,[horizontal,vertical]).to_wkt()
    else: definition=horizontal.to_wkt()  # LOCAL height is explicit in attributes, never a false EPSG height.
    ref=osr.SpatialReference(); ref.ImportFromWkt(definition); ref.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return ref


def write_geopackage(path, records, source_crs, review):
    from osgeo import ogr
    definitions=[(key,name,crs,height,_spatial_reference(crs,height)) for key,name,crs,height in
                 (('source','source_points',source_crs,review.source_height),
                  ('adjusted','adjusted_points',source_crs,review.source_height),
                  ('delivered','delivery_points',review.target_crs,review.target_height))]
    ds=ogr.GetDriverByName('GPKG').CreateDataSource(str(path))
    if ds is None: raise ValueError('Cannot create new delivery GeoPackage.')
    try:
        for key,name,crs,height,ref in definitions:
            layer=ds.CreateLayer(name,ref,ogr.wkbPoint25D)
            if layer is None: raise ValueError('Cannot create PointZ layer.')
            for field in ('point_id','role','height_reference','horizontal_crs','coordinate_epoch','synthetic','uncertainty'):
                layer.CreateField(ogr.FieldDefn(field,ogr.OFTString))
            for record in records:
                if record[key] is None: continue
                feature=ogr.Feature(layer.GetLayerDefn())
                for field,value in zip(('point_id','role','height_reference','horizontal_crs','coordinate_epoch','synthetic','uncertainty'),
                                       (record['id'],record['role'],height,crs,record['coordinate_epoch'],str(record['synthetic']),record['output_uncertainty'])):
                    feature.SetField(field,value)
                geometry=ogr.Geometry(ogr.wkbPoint25D); geometry.AddPoint(*record[key]); feature.SetGeometry(geometry)
                if layer.CreateFeature(feature)!=0: raise ValueError('PointZ write failed.')
            feature=None; layer=None
    finally:
        feature=None; layer=None; ds=None
    verify_geopackage(path,records)


def verify_geopackage(path,records):
    from osgeo import ogr
    ds=ogr.Open(str(path),0)
    if ds is None: raise ValueError('Cannot read delivery GeoPackage back.')
    try:
        for key,name in (('source','source_points'),('adjusted','adjusted_points'),('delivered','delivery_points')):
            layer=ds.GetLayerByName(name); expected={r['id']:r for r in records if r[key] is not None}
            if layer is None or layer.GetFeatureCount()!=len(expected): raise ValueError('GeoPackage readback count differs.')
            seen=set()
            for f in layer:
                name=f.GetField('point_id'); geom=f.GetGeometryRef()
                if name not in expected or name in seen or geom is None or not geom.Is3D(): raise ValueError('Invalid PointZ readback.')
                row=expected[name]; prefix='target' if key=='delivered' else 'source'
                if list(geom.GetPoint())!=row[key]: raise ValueError('GeoPackage coordinates/heights changed during write/readback.')
                attrs={'role':row['role'],'height_reference':row[prefix+'_height'],'horizontal_crs':row[prefix+'_crs'],
                       'coordinate_epoch':row['coordinate_epoch'],'synthetic':str(row['synthetic']),'uncertainty':row['output_uncertainty']}
                if any(f.GetField(field)!=value for field,value in attrs.items()): raise ValueError('GeoPackage reference/epoch attributes differ.')
                expected_ref=_spatial_reference(row[prefix+'_crs'],row[prefix+'_height'])
                if not layer.GetSpatialRef() or not layer.GetSpatialRef().IsSame(expected_ref): raise ValueError('GeoPackage horizontal/vertical CRS differs.')
                seen.add(name)
            f=None; layer=None
    finally:
        f=None; layer=None; ds=None


def export_package(parent, run, review, paths, expected_preview=None):
    # Re-read/check every run byte immediately before freezing the local package.
    fresh=load_run(run['folder']/'quality.json',run['program'])
    if fresh['files']!=run['files']: raise ValueError('Run changed since the review opened. Reopen it.')
    result,operation=preview(fresh,review,paths)
    if expected_preview is not None and result!=expected_preview: raise ValueError('Preview is stale; review current settings and model files again.')
    import pyproj
    from osgeo import gdal
    parent=Path(parent); _plain_file(parent)
    if not parent.is_dir(): raise ValueError('Select an existing local parent folder.')
    tag=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    folder=parent/('Survey_Delivery_'+tag+'.incomplete'); folder.mkdir(exist_ok=False)
    (folder/'calculation').mkdir()
    for name,data in fresh['files'].items(): (folder/'calculation'/name).write_bytes(data)
    models=[]
    for info in operation.models.values():
        data=_read(info['path'],2000000)
        if digest(data)!=info['sha256']: raise ValueError('HREF model changed during export.')
        (folder/'models').mkdir(exist_ok=True); (folder/'models'/info['name']).write_bytes(data)
        models.append({k:v for k,v in info.items() if k!='path'})
    document=dict(format='survey-adjustment-delivery',schema_version=1,software_version=VERSION,
        created_utc=datetime.now(timezone.utc).isoformat(),synthetic=fresh['project']['synthetic'],
        calculation_software_version=fresh['report']['software_version'],calculation_report_sha256=fresh['identity'],
        coordinate_epoch=fresh['project']['epoch'],acquisition_sessions=result['sessions'],reference_review=asdict(review),
        source_crs=fresh['project']['crs'],operation=result['operation'],models=models,records=result['records'],
        warnings=result['warnings'],runtime=dict(pyproj=pyproj.__version__,proj=pyproj.proj_version_str,gdal=gdal.VersionInfo()),
        notice=NOTICE,reference_guidance=REFERENCE_URL)
    (folder/'delivery.json').write_bytes(encoded(document)); (folder/'coordinates.csv').write_bytes(_csv(result['records']))
    write_geopackage(folder/'coordinates.gpkg',result['records'],fresh['project']['crs'],review)
    text='Survey Adjustment '+VERSION+' - LOCAL DELIVERY REVIEW\n'+('SYNTHETIC TRAINING DATA' if document['synthetic'] else 'PRIVATE MEASUREMENTS')+'\n\n'
    text+='Reference declarations, acquisition times, changes, source/adjusted/delivered coordinates and operations: delivery.json\n'
    text+='Complete original calculation, options, observations, exclusions, covariances and engine evidence: calculation/\n'
    text+='Reviewed HREF files, when used: models/ (Kartverket; CC BY 4.0; sources/hashes in delivery.json)\n\n'
    text+='\n'.join(k.replace('_',' ').title()+': '+str(v) for k,v in asdict(review).items())+'\n\n'
    text+='Coordinate epoch: '+document['coordinate_epoch']+' (no coordinate re-epoching)\n\n'+'\n'.join(result['warnings'])+'\n\n'+NOTICE+'\n'
    (folder/'delivery.txt').write_text(text,encoding='utf-8')
    (folder/'delivery.html').write_text('<!doctype html><meta charset="utf-8"><title>Survey Adjustment delivery</title><pre style="white-space:pre-wrap;font:15px sans-serif">'+html.escape(text)+'</pre>',encoding='utf-8')
    for name in ('LICENSE','NOTICE.txt','DISCLAIMER.txt'):
        source=Path(__file__).parent/name
        if not source.exists(): source=Path(__file__).parent.parent/name
        (folder/name).write_bytes(_read(source))
    hashes={p.relative_to(folder).as_posix():digest(p.read_bytes()) for p in sorted(folder.rglob('*')) if p.is_file()}
    (folder/'checksums.json').write_bytes(encoded(hashes))
    verify_package(folder)
    # Original run/model files must still match; do not accept a mixed-time snapshot.
    for name,data in fresh['files'].items():
        if _read(fresh['folder']/name)!=data: raise ValueError('Calculation changed during delivery export.')
    final=parent/('Survey_Delivery_'+tag); folder.rename(final)
    return final


def verify_package(folder):
    folder=Path(folder); _plain_file(folder)
    hashes=json.loads(_read(folder/'checksums.json',200000))
    if not isinstance(hashes,dict) or not 8<=len(hashes)<=40: raise ValueError('Invalid delivery manifest.')
    required={'delivery.json','coordinates.csv','coordinates.gpkg','delivery.txt','delivery.html','LICENSE','NOTICE.txt','DISCLAIMER.txt'}
    if not required<=set(hashes): raise ValueError('Delivery is incomplete.')
    for name,sha in hashes.items():
        _relative(name); _plain_file(folder/name)
        if not isinstance(sha,str) or not re.fullmatch('[0-9a-f]{64}',sha) or digest(_read(folder/name))!=sha:
            raise ValueError('Delivery checksum failed: '+name)
    actual={p.relative_to(folder).as_posix() for p in folder.rglob('*') if p.is_file()}
    if actual!=set(hashes)|{'checksums.json'}: raise ValueError('Delivery contains unmanifested or missing files.')
    doc=json.loads(_read(folder/'delivery.json'))
    if doc.get('format')!='survey-adjustment-delivery' or doc.get('schema_version')!=1: raise ValueError('Unsupported delivery format.')
    if _read(folder/'coordinates.csv')!=_csv(doc['records']): raise ValueError('Delivery CSV differs from recorded coordinates.')
    if digest(_read(folder/'calculation/quality.json'))!=doc['calculation_report_sha256']: raise ValueError('Calculation identity differs.')
    report=json.loads(_read(folder/'calculation/quality.json'))
    if report.get('kind')=='mixed_survey': project=m.loads(_read(folder/'calculation/mixed.samix.json').decode('utf-8'))['network']
    elif report.get('kind')=='gnss_network_quality': project=n.loads_project(_read(folder/'calculation/network.sanet.json').decode('utf-8'))
    else: raise ValueError('Unsupported packaged calculation.')
    if doc['coordinate_epoch']!=project['epoch'] or doc['source_crs']!=project['crs'] or doc['synthetic']!=project['synthetic']:
        raise ValueError('Delivery reference identity differs from the original calculation.')
    paths={}
    for info in doc['models']:
        if info['height'] not in MODELS or info['name']!=MODELS[info['height']][0] or info['sha256']!=MODELS[info['height']][1]:
            raise ValueError('Unreviewed model identity in delivery.')
        paths[info['height']]=folder/'models'/info['name']
        model(paths[info['height']],info['height'])
    reproduced,_=preview(dict(project=project,report=report),ReferenceReview(**doc['reference_review']),paths)
    if any(reproduced[key]!=doc[key] for key in ('records',)) or reproduced['sessions']!=doc['acquisition_sessions'] or reproduced['operation']!=doc['operation']:
        raise ValueError('Delivered coordinates/references do not reproduce the recorded operation.')
    verify_geopackage(folder/'coordinates.gpkg',doc['records'])
    return dict(status='INTEGRITY_AND_POINTZ_READBACK_PASSED',files=len(hashes),synthetic=doc['synthetic'],
                warning='Not a digital signature, authenticity proof or survey approval.')
