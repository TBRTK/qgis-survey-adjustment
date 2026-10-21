# Survey Adjustment

0.18.0 adds EXPERIMENTAL three-physical-reference FLOAT VRS in the SAME GNSS
dialog/catalogue/background queue. Joint DD GLS preserves shared master/pivot
covariance and non-integer phase bias: v8 requires AR OFF, no eligible FIX or
independent baseline. Do not fit rover data/control or substitute references.
Keep explicit ITRF2020 epochs, negative weights/extrapolation, zero spatial DOF,
clock bounds, NOAZI/provider limitations and original physical sources. Rebuild
geometry and virtual RINEX from local job copies on readback with the separate
pinned kernel. Full integer network/accuracy and precise products remain absent.
Real measurements/calibration/coordinates/results stay private and ignored.
User chose Oslo, Trondheim and Stavanger. Initial withheld field comparison
must remain visible even when worse than physical-reference processing.

0.17.0 adds explicit ITRF2020 static GPS v6, recorded $CLK-to-RINEX epoch audit,
recalculated clock CSV and a separate selected-epoch known Check-point comparison
using pinned local national EUREF89/NKG grids plus existing projection/HREF. Never
relabel global vectors ETRS89 or infer IGc20 alignment/coordinate epoch. Require
explicit national control and height-version review; no covariance propagation or
accuracy guarantee. Real measurements/calibration/comparisons stay ignored/local.
User expanded reference selection through Oslo/Trondheim distances and explicitly
allowed ignoring azimuth PCV for the current local experiment. Preserve original
provider model warnings/grids and vertical PCO; no invented unknown heading.

User direction 2026-10-21: STOP tilt/3D development. Main scope is stationary
30-100 km GNSS, reference data from own bases/Onocoy/Emlid Caster/BKG, and VRS
research. Do not resume the saved private dynamic-antenna draft without an explicit
request. 0.16.0 adds an experimental upstream GPS L1/L2 static atmosphere v5
profile, reusing the same engine/import/queue. Preserve explicit model provenance,
every epoch's matched ION/TROP/two-frequency residual evidence and recalculated
atmosphere CSV. Wet delay is not total delay or accuracy. Do not claim precise
products, Hatanaka conversion, downloads, caster recording or VRS synthesis.
Only declared station-change events can be detected; conversions may lose them.

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
0.11.0 extends that flow with GPS/Galileo/BeiDou and strict GPS-only ANTEX 1.4
receiver models. Never apply GPS patterns to other constellations. Require exact
antenna/radome/serial/generic/validity selection; no engine fallback. Preserve
original and selected calibration hashes, H/E/N and every constellation used.
The pinned BSD-2-Clause external binary remains separate from the GPL plugin.
0.12.0 adds a separate experimental Survey SA1 engine contract with per-system
receiver arrays, GLONASS FDMA coverage validation and correction-application audit.
Keep engine variants/hash identities explicit; upstream v1/v2 contracts unchanged.
No GPS-pattern, E5a/E5b, C01/C02 or antenna/radome fallback. GLONASS AR stays off.
No combined engine/compiler/upstream source or real calibration data in releases.
Full RS4 Pro support is not claimed: official azimuth/band model is outside this
limited profile. See docs/SYSTEM_ENGINE.txt and docs/RS4_PRO_SUPPORT.txt.
0.13.0 adds SA2 v4 in the SAME flow: complete azimuth grids, explicit fixed level
NRP headings and true G01/G02, R01/R02, E01/E05, C01/C06 IDs. Changing yaw/tilt
must be blocked, never inferred from track/IMU/camera. Preserve provider comments,
including RS4P copied/unverified calibration qualification. No full device/field
accuracy claim. Keep original upstream/SA1 audit contracts readable. Same signals
at both receivers; F/NAV clock selection, no invented B1C SPP bias; actual relative
BDS observations remain in residual reports. Audit every grid sample and separately
recalculate an applied correction. See docs/SYSTEM_ENGINE.txt for precise limits.
0.14.0 adds an ETC coordinate supplement inside the SAME processing dialog and
background queue. Preserve original raw engine/covariance/baseline contracts.
Unknown endpoint/frame/GGA-height convention blocks correction. Exact UTC joins
with explicit ZDA/RMC dates only. Already-ground coordinates remain unchanged;
never subtract antenna height twice. Retain exclusions/heading/tilt/provider
coefficients without fabricating full attitude/covariance/independent observations.
No full dynamic raw ANTEX/carrier correction is inferred from ETC heading.
Real orientation logs and local exported packages remain private; generate only
invented examples in gnss_demo.py. See docs/ORIENTATION.txt for precise limits.
0.15.0 adds optional exact recorded RINEX/ETC coverage in that same dialog.
Reuse inspect_source and existing reviewed source-minus-UTC metadata. Keep 100 ns
raw ticks exact; nearest deltas are diagnostic, never matching or clock inference.
Clock-offset epochs require a separately reviewed model. Local linked snapshots
copy only the selected rover and must recalculate without external source access.
Coordinate acceptance and raw coverage remain separate. No physical timing,
attitude or dynamic-carrier approval is inferred. Preserve v1 snapshot readback.
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
notes, qualified by GPLv3 sections 15â€“17 and applicable law. Retain GPL-3.0-only,
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
