# SPDX-License-Identifier: GPL-3.0-only
"""Explicit, fail-closed setup for independent Emlid coordinate observations."""
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
        info=QLabel('Egne Emlid CSV-filer: vektet utjevning av gjentatte absolutte koordinatobservasjoner i SALSA. '
            'Dette er ikke rå GNSS-/baselinjeutjevning. Krever bredde/lengde, ellipsoidisk punkthøyde og dokumenterte 1σ-verdier. '
            'Eksporter helst CSV «All columns» med kolonnenavn fra Emlid Flow. RINEX og vanlig NN2000-Elevation kan ikke brukes direkte her.')
        info.setWordWrap(True); form.addRow(info)
        row=QHBoxLayout()
        for title,callback in [('Åpne egen Emlid-CSV …',self.open_csv),('Last syntetisk koordinateksempel',self.load_demo)]:
            button=QPushButton(title); button.clicked.connect(callback); row.addWidget(button)
        form.addRow(row)
        self.source=QLabel('Ingen CSV valgt.'); self.source.setWordWrap(True); form.addRow(self.source)
        self.mapping=QTableWidget(len(FIELDS),2)
        self.mapping.setHorizontalHeaderLabels(['Hva brukes?','Kildekolonne'])
        self.mapping.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.mapping.setMinimumHeight(270); form.addRow(self.mapping)
        self.frame=QComboBox(); self.frame.addItem('Velg dokumentert grunnlag for bredde/lengde/h','')
        self.frame.addItem('ETRS89 – ellipsoidisk GRS80 (EPSG:4937)','EPSG:4937')
        self.frame.addItem('WGS84 – ellipsoidisk (EPSG:4979)','EPSG:4979')
        form.addRow('Geodetisk koordinatgrunnlag',self.frame)
        self.epoch=QLineEdit(); self.epoch.setPlaceholderText('Måleepoke ÅÅÅÅ-MM-DD; ingen epokeomregning')
        form.addRow('Måleepoke',self.epoch)
        self.sigma_mode=QComboBox(); self.sigma_mode.addItem('Felles brukerangitte 1σ-verdier','constant'); self.sigma_mode.addItem('Dokumenterte 1σ-kolonner','columns')
        form.addRow('Usikkerhetsmodell',self.sigma_mode)
        row=QHBoxLayout(); self.sigmas=[]
        for label in ('Øst','Nord','Opp'):
            row.addWidget(QLabel(label)); box=QDoubleSpinBox(); box.setRange(0,100); box.setDecimals(4); box.setSuffix(' m'); box.setSingleStep(.005)
            row.addWidget(box); self.sigmas.append(box)
        form.addRow('Felles 1σ (ved felles modell)',row)
        self.uncertainty=QLineEdit(); self.uncertainty.setPlaceholderText('Grunnlag for 1σ; Emlid RMS kopieres ikke automatisk')
        form.addRow('Kilde/vurdering av usikkerhet',self.uncertainty)
        self.corrections=QLineEdit(); self.corrections.setPlaceholderText('Korreksjonstjeneste/mountpoint eller base og koordinatgrunnlag')
        form.addRow('Korreksjoner/base',self.corrections)
        self.antenna=QLineEdit(); self.antenna.setPlaceholderText('Antennehøyde/tilt og bekreftelse på at CSV-høyden gjelder punktet')
        form.addRow('Antenne og punkthøyde',self.antenna)
        self.ellipsoid=QCheckBox('Jeg har kontrollert datum og at valgt høyde er ellipsoidisk punkthøyde i meter, ikke NN2000/NN54.')
        self.uncertainty_confirm=QCheckBox('Verdiene er vurderte 1σ i geodetisk ENU; korrelasjon mellom E/N/U settes til null.')
        self.independent=QCheckBox('Gjentakene er selvstendige; felles base-/datumfeil og korrelasjon mellom målingene er ikke modellert.')
        for checkbox in (self.ellipsoid,self.uncertainty_confirm,self.independent): form.addRow(checkbox)
        hint=QLabel('Samme kontrollpunkt-ID betyr samme fysiske punkt. Minst to FIX-målinger per punkt kreves. '
            'Kjente kontrollpunkter brukes som separat kontroll, ikke som feilfrie tvangspunkter i denne profilen. '
            'En fast beregningsorigo brukes bare som tallforskyvning i SALSA; det er ikke en innmålt base. '
            'Originalfilen beholdes. Resultatet får ECEF og bredde/lengde/ellipsoidisk høyde, ingen NN2000-omregning.')
        hint.setWordWrap(True); form.addRow(hint)
        row=QHBoxLayout(); self.preview_button=QPushButton('Kontroller oppsett'); self.preview_button.clicked.connect(self.preview)
        row.addWidget(self.preview_button); self.run_button=QPushButton('Utjevn egen CSV i SALSA …'); self.run_button.clicked.connect(self.run)
        row.addWidget(self.run_button); form.addRow(row)
        self.preview_text=QTextBrowser(); self.preview_text.setMinimumHeight(110); form.addRow(self.preview_text)
        self.map_button=QPushButton('Vis siste SALSA-koordinatresultat i kart (ELLIP)'); self.map_button.setEnabled(False)
        self.map_button.clicked.connect(self.add_layer); form.addRow(self.map_button)
        for control in (self.frame,self.sigma_mode): control.currentIndexChanged.connect(self.invalidate)
        for control in (self.epoch,self.uncertainty,self.corrections,self.antenna): control.textChanged.connect(self.invalidate)
        for control in self.sigmas: control.valueChanged.connect(self.invalidate)
        for control in (self.ellipsoid,self.uncertainty_confirm,self.independent): control.toggled.connect(self.invalidate)
        self.rebuild()

    def invalidate(self,*_):
        self.map_button.setEnabled(False); self.preview_text.setPlainText('Oppsettet er endret. Kontroller og beregn på nytt.')

    def rebuild(self):
        defaults=mapping_for(self.table['headers']) if self.table else {}
        for row,(field,(label,_)) in enumerate(FIELDS.items()):
            old=self.mapping.cellWidget(row,1)
            if old:
                old.hide(); self.mapping.removeCellWidget(row,1)
            item=QTableWidgetItem(label); self.mapping.setItem(row,0,item)
            combo=QComboBox(); combo.addItem('— ingen —','')
            for name in self.table['headers'] if self.table else []: combo.addItem(name,name)
            combo.setCurrentIndex(max(0,combo.findData(defaults.get(field,''))))
            combo.currentIndexChanged.connect(self.invalidate); self.mapping.setCellWidget(row,1,combo)

    def open_csv(self):
        path,_=QFileDialog.getOpenFileName(self,'Velg egen Emlid CSV','','CSV (*.csv);;Alle filer (*)')
        if not path: return
        try:
            self.table=read_table(path); self.synthetic=False; self.rebuild()
            self.source.setText(Path(path).name+f" – {len(self.table['rows'])} rader")
            self.frame.setCurrentIndex(0); self.epoch.clear(); self.uncertainty.clear(); self.corrections.clear(); self.antenna.clear()
            for box in self.sigmas: box.setValue(0)
            for box in (self.ellipsoid,self.uncertainty_confirm,self.independent): box.setChecked(False)
            self.invalidate()
        except Exception as error: self.owner.error(error)

    def load_demo(self):
        try:
            self.table=read_table(Path(__file__).parent/'examples/emlid_salsa_llh.csv'); self.synthetic=True; self.rebuild()
            self.source.setText('Syntetisk koordinateksempel – ikke feltdata.')
            self.frame.setCurrentIndex(1); self.epoch.setText('2026-10-18'); self.sigma_mode.setCurrentIndex(1)
            self.uncertainty.setText('Syntetiske kjente 1σ-verdier, geodetisk ENU.')
            self.corrections.setText('Syntetisk eksempel; ingen reell korreksjonstjeneste/base.')
            self.antenna.setText('Syntetiske punkthøyder; ingen ekstra antennehøydekorreksjon.')
            for box in (self.ellipsoid,self.uncertainty_confirm,self.independent): box.setChecked(False)
            self.invalidate()
        except Exception as error: self.owner.error(error)

    def values(self):
        if not self.table: raise ValueError('Velg en egen CSV eller last koordinateksempelet.')
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
            self.preview_text.setPlainText(f"{len(meta['observations'])} FIX-koordinater, {len(meta['identifiers'])} kontrollpunkter, {len(meta['excluded'])} utelatt.\n"+
                meta['frame_name']+'; ellipsoidisk punkthøyde; måleepoke '+meta['epoch']+'\n'+ '\n'.join(meta['assumptions']))
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
                rows.append(([result['identifiers'][label],result['epoch'],'ELLIP','MÅ VURDERES'],QgsGeometry(QgsPoint(lon,lat,h))))
            layer=make_layer('PointZ','SALSA – koordinatutjevning (ELLIP)',result['frame'],
                [(n,QMetaType.Type.QString) for n in ('kontroll_id','epoke','hoydegrunnlag','vurdering')],rows)
            layer.renderer().setSymbol(QgsMarkerSymbol.createSimple({'name':'circle','color':'#d89423','size':'3'}))
            labels=QgsPalLayerSettings(); labels.fieldName='kontroll_id'
            layer.setLabeling(QgsVectorLayerSimpleLabeling(labels)); layer.setLabelsEnabled(True)
            QgsProject.instance().addMapLayer(layer); self.owner.iface.setActiveLayer(layer)
            self.owner.status.setText('SALSA-resultat lagt til med ellipsoidisk Z. Ingen NN2000-transformasjon.')
        except Exception as error: self.owner.error(error)
