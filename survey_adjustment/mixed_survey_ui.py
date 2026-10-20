# SPDX-License-Identifier: GPL-3.0-only
"""English, receiver-neutral mixed CSV workflow; immutable network snapshots."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from uuid import uuid4

from qgis.PyQt.QtCore import QProcess,QProcessEnvironment,QTimer,QUrl,QMetaType
from qgis.PyQt.QtGui import QColor,QDesktopServices
from qgis.PyQt.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QFormLayout,QLabel,QLineEdit,QComboBox,
    QCheckBox,QPushButton,QFileDialog,QMessageBox,QTabWidget,QTextBrowser,QDialogButtonBox)
from qgis.core import QgsProject,QgsGeometry,QgsPoint,QgsLineString
from . import mixed_survey as m,network as n,engine
from .network_quality_ui import NetworkQualityDialog
from .layers import make_layer


class MixedSurveyDialog(QDialog):
    def __init__(self,parent,project,host):
        super().__init__(parent)
        self.host=host; self.project=deepcopy(project); self.source=None
        self.result=self.folder=self.job=self.program=None
        self.process=None; self.aborted=False; self.dirty=False; self.saved_path=None; self.saved_digest=None
        self.setWindowTitle('Mixed survey - total station and levelling'); self.resize(1140,870)
        layout=QVBoxLayout(self)
        self.banner=QLabel(); self.banner.setWordWrap(True); layout.addWidget(self.banner)
        self.controls=QDialog(); form=QFormLayout(self.controls)
        row=QHBoxLayout()
        for label,callback in [('Open survey CSV …',self.open_csv),('Open mixed project …',self.open_project),
                               ('Save mixed project …',self.save_project),('Save source CSV / template …',self.save_csv)]:
            button=QPushButton(label); button.clicked.connect(callback); row.addWidget(button)
        form.addRow(row)
        row=QHBoxLayout()
        for label,profile in [('Load synthetic 3D mixed example','MIXED_3D'),('Load synthetic NN2000 levelling example','LEVEL_1D')]:
            button=QPushButton(label); button.clicked.connect(lambda _=False,p=profile:self.load_example(p)); row.addWidget(button)
        form.addRow(row)
        self.profile=QComboBox(); self.profile.addItem('Mixed 3D: GNSS + slope distances + angles + ELLIP height differences','MIXED_3D')
        self.profile.addItem('Levelling 1D: height differences in the project height datum; E/N retained','LEVEL_1D')
        form.addRow('Calculation profile',self.profile)
        self.alpha=QLineEdit('0.05')
        self.alpha.setToolTip('Two-sided global chi-square and corrected local residual/check test significance. This is not a delivery tolerance.')
        form.addRow('Significance alpha',self.alpha)
        self.source_label=QLabel('No survey CSV selected.'); self.source_label.setWordWrap(True); form.addRow(self.source_label)
        self.synthetic_csv=QCheckBox('This CSV contains constructed synthetic training data only (must match the network)')
        form.addRow(self.synthetic_csv)
        self.solver=QLineEdit(host.solver.text()); row=QHBoxLayout(); row.addWidget(self.solver)
        button=QPushButton('Choose SALSA …'); button.clicked.connect(self.choose_solver); row.addWidget(button); form.addRow('SALSA lsasolver',row)
        self.note=QLineEdit(); self.note.setPlaceholderText('Describe uncertainty, session dependence, endpoint reductions and the coordinate/height reference')
        form.addRow('Model review',self.note)
        self.frame_review=QCheckBox('I verified the reference frame, coordinate epoch and height basis for every active measurement')
        self.reduction_review=QCheckBox('I verified metres / degrees / gon, clockwise angles, face reductions, instrument/target heights and prior corrections')
        self.independence_review=QCheckBox('I reviewed independent blocks; assessed 1-sigma includes centring, height and reduction uncertainty')
        for checkbox in (self.frame_review,self.reduction_review,self.independence_review): form.addRow(checkbox)
        layout.addWidget(self.controls)
        self.help=QLabel('CSV is receiver-neutral and uses the documented template. DISTANCE is slope distance, never grid/horizontal distance. '
            'HORIZONTAL_ANGLE is clockwise from backsight to target at from_point; zenith is 0 upward / 90 degrees or 100 gon horizontal. '
            'Angles must already refer to ELLIPSOID_NORMAL; raw gravity-referenced angles, DOV, atmosphere, face/prism corrections and geoid conversion are not inferred. '
            'For NN2000/NN54 use Levelling 1D with reduced point-to-point height differences. Original network records remain unchanged.')
        self.help.setWordWrap(True); layout.addWidget(self.help)
        row=QHBoxLayout()
        self.preview_button=QPushButton('Check CSV / preview adjustment'); self.preview_button.clicked.connect(self.preview)
        self.run_button=QPushButton('Run SALSA and verify'); self.run_button.clicked.connect(self.start)
        self.stop_button=QPushButton('Stop'); self.stop_button.clicked.connect(self.stop); self.stop_button.setEnabled(False)
        self.map_button=QPushButton('Show result on map (with Z)'); self.map_button.clicked.connect(self.add_map); self.map_button.setEnabled(False)
        self.folder_button=QPushButton('Open report folder'); self.folder_button.clicked.connect(self.open_folder); self.folder_button.setEnabled(False)
        for button in (self.preview_button,self.run_button,self.stop_button,self.map_button,self.folder_button): row.addWidget(button)
        layout.addLayout(row)
        self.status=QLabel('Load an example or open a survey CSV, review the model, then calculate.'); self.status.setWordWrap(True); layout.addWidget(self.status)
        self.delivery_button=QPushButton('Norwegian reference review / delivery …'); self.delivery_button.setEnabled(False)
        self.delivery_button.clicked.connect(self.open_delivery); layout.addWidget(self.delivery_button)
        self.tabs=QTabWidget(); layout.addWidget(self.tabs,1)
        self.overview=QTextBrowser(); self.tabs.addTab(self.overview,'Overview / model warnings')
        self.source_table=NetworkQualityDialog.table(['ID','Kind','Session','From / station','To','Backsight','Value','Unit','1-sigma','HI','HT','Back HT','Use / enabled'])
        self.point_table=NetworkQualityDialog.table(['Point','Role','Easting','Northing','Point height','Sigma geodetic E','Sigma geodetic N','Sigma height','Review'])
        self.residual_table=NetworkQualityDialog.table(['Observation / prior','Kind','Unit','Residuals','Standardized','Mean redundancy','Weakest direction','Review'])
        self.check_table=NetworkQualityDialog.table(['Check','Status','Unit','Residual','Pvalue'])
        for widget,title in ((self.source_table,'Survey CSV / units'),(self.point_table,'Adjusted points'),(self.residual_table,'Residuals / redundancy'),(self.check_table,'Held-out checks')):
            self.tabs.addTab(widget,title)
        self.csv_help=QTextBrowser()
        help_path=Path(__file__).parent/'MIXED_SURVEY.txt'
        if not help_path.exists(): help_path=Path(__file__).parent.parent/'docs/MIXED_SURVEY.txt'
        self.csv_help.setPlainText(help_path.read_text(encoding='utf-8') if help_path.exists() else 'See docs/MIXED_SURVEY.txt for the CSV template, units and reduction conventions.')
        self.tabs.addTab(self.csv_help,'CSV field help')
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Close); buttons.rejected.connect(self.reject); layout.addWidget(buttons)
        self.timer=QTimer(self); self.timer.setSingleShot(True); self.timer.timeout.connect(self.stop)
        self.profile.currentIndexChanged.connect(self.invalidate)
        self.alpha.textChanged.connect(self.invalidate)
        for field in (self.note,self.solver): field.textChanged.connect(self.invalidate)
        for field in (self.frame_review,self.reduction_review,self.independence_review,self.synthetic_csv): field.toggled.connect(self.invalidate)
        self.update_banner()

    def update_banner(self):
        self.banner.setText(('SYNTHETIC TRAINING DATA - ' if self.project['synthetic'] else 'PRIVATE LOCAL DATA - ')+
            self.project['name']+' | '+self.project['crs']+' | '+self.project['height_datum']+
            '. This separate snapshot does not overwrite your network or QGIS project.')

    def options(self):
        from .network_ui import number
        return m.MixedOptions(profile=self.profile.currentData(),frame_reviewed=self.frame_review.isChecked(),
            reductions_reviewed=self.reduction_review.isChecked(),independence_reviewed=self.independence_review.isChecked(),model_note=self.note.text().strip(),alpha=number(self.alpha.text()))

    def current_job(self):
        if self.source is None: raise ValueError('Open a survey CSV or load a synthetic example first.')
        source=deepcopy(self.source); source['synthetic']=self.synthetic_csv.isChecked()
        return m.snapshot(self.project,source,self.options())

    def invalidate(self,*_):
        if self.process and self.process.state()!=QProcess.ProcessState.NotRunning: self.stop()
        self.result=None; self.dirty=True; self.map_button.setEnabled(False); self.folder_button.setEnabled(False)
        self.delivery_button.setEnabled(False)
        self.overview.clear()
        for table in (self.point_table,self.residual_table,self.check_table): table.setRowCount(0)
        self.status.setText('Settings changed. Check the CSV or calculate again; old results are no longer current.')

    def set_source(self,source):
        rows=m.parse_source(source)
        self.source=deepcopy(source); self.invalidate()
        self.synthetic_csv.setChecked(source['synthetic'])
        self.source_label.setText(f"{source['name']}: {len(rows)} survey records, SHA256 {source['sha256'][:16]}…")
        self.source_table.setRowCount(len(rows))
        from qgis.PyQt.QtWidgets import QTableWidgetItem
        for index,o in enumerate(rows):
            values=[o[k] for k in ('id','kind','session','from_point','to_point','backsight','value','unit','sigma','instrument_height','target_height','backsight_height')]+[o['use']+' / '+str(o['enabled'])]
            for column,value in enumerate(values):
                item=QTableWidgetItem('' if value is None else str(value)); item.setToolTip(json.dumps(o,ensure_ascii=False,indent=2))
                if not o['enabled']: item.setBackground(QColor('#eeeeee'))
                self.source_table.setItem(index,column,item)

    def load_example(self,profile='MIXED_3D'):
        if self.dirty and self.source is not None and not self.maybe_leave(): return False
        job=m.synthetic_job(profile); self.project=job['network']; self.saved_path=self.saved_digest=None
        self.profile.setCurrentIndex(self.profile.findData(profile))
        self.alpha.setText(str(job['options']['alpha']))
        self.note.setText('SYNTHETIC independent Gaussian errors; known reductions, units and physical endpoints. No field data.')
        for field in (self.frame_review,self.reduction_review,self.independence_review): field.setChecked(False)
        self.set_source(job['source']); self.update_banner(); return True

    def open_csv(self):
        path,_=QFileDialog.getOpenFileName(self,'Open documented mixed-survey CSV','','Survey CSV (*.csv);;All files (*)')
        if not path: return False
        try:
            source=m.read_source(path)
            if self.dirty and self.source is not None and not self.maybe_leave(): return False
            for field in (self.frame_review,self.reduction_review,self.independence_review): field.setChecked(False)
            self.set_source(source); return True
        except Exception as error: self.status.setText('Cannot import CSV: '+str(error)); return False

    def open_project(self):
        path,_=QFileDialog.getOpenFileName(self,'Open mixed project snapshot','','Mixed survey (*.samix.json)')
        if not path: return False
        try:
            p=Path(path)
            if p.stat().st_size>n.MAX_BYTES: raise ValueError('Mixed project exceeds 20 MB.')
            data=p.read_bytes(); job=m.loads(data.decode('utf-8-sig'))
            if self.dirty and self.source is not None and not self.maybe_leave(): return False
            self.project=job['network']; self.set_source(job['source'])
            self.profile.setCurrentIndex(self.profile.findData(job['options']['profile'])); self.note.setText(job['options']['model_note'])
            self.alpha.setText(str(job['options']['alpha']))
            for field in (self.frame_review,self.reduction_review,self.independence_review): field.setChecked(False)
            self.saved_path=p; self.saved_digest=m.digest(data); self.dirty=False; self.update_banner()
            self.status.setText('Snapshot opened. Review confirmations again before calculating.'); return True
        except Exception as error: self.status.setText('Cannot open mixed project: '+str(error)); return False

    def save_project(self):
        try:
            text=m.dumps(self.current_job())
            path,_=QFileDialog.getSaveFileName(self,'Save separate mixed project',str(self.saved_path or 'survey.samix.json'),'Mixed survey (*.samix.json)')
            if not path: return False
            if not path.lower().endswith('.json'): path+='.samix.json'
            p=Path(path)
            if self.saved_path and p.resolve()==self.saved_path.resolve() and (not p.exists() or m.digest(p.read_bytes())!=self.saved_digest):
                raise ValueError('Project changed outside this dialog. Open it again or save under a different name.')
            temporary=p.with_name('.'+p.name+'.'+uuid4().hex+'.tmp')
            try:
                with temporary.open('x',encoding='utf-8',newline='') as handle: handle.write(text)
                os.replace(temporary,p)
            finally:
                if temporary.exists(): temporary.unlink()
            self.saved_path=p; self.saved_digest=m.digest(p.read_bytes()); self.dirty=False
            self.status.setText('Mixed project saved with its unchanged source CSV and network snapshot.'); return True
        except Exception as error: self.status.setText('Cannot save mixed project: '+str(error)); return False

    def save_csv(self):
        path,_=QFileDialog.getSaveFileName(self,'Save original CSV or synthetic template','survey_observations.csv','Survey CSV (*.csv)')
        if not path: return False
        try:
            source=self.source or m.synthetic_job(self.profile.currentData())['source']
            Path(path).write_bytes(m.source_bytes(source)); self.status.setText('CSV saved. Synthetic templates are constructed training data, not your field measurements.'); return True
        except Exception as error: self.status.setText('Cannot save CSV: '+str(error)); return False

    def choose_solver(self):
        path,_=QFileDialog.getOpenFileName(self,'Choose separately installed SALSA lsasolver','','SALSA (lsasolver.exe lsasolver);;All files (*)')
        if path: self.solver.setText(path)

    def preview(self):
        try:
            job=self.current_job(); report=m.analyse(job)
            self.result=report; self.job=job; self.show_report(report)
            self.map_button.setEnabled(True); self.folder_button.setEnabled(False)
            self.status.setText('INDEPENDENT PREVIEW - SALSA has not run. No survey/delivery approval.'); return True
        except Exception as error: self.invalidate(); self.status.setText('Cannot preview: '+str(error)); return False

    def start(self):
        if self.process and self.process.state()!=QProcess.ProcessState.NotRunning: return False
        try:
            program=engine.solver_program(self.solver.text()); job=self.current_job(); m.analyse(job)
            parent=QFileDialog.getExistingDirectory(self,'Select LOCAL parent folder for a new mixed-survey report')
            if not parent: return False
            folder=Path(parent)/('Survey_Mixed_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
            m.prepare(folder,job,program)
            self.folder,self.program,self.job=folder,program,job; self.result=None; self.aborted=False
            self.map_button.setEnabled(False); self.folder_button.setEnabled(False)
            self.process=QProcess(self); env=QProcessEnvironment()
            for key,value in engine.environment(program).items(): env.insert(key,value)
            self.process.setProcessEnvironment(env); self.process.setWorkingDirectory(str(folder))
            command=m.q.network_command(program); self.process.setProgram(command[0]); self.process.setArguments(command[1:])
            self.process.finished.connect(self.finished); self.process.errorOccurred.connect(self.process_error)
            self.controls.setEnabled(False); self.preview_button.setEnabled(False); self.run_button.setEnabled(False); self.stop_button.setEnabled(True)
            self.status.setText('Running SALSA on the saved mixed snapshot. Partial files are not accepted.'); self.timer.start(90000); self.process.start(); return True
        except Exception as error: self.status.setText('Cannot run: '+str(error)); return False

    def idle(self):
        self.timer.stop(); self.controls.setEnabled(True); self.preview_button.setEnabled(True); self.run_button.setEnabled(True); self.stop_button.setEnabled(False)

    def process_error(self,error):
        if error==QProcess.ProcessError.FailedToStart:
            self.idle(); self.result=None; self.status.setText('SALSA could not start. No partial result is accepted.')

    def finished(self,exit_code,*_):
        self.idle()
        try:
            (self.folder/'console.txt').write_bytes(bytes(self.process.readAllStandardOutput())+b'\n'+bytes(self.process.readAllStandardError()))
            if self.aborted: raise ValueError('Calculation stopped; do not use partial results.')
            if m.dumps(self.current_job())!=m.dumps(self.job): raise ValueError('Settings changed during calculation; run again.')
            self.result=m.inspect_result(self.folder,exit_code,self.program); self.show_report(self.result)
            self.delivery_button.setEnabled(True)
            self.map_button.setEnabled(True); self.folder_button.setEnabled(True)
            self.status.setText('SALSA verified against the independent model. Read warnings and held-out checks; this is not survey approval.')
        except Exception as error:
            self.result=None; self.map_button.setEnabled(False); self.folder_button.setEnabled(False); self.status.setText('Result rejected: '+str(error))

    def stop(self):
        if self.process and self.process.state()!=QProcess.ProcessState.NotRunning:
            self.aborted=True; self.process.kill()

    def show_report(self,report):
        self.overview.setPlainText(m.report_text(report))
        from qgis.PyQt.QtWidgets import QTableWidgetItem
        def fill(table,rows):
            table.setRowCount(len(rows))
            for i,values in enumerate(rows):
                for j,value in enumerate(values): table.setItem(i,j,QTableWidgetItem('' if value is None else str(value)))
        fill(self.point_table,[[p['id'],p['role'],*(p['position_grid'] or [None]*3),*(p['sigma'] or [None]*3),', '.join(p['flags'])] for p in report['points']])
        fill(self.residual_table,[[o['id'],o['kind'],o['unit'],str(o['residual']),str(o['standardized']),o['redundancy_fraction'],o['minimum_direction_redundancy'],', '.join(o['flags'])] for o in report['observations']])
        fill(self.check_table,[[o['id'],o['status'],o['unit'],str(o['residual']),o['pvalue']] for o in report['checks']])

    def add_map(self):
        try:
            if not self.result or m.dumps(self.current_job())!=m.dumps(self.job): raise ValueError('Calculate the current snapshot first.')
            expected=m.analyse(self.job)
            for key in expected:
                if self.result.get(key)!=expected[key]:
                    raise ValueError('Derived mixed result differs from the recorded model. Calculate again before mapping.')
            report=self.result; positions={p['id']:p['position_grid'] for p in report['points'] if p['position_grid'] is not None}
            rows=[]
            for p in report['points']:
                if p['position_grid'] is not None:
                    rows.append(([p['id'],report['height'],report['profile'],'SALSA VERIFIED' if report.get('engine_confirmation') else 'PREVIEW'],QgsGeometry(QgsPoint(*p['position_grid']))))
            fields=[(v,QMetaType.Type.QString) for v in ('point_id','height_datum','profile','assessment')]
            point_layer=make_layer('PointZ','Mixed survey points - '+report['height'],report['point_crs'],fields,rows)
            point_layer.renderer().symbol().setColor(QColor('#246b50'))
            lines=[]
            observations,_=m._observations(self.job)
            for o in observations:
                if not o['enabled']: continue
                pairs=[(o['from_point'],o['to_point'])]
                if o.get('backsight'): pairs.append((o['from_point'],o['backsight']))
                for a,z in pairs:
                    if a in positions and z in positions:
                        lines.append(([o['id'],o['kind'],o['use']],QgsGeometry(QgsLineString([QgsPoint(*positions[a]),QgsPoint(*positions[z])]))))
            line_layer=make_layer('LineStringZ','Mixed survey links - actual point heights',report['point_crs'],[(v,QMetaType.Type.QString) for v in ('observation','kind','use')],lines)
            group=QgsProject.instance().layerTreeRoot().addGroup('Survey Adjustment - mixed snapshot - '+('SYNTHETIC' if report['synthetic'] else 'PRIVATE'))
            for layer in (point_layer,line_layer): QgsProject.instance().addMapLayer(layer,False); group.addLayer(layer)
            canvas=self.host.iface.mapCanvas(); destination=canvas.mapSettings().destinationCrs()
            from qgis.core import QgsCoordinateTransform
            if not destination.isValid():
                canvas.setDestinationCrs(point_layer.crs()); destination=point_layer.crs()
            extent=QgsCoordinateTransform(point_layer.crs(),destination,QgsProject.instance()).transformBoundingBox(point_layer.extent())
            self.host.iface.setActiveLayer(point_layer); canvas.setExtent(extent); canvas.refresh()
            return True
        except Exception as error: self.status.setText('Cannot show mixed snapshot: '+str(error)); return False

    def open_delivery(self):
        if self.result and self.result.get('engine_confirmation') and self.folder:
            from .delivery_ui import DeliveryDialog
            dialog=DeliveryDialog(self,self,self.folder/'quality.json')
            dialog.exec(); dialog.deleteLater()

    def open_folder(self):
        if self.result and self.folder: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.folder)))

    def maybe_leave(self):
        if not self.dirty or self.source is None: return True
        answer=QMessageBox.question(self,'Unsaved mixed snapshot','Save the mixed project, source CSV and settings before continuing?',
            QMessageBox.StandardButton.Save|QMessageBox.StandardButton.Discard|QMessageBox.StandardButton.Cancel,QMessageBox.StandardButton.Cancel)
        return self.save_project() if answer==QMessageBox.StandardButton.Save else answer==QMessageBox.StandardButton.Discard

    def reject(self):
        if self.process and self.process.state()!=QProcess.ProcessState.NotRunning:
            self.stop(); self.process.waitForFinished(1500)
        if self.maybe_leave(): super().reject()

    def closeEvent(self,event):
        if self.process and self.process.state()!=QProcess.ProcessState.NotRunning:
            self.stop(); self.process.waitForFinished(1500)
        if self.maybe_leave(): event.accept()
        else: event.ignore()
