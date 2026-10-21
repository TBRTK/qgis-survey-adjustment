# SPDX-License-Identifier: GPL-3.0-only
from dataclasses import replace
from datetime import datetime,timedelta
from copy import deepcopy
from pathlib import Path
import csv,hashlib,io,json,math,tempfile,unittest
from survey_adjustment import orientation as o,gnss_demo as d,coordinate_job as c,rinex as r,network as n

ROOT=Path(__file__).resolve().parents[1]


def options(**changes):
    return replace(o.OrientationOptions('uncompensated-antenna','ellipsoid-altitude','EPSG:4979',2.,
        'SYNTHETIC known antenna endpoint, ellipsoidal height, WGS84 and synchronized UTC; not field accuracy.',True,True,True),**changes)


class OrientationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT/'test_output');self.root=Path(self.tmp.name)
        self.path=d.orientation_example(self.root/'input')
    def tearDown(self):self.tmp.cleanup()
    def mutate(self,change):
        lines=self.path.read_text().splitlines();change(lines)
        self.path.write_text('\n'.join(lines)+'\n',encoding='ascii')
    def test_known_checksums_not_copied_incorrect_examples(self):
        self.assertEqual(o.sentence('GNETC,,,,,,,,,'),'$GNETC,,,,,,,,,*77')
        self.assertEqual(o.sentence('GNETC,193828.80,30,00,172.543,5.621,2.344,1.013,0.993,1.021')[-2:],'5E')
    def test_exact_matching_midnight_turning_and_exclusions(self):
        source=o.read(self.path);result=o.review(source,options())
        self.assertEqual(len(result['rows']),121);self.assertEqual(result['accepted'],117)
        self.assertTrue(result['rows'][0]['utc'].startswith('2026-10-20'))
        self.assertTrue(result['rows'][-1]['utc'].startswith('2026-10-21'))
        self.assertEqual(result['rows'][0]['etc']['heading'],359.)
        self.assertEqual(result['rows'][1]['etc']['heading'],6.)
        for index,reason in ((5,'not RTK FIX'),(10,'not compensating'),(15,'notification 10'),(20,'No exact ETC')):
            self.assertFalse(result['rows'][index]['accepted']);self.assertIn(reason,result['rows'][index]['reason'])
            self.assertIsNone(result['rows'][index]['ground_llh'])
    def test_pole_axis_cardinal_cases_and_heading_is_not_tilt_direction(self):
        for angle,expected in ((0,[0,0,-2]),(90,[-2,0,0]),(180,[0,0,2])):
            actual=o.delta_enu(2,90,angle)
            self.assertLess(math.dist(actual,expected),1e-12)
        self.assertLess(math.dist(o.delta_enu(2,0,90),[0,-2,0]),1e-12)
        source=o.read(self.path);a=o.review(source,options())
        for row in source['rows']:
            if row['etc'] and row['etc']['state']==30:row['etc']['heading']=(row['etc']['heading']+90)%360
        b=o.review(source,options())
        self.assertEqual([r['ground_xyz'] for r in a['rows']],[r['ground_xyz'] for r in b['rows']])
    def test_independent_truth_and_height_export(self):
        result=o.review(o.read(self.path),options());truth=json.loads((self.path.parent/'SYNTHETIC_TRUTH.json').read_text())
        for index,row in enumerate(result['rows']):
            if row['accepted']:self.assertLess(math.dist(row['ground_xyz'],truth['ground_xyz'][index]),2e-6)
        self.assertLess(result['rows'][0]['ground_llh'][2],result['rows'][0]['original_llh'][2])
    def test_already_compensated_coordinates_are_bitwise_preserved(self):
        source=o.read(self.path);result=o.review(source,options(endpoint='already-ground',antenna_height_m=None))
        for row in result['rows']:
            if row['accepted']:
                self.assertEqual(row['ground_llh'],row['original_llh']);self.assertEqual(row['delta_enu_m'],[0.,0.,0.])
                self.assertIsNone(row['tilt_estimate_neu_mm'])
        with self.assertRaisesRegex(ValueError,'second correction'):o.review(source,options(endpoint='already-ground'))
    def test_unknown_endpoint_height_frame_and_unreviewed_inputs_blocked(self):
        source=o.read(self.path)
        for change in (dict(endpoint=''),dict(height_mode=''),dict(frame=''),dict(antenna_height_m=None),dict(antenna_height_m=math.nan),dict(endpoint_reviewed=False),dict(height_reviewed=False),dict(time_reviewed=False),dict(evidence='')):
            with self.subTest(change=change),self.assertRaises(ValueError):o.review(source,options(**change))
    def test_explicit_standard_height_adds_separation_once(self):
        def change(lines):
            for i,line in enumerate(lines):
                if line.startswith('$GNGGA'):
                    fields=line[1:-3].split(',');fields[11]='40.5';lines[i]=o.sentence(','.join(fields))
        self.mutate(change);source=o.read(self.path)
        a=o.review(source,options(endpoint='already-ground',antenna_height_m=None))
        b=o.review(source,options(endpoint='already-ground',antenna_height_m=None,height_mode='msl-plus-separation'))
        self.assertEqual(b['rows'][0]['ground_llh'][2]-a['rows'][0]['ground_llh'][2],40.5)
    def test_checksum_nonascii_binary_and_missing_dates_block(self):
        original=self.path.read_bytes()
        for payload in (original.replace(b'*',b'!',1),original+b'\xff',original+o.sentence('GPTXT,invalid\x00text').encode(),b'not nmea\n',b'\n'.join(line for line in original.splitlines() if not line.startswith(b'$GNZDA'))):
            self.path.write_bytes(payload)
            with self.assertRaises(ValueError):o.read(self.path)
    def test_missing_or_multiple_hemisphere_letters_cannot_mirror_a_position(self):
        for hemisphere in ('','NS','NE','W'):
            with self.subTest(hemisphere=hemisphere),self.assertRaisesRegex(ValueError,'hemisphere'):o._coordinate('6024.000',hemisphere,True)
        for hemisphere in ('','EW','NE','N'):
            with self.subTest(hemisphere=hemisphere),self.assertRaisesRegex(ValueError,'hemisphere'):o._coordinate('00512.000',hemisphere,False)
        self.assertEqual(o._coordinate('6024.000','N',True),60.4)
        self.assertEqual(o._coordinate('00512.000','W',False),-5.2)
    def test_duplicate_epochs_cannot_merge_two_streams(self):
        self.mutate(lambda lines:lines.insert(3,lines[2]))
        with self.assertRaisesRegex(ValueError,'Duplicate ETC'):o.read(self.path)
    def test_different_epoch_cannot_receive_nearest_orientation(self):
        def change(lines):
            fields=lines[2][1:-3].split(',');fields[1]='235950.001';lines[2]=o.sentence(','.join(fields))
        self.mutate(change);result=o.review(o.read(self.path),options())
        self.assertEqual(result['accepted'],116)
        self.assertIn('No exact ETC',result['rows'][0]['reason'])
    def test_missing_continuous_date_coverage_is_not_extrapolated(self):
        self.mutate(lambda lines:lines.__setitem__(slice(None),[line for i,line in enumerate(lines) if i==0 or not line.startswith('$GNZDA')]))
        with self.assertRaisesRegex(ValueError,'within 2 seconds'):o.read(self.path)
    def test_invalid_angles_unknown_status_and_nan_are_blocked(self):
        original=self.path.read_text()
        for column,value in ((2,'40'),(3,'99'),(4,'361'),(5,'-1'),(6,'181'),(7,'NaN'),(8,'-2')):
            lines=original.splitlines();fields=lines[2][1:-3].split(',');fields[column]=value;lines[2]=o.sentence(','.join(fields))
            self.path.write_text('\n'.join(lines)+'\n')
            with self.subTest(column=column),self.assertRaises(ValueError):o.read(self.path)
    def test_coefficients_are_retained_as_mm_estimates_without_covariance(self):
        result=o.review(o.read(self.path),options())
        self.assertEqual(result['rows'][0]['tilt_estimate_neu_mm'],[2.,2.4,3.])
        self.assertNotIn('covariance',result['rows'][0]);self.assertNotIn('Sigma',o._csv(result).decode())
    def test_checked_export_all_epochs_and_existing_coordinate_mapping(self):
        source=o.read(self.path);original=self.path.read_bytes();folder=o.export_snapshot(self.root/'package',source,options())
        result=o.verify_snapshot(folder);self.assertEqual(result['accepted'],117);self.assertEqual(self.path.read_bytes(),original)
        with (folder/'ground_coordinates.csv').open(newline='') as handle:
            reader=csv.DictReader(handle);mapping=c.mapping_for(reader.fieldnames);rows=list(reader)
        self.assertEqual(mapping['h'],'Ellipsoidal height');self.assertEqual(len(rows),117)
        for actual,expected in zip(rows,[r for r in result['rows'] if r['accepted']]):
            self.assertEqual([float(actual[k]) for k in ('Latitude','Longitude','Ellipsoidal height')],expected['ground_llh'])
        with (folder/'epochs.csv').open(newline='') as handle:self.assertEqual(len(list(csv.DictReader(handle))),121)
        self.assertFalse(any(name.endswith('.pos') for name in (p.name for p in folder.iterdir())))
    def test_changed_source_blocks_export_before_creating_output(self):
        source=o.read(self.path);self.path.write_bytes(self.path.read_bytes()+b'\n')
        with self.assertRaisesRegex(ValueError,'changed since review'):o.export_snapshot(self.root/'package',source,options())
        self.assertFalse((self.root/'package').exists())
    def test_coordinate_tamper_with_rehashed_manifest_fails_recalculation(self):
        folder=o.export_snapshot(self.root/'package',o.read(self.path),options())
        path=folder/'ground_coordinates.csv';path.write_bytes(path.read_bytes().replace(b'98.',b'97.',1))
        hashes=json.loads((folder/'checksums.json').read_text());hashes[path.name]=hashlib.sha256(path.read_bytes()).hexdigest()
        (folder/'checksums.json').write_text(json.dumps(hashes))
        with self.assertRaisesRegex(ValueError,'recalculated'):o.verify_snapshot(folder)
    def test_cancelled_read_and_plain_csv_have_no_orientation_fallback(self):
        with self.assertRaisesRegex(ValueError,'cancelled'):o.read(self.path,lambda:True)
        path=self.root/'points.csv';path.write_text('Latitude,Longitude,Height\n60,5,100\n')
        with self.assertRaises(ValueError):o.read(path)


class OrientationCoverageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT/'test_output');self.root=Path(self.tmp.name)
        self.project=d.create(self.root/'raw',moving=True);self.rover=next(s for s in self.project['raw_sources'] if s['role']=='ROVER')
        self.path=d.orientation_example(self.root/'orientation',self.rover,bad_epochs=False);self.source=o.read(self.path)
    def tearDown(self):self.tmp.cleanup()
    def coverage(self,**kw):return o.rinex_coverage(self.source,self.project,self.rover['id'],**kw)
    def rehash(self,folder,name):
        path=folder/'checksums.json';hashes=json.loads(path.read_text());hashes[name]=hashlib.sha256((folder/name).read_bytes()).hexdigest();path.write_text(json.dumps(hashes))
    def test_exact_epochs_use_reviewed_offset_and_do_not_change_network(self):
        before=deepcopy(self.project);result=self.coverage()
        self.assertEqual(result['status'],'EXACT_USABLE_EPOCH_COVERAGE');self.assertEqual(result['raw_epochs'],121)
        self.assertEqual(result['usable_epochs'],121);self.assertEqual(result['exact_etc_epochs'],121)
        self.assertEqual(result['source_minus_utc_seconds'],18);self.assertEqual(result['time_system'],'GPS')
        self.assertEqual(result['rows'][0]['utc'],'2026-10-18T08:00:00.0000000Z')
        self.assertTrue(all(row['nearest_etc_delta_ticks']==0 for row in result['rows']))
        self.assertEqual(self.project,before);self.assertIn('does not compare positions',' '.join(result['warnings']))
    def test_missing_etc_float_setup_and_notification_remain_visible(self):
        self.source=o.read(d.orientation_example(self.root/'bad',self.rover))
        result=self.coverage();self.assertEqual(result['status'],'INCOMPLETE_EPOCH_COVERAGE')
        self.assertEqual((result['exact_etc_epochs'],result['usable_epochs'],result['missing_etc_epochs'],result['unusable_etc_epochs']),(120,117,1,3))
        reviewed=o.review(self.source,options(),self.project,self.rover['id'])
        self.assertEqual(reviewed['accepted'],117);self.assertEqual(reviewed['rinex_coverage'],result)
    def test_shifted_stream_is_diagnostic_only_and_does_not_shift_raw_time(self):
        lines=[]
        for line in self.path.read_text().splitlines():
            fields=line[1:-3].split(',');tick=datetime.strptime(fields[1],'%H%M%S.%f')+timedelta(microseconds=200000)
            fields[1]=tick.strftime('%H%M%S.%f');lines.append(o.sentence(','.join(fields)))
        self.path.write_text('\n'.join(lines)+'\n');self.source=o.read(self.path);result=self.coverage()
        self.assertEqual(result['exact_etc_epochs'],0);self.assertEqual(result['usable_epochs'],0)
        self.assertEqual(result['rows'][0]['nearest_etc_delta_ticks'],2000000)
        self.assertEqual(o.review(self.source,options(),self.project,self.rover['id'])['accepted'],121)
    def test_no_invented_rover_role_offset_or_clock_model(self):
        for source in self.project['raw_sources']:
            if source['role']!='ROVER':
                with self.assertRaisesRegex(ValueError,'existing reviewed rover'):o.rinex_coverage(self.source,self.project,source['id'])
        with self.assertRaisesRegex(ValueError,'existing reviewed rover'):o.rinex_coverage(self.source,self.project,'missing')
        self.rover['utc_offset_seconds']=None
        with self.assertRaisesRegex(ValueError,'offset is undocumented'):self.coverage()
        self.rover['utc_offset_seconds']=18
        raw=Path(self.rover['path']);text=raw.read_text()
        # Parse an actual optional epoch clock field; rebuild only this test fixture.
        lines=text.splitlines();idx=next(i for i,line in enumerate(lines) if line.startswith('>'))
        lines[idx]=lines[idx].ljust(41)+' 0.000001000000'
        raw.write_text('\n'.join(lines)+'\n',encoding='ascii')
        fresh=r.read_source(raw);self.assertGreater(fresh['metadata']['receiver_clock_offset_epochs'],0)
        self.rover.update({key:deepcopy(fresh[key]) for key in ('source_sha256','fingerprint','size_bytes','expanded_bytes','metadata')})
        with self.assertRaisesRegex(ValueError,'receiver-clock-offset'):self.coverage()
    def test_submicrosecond_raw_epoch_is_not_rounded_to_nmea(self):
        raw=Path(self.rover['path']);lines=raw.read_text().splitlines();indices=[i for i,line in enumerate(lines) if line.startswith('>')]
        i=indices[1];self.assertIn('23.0000000',lines[i]);lines[i]=lines[i].replace('23.0000000','23.0000001')
        raw.write_text('\n'.join(lines)+'\n',encoding='ascii');fresh=r.read_source(raw)
        self.rover.update({key:deepcopy(fresh[key]) for key in ('source_sha256','fingerprint','size_bytes','expanded_bytes','metadata')})
        result=self.coverage();self.assertEqual(result['usable_epochs'],120)
        self.assertEqual(result['rows'][1]['nearest_etc_delta_ticks'],-1);self.assertFalse(result['rows'][1]['exact_etc'])
        with self.assertRaisesRegex(ValueError,'sub-microsecond'):d.orientation_example(self.root/'cannotround',self.rover)
    def test_changed_source_and_cancellation_are_blocked(self):
        with self.assertRaisesRegex(ValueError,'cancelled'):self.coverage(cancelled=lambda:True)
        raw=Path(self.rover['path']);raw.write_bytes(raw.read_bytes()+b'\n')
        with self.assertRaisesRegex(ValueError,'changed since import'):self.coverage()
    def test_linked_snapshot_is_self_contained_and_heights_are_unchanged(self):
        before=deepcopy(self.project);raw=Path(self.rover['path']).read_bytes()
        folder=o.export_snapshot(self.root/'package',self.source,options(),self.project,self.rover['id'])
        self.assertEqual((folder/'raw_rover.rnx').read_bytes(),raw)
        self.path.unlink();Path(self.rover['path']).unlink()
        result=o.verify_snapshot(folder);self.assertEqual(result['rinex_coverage']['usable_epochs'],121)
        self.assertEqual(result['accepted'],121);self.assertEqual(self.project,before)
        with (folder/'ground_coordinates.csv').open() as handle:rows=list(csv.DictReader(handle))
        self.assertEqual(float(rows[0]['Ellipsoidal height']),result['rows'][0]['ground_llh'][2])
        with (folder/'rinex_coverage.csv').open() as handle:self.assertEqual(len(list(csv.DictReader(handle))),121)
    def test_rehashed_time_table_or_raw_copy_cannot_override_recalculation(self):
        for name in ('rinex_coverage.csv','raw_rover.rnx','network.sanet.json'):
            folder=o.export_snapshot(self.root/name.replace('.','_'),self.source,options(),self.project,self.rover['id'])
            if name=='network.sanet.json':
                project=n.load_project(folder/name);project['raw_sources'][1]['time_evidence']+=' altered';(folder/name).write_text(n.dumps_project(project))
            else:(folder/name).write_bytes((folder/name).read_bytes()+b'\n')
            self.rehash(folder,name)
            with self.subTest(name=name),self.assertRaises(ValueError):o.verify_snapshot(folder)
