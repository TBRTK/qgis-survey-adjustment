SURVEY ADJUSTMENT 0.2.2 – INDEPENDENT PROTOTYPE FOR QGIS 4
Author: Tore Berthelsen – https://www.3dmapper.no
Date: 2026-10-18
Project: https://github.com/TBRTK/qgis-survey-adjustment

The software is provided “AS IS”, without warranty. Disclaimers and limitations
of liability apply to the extent permitted by GPLv3 sections 15–17 and applicable
law. See LICENSE. This is not an unconditional exclusion of statutory liability.

WHAT CHANGED IN 0.2.2
The plugin interface, help, validation messages, map results and generated
reports are in English. Norwegian source-column aliases remain supported.
New repeatability reports use check.json, check.txt, check.html, means.csv,
residuals.csv and sha256.json. Output status values are WITHIN TOLERANCE,
OUTSIDE TOLERANCE and INCONCLUSIVE. SALSA results retain REVIEW REQUIRED.
Generated layer fields also use English names, for example control_id,
height_datum and assessment. Existing files and layers are not rewritten.

INSTALLATION
In QGIS 4: Plugins > Manage and Install Plugins > Install from ZIP.
Select survey_adjustment-0.2.2.zip and enable the plugin. The Python package
is survey_adjustment. SOSI Survey remains a separate plugin.
Select the separately installed SALSA lsasolver on tab 3. Keep the full SALSA
folder with its own DLLs; a standalone lsasolver.exe is usually insufficient.

This is an independent prototype. It is not made or approved by SALSA, ARL:UT,
Emlid, QGIS, a receiver manufacturer or a Norwegian utility owner.
All calculations run locally. No measurement file is uploaded online.
Native QGIS/operating-system dialogs and number formatting follow the host
application settings; this plugin does not change QGIS's language globally.

CALCULATIONS
1. Tab 1 checks repeated final E/N/Z coordinates. This is an unweighted check
   of spread and offsets from reference points, calculated inside the plugin.
2. Tab 3 runs a separate synthetic GNSS vector network through SALSA 1.17.0
   and compares the final positions with an independent calculation.
3. Tab 4 adjusts repeated absolute coordinate observations through SALSA,
   using explicit ellipsoidal point heights and a documented 1σ model.
   It does not process raw GNSS observations or measured baselines.

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
No RINEX processing, measured-baseline import, full network adjustment with
cross-covariance, or geoid/NN2000/datum/epoch transformation is implemented.
Real anonymised receiver exports have not yet been validated. Before field use,
check your columns, units, reference/height datum, corrections, antenna/tilt
settings, times and how 1σ is determined. This is not delivery-approved software.

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
