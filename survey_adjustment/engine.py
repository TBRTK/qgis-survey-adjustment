# SPDX-License-Identifier: GPL-3.0-only
"""File/process bridge to an unmodified, separately installed SALSA solver."""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def ecef(latitude, longitude, height):
    """WGS84, ellipsoidal height only. Used to create the synthetic engine example."""
    phi,lam = math.radians(latitude),math.radians(longitude)
    a, f = 6378137., 1/298.257223563
    es = f*(2-f)
    radius = a/math.sqrt(1-es*math.sin(phi)**2)
    return ((radius+height)*math.cos(phi)*math.cos(lam),
            (radius+height)*math.cos(phi)*math.sin(lam),
            (radius*(1-es)+height)*math.sin(phi))


def demo_job():
    origin = ecef(60.4,5.2,80)
    points = {'CP01': tuple(v+d for v,d in zip(origin,(25,30,12))),
              'CP02': tuple(v+d for v,d in zip(origin,(-15,50,20)))}
    noise = {'CP01':[(-.02,.01,.03),(.02,-.03,-.01),(.01,.025,-.025)],
             'CP02':[(-.01,.02,-.03),(.015,-.015,.02),(-.025,.005,.005)]}
    lines = ['# SYNTHETIC - NOT FIELD MEASUREMENTS', 'TITLE Survey Adjustment synthetic GNSS vector example',
             'DIM 3','PREC 0.000001 M', 'CONV 15 ITER 0.000000001 CONV', 'CONFIDENCE 0.05',
             'OUT NOAPV', 'POS REF0 ' + ' '.join(f'{v:.9f}' for v in origin) + ' M FIX']
    expected = {}
    for name,xyz in points.items():
        lines.append('POS '+name+' '+' '.join(f'{v:.9f}' for v in xyz)+' M EST')
        observed = []
        for index,error in enumerate(noise[name],1):
            obs = tuple(v+d for v,d in zip(xyz,error))
            observed.append(obs)
            delta = tuple(v-o for v,o in zip(obs,origin))
            lines.append('DEL REF0 '+name+' '+' '.join(f'{v:.9f}' for v in delta)+
                         f' 0.0004 0 0 0.0004 0 0.0004 M tag={name}_{index}')
        expected[name] = [math.fsum(v[axis] for v in observed)/len(observed) for axis in range(3)]
    return '\n'.join(lines)+'\n',dict(frame='WGS84 ECEF XYZ, metre',height='ELLIP',
                synthetic=True, expected_positions=expected,
                description='Two unknown points, six synthetic GNSS vectors from one fixed reference point. '
                            'This is a separate SALSA example, not an automatic conversion of a GNSS coordinate list.')


def solver_program(path):
    path = Path(path).expanduser().resolve()
    if not path.is_file() or path.name.lower() not in ('lsasolver.exe','lsasolver'):
        raise ValueError('Select SALSA lsasolver.exe (or lsasolver on Linux).')
    return path


def environment(program):
    # Avoid inheriting QGIS' Qt6/GDAL plugin paths into the Qt5 SALSA process.
    env = dict(os.environ)
    for name in list(env):
        if name.upper().startswith(('QT_','QGIS_','PYTHON','GDAL_','PROJ_','OSGEO')):
            env.pop(name,None)
    if os.name == 'nt':
        system = env.get('SystemRoot',r'C:\Windows')
        env['PATH'] = os.pathsep.join([str(program.parent),str(Path(system)/'System32'),system])
    else:
        env['PATH'] = str(program.parent)+os.pathsep+env.get('PATH','')
    return env


def prepare_demo(folder):
    folder = Path(folder)
    folder.mkdir(parents=True,exist_ok=False)
    data,meta = demo_job()
    meta['input_dat_sha256'] = hashlib.sha256(data.encode('ascii')).hexdigest()
    (folder/'input.dat').write_text(data,encoding='ascii',newline='\n')
    (folder/'input_metadata.json').write_text(json.dumps(meta,indent=2,ensure_ascii=False),encoding='utf-8')
    return folder


def command(program,folder):
    return [str(solver_program(program)), '--input', 'input.dat', '--out','solution.out',
            '--bin','solution.bin','--forcestable','--linprecP','6','--linprecM','6']


def run_demo(program,folder,timeout=45):
    """Also used by integration tests; the QGIS UI uses nonblocking QProcess."""
    program = solver_program(program)
    folder = prepare_demo(folder)
    proc = subprocess.run(command(program,folder),cwd=folder,env=environment(program),
                          capture_output=True,timeout=timeout,
                          creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    (folder/'console.txt').write_bytes(proc.stdout+b'\n'+proc.stderr)
    return inspect_result(folder,proc.returncode,program)


def inspect_result(folder,exit_code,program):
    folder = Path(folder)
    text = (folder/'solution.out').read_text(encoding='utf-8',errors='replace') if (folder/'solution.out').exists() else ''
    errors = re.findall(r'^.*(?:\bError\b|\bException\b|\bFatal\b).*$',text,re.M|re.I)
    if exit_code != 0 or errors or not (folder/'solution.bin').is_file():
        raise ValueError('The SALSA run failed. ' + ('; '.join(errors[:3]) or f'Exit code {exit_code}; result files are missing.'))
    if ('Convergence reached at iteration' not in text or 'Final Adjusted Positions XYZ' not in text or
        not re.search(r'Degrees of freedom\s*=\s*[1-9]\d*;.*\(Final\)',text)):
        raise ValueError('The SALSA result lacks a confirmed final calculation/statistics.')
    warnings = [line.strip() for line in text.splitlines() if
                re.match(r'^\s*Warning\b',line,re.I) or
                ('Chi-squared test' in line and re.search(r'\bfail\b',line,re.I)) or
                (line.lstrip().startswith('Del.') and '**' in line)]
    meta = json.loads((folder/'input_metadata.json').read_text(encoding='utf-8'))
    if meta.get('input_dat_sha256') and sha256(folder/'input.dat')!=meta['input_dat_sha256']:
        raise ValueError('The SALSA input has changed since preparation.')
    if meta.get('kind')=='coordinate_observations' and sha256(folder/'source.csv')!=meta['source']['sha256']:
        raise ValueError('The source copy has changed since preparation.')
    section = text.split('Final Adjusted Positions XYZ',1)[-1].split('Final Adjusted Positions LLH',1)[0]
    positions = {}
    for line in section.splitlines():
        parts = line.split()
        if parts and parts[0] in meta['expected_positions']:
            try:
                if parts[0] in positions:
                    raise ValueError('Duplicate final position.')
                positions[parts[0]] = [float(v) for v in parts[1:4]]
                if len(positions[parts[0]])!=3 or not all(math.isfinite(v) for v in positions[parts[0]]):
                    raise ValueError('Missing finite XYZ.')
            except ValueError:
                raise ValueError('Invalid numbers in the SALSA final positions.') from None
    if set(positions) != set(meta['expected_positions']):
        raise ValueError('SALSA final positions are missing for one or more points.')
    difference = max(abs(v-w) for name,xyz in positions.items() for v,w in zip(xyz,meta['expected_positions'][name]))
    if not math.isfinite(difference) or difference > 0.000002:
        raise ValueError('The SALSA result differs from the independent calculation of the selected model.')
    warnings.extend(meta.get('assumptions',[]))
    synthetic = meta.get('synthetic',True)
    coordinates = meta.get('kind')=='coordinate_observations'
    version = re.search(r'SALSA Ver\.\s+(\S+)',text)
    result = dict(engine='SALSA',version=version[1] if version else 'unknown',
                  execution='CALCULATED',quality='REVIEW REQUIRED' if warnings else 'No warnings found in the log; professional review is still required',
                  warnings=warnings,solver_sha256=sha256(program),input_sha256=sha256(folder/'input.dat'),
                  output_sha256=sha256(folder/'solution.out'),synthetic=synthetic,
                  independent_comparison=('PASSED against the direct XYZ mean for this linear example' if not coordinates else
                      'PASSED against the independent weighted XYZ solution of the selected coordinate model'),
                  maximum_difference_metres=difference,positions_ecef=positions,
                  frame=meta['frame'],height=meta['height'],folder=str(folder.resolve()))
    if coordinates:
        from .coordinate_job import write_coordinate_results
        result['positions_geodetic'] = write_coordinate_results(folder,meta,positions)
        result['identifiers'] = meta['identifiers']
        result['epoch'] = meta['epoch']
        result['source'] = meta['source']
        result['excluded'] = meta['excluded']
        result['model'] = 'Independent absolute coordinate observations; no measured GNSS baselines'
        result['uncertainty_source'] = meta['uncertainty_source']
        result['kind'] = 'coordinate_observations'
    (folder/'run_report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return result
