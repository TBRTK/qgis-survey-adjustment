# SPDX-License-Identifier: GPL-3.0-only
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import json
import math
import tempfile
import unittest
from unittest.mock import patch

from test_qgis import app,ROOT
import numpy as np
from survey_adjustment import network as n,network_quality as q,baselines as b

PROGRAM=ROOT/'upstream/extracted/SALSA/bin/lsasolver.exe'


def options(**changes):
    return replace(q.QualityOptions(frame_epoch_reviewed=True,endpoints_reviewed=True,independence_reviewed=True,
        model_note='SYNTHETIC independent Gaussian blocks; control, frame, epoch and point-to-point reductions reviewed.'),**changes)


def small_network(repeat=True):
    p=q.synthetic_quality_project()
    p['points']=[deepcopy(p['points'][0]),dict(n.point(),id='P01')]
    p['observations']=[]
    covariance=[.0004,.00008,-.00003,.0009,.00002,.0016]
    for index,values in enumerate(([10.01,20.02,30.03],[9.99,19.98,29.97]) if repeat else ([10.01,20.02,30.03],)):
        p['observations'].append(dict(n.observation(),id=f'V{index}',kind='GNSS_VECTOR',from_point='REF0',to_point='P01',
            session='S01' if index==0 else 'S02',values=list(values),covariance=list(covariance),frame='EPSG:4936',notes='SYNTHETIC independent vector.'))
    return p


class NetworkQualityTests(unittest.TestCase):
    def test_stored_itrf2020_vector_is_not_silently_accepted_as_etrs89(self):
        project=q.synthetic_quality_project()
        for row in project['observations']:row['frame']='EPSG:9988'
        n.validate_project(project)
        with self.assertRaisesRegex(ValueError,'ETRS89 ECEF'):q.analyse(project,options())

    def test_clean_full_covariance_network_and_source_immutability(self):
        p=q.synthetic_quality_project(); before=deepcopy(p); r=q.analyse(p,options())
        self.assertEqual(p,before); self.assertEqual(r['global_test']['degrees_of_freedom'],15)
        self.assertEqual(r['global_test']['status'],'WITHIN_MODEL_INTERVAL')
        self.assertTrue(all(not row['flags'] for row in r['observations']))
        self.assertEqual(r['checks'][0]['status'],'WITHIN_MODEL_TEST')
        self.assertEqual(r['network']['rank'],9)
        self.assertAlmostEqual(sum(row['redundancy_number'] for row in r['observations']),15,places=8)
        self.assertTrue(all(p['sigma_enu_m'][2]>0 for p in r['points'] if p['role'] in ('UNKNOWN','CONSTRAINED')))
        self.assertFalse(any(np.isnan(p).any() for p in np.array(r['parameter_covariance_xyz'])))

    def test_analytic_correlated_repeats_have_mean_half_covariance_and_half_redundancy(self):
        p=small_network(); r=q.analyse(p,options())
        base=np.array(r['positions_ecef']['REF0']); expected=base+[10.,20.,30.]
        np.testing.assert_allclose(r['positions_ecef']['P01'],expected,rtol=0,atol=1e-8)
        covariance=np.array(b.matrix(p['observations'][0]['covariance']))
        point=next(v for v in r['points'] if v['id']=='P01')
        np.testing.assert_allclose(point['covariance_xyz'],covariance/2,rtol=1e-11,atol=1e-16)
        self.assertEqual(r['global_test']['degrees_of_freedom'],3)
        for row in r['observations']:
            self.assertAlmostEqual(row['redundancy_fraction'],.5,places=10)
        residual=np.array([.01,.02,.03])
        self.assertAlmostEqual(r['global_test']['statistic'],2*float(residual@np.linalg.solve(covariance,residual)),places=8)

    def test_zero_dof_never_claims_fit_test_or_zero_uncertainty(self):
        r=q.analyse(small_network(False),options())
        self.assertEqual(r['global_test']['degrees_of_freedom'],0)
        self.assertEqual(r['global_test']['status'],'NOT_TESTABLE'); self.assertIsNone(r['global_test']['apv'])
        self.assertIn('NO_REDUNDANCY',r['observations'][0]['flags'])
        self.assertEqual(r['observations'][0]['standardized_xyz'],[None]*3)
        self.assertIn('WEAK_CONTROL',next(p for p in r['points'] if p['id']=='P01')['flags'])
        self.assertTrue(any('ZERO DEGREES' in v for v in r['warnings']))

    def test_weighted_anchor_without_error_free_control_propagates_its_covariance(self):
        p=small_network(); p['points'][0].update(role='CONSTRAINED',sigma=[.01,.02,.03])
        r=q.analyse(p,options())
        prior=q.GridCoordinates(p['crs']).prior_covariance(p['points'][0])
        covariance=np.array(b.matrix(p['observations'][0]['covariance']))
        reference=next(v for v in r['points'] if v['id']=='REF0')
        target=next(v for v in r['points'] if v['id']=='P01')
        np.testing.assert_allclose(reference['covariance_xyz'],prior,rtol=1e-10,atol=1e-15)
        np.testing.assert_allclose(target['covariance_xyz'],prior+covariance/2,rtol=1e-10,atol=1e-15)
        np.testing.assert_allclose(np.array(r['parameter_covariance_xyz'])[:3,3:],prior,rtol=1e-10,atol=1e-15)
        self.assertEqual(r['global_test']['degrees_of_freedom'],3)
        if PROGRAM.exists():
            with tempfile.TemporaryDirectory(dir=ROOT/'test_output') as temporary:
                result=q.run_network(PROGRAM,Path(temporary)/'weighted',p,options())
                self.assertEqual(result['engine_confirmation']['status'],'INDEPENDENT_MODEL_COMPARISON_PASSED')

    def test_bridge_point_is_weak_even_when_global_network_has_redundancy(self):
        p=q.synthetic_quality_project(); p['points'].append(dict(n.point(),id='LEAF'))
        p['observations'].append(dict(n.observation(),id='LEAF_VECTOR',kind='GNSS_VECTOR',session='S01',from_point='P02',to_point='LEAF',
            values=[1.,2.,3.],covariance=[.0004,0.,0.,.0004,0.,.0004],frame='EPSG:4936'))
        r=q.analyse(p,options())
        self.assertEqual(r['global_test']['degrees_of_freedom'],15)
        leaf=next(v for v in r['observations'] if v['id']=='LEAF_VECTOR')
        self.assertIn('NO_REDUNDANCY',leaf['flags']); self.assertLess(max(map(abs,leaf['residual_xyz_m'])),1e-8)
        self.assertIn('WEAK_CONTROL',next(v for v in r['points'] if v['id']=='LEAF')['flags'])

    def test_gross_error_is_flagged_without_automatic_deletion_or_sigma_rescaling(self):
        p=q.synthetic_quality_project(); p['observations'][2]['values'][0]+=.5
        before=deepcopy(p); r=q.analyse(p,options())
        clean=q.analyse(q.synthetic_quality_project(),options())
        self.assertEqual(r['global_test']['status'],'EXCESS_SCATTER')
        self.assertTrue(any('RESIDUAL_REVIEW' in v['flags'] for v in r['observations']))
        self.assertEqual(len(r['observations']),8); self.assertEqual(p,before)
        np.testing.assert_allclose(r['parameter_covariance_xyz'],clean['parameter_covariance_xyz'],atol=1e-14)

    def test_tiny_scatter_warns_instead_of_shrinking_reported_covariance(self):
        p=small_network(); p['observations'][1]['values']=list(p['observations'][0]['values'])
        r=q.analyse(p,options())
        self.assertEqual(r['global_test']['status'],'LOW_SCATTER')
        point=next(v for v in r['points'] if v['id']=='P01')
        self.assertGreater(point['sigma_enu_m'][2],0)

    def test_checks_are_held_out_and_missing_check_uncertainty_is_inconclusive(self):
        p=q.synthetic_quality_project(); a=q.analyse(p,options())
        p['observations'][-1]['values'][0]+=10
        r=q.analyse(p,options())
        self.assertEqual(r['positions_ecef'],a['positions_ecef']); self.assertEqual(r['global_test'],a['global_test'])
        self.assertEqual(r['checks'][0]['status'],'CHECK_REVIEW')
        p['points'][-1]['sigma']=None
        r=q.analyse(p,options()); self.assertEqual(r['checks'][0]['status'],'INCONCLUSIVE')
        self.assertIsNone(r['checks'][0]['pvalue'])

    def test_user_warning_limits_are_explicit_and_not_delivery_standards(self):
        r=q.analyse(q.synthetic_quality_project(),options(horizontal_95_limit=.0001,vertical_sigma_limit=.0001))
        for p in r['points']:
            if p['role'] in ('UNKNOWN','CONSTRAINED'):
                self.assertIn('USER_HORIZONTAL_LIMIT',p['flags']); self.assertIn('USER_VERTICAL_LIMIT',p['flags'])

    def test_reference_anchoring_is_not_inferred_from_unknown_initial_coordinates(self):
        p=small_network(); p['points'][0]['role']='UNKNOWN'
        with self.assertRaisesRegex(ValueError,'Unanchored'): q.analyse(p,options())
        p=q.synthetic_quality_project(); p['points'].extend([dict(n.point(),id='X'),dict(n.point(),id='Y')])
        p['observations'].append(dict(n.observation(),id='UNANCHORED',kind='GNSS_VECTOR',session='S01',from_point='X',to_point='Y',
            values=[1.,2.,3.],covariance=[1.,0.,0.,1.,0.,1.],frame='EPSG:4936'))
        with self.assertRaisesRegex(ValueError,'Unanchored'): q.analyse(p,options())

    def test_required_reviews_frames_heights_types_and_repeated_session_pairs(self):
        p=q.synthetic_quality_project()
        for change in (dict(frame_epoch_reviewed=False),dict(endpoints_reviewed=False),dict(independence_reviewed=False),dict(model_note=''),dict(alpha=0.),dict(horizontal_95_limit=-1.)):
            with self.subTest(change=change),self.assertRaises(ValueError): q.analyse(p,options(**change))
        for change in (dict(height_datum='NN2000'),dict(epoch=''),dict(crs='')):
            candidate=dict(deepcopy(p),**change)
            with self.subTest(change=change),self.assertRaises(ValueError): q.analyse(candidate,options())
        for change in (dict(frame='EPSG:4978'),dict(instrument_height=.045),
                       dict(kind='DISTANCE',values=[1.],covariance=[.01],frame='')):
            candidate=deepcopy(p); candidate['observations'][0].update(change)
            with self.subTest(change=change),self.assertRaises(ValueError): q.analyse(candidate,options())
        duplicate=deepcopy(p['observations'][0]); duplicate['id']='duplicate'; p['observations'].append(duplicate)
        with self.assertRaisesRegex(ValueError,'Repeated endpoint pair'): q.analyse(p,options())

    def test_disabled_observations_and_unused_points_remain_visible(self):
        p=q.synthetic_quality_project(); p['observations'][0].update(enabled=False,notes='SYNTHETIC operator exclusion, retained in snapshot.')
        p['points'].append(dict(n.point(),id='UNUSED'))
        r=q.analyse(p,options())
        self.assertEqual(r['excluded'][0]['id'],'V01')
        self.assertIn('UNOBSERVED_NOT_ADJUSTED',r['points'][-1]['flags'])

    def test_fixed_coordinate_error_can_shift_solution_without_affecting_internal_fit(self):
        p=small_network(); a=q.analyse(p,options())
        p['points'][0]['coordinates'][0]+=100.
        r=q.analyse(p,options())
        self.assertAlmostEqual(r['global_test']['statistic'],a['global_test']['statistic'],places=8)
        self.assertGreater(math.dist(r['positions_ecef']['P01'],a['positions_ecef']['P01']),99.)
        self.assertTrue(any('Fixed coordinates' in text for text in r['warnings']))

    def test_generated_solver_syntax_uses_safe_labels_and_explicit_prior_origin(self):
        p=q.synthetic_quality_project(); old='P01'; new='P01 # quoted " point'
        next(v for v in p['points'] if v['id']==old)['id']=new
        for o in p['observations']:
            for key in ('from_point','to_point'):
                if o[key]==old: o[key]=new
        a=q.analyse(p,options()); data=q.solver_input(p,a)
        self.assertNotIn(new,data); self.assertIn('PRIOR_ORIGIN',data)
        self.assertNotIn(' M ADJ',data); self.assertEqual(data.count('tag=OBS_'),7)
        self.assertEqual(data.count('tag=PRIOR_'),1)

    @unittest.skipUnless(PROGRAM.exists(),'Local SALSA engine unavailable')
    def test_real_salsa_matches_independent_model_and_retains_local_reports(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'test_output') as temporary:
            folder=Path(temporary)/'quality'; p=q.synthetic_quality_project(); before=deepcopy(p)
            result=q.run_network(PROGRAM,folder,p,options())
            self.assertEqual(result['engine_confirmation']['version'],'1.17.0')
            self.assertLess(result['engine_confirmation']['maximum_position_difference_m'],2e-6)
            self.assertEqual(p,before); self.assertEqual(n.load_project(folder/'network.sanet.json'),p)
            self.assertTrue((folder/'quality.html').exists()); self.assertTrue((folder/'points.csv').exists())
            self.assertIn('AS IS',(folder/'quality.txt').read_text())
            path=folder/'input.dat'; path.write_bytes(path.read_bytes()+b'\n# changed\n')
            with self.assertRaisesRegex(ValueError,'changed'): q.inspect_network_result(folder,0,PROGRAM)

    @unittest.skipUnless(PROGRAM.exists(),'Local SALSA engine unavailable')
    def test_real_salsa_evenly_determined_network_is_not_statistically_approved(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'test_output') as temporary:
            result=q.run_network(PROGRAM,Path(temporary)/'zero',small_network(False),options())
            self.assertEqual(result['global_test']['status'],'NOT_TESTABLE')
            self.assertIn('NO_REDUNDANCY',result['observations'][0]['flags'])

    @unittest.skipUnless(PROGRAM.exists(),'Local SALSA engine unavailable')
    def test_real_salsa_gross_error_and_edited_output_fail_or_warn(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'test_output') as temporary:
            p=q.synthetic_quality_project(); p['observations'][0]['values'][1]+=.4
            folder=Path(temporary)/'gross'; result=q.run_network(PROGRAM,folder,p,options())
            self.assertEqual(result['global_test']['status'],'EXCESS_SCATTER')
            self.assertTrue(any('RESIDUAL_REVIEW' in r['flags'] for r in result['observations']))
            text=(folder/'solution.out').read_text()
            value=next(iter(result['positions_ecef'].values()))[0]
            # A guaranteed final-section tamper, independent of formatted values.
            (folder/'solution.out').write_text(text.replace('Final Adjusted Positions XYZ','Corrupted final section'))
            with self.assertRaises(ValueError): q.inspect_network_result(folder,0,PROGRAM)


if __name__=='__main__': unittest.main(verbosity=2)
