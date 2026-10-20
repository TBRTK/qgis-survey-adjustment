# SPDX-License-Identifier: GPL-3.0-only
from copy import deepcopy
import csv,io,json,tempfile,unittest
from pathlib import Path
import numpy as np
from test_qgis import app,ROOT,PROGRAM
from survey_adjustment import mixed_survey as m


def job(profile='MIXED_3D'):
    value=m.synthetic_job(profile)
    value['options'].update(frame_reviewed=True,reductions_reviewed=True,independence_reviewed=True,
        model_note='SYNTHETIC independent Gaussian blocks; frame, reductions and height basis reviewed.')
    return value


def edit_rows(value,callback):
    source=value['source']; reader=csv.DictReader(io.StringIO(source['utf8']))
    rows=list(reader); callback(rows)
    buffer=io.StringIO(newline=''); writer=csv.DictWriter(buffer,fieldnames=m.FIELDS,lineterminator='\n'); writer.writeheader(); writer.writerows(rows)
    source['utf8']=buffer.getvalue(); source['sha256']=m.digest(m.source_bytes(dict(source,sha256=m.digest((b'\xef\xbb\xbf' if source['bom'] else b'')+source['utf8'].encode('utf-8')))))
    return value


class MixedSurveyTests(unittest.TestCase):
    def test_mixed_fit_units_covariance_heldout_and_immutable_snapshot(self):
        value=job(); before=deepcopy(value); report=m.analyse(value)
        self.assertEqual(value,before); self.assertEqual(report['global_test']['degrees_of_freedom'],15)
        self.assertEqual(report['global_test']['status'],'WITHIN_MODEL_INTERVAL')
        self.assertEqual(report['checks'][0]['status'],'WITHIN_MODEL_TEST')
        self.assertEqual(len(report['excluded']),1)
        self.assertEqual({r['unit'] for r in report['observations']},{'m','deg','gon'})
        self.assertAlmostEqual(sum(o['redundancy_fraction']*(3 if o['kind'] in ('GNSS_VECTOR','COORDINATE_PRIOR') else 1) for o in report['observations']),15,places=7)
        for name,target in [('P01',[291060.,6701060.,82.]),('P02',[291150.,6701100.,84.])]:
            point=next(p for p in report['points'] if p['id']==name)
            np.testing.assert_allclose(point['position_grid'],target,rtol=0,atol=.015)
        self.assertEqual(m.loads(m.dumps(value)),value)

    def test_nn2000_level_only_matches_independent_linear_height_solution(self):
        value=job('LEVEL_1D'); report=m.analyse(value)
        order=report['parameter_order']; points={p['id']:p for p in value['network']['points']}
        a=[]; y=[]
        for o in m.parse_source(value['source']):
            if not o['enabled'] or o['use']!='ADJUSTMENT': continue
            row=np.zeros(len(order)); rhs=o['value']
            for name,sign in ((o['from_point'],-1),(o['to_point'],1)):
                if name in order: row[order.index(name)]=sign
                else: rhs-=sign*points[name]['coordinates'][2]
            a.append(row/o['sigma']); y.append(rhs/o['sigma'])
        row=np.zeros(len(order)); row[order.index('REF1')]=1/points['REF1']['sigma'][2]
        a.append(row); y.append(points['REF1']['coordinates'][2]/points['REF1']['sigma'][2])
        expected=np.linalg.lstsq(a,y,rcond=None)[0]
        for i,name in enumerate(order): self.assertAlmostEqual(report['positions'][name][0],expected[i],places=8)
        self.assertEqual(report['height'],'NN2000')
        for point in report['points']:
            if point['position_grid']:
                self.assertEqual(point['position_grid'][:2],points[point['id']]['coordinates'][:2])
                self.assertEqual(point['sigma'][:2],[None,None])
        self.assertEqual(report['global_test']['degrees_of_freedom'],3)

    def test_zero_dof_is_not_testable_and_does_not_invent_control(self):
        value=job('LEVEL_1D')
        value['network']['points']=[p for p in value['network']['points'] if p['id'] in ('REF0','P01')]
        value=edit_rows(value,lambda rows:rows.__setitem__(slice(None),[rows[0]]))
        report=m.analyse(value)
        self.assertEqual(report['global_test']['status'],'NOT_TESTABLE')
        self.assertIsNone(report['global_test']['apv']); self.assertEqual(report['observations'][0]['flags'],['NO_REDUNDANCY'])
        value['network']['points'][0]['role']='UNKNOWN'
        with self.assertRaisesRegex(ValueError,'unanchored'): m.analyse(value)

    def test_required_reviews_and_incompatible_height_profiles_are_rejected(self):
        for name in ('frame_reviewed','reductions_reviewed','independence_reviewed'):
            value=job(); value['options'][name]=False
            with self.subTest(name=name),self.assertRaises(ValueError): m.analyse(value)
        value=job(); value['network']['height_datum']='NN2000'
        with self.assertRaisesRegex(ValueError,'ELLIP'): m.analyse(value)
        value=job('LEVEL_1D'); value['network']['observations']=[deepcopy(job()['network']['observations'][0])]
        with self.assertRaisesRegex(ValueError,'GNSS vectors'): m.analyse(value)
        value=edit_rows(job(),lambda rows:rows[-3].update(height_basis='NN2000'))
        with self.assertRaisesRegex(ValueError,'basis'): m.analyse(value)

    def test_invalid_units_faces_sigma_heights_backsight_and_unknown_fields_fail(self):
        changes=[(0,dict(unit='ft')),(0,dict(instrument_height='')),(0,dict(value='-1')),(0,dict(sigma='NaN')),
            (0,dict(sigma='0')),(0,dict(backsight='REF1')),(5,dict(backsight='')),
            (5,dict(angle_reference='GRAVITY')),(8,dict(value='300')),(8,dict(unit='rad')),
            (10,dict(instrument_height='1.5')),(0,dict(notes='')),(5,dict(instrument_height='1.5'))]
        for index,change in changes:
            value=edit_rows(job(),lambda rows:rows[index].update(change))
            with self.subTest(change=change),self.assertRaises(ValueError): m.analyse(value)
        value=job(); value['source']['utf8']=value['source']['utf8'].replace('id,session,','id,unknown,',1)
        value['source']['sha256']=m.digest(value['source']['utf8'].encode())
        with self.assertRaisesRegex(ValueError,'template'): m.analyse(value)

    def test_degree_and_gon_forms_represent_the_same_physical_angle(self):
        original=job(); changed=deepcopy(original)
        def convert(rows):
            for row in rows:
                if row['unit']=='gon':
                    row.update(unit='deg',value=str(float(row['value'])*.9),sigma=str(float(row['sigma'])*.9))
        edit_rows(changed,convert)
        a,c=m.analyse(original),m.analyse(changed)
        for name in a['positions']: np.testing.assert_allclose(a['positions'][name],c['positions'][name],rtol=0,atol=1e-7)
        self.assertAlmostEqual(a['global_test']['statistic'],c['global_test']['statistic'],places=5)

    def test_heldout_error_does_not_shift_fit_and_blunder_does_not_delete_or_rescale(self):
        value=job(); baseline=m.analyse(value)
        altered=edit_rows(deepcopy(value),lambda rows:rows[-2].update(value=str(float(rows[-2]['value'])+1.)))
        check=m.analyse(altered)
        self.assertEqual(check['positions'],baseline['positions']); self.assertEqual(check['checks'][0]['status'],'CHECK_REVIEW')
        altered=edit_rows(deepcopy(value),lambda rows:rows[0].update(value=str(float(rows[0]['value'])+.4)))
        result=m.analyse(altered)
        self.assertEqual(result['global_test']['status'],'EXCESS_SCATTER')
        self.assertTrue(any('RESIDUAL_REVIEW' in o['flags'] for o in result['observations']))
        self.assertEqual(len(result['observations']),len(baseline['observations']))

    def test_source_provenance_bom_semicolon_decimal_comma_and_synthetic_classification(self):
        value=job(); original=value['source']; rows=list(csv.DictReader(io.StringIO(original['utf8'])))
        out=io.StringIO(newline=''); writer=csv.DictWriter(out,fieldnames=m.FIELDS,delimiter=';',lineterminator='\r\n'); writer.writeheader()
        for row in rows:
            for key in ('value','sigma','instrument_height','target_height','backsight_height'): row[key]=row[key].replace('.',',')
            writer.writerow(row)
        with tempfile.TemporaryDirectory(dir=ROOT/'test_output') as folder:
            path=Path(folder)/'data.csv'; data=b'\xef\xbb\xbf'+out.getvalue().encode(); path.write_bytes(data)
            source=m.read_source(path); self.assertEqual(m.source_bytes(source),data)
            with self.assertRaisesRegex(ValueError,'Synthetic/real'): m.snapshot(value['network'],source,m.MixedOptions())
            source['synthetic']=True; value['source']=source
            report=m.analyse(value); expected=m.analyse(job())
            for name in report['positions']: np.testing.assert_allclose(report['positions'][name],expected['positions'][name],atol=1e-8)
            source['utf8']+='\n'
            with self.assertRaisesRegex(ValueError,'SHA256'): m.parse_source(source)

    def test_unknown_approximation_unanchored_and_underobserved_geometry_fail(self):
        value=job(); value['network']['points'][2]['coordinates']=None
        with self.assertRaisesRegex(ValueError,'approximate'): m.analyse(value)
        value=job(); value['network']['observations']=[]
        value=edit_rows(value,lambda rows:rows.__setitem__(slice(None),[rows[0]]))
        with self.assertRaisesRegex(ValueError,'Rank-deficient'): m.analyse(value)

    @unittest.skipUnless(PROGRAM.exists(),'Separate SALSA engine not installed')
    def test_actual_salsa_nonzero_heights_angles_level_covariance_reports_and_tamper(self):
        for profile in ('MIXED_3D','LEVEL_1D'):
            with self.subTest(profile=profile),tempfile.TemporaryDirectory(dir=ROOT/'test_output') as folder:
                value=job(profile); before=deepcopy(value); path=Path(folder)/'run'
                result=m.run(PROGRAM,path,value)
                self.assertEqual(value,before)
                self.assertLess(result['engine_confirmation']['maximum_position_difference_m'],2e-5)
                self.assertEqual((path/'source.csv').read_bytes(),m.source_bytes(value['source']))
                self.assertIn('AS IS',(path/'quality.txt').read_text())
                self.assertEqual(len(list(csv.DictReader((path/'points.csv').open(encoding='utf-8-sig')))),5)
                (path/'input.dat').write_bytes((path/'input.dat').read_bytes()+b'\n')
                with self.assertRaisesRegex(ValueError,'changed'): m.inspect_result(path,0,PROGRAM)
