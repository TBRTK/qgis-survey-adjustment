# SPDX-License-Identifier: GPL-3.0-only
from pathlib import Path
import tempfile,time,unittest
from unittest.mock import patch
from test_qgis import app,ROOT,Iface
from qgis.PyQt.QtWidgets import QFileDialog
from survey_adjustment.plugin import SurveyDialog
from survey_adjustment.gnss_processing_ui import ProcessingDialog
from survey_adjustment.orientation_ui import OrientationDialog
from survey_adjustment import orientation as o


class OrientationQgisTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT/'test_output');self.root=Path(self.tmp.name)
        self.host=SurveyDialog(Iface());self.parent=ProcessingDialog(self.host.network_panel)
        self.dialog=OrientationDialog(self.parent);self.before=self.host.network_panel.snapshot()
    def tearDown(self):
        self.dialog.reject();self.dialog.deleteLater();self.parent.reject();self.parent.deleteLater()
        self.host._unloading=True;self.host.close();self.host.deleteLater();app.processEvents();self.tmp.cleanup()
    def example(self):
        with patch.object(QFileDialog,'getExistingDirectory',return_value=str(self.root)):self.assertTrue(self.dialog.example())
        for field in (self.dialog.endpoint_review,self.dialog.height_review,self.dialog.time_review):
            self.assertFalse(field.isChecked());field.setChecked(True)
    def start(self):
        self.assertTrue(self.dialog.start());deadline=time.monotonic()+10
        while self.dialog.busy and time.monotonic()<deadline:app.processEvents();time.sleep(.01)
        self.assertFalse(self.dialog.busy);self.assertIsNotNone(self.dialog.result,self.dialog.status.text())
    def test_existing_queue_pointz_height_export_and_unchanged_network(self):
        self.example();self.start();dialog=self.dialog
        self.assertEqual(dialog.result['accepted'],117);self.assertEqual(dialog.table.rowCount(),121)
        self.assertEqual(dialog.layer.featureCount(),117);self.assertTrue(dialog.layer.readOnly())
        feature=next(dialog.layer.getFeatures());self.assertTrue(feature.geometry().constGet().is3D())
        self.assertEqual(feature.geometry().constGet().z(),dialog.result['rows'][0]['ground_llh'][2])
        self.assertIn('not raw PPK',dialog.report.toPlainText());self.assertIn('0.095',dialog.report.toPlainText())
        with patch.object(QFileDialog,'getExistingDirectory',return_value=str(self.root)):self.assertTrue(dialog.export(),dialog.status.text())
        folder=next(self.root.glob('ETC_coordinates_*'));self.assertEqual(o.verify_snapshot(folder)['accepted'],117)
        self.assertEqual(self.before,self.host.network_panel.snapshot())
    def test_settings_invalidate_and_already_ground_skips_second_correction(self):
        self.example();self.start();dialog=self.dialog;dialog.endpoint.setCurrentIndex(dialog.endpoint.findData('already-ground'))
        self.assertIsNone(dialog.result);self.assertFalse(dialog.export_button.isEnabled());self.assertEqual(dialog.table.rowCount(),0)
        dialog.height.clear();self.start()
        self.assertEqual(dialog.result['rows'][0]['ground_llh'],dialog.result['rows'][0]['original_llh'])
    def test_unknown_endpoint_and_changed_original_block_export(self):
        self.example();dialog=self.dialog;dialog.endpoint.setCurrentIndex(0)
        self.assertTrue(dialog.start());deadline=time.monotonic()+10
        while dialog.busy and time.monotonic()<deadline:app.processEvents();time.sleep(.01)
        self.assertIsNone(dialog.result);self.assertFalse(dialog.export_button.isEnabled());self.assertIn('Declare',dialog.status.text())
        dialog.endpoint.setCurrentIndex(dialog.endpoint.findData('uncompensated-antenna'));self.start()
        path=Path(dialog.path.text());path.write_bytes(path.read_bytes()+b'\n')
        with patch.object(QFileDialog,'getExistingDirectory',return_value=str(self.root)):self.assertFalse(dialog.export())
        self.assertIsNone(dialog.result);self.assertIn('changed since review',dialog.status.text())
    def test_paired_example_reuses_existing_links_queue_coverage_and_snapshot(self):
        self.dialog.paired_example.setChecked(True);self.example();before=self.host.network_panel.snapshot();self.start();dialog=self.dialog
        self.assertEqual(len(before['raw_sources']),3);self.assertEqual(len(before['observations']),0)
        self.assertEqual(dialog.rover.count(),2);self.assertEqual(dialog.coverage_table.rowCount(),121)
        self.assertEqual(dialog.result['rinex_coverage']['usable_epochs'],117)
        self.assertIn('INCOMPLETE_EPOCH_COVERAGE',dialog.report.toPlainText())
        self.assertIn('minus UTC = 18',dialog.report.toPlainText())
        self.assertEqual(dialog.coverage_table.item(20,2).text(),'False')
        with patch.object(QFileDialog,'getExistingDirectory',return_value=str(self.root)):self.assertTrue(dialog.export(),dialog.status.text())
        folder=next(self.root.glob('ETC_coordinates_*'));result=o.verify_snapshot(folder)
        self.assertEqual(result['rinex_coverage']['raw_epochs'],121)
        self.assertEqual(before,self.host.network_panel.snapshot())
        dialog.rover.setCurrentIndex(0);self.assertIsNone(dialog.result);self.assertEqual(dialog.coverage_table.rowCount(),0)
        self.start();self.assertNotIn('rinex_coverage',dialog.result)
    def test_changed_linked_network_blocks_review_and_export(self):
        self.dialog.paired_example.setChecked(True);self.example();self.start()
        self.host.network_panel.notes.setText(self.host.network_panel.notes.text()+' altered')
        with patch.object(QFileDialog,'getExistingDirectory',return_value=str(self.root)):self.assertFalse(self.dialog.export())
        self.assertIsNone(self.dialog.result);self.assertIn('Network changed',self.dialog.status.text())
        self.assertFalse(self.dialog.start());self.assertIn('Network changed',self.dialog.status.text())
