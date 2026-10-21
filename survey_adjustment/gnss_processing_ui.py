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
from . import gnss_processing as p,gnss_demo as demo,network as n,engine,delivery
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
        label=QLabel('Process existing RINEX links locally. Upstream EX retains its legacy calibration limits; experimental Survey SA1 adds GLONASS and separate receiver patterns for G/E/C/R. PPK/static states are correlated. Review one stationary occupation through the existing baseline import.')
        label.setWordWrap(True);layout.addWidget(label)
        profile_row=QHBoxLayout();profile_row.addWidget(QLabel('Engine profile'))
        self.engine_variant=combo({'upstream':'Upstream EX 2.5.1 — legacy G/E/C, GPS-only ANTEX',
            'survey-sa1':'Experimental Survey SA1 — legacy G/E/C/R, NOAZI ANTEX',
            'survey-sa2':'Experimental Survey SA2 — azimuth ANTEX, Galileo E1/E5a, BeiDou B1C/B3I'})
        profile_row.addWidget(self.engine_variant,1);layout.addLayout(profile_row)
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
        self.frame=combo({'':'Select documented ECEF frame','EPSG:4978':'WGS84 ECEF — no realisation/epoch transform','EPSG:4936':'ETRS89 ECEF — requires independent evidence','EPSG:9988':'ITRF2020 ECEF — document realisation and coordinate epoch'})
        form.addWidget(QLabel('Declared frame'),6,0);form.addWidget(self.frame,6,1,1,4)
        self.frequency=combo({1:'First frequency slot',2:'Two frequency slots'},2)
        self.atmosphere=combo({'broadcast/saas':'Broadcast ionosphere / Saastamoinen',p.STATIC_ATMOSPHERE:'EXPERIMENTAL Static GPS L1/L2 — estimate ionosphere / wet delay','ideal-synthetic':'SYNTHETIC ONLY — no atmosphere'})
        form.addWidget(self.frequency,7,0,1,2);form.addWidget(self.atmosphere,7,2,1,3)
        self.elevation=QDoubleSpinBox();self.elevation.setRange(10,45);self.elevation.setValue(15)
        self.ratio=QDoubleSpinBox();self.ratio.setRange(3,10);self.ratio.setValue(3)
        form.addWidget(QLabel('Elevation mask °'),8,0);form.addWidget(self.elevation,8,1)
        form.addWidget(QLabel('AR ratio threshold'),8,2);form.addWidget(self.ratio,8,3)
        self.antenna_note=QLineEdit();self.antenna_note.setPlaceholderText('Review marker-to-ARP H/E/N, antenna installation/orientation and the selected PCO/PCV model or its explicit absence.')
        self.model_note=QLineEdit();self.model_note.setPlaceholderText('Base provenance/uncertainty, coordinate epoch, broadcast model limits, dependence and intended use. FIX is not accuracy.')
        form.addWidget(QLabel('Antenna / endpoints'),9,0);form.addWidget(self.antenna_note,9,1,1,4)
        form.addWidget(QLabel('Model / frame evidence'),10,0);form.addWidget(self.model_note,10,1,1,4)
        self.systems={s:QCheckBox(p.SYSTEMS[s][0]) for s in p.SYSTEMS}
        self.systems['G'].setChecked(True);self.systems['G'].setEnabled(False)
        system_row=QHBoxLayout()
        for field in self.systems.values():system_row.addWidget(field)
        form.addWidget(QLabel('Satellite systems'),11,0);form.addLayout(system_row,11,1,1,4)
        self.calibration=combo({'none':'Explicitly uncalibrated — header H/E/N only','antex':'Receiver ANTEX PCO/PCV — reviewed engine bands'})
        form.addWidget(QLabel('Receiver calibration'),12,0);form.addWidget(self.calibration,12,1,1,4)
        self.antex=QLineEdit();self.antex.setPlaceholderText('Local absolute ANTEX 1.4; exact antenna + radome, serial/generic model, entire occupation valid')
        choose_antex=QPushButton('Choose ANTEX …');choose_antex.clicked.connect(self.choose_antex)
        form.addWidget(QLabel('ANTEX file'),13,0);form.addWidget(self.antex,13,1,1,3);form.addWidget(choose_antex,13,4)
        self.calibration_note=QLineEdit();self.calibration_note.setPlaceholderText('Source/version and usage rights; verify antenna serial, radome, mounting, north orientation and ARP height')
        review_model=QPushButton('Review antenna models …');review_model.clicked.connect(self.review_models)
        form.addWidget(QLabel('Calibration evidence'),14,0);form.addWidget(self.calibration_note,14,1,1,3);form.addWidget(review_model,14,4)
        hint=QLabel('Upstream/SA1: GPS L1/L2, Galileo E1/E5b, BeiDou B1I/B2I; SA1 adds GLONASS G1/G2. SA2: Galileo E1/E5a, BeiDou B1C/B3I, same GPS/GLONASS pairs. GPS required; GLONASS fixing off. Absolute ANTEX 1.4, zenith 0/90/5. SA2 adds complete azimuth grids. H/E/N is marker-to-ARP without PCO already included. No fixed 95 mm addition or automatic antenna/band substitution.')
        hint.setWordWrap(True);form.addWidget(hint,15,0,1,5)
        self.reference_help=QLabel()
        self.reference_help.setWordWrap(True);form.addWidget(self.reference_help,19,0,1,5)
        self.orientation_box=QWidget();orientation_layout=QGridLayout(self.orientation_box);orientation_layout.setContentsMargins(0,0,0,0)
        self.orientation_mode=combo({'':'Select antenna motion / orientation','fixed-level':'Level, fixed NRP direction throughout this occupation',
            'variable':'Changing yaw / tilt — raw SA2 processing is not supported'})
        orientation_layout.addWidget(self.orientation_mode,0,0,1,2)
        self.base_heading=QLineEdit();self.rover_heading=QLineEdit()
        for i,(label,field) in enumerate((('Base antenna NRP azimuth °',self.base_heading),('Rover antenna NRP azimuth °',self.rover_heading))):
            field.setPlaceholderText('Required: 0 <= angle < 360; decimal point')
            orientation_layout.addWidget(QLabel(label),i+1,0);orientation_layout.addWidget(field,i+1,1)
        orientation_help=QLabel('SA2: bearing of the antenna north reference point (NRP), clockwise from true/geodetic north: 0° north, 90° east. Use the antenna diagram, not the camera direction. Grid north, magnetic north and direction of travel are different. Antenna must stay level and keep this heading throughout the occupation. Changing yaw/tilt or IMU-assisted measurements need another model. Review provider notes below, including copied/unverified calibration.')
        orientation_help.setWordWrap(True);orientation_layout.addWidget(orientation_help,3,0,1,2)
        form.addWidget(self.orientation_box,16,0,3,5);self.orientation_box.hide()
        self.base_review=QCheckBox('I verified the fixed base coordinates, physical point, frame and coordinate epoch')
        self.antenna_review=QCheckBox('I verified H/E/N is marker-to-ARP, without duplicated PCO, and reviewed mounting/model or its explicit absence')
        self.model_review=QCheckBox('I reviewed model limits and dependence; FIX/formal covariance are not absolute accuracy')
        self.license_review=QCheckBox('I reviewed RTKLIB EX 2.5.1 BSD-2-Clause licence; engine is separately installed')
        for row,field in enumerate((self.base_review,self.antenna_review,self.model_review,self.license_review),20):form.addWidget(field,row,0,1,5)
        self.orientation_review=QCheckBox('I verified constant, level antenna orientation and true-north azimuths, and reviewed the calibration provider notes')
        form.addWidget(self.orientation_review,24,0,1,5);self.orientation_review.hide()
        link=QLabel('<a href="'+p.ENGINE_LICENSE+'">RTKLIB licence</a> · <a href="'+p.ENGINE_RELEASE+'">Reviewed engine release</a> — engine binary is not part of the plugin ZIP')
        link.setOpenExternalLinks(True);form.addWidget(link,25,0,1,5)
        scroll=QScrollArea();scroll.setWidgetResizable(True);scroll.setWidget(page);scroll.setMaximumHeight(400);layout.addWidget(scroll)
        row=QHBoxLayout();self.example_button=QPushButton('New synthetic example for selected mode …');self.example_button.clicked.connect(self.example)
        self.run_button=QPushButton('Run local PPK / static …');self.run_button.clicked.connect(self.start)
        self.stop_button=QPushButton('Stop');self.stop_button.clicked.connect(self.stop);self.stop_button.setEnabled(False)
        self.open_button=QPushButton('Open result folder');self.open_button.clicked.connect(self.open_folder);self.open_button.setEnabled(False)
        for field in (self.example_button,self.run_button,self.stop_button,self.open_button):row.addWidget(field)
        layout.addLayout(row)
        self.orientation_button=QPushButton('Review orientation / pole-tip coordinates from NMEA ETC …')
        self.orientation_button.clicked.connect(self.review_orientation);layout.addWidget(self.orientation_button)
        self.status=QLabel('No calculation. Review existing links and all required fields.');self.status.setWordWrap(True);layout.addWidget(self.status)
        self.views=QTabWidget();self.views.setMinimumHeight(200);layout.addWidget(self.views,1)
        self.report=QTextBrowser();self.views.addTab(self.report,'Report / limitations')
        self.table=QTableWidget(0,7);self.table.setHorizontalHeaderLabels(['Line','Time GPST','X ECEF m','Y ECEF m','Z ECEF m','Q','Eligible'])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows);self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers);self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.views.addTab(self.table,'Solution epochs')
        map_page=QWidget();map_box=QVBoxLayout(map_page)
        hint=QLabel('Engine positions on their declared output frame/ellipsoid for display. PointZ preserves ellipsoidal height; ECEF Z is separate. No national reference/height approval. Click a marker to select its original epoch.')
        hint.setWordWrap(True);map_box.addWidget(hint)
        self.canvas=QgsMapCanvas();self.canvas.setDestinationCrs(QgsCoordinateReferenceSystem('EPSG:4979'));self.canvas.setCanvasColor(QColor('#f4f6f8'));map_box.addWidget(self.canvas)
        self.inspect=InspectTool(self.canvas,self,keys=['points'],kinds={'points':'processing_epoch'});self.inspect.picked.connect(self.select_record);self.canvas.setMapTool(self.inspect)
        self.views.addTab(map_page,'Computed positions')
        self.message=self.status
        self.handoff=QPushButton('Review solution through existing baseline import …');self.handoff.setEnabled(False);self.handoff.clicked.connect(self.review_baseline);layout.addWidget(self.handoff)
        self.compare_button=QPushButton('Compare selected ITRF2020 epoch with known EUREF89 point …');self.compare_button.setEnabled(False)
        self.compare_button.clicked.connect(self.compare_reference);layout.addWidget(self.compare_button)
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Close);buttons.rejected.connect(self.reject);layout.addWidget(buttons)
        self.timer=QTimer(self);self.timer.setSingleShot(True);self.timer.timeout.connect(self.stop)
        for field in (self.executable,self.base_xyz,self.antenna_note,self.model_note,self.antex,self.calibration_note,self.base_heading,self.rover_heading):field.textChanged.connect(self.invalidate)
        for field in (self.mode,self.base,self.rover,self.navigation,self.frame,self.frequency,self.atmosphere,self.calibration):field.currentIndexChanged.connect(self.invalidate)
        self.engine_variant.currentIndexChanged.connect(self.profile_changed)
        for field in self.systems.values():field.toggled.connect(self.invalidate)
        for field in (self.elevation,self.ratio):field.valueChanged.connect(self.invalidate)
        for field in (self.base_review,self.antenna_review,self.model_review,self.license_review):field.toggled.connect(self.invalidate)
        self.orientation_review.toggled.connect(self.invalidate)
        self.orientation_mode.currentIndexChanged.connect(self.invalidate)
        self.populate()

    def populate(self):
        self.reference_help.setText('Network coordinate epoch: '+(self.project['epoch'] or 'MISSING')+'. Fixed base uncertainty is excluded. Header H/E/N is separate from ANTEX; no frame/epoch transformation. Use simultaneous base/rover OBS and NAV. Own bases/BKG RINEX fit the existing import. Caster data need recorded raw observations converted to RINEX, station/coordinate continuity and documented frame/epoch. Onocoy roaming can change the reference station; a caster is not a VRS generator. Compact Hatanaka data must first be expanded. No credentials, live streams, downloads or precise SP3/CLK products are processed by this dialog.')
        for combo_,role in ((self.base,'BASE'),(self.rover,'ROVER'),(self.navigation,'NAV')):
            combo_.clear();combo_.addItem('Select existing '+role+' source','')
            for source in self.project.get('raw_sources',[]):
                if source['role']==role:combo_.addItem(source['source_name']+' / '+source['point']+' / '+source['session'],source['id'])
            if combo_.count()==2:combo_.setCurrentIndex(1)

    def review_orientation(self):
        from .orientation_ui import OrientationDialog
        dialog=OrientationDialog(self);dialog.exec();dialog.deleteLater()

    def options(self):
        try:coordinates=tuple(float(v) for v in self.base_xyz.text().replace(',',' ').split())
        except ValueError:raise ValueError('Enter three fixed base ECEF coordinates with decimal points.') from None
        try:headings=[float(field.text()) if field.text().strip() else None for field in (self.base_heading,self.rover_heading)]
        except ValueError:raise ValueError('Enter antenna azimuths with decimal points.') from None
        return p.ProcessingOptions(base=self.base.currentData(),rover=self.rover.currentData(),navigation=self.navigation.currentData(),
            mode=self.mode.currentData(),base_xyz=coordinates,frame=self.frame.currentData(),epoch=self.project['epoch'],
            frequencies=self.frequency.currentData(),elevation=self.elevation.value(),ratio=self.ratio.value(),atmosphere=self.atmosphere.currentData(),
            antenna_note=self.antenna_note.text().strip(),model_note=self.model_note.text().strip(),
            systems=tuple(s for s,field in self.systems.items() if field.isChecked()),calibration=self.calibration.currentData(),
            antex_path=self.antex.text().strip(),calibration_note=self.calibration_note.text().strip(),
            engine_variant=self.engine_variant.currentData(),
            base_azimuth_deg=headings[0],rover_azimuth_deg=headings[1],orientation_reviewed=self.orientation_review.isChecked(),
            orientation_mode=self.orientation_mode.currentData() if self.engine_variant.currentData()=='survey-sa2' else '',
            base_reviewed=self.base_review.isChecked(),antenna_reviewed=self.antenna_review.isChecked(),model_reviewed=self.model_review.isChecked(),license_reviewed=self.license_review.isChecked())

    def invalidate(self,*_):
        if self.preparing or (self.process and self.process.state()!=QProcess.ProcessState.NotRunning):self.stop()
        self.result=self.source=None;self.layers={};self.canvas.setLayers([]);self.canvas.hide();self.canvas.refresh()
        self.report.clear();self.table.setRowCount(0);self.handoff.setEnabled(False);self.open_button.setEnabled(False)
        self.compare_button.setEnabled(False)
        self.status.setText('Settings changed. A new checked processing job is required.')
        if self.atmosphere.currentData()==p.STATIC_ATMOSPHERE:
            self.report.setHtml('<h3>Experimental stationary atmosphere estimation</h3><p>Initial profile: upstream EX, Static, GPS L1/L2. It estimates baseline-scaled ionosphere and receiver zenith wet-delay states. Every solution epoch must have matching engine states and two-frequency GPS residuals.</p><p>The example button creates an invented ~60 km / 1 hour data set with analytic atmosphere. This is a software test, not field accuracy. Broadcast orbits remain selected; precise products, PPP and network VRS generation are not implemented.</p><p>Reference data: reuse the existing raw import for base OBS, rover OBS and NAV. Document the actual station, frame/realisation, coordinate epoch, antenna/ARP and station continuity. A supplied virtual station is not a physical antenna; do not invent its antenna correction.</p><p><a href="https://igs.bkg.bund.de/holdings">BKG data/products</a> · <a href="https://docs.onocoy.com/documentation/service-levels">Onocoy source/roaming guidance</a> · <a href="https://docs.emlid.com/emlid-studio/convert-to-rinex-workflow/">Emlid raw-to-RINEX conversion</a></p>')

    def choose_engine(self):
        path,_=QFileDialog.getOpenFileName(self,'Choose reviewed executable for the selected engine profile','','RTKLIB (rnx2rtkp.exe)')
        if path:self.executable.setText(path)

    def profile_changed(self):
        self.invalidate();self.license_review.setChecked(False)
        variant=self.engine_variant.currentData();extended=variant in ('survey-sa1','survey-sa2');azimuth=variant=='survey-sa2'
        self.license_review.setText('I reviewed experimental Survey '+variant[-3:].upper()+' GPLv3 extension and upstream BSD/SOFA notices; separately installed engine' if extended else 'I reviewed RTKLIB EX 2.5.1 BSD-2-Clause licence; engine is separately installed')
        self.orientation_box.setVisible(azimuth);self.orientation_review.setVisible(azimuth);self.orientation_review.setChecked(False)
        self.base_heading.clear();self.rover_heading.clear()
        self.orientation_mode.setCurrentIndex(0)
        self.systems['E'].setText('Galileo E1/E5a' if azimuth else 'Galileo')
        self.systems['C'].setText('BeiDou B1C/B3I' if azimuth else 'BeiDou B1I/B2I')
        if azimuth:self.calibration.setCurrentIndex(self.calibration.findData('antex'))
        key={'survey-sa1':'SurveyAdjustment/rtklib_sa1','survey-sa2':'SurveyAdjustment/rtklib_sa2'}.get(variant,'SurveyAdjustment/rtklib_ex251')
        self.executable.setText(str(QSettings().value(key,'')))

    def choose_antex(self):
        path,_=QFileDialog.getOpenFileName(self,'Choose local absolute receiver ANTEX','','ANTEX (*.atx *.ATX)')
        if path:self.antex.setText(path)

    def review_models(self):
        self.invalidate()
        try:
            options=self.options();sources={s['id']:s for s in self.project.get('raw_sources',[])}
            model=p.calibration_review(options,sources[options.base],sources[options.rover])
            if model is None:raise ValueError('Select ANTEX calibration and its local file first.')
            self.report.setHtml('<h3>Antenna model review — no computation</h3>'+self.calibration_html(model))
            self.views.setCurrentIndex(0);self.status.setText('Review original provider notes and model limits before confirming orientation/calibration.')
            return True
        except (ValueError,KeyError,OSError) as error:self.status.setText('Cannot review antenna models: '+str(error));return False

    def calibration_html(self,calibration):
        parts=[]
        for key in ('base','rover'):
            model=calibration[key]
            parts.append('<p><b>'+key+': '+html.escape(model['type'])+'</b>; DAZI '+str(model['dazi'])+'°; bands '+html.escape(', '.join(model['frequencies']))+'.</p>')
            comments=model.get('comments',[])
            warnings=[line for line in comments if any(word in line.upper() for word in ('COPIED','VERIFICATION','WARNING','ATTENTION'))]
            if warnings:parts.append('<p><b>Provider qualification: '+html.escape(' '.join(warnings))+'</b></p>')
        return ''.join(parts)

    def example(self):
        if not self.owner.maybe_leave():return False
        systems=tuple(s for s,field in self.systems.items() if field.isChecked());calibrated=self.calibration.currentData()=='antex'
        azimuth=self.engine_variant.currentData()=='survey-sa2'
        long_static=self.atmosphere.currentData()==p.STATIC_ATMOSPHERE
        if long_static and (self.mode.currentData()!='static' or systems!=('G',) or calibrated or self.frequency.currentData()!=2 or self.engine_variant.currentData()!='upstream'):
            self.status.setText('This constructed long-static example requires upstream EX, Static, GPS only, two frequency slots and explicit uncalibrated input. Real GPS ANTEX jobs can be reviewed separately.');return False
        if (calibrated and systems!=('G',) or 'R' in systems) and self.engine_variant.currentData() not in ('survey-sa1','survey-sa2'):
            self.status.setText('Select Survey SA1 for GLONASS or mixed-system ANTEX examples. Upstream EX retains its GPS-only calibration contract.');return False
        parent=QFileDialog.getExistingDirectory(self,'Local folder for a NEW constructed GNSS example')
        if not parent:return False
        try:
            folder=Path(parent)/('Synthetic_GNSS_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
            self.project=demo.create(folder,moving=self.mode.currentData()=='kinematic',systems=systems,calibrated=calibrated,azimuth=azimuth,long_static=long_static)
            self.owner.replace_project(self.project,folder/'example.sanet.json');self.populate()
            self.base_xyz.setText(' '.join(f'{v:.8f}' for v in demo.BASE));self.frame.setCurrentIndex(self.frame.findData('EPSG:4978'))
            self.atmosphere.setCurrentIndex(self.atmosphere.findData(p.STATIC_ATMOSPHERE if long_static else 'ideal-synthetic'))
            self.antenna_note.setText('SYNTHETIC marker endpoints; base/rover H=1/2 m; invented PCO and NOAZI PCV.' if calibrated else 'SYNTHETIC electrical endpoints; zero H/E/N and antenna patterns.')
            self.antex.setText(str(folder/'synthetic.atx') if calibrated else '')
            self.calibration_note.setText('Own invented ANTEX models, no real receiver calibration. Constructed north-oriented antenna offsets for this integration test.' if calibrated else '')
            if azimuth:
                self.orientation_mode.setCurrentIndex(self.orientation_mode.findData('fixed-level'))
                self.base_heading.setText('0');self.rover_heading.setText('37');self.orientation_review.setChecked(False)
                self.antenna_note.setText('SYNTHETIC level antennas: base NRP 0°, rover NRP 37° clockwise from true north; marker-to-ARP H=1/2 m.')
                self.calibration_note.setText('Own invented DAZI=5 grids and exact per-system bands. No actual RS4 Pro calibration or field validation.')
            self.model_note.setText('SYNTHETIC ~60 km / 1 hour stationary GPS, constructed dispersive ionosphere and simple mapped dry/wet delay. Broadcast ideal circular orbits; not real 30–100 km accuracy validation.' if long_static else 'SYNTHETIC circular '+''.join(systems)+' orbits, ideal signals, no atmosphere; WGS84 ellipsoid test coordinates only. Not field accuracy.')
            for field in (self.base_review,self.antenna_review,self.model_review,self.license_review):field.setChecked(False)
            self.status.setText('Constructed example created in the existing network. Review fields and confirmations before running. Original user project was not overwritten.')
            return True
        except (ValueError,OSError) as error:self.status.setText('Cannot create example: '+str(error));return False

    def idle(self):
        self.timer.stop();self.controls.setEnabled(True);self.example_button.setEnabled(True);self.run_button.setEnabled(True);self.stop_button.setEnabled(False)
        self.orientation_button.setEnabled(True)

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
            self.orientation_button.setEnabled(False)
            QSettings().setValue({'survey-sa1':'SurveyAdjustment/rtklib_sa1','survey-sa2':'SurveyAdjustment/rtklib_sa2'}.get(options.engine_variant,'SurveyAdjustment/rtklib_ex251'),str(executable))
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
                geographic='EPSG:9989' if self.submitted['frame']=='EPSG:9988' else 'EPSG:4979'
                lat,lon,height=llh(row['values'],geographic)
                rows.append(([str(row['line']),row['time'],row['quality'],*row['values']],QgsGeometry(QgsPoint(lon,lat,height))))
            layer=make_layer('PointZ','Computed '+self.result['mode']+' GNSS positions',geographic,
                [('record_id',QMetaType.Type.QString),('time_gpst',QMetaType.Type.QString),('quality',QMetaType.Type.QString),
                 ('ecef_x',QMetaType.Type.Double),('ecef_y',QMetaType.Type.Double),('ecef_z',QMetaType.Type.Double)],rows)
            layer.renderer().setSymbol(QgsMarkerSymbol.createSimple({'name':'circle','color':'#177e89','size':'2'}))
            self.layers={'points':layer};self.canvas.setDestinationCrs(QgsCoordinateReferenceSystem(geographic));self.canvas.setLayers([layer]);self.canvas.setExtent(layer.extent().buffered(.0001));self.canvas.show();self.canvas.refresh()
            esc=lambda value:html.escape(str(value))
            used=self.result.get('used_satellites',{});calibration=self.result.get('calibration')
            audit='<p>Valid engine residuals: '+esc('; '.join(s+': '+', '.join(ids) for s,ids in used.items()))+'</p>'
            if calibration:
                audit+='<p>Applied receiver ANTEX: '+esc('; '.join(key+': '+calibration[key]['type']+' ('+calibration[key]['selection']+')' for key in ('base','rover')))+'. Original SHA256 '+esc(calibration['original_sha256'])+'.</p>'
                audit+=self.calibration_html(calibration)
            if self.result.get('signal_plan'):
                audit+='<p>Actual signals: '+esc(json.dumps(self.result['signal_plan']))+'; antenna NRP azimuths: '+esc(json.dumps(self.result['antenna_azimuth_deg']))+'°.</p>'
            if self.result.get('atmosphere_audit'):
                state=self.result['atmosphere_audit'];length=self.result['computed_baseline_length_m']['last']/1000
                audit+='<h3>Experimental static atmosphere audit</h3><p>Computed baseline: '+f'{length:.3f}'+' km. '+str(state['epochs'])+' solution epochs with matching state/residual evidence; '+str(state['ionosphere_records'])+' ionosphere and '+str(state['wet_delay_records'])+' wet-delay records. See atmosphere_states.csv and the unchanged engine status file.</p><p>'+esc(state['note'])+'</p>'
            self.report.setHtml('<h3>'+esc(self.result['engine_version'])+' — '+esc(self.result['mode'])+'</h3><p>'+str(self.result['epochs'])+' solution epochs; '+str(self.result['eligible_fix'])+' eligible FIX rows. All quality states retained.</p><p>'+esc(self.folder)+'</p>'+audit+'<ul>'+''.join('<li>'+esc(s)+'</li>' for s in self.result['warnings'])+'</ul><p>Use Solution epochs to inspect results. Review one documented stationary occupation through the existing baseline dialog; the full trajectory is never inserted as independent point measurements.</p>')
            self.open_button.setEnabled(True);self.handoff.setEnabled(self.result['eligible_fix']>0)
            self.compare_button.setEnabled(self.submitted['frame']=='EPSG:9988')
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

    def compare_reference(self):
        try:
            if not self.result or self.table.currentRow()<0:raise ValueError('Select one completed solution epoch first; float and fixed remain explicitly labelled.')
            if self.project!=self.owner.snapshot():raise ValueError('Network changed; reopen/recalculate the checked snapshot.')
            source=p.source_for_import(self.folder/'solution.pos')
            if source['processing']['frame']!='EPSG:9988':raise ValueError('This comparison requires explicitly documented ITRF2020 processing.')
            row=source['rows'][self.table.currentRow()]
            point=next(point for point in self.project['points'] if point['id']==source['processing']['rover'])
            if point['role']!='CHECK' or point['coordinates'] is None:raise ValueError('The existing rover point must contain a known coordinate and be Check only; it stays outside the fit.')
            dialog=QDialog(self);dialog.setWindowTitle('Review national reference comparison');dialog.resize(780,420)
            box=QVBoxLayout(dialog);form=QFormLayout();box.addLayout(form)
            form.addRow('Known point',QLabel(point['id']+' / '+str(point['coordinates'])+' / '+self.project['crs']))
            form.addRow('Selected solution',QLabel(row['time']+' GPST; Q='+row['quality']+' (1 FIX, 2 float)'))
            dialog.epoch=QLineEdit();dialog.epoch.setPlaceholderText('Actual coordinate epoch of fixed global base, decimal year. Not the EUREF89 reference epoch.')
            form.addRow('Global coordinate epoch',dialog.epoch)
            dialog.height=combo({'':'Select documented known-point height','ELLIP':'EUREF89 ellipsoidal height','NN2000:2018':'NN2000 with HREF2018B','NN2000:2025':'NN2000 with HREF2025A'})
            form.addRow('Known-point height reference',dialog.height)
            from pyproj import datadir
            model_root=Path(datadir.get_data_dir())
            dialog.href=QLineEdit();dialog.href.setPlaceholderText('Reviewed local HREF GeoTIFF if the known point uses NN2000')
            choose=QPushButton('Choose HREF GeoTIFF …')
            def select_model():
                path,_=QFileDialog.getOpenFileName(dialog,'Choose reviewed HREF model','','GeoTIFF (*.tif)')
                if path:dialog.href.setText(path)
            choose.clicked.connect(select_model);form.addRow(dialog.href,choose)
            text=QLabel('Transforms ITRF2020 to national EUREF89 using pinned local NKG grids, then the existing projection/HREF operation. Document IGc20 alignment, antenna approximations, base uncertainty and control provenance in the processing notes. Original engine coordinates/covariance stay unchanged. This is a held-out difference, not survey approval.')
            text.setWordWrap(True);box.addWidget(text)
            dialog.reviewed=QCheckBox('I verified national EUREF89, actual coordinate epoch, height version and the processing/control assumptions')
            box.addWidget(dialog.reviewed)
            buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel)
            buttons.accepted.connect(dialog.accept);buttons.rejected.connect(dialog.reject);box.addWidget(buttons)
            if dialog.exec()!=QDialog.DialogCode.Accepted:return False
            if not dialog.reviewed.isChecked():raise ValueError('Review the national reference, epoch and uncertainty assumptions explicitly.')
            height=dialog.height.currentData();paths={height:dialog.href.text().strip()} if height in delivery.MODELS else {}
            if (self.project['height_datum']=='NN2000' and height not in delivery.MODELS) or (self.project['height_datum']=='ELLIP' and height!='ELLIP') or self.project['height_datum'] not in ('NN2000','ELLIP'):
                raise ValueError('Comparison height reference must agree with the known-point network height declaration.')
            result=delivery.compare_itrf2020_point(row['values'],point['coordinates'],self.project['crs'],height,float(dialog.epoch.text()),
                {name:model_root/name for name in delivery.GlobalReferenceOperation.GRIDS},paths)
            result.update(source_job_id=source['processing']['job_id'],source_pos_sha256=engine.sha256(self.folder/'solution.pos'),source_line=row['line'],
                time_gpst=row['time'],quality=row['quality'],eligible_fix=row['eligible'],known_point_id=point['id'],control_notes=point['notes'],
                raw_processing_model_note=source['processing']['model_note'],raw_antenna_note=source['processing']['antenna_note'],known_point_used_to_fit=False)
            output=self.folder/'reference_comparisons';output.mkdir(exist_ok=True)
            path=output/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'_line'+str(row['line'])+'.json')
            with path.open('x',encoding='utf-8') as handle:json.dump(result,handle,indent=2,ensure_ascii=False,allow_nan=False)
            self.last_comparison=result;self.last_comparison_path=path
            cells=''.join('<tr><td>'+label+'</td><td>'+f'{known:.6f}'+'</td><td>'+f'{computed:.6f}'+'</td><td>'+f'{delta:+.6f}'+'</td></tr>' for label,known,computed,delta in zip(('Easting','Northing','Height'),result['known'],result['computed'],result['delta']))
            self.report.append('<h3>Known-point comparison — '+html.escape(point['id'])+'</h3><p>'+html.escape(row['time'])+' GPST; Q='+row['quality']+'; '+html.escape(height)+'. National EUREF89 after explicit ITRF2020 transformation.</p><table border="1" cellpadding="4"><tr><th>Metres</th><th>Known</th><th>Computed</th><th>Computed − known</th></tr>'+cells+'</table><p>Control held out. '+html.escape(result['uncertainty'])+'</p><p>'+html.escape(str(path))+'</p>')
            self.views.setCurrentWidget(self.report);self.report.moveCursor(self.report.textCursor().MoveOperation.End)
            self.status.setText('Selected epoch compared with the held-out known point. Q='+row['quality']+'. Original GNSS solution unchanged; separate comparison saved.')
            return True
        except (ValueError,OSError,KeyError,StopIteration) as error:
            self.status.setText('Cannot compare reference: '+str(error));return False

    def open_folder(self):
        if self.result and self.folder:QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.folder)))

    def stop(self):
        self.aborted=True;self.result=self.source=None;self.handoff.setEnabled(False);self.open_button.setEnabled(False);self.compare_button.setEnabled(False)
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
