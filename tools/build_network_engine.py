# SPDX-License-Identifier: GPL-3.0-only
"""Build the separate reviewed FLOAT-network geometry kernel; no downloads.

Requires the same pinned upstream RTKLIB source archive and Zig compiler as the
existing Survey engine builds. Retains upstream BSD/SOFA notices in the source
tree. Engine binaries/compiler are never included in the plugin ZIP.
"""
from pathlib import Path,PurePosixPath
from zipfile import ZipFile
import argparse,hashlib,json,os,subprocess
from build_system_engine import SOURCE_SHA,COMPILER_SHA,COMMIT,normalize_build_identity

EXPECTED='3364cdc0f76e674633f2f8193b2e065af03a73f0e3a8fd4ed67c59f541c50e69'


def build(archive,compiler,output):
    archive=Path(archive).resolve();compiler=Path(compiler).resolve();output=Path(output).resolve()
    if hashlib.sha256(archive.read_bytes()).hexdigest()!=SOURCE_SHA:raise ValueError('Unreviewed upstream source archive.')
    if hashlib.sha256(compiler.read_bytes()).hexdigest()!=COMPILER_SHA:raise ValueError('Unreviewed compiler executable.')
    if subprocess.check_output([str(compiler),'version'],text=True).strip()!='0.15.2':raise ValueError('Pinned Zig 0.15.2 required.')
    output.mkdir(parents=True,exist_ok=False);source=output/'source'
    with ZipFile(archive) as package:
        for entry in package.infolist():
            parts=PurePosixPath(entry.filename).parts[1:]
            if not parts or entry.is_dir() or (parts[0]!='src' and parts[0]!='license.txt' and not (parts[:2]==('lib','sofa'))):continue
            target=source.joinpath(*parts)
            if not target.resolve().is_relative_to(source):raise ValueError('Unsafe upstream archive member.')
            target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(package.read(entry))
    wrapper=Path(__file__).with_name('network_kernel.c')
    names='rtkcmn trace rinex rtkpos postpos solution lambda geoid sbas preceph pntpos ephemeris options ppp ppp_ar rtcm rtcm2 rtcm3 rtcm3e ionex tides sofa'.split()
    executable=output/'network_kernel.exe'
    args=[str(compiler),'cc','-target','x86_64-windows-gnu','-O2','-std=c17','-DWIN32','-DWIN_STATIC','-DTRACE','-DENAGLO','-DENAGAL','-DENACMP','-DNFREQ=3','-DNEXOBS=3','-I'+str(source/'src'),str(wrapper),*[str(source/'src'/(name+'.c')) for name in names],'-ffile-prefix-map='+str(source)+'=/rtklib','-lwinmm','-lws2_32','-o',str(executable)]
    temporary=output/'tmp';temporary.mkdir()
    environment=dict(os.environ,ZIG_GLOBAL_CACHE_DIR=str(archive.parent/'zig-global-cache'),ZIG_LOCAL_CACHE_DIR=str(output/'local-cache'),SOURCE_DATE_EPOCH='0',TEMP=str(temporary),TMP=str(temporary))
    result=subprocess.run(args,capture_output=True,text=True,timeout=300,env=environment)
    (output/'build.log').write_text(result.stdout+'\n'+result.stderr,encoding='utf-8')
    if result.returncode:raise ValueError('Build failed; retain and inspect local build.log.')
    original=hashlib.sha256(executable.read_bytes()).hexdigest()
    executable.write_bytes(normalize_build_identity(executable.read_bytes(),'network_kernel.pdb'))
    digest=hashlib.sha256(executable.read_bytes()).hexdigest()
    if digest!=EXPECTED:raise ValueError('Built network kernel differs from the reviewed identity; do not silently approve it.')
    receipt=dict(upstream_commit=COMMIT,upstream_source_sha256=SOURCE_SHA,compiler_sha256=COMPILER_SHA,
        own_wrapper_sha256=hashlib.sha256(wrapper.read_bytes()).hexdigest(),raw_binary_sha256=original,kernel_sha256=digest,
        identity=subprocess.check_output([str(executable),'identity'],text=True).strip(),
        rights='Own GPL-3.0-only wrapper; upstream BSD-2-Clause and SOFA notices retained. Separate experimental engine, not an upstream release. No binary distribution with plugin.')
    (output/'PROVENANCE.json').write_text(json.dumps(receipt,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(receipt,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',required=True);parser.add_argument('--compiler',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args();build(args.source,args.compiler,args.output)
