# SPDX-License-Identifier: GPL-3.0-only
"""Network editor and an explicit, snapshot-based GNSS quality workflow."""
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path

from qgis.PyQt.QtCore import Qt, QSettings
from qgis.PyQt.QtWidgets import (QWidget, QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QGridLayout, QLabel, QLineEdit, QComboBox, QCheckBox, QPushButton, QDialogButtonBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView, QTabWidget,
    QTextBrowser, QFileDialog, QMessageBox, QInputDialog, QScrollArea, QSizePolicy)
from . import network as model


def combo(items, value=''):
    box = QComboBox()
    for key, label in items.items():
        box.addItem(label, key)
    box.setCurrentIndex(max(0, box.findData(value)))
    return box


def number(text):
    if not text.strip():
        return None
    try:
        value = float(text.strip().replace(',', '.'))
        if not math.isfinite(value):
            raise ValueError()
        return value
    except ValueError:
        raise ValueError('Enter finite numbers, using a decimal point or decimal comma without thousands separators.') from None


def numeric_text(value):
    # Python's shortest round-trip representation keeps the exact stored float
    # without displaying binary artefacts such as 0.029999999999999999.
    return '' if value is None else str(value)


class RecordEditor(QDialog):
    def __init__(self, parent, collection, project, record=None):
        super().__init__(parent)
        self.collection, self.project = collection, project
        self.is_new = record is None
        self.original = deepcopy(record or {'points': model.point, 'sessions': model.session, 'observations': model.observation}[collection]())
        self.record = None
        self.setWindowTitle(('Edit ' if record else 'Add ')+collection.rstrip('s'))
        self.resize(680, 640)
        layout = QVBoxLayout(self)
        scroll=QScrollArea()
        scroll.setWidgetResizable(True)
        content=QWidget()
        content_layout=QVBoxLayout(content)
        scroll.setWidget(content)
        layout.addWidget(scroll)
        self.help = QLabel()
        self.help.setWordWrap(True)
        content_layout.addWidget(self.help)
        self.form = QFormLayout()
        content_layout.addLayout(self.form)
        content_layout.addStretch()
        self.identifier = QLineEdit(self.original['id'])
        self.identifier.setPlaceholderText('Unique physical point / session / observation ID')
        self.form.addRow('ID', self.identifier)
        if collection == 'points':
            self.build_point()
        elif collection == 'sessions':
            self.build_session()
        else:
            self.build_observation()
        self.notes = QLineEdit(self.original['notes'])
        self.notes.setPlaceholderText('Source, assumptions, fixed-point justification or exclusion reason')
        self.form.addRow('Notes', self.notes)
        from .baselines import imported_snapshot
        self.imported = collection=='observations' and imported_snapshot(project, self.original['id']) is not None
        if self.imported:
            for field in [self.identifier,self.kind,self.session,self.origin,self.target,self.frame,*self.values,*self.sigmas,*self.correlations,*self.heights]:
                field.setEnabled(False)
            self.help.setText('Imported measured baseline: original vector, covariance, endpoints, ID and acquisition session are locked. '
                              'Notes, intended use and enabled status may be reviewed. Original source row and conversion remain in the import audit.')
        self.error = QLabel()
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.submit)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def numeric_row(self, label, values, placeholders):
        row = QHBoxLayout()
        fields = []
        for value, placeholder in zip(values, placeholders):
            field = QLineEdit(numeric_text(value))
            field.setPlaceholderText(placeholder)
            row.addWidget(field)
            fields.append(field)
        self.form.addRow(label, row)
        return fields

    def build_point(self):
        self.role = combo(model.ROLES, self.original['role'])
        self.form.addRow('Point role', self.role)
        self.coordinates = self.numeric_row('Point coordinates (m)', self.original['coordinates'] or [None]*3, ['Easting', 'Northing', 'Point height Z'])
        self.sigmas = self.numeric_row('Coordinate 1σ (m)', self.original['sigma'] or [None]*3, ['σE', 'σN', 'σZ'])
        self.role.currentIndexChanged.connect(self.point_help)
        self.point_help()

    def point_help(self):
        role = self.role.currentData()
        explanations = {
            'UNKNOWN': 'Coordinates can be estimated by the GNSS network workflow. Leave all three blank if no approximate position is documented; documented anchor connections may provide the initial estimate.',
            'CONSTRAINED': 'Known coordinates with positive standard deviations in the project E/N/Z axes. These are weighted constraints, not error-free anchors.',
            'FIXED': 'Held exact in the selected model. Enter all coordinates and explain the reason in Notes. This mathematical choice is not a field-quality guarantee.',
            'CHECK': 'Independent check only: coordinates and observations marked Check are held outside adjustment. Coordinates are required; uncertainty is needed for a statistical check.'}
        self.help.setText(explanations[role]+' All point coordinates share the project CRS and height datum. No transformation or antenna correction is applied. Referenced IDs cannot be renamed without updating dependent records.')
        for field in self.sigmas:
            field.setEnabled(role in ('CONSTRAINED', 'CHECK'))

    def build_session(self):
        self.help.setText('Group observations by acquisition session. Record instrument/receiver, times and corrections. A different session ID alone does not establish independence. Times require Z or a UTC offset.')
        self.session_fields = {}
        for key, label in [('start','Start time'), ('end','End time'), ('instrument','Instrument / receiver'), ('corrections','Corrections / base / reference frame')]:
            field = QLineEdit(self.original[key])
            if key in ('start','end'):
                field.setPlaceholderText('2026-10-18T08:00:00Z; leave blank if undocumented')
            self.form.addRow(label, field)
            self.session_fields[key] = field

    def build_observation(self):
        self.help.setText('Enter measured values, never invented differences between RTK point coordinates. The GNSS quality workflow accepts documented ETRS89 point-to-point vectors; mixed survey observations are stored for later profiles. Vector components are ECEF metres; covariance is in m². Instrument/target heights are recorded, not applied. Document all reductions in Notes.')
        self.kind = combo(model.KINDS, self.original['kind'])
        self.form.addRow('Observation type', self.kind)
        self.session = combo({s['id']:s['id'] for s in self.project['sessions']}, self.original['session'])
        self.form.addRow('Session', self.session)
        points = {p['id']:p['id']+' - '+model.ROLES[p['role']] for p in self.project['points']}
        self.origin, self.target = combo(points, self.original['from_point']), combo(points, self.original['to_point'])
        self.form.addRow('From point', self.origin)
        self.form.addRow('To point', self.target)
        self.use = combo(model.USES, self.original['use'])
        self.form.addRow('Intended use', self.use)
        self.enabled = QCheckBox('Enabled (disabling requires a reason in Notes)')
        self.enabled.setChecked(self.original['enabled'])
        self.form.addRow(self.enabled)
        vector = self.original['kind'] == 'GNSS_VECTOR'
        values = [None]*3 if self.is_new else self.original['values']+[None]*(3-len(self.original['values']))
        self.values = self.numeric_row('Value(s), metres', values, ['Distance / Δheight / ΔX', 'ΔY (vector)', 'ΔZ (vector)'])
        cov = self.original['covariance']
        sigmas = [None]*3 if self.is_new else [math.sqrt(cov[i]) for i in (0,3,5)] if vector else [math.sqrt(cov[0]), None, None]
        self.sigmas = self.numeric_row('Observation 1σ (m)', sigmas, ['σ value / σX', 'σY (vector)', 'σZ (vector)'])
        correlations = [None]*3 if self.is_new else [cov[1]/sigmas[0]/sigmas[1], cov[2]/sigmas[0]/sigmas[2], cov[4]/sigmas[1]/sigmas[2]] if vector else [0,0,0]
        self.correlations = self.numeric_row('Vector correlations', correlations, ['ρXY', 'ρXZ', 'ρYZ'])
        self.frame = combo({'':'Select documented ECEF frame', 'EPSG:4936':'ETRS89 ECEF XYZ (EPSG:4936)', 'EPSG:4978':'WGS84 ECEF XYZ (EPSG:4978)'}, self.original['frame'])
        self.form.addRow('Vector reference frame', self.frame)
        self.heights = self.numeric_row('Instrument / target (m)', [self.original['instrument_height'], self.original['target_height']], ['Instrument height (or blank)', 'Target height (or blank)'])
        self.kind.currentIndexChanged.connect(self.observation_controls)
        self.observation_controls()

    def observation_controls(self):
        vector = self.kind.currentData() == 'GNSS_VECTOR'
        for field in self.values[1:]+self.sigmas[1:]+self.correlations+[self.frame]:
            field.setEnabled(vector)

    def submit(self):
        self.error.clear()
        try:
            r = deepcopy(self.original)
            r.update(id=self.identifier.text().strip(), notes=self.notes.text())
            if self.collection == 'points':
                coords = [number(f.text()) for f in self.coordinates]
                sigma = [number(f.text()) for f in self.sigmas] if self.role.currentData() in ('CONSTRAINED','CHECK') else [None]*3
                r.update(role=self.role.currentData(), coordinates=None if all(v is None for v in coords) else coords,
                         sigma=None if all(v is None for v in sigma) else sigma)
            elif self.collection == 'sessions':
                r.update({key:field.text().strip() for key, field in self.session_fields.items()})
            else:
                kind = self.kind.currentData()
                r.update(use=self.use.currentData(), enabled=self.enabled.isChecked())
                if not self.imported:
                    sigmas = [number(f.text()) for f in self.sigmas[:3 if kind=='GNSS_VECTOR' else 1]]
                    if any(v is None or v <= 0 for v in sigmas):
                        raise ValueError('Enter positive documented observation standard deviations in metres.')
                    covariance = model.covariance_from_sigmas(sigmas, [number(f.text()) for f in self.correlations]) if kind=='GNSS_VECTOR' else [sigmas[0]**2]
                    r.update(kind=kind, session=self.session.currentData(), from_point=self.origin.currentData(), to_point=self.target.currentData(),
                        values=[number(f.text()) for f in self.values[:3 if kind=='GNSS_VECTOR' else 1]],
                        covariance=covariance, frame=self.frame.currentData() if kind=='GNSS_VECTOR' else '',
                        instrument_height=number(self.heights[0].text()), target_height=number(self.heights[1].text()))
            # Replacing the original ID also checks dependent records and duplicate IDs.
            index = next((i for i, v in enumerate(self.project[self.collection]) if v['id']==self.original['id']), None)
            model.replace_record(self.project, self.collection, r, index)
            self.record = r
            self.accept()
        except (ValueError, TypeError, OverflowError) as error:
            self.error.setText('Cannot save record: '+str(error))


class NetworkPanel(QWidget):
    RECOVERY = 'survey_adjustment/network_draft_recovery_v1'

    def __init__(self, host):
        super().__init__(host)
        self.host, self.settings = host, QSettings()
        self.project, self.path, self.disk_digest, self.dirty = model.new_project(), None, None, False
        self.refreshing = False
        layout = QVBoxLayout(self)
        info = QLabel('Network project: points, sessions and observations. Check structure, or explicitly run GNSS adjustment / quality on a separate snapshot. Independent checks remain outside the fit. Save this network separately from the QGIS map project.')
        info.setWordWrap(True)
        layout.addWidget(info)
        row = QHBoxLayout()
        for label, callback in [('New',self.new), ('Open …',self.open), ('Save',self.save), ('Save as …',self.save_as), ('Load synthetic network',self.load_demo)]:
            button = QPushButton(label)
            button.clicked.connect(callback)
            row.addWidget(button)
        self.recover_button = QPushButton('Recover unsaved draft')
        self.recover_button.clicked.connect(self.recover)
        row.addWidget(self.recover_button)
        layout.addLayout(row)
        grid = QGridLayout()
        self.name, self.epoch, self.notes = QLineEdit(), QLineEdit(), QLineEdit()
        self.crs = combo({'':'Select point CRS', **model.CRS})
        self.height = combo({'':'Select height datum', **{v:v for v in model.HEIGHTS}})
        self.epoch.setPlaceholderText('YYYY-MM-DD, or blank; no transformation')
        self.notes.setPlaceholderText('Project purpose, sources and assumptions')
        grid.addWidget(QLabel('Project name'),0,0); grid.addWidget(self.name,0,1,1,3)
        grid.addWidget(QLabel('Point CRS'),1,0); grid.addWidget(self.crs,1,1)
        grid.addWidget(QLabel('Height datum'),1,2); grid.addWidget(self.height,1,3)
        grid.addWidget(QLabel('Coordinate epoch'),2,0); grid.addWidget(self.epoch,2,1)
        grid.addWidget(QLabel('Notes'),2,2); grid.addWidget(self.notes,2,3)
        layout.addLayout(grid)
        self.file_info = QLabel()
        self.file_info.setWordWrap(True)
        layout.addWidget(self.file_info)
        import_row = QHBoxLayout()
        self.import_button = QPushButton('Import GNSS file …')
        self.import_button.clicked.connect(self.import_gnss); import_row.addWidget(self.import_button)
        audit_button = QPushButton('Show import audit'); audit_button.clicked.connect(self.show_import_audit); import_row.addWidget(audit_button)
        hint = QLabel('Processed POS / ΔXYZ CSV or raw RINEX. Each file uses its existing typed review; raw data never become baselines automatically.')
        hint.setWordWrap(True); import_row.addWidget(hint,1); layout.addLayout(import_row)
        self.tabs = QTabWidget()
        # A hidden raw-page minimum must not enlarge the whole plugin on start.
        # Raw views gain the tool area when that page is selected.
        self.tabs.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Ignored)
        self.tabs.setMinimumHeight(180)
        self.tables = {}
        self.headers = {
            'points':['ID','Role','E (m)','N (m)','Z (m)','σE (m)','σN (m)','σZ (m)','Notes'],
            'sessions':['ID','Start','End','Instrument / receiver','Corrections / base','Notes'],
            'observations':['ID','Session','Type','From','To','Use','Enabled','Value(s) in m','Notes']}
        for collection in self.headers:
            page = QWidget(); box = QVBoxLayout(page)
            table = QTableWidget(0, len(self.headers[collection]))
            table.setHorizontalHeaderLabels(self.headers[collection])
            table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
            table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
            table.horizontalHeader().setSectionResizeMode(len(self.headers[collection])-1,QHeaderView.ResizeMode.Stretch)
            table.cellDoubleClicked.connect(lambda _r,_c,key=collection:self.edit_record(key))
            self.tables[collection] = table; box.addWidget(table)
            actions = QHBoxLayout()
            for label, callback in [('Add …',lambda _=False,key=collection:self.edit_record(key,True)),
                                    ('Edit selected …',lambda _=False,key=collection:self.edit_record(key)),
                                    ('Remove selected',lambda _=False,key=collection:self.delete_record(key))]:
                button = QPushButton(label); button.clicked.connect(callback); actions.addWidget(button)
            box.addLayout(actions); self.tabs.addTab(page,collection.title())
        from .rinex_ui import RawPanel
        self.raw_panel=RawPanel(self); self.tabs.addTab(self.raw_panel,'Raw GNSS / RINEX')
        layout.addWidget(self.tabs,1)
        self.network_tools=QWidget(); tools=QVBoxLayout(self.network_tools); tools.setContentsMargins(0,0,0,0)
        button = QPushButton('Check project structure (does not calculate)')
        button.clicked.connect(self.check)
        tools.addWidget(button)
        quality_row=QHBoxLayout()
        quality_example=QPushButton('Load GNSS quality example'); quality_example.clicked.connect(self.load_quality_example)
        quality_run=QPushButton('GNSS adjustment / quality …'); quality_run.clicked.connect(self.network_quality)
        quality_row.addWidget(quality_example); quality_row.addWidget(quality_run); tools.addLayout(quality_row)
        mixed=QPushButton('Mixed survey / total station and levelling …')
        mixed.clicked.connect(self.mixed_survey); tools.addWidget(mixed)
        delivery=QPushButton('Norwegian reference review / delivery package …')
        delivery.clicked.connect(self.delivery); tools.addWidget(delivery)
        self.summary = QTextBrowser()
        self.summary.setMaximumHeight(125)
        tools.addWidget(self.summary); layout.addWidget(self.network_tools)
        self.tabs.currentChanged.connect(lambda _:self.network_tools.setVisible(self.tabs.currentWidget()!=self.raw_panel))
        for field in (self.name,self.epoch,self.notes):
            field.editingFinished.connect(self.changed)
        for field in (self.crs,self.height):
            field.currentIndexChanged.connect(self.changed)
        self.refresh()

    def form_values(self):
        return dict(name=self.name.text(), crs=self.crs.currentData(), height_datum=self.height.currentData(), epoch=self.epoch.text(), notes=self.notes.text())

    def snapshot(self):
        value = deepcopy(self.project)
        value.update(self.form_values())
        model.validate_project(value)
        return value

    def has_changes(self):
        return self.dirty or any(self.project[k]!=v for k,v in self.form_values().items())

    def changed(self):
        if not self.refreshing:
            self.raw_panel.invalidate()
            self.dirty = True
            self.summary.setPlainText('Project edited. Check the structure again; this is not an adjusted network.')
            self.stash_recovery()
            self.update_file_info()

    def update_file_info(self):
        self.file_info.setText(('SYNTHETIC EXAMPLE - ' if self.project['synthetic'] else '')+
            (str(self.path) if self.path else 'Network project has not been saved')+('  * unsaved changes' if self.has_changes() else ''))
        self.recover_button.setEnabled(self.settings.contains(self.RECOVERY))

    def refresh(self):
        self.refreshing = True
        for key, field in [('name',self.name),('epoch',self.epoch),('notes',self.notes)]:
            field.setText(self.project[key])
        for key, field in [('crs',self.crs),('height_datum',self.height)]:
            field.setCurrentIndex(max(0,field.findData(self.project[key])))
        for key, table in self.tables.items():
            table.setRowCount(len(self.project[key]))
            for row, r in enumerate(self.project[key]):
                if key=='points':
                    values=[r['id'],model.ROLES[r['role']],*(r['coordinates'] or [None]*3),*(r['sigma'] or [None]*3),r['notes']]
                elif key=='sessions':
                    values=[r[k] for k in ('id','start','end','instrument','corrections','notes')]
                else:
                    values=[r['id'],r['session'],model.KINDS[r['kind']],r['from_point'],r['to_point'],model.USES[r['use']], 'Yes' if r['enabled'] else 'No',
                            ', '.join(numeric_text(v) for v in r['values']),r['notes']]
                for col,v in enumerate(values):
                    item=QTableWidgetItem('' if v is None else format(v,'.10g') if isinstance(v,float) else str(v))
                    item.setToolTip(json.dumps(r,ensure_ascii=False,indent=2))
                    table.setItem(row,col,item)
        self.raw_panel.refresh()
        self.refreshing = False
        self.update_file_info()

    def stash_recovery(self):
        if not self.has_changes():
            return True
        try:
            # Keep the last valid records plus unfinished metadata text; never write a source file automatically.
            drafts=self.recovery_drafts()
            drafts[self.project['project_id']]=dict(project=self.project,form=self.form_values())
            self.settings.setValue(self.RECOVERY,json.dumps(dict(drafts=drafts),ensure_ascii=False,allow_nan=False))
            self.settings.sync()
            if self.settings.status()!=QSettings.Status.NoError:
                raise ValueError('QGIS settings could not be written.')
            self.recover_button.setEnabled(True)
            return True
        except (ValueError,TypeError,OSError) as error:
            self.summary.setPlainText('Recovery draft could not be stored. Save the network to a file before closing or disabling the plugin.\n'+str(error))
            return False

    def recovery_drafts(self):
        payload=str(self.settings.value(self.RECOVERY,''))
        if not payload:
            return {}
        wrapper=json.loads(payload)
        if 'project' in wrapper:  # Accept a draft from the first schema-1 development build.
            return {wrapper['project']['project_id']:wrapper}
        if not isinstance(wrapper.get('drafts'),dict):
            raise ValueError('Invalid recovery store. Existing recovery data was not overwritten.')
        return wrapper['drafts']

    def forget_recovery(self):
        try:
            drafts=self.recovery_drafts()
        except (ValueError,TypeError):
            return  # Preserve an unreadable recovery store for manual recovery.
        drafts.pop(self.project['project_id'],None)
        if drafts:
            self.settings.setValue(self.RECOVERY,json.dumps(dict(drafts=drafts),ensure_ascii=False,allow_nan=False))
        else:
            self.settings.remove(self.RECOVERY)
        self.settings.sync()

    def error(self,error):
        QMessageBox.warning(self,'Network project',str(error))

    def maybe_leave(self):
        if not self.has_changes():
            return True
        answer=QMessageBox.question(self,'Unsaved network project','Save changes to the separate network project before continuing?',
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if answer==QMessageBox.StandardButton.Save:
            return self.save()
        if answer==QMessageBox.StandardButton.Discard:
            self.forget_recovery()
            return True
        return False

    def replace_project(self,project,path=None,dirty=False):
        self.project, self.path, self.dirty = model.upgrade_project(project), Path(path) if path else None, dirty
        self.disk_digest=hashlib.sha256(self.path.read_bytes()).hexdigest() if self.path else None
        self.refresh()
        self.summary.setPlainText('Network inventory opened. No network adjustment has been performed.')
        if dirty:
            self.stash_recovery()

    def new(self):
        if self.maybe_leave():
            self.replace_project(model.new_project())

    def load_demo(self):
        if self.maybe_leave():
            self.replace_project(model.synthetic_project(),dirty=True)
            self.check()

    def open(self):
        path,_=QFileDialog.getOpenFileName(self,'Open network project','','Survey network (*.sanet.json *.json)')
        if not path:
            return False
        try:
            project=model.load_project(path)
            if not self.maybe_leave():
                return False
            self.replace_project(project,path)
            return True
        except (OSError,ValueError) as error:
            self.error(error)
            return False

    def write(self,path):
        try:
            path=Path(path)
            project=self.snapshot()
            if self.path and path.resolve()==self.path.resolve() and (not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest()!=self.disk_digest):
                raise ValueError('The file was changed or removed outside this editor. Open it again or save under a different name.')
            model.save_project(path,project)
            self.replace_project(project,path)
            self.forget_recovery()
            self.update_file_info()
            self.summary.setPlainText('Network inventory saved. No network adjustment has been performed.\n'+'\n'.join(model.validate_project(project)))
            return True
        except (OSError,ValueError) as error:
            self.error(error)
            return False

    def save(self):
        return self.write(self.path) if self.path else self.save_as()

    def save_as(self):
        path,_=QFileDialog.getSaveFileName(self,'Save network project',str(self.path or 'network.sanet.json'),'Survey network (*.sanet.json)')
        if not path:
            return False
        if not path.lower().endswith('.json'):
            path += '.sanet.json'
        return self.write(path)

    def recover(self):
        try:
            drafts=self.recovery_drafts()
            if not drafts:
                return False
            keys=list(drafts)
            if len(keys)>1:
                labels=[str(drafts[key]['form']['name'])+' ['+key+']' for key in keys]
                label,ok=QInputDialog.getItem(self,'Recover network draft','Choose a draft (other drafts are kept)',labels,0,False)
                if not ok:
                    return False
                key=keys[labels.index(label)]
            else:
                key=keys[0]
            wrapper=deepcopy(drafts[key])
            wrapper['project']=model.upgrade_project(wrapper['project'])
            model.validate_project(wrapper['project'])
            # Read recovery before maybe_leave can clear/replace it.
            if not self.maybe_leave():
                return False
            self.replace_project(wrapper['project'],dirty=True)
            self.refreshing=True
            for key,field in [('name',self.name),('epoch',self.epoch),('notes',self.notes)]:
                field.setText(wrapper['form'][key])
            for key,field in [('crs',self.crs),('height_datum',self.height)]:
                field.setCurrentIndex(max(0,field.findData(wrapper['form'][key])))
            self.refreshing=False
            self.changed()
            return True
        except (ValueError,KeyError,TypeError) as error:
            self.error('Cannot recover network draft: '+str(error))
            return False

    def import_gnss(self):
        path,_=QFileDialog.getOpenFileName(self,'Import GNSS source into this network','','GNSS sources (*.pos *.csv *.txt *.tsv *.rnx *.obs *.nav *.gz *.*o *.*n *.*g);;All files (*)')
        if not path: return False
        try:
            with Path(path).open('rb') as handle: head=handle.read(160)
            suffix=Path(path).suffix.lower()
            raw=(b'RINEX VERSION / TYPE' in head or b'CRINEX' in head or head.startswith(b'\x1f\x8b') or suffix in ('.rnx','.obs','.nav','.rinex'))
            if raw:
                self.tabs.setCurrentWidget(self.raw_panel)
                return self.raw_panel.open_source(path)
            return self.import_baselines(path)
        except (ValueError,OSError) as error: self.error(error); return False

    def import_baselines(self,path=None,line=None):
        try:
            from .baseline_ui import BaselineImportDialog
            project=self.snapshot()
            if len(project['points'])<2 or not project['sessions']:
                raise ValueError('Add the physical base/rover points and a documented UTC acquisition session before importing a baseline.')
            dialog=BaselineImportDialog(self,project)
            if path: dialog.load(path)
            if line is not None and dialog.source:
                for index,row in enumerate(dialog.source['rows']):
                    if row['line']==line:dialog.table.selectRow(index);break
            if dialog.exec()==QDialog.DialogCode.Accepted:
                if self.snapshot()!=project:
                    raise ValueError('Network changed during import review. Reopen the import dialog.')
                self.project=deepcopy(dialog.result_project); self.dirty=True; self.refresh(); self.changed()
                self.summary.setPlainText('One processed measured baseline added. Save the network project explicitly. No network adjustment has been performed.')
                return True
        except (ValueError,OSError) as error:
            self.error(error)
        return False

    def load_quality_example(self):
        try:
            if self.maybe_leave():
                self.replace_project(model.load_project(Path(__file__).parent/'examples/gnss_quality.sanet.json'),dirty=True)
                self.check()
                return True
        except Exception as error:
            self.error('Cannot load quality example: '+str(error))
        return False

    def network_quality(self):
        try:
            from .network_quality_ui import NetworkQualityDialog
            from qgis.PyQt.QtCore import QProcess
            if self.host.process and self.host.process.state()!=QProcess.ProcessState.NotRunning:
                raise ValueError('Wait for or stop the current SALSA calculation first.')
            dialog=NetworkQualityDialog(self,self.snapshot(),self.host)
            dialog.exec()
        except Exception as error:
            self.error('Cannot open GNSS network quality: '+str(error))

    def delivery(self):
        from .delivery_ui import DeliveryDialog
        dialog=DeliveryDialog(self,self.host)
        dialog.exec()
        dialog.deleteLater()

    def mixed_survey(self):
        try:
            from .mixed_survey_ui import MixedSurveyDialog
            from qgis.PyQt.QtCore import QProcess
            if self.host.process and self.host.process.state()!=QProcess.ProcessState.NotRunning:
                raise ValueError('Wait for or stop the current SALSA calculation first.')
            dialog=MixedSurveyDialog(self,self.snapshot(),self.host)
            dialog.exec()
        except Exception as error:
            self.error('Cannot open mixed survey: '+str(error))

    def show_import_audit(self):
        try:
            project=self.snapshot()
            dialog=QDialog(self); dialog.setWindowTitle('Measured baseline import audit — local project data'); dialog.resize(900,650)
            layout=QVBoxLayout(dialog); info=QLabel('Original selected source rows, SHA256, frame/epoch, processing-base position and full covariance conversion. '
                'This local audit contains measurement data. It is not a quality approval, digital signature or a file for GitHub.')
            info.setWordWrap(True); layout.addWidget(info)
            text=QTextBrowser(); text.setPlainText(json.dumps(project['imports'],ensure_ascii=False,indent=2)); layout.addWidget(text)
            buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Close); buttons.rejected.connect(dialog.reject); layout.addWidget(buttons); dialog.exec()
        except ValueError as error:
            self.error(error)

    def edit_record(self,collection,add=False):
        try:
            project=self.snapshot()
            index=None if add else self.tables[collection].currentRow()
            if not add and index<0:
                raise ValueError('Select a record to edit.')
            if collection=='observations' and (len(project['points'])<2 or not project['sessions']):
                raise ValueError('Add at least two points and one session before entering an observation.')
            editor=RecordEditor(self,collection,project,None if add else project[collection][index])
            if editor.exec()==QDialog.DialogCode.Accepted:
                self.project=model.replace_record(project,collection,editor.record,index)
                self.dirty=True; self.refresh(); self.changed()
                return True
        except (ValueError,OSError) as error:
            self.error(error)
        return False

    def delete_record(self,collection):
        try:
            index=self.tables[collection].currentRow()
            if index<0:
                raise ValueError('Select a record to remove.')
            project=model.remove_record(self.snapshot(),collection,index)
            if QMessageBox.question(self,'Remove record','Remove this record from the network inventory?',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,QMessageBox.StandardButton.No)!=QMessageBox.StandardButton.Yes:
                return False
            self.project=project; self.dirty=True; self.refresh(); self.changed()
            return True
        except ValueError as error:
            self.error('Cannot remove record. Referenced points/sessions must be kept until dependent observations are changed or removed.\n'+str(error))
            return False

    def check(self):
        try:
            project=self.snapshot()
            warnings=model.validate_project(project)
            active=[o for o in project['observations'] if o['enabled']]
            self.summary.setPlainText('STRUCTURE VALID - inventory only, not adjusted or delivery approved.\n'+
                f"{len(project['points'])} points; {len(project['sessions'])} sessions; {len(project['observations'])} observations. "+
                f"Active intended adjustment: {sum(o['use']=='ADJUSTMENT' for o in active)}; held-out checks: {sum(o['use']=='CHECK' for o in active)}.\n"+
                '\n'.join(warnings))
            return True
        except ValueError as error:
            self.summary.setPlainText('STRUCTURE ERROR - '+str(error))
            return False
