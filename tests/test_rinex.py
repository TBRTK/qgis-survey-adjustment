# SPDX-License-Identifier: GPL-3.0-only
from copy import deepcopy
from dataclasses import replace
import gzip,json,tempfile,unittest,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT))
from survey_adjustment import rinex as r,network as n


def review(role='BASE',point='REF0',**changes):
    return replace(r.ImportReview(role=role,point=point,session='S01',utc_offset_seconds=18,
        time_evidence='SYNTHETIC GPS = UTC + 18 s test declaration.',antenna_evidence='SYNTHETIC antenna H/E/N retained, no reduction.',
        note='SYNTHETIC software test only.',synthetic=True,confirmed=True),**changes)


def catalogue(folder=None):
    project=n.synthetic_project()
    for name,role,point in (('base','BASE','REF0'),('rover','ROVER','P01'),('navigation','NAV','')):
        path=ROOT/'survey_adjustment/examples'/('synthetic_'+name+'.rnx')
        if folder:
            target=Path(folder)/path.name; target.write_bytes(path.read_bytes()); path=target
        source=r.read_source(path)
        project=r.plan_import(project,source,review(role,point))
    return project


def h(value,label): return f'{value:<60}{label:<20}\n'


def legacy():
    # Constructed 2.11, six types and thirteen satellites exercise both kinds
    # of mandatory continuation. No actual receiver measurements.
    text=h(f'{"2.11":>9}           O                   G','RINEX VERSION / TYPE')
    text+=h('SYNTHETIC LEGACY TEST','COMMENT')+h('LEGACY','MARKER NAME')
    text+=h('     6    C1    L1    C2    L2    S1    S2','# / TYPES OF OBSERV')
    text+=h(f'{2026:6d}{10:6d}{18:6d}{8:6d}{0:6d}{18.:13.7f}     GPS','TIME OF FIRST OBS')+h('','END OF HEADER')
    text+=f' 26 10 18 08 00{18.:11.7f}  0{13:3d}'+''.join(f'G{i:02d}' for i in range(1,13))+'\n'
    text+=' '*32+'G13\n'
    for _ in range(13): text+=''.join(f'{v:14.3f}07' for v in (20000000.,100000000.,20000020.,80000000.,45.))+'\n'+f'{40.:14.3f}07\n'
    return text.encode('ascii')


class RinexTests(unittest.TestCase):
    def test_observation_counts_exact_source_time_gap_and_lli(self):
        p=catalogue(); base,rover,nav=p['raw_sources']; self.assertEqual(base['metadata']['epochs'],5)
        self.assertEqual(rover['metadata']['epochs'],4); self.assertEqual(len(rover['metadata']['gaps']),1)
        self.assertEqual(rover['metadata']['lli_loss_of_lock'],1); self.assertEqual(nav['metadata']['records'],4)
        report=r.inspect_catalogue(p); self.assertEqual(report['status'],'RAW_CATALOGUE_CHECKED',report)
        self.assertEqual(report['pairs'][0]['matched_epochs'],4); self.assertEqual(report['pairs'][0]['minimum_common_code_phase_satellites'],4)
        self.assertEqual(report['pairs'][0]['first_utc'],'2026-10-18T08:00:00.0000000Z')
        self.assertEqual(r.tick_text(base['metadata']['first_tick']),'2026-10-18T08:00:18.0000000')

    def test_raw_catalogue_never_creates_or_changes_network_observations_points_sessions(self):
        p=catalogue(); original=n.synthetic_project()
        for key in ('points','sessions','observations','imports','epoch','crs','height_datum'): self.assertEqual(p[key],original[key])
        self.assertEqual(p['schema_version'],3); self.assertEqual(n.loads_project(n.dumps_project(p)),p)
        old=n.synthetic_project(); old_bytes=n.dumps_project(old); self.assertEqual(n.loads_project(old_bytes),old)
        self.assertEqual(n.dumps_project(n.loads_project(old_bytes)),old_bytes); self.assertEqual(old['schema_version'],2)

    def test_duplicate_filename_renaming_gzip_and_line_endings_are_not_new_imports(self):
        p=catalogue(); source=r.read_source(ROOT/'survey_adjustment/examples/synthetic_base.rnx')
        for payload in ((ROOT/'survey_adjustment/examples/synthetic_base.rnx').read_bytes(),gzip.compress((ROOT/'survey_adjustment/examples/synthetic_base.rnx').read_bytes().replace(b'\n',b'\r\n'))):
            renamed=r.parse_source(payload,'renamed.rnx.gz'); renamed['path']=source['path']
            with self.assertRaisesRegex(ValueError,'already imported'): r.plan_import(p,renamed,review())
        self.assertEqual(len(p['raw_sources']),3)

    def test_legacy_continuations_and_zero_missing_values(self):
        source=r.parse_source(legacy(),'constructed.26o'); meta=source['metadata']
        self.assertEqual(meta['epochs'],1); self.assertEqual(len(meta['satellites']),13)
        self.assertEqual(len(meta['samples'][0][2].split()),26)
        altered=legacy().replace(b' 100000000.00007',b'         0.00007')
        self.assertLess(len(r.parse_source(altered,'missing.26o')['metadata']['samples'][0][2].split()),26)

    def test_lli_bits_do_not_mislabel_boc_tracking_as_cycle_slip(self):
        data=(ROOT/'survey_adjustment/examples/synthetic_base.rnx').read_bytes().replace(b'115000000.00007',b'115000000.00047',1)
        meta=r.parse_source(data,'constructed.rnx')['metadata']
        self.assertEqual(meta['lli_loss_of_lock'],0); self.assertEqual(meta['lli_boc_tracking'],1)

    def test_truncation_bad_counts_nonfinite_duplicate_epochs_and_versions_rejected(self):
        data=(ROOT/'survey_adjustment/examples/synthetic_base.rnx').read_bytes()
        cases=[data.replace(b'3.05',b'4.02',1),data.replace(b'G    4',b'G    5',1),data[:data.index(b'> 2026')+40],
               data.replace(b'22000000.000',b'         NaN',1),data.replace(b'08 00 48.0000000',b'08 00 18.0000000',1),
               data.replace(b'END OF HEADER',b'NO END HEADER',1),data.replace(b'G01  ',b'G99  ',1).replace(b'G02  ',b'G99  ',1)]
        for payload in cases:
            with self.subTest(payload=payload[:40]),self.assertRaises(ValueError): r.parse_source(payload,'bad.rnx')

    def test_event_records_and_header_change_are_not_silent_observations(self):
        data=(ROOT/'survey_adjustment/examples/synthetic_base.rnx').read_bytes()
        index=data.index(b'> 2026')
        event=b'> 2026 10 18 08 00 17.0000000  5  1\nSYNTHETIC EVENT\n'
        meta=r.parse_source(data[:index]+event+data[index:],'event.rnx')['metadata']
        self.assertEqual(meta['events'],{'5':1}); self.assertEqual(meta['epochs'],5)
        with self.assertRaisesRegex(ValueError,'header changes'): r.parse_source(data[:index]+event.replace(b'  5  1',b'  4  1')+data[index:],'changed_header.rnx')

    def test_cancel_and_gzip_expansion_limit_do_not_return_a_partial_catalogue(self):
        from unittest.mock import patch
        with self.assertRaisesRegex(ValueError,'cancelled'): r.read_source(ROOT/'survey_adjustment/examples/synthetic_base.rnx',lambda:True)
        with patch.object(r,'MAX_BYTES',1024),self.assertRaisesRegex(ValueError,'Decompressed'): r.parse_source(gzip.compress(b' '*4096),'large.gz')
        for payload in (b'not RINEX',b'\x1f\x8bbroken',b'\x00'):
            with self.assertRaises(ValueError): r.parse_source(payload,'bad')

    def test_review_classification_links_and_time_metadata_are_required_or_blocked(self):
        source=r.read_source(ROOT/'survey_adjustment/examples/synthetic_base.rnx'); p=n.synthetic_project()
        for options in (review(confirmed=False),review(synthetic=False),review(point='missing'),review(time_evidence=''),review(utc_offset_seconds=18.5),review(role='NAV')):
            with self.assertRaises(ValueError): r.plan_import(p,source,options)
        p=catalogue(); p['raw_sources'][0]['utc_offset_seconds']=None
        report=r.inspect_catalogue(p); self.assertEqual(report['status'],'BLOCKED'); self.assertTrue(any('offset' in s for s in report['blockers']))
        p=catalogue(); p['raw_sources'][0]['utc_offset_seconds']=0
        self.assertTrue(any('aligned' in s for s in r.inspect_catalogue(p)['blockers']))

    def test_missing_changed_file_source_and_session_bounds_are_visible_blockers(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'test_output') as folder:
            p=catalogue(folder); path=Path(p['raw_sources'][0]['path']); path.write_bytes(path.read_bytes()+b'\n')
            report=r.inspect_catalogue(p); self.assertIn('SOURCE_UNAVAILABLE_OR_CHANGED',[e['status'] for e in report['entries']])
            self.assertNotIn(p['raw_sources'][0]['id'],report['verified_sources'])
            Path(p['raw_sources'][1]['path']).unlink(); self.assertEqual(r.inspect_catalogue(p)['status'],'BLOCKED')
        p=catalogue(); p['sessions'][0]['start']='2026-10-18T08:01:00Z'
        self.assertTrue(any('outside' in s for s in r.inspect_catalogue(p)['blockers']))

    def test_header_incomplete_location_and_time_are_not_guessed(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'test_output') as folder:
            p=catalogue(folder); path=Path(p['raw_sources'][0]['path'])
            text=path.read_text(); text='\n'.join(line for line in text.splitlines() if line[60:80].strip() not in ('ANT # / TYPE','APPROX POSITION XYZ'))+'\n'
            path.write_text(text,encoding='ascii'); source=r.read_source(path)
            p['raw_sources'].pop(0); p=r.plan_import(p,source,review())
            report=r.inspect_catalogue(p)
            self.assertTrue(any('Antenna' in s for s in report['blockers'])); self.assertTrue(any('not mapped' in s for s in report['warnings']))

    def test_referenced_point_or_session_cannot_be_removed_and_raw_damage_is_rejected(self):
        p=catalogue()
        with self.assertRaises(ValueError): n.remove_record(p,'points',0)
        with self.assertRaises(ValueError): n.remove_record(p,'sessions',0)
        for mutation in ('samples','id','hash'):
            bad=deepcopy(p)
            if mutation=='samples': bad['raw_sources'][0]['metadata']['samples'][0][0]='not numeric'
            elif mutation=='id': bad['raw_sources'][1]['id']=bad['raw_sources'][0]['id']
            else: bad['raw_sources'][0]['source_sha256']='changed'
            with self.assertRaises(ValueError): n.validate_project(bad)

    def test_navigation_is_framed_only_and_truncation_is_rejected(self):
        data=(ROOT/'survey_adjustment/examples/synthetic_navigation.rnx').read_bytes()
        self.assertIn('NOT validated',r.parse_source(data,'nav.rnx')['metadata']['navigation_scope'])
        with self.assertRaises(ValueError): r.parse_source(b'\n'.join(data.splitlines()[:-2]),'truncated.rnx')

    def test_rinex3_header_continuation_long_records_and_version_profiles(self):
        data=(ROOT/'survey_adjustment/examples/synthetic_base.rnx').read_text()
        codes=['C1C','L1C','D1C','S1C','C2W','L2W','D2W','S2W','C5Q','L5Q','D5Q','S5Q','C1W','L1W']
        replacement=h('G   14 '+' '.join(codes[:13]),'SYS / # / OBS TYPES')+h('G      '+codes[13],'SYS / # / OBS TYPES')
        data=data.replace(h('G    4 C1C L1C C2W L2W','SYS / # / OBS TYPES'),replacement)
        lines=data.splitlines()
        for i,line in enumerate(lines):
            if line.startswith(('G01','G02')): lines[i]=line[:3]+''.join(f'{100.+k:14.3f}07' for k in range(14))
        for version in ('3.02','3.03','3.04','3.05'):
            source=r.parse_source(('\n'.join(lines)+'\n').replace('3.05',version,1).encode(),'long.rnx')
            self.assertEqual(len(source['metadata']['observation_types']['G']),14); self.assertEqual(source['metadata']['epochs'],5)

    def test_receiver_clock_offsets_remain_indicators_without_rewriting_epochs(self):
        data=(ROOT/'survey_adjustment/examples/synthetic_base.rnx').read_bytes()
        changed=data.replace(b'> 2026 10 18 08 00 18.0000000  0  4\n',b'> 2026 10 18 08 00 18.0000000  0  4      0.123456789000\n')
        first=r.parse_source(data,'base.rnx')['metadata']; second=r.parse_source(changed,'clock.rnx')['metadata']
        self.assertEqual(first['samples'],second['samples']); self.assertEqual(second['receiver_clock_offset_epochs'],1)

    def test_metadata_validation_rejects_invalid_gap_counts_without_touching_files(self):
        project=catalogue()
        for field,value in (('gaps',None),('signal_counts',[]),('events',{'5':'not numeric'}),('interval_s',float('inf'))):
            damaged=deepcopy(project); damaged['raw_sources'][0]['metadata'][field]=value
            with self.subTest(field=field),self.assertRaises(ValueError): n.validate_project(damaged)

    def test_glonass_305_navigation_requires_the_additional_record(self):
        text=h(f'{"3.05":>9}           N                   R','RINEX VERSION / TYPE')+h('SYNTHETIC NAV TEST','COMMENT')+h('','END OF HEADER')
        text+='R01 2026 10 18 08 00 00'+''.join(f'{0.:19.12E}' for _ in range(3))+'\n'
        row='    '+''.join(f'{0.:19.12E}' for _ in range(4))+'\n'
        self.assertEqual(r.parse_source((text+row*4).encode(),'glo.rnx')['metadata']['records'],1)
        with self.assertRaises(ValueError): r.parse_source((text+row*3).encode(),'glo_truncated.rnx')
