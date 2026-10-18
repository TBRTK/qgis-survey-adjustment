# SPDX-License-Identifier: GPL-3.0-only
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from survey_adjustment.core import *
from survey_adjustment.reporting import save_bundle,text_report
EXAMPLES = ROOT/'survey_adjustment/examples'


class CoreTests(unittest.TestCase):
    def test_receiver_neutral_headers_and_decimal_comma_preserve_all_observations(self):
        original=read_table(EXAMPLES/'emlid_gjentak.csv')
        alternative=read_table(EXAMPLES/'gnss_gjentak.csv')
        self.assertEqual(alternative['delimiter'],';')
        left=observations(original,default_mapping(original['headers']))
        right=observations(alternative,default_mapping(alternative['headers']))
        self.assertEqual(left,right)
        _,refs=self.setup_data()
        self.assertEqual(analyze(left,refs,Options(confirmed=True)),
                         analyze(right,refs,Options(confirmed=True)))

    def test_headerless_pnezd_is_rejected_without_losing_first_observation(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'test_output') as folder:
            for name,data in (('numeric.csv','1,6701000,291000,24,KP\n2,6701001,291001,25,KP\n'),
                              ('names.txt','P1;6701000;291000;24;KP\nP2;6701001;291001;25;KP\n')):
                path=Path(folder)/name; path.write_text(data,encoding='utf-8')
                with self.assertRaisesRegex(ValueError,'overskriftsrad'): read_table(path)

    def test_numeric_solution_codes_are_not_guessed_as_fixed(self):
        for code in ('1','4','50'):
            items=[Observation(2,'A','P',100,100,1,code,'t1'),Observation(3,'B','P',100,100,1,code,'t2')]
            with self.subTest(code=code),self.assertRaisesRegex(ValueError,'FIX-filteret'):
                analyze(items,{},Options(confirmed=True))

    def setup_data(self):
        table = read_table(EXAMPLES/'emlid_gjentak.csv')
        ref = read_table(EXAMPLES/'kontrollpunkter.csv')
        return observations(table,default_mapping(table['headers'])),references(ref,default_mapping(ref['headers']))

    def test_example_expected_statuses_filter_means_sd_and_unchanged_source(self):
        files = list(EXAMPLES.glob('*.csv'))
        original = {p:p.read_bytes() for p in files}
        items,refs = self.setup_data()
        result = analyze(items,refs,Options(confirmed=True))
        self.assertEqual(result['used_count'],9)
        self.assertEqual(result['excluded'][0]['name'],'KP03_FLOAT')
        self.assertEqual([g['status'] for g in result['groups']],['INNENFOR','AVVIK','AVVIK','UAVKLART'])
        self.assertAlmostEqual(result['groups'][0]['mean'][0],291000.004/3+2*291000/3,places=8)
        self.assertAlmostEqual(result['groups'][0]['sd'][0],statistics.stdev([.006,-.004,.002]),places=8)
        self.assertEqual(result['groups'][3]['sd'],(None,None,None))
        self.assertEqual({p:p.read_bytes() for p in files},original)

    def test_individual_reference_failure_cannot_hide_in_mean(self):
        items = [Observation(2,'A','P',100,100,1,'FIX','t1'),Observation(3,'B','P',100.05,100,1,'FIX','t2')]
        result = analyze(items,{'P':(100,100,1)},Options(confirmed=True))
        self.assertLess(result['groups'][0]['max_horizontal_residual'],.03)
        self.assertLess(result['groups'][0]['reference_delta'][0],.03)
        self.assertEqual(result['groups'][0]['status'],'AVVIK')
        self.assertTrue(result['observations'][1]['exceed'])

    def test_unknown_status_insufficient_and_duplicate_times_are_explicit(self):
        items = [Observation(2,'A','P',100,100,1,'','t'),Observation(3,'B','P',100,100,1,'FLOAT','t')]
        with self.assertRaises(ValueError): analyze(items,{},Options(confirmed=True))
        result = analyze(items,{},Options(confirmed=True,only_fix=False))
        self.assertEqual(result['groups'][0]['status'],'UAVKLART')
        self.assertTrue(any('samme måletid' in w for w in result['warnings']))

    def test_require_confirmation_crs_positive_limits_and_axis_order(self):
        items,refs = self.setup_data()
        for options in (Options(),Options(crs='EPSG:4326',confirmed=True),Options(confirmed=True,horizontal_tolerance=0)):
            with self.assertRaises(ValueError): analyze(items,refs,options)
        items[0].e,items[0].n = items[0].n,items[0].e
        with self.assertRaises(ValueError): analyze(items,refs,Options(confirmed=True))

    def test_semicolon_decimal_comma_and_strict_header_rows_numbers(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'test_output') as folder:
            path = Path(folder)/'test.csv'
            path.write_text('Name;Easting;Northing;Elevation\nP1;291000,125;6701000,25;-1,5\n',encoding='utf-8-sig')
            table = read_table(path)
            item = observations(table,default_mapping(table['headers']))[0]
            self.assertEqual((item.e,item.n,item.z),(291000.125,6701000.25,-1.5))
            for data in ('Name,Name,Easting,Northing,Elevation\nP,P,1,2,3\n',
                         'Name,Easting,Northing,Elevation\nP,1,2,3,4\n',
                         'Name,Easting,Northing,Elevation\nP,NaN,2,3\n',
                         'Name,Easting,Northing,Elevation\nP,1,2,\n'):
                path.write_text(data,encoding='utf-8')
                with self.assertRaises(ValueError):
                    table = read_table(path)
                    observations(table,default_mapping(table['headers']))

    def test_duplicate_reference_is_rejected(self):
        table = read_table(EXAMPLES/'kontrollpunkter.csv')
        table['rows'].append(table['rows'][0])
        with self.assertRaises(ValueError): references(table,default_mapping(table['headers']))

    def test_report_bundle_hashes_html_escaping_and_unique_folders(self):
        items,refs = self.setup_data()
        result = analyze(items,refs,Options(confirmed=True))
        result['groups'][0]['reasons'].append('<script>bad</script>')
        with tempfile.TemporaryDirectory(dir=ROOT/'test_output') as folder:
            first,second = save_bundle(folder,result),save_bundle(folder,result)
            self.assertNotEqual(first,second)
            self.assertNotIn('<script>',(first/'kontroll.html').read_text(encoding='utf-8'))
            hashes = json.loads((first/'sha256.json').read_text(encoding='utf-8'))
            for name,digest in hashes.items(): self.assertEqual(hashlib.sha256((first/name).read_bytes()).hexdigest(),digest)
            self.assertIn('ikke SALSA',text_report(result))


if __name__=='__main__':
    (ROOT/'test_output').mkdir(exist_ok=True)
    unittest.main(verbosity=2)
