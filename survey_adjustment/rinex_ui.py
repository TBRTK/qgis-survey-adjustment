# SPDX-License-Identifier: GPL-3.0-only
"""Raw-file review and linked preflight views inside the existing network panel."""
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
import json,math,html
from qgis.PyQt import sip
from qgis.PyQt.QtCore import Qt,pyqtSignal,QMetaType
from qgis.PyQt.QtGui import QColor,QBrush,QPen
from qgis.PyQt.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QFormLayout,QDialog,QLabel,
    QComboBox,QLineEdit,QCheckBox,QPushButton,QDialogButtonBox,QTextBrowser,QTabWidget,
    QTableWidget,QTableWidgetItem,QAbstractItemView,QHeaderView,QGraphicsView,QGraphicsScene,
    QScrollArea,QMessageBox)
from qgis.core import QgsTask,QgsApplication,QgsGeometry,QgsPoint,QgsProject,QgsCoordinateReferenceSystem,QgsMarkerSymbol
from qgis.gui import QgsMapCanvas
from . import rinex as r,network as n
from .network_ui import combo
from .layers import make_layer
from .coordinate_job import llh
from .network_map_ui import InspectTool
from .network_map import _labels


class RawImportDialog(QDialog):
    def __init__(self,parent,project,source,existing=None):
        super().__init__(parent); self.project=deepcopy(project); self.source=source; self.existing=existing; self.result_project=None
        self.setWindowTitle('Review RINEX source - catalogue only'); self.resize(920,760)
        layout=QVBoxLayout(self); meta=source['metadata']; obs=meta['file_type']=='OBS'
        banner=QLabel(('SYNTHETIC SOURCE - ' if source['marked_synthetic'] else 'LOCAL SOURCE - ')+source['source_name']+
            '\nRaw data stay in the original file. This adds a catalogue link, not coordinates or a baseline. Header XYZ is approximate; no antenna or height correction is applied.')
        banner.setWordWrap(True); layout.addWidget(banner)
        info=QTextBrowser(); info.setMaximumHeight(155)
        shown={key:value for key,value in meta.items() if key not in ('samples','header')}
        info.setPlainText(json.dumps(shown,indent=2,ensure_ascii=False)); layout.addWidget(info)
        page=QWidget(); form=QFormLayout(page)
        self.role=combo({'BASE':'Base','ROVER':'Rover'} if obs else {'NAV':'Navigation catalogue'},existing['role'] if existing else ('ROVER' if obs else 'NAV'))
        self.point=combo({'':'Select existing physical point',**{p['id']:p['id'] for p in project['points']}},existing['point'] if existing else '')
        self.session=combo({'':'Select acquisition session',**{s['id']:s['id'] for s in project['sessions']}},existing['session'] if existing else '')
        for label,field in (('File role',self.role),('Physical point',self.point),('Acquisition session',self.session)): form.addRow(label,field)
        self.point.setEnabled(obs); self.session.setEnabled(obs)
        self.fields={}
        help_texts={
            'utc_offset_seconds':('Source minus UTC (s)','Documented integer offset: e.g. GPS time minus UTC. Enter 0 only for an explicitly UTC source. No default or leap-second inference. Blank is saved as unresolved and blocks time preflight.'),
            'time_evidence':('Time evidence','Document the declared source time system and offset applicable to these observations. Source epochs and coordinate epoch remain separate.'),
            'antenna_evidence':('Antenna review','Document antenna identity, reference point and H/E/N offsets. Original header is retained; no calibration or antenna-height correction is applied here.'),
            'note':('Review / purpose','State the source purpose, provenance and limitations. This is a metadata review, not survey approval.')}
        for key,(label,help_text) in help_texts.items():
            value=existing.get(key) if existing else ''
            field=QLineEdit('' if value is None else str(value)); field.setPlaceholderText(help_text); field.setToolTip(help_text)
            field.setEnabled(obs or key=='note'); self.fields[key]=field
            box=QWidget(); inside=QVBoxLayout(box); inside.setContentsMargins(0,0,0,0); inside.addWidget(field)
            hint=QLabel(help_text); hint.setWordWrap(True); inside.addWidget(hint); form.addRow(label,box)
        self.synthetic=QCheckBox('This source is constructed synthetic example data'); form.addRow(self.synthetic)
        self.confirm=QCheckBox('I reviewed the source metadata, links, classification and unresolved fields'); form.addRow(self.confirm)
        scroll=QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(page); layout.addWidget(scroll,1)
        self.status=QLabel('No import applied. Missing metadata remains unresolved and visible in preflight.'); self.status.setWordWrap(True); layout.addWidget(self.status)
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText('Save review' if existing else 'Add source to network')
        buttons.accepted.connect(self.submit); buttons.rejected.connect(self.reject); layout.addWidget(buttons)

    def submit(self):
        try:
            offset=self.fields['utc_offset_seconds'].text().strip()
            review=r.ImportReview(role=self.role.currentData(),point=self.point.currentData(),session=self.session.currentData(),
                utc_offset_seconds=int(offset) if offset else None,time_evidence=self.fields['time_evidence'].text().strip(),
                antenna_evidence=self.fields['antenna_evidence'].text().strip(),note=self.fields['note'].text().strip(),
                synthetic=self.synthetic.isChecked(),confirmed=self.confirm.isChecked())
            # Recheck exact bytes immediately before applying an import/review.
            import hashlib
            with Path(self.source['path']).open('rb') as handle: payload=handle.read(r.MAX_BYTES+1)
            if hashlib.sha256(payload).hexdigest()!=self.source['source_sha256']: raise ValueError('Source changed during review; reopen it.')
            base=deepcopy(self.project)
            if self.existing:
                if self.source['source_sha256']!=self.existing['source_sha256']: raise ValueError('Existing source bytes changed; review a separate replacement, preserving the original.')
                index=next(i for i,row in enumerate(base['raw_sources']) if row['id']==self.existing['id']); base['raw_sources'].pop(index)
            result=r.plan_import(base,self.source,review)
            if self.existing:
                row=result['raw_sources'].pop(); row['id']=self.existing['id']; result['raw_sources'].insert(index,row); n.validate_project(result)
            self.result_project=result; self.accept(); return True
        except (ValueError,OSError) as error:
            self.status.setText('Cannot apply review: '+str(error)); return False


class Timeline(QGraphicsView):
    picked=pyqtSignal(str)

    def __init__(self):
        super().__init__(); self.setScene(QGraphicsScene(self)); self.setMinimumHeight(135)

    def display(self,project,report):
        scene=self.scene(); scene.clear(); self.bars={}
        rows=[row for row in project.get('raw_sources',[]) if row['id'] in report['verified_sources'] and row['metadata']['file_type']=='OBS' and row['utc_offset_seconds'] is not None and row['metadata']['time_system']]
        if not rows: scene.addText('No verified observations with a documented UTC offset. Run raw preflight.'); return
        start=min(row['metadata']['first_tick']-row['utc_offset_seconds']*r.TICK for row in rows)
        end=max(row['metadata']['last_tick']-row['utc_offset_seconds']*r.TICK for row in rows); span=max(end-start,r.TICK)
        scene.addText('UTC from explicitly reviewed offsets - gaps are excluded; click a bar to inspect the source.').setPos(0,0)
        scene.addText(r.tick_text(start)+'Z').setPos(215,25); scene.addText(r.tick_text(end)+'Z').setPos(775,25)
        for index,row in enumerate(rows):
            y=60+index*42; meta=row['metadata']; offset=row['utc_offset_seconds']*r.TICK
            label=scene.addText(row['role']+' / '+row['point']+' / '+row['source_name']); label.setPos(0,y); label.setTextWidth(210)
            segments=[]; begin=meta['first_tick']
            for a,b,_ in meta['gaps']: segments.append((begin,a)); begin=b
            segments.append((begin,meta['last_tick'])); self.bars[row['id']]=[]
            for a,b in segments:
                x=220+760*(a-offset-start)/span; width=max(3.,760*(b-a)/span)
                item=scene.addRect(x,y+5,width,18,QPen(QColor('#167367')),QBrush(QColor('#39a391')))
                item.setData(0,row['id']); item.setToolTip(row['source_name']+'\n'+r.tick_text(a-offset)+'Z to '+r.tick_text(b-offset)+'Z')
                self.bars[row['id']].append(item)
        scene.setSceneRect(scene.itemsBoundingRect().adjusted(-4,-4,15,15))

    def select(self,identifier):
        for key,items in getattr(self,'bars',{}).items():
            for item in items: item.setBrush(QBrush(QColor('#efaa32' if key==identifier else '#39a391')))

    def mousePressEvent(self,event):
        item=self.itemAt(event.pos())
        if item and item.data(0): self.picked.emit(item.data(0))
        super().mousePressEvent(event)


def raw_map_layers(project,report):
    if report['project_sha256']!=r.content_fingerprint(project): raise ValueError('Raw preflight is stale.')
    rows=[]
    for source in project.get('raw_sources',[]):
        xyz=source['metadata'].get('approximate_xyz')
        if source['id'] not in report['verified_sources'] or xyz is None: continue
        if not 5_000_000<math.sqrt(sum(v*v for v in xyz))<8_000_000 or math.hypot(*xyz[:2])<1: continue
        # Ellipsoid conversion only; no claim about frame/realisation or epoch.
        lat,lon,h=llh(xyz,'EPSG:4979')
        rows.append(([source['id'],source['source_name'],source['point'],source['role'],source['source_sha256'],
            'DISPLAY ONLY - header approximate XYZ; frame/height not established; no antenna reduction',*xyz],QgsGeometry(QgsPoint(lon,lat,h))))
    text,number=QMetaType.Type.QString,QMetaType.Type.Double
    layer=make_layer('PointZ','RINEX - APPROXIMATE HEADER LOCATIONS - DISPLAY ONLY','EPSG:4979',
        [('record_id',text),('source_name',text),('point_id',text),('role',text),('source_sha256',text),('scope',text),('header_x',number),('header_y',number),('header_z',number)],rows)
    layer.renderer().setSymbol(QgsMarkerSymbol.createSimple({'name':'diamond','color':'#d28a1b','size':'4'}))
    _labels(layer,'"point_id" || \' / APPROX ONLY\'')
    layer.setCustomProperty('survey_adjustment/raw_catalogue',True)
    layer.setCustomProperty('survey_adjustment/snapshot_sha256',report['project_sha256'])
    return {'points':layer}


class RawPanel(QWidget):
    def __init__(self,owner):
        super().__init__(owner); self.owner=owner; self.task=None; self.report=None; self.layers={}; self.selection_guard=False
        layout=QVBoxLayout(self)
        note=QLabel('Raw sources belong to this network. Use Import GNSS file above, then review and check files. Adjustment/delivery tools remain on the Points, Sessions and Observations tabs. Raw epochs do not create points or baselines.')
        note.setWordWrap(True); layout.addWidget(note)
        row=QHBoxLayout()
        self.check_button=QPushButton('Check files / refresh map and timeline'); self.check_button.clicked.connect(self.check)
        self.review_button=QPushButton('Review selected metadata …'); self.review_button.clicked.connect(self.review_selected)
        self.remove_button=QPushButton('Remove link …'); self.remove_button.clicked.connect(self.remove_selected)
        self.cancel_button=QPushButton('Cancel inspection'); self.cancel_button.clicked.connect(self.cancel); self.cancel_button.setEnabled(False)
        self.example_button=QPushButton('Add synthetic RINEX examples'); self.example_button.clicked.connect(self.load_examples)
        for button in (self.check_button,self.review_button,self.remove_button,self.example_button,self.cancel_button): row.addWidget(button)
        layout.addLayout(row)
        self.table=QTableWidget(0,8); self.table.setHorizontalHeaderLabels(['Source','Role / point','Session','Version / type','Time basis','Epochs / records','Gaps / LLI','State'])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows); self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers); self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self.table_selected); self.table.setMinimumHeight(110); self.table.setMaximumHeight(140); layout.addWidget(self.table)
        self.views=QTabWidget(); self.views.setMinimumHeight(240); layout.addWidget(self.views,1)
        self.overview=QTextBrowser(); self.views.addTab(self.overview,'Preflight / source details')
        self.timeline=Timeline(); self.timeline.picked.connect(lambda key:self.select_record('raw_source',key)); self.views.addTab(self.timeline,'UTC timeline')
        page=QWidget(); box=QVBoxLayout(page)
        label=QLabel('DISPLAY ONLY - approximate header locations on a WGS84 ellipsoid; no national reference/height claim. Missing locations remain in the file table. Click a marker to select its original source.')
        label.setWordWrap(True); box.addWidget(label)
        self.canvas=QgsMapCanvas(); self.canvas.setDestinationCrs(QgsCoordinateReferenceSystem('EPSG:4979')); self.canvas.setCanvasColor(QColor('#f4f6f8')); box.addWidget(self.canvas,1)
        self.inspect=InspectTool(self.canvas,self,keys=['points'],kinds={'points':'raw_source'}); self.inspect.picked.connect(self.select_record); self.canvas.setMapTool(self.inspect)
        self.views.addTab(page,'Approximate source map')
        self.message=QLabel('No current raw-file check. Original files are never modified.'); self.message.setWordWrap(True); layout.addWidget(self.message)
        self.refresh()

    def busy(self,enabled):
        for button in (self.check_button,self.review_button,self.remove_button,self.example_button,self.owner.import_button): button.setEnabled(not enabled)
        self.cancel_button.setEnabled(enabled)

    def cancel(self):
        if self.task: self.task.cancel()

    def work(self,label,operation,done):
        if self.task: self.message.setText('Wait for or cancel the current inspection.'); return False
        self.busy(True); self.message.setText(label+' - local read-only inspection …')
        def execute(task): return operation(task.isCanceled)
        def finish(error,result=None):
            if sip.isdeleted(self): return
            task=self.task; self.task=None; self.busy(False)
            if error or (task and task.isCanceled()):
                self.message.setText('Inspection not applied: '+str(error or 'Cancelled')); return
            try: done(result)
            except (ValueError,OSError,KeyError,TypeError) as problem: self.message.setText('Inspection not applied: '+str(problem))
        self.task=QgsTask.fromFunction(label,execute,on_finished=finish)
        QgsApplication.taskManager().addTask(self.task); return True

    def invalidate(self):
        self.report=None; self.layers={}; self.canvas.setLayers([]); self.canvas.hide(); self.canvas.refresh(); self.timeline.scene().clear()
        self.overview.clear(); self.message.setText('Network/raw review changed. Check files again; previous raw map/timeline is no longer current.')

    def refresh(self):
        self.invalidate(); sources=self.owner.project.get('raw_sources',[]); self.table.setRowCount(len(sources))
        for index,row in enumerate(sources):
            meta=row['metadata']; values=[row['source_name'],row['role']+' / '+row['point'],row['session'],meta['version']+' / '+meta['file_type'],
                meta['time_system'] or 'Undocumented',meta.get('epochs',meta.get('records')),str(len(meta.get('gaps',[])))+' / '+str(meta.get('lli_loss_of_lock',0)),'Not rechecked']
            for col,value in enumerate(values): self.table.setItem(index,col,QTableWidgetItem(str(value)))

    def open_source(self,path,existing=None):
        project=self.owner.snapshot()
        def loaded(source):
            if self.owner.snapshot()!=project: raise ValueError('Network changed during inspection; open the file again.')
            dialog=RawImportDialog(self,project,source,existing)
            try:
                if dialog.exec()==QDialog.DialogCode.Accepted:
                    if self.owner.snapshot()!=project: raise ValueError('Network changed during review; no import applied.')
                    self.owner.project=dialog.result_project; self.owner.dirty=True; self.owner.refresh(); self.owner.changed()
                    self.owner.tabs.setCurrentWidget(self); self.message.setText('Raw source/review saved in this network draft. Save the network explicitly, then check files.')
            finally: dialog.deleteLater()
        return self.work('Read RINEX source',lambda cancelled:r.read_source(path,cancelled),loaded)

    def review_selected(self):
        index=self.table.currentRow(); sources=self.owner.project.get('raw_sources',[])
        if index<0: self.message.setText('Select a raw source first.'); return False
        row=deepcopy(sources[index]); return self.open_source(row['path'],row)

    def check(self):
        try: project=self.owner.snapshot()
        except ValueError as error: self.message.setText(str(error)); return False
        self.invalidate()
        def finished(report):
            if self.owner.snapshot()!=project: raise ValueError('Network changed during preflight; run the check again.')
            self.report=report; self.layers=raw_map_layers(project,report); self.canvas.setLayers(list(self.layers.values()))
            layer=self.layers['points']
            if layer.featureCount(): self.canvas.setExtent(layer.extent().buffered(.0005))
            self.canvas.show(); self.canvas.refresh()
            self.timeline.display(project,report)
            labels={'CHECKED':'File unchanged','BLOCKED':'Review required','SOURCE_UNAVAILABLE_OR_CHANGED':'Missing / changed'}
            states={entry['id']:labels[entry['status']] for entry in report['entries']}
            for i,row in enumerate(project.get('raw_sources',[])): self.table.setItem(i,7,QTableWidgetItem(states[row['id']]))
            self.show_report()
            self.message.setText(('Review required' if report['blockers'] else 'Raw file checks completed')+' - '+str(len(report['verified_sources']))+' unchanged files; '+str(len(report['pairs']))+' base/rover pairs. No positioning or adjustment performed.')
        return self.work('RINEX preflight',lambda cancelled:r.inspect_catalogue(project,cancelled),finished)

    def table_selected(self):
        if self.selection_guard: return
        index=self.table.currentRow(); rows=self.owner.project.get('raw_sources',[])
        if index<0 or index>=len(rows): return
        source=rows[index]; self.timeline.select(source['id'])
        if self.layers:
            layer=self.layers['points']; layer.selectByIds([f.id() for f in layer.getFeatures() if f['record_id']==source['id']])
        self.show_report(source)

    def show_report(self,source=None):
        esc=lambda value:html.escape(str(value)); report=self.report
        text='<h3>Raw GNSS file review</h3><p>Catalogue and availability checks only. No position, baseline or survey accuracy has been calculated.</p>'
        if report:
            text+='<h4>'+('Review required' if report['blockers'] else 'File checks completed')+'</h4>'
            for label,key in (('Items requiring review','blockers'),('Scope and warnings','warnings')):
                text+='<b>'+label+'</b><ul>'+(''.join('<li>'+esc(value)+'</li>' for value in report[key]) or '<li>None identified by this limited profile.</li>')+'</ul>'
            text+='<h4>Base / rover availability by acquisition session</h4>'
            names={row['id']:row['source_name'] for row in self.owner.project.get('raw_sources',[])}
            for pair in report['pairs']:
                text+='<p>'+esc(names[pair['base']])+' → '+esc(names[pair['rover']])+': '+str(pair['matched_epochs'])+' exactly aligned epochs; '+str(pair['minimum_common_code_phase_satellites'])+'–'+str(pair['maximum_common_code_phase_satellites'])+' matching code/phase satellites.<br>'+esc(pair['first_utc'])+' to '+esc(pair['last_utc'])+'</p>'
        else: text+='<p>Files have not been rechecked against this current network snapshot.</p>'
        if source:
            meta=source['metadata']; text+='<h4>Selected source: '+esc(source['source_name'])+'</h4><table cellpadding="4">'
            for label,value in [('Original local file',source['path']),('Role / point / session',source['role']+' / '+source['point']+' / '+source['session']),
                ('Original source time system',meta['time_system'] or 'Undocumented'),('Declared source minus UTC (s)',source['utc_offset_seconds']),
                ('Time evidence',source['time_evidence']),('Antenna evidence',source['antenna_evidence']),('Review / purpose',source['note']),
                ('Header approximate XYZ (m)',meta['approximate_xyz']),('Header antenna H/E/N (m)',meta['antenna_hen']),('Original file SHA256',source['source_sha256'])]:
                text+='<tr><td><b>'+label+'</b></td><td>'+esc(value)+'</td></tr>'
            text+='</table><h4>Unchanged original header</h4><pre>'+esc('\n'.join(meta['header']))+'</pre>'
        self.overview.setHtml(text)

    def remove_selected(self):
        index=self.table.currentRow()
        if index<0: self.message.setText('Select a catalogue link first.'); return False
        if QMessageBox.question(self,'Remove catalogue link','Remove this link from the network draft? The original raw file stays on disk.',
            QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.Cancel,QMessageBox.StandardButton.Cancel)!=QMessageBox.StandardButton.Yes: return False
        self.owner.project=n.remove_record(self.owner.snapshot(),'raw_sources',index)
        self.owner.dirty=True; self.owner.refresh(); self.owner.changed(); return True

    def select_record(self,kind,identifier):
        for index,row in enumerate(self.owner.project.get('raw_sources',[])):
            if row['id']==identifier: self.table.selectRow(index); self.table_selected(); return

    def load_examples(self):
        try:
            project=self.owner.snapshot()
            if not project['synthetic']: raise ValueError('Load a synthetic network example first; real projects are never relabelled as synthetic.')
            points={p['id'] for p in project['points']}; sessions={s['id'] for s in project['sessions']}
            if not {'REF0','P01'}<=points or 'S01' not in sessions: raise ValueError('Examples require existing REF0, P01 and S01 from the synthetic network inventory.')
            folder=Path(__file__).parent/'examples'
            def load(cancelled):
                updated=deepcopy(project)
                for name,role,point in (('synthetic_base.rnx','BASE','REF0'),('synthetic_rover.rnx','ROVER','P01'),('synthetic_navigation.rnx','NAV','')):
                    source=r.read_source(folder/name,cancelled)
                    review=r.ImportReview(role=role,point=point,session='S01' if point else '',utc_offset_seconds=18 if point else None,
                        time_evidence='Constructed GPS timestamps = synthetic UTC session + 18 seconds; explicit test declaration.',
                        antenna_evidence='Constructed SYNTHETIC antenna and H/E/N offsets; no real receiver or calibration.',
                        note='Constructed software fixture only; not usable for positioning.',synthetic=True,confirmed=True)
                    updated=r.plan_import(updated,source,review)
                return updated
            def finish(updated):
                if self.owner.snapshot()!=project: raise ValueError('Network changed; no examples added.')
                self.owner.project=updated; self.owner.dirty=True; self.owner.refresh(); self.owner.changed(); self.owner.tabs.setCurrentWidget(self)
                self.message.setText('Three constructed RINEX sources added without creating points, sessions or baseline observations. Save network, then check files.')
            return self.work('Load synthetic RINEX catalogue',load,finish)
        except (ValueError,OSError) as error: self.message.setText(str(error)); return False
