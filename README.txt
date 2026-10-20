SURVEY ADJUSTMENT 0.10.0 – INDEPENDENT PROTOTYPE FOR QGIS 4
Author: Tore Berthelsen – https://www.3dmapper.no
Date: 2026-10-20
Project: https://github.com/TBRTK/qgis-survey-adjustment

The software is provided “AS IS”, without warranty. Disclaimers and limitations
of liability apply to the extent permitted by GPLv3 sections 15–17 and applicable
law. See LICENSE. This is not an unconditional exclusion of statutory liability.

WHAT CHANGED IN 0.10.0
Adds explicit local PPK kinematic and static relative GPS processing from the
existing raw RINEX links. It reuses the background queue, saved network,
source identity checks, map construction/picking and existing POS/baseline
review. No second import workflow or new project store. Nothing is imported
automatically; a documented stationary occupation is required for a baseline.

External engine: reviewed RTKLIB EX 2.5.1 Windows rnx2rtkp.exe, exact binary
SHA256. BSD-2-Clause engine stays separate from this GPL-3.0-only plugin ZIP.
First profile: GPS L1 or L1+L2, broadcast ephemerides, explicit fixed ECEF base,
frame/coordinate epoch and header H/E/N treatment. No receiver PCV/PCO
calibration, precise products, VRS or datum/epoch transformation. Processing
other constellations or engine binaries requires a separately reviewed profile.

Jobs preserve copied inputs, original identities, settings, engine/configuration
hashes, original full-covariance POS, GPST epochs, ECEF coordinates and explicit
WGS84-ellipsoid height CSV. Stop/timeouts and changed inputs/settings invalidate
results. Static filter states and PPK epochs remain correlated. A repeated raw
occupation cannot be imported as another independent baseline. Header XYZ is
never used automatically as a fixed base. FIX is not survey approval/accuracy.

Open Network project > Raw GNSS / RINEX > PPK / static processing. Choose a
mode, then New synthetic example for selected mode for independent constructed
GPS orbits/signals with known stationary/moving truth. The old 0.9 catalogue
fixtures have nonphysical navigation and are rejected for processing.
See docs/PPK_STATIC.txt (source) or PPK_STATIC.txt (installed package).

FEATURES FROM 0.9.0
Adds raw RINEX catalogue, preflight and linked map/timeline inside the existing
Network project. Import GNSS file routes processed POS/vector CSV through the
existing measured-baseline review, and RINEX through a raw metadata review.
The same saved project, duplicate-source gate, map creation/labels/picking and
downstream quality/delivery paths are reused. Raw epochs never create points,
coordinate observations or baseline vectors. No separate GNSS Solutions tab.

First profile: RINEX 2.11 / 3.02-3.05 OBS, common broadcast NAV record framing,
optional bounded gzip. Local originals stay read-only; exact hashes and metadata
are checked again before a map/timeline refresh. Review point/session binding,
time basis and explicit source-minus-UTC offset, antenna treatment and source
classification. Gaps, signal availability, LLI indicators, missing metadata and
unaligned base/rover coverage remain visible. Background tasks are cancellable;
edits invalidate previous views. Approximate header locations are DISPLAY ONLY.

Raw sources use schema 3 of the existing network project. Legacy projects/runs
retain schema-2 serialization. Original raw bytes remain in their external files;
a saved catalogue is not a raw-file archive. Catalogue inspection alone does
not calculate positions, ambiguity resolution, orbit/fit or cycle-slip detection.
Explicit processing is a separate action within this same raw-source workflow.
See docs/RINEX.txt (source) or RINEX.txt (installed package). Three deliberately
constructed parser/preflight fixtures include nonphysical navigation data and
must never be used for positioning. Real receiver exports still need validation.

FEATURES FROM 0.8.0
Adds Norwegian reference review and traceable local delivery from completed,
verified GNSS-network and mixed-survey SALSA runs. Frame/coordinate-epoch/
height evidence, recipient/specification, purpose, reviewer, uncertainty review
and manual change/exclusion log are mandatory. Acquisition-session timestamps
are separate from coordinate epoch. Original calculations are never rewritten.

Explicit same-frame EUREF89 UTM/NTM projection and HREF2018B/HREF2025A height
conversion preserve source, adjusted and delivered coordinates separately.
Choose NN2000:2018 or NN2000:2025; no automatic model switch. Exact reviewed
local GeoTIFF hashes, independent raster interpolation and coverage/no-data
checks prevent missing-grid or ballpark fallback. Models are obtained separately,
not bundled in the plugin. The plugin performs no network download or upload.

A new local delivery folder contains unchanged calculation evidence, selected
models with attribution/license, explicit references/epoch, CSV, three PointZ
GeoPackage layers, readable reports and exact checksums. Coordinate, Z and CRS
readback is checked before accepting the folder. A separate package checker
reproduces the recorded coordinate operation; checksums are not signatures or
survey approval. Output-coordinate uncertainty is NOT propagated. Original
covariance remains in the calculation report. See docs/DELIVERY.txt in source,
or DELIVERY.txt / Help and model references in the installed English dialog.

FEATURES FROM 0.7.0
Adds a receiver-neutral CSV workflow for mixed total-station/GNSS observations
and a separate 1D levelling workflow. Open Network project -> Mixed survey /
total station and levelling. Read MIXED_SURVEY.txt (docs/MIXED_SURVEY.txt in
the source checkout), also available on the dialog's CSV field help tab.

Mixed 3D combines documented ETRS89 ECEF vectors, slope distances, clockwise
horizontal angles, zenith angles and ELLIP point-height differences. Units,
endpoint heights and prior reductions are explicit. Separate levelling 1D
preserves NN2000/NN54/ELLIP/LOCAL height differences and retains E/N unchanged.
No automatic geoid/DOV, prism/face, atmospheric or datum conversion is added.
Angles must already refer to the documented ellipsoid-normal convention.

Original CSV bytes, options and network are preserved in a .samix.json snapshot.
The independent model checks SALSA positions, covariance, dof and APV. Local
reports include model review, source/input/engine hashes, residuals in declared
units, weakest-direction redundancy, held-out checks and excluded records.
PointZ/LineStringZ result layers retain the actual heights and declared datum.
Two constructed training snapshots and CSV variants are included; real
manufacturer/export configurations still require validation. See limitations.

GNSS MAP FEATURES FROM 0.6.0
Adds a linked local GNSS network map/table workspace: actual 3D points and
baseline endpoints, full-covariance horizontal 95% ellipses, sigma-U labels,
residual/coordinate-change arrows and read-only original-record/import details.
Explicit display enlargement never changes true attributes or source heights.
Inspect weak/excluded/check connections and compare completed SALSA runs.
Add snapshot layers to QGIS or save a local GeoPackage/style/manifest package.

The versioned Network project stores points, sessions, observations and import
audits separately from the QGIS map. GNSS network adjustment through SALSA is
verified against an independent full-covariance model. Unknown, weighted known,
fixed and held-out check roles are supported. This profile requires compatible
ETRS89 vectors and ellipsoidal point heights. Raw RINEX/VRS processing remains
future work. Existing coordinate/repeatability workflows stay
available. The plugin remains separate from SOSI Survey.

INSTALLATION
In QGIS 4: Plugins > Manage and Install Plugins > Install from ZIP.
Select survey_adjustment-0.10.0.zip and enable the plugin. The Python package
is survey_adjustment. SOSI Survey remains a separate plugin.
Select the separately installed SALSA lsasolver on tab 3. Keep the full SALSA
folder with its own DLLs; a standalone lsasolver.exe is usually insufficient.

This is an independent prototype. It is not made or approved by SALSA, ARL:UT,
Emlid, QGIS, a receiver manufacturer or a Norwegian utility owner.
All calculations run locally. No measurement file is uploaded online.
Native QGIS/operating-system dialogs and number formatting follow the host
application settings; this plugin does not change QGIS's language globally.

GNSS NETWORK ADJUSTMENT AND QUALITY (TAB 5)
Practice: Load GNSS quality example, then GNSS adjustment / quality. The example
contains five invented points, seven independent full-covariance GNSS vectors,
one weighted coordinate prior and one independent held-out check. It is also
included as examples/gnss_quality.sanet.json. It is mathematical training data,
not a receiver export. Never mix training observations with field projects.

For your own data, create/open a documented network, enter the control points
and sessions, and import eligible processed baselines or enter documented
measured ECEF vectors. Check project structure first. Each connected component
needs fixed or weighted control. Approximate unknown coordinates only initialize
the model; they do not constrain its solution. Independent check points and
observations remain outside the fit.

This profile requires EUREF89/UTM zones 31-36, ELLIP point heights, a documented
coordinate epoch and ETRS89 ECEF (EPSG:4936) vectors. Grid coordinates convert
within that frame for calculation, with original coordinates retained in the
snapshot. No datum/epoch/geoid/antenna transformation is inferred. Imported
epochs must match the network epoch. Nonzero instrument/target heights are
rejected: vectors must already refer to the intended physical points. Blank
height fields are not a correction and require explicit endpoint review.
Active slope distances, height differences and WGS84 vectors are rejected,
not silently omitted. First-profile limits: 50 points and 200 active vectors.

Document uncertainty sources, common base/control errors, independence and
endpoint reductions in Model review. Explicitly confirm frame/epoch/heights,
physical endpoints and the independent covariance-block assumptions. Separate
session labels do not establish independence. Shared errors are not modelled.
Optional H95 and sigma-U limits are user warning settings, not standards.
Significance alpha defaults to 0.05; the weak-direction redundancy threshold
defaults to 0.10. Both are recorded with the result.

Preview model / network quality calculates the independent model only and is
clearly labelled SALSA has not run. Run SALSA and verify creates a new local
folder and runs the engine without blocking QGIS. Stop, timeout, failure,
changed settings/input or disagreement with the independent calculation do not
produce a verified result. Changing settings clears displayed results. A saved
report belongs to its immutable snapshot, not subsequent edits of the project.

Read the results together:
- Point uncertainty: geodetic E/N/U one-sigma values, horizontal 95% ellipse
  semi-axes and vertical 95% halfwidth. They are separate marginal regions,
  conditional on the stated model, not a combined 3D accuracy guarantee.
  A fixed point's zero model covariance is an assumption, not measured accuracy.
- Residuals / redundancy: observed-minus-calculated ECEF residuals, standardized
  with residual covariance. Local component and block tests use a Bonferroni
  correction. Redundancy fraction is internal control, not percentage accuracy;
  its weakest direction is in the tooltip/report. No/low redundancy is flagged
  even when residuals are zero. Weighted coordinate priors have their own rows.
- Overview: global two-sided chi-square, degrees of freedom, a posteriori
  variance factor (APV), rank and model warnings. Both excessive and unusually
  small scatter require review. With zero degrees of freedom fit is NOT_TESTABLE;
  determined coordinates do not imply a successful statistical quality test.
- Independent checks: observation and estimated/reference-coordinate uncertainty
  are combined, including joint fitted-point covariance. Undocumented independent
  reference uncertainty is INCONCLUSIVE; it is never replaced by zero. These
  checks are useful only when actually independent of the fitted observations.

Coordinate priors propagate the recorded diagonal grid E/N/H uncertainty through
the projection Jacobian, including convergence and scale. SALSA receives these
as explicitly tagged coordinate constraints using a numerical translation
origin, distinct from measured GNSS baselines. They are not field observations
from an invented physical base. Within-vector covariance and between-point
parameter covariance are retained. Cross-observation/session covariance is not
supported. No automatic deletion, robust estimation or APV rescaling is applied.

Verified local outputs: network.sanet.json, input.dat, input_metadata.json,
preflight.json, solution.out/bin, console.txt, quality.json/txt/html, points.csv,
residuals.csv, checks.csv and SHA256 checksums. The source, solver executable and
input hashes are checked. Full report covariance comes from the independent
model; SALSA's rounded text covariance is compared within its output precision.
Final ECEF positions must agree within 0.000002 m, and degrees of freedom/APV
must match. This verifies model transfer/calculation, not survey assumptions.
CSV text IDs starting with =, +, - or @ receive an apostrophe for spreadsheet
safety; original IDs remain exact in the JSON snapshot and report.

NumPy and SciPy are required by this analysis (tested in QGIS 4.0.0 with NumPy
2.4.2 and SciPy 1.17.0); SALSA 1.17.0 is installed separately. The plugin does
not download an engine or dependencies. Network maps, ellipses and comparison
of completed runs are available through Network map / confidence ellipses. See
docs/GNSS_SOLUTIONS_AND_VRS.txt in the source repository for the separate
GNSS Solutions design-reference/VRS investigation.

NETWORK MAP / CONFIDENCE ELLIPSES
After preview or SALSA verification, choose Network map / confidence ellipses.
The English workspace links map selection and Points/Observations tables, with
read-only originals, adjusted E/N/ellipsoidal height, uncertainty, residuals and
import provenance. Preview stays labelled SALSA NOT RUN. Verified means model
transfer agreement, not survey approval. Global test status/dof stay visible.

Choose Inspect (optionally restrict the record type), Pan, Fit network or Zoom
selected. Coincident repeated baselines are separately selectable in the table.
Layer checkboxes control ellipses, residuals, coordinate changes, originals and
excluded data. Blue links are adjustment observations, amber weak control, red
residual review, purple dashed independent checks and grey dotted exclusions.
Fixed points use triangles and a held-exact label, not a claim of zero error.

Horizontal 95% ellipses come from full fitted covariance transformed to the
project grid, including convergence, projection scale and height reduction.
Sigma-U labels/vertical intervals describe height uncertainty separately.
Ellipse and displacement display factors default to 500 for visibility; choose
1 for actual map size. DISPLAY ONLY appears in the banner, names, attributes
and export manifest. True axes/deltas remain unscaled. Decorative arrows hold
Z at their origin; actual dH remains an attribute. Actual point/baseline Z and
original source coordinates are unchanged. Do not measure enlarged graphics as
survey geometry. All uncertainties remain conditional on the stated model.

Compare saved run opens quality.json from a completed original SALSA result
folder. Checksums, source/input/output and the independent model are reviewed
read-only, using the matching separate engine binary without rerunning SALSA.
Same network/CRS/epoch/height metadata are required. Differences are CURRENT
MINUS COMPARISON in E/N/H, with unmatched/unestimated points labelled. Shared
data/errors mean these coordinate changes are not a significance test.

Add to QGIS creates a separate group of read-only temporary snapshot layers,
preserving existing project layers. It uses the canvas CRS after QGIS initializes
any blank project from its first layer. Save map creates a new LOCAL folder:
network_map.gpkg (Z retained), layer QML styles, original network snapshot,
quality/optional comparison JSON and a checksum/display/AS IS manifest. Load
the matching QML for an exported layer's style. This derived map package is
separate from the original complete SALSA report needed for rerun comparison.
See docs/NETWORK_MAP.txt in the source repository for full definitions.

MEASURED BASELINE IMPORT (TAB 5)
Create/open the correct network, add the physical base/rover point IDs and a
documented UTC acquisition session. Choose Import measured baseline, open the
processed source, select one eligible FIX row and select the matching endpoints,
session, ECEF reference frame, coordinate epoch and intended use. Document the
processor's antenna/ARP/tilt/point-height treatment and covariance assumptions.
Verify that the processing-base coordinates shown in the dialog belong to the
selected physical base point. No projection/geoid check of that association is
performed here. Confirm all three review statements, Preview selected baseline,
review the full vector and covariance, then Add baseline. Save the network
explicitly. Cancelling, invalid input or source changes after preview add nothing.

Emlid Studio / RTKLIB POS:
- Supports complete, single-header RTKLIB-style relative static/kinematic POS
  with a processing mode, ref pos, UTC or GPST calendar time, units, quality Q,
  satellite count and all six covariance fields in the documented column order.
- If a producer omits pos mode (including some Emlid Studio exports), load the
  file, then explicitly select its documented Static or Kinematic processing
  workflow in POS processing mode. This declaration is added to the local
  import audit alongside the original file checksum. It never overrides a
  conflicting header and resets when opening another source. Receiver motion
  alone does not establish processing mode. Original source bytes are retained.
- LLH requires decimal latitude/longitude and explicitly ellipsoidal heights.
  The ECEF baseline is computed from this processed rover solution minus its
  documented processing-base position. This is not a baseline fabricated by
  subtracting arbitrary independently surveyed points. Single/PPP/moving-base,
  DMS, geoid heights, missing metadata and partial covariance are rejected.
- Absolute XYZ POS is likewise differenced against its processing base.
  ENU-baseline POS is rotated at the base. LLH covariance is rotated at the
  rover. The full 3x3 covariance, including signed off-diagonal terms, is kept.
  RTKLIB sdxy/sdyz/sdzx, sdne/sdeu/sdun and sden/sdnu/sdue are signed square
  roots of cross-covariances, not correlation coefficients or RMS values.
- Only Q=1 with positive-definite full covariance is eligible. Other epochs
  remain visible as excluded. No missing covariance is replaced with zeros.
- Prefer an explicitly documented static/occupation solution (for example a
  suitable One Best output). Selecting an arbitrary FIX epoch is not a static
  session adjustment. No averaging or sigma/sqrt(n) reduction is performed.
  One baseline per endpoint pair/acquisition session prevents loading a whole
  correlated track as independent repeats. Session labels alone prove nothing.
- GPST stays GPST in the audit; it is not relabelled UTC or compared with UTC
  session bounds. Calendar timestamps support at most six fractional digits.
  UTC source observations are checked against documented session bounds.
- The POS output label WGS84 does not establish the actual correction-service
  datum/realisation. Select and document the real frame and coordinate epoch.
  Native LLH and LLH ref-pos are decoded on RTKLIB's WGS84 ellipsoid, including
  when the separately documented processing coordinates are ETRS89. Selecting
  the ECEF frame label does not change that native decoding or transform datum.
  No automatic datum, epoch, antenna, tilt or geoid corrections are performed.

Vendor-neutral processed-baseline CSV:
Use the exact header below, with comma, semicolon or tab separation. Decimal
comma is supported only with semicolon/tab. Every row is an already processed
relative vector to minus from in ECEF metres, with full covariance in m².
Point/session/observation IDs must match the source; no implicit renaming.
Use FIX and a timezone-aware time_utc. A final coordinate list or an Emlid Flow
corrected point CSV is not this baseline exchange format.

baseline_id,from_point,to_point,session,dx_m,dy_m,dz_m,cxx_m2,cxy_m2,cxz_m2,cyy_m2,cyz_m2,czz_m2,status,time_utc

Import audit and limitations:
Show import audit lists the source basename, SHA256, selected line/original row,
native values/covariance, time system, processing-base position, documented
frame/epoch/assumptions and converted ECEF vector/covariance. Original source
files are never modified or uploaded. Keep the original processor output with
your project; the audit does not embed every source header or the whole file.
Measured values, covariance, endpoint IDs, session and imported observation ID
are locked. Notes, enabled status and intended use may be reviewed. Removing
an observation retains its historical audit and duplicate protection. Correct a
wrong endpoint/source association in a separate or restored pre-import network.
Repeated source imports, renamed
copies and equivalent same-epoch re-exports are blocked. A changed processing
base (>1 mm in the same documented frame/epoch) requires review/reprocessing.
The audit is not a digital signature or a professional quality approval.

ECEF delta-Z is not an elevation or an NN2000 height difference. Existing point
heights are retained unchanged. Coordinate conversions rotate vectors/covariance
only; no GNSS point coordinates are automatically added or overwritten. Formal
POS precision is conditional on the processor's fixed base/model. Base-position,
datum, antenna and shared errors and cross-observation covariances are not added.
Import alone does not run SALSA. Compatible ETRS89 vectors can now be analysed
with the separate, explicitly reviewed GNSS adjustment / quality action above.
Raw RINEX processing is not added.
Emlid Studio 1.10 exports have been checked locally: an eligible FIX epoch was
imported through the QGIS dialog/panel, structure-checked, saved and reopened
with original vectors, covariance, audit and heights retained. Actual FLOAT
and SINGLE (Q=5) rows were read and excluded, including blocked import attempts
and a disabled Add button; empty events sources were rejected. No excluded
position was stored as a baseline. Missing processing mode requires
explicit declaration. These are technical software tests; coordinate epoch,
physical endpoint reduction, independence and survey accuracy need separate
verification. This does not validate every exporter variant. Other required
headers must be present; units, height declaration and covariance are not guessed.

Schema 1 network files and recovery drafts are upgraded to schema 2 in memory,
adding an empty import audit. Original files are changed only on explicit Save.
Schema 2 saves include provenance and cannot be opened by the older 0.3.0 plugin.
Other/unknown schema fields are rejected rather than silently dropped.

Synthetic practice: Load synthetic network, then Import measured baseline and
Load synthetic POS. Select the first FIX row, REF0 -> P01, session S01, ETRS89
ECEF and coordinate epoch 2026-10-18; enter the synthetic assumptions and make
the three explicit confirmations and the explicit synthetic-source checkbox.
The examples are mathematically constructed,
not receiver exports. Files: examples/synthetic_baseline_llh.pos,
synthetic_baseline_xyz.pos, synthetic_baseline_enu.pos and
synthetic_measured_baseline.csv. Do not mix these with real survey projects.

Format references (reviewed 2026-10-19; implementations are independent):
https://github.com/tomojitakasu/RTKLIB/blob/master/src/solution.c
https://www.rtklib.com/prog/manual_2.4.2.pdf
https://docs.emlid.com/emlid-studio/static-processing-workflow/
https://docs.emlid.com/emlid-studio/stop-n-go-with-ef-workflow/

NETWORK PROJECT INVENTORY (TAB 5)
Use New, select the documented point CRS and height datum, and enter a name.
The inventory stores more observation types than the first GNSS solver supports;
the explicit GNSS adjustment / quality action validates its narrower profile.
Point CRS options are EUREF89 / UTM zones 31-36; E/N/Z are metres. Coordinates
are preserved when storing/importing. The separate GNSS calculation converts
a snapshot within ETRS89. Point height datum is NN2000, NN54, ELLIP or LOCAL.
Missing CRS/height metadata can be saved explicitly as a draft; Check project
structure reports it as undocumented. Coordinate epoch is recorded only.

Points:
- Unknown / estimate: optional approximate E/N/Z; all blank is allowed.
- Known with uncertainty: E/N/Z and three positive coordinate standard
  deviations in the project E/N/Z axes. The draft stores a diagonal coordinate
  uncertainty model; it is not automatically reinterpreted as geodetic ENU.
- Fixed / held exact: E/N/Z, with a required explanation in Notes. Choosing
  this role holds the point exact in the GNSS model; it is not proof of error-free
  control.
- Independent check only: E/N/Z; optional uncertainty. Enabled observations
  touching a check point must be marked Independent check only, not Adjustment.
  Checks are held out of the fit. The GNSS quality profile reports supported
  held-out vectors; statistical checks also require reference uncertainty.

Sessions: unique ID, start/end with Z or a UTC offset, instrument/receiver,
corrections/base information and Notes. No receiver brand is required. Session
labels and dates alone do not establish measurement independence.

Observation inventory: manual entry of ECEF XYZ GNSS vectors, slope distances
and signed height differences (to minus from), with existing point/session IDs,
intended use and an enabled flag. GNSS vectors require the documented ECEF
frame EPSG:4936 or EPSG:4978, positive sigmas and valid component correlations;
the full symmetric 3x3 covariance is stored in m². Other values/sigmas are metres.
New observations have no assumed sigma value. Do not invent measured baselines
by differencing RTK point coordinates. Import measured baseline is described above.
Instrument/target heights are stored only: blank means undocumented; zero
means a point-to-point value. No height, curvature, grid or geoid reduction is
applied by this inventory editor. No cross-observation/session covariance is
implemented. The separate mixed CSV workflow adds an explicit angle model and
endpoint-height reductions. The GNSS-only solver validates compatibility before use.

Double-click or Edit selected to edit a record. Referenced IDs cannot be renamed
or removed if observations would become orphaned; change dependent records
explicitly first. Disabled observations remain stored and need a reason.
Check project structure validates IDs, links, roles, units and covariance shape;
it does not assess network geometry, redundancy, field quality or solve a network.

Save / Save as write versioned JSON atomically, preserving numeric values and
heights. Original files are never silently rewritten. Unsupported schemas,
unknown fields and duplicate JSON keys are rejected rather than discarded.
External changes to the opened file prevent Save; use a different filename or
open the changed file. The draft limit is 20 MB and 10000 records per collection.
Unsaved drafts are retained locally in QGIS settings, separately per project.
Recover unsaved draft restores records and unfinished metadata, requiring a
new Save as. Normal Close offers Save / Discard / Cancel. Disabling the plugin
retains an unsaved recovery draft. Local QGIS settings contain project data;
they are not included in a release or sent online. A saved file is still needed
for reliable project backup and transfer.

Load synthetic network provides a constructed training inventory with all
four point roles, one session and four stored scalar observations. It is not
field data and is not an adjusted network. The same inventory is included as
examples/network_inventory.sanet.json. This example is separate from the
existing GNSS coordinate examples. Its scalar observations are not accepted by
the first GNSS solver; Load GNSS quality example supplies a compatible network.

CALCULATIONS
1. Tab 1 checks repeated final E/N/Z coordinates. This is an unweighted check
   of spread and offsets from reference points, calculated inside the plugin.
2. Tab 3 runs a separate synthetic GNSS vector network through SALSA 1.17.0
   and compares the final positions with an independent calculation.
3. Tab 4 adjusts repeated absolute coordinate observations through SALSA,
   using explicit ellipsoidal point heights and a documented 1σ model.
   It does not process raw GNSS observations or measured baselines.
4. Tab 5 explicitly adjusts a compatible GNSS vector network, including weighted
   coordinate priors, then reports conditional uncertainty, redundancy, global
   and local tests, and independent held-out checks.

TRY THE REPEATABILITY EXAMPLE
- Open Plugins > Survey Adjustment > Survey Adjustment – GNSS and SALSA.
- Select “Load synthetic GNSS example”. It contains ten synthetic measurements
  of four control points. Control ID is a custom example column for grouping
  repeats, not a guaranteed standard export field from any manufacturer.
- Check UTM 32 and NN2000 and confirm them for these synthetic data.
- Keep the FIX filter and example tolerances: horizontal 0.03 m, vertical 0.05 m.
- Calculate. Expect KP01 WITHIN TOLERANCE, KP02 OUTSIDE TOLERANCE,
  KP03 OUTSIDE TOLERANCE and KP04 INCONCLUSIVE. Nine measurements are used;
  KP03_FLOAT is excluded.
- “Show results on map” creates four separate memory layers: actual XYZ,
  unweighted means, references and residual lines at their actual lengths.
  Means have point labels and status colours and are drawn above observations.
  Centimetre-scale residual lines may require substantial zooming to see.
- “Save report package” writes TXT/HTML, complete JSON, means/residuals CSV
  and SHA256 hashes. Source hashes and field mappings are recorded. Each run
  creates a new folder and does not replace an existing report package.

YOUR GNSS FILES FROM DIFFERENT RECEIVERS
Use CSV, TXT or TSV with column headers. Comma, semicolon and tab delimiters
are supported; decimal commas are supported with semicolon/tab delimiters.
Fields are suggested by column name and can be mapped manually. No receiver
brand is required. Emlid Flow “All columns” is one example, not a requirement.

For tab 1, use projected coordinates in metres, for example Easting, Northing
and Elevation. Map measurement name, control point ID and E/N/Z. Status and
measurement time can also be mapped. With the FIX filter enabled, blank/unknown
status and FLOAT/SINGLE are excluded. Disable it only when you intentionally
want these measurements included; the result will identify that choice.

The same control point ID must identify the same physical point. A shared
feature code such as “manhole” must not group different manholes together.
Name suffixes are not removed and point identities are not guessed.
The reference file must have one row per control point, in the same CRS and
height datum. It is used for comparison, not to shift observations. Reference
point uncertainty is not modelled in this version.

Repeatability supports EUREF89/UTM zones 31–36 (EPSG:25831–25836). No datum,
geoid or coordinate transformation is applied. Explicit conflicting EPSG or
height metadata is rejected; otherwise verify the file settings yourself.
Elevation must already be the point/ground height in the selected datum.
Antenna height is not subtracted again. Local heights use the LOCAL option;
the legacy LOKAL token is accepted by the core reader for compatibility.

FORMATS AND QUALITY FIELDS
The same file type does not guarantee equivalent contents. Check axis order,
metres/feet, decimal degrees/DMS, reference frame, height datum and status codes.
Text FIX, FIXED, RTK FIX and RTK FIXED are recognised. Numeric codes such as
1 or 4 are not guessed as FIX. Document and normalise them in a separate working
copy before import, retaining the original. RMS/HRMS/VRMS and 95% values are
not automatically interpreted as 1σ ENU, regardless of receiver brand.
Headerless point lists (PNEZD), JOB/DBX, proprietary project files and RINEX
are not imported directly. Export a text file with named columns first.
Format support is not certification of every receiver or export configuration.

ADDITIONAL SYNTHETIC EXAMPLES
examples/gnss_gjentak.csv and examples/gnss_salsa_llh.csv contain the same
synthetic observations as the original Emlid-like examples, with alternative
headers, semicolons and decimal commas. They demonstrate receiver-independent
field mapping. They are constructed examples, not approved manufacturer exports.

INTERPRETATION
The repeatability mean is unweighted. Residuals are observation minus mean.
Spread is the maximum horizontal distance/vertical difference from the mean.
Sample standard deviations use n−1. Both individual measurements and the mean
are compared with the reference, so opposing errors cannot cancel into a
misleadingly good mean. A single observation cannot establish repeatability.
Tolerances are user settings, not national standards or recipient requirements.
Outliers are not removed automatically. FIX is not a quality guarantee.
Repeats can be correlated. Common base errors can produce small spread but
incorrect absolute positions. Receiver RMS fields are not used as weights.

SYNTHETIC SALSA VECTOR EXAMPLE (TAB 3)
Select lsasolver and an output parent folder. A new DAT project is created
and SALSA runs in a separate process with its own environment, keeping QGIS
Qt6 and SALSA Qt5 dependencies apart. The example has one fixed reference,
two unknown points, six vectors, WGS84 ECEF XYZ and ellipsoidal heights.
Its covariance is synthetic and known. A direct XYZ mean is an independent
check for this specific linear equal-weight example, not for all SALSA models.
An exit code alone is insufficient: the plugin requires final calculation,
convergence, output files, final positions and agreement within 0.000002 m.
Errors, warnings and failed chi-squared tests remain visible for review.
The missing-geoid warning means local gravity corrections are not applied.
No NN2000 transformation is performed.

YOUR COORDINATE OBSERVATIONS IN SALSA (TAB 4)
Use CSV/TXT/TSV with column headers. Map measurement name, physical control
point ID, latitude, longitude, ellipsoidal point height and solution status.
Latitude/longitude must be decimal degrees and height metres. Measurement
start time is recommended. At least two independent FIX measurements per
control point are required. FLOAT/blank/other statuses remain in the exclusion
list. The profile supports at most 500 coordinate observations per run.

Choose the documented reference frame: ETRS89/GRS80 (EPSG:4937) or WGS84
(EPSG:4979). Enter the measurement epoch as YYYY-MM-DD; the epoch is recorded,
not transformed. Do not assume lat/lon use the projected project's datum.
NN2000/NN54 Elevation must not be used as ellipsoidal height. UTM/NN2000 files
without ellipsoidal height can still be checked on tab 1, not in this profile.
Height must already refer to the point, including any antenna/tilt correction.

Supply three assessed 1σ values in geodetic east/north/up, either common values
or documented sigma columns. Values must be standard deviations in metres,
not variances. RMS fields are not copied automatically into 1σ. Describe the
uncertainty source, correction service/base and antenna/point-height treatment.
Confirm the height and model assumptions. Component and cross-observation
correlations are set to zero in ENU. Common base, datum, centring and environment
errors are not removed. This is not a model for arbitrary correlated RTK epochs.

The model is absolute XYZ(point)=XYZ(observation)+noise. Each ENU covariance
is fully rotated into ECEF. SALSA DEL records use a fixed computational origin
only as a numerical translation, not as a measured baseline from a physical
or error-free base. No measured points are fixed as error-free. Known control
points are checked separately.

The result folder contains the unchanged source.csv, input.dat,
input_metadata.json, SALSA solution.out/solution.bin, console.txt and
run_report.json with SHA256 hashes. adjusted_coordinates.csv contains point
ID, lat/lon/ellipsoidal height, ECEF, reference frame, epoch and assessment.
coordinate_residuals.csv contains residuals for each original source row.
The selected model is compared with an independent weighted XYZ solution
within 0.000002 m. This checks model transfer and numerical calculation,
not whether field assumptions and supplied uncertainties are correct.
Warnings and failed chi-squared tests remain visible. The result layer is
PointZ in the selected geodetic CRS, with ELLIP height and REVIEW REQUIRED.

“Load synthetic coordinate example” provides two points with three FIX repeats
each, unequal 1σ values and one excluded FLOAT observation. You must check
the three confirmations. Scatter is deliberately small relative to the sigmas,
producing a lower-bound chi-squared warning. A completed calculation is not
professional approval of the measurements.

LIMITS AND FURTHER VALIDATION
PPK/static GPS requires the separate reviewed RTKLIB profile. No VRS synthesis,
automatic raw direction-set/face decoding,
cross-observation/session covariance, general datum/epoch transformation or DOV correction
or professional delivery approval is implemented. Baseline import is limited
to the documented processed POS/relative-vector CSV profiles. Actual Emlid
Studio FIX/FLOAT/SINGLE files were tested locally; this does not validate every
exporter variant or field assumptions. Network quality was tested with
constructed full-covariance networks, independent analytic cases and actual
SALSA runs, including gross errors, weak bridges and zero redundancy. Common
errors may remain invisible even with small residuals and an acceptable global
test. Check columns, units, frames/heights, antenna/tilt settings, epochs,
independence and the uncertainty model before practical use.

LICENSING AND DISTRIBUTION
The plugin's own source and synthetic examples are GPL-3.0-only. See LICENSE.
SALSA is Copyright (c) 2015-2024 Applied Research Laboratories, The University
of Texas at Austin (ARL:UT), and is separately distributed under GPL-3.0-only.
QGIS is GPL-2.0-or-later. This plugin is distributed under GPLv3.
The plugin ZIP contains its Python source. A private repository does not remove
GPL obligations when copies are distributed; recipients retain GPL rights.
SALSA binaries, dependencies, installation packages, manuals and logos are not
included. Obtain the engine from the official SALSA project. Any future engine
bundling must address corresponding source, build and third-party notices for
that exact version. No official affiliation or endorsement is claimed.

REFERENCE SOURCES REVIEWED 2026-10-18
https://wwwext.arlut.utexas.edu/salsa/
https://github.com/SGL-UT/SALSA/tree/1.17.0
https://github.com/SGL-UT/SALSA/releases/tag/v1.17.0
https://www.arlut.utexas.edu/salsa/pdfs/SalsaUserManual-6-2025.pdf
https://www.gnu.org/licenses/gpl-3.0.html
https://www.gnu.org/licenses/gpl-faq.html#GPLAndPlugins
https://www.gnu.org/licenses/gpl-faq.html#UnreleasedMods
https://www.qgis.org/license/
https://github.com/qgis/QGIS
https://docs.emlid.com/emlid-flow/export-projects/

PUBLISHING AND PRIVACY
Use RELEASE_TEMPLATE.md for all GitHub releases. See docs/PUBLISHING.md for mandatory source, history, synthetic-fixture and ZIP checks before uploading.

## Support

If this project has been useful to you and you would like to support further testing, documentation, and development, you can buy me a coffee:

[☕ Buy me a coffee](https://buymeacoffee.com/tb_rtk)

Support is completely optional and is greatly appreciated.

## License

See [LICENSE](LICENSE).
