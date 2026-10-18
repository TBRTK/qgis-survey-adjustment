SURVEY ADJUSTMENT 0.1.1 – SELVSTENDIG UTKAST FOR QGIS 4.0
Forfatter: Tore Berthelsen – https://www.3dmapper.no
Dato: 18.10.2026
Prosjektside: https://github.com/TBRTK/qgis-survey-adjustment

Programvaren leveres «som den er» (AS IS), uten garanti. Ansvarsfraskrivelse
og ansvarsbegrensning gjelder i det omfang GPLv3 §§15–17 og gjeldende rett
tillater. Se LICENSE. Den kan ikke omtales som en ubetinget fritakelse fra
alle former for lovpålagt ansvar.

INSTALLASJON
QGIS 4.0: Programtillegg > Håndtering og installering av programtillegg >
Installer fra ZIP. Velg survey_adjustment-0.1.1.zip og aktiver tillegget.
Python-pakken heter survey_adjustment; SOSI Survey er en separat plugin.
SALSA må velges separat på fane 3. Hele SALSA-mappen med motorens egne
DLL-er må beholdes; en løs lsasolver.exe er normalt ikke tilstrekkelig.

Dette er en uavhengig prototype. Den har ingen kodeavhengighet til SOSI Survey,
og endrer ikke SOSI-pluginen. Den er ikke laget eller godkjent av SALSA,
ARL:UT, Emlid, QGIS eller en norsk netteier.

TO SEPARATE BEREGNINGER
1. Gjentakskontroll av ferdige E/N/Z-koordinater fra CSV. Den er uvektet og
   kjøres i pluginen. Den dokumenterer spredning og avvik mot referansepunkter.
2. En programkobling til SALSA 1.17.0. Førsteutkastet kjører et eget syntetisk
   GNSS-vektornett, og kontrollerer sluttposisjonene mot en uavhengig beregning.
   Egen måle-CSV fra fane 1 sendes ikke til SALSA. Den blir ikke omgjort til
   GNSS-baselinjer. Import av egne observasjonsnett er senere arbeid.

SLIK PRØVER DU EMLID-EKSEMPLET
- Åpne Programtillegg > Survey Adjustment > Survey Adjustment – GNSS og SALSA.
- Velg «Last Emlid-eksempel». Eksemplet inneholder 10 syntetiske målinger,
  fordelt på fire kontrollpunkter. «Control ID» er en egen eksempelkolonne
  for å gruppere gjentak; den er ikke lovet som standardkolonne fra Emlid.
- Kontroller UTM 32 og NN2000 og kryss av bekreftelsen for eksempeldataene.
- Behold FIX-filteret og demogrensene 0.03 m i plan / 0.05 m i høyde.
- Beregn. Forvent KP01 INNENFOR, KP02 AVVIK, KP03 AVVIK, KP04 UAVKLART.
  KP03_FLOAT skal listes som utelatt. Ni av ti målinger brukes.
- «Vis resultater i kart» lager fire nye minnelag: faktisk XYZ, uvektet
  middel, referansepunkter og restavvikslinjer med faktisk lengde. Laget med
  middelverdier får kontrollpunktnavn og farger. Restavvik på centimeternivå
  kan være for små til å sees uten kraftig innzooming.
  Middelverdilaget tegnes øverst slik at samlet kontrollstatus er synlig.
  Uavklarte gjentak merkes også uavklart på enkeltmålinger uten påvist avvik.
- Lagre rapportpakken. Den inneholder lesbar TXT/HTML, komplett JSON,
  middelverdier/restavvik som CSV og SHA256 for rapportfilene. Kildenes SHA256
  og de valgte feltkoblingene følger kontrollrapporten. Eksisterende filer
  erstattes ikke; hver kjøring får en ny mappe.

EGNE EMLID-FILER
Eksporter CSV med kolonnenavn fra Emlid Flow. Bruk prosjekterte koordinater
i meter, for eksempel Easting, Northing og Elevation. Navn, kontrollpunkt-ID,
E/N/Z må kobles. Status og måletid kan kobles. Når FIX-filteret er på,
utelates tom/ukjent status samt FLOAT/SINGLE. Slå av filteret bare når du
bevisst ønsker at disse målingene skal inngå; dette merkes i resultatet.

Velg en kolonne som entydig sier hvilket fysisk punkt hver måling gjelder.
Flere målinger av samme punkt skal ha samme kontrollpunkt-ID. Felles Code
som «kum» grupperer ellers ulike kummer sammen og må ikke brukes slik.
Pluginen kutter ikke navnesuffiks og gjetter ikke hvilke punkter som hører sammen.

Referansefilen skal ha én rad per kontrollpunkt, i samme CRS og høydegrunnlag.
Den brukes til sammenligning, ikke til å flytte eller «rette» observasjonene.
Referansepunktenes egen usikkerhet er ikke modellert i denne versjonen.
Koordinatsystemer i førsteutkastet: EUREF89/UTM 31–36 (EPSG:25831–25836).
Ingen geoid-, datum- eller koordinattransformasjon skjer i gjentakskontrollen.
CSV med eksplisitt annet EPSG/datum stoppes. Ellers må brukeren kontrollere
de faktiske filinnstillingene og bekrefte at filene er sammenlignbare.
Elevation må allerede være punkt-/bakkehøyde. Antennehøyde korrigeres ikke igjen.

TOLKNING
Middelverdien er et uvektet gjennomsnitt. Restavvik er måling minus middel.
Spredning kontrolleres som største planavstand / høydeforskjell fra middel.
Utvalgsstandardavvik beregnes med n−1. Både middel og hver enkelt måling
sammenlignes med referansen, slik at motsatte feil ikke kan kansellere seg
til et tilsynelatende godt middel. En enkeltmåling kan ikke dokumentere gjentak.
Grensene er brukerens valg; de er ikke norske standardkrav eller mottakerkrav.
Ingen automatisk uteliggerfjerning. FIX er ikke en kvalitetsgaranti.
Gjentak kan være korrelerte. Felles basefeil kan gi liten spredning og feil
absolutt posisjon. Instrumentets RMS-felt brukes ikke som standardavvik/vekter.
Ingen konfidensregion, RINEX-prosessering eller GNSS-baselineberegning tilbys.

SALSA-EKSEMPLET
Velg den separat tilgjengelige SALSA-motoren lsasolver.exe på fane 3 og en
resultatmappe. Pluginen oppretter et nytt DAT-prosjekt og starter SALSA i
egen prosess med eget miljø, slik at QGIS' Qt6 ikke blandes med SALSAs Qt5.
Eksemplet har ett fast referansepunkt, to ukjente punkter, seks vektorer,
WGS84 ECEF XYZ og ellipsoidiske høyder. Kovariansen er syntetisk og kjent.
SALSA-resultatet er en reell SALSA-beregning. Den sjekkes mot direkte
XYZ-middel, som er korrekt for akkurat dette lineære eksemplet med like vekter.
Dette er ikke en generell uavhengig kontroll av alle SALSAs beregningstyper.
Prosesskode alene gir ikke bestått. Pluginen krever sluttberegning,
konvergens, resultatfil, sluttposisjoner og samsvar innen 0.000002 m.
Feil/varsler og Chi-kvadrat-avvik vises; filene beholdes for vurdering.
Geoidvarselet i dette eksemplet skyldes at ingen geoidfil er valgt. Det betyr
at tyngdekorreksjoner ikke brukes. Det gjøres ingen NN2000-transformasjon.
Ingen rå Emlid-målefil lastes opp på nettet. All beregning er lokal.

RETTIGHETER OG GITHUB
Pluginens egen kildekode er GPL-3.0-only. Lisensen følger som LICENSE.
SALSA er Copyright (c) 2015-2024 Applied Research Laboratories,
The University of Texas at Austin (ARL:UT), og er GPL-3.0-only.
Prosjektet tillater bruk, endring og videreformidling under GPLv3.
QGIS er GPL-2.0-or-later; GPLv3 er et kompatibelt valg for dette tillegget.

Vurderingen er at egen plugin-kildekode kan publiseres på GitHub, offentlig
eller privat, med medfølgende lisens, kildekode og korrekte merknader.
Privat repo opphever ikke GPL-plikter ved videreformidling til andre.
Mottakere beholder GPL-rettighetene, også retten til videreformidling.
Ved publisering av Python-pluginen følger hele dens kildekode med ZIP-filen.
SALSA-programmer, DLL-er, installasjonspakke, logoer og manual distribueres
ikke i pluginpakken. Brukeren henter motoren fra offisielt SALSA-prosjekt.
Dette unngår at pluginpakken også må distribuere motorens korresponderende
kildekode, byggesystem og tredjeparts lisensfiler. Hvis motoren senere bundles,
må dette vurderes og gjennomføres etter GPLv3 §6 for den konkrete utgaven.
Det brukes ikke SALSA/Emlid-logo, og ingen offisiell tilknytning hevdes.
Navn brukes beskrivende for kompatibilitet. Det er ikke gjort et fullstendig
varemerkesøk, og vurderingen gir ingen garanti for alle mulige navnerettigheter.

KILDER KONTROLLERT 18.10.2026
SALSA prosjekt og lisens:
https://wwwext.arlut.utexas.edu/salsa/
SALSA kildekode/lisensmerknader og offisiell 1.17.0-utgave:
https://github.com/SGL-UT/SALSA/tree/1.17.0
https://github.com/SGL-UT/SALSA/releases/tag/v1.17.0
https://www.arlut.utexas.edu/salsa/pdfs/SalsaUserManual-6-2025.pdf
GPLv3 tekst og plikter ved videreformidling:
https://www.gnu.org/licenses/gpl-3.0.html
https://www.gnu.org/licenses/gpl-faq.html#GPLAndPlugins
https://www.gnu.org/licenses/gpl-faq.html#UnreleasedMods
QGIS-lisens:
https://www.qgis.org/license/
https://github.com/qgis/QGIS
Emlid CSV/prosjektformat og kvalitetskolonner:
https://docs.emlid.com/emlid-flow/setting-up-team-and-projects/creating-project/
https://docs.emlid.com/emlid-flow/ef-changelogs/android-changelogs/

GJENSTÅENDE ARBEID
Reelle anonymiserte Emlid-filer må testes mot eksportformatet du bruker.
Egne SALSA-observasjonsnett, full kovarians, korrelasjoner, fastmerkeusikkerhet,
geoid/datum/epoke og eventuelle rå GNSS-baselinjer må spesifiseres før tillegget
kan brukes til faglig nettutjevning. Det er ikke leveransegodkjent programvare.
