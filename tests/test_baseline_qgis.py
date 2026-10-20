# SPDX-License-Identifier: GPL-3.0-only
from copy import deepcopy
from pathlib import Path
import json
import math
import tempfile
import unittest
from unittest.mock import patch
from test_qgis import app,Iface,ROOT
from qgis.core import QgsCoordinateReferenceSystem,QgsCoordinateTransform,QgsProject,QgsVector3D
from qgis.PyQt.QtCore import QSettings,QCoreApplication,QEvent
from qgis.PyQt.QtWidgets import QDialog,QFileDialog,QMessageBox
from survey_adjustment import network as n,baselines as b
from survey_adjustment.baseline_ui import BaselineImportDialog
from survey_adjustment.network_ui import RecordEditor
from survey_adjustment.plugin import SurveyDialog


class BaselineQgisTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'test_output'); self.folder=Path(self.temp.name)
        self.settings=QSettings(str(self.folder/'settings.ini'),QSettings.Format.IniFormat)
        self.settings_patch=patch('survey_adjustment.network_ui.QSettings',return_value=self.settings)
        mock=self.settings_patch.start(); mock.Status=QSettings.Status
        self.host=SurveyDialog(Iface()); self.panel=self.host.network_panel
        self.panel.replace_project(n.synthetic_project())
        self.dialog=BaselineImportDialog(self.panel,self.panel.snapshot())

    def tearDown(self):
        self.dialog.close(); self.dialog.deleteLater()
        self.host._unloading=True; self.host.close(); self.host.deleteLater()
        app.processEvents(); QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        self.settings_patch.stop(); self.temp.cleanup()

    def configure(self,dialog,path=None):
        self.assertTrue(dialog.load(path or ROOT/'survey_adjustment/examples/synthetic_baseline_llh.pos'))
        dialog.synthetic.setChecked(True)
        dialog.table.setCurrentCell(0,0); dialog.table.selectRow(0)
        for field,value in ((dialog.origin,'REF0'),(dialog.target,'P01'),(dialog.session,'S01'),(dialog.frame,'EPSG:4936')):
            field.setCurrentIndex(field.findData(value))
        dialog.antenna_note.setText('SYNTHETIC physical points; no actual antenna.')
        dialog.uncertainty_note.setText('SYNTHETIC formal covariance; fixed processing base; dependence reviewed.')

    def confirm(self,dialog):
        for check in (dialog.relative,dialog.endpoints,dialog.covariance): check.setChecked(True)

    def test_preview_requires_explicit_confirmations_and_changes_invalidate_it(self):
        dialog=self.dialog; self.configure(dialog)
        self.assertFalse(dialog.preview()); self.assertFalse(dialog.import_button.isEnabled())
        self.assertFalse(dialog.finish()); self.assertIsNone(dialog.result_project)
        self.confirm(dialog); self.assertTrue(dialog.preview(),dialog.review.toPlainText())
        self.assertTrue(dialog.import_button.isEnabled())
        dialog.antenna_note.setText('SYNTHETIC revised treatment')
        self.assertFalse(dialog.import_button.isEnabled()); self.assertIsNone(dialog.plan)

    def test_full_panel_import_check_save_open_retains_covariance_audit_and_heights(self):
        before=self.panel.snapshot()
        def execute(dialog):
            self.configure(dialog); self.confirm(dialog)
            self.assertTrue(dialog.preview(),dialog.review.toPlainText())
            dialog.show(); app.processEvents(); dialog.grab().save(str(ROOT/'test_output/qgis_baseline_preview_040.png'))
            self.assertTrue(dialog.finish(),dialog.review.toPlainText())
            return dialog.result()
        with patch.object(BaselineImportDialog,'exec',execute): self.assertTrue(self.panel.import_baselines())
        imported=self.panel.snapshot()
        self.assertEqual(imported['points'],before['points']); self.assertEqual(len(imported['observations']),5)
        self.assertEqual(len(imported['imports']),1); self.assertTrue(self.panel.check())
        path=self.folder/'measured.sanet.json'; self.assertTrue(self.panel.write(path))
        self.panel.new()
        with patch.object(QFileDialog,'getOpenFileName',return_value=(str(path),'')): self.assertTrue(self.panel.open())
        self.assertEqual(self.panel.snapshot(),imported)
        self.host.tabs.setCurrentIndex(4); self.host.show(); app.processEvents()
        self.host.grab().save(str(ROOT/'test_output/qgis_baseline_network_040.png'))

    def test_cancel_does_not_change_project_and_missing_endpoints_are_not_created(self):
        before=self.panel.snapshot()
        with patch.object(BaselineImportDialog,'exec',return_value=QDialog.DialogCode.Rejected):
            self.assertFalse(self.panel.import_baselines())
        self.assertEqual(self.panel.snapshot(),before)
        self.configure(self.dialog); self.confirm(self.dialog)
        self.dialog.target.setCurrentIndex(0)
        self.assertFalse(self.dialog.preview()); self.assertEqual(self.panel.snapshot(),before)

    def test_synthetic_classification_requires_explicit_choice_for_every_source(self):
        self.assertFalse(self.dialog.synthetic.isChecked())
        self.configure(self.dialog); self.assertTrue(self.dialog.synthetic.isChecked())
        self.assertTrue(self.dialog.load(ROOT/'survey_adjustment/examples/synthetic_baseline_llh.pos'))
        self.assertFalse(self.dialog.synthetic.isChecked())
        self.dialog.table.setCurrentCell(0,0); self.dialog.table.selectRow(0)
        self.confirm(self.dialog)
        self.assertFalse(self.dialog.preview()); self.assertIn('separate',self.dialog.review.toPlainText())

    def test_source_change_or_removal_after_preview_leaves_import_unapplied(self):
        path=self.folder/'synthetic.pos'; path.write_bytes((ROOT/'survey_adjustment/examples/synthetic_baseline_llh.pos').read_bytes())
        self.configure(self.dialog,path); self.confirm(self.dialog); self.assertTrue(self.dialog.preview())
        path.write_bytes(path.read_bytes()+b'\n% changed after preview\n')
        self.assertFalse(self.dialog.finish()); self.assertIsNone(self.dialog.result_project)
        self.assertIn('changed after preview',self.dialog.review.toPlainText())
        self.configure(self.dialog,path); self.confirm(self.dialog); self.assertTrue(self.dialog.preview())
        path.unlink(); self.assertFalse(self.dialog.finish()); self.assertIsNone(self.dialog.result_project)

    def test_missing_mode_is_explicit_and_resets_for_each_new_source(self):
        path=self.folder/'constructed.pos'
        text=(ROOT/'survey_adjustment/examples/synthetic_baseline_llh.pos').read_text().replace('% pos mode  : static\n','')
        path.write_text(text,encoding='utf-8')
        dialog=self.dialog
        self.assertFalse(dialog.load(path)); self.assertIn('omits processing mode',dialog.review.toPlainText())
        dialog.source_mode.setCurrentIndex(dialog.source_mode.findData('static'))
        self.assertIsNotNone(dialog.source); self.assertEqual(dialog.table.rowCount(),3)
        self.assertIn('operator explicitly declared static',dialog.review.toPlainText())
        dialog.synthetic.setChecked(True); dialog.table.setCurrentCell(0,0); dialog.table.selectRow(0)
        for field,value in ((dialog.origin,'REF0'),(dialog.target,'P01'),(dialog.session,'S01'),(dialog.frame,'EPSG:4936')):
            field.setCurrentIndex(field.findData(value))
        dialog.antenna_note.setText('SYNTHETIC physical points; no antenna.')
        dialog.uncertainty_note.setText('SYNTHETIC static processing workflow confirmed.')
        self.confirm(dialog); self.assertTrue(dialog.preview(),dialog.review.toPlainText())
        self.assertIn('operator explicitly declared static',dialog.plan['imports'][0]['uncertainty_note'])
        dialog.source_mode.setCurrentIndex(0)
        self.assertIsNone(dialog.plan); self.assertIsNone(dialog.source); self.assertFalse(dialog.import_button.isEnabled())
        self.assertFalse(any(check.isChecked() for check in (dialog.relative,dialog.endpoints,dialog.covariance)))
        dialog.source_mode.setCurrentIndex(dialog.source_mode.findData('static'))
        self.assertIsNotNone(dialog.source)
        self.assertFalse(dialog.load(path))  # Opening any source requires a new declaration.
        self.assertEqual(dialog.source_mode.currentData(),'')

    def test_imported_editor_cannot_recompute_or_round_original_covariance(self):
        self.configure(self.dialog); self.confirm(self.dialog); self.assertTrue(self.dialog.preview())
        project=self.dialog.plan; original=deepcopy(project['observations'][-1])
        editor=RecordEditor(self.panel,'observations',project,original)
        self.assertFalse(editor.values[0].isEnabled()); self.assertFalse(editor.sigmas[0].isEnabled())
        self.assertFalse(editor.identifier.isEnabled()); self.assertTrue(editor.notes.isEnabled())
        editor.notes.setText('SYNTHETIC reviewed; original measurements unchanged')
        editor.submit(); self.assertEqual(editor.result(),QDialog.DialogCode.Accepted,editor.error.text())
        for field in b.LOCKED_FIELDS: self.assertEqual(editor.record[field],original[field])
        editor.deleteLater()

    def test_legacy_recovery_upgrades_in_memory_without_losing_height(self):
        legacy=n.synthetic_project(); legacy.pop('imports'); legacy['schema_version']=1
        self.settings.setValue(self.panel.RECOVERY,json.dumps(dict(project=legacy,form=self.panel.form_values())))
        self.assertTrue(self.panel.recover())
        self.assertEqual(self.panel.project['schema_version'],2)
        self.assertEqual(self.panel.project['imports'],[])
        self.assertEqual(self.panel.project['points'][-1]['coordinates'][2],81.23456789)

    def test_ecef_conversion_matches_independent_qgis_proj_transform(self):
        src=b.read_source(ROOT/'survey_adjustment/examples/synthetic_baseline_llh.pos')
        # Native POS representation uses WGS84 ellipsoid; processing datum is documented separately.
        transform=QgsCoordinateTransform(QgsCoordinateReferenceSystem('EPSG:4979'),QgsCoordinateReferenceSystem('EPSG:4978'),QgsProject.instance())
        base=src['metadata']['reference']; rover=src['rows'][0]['values']
        a=transform.transform(QgsVector3D(base[1],base[0],base[2]))
        c=transform.transform(QgsVector3D(rover[1],rover[0],rover[2]))
        expected=[c.x()-a.x(),c.y()-a.y(),c.z()-a.z()]
        vector,_,_=b.to_ecef(rover,src['rows'][0]['covariance'],src['metadata'],'EPSG:4936')
        self.assertLess(math.dist(expected,vector),1e-7)


if __name__=='__main__': unittest.main(verbosity=2)
