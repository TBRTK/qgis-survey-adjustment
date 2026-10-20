# SPDX-License-Identifier: GPL-3.0-only
import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from test_qgis import app,ROOT,PROGRAM,Iface
from test_delivery import create_run,review,PATHS
from qgis.PyQt.QtCore import QSettings,QCoreApplication,QEvent
from qgis.PyQt.QtWidgets import QFileDialog,QPushButton
from survey_adjustment.plugin import SurveyDialog
from survey_adjustment.delivery_ui import DeliveryDialog
from survey_adjustment import delivery as d


@unittest.skipUnless(PROGRAM.exists() and all(p.exists() for p in PATHS.values()),'Separate SALSA and reviewed local HREF grids required')
class DeliveryQgisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory(dir=ROOT/'test_output'); cls.path=Path(cls.tmp.name)
        cls.completed=create_run(cls.path/'calculation')

    @classmethod
    def tearDownClass(cls): cls.tmp.cleanup()

    def setUp(self):
        self.case=tempfile.TemporaryDirectory(dir=self.path); self.folder=Path(self.case.name)
        settings=QSettings(str(self.folder/'settings.ini'),QSettings.Format.IniFormat)
        self.settings_patch=patch('survey_adjustment.network_ui.QSettings',return_value=settings)
        mocked=self.settings_patch.start(); mocked.Status=QSettings.Status
        self.host=SurveyDialog(Iface()); self.host.solver.setText(str(PROGRAM))
        self.dialog=DeliveryDialog(self.host,self.host,self.completed['folder']/'quality.json')

    def tearDown(self):
        self.dialog.reject(); self.dialog.deleteLater(); self.host._unloading=True; self.host.close(); self.host.deleteLater()
        QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete); self.settings_patch.stop(); self.case.cleanup()

    def fill(self):
        r=review(target_height='NN2000:2025',target_crs='EPSG:5105')
        for name,field in self.dialog.fields.items(): field.setText(getattr(r,name))
        self.dialog.source_height.setCurrentIndex(self.dialog.source_height.findData(r.source_height))
        self.dialog.target_height.setCurrentIndex(self.dialog.target_height.findData(r.target_height))
        self.dialog.target_crs.setCurrentIndex(self.dialog.target_crs.findData(r.target_crs))
        for height,path in PATHS.items(): self.dialog.model_paths[height].setText(str(path))
        self.dialog.reviewed.setChecked(True)

    def test_mandatory_evidence_preview_export_check_and_stale_settings(self):
        self.assertIsNotNone(self.dialog.run); self.assertFalse(self.dialog.preview()); self.assertFalse(self.dialog.export_button.isEnabled())
        self.fill(); self.assertTrue(self.dialog.preview(),self.dialog.status.text())
        self.assertTrue(self.dialog.export_button.isEnabled())
        with patch.object(QFileDialog,'getExistingDirectory',return_value=str(self.folder)): self.assertTrue(self.dialog.export(),self.dialog.status.text())
        self.assertTrue(d.verify_package(self.dialog.folder)); self.assertTrue(self.dialog.folder_button.isEnabled())
        with patch.object(QFileDialog,'getExistingDirectory',return_value=str(self.dialog.folder)): self.assertTrue(self.dialog.check_package())
        self.dialog.fields['epoch_evidence'].setText('Changed review')
        self.assertIsNone(self.dialog.result); self.assertFalse(self.dialog.export_button.isEnabled())

    def test_cancelled_destination_and_open_preserve_current_run(self):
        self.fill(); self.assertTrue(self.dialog.preview()); identity=self.dialog.run['identity']
        with patch.object(QFileDialog,'getExistingDirectory',return_value=''): self.assertFalse(self.dialog.export())
        self.assertIsNone(self.dialog.folder)
        with patch.object(QFileDialog,'getOpenFileName',return_value=('','')): self.assertFalse(self.dialog.open_run())
        self.assertEqual(self.dialog.run['identity'],identity)

    def test_reload_requires_fresh_confirmation_and_input_version_is_not_guessed(self):
        self.fill(); self.assertTrue(self.dialog.reviewed.isChecked())
        self.assertTrue(self.dialog.load(self.completed['folder']/'quality.json'))
        self.assertFalse(self.dialog.reviewed.isChecked()); self.assertFalse(self.dialog.preview())
        self.assertEqual(self.dialog.source_height.currentData(),'ELLIP')
        self.assertEqual(self.dialog.target_height.currentData(),'')
        self.assertTrue(all(not f.text() for f in self.dialog.fields.values()))

    def test_field_help_and_scroll_layout_render_in_qgis(self):
        self.fill(); self.dialog.show(); app.processEvents()
        self.assertTrue(all(f.toolTip() for f in self.dialog.fields.values()))
        self.assertIn('HREF2025A',self.dialog.help.toPlainText())
        self.assertTrue(any('delivery package' in b.text().lower() for b in self.host.network_panel.findChildren(QPushButton)))
        self.assertTrue(self.dialog.grab().save(str(ROOT/'test_output/qgis_delivery_080.png')))
