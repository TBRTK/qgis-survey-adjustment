# SPDX-License-Identifier: GPL-3.0-only
"""External PPK/static job review in the existing raw network workflow."""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime,timezone
from pathlib import Path
import html,json
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QProcess,QProcessEnvironment,QTimer,QSettings,QUrl,QMetaType
from qgis.PyQt.QtGui import QColor,QDesktopServices
from qgis.PyQt.QtWidgets import (QDialog,QWidget,QVBoxLayout,QHBoxLayout,QFormLayout,QGridLayout,
    QLabel,QLineEdit,QPushButton,QComboBox,QCheckBox,QDoubleSpinBox,QTabWidget,QTextBrowser,
    QTableWidget,QTableWidgetItem,QAbstractItemView,QHeaderView,QFileDialog,QDialogButtonBox,QScrollArea)
from qgis.core import QgsCoordinateReferenceSystem,QgsGeometry,QgsPoint,QgsMarkerSymbol
from qgis.gui import QgsMapCanvas
from . import gnss_processing as p,gnss_demo as demo,network as n,engine
from .network_ui import combo
from .coordinate_job import llh
from .layers import make_layer
from .network_map_ui import InspectTool


class ProcessingDialog(QDialog):
    def __init__(self,owner):
        super().__init__(owner);self.owner=owner;self.project=owner.snapshot()
        self.process=None;self.folder=None;self.result=None;self.source=None;self.layers={};self.aborted=False;self.preparing=False
        self.setWindowTitle('GNSS PPK / static — external RTKLIB job');self.resize(1120,850)
        layout=QVBoxLayout(self)
        label=QLabel('Process existing RINEX links locally. GPS broadcast L1/L2 first profile. PPK produces a trajectory; static produces correlated filter states. Nothing is imported automatically. Base coordinates must be independently documented.')
        label.setWordWrap(True);layout.addWidget(label)
        page=QWidget();form=QGridLayout(page);self.controls=page
        self.executable=QLineEdit(str(QSettings().value('SurveyAdjustment/rtklib_ex251','')))
        choose=QPushButton('Choose RTKLIB …');choose.clicked.connect(self.choose_engine)
        form.addWidget(QLabel('rnx2rtkp.exe'),0,0);form.addWidget(self.executable,0,1,1,3);form.addWidget(choose,0,4)
        self.mode=combo({'static':'Static relative','kinematic':'PPK kinematic'})
        self.base=QComboBox();self.rover=QComboBox();self.navigation=QComboBox()
        for row,(label,field) in enumerate((('Mode',self.mode),('Existing base source',self.base),('Existing rover source',self.rover),('Existing navigation',self.navigation)),1):
            form.addWidget(QLabel(label),row,0);form.addWidget(field,row,1,1,4)
        self.base_xyz=QLineEdit();self.base_xyz.setPlaceholderText('Documented fixed base X Y Z, metres. Never taken automatically from approximate header XYZ.')
        form.addWidget(QLabel('Fixed base ECEF XYZ'),5,0);form.addWidget(self.base_xyz,5,1,1,4)
        self.frame=combo({'':'Select documented ECEF frame','EPSG:4978':'WGS84 ECEF — no realisation/epoch transform','EPSG:4936':'ETRS89 ECEF — requires independent evidence'})
        form.addWidget(QLabel('Declared frame'),6,0);form.addWidget(self.frame,6,1,1,4)
        self.frequency=combo({1:'GPS L1',2:'GPS L1+L2'},2)
        self.atmosphere=combo({'broadcast/saas':'Broadcast ionosphere / Saastamoinen','ideal-synthetic':'SYNTHETIC ONLY — no atmosphere'})
        form.addWidget(self.frequency,7,0,1,2);form.addWidget(self.atmosphere,7,2,1,3)
        self.elevation=QDoubleSpinBox();self.elevation.setRange(10,45);self.elevation.setValue(15)
        self.ratio=QDoubleSpinBox();self.ratio.setRange(3,10);self.ratio.setValue(3)
        form.addWidget(QLabel('Elevation mask °'),8,0);form.addWidget(self.elevation,8,1)
        form.addWidget(QLabel('AR ratio threshold'),8,2);form.addWidget(self.ratio,8,3)
        self.antenna_note=QLineEdit();self.antenna_note.setPlaceholderText('Header H/E/N offsets are applied to marker endpoints. Review ARP/mark and missing antenna PCV/PCO calibration.')
        self.model_note=QLineEdit();self.model_note.setPlaceholderText('Base provenance/uncertainty, coordinate epoch, broadcast model limits, dependence and intended use. FIX is not accuracy.')
        form.addWidget(QLabel('Antenna / endpoints'),9,0);form.addWidget(self.antenna_note,9,1,1,4)
        form.addWidget(QLabel('Model / frame evidence'),10,0);form.addWidget(self.model_note,10,1,1,4)
        self.reference_help=QLabel()
        self.reference_help.setWordWrap(True);form.addWidget(self.reference_help,11,0,1,5)
        self.base_review=QCheckBox('I verified the fixed base coordinates, physical point, frame and coordinate epoch')
        self.antenna_review=QCheckBox('I reviewed header H/E/N, marker endpoints and the absence of antenna calibration')
        self.model_review=QCheckBox('I reviewed model limits and dependence; FIX/formal covariance are not absolute accuracy')
        self.license_review=QCheckBox('I reviewed RTKLIB EX 2.5.1 BSD-2-Clause licence; engine is separately installed')
        for row,field in enumerate((self.base_review,self.antenna_review,self.model_review,self.license_review),12):form.addWidget(field,row,0,1,5)
        link=QLabel('<a href="'+p.ENGINE_LICENSE+'">RTKLIB licence</a> · <a href="'+p.ENGINE_RELEASE+'">Reviewed engine release</a> — engine binary is not part of the plugin ZIP')
        link.setOpenExternalLinks(True);form.addWidget(link,16,0,1,5)
        scroll=QScrollArea();scroll.setWidgetResizable(True);scroll.setWidget(page);scroll.setMaximumHeight(400);layout.addWidget(scroll)
        row=QHBoxLayout();self.example_button=QPushButton('New synthetic example for selected mode …');self.example_button.clicked.connect(self.example)
        self.run_button=QPushButton('Run local PPK / static …');self.run_button.clicked.connect(self.start)
        self.stop_button=QPushButton('Stop');self.stop_button.clicked.connect(self.stop);self.stop_button.setEnabled(False)
        self.open_button=QPushButton('Open result folder');self.open_button.clicked.connect(self.open_folder);self.open_button.setEnabled(False)
        for field in (self.example_button,self.run_button,self.stop_button,self.open_button):row.addWidget(field)
        layout.addLayout(row)
        self.status=QLabel('No calculation. Review existing links and all required fields.');self.status.setWordWrap(True);layout.addWidget(self.status)
        self.views=QTabWidget();self.views.setMinimumHeight(200);layout.addWidget(self.views,1)
        self.report=QTextBrowser();self.views.addTab(self.report,'Report / limitations')
        self.table=QTableWidget(0,7);self.table.setHorizontalHeaderLabels(['Line','Time GPST','X ECEF m','Y ECEF m','Z ECEF m','Q','Eligible'])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows);self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers);self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.views.addTab(self.table,'Solution epochs')
        map_page=QWidget();map_box=QVBoxLayout(map_page)
        hint=QLabel('Engine positions on WGS84 ellipsoid for display. PointZ preserves ellipsoidal height; ECEF Z is separate. No national reference/height approval. Click a marker to select its original epoch.')
        hint.setWordWrap(True);map_box.addWidget(hint)
        self.canvas=QgsMapCanvas();self.canvas.setDestinationCrs(QgsCoordinateReferenceSystem('EPSG:4979'));self.canvas.setCanvasColor(QColor('#f4f6f8'));map_box.addWidget(self.canvas)
        self.inspect=InspectTool(self.canvas,self,keys=['points'],kinds={'points':'processing_epoch'});self.inspect.picked.connect(self.select_record);self.canvas.setMapTool(self.inspect)
        self.views.addTab(map_page,'Computed positions')
        self.message=self.status
        self.handoff=QPushButton('Review solution through existing baseline import …');self.handoff.setEnabled(False);self.handoff.clicked.connect(self.review_baseline);layout.addWidget(self.handoff)
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Close);buttons.rejected.connect(self.reject);layout.addWidget(buttons)
        self.timer=QTimer(self);self.timer.setSingleShot(True);self.timer.timeout.connect(self.stop)
        for field in (self.executable,self.base_xyz,self.antenna_note,self.model_note):field.textChanged.connect(self.invalidate)
        for field in (self.mode,self.base,self.rover,self.navigation,self.frame,self.frequency,self.atmosphere):field.currentIndexChanged.connect(self.invalidate)
        for field in (self.elevation,self.ratio):field.valueChanged.connect(self.invalidate)
        for field in (self.base_review,self.antenna_review,self.model_review,self.license_review):field.toggled.connect(self.invalidate)
        self.populate()

    def populate(self):
        self.reference_help.setText('Network coordinate epoch: '+(self.project['epoch'] or 'MISSING')+'. Fixed base uncertainty is excluded. Header H/E/N is applied; antenna calibration and tides are not. Frame selection is a declaration, never a transformation. Keep originals and this local result folder together.')
        for combo_,role in ((self.base,'BASE'),(self.rover,'ROVER'),(self.navigation,'NAV')):
            combo_.clear();combo_.addItem('Select existing '+role+' source','')
            for source in self.project.get('raw_sources',[]):
                if source['role']==role:combo_.addItem(source['source_name']+' / '+source['point']+' / '+source['session'],source['id'])
            if combo_.count()==2:combo_.setCurrentIndex(1)

    def options(self):
        try:coordinates=tuple(float(v) for v in self.base_xyz.text().replace(',',' ').split())
        except ValueError:raise ValueError('Enter three fixed base ECEF coordinates with decimal points.') from None
        return p.ProcessingOptions(base=self.base.currentData(),rover=self.rover.currentData(),navigation=self.navigation.currentData(),
            mode=self.mode.currentData(),base_xyz=coordinates,frame=self.frame.currentData(),epoch=self.project['epoch'],
            frequencies=self.frequency.currentData(),elevation=self.elevation.value(),ratio=self.ratio.value(),atmosphere=self.atmosphere.currentData(),
            antenna_note=self.antenna_note.text().strip(),model_note=self.model_note.text().strip(),
            base_reviewed=self.base_review.isChecked(),antenna_reviewed=self.antenna_review.isChecked(),model_reviewed=self.model_review.isChecked(),license_reviewed=self.license_review.isChecked())

    def invalidate(self,*_):
        if self.preparing or (self.process and self.process.state()!=QProcess.ProcessState.NotRunning):self.stop()
        self.result=self.source=None;self.layers={};self.canvas.setLayers([]);self.canvas.hide();self.canvas.refresh()
        self.report.clear();self.table.setRowCount(0);self.handoff.setEnabled(False);self.open_button.setEnabled(False)
        self.status.setText('Settings changed. A new checked processing job is required.')

    def choose_engine(self):
        path,_=QFileDialog.getOpenFileName(self,'Choose reviewed RTKLIB EX 2.5.1','','RTKLIB (rnx2rtkp.exe)')
        if path:self.executable.setText(path)

    def example(self):
        if not self.owner.maybe_leave():return False
        parent=QFileDialog.getExistingDirectory(self,'Local folder for a NEW constructed GPS example')
        if not parent:return False
        try:
            folder=Path(parent)/('Synthetic_GPS_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
            self.project=demo.create(folder,moving=self.mode.currentData()=='kinematic')
            self.owner.replace_project(self.project,folder/'example.sanet.json');self.populate()
            self.base_xyz.setText(' '.join(f'{v:.8f}' for v in demo.BASE));self.frame.setCurrentIndex(self.frame.findData('EPSG:4978'))
            self.atmosphere.setCurrentIndex(self.atmosphere.findData('ideal-synthetic'))
            self.antenna_note.setText('SYNTHETIC electrical endpoints; zero H/E/N and antenna patterns.')
            self.model_note.setText('SYNTHETIC circular GPS orbits, ideal signals, no atmosphere; WGS84 ellipsoid test coordinates only. Not field accuracy.')
            for field in (self.base_review,self.antenna_review,self.model_review,self.license_review):field.setChecked(False)
            self.status.setText('Constructed example created in the existing network. Review fields and confirmations before running. Original user project was not overwritten.')
            return True
        except (ValueError,OSError) as error:self.status.setText('Cannot create example: '+str(error));return False

    def idle(self):
        self.timer.stop();self.controls.setEnabled(True);self.example_button.setEnabled(True);self.run_button.setEnabled(True);self.stop_button.setEnabled(False)

    def start(self):
        if self.preparing or (self.process and self.process.state()!=QProcess.ProcessState.NotRunning):return False
        try:
            executable=p.program(self.executable.text());options=self.options()
            if self.owner.snapshot()!=self.project:raise ValueError('Network changed. Reopen PPK/static before processing.')
            p.validate(self.project,options,preflight=False)
            parent=QFileDialog.getExistingDirectory(self,'Local folder for a NEW private raw-processing result')
            if not parent:return False
            self.invalidate();self.aborted=False
            self.folder=Path(parent)/('GNSS_'+options.mode+'_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
            self.submitted=asdict(options);self.program=executable;self.preparing=True
            self.controls.setEnabled(False);self.example_button.setEnabled(False);self.run_button.setEnabled(False);self.stop_button.setEnabled(True)
            QSettings().setValue('SurveyAdjustment/rtklib_ex251',str(executable))
            self.status.setText('Checking and copying local raw inputs in the existing background queue …');self.timer.start(120000)
            folder=self.folder;project=deepcopy(self.project)
            def preparation_failed(error):
                if (folder/'processing.json').is_file():
                    try:p.abort(folder,str(error))
                    except (ValueError,OSError):pass
                if sip.isdeleted(self):return
                self.preparing=False;self.idle();self.status.setText('Job not started: '+str(error))
            def prepared(manifest):
                if sip.isdeleted(self) or self.aborted:preparation_failed('Closed or cancelled.');return
                self.preparing=False
                if self.owner.snapshot()!=project:raise ValueError('Network changed during preparation.')
                self.launch()
            if not self.owner.raw_panel.work('Prepare PPK/static job',lambda cancelled:p.prepare(folder,project,options,executable,cancelled),prepared,preparation_failed):
                self.preparing=False;self.idle();return False
            return True
        except (ValueError,OSError) as error:self.idle();self.status.setText('Cannot run: '+str(error));return False

    def launch(self):
        self.process=QProcess(self);environment=QProcessEnvironment()
        for key,value in engine.environment(self.program).items():environment.insert(key,value)
        self.process.setProcessEnvironment(environment);self.process.setWorkingDirectory(str(self.folder))
        args=p.command(self.program);self.process.setProgram(args[0]);self.process.setArguments(args[1:])
        self.log=bytearray();self.process.readyReadStandardOutput.connect(self.drain);self.process.readyReadStandardError.connect(self.drain)
        self.process.finished.connect(self.finished);self.process.errorOccurred.connect(self.failed)
        self.status.setText('Running local '+self.submitted['mode']+' job. Original files and network observations remain unchanged.')
        self.process.start()

    def drain(self):
        data=bytes(self.process.readAllStandardOutput())+bytes(self.process.readAllStandardError())
        self.log.extend(data[:max(0,5*1024*1024-len(self.log))])

    def failed(self,error):
        if error==QProcess.ProcessError.FailedToStart:self.stop();self.status.setText('RTKLIB could not start. Partial outputs are not accepted.')

    def finished(self,exit_code,*_):
        self.idle();self.drain();(self.folder/'console.txt').write_bytes(self.log)
        try:
            if self.aborted:raise ValueError('Calculation was stopped.')
            if asdict(self.options())!=self.submitted or self.owner.snapshot()!=self.project:raise ValueError('Project or settings changed during processing.')
            self.result,self.source=p.inspect_result(self.folder,exit_code,self.program)
            self.table.setRowCount(len(self.source['rows']))
            rows=[]
            for index,row in enumerate(self.source['rows']):
                values=[row['line'],row['time'],*row['values'],row['quality'],'FIX eligible' if row['eligible'] else row['reason']]
                for column,value in enumerate(values):self.table.setItem(index,column,QTableWidgetItem(str(value)))
                lat,lon,height=llh(row['values'],'EPSG:4979')
                rows.append(([str(row['line']),row['time'],row['quality'],*row['values']],QgsGeometry(QgsPoint(lon,lat,height))))
            layer=make_layer('PointZ','Computed '+self.result['mode']+' GNSS positions','EPSG:4979',
                [('record_id',QMetaType.Type.QString),('time_gpst',QMetaType.Type.QString),('quality',QMetaType.Type.QString),
                 ('ecef_x',QMetaType.Type.Double),('ecef_y',QMetaType.Type.Double),('ecef_z',QMetaType.Type.Double)],rows)
            layer.renderer().setSymbol(QgsMarkerSymbol.createSimple({'name':'circle','color':'#177e89','size':'2'}))
            self.layers={'points':layer};self.canvas.setLayers([layer]);self.canvas.setExtent(layer.extent().buffered(.0001));self.canvas.show();self.canvas.refresh()
            esc=lambda value:html.escape(str(value))
            self.report.setHtml('<h3>'+esc(p.ENGINE_VERSION)+' — '+esc(self.result['mode'])+'</h3><p>'+str(self.result['epochs'])+' solution epochs; '+str(self.result['eligible_fix'])+' eligible FIX rows. All quality states retained.</p><p>'+esc(self.folder)+'</p><ul>'+''.join('<li>'+esc(s)+'</li>' for s in p.WARNINGS)+'</ul><p>Use Solution epochs to inspect results. Review one documented stationary occupation through the existing baseline dialog; the full trajectory is never inserted as independent point measurements.</p>')
            self.open_button.setEnabled(True);self.handoff.setEnabled(self.result['eligible_fix']>0)
            self.status.setText('Engine completed: '+str(self.result['epochs'])+' epochs; '+str(self.result['eligible_fix'])+' eligible FIX. Input/output integrity checked; review limitations. No baseline added.')
        except (ValueError,OSError) as error:
            self.result=self.source=None;self.handoff.setEnabled(False)
            if not self.aborted:p.abort(self.folder,str(error))
            self.status.setText('No accepted processing result: '+str(error))

    def select_record(self,kind,identifier):
        if self.source:
            for index,row in enumerate(self.source['rows']):
                if str(row['line'])==identifier:
                    self.views.setCurrentWidget(self.table);self.table.selectRow(index)
                    self.status.setText('Selected solution epoch '+row['time']+' GPST; Q='+row['quality']+'. Review a stationary occupation before baseline import.')
                    QTimer.singleShot(0,lambda:self.table.scrollToItem(self.table.item(index,0)))
                    return

    def review_baseline(self):
        if not self.result:return False
        # The owner routes to the same BaselineImportDialog as every POS import.
        selected=self.table.currentRow()
        if selected<0:
            self.views.setCurrentWidget(self.table);self.status.setText('Select one solution epoch first. A moving PPK trajectory is not a stationary point occupation.');return False
        success=self.owner.import_baselines(str(self.folder/'solution.pos'),line=self.source['rows'][selected]['line'])
        if success:
            self.project=self.owner.snapshot();self.handoff.setEnabled(False)
            self.status.setText('One reviewed baseline added through the existing import/audit. Save the network explicitly. Other epochs remain in the local processing result.')
        return success

    def open_folder(self):
        if self.result and self.folder:QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.folder)))

    def stop(self):
        self.aborted=True;self.result=self.source=None;self.handoff.setEnabled(False);self.open_button.setEnabled(False)
        if self.preparing:self.owner.raw_panel.cancel()
        if self.process and self.process.state()!=QProcess.ProcessState.NotRunning:self.process.kill();self.process.waitForFinished(1500)
        if self.folder:
            try:p.abort(self.folder,'Stopped, timed out or failed to start.')
            except (ValueError,OSError):pass
        self.idle();self.status.setText('Stopped. Partial result files cannot enter the baseline workflow.')

    def reject(self):
        if self.preparing or (self.process and self.process.state()!=QProcess.ProcessState.NotRunning):self.stop()
        super().reject()

    def closeEvent(self,event):
        if self.preparing or (self.process and self.process.state()!=QProcess.ProcessState.NotRunning):self.stop()
        event.accept()
