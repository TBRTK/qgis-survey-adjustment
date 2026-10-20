# SPDX-License-Identifier: GPL-3.0-only
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import hashlib
import json
import math
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from survey_adjustment import baselines as b, network as n
from survey_adjustment.coordinate_job import xyz,llh
EXAMPLES=ROOT/'survey_adjustment/examples'


def options(**kwargs):
    return b.ImportOptions(**dict(dict(frame='EPSG:4936',epoch='2026-10-18',origin='REF0',target='P01',
        session='S01',identifier='B1',antenna_note='SYNTHETIC point-to-point, no actual antenna.',
        uncertainty_note='SYNTHETIC formal covariance; fixed processing base, common errors not modelled.',
        relative_confirmed=True,endpoints_confirmed=True,covariance_confirmed=True,synthetic=True),**kwargs))


def source(kind='llh'):
    return b.read_source(EXAMPLES/f'synthetic_baseline_{kind}.pos')


class BaselineTests(unittest.TestCase):
    def test_pos_profiles_are_relative_and_preserve_signed_cross_covariance(self):
        for kind in ('llh','xyz','enu'):
            src=source(kind); row=src['rows'][0]
            self.assertEqual(src['metadata']['representation'],kind.upper())
            self.assertEqual([r['eligible'] for r in src['rows']],[True,False,True])
            expected=[.0001,-.000016,.000025,.0004,-.000009,.0009] if kind=='llh' else [.0004,-.000016,-.000009,.0001,.000025,.0009]
            for actual,value in zip(row['covariance'],expected): self.assertAlmostEqual(actual,value,15)
            self.assertEqual(row['time'],'2026-10-18T08:00:00Z')
            self.assertEqual(src['source_sha256'],hashlib.sha256((EXAMPLES/f'synthetic_baseline_{kind}.pos').read_bytes()).hexdigest())

    def test_enu_rotation_has_independently_known_axis_and_covariance_result(self):
        meta=dict(representation='ENU',mode='static',time_system='UTC',reference=[0.,0.,80.])
        vector,cov,reference=b.to_ecef([1.,2.,3.],[4.,.1,-.2,9.,.3,16.],meta,'EPSG:4978')
        self.assertEqual(vector,[3.,1.,2.])
        self.assertEqual(cov,[16.,-.2,.3,4.,.1,9.])
        self.assertEqual(reference,[6378217.,0.,0.])

    def test_native_pos_ellipsoid_is_independent_of_declared_processing_frame(self):
        src=source(); row=src['rows'][0]; ref=src['metadata']['reference']
        decoded=b.to_ecef(row['values'],row['covariance'],src['metadata'],'EPSG:4936')
        self.assertEqual(decoded,b.to_ecef(row['values'],row['covariance'],src['metadata'],'EPSG:4978'))
        self.assertEqual(decoded[2],xyz(*ref,'EPSG:4979'))
        self.assertGreater(math.dist(decoded[2],xyz(*ref,'EPSG:4937')),0.00005)

    def test_llh_ecef_enu_and_csv_same_constructed_baseline_and_height(self):
        vectors=[]
        for kind in ('llh','xyz','enu'):
            p=n.synthetic_project(); src=source(kind)
            out=b.plan_import(p,src,src['rows'][0]['line'],options())
            observation=out['observations'][-1]; vectors.append(observation['values'])
            self.assertEqual(out['points'],p['points'])
            self.assertIsNone(out['points'][2]['coordinates'])
            # Physical ECEF chord differs from UTM grid distance (projection scale).
            self.assertAlmostEqual(math.sqrt(sum(v*v for v in observation['values'])),100.0077557673544,places=7)
            base=b.to_ecef(src['rows'][0]['values'],src['rows'][0]['covariance'],src['metadata'],'EPSG:4936')[2]
            target=llh([v+r for v,r in zip(observation['values'],base)],'EPSG:4937')
            self.assertAlmostEqual(target[2],82.0,places=7)
            self.assertEqual(n.loads_project(n.dumps_project(out)),out)
        for v in vectors[1:]:
            self.assertLess(math.dist(vectors[0],v),1e-7)

    def test_source_unchanged_atomic_import_and_full_audit_save_open(self):
        p=n.synthetic_project(); original=deepcopy(p); src=source(); payload=(EXAMPLES/'synthetic_baseline_llh.pos').read_bytes()
        out=b.plan_import(p,src,src['rows'][0]['line'],options())
        self.assertEqual(p,original)
        self.assertEqual((EXAMPLES/'synthetic_baseline_llh.pos').read_bytes(),payload)
        self.assertEqual(out['imports'][0]['records'][0]['raw'],src['rows'][0]['raw'])
        self.assertNotIn('C:',out['imports'][0]['source_name'])
        with tempfile.TemporaryDirectory(dir=ROOT/'test_output') as folder:
            path=Path(folder)/'imported.sanet.json'; n.save_project(path,out)
            self.assertEqual(n.load_project(path),out)

    def test_unconfirmed_or_missing_model_metadata_cannot_add_any_observation(self):
        p=n.synthetic_project(); original=deepcopy(p); src=source()
        for change in (dict(relative_confirmed=False),dict(endpoints_confirmed=False),dict(covariance_confirmed=False),
                       dict(frame=''),dict(epoch=''),dict(antenna_note=''),dict(uncertainty_note=''),dict(relative_confirmed='no')):
            with self.subTest(change=change),self.assertRaises(ValueError): b.plan_import(p,src,src['rows'][0]['line'],replace(options(),**change))
        self.assertEqual(p,original)

    def test_nonfix_duplicate_epoch_truncated_covariance_ppp_and_geoid_rejected(self):
        text=(EXAMPLES/'synthetic_baseline_llh.pos').read_text()
        p=n.synthetic_project(); src=source()
        with self.assertRaisesRegex(ValueError,'eligible FIX'): b.plan_import(p,src,src['rows'][1]['line'],options())
        for content in (text.replace('pos mode  : static','pos mode  : ppp-static'),
                        text.replace('WGS84/ellipsoidal','WGS84/geodetic'),
                        text.replace('sdne(m) ','') ,text.replace('ref pos','unknown reference'),
                        text.replace('08:00:01','08:00:00'), text.replace('0.02 0.01 0.03','NaN 0.01 0.03')):
            with self.subTest(content=content[:20]),self.assertRaises(ValueError): b.parse_source(content.encode(),'synthetic.pos')
        invalid=b.parse_source(text.replace('0.02 0.01 0.03','0.0 0.01 0.03').encode(),'synthetic.pos')
        self.assertFalse(invalid['rows'][0]['eligible'])

    def test_gpst_stays_gpst_and_is_not_silently_compared_as_utc(self):
        text=(EXAMPLES/'synthetic_baseline_llh.pos').read_text().replace('% UTC ','% GPST ')
        src=b.parse_source(text.encode(),'synthetic.pos')
        self.assertEqual(src['rows'][0]['time'],'2026-10-18T08:00:00')
        out=b.plan_import(n.synthetic_project(),src,src['rows'][0]['line'],options())
        self.assertEqual(out['imports'][0]['metadata']['time_system'],'GPST')
        self.assertEqual(n.loads_project(n.dumps_project(out)),out)
        for value in ('2026-10-18T08:00:00.000000001','2026-10-18T08:00:60','2026-10-18'):
            with self.assertRaises(ValueError): b.epoch_text(value,'GPST',1)

    def test_missing_processing_mode_requires_explicit_declaration_retained_in_audit(self):
        payload=(EXAMPLES/'synthetic_baseline_llh.pos').read_bytes().replace(b'% pos mode  : static\n',b'')
        # Tolerate checkout line endings without changing fixture contents.
        payload=payload.replace(b'% pos mode  : static\r\n',b'')
        with self.assertRaisesRegex(ValueError,'omits processing mode'):
            b.parse_source(payload,'constructed.pos')
        src=b.parse_source(payload,'constructed.pos',processing_mode='static')
        self.assertEqual(src['source_sha256'],hashlib.sha256(payload).hexdigest())
        self.assertIn('operator explicitly declared static',src['mode_declaration'])
        out=b.plan_import(n.synthetic_project(),src,src['rows'][0]['line'],options())
        self.assertIn(src['mode_declaration'],out['imports'][0]['uncertainty_note'])
        self.assertEqual(n.loads_project(n.dumps_project(out)),out)

    def test_processing_mode_cannot_override_conflicting_or_unsupported_headers_or_csv(self):
        payload=(EXAMPLES/'synthetic_baseline_llh.pos').read_bytes()
        for contents,mode in ((payload,'kinematic'),(payload,'unknown'),
                              (payload.replace(b'pos mode  : static',b'pos mode  : ppp-static'),'static')):
            with self.assertRaises(ValueError): b.parse_source(contents,'constructed.pos',processing_mode=mode)
        with self.assertRaises(ValueError):
            b.parse_source((EXAMPLES/'synthetic_measured_baseline.csv').read_bytes(),'constructed.csv',processing_mode='static')

    def test_declared_mode_never_promotes_float_or_fabricates_empty_event_observations(self):
        text=(EXAMPLES/'synthetic_baseline_llh.pos').read_text().replace('% pos mode  : static\n','').replace(' 1 16 ',' 2 16 ')
        src=b.parse_source(text.encode(),'constructed.pos',processing_mode='static')
        self.assertTrue(all(row['quality']=='2' and not row['eligible'] for row in src['rows']))
        project=n.synthetic_project(); original=deepcopy(project)
        with self.assertRaisesRegex(ValueError,'eligible FIX'):
            b.plan_import(project,src,src['rows'][0]['line'],options())
        self.assertEqual(project,original)
        header='\n'.join(line for line in text.splitlines() if line.startswith('%'))+'\n'
        with self.assertRaisesRegex(ValueError,'no observations'):
            b.parse_source(header.encode(),'constructed_events.pos',processing_mode='static')

    def test_csv_explicit_units_full_covariance_decimal_comma_and_id_consistency(self):
        path=EXAMPLES/'synthetic_measured_baseline.csv'; src=b.read_source(path)
        opts=options(identifier='SYNTHETIC_B01')
        out=b.plan_import(n.synthetic_project(),src,2,opts)
        self.assertEqual(src['rows'][0]['raw'],path.read_text().splitlines()[1])
        self.assertEqual(out['observations'][-1]['values'],src['rows'][0]['values'])
        self.assertEqual(out['observations'][-1]['covariance'],src['rows'][0]['covariance'])
        with self.assertRaisesRegex(ValueError,'IDs must match'): b.plan_import(n.synthetic_project(),src,2,options(identifier='renamed'))
        import csv,io
        reader=list(csv.reader(io.StringIO(path.read_text()))); buffer=io.StringIO(); writer=csv.writer(buffer,delimiter=';')
        writer.writerow(reader[0]); writer.writerow([v.replace('.',',') if i in range(4,13) else v for i,v in enumerate(reader[1])])
        decimal=b.parse_source(buffer.getvalue().encode(),'synthetic.csv')
        self.assertEqual(decimal['rows'][0]['values'],src['rows'][0]['values'])
        with self.assertRaises(ValueError): b.parse_source(b'Name,Easting,Northing,Elevation\nP1,1,2,3\n','points.csv')

    def test_session_bounds_check_roles_and_synthetic_data_separation(self):
        p=n.synthetic_project(); src=source(); line=src['rows'][0]['line']
        for option in (options(target='CHECK1'), options(synthetic=False),options(session='missing')):
            with self.assertRaises(ValueError): b.plan_import(p,src,line,option)
        out=b.plan_import(p,src,line,options(target='CHECK1',use='CHECK'))
        self.assertEqual(out['observations'][-1]['use'],'CHECK')
        p['sessions'][0]['start']='2026-10-18T08:10:00Z'
        with self.assertRaisesRegex(ValueError,'outside'): b.plan_import(p,src,line,options())

    def test_same_session_or_renamed_copy_or_reexport_is_not_independent_repeat(self):
        p=n.synthetic_project(); src=source(); first=b.plan_import(p,src,src['rows'][0]['line'],options())
        original=deepcopy(first)
        with self.assertRaisesRegex(ValueError,'already exists'): b.plan_import(first,src,src['rows'][2]['line'],options(identifier='B2'))
        first['sessions'].append(dict(n.session(),id='S02',start='2026-10-18T08:00:00Z'))
        renamed=deepcopy(src); renamed['source_name']='renamed.pos'
        with self.assertRaisesRegex(ValueError,'already imported'): b.plan_import(first,renamed,src['rows'][0]['line'],options(identifier='B2',session='S02'))
        reexport=source('xyz')
        with self.assertRaisesRegex(ValueError,'equivalent baseline'): b.plan_import(first,reexport,reexport['rows'][0]['line'],options(identifier='B2',session='S02'))
        self.assertEqual(first['observations'],original['observations'])

    def test_imported_measurements_immutable_notes_use_and_exclusion_reviewable(self):
        src=source(); p=b.plan_import(n.synthetic_project(),src,src['rows'][0]['line'],options())
        for change in (dict(id='changed'),dict(values=[0.,0.,0.]),dict(frame='EPSG:4978'),dict(covariance=[1.,0.,0.,1.,0.,1.]),dict(from_point='REF1')):
            record=dict(deepcopy(p['observations'][-1]),**change)
            with self.subTest(change=change),self.assertRaises(ValueError): n.replace_record(p,'observations',record,len(p['observations'])-1)
        record=dict(deepcopy(p['observations'][-1]),enabled=False,use='CHECK',notes='SYNTHETIC excluded after review')
        out=n.replace_record(p,'observations',record,len(p['observations'])-1)
        self.assertEqual(out['observations'][-1]['values'],p['observations'][-1]['values'])
        removed=n.remove_record(out,'observations',len(out['observations'])-1)
        self.assertEqual(removed['imports'],p['imports'])

    def test_processing_base_change_and_audit_tampering_rejected(self):
        src=source(); p=b.plan_import(n.synthetic_project(),src,src['rows'][0]['line'],options())
        p['points'].append(dict(n.point(),id='P02'))
        text=(EXAMPLES/'synthetic_baseline_llh.pos').read_text()
        reference_line=next(line for line in text.splitlines() if line.startswith('% ref pos'))
        prefix,height=reference_line.rsplit(' ',1)
        altered=b.parse_source(text.replace(reference_line,prefix+' '+repr(float(height)+.01)).encode(),'synthetic_changed_base.pos')
        with self.assertRaisesRegex(ValueError,'Processing-base'): b.plan_import(p,altered,src['rows'][0]['line'],options(target='P02',identifier='B2'))
        for field,value in (('fingerprint','0'*64),('native_covariance',[1.,0.,0.,1.,0.,1.])):
            out=deepcopy(p); out['imports'][0]['records'][0][field]=value
            with self.assertRaises(ValueError): n.validate_project(out)

    def test_schema_one_migration_retains_original_file_and_rejects_unknown_fields(self):
        path=EXAMPLES/'network_inventory.sanet.json'; original=path.read_bytes()
        migrated=n.load_project(path)
        self.assertEqual(migrated['schema_version'],2); self.assertEqual(migrated['imports'],[])
        self.assertEqual(path.read_bytes(),original)
        bad=json.loads(original); bad['unknown_model']='must not disappear'
        with self.assertRaises(ValueError): n.loads_project(json.dumps(bad))

    def test_audit_allows_platform_roundoff_but_preserves_stored_measurements(self):
        src=source(); p=b.plan_import(n.synthetic_project(),src,src['rows'][0]['line'],options())
        original=p['imports'][0]['records'][0]['observation']
        original['values'][0]+=1e-9; p['observations'][-1]['values'][0]+=1e-9
        self.assertEqual(n.loads_project(n.dumps_project(p)),p)
        original['values'][0]+=.001; p['observations'][-1]['values'][0]+=.001
        with self.assertRaises(ValueError): n.validate_project(p)

    def test_import_size_limit_and_structural_headers_after_data_are_not_silently_accepted(self):
        src=source(); p=n.synthetic_project(); before=deepcopy(p)
        with patch.object(n,'MAX_BYTES',100),self.assertRaisesRegex(ValueError,'20 MB'):
            b.plan_import(p,src,src['rows'][0]['line'],options())
        self.assertEqual(p,before)
        text=(EXAMPLES/'synthetic_baseline_llh.pos').read_text()
        with self.assertRaisesRegex(ValueError,'precede'): b.parse_source((text+'% ref pos : 60 5 80\n').encode(),'synthetic.pos')


if __name__=='__main__':
    (ROOT/'test_output').mkdir(exist_ok=True)
    unittest.main(verbosity=2)
