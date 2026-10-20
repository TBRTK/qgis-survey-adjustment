# SPDX-License-Identifier: GPL-3.0-only
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import csv,json,math,tempfile,unittest
from unittest.mock import patch
from test_gnss_processing import ROOT,ENGINE,options,handoff
from survey_adjustment import gnss_demo as d,gnss_processing as p,network as n,baselines as b
from survey_adjustment import antenna
from survey_adjustment.coordinate_job import llh


class MultisystemTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT/'test_output');self.root=Path(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()
    def example(self,calibrated=False,moving=False,systems=('G','E','C')):
        project=d.create(self.root/'example',moving=moving,systems=systems,calibrated=calibrated)
        opts=options(project,systems=systems,mode='kinematic' if moving else 'static',calibration='antex' if calibrated else 'none',
            antex_path=str(self.root/'example/synthetic.atx') if calibrated else '',calibration_note='Own invented model, integration test only.' if calibrated else '')
        return project,opts

    def test_signal_slots_and_explicit_selection_no_unsupported_substitution(self):
        project,opts=self.example()
        p.validate(project,opts);config=p.configuration(opts,*project['raw_sources'][:2])
        self.assertIn('pos1-navsys=41\n',config);self.assertIn('pos2-bdsarmode=off',config)
        for systems in ((),('E',),('G','G'),('G','R'),('G','J')):
            with self.subTest(systems=systems),self.assertRaises(ValueError):p.validate(project,replace(opts,systems=systems))
        damaged=deepcopy(project);damaged['raw_sources'][1]['metadata']['observation_types']['E']=['C1C','L1C','C5Q','L5Q']
        with self.assertRaisesRegex(ValueError,'E5a'):p.validate(damaged,opts,preflight=False)

    def test_calibration_mixed_systems_and_ignored_files_blocked(self):
        project,opts=self.example()
        with self.assertRaisesRegex(ValueError,'multiple systems'):p.validate(project,replace(opts,calibration='antex'))
        with self.assertRaisesRegex(ValueError,'silently ignored'):p.validate(project,replace(opts,antex_path='unused.atx'))

    @unittest.skipUnless(ENGINE.is_file(),'External engine intentionally not bundled')
    def test_static_all_three_systems_actually_contribute_preserve_height_covariance_and_audit(self):
        project,opts=self.example();original=deepcopy(project)
        result,source=p.run(self.root/'job',project,opts,ENGINE)
        self.assertEqual(result['epochs'],121);self.assertGreater(result['eligible_fix'],100)
        self.assertEqual(set(result['used_satellites']),{'G','E','C'})
        self.assertTrue(all(len(sats)>=4 for sats in result['used_satellites'].values()))
        self.assertLess(math.dist(source['rows'][-1]['values'],d.rover_at(600,False)),.04)
        imported=b.plan_import(project,source,source['rows'][-1]['line'],handoff(project))
        self.assertEqual(n.loads_project(n.dumps_project(imported)),imported)
        self.assertEqual(imported['observations'][0]['covariance'],source['rows'][-1]['covariance'])
        self.assertEqual(imported['imports'][0]['processing']['systems'],['G','E','C']);self.assertEqual(original,project)
        with (self.root/'job/epochs.csv').open(newline='') as handle:row=list(csv.DictReader(handle))[-1]
        self.assertEqual(float(row['Z ECEF m']),source['rows'][-1]['values'][2])
        self.assertAlmostEqual(float(row['h WGS84 ellipsoid m']),llh(source['rows'][-1]['values'],'EPSG:4979')[2])
        legacy=deepcopy(source['processing']);legacy['profile']=p.PROFILE
        for key in ('systems','frequencies','calibration'):del legacy[key]
        p.validate_context(legacy)  # Historical 0.10 audits remain readable.

    @unittest.skipUnless(ENGINE.is_file(),'External engine intentionally not bundled')
    def test_ppk_all_three_tracks_motion_and_one_frequency_profile(self):
        project,opts=self.example(moving=True)
        for freq in (1,2):
            result,source=p.run(self.root/('job'+str(freq)),project,replace(opts,frequencies=freq),ENGINE)
            self.assertTrue(all(result['used_satellites'].values()));self.assertGreater(result['eligible_fix'],50)
            self.assertLess(math.dist(source['rows'][-1]['values'],d.rover_at(600,True)),.04)
            self.assertGreater(source['rows'][-1]['values'][0]-source['rows'][0]['values'][0],11.)
        self.assertFalse(project['observations'])

    @unittest.skipUnless(ENGINE.is_file(),'External engine intentionally not bundled')
    def test_actual_antenna_pco_pcv_and_hen_recover_marker_without_double_height_correction(self):
        project,opts=self.example(calibrated=True,systems=('G',))
        result,source=p.run(self.root/'calibrated',project,opts,ENGINE)
        truth=llh(d.rover_at(600,False),'EPSG:4979')[2]
        height=llh(source['rows'][-1]['values'],'EPSG:4979')[2]
        self.assertAlmostEqual(height,truth,delta=.04)
        self.assertEqual(result['calibration']['rover']['type'],'SIM_ROVERANT    NONE')
        uncal=replace(opts,calibration='none',antex_path='',calibration_note='')
        _,raw=p.run(self.root/'uncalibrated',project,uncal,ENGINE)
        self.assertGreater(abs(llh(raw['rows'][-1]['values'],'EPSG:4979')[2]-truth),.18)
        self.assertFalse(project['observations'])
        imported=b.plan_import(project,source,source['rows'][-1]['line'],handoff(project))
        self.assertEqual(n.loads_project(n.dumps_project(imported)),imported)
        with self.assertRaisesRegex(ValueError,'already imported'):p.validate_handoff(imported,raw,handoff(project))
        for name in ('receiver.atx','original.atx'):
            path=self.root/'calibrated'/name;data=path.read_bytes();path.write_bytes(data+b'\n')
            with self.assertRaisesRegex(ValueError,'calibration changed'):b.read_source(self.root/'calibrated/solution.pos')
            path.write_bytes(data)
        manifest=self.root/'calibrated/processing.json';data=manifest.read_text();bad=json.loads(data)
        bad['context']['calibration']['base']['frequencies']['G01']['neu_mm'][0]+=1
        manifest.write_text(json.dumps(bad))
        with self.assertRaisesRegex(ValueError,'original calibration'):b.read_source(self.root/'calibrated/solution.pos')
        manifest.write_text(data)

    @unittest.skipUnless(ENGINE.is_file(),'External engine intentionally not bundled')
    def test_selected_system_with_no_used_residuals_is_rejected(self):
        project,opts=self.example();folder=self.root/'job';p.run(folder,project,opts,ENGINE)
        manifest=p.load_manifest(folder);manifest['state']='PREPARED';manifest['outputs']={};p._write(folder,manifest)
        path=folder/'solution.pos.stat';path.write_text('\n'.join(line for line in path.read_text().splitlines() if ',E' not in line)+'\n')
        with self.assertRaisesRegex(ValueError,'no valid residuals'):p.inspect_result(folder,0,ENGINE)

    @unittest.skipUnless(ENGINE.is_file(),'External engine intentionally not bundled')
    def test_engine_uses_zenith_pcv_as_well_as_pco(self):
        original=d.synthetic_model
        def strong_pattern(base):
            model=original(base)
            for values in model['frequencies'].values():values['noazi_mm']=[v*20 for v in values['noazi_mm']]
            return model
        with patch.object(d,'synthetic_model',strong_pattern):project,opts=self.example(calibrated=True,systems=('G',))
        _,good=p.run(self.root/'pcv',project,opts,ENGINE)
        truth=llh(d.rover_at(600,False),'EPSG:4979')[2]
        self.assertAlmostEqual(llh(good['rows'][-1]['values'],'EPSG:4979')[2],truth,delta=.04)
        models=[]
        for base,name in ((True,'SIM_BASEANT     NONE'),(False,'SIM_ROVERANT    NONE')):
            model=strong_pattern(base)
            for values in model['frequencies'].values():values['noazi_mm']=[0.]*19
            models.append((name,model))
        path=self.root/'zero_pcv.atx';path.write_bytes(antenna.subset(models))
        _,wrong=p.run(self.root/'pco_only',project,replace(opts,antex_path=str(path)),ENGINE)
        self.assertGreater(abs(llh(wrong['rows'][-1]['values'],'EPSG:4979')[2]-truth),.08)
