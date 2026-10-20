# SPDX-License-Identifier: GPL-3.0-only
from copy import deepcopy
from pathlib import Path
import tempfile,time,unittest
from unittest.mock import patch
from test_qgis import app,ROOT,Iface
from test_network_quality import options,PROGRAM
from test_baselines import options as import_options,source as baseline_source
from survey_adjustment import baselines as b
from qgis.PyQt.QtCore import QSettings,QCoreApplication,QEvent,Qt,QPoint
from qgis.PyQt.QtTest import QTest
from qgis.PyQt.QtWidgets import QFileDialog
from qgis.core import QgsProject,QgsPointXY,QgsVectorLayer,QgsCoordinateReferenceSystem
from survey_adjustment import network as n,network_quality as q,network_map as m
from survey_adjustment.plugin import SurveyDialog
from survey_adjustment.network_map_ui import NetworkMapDialog
from survey_adjustment.network_quality_ui import NetworkQualityDialog


class NetworkMapQgisTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'test_output'); self.folder=Path(self.temp.name)
        self.settings=QSettings(str(self.folder/'settings.ini'),QSettings.Format.IniFormat)
        self.settings_patch=patch('survey_adjustment.network_ui.QSettings',return_value=self.settings)
        mocked=self.settings_patch.start(); mocked.Status=QSettings.Status
        self.host=SurveyDialog(Iface()); self.panel=self.host.network_panel
        self.project=n.load_project(ROOT/'survey_adjustment/examples/gnss_quality.sanet.json')
        self.panel.replace_project(self.project); self.report=q.analyse(self.project,options())
        self.dialog=NetworkMapDialog(self.host,self.project,self.report,self.host,str(PROGRAM))
        self.initial_layers=set(QgsProject.instance().mapLayers())

    def tearDown(self):
        self.dialog.reject(); self.dialog.deleteLater()
        self.host._unloading=True; self.host.close(); self.host.deleteLater()
        QgsProject.instance().removeMapLayers(list(set(QgsProject.instance().mapLayers())-self.initial_layers))
        for node in list(QgsProject.instance().layerTreeRoot().children()):
            if node.name().startswith('Survey network ') and not node.children(): QgsProject.instance().layerTreeRoot().removeChildNode(node)
        app.processEvents(); QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        self.settings_patch.stop(); self.temp.cleanup()

    def show(self):
        self.dialog.show(); app.processEvents(); self.dialog.fit(); app.processEvents()
        deadline=time.monotonic()+3
        while self.dialog.canvas.isDrawing() and time.monotonic()<deadline: app.processEvents()

    def test_preview_labels_and_selection_linked_both_directions(self):
        before=deepcopy(self.project); self.show()
        self.assertIn('PREVIEW',self.dialog.banner.text()); self.assertIn('DISPLAY ONLY',self.dialog.banner.text())
        self.assertFalse(self.dialog.compare_button.isEnabled())
        self.assertEqual(self.dialog.point_table.rowCount(),5); self.assertEqual(self.dialog.observation_table.rowCount(),8)
        point=next(p for p in self.report['points'] if p['id']=='P01')['position_grid']
        pixel=self.dialog.canvas.getCoordinateTransform().transform(QgsPointXY(point[0],point[1]))
        QTest.mouseClick(self.dialog.canvas.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,QPoint(round(pixel.x()),round(pixel.y())))
        app.processEvents(); self.assertEqual(self.dialog.selected,('point','P01'))
        self.assertIn('Original record',self.dialog.detail.toPlainText()); self.assertIn('σU',self.dialog.detail.toPlainText())
        self.dialog.observation_table.selectRow(0); app.processEvents()
        self.assertEqual(self.dialog.selected,('observation','V01'))
        self.assertEqual(self.dialog.layers['baselines'].selectedFeatureCount(),1)
        self.dialog.zoom_selected(); self.assertTrue(self.dialog.canvas.extent().isFinite())
        self.assertEqual(self.project,before); self.assertEqual(self.panel.snapshot(),before)
        self.assertEqual(set(QgsProject.instance().mapLayers()),self.initial_layers)
        self.assertTrue(self.dialog.grab().save(str(ROOT/'test_output/qgis_network_map_060_preview.png')))

    def test_scale_visibility_and_original_coordinates_remain_independent(self):
        before=[f.geometry().asWkt(17) for f in self.dialog.layers['points'].getFeatures()]
        self.dialog.select_record('point','P02')
        self.dialog.ellipse_factor.setCurrentIndex(4); self.dialog.vector_factor.setCurrentIndex(0)
        self.assertEqual(self.dialog.selected,('point','P02')); self.assertIn('×1000',self.dialog.banner.text())
        self.assertEqual(before,[f.geometry().asWkt(17) for f in self.dialog.layers['points'].getFeatures()])
        self.dialog.visibility['ellipses'].setChecked(False)
        self.assertNotIn(self.dialog.layers['ellipses'],self.dialog.canvas.layers())
        self.dialog.visibility['sources'].setChecked(True)
        self.assertIn(self.dialog.layers['sources'],self.dialog.canvas.layers())

    def test_map_only_after_model_available_and_stale_owner_blocked(self):
        quality=NetworkQualityDialog(self.panel,self.project,self.host)
        self.assertFalse(quality.map_button.isEnabled()); self.assertFalse(quality.open_map())
        for field in (quality.frame_review,quality.endpoint_review,quality.independence_review): field.setChecked(True)
        quality.note.setText('SYNTHETIC independent training blocks and reviewed endpoint/frame assumptions')
        self.assertTrue(quality.preview()); self.assertTrue(quality.map_button.isEnabled())
        with patch('survey_adjustment.network_map_ui.NetworkMapDialog.exec',return_value=0): self.assertTrue(quality.open_map())
        changed=deepcopy(self.project); changed['notes']+=' Changed owner'; self.panel.replace_project(changed)
        self.assertFalse(quality.open_map()); self.assertIn('Network changed',quality.status.text())
        quality.horizontal.setText('1'); self.assertFalse(quality.map_button.isEnabled()); quality.reject(); quality.deleteLater()

    def test_add_layers_preserves_existing_project_and_uses_separate_group(self):
        existing=QgsVectorLayer('Point?crs=EPSG:4326','Existing user layer','memory'); QgsProject.instance().addMapLayer(existing)
        try:
            self.assertTrue(self.dialog.add_to_qgis(),self.dialog.message.text())
            self.assertIn(existing.id(),QgsProject.instance().mapLayers())
            self.assertIsNotNone(self.dialog.added_group)
            self.assertEqual(len(self.dialog.added_group.findLayers()),6)
            self.assertEqual(self.dialog.added_group.customProperty('survey_adjustment/snapshot_sha256'),self.report['project_sha256'])
            for node in self.dialog.added_group.findLayers(): self.assertTrue(node.layer().readOnly())
        finally: QgsProject.instance().removeMapLayer(existing.id())

    def test_cancelled_compare_and_export_write_nothing(self):
        with patch.object(QFileDialog,'getExistingDirectory',return_value=''):
            self.assertFalse(self.dialog.save_package())
        with patch.object(QFileDialog,'getOpenFileName',return_value=('','')):
            self.assertFalse(self.dialog.load_comparison())
        self.assertIsNone(self.dialog.last_export); self.assertIsNone(self.dialog.comparison)

    def test_main_canvas_extent_uses_crs_after_first_layer_initialization(self):
        canvas=self.host.iface.mapCanvas(); canvas.resize(800,600)
        canvas.setDestinationCrs(QgsCoordinateReferenceSystem('EPSG:4326'))
        def initialize_first_layer(_): canvas.setDestinationCrs(QgsCoordinateReferenceSystem('EPSG:25832'))
        QgsProject.instance().layersAdded.connect(initialize_first_layer)
        try:
            self.assertTrue(self.dialog.add_to_qgis(),self.dialog.message.text())
            expected=m.extent(self.dialog.layers).center(); actual=canvas.extent().center()
            self.assertAlmostEqual(actual.x(),expected.x(),places=6)
            self.assertAlmostEqual(actual.y(),expected.y(),places=6)
        finally: QgsProject.instance().layersAdded.disconnect(initialize_first_layer)

    def test_local_map_package_export_action(self):
        with patch.object(QFileDialog,'getExistingDirectory',return_value=str(self.folder)):
            self.assertTrue(self.dialog.save_package(),self.dialog.message.text())
        self.assertTrue((self.dialog.last_export/'network_map.gpkg').is_file())
        self.assertTrue((self.dialog.last_export/'map_manifest.json').is_file())

    def test_imported_baseline_provenance_is_available_without_source_changes(self):
        project=deepcopy(self.project)
        # Construct a test model with a free endpoint/session slot; the real
        # import duplicate guard also protects disabled observations.
        project['observations']=[o for o in project['observations'] if o['id']!='V01']
        src=baseline_source(); project=b.plan_import(project,src,src['rows'][0]['line'],import_options())
        before=deepcopy(project); report=q.analyse(project,options())
        self.dialog.reject(); self.dialog.deleteLater()
        self.dialog=NetworkMapDialog(self.host,project,report,self.host,str(PROGRAM))
        self.dialog.select_record('observation','B1')
        text=self.dialog.detail.toPlainText()
        self.assertIn('Original import provenance',text); self.assertIn(src['source_sha256'],text)
        self.assertEqual(self.dialog.project,before)

    @unittest.skipUnless(PROGRAM.exists(),'Local SALSA engine unavailable')
    def test_verified_map_and_saved_run_comparison_flow(self):
        previous=q.run_network(PROGRAM,self.folder/'previous',self.project,options())
        changed=deepcopy(self.project); changed['observations'][0].update(enabled=False,notes='SYNTHETIC excluded for comparison')
        report=q.run_network(PROGRAM,self.folder/'current',changed,options())
        self.dialog.reject(); self.dialog.deleteLater()
        self.dialog=NetworkMapDialog(self.host,changed,report,self.host,str(PROGRAM)); self.show()
        self.assertTrue(self.dialog.compare_button.isEnabled())
        with patch.object(QFileDialog,'getOpenFileName',return_value=(str(self.folder/'previous/quality.json'),'')):
            self.assertTrue(self.dialog.load_comparison(),self.dialog.message.text())
        self.assertEqual(self.dialog.comparison_table.rowCount(),5); self.assertIn('comparison',self.dialog.layers)
        self.dialog.excluded.setChecked(False)
        self.assertEqual(self.dialog.layers['baselines'].featureCount(),7)
        self.dialog.select_record('point','P01'); self.assertIn('Current minus comparison',self.dialog.detail.toPlainText())
        self.assertTrue(self.dialog.grab().save(str(ROOT/'test_output/qgis_network_map_060_verified.png')))
        self.dialog.clear_comparison(); self.assertIsNone(self.dialog.comparison); self.assertNotIn('comparison',self.dialog.layers)


if __name__=='__main__': unittest.main(verbosity=2)
