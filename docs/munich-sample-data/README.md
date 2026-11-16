# Anbindung München an CWYM: Bestandsaufnahme und offene Fragen

Stand: 16.11.2026

Chat With Your Map (CWYM) soll im ASCEND-Portal Harthof laufen. Dafür braucht
CWYM vier Datenquellen: Adresssuche, Layer-Index, Verwaltungsgrenzen und die
ASCEND-Gebäudedaten.

Ursprünglich wollten wir dafür Beispieldaten aus München anfragen. Beim Prüfen
hat sich gezeigt, dass alle vier Quellen bereits öffentlich erreichbar sind
(ohne Anmeldung, CORS für alle Ursprünge freigegeben). **Beispiel- oder
Dummy-Daten brauchen wir daher nicht mehr.** Dieses Dokument fasst zusammen,
was wir gefunden haben, und listet die Fragen, die nur München beantworten
kann.

## 1. Adresssuche

- Dienst: `https://geoportal.muenchen.de/search/p1_adressen_4326/_search/template`
- Abfrage wie im ASCEND-Portal: Template `all_features`, Parameter
  `search_term` und `size`.
- Treffertypen (`_source.properties.type`): `Address` (rund 162.000),
  `Street` (rund 6.300), `District` (25 Stadtbezirke).
- Name in `properties.searchfield`, Punkt in `_source.geometry` (WGS84),
  Ausdehnung in `properties.geographicextent`.

Beobachtungen:

- Die Ausdehnung ist bei allen Treffern ein Rechteck, auch bei
  Stadtbezirken. Die echten Grenzen holen wir deshalb aus dem WFS (Abschnitt 3).
- Bezirksteile und Gebietsnamen wie „Harthof“ sind nicht enthalten.
- Zwischen Straße und Hausnummer stehen mehrere Leerzeichen
  (`"Hugo-Wolf-Straße  57"`).
- Der Index wird offenbar täglich neu aufgebaut (`p1_adressen_4326_<Datum>`).

## 2. Layer-Index

- Dienst: `https://geoportal.muenchen.de/search/portal.ascend_harthof/_search/template`
- Abfrage wie im ASCEND-Portal: Template `all_layers`, Parameter
  `query_string`, `typ`, `size`, `fuzziness`.
- 35 Layer. Je Layer: `id`, `name` (oft zweisprachig, z. B.
  „ASCEND Energieeffizienzklasse | building energy efficiency class“),
  `layers`, `url` sowie `datasets` mit `md_id`, `md_name` und einem Verweis
  auf die Metadaten (CSW).
- 32 der 35 IDs stehen in `services-internet.json`. Es fehlen `terrain`,
  `20260623_testdaten_harthof` und `einzelbaum_punkt`.

Beobachtungen:

- Anders als in Hamburg gibt es weder Diensttyp (`typ`) noch Beschreibung oder
  Schlagwörter im Index. Für die Suche nach passenden Ebenen haben wir damit
  nur Name und Metadatenname.

## 3. Verwaltungsgrenzen

WFS `https://geoportal.muenchen.de/geoserver/gsm_wfs/wfs`, GeoJSON in WGS84:

| Feature-Typ | Inhalt | Namensfeld |
|---|---|---|
| `gsm_wfs:vablock_stadtbezirk` | 25 Stadtbezirke (27 Flächen, zwei Bezirke bestehen aus zwei Teilen) | `sb_name`, Nummer `sb_nummer` |
| `gsm_wfs:vablock_bezirksteil` | 110 Bezirksteile | nur Nummer `bt_nummer`, kein Name |
| `gsm_wfs:vablock_viertel` | Stadtbezirksviertel | nicht geprüft |
| `gsm_wfs:ascend_umgriff_projektgebiet` | Projektgebiet ASCEND (1 Fläche) | keine Attribute |

OGC API Features ist auf dem GeoServer nicht freigeschaltet. Wir binden den
WFS direkt an.

## 4. ASCEND-Gebäudedaten

WFS `https://geoportal.muenchen.de/geoserver/gsm_wfs/wfs`, Feature-Typ
`gsm_wfs:ascend_gebaeude_gfi_wfs`: 273 Gebäude mit 24 Attributen, Zahlen
teilweise als echte Zahlen (`baujahr`, `geschosse`), fehlende Werte als `null`.

| Attribut | Werte (Auszug) | Anmerkung |
|---|---|---|
| `gebid` | `000U6WC` | eindeutig |
| `baugruppe` | `HUGO_17` (95 Gruppen) | |
| `adresse` | `Hugo-Wolf-Straße 66` | 261 verschiedene bei 273 Gebäuden |
| `eigentuemer` | Münchner Wohnen, LHM, Kirche | |
| `nutzung` | Wohnen, Schule, Kita, Kirche, Wohnen/Gewerbe, … | |
| `geschosse`, `baujahr` | Zahl | |
| `energieeffizienzklasse` | A+ … H | 38 ohne Wert |
| `sanierungsmassnahme`, `sanierungszustand_23`, `gebäudezustand`, `sanierungsjahr`, `sanierungsmassnahme_jahr` | z. B. „Nicht saniert“, „Abgerissen“, „Neubau“ | Jahre als Text |
| `fernwaerme`, `fernwaerme_23`, `fernwaerme_massnahme`, `fernwaerme_massnahme_jahr` | angeschlossen, nicht angeschlossen, Anschluss geplant, … | |
| `photovoltaik`, `photovoltaik_23`, `photovoltaik_massnahme` | `photovoltaik` mischt Jahreszahlen und Zustände („2026“, „Vorhanden“) | |
| `pv_leistung` | `"57,04"` | Text mit Dezimalkomma, vermutlich kWp |
| `balkon_pv` | 445, 890 | Einheit unklar |
| `sonstiges`, `plz` | | |

## Offene Fragen an München

**Allgemein**

1. Dürfen wir die oben genannten Dienste (Adresssuche, Layer-Index, WFS)
   dauerhaft serverseitig aus CWYM abfragen? Gibt es Nutzungsbedingungen,
   Ratenlimits oder geplante Änderungen (z. B. Template-Namen)?

**Gebäudedaten**

2. Ist `gsm_wfs:ascend_gebaeude_gfi_wfs` der offizielle und dauerhafte WFS
   für die Gebäudedaten, oder ein Zwischenstand? (Laut Mail ist bisher nur
   WMS veröffentlicht.)
3. Der ASCEND-Assistent kennt 235 Gebäude, der WFS liefert 273. Woher kommt
   der Unterschied (z. B. abgerissene Gebäude, Neubauten, Nicht-Wohngebäude)?
   Welche Gebäude sollen bei Statistiken mitzählen?
4. Was bedeuten die Attributgruppen genau?
   - Unterschied zwischen `sanierungszustand_23`, `gebäudezustand` und
     `sanierungsmassnahme`; ebenso `fernwaerme` gegenüber `fernwaerme_23`
     und `photovoltaik` gegenüber `photovoltaik_23`. Steht `_23` für den
     Stand 2023?
   - Warum enthält `photovoltaik` teils Jahreszahlen, teils Zustände?
   - Einheiten von `pv_leistung` (kWp?) und `balkon_pv` (Wp?).
5. Wer pflegt die Werte, und wie oft ändern sie sich?
6. Welche Kartenebene gehört zu welchem Attribut? Unsere Annahme:
   `gsm:ascend_baugruppen_baujahr` → `baujahr`,
   `…_energieeffizienzklasse` → `energieeffizienzklasse`,
   `…_eigentuemer` → `eigentuemer`, `…_nutzung` → `nutzung`,
   `…_sanierung` → ?, `…_fernwaerme` → ?, `…_pv` und `…_pv_stand` → ?

**Grenzen und Orte**

7. Gibt es für die Bezirksteile Namen, oder nur Nummern?
8. „Harthof“ ist weder in der Adresssuche noch als Bezirksteil zu finden.
   Sollen wir für „Harthof“ das ASCEND-Projektgebiet verwenden?

**Layer-Index**

9. Gibt es einen stadtweiten Layer-Index, oder nur einen je Portal?
10. Ist geplant, Beschreibungen oder Schlagwörter in den Index aufzunehmen?

**Betrieb**

11. Welches Sprachmodell bzw. welcher Anbieter ist vorgesehen, und unterstützt
    es OpenAI-kompatibles Tool Calling?
12. Dürfen Nutzerfragen zur Fehleranalyse protokolliert werden, oder nur
    Metadaten?
13. Kann das ASCEND-Portal auf Masterportal 3.27 aktualisiert werden?
