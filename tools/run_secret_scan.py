"""Pinned upstream secret scanner. Fully redact findings; fail on scan/download errors."""
from pathlib import Path
from zipfile import ZipFile
import hashlib
import io
import os
import platform
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

VERSION = '8.30.0'
ROOT = Path(__file__).resolve().parents[1]
PLATFORMS = {
    ('Windows', 'AMD64'): ('windows_x64.zip', '54fe94f644b832dd08e8c3a5915efb3bfa862386d59fb27ca0792cb687a83573', 'gitleaks.exe'),
    ('Linux', 'x86_64'): ('linux_x64.tar.gz', '79a3ab579b53f71efd634f3aaf7e04a0fa0cf206b7ed434638d1547a2470a66e', 'gitleaks'),
}

def main():
    suffix, expected, executable = PLATFORMS[(platform.system(), platform.machine())]
    url = f'https://github.com/gitleaks/gitleaks/releases/download/v{VERSION}/gitleaks_{VERSION}_{suffix}'
    with tempfile.TemporaryDirectory(prefix='survey-secret-scan-') as temporary:
        folder = Path(temporary)
        with urllib.request.urlopen(url, timeout=45) as response:
            data = response.read(30_000_001)
        if len(data) > 30_000_000 or hashlib.sha256(data).hexdigest() != expected:
            raise ValueError('Scanner checksum mismatch')
        if suffix.endswith('.zip'):
            with ZipFile(io.BytesIO(data)) as archive:
                binary = archive.read(executable)
        else:
            with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
                binary = archive.extractfile(executable).read()
        program = folder / executable
        program.write_bytes(binary)
        program.chmod(0o700)
        env = dict(os.environ)
        # Force upstream rules; no repo ignore file or inline suppression can hide a finding.
        for name in ('GITLEAKS_CONFIG', 'GITLEAKS_CONFIG_TOML'):
            env.pop(name, None)
        config = folder / 'rules.toml'
        config.write_text('[extend]\nuseDefault = true\n', encoding='utf-8')
        ignore = folder / 'empty.ignore'
        ignore.write_text('', encoding='utf-8')
        args = [str(program), 'git', str(ROOT), '--log-opts=--all', '--redact=100',
                '--ignore-gitleaks-allow', '--gitleaks-ignore-path', str(ignore),
                '--config', str(config), '--no-banner', '--no-color', '--timeout=120']
        return subprocess.run(args, env=env, timeout=150).returncode

if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f'Secret scan failed ({type(error).__name__}); upload must stop.', file=sys.stderr)
        raise SystemExit(1)
