# SPDX-License-Identifier: GPL-3.0-only
"""Coordinate supplement inside the existing Raw GNSS processing workflow."""
from datetime import datetime,timezone
from pathlib import Path
import html
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QMetaType
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QFormLayout,QLabel,QLineEdit,QPushButton,
    QCheckBox,QTextBrowser,QTableWidget,QTableWidgetItem,QAbstractItemView,QHeaderView,QFileDialog,QTabWidget,QWidget,QDialogButtonBox)
from qgis.core import QgsCoordinateReferenceSystem,QgsGeometry,QgsPoint,QgsMarkerSymbol
from qgis.gui import QgsMapCanvas
from . import orientation as o,gnss_demo as demo
from .network_ui import combo
from .layers import make_layer


GUIDE=('For Reach RS4 Pro / firmware 34.8: preserve RINEX separately. Record a timestamped NMEA stream containing GGA + ETC and continuous ZDA or RMC (GST optional). '
    'ETC carries UTC time, state, notification, heading, tilt direction/value and N/E/U coefficients. RINEX and ordinary point CSV do not reconstruct this series. '
    'In Emlid Flow / Reach Panel, check the position-output compensation setting: NMEA may already report the ground point. Keep that setting and installation/height evidence with the log. '
    'No receiver settings or live logging are changed here. Consult Emlid documentation for the exact output configuration. '
    'The developer PDF calls GGA heights ellipsoidal, while standard GGA uses altitude plus geoid separation; choose the convention supported by your actual output. '
    'The PDF gives a model-specific 0.095 m bottom-to-ARP value for RS4/Pro. This is not automatically added: document the actual position endpoint and complete pole-axis distance. '
    'Heading is not used as raw antenna NRP/yaw. Full dynamic ANTEX/carrier processing still needs a documented attitude/antenna-axis mapping, timing and phase model.')


class OrientationDialog(QDialog):
    def __init__(self,parent):
        super().__init__(parent);self.owner=parent.owner;self.catalogue=None;self.result=None;self.layer=None;self.busy=False
        self.setWindowTitle('GNSS orientation / pole-tip review — NMEA ETC');self.resize(1080,850)
        layout=QVBoxLayout(self);note=QLabel('Review changing heading/tilt from a local ETC log. Explicit coordinate-only pole-tip correction; no raw IMU/PPK/ANTEX calculation or automatic observations.')
        note.setWordWrap(True);layout.addWidget(note)
        self.controls=QWidget();form=QFormLayout(self.controls)
        path_row=QHBoxLayout();self.path=QLineEdit();self.path.setPlaceholderText('Local ASCII NMEA log; GGA + ETC + ZDA/RMC; checksums required')
        choose=QPushButton('Choose NMEA …');choose.clicked.connect(self.choose);path_row.addWidget(self.path,1);path_row.addWidget(choose);form.addRow('Orientation / position log',path_row)
        self.endpoint=combo({'':'Select documented input endpoint','uncompensated-antenna':'Uncompensated antenna position → pole tip','already-ground':'Already-compensated ground point → preserve coordinates'})
        self.height_mode=combo({'':'Select actual GGA height convention','ellipsoid-altitude':'GGA altitude field is already ellipsoidal height','msl-plus-separation':'Standard GGA: ellipsoidal h = altitude + separation'})
        self.frame=combo({'':'Select documented geographic frame','EPSG:4979':'WGS84 geographic / ellipsoid — no datum transformation','EPSG:4937':'ETRS89 geographic / GRS80 — requires source evidence'})
        self.height=QLineEdit();self.height.setPlaceholderText('Total pole-tip → input antenna-position endpoint distance, metres along pole. Clear for already-ground.')
        self.evidence=QLineEdit();self.evidence.setPlaceholderText('Output compensation setting, actual endpoint/height convention, frame, pole/installation and UTC synchronization evidence')
        for label,field in (('Input endpoint',self.endpoint),('Height convention',self.height_mode),('Declared frame',self.frame),('Pole-axis endpoint distance m',self.height),('Evidence / source settings',self.evidence)):form.addRow(label,field)
        self.endpoint_review=QCheckBox('I verified whether the source is already tilt/height compensated; the same correction must never be applied twice')
        self.height_review=QCheckBox('I verified the coordinate reference point, frame and ellipsoidal-height convention; no automatic ARP/PCO/geoid addition')
        self.time_review=QCheckBox('I reviewed UTC dates/synchronization, ETC state/notifications and exact GGA/ETC timestamps')
        for field in (self.endpoint_review,self.height_review,self.time_review):form.addRow(field)
        layout.addWidget(self.controls)
        row=QHBoxLayout();self.example_button=QPushButton('New synthetic turning / tilting example …');self.example_button.clicked.connect(self.example)
        self.review_button=QPushButton('Check log / review pole-tip coordinates');self.review_button.clicked.connect(self.start)
        self.export_button=QPushButton('Export checked coordinate package …');self.export_button.clicked.connect(self.export);self.export_button.setEnabled(False)
        for field in (self.example_button,self.review_button,self.export_button):row.addWidget(field)
        layout.addLayout(row);self.status=QLabel('No orientation review. Unknown endpoints block correction.');self.status.setWordWrap(True);layout.addWidget(self.status)
        self.views=QTabWidget();layout.addWidget(self.views,1);self.report=QTextBrowser();self.views.addTab(self.report,'Review / logging help')
        self.table=QTableWidget(0,8);self.table.setHorizontalHeaderLabels(['UTC','Heading °','Tilt direction °','Tilt °','State / notification','Accepted','Ground h m','Reason'])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers);self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents);self.views.addTab(self.table,'Orientation timeline')
        self.canvas=QgsMapCanvas();self.canvas.setCanvasColor(QColor('#f4f6f8'));self.views.addTab(self.canvas,'Derived ground PointZ')
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Close);buttons.rejected.connect(self.reject);layout.addWidget(buttons)
        for field in (self.path,self.height,self.evidence):field.textChanged.connect(self.invalidate)
        for field in (self.endpoint,self.height_mode,self.frame):field.currentIndexChanged.connect(self.invalidate)
        for field in (self.endpoint_review,self.height_review,self.time_review):field.toggled.connect(self.invalidate)
        self.show_help()

    def show_help(self):
        self.report.setHtml('<h3>Orientation / pole-tip supplement</h3><p>'+html.escape(GUIDE)+'</p><p><a href="'+o.SPEC+'">Emlid ETC developer specification</a></p><ul>'+''.join('<li>'+html.escape(s)+'</li>' for s in o.LIMITATIONS)+'</ul>')

    def invalidate(self,*_):
        self.catalogue=self.result=None;self.layer=None;self.canvas.setLayers([]);self.canvas.refresh();self.table.setRowCount(0)
        self.export_button.setEnabled(False);self.show_help();self.status.setText('Settings changed. Check the log again before export.')

    def options(self):
        try:height=float(self.height.text()) if self.height.text().strip() else None
        except ValueError:raise ValueError('Enter pole-axis endpoint distance with a decimal point.') from None
        return o.OrientationOptions(self.endpoint.currentData(),self.height_mode.currentData(),self.frame.currentData(),height,self.evidence.text().strip(),
            self.endpoint_review.isChecked(),self.height_review.isChecked(),self.time_review.isChecked())

    def choose(self):
        path,_=QFileDialog.getOpenFileName(self,'Choose local NMEA orientation/position log','','NMEA (*.nmea *.log *.txt);;All files (*)')
        if path:self.path.setText(path)

    def example(self):
        parent=QFileDialog.getExistingDirectory(self,'Local folder for a NEW synthetic orientation example')
        if not parent:return False
        try:
            path=demo.orientation_example(Path(parent)/('Synthetic_ETC_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')))
            self.path.setText(str(path));self.endpoint.setCurrentIndex(self.endpoint.findData('uncompensated-antenna'))
            self.height_mode.setCurrentIndex(self.height_mode.findData('ellipsoid-altitude'));self.frame.setCurrentIndex(self.frame.findData('EPSG:4979'));self.height.setText('2')
            self.evidence.setText('SYNTHETIC: invented uncompensated antenna positions, 2 m endpoint distance, WGS84 ellipsoid, exact UTC GGA/ETC with ZDA; no real receiver accuracy claim.')
            for field in (self.endpoint_review,self.height_review,self.time_review):field.setChecked(False)
            self.status.setText('Synthetic turning/tilting log created, including UTC midnight and excluded epochs. Review settings and confirmations. Network unchanged.')
            return True
        except (ValueError,OSError) as error:self.status.setText('Cannot create example: '+str(error));return False

    def idle(self):
        self.busy=False;self.controls.setEnabled(True);self.example_button.setEnabled(True);self.review_button.setEnabled(True)

    def start(self):
        if self.busy:return False
        try:
            options=self.options();path=self.path.text().strip()
            self.invalidate();self.busy=True;self.controls.setEnabled(False);self.example_button.setEnabled(False);self.review_button.setEnabled(False)
            self.status.setText('Checking local NMEA checksums, dates and exact UTC matches in the existing background queue …')
            def operation(cancelled):
                catalogue=o.read(path,cancelled);return catalogue,o.review(catalogue,options)
            def failed(error):
                if sip.isdeleted(self):return
                self.idle();self.status.setText('No accepted review: '+str(error))
            def done(data):
                if sip.isdeleted(self):return
                self.idle()
                if self.options()!=options or self.path.text().strip()!=path:raise ValueError('Settings changed during review.')
                self.catalogue,self.result=data;self.display()
            if not self.owner.raw_panel.work('ETC orientation review',operation,done,failed):self.idle();return False
            return True
        except (ValueError,OSError) as error:self.idle();self.status.setText('Cannot review: '+str(error));return False

    def display(self):
        result=self.result;self.table.setRowCount(len(result['rows']));points=[]
        for index,row in enumerate(result['rows']):
            etc=row['etc'] or {};ground=row['ground_llh'];accepted=row['accepted']
            values=[row['utc'] or '',*[etc.get(key,'') for key in ('heading','direction','tilt')],str(etc.get('state'))+' / '+str(etc.get('notification')),accepted,ground[2] if ground else '',row['reason']]
            for column,value in enumerate(values):self.table.setItem(index,column,QTableWidgetItem(str(value)))
            if ground:points.append(([row['utc'],ground[2],etc.get('heading'),etc.get('tilt')],QgsGeometry(QgsPoint(ground[1],ground[0],ground[2]))))
        frame=result['options']['frame'];self.canvas.setDestinationCrs(QgsCoordinateReferenceSystem(frame))
        self.layer=make_layer('PointZ','ETC derived ground — no accuracy approval',frame,
            [('utc',QMetaType.Type.QString),('ellipsoid_h_m',QMetaType.Type.Double),('heading_deg',QMetaType.Type.Double),('tilt_deg',QMetaType.Type.Double)],points)
        self.layer.renderer().setSymbol(QgsMarkerSymbol.createSimple({'name':'circle','color':'#177e89','size':'2'}));self.canvas.setLayers([self.layer])
        if points:self.canvas.setExtent(self.layer.extent().buffered(.00003))
        self.canvas.refresh();self.show_help()
        self.report.setHtml('<h3>'+str(result['accepted'])+' accepted / '+str(len(result['rows']))+' epochs</h3><p>Mode: '+html.escape(result['options']['endpoint'])+'; frame '+frame+'; height '+html.escape(result['options']['height_mode'])+'.</p><p>Source SHA256 '+result['source_sha256']+'</p><p>'+html.escape(result['options']['evidence'])+'</p>'+self.report.toHtml())
        self.export_button.setEnabled(result['accepted']>0);self.status.setText(str(result['accepted'])+' accepted of '+str(len(result['rows']))+' epochs. All exclusions visible; no baseline/network observations added. Full dynamic raw processing remains unsupported.')
        self.views.setCurrentIndex(0)

    def export(self):
        if not self.result or not self.catalogue:return False
        parent=QFileDialog.getExistingDirectory(self,'Local folder for a NEW checked orientation coordinate package')
        if not parent:return False
        try:
            folder=o.export_snapshot(Path(parent)/('ETC_coordinates_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')),self.catalogue,self.options())
            self.status.setText('Export verified by recalculation and checksums: '+str(folder)+'. CSV retains ellipsoidal heights; no covariance or independent observations invented.')
            return True
        except (ValueError,OSError) as error:
            self.invalidate();self.status.setText('Export blocked: '+str(error));return False

    def reject(self):
        if self.busy:self.owner.raw_panel.cancel()
        super().reject()
