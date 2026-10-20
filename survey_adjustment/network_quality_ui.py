# SPDX-License-Identifier: GPL-3.0-only
"""English GNSS quality review; local snapshots and a nonblocking SALSA process."""
from dataclasses import asdict
from datetime import datetime,timezone
import html
import json
from pathlib import Path

from qgis.PyQt.QtCore import QProcess,QProcessEnvironment,QTimer,QUrl
from qgis.PyQt.QtGui import QColor,QDesktopServices
from qgis.PyQt.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QGridLayout,QLabel,QLineEdit,
    QPushButton,QCheckBox,QDoubleSpinBox,QTabWidget,QTextBrowser,QTableWidget,QTableWidgetItem,
    QHeaderView,QAbstractItemView,QProgressBar,QFileDialog,QWidget,QDialogButtonBox)
from . import network as n,network_quality as q,engine


class NetworkQualityDialog(QDialog):
    def __init__(self,parent,project,host):
        super().__init__(parent)
        self.owner,self.host=parent,host
        self.project=n.loads_project(n.dumps_project(project))
        self.preview_analysis=self.result=self.folder=self.program=None
        self.process=None; self.aborted=False; self.submitted_options=None
        self.setWindowTitle('GNSS network adjustment and quality'); self.resize(1120,850)
        layout=QVBoxLayout(self)
        self.banner=QLabel(('SYNTHETIC TRAINING NETWORK — ' if project['synthetic'] else 'PRIVATE MEASUREMENTS — ')+
            'ETRS89 GNSS vectors and ellipsoidal heights. Results are separate snapshots; original point coordinates are not overwritten.')
        self.banner.setWordWrap(True); layout.addWidget(self.banner)
        self.controls=QWidget(); form=QGridLayout(self.controls)
        self.solver=QLineEdit(host.solver.text()); choose=QPushButton('Choose SALSA …'); choose.clicked.connect(self.choose_solver)
        form.addWidget(QLabel('SALSA lsasolver'),0,0); form.addWidget(self.solver,0,1,1,3); form.addWidget(choose,0,4)
        self.alpha=QDoubleSpinBox(); self.alpha.setRange(.001,.20); self.alpha.setDecimals(3); self.alpha.setValue(.05)
        self.redundancy=QDoubleSpinBox(); self.redundancy.setRange(0.,1.); self.redundancy.setDecimals(2); self.redundancy.setValue(.10)
        self.horizontal=QLineEdit(); self.horizontal.setPlaceholderText('Optional user H95 warning, metres')
        self.vertical=QLineEdit(); self.vertical.setPlaceholderText('Optional user sigma-U warning, metres')
        form.addWidget(QLabel('Significance alpha'),1,0); form.addWidget(self.alpha,1,1)
        form.addWidget(QLabel('Weak direction redundancy'),1,2); form.addWidget(self.redundancy,1,3)
        form.addWidget(self.horizontal,2,0,1,2); form.addWidget(self.vertical,2,2,1,3)
        self.note=QLineEdit(); self.note.setPlaceholderText('Document uncertainty sources, dependence, base/control errors and endpoint reductions')
        form.addWidget(QLabel('Model review'),3,0); form.addWidget(self.note,3,1,1,4)
        self.frame_review=QCheckBox('I verified the ETRS89 frame, coordinate epoch and ellipsoidal point heights')
        self.endpoint_review=QCheckBox('I verified the physical endpoints and all required antenna/point-height reductions')
        self.independence_review=QCheckBox('I reviewed independent covariance blocks; common/cross-observation errors are not added by the plugin')
        for row,check in enumerate((self.frame_review,self.endpoint_review,self.independence_review),4):
            form.addWidget(check,row,0,1,5)
        layout.addWidget(self.controls)
        help_text=QLabel('Optional uncertainty limits are your warning settings, not a delivery standard. Redundancy measures internal control, '
            'not accuracy. Standardized residuals use residual covariance and Bonferroni local tests. Zero-redundancy observations can hide errors. '
            'Fixed points are held exact by model choice. Nothing is removed automatically. Open Network map to inspect links and explicitly enlarged uncertainty graphics.')
        help_text.setWordWrap(True); layout.addWidget(help_text)
        row=QHBoxLayout(); self.preview_button=QPushButton('Preview model / network quality'); self.preview_button.clicked.connect(self.preview)
        self.run_button=QPushButton('Run SALSA and verify'); self.run_button.clicked.connect(self.start)
        self.stop_button=QPushButton('Stop'); self.stop_button.clicked.connect(self.stop); self.stop_button.setEnabled(False)
        self.open_button=QPushButton('Open local report folder'); self.open_button.clicked.connect(self.open_folder); self.open_button.setEnabled(False)
        for button in (self.preview_button,self.run_button,self.stop_button,self.open_button): row.addWidget(button)
        layout.addLayout(row)
        self.map_button=QPushButton('Network map / confidence ellipses …'); self.map_button.setEnabled(False)
        self.map_button.clicked.connect(self.open_map); layout.addWidget(self.map_button)
        self.status=QLabel('Review the model, then preview. No calculation has been run.'); self.status.setWordWrap(True); layout.addWidget(self.status)
        self.tabs=QTabWidget(); layout.addWidget(self.tabs,1)
        self.overview=QTextBrowser(); self.tabs.addTab(self.overview,'Overview / warnings')
        self.point_table=self.table(['Point','Role','Sigma E (m)','Sigma N (m)','Sigma U (m)','H95 major (m)','V95 halfwidth (m)','Review'])
        self.residual_table=self.table(['Observation / prior','Kind','dX (m)','dY (m)','dZ (m)','Max |standardized|','Redundancy %','Review'])
        self.check_table=self.table(['Held-out check','Status','dX (m)','dY (m)','dZ (m)','Reason'])
        self.tabs.addTab(self.point_table,'Point uncertainty'); self.tabs.addTab(self.residual_table,'Residuals / redundancy'); self.tabs.addTab(self.check_table,'Independent checks')
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Close); buttons.rejected.connect(self.reject); layout.addWidget(buttons)
        self.timer=QTimer(self); self.timer.setSingleShot(True); self.timer.timeout.connect(self.stop)
        for field in (self.solver,self.note,self.horizontal,self.vertical): field.textChanged.connect(self.invalidate)
        for field in (self.alpha,self.redundancy): field.valueChanged.connect(self.invalidate)
        for field in (self.frame_review,self.endpoint_review,self.independence_review): field.toggled.connect(self.invalidate)

    @staticmethod
    def table(headers):
        table=QTableWidget(0,len(headers)); table.setHorizontalHeaderLabels(headers)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(len(headers)-1,QHeaderView.ResizeMode.Stretch)
        return table

    def options(self):
        from .network_ui import number
        return q.QualityOptions(frame_epoch_reviewed=self.frame_review.isChecked(),endpoints_reviewed=self.endpoint_review.isChecked(),
            independence_reviewed=self.independence_review.isChecked(),model_note=self.note.text().strip(),alpha=self.alpha.value(),
            low_redundancy=self.redundancy.value(),horizontal_95_limit=number(self.horizontal.text()),vertical_sigma_limit=number(self.vertical.text()))

    def invalidate(self,*_):
        if self.process and self.process.state()!=QProcess.ProcessState.NotRunning: self.stop()
        self.result=self.preview_analysis=None; self.open_button.setEnabled(False)
        self.map_button.setEnabled(False)
        self.status.setText('Settings changed. Preview or run again; earlier displayed tables are not current results.')
        for table in (self.point_table,self.residual_table,self.check_table): table.setRowCount(0)
        self.overview.clear()

    def choose_solver(self):
        path,_=QFileDialog.getOpenFileName(self,'Choose separately installed SALSA lsasolver','','SALSA (lsasolver.exe lsasolver);;All files (*)')
        if path: self.solver.setText(path)

    def preview(self):
        try:
            self.preview_analysis=q.analyse(self.project,self.options())
            self.result=None; self.open_button.setEnabled(False)
            self.show_report(self.preview_analysis)
            self.map_button.setEnabled(True)
            self.status.setText('INDEPENDENT MODEL PREVIEW — SALSA has not run. Statistical results depend on the stated model; no survey/delivery approval.')
            return True
        except Exception as error:
            self.invalidate()
            self.status.setText('Cannot preview: '+str(error)); return False

    def start(self):
        if self.process and self.process.state()!=QProcess.ProcessState.NotRunning: return False
        try:
            program=engine.solver_program(self.solver.text()); options=self.options()
            if n.dumps_project(self.owner.snapshot())!=n.dumps_project(self.project):
                raise ValueError('Network changed since this workflow opened. Reopen it before calculating.')
            # Validate before asking for a destination or writing any files.
            q.analyse(self.project,options)
            parent=QFileDialog.getExistingDirectory(self,'Select a LOCAL folder for a new private network report')
            if not parent: return False
            folder=Path(parent)/('Survey_Network_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
            q.prepare_network(folder,self.project,options,program)
            self.folder,self.program,self.submitted_options=folder,program,asdict(options)
            self.result=None; self.aborted=False; self.open_button.setEnabled(False)
            self.map_button.setEnabled(False)
            self.process=QProcess(self); env=QProcessEnvironment()
            for key,value in engine.environment(program).items(): env.insert(key,value)
            self.process.setProcessEnvironment(env); self.process.setWorkingDirectory(str(folder))
            command=q.network_command(program); self.process.setProgram(command[0]); self.process.setArguments(command[1:])
            self.process.finished.connect(self.finished); self.process.errorOccurred.connect(self.process_error)
            self.controls.setEnabled(False); self.preview_button.setEnabled(False); self.run_button.setEnabled(False); self.stop_button.setEnabled(True)
            self.status.setText('Running SALSA on the saved local snapshot. Original network and observations remain unchanged.')
            self.timer.start(60000); self.process.start(); return True
        except Exception as error:
            self.status.setText('Cannot run: '+str(error)); return False

    def set_idle(self):
        self.timer.stop(); self.controls.setEnabled(True); self.preview_button.setEnabled(True); self.run_button.setEnabled(True); self.stop_button.setEnabled(False)

    def process_error(self,error):
        if error==QProcess.ProcessError.FailedToStart:
            self.set_idle(); self.result=None; self.status.setText('SALSA could not start. Check its installation and dependencies. No partial result is accepted.')

    def finished(self,exit_code,*_):
        self.set_idle()
        try:
            console=bytes(self.process.readAllStandardOutput())+b'\n'+bytes(self.process.readAllStandardError())
            (self.folder/'console.txt').write_bytes(console)
            if self.aborted: raise ValueError('Calculation stopped. Partial files must not be used.')
            if asdict(self.options())!=self.submitted_options: raise ValueError('Options changed during calculation. Run again.')
            if n.dumps_project(self.owner.snapshot())!=n.dumps_project(self.project):
                raise ValueError('Network changed during calculation. Reopen this workflow; current project has no verified result.')
            self.result=q.inspect_network_result(self.folder,exit_code,self.program)
            self.show_report(self.result); self.open_button.setEnabled(True); self.map_button.setEnabled(True)
            self.status.setText('SALSA VERIFIED against independent coordinates, covariance and statistics. Review warnings and held-out checks. Local report: '+str(self.folder))
        except Exception as error:
            self.result=None; self.open_button.setEnabled(False); self.map_button.setEnabled(False); self.status.setText('No verified result: '+str(error))

    def stop(self):
        self.aborted=True; self.result=None; self.open_button.setEnabled(False)
        self.map_button.setEnabled(False)
        if self.process and self.process.state()!=QProcess.ProcessState.NotRunning:
            self.process.kill(); self.process.waitForFinished(1500)
        self.set_idle(); self.status.setText('Calculation stopped. Partial result files are not accepted.')

    def show_report(self,report):
        global_test=report['global_test']; network=report['network']
        stage='SALSA independently verified' if report.get('engine_confirmation') else 'Independent model preview; SALSA not run'
        self.overview.setHtml('<h3>'+html.escape(stage)+'</h3><p><b>'+html.escape(global_test['status'])+'</b> — '+
            f"{network['active_vectors']} vectors; {network['coordinate_priors']} weighted coordinate priors; {global_test['degrees_of_freedom']} degrees of freedom.</p>"+
            '<p>Original point coordinates are retained. Results and quality metrics belong to this separate snapshot.</p>'+
            '<ul>'+''.join('<li>'+html.escape(w)+'</li>' for w in report['warnings'])+'</ul><pre>'+html.escape(q.report_text(report))+'</pre>')
        def populate(table,rows):
            table.setRowCount(len(rows))
            for index,(values,record,flagged) in enumerate(rows):
                for col,value in enumerate(values):
                    item=QTableWidgetItem('' if value is None else f'{value:.7g}' if isinstance(value,float) else str(value))
                    item.setToolTip(json.dumps(record,ensure_ascii=False,indent=2))
                    if flagged: item.setBackground(QColor('#fff0cc'))
                    table.setItem(index,col,item)
        populate(self.point_table,[([p['id'],p['role'],*(p['sigma_enu_m'] or [None]*3),
            p['horizontal_95_axes_m'][0] if p['horizontal_95_axes_m'] else None,p['vertical_95_halfwidth_m'],', '.join(p['flags'])],p,
            any(v in p['flags'] for v in ('WEAK_CONTROL','USER_HORIZONTAL_LIMIT','USER_VERTICAL_LIMIT','UNOBSERVED_NOT_ADJUSTED'))) for p in report['points']])
        populate(self.residual_table,[([r['id'],r['kind'],*r['residual_xyz_m'],
            max((abs(v) for v in r['standardized_xyz'] if v is not None),default=None),None,', '.join(r['flags'])],r,bool(r['flags'])) for r in report['observations']])
        for index,r in enumerate(report['observations']):
            bar=QProgressBar(); bar.setRange(0,100); bar.setValue(round(100*r['redundancy_fraction']))
            bar.setToolTip(f"Block redundancy fraction: {r['redundancy_fraction']:.6g}; weakest direction: {r['minimum_direction_redundancy']:.6g}. Internal control, not accuracy.")
            if r['flags']: bar.setStyleSheet('QProgressBar::chunk {background:#d99a25;}')
            self.residual_table.setCellWidget(index,6,bar)
        populate(self.check_table,[([r['id'],r['status'],*(r['residual_xyz_m'] or [None]*3),r['reason']],r,r['status']!='WITHIN_MODEL_TEST') for r in report['checks']])

    def open_folder(self):
        if self.result and self.folder: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.folder)))

    def open_map(self):
        try:
            if self.process and self.process.state()!=QProcess.ProcessState.NotRunning:
                raise ValueError('Wait for or stop the calculation before opening a snapshot map.')
            if n.dumps_project(self.owner.snapshot())!=n.dumps_project(self.project):
                raise ValueError('Network changed. Reopen the quality workflow before viewing a map.')
            report=self.result or self.preview_analysis
            if report is None: raise ValueError('Preview or verify a calculation first.')
            from .network_map_ui import NetworkMapDialog
            dialog=NetworkMapDialog(self,self.project,report,self.host,self.solver.text())
            dialog.exec(); return True
        except Exception as error:
            self.status.setText('Cannot open map: '+str(error)); return False

    def reject(self):
        if self.process and self.process.state()!=QProcess.ProcessState.NotRunning:
            self.stop(); self.process.waitForFinished(1500)
        super().reject()

    def closeEvent(self,event):
        if self.process and self.process.state()!=QProcess.ProcessState.NotRunning:
            self.stop(); self.process.waitForFinished(1500)
        event.accept()
