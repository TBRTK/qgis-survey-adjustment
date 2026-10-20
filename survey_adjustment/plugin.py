# SPDX-License-Identifier: GPL-3.0-only
"""Independent Qt6/QGIS4 CSV checks and explicit SALSA coordinate adjustment."""
from datetime import datetime,timezone
import json
from pathlib import Path
import re

from qgis.PyQt.QtCore import Qt,QProcess,QProcessEnvironment,QTimer,QSettings
from qgis.PyQt.QtGui import QAction,QColor
from qgis.PyQt.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QFormLayout,QLabel,
    QPushButton,QLineEdit,QFileDialog,QMessageBox,QTabWidget,QTableWidget,
    QTableWidgetItem,QComboBox,QCheckBox,QDoubleSpinBox,QTextBrowser,QHeaderView)
from qgis.core import QgsProject,QgsCoordinateReferenceSystem,QgsCoordinateTransform,QgsPointXY
from .core import ALIASES,Options,read_table,default_mapping,observations,references,analyze
from .engine import solver_program,environment,prepare_demo,command,inspect_result
from .layers import result_layers,make_layer
from .reporting import text_report,save_bundle
from .coordinate_ui import CoordinatePanel
from .network_ui import NetworkPanel
from .version import VERSION

PACKAGE = Path(__file__).parent


class SurveyAdjustmentPlugin:
    def __init__(self,iface): self.iface,self.dialog = iface,None
    def initGui(self):
        self.action = QAction('Survey Adjustment – GNSS and SALSA',self.iface.mainWindow())
        self.action.triggered.connect(self.run)
        self.iface.addPluginToMenu('Survey Adjustment',self.action)
    def run(self):
        if self.dialog is None: self.dialog = SurveyDialog(self.iface)
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()
    def unload(self):
        if self.dialog:
            if not self.dialog.network_panel.stash_recovery():
                raise RuntimeError('Unsaved network recovery could not be stored. The plugin remains active. Save the network to a file before disabling it.')
            self.dialog.stop_engine()
            self.dialog._unloading = True
            self.dialog.close()
            self.dialog.deleteLater()
        self.iface.removePluginMenu('Survey Adjustment',self.action)
        self.action.deleteLater()


class SurveyDialog(QDialog):
    def __init__(self,iface):
        super().__init__(iface.mainWindow())
        self.iface = iface
        self.setWindowTitle(f'Survey Adjustment {VERSION} – GNSS / SALSA prototype')
        self.resize(1080,760)
        self.table,self.reference_table,self.result,self.engine_result = None,None,None,None
        self.process = None
        self.synthetic = False
        layout = QVBoxLayout(self)
        label = QLabel('Independent prototype for QGIS 4. Repeatability checks and SALSA produce separate results. '
                       'Original files are preserved. Example data are synthetic.')
        label.setWordWrap(True)
        layout.addWidget(label)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        page = QDialog()
        form = QFormLayout(page)
        row = QHBoxLayout()
        for title,callback in [('Load synthetic GNSS example',self.load_demo),('Open measurement file …',self.open_csv),('Open reference file …',self.open_reference),('Clear reference',self.clear_reference)]:
            button = QPushButton(title)
            button.clicked.connect(callback)
            row.addWidget(button)
        form.addRow(row)
        self.source_info = QLabel('GNSS from different receivers: choose CSV/TXT/TSV with column headers or load the example. No connection to receivers.')
        self.source_info.setWordWrap(True)
        form.addRow(self.source_info)
        self.mapping = QTableWidget(len(ALIASES),4)
        self.mapping.setHorizontalHeaderLabels(['Use','Measurement file','Reference file','What to enter'])
        self.mapping.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        form.addRow(self.mapping)
        self.crs = QComboBox()
        for zone in range(31,37): self.crs.addItem(f'EUREF89 / UTM {zone}N',f'EPSG:258{zone}')
        self.crs.setCurrentIndex(1)
        form.addRow('Coordinate system',self.crs)
        self.height = QComboBox()
        self.height.addItems(['Select height datum','NN2000','NN54','ELLIP','LOCAL'])
        form.addRow('Height datum for Elevation/Z',self.height)
        self.confirm = QCheckBox('Both files use the selected coordinate system and height datum; E/N/Z are in metres')
        form.addRow(self.confirm)
        self.only_fix = QCheckBox('Use only FIX/RTK FIX measurements (others are listed as excluded)')
        self.only_fix.setChecked(True)
        form.addRow(self.only_fix)
        self.h_tol,self.v_tol = QDoubleSpinBox(),QDoubleSpinBox()
        for widget,value in ((self.h_tol,.03),(self.v_tol,.05)):
            widget.setRange(.0001,100.)
            widget.setDecimals(4)
            widget.setSingleStep(.01)
            widget.setValue(value)
            widget.setSuffix(' m')
        form.addRow('Your horizontal tolerance (example: 3 cm)',self.h_tol)
        form.addRow('Your vertical tolerance (example: 5 cm)',self.v_tol)
        hint = QLabel('Control point ID groups repeated measurements of the same physical point. '
                      'Do not select Code if different points share the code. Name suffixes are not removed automatically.\n'
                      'Elevation must be the final point/ground height in the selected datum. Antenna height is not subtracted again. '
                      'RMS values are not used as weights. Tolerances are your check settings, not a delivery standard.')
        hint.setWordWrap(True)
        form.addRow(hint)
        button = QPushButton('Calculate repeatability →')
        button.clicked.connect(self.calculate)
        form.addRow(button)
        self.tabs.addTab(page,'1. GNSS / repeats')
        page = QDialog()
        result_layout = QVBoxLayout(page)
        self.summary = QLabel('No calculation has been run.')
        self.summary.setWordWrap(True)
        result_layout.addWidget(self.summary)
        self.results = QTableWidget(0,9)
        self.results.setHorizontalHeaderLabels(['Point','Count','Status','Max horizontal residual (m)','Max Z residual (m)','Mean−ref E (m)','Mean−ref N (m)','Mean−ref Z (m)','Note'])
        self.results.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.results.horizontalHeader().setSectionResizeMode(8,QHeaderView.ResizeMode.Stretch)
        result_layout.addWidget(self.results)
        self.report = QTextBrowser()
        result_layout.addWidget(self.report)
        actions = QHBoxLayout()
        for title,callback in [('Show results on map',self.add_layers),('Save report package …',self.save_results)]:
            button = QPushButton(title)
            button.clicked.connect(callback)
            actions.addWidget(button)
        result_layout.addLayout(actions)
        self.tabs.addTab(page,'2. Check results')
        page = QDialog()
        engine_layout = QFormLayout(page)
        info = QLabel('SALSA runs a separate synthetic 3D network here: six GNSS vectors, one fixed point and two unknown points. '
                      'Results are compared with an independent calculation.\n'
                      'Your GNSS coordinates from tab 1 are not converted automatically into GNSS baselines. '
                      'Your coordinate observations with documented uncertainty can be adjusted on tab 4. '
                      'Raw GNSS baselines/RINEX are not supported by this profile.')
        info.setWordWrap(True)
        engine_layout.addRow(info)
        self.solver = QLineEdit(str(QSettings().value('survey_adjustment/solver','')))
        row = QHBoxLayout()
        row.addWidget(self.solver)
        button = QPushButton('Select lsasolver …')
        button.clicked.connect(self.choose_solver)
        row.addWidget(button)
        engine_layout.addRow('SALSA engine',row)
        self.engine_run = QPushButton('Run synthetic SALSA example …')
        self.engine_run.clicked.connect(self.run_engine)
        engine_layout.addRow(self.engine_run)
        button = QPushButton('Stop calculation')
        button.clicked.connect(self.stop_engine)
        engine_layout.addRow(button)
        self.engine_log = QTextBrowser()
        self.engine_log.setPlainText('Select SALSA separately. The engine is not included in the plugin package. No measurements are sent online.')
        engine_layout.addRow(self.engine_log)
        self.tabs.addTab(page,'3. SALSA engine')
        self.coordinate_panel = CoordinatePanel(self)
        self.tabs.addTab(self.coordinate_panel,'4. GNSS → SALSA')
        self.network_panel = NetworkPanel(self)
        self.tabs.addTab(self.network_panel,'5. Network project')
        page = QDialog()
        docs_layout = QVBoxLayout(page)
        docs = QTextBrowser()
        docs.setPlainText((PACKAGE/'README.txt').read_text(encoding='utf-8') if (PACKAGE/'README.txt').exists() else 'See the documentation in the plugin folder.')
        docs_layout.addWidget(docs)
        self.tabs.addTab(page,'6. Help and licensing')
        self.status = QLabel('Choose example data or your own CSV files.')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        close = QPushButton('Close')
        close.clicked.connect(self.close)
        layout.addWidget(close)
        for widget in (self.crs,self.height): widget.currentIndexChanged.connect(self.frame_changed)
        for widget in (self.confirm,self.only_fix): widget.toggled.connect(self.invalidate)
        for widget in (self.h_tol,self.v_tol): widget.valueChanged.connect(self.invalidate)
        self.rebuild_mappings()

    def closeEvent(self,event):
        if not getattr(self,'_unloading',False):
            if not self.network_panel.maybe_leave():
                event.ignore()
                return
            if self.network_panel.has_changes():
                # Discard was selected; reopening the persistent dialog must not resurrect that draft.
                try:
                    path=self.network_panel.path
                    from .network import load_project,new_project
                    self.network_panel.replace_project(load_project(path) if path else new_project(),path)
                except (OSError,ValueError):
                    self.network_panel.replace_project(new_project())
        super().closeEvent(event)

    def error(self,error): QMessageBox.warning(self,'Survey Adjustment',str(error))
    def invalidate(self,*_):
        self.result = None
        self.summary.setText('Settings have changed. Recalculate the repeatability check.')
        self.report.clear()
        self.results.setRowCount(0)
    def frame_changed(self,*_):
        self.confirm.setChecked(False)
        self.invalidate()

    def rebuild_mappings(self):
        labels = {'name':'Measurement name','group':'Control point ID','e':'Easting (E)','n':'Northing (N)','z':'Height (Z)','status':'Solution status','time':'Measurement time'}
        help_text = {'name':'Identifies each measurement.','group':'Same ID = same physical control point.',
                     'e':'Projected easting in metres.','n':'Projected northing in metres.',
                     'z':'Final point/ground height in the selected datum.', 'status':'Text FIX/RTK FIX/FIXED, FLOAT, etc. Numeric codes are not interpreted automatically; document and normalise status in a working copy.',
                     'time':'Used to flag missing or duplicate measurement times.'}
        for row,field in enumerate(ALIASES):
            self.mapping.setItem(row,0,self.item(labels[field]))
            self.mapping.setItem(row,3,self.item(help_text[field]))
            for col,table in ((1,self.table),(2,self.reference_table)):
                combo = QComboBox()
                combo.addItem('— none —','')
                for header in table['headers'] if table else []: combo.addItem(header,header)
                mapping = default_mapping(table['headers']) if table else {}
                combo.setCurrentIndex(max(0,combo.findData(mapping.get(field,''))))
                combo.currentIndexChanged.connect(self.invalidate)
                self.mapping.setCellWidget(row,col,combo)
        self.confirm.setChecked(False)
        self.invalidate()
        self.source_info.setText('\n'.join(f"{'Measurements' if index==0 else 'Reference'}: {Path(t['path']).name} – {len(t['rows'])} rows"
                                for index,t in enumerate((self.table,self.reference_table)) if t) or 'No file selected.')

    @staticmethod
    def item(value):
        item = QTableWidgetItem(str(value))
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        return item
    def mappings(self,col):
        return {field:self.mapping.cellWidget(row,col).currentData() for row,field in enumerate(ALIASES)}
    def load_demo(self):
        try:
            self.table = read_table(PACKAGE/'examples/emlid_gjentak.csv')
            self.reference_table = read_table(PACKAGE/'examples/kontrollpunkter.csv')
            self.synthetic = True
            self.crs.setCurrentIndex(1)
            self.height.setCurrentText('NN2000')
            self.rebuild_mappings()
            self.status.setText('Synthetic GNSS example loaded. Confirm UTM 32 / NN2000 before checking.')
        except Exception as error: self.error(error)
    def open_csv(self):
        path,_ = QFileDialog.getOpenFileName(self,'Open GNSS measurements with column headers','','Text data (*.csv *.txt *.tsv);;All files (*)')
        if path:
            try:
                self.table = read_table(path)
                self.synthetic = False
                self.rebuild_mappings()
            except Exception as error: self.error(error)
    def open_reference(self):
        path,_ = QFileDialog.getOpenFileName(self,'Open reference points in the same CRS/height datum','','Text data (*.csv *.txt *.tsv);;All files (*)')
        if path:
            try:
                self.reference_table = read_table(path)
                self.synthetic = False
                self.rebuild_mappings()
            except Exception as error: self.error(error)
    def clear_reference(self):
        self.reference_table = None
        self.rebuild_mappings()
    def options(self):
        return Options(self.crs.currentData(),self.height.currentText(),self.confirm.isChecked(),
                       self.only_fix.isChecked(),self.h_tol.value(),self.v_tol.value())
    def calculate(self):
        try:
            if not self.table: raise ValueError('Select a measurement file or load the GNSS example.')
            self.invalidate()
            options = self.options()
            for table in (self.table,self.reference_table):
                if not table: continue
                for row in table['rows']:
                    declared = row.get('CS name','')
                    code = re.search(r'EPSG\s*:\s*(\d+)',declared,re.I)
                    if code and 'EPSG:'+code[1] != options.crs:
                        raise ValueError('The CSV declares a different coordinate system from the selected one: '+declared)
                    datum = row.get('Height datum','')
                    if datum and datum != options.height:
                        raise ValueError('The CSV declares a different height datum from the selected one: '+datum)
            refs = references(self.reference_table,self.mappings(2)) if self.reference_table else {}
            result = analyze(observations(self.table,self.mappings(1)),refs,options)
            result['synthetic'] = self.synthetic
            result['plugin_version'] = VERSION
            result['sources'] = [{k:table[k] for k in ('path','sha256','encoding','delimiter')} for table in (self.table,self.reference_table) if table]
            result['mappings'] = {'observations':self.mappings(1),'references':self.mappings(2)}
            self.result = result
            self.show_result()
            self.tabs.setCurrentIndex(1)
            return True
        except Exception as error:
            self.error(error)
            return False
    def show_result(self):
        self.results.setRowCount(len(self.result['groups']))
        for row,g in enumerate(self.result['groups']):
            ref = [f'{v:+.4f}' for v in g['reference_delta']] if g['reference_delta'] else ['—']*3
            values = [g['group'],g['count'],g['status'],f"{g['max_horizontal_residual']:.4f}",f"{g['max_vertical_residual']:.4f}",*ref,'; '.join(g['reasons'])]
            for col,value in enumerate(values):
                item = self.item(value)
                if col==2:
                    item.setBackground(QColor({'WITHIN TOLERANCE':'#bbebcb','OUTSIDE TOLERANCE':'#ffc9c9','INCONCLUSIVE':'#ffe1a2'}[g['status']]))
                    item.setForeground(QColor('#111111'))
                self.results.setItem(row,col,item)
        self.summary.setText(f"{len(self.result['groups'])} control points • {self.result['used_count']} measurements used • {len(self.result['excluded'])} excluded. "
                             'Unweighted repeatability check; SALSA was not used in this calculation.')
        self.report.setPlainText(text_report(self.result))
        self.status.setText('Check calculated. OUTSIDE TOLERANCE/INCONCLUSIVE results stay in the report; no outliers are removed automatically.')
    def add_layers(self):
        if not self.result:
            self.error('Recalculate before creating result layers.')
            return
        try:
            layers = result_layers(self.result)
            project = QgsProject.instance()
            group = project.layerTreeRoot().addGroup('Survey Adjustment – synthetic example' if self.synthetic else 'Survey Adjustment – repeatability')
            # Draw aggregate quality above individual observations and references.
            for layer in (layers[1],layers[0],layers[2],layers[3]):
                project.addMapLayer(layer,False)
                group.addLayer(layer)
            self.iface.setActiveLayer(layers[1])
            canvas = self.iface.mapCanvas()
            transform = QgsCoordinateTransform(layers[1].crs(),canvas.mapSettings().destinationCrs(),project)
            extent = transform.transformBoundingBox(layers[1].extent())
            extent.scale(1.25)
            canvas.setExtent(extent)
            canvas.refresh()
            self.status.setText('Four separate memory layers created. The measurement layer retains actual XYZ. Save the required layers in your project working format.')
        except Exception as error: self.error(error)
    def save_results(self):
        if not self.result:
            self.error('Recalculate before saving the report.')
            return
        folder = QFileDialog.getExistingDirectory(self,'Select folder for a new report package')
        if folder:
            try:
                path = save_bundle(folder,self.result)
                self.status.setText('Report package saved: '+str(path))
                QMessageBox.information(self,'Report package saved',str(path))
            except Exception as error: self.error(error)
    def choose_solver(self):
        path,_ = QFileDialog.getOpenFileName(self,'Select the SALSA lsasolver program','','SALSA (lsasolver.exe lsasolver);;All files (*)')
        if path:
            self.solver.setText(path)
            QSettings().setValue('survey_adjustment/solver',path)
    def run_engine(self):
        self.start_job(prepare_demo,'SALSA_demo_',True)
    def start_job(self,prepare,prefix,synthetic):
        if self.process and self.process.state()!=QProcess.ProcessState.NotRunning: return
        try:
            program = solver_program(self.solver.text())
            parent = QFileDialog.getExistingDirectory(self,'Select folder for a new SALSA result')
            if not parent: return
            folder = Path(parent)/(prefix+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
            prepare(folder)
            self.engine_result = None
            self.engine_aborted = False
            self.coordinate_panel.map_button.setEnabled(False)
            self.engine_program,self.engine_folder = program,folder
            self.process = QProcess(self)
            env = QProcessEnvironment()
            for name,value in environment(program).items(): env.insert(name,value)
            self.process.setProcessEnvironment(env)
            self.process.setWorkingDirectory(str(folder))
            self.process.setProgram(str(program))
            self.process.setArguments(command(program,folder)[1:])
            self.process.finished.connect(self.engine_finished)
            self.process.errorOccurred.connect(self.engine_error)
            self.timer = QTimer(self)
            self.timer.setSingleShot(True)
            self.timer.timeout.connect(self.stop_engine)
            self.timer.start(45000)
            self.engine_run.setEnabled(False)
            self.coordinate_panel.setEnabled(False)
            self.tabs.setCurrentIndex(2)
            self.engine_log.setPlainText(('Running the synthetic example in SALSA.' if synthetic else
                'Running coordinate observations in SALSA; these are not measured GNSS baselines.')+'\n'+str(folder))
            self.process.start()
        except Exception as error: self.error(error)
    def engine_error(self,error):
        if error==QProcess.ProcessError.FailedToStart:
            self.timer.stop()
            self.engine_run.setEnabled(True)
            self.coordinate_panel.setEnabled(True)
            self.engine_log.setPlainText('SALSA could not start. Check the program path and SALSA dependencies.')
    def engine_finished(self,exit_code,*_):
        self.timer.stop()
        self.engine_run.setEnabled(True)
        self.coordinate_panel.setEnabled(True)
        console = bytes(self.process.readAllStandardOutput())+b'\n'+bytes(self.process.readAllStandardError())
        (self.engine_folder/'console.txt').write_bytes(console)
        try:
            if self.engine_aborted: raise ValueError('The run was stopped; do not use partial result files.')
            self.engine_result = inspect_result(self.engine_folder,exit_code,self.engine_program)
            data = self.engine_result
            self.coordinate_panel.map_button.setEnabled(data.get('kind')=='coordinate_observations')
            self.engine_log.setPlainText(f"SALSA {data['version']}: calculation completed.\n"+
                ('Synthetic example data.\n' if data['synthetic'] else 'Your source file; not validated against your field measurements.\n')+
                f"Independent model check: PASSED, maximum XYZ difference {data['maximum_difference_metres']:.9g} m.\n"
                f"Reference frame: {data['frame']}, ellipsoidal heights. No NN2000 transformation.\n"
                'SALSA warnings require review:\n'+'\n'.join(data['warnings'])+'\n\nResult folder: '+str(self.engine_folder)+
                '\n\n'+(self.engine_folder/'solution.out').read_text(encoding='utf-8',errors='replace'))
            self.status.setText('SALSA calculation completed and model checked. Review the warnings and report in the result folder.')
        except Exception as error:
            self.engine_result = None
            self.engine_log.setPlainText('The plugin could not validate the SALSA result: '+str(error)+'\n'+str(self.engine_folder))
    def stop_engine(self):
        if self.process and self.process.state()!=QProcess.ProcessState.NotRunning:
            self.engine_aborted = True
            self.process.kill()
            self.engine_result = None
            self.engine_log.setPlainText('SALSA stopped. Do not use partial result files.')
