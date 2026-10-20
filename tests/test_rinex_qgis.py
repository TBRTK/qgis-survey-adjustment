# SPDX-License-Identifier: GPL-3.0-only
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
import tempfile,time,unittest
from test_qgis import app,ROOT,Iface
from test_rinex import catalogue
from qgis.PyQt.QtCore import QSettings,QCoreApplication,QEvent
from qgis.PyQt.QtWidgets import QFileDialog,QDialog
from survey_adjustment import network as n,rinex as r
from survey_adjustment.plugin import SurveyDialog
from survey_adjustment.rinex_ui import RawImportDialog,raw_map_layers
from survey_adjustment.baseline_ui import BaselineImportDialog


class RinexQgisTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT/'test_output'); self.root=Path(self.tmp.name)
        settings=QSettings(str(self.root/'settings.ini'),QSettings.Format.IniFormat)
        self.settings_patch=patch('survey_adjustment.network_ui.QSettings',return_value=settings)
        mock=self.settings_patch.start(); mock.Status=QSettings.Status
        self.host=SurveyDialog(Iface()); self.panel=self.host.network_panel; self.panel.replace_project(n.synthetic_project())
        self.raw=self.panel.raw_panel

    def tearDown(self):
        if self.raw.task: self.raw.cancel(); self.wait()
        self.host._unloading=True; self.host.close(); self.host.deleteLater()
        QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete); self.settings_patch.stop(); self.tmp.cleanup()

    def wait(self):
        deadline=time.monotonic()+20
        while self.raw.task is not None and time.monotonic()<deadline: app.processEvents(); time.sleep(.01)
        self.assertIsNone(self.raw.task,self.raw.message.text()); app.processEvents()

    def fill(self,dialog):
        dialog.role.setCurrentIndex(dialog.role.findData('BASE')); dialog.point.setCurrentIndex(dialog.point.findData('REF0'))
        dialog.session.setCurrentIndex(dialog.session.findData('S01')); dialog.fields['utc_offset_seconds'].setText('18')
        dialog.fields['time_evidence'].setText('SYNTHETIC GPS-UTC offset 18 seconds for these fixtures.')
        dialog.fields['antenna_evidence'].setText('SYNTHETIC model and offsets; no antenna correction.')
        dialog.fields['note'].setText('SYNTHETIC UI test.'); dialog.synthetic.setChecked(True); dialog.confirm.setChecked(True)

    def test_one_import_entry_routes_rinex_and_preserves_existing_pos_dialog(self):
        def execute(dialog):
            self.fill(dialog); self.assertTrue(dialog.submit(),dialog.status.text()); return dialog.result()
        with patch.object(QFileDialog,'getOpenFileName',return_value=(str(ROOT/'survey_adjustment/examples/synthetic_base.rnx'),'')),patch.object(RawImportDialog,'exec',execute):
            self.assertTrue(self.panel.import_gnss()); self.wait()
        self.assertEqual(len(self.panel.project['raw_sources']),1); self.assertEqual(len(self.panel.project['observations']),4)
        with patch.object(QFileDialog,'getOpenFileName',return_value=(str(ROOT/'survey_adjustment/examples/synthetic_baseline_llh.pos'),'')),patch.object(BaselineImportDialog,'exec',return_value=QDialog.DialogCode.Rejected) as existing:
            self.assertFalse(self.panel.import_gnss()); self.assertTrue(existing.called)
        with patch.object(QFileDialog,'getOpenFileName',return_value=('','')): self.assertFalse(self.panel.import_gnss())

    def test_examples_preflight_linked_timeline_map_heights_save_reopen_and_stale_state(self):
        before=deepcopy(self.panel.project)
        self.assertTrue(self.raw.load_examples()); self.wait(); self.assertEqual(self.raw.table.rowCount(),3)
        for key in ('points','sessions','observations','imports'): self.assertEqual(self.panel.project[key],before[key])
        self.assertTrue(self.raw.check()); self.wait(); self.assertIsNotNone(self.raw.report,self.raw.message.text())
        self.assertEqual(self.raw.report['status'],'RAW_CATALOGUE_CHECKED',self.raw.report)
        layer=self.raw.layers['points']; self.assertEqual(layer.featureCount(),2); self.assertTrue(layer.readOnly())
        first=next(layer.getFeatures()); self.assertTrue(first.geometry().constGet().is3D()); self.assertAlmostEqual(first.geometry().constGet().z(),80.,places=3)
        self.raw.select_record('raw_source',self.panel.project['raw_sources'][1]['id']); self.assertEqual(self.raw.table.currentRow(),1)
        self.assertEqual(layer.selectedFeatureCount(),1); self.assertEqual(len(self.raw.timeline.bars),2)
        path=self.root/'raw_network.sanet.json'; self.assertTrue(self.panel.write(path)); self.assertEqual(n.load_project(path),self.panel.snapshot())
        self.assertIsNone(self.raw.report); self.assertFalse(self.raw.layers)
        self.assertTrue(self.raw.check()); self.wait(); self.panel.notes.setText('Changed model note'); self.panel.changed()
        self.assertIsNone(self.raw.report); self.assertFalse(self.raw.canvas.layers())
        self.assertTrue(self.raw.canvas.isHidden(),'A stale rendered image must not remain visible after metadata changes')

    def test_duplicate_examples_rejected_atomically_and_missing_source_not_mapped(self):
        self.raw.load_examples(); self.wait(); before=self.panel.snapshot()
        self.raw.load_examples(); self.wait(); self.assertEqual(self.panel.snapshot(),before); self.assertIn('already imported',self.raw.message.text())
        project=catalogue(self.root); Path(project['raw_sources'][0]['path']).unlink()
        self.panel.replace_project(project); self.raw.check(); self.wait()
        self.assertEqual(self.raw.report['status'],'BLOCKED'); self.assertEqual(self.raw.layers['points'].featureCount(),1)

    def test_raw_review_requires_confirmation_and_rechecks_changed_source(self):
        path=self.root/'base.rnx'; path.write_bytes((ROOT/'survey_adjustment/examples/synthetic_base.rnx').read_bytes())
        source=r.read_source(path); dialog=RawImportDialog(self.panel,self.panel.snapshot(),source)
        self.assertFalse(dialog.submit()); self.fill(dialog)
        path.write_bytes(path.read_bytes()+b'\n'); self.assertFalse(dialog.submit()); self.assertIn('changed',dialog.status.text())
        self.assertIsNone(dialog.result_project); dialog.reject(); dialog.deleteLater()

    def test_review_existing_metadata_keeps_source_identity_and_no_second_import(self):
        project=catalogue(); source=r.read_source(project['raw_sources'][0]['path'])
        dialog=RawImportDialog(self.panel,project,source,project['raw_sources'][0]); self.fill(dialog)
        dialog.fields['note'].setText('SYNTHETIC clarified provenance.'); self.assertTrue(dialog.submit(),dialog.status.text())
        self.assertEqual(len(dialog.result_project['raw_sources']),3)
        self.assertEqual(dialog.result_project['raw_sources'][0]['id'],project['raw_sources'][0]['id'])
        self.assertEqual(dialog.result_project['raw_sources'][0]['metadata'],project['raw_sources'][0]['metadata'])
        dialog.deleteLater()

    def test_changed_network_during_task_cannot_apply_result_and_ui_layout_renders(self):
        self.raw.load_examples(); self.panel.notes.setText('Changed while task runs'); self.panel.changed(); self.wait()
        self.assertFalse(self.panel.project.get('raw_sources')); self.assertIn('changed',self.raw.message.text())
        self.panel.replace_project(n.synthetic_project()); self.raw.load_examples(); self.wait(); self.raw.check(); self.wait()
        self.host.tabs.setCurrentIndex(4); self.panel.tabs.setCurrentWidget(self.raw); self.host.show(); app.processEvents()
        self.assertTrue(self.host.grab().save(str(ROOT/'test_output/qgis_rinex_090.png')))
        self.raw.views.setCurrentWidget(self.raw.timeline); app.processEvents()
        self.assertTrue(self.host.grab().save(str(ROOT/'test_output/qgis_rinex_timeline_090.png')))
        self.raw.views.setCurrentIndex(2); app.processEvents()
        self.assertTrue(self.host.grab().save(str(ROOT/'test_output/qgis_rinex_map_090.png')))

    def test_cancelled_background_inspection_does_not_apply_a_result(self):
        applied=[]
        def slow(cancelled):
            deadline=time.monotonic()+2
            while not cancelled() and time.monotonic()<deadline: time.sleep(.01)
            return 'discarded'
        self.raw.work('Cancellation test',slow,applied.append); self.raw.cancel(); self.wait()
        self.assertEqual(applied,[]); self.assertFalse(self.panel.project.get('raw_sources'))
        self.assertIn('not applied',self.raw.message.text())

    def test_closing_plugin_cancels_pending_raw_task(self):
        applied=[]
        def slow(cancelled):
            deadline=time.monotonic()+2
            while not cancelled() and time.monotonic()<deadline: time.sleep(.01)
            return 'must not be imported'
        self.raw.work('Close during inspection',slow,applied.append)
        self.host._unloading=True; self.host.close(); self.wait(); self.assertEqual(applied,[])
