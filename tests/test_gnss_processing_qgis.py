# SPDX-License-Identifier: GPL-3.0-only
from pathlib import Path
import tempfile,time,unittest
from unittest.mock import patch
from test_qgis import app,ROOT,Iface
from test_gnss_processing import ENGINE
from test_system_engine import SA_ENGINE
from test_azimuth_antenna import AZ_ENGINE
from qgis.PyQt.QtCore import QSettings,QProcess
from qgis.PyQt.QtWidgets import QFileDialog,QDialog
from survey_adjustment.plugin import SurveyDialog
from survey_adjustment.gnss_processing_ui import ProcessingDialog
from survey_adjustment.baseline_ui import BaselineImportDialog
from survey_adjustment import gnss_processing as p,gnss_demo as d,network as n
import test_gnss_processing as processing_tests
import test_network_vrs as network_tests


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

    def test_itrf2020_uses_explicit_frame_in_existing_map_and_import_review(self):
        point=self.owner.project['points'][1];point.update(role='CHECK',coordinates=[280000.,6700000.,80.],notes='Constructed held-out reference only.')
        self.dialog.project=self.owner.snapshot()
        self.dialog.frame.setCurrentIndex(self.dialog.frame.findData('EPSG:9988'));self.start()
        self.assertEqual(self.dialog.layers['points'].crs().authid(),'EPSG:9989')
        self.assertEqual(self.dialog.canvas.mapSettings().destinationCrs().authid(),'EPSG:9989')
        review=BaselineImportDialog(self.dialog,self.owner.snapshot())
        self.assertGreaterEqual(review.frame.findData('EPSG:9988'),0);review.deleteLater()
        self.dialog.table.selectRow(self.dialog.table.rowCount()-1)
        from pyproj import datadir
        from survey_adjustment import delivery
        if not all((Path(datadir.get_data_dir())/name).is_file() for name in delivery.GlobalReferenceOperation.GRIDS):return
        def accept(dialog):
            dialog.epoch.setText('2026.8');dialog.height.setCurrentIndex(dialog.height.findData('ELLIP'));dialog.reviewed.setChecked(True)
            return QDialog.DialogCode.Accepted
        with patch.object(QDialog,'exec',accept):self.assertTrue(self.dialog.compare_reference(),self.dialog.status.text())
        self.assertFalse(self.dialog.last_comparison['known_point_used_to_fit'])
        self.assertTrue(self.dialog.last_comparison_path.is_file())
        self.assertFalse(self.owner.project['observations'])

    def test_three_reference_review_uses_existing_queue_without_virtual_import(self):
        fixture=processing_tests.ReferenceNetworkTests();fixture.setUp()
        try:
            self.owner.replace_project(fixture.project);before=self.owner.snapshot()
            dialog=ProcessingDialog(self.owner);dialog.show()
            def accept(review):
                self.assertEqual(review.windowTitle(),'Three-reference network / VRS feasibility')
                for fields,reference in zip(review.references,fixture.references):
                    source,xyz,epoch,evidence=fields
                    source.setCurrentIndex(source.findData(reference['source_id']))
                    xyz.setText(' '.join(map(str,reference['xyz'])));epoch.setText(str(reference['coordinate_epoch_decimal_year']));evidence.setText(reference['evidence'])
                review.target.setText(' '.join(map(str,fixture.target)));review.reviewed.setChecked(True)
                return QDialog.DialogCode.Accepted
            try:
                with patch.object(QDialog,'exec',accept),patch.object(QFileDialog,'getSaveFileName',return_value=(str(self.root/'network_review.json'),'')):
                    self.assertTrue(dialog.review_reference_network(),dialog.status.text())
                deadline=time.monotonic()+20
                while self.owner.raw_panel.task and time.monotonic()<deadline:app.processEvents();time.sleep(.01)
                self.assertIsNone(self.owner.raw_panel.task)
                self.assertFalse(dialog.last_network_review['virtual_observations_generated'])
                self.assertIn('Virtual observations generated: NO',dialog.report.toPlainText())
                self.assertEqual(self.owner.snapshot(),before)
                self.assertTrue(dialog.last_network_review_path.is_file())
            finally:dialog.reject();dialog.deleteLater();app.processEvents()
        finally:fixture.tearDown()

    @unittest.skipUnless(network_tests.KERNEL.is_file(),'Separate network kernel not bundled')
    def test_three_reference_generation_uses_same_queue_import_float_static_report(self):
        folder=self.root/'network';folder.mkdir()
        project,references,target,antex=network_tests.fixture(folder)
        self.owner.replace_project(project);dialog=self.dialog;dialog.project=self.owner.snapshot();dialog.populate()
        def accept(review):
            for fields,reference in zip(review.references,references):
                source,xyz,epoch,evidence=fields;source.setCurrentIndex(source.findData(reference['source_id']))
                xyz.setText(' '.join(map(str,reference['xyz'])));epoch.setText(str(reference['coordinate_epoch_decimal_year']));evidence.setText(reference['evidence'])
            review.target.setText(' '.join(map(str,target)));review.generate.setChecked(True);review.reviewed.setChecked(True)
            review.kernel.setText(str(network_tests.KERNEL));review.calibration.setText(str(antex))
            review.model_note.setText('SYNTHETIC shared FLOAT network; own GPL/BSD/SOFA review; no integer or accuracy claim.')
            return QDialog.DialogCode.Accepted
        with patch.object(QDialog,'exec',accept),patch.object(QFileDialog,'getExistingDirectory',return_value=str(self.root)):
            self.assertTrue(dialog.review_reference_network(),dialog.status.text())
        deadline=time.monotonic()+30
        while self.owner.raw_panel.task and time.monotonic()<deadline:app.processEvents();time.sleep(.01)
        self.assertIsNone(self.owner.raw_panel.task)
        self.assertEqual(dialog.ambiguity.currentData(),'off');self.assertFalse(dialog.ambiguity.isEnabled())
        self.assertEqual(dialog.mode.currentData(),'static');self.assertEqual(dialog.options().systems,('G',))
        self.assertIn('FLOAT VRS generated',dialog.status.text());self.assertTrue(dialog.reference_product.text())
        for check in (dialog.base_review,dialog.antenna_review,dialog.model_review,dialog.license_review):
            self.assertFalse(check.isChecked());check.setChecked(True)
        self.start();self.assertEqual(dialog.result['eligible_fix'],0)
        self.assertEqual(dialog.source['processing']['profile'],p.NETWORK_FLOAT_PROFILE)
        self.assertFalse(dialog.handoff.isEnabled());self.assertFalse(self.owner.project['observations'])
        self.assertIn('FLOAT ONLY',dialog.report.toPlainText())
        physical=next(row for row in project['raw_sources'] if row['role']=='BASE')
        dialog.base.setCurrentIndex(dialog.base.findData(physical['id']))
        self.assertFalse(dialog.reference_product.text());self.assertTrue(dialog.ambiguity.isEnabled())

    def test_multisystem_example_reuses_existing_project_and_qprocess_with_used_satellites(self):
        dialog=self.dialog
        for s in ('E','C'):dialog.systems[s].setChecked(True)
        with patch.object(QFileDialog,'getExistingDirectory',return_value=str(self.root)),patch.object(self.owner,'maybe_leave',return_value=True):self.assertTrue(dialog.example())
        self.assertEqual(dialog.options().systems,('G','E','C'));self.assertFalse(dialog.base_review.isChecked())
        self.fill();self.start()
        self.assertEqual(set(dialog.result['used_satellites']),{'G','E','C'})
        self.assertIn('E02',dialog.report.toPlainText());self.assertIn('C07',dialog.report.toPlainText())
        self.assertEqual(len(self.owner.project['raw_sources']),3);self.assertFalse(self.owner.project['observations'])

    def test_antex_example_calibration_report_height_and_mixed_system_block(self):
        from survey_adjustment.coordinate_job import llh
        dialog=self.dialog;dialog.calibration.setCurrentIndex(dialog.calibration.findData('antex'))
        with patch.object(QFileDialog,'getExistingDirectory',return_value=str(self.root)),patch.object(self.owner,'maybe_leave',return_value=True):self.assertTrue(dialog.example())
        for field in (dialog.base_review,dialog.antenna_review,dialog.model_review,dialog.license_review):self.assertFalse(field.isChecked());field.setChecked(True)
        self.start();self.assertIn('Applied receiver ANTEX',dialog.report.toPlainText())
        self.assertAlmostEqual(llh(dialog.source['rows'][-1]['values'],'EPSG:4979')[2],llh(d.rover_at(600,False),'EPSG:4979')[2],delta=.04)
        dialog.systems['E'].setChecked(True)
        with patch.object(QFileDialog,'getExistingDirectory') as picker:self.assertFalse(dialog.start());self.assertFalse(picker.called)
        self.assertIn('multiple systems',dialog.status.text());self.assertFalse(dialog.handoff.isEnabled())

    @unittest.skipUnless(SA_ENGINE.is_file(),'Separate experimental engine not bundled')
    def test_survey_engine_qprocess_four_system_example_and_existing_baseline_handoff(self):
        dialog=self.dialog;dialog.engine_variant.setCurrentIndex(dialog.engine_variant.findData('survey-sa1'))
        dialog.executable.setText(str(SA_ENGINE));dialog.calibration.setCurrentIndex(dialog.calibration.findData('antex'))
        for system in ('E','C','R'):dialog.systems[system].setChecked(True)
        with patch.object(QFileDialog,'getExistingDirectory',return_value=str(self.root)),patch.object(self.owner,'maybe_leave',return_value=True):self.assertTrue(dialog.example())
        self.assertFalse(dialog.license_review.isChecked())
        for field in (dialog.base_review,dialog.antenna_review,dialog.model_review,dialog.license_review):field.setChecked(True)
        self.start();self.assertIn('Survey PCV',dialog.report.toPlainText());self.assertIn('R01',dialog.report.toPlainText())
        self.assertEqual(set(dialog.result['used_satellites']),{'G','E','C','R'})
        dialog.select_record('processing_epoch',str(dialog.source['rows'][-1]['line']))
        def execute(review):
            for check in (review.synthetic,review.relative,review.endpoints,review.covariance,review.occupation):check.setChecked(True)
            review.occupation_note.setText('Constructed stationary point, selected final correlated state; not a trajectory average.')
            self.assertTrue(review.preview(),review.review.toPlainText());self.assertTrue(review.finish());return QDialog.DialogCode.Accepted
        with patch.object(BaselineImportDialog,'exec',execute):self.assertTrue(dialog.review_baseline())
        self.assertEqual(len(self.owner.project['observations']),1)
        self.assertEqual(self.owner.project['imports'][0]['processing']['profile'],p.SYSTEM_PROFILE)
        self.owner.write(self.root/'saved_extended.sanet.json');self.assertEqual(n.load_project(self.root/'saved_extended.sanet.json'),self.owner.snapshot())

    @unittest.skipUnless(AZ_ENGINE.is_file(),'Separate experimental azimuth engine not bundled')
    def test_sa2_qprocess_azimuth_review_signal_plan_and_existing_baseline_handoff(self):
        dialog=self.dialog;dialog.engine_variant.setCurrentIndex(dialog.engine_variant.findData('survey-sa2'))
        dialog.executable.setText(str(AZ_ENGINE))
        for system in ('E','C','R'):dialog.systems[system].setChecked(True)
        with patch.object(QFileDialog,'getExistingDirectory',return_value=str(self.root)),patch.object(self.owner,'maybe_leave',return_value=True):self.assertTrue(dialog.example())
        self.assertFalse(dialog.orientation_review.isChecked());self.assertEqual(dialog.rover_heading.text(),'37')
        self.assertTrue(dialog.review_models());self.assertIn('E05',dialog.report.toPlainText());self.assertIn('C06',dialog.report.toPlainText())
        for field in (dialog.base_review,dialog.antenna_review,dialog.model_review,dialog.license_review):field.setChecked(True)
        with patch.object(QFileDialog,'getExistingDirectory') as picker:self.assertFalse(dialog.start());self.assertFalse(picker.called)
        dialog.orientation_review.setChecked(True);self.start()
        self.assertIn('B1C/B3I',dialog.report.toPlainText());self.assertEqual(dialog.result['signal_plan']['E'],['1C','5Q'])
        dialog.select_record('processing_epoch',str(dialog.source['rows'][-1]['line']))
        def execute(review):
            for check in (review.synthetic,review.relative,review.endpoints,review.covariance,review.occupation):check.setChecked(True)
            review.occupation_note.setText('SYNTHETIC fixed level antennas with NRP azimuths 0/37 degrees; final correlated state only.')
            self.assertTrue(review.preview(),review.review.toPlainText());self.assertTrue(review.finish());return QDialog.DialogCode.Accepted
        with patch.object(BaselineImportDialog,'exec',execute):self.assertTrue(dialog.review_baseline())
        self.assertEqual(len(self.owner.project['observations']),1)
        self.assertEqual(self.owner.project['imports'][0]['processing']['profile'],p.AZIMUTH_PROFILE)
        self.owner.write(self.root/'saved_azimuth.sanet.json');self.assertEqual(n.load_project(self.root/'saved_azimuth.sanet.json'),self.owner.snapshot())
        dialog.rover_heading.setText('38');self.assertFalse(dialog.handoff.isEnabled());self.assertIsNone(dialog.result)

    def test_provider_copied_unverified_notes_are_visible_in_model_review(self):
        from survey_adjustment import antenna
        dialog=self.dialog;dialog.engine_variant.setCurrentIndex(dialog.engine_variant.findData('survey-sa2'))
        for system in ('E','C','R'):dialog.systems[system].setChecked(True)
        with patch.object(QFileDialog,'getExistingDirectory',return_value=str(self.root)),patch.object(self.owner,'maybe_leave',return_value=True):self.assertTrue(dialog.example())
        path=Path(dialog.antex.text());text=path.read_text()
        label=d.header('SYNTHETIC: COPIED WITHOUT VERIFICATION BY CALIBRATION!','COMMENT')
        text=text.replace(d.header('     5.0','DAZI'),label+d.header('     5.0','DAZI'))
        path.write_text(text,encoding='ascii')
        self.assertTrue(dialog.review_models());self.assertIn('WITHOUT VERIFICATION',dialog.report.toPlainText())
        self.assertFalse(dialog.handoff.isEnabled());self.assertFalse(dialog.orientation_review.isChecked())

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

    def test_long_static_example_existing_queue_map_atmosphere_audit_and_handoff(self):
        dialog=self.dialog;dialog.atmosphere.setCurrentIndex(dialog.atmosphere.findData(p.STATIC_ATMOSPHERE))
        self.assertIn('not a physical antenna',dialog.report.toPlainText())
        with patch.object(QFileDialog,'getExistingDirectory',return_value=str(self.root)),patch.object(self.owner,'maybe_leave',return_value=True):self.assertTrue(dialog.example())
        self.assertEqual(dialog.atmosphere.currentData(),p.STATIC_ATMOSPHERE);self.assertEqual(len(self.owner.snapshot()['raw_sources']),3)
        self.assertFalse(dialog.model_review.isChecked())
        for field in (dialog.base_review,dialog.antenna_review,dialog.model_review,dialog.license_review):field.setChecked(True)
        self.start();self.assertEqual(dialog.result['atmosphere_audit']['epochs'],121);self.assertEqual(dialog.table.rowCount(),121)
        self.assertIn('60.637',dialog.report.toPlainText());self.assertIn('wet delay is not total zenith',dialog.report.toPlainText())
        self.assertTrue((dialog.folder/'atmosphere_states.csv').is_file());self.assertTrue(next(dialog.layers['points'].getFeatures()).geometry().constGet().is3D())
        dialog.select_record('processing_epoch',str(dialog.source['rows'][-1]['line']))
        covariance=list(dialog.source['rows'][-1]['covariance'])
        def execute(review):
            for check in (review.synthetic,review.relative,review.endpoints,review.covariance,review.occupation):check.setChecked(True)
            review.occupation_note.setText('SYNTHETIC 60 km / 1 hour stationary occupation, selected final filter state; no average or field validation.')
            self.assertTrue(review.preview(),review.review.toPlainText());self.assertTrue(review.finish());return QDialog.DialogCode.Accepted
        with patch.object(BaselineImportDialog,'exec',execute):self.assertTrue(dialog.review_baseline())
        self.assertEqual(self.owner.project['observations'][0]['covariance'],covariance);self.assertEqual(len(self.owner.project['observations']),1)
