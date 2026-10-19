# SPDX-License-Identifier: GPL-3.0-only
"""Build only exact reviewed members; refuse unsafe inputs and verify the final ZIP."""
from pathlib import Path
import configparser
import hashlib
from zipfile import ZipFile, ZIP_DEFLATED
from tools.check_release import ROOT, checked_package, inspect_archive, worktree_files
import json

members = checked_package()
metadata = configparser.ConfigParser()
metadata.read(ROOT / "survey_adjustment/metadata.txt", encoding="utf-8")
output = ROOT / ("survey_adjustment-" + metadata["general"]["version"] + ".zip")
with ZipFile(output, "w", ZIP_DEFLATED) as archive:
    for destination, source in sorted(members.items()):
        archive.write(ROOT / source, destination)
policy = json.loads((ROOT / "publication_manifest.json").read_text(encoding="utf-8"))
if inspect_archive(output, worktree_files(), policy):
    raise ValueError("Final ZIP differs from reviewed publication allowlist.")
print(output.name)
print("SHA256", hashlib.sha256(output.read_bytes()).hexdigest())
