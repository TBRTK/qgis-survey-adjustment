# SPDX-License-Identifier: GPL-3.0-only
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from survey_adjustment import network as n


class NetworkTests(unittest.TestCase):
    def test_project_round_trip_preserves_roles_heights_covariance_and_disabled_observations(self):
        p=n.synthetic_project()
        p['observations'][0].update(enabled=False,notes='SYNTHETIC rejected observation retained for review.')
        p['observations'].append(dict(n.observation(),id='V01',session='S01',from_point='REF0',to_point='P01',
            kind='GNSS_VECTOR',values=[-23.12345678901234,0.0,1e-9],
            covariance=n.covariance_from_sigmas([.01,.02,.03],[.2,-.1,.15]),frame='EPSG:4936'))
        original=deepcopy(p)
        with tempfile.TemporaryDirectory(dir=ROOT/'test_output') as folder:
            path=Path(folder)/'test.sanet.json'; n.save_project(path,p)
            self.assertEqual(n.load_project(path),original)
        self.assertEqual(p,original)
        self.assertIsNone(p['points'][2]['coordinates'])
        self.assertEqual(p['points'][3]['coordinates'][2],81.23456789)

    def test_future_schema_unknown_fields_duplicate_json_and_nonfinite_rejected(self):
        p=n.synthetic_project()
        bad=[]
        newer=deepcopy(p); newer['schema_version']=n.SCHEMA_VERSION+1; bad.append(json.dumps(newer))
        extra=deepcopy(p); extra['points'][0]['future_height_model']='do not drop'; bad.append(json.dumps(extra))
        bad.append(n.dumps_project(p).replace('"schema_version": 2','"schema_version": 2, "schema_version": 2'))
        bad.append(n.dumps_project(p).replace('81.23456789','NaN'))
        bad.append(n.dumps_project(p).replace('81.23456789','1e999'))
        for text in bad:
            with self.subTest(text=text[:80]),self.assertRaises(ValueError): n.loads_project(text)

    def test_duplicate_ids_and_missing_references_rejected_even_if_disabled(self):
        p=n.synthetic_project()
        for collection in ('points','sessions','observations'):
            copy=deepcopy(p); copy[collection].append(deepcopy(copy[collection][0]))
            with self.subTest(collection=collection),self.assertRaisesRegex(ValueError,'duplicate ID'):
                n.validate_project(copy)
        for field in ('from_point','to_point','session'):
            copy=deepcopy(p); copy['observations'][0].update(enabled=False,notes='Disabled test')
            copy['observations'][0][field]='missing'
            with self.subTest(field=field),self.assertRaises(ValueError): n.validate_project(copy)

    def test_rename_and_remove_referenced_record_do_not_change_original_project(self):
        p=n.synthetic_project(); original=deepcopy(p)
        for collection in ('points','sessions'):
            record=deepcopy(p[collection][0]); record['id']='NEW_ID'
            with self.assertRaises(ValueError): n.replace_record(p,collection,record,0)
            with self.assertRaises(ValueError): n.remove_record(p,collection,0)
        self.assertEqual(p,original)
        updated=n.remove_record(p,'observations',0)
        self.assertEqual(len(p['observations']),4)
        self.assertEqual(len(updated['observations']),3)

    def test_independent_check_cannot_be_an_active_adjustment_constraint(self):
        p=n.synthetic_project()
        p['observations'][-1]['use']='ADJUSTMENT'
        with self.assertRaisesRegex(ValueError,'independent check point'): n.validate_project(p)
        p['observations'][-1].update(enabled=False,notes='Held outside the model')
        n.validate_project(p)
        p['observations'][-1].update(enabled=True,use='CHECK')
        n.validate_project(p)

    def test_point_roles_require_explicit_coordinates_uncertainty_and_fixed_reason(self):
        for role,coords,sigma,notes in [('CONSTRAINED',None,[.01]*3,'known'),('CONSTRAINED',[291000,6701000,0],None,'known'),
             ('FIXED',[291000,6701000,0],None,''),('CHECK',None,None,'check'),('UNKNOWN',None,[.01]*3,'unknown')]:
            p=n.new_project(); p['points']=[dict(n.point(),id='P',role=role,coordinates=coords,sigma=sigma,notes=notes)]
            with self.subTest(role=role,coords=coords),self.assertRaises(ValueError): n.validate_project(p)

    def test_invalid_covariance_units_fields_and_negative_distance_are_not_accepted(self):
        with self.assertRaises(ValueError): n.covariance_from_sigmas([.01,.02,.03],[.99,.99,-.99])
        with self.assertRaises(ValueError): n.validate_covariance([.01,.1,0,.01,0,.01])
        p=n.synthetic_project(); p['observations'][0]['values']=[-.1]
        with self.assertRaises(ValueError): n.validate_project(p)
        p['observations'][0]['kind']='HEIGHT_DIFFERENCE'
        n.validate_project(p)

    def test_times_need_timezone_and_end_cannot_precede_start(self):
        for start,end in [('2026-10-18T08:00:00',''),('2026-10-18T09:00:00Z','2026-10-18T08:00:00Z'),('','2026-10-18T08:00:00Z')]:
            p=n.synthetic_project(); p['sessions'][0].update(start=start,end=end)
            with self.subTest(start=start),self.assertRaises(ValueError): n.validate_project(p)

    def test_atomic_save_failure_preserves_existing_file_and_removes_temp(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'test_output') as folder:
            path=Path(folder)/'original.sanet.json'; n.save_project(path,n.synthetic_project())
            original=path.read_bytes()
            p=n.synthetic_project(); p['name']='changed'
            with patch.object(n.os,'replace',side_effect=OSError('Simulated disk failure')),self.assertRaises(OSError): n.save_project(path,p)
            self.assertEqual(path.read_bytes(),original)
            self.assertEqual(list(Path(folder).iterdir()),[path])
            p['points'][0]['coordinates'][2]=float('nan')
            with self.assertRaises(ValueError): n.save_project(path,p)
            self.assertEqual(path.read_bytes(),original)

    def test_undocumented_metadata_can_be_saved_as_draft_without_claiming_readiness(self):
        p=n.new_project()
        warnings=n.validate_project(p)
        self.assertTrue(any('select the point coordinate system' in w for w in warnings))
        self.assertEqual(n.loads_project(n.dumps_project(p)),p)
        self.assertEqual(p['crs'],'')
        self.assertEqual(p['height_datum'],'')


if __name__=='__main__': unittest.main(verbosity=2)
