# SPDX-License-Identifier: GPL-3.0-only
from copy import deepcopy
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from test_qgis import app,Iface,ROOT
from qgis.PyQt.QtCore import QSettings,QCoreApplication,QEvent,QProcess
from qgis.PyQt.QtWidgets import QFileDialog
from survey_adjustment.plugin import SurveyDialog
from survey_adjustment import network as n,network_quality as q
from survey_adjustment.network_quality_ui import NetworkQualityDialog

PROGRAM=ROOT/'upstream/extracted/SALSA/bin/lsasolver.exe'


class NetworkQualityQgisTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'test_output'); self.folder=Path(self.temp.name)
        self.settings=QSettings(str(self.folder/'settings.ini'),QSettings.Format.IniFormat)
        self.settings_patch=patch('survey_adjustment.network_ui.QSettings',return_value=self.settings)
        mocked=self.settings_patch.start(); mocked.Status=QSettings.Status
        self.host=SurveyDialog(Iface()); self.panel=self.host.network_panel
        self.panel.replace_project(q.synthetic_quality_project())
        self.host.solver.setText(str(PROGRAM))
        self.dialog=NetworkQualityDialog(self.panel,self.panel.snapshot(),self.host)

    def tearDown(self):
        self.dialog.reject(); self.dialog.deleteLater()
        self.host._unloading=True; self.host.close(); self.host.deleteLater()
        app.processEvents(); QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        self.settings_patch.stop(); self.temp.cleanup()

    def review(self):
        self.dialog.note.setText('SYNTHETIC independent Gaussian blocks and independent coordinate prior; frame/epoch/endpoints reviewed.')
        for field in (self.dialog.frame_review,self.dialog.endpoint_review,self.dialog.independence_review): field.setChecked(True)

    def wait(self):
        deadline=time.monotonic()+20
        while self.dialog.process.state()!=QProcess.ProcessState.NotRunning and time.monotonic()<deadline:
            app.processEvents(); time.sleep(.01)
        app.processEvents()
        self.assertEqual(self.dialog.process.state(),QProcess.ProcessState.NotRunning,self.dialog.status.text())

    def test_review_preview_uncertainty_and_redundancy_tables_and_invalidation(self):
        with patch.object(self.panel,'maybe_leave',return_value=True):
            self.assertTrue(self.panel.load_quality_example())
        self.assertEqual(self.panel.snapshot(),n.load_project(ROOT/'survey_adjustment/examples/gnss_quality.sanet.json'))
        before=self.panel.snapshot(); self.assertFalse(self.dialog.preview())
        self.review(); self.assertTrue(self.dialog.preview(),self.dialog.status.text())
        self.assertIsNone(self.dialog.result)
        self.assertIn('SALSA has not run',self.dialog.status.text())
        self.assertEqual(self.dialog.point_table.rowCount(),5)
        self.assertEqual(self.dialog.residual_table.rowCount(),8)
        self.assertEqual(self.dialog.check_table.rowCount(),1)
        self.assertIsNotNone(self.dialog.residual_table.cellWidget(0,6))
        self.dialog.show(); app.processEvents()
        self.assertTrue(self.dialog.grab().save(str(ROOT/'test_output/qgis_network_quality_050_preview.png')))
        self.dialog.horizontal.setText('0.01')
        self.assertIsNone(self.dialog.preview_analysis); self.assertEqual(self.dialog.point_table.rowCount(),0)
        self.assertEqual(self.panel.snapshot(),before)

    def test_no_files_written_for_invalid_model_or_cancelled_destination(self):
        self.assertFalse(self.dialog.start())
        self.review()
        with patch.object(QFileDialog,'getExistingDirectory',return_value=''):
            self.assertFalse(self.dialog.start())
        self.assertIsNone(self.dialog.folder)

    def test_failed_preview_clears_previous_tables_and_report_link(self):
        self.review(); self.assertTrue(self.dialog.preview())
        self.dialog.open_button.setEnabled(True)
        with patch.object(q,'analyse',side_effect=ValueError('Model is no longer valid')):
            self.assertFalse(self.dialog.preview())
        self.assertIsNone(self.dialog.result); self.assertIsNone(self.dialog.preview_analysis)
        self.assertFalse(self.dialog.open_button.isEnabled())
        for table in (self.dialog.point_table,self.dialog.residual_table,self.dialog.check_table):
            self.assertEqual(table.rowCount(),0)
        self.assertEqual(self.dialog.overview.toPlainText(),'')
        self.assertIn('Cannot preview',self.dialog.status.text())

    @unittest.skipUnless(PROGRAM.exists(),'Local SALSA engine unavailable')
    def test_actual_nonblocking_salsa_flow_and_local_report_snapshot(self):
        before=deepcopy(self.panel.snapshot()); self.review()
        with patch.object(QFileDialog,'getExistingDirectory',return_value=str(self.folder)):
            self.assertTrue(self.dialog.start(),self.dialog.status.text())
        self.wait(); result=self.dialog.result
        self.assertIsNotNone(result,self.dialog.status.text())
        self.assertEqual(result['engine_confirmation']['version'],'1.17.0')
        self.assertTrue(self.dialog.open_button.isEnabled()); self.assertTrue(self.dialog.controls.isEnabled())
        self.assertEqual(self.panel.snapshot(),before)
        self.assertEqual(n.load_project(self.dialog.folder/'network.sanet.json'),before)
        self.assertTrue((self.dialog.folder/'quality.json').exists())
        self.dialog.show(); app.processEvents()
        self.assertTrue(self.dialog.grab().save(str(ROOT/'test_output/qgis_network_quality_050_verified.png')))

    @unittest.skipUnless(PROGRAM.exists(),'Local SALSA engine unavailable')
    def test_stop_rejects_partial_results(self):
        self.review()
        with patch.object(QFileDialog,'getExistingDirectory',return_value=str(self.folder)):
            self.assertTrue(self.dialog.start())
        self.dialog.stop(); self.wait()
        self.assertIsNone(self.dialog.result); self.assertFalse(self.dialog.open_button.isEnabled())
        self.assertFalse(self.dialog.map_button.isEnabled())
        self.assertIn('stopped',self.dialog.status.text().lower())

    @unittest.skipUnless(PROGRAM.exists(),'Local SALSA engine unavailable')
    def test_changed_network_cannot_be_shown_as_current_verified_result(self):
        self.review()
        with patch.object(QFileDialog,'getExistingDirectory',return_value=str(self.folder)):
            self.assertTrue(self.dialog.start())
        changed=self.panel.snapshot(); changed['notes']+=' SYNTHETIC edit during run'
        self.panel.replace_project(changed)
        self.wait()
        self.assertIsNone(self.dialog.result); self.assertIn('Network changed',self.dialog.status.text())


if __name__=='__main__': unittest.main(verbosity=2)
