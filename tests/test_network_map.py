# SPDX-License-Identifier: GPL-3.0-only
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import hashlib,json,math,tempfile,unittest
import numpy as np
from test_qgis import app,ROOT
from test_network_quality import options,PROGRAM
from survey_adjustment import network as n,network_quality as q,network_map as m,baselines as b
from qgis.core import QgsVectorLayer,QgsWkbTypes
from osgeo import ogr
ogr.UseExceptions()


class NetworkMapTests(unittest.TestCase):
    def setUp(self):
        self.project=n.load_project(ROOT/'survey_adjustment/examples/gnss_quality.sanet.json')
        self.report=q.analyse(self.project,options())

    def test_ellipse_analytic_axes_orientation_and_confidence_boundary(self):
        theta=math.radians(30); rotate=np.array([[math.sin(theta),math.cos(theta)],[math.cos(theta),-math.sin(theta)]])
        horizontal=rotate@np.diag([.0009,.0004])@rotate.T
        covariance=np.zeros((3,3)); covariance[:2,:2]=horizontal; covariance[2,2]=.0025
        axes,angle,vectors=m.ellipse_parameters(covariance)
        np.testing.assert_allclose(axes,np.sqrt(5.991464547107979*np.array([.0009,.0004])),rtol=1e-12)
        self.assertAlmostEqual(angle,30,places=10)
        center=[291000.,6701000.,81.123456789]
        geometry=m.ellipse_geometry(center,axes,vectors,500.)
        vertices=list(geometry.vertices()); self.assertEqual(len(vertices),97)
        self.assertEqual(vertices[0],vertices[-1]); self.assertTrue(geometry.isGeosValid())
        for point in vertices:
            delta=(np.array([point.x(),point.y()])-center[:2])/500.
            self.assertAlmostEqual(float(delta@np.linalg.solve(horizontal,delta)),5.991464547107979,places=7)
            self.assertEqual(point.z(),center[2])
        with self.assertRaises(ValueError): m.ellipse_parameters(np.diag([-.1,.2,.3]))

    def test_projected_covariance_includes_grid_convergence_and_scale(self):
        from pyproj import Proj
        grid=q.GridCoordinates(self.project['crs']); position=grid.ecef([291000.,6701000.,80.])
        lat,lon,height=m.llh(position,'EPSG:4937'); transform=np.array(b.enu_to_ecef(lat,lon))
        enu=np.array([[.0001,.00003,.00002],[.00003,.0009,-.00001],[.00002,-.00001,.0016]])
        converted=m.grid_covariance(grid,position,transform@enu@transform.T)
        factors=Proj(self.project['crs']).get_factors(lon,lat)
        gamma=math.radians(factors.meridian_convergence); k=factors.meridional_scale
        # PROJ factors are on the ellipsoid. An ECEF tangent displacement at
        # height h must also be reduced to the ellipsoid (N/(N+h), M/(M+h)).
        f=1/298.257222101; e2=f*(2-f); w=1-e2*math.sin(math.radians(lat))**2
        prime=6378137/math.sqrt(w); meridian=6378137*(1-e2)/w**1.5
        east_scale=k*prime/(prime+height); north_scale=k*meridian/(meridian+height)
        rotation=np.array([[east_scale*math.cos(gamma),-north_scale*math.sin(gamma),0],
            [east_scale*math.sin(gamma),north_scale*math.cos(gamma),0],[0,0,1]])
        np.testing.assert_allclose(converted,rotation@enu@rotation.T,rtol=2e-6,atol=2e-12)
        self.assertGreater(abs(converted[0,0]-enu[0,0]),1e-7)

    def test_actual_xyz_and_full_observations_retained_display_scaling_only(self):
        before=deepcopy(self.project); report_before=deepcopy(self.report)
        actual,_=m.make_map_layers(self.project,self.report,m.MapOptions(1,1))
        enlarged,_=m.make_map_layers(self.project,self.report,m.MapOptions(500,1000))
        self.assertEqual(self.project,before); self.assertEqual(self.report,report_before)
        points={f['record_id']:f for f in enlarged['points'].getFeatures()}
        for row in self.report['points']:
            if row['position_grid'] is not None:
                point=points[row['id']].geometry().constGet()
                self.assertEqual([point.x(),point.y(),point.z()],row['position_grid'])
        original={f['record_id']:f for f in enlarged['sources'].getFeatures()}
        for row in self.project['points']:
            if row['coordinates'] is not None:
                point=original[row['id']].geometry().constGet()
                self.assertEqual([point.x(),point.y(),point.z()],row['coordinates'])
        for key in ('points','baselines','sources'):
            self.assertEqual([f.geometry().asWkt(17) for f in actual[key].getFeatures()],
                             [f.geometry().asWkt(17) for f in enlarged[key].getFeatures()])
        small={f['record_id']:f for f in actual['ellipses'].getFeatures()}
        for feature in enlarged['ellipses'].getFeatures():
            self.assertEqual(feature['h95_grid_major_m'],small[feature['record_id']]['h95_grid_major_m'])
            self.assertAlmostEqual(feature.geometry().area()/small[feature['record_id']].geometry().area(),500**2,delta=.1)
        self.assertEqual(enlarged['ellipses'].featureCount(),3)
        for feature,source in zip(enlarged['baselines'].getFeatures(),self.project['observations']):
            self.assertEqual(json.loads(feature['observed_values']),source['values'])
            self.assertEqual(json.loads(feature['covariance']),source['covariance'])
        self.assertTrue(all(QgsWkbTypes.hasZ(layer.wkbType()) for layer in enlarged.values()))
        self.assertTrue(all(layer.readOnly() for layer in enlarged.values()))

    def test_disabled_checks_and_unlocated_records_are_explicit(self):
        self.project['observations'][0].update(enabled=False,notes='SYNTHETIC review exclusion')
        self.project['points'].append(dict(n.point(),id='UNLOCATED'))
        report=q.analyse(self.project,options()); layers,missing=m.make_map_layers(self.project,report)
        states={f['record_id']:f['status'] for f in layers['baselines'].getFeatures()}
        self.assertEqual(states['V01'],'DISABLED'); self.assertEqual(states['V08'],'CHECK')
        self.assertTrue(any('UNLOCATED' in value for value in missing))
        self.assertEqual(layers['points'].featureCount(),5)

    def test_snapshot_mismatch_and_invalid_display_options_are_rejected(self):
        changed=deepcopy(self.project); changed['epoch']='2026-10-17'
        with self.assertRaises(ValueError): m.make_map_layers(changed,self.report)
        changed=deepcopy(self.report); changed['points'][1]['position_grid'][2]+=1
        with self.assertRaises(ValueError): m.make_map_layers(self.project,changed)
        for value in (0,-1,float('nan'),True,100001):
            with self.subTest(value=value),self.assertRaises(ValueError): m.make_map_layers(self.project,self.report,m.MapOptions(value,1))

    def test_export_geopackage_readback_z_precision_and_manifest(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'test_output') as temporary:
            folder=m.export_package(temporary,self.project,self.report,m.MapOptions(100,500))
            manifest=json.loads((folder/'map_manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(manifest['stage'],'PREVIEW - SALSA NOT RUN')
            self.assertIn('DISPLAY ONLY',manifest['meaning']); self.assertIn('AS IS',manifest['notice'])
            self.assertEqual(n.load_project(folder/'network.sanet.json'),self.project)
            for name,checksum in manifest['files'].items():
                self.assertEqual(hashlib.sha256((folder/name).read_bytes()).hexdigest(),checksum)
            # Read the written file independently through OGR. QGIS providers
            # keep pooled read handles on Windows; this explicitly closes its
            # dataset before removing the temporary test package.
            dataset=ogr.Open(str(folder/'network_map.gpkg'),0)
            self.assertIsNotNone(dataset)
            try:
                for name in ('points','sources','baselines','ellipses','residuals','changes'):
                    layer=dataset.GetLayerByName(name); self.assertIsNotNone(layer,name)
                    values={}
                    for feature in layer:
                        geometry=feature.GetGeometryRef(); self.assertEqual(geometry.GetCoordinateDimension(),3)
                        if name=='points': values[feature.GetField('record_id')]=geometry.GetZ()
                    if name=='points':
                        for point in self.report['points']:
                            if point['position_grid'] is not None: self.assertEqual(values[point['id']],point['position_grid'][2])
                    self.assertTrue((folder/(name+'.qml')).is_file())
            finally:
                geometry=None; feature=None; layer=None; dataset=None

    @unittest.skipUnless(PROGRAM.exists(),'Local SALSA engine unavailable')
    def test_read_only_verified_run_comparison_and_tamper_rejection(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'test_output') as temporary:
            base=q.run_network(PROGRAM,Path(temporary)/'base',self.project,options())
            before={p.name:p.read_bytes() for p in (Path(temporary)/'base').iterdir() if p.is_file()}
            comparison=m.load_comparison(Path(temporary)/'base/quality.json',PROGRAM)
            self.assertEqual(before,{p.name:p.read_bytes() for p in (Path(temporary)/'base').iterdir() if p.is_file()})
            changed=deepcopy(self.project); changed['observations'][0].update(enabled=False,notes='SYNTHETIC exclusion for comparison')
            report=q.run_network(PROGRAM,Path(temporary)/'changed',changed,options())
            rows=m.compare_points(changed,report,comparison)
            p=next(r for r in rows if r['id']=='P01')
            np.testing.assert_allclose(p['delta_grid_m'],np.array(report['points'][2]['position_grid'])-base['points'][2]['position_grid'],rtol=0,atol=0)
            self.assertGreater(math.dist(p['delta_grid_m'],[0,0,0]),.00001)
            self.assertIsNone(next(r for r in rows if r['id']=='CHECK1')['delta_grid_m'])
            layers,_=m.make_map_layers(changed,report,m.MapOptions(),comparison)
            self.assertGreater(layers['comparison'].featureCount(),0)
            wrong=deepcopy(comparison); wrong['epoch']='2026-10-17'
            with self.assertRaises(ValueError): m.check_comparison(changed,report,wrong)
            with self.assertRaises(ValueError): m.check_comparison(self.project,self.report,comparison)
            source=Path(temporary)/'base/quality.json'; source.write_bytes(source.read_bytes()+b' ')
            with self.assertRaisesRegex(ValueError,'changed'): m.load_comparison(source,PROGRAM)


if __name__=='__main__': unittest.main(verbosity=2)
