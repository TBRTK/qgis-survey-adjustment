# SPDX-License-Identifier: GPL-3.0-only
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from survey_adjustment.core import read_table
from survey_adjustment.coordinate_job import *
from survey_adjustment.engine import command,environment,inspect_result
PROGRAM=ROOT/'upstream/extracted/SALSA/bin/lsasolver.exe'


def example():
    table=read_table(ROOT/'survey_adjustment/examples/emlid_salsa_llh.csv')
    options=CoordinateOptions(synthetic=True,frame='EPSG:4937',epoch='2026-10-18',
        ellipsoid_confirmed=True,independent_confirmed=True,uncertainty_confirmed=True,
        sigma_mode='columns',uncertainty_source='Syntetisk 1 sigma ENU',
        correction_source='Syntetisk eksempel',antenna_note='Syntetiske punkthøyder')
    return table,mapping_for(table['headers']),options


class CoordinateTests(unittest.TestCase):
    def test_receiver_neutral_semicolon_and_tab_headers_give_identical_weighted_xyz(self):
        table,mapping,options=example()
        original,meta=coordinate_job(table,mapping,options)
        alternative=read_table(ROOT/'survey_adjustment/examples/gnss_salsa_llh.csv')
        data,other=coordinate_job(alternative,mapping_for(alternative['headers']),options)
        self.assertEqual(original,data)
        self.assertEqual(meta['expected_positions'],other['expected_positions'])
        self.assertEqual(meta['model_covariance_xyz'],other['model_covariance_xyz'])
        self.assertEqual(meta['excluded'],other['excluded'])
        with tempfile.TemporaryDirectory(dir=ROOT/'test_output') as folder:
            path=Path(folder)/'measurements.tsv'
            path.write_text((ROOT/'survey_adjustment/examples/gnss_salsa_llh.csv').read_text(encoding='utf-8').replace(';','\t'),encoding='utf-8')
            tabular=read_table(path)
            self.assertEqual(tabular['delimiter'],'\t')
            self.assertEqual(coordinate_job(tabular,mapping_for(tabular['headers']),options)[0],original)

    def test_numeric_solution_code_excluded_and_generic_height_not_assumed_ellipsoidal(self):
        table,mapping,options=example()
        modified=dict(table,rows=[dict(r) for r in table['rows']])
        modified['rows'][0]['Solution status']='4'
        _,meta=coordinate_job(modified,mapping,options)
        self.assertEqual(len(meta['observations']),5)
        self.assertEqual(len(meta['excluded']),2)
        self.assertIn('4',meta['excluded'][0]['reason'])
        self.assertEqual(mapping_for(['Observation','Target ID','Lat','Lon','Elevation'])['h'],'')

    def test_unequal_weights_analytic_solution_no_fictitious_gnss_baseline(self):
        table,mapping,options=example()
        rows=[]
        for i,(h,su) in enumerate(((100.,.1),(100.1,.2))):
            row=dict(table['rows'][i]); row.update({'Latitude':'0','Longitude':'0','Ellipsoidal height':str(h),'Sigma U':str(su),'Control ID':'One point'})
            rows.append(row)
        table=dict(table,rows=rows)
        text,meta=coordinate_job(table,mapping,options)
        label=next(iter(meta['expected_positions']))
        self.assertAlmostEqual(meta['expected_positions'][label][0],6378137+100.02,places=8)
        self.assertAlmostEqual(meta['model_covariance_xyz'][label][0][0],.008,places=12)
        self.assertIn('not a surveyed base',text)
        self.assertNotIn('One point',text)
        self.assertEqual(meta['identifiers'][label],'One point')

    def test_full_enu_rotation_roundtrip_both_frames_and_spd_validation(self):
        for frame in FRAMES:
            for lat,lon,h in ((60.4,5.2,80),(-50.,-120.,-50),(85,179,1000)):
                out=llh(xyz(lat,lon,h,frame),frame)
                for a,b in zip(out,(lat,lon,h)): self.assertAlmostEqual(a,b,places=7)
        c=covariance(60.4,5.2,(.02,.03,.06))
        self.assertGreater(abs(c[0][2]),.0001)
        product=matmul(c,inverse_spd(c))
        for i in range(3):
            for j in range(3): self.assertAlmostEqual(product[i][j],float(i==j),places=12)
        with self.assertRaises(ValueError): inverse_spd([[1,2,0],[2,1,0],[0,0,1]])

    def test_required_knowledge_datum_sigma_and_independence_gates(self):
        table,mapping,options=example()
        for changes in ({'frame':''},{'frame':'EPSG:25832'},{'epoch':'ukjent'},
            {'ellipsoid_confirmed':False},{'independent_confirmed':False},
            {'uncertainty_confirmed':False},{'uncertainty_source':''},
            {'correction_source':''},{'antenna_note':''},
            {'sigma_mode':'constant','sigma_e':.02,'sigma_n':.02,'sigma_u':0}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                coordinate_job(table,mapping,replace(options,**changes))

    def test_reject_rms_orthometric_mismatched_frame_nonfinite_duplicates_and_single(self):
        table,mapping,options=example()
        for field,value in (('Latitude','nan'),('Longitude','291000'),('Sigma U','0'),('Geodetic CRS','EPSG:4979')):
            modified=dict(table,rows=[dict(r) for r in table['rows']]); modified['rows'][0][field]=value
            with self.subTest(field=field),self.assertRaises(ValueError): coordinate_job(modified,mapping,options)
        modified=dict(table,rows=[dict(r) for r in table['rows']]); modified['rows'][1]['Averaging start']=modified['rows'][0]['Averaging start']
        with self.assertRaises(ValueError): coordinate_job(modified,mapping,options)
        with self.assertRaises(ValueError): coordinate_job(dict(table,rows=table['rows'][:1]),mapping,options)
        modified=dict(table,headers=table['headers']+['Easting RMS'])
        with self.assertRaisesRegex(ValueError,'RMS'): coordinate_job(modified,dict(mapping,se='Easting RMS'),options)
        modified=dict(table,headers=table['headers']+['Elevation','Height datum'],rows=[dict(r,Elevation=r['Ellipsoidal height'],**{'Height datum':'NN2000'}) for r in table['rows']])
        with self.assertRaisesRegex(ValueError,'ortometrisk'): coordinate_job(modified,dict(mapping,h='Elevation'),options)

    def test_fix_filter_source_integrity_snapshot_and_unique_dat_tags(self):
        table,mapping,options=example(); data,meta=coordinate_job(table,mapping,options)
        self.assertEqual(len(meta['observations']),6); self.assertEqual(len(meta['excluded']),1)
        tags=[line.split('tag=')[1] for line in data.splitlines() if 'tag=' in line]
        self.assertEqual(len(set(tags)),6)
        self.assertTrue(meta['synthetic']); self.assertEqual(meta['height'],'ELLIP')
        with tempfile.TemporaryDirectory(dir=ROOT/'test_output') as tmp:
            folder=prepare_coordinate(Path(tmp)/'job',table,mapping,options)
            self.assertEqual((folder/'source.csv').read_bytes(),Path(table['path']).read_bytes())
            with self.assertRaises(FileExistsError): prepare_coordinate(folder,table,mapping,options)
            with self.assertRaisesRegex(ValueError,'endret'): prepare_coordinate(Path(tmp)/'changed',dict(table,sha256='wrong'),mapping,options)

    @unittest.skipUnless(PROGRAM.exists(),'Local SALSA engine unavailable')
    def test_real_salsa_coordinate_adjustment_full_covariance_and_exports(self):
        table,mapping,options=example()
        source_hash=hashlib.sha256(Path(table['path']).read_bytes()).hexdigest()
        with tempfile.TemporaryDirectory(dir=ROOT/'test_output') as tmp:
            folder=prepare_coordinate(Path(tmp)/'job',table,mapping,options)
            process=subprocess.run(command(PROGRAM,folder),cwd=folder,env=environment(PROGRAM),capture_output=True,timeout=45,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform=='win32' else 0)
            report=inspect_result(folder,process.returncode,PROGRAM)
            self.assertEqual(report['kind'],'coordinate_observations')
            self.assertLess(report['maximum_difference_metres'],2e-6)
            self.assertEqual(len(report['positions_geodetic']),2)
            self.assertTrue(report['warnings'])
            self.assertIn('Ellipsoidal height',(folder/'adjusted_coordinates.csv').read_text(encoding='utf-8-sig'))
            self.assertTrue((folder/'coordinate_residuals.csv').exists())
            self.assertEqual(report['frame'],'EPSG:4937')
            original=(folder/'input.dat').read_text(encoding='ascii')
            (folder/'input.dat').write_text(original+'# changed\n',encoding='ascii')
            with self.assertRaisesRegex(ValueError,'endret'): inspect_result(folder,process.returncode,PROGRAM)
            (folder/'input.dat').write_text(original,encoding='ascii',newline='\n')
            (folder/'source.csv').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError,'Kildekopien'): inspect_result(folder,process.returncode,PROGRAM)
        self.assertEqual(hashlib.sha256(Path(table['path']).read_bytes()).hexdigest(),source_hash)


if __name__=='__main__':
    (ROOT/'test_output').mkdir(exist_ok=True)
    unittest.main(verbosity=2)
