# SPDX-License-Identifier: GPL-3.0-only
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import json,math,tempfile,unittest,csv
from test_gnss_processing import ROOT,ENGINE,options,handoff
from survey_adjustment import gnss_processing as p,gnss_demo as d,antenna,network as n,baselines as b,rinex as r
from survey_adjustment.coordinate_job import llh
SA_ENGINE=ROOT/'upstream/rtklib-sa1-final/rnx2rtkp.exe'


def extended_options(project,folder,**changes):
    return options(project,systems=('G','E','C','R'),engine_variant='survey-sa1',calibration='antex',
        antex_path=str(Path(folder)/'synthetic.atx'),calibration_note='Own invented per-system models, source and mounting known for software integration only.',**changes)


class SystemEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT/'test_output');self.root=Path(self.tmp.name)
        self.project=d.create(self.root/'input',systems=('G','E','C','R'),calibrated=True)
        self.opts=extended_options(self.project,self.root/'input')
    def tearDown(self):self.tmp.cleanup()

    def test_actual_frequency_ids_and_distinct_models_never_relabel_gps(self):
        model=p.calibration_review(self.opts,*self.project['raw_sources'][:2])
        self.assertEqual(set(model['base']['frequencies']),{'G01','G02','E01','E07','C02','C07','R01','R02'})
        self.assertNotEqual(model['base']['frequencies']['G01'],model['base']['frequencies']['E01'])
        self.assertNotEqual(model['base']['frequencies']['C02'],model['base']['frequencies']['R01'])
        cat=antenna.read(self.opts.antex_path);del cat['entries'][0]['frequencies']['E07'];cat['entries'][0]['count']-=1
        with self.assertRaisesRegex(ValueError,'E07'):antenna.select(cat,self.project['raw_sources'][0]['metadata'],2,self.opts.systems)

    def test_legacy_profile_cannot_enable_glonass_or_mixed_calibration(self):
        with self.assertRaisesRegex(ValueError,'GLONASS requires'):p.validate(self.project,replace(self.opts,engine_variant='upstream'))
        conf=p.configuration(self.opts,*self.project['raw_sources'][:2]);self.assertIn('pos1-navsys=45\n',conf)
        self.assertIn('pos2-gloarmode=off\n',conf)

    def test_glonass_channel_validation_and_missing_satellite_record(self):
        nav=self.project['raw_sources'][2];path=Path(nav['path']);text=path.read_text()
        lines=text.splitlines();index=next(i for i,line in enumerate(lines) if line.startswith('R01 '))
        # Second continuation line, fourth number: FDMA frequency channel.
        lines[index+2]=lines[index+2][:61]+f'{9.:19.12E}'
        path.write_text('\n'.join(lines)+'\n',encoding='ascii');fresh=r.read_source(path)
        with self.assertRaisesRegex(ValueError,'FDMA channel'):p._navigation_check(fresh,self.opts.systems)
        lines[index+2]=lines[index+2][:61]+' '*19
        path.write_text('\n'.join(lines)+'\n',encoding='ascii');fresh=r.read_source(path)
        with self.assertRaisesRegex(ValueError,'Missing GLONASS FDMA channel'):p._navigation_check(fresh,self.opts.systems)
        path.write_text(text,encoding='ascii')
        bad=deepcopy(self.project);bad['raw_sources'][2]['metadata']['satellites']=[s for s in nav['metadata']['satellites'] if not s.startswith('R')]
        # Catalogue tampering is detected even before a missing-channel fallback.
        with self.assertRaises(ValueError):p.validate(bad,self.opts)

    @unittest.skipUnless(SA_ENGINE.is_file(),'Separately built reviewed experimental engine not bundled')
    def test_wrong_executable_profile_blocked_before_run(self):
        with self.assertRaisesRegex(ValueError,'chosen engine profile'):p.prepare(self.root/'wrong',self.project,self.opts,ENGINE)
        self.assertFalse((self.root/'wrong').exists())

    @unittest.skipUnless(SA_ENGINE.is_file(),'Separate experimental engine not bundled')
    def test_actual_static_four_systems_calibration_covariance_and_height_export(self):
        original=deepcopy(self.project);result,source=p.run(self.root/'job',self.project,self.opts,SA_ENGINE)
        self.assertEqual(result['epochs'],121);self.assertGreater(result['eligible_fix'],100)
        self.assertEqual(set(result['used_satellites']),set(self.opts.systems));self.assertTrue(all(len(v)>=4 for v in result['used_satellites'].values()))
        self.assertIn('Every selected',result['receiver_calibration_audit'])
        final=source['rows'][-1];self.assertLess(math.dist(final['values'],d.rover_at(600,False)),.04)
        imported=b.plan_import(self.project,source,final['line'],handoff(self.project))
        self.assertEqual(imported['observations'][0]['covariance'],final['covariance']);self.assertEqual(n.loads_project(n.dumps_project(imported)),imported)
        self.assertEqual(imported['imports'][0]['processing']['profile'],p.SYSTEM_PROFILE)
        self.assertEqual(original,self.project)
        with (self.root/'job/epochs.csv').open(newline='') as handle:export=list(csv.DictReader(handle))[-1]
        self.assertEqual(float(export['Z ECEF m']),final['values'][2]);self.assertEqual(float(export['h WGS84 ellipsoid m']),llh(final['values'],'EPSG:4979')[2])
        for raw in self.project['raw_sources']:self.assertEqual(r.read_source(raw['path'])['source_sha256'],raw['source_sha256'])
        self.assertEqual(b.read_source(self.root/'job/solution.pos')['processing'],source['processing'])

    @unittest.skipUnless(SA_ENGINE.is_file(),'Separate engine not bundled')
    def test_actual_moving_four_systems_and_single_frequency(self):
        moving=d.create(self.root/'moving',systems=self.opts.systems,calibrated=True,moving=True)
        for frequencies in (1,2):
            opts=extended_options(moving,self.root/'moving',mode='kinematic',frequencies=frequencies)
            result,source=p.run(self.root/('job'+str(frequencies)),moving,opts,SA_ENGINE)
            self.assertGreater(result['eligible_fix'],40);self.assertTrue(result['used_satellites']['R'])
            self.assertLess(math.dist(source['rows'][-1]['values'],d.rover_at(600,True)),.04)
        self.assertFalse(moving['observations'])

    @unittest.skipUnless(SA_ENGINE.is_file(),'Separate engine not bundled')
    def test_engine_audit_must_confirm_every_receiver_system_and_frequency(self):
        folder=self.root/'job';p.run(folder,self.project,self.opts,SA_ENGINE)
        manifest=p.load_manifest(folder);manifest['state']='PREPARED';manifest['outputs']={};p._write(folder,manifest)
        path=folder/'console.txt';text=path.read_text();path.write_text(text.replace('SA_PCV,SA_ROVER        NONE,E,1,','REMOVED_PCV,SA_ROVER        NONE,E,1,'))
        with self.assertRaisesRegex(ValueError,'E07 receiver calibration'):p.inspect_result(folder,0,SA_ENGINE)

    @unittest.skipUnless(SA_ENGINE.is_file(),'Separate engine not bundled')
    def test_calibration_change_does_not_make_new_independent_occupation(self):
        _,source=p.run(self.root/'first',self.project,self.opts,SA_ENGINE)
        imported=b.plan_import(self.project,source,source['rows'][-1]['line'],handoff(self.project))
        _,alternative=p.run(self.root/'alternative',self.project,replace(self.opts,calibration='none',antex_path='',calibration_note=''),SA_ENGINE)
        with self.assertRaisesRegex(ValueError,'already imported'):p.validate_handoff(imported,alternative,handoff(self.project))
