# SPDX-License-Identifier: GPL-3.0-only
"""Explicit allowlist: no engine binaries, local paths, recordings or private data."""
import hashlib
from pathlib import Path
from zipfile import ZipFile,ZIP_DEFLATED

ROOT = Path(__file__).resolve().parent
package = ROOT/'survey_adjustment'
output = ROOT/'survey_adjustment-0.2.1.zip'
with ZipFile(output,'w',ZIP_DEFLATED) as archive:
    for path in sorted(package.rglob('*')):
        if path.is_file() and '__pycache__' not in path.parts and (path.suffix in ('.py','.txt','.csv') or path.name=='LICENSE'):
            archive.write(path,path.relative_to(ROOT).as_posix())
print(output)
print('SHA256',hashlib.sha256(output.read_bytes()).hexdigest())
