# Publishing and data privacy

The software is provided "AS IS", without warranty. Disclaimers and limitations
of liability apply only to the extent permitted by GPLv3 sections 15–17 and
applicable law. This does not exclude statutory liability that cannot lawfully
be excluded. See LICENSE.

Use RELEASE_TEMPLATE.md for every GitHub version, including prereleases. Keep the notice in README, NOTICE, DISCLAIMER.txt and release descriptions. Preserve the project licence, attributions and optional Support/License footer.

Only reviewed source, documentation and constructed synthetic fixtures may be published. Do not upload customer measurements, reference-station coordinates, GNSS/RINEX/RTCM data, receiver serials, project names, QGIS project files, GeoPackages, reports, screenshots, local paths, passwords, NTRIP credentials, private keys, tokens or installed binaries. The author name, project website and support link are intentionally public. The plugin continues to read and write user data locally; these rules apply to distribution.

## Before committing or uploading

1. Activate repository hooks once: `git config core.hooksPath .githooks`. Python must be available as `python`, or configure this checkout with `git config survey.python <path-to-python>`. This local Git setting is never committed. SURVEY_PYTHON may override it for one run.
2. Review the exact staged diff. `python -B tools/check_release.py --staged --self-test` checks index bytes and blocks unknown files, secrets and changed measurement fixtures.
3. Use `python -B tools/safe_push.py` for GitHub pushes. It scans the working tree and all reachable history, runs pinned Gitleaks, builds and verifies the exact ZIP, then performs a normal non-forced push to origin/main. Any scan/download/build failure stops the push.
4. Before uploading a release asset, run `python -B tools/check_release.py --archive <plugin-version.zip> --release-notes RELEASE_NOTES.txt`, and scan with `python -B tools/run_secret_scan.py`. Copy the qualified notice from RELEASE_TEMPLATE.md into the GitHub release. Never upload the whole working folder.

publication_manifest.json lists every approved repository file and exact ZIP mapping. Measurement fixtures are locked by SHA256 with a synthetic-data provenance note. Hashes normalize CRLF to LF for Git/Windows compatibility; every other byte and field value is retained. New or changed fixtures must be manually reviewed as constructed synthetic data before updating the manifest. Do not regenerate fixture hashes to silence a failure. Never add real data to the allowlist. Tests may generate temporary files locally.

The pre-commit hook checks staged bytes. The pre-push hook also scans complete history with Gitleaks. GitHub Actions repeats these checks on pushes, pull requests and release events without write permissions or stored checkout credentials. The build uses exact package members from the manifest and includes the licence and disclaimer.

## Limits and incident response

Automated checks reduce risk; they cannot prove that every sensitive value is absent. Hooks can be bypassed, browser uploads and other checkouts are outside local hook control, and GitHub Actions runs after an upload. Always review before sharing. Do not claim that a private repository or a passing scan makes customer data safe to publish. If a secret is found, stop uploading and revoke/rotate it before attempting history cleanup. Deleting a file from the current tree does not remove history, release assets, forks or caches.
