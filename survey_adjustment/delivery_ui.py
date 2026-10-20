# SPDX-License-Identifier: GPL-3.0-only
"""English reference review and local, traceable delivery packages."""
from pathlib import Path
from dataclasses import asdict
import json
import html
from qgis.PyQt.QtCore import QUrl
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QFormLayout,QLabel,QLineEdit,
    QPushButton,QComboBox,QCheckBox,QTextBrowser,QFileDialog,QDialogButtonBox,QTabWidget,QWidget)
from . import delivery as d


class DeliveryDialog(QDialog):
    def __init__(self,parent,host,run_path=None):
        super().__init__(parent)
        self.host=host; self.run=None; self.result=None; self.folder=None
        self.setWindowTitle('Norwegian reference review and delivery'); self.resize(1100,850)
        layout=QVBoxLayout(self)
        self.banner=QLabel('Choose a completed GNSS-network or mixed-survey quality.json. All review and export are local; original calculations are preserved.')
        self.banner.setWordWrap(True); layout.addWidget(self.banner)
        row=QHBoxLayout()
        self.open_button=QPushButton('Open verified calculation …'); self.open_button.clicked.connect(self.open_run)
        self.check_package_button=QPushButton('Check an existing delivery package …'); self.check_package_button.clicked.connect(self.check_package)
        row.addWidget(self.open_button); row.addWidget(self.check_package_button); layout.addLayout(row)
        self.tabs=QTabWidget(); layout.addWidget(self.tabs,1)
        page=QWidget(); form=QFormLayout(page)
        self.source_label=QLabel('No calculation selected.'); self.source_label.setWordWrap(True); form.addRow('Saved calculation',self.source_label)
        self.epoch_label=QLabel(); form.addRow('Coordinate epoch',self.epoch_label)
        self.source_height=QComboBox(); self.source_height.addItem('Select the documented input height reference','')
        self.target_height=QComboBox(); self.target_height.addItem('Select delivery height reference','')
        for height in d.HEIGHTS:
            self.source_height.addItem(height,height); self.target_height.addItem(height,height)
        form.addRow('Input height reference',self.source_height); form.addRow('Delivery height reference',self.target_height)
        self.target_crs=QComboBox(); self.target_crs.addItem('Select delivery projection','')
        for crs,label in d.OUTPUT_CRS.items(): self.target_crs.addItem(label+' ('+crs+')',crs)
        form.addRow('Delivery E/N projection',self.target_crs)
        self.fields={}
        helps={
            'reviewer':'Person responsible for reviewing this delivery. This is recorded locally in the package.',
            'recipient':'Recipient / project specification and revision. No email or upload is performed.',
            'purpose':'What this package is intended for, and the scope of the review.',
            'frame_evidence':'Document evidence that input coordinates are in national EUREF89. A generic ETRS89/WGS84 label is insufficient for a precise frame shift.',
            'epoch_evidence':'Source of the saved coordinate epoch. Acquisition timestamps and the EUREF89 frame anchor epoch are separate concepts. No re-epoching is performed.',
            'height_evidence':'Source of the point-height basis, NN2000 version, HREF history and physical mark/antenna reductions. Do not apply HREF again to an NN2000 height.',
            'uncertainty_review':'Explain original weights/correlations and any transformation/model uncertainty. Output sigmas are not propagated; do not reuse original sigmas as transformed accuracy.',
            'change_log':'Describe manual changes and exclusions, or explicitly state none with the review scope. Original calculation options, records and exclusion reasons are preserved separately.',
        }
        for name,help_text in helps.items():
            field=QLineEdit(); field.setPlaceholderText(help_text); field.setToolTip(help_text); self.fields[name]=field
            box=QWidget(); box_layout=QVBoxLayout(box); box_layout.setContentsMargins(0,0,0,0)
            box_layout.addWidget(field); help_label=QLabel(help_text); help_label.setWordWrap(True); box_layout.addWidget(help_label)
            form.addRow(name.replace('_',' ').title(),box)
        self.model_paths={}
        for height,(filename,_) in d.MODELS.items():
            box=QWidget(); line=QHBoxLayout(box); line.setContentsMargins(0,0,0,0)
            field=QLineEdit(); field.setPlaceholderText(filename+' — needed only when converting this height reference')
            self.model_paths[height]=field; button=QPushButton('Choose local HREF …')
            button.clicked.connect(lambda _=False,h=height:self.choose_model(h)); line.addWidget(field); line.addWidget(button)
            form.addRow(height+' model',box)
        self.reviewed=QCheckBox('I reviewed the reference evidence, acquisition times, model selection, changes and uncertainty limitations')
        form.addRow(self.reviewed)
        # Long field guidance must remain reachable on smaller desktop screens.
        from qgis.PyQt.QtWidgets import QScrollArea
        scroll=QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(page)
        self.tabs.addTab(scroll,'Reference evidence and output')
        self.overview=QTextBrowser(); self.tabs.addTab(self.overview,'Review / delivered coordinates')
        self.help=QTextBrowser(); help_path=Path(__file__).parent/'DELIVERY.txt'
        if not help_path.exists(): help_path=Path(__file__).parent.parent/'docs/DELIVERY.txt'
        self.help.setPlainText(help_path.read_text(encoding='utf-8') if help_path.exists() else '\n'.join(d.LIMITS))
        self.tabs.addTab(self.help,'Help and model references')
        note=QLabel('Choose NN2000:2018 or NN2000:2025 explicitly. HREF2025A became official on 2026-10-14; keep established project continuity. '
                    'Only reviewed local GeoTIFF bytes are accepted. No automatic grid download, frame shift, epoch transformation or uncertainty propagation.')
        note.setWordWrap(True); layout.addWidget(note)
        row=QHBoxLayout()
        self.preview_button=QPushButton('Check references / preview delivery'); self.preview_button.clicked.connect(self.preview)
        self.export_button=QPushButton('Create local delivery package …'); self.export_button.clicked.connect(self.export); self.export_button.setEnabled(False)
        self.folder_button=QPushButton('Open delivery folder'); self.folder_button.clicked.connect(self.open_folder); self.folder_button.setEnabled(False)
        for button in (self.preview_button,self.export_button,self.folder_button): row.addWidget(button)
        layout.addLayout(row)
        self.status=QLabel('No delivery prepared. Reference evidence is never inferred from point coordinates.'); self.status.setWordWrap(True); layout.addWidget(self.status)
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Close); buttons.rejected.connect(self.reject); layout.addWidget(buttons)
        for field in self.fields.values(): field.textChanged.connect(self.invalidate)
        for field in self.model_paths.values(): field.textChanged.connect(self.invalidate)
        for field in (self.source_height,self.target_height,self.target_crs): field.currentIndexChanged.connect(self.invalidate)
        self.reviewed.toggled.connect(self.invalidate)
        if run_path: self.load(run_path)

    def invalidate(self,*_):
        self.result=None; self.export_button.setEnabled(False); self.folder_button.setEnabled(False); self.overview.clear()
        self.status.setText('Settings changed. Check the current reference review before exporting.')

    def review(self):
        return d.ReferenceReview(source_height=self.source_height.currentData(),target_height=self.target_height.currentData(),
            target_crs=self.target_crs.currentData(),reviewed=self.reviewed.isChecked(),
            **{name:field.text().strip() for name,field in self.fields.items()})

    def paths(self):
        return {height:field.text().strip() for height,field in self.model_paths.items()}

    def load(self,path):
        try:
            run=d.load_run(path,self.host.solver.text())
            self.run=run; project=run['project']; self.invalidate()
            self.source_label.setText(project['name']+' | '+project['crs']+' | '+project['height_datum']+' | '+run['report']['kind'])
            self.epoch_label.setText(project['epoch']+' — retained from the calculation; acquisition-session times are listed in the preview')
            self.banner.setText(('SYNTHETIC TRAINING DATA — ' if project['synthetic'] else 'PRIVATE LOCAL MEASUREMENTS — ')+str(path))
            self.source_height.setCurrentIndex(max(0,self.source_height.findData(project['height_datum'])))
            self.target_height.setCurrentIndex(0); self.target_crs.setCurrentIndex(self.target_crs.findData(project['crs']))
            for field in self.fields.values(): field.clear()
            self.reviewed.setChecked(False)
            self.status.setText('Completed SALSA run verified read-only. Supply and review the separate delivery reference evidence.')
            return True
        except Exception as error:
            self.status.setText('Cannot open calculation: '+str(error)); return False

    def open_run(self):
        path,_=QFileDialog.getOpenFileName(self,'Choose quality.json from a completed SALSA run','','Completed calculation (quality.json)')
        return self.load(path) if path else False

    def choose_model(self,height):
        path,_=QFileDialog.getOpenFileName(self,'Choose reviewed '+height+' HREF GeoTIFF','','GeoTIFF (*.tif)')
        if path: self.model_paths[height].setText(path)

    def preview(self):
        try:
            if not self.run: raise ValueError('Open a completed verified SALSA run first.')
            fresh=d.load_run(self.run['folder']/'quality.json',self.run['program'])
            if fresh['files']!=self.run['files']: raise ValueError('Calculation changed; reopen it before reviewing.')
            self.result,_=d.preview(fresh,self.review(),self.paths())
            esc=lambda value:html.escape(str(value))
            r=self.review()
            coordinates=lambda values:' / '.join(f'{v:.6f}' for v in values) if values is not None else 'Not adjusted / not delivered'
            text='<h3>Delivery reference review</h3><p><b>EUREF89</b> — coordinate epoch '+esc(self.run['project']['epoch'])+' retained.</p>'
            text+='<p>Input: '+esc(self.run['project']['crs'])+' / '+esc(r.source_height)+'<br>Output: '+esc(r.target_crs)+' / '+esc(r.target_height)+'</p>'
            text+='<p><b>Output uncertainty is not propagated.</b> Original covariance and model warnings remain in the calculation report.</p>'
            text+='<h4>Acquisition sessions (UTC)</h4><ul>'+''.join('<li>'+esc(s['id'])+': '+esc(s['start_utc'])+' to '+esc(s['end_utc'] or 'end undocumented')+'</li>' for s in self.result['sessions'])+'</ul>'
            text+='<h4>Point coordinates — Easting / Northing / height, metres</h4><table border="1" cellpadding="5"><tr><th>Point / role</th><th>Source</th><th>Adjusted</th><th>Delivery</th></tr>'
            for row in self.result['records']:
                text+='<tr><td>'+esc(row['id'])+' / '+esc(row['role'])+'</td>'+''.join('<td>'+esc(coordinates(row[key]))+'</td>' for key in ('source','adjusted','delivered'))+'</tr>'
            text+='</table><h4>Review before use</h4><ul>'+''.join('<li>'+esc(w)+'</li>' for w in self.result['warnings'])+'</ul>'
            self.overview.setHtml(text)
            self.export_button.setEnabled(True); self.tabs.setCurrentWidget(self.overview)
            self.status.setText('Reference preview complete. Review warnings, height changes and acquisition times; this is not delivery approval.')
            return True
        except Exception as error:
            self.invalidate(); self.status.setText('Cannot prepare delivery: '+str(error)); return False

    def export(self):
        try:
            if self.result is None: raise ValueError('Preview the current reference settings before exporting.')
            parent=QFileDialog.getExistingDirectory(self,'Choose a LOCAL parent folder for a new delivery package')
            if not parent: return False
            folder=d.export_package(parent,self.run,self.review(),self.paths(),self.result)
            self.folder=folder; self.folder_button.setEnabled(True)
            self.status.setText('Local package verified, including PointZ readback and checksums. Original calculation preserved. '+str(folder))
            return True
        except Exception as error:
            self.status.setText('No complete delivery accepted: '+str(error)); return False

    def check_package(self):
        folder=QFileDialog.getExistingDirectory(self,'Choose a LOCAL Survey_Delivery folder to check')
        if not folder: return False
        try:
            checked=d.verify_package(folder)
            self.status.setText(checked['status']+' — '+str(checked['files'])+' files. '+checked['warning']); return True
        except Exception as error:
            self.status.setText('Delivery check failed: '+str(error)); return False

    def open_folder(self):
        if self.folder: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.folder)))
