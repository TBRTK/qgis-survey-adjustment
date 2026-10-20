# SPDX-License-Identifier: GPL-3.0-only
"""Preview one documented processed baseline before changing a network project."""
from copy import deepcopy
from pathlib import Path
from qgis.PyQt.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QPushButton, QFileDialog, QTableWidget, QTableWidgetItem, QAbstractItemView,
    QHeaderView, QLineEdit, QCheckBox, QTextBrowser, QDialogButtonBox, QScrollArea, QWidget)
from . import baselines as b
from . import network as n
from .network_ui import combo


class BaselineImportDialog(QDialog):
    def __init__(self, parent, project):
        super().__init__(parent)
        self.project = deepcopy(project)
        self.path = self.source = self.plan = self.result_project = None
        self.setWindowTitle('Import measured GNSS baseline')
        self.resize(1000, 790)
        layout = QVBoxLayout(self)
        help_text = QLabel('Import a processed base → rover solution, not raw RINEX or a list of repeated point coordinates. '
            'POS requires the processing mode, reference position, full covariance and supported headers. '
            'Add the physical endpoints and a UTC acquisition session to the network first. '
            'Choose exactly one FIX epoch / documented occupation solution; no averaging or automatic independence.')
        help_text.setWordWrap(True); layout.addWidget(help_text)
        row = QHBoxLayout()
        choose = QPushButton('Open POS / baseline CSV …'); choose.clicked.connect(self.choose)
        example = QPushButton('Load synthetic POS'); example.clicked.connect(lambda: self.load(Path(__file__).parent/'examples/synthetic_baseline_llh.pos'))
        row.addWidget(choose); row.addWidget(example)
        self.filename = QLabel('No source selected'); self.filename.setWordWrap(True); row.addWidget(self.filename, 1)
        layout.addLayout(row)
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel('POS processing mode'))
        self.source_mode = combo({'':'Read from file header (default)',
            'static':'Static — operator documented if omitted from header',
            'kinematic':'Kinematic — operator documented if omitted from header'})
        self.source_mode.setToolTip('Use the actual Emlid/RTKLIB processing workflow, not receiver motion. The declaration is retained in the import audit. Original headers are never changed or overridden.')
        mode_row.addWidget(self.source_mode, 1); layout.addLayout(mode_row)
        self.source_mode.currentIndexChanged.connect(self.mode_changed)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(['Line', 'Source time', 'Quality', 'Native position / vector', 'Eligible', 'Reason'])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.table.setMinimumHeight(100); self.table.setMaximumHeight(120)
        self.table.itemSelectionChanged.connect(self.selected)
        layout.addWidget(self.table)
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        content = QWidget(); box = QVBoxLayout(content); grid = QGridLayout(); box.addLayout(grid)
        self.origin = combo({'':'Select processing base point', **{p['id']:p['id'] for p in project['points']}})
        self.target = combo({'':'Select rover point', **{p['id']:p['id'] for p in project['points']}})
        self.session = combo({'':'Select acquisition session', **{s['id']:s['id'] for s in project['sessions']}})
        self.use = combo(n.USES)
        self.frame = combo({'':'Select documented ECEF frame', 'EPSG:4936':'ETRS89 / GRS80 ECEF', 'EPSG:4978':'WGS84 ECEF'})
        self.identifier = QLineEdit(); self.epoch = QLineEdit(project['epoch'])
        self.epoch.setPlaceholderText('YYYY-MM-DD; coordinate reference epoch, not inferred')
        self.antenna_note = QLineEdit(); self.antenna_note.setPlaceholderText('Which physical points? Antenna/ARP/tilt corrections already applied by processor?')
        self.uncertainty_note = QLineEdit(); self.uncertainty_note.setPlaceholderText('Processing software/settings, fixed base, formal covariance and shared errors / dependence')
        for index, (label, field) in enumerate((('From / base', self.origin), ('To / rover', self.target),
                ('Acquisition session', self.session), ('Intended use', self.use), ('Observation ID', self.identifier),
                ('Vector frame', self.frame), ('Coordinate epoch', self.epoch))):
            row, col = divmod(index, 2)
            grid.addWidget(QLabel(label), row, col*2); grid.addWidget(field, row, col*2+1)
        for row, label, field in ((4, 'Endpoint / antenna treatment', self.antenna_note), (5, 'Covariance / model review', self.uncertainty_note)):
            grid.addWidget(QLabel(label), row, 0); grid.addWidget(field, row, 1, 1, 3)
        detail = QLabel('LLH heights must be ellipsoidal. RTKLIB signed covariance roots are decoded and the full matrix is rotated '
            'to ECEF at the documented base/rover location. CSV uses explicit ΔXYZ metres and covariance m². '
            'NN2000/point heights in the project are kept unchanged. The vector must already refer to those physical endpoints. '
            'ECEF ΔZ is not a point height or NN2000 height difference. '
            'GPST is retained as GPST; it is not relabelled UTC or checked against UTC session bounds.')
        detail.setWordWrap(True); box.addWidget(detail)
        self.synthetic = QCheckBox('Source is a constructed synthetic demonstration (keep separate from real surveys)')
        box.addWidget(self.synthetic)  # Never label a new file synthetic from the project flag alone.
        self.relative = QCheckBox('I verified the relative base → rover solution, selected occupation and point IDs')
        self.endpoints = QCheckBox('I documented the frame, epoch and antenna / point-height treatment')
        self.covariance = QCheckBox('I reviewed covariance and dependence; formal precision is not absolute accuracy')
        for field in (self.relative, self.endpoints, self.covariance): box.addWidget(field)
        self.occupation=QCheckBox('The selected processed result represents a documented stationary occupation of this physical point')
        self.occupation_note=QLineEdit(); self.occupation_note.setPlaceholderText('Required for raw-job handoff: occupied point, stationary interval and why this epoch/result represents it. Never treat a moving track as point observations.')
        box.addWidget(self.occupation); box.addWidget(self.occupation_note)
        self.occupation.hide(); self.occupation_note.hide()
        box.addStretch(); scroll.setWidget(content); layout.addWidget(scroll, 1)
        self.review = QTextBrowser(); self.review.setMaximumHeight(130); layout.addWidget(self.review)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.preview_button = self.buttons.addButton('Preview selected baseline', QDialogButtonBox.ButtonRole.ActionRole)
        self.import_button = self.buttons.addButton('Add baseline to inventory', QDialogButtonBox.ButtonRole.ActionRole)
        self.import_button.setEnabled(False)
        self.preview_button.clicked.connect(self.preview); self.import_button.clicked.connect(self.finish)
        self.buttons.rejected.connect(self.reject); layout.addWidget(self.buttons)
        for field in (self.origin,self.target,self.session,self.use,self.frame): field.currentIndexChanged.connect(self.invalidate)
        for field in (self.identifier,self.epoch,self.antenna_note,self.uncertainty_note): field.textChanged.connect(self.invalidate)
        for field in (self.synthetic,self.relative,self.endpoints,self.covariance): field.toggled.connect(self.invalidate)
        self.occupation.toggled.connect(self.invalidate); self.occupation_note.textChanged.connect(self.invalidate)

    def invalidate(self, *_):
        self.plan = None
        self.result_project = None
        self.import_button.setEnabled(False)
        self.review.setPlainText('Preview required. Source files and project are not changed until you explicitly add the baseline.')

    def choose(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Select a processed relative GNSS solution', '', 'GNSS solution (*.pos *.csv *.txt *.tsv)')
        if path: return self.load(path)
        return False

    def mode_changed(self, *_):
        if self.path is not None:
            return self.load(self.path, keep_mode=True)
        self.invalidate()

    def load(self, path, *, keep_mode=False):
        if not keep_mode:
            self.source_mode.blockSignals(True)
            self.source_mode.setCurrentIndex(0)
            self.source_mode.blockSignals(False)
        self.invalidate(); self.source = None; self.path = Path(path); self.table.setRowCount(0)
        self.occupation.setChecked(False);self.occupation_note.clear();self.occupation.hide();self.occupation_note.hide()
        for check in (self.synthetic,self.relative,self.endpoints,self.covariance):
            check.setChecked(False)
        try:
            source = b.read_source(path, processing_mode=self.source_mode.currentData() or None)
            self.path, self.source = Path(path), source
            if source.get('processing'):
                context=source['processing'];self.occupation.show();self.occupation_note.show()
                for field,key in ((self.origin,'base'),(self.target,'rover'),(self.session,'session'),(self.frame,'frame')):
                    field.setCurrentIndex(max(0,field.findData(context[key])))
                self.epoch.setText(context['epoch'])
                self.antenna_note.setText(context['antenna_note'])
                self.uncertainty_note.setText(context['model_note']+'; raw job '+context['job_id']+'; engine/config/input hashes retained in audit.')
            meta=source['metadata']
            reference=f"Processing base ({'XYZ metres' if meta['representation']=='XYZ' else 'lat/lon/ellipsoidal h'}): {meta['reference']}" if meta['reference'] is not None else 'Precomputed relative ΔXYZ; processing-base coordinates are not included in this CSV.'
            self.filename.setText(f"{source['source_name']} — {meta['representation']}, {meta['mode']}, {meta['time_system']}\n{reference}")
            self.table.setRowCount(len(source['rows']))
            for index, record in enumerate(source['rows']):
                values = [record['line'], record['time'], record['quality'], str(record['values']),
                          'Yes' if record['eligible'] else 'No', record['reason']]
                for col, value in enumerate(values): self.table.setItem(index,col,QTableWidgetItem(str(value)))
            self.table.clearSelection(); self.table.setCurrentCell(-1,-1)
            self.review.setPlainText('Select one eligible FIX row. SHA256: '+source['source_sha256']+'\n'
                +source['mode_declaration']+'\n'+'\n'.join(b.WARNINGS))
            return True
        except (OSError, ValueError) as error:
            self.filename.setText('Source not loaded'); self.review.setPlainText('Cannot read baseline source: '+str(error))
            return False

    def selected(self):
        self.invalidate()
        index = self.table.currentRow()
        if self.source and 0 <= index < len(self.source['rows']):
            row = self.source['rows'][index]
            self.identifier.setText(row['identifier'] or f"B_{self.source['source_sha256'][:8]}_{row['line']}")
            for key, field in (('origin',self.origin),('target',self.target),('session',self.session)):
                if row[key]: field.setCurrentIndex(max(0,field.findData(row[key])))

    def options(self):
        options=b.ImportOptions(frame=self.frame.currentData(),epoch=self.epoch.text().strip(),
            origin=self.origin.currentData(),target=self.target.currentData(),session=self.session.currentData(),
            identifier=self.identifier.text().strip(),use=self.use.currentData(),
            antenna_note=self.antenna_note.text().strip(),uncertainty_note=self.uncertainty_note.text().strip(),
            relative_confirmed=self.relative.isChecked(),endpoints_confirmed=self.endpoints.isChecked(),
            covariance_confirmed=self.covariance.isChecked(),synthetic=self.synthetic.isChecked())
        options.stationary_confirmed=self.occupation.isChecked();options.occupation_note=self.occupation_note.text().strip()
        return options

    def preview(self):
        self.plan = None; self.import_button.setEnabled(False)
        try:
            index = self.table.currentRow()
            if not self.source or index < 0:
                raise ValueError('Open a source and select exactly one eligible row.')
            plan = b.plan_import(self.project,self.source,self.source['rows'][index]['line'],self.options())
            obs = plan['observations'][-1]
            self.review.setPlainText(f"Ready to add one baseline: {obs['from_point']} → {obs['to_point']}, session {obs['session']}.\n"
                f"Source epoch: {self.source['rows'][index]['time']} [{self.source['metadata']['time_system']}]. Declared frame {obs['frame']}; coordinate epoch {self.epoch.text()}.\n"
                f"ECEF ΔX/ΔY/ΔZ (m): {obs['values']}\nCovariance [XX, XY, XZ, YY, YZ, ZZ] (m²): {obs['covariance']}\n"
                +'\n'.join(b.WARNINGS))
            self.plan = plan; self.import_button.setEnabled(True)
            return True
        except (ValueError, OSError) as error:
            self.review.setPlainText('Cannot import selected baseline: '+str(error)); return False

    def finish(self):
        try:
            if self.plan is None or not self.source:
                raise ValueError('Preview the selected baseline before adding it.')
            if b.read_source(self.path, processing_mode=self.source_mode.currentData() or None)['source_sha256'] != self.source['source_sha256']:
                raise ValueError('Source file changed after preview. Reopen it and review the new contents.')
            n.validate_project(self.plan)
            self.result_project = deepcopy(self.plan)
            self.accept()
            return True
        except (ValueError, OSError) as error:
            self.invalidate(); self.review.setPlainText('Baseline was not added: '+str(error)); return False
