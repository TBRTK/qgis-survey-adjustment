# Survey Adjustment

Keep this plugin independent of SOSI Survey. Package name: survey_adjustment.
Use QGIS 4 / Qt6 APIs. The SALSA engine runs as a separate program.

User direction, 2026-10-20: reuse and improve existing GNSS workflows. Add only
useful missing capabilities. Before adding a feature, inspect existing import,
validation, project, processing, map, report and delivery code and identify the
specific gap. Extend the owning component; extract shared logic where needed
instead of copying it. Do not create parallel imports, duplicate inventory
observations, competing project stores or another GNSS Solutions workflow/tab.
RINEX/raw-data support belongs in the existing GNSS/network workflow;
reuse core.py/coordinate_job.py for coordinate input, baselines.py for processed
relative solutions and its duplicate-import checks, network.py for inventory,
and existing quality/map/delivery components downstream. New format adapters
must preserve the distinction between raw observations, absolute coordinate
solutions and measured relative baselines, including covariance/dependence and
provenance. A shared entry point must not silently reinterpret one as another.
Do not reimplement import audit/fingerprint checks in a raw-processing module.
See docs/GNSS_SOLUTIONS_AND_VRS.txt for the agreed scope. This direction does not
mean arbitrary raw engines or VRS are supported. Version 0.9.0 adds the raw
catalogue; 0.10.0 adds explicitly reviewed RTKLIB EX 2.5.1 GPS relative processing.
The pinned BSD-2-Clause external binary remains separate from the GPL plugin.
Preserve original engine POS/covariance, raw occupation identity, base coordinates,
antenna offsets and processing audit. Static/PPK epochs are correlated; no
automatic averaging, coordinate updates or independent baseline insertion.
Use the existing background queue, POS review and baseline gate. Never relabel
a raw occupation through another session/mode/epoch as a new independent vector.
Keep legacy schema-2 serialization unchanged; raw links opt the project into
schema 3. Do not infer UTC offsets or turn header approximate XYZ into controls.
Only the three hash-reviewed synthetic .rnx parser fixtures may be published.
Keep external source files, raw catalogue projects and source paths private.

The user requires a clear AS IS / no-warranty / liability-limitation notice in
every version posted on GitHub. Preserve this in README, NOTICE, and release
notes, qualified by GPLv3 sections 15–17 and applicable law. Retain GPL-3.0-only,
the license file, and correct attribution. Do not imply official SALSA/Emlid
endorsement. Engine binaries and dependencies are not part of this plugin ZIP.
Keep the user's Support and License footer at the end of both root and packaged
README, linking to https://buymeacoffee.com/tb_rtk and LICENSE.

Synthetic examples must remain clearly identified. Coordinate repeatability
checks are not a raw GNSS/network adjustment. Do not silently reinterpret
orthometric heights, RMS fields, independence, or covariance.

Before any GitHub upload, follow docs/PUBLISHING.md and RELEASE_TEMPLATE.md. Run the publication/privacy gate, all-history Gitleaks scan and exact ZIP verification. Only constructed synthetic data may be published. No real measurements, reports, backups, customer information, personal paths or credentials. Do not bypass a scan failure or automatically approve changed fixture hashes.

Measured baseline import retains full covariance and source provenance in local
schema-2 networks. Keep processing-base/rover, frame, time system, coordinate
epoch, antenna corrections and dependence explicit. Do not create baselines from
arbitrary point lists or treat correlated POS epochs as independent repeats.
Never infer missing covariance, UTC offsets, geoid or antenna corrections.
Real POS sources and import audits are private project data, not release fixtures.

GNSS network quality is a separate ETRS89/ELLIP snapshot workflow. Preserve full
within-vector covariance, joint point covariance, prior grid-axis semantics and
held-out checks. Do not silently add datum/height/antenna corrections, drop active
observations, infer independence or rescale covariance by APV. Statistical model
tests and redundancy are not survey approval or accuracy guarantees. Zero degrees
of freedom must remain NOT_TESTABLE. Verify the separate SALSA run against the
independent model and source/input/engine hashes. Keep generated reports local.

Network map graphics are derived snapshots. Preserve actual PointZ/baseline
heights and unscaled covariance/deltas. Enlarged ellipses/arrows must retain
DISPLAY ONLY labels and explicit scale factors. Fixed covariance is an assumption;
horizontal/vertical intervals are separate marginal regions. Preserve original
records/provenance, unmapped/excluded visibility and read-only run comparison.
Never infer significance from correlated alternative-run coordinate differences.
Real map packages/screenshots remain local, outside the publication allowlist.

Norwegian delivery review is a separate local snapshot workflow. Require evidence
for national EUREF89, coordinate epoch versus acquisition times, explicit height
version and model/uncertainty assumptions. Preserve original and adjusted points.
HREF conversion accepts only reviewed official hashes and mandatory local grids;
never auto-approve another model, download grids, select ballpark fallback or infer
frame/epoch shifts. Preserve NN2000:2018/2025 distinction and project continuity.
Keep real delivery folders and HREF binaries outside public distribution. Preserve
model attribution/license in local packages. Output-coordinate covariance is not
propagated in this profile: do not present original sigmas as transformed accuracy.
Checksums/readback are integrity checks, not authenticity or delivery certification.
