# SPDX-License-Identifier: GPL-3.0-only
"""Explicit, receiver-independent setup for GNSS coordinate observations."""
from pathlib import Path
from qgis.PyQt.QtCore import QMetaType
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (QWidget,QVBoxLayout,QFormLayout,QHBoxLayout,QLabel,
    QPushButton,QFileDialog,QTableWidget,QTableWidgetItem,QComboBox,QLineEdit,QCheckBox,
    QDoubleSpinBox,QScrollArea,QHeaderView,QTextBrowser)
from qgis.core import QgsProject,QgsPoint,QgsGeometry,QgsMarkerSymbol,QgsPalLayerSettings,QgsVectorLayerSimpleLabeling
from .core import read_table
from .coordinate_job import FIELDS,CoordinateOptions,mapping_for,coordinate_job,prepare_coordinate
from .layers import make_layer


class CoordinatePanel(QWidget):
    def __init__(self,owner):
        super().__init__(); self.owner=owner; self.table=None; self.synthetic=False
        layout=QVBoxLayout(self); area=QScrollArea(); area.setWidgetResizable(True)
        layout.addWidget(area); content=QWidget(); form=QFormLayout(content); area.setWidget(content)
        info=QLabel('GNSS from different receivers: weighted adjustment of repeated absolute coordinate observations in SALSA. '
            'This is not raw GNSS/baseline adjustment. Requires latitude/longitude, ellipsoidal point height and documented 1σ values. '
            'Use CSV/TXT/TSV with column headers and map the fields below. Example: Emlid Flow “All columns”. '
            'The same file type does not guarantee the same units, heights or quality fields. RINEX and ordinary NN2000 Elevation cannot be used directly here.')
        info.setWordWrap(True); form.addRow(info)
        row=QHBoxLayout()
        for title,callback in [('Open GNSS file …',self.open_csv),('Load synthetic coordinate example',self.load_demo)]:
            button=QPushButton(title); button.clicked.connect(callback); row.addWidget(button)
        form.addRow(row)
        self.source=QLabel('No CSV selected.'); self.source.setWordWrap(True); form.addRow(self.source)
        self.mapping=QTableWidget(len(FIELDS),2)
        self.mapping.setHorizontalHeaderLabels(['Use','Source column'])
        self.mapping.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.mapping.setMinimumHeight(270); form.addRow(self.mapping)
        self.frame=QComboBox(); self.frame.addItem('Select the documented reference frame for latitude/longitude/h','')
        self.frame.addItem('ETRS89 – ellipsoidal GRS80 (EPSG:4937)','EPSG:4937')
        self.frame.addItem('WGS84 – ellipsoidal (EPSG:4979)','EPSG:4979')
        form.addRow('Geodetic reference frame',self.frame)
        self.epoch=QLineEdit(); self.epoch.setPlaceholderText('Measurement epoch YYYY-MM-DD; no epoch transformation')
        form.addRow('Measurement epoch',self.epoch)
        self.sigma_mode=QComboBox(); self.sigma_mode.addItem('Common user-specified 1σ values','constant'); self.sigma_mode.addItem('Documented 1σ columns','columns')
        form.addRow('Uncertainty model',self.sigma_mode)
        row=QHBoxLayout(); self.sigmas=[]
        for label in ('East','North','Up'):
            row.addWidget(QLabel(label)); box=QDoubleSpinBox(); box.setRange(0,100); box.setDecimals(4); box.setSuffix(' m'); box.setSingleStep(.005)
            row.addWidget(box); self.sigmas.append(box)
        form.addRow('Common 1σ (common-value model)',row)
        self.uncertainty=QLineEdit(); self.uncertainty.setPlaceholderText('Basis for 1σ; receiver RMS/HRMS/VRMS are not copied automatically')
        form.addRow('Uncertainty source/assessment',self.uncertainty)
        self.corrections=QLineEdit(); self.corrections.setPlaceholderText('Correction service/mountpoint or base and reference frame')
        form.addRow('Corrections/base',self.corrections)
        self.antenna=QLineEdit(); self.antenna.setPlaceholderText('Antenna height/tilt and confirmation that the CSV height refers to the point')
        form.addRow('Antenna and point height',self.antenna)
        self.ellipsoid=QCheckBox('I have checked the datum and confirmed that the selected height is ellipsoidal point height in metres, not NN2000/NN54.')
        self.uncertainty_confirm=QCheckBox('The values are assessed 1σ in geodetic ENU; correlations between E/N/U are set to zero.')
        self.independent=QCheckBox('The repeats are independent; common base/datum errors and correlations between measurements are not modelled.')
        for checkbox in (self.ellipsoid,self.uncertainty_confirm,self.independent): form.addRow(checkbox)
        hint=QLabel('The same control point ID means the same physical point. At least two FIX measurements per point are required. '
            'Text FIX/FIXED/RTK FIX is required; numeric status codes are not interpreted automatically. Document and normalise them in a working copy before import. Known control points are used for separate checks, not as error-free constraints in this profile. '
            'A fixed computational origin is used only as a numerical translation in SALSA; it is not a surveyed base. '
            'The original file is preserved. Results contain ECEF and latitude/longitude/ellipsoidal height, with no NN2000 conversion.')
        hint.setWordWrap(True); form.addRow(hint)
        row=QHBoxLayout(); self.preview_button=QPushButton('Check setup'); self.preview_button.clicked.connect(self.preview)
        row.addWidget(self.preview_button); self.run_button=QPushButton('Adjust your CSV in SALSA …'); self.run_button.clicked.connect(self.run)
        row.addWidget(self.run_button); form.addRow(row)
        self.preview_text=QTextBrowser(); self.preview_text.setMinimumHeight(110); form.addRow(self.preview_text)
        self.map_button=QPushButton('Show latest SALSA coordinate result on map (ELLIP)'); self.map_button.setEnabled(False)
        self.map_button.clicked.connect(self.add_layer); form.addRow(self.map_button)
        for control in (self.frame,self.sigma_mode): control.currentIndexChanged.connect(self.invalidate)
        for control in (self.epoch,self.uncertainty,self.corrections,self.antenna): control.textChanged.connect(self.invalidate)
        for control in self.sigmas: control.valueChanged.connect(self.invalidate)
        for control in (self.ellipsoid,self.uncertainty_confirm,self.independent): control.toggled.connect(self.invalidate)
        self.rebuild()

    def invalidate(self,*_):
        self.map_button.setEnabled(False); self.preview_text.setPlainText('Settings have changed. Check and recalculate.')

    def rebuild(self):
        defaults=mapping_for(self.table['headers']) if self.table else {}
        for row,(field,(label,_)) in enumerate(FIELDS.items()):
            old=self.mapping.cellWidget(row,1)
            if old:
                old.hide(); self.mapping.removeCellWidget(row,1)
            item=QTableWidgetItem(label); self.mapping.setItem(row,0,item)
            combo=QComboBox(); combo.addItem('— none —','')
            for name in self.table['headers'] if self.table else []: combo.addItem(name,name)
            combo.setCurrentIndex(max(0,combo.findData(defaults.get(field,''))))
            combo.currentIndexChanged.connect(self.invalidate); self.mapping.setCellWidget(row,1,combo)

    def open_csv(self):
        path,_=QFileDialog.getOpenFileName(self,'Select GNSS coordinates with column headers','','Text data (*.csv *.txt *.tsv);;All files (*)')
        if not path: return
        try:
            self.table=read_table(path); self.synthetic=False; self.rebuild()
            self.source.setText(Path(path).name+f" – {len(self.table['rows'])} rows")
            self.frame.setCurrentIndex(0); self.epoch.clear(); self.uncertainty.clear(); self.corrections.clear(); self.antenna.clear()
            for box in self.sigmas: box.setValue(0)
            for box in (self.ellipsoid,self.uncertainty_confirm,self.independent): box.setChecked(False)
            self.invalidate()
        except Exception as error: self.owner.error(error)

    def load_demo(self):
        try:
            self.table=read_table(Path(__file__).parent/'examples/emlid_salsa_llh.csv'); self.synthetic=True; self.rebuild()
            self.source.setText('Synthetic coordinate example – not field data.')
            self.frame.setCurrentIndex(1); self.epoch.setText('2026-10-18'); self.sigma_mode.setCurrentIndex(1)
            self.uncertainty.setText('Synthetic known 1σ values, geodetic ENU.')
            self.corrections.setText('Synthetic example; no actual correction service/base.')
            self.antenna.setText('Synthetic point heights; no additional antenna height correction.')
            for box in (self.ellipsoid,self.uncertainty_confirm,self.independent): box.setChecked(False)
            self.invalidate()
        except Exception as error: self.owner.error(error)

    def values(self):
        if not self.table: raise ValueError('Select your CSV or load the coordinate example.')
        mapping={field:self.mapping.cellWidget(row,1).currentData() for row,field in enumerate(FIELDS)}
        options=CoordinateOptions(synthetic=self.synthetic,frame=self.frame.currentData(),epoch=self.epoch.text().strip(),
            ellipsoid_confirmed=self.ellipsoid.isChecked(),independent_confirmed=self.independent.isChecked(),
            uncertainty_confirmed=self.uncertainty_confirm.isChecked(),sigma_mode=self.sigma_mode.currentData(),
            sigma_e=self.sigmas[0].value(),sigma_n=self.sigmas[1].value(),sigma_u=self.sigmas[2].value(),
            uncertainty_source=self.uncertainty.text().strip(),correction_source=self.corrections.text().strip(),antenna_note=self.antenna.text().strip())
        return self.table,mapping,options

    def preview(self):
        try:
            _,meta=coordinate_job(*self.values())
            self.preview_text.setPlainText(f"{len(meta['observations'])} FIX coordinates, {len(meta['identifiers'])} control points, {len(meta['excluded'])} excluded.\n"+
                meta['frame_name']+'; ellipsoidal point height; measurement epoch '+meta['epoch']+'\n'+ '\n'.join(meta['assumptions']))
            return True
        except Exception as error:
            self.owner.error(error); return False

    def run(self):
        if not self.preview(): return
        table,mapping,options=self.values()
        self.owner.start_job(lambda folder:prepare_coordinate(folder,table,mapping,options),'SALSA_coordinates_',False)

    def add_layer(self):
        result=self.owner.engine_result
        if not result or result.get('kind')!='coordinate_observations' or not self.map_button.isEnabled(): return
        try:
            rows=[]
            for label,(lat,lon,h) in result['positions_geodetic'].items():
                rows.append(([result['identifiers'][label],result['epoch'],'ELLIP','REVIEW REQUIRED'],QgsGeometry(QgsPoint(lon,lat,h))))
            layer=make_layer('PointZ','SALSA – coordinate adjustment (ELLIP)',result['frame'],
                [(n,QMetaType.Type.QString) for n in ('control_id','epoch','height_datum','assessment')],rows)
            layer.renderer().setSymbol(QgsMarkerSymbol.createSimple({'name':'circle','color':'#d89423','size':'3'}))
            labels=QgsPalLayerSettings(); labels.fieldName='control_id'
            layer.setLabeling(QgsVectorLayerSimpleLabeling(labels)); layer.setLabelsEnabled(True)
            QgsProject.instance().addMapLayer(layer); self.owner.iface.setActiveLayer(layer)
            self.owner.status.setText('SALSA result added with ellipsoidal Z. No NN2000 transformation.')
        except Exception as error: self.owner.error(error)
