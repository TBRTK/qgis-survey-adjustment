# SPDX-License-Identifier: GPL-3.0-only
import tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from test_qgis import app,ROOT,PROGRAM,Iface
from qgis.core import QgsProject,QgsVectorLayer
from qgis.PyQt.QtCore import QSettings,QProcess,QCoreApplication,QEvent
from qgis.PyQt.QtWidgets import QFileDialog,QMessageBox
from survey_adjustment.plugin import SurveyDialog
from survey_adjustment.mixed_survey_ui import MixedSurveyDialog
from survey_adjustment import mixed_survey as m


class MixedSurveyQgisTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(dir=ROOT/'test_output'); self.path=Path(self.temporary.name)
        self.settings=QSettings(str(self.path/'settings.ini'),QSettings.Format.IniFormat)
        self.mock_settings=patch('survey_adjustment.network_ui.QSettings',return_value=self.settings)
        patched=self.mock_settings.start(); patched.Status=QSettings.Status
        self.host=SurveyDialog(Iface()); self.dialog=MixedSurveyDialog(self.host.network_panel,self.host.network_panel.snapshot(),self.host)
        self.layers=set(QgsProject.instance().mapLayers())

    def tearDown(self):
        self.dialog.dirty=False; self.dialog.reject(); self.dialog.deleteLater()
        self.host._unloading=True; self.host.close(); self.host.deleteLater()
        QgsProject.instance().removeMapLayers(list(set(QgsProject.instance().mapLayers())-self.layers))
        self.mock_settings.stop(); self.temporary.cleanup()

    def confirm(self):
        for field in (self.dialog.frame_review,self.dialog.reduction_review,self.dialog.independence_review): field.setChecked(True)

    def test_examples_require_explicit_review_save_open_source_map_and_z_roundtrip(self):
        for profile in ('MIXED_3D','LEVEL_1D'):
            self.dialog.dirty=False; self.assertTrue(self.dialog.load_example(profile)); self.assertFalse(self.dialog.preview())
            self.confirm(); self.assertTrue(self.dialog.preview(),self.dialog.status.text())
            self.assertTrue(self.dialog.add_map(),self.dialog.status.text())
            layer=self.host.iface.active
            self.assertTrue(layer.isValid()); self.assertTrue(layer.readOnly()); self.assertEqual(layer.crs().authid(),'EPSG:25832')
            expected={p['id']:p['position_grid'] for p in self.dialog.result['points'] if p['position_grid']}
            for f in layer.getFeatures():
                p=f.geometry().constGet(); self.assertEqual([p.x(),p.y(),p.z()],expected[f['point_id']]); self.assertEqual(f['height_datum'],self.dialog.project['height_datum'])
            filename=self.path/(profile+'.samix.json')
            with patch.object(QFileDialog,'getSaveFileName',return_value=(str(filename),'')): self.assertTrue(self.dialog.save_project())
            self.assertTrue(filename.exists())
            with patch.object(QFileDialog,'getOpenFileName',return_value=(str(filename),'')): self.assertTrue(self.dialog.open_project())
            self.assertFalse(self.dialog.frame_review.isChecked()); self.assertIsNone(self.dialog.result)
            self.confirm(); self.assertTrue(self.dialog.preview())
            self.dialog.note.setText('Changed model review')
            self.assertIsNone(self.dialog.result); self.assertFalse(self.dialog.add_map())
        self.dialog.dirty=False

    def test_csv_import_template_and_bad_file_do_not_replace_valid_source(self):
        self.dialog.load_example(); self.dialog.dirty=False
        path=self.path/'export.csv'
        with patch.object(QFileDialog,'getSaveFileName',return_value=(str(path),'')): self.assertTrue(self.dialog.save_csv())
        original=self.dialog.source['sha256']
        with patch.object(QFileDialog,'getOpenFileName',return_value=(str(path),'')): self.assertTrue(self.dialog.open_csv())
        self.assertFalse(self.dialog.synthetic_csv.isChecked()); self.assertEqual(self.dialog.source['sha256'],original)
        self.confirm(); self.assertFalse(self.dialog.preview())
        self.dialog.synthetic_csv.setChecked(True); self.assertTrue(self.dialog.preview())
        bad=self.path/'bad.csv'; bad.write_text('id,distance\nP1,1\n')
        with patch.object(QFileDialog,'getOpenFileName',return_value=(str(bad),'')): self.assertFalse(self.dialog.open_csv())
        self.assertEqual(self.dialog.source['sha256'],original)

    def test_saved_alpha_precision_restores_and_invalid_alpha_is_not_clamped(self):
        self.dialog.load_example(); self.dialog.alpha.setText('0.0123456789'); self.confirm()
        self.assertTrue(self.dialog.preview(),self.dialog.status.text())
        path=self.path/'alpha.samix.json'
        with patch.object(QFileDialog,'getSaveFileName',return_value=(str(path),'')): self.assertTrue(self.dialog.save_project())
        self.dialog.alpha.setText('0.05'); self.dialog.dirty=False
        with patch.object(QFileDialog,'getOpenFileName',return_value=(str(path),'')): self.assertTrue(self.dialog.open_project())
        self.assertEqual(self.dialog.options().alpha,.0123456789)
        self.confirm(); self.assertTrue(self.dialog.preview())
        self.dialog.alpha.setText('0.25'); self.assertFalse(self.dialog.preview()); self.assertEqual(self.dialog.alpha.text(),'0.25')

    def test_changed_derived_coordinates_are_rejected_before_adding_layers(self):
        self.dialog.load_example(); self.confirm(); self.assertTrue(self.dialog.preview())
        before=set(QgsProject.instance().mapLayers())
        point=next(p for p in self.dialog.result['points'] if p['role']=='UNKNOWN')
        point['position_grid'][2]+=5
        self.assertFalse(self.dialog.add_map()); self.assertEqual(set(QgsProject.instance().mapLayers()),before)
        self.assertIn('differs',self.dialog.status.text())

    def test_cancelled_open_and_external_change_preserve_the_current_snapshot(self):
        self.dialog.load_example(); self.confirm(); path=self.path/'snapshot.samix.json'
        with patch.object(QFileDialog,'getSaveFileName',return_value=(str(path),'')): self.assertTrue(self.dialog.save_project())
        original=m.dumps(self.dialog.current_job()); self.dialog.note.setText('Unsaved review')
        with patch.object(QFileDialog,'getOpenFileName',return_value=(str(path),'')),patch.object(QMessageBox,'question',return_value=QMessageBox.StandardButton.Cancel):
            self.assertFalse(self.dialog.open_project())
        self.assertEqual(self.dialog.note.text(),'Unsaved review')
        path.write_text(original+'\n',encoding='utf-8'); disk=path.read_bytes()
        with patch.object(QFileDialog,'getSaveFileName',return_value=(str(path),'')): self.assertFalse(self.dialog.save_project())
        self.assertEqual(path.read_bytes(),disk)

    @unittest.skipUnless(PROGRAM.exists(),'Separate SALSA engine not installed')
    def test_nonblocking_salsa_and_report_map_full_flow(self):
        self.dialog.load_example(); self.confirm(); self.dialog.solver.setText(str(PROGRAM))
        with patch.object(QFileDialog,'getExistingDirectory',return_value=str(self.path)):
            self.assertTrue(self.dialog.start(),self.dialog.status.text())
        deadline=time.monotonic()+40
        while time.monotonic()<deadline and (self.dialog.process.state()!=QProcess.ProcessState.NotRunning or self.dialog.result is None):
            app.processEvents(); time.sleep(.01)
            if self.dialog.status.text().startswith('Result rejected:'): break
        self.assertIsNotNone(self.dialog.result,self.dialog.status.text())
        self.assertIn('engine_confirmation',self.dialog.result); self.assertTrue(self.dialog.add_map())
        self.assertTrue((self.dialog.folder/'quality.html').exists()); self.assertTrue((self.dialog.folder/'points.csv').exists())
        self.dialog.show(); app.processEvents(); QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete); app.processEvents()
        self.dialog.grab().save(str(ROOT/'test_output/qgis_mixed_survey.png'))
        self.dialog.dirty=False
