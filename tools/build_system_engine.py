# SPDX-License-Identifier: GPL-3.0-only
"""Build a separate, pinned experimental RTKLIB receiver-PCV engine locally.

Requires separately downloaded official RTKLIB source ZIP and Zig 0.15.2.
No downloads, system installation or binary distribution by this script.
Original RTKLIB and SOFA notices remain in the source archive/build directory.
Patch anchors derive from RTKLIB by T. Takasu and other contributors;
see RTKLIB_LICENSE.txt for the retained upstream copyright and BSD conditions.
"""
from pathlib import Path,PurePosixPath
from zipfile import ZipFile
import argparse,hashlib,json,subprocess,shutil,os,struct

SOURCE_SHA='99d25f03656158be1924beb8e92239ce8138a8f6181d915571831617f4ccc9a1'
COMMIT='62d4677ed8425a4e2748c6d390b500d1afb493fc'
COMPILER_SHA='d408dd38eed3e5204af841bcebf70502a4dbbb8399a3a3262be55059370bc018'


def normalize_build_identity(payload):
    """Normalize only timestamps/CodeView GUID in our unsigned compiler output.

    Zig emits path-dependent debug identities even with no debug symbols.
    Machine code/data, imports, sections and application behaviour are untouched.
    This is not signing/authentication; the final full SHA256 is the engine ID.
    """
    data=bytearray(payload);pe=struct.unpack_from('<I',data,60)[0];opt=pe+24
    if data[:2]!=b'MZ' or data[pe:pe+4]!=b'PE\0\0' or struct.unpack_from('<H',data,opt)[0]!=0x20b:raise ValueError('Unexpected compiler output format.')
    if struct.unpack_from('<II',data,opt+112+4*8)!=(0,0):raise ValueError('Never normalize a signed executable.')
    sections=struct.unpack_from('<H',data,pe+6)[0];optsize=struct.unpack_from('<H',data,pe+20)[0]
    rva,size=struct.unpack_from('<II',data,opt+112+6*8);location=None
    for index in range(sections):
        at=opt+optsize+40*index;vsz,va,rawsize,raw=struct.unpack_from('<IIII',data,at+8)
        if va<=rva<va+max(vsz,rawsize):location=raw+rva-va
    if location is None or size!=56:raise ValueError('Unexpected pinned compiler debug directory.')
    struct.pack_into('<I',data,pe+8,0)
    for at in range(location,location+size,28):
        _,_,_,_,kind,count,_,raw=struct.unpack_from('<IIHHIIII',data,at)
        if kind not in (2,16):raise ValueError('Unexpected compiler debug record.')
        struct.pack_into('<I',data,at+4,0)
        if kind==2:
            if count!=37 or data[raw:raw+4]!=b'RSDS':raise ValueError('Unexpected CodeView identity.')
            data[raw+4:raw+20]=bytes(16)
    return bytes(data)


def replace_one(path,old,new):
    text=path.read_text(encoding='utf-8')
    if text.count(old)!=1:raise ValueError('Pinned source contract differs: '+path.name)
    path.write_text(text.replace(old,new),encoding='utf-8',newline='\n')


def build(archive,zig,output):
    archive=Path(archive).resolve();zig=Path(zig).resolve();output=Path(output).resolve()
    if hashlib.sha256(archive.read_bytes()).hexdigest()!=SOURCE_SHA:raise ValueError('Unreviewed source archive.')
    if hashlib.sha256(zig.read_bytes()).hexdigest()!=COMPILER_SHA:raise ValueError('Unreviewed compiler executable.')
    if subprocess.check_output([str(zig),'version'],text=True).strip()!='0.15.2':raise ValueError('Pinned Zig 0.15.2 required.')
    output.mkdir(parents=True,exist_ok=False);source=output/'source'
    with ZipFile(archive) as package:
        for item in package.infolist():
            parts=PurePosixPath(item.filename).parts[1:]
            if not parts or item.is_dir() or parts[0] not in ('src','app','lib','license.txt'):continue
            if parts[0]=='app' and (len(parts)<3 or parts[1:3]!=('consapp','rnx2rtkp')):continue
            if parts[0]=='lib' and (len(parts)<2 or parts[1]!='sofa'):continue
            target=source.joinpath(*parts)
            if not target.resolve().is_relative_to(source):raise ValueError('Unsafe source path.')
            target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(package.read(item))
    header=source/'src/rtklib.h';cmn=source/'src/rtkcmn.c';rtk=source/'src/rtkpos.c'
    replace_one(header,'"2.5.1"','"2.5.1-SA1"')
    replace_one(header,'double off[NFREQ][ 3];',
        'double sa_off[4][NFREQ][3];\n    double sa_var[4][NFREQ][19];\n    unsigned int sa_present[4];\n    double off[NFREQ][ 3];')
    with header.open('a',encoding='utf-8') as handle:handle.write('\nint sa_system_index(int sys);\nint sa_antmodel(const pcv_t *,const double *,const double *,int,int,int,double *);\n')
    replace_one(cmn,'int i,f,freq=0,state=0,freqs[]={1,2,5,0};','int i,f,freq=0,state=0,freqs[]={1,2,5,0},sa_sys=-1,sa_slot=-1;')
    replace_one(cmn,"if (!pcv.sat&&buff[3]!='G') continue; /* only read rec ant for GPS */",'''sa_sys=sa_slot=-1;
            if (!pcv.sat) {
                int sys=buff[3]=='G'?SYS_GPS:buff[3]=='R'?SYS_GLO:buff[3]=='E'?SYS_GAL:buff[3]=='C'?SYS_CMP:0;
                sa_sys=sa_system_index(sys);
                f=atoi(buff+4);
                if (sys==SYS_GPS||sys==SYS_GLO) sa_slot=f==1?0:f==2?1:f==5?2:-1;
                else if (sys==SYS_GAL) sa_slot=f==1?0:f==7?1:f==5?2:-1;
                else if (sys==SYS_CMP) sa_slot=f==2?0:f==7?1:f==5?2:-1;
                if (sa_sys>=0&&sa_slot>=0&&sa_slot<NFREQ) pcv.sa_present[sa_sys]|=1u<<sa_slot;
                freq=0; continue;
            }''')
    replace_one(cmn,'if (freq<1||NFREQ<freq) continue;\n            if (decodef(buff,3,neu)<3) continue;', '''if (!pcv.sat) {
                if (sa_sys<0||sa_slot<0||sa_slot>=NFREQ||decodef(buff,3,neu)<3) continue;
                pcv.sa_off[sa_sys][sa_slot][0]=neu[1];
                pcv.sa_off[sa_sys][sa_slot][1]=neu[0];
                pcv.sa_off[sa_sys][sa_slot][2]=neu[2]; continue;
            }
            if (freq<1||NFREQ<freq) continue;
            if (decodef(buff,3,neu)<3) continue;''')
    replace_one(cmn,'if (freq<1||NFREQ<freq) continue;\n            if ((i=decodef(buff+8,19,pcv.var[freq-1]))<=0) continue;', '''if (!pcv.sat) {
                if (sa_sys>=0&&sa_slot>=0&&sa_slot<NFREQ) decodef(buff+8,19,pcv.sa_var[sa_sys][sa_slot]);
                continue;
            }
            if (freq<1||NFREQ<freq) continue;
            if ((i=decodef(buff+8,19,pcv.var[freq-1]))<=0) continue;''')
    replace_one(rtk,'antmodel(opt->pcvr+base,opt->antdel[base],azel+i*2,opt->posopt[1],\n                 dant);',
        'if (!sa_antmodel(opt->pcvr+base,opt->antdel[base],azel+i*2,opt->posopt[1],\n                         satsys(obs[i].sat,NULL),opt->nf,dant)) return 0;')
    extension=Path(__file__).with_name('system_pcv.c');shutil.copy2(extension,source/'src/system_pcv.c')
    names='rtkcmn trace rinex rtkpos postpos solution lambda geoid sbas preceph pntpos ephemeris options ppp ppp_ar rtcm rtcm2 rtcm3 rtcm3e ionex tides sofa system_pcv'.split()
    command=[str(zig),'cc','-target','x86_64-windows-gnu','-O2','-std=c17','-DWIN32','-DWIN_STATIC','-DTRACE',
        '-DENAGLO','-DENAGAL','-DENACMP','-DNFREQ=3','-DNEXOBS=3','-I'+str(source/'src'),
        str(source/'app/consapp/rnx2rtkp/rnx2rtkp.c'),*[str(source/'src'/(name+'.c')) for name in names],
        '-ffile-prefix-map='+str(source)+'=/rtklib','-lwinmm','-lws2_32','-o',str(output/'rnx2rtkp.exe')]
    temporary=output/'tmp';temporary.mkdir()
    environment=dict(os.environ,ZIG_GLOBAL_CACHE_DIR=str(archive.parent/'zig-global-cache'),ZIG_LOCAL_CACHE_DIR=str(output/'local-cache'),SOURCE_DATE_EPOCH='0',TEMP=str(temporary),TMP=str(temporary))
    result=subprocess.run(command,capture_output=True,text=True,timeout=300,env=environment)
    (output/'build.log').write_text(result.stdout+'\n'+result.stderr,encoding='utf-8')
    if result.returncode:raise ValueError('Build failed; inspect local build.log.')
    executable=output/'rnx2rtkp.exe';original_hash=hashlib.sha256(executable.read_bytes()).hexdigest()
    executable.write_bytes(normalize_build_identity(executable.read_bytes()))
    provenance=dict(upstream_commit=COMMIT,source_archive_sha256=SOURCE_SHA,compiler_version='0.15.2',
        compiler_sha256=hashlib.sha256(zig.read_bytes()).hexdigest(),extension_sha256=hashlib.sha256(extension.read_bytes()).hexdigest(),
        command=command,engine_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),pre_normalization_sha256=original_hash,
        normalization='Only unsigned PE/COFF timestamps and CodeView GUID, for deterministic rebuilds; machine code/data unchanged.',
        identity='RTKLIB EX 2.5.1-SA1 experimental local receiver-PCV extension',
        rights='Own extension GPL-3.0-only; upstream BSD-2-Clause and SOFA notices retained. Not an upstream release. Engine binaries not included in plugin/publication.')
    (output/'PROVENANCE.json').write_text(json.dumps(provenance,indent=2)+'\n',encoding='utf-8')
    print(provenance['identity']);print(provenance['engine_sha256'])


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-zip',required=True);parser.add_argument('--zig',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args();build(args.source_zip,args.zig,args.output)
