# Beispieldaten für die Anbindung München (CWYM)

Dieses Verzeichnis zeigt, welche Daten wir von der Landeshauptstadt München
benötigen, um Chat With Your Map (CWYM) an das Geoportal München und das
ASCEND-Projektgebiet Harthof anzubinden.

## Das Wichtigste vorab

**Die Werte dürfen erfunden sein, die Struktur nicht.**

Die Dateien in diesem Ordner sind Beispiele in der Form, wie sie heute aus den
Hamburger Diensten kommen, nur mit Münchner Werten gefüllt. Sie dienen zur
Orientierung, nicht als Vorlage zum Nachbauen.

Für uns zählt, wie die Antworten Ihrer Dienste **tatsächlich** aussehen:
Feldnamen, Verschachtelung, Koordinatensystem, Datentypen, leere Werte. Genau
daran passen wir unsere Anbindung an. Bitte schicken Sie uns daher:

- je Dienst **5–10 Treffer als unveränderte JSON-Antwort**, so wie der Dienst
  sie liefert (Inhalte dürfen gekürzt oder anonymisiert sein, Feldnamen und
  Aufbau bitte nicht ändern);
- dazu die **Anfrage**, mit der die Antwort erzeugt wurde (URL, Parameter, ggf.
  Template-ID und Request-Body).

Diese Antworten verwenden wir außerdem als Testdaten, damit die Anbindung ohne
Live-Zugriff auf Ihre Dienste getestet werden kann.

## Übersicht

| Nr. | Datei | Dienst | Wofür CWYM ihn braucht |
|---|---|---|---|
| 1 | `01_adresssuche_beispiel.json` | Adresssuche (`p1_adressen_4326`) | Orte, Straßen und Adressen finden („Zoom zum Harthof“) |
| 2 | `02_layerindex_beispiel.json` | Layer-Index (`portal.ascend_harthof`) | passende Kartenebenen zu einer Frage finden |
| 3 | `03_stadtbezirke_beispiel.geojson` | Verwaltungsgrenzen | Filter „in Milbertshofen-Am Hart“ mit der amtlichen Grenze |
| 4 | `04_gebaeude_beispiel.geojson` | Gebäudedaten (`gsm:ascend_gebaeude_gfi`) | Statistiken, Top-N, Adressabfrage zu den Gebäuden |

## 1. Adresssuche (`p1_adressen_4326`)

Bitte drei Antworten schicken: eine Suche nach einer **vollständigen Adresse**
(z. B. „Hugo-Wolf-Straße 57“), nach einer **Straße** und nach einem
**Gebietsnamen** (z. B. „Harthof“ oder „Milbertshofen-Am Hart“).

Das Beispiel enthält der Kürze halber je einen Treffer für Adresse, Straße und
Stadtbezirk in einer Antwort. Bitte schicken Sie stattdessen drei getrennte
Antworten mit allen Treffern, die der Dienst jeweils liefert. Aus jedem Treffer
lesen wir:

| Angabe | Feld im Beispiel | Frage an München |
|---|---|---|
| Anzeigename | `_source.properties.searchfield` | Welches Feld enthält den Namen? |
| Art des Treffers | `_source.properties.type` | Welche Werte gibt es (Adresse, Straße, Stadtbezirk, Bezirksteil, …)? |
| Koordinate | `_source.geometry` (Punkt) | Welches Koordinatensystem? Wir erwarten WGS84 (EPSG:4326), Reihenfolge Länge, Breite. |
| Gebietsumriss (optional) | `_source.properties.geographicextent` | Gibt es für Gebiete einen Umriss oder ein Rechteck? |
| Relevanz | `_score` | – |

Weitere Fragen:

- Welche Search-Template-ID und welche Parameter werden verwendet? Oder ist
  eine normale `_search`-Anfrage mit Query-Body erlaubt?
- Darf ein Server (unser Backend) den Dienst direkt abfragen? Gibt es
  IP-Freigaben, CORS-Einschränkungen oder Ratenlimits?

## 2. Layer-Index (`portal.ascend_harthof`)

Bitte eine Antwort auf eine Suche nach z. B. „Energie“ oder „Gebäude“ schicken,
idealerweise auch einmal den **vollständigen Index** (alle Layer eines Portals).

Je Layer lesen wir:

| Angabe | Feld im Beispiel |
|---|---|
| Layer-ID | `_source.id` |
| Name | `_source.name` |
| Diensttyp (WMS, WFS, OAF, …) | `_source.typ` |
| Dienst-URL | `_source.url` |
| Beschreibung | `_source.datasets[0].description` |
| Schlagwörter | `_source.datasets[0].keywords` |

Fragen:

- Stimmen die Layer-IDs mit den IDs in `services-internet.json` überein? Das
  ist Voraussetzung dafür, dass CWYM die Ebenen im Masterportal schalten kann.
- Gibt es einen stadtweiten Index oder nur einen je Portal?

## 3. Verwaltungsgrenzen

Für Fragen wie „Wie viele Gebäude in Milbertshofen-Am Hart?“ brauchen wir die
amtlichen Umrisse der Gebiete. Am liebsten als **OGC API Features** oder
**WFS**, sonst als GeoJSON-Datei:

- eine Sammlung je Gebietsebene (Stadtbezirke, Bezirksteile);
- ein Feature je Gebiet, mit einem Feld für den **Namen** (im Beispiel
  `bezirk_name`), so geschrieben wie in der Adresssuche;
- Koordinaten in WGS84 (EPSG:4326).

Für das Projektgebiet bitte zusätzlich `ascend_umgriff_projektgebiet` als
GeoJSON.

## 4. Gebäudedaten (`gsm:ascend_gebaeude_gfi`)

Am besten als **WFS oder OGC API Features**. Falls das nicht möglich ist,
genügt ein **GeoJSON-Export** aller Gebäude (FeatureCollection, ein Feature je
Gebäude, Koordinaten in WGS84). Werte dürfen als Text vorliegen, wie heute
im ASCEND-Assistenten (`"baujahr": "1962"`).

Neben den Daten brauchen wir eine **Attributliste**. Je Attribut:

| Attribut | Bedeutung | Einheit | Art | Reihenfolge / Werte |
|---|---|---|---|---|
| `gebid` | eindeutige Gebäude-ID | – | ID | – |
| `adresse` | Adresse | – | Text | – |
| `eigentuemer` | Eigentümer | – | Kategorie | z. B. Münchner Wohnen, privat, … |
| `baujahr` | Baujahr | Jahr | Zahl | – |
| `geschosse` | Anzahl Geschosse | – | Zahl | – |
| `energieeffizienzklasse` | Energieeffizienzklasse | – | geordnete Kategorie | A+, A, B, C, D, E, F, G, H |
| `saniert` | Gebäude saniert | – | Kategorie | ja, nein |
| `fernwaerme` | Fernwärmeanschluss | – | Kategorie | ja, nein |
| `pv_leistung` | installierte PV-Leistung | kWp | Zahl | – |

(Die Tabelle ist ein Beispiel. Bitte mit den echten Attributen ausfüllen.)

Zur „Art“:

- **Zahl:** man kann damit rechnen (Durchschnitt, Minimum, Maximum, Summe).
- **Kategorie:** man kann zählen und eine Verteilung zeigen.
- **Geordnete Kategorie:** wie Kategorie, aber mit fester Reihenfolge, z. B.
  für „die 5 Gebäude mit der schlechtesten Energieklasse“.

Weitere Fragen:

- Wie werden **fehlende Werte** dargestellt: leerer Text `""`, `null`, ein
  Platzhalter wie `"k. A."` oder fehlt das Feld ganz? (Im Beispiel kommen alle
  Varianten vor; bitte angeben, welche bei Ihnen gilt.)
- Welches Dezimaltrennzeichen wird verwendet (`12,5` oder `12.5`)?
- Wer pflegt die Werte, und wie oft ändern sie sich? Ist eine Datei, die von
  Hand aktualisiert wird, für den Anfang in Ordnung?
- Welche Kartenebene gehört zu welchem Attribut (z. B.
  `gsm:ascend_baugruppen_energieeffizienzklasse` für die Energieklasse)? Dann
  kann CWYM zu einer Statistik gleich die passende Einfärbung einschalten.

## Sonstige Fragen

- **Sprachmodell:** Welcher Anbieter und welches Modell sind vorgesehen?
  Unterstützt es OpenAI-kompatibles Tool Calling?
- **Protokollierung:** Dürfen Nutzerfragen in Traces gespeichert werden, oder
  nur Metadaten?
- **Masterportal:** Kann das ASCEND-Portal auf Masterportal 3.27 aktualisiert
  werden?
