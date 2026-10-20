# SPDX-License-Identifier: GPL-3.0-only
"""Read-only linked map/table workspace for a GNSS network result snapshot."""
from copy import deepcopy
from datetime import datetime,timezone
import html
import json

from qgis.PyQt.QtCore import Qt,pyqtSignal
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QLabel,QPushButton,
    QComboBox,QCheckBox,QSplitter,QWidget,QTabWidget,QTextBrowser,QTableWidget,
    QTableWidgetItem,QHeaderView,QAbstractItemView,QFileDialog,QDialogButtonBox)
from qgis.core import (QgsGeometry,QgsPointXY,QgsProject,QgsRectangle,QgsCoordinateReferenceSystem,QgsCoordinateTransform)
from qgis.gui import QgsMapCanvas,QgsMapTool,QgsMapToolPan

from . import network as n,network_map as m
from .version import VERSION


class InspectTool(QgsMapTool):
    picked=pyqtSignal(str,str)

    def __init__(self,canvas,workspace,keys=None,kinds=None):
        super().__init__(canvas); self.workspace=workspace; self.keys=keys; self.kinds=kinds or {}
        self.setCursor(Qt.CursorShape.CrossCursor)

    def canvasReleaseEvent(self,event):
        if event.button()!=Qt.MouseButton.LeftButton: return
        point=QgsGeometry.fromPointXY(QgsPointXY(event.mapPoint()))
        scope=self.workspace.inspect_scope.currentData() if hasattr(self.workspace,'inspect_scope') else None
        keys=[scope] if scope else self.keys or ['points','baselines','residuals','changes','comparison']
        tolerance=self.canvas().mapUnitsPerPixel()*9; candidates=[]
        visible=self.canvas().layers()
        for priority,key in enumerate(keys):
            layer=self.workspace.layers.get(key)
            if layer is None or layer not in visible: continue
            for feature in layer.getFeatures():
                distance=feature.geometry().distance(point)
                if distance<=tolerance:
                    # Prefer a point within the pick radius; repeated/overlapping
                    # edges remain individually available in the linked table.
                    candidates.append((0 if key=='points' else 1,distance,priority,key,str(feature['record_id'])))
        if candidates:
            _,_,_,key,identifier=min(candidates)
            self.picked.emit(self.kinds.get(key,'point' if key in ('points','changes','comparison') else 'observation'),identifier)
        else: self.workspace.message.setText('No record within the pick radius. Choose a specific inspect type or a table row.')


class NetworkMapDialog(QDialog):
    def __init__(self,parent,project,report,host,program):
        super().__init__(parent)
        m.validate_snapshot(project,report)
        self.project=n.loads_project(n.dumps_project(project)); self.report=deepcopy(report)
        self.host=host; self.program=program; self.comparison=None; self.layers={}; self.missing=[]
        self.selecting=False; self.selected=None; self.last_export=None; self.added_group=None
        self.setWindowTitle(f'Survey Adjustment {VERSION} - GNSS network map')
        available=self.screen().availableGeometry(); self.resize(min(1320,available.width()-40),min(820,available.height()-60))
        layout=QVBoxLayout(self)
        self.banner=QLabel(); self.banner.setWordWrap(True)
        self.banner.setStyleSheet('QLabel {background:#243d57;color:white;padding:9px;font-weight:600;}')
        layout.addWidget(self.banner)
        toolbar=QHBoxLayout()
        self.pan_button=QPushButton('Pan'); self.pan_button.clicked.connect(lambda:self.canvas.setMapTool(self.pan_tool))
        self.inspect_button=QPushButton('Inspect'); self.inspect_button.clicked.connect(lambda:self.canvas.setMapTool(self.inspect_tool))
        self.inspect_scope=QComboBox()
        for label,key in [('Points / connections',''),('Points','points'),('Baselines','baselines'),('Residuals','residuals'),('Coordinate changes','changes'),('Run differences','comparison')]:
            self.inspect_scope.addItem(label,key)
        self.inspect_scope.currentIndexChanged.connect(lambda:self.canvas.setMapTool(self.inspect_tool))
        self.fit_button=QPushButton('Fit network'); self.fit_button.clicked.connect(self.fit)
        self.zoom_button=QPushButton('Zoom selected'); self.zoom_button.clicked.connect(self.zoom_selected)
        self.add_button=QPushButton('Add to QGIS'); self.add_button.setToolTip('Add a separate group of read-only snapshot layers to the current QGIS project'); self.add_button.clicked.connect(self.add_to_qgis)
        self.save_button=QPushButton('Save map …'); self.save_button.setToolTip('Save a new local GeoPackage, layer styles, source snapshot, report and display manifest'); self.save_button.clicked.connect(self.save_package)
        for widget in (self.pan_button,self.inspect_button,self.inspect_scope,self.fit_button,self.zoom_button,self.add_button,self.save_button): toolbar.addWidget(widget)
        layout.addLayout(toolbar)
        row=QHBoxLayout(); self.ellipse_factor=self.factor(); self.vector_factor=self.factor()
        row.addWidget(QLabel('Ellipse display ×')); row.addWidget(self.ellipse_factor)
        row.addWidget(QLabel('Displacement display ×')); row.addWidget(self.vector_factor)
        self.compare_button=QPushButton('Compare saved run …'); self.compare_button.clicked.connect(self.load_comparison)
        self.compare_button.setEnabled(bool(report.get('engine_confirmation')))
        self.clear_compare=QPushButton('Clear comparison'); self.clear_compare.clicked.connect(self.clear_comparison); self.clear_compare.setEnabled(False)
        row.addStretch(); row.addWidget(self.compare_button); row.addWidget(self.clear_compare); layout.addLayout(row)
        row=QHBoxLayout(); self.visibility={}
        for key,label,checked in [('points','Points',True),('baselines','Baselines',True),('ellipses','95% ellipses',True),
                                 ('residuals','Residuals',False),('changes','Coordinate changes',False),('sources','Original coordinates',False),('comparison','Run differences',True)]:
            checkbox=QCheckBox(label); checkbox.setChecked(checked); checkbox.toggled.connect(self.update_visibility)
            self.visibility[key]=checkbox; row.addWidget(checkbox)
        self.excluded=QCheckBox('Excluded'); self.excluded.setChecked(True); self.excluded.toggled.connect(self.update_visibility); row.addWidget(self.excluded)
        layout.addLayout(row)
        self.legend=QLabel('Triangle: fixed by model   Diamond: known with uncertainty   Circle: adjusted   Cross: independent check   '
            'Blue: adjustment   Amber: weak control   Red: residual review   Purple dashed: check   Grey dotted: excluded')
        self.legend.setWordWrap(True); layout.addWidget(self.legend)
        split=QSplitter(Qt.Orientation.Horizontal); layout.addWidget(split,1)
        self.canvas=QgsMapCanvas(); self.canvas.setCanvasColor(QColor('#f4f7fa'))
        self.canvas.setDestinationCrs(QgsCoordinateReferenceSystem(project['crs']))
        split.addWidget(self.canvas)
        sidebar=QWidget(); sidebar_layout=QVBoxLayout(sidebar); sidebar_layout.setContentsMargins(2,0,0,0)
        self.tables=QTabWidget(); sidebar_layout.addWidget(self.tables,2)
        self.point_table=self.table(['Point','Role','σU mm','H95 major mm','Review'])
        self.observation_table=self.table(['Observation','Session','Use / state','Redundancy %'])
        self.comparison_table=self.table(['Point','dE mm','dN mm','dH mm','Status'])
        self.tables.addTab(self.point_table,'Points'); self.tables.addTab(self.observation_table,'Observations'); self.tables.addTab(self.comparison_table,'Comparison')
        self.detail=QTextBrowser(); self.detail.setOpenExternalLinks(False); sidebar_layout.addWidget(self.detail,3)
        split.addWidget(sidebar); split.setSizes([810,490]); split.setStretchFactor(0,2); split.setStretchFactor(1,1)
        self.message=QLabel('Select a point or connection in the map or table. Mouse wheel zooms; Pan moves the map.'); self.message.setWordWrap(True)
        layout.addWidget(self.message)
        self.units=QLabel(); layout.addWidget(self.units)
        self.canvas.scaleChanged.connect(self.update_units)
        self.pan_tool=QgsMapToolPan(self.canvas); self.inspect_tool=InspectTool(self.canvas,self)
        self.inspect_tool.picked.connect(self.select_record); self.canvas.setMapTool(self.inspect_tool)
        self.point_table.itemSelectionChanged.connect(lambda:self.table_selected('point',self.point_table))
        self.observation_table.itemSelectionChanged.connect(lambda:self.table_selected('observation',self.observation_table))
        self.comparison_table.itemSelectionChanged.connect(lambda:self.table_selected('point',self.comparison_table))
        self.ellipse_factor.currentIndexChanged.connect(self.rebuild)
        self.vector_factor.currentIndexChanged.connect(self.rebuild)
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Close); buttons.rejected.connect(self.reject); layout.addWidget(buttons)
        self.rebuild(); self.fit(); self.select_record('point',project['points'][0]['id'])

    @staticmethod
    def factor():
        combo=QComboBox()
        for value in (1,10,100,500,1000,5000): combo.addItem(f'{value:g}',float(value))
        combo.setCurrentIndex(3); return combo

    @staticmethod
    def table(headers):
        table=QTableWidget(0,len(headers)); table.setHorizontalHeaderLabels(headers)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setStretchLastSection(True)
        return table

    def options(self): return m.MapOptions(self.ellipse_factor.currentData(),self.vector_factor.currentData())

    def rebuild(self,*_):
        try:
            layers,missing=m.make_map_layers(self.project,self.report,self.options(),self.comparison)
            self.canvas.stopRendering(); self.canvas.setLayers([])
            self.layers,self.missing=layers,missing
            self.populate_tables(); self.update_visibility()
            options=self.options()
            self.banner.setText(('SYNTHETIC TRAINING DATA | ' if self.project['synthetic'] else 'PRIVATE LOCAL DATA | ')+m.stage(self.report)+
                f" | Global: {self.report['global_test']['status']}; dof {self.report['global_test']['degrees_of_freedom']}"+
                f' | Ellipses ×{options.ellipse_factor:g}; displacements ×{options.vector_factor:g} — DISPLAY ONLY. True values remain in metres.')
            self.update_units()
            if self.selected: self.select_record(*self.selected)
            if missing: self.message.setText(f'{len(missing)} unmapped record(s): '+'; '.join(missing))
            return True
        except Exception as error:
            self.message.setText('Cannot build map: '+str(error)); return False

    def populate_tables(self):
        self.selecting=True
        try:
            def fill(table,rows):
                table.setRowCount(len(rows))
                for index,values in enumerate(rows):
                    for col,value in enumerate(values):
                        item=QTableWidgetItem('' if value is None else f'{value:.3f}' if isinstance(value,float) else str(value))
                        item.setData(Qt.ItemDataRole.UserRole,str(values[0])); table.setItem(index,col,item)
            fill(self.point_table,[[p['id'],p['role'],1000*p['sigma_enu_m'][2] if p['sigma_enu_m'] else None,
                1000*p['horizontal_95_axes_m'][0] if p['horizontal_95_axes_m'] else None,', '.join(p['flags'])] for p in self.report['points']])
            diagnostics={o['id']:o for o in self.report['observations'] if o['kind']=='GNSS_VECTOR'}
            fill(self.observation_table,[[o['id'],o['session'],o['use'] if o['enabled'] else 'DISABLED',
                100*diagnostics[o['id']]['redundancy_fraction'] if o['id'] in diagnostics else None] for o in self.project['observations']])
            fill(self.comparison_table,[[r['id'],*([1000*d for d in r['delta_grid_m']] if r['delta_grid_m'] is not None else [None]*3),r['status']]
                for r in m.compare_points(self.project,self.report,self.comparison)] if self.comparison else [])
        finally: self.selecting=False

    def update_visibility(self,*_):
        if not self.layers: return
        subset='' if self.excluded.isChecked() else '"status" != \'DISABLED\''
        self.layers['baselines'].setSubsetString(subset)
        order=['points','sources','residuals','changes','comparison','ellipses','baselines']
        self.canvas.setLayers([self.layers[key] for key in order if key in self.layers and self.visibility[key].isChecked()])
        self.canvas.refresh()

    def update_units(self,*_):
        self.units.setText(f"{self.project['crs']} | E/N metres; point Z = ellipsoidal height | Scale 1:{self.canvas.scale():,.0f} | "
            'Horizontal 95% ellipse and σU are separate model quantities. No survey approval.')

    def fit(self):
        if self.layers: self.canvas.setExtent(m.extent(self.layers)); self.canvas.refresh()

    def table_selected(self,kind,table):
        if self.selecting or table.currentRow()<0: return
        item=table.item(table.currentRow(),0)
        if item is not None: self.select_record(kind,item.text())

    def select_record(self,kind,identifier):
        self.selected=(kind,identifier); self.selecting=True
        try:
            table=self.point_table if kind=='point' else self.observation_table
            for row in range(table.rowCount()):
                if table.item(row,0).text()==identifier: table.selectRow(row); break
            if kind=='point' and self.tables.currentIndex()!=2: self.tables.setCurrentIndex(0)
            elif kind=='observation': self.tables.setCurrentIndex(1)
            for key,layer in self.layers.items():
                field='point_id' if kind=='point' else 'observation_id'
                selected=[f.id() for f in layer.getFeatures() if field in layer.fields().names() and str(f[field])==identifier]
                layer.selectByIds(selected)
            self.show_detail(kind,identifier); self.canvas.refresh()
        finally: self.selecting=False

    def show_detail(self,kind,identifier):
        source=next((r for r in self.project['points' if kind=='point' else 'observations'] if r['id']==identifier),None)
        if source is None:
            self.detail.setPlainText('Record is present only in the comparison run.'); return
        result=next((r for r in self.report['points' if kind=='point' else 'observations'] if r['id']==identifier),None)
        if kind=='observation' and result is None: result=next((r for r in self.report['checks'] if r['id']==identifier),None)
        pieces=['<h3>'+html.escape(identifier)+'</h3><p>Read-only snapshot. Original inputs remain unchanged.</p>']
        if kind=='point':
            p=result
            if p['role']=='FIXED':
                pieces.append('<p><b>Held exact by model choice.</b> Zero model covariance does not establish the control point\'s accuracy.</p>')
            elif p['sigma_enu_m']:
                pieces.append(f"<p><b>σU {p['sigma_enu_m'][2]*1000:.2f} mm</b>; vertical 95% ±{p['vertical_95_halfwidth_m']*1000:.2f} mm.<br>"
                    f"Horizontal 95% semi-axes (geodetic ENU): {p['horizontal_95_axes_m'][0]*1000:.2f} / {p['horizontal_95_axes_m'][1]*1000:.2f} mm.</p>")
            pieces.append('<p>Original coordinate may be an initial approximation. Fixed zero uncertainty is a model assumption.</p>')
            if p['position_grid'] is not None:
                east,north,height=p['position_grid']
                pieces.append(f'<p>Result E {east:.4f} m<br>N {north:.4f} m<br>Ellipsoidal height {height:.4f} m</p>')
        elif result and 'residual_xyz_m' in result and result['residual_xyz_m'] is not None:
            pieces.append('<p><b>Residual XYZ (observed − calculated):</b><br>'+
                ' / '.join(f'{value*1000:.2f}' for value in result['residual_xyz_m'])+' mm</p>')
            if 'redundancy_fraction' in result:
                pieces.append(f"<p>Internal redundancy {100*result['redundancy_fraction']:.1f}%; weakest direction {100*result['minimum_direction_redundancy']:.1f}%.<br>"+
                    html.escape(', '.join(result['flags']) or 'No local review flag')+'</p>')
        for title,value in [('Original record',source),('Calculated result',result)]:
            pieces.append('<h4>'+title+'</h4><pre>'+html.escape(json.dumps(value,indent=2,ensure_ascii=False))+'</pre>')
        if kind=='observation':
            audit=[dict(provenance={key:value for key,value in batch.items() if key!='records'},record=row)
                for batch in self.project['imports'] for row in batch['records'] if row['observation']['id']==identifier]
            if audit: pieces.append('<h4>Original import provenance</h4><pre>'+html.escape(json.dumps(audit,indent=2,ensure_ascii=False))+'</pre>')
        if self.comparison and kind=='point':
            row=next((r for r in m.compare_points(self.project,self.report,self.comparison) if r['id']==identifier),None)
            pieces.append('<h4>Current minus comparison</h4><p>Coordinate differences only; shared errors/correlation prevent inferring significance here.</p><pre>'+html.escape(json.dumps(row,indent=2))+'</pre>')
        self.detail.setHtml(''.join(pieces))

    def zoom_selected(self):
        geometries=[f.geometry().boundingBox() for layer in self.layers.values() for f in layer.selectedFeatures()]
        if not geometries: self.message.setText('This record has no map geometry. Read its table/details.'); return
        rectangle=QgsRectangle(); rectangle.setNull()
        for geometry in geometries: rectangle.combineExtentWith(geometry)
        if rectangle.width()<10 or rectangle.height()<10:
            center=rectangle.center(); size=max(10.,rectangle.width(),rectangle.height())
            rectangle=QgsRectangle(center.x()-size/2,center.y()-size/2,center.x()+size/2,center.y()+size/2)
        rectangle.scale(1.3); self.canvas.setExtent(rectangle); self.canvas.refresh()

    def load_comparison(self):
        try:
            path,_=QFileDialog.getOpenFileName(self,'Choose quality.json from a completed LOCAL SALSA run','','GNSS quality report (quality.json)')
            if not path: return False
            candidate=m.load_comparison(path,self.program); m.check_comparison(self.project,self.report,candidate)
            previous=self.comparison; self.comparison=candidate
            if not self.rebuild(): self.comparison=previous; self.rebuild(); return False
            self.clear_compare.setEnabled(True); self.tables.setCurrentIndex(2)
            self.message.setText('Saved run verified and compared. Differences are current minus comparison; no independence/significance is inferred.'); return True
        except Exception as error: self.message.setText('Cannot compare: '+str(error)); return False

    def clear_comparison(self):
        self.comparison=None; self.clear_compare.setEnabled(False); self.rebuild()

    def add_to_qgis(self):
        try:
            layers,_=m.make_map_layers(self.project,self.report,self.options(),self.comparison)
            project=QgsProject.instance(); main=self.host.iface.mapCanvas(); rectangle=m.extent(layers)
            group=project.layerTreeRoot().addGroup('Survey network '+VERSION+' | '+m.stage(self.report)+' | '+datetime.now(timezone.utc).strftime('%H:%M:%S UTC'))
            group.setCustomProperty('survey_adjustment/snapshot_sha256',self.report['project_sha256'])
            for key in ('points','sources','residuals','changes','comparison','ellipses','baselines'):
                if key not in layers: continue
                layer=layers[key]
                if key=='baselines' and not self.excluded.isChecked(): layer.setSubsetString('"status" != \'DISABLED\'')
                project.addMapLayer(layer,False); node=group.addLayer(layer); node.setItemVisibilityChecked(self.visibility[key].isChecked())
            # QGIS may initialize a blank project's canvas CRS from its first
            # added layer. Resolve the destination AFTER layer registration.
            transform=QgsCoordinateTransform(QgsCoordinateReferenceSystem(self.project['crs']),main.mapSettings().destinationCrs(),project)
            destination=transform.transformBoundingBox(rectangle)
            main.setExtent(destination); main.refresh()
            self.added_group=group; self.message.setText('Added a separate snapshot group to QGIS. These are temporary memory layers; Save map package creates persistent GeoPackage layers.'); return True
        except Exception as error: self.message.setText('Cannot add layers: '+str(error)); return False

    def save_package(self):
        try:
            parent=QFileDialog.getExistingDirectory(self,'Choose a LOCAL parent folder for a new map package')
            if not parent: return False
            self.last_export=m.export_package(parent,self.project,self.report,self.options(),self.comparison)
            self.message.setText('Saved local GeoPackage, input snapshot, report and display manifest: '+str(self.last_export)); return True
        except Exception as error: self.message.setText('Cannot save map package: '+str(error)); return False

    def done(self,result):
        self.canvas.stopRendering(); self.canvas.setLayers([])
        super().done(result)
