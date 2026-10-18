# SPDX-License-Identifier: GPL-3.0-only
"""Independent Qt6/QGIS4 prototype: descriptive CSV checks and SALSA engine demo."""
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

PACKAGE = Path(__file__).parent


class SurveyAdjustmentPlugin:
    def __init__(self,iface): self.iface,self.dialog = iface,None
    def initGui(self):
        self.action = QAction('Survey Adjustment – GNSS og SALSA',self.iface.mainWindow())
        self.action.triggered.connect(self.run)
        self.iface.addPluginToMenu('Survey Adjustment',self.action)
    def run(self):
        if self.dialog is None: self.dialog = SurveyDialog(self.iface)
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()
    def unload(self):
        if self.dialog:
            self.dialog.stop_engine()
            self.dialog.close()
            self.dialog.deleteLater()
        self.iface.removePluginMenu('Survey Adjustment',self.action)
        self.action.deleteLater()


class SurveyDialog(QDialog):
    def __init__(self,iface):
        super().__init__(iface.mainWindow())
        self.iface = iface
        self.setWindowTitle('Survey Adjustment 0.1.1 – GNSS / SALSA-utkast')
        self.resize(1080,760)
        self.table,self.reference_table,self.result,self.engine_result = None,None,None,None
        self.process = None
        self.synthetic = False
        layout = QVBoxLayout(self)
        label = QLabel('Uavhengig prototype for QGIS 4. Gjentakskontroll og SALSA har separate resultater. '
                       'Originalfilene beholdes. Eksempeldata er syntetiske.')
        label.setWordWrap(True)
        layout.addWidget(label)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        page = QDialog()
        form = QFormLayout(page)
        row = QHBoxLayout()
        for title,callback in [('Last Emlid-eksempel',self.load_demo),('Åpne måle-CSV …',self.open_csv),('Åpne referanse-CSV …',self.open_reference),('Fjern referanse',self.clear_reference)]:
            button = QPushButton(title)
            button.clicked.connect(callback)
            row.addWidget(button)
        form.addRow(row)
        self.source_info = QLabel('Velg en CSV eller last eksempelet. Ingen tilkobling til antenner.')
        self.source_info.setWordWrap(True)
        form.addRow(self.source_info)
        self.mapping = QTableWidget(len(ALIASES),4)
        self.mapping.setHorizontalHeaderLabels(['Bruk','Målefil','Referansefil','Hva fylles inn?'])
        self.mapping.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        form.addRow(self.mapping)
        self.crs = QComboBox()
        for zone in range(31,37): self.crs.addItem(f'EUREF89 / UTM {zone}N',f'EPSG:258{zone}')
        self.crs.setCurrentIndex(1)
        form.addRow('Koordinatsystem',self.crs)
        self.height = QComboBox()
        self.height.addItems(['Velg høydegrunnlag','NN2000','NN54','ELLIP','LOKAL'])
        form.addRow('Høydegrunnlag for Elevation/Z',self.height)
        self.confirm = QCheckBox('Begge filer har samme valgte koordinatsystem og høydegrunnlag; E/N/Z er meter')
        form.addRow(self.confirm)
        self.only_fix = QCheckBox('Bruk bare FIX/RTK FIX i beregningen (andre målinger listes som utelatt)')
        self.only_fix.setChecked(True)
        form.addRow(self.only_fix)
        self.h_tol,self.v_tol = QDoubleSpinBox(),QDoubleSpinBox()
        for widget,value in ((self.h_tol,.03),(self.v_tol,.05)):
            widget.setRange(.0001,100.)
            widget.setDecimals(4)
            widget.setSingleStep(.01)
            widget.setValue(value)
            widget.setSuffix(' m')
        form.addRow('Din grense i plan (demo: 3 cm)',self.h_tol)
        form.addRow('Din grense i høyde (demo: 5 cm)',self.v_tol)
        hint = QLabel('Kontrollpunkt-ID grupperer gjentatte målinger av samme fysiske punkt. '
                      'Ikke velg Code hvis flere ulike punkter deler koden. Navnesuffiks fjernes ikke automatisk.\n'
                      'Elevation skal være ferdig punkt-/bakkehøyde i valgt datum. Antennehøyde trekkes ikke fra igjen. '
                      'RMS-verdier brukes ikke som vekter. Grensene er dine kontrollvalg, ikke en leveransestandard.')
        hint.setWordWrap(True)
        form.addRow(hint)
        button = QPushButton('Beregn gjentakskontroll →')
        button.clicked.connect(self.calculate)
        form.addRow(button)
        self.tabs.addTab(page,'1. Emlid / gjentak')
        page = QDialog()
        result_layout = QVBoxLayout(page)
        self.summary = QLabel('Ingen beregning kjørt.')
        self.summary.setWordWrap(True)
        result_layout.addWidget(self.summary)
        self.results = QTableWidget(0,9)
        self.results.setHorizontalHeaderLabels(['Punkt','Antall','Status','Maks rest plan (m)','Maks rest Z (m)','Middel−ref E (m)','Middel−ref N (m)','Middel−ref Z (m)','Merknad'])
        self.results.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.results.horizontalHeader().setSectionResizeMode(8,QHeaderView.ResizeMode.Stretch)
        result_layout.addWidget(self.results)
        self.report = QTextBrowser()
        result_layout.addWidget(self.report)
        actions = QHBoxLayout()
        for title,callback in [('Vis resultater i kart',self.add_layers),('Lagre rapportpakke …',self.save_results)]:
            button = QPushButton(title)
            button.clicked.connect(callback)
            actions.addWidget(button)
        result_layout.addLayout(actions)
        self.tabs.addTab(page,'2. Kontrollresultat')
        page = QDialog()
        engine_layout = QFormLayout(page)
        info = QLabel('SALSA kjører her et eget syntetisk 3D-målenett: seks GNSS-vektorer, ett fast punkt og to ukjente punkter. '
                      'Resultatene sammenlignes med en uavhengig kontrollberegning.\n'
                      'Egne Emlid-koordinater fra fane 1 blir ikke automatisk til GNSS-baselinjer. '
                      'Import av egne observasjonsnett og full kovariansmodell kommer i en senere versjon.')
        info.setWordWrap(True)
        engine_layout.addRow(info)
        self.solver = QLineEdit(str(QSettings().value('survey_adjustment/solver','')))
        row = QHBoxLayout()
        row.addWidget(self.solver)
        button = QPushButton('Velg lsasolver …')
        button.clicked.connect(self.choose_solver)
        row.addWidget(button)
        engine_layout.addRow('SALSA-motor',row)
        self.engine_run = QPushButton('Kjør syntetisk SALSA-eksempel …')
        self.engine_run.clicked.connect(self.run_engine)
        engine_layout.addRow(self.engine_run)
        button = QPushButton('Stopp beregningen')
        button.clicked.connect(self.stop_engine)
        engine_layout.addRow(button)
        self.engine_log = QTextBrowser()
        self.engine_log.setPlainText('SALSA må velges separat. Motoren følger ikke med pluginpakken. Ingen måledata sendes på nett.')
        engine_layout.addRow(self.engine_log)
        self.tabs.addTab(page,'3. SALSA-motor')
        page = QDialog()
        docs_layout = QVBoxLayout(page)
        docs = QTextBrowser()
        docs.setPlainText((PACKAGE/'README.txt').read_text(encoding='utf-8') if (PACKAGE/'README.txt').exists() else 'Se dokumentasjonen i pluginmappen.')
        docs_layout.addWidget(docs)
        self.tabs.addTab(page,'4. Hjelp og rettigheter')
        self.status = QLabel('Velg eksempeldata eller egne CSV-filer.')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        close = QPushButton('Lukk')
        close.clicked.connect(self.close)
        layout.addWidget(close)
        for widget in (self.crs,self.height): widget.currentIndexChanged.connect(self.frame_changed)
        for widget in (self.confirm,self.only_fix): widget.toggled.connect(self.invalidate)
        for widget in (self.h_tol,self.v_tol): widget.valueChanged.connect(self.invalidate)
        self.rebuild_mappings()

    def error(self,error): QMessageBox.warning(self,'Survey Adjustment',str(error))
    def invalidate(self,*_):
        self.result = None
        self.summary.setText('Oppsettet er endret. Kjør gjentakskontrollen på nytt.')
        self.report.clear()
        self.results.setRowCount(0)
    def frame_changed(self,*_):
        self.confirm.setChecked(False)
        self.invalidate()

    def rebuild_mappings(self):
        labels = {'name':'Målingens navn','group':'Kontrollpunkt-ID','e':'Øst (E)','n':'Nord (N)','z':'Høyde (Z)','status':'Løsningsstatus','time':'Måletid'}
        help_text = {'name':'Identifiserer hver måling.','group':'Samme ID = samme fysiske kontrollpunkt.',
                     'e':'Projisert østkoordinat i meter.','n':'Projisert nordkoordinat i meter.',
                     'z':'Ferdig punkt-/bakkehøyde i valgt datum.', 'status':'FIX, FLOAT osv. Tom verdi er ikke dokumentert FIX.',
                     'time':'Brukes til å varsle om manglende eller like måletider.'}
        for row,field in enumerate(ALIASES):
            self.mapping.setItem(row,0,self.item(labels[field]))
            self.mapping.setItem(row,3,self.item(help_text[field]))
            for col,table in ((1,self.table),(2,self.reference_table)):
                combo = QComboBox()
                combo.addItem('— ingen —','')
                for header in table['headers'] if table else []: combo.addItem(header,header)
                mapping = default_mapping(table['headers']) if table else {}
                combo.setCurrentIndex(max(0,combo.findData(mapping.get(field,''))))
                combo.currentIndexChanged.connect(self.invalidate)
                self.mapping.setCellWidget(row,col,combo)
        self.confirm.setChecked(False)
        self.invalidate()
        self.source_info.setText('\n'.join(f"{'Målinger' if index==0 else 'Referanse'}: {Path(t['path']).name} – {len(t['rows'])} rader"
                                for index,t in enumerate((self.table,self.reference_table)) if t) or 'Ingen fil valgt.')

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
            self.status.setText('Syntetisk Emlid-eksempel lastet. Bekreft UTM 32 / NN2000 før kontroll.')
        except Exception as error: self.error(error)
    def open_csv(self):
        path,_ = QFileDialog.getOpenFileName(self,'Åpne Emlid-/måle-CSV','','CSV (*.csv *.txt)')
        if path:
            try:
                self.table = read_table(path)
                self.synthetic = False
                self.rebuild_mappings()
            except Exception as error: self.error(error)
    def open_reference(self):
        path,_ = QFileDialog.getOpenFileName(self,'Åpne referansepunkter i samme CRS/høydegrunnlag','','CSV (*.csv *.txt)')
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
            if not self.table: raise ValueError('Velg målefil eller last Emlid-eksempelet.')
            self.invalidate()
            options = self.options()
            for table in (self.table,self.reference_table):
                if not table: continue
                for row in table['rows']:
                    declared = row.get('CS name','')
                    code = re.search(r'EPSG\s*:\s*(\d+)',declared,re.I)
                    if code and 'EPSG:'+code[1] != options.crs:
                        raise ValueError('CSV oppgir et annet koordinatsystem enn valget: '+declared)
                    datum = row.get('Height datum','')
                    if datum and datum != options.height:
                        raise ValueError('CSV oppgir et annet høydegrunnlag enn valget: '+datum)
            refs = references(self.reference_table,self.mappings(2)) if self.reference_table else {}
            result = analyze(observations(self.table,self.mappings(1)),refs,options)
            result['synthetic'] = self.synthetic
            result['plugin_version'] = '0.1.1'
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
                    item.setBackground(QColor({'INNENFOR':'#bbebcb','AVVIK':'#ffc9c9','UAVKLART':'#ffe1a2'}[g['status']]))
                    item.setForeground(QColor('#111111'))
                self.results.setItem(row,col,item)
        self.summary.setText(f"{len(self.result['groups'])} kontrollpunkter • {self.result['used_count']} brukte målinger • {len(self.result['excluded'])} utelatt. "
                             'Uvektet gjentakskontroll; SALSA er ikke brukt i denne beregningen.')
        self.report.setPlainText(text_report(self.result))
        self.status.setText('Kontroll beregnet. AVVIK/UAVKLART beholdes i rapporten; ingen uteliggere fjernes automatisk.')
    def add_layers(self):
        if not self.result:
            self.error('Beregn på nytt før du oppretter resultatlag.')
            return
        try:
            layers = result_layers(self.result)
            project = QgsProject.instance()
            group = project.layerTreeRoot().addGroup('Survey Adjustment – syntetisk prøve' if self.synthetic else 'Survey Adjustment – gjentak')
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
            self.status.setText('Fire separate minnelag opprettet. Målelaget beholder faktisk XYZ. Lagre ønskede lag i prosjektets arbeidsformat.')
        except Exception as error: self.error(error)
    def save_results(self):
        if not self.result:
            self.error('Beregn på nytt før rapporten lagres.')
            return
        folder = QFileDialog.getExistingDirectory(self,'Velg mappe for ny rapportpakke')
        if folder:
            try:
                path = save_bundle(folder,self.result)
                self.status.setText('Lagret rapportpakke: '+str(path))
                QMessageBox.information(self,'Rapportpakke lagret',str(path))
            except Exception as error: self.error(error)
    def choose_solver(self):
        path,_ = QFileDialog.getOpenFileName(self,'Velg SALSAs lsasolver-program','','SALSA (lsasolver.exe lsasolver);;Alle filer (*)')
        if path:
            self.solver.setText(path)
            QSettings().setValue('survey_adjustment/solver',path)
    def run_engine(self):
        if self.process and self.process.state()!=QProcess.ProcessState.NotRunning: return
        try:
            program = solver_program(self.solver.text())
            parent = QFileDialog.getExistingDirectory(self,'Velg mappe for nytt syntetisk SALSA-resultat')
            if not parent: return
            folder = Path(parent)/('SALSA_demo_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
            prepare_demo(folder)
            self.engine_result = None
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
            self.engine_log.setPlainText('Kjører syntetisk eksempel i SALSA. Emlid-kontrollen er separat.\n'+str(folder))
            self.process.start()
        except Exception as error: self.error(error)
    def engine_error(self,error):
        if error==QProcess.ProcessError.FailedToStart:
            self.timer.stop()
            self.engine_run.setEnabled(True)
            self.engine_log.setPlainText('SALSA kunne ikke starte. Kontroller programsti og SALSAs egne avhengigheter.')
    def engine_finished(self,exit_code,*_):
        self.timer.stop()
        self.engine_run.setEnabled(True)
        console = bytes(self.process.readAllStandardOutput())+b'\n'+bytes(self.process.readAllStandardError())
        (self.engine_folder/'console.txt').write_bytes(console)
        try:
            self.engine_result = inspect_result(self.engine_folder,exit_code,self.engine_program)
            data = self.engine_result
            self.engine_log.setPlainText(f"SALSA {data['version']}: beregning fullført.\n"
                f"Uavhengig eksempelkontroll: BESTÅTT, største XYZ-forskjell {data['maximum_difference_metres']:.9g} m.\n"
                'Referanse: WGS84 ECEF XYZ, ellipsoidiske høyder. Ingen NN2000-transformasjon.\n'
                'Varsler fra SALSA må vurderes:\n'+'\n'.join(data['warnings'])+'\n\nResultatmappe: '+str(self.engine_folder)+
                '\n\n'+(self.engine_folder/'solution.out').read_text(encoding='utf-8',errors='replace'))
            self.status.setText('SALSA-eksempelet er beregnet og kontrollert. Se varsler og rapport i resultatmappen.')
        except Exception as error:
            self.engine_result = None
            self.engine_log.setPlainText('SALSA-resultatet ble ikke godkjent av programkoblingen: '+str(error)+'\n'+str(self.engine_folder))
    def stop_engine(self):
        if self.process and self.process.state()!=QProcess.ProcessState.NotRunning:
            self.process.kill()
            self.engine_result = None
            self.engine_log.setPlainText('SALSA stoppet. Delvise resultatfiler skal ikke brukes.')
