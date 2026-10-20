# SPDX-License-Identifier: GPL-3.0-only
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import csv,math,tempfile,unittest
from test_gnss_processing import ROOT,ENGINE,options,handoff
from survey_adjustment import antenna as a,gnss_demo as d,gnss_processing as p,network as n,baselines as b
from survey_adjustment.coordinate_job import llh

AZ_ENGINE=ROOT/'upstream/rtklib-sa2-reviewed-v2/rnx2rtkp.exe'


def azimuth_options(project,folder,**changes):
    return options(project,systems=tuple('GECR'),engine_variant='survey-sa2',calibration='antex',
        antex_path=str(Path(folder)/'synthetic.atx'),calibration_note='Own invented grid and orientation; no receiver accuracy claim.',
        base_azimuth_deg=0.,rover_azimuth_deg=37.,orientation_reviewed=True,orientation_mode='fixed-level',**changes)


class AzimuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT/'test_output');self.root=Path(self.tmp.name)
        self.project=d.create(self.root/'input',systems=tuple('GECR'),calibrated=True,azimuth=True)
        self.opts=azimuth_options(self.project,self.root/'input')
    def tearDown(self):self.tmp.cleanup()

    def test_bilinear_interpolation_wrap_and_heading_have_hand_computed_answers(self):
        values=dict(neu_mm=[0.,0.,0.],noazi_mm=[999.]*19,
            azi_mm=[[float(angle),*[offset+2*j for j in range(19)]] for angle,offset in ((0,0),(90,20),(180,40),(270,60),(360,0))])
        model=dict(dazi=90.,zenith=[0.,90.,5.],frequencies={'G01':values})
        a.validate_grid(model)
        self.assertAlmostEqual(a.correction(model,'G01',45,12.5),.015)
        self.assertAlmostEqual(a.correction(model,'G01',315,12.5),.035)
        self.assertAlmostEqual(a.correction(model,'G01',135,12.5,90),.015)
        self.assertAlmostEqual(a.correction(model,'G01',0,12.5),a.correction(model,'G01',360,12.5))
        values['neu_mm']=[100.,0.,0.];values['azi_mm']=[[row[0],*[0.]*19] for row in values['azi_mm']]
        self.assertAlmostEqual(a.correction(model,'G01',90,90,90),-.1)

    def test_incomplete_duplicate_nonfinite_and_seam_grids_blocked(self):
        original=d.multi_model(True,tuple('GECR'),True);original['zenith']=[0.,90.,5.]
        for kind in ('missing','duplicate','nan','seam','step'):
            model=deepcopy(original);rows=model['frequencies']['G01']['azi_mm']
            if kind=='missing':rows.pop(20)
            if kind=='duplicate':rows[20][0]=rows[19][0]
            if kind=='nan':rows[20][4]=math.nan
            if kind=='seam':rows[-1][3]+=.1
            if kind=='step':model['dazi']=7.
            with self.subTest(kind=kind),self.assertRaises(ValueError):a.validate_grid(model)

    def test_actual_ids_grid_samples_and_provider_comments_survive_selection(self):
        path=Path(self.opts.antex_path);text=path.read_text()
        note=d.header('SYNTHETIC: COPIED WITHOUT VERIFICATION BY CALIBRATION!','COMMENT')
        text=text.replace(d.header('     5.0','DAZI'),note+d.header('     5.0','DAZI'))
        path.write_text(text,encoding='ascii')
        model=p.calibration_review(self.opts,*self.project['raw_sources'][:2])
        self.assertEqual(set(model['base']['frequencies']),{'G01','G02','R01','R02','E01','E05','C01','C06'})
        self.assertTrue(any('WITHOUT VERIFICATION' in s for s in model['base']['comments']))
        selected=model['base'];self.assertEqual(len(selected['frequencies']['C01']['azi_mm']),73)
        subset=self.root/'subset.atx';subset.write_bytes(a.subset([('TEST            NONE',selected)]))
        self.assertEqual(a.read(subset)['entries'][0]['frequencies'],selected['frequencies'])
        self.assertNotEqual(selected['frequencies']['C01']['azi_mm'],selected['frequencies']['E01']['azi_mm'])

    def test_wrong_bands_and_legacy_azimuth_fallback_are_refused(self):
        cat=a.read(self.opts.antex_path);entry=cat['entries'][0]
        entry['frequencies']['E07']=entry['frequencies'].pop('E05')
        with self.assertRaisesRegex(ValueError,'E05'):a.select(cat,self.project['raw_sources'][0]['metadata'],2,tuple('GECR'),True)
        with self.assertRaisesRegex(ValueError,'DAZI'):a.select(a.read(self.opts.antex_path),self.project['raw_sources'][0]['metadata'],2,('G',))

    def test_signal_plan_forces_the_same_common_code_and_phase(self):
        base,rover=deepcopy(self.project['raw_sources'][:2]);base['metadata']['observation_types']['C']+=['C1D','L1D']
        plan=p.selected_signals(self.opts,base,rover)
        self.assertEqual(plan['C'],['1P','6I']);self.assertEqual(plan['E'],['1C','5Q'])
        conf=p.configuration(self.opts,base,rover)
        self.assertEqual(conf.count('-CL1P'),2);self.assertEqual(conf.count('-EL5Q'),2)
        rover['metadata']['observation_types']['C']=['C2I','L2I','C6I','L6I']
        with self.assertRaisesRegex(ValueError,'common reviewed'):p.selected_signals(self.opts,base,rover)

    def test_missing_unreviewed_or_conflicting_orientation_cannot_start(self):
        for change in (dict(base_azimuth_deg=None),dict(rover_azimuth_deg=360.),dict(orientation_reviewed=False),dict(rover_azimuth_deg=0.),dict(orientation_mode='variable'),dict(orientation_mode='')):
            with self.subTest(change=change),self.assertRaises(ValueError):p.validate(self.project,replace(self.opts,**change))
        with self.assertRaisesRegex(ValueError,'requires an explicit'):p.validate(self.project,replace(self.opts,calibration='none',antex_path='',calibration_note=''))
        with self.assertRaisesRegex(ValueError,'legacy engines'):p.validate(self.project,replace(self.opts,engine_variant='survey-sa1'))

    def test_galileo_fnav_is_required_for_selected_clock_profile(self):
        nav=deepcopy(self.project['raw_sources'][2]);original=Path(nav['path']).read_text()
        lines=original.splitlines()
        for index,line in enumerate(lines):
            if line.startswith('E') and len(line)>40:lines[index+5]=lines[index+5][:23]+f'{517.:19.12E}'+lines[index+5][42:]
        path=self.root/'inav.rnx';path.write_text('\n'.join(lines)+'\n',encoding='ascii');nav['path']=str(path)
        with self.assertRaisesRegex(ValueError,'navigation for every selected system'):p._navigation_check(nav,self.opts.systems,True)

    def test_motion_body_frame_and_already_corrected_headers_are_not_ignored(self):
        for label,message in (('ANTENNA: B.SIGHT XYZ','body-frame'),('ANTENNA: ZERODIR XYZ','body-frame'),('SYS / PCVS APPLIED','already applied')):
            project=deepcopy(self.project)
            project['raw_sources'][1]['metadata']['header'].insert(-1,d.header('1',label).rstrip())
            with self.subTest(label=label),self.assertRaisesRegex(ValueError,message):p.validate(project,self.opts,preflight=False)
        project=deepcopy(self.project);project['raw_sources'][1]['metadata']['events']['2']=1
        with self.assertRaisesRegex(ValueError,'antenna motion'):p.validate(project,self.opts,preflight=False)

    @unittest.skipUnless(AZ_ENGINE.is_file(),'Separate SA2 engine intentionally not bundled')
    def test_actual_static_grid_audit_baseline_covariance_height_and_reopen(self):
        result,source=p.run(self.root/'job',self.project,self.opts,AZ_ENGINE)
        self.assertEqual(result['epochs'],121);self.assertGreater(result['eligible_fix'],100)
        self.assertEqual(set(result['used_satellites']),set('GECR'));self.assertIn('independently recalculated',result['receiver_calibration_audit'])
        final=source['rows'][-1];self.assertLess(math.dist(final['values'],d.rover_at(600,False)),.04)
        imported=b.plan_import(self.project,source,final['line'],handoff(self.project))
        self.assertEqual(imported['observations'][0]['covariance'],final['covariance'])
        self.assertEqual(n.loads_project(n.dumps_project(imported)),imported)
        self.assertEqual(b.read_source(self.root/'job/solution.pos')['processing']['profile'],p.AZIMUTH_PROFILE)
        with (self.root/'job/epochs.csv').open(newline='') as handle:rows=list(csv.DictReader(handle))
        for row,export in zip(source['rows'],rows):
            self.assertEqual(row['values'],[float(export[k]) for k in ('X ECEF m','Y ECEF m','Z ECEF m')])
            self.assertEqual(llh(row['values'],'EPSG:4979')[2],float(export['h WGS84 ellipsoid m']))
        _,repeat=p.run(self.root/'repeat',self.project,self.opts,AZ_ENGINE)
        with self.assertRaisesRegex(ValueError,'already imported'):p.validate_handoff(imported,repeat,handoff(self.project))

    @unittest.skipUnless(AZ_ENGINE.is_file(),'Separate SA2 engine intentionally not bundled')
    def test_moving_one_and_two_frequencies_keep_height_and_originals(self):
        moving=d.create(self.root/'moving',moving=True,systems=tuple('GECR'),calibrated=True,azimuth=True)
        original=deepcopy(moving)
        for frequencies in (1,2):
            opts=azimuth_options(moving,self.root/'moving',mode='kinematic',frequencies=frequencies)
            result,source=p.run(self.root/('moving_job'+str(frequencies)),moving,opts,AZ_ENGINE)
            self.assertGreater(result['eligible_fix'],40);self.assertTrue(result['used_satellites']['C'])
            self.assertLess(math.dist(source['rows'][-1]['values'],d.rover_at(600,True)),.04)
        self.assertEqual(original,moving);self.assertFalse(moving['observations'])

    @unittest.skipUnless(AZ_ENGINE.is_file(),'Separate SA2 engine intentionally not bundled')
    def test_missing_middle_grid_sample_wrong_signal_or_correction_blocks_acceptance(self):
        folder=self.root/'job';p.run(folder,self.project,self.opts,AZ_ENGINE)
        manifest=p.load_manifest(folder);console=(folder/'console.txt').read_text()
        for kind in ('missing','signal','orientation','correction'):
            lines=console.splitlines()
            for i,line in enumerate(lines):
                if kind=='missing' and line.startswith('SA_AZI,') and ',E,1,25,' in line:lines.pop(i);break
                if kind=='signal' and line.startswith('SA_APPLY,') and ',C,0,1P,' in line:lines[i]=line.replace(',1P,',',2I,');break
                if kind=='orientation' and line.startswith('SA_ORIENT,'):lines[i]=line.replace(',0.000000000,',',90.000000000,');break
                if kind=='correction' and line.startswith('SA_APPLY,'):lines[i]=','.join(line.split(',')[:-1]+['99.0']);break
            with self.subTest(kind=kind),self.assertRaises(ValueError):p.verify_azimuth_audit('\n'.join(lines),manifest)

    @unittest.skipUnless(AZ_ENGINE.is_file(),'Separate SA2 engine intentionally not bundled')
    def test_wrong_engine_cannot_create_a_job(self):
        with self.assertRaisesRegex(ValueError,'chosen engine profile'):p.prepare(self.root/'wrong',self.project,self.opts,ENGINE)
        self.assertFalse((self.root/'wrong').exists())
