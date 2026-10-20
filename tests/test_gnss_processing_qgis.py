# SPDX-License-Identifier: GPL-3.0-only
from pathlib import Path
import tempfile,time,unittest
from unittest.mock import patch
from test_qgis import app,ROOT,Iface
from test_gnss_processing import ENGINE
from qgis.PyQt.QtCore import QSettings,QProcess
from qgis.PyQt.QtWidgets import QFileDialog,QDialog
from survey_adjustment.plugin import SurveyDialog
from survey_adjustment.gnss_processing_ui import ProcessingDialog
from survey_adjustment.baseline_ui import BaselineImportDialog
from survey_adjustment import gnss_processing as p,gnss_demo as d,network as n


@unittest.skipUnless(ENGINE.is_file(),'Reviewed external engine intentionally not bundled')
class ProcessingQgisTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT/'test_output');self.root=Path(self.tmp.name)
        settings=QSettings(str(self.root/'settings.ini'),QSettings.Format.IniFormat)
        self.settings=[]
        for module in ('survey_adjustment.network_ui.QSettings','survey_adjustment.gnss_processing_ui.QSettings'):
            task=patch(module,return_value=settings);mock=task.start();mock.Status=QSettings.Status;self.settings.append(task)
        self.host=SurveyDialog(Iface());self.owner=self.host.network_panel
        self.owner.replace_project(d.create(self.root/'example'))
        self.dialog=ProcessingDialog(self.owner);self.fill()
    def tearDown(self):
        self.dialog.reject();self.dialog.deleteLater();self.host._unloading=True;self.host.close();self.host.deleteLater();app.processEvents()
        for task in self.settings:task.stop()
        self.tmp.cleanup()
    def fill(self):
        dialog=self.dialog;dialog.executable.setText(str(ENGINE));dialog.base_xyz.setText(' '.join(map(str,d.BASE)))
        dialog.frame.setCurrentIndex(dialog.frame.findData('EPSG:4978'));dialog.atmosphere.setCurrentIndex(dialog.atmosphere.findData('ideal-synthetic'))
        dialog.antenna_note.setText('SYNTHETIC zero offsets and electrical endpoints.');dialog.model_note.setText('SYNTHETIC fixed base and ideal signals; no field accuracy.')
        for field in (dialog.base_review,dialog.antenna_review,dialog.model_review,dialog.license_review):field.setChecked(True)
    def wait(self):
        deadline=time.monotonic()+20
        while (self.dialog.preparing or (self.dialog.process and self.dialog.process.state()!=QProcess.ProcessState.NotRunning)) and time.monotonic()<deadline:
            app.processEvents();time.sleep(.01)
        for _ in range(5):app.processEvents()
    def start(self):
        with patch.object(QFileDialog,'getExistingDirectory',return_value=str(self.root)):
            self.assertTrue(self.dialog.start(),self.dialog.status.text())
        self.wait();self.assertIsNotNone(self.dialog.result,self.dialog.status.text())

    def test_native_qprocess_static_report_map_pointz_and_existing_handoff(self):
        self.start();dialog=self.dialog;self.assertEqual(dialog.table.rowCount(),121)
        layer=dialog.layers['points'];self.assertTrue(layer.readOnly());first=next(layer.getFeatures())
        self.assertTrue(first.geometry().constGet().is3D());self.assertGreater(first.geometry().constGet().z(),80.)
        dialog.select_record('processing_epoch',str(dialog.source['rows'][-1]['line']));self.assertEqual(dialog.table.currentRow(),120)
        self.assertIn(dialog.source['rows'][-1]['time'],dialog.status.text())
        def execute(review):
            self.assertTrue(review.occupation.isVisibleTo(review));self.assertEqual(review.table.currentRow(),120)
            for check in (review.synthetic,review.relative,review.endpoints,review.covariance,review.occupation):check.setChecked(True)
            review.occupation_note.setText('SYNTHETIC stationary ten minute occupation; selected final filter state, not average.')
            self.assertTrue(review.preview(),review.review.toPlainText());self.assertTrue(review.finish());return QDialog.DialogCode.Accepted
        with patch.object(BaselineImportDialog,'exec',execute):self.assertTrue(dialog.review_baseline())
        self.assertEqual(len(self.owner.project['observations']),1)
        self.assertIn('processing',self.owner.project['imports'][0]);path=self.root/'saved.sanet.json'
        self.owner.write(path);self.assertEqual(n.load_project(path),self.owner.snapshot())
        dialog.show();app.processEvents();self.assertTrue(dialog.grab().save(str(ROOT/'test_output/qgis_ppk0100.png')))

    def test_qprocess_ppk_result_and_settings_change_invalidate_map(self):
        self.dialog.mode.setCurrentIndex(self.dialog.mode.findData('kinematic'));self.start()
        self.assertEqual(self.dialog.result['mode'],'kinematic');self.assertFalse(self.owner.project['observations'])
        self.dialog.model_note.setText('Changed review');self.assertIsNone(self.dialog.result)
        self.assertFalse(self.dialog.layers);self.assertTrue(self.dialog.canvas.isHidden());self.assertFalse(self.dialog.handoff.isEnabled())

    def test_cancelled_or_closed_job_is_marked_aborted(self):
        with patch.object(QFileDialog,'getExistingDirectory',return_value=str(self.root)):self.assertTrue(self.dialog.start())
        deadline=time.monotonic()+10
        while self.dialog.preparing and time.monotonic()<deadline:app.processEvents();time.sleep(.01)
        self.dialog.reject();self.wait();self.assertIsNone(self.dialog.result)
        self.assertEqual(p.load_manifest(self.dialog.folder)['state'],'ABORTED')
        self.assertFalse(self.owner.project['observations'])

    def test_blank_reviews_cannot_start_and_new_example_requires_explicit_reviews(self):
        self.dialog.base_review.setChecked(False)
        with patch.object(QFileDialog,'getExistingDirectory') as picker:
            self.assertFalse(self.dialog.start());self.assertFalse(picker.called)
        with patch.object(QFileDialog,'getExistingDirectory',return_value=str(self.root)):
            self.assertTrue(self.dialog.example(),self.dialog.status.text())
        self.assertFalse(self.dialog.base_review.isChecked());self.assertEqual(len(self.owner.project['raw_sources']),3)
        self.assertIn(self.owner.project['epoch'],self.dialog.reference_help.text())

    def test_external_project_edit_during_run_discards_result(self):
        with patch.object(QFileDialog,'getExistingDirectory',return_value=str(self.root)):self.assertTrue(self.dialog.start())
        self.owner.notes.setText('Changed outside processing');self.owner.changed();self.wait()
        self.assertIsNone(self.dialog.result);self.assertEqual(p.load_manifest(self.dialog.folder)['state'],'ABORTED')

    def test_cancel_during_shared_background_preparation_never_launches_engine(self):
        actual=p.prepare
        def slow(*args,**kwargs):
            cancelled=args[-1]
            deadline=time.monotonic()+2
            while not cancelled() and time.monotonic()<deadline:time.sleep(.01)
            if cancelled():raise ValueError('Preparation cancelled before engine launch.')
            return actual(*args,**kwargs)
        with patch.object(p,'prepare',slow),patch.object(QFileDialog,'getExistingDirectory',return_value=str(self.root)):
            self.assertTrue(self.dialog.start());self.assertTrue(self.dialog.preparing);self.dialog.stop();self.wait()
        self.assertIsNone(self.dialog.process);self.assertIsNone(self.dialog.result);self.assertFalse(self.owner.project['observations'])
