# SPDX-License-Identifier: GPL-3.0-only
"""Constructed forward models only; no field measurements or accuracy claim."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import hashlib,json,math,shutil,sys,tempfile,unittest
from unittest.mock import patch
import numpy as np
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from survey_adjustment import network_vrs as v,network as n,rinex as r,gnss_demo as d,gnss_processing as p,antenna
KERNEL=ROOT/'upstream/network-kernel-0180-reproduce/network_kernel.exe'
ENGINE=ROOT/'test_output/ppk_engine_0100/rnx2rtkp.exe'


def fixture(folder):
    """Three physically generated, invented reference observations, not metadata clones."""
    from survey_adjustment.coordinate_job import xyz
    original=d.observations
    def observations(*args,**kwargs):return original(*args,**kwargs).replace('ZERO-PATTERN'.ljust(20),'SIM_NET_ZERO'.ljust(16)+'NONE')
    with patch.object(d,'observations',observations):project=d.create(folder/'example',long_static=True)
    project['raw_sources']=[row for row in project['raw_sources'] if row['role']!='BASE']
    references=[]
    for index,(lat,lon) in enumerate(((60.35,5.1),(60.35,5.5),(60.65,5.2))):
        point='SIM_NET_'+str(index);position=xyz(lat,lon,100.,'EPSG:9989')
        project['points'].append(dict(n.point(),id=point))
        with patch.object(d,'BASE',position):text=observations(False,True,long_static=True)
        path=folder/(point+'.rnx');path.write_text(text,encoding='ascii')
        project=r.plan_import(project,r.read_source(path),r.ImportReview(role='BASE',point=point,session='SIM01',utc_offset_seconds=18,
            time_evidence='SYNTHETIC GPS minus UTC 18s.',antenna_evidence='SYNTHETIC zero electrical endpoint.',note='SYNTHETIC invented physical forward model, not a real reference.',synthetic=True,confirmed=True))
        references.append(dict(source_id=project['raw_sources'][-1]['id'],xyz=position,frame='EPSG:9988',coordinate_epoch_decimal_year=2026.8,evidence='SYNTHETIC invented ITRF2020 definition.'))
    zero=dict(dazi=0.,frequencies={code:dict(neu_mm=[0.,0.,0.],noazi_mm=[0.]*19) for code in ('G01','G02')})
    atx=folder/'synthetic.atx';atx.write_bytes(antenna.subset([('SIM_NET_ZERO'.ljust(16)+'NONE',zero)]))
    return project,references,xyz(60.4,5.2,100.,'EPSG:9989'),atx


class NetworkMathTests(unittest.TestCase):
    def test_forward_equations_clocks_ambiguity_sign_and_shared_covariance(self):
        rng=np.random.default_rng(42);k=5;count=40
        positions=np.array([[0.,0.],[100000.,0.],[0.,100000.]])
        ambiguity=rng.integers(-10000,10000,size=(3,k,2));values=np.zeros((count,3,k,4));truth=[]
        # Independent measurement equations: code +I, phase -I, same clock.
        for time in range(count):
            fields=[]
            for sat in range(k):
                a=positions@np.array([2e-6,1e-6])*(1+sat*.2)+.01*math.sin(time/20+sat)
                ion=positions@np.array([1e-5,-1e-6])*(1+sat*.3)+.1*math.sin(time/30+sat)
                fields.append((a,ion))
                for receiver in range(3):
                    clock=10*receiver+3*sat+.005*time
                    values[time,receiver,sat,:2]=clock+a[receiver]+np.array([1,v.G])*ion[receiver]
                    values[time,receiver,sat,2:]=clock+a[receiver]-np.array([1,v.G])*ion[receiver]+v.LAM*ambiguity[receiver,sat]
            truth.append(fields)
        result=v.fit_float_block(values,np.full((count,3,k),50.))
        expected=np.array([ambiguity[ref,sat]-ambiguity[0,sat]-ambiguity[ref,0]+ambiguity[0,0] for ref in (1,2) for sat in range(1,k)]).reshape(-1)
        np.testing.assert_allclose(result['ambiguities'],expected,atol=1e-7)
        self.assertGreater(np.linalg.eigvalsh(result['covariance']).min(),0)
        self.assertGreater(abs(result['covariance'][0,2]),.01) # shared pivot
        self.assertGreater(abs(result['covariance'][0,2*(k-1)]),.01) # shared master
        for time in range(count):
            for ref in (1,2):
                for sat in range(1,k):
                    index=(ref-1)*(k-1)+sat-1;a,ion=truth[time][sat];ap,ip=truth[time][0]
                    self.assertAlmostEqual(result['nondispersive'][time,index],a[ref]-a[0]-ap[ref]+ap[0],places=7)
                    self.assertAlmostEqual(result['iono'][time,index],ion[ref]-ion[0]-ip[ref]+ip[0],places=7)

    def test_nonfinite_bounds_and_cancel_block_estimation(self):
        y=np.zeros((30,3,4,4));e=np.full((30,3,4),50.)
        for bad in (np.zeros((121,3,4,4)),np.full_like(y,np.nan),np.zeros((30,2,4,4))):
            with self.assertRaises(ValueError):v.fit_float_block(bad,e)
        with self.assertRaises(ValueError):v.fit_float_block(y,np.zeros_like(e))
        with self.assertRaisesRegex(ValueError,'cancelled'):v.fit_float_block(y,e,lambda:True)
        with self.assertRaises(ValueError):v.float_observations([{}, {}, {}],[1.,-9.,9.])

    def test_virtual_rinex_width_height_phase_lli_and_real_synthetic_identity(self):
        epochs=[dict(tick=(2400,3000000000000+i*30*r.TICK),observations=[dict(sat='G01',values=[22000000.,115000000.125,22000001.,89000000.5],lli=1 if i==0 else 0)]) for i in range(2)]
        with tempfile.TemporaryDirectory(dir=ROOT/'test_output') as directory:
            path=Path(directory)/'virtual.rnx'
            for synthetic in (False,True):
                path.write_bytes(v.rinex_bytes(epochs,d.BASE,synthetic));source=r.read_source(path)
                self.assertEqual(source['metadata']['epochs'],2);self.assertEqual(source['marked_synthetic'],synthetic)
                self.assertTrue(v.requires_float(source));self.assertEqual(source['metadata']['antenna_hen'],[0.,0.,0.])
                lines=[line for line in path.read_text().splitlines() if line.startswith('G01')]
                self.assertEqual(len(lines[0]),67);self.assertEqual(lines[0][33],'1');self.assertEqual(lines[1][33],' ')
                self.assertEqual(float(lines[0][19:33]),115000000.125)


@unittest.skipUnless(KERNEL.is_file() and ENGINE.is_file(),'Separately built reviewed engines intentionally not bundled')
class NetworkProductTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT/'test_output');self.root=Path(self.tmp.name)
        self.project,self.references,self.target,self.atx=fixture(self.root)
        self.nav=next(row['id'] for row in self.project['raw_sources'] if row['role']=='NAV')
    def tearDown(self):self.tmp.cleanup()
    def generate(self):return v.generate(self.root/'product',self.project,self.references,self.target,KERNEL,self.atx,self.nav,'SYNTHETIC FLOAT shared-network model and separate GPL/BSD/SOFA engine; no accuracy approval.')

    def test_originals_generate_recalculate_attach_and_static_float_without_independent_baseline(self):
        product=self.generate();self.assertGreater(product['report']['epochs'],30)
        self.assertFalse(product['report']['integer_fix']);self.assertFalse(product['report']['known_control_used_to_fit'])
        before=deepcopy(self.project);project=v.attach_product(self.project,product,KERNEL,'SIM_VRS')
        self.assertEqual(self.project,before);self.assertFalse(project['observations'])
        self.assertIsNone(project['points'][-1]['coordinates'])
        with self.assertRaises(ValueError):v.attach_product(project,product,KERNEL,'SIM_VRS')
        base=project['raw_sources'][-1];rover=next(row for row in project['raw_sources'] if row['role']=='ROVER')
        options=p.ProcessingOptions(base=base['id'],rover=rover['id'],navigation=self.nav,frame='EPSG:9988',epoch=project['epoch'],base_xyz=tuple(self.target),
            atmosphere=p.STATIC_ATMOSPHERE,ambiguity_mode='off',reference_network_path=product['folder'],network_kernel_path=str(KERNEL),
            calibration='antex',antex_path=product['antex_path'],calibration_note='SYNTHETIC zero virtual/physical endpoints, original model retained.',
            antenna_note='SYNTHETIC electrical endpoints, zero HEN and receiver patterns.',model_note='SYNTHETIC network model; no field accuracy; inherited covariance excluded.',
            base_reviewed=True,antenna_reviewed=True,model_reviewed=True,license_reviewed=True)
        with self.assertRaisesRegex(ValueError,'FLOAT ONLY'):p.validate(project,replace(options,ambiguity_mode='continuous'),preflight=False)
        with self.assertRaisesRegex(ValueError,'original reviewed network'):p.validate(project,replace(options,reference_network_path=''),preflight=False)
        result,solution=p.run(self.root/'static',project,options,ENGINE)
        self.assertEqual(result['eligible_fix'],0);self.assertEqual(set(result['quality_counts']),{'2'})
        self.assertEqual(solution['processing']['profile'],p.NETWORK_FLOAT_PROFILE)
        # Original product and all physical parent files can disappear after job snapshot.
        shutil.rmtree(self.root/'product')
        for row in self.project['raw_sources']:
            if row['role']=='BASE':Path(row['path']).unlink()
        self.assertEqual(len(p.source_for_import(self.root/'static/solution.pos')['rows']),result['epochs'])
        manifest=json.loads((self.root/'static/reference_network/vrs.json').read_text())
        path=self.root/'static/reference_network/virtual_and_receivers.atx';path.write_bytes(path.read_bytes().replace(b'   0.00',b'   1.00',1))
        manifest['outputs'][path.name]=hashlib.sha256(path.read_bytes()).hexdigest()
        (self.root/'static/reference_network/vrs.json').write_text(json.dumps(manifest))
        with self.assertRaises(ValueError):p.source_for_import(self.root/'static/solution.pos')

    def test_rehashed_geometry_virtual_data_and_model_tamper_fail_recalculation(self):
        product=self.generate();folder=Path(product['folder'])
        originals={path.name:path.read_bytes() for path in folder.iterdir() if path.is_file()}
        for name in ('reference_0_model.csv','virtual_float.rnx','network_model.json','virtual_and_receivers.atx'):
            for filename,payload in originals.items():(folder/filename).write_bytes(payload)
            path=folder/name
            if name=='network_model.json':
                model=json.loads(path.read_text());model['weights'][0]+=.1;path.write_text(json.dumps(model))
            else:path.write_bytes(path.read_bytes()+b'\nTAMPER\n')
            manifest=json.loads((folder/'vrs.json').read_text());manifest['outputs'][name]=hashlib.sha256(path.read_bytes()).hexdigest()
            (folder/'vrs.json').write_text(json.dumps(manifest))
            with self.assertRaises((ValueError,KeyError)):v.load_product(folder,KERNEL)


if __name__=='__main__':unittest.main()
