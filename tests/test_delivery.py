# SPDX-License-Identifier: GPL-3.0-only
from copy import deepcopy
from dataclasses import replace
import json,shutil,subprocess,tempfile,unittest
from pathlib import Path
from test_qgis import app,ROOT,PROGRAM
from test_mixed_survey import job
from test_network_quality import options
from survey_adjustment import delivery as d, mixed_survey as m, network_quality as q, engine

MODEL_ROOT=ROOT/'upstream/reference_models_080'
PATHS={h:MODEL_ROOT/v[0] for h,v in d.MODELS.items()}


class GlobalReferenceTests(unittest.TestCase):
    def test_missing_or_modified_models_cannot_fall_back(self):
        with self.assertRaisesRegex(ValueError,'reviewed local'):d.GlobalReferenceOperation({})
        with tempfile.TemporaryDirectory(dir=ROOT/'test_output') as temporary:
            path=Path(temporary)/'wrong.tif';path.write_bytes(b'SYNTHETIC invalid grid')
            with self.assertRaisesRegex(ValueError,'differs'):d.GlobalReferenceOperation({name:path for name in d.GlobalReferenceOperation.GRIDS})

    def test_actual_official_operation_epoch_and_national_realisation(self):
        from pyproj import datadir
        from pyproj.transformer import TransformerGroup,AreaOfInterest
        from survey_adjustment.coordinate_job import xyz
        import math
        paths={name:Path(datadir.get_data_dir())/name for name in d.GlobalReferenceOperation.GRIDS}
        if not all(path.is_file() for path in paths.values()):self.skipTest('Reviewed official reference grids intentionally not bundled')
        operation=d.GlobalReferenceOperation(paths)
        # Constructed point only; no actual station/customer coordinate fixture.
        point=xyz(60.4,5.2,80.,'EPSG:9989')
        group=TransformerGroup('EPSG:9988','EPSG:10873',always_xy=True,area_of_interest=AreaOfInterest(4,58,6,61),allow_ballpark=False)
        official=next(item for item in group.transformers if item.description=='ITRF2020 to ETRS89-NOR [EUREF89] (1)')
        result=operation.transform(point,2026.5)
        self.assertLess(math.dist(result,official.transform(*point,2026.5,errcheck=True)[:3]),1e-8)
        self.assertGreater(math.dist(result,point),.2)
        self.assertGreater(math.dist(result,operation.transform(point,2020.5)),.05)
        self.assertEqual(operation.metadata()['fallback'],'NONE')
        self.assertIn('NOT_PROPAGATED',operation.metadata()['covariance'])
        for epoch in (None,float('nan'),1900.,9999.):
            with self.subTest(epoch=epoch),self.assertRaises(ValueError):operation.transform(point,epoch)


def review(**changes):
    return replace(d.ReferenceReview(source_height='ELLIP',target_height='ELLIP',target_crs='EPSG:25832',
        reviewer='Synthetic test reviewer',recipient='Constructed local test specification',purpose='Synthetic software validation only',
        frame_evidence='Constructed EUREF89 geometry, no field data.',epoch_evidence='Explicit synthetic coordinate epoch, distinct from acquisition timestamps.',
        height_evidence='Constructed ellipsoidal mark heights; no antenna correction inferred.',
        uncertainty_review='Independent synthetic observation blocks. Model/transform sigma not supplied; output sigma must remain unreported.',
        change_log='No manual changes. Original snapshot and exclusions retained.',reviewed=True),**changes)


def create_run(folder,profile='MIXED_3D'):
    m.prepare(folder,job(profile),PROGRAM)
    result=subprocess.run(q.network_command(PROGRAM),cwd=folder,env=engine.environment(PROGRAM),capture_output=True,
                          creationflags=subprocess.CREATE_NO_WINDOW if __import__('os').name=='nt' else 0,timeout=60)
    (folder/'console.txt').write_bytes(result.stdout+b'\n'+result.stderr)
    m.inspect_result(folder,result.returncode,PROGRAM)
    return d.load_run(folder/'quality.json',PROGRAM)


@unittest.skipUnless(PROGRAM.exists() and all(p.exists() for p in PATHS.values()),'Separate SALSA and reviewed local HREF grids required; see docs/DELIVERY.txt')
class DeliveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory(dir=ROOT/'test_output'); cls.root=Path(cls.tmp.name)
        cls.mixed_run=create_run(cls.root/'mixed')
        cls.level=create_run(cls.root/'level','LEVEL_1D')
        project=q.synthetic_quality_project(); q.run_network(PROGRAM,cls.root/'network',project,options())
        cls.network=d.load_run(cls.root/'network/quality.json',PROGRAM)

    @classmethod
    def tearDownClass(cls): cls.tmp.cleanup()

    def setUp(self):
        self.tmpcase=tempfile.TemporaryDirectory(dir=self.root); self.case=Path(self.tmpcase.name)

    def tearDown(self): self.tmpcase.cleanup()

    def test_revalidation_is_read_only_and_supports_both_calculation_types(self):
        for run in (self.mixed_run,self.level,self.network):
            before={p.name:(p.read_bytes(),p.stat().st_mtime_ns) for p in run['folder'].iterdir() if p.is_file()}
            loaded=d.load_run(run['folder']/'quality.json',PROGRAM)
            self.assertEqual(loaded['identity'],run['identity'])
            self.assertEqual(before,{p.name:(p.read_bytes(),p.stat().st_mtime_ns) for p in run['folder'].iterdir() if p.is_file()})

    def test_unknown_datum_review_epoch_and_missing_acquisition_fail(self):
        for r in (replace(review(),reviewed=False),replace(review(),epoch_evidence=''),replace(review(),source_height='NN2000'),
                  replace(review(),source_height='NN2000:2025'),replace(review(),target_crs='EPSG:4326')):
            with self.assertRaises(ValueError): d.preview(self.mixed_run,r,{})
        for field in ('epoch','sessions'):
            run=deepcopy({k:v for k,v in self.mixed_run.items() if k!='files'}); run['project'][field]='' if field=='epoch' else []
            with self.assertRaises(ValueError): d.preview(run,review(),{})

    def test_no_conversion_retains_exact_values_and_never_applies_href_twice(self):
        result,op=d.preview(self.mixed_run,review(),{})
        self.assertFalse(op.models)
        for row in result['records']: self.assertEqual(row['adjusted'],row['delivered'])
        result,op=d.preview(self.level,review(source_height='NN2000:2025',target_height='NN2000:2025'),{})
        self.assertFalse(op.models)
        for row in result['records']: self.assertEqual(row['adjusted'],row['delivered'])
        self.assertIn('2026-10-18',result['sessions'][0]['start_utc'])

    def test_missing_wrong_changed_model_and_unsupported_height_change_fail(self):
        with self.assertRaises(ValueError): d.preview(self.mixed_run,review(target_height='NN2000:2025'),{})
        path=self.case/d.MODELS['NN2000:2025'][0]; path.write_bytes(b'Not a reviewed HREF model')
        with self.assertRaisesRegex(ValueError,'bytes'): d.model(path,'NN2000:2025')
        with self.assertRaisesRegex(ValueError,'bytes'): d.model(PATHS['NN2000:2018'],'NN2000:2025')
        with self.assertRaises(ValueError): d.preview(self.mixed_run,review(target_height='NN54'),{})

    def test_official_kartverket_height_benchmarks_and_sign(self):
        # Kartverket EPSG-code guidance: 500000,6600000,100 -> 58.042431;
        # geographic 10E,60N,100 -> 59.4360 using HREF2018B. No field measurements.
        operation=d.CoordinateOperation('EPSG:25832','EPSG:25832','ELLIP','NN2000:2018',PATHS)
        result,offsets=operation.transform([500000.,6600000.,100.])
        self.assertEqual(result[:2],[500000.,6600000.]); self.assertAlmostEqual(result[2],58.042431,places=6)
        from pyproj import Proj
        en=Proj(d.projection('EPSG:25832'))(10.,60.)
        result,_=operation.transform([*en,100.]); self.assertAlmostEqual(result[2],59.43600082397461,places=7)
        self.assertGreater(offsets['NN2000:2018'],0.)

    def test_2025_grid_node_matches_raw_raster_and_model_switch_roundtrips(self):
        from osgeo import gdal
        from pyproj import Proj
        with __import__('warnings').catch_warnings():
            __import__('warnings').simplefilter('ignore',FutureWarning)
            ds=gdal.Open(str(PATHS['NN2000:2025']))
        gt=ds.GetGeoTransform(); x,y=100,550
        lon=gt[0]+(x+.5)*gt[1]; lat=gt[3]+(y+.5)*gt[5]
        raw=float(ds.GetRasterBand(1).ReadAsArray(x,y,1,1)[0,0]); self.assertTrue(__import__('math').isfinite(raw)); ds=None
        en=Proj(d.projection('EPSG:25832'))(lon,lat)
        forward=d.CoordinateOperation('EPSG:25832','EPSG:25832','ELLIP','NN2000:2025',PATHS)
        result,_=forward.transform([*en,100.]); self.assertAlmostEqual(result[2],100.-raw,places=7)
        backward=d.CoordinateOperation('EPSG:25832','EPSG:25832','NN2000:2025','ELLIP',PATHS)
        recovered,_=backward.transform(result); self.assertAlmostEqual(recovered[2],100.,places=10)
        old=d.CoordinateOperation('EPSG:25832','EPSG:25832','ELLIP','NN2000:2018',PATHS).transform([291000.,6701000.,100.])[0]
        new=d.CoordinateOperation('EPSG:25832','EPSG:25832','NN2000:2018','NN2000:2025',PATHS).transform(old)[0]
        direct=forward.transform([291000.,6701000.,100.])[0]
        self.assertEqual(new,direct)

    def test_ntm_projection_matches_independent_qgis_and_no_epoch_shift(self):
        from qgis.core import QgsCoordinateTransform,QgsCoordinateReferenceSystem,QgsProject,QgsPointXY
        point=[291000.,6701000.,80.]
        operation=d.CoordinateOperation('EPSG:25832','EPSG:5105','ELLIP','ELLIP',{})
        actual,_=operation.transform(point)
        transform=QgsCoordinateTransform(QgsCoordinateReferenceSystem('EPSG:25832'),QgsCoordinateReferenceSystem('EPSG:5105'),QgsProject.instance())
        p=transform.transform(QgsPointXY(*point[:2]))
        self.assertAlmostEqual(actual[0],p.x(),places=6); self.assertAlmostEqual(actual[1],p.y(),places=6); self.assertEqual(actual[2],80.)
        self.assertEqual(operation.metadata()['epoch_operation'],'NONE - coordinate epoch retained')

    def test_outside_coverage_swapped_axes_and_nonfinite_are_rejected(self):
        operation=d.CoordinateOperation('EPSG:25832','EPSG:25832','ELLIP','NN2000:2025',PATHS)
        for point in ([6701000.,291000.,80.],[500000.,9000000.,80.],[float('nan'),6701000.,80.]):
            with self.assertRaises(Exception): operation.transform(point)

    def test_export_all_evidence_xyz_readback_and_integrity_without_overwrite(self):
        r=review(target_height='NN2000:2025',target_crs='EPSG:5105')
        result,_=d.preview(self.mixed_run,r,PATHS)
        folder=d.export_package(self.case,self.mixed_run,r,PATHS,result)
        self.assertFalse(folder.name.endswith('.incomplete'))
        checked=d.verify_package(folder); self.assertEqual(checked['status'],'INTEGRITY_AND_POINTZ_READBACK_PASSED')
        doc=json.loads((folder/'delivery.json').read_bytes())
        self.assertEqual(doc['coordinate_epoch'],self.mixed_run['project']['epoch']); self.assertEqual(doc['reference_review']['height_evidence'],r.height_evidence)
        for name,data in self.mixed_run['files'].items(): self.assertEqual((folder/'calculation'/name).read_bytes(),data)
        self.assertEqual(doc['records'],result['records']); self.assertTrue(doc['models'])
        self.assertEqual(doc['records'][-1]['role'],'CHECK'); self.assertIsNone(doc['records'][-1]['delivered'])
        self.assertIn('AS IS',(folder/'delivery.txt').read_text()); self.assertTrue((folder/'LICENSE').exists())
        self.assertFalse(any(p.suffix in ('.exe','.dll') for p in folder.rglob('*')))
        again=d.export_package(self.case,self.mixed_run,review(),{}); self.assertNotEqual(folder,again)

    def test_stale_preview_and_tampered_reports_sources_and_package_fail(self):
        result,_=d.preview(self.mixed_run,review(),{})
        with self.assertRaisesRegex(ValueError,'stale'): d.export_package(self.case,self.mixed_run,review(target_crs='EPSG:5105'),{},result)
        copy=self.case/'copy'; shutil.copytree(self.mixed_run['folder'],copy)
        (copy/'source.csv').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'changed'): d.load_run(copy/'quality.json',PROGRAM)
        folder=d.export_package(self.case,self.mixed_run,review(),{})
        (folder/'coordinates.csv').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'checksum'): d.verify_package(folder)

    def test_legacy_nn2000_needs_version_and_keeps_orthometric_z(self):
        with self.assertRaises(ValueError): d.preview(self.level,review(source_height='NN2000',target_height='NN2000:2018'),{})
        result,_=d.preview(self.level,review(source_height='NN2000:2018',target_height='NN2000:2018'),{})
        folder=d.export_package(self.case,self.level,review(source_height='NN2000:2018',target_height='NN2000:2018'),{})
        self.assertTrue(d.verify_package(folder)); self.assertTrue(any('continuity' in w for w in result['warnings']))

    def test_path_escape_and_unknown_run_artifacts_are_rejected(self):
        for path in ('../secret','a/../secret','C:/secret','/secret','a\\secret','./secret'):
            with self.assertRaises(ValueError): d._relative(path)
        folder=self.case/'bad_run'; shutil.copytree(self.mixed_run['folder'],folder)
        sha=json.loads((folder/'sha256.json').read_bytes()); sha['../secret']='0'*64
        (folder/'sha256.json').write_text(json.dumps(sha))
        with self.assertRaises(ValueError): d.load_run(folder/'quality.json',PROGRAM)

    def test_rehashed_changed_calculation_report_still_fails_independent_comparison(self):
        folder=self.case/'run'; shutil.copytree(self.mixed_run['folder'],folder)
        report=json.loads((folder/'quality.json').read_bytes()); report['points'][2]['position_grid'][2]+=5.
        (folder/'quality.json').write_bytes(d.encoded(report))
        hashes=json.loads((folder/'sha256.json').read_bytes()); hashes['quality.json']=d.digest((folder/'quality.json').read_bytes())
        (folder/'sha256.json').write_bytes(d.encoded(hashes))
        with self.assertRaisesRegex(ValueError,'reproducible'): d.load_run(folder/'quality.json',PROGRAM)

    def test_rehashed_geopackage_height_reference_tampering_is_detected(self):
        from osgeo import ogr
        folder=d.export_package(self.case,self.mixed_run,review(),{})
        ds=ogr.Open(str(folder/'coordinates.gpkg'),1); layer=ds.GetLayerByName('delivery_points')
        f=layer.GetNextFeature(); f.SetField('height_reference','NN2000:2025'); layer.SetFeature(f)
        f=None; layer=None; ds=None
        hashes=json.loads((folder/'checksums.json').read_bytes()); hashes['coordinates.gpkg']=d.digest((folder/'coordinates.gpkg').read_bytes())
        (folder/'checksums.json').write_bytes(d.encoded(hashes))
        with self.assertRaisesRegex(ValueError,'attributes'): d.verify_package(folder)

    def test_missing_grid_corner_stops_even_when_proj_would_return_a_number(self):
        from osgeo import gdal,osr
        import numpy as np
        path=self.case/'constructed_grid.tif'
        ds=gdal.GetDriverByName('GTiff').Create(str(path),3,3,1,gdal.GDT_Float32)
        ds.SetGeoTransform((4.,1.,0.,62.,0.,-1.)); ds.GetRasterBand(1).SetNoDataValue(float('nan'))
        ds.GetRasterBand(1).WriteArray(np.array([[1.,2.,3.],[4.,5.,6.],[7.,8.,float('nan')]],dtype=np.float32)); ds=None
        self.assertEqual(d._grid_offset({'path':path},5.,61.),3.)
        with self.assertRaisesRegex(ValueError,'no-data'): d._grid_offset({'path':path},6.,60.)
        with self.assertRaisesRegex(ValueError,'coverage'): d._grid_offset({'path':path},3.,60.)

    def test_rehashed_coordinate_epoch_change_cannot_relabel_the_calculation(self):
        folder=d.export_package(self.case,self.mixed_run,review(),{})
        doc=json.loads((folder/'delivery.json').read_bytes()); doc['coordinate_epoch']='2000-01-01'
        (folder/'delivery.json').write_bytes(d.encoded(doc))
        hashes=json.loads((folder/'checksums.json').read_bytes()); hashes['delivery.json']=d.digest((folder/'delivery.json').read_bytes())
        (folder/'checksums.json').write_bytes(d.encoded(hashes))
        with self.assertRaisesRegex(ValueError,'identity'): d.verify_package(folder)
