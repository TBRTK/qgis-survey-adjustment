# SPDX-License-Identifier: GPL-3.0-only
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from survey_adjustment.engine import *
PROGRAM = ROOT/'upstream/extracted/SALSA/bin/lsasolver.exe'


class EngineTests(unittest.TestCase):
    def test_demo_structure_unique_tags_units_and_height_frame(self):
        data,meta = demo_job()
        self.assertEqual(sum(line.startswith('DEL ') for line in data.splitlines()),6)
        tags = re.findall(r'tag=(\S+)',data)
        self.assertEqual(len(tags),len(set(tags)))
        self.assertEqual(meta['height'],'ELLIP')
        self.assertNotIn('NN2000',data)
        self.assertTrue(meta['synthetic'])

    @unittest.skipUnless(PROGRAM.exists(),'SALSA-motoren er ikke tilgjengelig lokalt')
    def test_real_solver_then_detect_tampered_output_and_nonzero_exit(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'test_output') as temporary:
            folder = Path(temporary)/'job'
            report = run_demo(PROGRAM,folder)
            self.assertEqual(report['version'],'1.17.0')
            self.assertLess(report['maximum_difference_metres'],.000002)
            self.assertEqual(len(report['positions_ecef']),2)
            self.assertTrue(any('geoid' in w for w in report['warnings']))
            self.assertFalse(any('Convergence test at iteration 1' in w for w in report['warnings']))
            with self.assertRaises(ValueError): inspect_result(folder,1,PROGRAM)
            path = folder/'solution.out'
            original = path.read_text(encoding='utf-8')
            pos = report['positions_ecef']['CP01'][0]
            path.write_text(original.replace(f'{pos:.6f}',f'{pos+1:.6f}'),encoding='utf-8')
            with self.assertRaises(ValueError): inspect_result(folder,0,PROGRAM)
            path.write_text('Error - invalid record\n'+original,encoding='utf-8')
            with self.assertRaises(ValueError): inspect_result(folder,0,PROGRAM)

    def test_existing_job_and_wrong_program_not_used(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'test_output') as temporary:
            with self.assertRaises(FileExistsError): prepare_demo(temporary)
        with self.assertRaises(ValueError): solver_program('not_a_solver.exe')


if __name__=='__main__':
    (ROOT/'test_output').mkdir(exist_ok=True)
    unittest.main(verbosity=2)
