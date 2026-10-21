# SPDX-License-Identifier: GPL-3.0-only
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import csv,hashlib,json,math,tempfile,unittest,sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from survey_adjustment import gnss_demo as d,gnss_processing as p,network as n,baselines as b,rinex as r
ENGINE=ROOT/'test_output/ppk_engine_0100/rnx2rtkp.exe'


def options(project,**changes):
    base,rover,nav=project['raw_sources']
    return replace(p.ProcessingOptions(base=base['id'],rover=rover['id'],navigation=nav['id'],base_xyz=tuple(d.BASE),
        frame='EPSG:4978',epoch=project['epoch'],atmosphere='ideal-synthetic',antenna_note='SYNTHETIC zero antenna offsets / electrical endpoints.',
        model_note='SYNTHETIC ideal signals; fixed base held exact; no field accuracy claim.',base_reviewed=True,antenna_reviewed=True,model_reviewed=True,license_reviewed=True),**changes)


def handoff(project):
    return b.ImportOptions(frame='EPSG:4978',epoch=project['epoch'],origin='SIM_BASE',target='SIM_ROVER',session='SIM01',identifier='RAW01',
        antenna_note='SYNTHETIC endpoints, zero offsets.',uncertainty_note='Synthetic fixed base and correlated raw occupation.',
        relative_confirmed=True,endpoints_confirmed=True,covariance_confirmed=True,synthetic=True,
        stationary_confirmed=True,occupation_note='Constructed stationary ten minute occupation; final fixed filter state, not an averaged result.')


class StaticAtmosphereTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT/'test_output');self.root=Path(self.tmp.name)
        self.project=d.create(self.root/'example',long_static=True);self.opts=options(self.project,atmosphere=p.STATIC_ATMOSPHERE)
    def tearDown(self):self.tmp.cleanup()
    def stat_example(self):
        return '\n'.join(('$ION,2441,28818.000,1,G02,90,45,1.2,1.3',
            '$TROP,2441,28818.000,1,1,0.14,0.15','$TROP,2441,28818.000,1,2,0.11,0.12',
            '$SAT,2441,28818.000,G02,1,90,45,0.01,0.02,1',
            '$SAT,2441,28818.000,G02,2,90,45,0.01,0.02,1'))+'\n'
    def test_static_dual_gps_profile_rejects_unsupported_modes_and_models(self):
        for change in (dict(mode='kinematic'),dict(frequencies=1),dict(systems=('G','E')),dict(engine_variant='survey-sa1')):
            with self.subTest(change=change),self.assertRaisesRegex(ValueError,'initial estimated-atmosphere'):p.validate(self.project,replace(self.opts,**change))
        conf=p.configuration(self.opts,*self.project['raw_sources'][:2]);self.assertIn('pos1-ionoopt=est-stec\n',conf);self.assertIn('pos1-tropopt=est-ztd\n',conf)
        self.assertIn('stats-prniono=0.001\n',conf);self.assertIn('pos1-sateph=brdc\n',conf)
        with self.assertRaisesRegex(ValueError,'stationary GPS'):d.create(self.root/'unsupported',long_static=True,moving=True)
    def test_static_station_or_header_changes_are_not_silently_merged(self):
        for role in ('BASE','ROVER'):
            for flag in ('2','3','4'):
                project=deepcopy(self.project);source=next(s for s in project['raw_sources'] if s['role']==role);source['metadata']['events'][flag]=1
                with self.subTest(role=role,flag=flag),self.assertRaisesRegex(ValueError,'Split and review'):p.validate(project,self.opts,preflight=False)
    def test_exact_epoch_state_audit_retains_float_fixed_and_wet_semantics(self):
        result=p.static_atmosphere_audit(self.stat_example(),[dict(time='2026-10-18T08:00:18',quality='1')])
        self.assertEqual(result['epochs'],1);self.assertEqual(result['wet_delay_records'],2);self.assertEqual(result['ionosphere_records'],1)
        self.assertEqual(result['rows'][0]['float_m'],1.2);self.assertEqual(result['rows'][0]['fixed_m'],1.3)
        self.assertIn('not total zenith',result['note']);self.assertIn('not independent',p._atmosphere_csv(result).decode())
    def test_missing_duplicate_nan_wrong_time_quality_or_frequency_state_is_rejected(self):
        text=self.stat_example();lines=text.splitlines()
        cases=[text.replace(lines[0]+'\n',''),text.replace(lines[2]+'\n',''),text.replace(lines[-1]+'\n',''),text+lines[0]+'\n',
            text.replace('1.2,1.3','NaN,1.3'),text.replace('28818.000','28818.001',1),text.replace('28818.000,1,G02','28818.000,2,G02'),text.replace('G02,2,','G02,3,')]
        for index,changed in enumerate(cases):
            with self.subTest(index=index),self.assertRaises(ValueError):p.static_atmosphere_audit(changed,[dict(time='2026-10-18T08:00:18',quality='1')])
    @unittest.skipUnless(ENGINE.is_file(),'Reviewed external engine intentionally not bundled')
    def test_actual_60km_static_atmosphere_states_height_baseline_and_reprocessing_gate(self):
        original=deepcopy(self.project);result,source=p.run(self.root/'estimated',self.project,self.opts,ENGINE)
        final=source['rows'][-1];self.assertTrue(final['eligible']);self.assertGreater(result['eligible_fix'],0)
        self.assertLess(math.dist(final['values'],d.long_static_rover()),.05)
        self.assertGreater(result['computed_baseline_length_m']['last'],60000)
        self.assertEqual(result['atmosphere_audit']['epochs'],121);self.assertEqual(result['atmosphere_audit']['wet_delay_records'],242)
        self.assertEqual(len(b.read_source(self.root/'estimated/solution.pos')['rows']),121)
        with (self.root/'estimated/epochs.csv').open() as handle:rows=list(csv.DictReader(handle))
        from survey_adjustment.coordinate_job import llh
        self.assertEqual(float(rows[-1]['h WGS84 ellipsoid m']),llh(final['values'],'EPSG:4979')[2])
        selected=replace(handoff(self.project),occupation_note='SYNTHETIC one-hour stationary point; final conditional filter state only.')
        imported=b.plan_import(self.project,source,final['line'],selected)
        self.assertEqual(imported['observations'][0]['covariance'],final['covariance'])
        self.assertEqual(imported['imports'][0]['processing']['profile'],p.STATIC_PROFILE)
        self.assertEqual(imported['imports'][0]['processing']['atmosphere_model'],p.STATIC_ATMOSPHERE)
        self.assertEqual(n.loads_project(n.dumps_project(imported)),imported)
        broadcast,other=p.run(self.root/'broadcast',self.project,replace(self.opts,atmosphere='broadcast/saas'),ENGINE)
        self.assertGreater(math.dist(other['rows'][-1]['values'],d.long_static_rover()),.2)
        with self.assertRaisesRegex(ValueError,'already imported'):p.validate_handoff(imported,other,selected)
        self.assertEqual(original,self.project)
    @unittest.skipUnless(ENGINE.is_file(),'Reviewed external engine intentionally not bundled')
    def test_rehashed_atmosphere_table_cannot_override_original_engine_states(self):
        folder=self.root/'estimated';p.run(folder,self.project,self.opts,ENGINE)
        path=folder/'atmosphere_states.csv';path.write_bytes(path.read_bytes().replace(b'0.1496',b'9.1496',1))
        manifest=p.load_manifest(folder);manifest['outputs'][path.name]=p.engine.sha256(path);p._write(folder,manifest)
        with self.assertRaisesRegex(ValueError,'recalculated engine'):b.read_source(folder/'solution.pos')


class ProcessingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT/'test_output');self.root=Path(self.tmp.name)
        self.project=d.create(self.root/'example');self.options=options(self.project)
    def tearDown(self):self.tmp.cleanup()

    def test_constructed_observations_are_separate_from_parser_fixtures(self):
        self.assertEqual(r.inspect_catalogue(self.project)['status'],'RAW_CATALOGUE_CHECKED')
        self.assertEqual(self.project['raw_sources'][0]['metadata']['epochs'],121)
        self.assertFalse(self.project['observations']);self.assertTrue(self.project['synthetic'])
        self.assertGreater(len(self.project['raw_sources'][0]['metadata']['satellites']),4)
        with self.assertRaises(FileExistsError):d.create(self.root/'example')

    def test_required_reviews_base_model_and_gps_time_reject_unsafe_assumptions(self):
        for values in (dict(base_reviewed=False),dict(license_reviewed=False),dict(base_xyz=(0,0,0)),dict(mode='single'),dict(frame=''),dict(epoch='2000-01-01'),dict(ratio=1),dict(frequencies=3)):
            with self.subTest(values=values),self.assertRaises(ValueError):p.validate(self.project,replace(self.options,**values))
        damaged=deepcopy(self.project);damaged['raw_sources'][1]['utc_offset_seconds']=19
        with self.assertRaisesRegex(ValueError,'same documented'):p.validate(damaged,self.options)

    def test_nonphysical_parser_navigation_is_blocked_before_engine_execution(self):
        path=ROOT/'survey_adjustment/examples/synthetic_navigation.rnx'
        with self.assertRaisesRegex(ValueError,'nonphysical'):p._navigation_check(r.read_source(path))

    def test_unknown_engine_and_engine_name_rejected(self):
        exe=self.root/'rnx2rtkp.exe';exe.write_bytes(b'not an engine')
        with self.assertRaisesRegex(ValueError,'Unreviewed'):p.program(exe)
        with self.assertRaises(ValueError):p.program(self.root/'other.exe')

    def test_configuration_applies_header_hen_in_engine_enu_order(self):
        base,rover=self.project['raw_sources'][:2];base=deepcopy(base);rover=deepcopy(rover)
        base['metadata']['antenna_hen']=[1.2,.1,.2];rover['metadata']['antenna_hen']=[2.3,.3,.4]
        text=p.configuration(self.options,base,rover)
        self.assertIn('ant2-antdele=0.1\n',text);self.assertIn('ant2-antdeln=0.2\n',text);self.assertIn('ant1-antdelu=2.3\n',text)
        self.assertIn('pos1-dynamics=off',text);self.assertIn('out-solstatic=all',text)
        real=deepcopy(self.project);real['synthetic']=False
        for row in real['raw_sources']:row['synthetic']=False
        with self.assertRaisesRegex(ValueError,'Atmosphere-off'):p.validate(real,self.options)

    @unittest.skipUnless(ENGINE.is_file(),'Reviewed external engine is intentionally not bundled')
    def test_real_static_engine_roundtrip_full_covariance_and_existing_baseline_import(self):
        original=deepcopy(self.project);hashes={row['path']:hashlib.sha256(Path(row['path']).read_bytes()).hexdigest() for row in self.project['raw_sources']}
        result,source=p.run(self.root/'static',self.project,self.options,ENGINE)
        self.assertEqual(result['epochs'],121);self.assertGreater(result['eligible_fix'],100)
        final=source['rows'][-1];self.assertTrue(final['eligible']);self.assertLess(math.dist(final['values'],d.rover_at(600,False)),.03)
        reopened=b.read_source(self.root/'static/solution.pos');self.assertEqual(reopened['processing'],source['processing'])
        settings=handoff(self.project)
        with self.assertRaisesRegex(ValueError,'stationary'):b.plan_import(self.project,reopened,final['line'],replace(settings,stationary_confirmed=False))
        imported=b.plan_import(self.project,reopened,final['line'],settings)
        self.assertEqual(len(imported['observations']),1);self.assertEqual(len(imported['imports']),1)
        self.assertEqual(imported['observations'][0]['covariance'],final['covariance'])
        self.assertEqual(n.loads_project(n.dumps_project(imported)),imported)
        with self.assertRaises(ValueError):n.remove_record(imported,'raw_sources',0)
        self.assertEqual(self.project,original)
        self.assertEqual(hashes,{path:hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in hashes})
        with (self.root/'static/epochs.csv').open(newline='') as handle:
            rows=list(csv.DictReader(handle));self.assertEqual(len(rows),121);self.assertAlmostEqual(float(rows[-1]['h WGS84 ellipsoid m']),__import__('survey_adjustment.coordinate_job',fromlist=['llh']).llh(final['values'],'EPSG:4979')[2])

    @unittest.skipUnless(ENGINE.is_file(),'External engine not bundled')
    def test_real_ppk_tracks_motion_and_is_not_automatically_imported(self):
        moving=d.create(self.root/'moving',moving=True)
        result,source=p.run(self.root/'ppk',moving,options(moving,mode='kinematic'),ENGINE)
        self.assertEqual(result['mode'],'kinematic');self.assertGreater(result['eligible_fix'],50)
        self.assertLess(math.dist(source['rows'][-1]['values'],d.rover_at(600,True)),.03)
        self.assertFalse(moving['observations']);self.assertFalse(moving['imports'])
        self.assertGreater(source['rows'][-1]['values'][0]-source['rows'][0]['values'][0],11.)

    @unittest.skipUnless(ENGINE.is_file(),'External engine not bundled')
    def test_real_engine_gps_l1_profile(self):
        result,source=p.run(self.root/'l1',self.project,replace(self.options,frequencies=1),ENGINE)
        self.assertGreater(result['eligible_fix'],50)
        self.assertLess(math.dist(source['rows'][-1]['values'],d.rover_at(600,False)),.04)

    @unittest.skipUnless(ENGINE.is_file(),'External engine not bundled')
    def test_reprocessed_occupation_and_relabelled_session_cannot_make_independent_baseline(self):
        _,source=p.run(self.root/'first',self.project,self.options,ENGINE);final=source['rows'][-1]
        imported=b.plan_import(self.project,source,final['line'],handoff(self.project))
        _,second=p.run(self.root/'second',self.project,replace(self.options,mode='kinematic'),ENGINE)
        with self.assertRaisesRegex(ValueError,'already imported'):p.validate_handoff(imported,second,handoff(self.project))
        with self.assertRaisesRegex(ValueError,'new label'):p.validate_handoff(imported,second,replace(handoff(self.project),session='RENAMED'))

    @unittest.skipUnless(ENGINE.is_file(),'External engine not bundled')
    def test_tampered_input_configuration_output_and_report_are_rejected(self):
        folder=self.root/'run';p.run(folder,self.project,self.options,ENGINE)
        for name in ('base.rnx','processing.conf','solution.pos','report.txt'):
            path=folder/name;original=path.read_bytes();path.write_bytes(original+b'\n')
            with self.subTest(name=name),self.assertRaises(ValueError):b.read_source(folder/'solution.pos')
            path.write_bytes(original)
        self.assertIsNotNone(b.read_source(folder/'solution.pos')['processing'])

    @unittest.skipUnless(ENGINE.is_file(),'External engine not bundled')
    def test_real_engine_applies_reviewed_rover_height_offset(self):
        from survey_adjustment.coordinate_job import llh
        reference=self.project['raw_sources'][1];path=Path(reference['path'])
        lines=path.read_text().splitlines()
        for i,line in enumerate(lines):
            if line[60:80].strip()=='ANTENNA: DELTA H/E/N':lines[i]=d.header(''.join(f'{v:14.4f}' for v in (1.5,0.,0.)),'ANTENNA: DELTA H/E/N').rstrip('\n')
        path.write_text('\n'.join(lines)+'\n',encoding='ascii')
        fresh=r.read_source(path)
        for key in ('source_sha256','fingerprint','size_bytes','expanded_bytes','metadata'):reference[key]=fresh[key]
        result,source=p.run(self.root/'height',self.project,self.options,ENGINE)
        expected=llh(d.rover_at(600,False),'EPSG:4979')[2]-1.5
        self.assertAlmostEqual(llh(source['rows'][-1]['values'],'EPSG:4979')[2],expected,delta=.04)

    @unittest.skipUnless(ENGINE.is_file(),'External engine not bundled')
    def test_prepared_aborted_missing_output_and_nonzero_exit_never_import(self):
        folder=self.root/'pending';p.prepare(folder,self.project,self.options,ENGINE)
        with self.assertRaises(ValueError):p.inspect_result(folder,1,ENGINE)
        with self.assertRaisesRegex(ValueError,'Missing'):p.inspect_result(folder,0,ENGINE)
        (folder/'solution.pos').write_text('% incomplete\n')
        with self.assertRaisesRegex(ValueError,'incomplete'):b.read_source(folder/'solution.pos')
        p.abort(folder,'Test stop');self.assertEqual(p.load_manifest(folder)['state'],'ABORTED')
        with self.assertRaises(ValueError):b.read_source(folder/'solution.pos')
