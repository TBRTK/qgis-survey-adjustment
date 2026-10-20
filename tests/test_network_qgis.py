# SPDX-License-Identifier: GPL-3.0-only
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_qgis import app, Iface, ROOT
from qgis.PyQt.QtCore import QSettings,QCoreApplication,QEvent
from qgis.core import QgsCoordinateTransform,QgsCoordinateReferenceSystem,QgsProject,QgsPointXY
from qgis.PyQt.QtWidgets import QDialog,QFileDialog,QMessageBox,QInputDialog
from survey_adjustment.plugin import SurveyDialog,SurveyAdjustmentPlugin
from survey_adjustment import network as n
from survey_adjustment.network_ui import RecordEditor


class NetworkQgisTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'test_output')
        self.folder=Path(self.temp.name)
        self.settings=QSettings(str(self.folder/'settings.ini'),QSettings.Format.IniFormat)
        self.settings_patch=patch('survey_adjustment.network_ui.QSettings',return_value=self.settings)
        settings_mock=self.settings_patch.start()
        settings_mock.Status=QSettings.Status
        self.dialog=SurveyDialog(Iface()); self.panel=self.dialog.network_panel

    def tearDown(self):
        self.dialog._unloading=True
        self.dialog.close(); self.dialog.deleteLater()
        app.processEvents(); QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        self.settings_patch.stop()
        self.temp.cleanup()

    def test_editor_full_add_save_open_roundtrip_in_qgis_preserves_height_and_roles(self):
        panel=self.panel
        panel.name.setText('Manual synthetic test')
        panel.crs.setCurrentIndex(panel.crs.findData('EPSG:25832'))
        panel.height.setCurrentIndex(panel.height.findData('NN2000'))
        expected_z=81.234567890123
        for identifier,role,coords,sigma in [('A','CONSTRAINED',[291000,6701000,expected_z],[.01,.02,.03]),('B','UNKNOWN',None,None),('C','CHECK',[291020,6701000,82],None)]:
            def edit(editor):
                editor.identifier.setText(identifier)
                editor.role.setCurrentIndex(editor.role.findData(role))
                for field,value in zip(editor.coordinates,coords or [None]*3): field.setText('' if value is None else str(value))
                for field,value in zip(editor.sigmas,sigma or [None]*3): field.setText('' if value is None else str(value))
                editor.notes.setText('SYNTHETIC manual test'); editor.submit()
                self.assertEqual(editor.result(),QDialog.DialogCode.Accepted,editor.error.text())
                return editor.result()
            with patch.object(RecordEditor,'exec',edit): self.assertTrue(panel.edit_record('points',True))
        def session_editor(editor):
            editor.identifier.setText('S1'); editor.session_fields['start'].setText('2026-10-18T08:00:00Z')
            editor.submit(); return editor.result()
        with patch.object(RecordEditor,'exec',session_editor): self.assertTrue(panel.edit_record('sessions',True))
        def obs_editor(editor):
            self.assertEqual(editor.sigmas[0].text(),'')
            editor.identifier.setText('O1')
            editor.origin.setCurrentIndex(editor.origin.findData('A')); editor.target.setCurrentIndex(editor.target.findData('B'))
            editor.values[0].setText('100,1234567890123'); editor.sigmas[0].setText('0.012')
            editor.notes.setText('SYNTHETIC measured distance')
            editor.submit(); self.assertEqual(editor.result(),QDialog.DialogCode.Accepted,editor.error.text())
            return editor.result()
        with patch.object(RecordEditor,'exec',obs_editor): self.assertTrue(panel.edit_record('observations',True))
        self.assertTrue(panel.check()); self.assertIn('STRUCTURE VALID',panel.summary.toPlainText())
        self.assertIsNone(self.dialog.engine_result); self.assertIsNone(self.dialog.process)
        saved=panel.snapshot(); self.assertEqual(saved['points'][0]['coordinates'][2],expected_z)
        path=self.folder/'network.sanet.json'
        with patch.object(QFileDialog,'getSaveFileName',return_value=(str(path),'')): self.assertTrue(panel.save())
        self.assertFalse(panel.has_changes()); self.assertFalse(self.settings.contains(panel.RECOVERY))
        panel.new()
        with patch.object(QFileDialog,'getOpenFileName',return_value=(str(path),'')): self.assertTrue(panel.open())
        self.assertEqual(panel.snapshot(),saved)
        self.dialog.tabs.setCurrentIndex(4); self.dialog.show(); app.processEvents()
        QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        self.dialog.grab().save(str(ROOT/'test_output/qgis_network_project.png'))

    def test_independent_check_reference_delete_and_invalid_input_leave_state_intact(self):
        self.panel.load_demo(); original=self.panel.snapshot()
        self.panel.tables['points'].selectRow(0)
        with patch.object(QMessageBox,'warning') as warning:
            self.assertFalse(self.panel.delete_record('points')); self.assertTrue(warning.called)
        self.assertEqual(self.panel.snapshot(),original)
        editor=RecordEditor(self.panel,'observations',original,original['observations'][-1])
        editor.use.setCurrentIndex(editor.use.findData('ADJUSTMENT')); editor.submit()
        self.assertNotEqual(editor.result(),QDialog.DialogCode.Accepted)
        self.assertIn('independent check point',editor.error.text())
        editor.deleteLater()
        self.panel.epoch.setText('not a date'); self.assertFalse(self.panel.check())
        self.assertIn('STRUCTURE ERROR',self.panel.summary.toPlainText())

    def test_external_file_change_and_bad_open_never_overwrite_existing_or_current_draft(self):
        panel=self.panel; panel.load_demo(); path=self.folder/'network.sanet.json'
        self.assertTrue(panel.write(path))
        changed=n.load_project(path); changed['name']='External change'; n.save_project(path,changed)
        external=path.read_bytes(); panel.name.setText('Unsaved local change')
        with patch.object(QMessageBox,'warning'): self.assertFalse(panel.save())
        self.assertEqual(path.read_bytes(),external)
        malformed=self.folder/'bad.json'; malformed.write_text('{"schema_version":999}',encoding='utf-8')
        current=panel.snapshot()
        with patch.object(QFileDialog,'getOpenFileName',return_value=(str(malformed),'')),patch.object(QMessageBox,'warning'):
            self.assertFalse(panel.open())
        self.assertEqual(panel.snapshot(),current)

    def test_close_cancel_preserves_draft_and_discard_actually_discards(self):
        panel=self.panel; panel.load_demo(); self.dialog.show(); app.processEvents()
        with patch.object(QMessageBox,'question',return_value=QMessageBox.StandardButton.Cancel):
            self.assertFalse(self.dialog.close())
        self.assertTrue(panel.has_changes()); self.assertEqual(len(panel.project['points']),4)
        with patch.object(QMessageBox,'question',return_value=QMessageBox.StandardButton.Discard):
            self.assertTrue(self.dialog.close())
        self.assertFalse(panel.has_changes()); self.assertFalse(panel.project['points'])
        self.assertFalse(self.settings.contains(panel.RECOVERY))

    def test_unload_recovery_restores_valid_records_and_unfinished_metadata(self):
        plugin=SurveyAdjustmentPlugin(Iface()); plugin.initGui(); plugin.run()
        panel=plugin.dialog.network_panel; panel.load_demo(); panel.epoch.setText('unfinished epoch')
        expected=deepcopy(panel.project)
        plugin.unload()
        self.assertTrue(self.settings.contains(panel.RECOVERY))
        self.assertTrue(self.panel.recover())
        self.assertEqual(self.panel.project,expected)
        self.assertEqual(self.panel.epoch.text(),'unfinished epoch')
        self.assertIsNone(self.panel.path)
        self.assertFalse(self.panel.check())
        self.panel.epoch.setText('2026-10-18'); self.assertTrue(self.panel.check())
        self.assertTrue(self.panel.write(self.folder/'recovered.sanet.json'))
        self.assertFalse(self.settings.contains(panel.RECOVERY))

    def test_different_unsaved_projects_do_not_overwrite_each_others_recovery(self):
        first=n.synthetic_project(); second=n.synthetic_project(); second['name']='Second draft'
        self.panel.replace_project(first,dirty=True)
        # Represents opening another plugin window while the first has a recovery draft.
        self.panel.replace_project(second,dirty=True)
        self.assertEqual(len(self.panel.recovery_drafts()),2)
        with patch.object(QMessageBox,'question',return_value=QMessageBox.StandardButton.Discard),\
             patch.object(QInputDialog,'getItem',return_value=(first['name']+' ['+first['project_id']+']',True)):
            self.assertTrue(self.panel.recover())
        self.assertEqual(self.panel.project,first)
        self.assertTrue(self.panel.write(self.folder/'first.sanet.json'))
        self.assertFalse(self.settings.contains(self.panel.RECOVERY))

    def test_national_utm_choice_keeps_bergen_negative_easting_in_zone_33(self):
        transform=QgsCoordinateTransform(QgsCoordinateReferenceSystem('EPSG:4326'),QgsCoordinateReferenceSystem('EPSG:25833'),QgsProject.instance())
        xy=transform.transform(QgsPointXY(5.2,60.4))
        self.assertLess(xy.x(),0)
        project=n.new_project(); project.update(crs='EPSG:25833',height_datum='NN2000')
        project['points']=[dict(n.point(),id='Bergen',role='CHECK',coordinates=[xy.x(),xy.y(),81.23456789])]
        self.assertTrue(any('nominal UTM zone' in w for w in n.validate_project(project)))
        self.assertEqual(n.loads_project(n.dumps_project(project)),project)

    def test_corrupt_recovery_is_not_overwritten_when_editing_a_new_project(self):
        self.settings.setValue(self.panel.RECOVERY,'damaged recovery data')
        self.panel.name.setText('New draft'); self.panel.changed()
        self.assertEqual(self.settings.value(self.panel.RECOVERY),'damaged recovery data')
        self.assertIn('could not be stored',self.panel.summary.toPlainText())

    def test_manual_vector_requires_documented_covariance_and_keeps_component_correlations(self):
        project=n.synthetic_project()
        editor=RecordEditor(self.panel,'observations',project)
        editor.identifier.setText('V01'); editor.kind.setCurrentIndex(editor.kind.findData('GNSS_VECTOR'))
        editor.origin.setCurrentIndex(editor.origin.findData('REF0')); editor.target.setCurrentIndex(editor.target.findData('P01'))
        for field,value in zip(editor.values,[1.123456789012345,2,3]): field.setText(str(value))
        for field,value in zip(editor.sigmas,[.01,.02,.03]): field.setText(str(value))
        editor.frame.setCurrentIndex(editor.frame.findData('EPSG:4936'))
        self.assertTrue(all(field.text()=='' for field in editor.correlations))
        editor.submit(); self.assertNotEqual(editor.result(),QDialog.DialogCode.Accepted)
        for field,value in zip(editor.correlations,[.2,-.1,.15]): field.setText(str(value))
        editor.submit(); self.assertEqual(editor.result(),QDialog.DialogCode.Accepted,editor.error.text())
        self.assertEqual(editor.record['values'],[1.123456789012345,2,3])
        self.assertEqual(editor.record['covariance'],n.covariance_from_sigmas([.01,.02,.03],[.2,-.1,.15]))
        editor.show(); app.processEvents(); editor.grab().save(str(ROOT/'test_output/qgis_network_vector_editor.png'))
        editor.close(); editor.deleteLater()

    def test_saving_one_project_keeps_the_other_unsaved_recovery(self):
        first=n.synthetic_project(); second=n.synthetic_project()
        self.panel.replace_project(first,dirty=True)
        self.assertTrue(self.panel.stash_recovery())
        self.panel.replace_project(second,dirty=True)
        self.assertTrue(self.panel.write(self.folder/'second.sanet.json'))
        self.assertEqual(set(self.panel.recovery_drafts()),{first['project_id']})

    def test_unload_aborts_before_deleting_draft_if_recovery_storage_fails(self):
        plugin=SurveyAdjustmentPlugin(Iface()); plugin.initGui(); plugin.run()
        plugin.dialog.network_panel.load_demo()
        with patch.object(plugin.dialog.network_panel,'stash_recovery',return_value=False),self.assertRaisesRegex(RuntimeError,'remains active'):
            plugin.unload()
        self.assertEqual(plugin.iface.actions,['Survey Adjustment'])
        self.assertTrue(plugin.dialog.network_panel.has_changes())
        self.assertFalse(getattr(plugin.dialog,'_unloading',False))
        plugin.unload()

    def test_reopening_and_saving_point_without_edits_keeps_precision_and_readable_sigmas(self):
        project=n.synthetic_project(); original=deepcopy(project['points'][-1])
        original['coordinates'][2]=81.2345678912345
        project['points'][-1]=deepcopy(original)
        editor=RecordEditor(self.panel,'points',project,original)
        self.assertEqual(editor.sigmas[2].text(),'0.03')
        self.assertEqual(editor.coordinates[2].text(),'81.2345678912345')
        editor.submit()
        self.assertEqual(editor.result(),QDialog.DialogCode.Accepted,editor.error.text())
        self.assertEqual(editor.record,original)
        editor.deleteLater()


if __name__=='__main__': unittest.main(verbosity=2)
