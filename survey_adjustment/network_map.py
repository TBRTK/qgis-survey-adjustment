# SPDX-License-Identifier: GPL-3.0-only
"""Local GNSS snapshot maps. Display enlargement never changes measured values."""
from dataclasses import dataclass,asdict
from datetime import datetime,timezone
from pathlib import Path
import hashlib
import json
import math

import numpy as np
from scipy.stats import chi2
from qgis.core import (QgsFeature,QgsGeometry,QgsPoint,QgsPointXY,QgsLineString,QgsPolygon,
    QgsVectorLayer,QgsCategorizedSymbolRenderer,QgsRendererCategory,QgsMarkerSymbol,
    QgsLineSymbol,QgsFillSymbol,QgsPalLayerSettings,QgsVectorLayerSimpleLabeling,
    QgsTextFormat,QgsTextBufferSettings,QgsVectorFileWriter,QgsProject,QgsRectangle,Qgis,
    QgsArrowSymbolLayer)
from qgis.PyQt.QtCore import QMetaType
from qgis.PyQt.QtGui import QColor,QFont

from . import network as n,network_quality as q,engine
from .coordinate_job import llh
from . import baselines as b
from .layers import make_layer
from .version import VERSION


@dataclass(frozen=True)
class MapOptions:
    ellipse_factor: float = 500.
    vector_factor: float = 500.


def checked_options(options):
    if not isinstance(options,MapOptions): raise ValueError('Expected map display options.')
    for value in asdict(options).values():
        if type(value) not in (int,float) or not math.isfinite(value) or not 1<=value<=100000:
            raise ValueError('Display enlargement must be between 1 and 100000.')
    return options


def stage(report):
    return 'SALSA VERIFIED' if report.get('engine_confirmation') else 'PREVIEW - SALSA NOT RUN'


def validate_snapshot(project,report):
    n.validate_project(project)
    digest=hashlib.sha256(n.dumps_project(project).encode('utf-8')).hexdigest()
    if (report.get('kind')!='gnss_network_quality' or report.get('project_sha256')!=digest or
        report.get('project_id')!=project['project_id'] or report.get('point_crs')!=project['crs'] or
        report.get('height')!='ELLIP' or report.get('epoch')!=project['epoch']):
        raise ValueError('Map result and network snapshot do not match. Calculate again.')
    expected=q.analyse(project,q.QualityOptions(**report['options']))
    # Results may come from preview or a verified run; numerical contents must
    # reproduce the model before they are displayed as derived coordinates.
    for key in ('points','observations','checks','positions_ecef','parameter_covariance_xyz'):
        if expected[key]!=report[key]: raise ValueError('Map result differs from its recorded network model.')


def grid_covariance(grid,position,covariance):
    """ECEF covariance -> project grid E/N/H; includes convergence and scale."""
    values=np.array(position,dtype=float); columns=[]
    for axis in range(3):
        offset=np.zeros(3); offset[axis]=1.
        columns.append((np.array(grid.grid(values+offset))-np.array(grid.grid(values-offset)))/2.)
    jacobian=np.column_stack(columns)
    result=jacobian@np.array(covariance,dtype=float)@jacobian.T
    return (result+result.T)/2.


def ellipse_parameters(covariance):
    """95% horizontal marginal ellipse, angle clockwise from GRID north."""
    horizontal=np.array(covariance,dtype=float)[:2,:2]
    values,vectors=np.linalg.eigh((horizontal+horizontal.T)/2.)
    if not np.isfinite(values).all() or values.min() < -max(1e-16,float(values.max())*1e-10):
        raise ValueError('Invalid horizontal covariance for map ellipse.')
    order=np.argsort(values)[::-1]; values=np.maximum(values[order],0.); vectors=vectors[:,order]
    axes=np.sqrt(values*chi2.ppf(.95,2))
    angle=math.degrees(math.atan2(vectors[0,0],vectors[1,0]))%180.
    return axes.tolist(),angle,vectors


def ellipse_geometry(center,axes,vectors,factor,segments=96):
    vertices=[]
    for index in range(segments):
        theta=2*math.pi*index/segments
        delta=np.array(vectors)@(np.array(axes)*[math.cos(theta),math.sin(theta)])*factor
        vertices.append(QgsPoint(center[0]+float(delta[0]),center[1]+float(delta[1]),center[2]))
    vertices.append(QgsPoint(vertices[0].x(),vertices[0].y(),vertices[0].z()))
    polygon=QgsPolygon(); polygon.setExteriorRing(QgsLineString(vertices))
    return QgsGeometry(polygon)


def positions(project,report):
    fitted={p['id']:p['position_grid'] for p in report['points'] if p['position_grid'] is not None}
    return {p['id']:fitted.get(p['id'],p['coordinates']) for p in project['points']}


def _style(layer,field,kind,categories):
    types={'point':QgsMarkerSymbol,'line':QgsLineSymbol,'polygon':QgsFillSymbol}
    layer.setRenderer(QgsCategorizedSymbolRenderer(field,[QgsRendererCategory(key,types[kind].createSimple(properties),label)
        for key,label,properties in categories]))


def _labels(layer,expression):
    labels=QgsPalLayerSettings(); labels.fieldName=expression; labels.isExpression=True
    if layer.geometryType()==Qgis.GeometryType.Line: labels.placement=Qgis.LabelPlacement.Line
    text=QgsTextFormat(); text.setFont(QFont('Segoe UI',9)); text.setSize(9); text.setColor(QColor('#21334a'))
    buffer=QgsTextBufferSettings(); buffer.setEnabled(True); buffer.setSize(.8); buffer.setColor(QColor('white'))
    text.setBuffer(buffer); labels.setFormat(text)
    layer.setLabeling(QgsVectorLayerSimpleLabeling(labels)); layer.setLabelsEnabled(True)


def _arrow(color):
    symbol=QgsLineSymbol()
    arrow=QgsArrowSymbolLayer.create({'arrow_width':'0.35','arrow_start_width':'0.35',
        'head_length':'1.8','head_thickness':'1.1','is_curved':'0','is_repeated':'0'})
    arrow.setSubSymbol(QgsFillSymbol.createSimple({'color':color,'outline_style':'no'}))
    symbol.changeSymbolLayer(0,arrow)
    return symbol


def make_map_layers(project,report,options=MapOptions(),comparison=None):
    checked_options(options); validate_snapshot(project,report)
    if comparison is not None: check_comparison(project,report,comparison)
    text,number=QMetaType.Type.QString,QMetaType.Type.Double
    prefix='SYNTHETIC - ' if project['synthetic'] else 'GNSS - '
    meta=[('stage',text),('snapshot_sha256',text)]
    tail=[stage(report),report['project_sha256']]
    point=lambda coordinates:QgsGeometry(QgsPoint(*coordinates))
    line=lambda start,end:QgsGeometry(QgsLineString([QgsPoint(*start),QgsPoint(*end)]))
    grid=q.GridCoordinates(project['crs']); located=positions(project,report)
    results={p['id']:p for p in report['points']}; diagnostics={o['id']:o for o in report['observations'] if o['kind']=='GNSS_VECTOR'}
    checks={o['id']:o for o in report['checks']}
    point_rows=[]; source_rows=[]; ellipse_rows=[]; changes=[]; residuals=[]; edges=[]; missing=[]
    for original in project['points']:
        name=original['id']; result=results[name]; center=located[name]
        if center is None:
            missing.append('Point '+name+': no documented or estimated coordinate.'); continue
        sigma=result['sigma_enu_m']; axes=result['horizontal_95_axes_m']
        point_rows.append(([name,name,original['role'],*(sigma or [None]*3),axes[0] if axes else None,
            result['vertical_95_halfwidth_m'],', '.join(result['flags'])]+tail,point(center)))
        if original['coordinates'] is not None:
            source_rows.append(([name,name,original['role'],'Original coordinate; may be an initial approximation.']+tail,point(original['coordinates'])))
        if result['position_grid'] is None or original['role']=='FIXED': continue
        covariance=grid_covariance(grid,result['position_ecef'],result['covariance_xyz'])
        ellipse_axes,angle,vectors=ellipse_parameters(covariance)
        if ellipse_axes[0]>0:
            review=any(flag in result['flags'] for flag in ('WEAK_CONTROL','USER_HORIZONTAL_LIMIT','USER_VERTICAL_LIMIT'))
            ellipse_rows.append(([name,name,*ellipse_axes,angle,sigma[2],options.ellipse_factor,
                'DISPLAY ONLY - horizontal 95% marginal ellipse','REVIEW' if review else 'MODEL',', '.join(result['flags'])]+tail,
                ellipse_geometry(center,ellipse_axes,vectors,options.ellipse_factor)))
        if original['coordinates'] is not None:
            start=original['coordinates']; delta=[a-c for a,c in zip(center,start)]
            if math.dist(center,start)>1e-10:
                end=[start[0]+delta[0]*options.vector_factor,start[1]+delta[1]*options.vector_factor,start[2]]
                changes.append(([name,name,*delta,options.vector_factor,'DISPLAY ONLY - original/approximate to adjusted; Z held at start']+tail,line(start,end)))
    for observation in project['observations']:
        name=observation['id']; a,z=observation['from_point'],observation['to_point']
        start,end=located[a],located[z]
        if start is None or end is None:
            missing.append('Observation '+name+': at least one endpoint has no coordinate.'); continue
        diagnostic=diagnostics.get(name); check=checks.get(name)
        if not observation['enabled']: status='DISABLED'
        elif observation['use']=='CHECK': status='CHECK_REVIEW' if check and check['status']!='WITHIN_MODEL_TEST' else 'CHECK'
        elif diagnostic and 'RESIDUAL_REVIEW' in diagnostic['flags']: status='RESIDUAL_REVIEW'
        elif diagnostic and diagnostic['flags']: status='WEAK_CONTROL'
        else: status='ADJUSTMENT'
        edges.append(([name,name,a,z,observation['session'],observation['kind'],status,
            diagnostic['redundancy_fraction'] if diagnostic else None,
            ', '.join(diagnostic['flags']) if diagnostic else (check['status'] if check else observation['notes']),
            json.dumps(observation['values']),json.dumps(observation['covariance'])]+tail,line(start,end)))
        if diagnostic or (check and check['residual_xyz_m'] is not None and observation['kind']=='GNSS_VECTOR'):
            vector=(diagnostic or check)['residual_xyz_m']
            destination=np.array(report['positions_ecef'][z]) if z in report['positions_ecef'] else grid.ecef(end)
            delta=np.array(grid.grid(destination+np.array(vector)))-np.array(end)
            drawn=[end[0]+float(delta[0])*options.vector_factor,end[1]+float(delta[1])*options.vector_factor,end[2]]
            residuals.append(([name,name,a,z,*map(float,delta),*vector,status,options.vector_factor,
                'DISPLAY ONLY - observed minus calculated at target; Z held at target']+tail,line(end,drawn)))
    common=[('record_id',text),('point_id',text)]
    layers={
      'points':make_layer('PointZ',prefix+'adjusted / reference points (ELLIP)',project['crs'],common+
          [('role',text),('sigma_e_m',number),('sigma_n_m',number),('sigma_u_m',number),('h95_enu_major_m',number),('v95_halfwidth_m',number),('review',text)]+meta,point_rows),
      'sources':make_layer('PointZ',prefix+'original / approximate coordinates',project['crs'],common+[('role',text),('meaning',text)]+meta,source_rows),
      'baselines':make_layer('LineStringZ',prefix+'network connections (actual endpoints)',project['crs'],[('record_id',text),('observation_id',text),('from_point',text),('to_point',text),('session',text),('kind',text),('status',text),('redundancy_fraction',number),('review',text),('observed_values',text),('covariance',text)]+meta,edges),
      'ellipses':make_layer('PolygonZ',prefix+f'horizontal 95% ellipses x{options.ellipse_factor:g} (DISPLAY ONLY)',project['crs'],common+[('h95_grid_major_m',number),('h95_grid_minor_m',number),('grid_azimuth_deg',number),('sigma_u_m',number),('display_factor',number),('meaning',text),('status',text),('review',text)]+meta,ellipse_rows),
      'changes':make_layer('LineStringZ',prefix+f'coordinate changes x{options.vector_factor:g} (DISPLAY ONLY)',project['crs'],common+[('dE_m',number),('dN_m',number),('dH_m',number),('display_factor',number),('meaning',text)]+meta,changes),
      'residuals':make_layer('LineStringZ',prefix+f'residuals x{options.vector_factor:g} (DISPLAY ONLY)',project['crs'],[('record_id',text),('observation_id',text),('from_point',text),('to_point',text),('dE_m',number),('dN_m',number),('dH_m',number),('dX_m',number),('dY_m',number),('dZ_m',number),('status',text),('display_factor',number),('meaning',text)]+meta,residuals)}
    for key,layer in layers.items():
        layer.setCustomProperty('survey_adjustment/map_key',key)
        layer.setCustomProperty('survey_adjustment/snapshot_sha256',report['project_sha256'])
        layer.setCustomProperty('survey_adjustment/stage',stage(report))
        layer.setCustomProperty('survey_adjustment/display_options',json.dumps(asdict(options)))
    _style(layers['points'],'role','point',[
        ('FIXED','Fixed (held exact by model)',{'name':'triangle','color':'#253b52','size':'4.2'}),
        ('CONSTRAINED','Known with uncertainty',{'name':'diamond','color':'#16847e','size':'4'}),
        ('UNKNOWN','Adjusted point',{'name':'circle','color':'#1675bf','size':'3.5'}),
        ('CHECK','Independent check reference',{'name':'cross2','color':'#8f52ba','size':'4'})])
    layers['sources'].renderer().setSymbol(QgsMarkerSymbol.createSimple({'name':'circle','color':'255,255,255,0','outline_color':'#8b98a8','size':'4.7','outline_width':'.3'}))
    colors=[('ADJUSTMENT','Adjustment','#39769d','solid'),('WEAK_CONTROL','Weak internal control','#d68a13','solid'),
        ('RESIDUAL_REVIEW','Residual review','#d33f49','solid'),('CHECK','Independent check','#8f52ba','dash'),
        ('CHECK_REVIEW','Check review','#c747a5','dash'),('DISABLED','Disabled / excluded','#9aa2aa','dot')]
    for key in ('baselines','residuals'):
        _style(layers[key],'status','line',[(value,label,{'line_color':color,'line_width':'.65' if key=='residuals' else '.45','line_style':style}) for value,label,color,style in colors])
    layers['residuals'].setRenderer(QgsCategorizedSymbolRenderer('status',[
        QgsRendererCategory(value,_arrow(color),label) for value,label,color,_ in colors]))
    _style(layers['ellipses'],'status','polygon',[
        ('MODEL','Conditional 95% ellipse',{'color':'42,143,176,24','outline_color':'#238da9','outline_width':'.45'}),
        ('REVIEW','Uncertainty / control review',{'color':'214,138,19,35','outline_color':'#ce851a','outline_width':'.65'})])
    layers['changes'].renderer().setSymbol(_arrow('#d27a25'))
    _labels(layers['points'],'''"record_id" || CASE WHEN "role" = 'FIXED' THEN '\\nheld exact' WHEN "role" = 'CHECK' THEN '\\ncheck only' WHEN "sigma_u_m" IS NOT NULL THEN '\\nσU ' || format_number(1000 * "sigma_u_m",1) || ' mm' ELSE '\\nnot estimated' END''')
    _labels(layers['baselines'],'"record_id"')
    if comparison is not None:
        rows=compare_points(project,report,comparison)
        entries=[]
        for row in rows:
            if row['delta_grid_m'] is None: continue
            old=row['comparison_grid']; delta=row['delta_grid_m']
            if math.dist(row['current_grid'],old)<1e-10: continue
            end=[old[0]+delta[0]*options.vector_factor,old[1]+delta[1]*options.vector_factor,old[2]]
            entries.append(([row['id'],row['id'],*delta,options.vector_factor,
                'DISPLAY ONLY - current minus comparison; significance not inferred']+tail,line(old,end)))
        layers['comparison']=make_layer('LineStringZ',prefix+f'run differences x{options.vector_factor:g} (DISPLAY ONLY)',project['crs'],common+[('dE_m',number),('dN_m',number),('dH_m',number),('display_factor',number),('meaning',text)]+meta,entries)
        layers['comparison'].renderer().setSymbol(_arrow('#b13aaa'))
        layers['comparison'].setCustomProperty('survey_adjustment/comparison_sha256',comparison['project_sha256'])
    return layers,missing


def check_comparison(project,report,comparison):
    if (not comparison.get('engine_confirmation') or not report.get('engine_confirmation') or
        comparison.get('project_id')!=project['project_id'] or comparison.get('point_crs')!=report['point_crs'] or
        comparison.get('height')!=report['height'] or comparison.get('epoch')!=report['epoch'] or
        comparison.get('synthetic')!=report['synthetic']):
        raise ValueError('Compare SALSA-verified runs of the same network, frame, height datum and coordinate epoch.')


def compare_points(project,report,comparison):
    check_comparison(project,report,comparison)
    previous={p['id']:p for p in comparison['points']}
    rows=[]
    for point in report['points']:
        other=previous.get(point['id']); current=point['position_grid']; old=other['position_grid'] if other else None
        rows.append(dict(id=point['id'],current_grid=current,comparison_grid=old,
            delta_grid_m=[a-c for a,c in zip(current,old)] if current is not None and old is not None else None,
            status='COORDINATE DIFFERENCE ONLY' if current is not None and old is not None else 'NOT ESTIMATED IN BOTH RUNS'))
    for name in sorted(set(previous)-{p['id'] for p in report['points']}):
        rows.append(dict(id=name,current_grid=None,comparison_grid=previous[name]['position_grid'],delta_grid_m=None,status='ONLY IN COMPARISON RUN'))
    return rows


def load_comparison(path,program):
    """Read a complete local result package without writing or executing anything."""
    path=Path(path)
    if path.name!='quality.json': raise ValueError('Choose quality.json from a completed local GNSS report folder.')
    folder=path.parent
    if not (folder/'sha256.json').is_file() or not (folder/'input_metadata.json').is_file():
        raise ValueError('Choose the original complete SALSA calculation folder, not a derived map-only export.')
    hashes=json.loads((folder/'sha256.json').read_text(encoding='utf-8'))
    required={'quality.json','network.sanet.json','input.dat','input_metadata.json','solution.out','solution.bin'}
    if not isinstance(hashes,dict) or not required<=set(hashes): raise ValueError('Incomplete result checksum manifest.')
    for name in required:
        file=folder/name
        if file.is_symlink() or not file.is_file() or file.stat().st_size>50*1024*1024 or engine.sha256(file)!=hashes[name]:
            raise ValueError('Saved run has changed or is incomplete: '+name)
    saved=json.loads(path.read_text(encoding='utf-8'))
    if not saved.get('engine_confirmation'): raise ValueError('The comparison run has no verified SALSA result.')
    verified=q.inspect_network_result(folder,0,program,write=False)
    for key in ('project_sha256','positions_ecef','parameter_covariance_xyz','global_test'):
        if saved[key]!=verified[key]: raise ValueError('Saved report differs from its reproducible model/output.')
    return verified


def extent(layers):
    rectangle=QgsRectangle(); rectangle.setNull()
    for layer in layers.values():
        if layer.featureCount(): rectangle.combineExtentWith(layer.extent())
    if rectangle.isEmpty():
        center=next((f.geometry().boundingBox().center() for layer in layers.values() for f in layer.getFeatures()),QgsPointXY(0,0))
        rectangle=QgsRectangle(center.x()-10,center.y()-10,center.x()+10,center.y()+10)
    rectangle.scale(1.18)
    return rectangle


def export_package(parent,project,report,options,comparison=None):
    layers,missing=make_map_layers(project,report,options,comparison)
    folder=Path(parent)/('Survey_Map_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    folder.mkdir(parents=True,exist_ok=False)
    target=folder/'network_map.gpkg'
    for index,(key,layer) in enumerate(layers.items()):
        save=QgsVectorFileWriter.SaveVectorOptions(); save.driverName='GPKG'; save.layerName=key
        save.actionOnExistingFile=(QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteFile if index==0 else QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer)
        result=QgsVectorFileWriter.writeAsVectorFormatV3(layer,str(target),QgsProject.instance().transformContext(),save)
        if result[0]!=QgsVectorFileWriter.WriterError.NoError: raise ValueError('Map export failed; do not use the incomplete folder: '+str(result[1]))
        message,ok=layer.saveNamedStyle(str(folder/(key+'.qml')))
        if not ok: raise ValueError('Could not save map layer style: '+str(message))
    (folder/'network.sanet.json').write_text(n.dumps_project(project),encoding='utf-8')
    (folder/'quality.json').write_text(json.dumps(report,indent=2,ensure_ascii=False,allow_nan=False)+'\n',encoding='utf-8')
    if comparison is not None: (folder/'comparison.json').write_text(json.dumps(comparison,indent=2,ensure_ascii=False,allow_nan=False)+'\n',encoding='utf-8')
    manifest=dict(version=VERSION,kind='derived_network_map',stage=stage(report),height='ELLIP',point_crs=project['crs'],epoch=project['epoch'],
        snapshot_sha256=report['project_sha256'],display=asdict(options),unmapped=missing,
        meaning='Points and baseline endpoints use actual coordinates. Ellipses and displacement lines are enlarged DISPLAY ONLY; their attributes retain true unscaled values. Decorative lines hold Z at the origin. Horizontal/vertical confidence regions are separate marginal quantities. No survey approval.',
        notice='AS IS, without warranty. Disclaimers and limitations of liability apply to the extent permitted by GPLv3 sections 15-17 and applicable law. See LICENSE.',
        files={p.name:engine.sha256(p) for p in folder.iterdir() if p.is_file()})
    (folder/'map_manifest.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    return folder
