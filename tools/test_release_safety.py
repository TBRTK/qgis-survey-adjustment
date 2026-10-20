"""Adversarial publication tests: reject leaks, changed data and tampered ZIPs."""
from pathlib import Path
from zipfile import ZipFile
import hashlib
import io
import warnings
import unittest
from tools.check_release import content_issues, inspect_archive, inspect_files, notice_present, path_issues

class PublicationSafetyTests(unittest.TestCase):
    def test_reject_private_keys_and_tokens(self):
        self.assertTrue(content_issues(b'-----BEGIN ' + b'RSA PRIVATE KEY-----'))
        self.assertTrue(content_issues(b'github_' + b'pat_' + b'Z' * 50))

    def test_reject_embedded_credentials_and_personal_paths(self):
        self.assertTrue(content_issues(b'password=' + b'"synthetic-password"'))
        self.assertTrue(content_issues(b'ntrip://' + b'example:fake-password' + b'@example.invalid'))
        self.assertTrue(content_issues(b'C:' + bytes([92]) + b'Users' + bytes([92]) + b'example' + bytes([92])))

    def test_private_files_cannot_be_approved_by_allowlist(self):
        for name in ('raw.obs', 'project.qgz', '.env', 'private_data/data.txt', 'id_ed25519'):
            with self.subTest(name=name):
                self.assertTrue(path_issues(name))

    def test_changed_or_unknown_measurements_stop_publication(self):
        data = b'SYNTHETIC\n'
        policy = {'repository_files': ['demo.csv'], 'synthetic_fixtures': {
            'demo.csv': {'sha256': hashlib.sha256(data).hexdigest()}}, 'historical_synthetic_sha256': []}
        self.assertFalse(inspect_files({'demo.csv': data}, policy))
        self.assertFalse(inspect_files({'demo.csv': data.replace(b'\n', b'\r\n')}, policy))
        self.assertTrue(inspect_files({'demo.csv': data + b'new record\n'}, policy))
        self.assertTrue(inspect_files({'customer.csv': data}, policy))
        self.assertTrue(inspect_files({'demo.csv': data}, policy, historical=True))

    def test_unknown_source_is_not_silently_packaged(self):
        policy = {'repository_files': [], 'synthetic_fixtures': {}, 'historical_synthetic_sha256': []}
        self.assertTrue(inspect_files({'private_notes.py': b'notes'}, policy))

    def test_mixed_snapshots_require_exact_reviewed_synthetic_hashes(self):
        data=b'{"synthetic":true}\n'; name='examples/training.samix.json'
        policy={'repository_files':[name],'synthetic_fixtures':{},'historical_synthetic_sha256':[]}
        self.assertTrue(inspect_files({name:data},policy))
        policy['synthetic_fixtures'][name]={'sha256':hashlib.sha256(data).hexdigest()}
        self.assertFalse(inspect_files({name:data},policy))
        self.assertTrue(inspect_files({name:data+b'\n'},policy))

    def test_archive_rejects_added_missing_duplicate_and_changed_files(self):
        policy = {'package_files': {'plugin/LICENSE': 'LICENSE', 'plugin/DISCLAIMER.txt': 'DISCLAIMER.txt'}}
        files = {'LICENSE': b'GPL', 'DISCLAIMER.txt': b'AS IS'}
        for extra, missing, tampered, duplicate in ((True, False, False, False), (False, True, False, False), (False, False, True, False), (False, False, False, True), (False, False, False, False)):
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', UserWarning)
                path = io.BytesIO()
                with ZipFile(path, 'w') as archive:
                    archive.writestr('plugin/LICENSE', b'GPL')
                    if not missing:
                        archive.writestr('plugin/DISCLAIMER.txt', b'tampered' if tampered else b'AS IS')
                    if extra:
                        archive.writestr('plugin/customer.csv', b'private')
                    if duplicate:
                        archive.writestr('plugin/LICENSE', b'GPL')
                path.seek(0)
                self.assertEqual(bool(inspect_archive(path, files, policy)), extra or missing or tampered or duplicate)

    def test_notice_requires_qualified_warranty_and_liability(self):
        notice = b'AS IS, without warranty. Limitations of liability under GPLv3 sections 15-17 and applicable\nlaw.'
        self.assertTrue(notice_present(notice))
        self.assertFalse(notice_present(b'AS IS'))
        self.assertFalse(notice_present(notice.replace(b'applicable\nlaw', b'')))

    def test_binary_or_unsafe_paths_rejected(self):
        self.assertTrue(content_issues(b'\x00binary'))
        self.assertTrue(path_issues('../private.csv'))

if __name__ == '__main__':
    unittest.main(verbosity=2)
