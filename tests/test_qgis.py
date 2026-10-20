# SPDX-License-Identifier: GPL-3.0-only
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import hashlib
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from qgis.core import QgsApplication,QgsProject,Qgis
from qgis.gui import QgsMapCanvas
from qgis.PyQt.QtWidgets import QMainWindow,QMessageBox,QFileDialog
from qgis.PyQt.QtCore import QProcess
from qgis.PyQt.QtGui import QFontDatabase,QFont
from survey_adjustment.plugin import SurveyAdjustmentPlugin,SurveyDialog

app = QgsApplication([],True)
app.initQgis()
font_id = QFontDatabase.addApplicationFont(r'C:\Windows\Fonts\segoeui.ttf')
if font_id>=0: app.setFont(QFont(QFontDatabase.applicationFontFamilies(font_id)[0],10))
PROGRAM = ROOT/'upstream/extracted/SALSA/bin/lsasolver.exe'


class Iface:
    def __init__(self): self.window,self.canvas,self.actions = QMainWindow(),QgsMapCanvas(),[]
    def mainWindow(self): return self.window
    def mapCanvas(self): return self.canvas
    def setActiveLayer(self,layer): self.active = layer
    def addPluginToMenu(self,menu,action): self.actions.append(menu)
    def removePluginMenu(self,menu,action): self.actions.remove(menu)


class QgisTests(unittest.TestCase):
    def setUp(self): self.iface = Iface()
    def tearDown(self): QgsProject.instance().clear()

    def dialog(self):
        dlg = SurveyDialog(self.iface)
        dlg.load_demo()
        dlg.confirm.setChecked(True)
        return dlg

    def test_full_csv_control_map_layers_report_and_stale_state(self):
        hashes = {p:hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/'survey_adjustment/examples').glob('*.csv')}
        dlg = self.dialog()
        self.assertTrue(dlg.calculate())
        self.assertEqual(dlg.result['used_count'],9)
        self.assertEqual(dlg.results.rowCount(),4)
        dlg.add_layers()
        layers = list(QgsProject.instance().mapLayers().values())
        self.assertEqual(sorted(l.featureCount() for l in layers),[4,4,9,9])
        points = next(l for l in layers if 'actual XYZ' in l.name())
        obs = dlg.result['observations'][0]
        first = next(points.getFeatures()).geometry().constGet()
        self.assertEqual((first.x(),first.y(),first.z()),(obs['e'],obs['n'],obs['z']))
        self.assertTrue(points.readOnly())
        single = next(f for f in points.getFeatures() if f['control_id']=='KP04')
        self.assertEqual(single['status'],'INCONCLUSIVE')
        group = QgsProject.instance().layerTreeRoot().children()[0]
        self.assertIn('means',group.children()[0].layer().name())
        with tempfile.TemporaryDirectory(dir=ROOT/'test_output') as folder:
            with patch.object(QFileDialog,'getExistingDirectory',return_value=folder),patch.object(QMessageBox,'information'):
                dlg.save_results()
            self.assertEqual(len(list(Path(folder).glob('*/check.json'))),1)
        dlg.grab().save(str(ROOT/'test_output/qgis_gjentak.png'))
        dlg.h_tol.setValue(.01)
        self.assertIsNone(dlg.result)
        self.assertEqual(dlg.results.rowCount(),0)
        for p,digest in hashes.items(): self.assertEqual(hashlib.sha256(p.read_bytes()).hexdigest(),digest)
        dlg.close()

    def test_frame_metadata_mismatch_missing_confirmation_and_group_mapping(self):
        dlg = self.dialog()
        dlg.confirm.setChecked(False)
        with patch.object(QMessageBox,'warning') as warning:
            self.assertFalse(dlg.calculate())
            self.assertTrue(warning.called)
        dlg.crs.setCurrentIndex(2)
        dlg.confirm.setChecked(True)
        with patch.object(QMessageBox,'warning'):
            self.assertFalse(dlg.calculate())
        dlg.crs.setCurrentIndex(1)
        dlg.confirm.setChecked(True)
        combo = dlg.mapping.cellWidget(1,1)
        combo.setCurrentIndex(combo.findData('Name'))
        self.assertTrue(dlg.calculate())
        self.assertTrue(all(g['count']==1 for g in dlg.result['groups']))
        self.assertTrue(all(g['status']!='WITHIN TOLERANCE' for g in dlg.result['groups']))
        dlg.close()

    @unittest.skipUnless(PROGRAM.exists(),'Local SALSA engine unavailable')
    def test_real_salsa_from_qt_process_not_blocking_qgis(self):
        dlg = self.dialog()
        dlg.solver.setText(str(PROGRAM))
        with tempfile.TemporaryDirectory(dir=ROOT/'test_output') as folder:
            with patch.object(QFileDialog,'getExistingDirectory',return_value=folder): dlg.run_engine()
            deadline = time.monotonic()+15
            while not dlg.engine_run.isEnabled() and time.monotonic()<deadline:
                app.processEvents()
                time.sleep(.01)
            self.assertTrue(dlg.engine_run.isEnabled(),dlg.engine_log.toPlainText())
            self.assertIsNotNone(dlg.engine_result,dlg.engine_log.toPlainText())
            self.assertLess(dlg.engine_result['maximum_difference_metres'],.000002)
            self.assertTrue((dlg.engine_folder/'run_report.json').exists())
            dlg.tabs.setCurrentIndex(2)
            dlg.grab().save(str(ROOT/'test_output/qgis_salsa.png'))
        dlg.close()

    def test_independent_menu_lifecycle(self):
        plugin = SurveyAdjustmentPlugin(self.iface)
        plugin.initGui()
        self.assertEqual(self.iface.actions,['Survey Adjustment'])
        plugin.run()
        from survey_adjustment.version import VERSION
        self.assertIn(VERSION,plugin.dialog.windowTitle())
        plugin.unload()
        self.assertEqual(self.iface.actions,[])

    @unittest.skipUnless(PROGRAM.exists(),'Local SALSA engine unavailable')
    def test_own_coordinate_panel_process_exports_map_z_and_invalidation(self):
        dlg=SurveyDialog(self.iface); panel=dlg.coordinate_panel
        panel.load_demo(); dlg.tabs.setCurrentIndex(3); dlg.show(); app.processEvents()
        with patch.object(QMessageBox,'warning') as warning:
            self.assertFalse(panel.preview()); self.assertTrue(warning.called)
        for box in (panel.ellipsoid,panel.uncertainty_confirm,panel.independent): box.setChecked(True)
        self.assertTrue(panel.preview())
        dlg.grab().save(str(ROOT/'test_output/qgis_coordinate_setup.png'))
        dlg.solver.setText(str(PROGRAM))
        with tempfile.TemporaryDirectory(dir=ROOT/'test_output') as folder:
            with patch.object(QFileDialog,'getExistingDirectory',return_value=folder): panel.run()
            deadline=time.monotonic()+15
            while not dlg.engine_run.isEnabled() and time.monotonic()<deadline:
                app.processEvents(); time.sleep(.01)
            self.assertIsNotNone(dlg.engine_result,dlg.engine_log.toPlainText())
            self.assertTrue(dlg.engine_result['synthetic'])
            self.assertEqual(dlg.engine_result['kind'],'coordinate_observations')
            panel.add_layer()
            layer=self.iface.active
            self.assertEqual(layer.crs().authid(),'EPSG:4937'); self.assertTrue(layer.readOnly())
            first=next(layer.getFeatures()); lat,lon,h=next(iter(dlg.engine_result['positions_geodetic'].values()))
            point=first.geometry().constGet()
            self.assertEqual((point.x(),point.y(),point.z()),(lon,lat,h))
            self.assertEqual(first['height_datum'],'ELLIP')
            dlg.tabs.setCurrentIndex(2); dlg.grab().save(str(ROOT/'test_output/qgis_coordinate_result.png'))
            panel.epoch.setText('2026-10-19'); self.assertFalse(panel.map_button.isEnabled())
        dlg.close()


if __name__=='__main__':
    (ROOT/'test_output').mkdir(exist_ok=True)
    print('QGIS',Qgis.QGIS_VERSION,flush=True)
    unittest.main(verbosity=2)
