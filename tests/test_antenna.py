# SPDX-License-Identifier: GPL-3.0-only
from copy import deepcopy
from pathlib import Path
import tempfile,unittest,sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from survey_adjustment import antenna as a,gnss_demo as d,rinex as r


class AntennaTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT/'test_output');self.folder=Path(self.tmp.name)
        self.project=d.create(self.folder/'example',calibrated=True)
        self.path=self.folder/'example/synthetic.atx';self.payload=self.path.read_bytes()
        self.meta=self.project['raw_sources'][0]['metadata']
    def tearDown(self):self.tmp.cleanup()
    def catalogue(self,payload):
        self.path.write_bytes(payload);return a.read(self.path)

    def test_exact_radome_generic_and_specific_serial_selection(self):
        cat=a.read(self.path);generic=a.select(cat,self.meta,2)
        self.assertEqual(generic['selection'],'generic type/radome')
        entry=deepcopy(cat['entries'][0]);entry['serial']='SYNTHETIC';cat['entries'].append(entry)
        self.assertEqual(a.select(cat,self.meta,2)['selection'],'serial-specific')
        wrong=deepcopy(self.meta);wrong['antenna']=wrong['antenna'][:16]+'DOME'
        with self.assertRaisesRegex(ValueError,'exact antenna'):a.select(cat,wrong,2)
        cat['entries']=[entry];wrong=deepcopy(self.meta);wrong['antenna_serial']='DIFFERENT'
        with self.assertRaises(ValueError):a.select(cat,wrong,2)
        cat['entries'].append(deepcopy(entry))
        with self.assertRaisesRegex(ValueError,'ambiguous'):a.select(cat,self.meta,2)

    def test_entire_occupation_validity_not_just_start(self):
        cat=a.read(self.path);entry=cat['entries'][0]
        entry['start']=self.meta['first_tick'];entry['end']=self.meta['last_tick']
        self.assertIsNotNone(a.select(cat,self.meta,2))
        entry['end']-=1
        with self.assertRaisesRegex(ValueError,'entire occupation'):a.select(cat,self.meta,2)
        entry['end']=None;entry['start']+=1
        with self.assertRaises(ValueError):a.select(cat,self.meta,2)

    def test_no_silent_grid_azimuth_or_frequency_fallback(self):
        for key,value in (('dazi',5.),('zenith',[0.,90.,10.]),('count',3)):
            cat=a.read(self.path);cat['entries'][0][key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):a.select(cat,self.meta,2)
        for field in ('G02','G01'):
            cat=a.read(self.path);del cat['entries'][0]['frequencies'][field]
            cat['entries'][0]['count']=1
            with self.assertRaises(ValueError):a.select(cat,self.meta,2)
        cat=a.read(self.path);del cat['entries'][0]['frequencies']['G02'];cat['entries'][0]['count']=1
        self.assertIsNotNone(a.select(cat,self.meta,1))

    def test_malformed_nonfinite_relative_and_unsupported_version_rejected(self):
        for payload in (self.payload.replace(b'     1.4',b'     2.0',1),self.payload.replace(b'A'+b' '*59+b'PCV TYPE',b'R'+b' '*59+b'PCV TYPE'),self.payload[:-81]):
            with self.subTest(payload=payload[:8]),self.assertRaises(ValueError):self.catalogue(payload)
        cat=self.catalogue(self.payload);cat['entries'][0]['frequencies']['G01']['noazi_mm'][0]=float('nan')
        with self.assertRaises(ValueError):a.select(cat,self.meta,2)
        cat=self.catalogue(self.payload);cat['entries'][0]['frequencies']['G01']['neu_mm'][2]=10001
        with self.assertRaises(ValueError):a.select(cat,self.meta,2)

    def test_distinct_engine_aliases_preserve_neu_and_noazi_values(self):
        cat=a.read(self.path);models=[a.select(cat,row['metadata'],2) for row in self.project['raw_sources'][:2]]
        aliases=['SA_BASE         NONE','SA_ROVER        NONE']
        subset=self.catalogue(a.subset(list(zip(aliases,models))))
        for original,alias,selected in zip(models,aliases,subset['entries']):
            self.assertEqual(selected['type'],alias);self.assertEqual(original['frequencies'],selected['frequencies'])

    def test_fixed_width_negative_samples_and_large_pco_retain_separation(self):
        lines=self.payload.decode('ascii').splitlines()
        for i,line in enumerate(lines):
            if line.startswith('   NOAZI'):lines[i]='   NOAZI'+''.join(f'{-1000.:8.2f}' for _ in range(19))
        cat=self.catalogue(('\n'.join(lines)+'\n').encode('ascii'));model=a.select(cat,self.meta,2)
        self.assertEqual(model['frequencies']['G01']['noazi_mm'],[-1000.]*19)
        model['frequencies']['G01']['neu_mm']=[-10000.,10000.,-10000.]
        reread=self.catalogue(a.subset([('SA_BASE         NONE',model)]))
        self.assertEqual(reread['entries'][0]['frequencies'],model['frequencies'])
        self.assertTrue(all(len(line)<256 for line in self.path.read_text().splitlines()))
