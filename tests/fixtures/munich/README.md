# Munich fixtures

Recorded 2026-11-16 from the public endpoints with curl; structure untouched
(crs member, double spaces in `searchfield`, `_score`), only trimmed.

```
A=https://geoportal.muenchen.de/search/p1_adressen_4326/_search/template
curl -sG "$A" --data-urlencode 'source={"id":"all_features","params":{"search_term":"Hugo-Wolf-Straße 57","size":20}}' --data-urlencode source_content_type=application/json   # addr_hugo_wolf_57.json
# same with search_term "Milbertshofen-Am Hart" (addr_milbertshofen.json), "Hugo-Wolf-Straße" (addr_hugo_wolf_street.json, trimmed to 4 hits), "Harthof" (addr_harthof.json, 0 hits)
curl -s 'https://geoportal.muenchen.de/search/portal.ascend_harthof/_search?q=*:*&size=1000'   # layers_all.json (35 hits)
G=https://geoportal.muenchen.de/geoserver/gsm_wfs/wfs
curl -s "$G?service=WFS&version=2.0.0&request=GetFeature&typeNames=gsm_wfs:vablock_stadtbezirk&outputFormat=application/json&srsName=EPSG:4326"            # wfs_stadtbezirk.json
curl -s "$G?service=WFS&version=2.0.0&request=GetFeature&typeNames=gsm_wfs:ascend_umgriff_projektgebiet&outputFormat=application/json&srsName=EPSG:4326"  # wfs_projektgebiet.json
curl -s "$G?SERVICE=WFS&VERSION=2.0.0&REQUEST=DescribeFeatureType&TYPENAMES=vablock_bezirksteil"                                                         # wfs_bezirksteil_describe.xml
```

`wfs_bezirksteil_sample.json`: POST to `$G` with the body of
`wfs_filter.build_get_feature("vablock_bezirksteil", "gsm_wfs", "", 5)`.
GeoServer rejects `outputFormat="application/geo+json"` (HTTP 400), so the
fixture was fetched with `application/json` (what the client's fallback sends).

All WFS geometries were simplified once with shapely `simplify(0.0005)`.
`EPSG:4326` returns lon/lat order (lon ~11.5, lat ~48.1) - no CRS84 needed.
`wfs_stadtbezirk.json` has 27 features: "Untergiesing-Harlaching" and
"Thalkirchen-Obersendling-Forstenried-Fürstenried-Solln" each have two.
