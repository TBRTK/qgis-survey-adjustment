"""Release/privacy gate. No measurement contents or secret values are logged."""
from pathlib import Path, PurePosixPath
from zipfile import ZipFile
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
DATA_SUFFIXES = {'.csv', '.tsv', '.sos', '.sosi', '.pos', '.sanet.json', '.rnx'}
BLOCKED_SUFFIXES = {'.pem', '.key', '.p12', '.pfx', '.kdbx', '.sqlite', '.db',
    '.qgs', '.qgz', '.gpkg', '.rinex', '.obs', '.nav', '.ubx', '.rtcm', '.rtcm3',
    '.bag', '.pcap', '.pcapng', '.las', '.laz', '.exe', '.dll', '.msi', '.bin', '.zip', '.gz', '.crx'}
BLOCKED_PARTS = {'test_output', 'upstream', 'recordings', 'customer_data', 'private_data',
    '.ssh', '__pycache__', 'installed_backups', 'node_modules'}
RULES = {
    'private key': re.compile(rb'-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----'),
    'access token': re.compile(rb'(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[A-Z0-9]{16})'),
    'credentials in URL': re.compile(rb'(?i)\b[a-z][a-z0-9+.-]*://[^\s/@:]+:[^\s/@]+@'),
    'literal credential': re.compile(rb'''(?i)\b(?:password|passwd|api[_-]?key|access[_-]?token|client[_-]?secret|ntrip[_-]?(?:password|user))\s*[:=]\s*["'][^"'\r\n]{8,}["']'''),
    'personal computer path': re.compile(rb'(?i)(?:[a-z]:[\\/]Users[\\/][a-z0-9._-]+[\\/]|/(?:home|Users)/[a-z0-9._-]+/)'),
    'private network address': re.compile(rb'\b(?:192\.168\.[0-9]{1,3}\.[0-9]{1,3}|10\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3})\b'),
}

def git(*args):
    result = subprocess.run(['git', '-C', str(ROOT), *args], capture_output=True, timeout=120)
    if result.returncode:
        raise ValueError('Git inventory failed; no command output logged.')
    return result.stdout

def path_issues(name):
    path = PurePosixPath(name)
    parts = {part.lower() for part in path.parts}
    issues = []
    if path.is_absolute() or '..' in path.parts or '\\' in name or ':' in name:
        issues.append('unsafe path')
    if parts & BLOCKED_PARTS or path.suffix.lower() in BLOCKED_SUFFIXES:
        issues.append('private/generated/binary file type')
    if path.name.lower().startswith('.env') or path.name.lower() in {'credentials', 'credentials.json', 'local_settings.json', 'id_rsa', 'id_ed25519'}:
        issues.append('credential/configuration file')
    return issues

def content_issues(data):
    if len(data) > 2_000_000:
        return ['file exceeds reviewed size limit']
    if b'\x00' in data:
        return ['unexpected binary content']
    return [name for name, rule in RULES.items() if rule.search(data)]

def is_fixture(name):
    path = PurePosixPath(name)
    return path.suffix.lower() in DATA_SUFFIXES or name.lower().endswith(('.sanet.json','.samix.json'))

def fixture_digest(data):
    # Git may convert CRLF on Windows. Preserve every byte except line endings.
    return hashlib.sha256(data.replace(b'\r\n', b'\n')).hexdigest()

def notice_present(data):
    text = ' '.join(data.decode('utf-8').split())
    return ('AS IS' in text and any(s in text for s in ('15–17', '15-17'))
            and any(s in text for s in ('without warranty', 'uten garanti'))
            and any(s in text for s in ('liability', 'ansvarsbegrensning'))
            and any(s in text for s in ('applicable law', 'gjeldende rett')))

def inspect_files(files, policy, historical=False):
    findings = []
    approved = set(policy['repository_files'])
    fixtures = policy['synthetic_fixtures']
    historical_hashes = set(policy['historical_synthetic_sha256'])
    for name, data in files.items():
        issues = path_issues(name) + content_issues(data)
        if not historical and name not in approved:
            issues.append('file not in reviewed publication allowlist')
        if is_fixture(name):
            digest = fixture_digest(data)
            if historical:
                if digest not in historical_hashes:
                    issues.append('historical measurement fixture not reviewed as synthetic')
            elif name not in fixtures or fixtures[name]['sha256'] != digest:
                issues.append('unreviewed or changed measurement fixture')
        findings.extend((name, issue) for issue in issues)
    return findings

def index_files():
    files = {}
    for entry in git('ls-files', '--stage', '-z').split(b'\x00'):
        if not entry:
            continue
        info, name = entry.split(b'\t', 1)
        mode, oid, stage = info.split()
        name = name.decode('utf-8')
        if mode != b'100644' and mode != b'100755' or stage != b'0':
            raise ValueError('Symlink, submodule or unresolved index entry is not publishable.')
        files[name] = git('cat-file', 'blob', oid.decode())
    return files

def worktree_files():
    names = git('ls-files', '--cached', '--others', '--exclude-standard', '-z').split(b'\x00')
    files = {}
    for item in names:
        if not item:
            continue
        name = item.decode('utf-8')
        path = ROOT / name
        if path.is_symlink() or not path.is_file():
            raise ValueError('Missing file or symlink in publication inventory.')
        files[name] = path.read_bytes()
    return files

def historical_files():
    # Inspect every reachable blob, including old fixture versions, not only diffs.
    entries = git('rev-list', '--objects', '--all').decode('utf-8').splitlines()
    files = {}
    for entry in entries:
        oid, _, name = entry.partition(' ')
        if not name or git('cat-file', '-t', oid).strip() != b'blob':
            continue
        files[(oid, name)] = git('cat-file', 'blob', oid)
    return files

def policy_issues(files, policy):
    findings = []
    for name in policy['notice_files']:
        if name not in files or not notice_present(files[name]):
            findings.append((name, 'qualified AS IS notice missing'))
    for destination, source in policy['package_files'].items():
        findings.extend((destination, issue) for issue in path_issues(destination))
        findings.extend((source, issue) for issue in path_issues(source))
        if not destination.startswith(policy['package'] + '/'):
            findings.append((destination, 'archive entry outside plugin package'))
        if source not in files:
            findings.append((source, 'required package source missing'))
    return findings

def inspect_archive(path, files, policy):
    findings = []
    with ZipFile(path) as archive:
        expected = policy['package_files']
        names = archive.namelist()
        if len(set(names)) != len(names) or set(names) != set(expected):
            return [('release ZIP', 'archive members differ from reviewed package allowlist')]
        if any(info.file_size > 2_000_000 or info.flag_bits & 1 for info in archive.infolist()):
            return [('release ZIP', 'oversized or encrypted archive member')]
        if archive.testzip() is not None:
            return [('release ZIP', 'corrupt archive')]
        for name, source in expected.items():
            data = archive.read(name)
            if data != files.get(source):
                findings.append((name, 'archive does not match reviewed source'))
            findings.extend((name, issue) for issue in content_issues(data))
    return findings

def checked_package():
    """Called before a build. Only exact reviewed package members are returned."""
    policy = json.loads((ROOT / 'publication_manifest.json').read_text(encoding='utf-8'))
    files = worktree_files()
    findings = inspect_files(files, policy) + policy_issues(files, policy)
    if findings:
        show_findings(findings)
        raise ValueError('Release/privacy gate refused this build.')
    return policy['package_files']

def show_findings(findings):
    for name, reason in findings:
        print(f'BLOCKED: {name}: {reason} [content withheld]', file=sys.stderr)

def self_test():
    # Construct fake test values at runtime; never commit actual credentials.
    fake_token = b'gh' + b'p_' + b'Z' * 36
    assert content_issues(fake_token)
    assert content_issues(b'-----BEGIN ' + b'RSA PRIVATE KEY-----')
    assert content_issues(b'ntrip://' + b'example:fake-password' + b'@example.invalid')
    assert content_issues(b'C:' + bytes([92]) + b'Users' + bytes([92]) + b'example' + bytes([92]))
    assert path_issues('customer_data/raw.obs') and path_issues('.env.local')
    policy = {'repository_files': ['demo.csv'], 'synthetic_fixtures': {
        'demo.csv': {'sha256': hashlib.sha256(b'SYNTHETIC\n').hexdigest()}},
        'historical_synthetic_sha256': []}
    assert not inspect_files({'demo.csv': b'SYNTHETIC\n'}, policy)
    assert inspect_files({'demo.csv': b'changed data\n'}, policy)
    assert inspect_files({'new_file.txt': b'unknown'}, policy)
    assert not notice_present(b'AS IS')

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--staged', action='store_true', help='Check exact index bytes before committing.')
    parser.add_argument('--history', action='store_true', help='Also inspect all reachable Git blobs.')
    parser.add_argument('--self-test', action='store_true')
    parser.add_argument('--archive', type=Path)
    parser.add_argument('--release-notes', type=Path)
    args = parser.parse_args()
    if args.self_test:
        self_test()
    files = index_files() if args.staged else worktree_files()
    policy = json.loads(files['publication_manifest.json'].decode('utf-8'))
    findings = inspect_files(files, policy) + policy_issues(files, policy)
    count = 0
    if args.history:
        for (oid, name), data in historical_files().items():
            count += 1
            findings.extend(inspect_files({name: data}, policy, historical=True))
    if args.archive:
        findings.extend(inspect_archive(args.archive, files, policy))
    if args.release_notes and not notice_present(args.release_notes.read_bytes()):
        findings.append(('release notes', 'qualified AS IS notice missing'))
    event_path = os.environ.get('GITHUB_EVENT_PATH')
    if os.environ.get('GITHUB_EVENT_NAME') == 'release' and event_path:
        body = json.loads(Path(event_path).read_text(encoding='utf-8'))['release']['body'] or ''
        if not notice_present(body.encode()):
            findings.append(('GitHub release', 'qualified AS IS notice missing'))
    if findings:
        show_findings(findings)
        return 1
    print(f'Release/privacy gate passed: {len(files)} current files; {count} historical blobs checked.')
    return 0

if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, KeyError, OSError) as error:
        print(f'Release/privacy gate failed ({type(error).__name__}); details withheld.', file=sys.stderr)
        raise SystemExit(1)
