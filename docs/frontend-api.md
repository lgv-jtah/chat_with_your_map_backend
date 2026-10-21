# CWYM-Agent: Frontend API

How a frontend talks to the CWYM-Agent backend: what you send, what
streams back, and how to execute the map commands inside that stream.

The Swagger UI (`/docs`) only covers the **request body** of
`POST /api/query`. The response is a Server-Sent Events stream, which
OpenAPI cannot describe, so this document covers the rest:

1. [Overview](#1-overview)
2. [Request](#2-request-post-apiquery)
3. [Response: the AG-UI event stream](#3-response-the-ag-ui-event-stream)
4. [MapCommand protocol](#4-mapcommand-protocol)
5. [Conversation state round trip](#5-conversation-state-round-trip)
6. [Example 1: "Zeige mir Fahrradstationen in Wandsbek"](#6-example-1-zeige-mir-fahrradstationen-in-wandsbek)
7. [Example 2: follow-up "Zeig wieder alle Fahrradstationen"](#7-example-2-follow-up-zeig-wieder-alle-fahrradstationen)
8. [Errors](#8-errors)

Source of truth for every type below: `src/cwym_agent/map_commands.py`
(MapCommands), `src/cwym_agent/geodata/conversation_context.py` (request
`state`), `src/cwym_agent/bridge.py` (which events are emitted). The
reference client is the Masterportal addon
`masterportal/addons/chatWithYourMap/` (`utils/buildRunInput.js`,
`utils/aguiClient.js`, `components/ChatWithYourMap.vue`).

---

## 1. Overview

```
 Frontend                                   CWYM-Agent backend
 ────────                                   ──────────────────
 POST /api/query  (RunAgentInput JSON) ───▶
                                            plan (LLM) → sub-agents → command_builder → text
                 ◀─── SSE: RUN_STARTED
                 ◀─── SSE: REASONING_*                (planner's thinking, optional to show)
                 ◀─── SSE: TOOL_CALL_* × n            (sub-agents: progress/status)
                 ◀─── SSE: TOOL_CALL_* × m            (toolCallName = "command_builder"
                                                        → MapCommand: EXECUTE ON THE MAP)
                 ◀─── SSE: TEXT_MESSAGE_*             (chat answer, streamed in chunks)
                 ◀─── SSE: RUN_FINISHED | RUN_ERROR
```

Things to know:

- **The backend is stateless.** It stores nothing between requests. Each
  request carries the recent chat history and the current map state,
  and follow-up questions are resolved only from those (see
  [section 5](#5-conversation-state-round-trip)).
- **One request = one run = one SSE stream.** The stream closes after
  `RUN_FINISHED` or `RUN_ERROR`.
- **Map actions arrive as MapCommands.** A MapCommand is the JSON
  `content` of a `TOOL_CALL_RESULT` whose tool call was opened with
  `toolCallName: "command_builder"`. Execute each one as soon as it
  arrives, in stream order.
- **Coordinates are always WGS84 (EPSG:4326)**, `[lon, lat]`. The frontend
  transforms them to the map projection (EPSG:25832 in Masterportal).
- **All field names on the wire are camelCase**, except the keys inside
  `forwardedProps.portal_context` (snake_case, see below) and the content
  of sub-agent `TOOL_CALL_RESULT`s (internal, snake_case, not a contract).
- **Runs are slow.** A first query takes from a few seconds up to about
  2–3 minutes (LLM planning plus geodata queries; a UDP analysis takes
  1–3 minutes on its own). Show progress from the `TOOL_CALL_START` events.

---

## 2. Request: `POST /api/query`

`Content-Type: application/json`. The body is a standard
[AG-UI `RunAgentInput`](https://docs.ag-ui.com/sdk/js/core/types#runagentinput).

```json
{
  "threadId": "3f6c2a1e-0b7d-4c55-9a51-2f1d8e6b7c90",
  "runId": "a1b2c3d4-0001-4000-8000-000000000001",
  "messages": [
    {"id": "m-1", "role": "user", "content": "Zeige mir Fahrradstationen in Wandsbek"}
  ],
  "state": {
    "activeLayerIds": [],
    "filters": [],
    "lastLocation": null
  },
  "tools": [],
  "context": [],
  "forwardedProps": {
    "portal_context": {
      "layers": [
        {"id": "453", "name": "Geobasiskarten (farbig)", "typ": "WMS", "url": "https://geodienste.hamburg.de/HH_WMS_Geobasiskarten", "collection": null},
        {"id": "27764", "name": "StadtRAD-Stationen Hamburg", "typ": "WFS", "url": "https://geodienste.hamburg.de/HH_WFS_Stadtrad", "collection": null}
      ],
      "languages": ["de", "en"]
    }
  }
}
```

| Field | Required | Meaning |
|---|---|---|
| `threadId` | yes | Stable for one chat session. Create it once when the chat opens, and create a new one on "new chat". Only used for correlating logs; the backend stores nothing under it. |
| `runId` | yes | New UUID per request. Echoed back in `RUN_STARTED`/`RUN_FINISHED`. |
| `messages` | yes | Chat history, oldest first. **The last entry must be the current user query** (`role: "user"`, non-empty), otherwise the request fails with 400. Every message needs an `id` (any string). Only `user`/`assistant` roles are used as history; the backend keeps the last 3 turns (6 messages) and cuts each to 500 characters. |
| `state` | no | Current map state, see table below. Omit or send `{}` on the first turn. |
| `tools`, `context` | yes (AG-UI) | Send `[]`. Not used. |
| `forwardedProps` | yes (AG-UI) | Send at least `{}`. |
| `forwardedProps.portal_context` | no, but strongly recommended | Which layers this portal offers. Layer search is then limited to them, and `state` layer ids are checked against them. **Without it, layer search runs against all ~6,500 Hamburg layers, and all `state` layer ids/filters are dropped.** Use exactly one of two modes, see below; mixing them, an empty `layers` list, or only one URL fails with 400. |

### `portal_context` modes

**Inline (preferred): `layers`.** The frontend sends the portal's own
layer list - in the Masterportal the root store getter `allLayerConfigs`
(services.json entries with the config.json overrides already applied),
folders skipped. The backend fetches nothing, so this works no matter
where the portal runs - including a portal on a developer's `localhost`
talking to the shared backend server.

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | Layer id as the map knows it (string; numbers are accepted and stringified), max 200 chars. This is the id `ACTIVATE_LAYER` etc. will use. |
| `name` | no | Display name, used in replies. Falls back to the id. Max 500 chars. |
| `typ` | no | Service type (`WMS`, `WFS`, `OAF`, …). Max 50 chars. |
| `url` | no | Service URL. FilterAgent needs it (with `typ: "OAF"` and `collection`) to filter a layer. Max 2000 chars. |
| `collection` | no | OGC API Features collection. Max 500 chars. |

Other fields are ignored. At most 2000 entries
(`CWYM_PORTAL_CONTEXT_MAX_LAYERS`); duplicate ids: the last one wins.

**URL (fallback): `config_json_url` + `services_json_url`.** The backend
fetches both itself and takes the layer ids from config.json's
`layerConfig.baselayer`/`subjectlayer`. Only works if the backend can
reach those URLs (not the case for a portal on someone's `localhost`
and a backend on a server). A fetch failure is **not** an error: it is
logged on the backend and the request runs unscoped.

```json
"portal_context": {
  "config_json_url": "https://portal.example/config.json",
  "services_json_url": "https://portal.example/resources/services-internet.json",
  "languages": ["de"]
}
```

### `state`

| Field | Type | Meaning |
|---|---|---|
| `activeLayerIds` | `string[]` (max 50) | Thematic layer ids currently visible on the map. Read them **live from the map** on every request, because the user can toggle layers by hand. |
| `filters` | `{layerId, filterSpec}[]` (max 10) | One entry per active `APPLY_FILTER` overlay: the `filterSpec` exactly as it arrived in that command. Echo it back unchanged; never construct it yourself. |
| `lastLocation` | `{name, kind}` or `null` | The last place shown, taken from `DRAW_BOUNDARY` or a single-location `NAVIGATE_TO_LOCATION` (only when it has a `kind`). `kind` ∈ `bezirk`, `stadtteil`, `address`. Lets "was gibt es dort?" work. |

The backend treats `state` as untrusted input: unknown layer ids, filters
on unknown layers, and filter operators it doesn't know are silently
dropped. Only exceeding the size limits produces a 400.

---

## 3. Response: the AG-UI event stream

`200 OK`, `Content-Type: text/event-stream`. Each event is one SSE frame:
a `data: <json>` line followed by a blank line. Native `EventSource`
cannot POST, so read the body with `fetch` plus a stream reader and split
on `\n\n` (see `utils/aguiClient.js`, or use the official
`@ag-ui/client`).

```
data: {"type":"RUN_STARTED","threadId":"3f6c…","runId":"a1b2…"}

data: {"type":"REASONING_START","messageId":"064d…"}

```

### Events the backend emits

| Event | Fields | What to do |
|---|---|---|
| `RUN_STARTED` | `threadId`, `runId` | Run began. Show a busy state. |
| `REASONING_START` / `REASONING_MESSAGE_CONTENT` / `REASONING_END` | `messageId`, `delta` | The planner LLM's internal reasoning (English, sent as one block). Debug view only; don't show it to end users. |
| `TOOL_CALL_START` | `toolCallId`, `toolCallName` | A step started. **Remember `toolCallName` by `toolCallId`**, because `TOOL_CALL_RESULT` doesn't repeat the name. |
| `TOOL_CALL_ARGS` | `toolCallId`, `delta` | JSON string of the step's arguments, always sent in one piece. |
| `TOOL_CALL_END` | `toolCallId` | Arguments complete (the step itself may still be running). |
| `TOOL_CALL_RESULT` | `toolCallId`, `messageId`, `content` | The step's result as a **JSON string**. If the step is `command_builder`, `content` is a MapCommand: `JSON.parse` it and execute it. |
| `TEXT_MESSAGE_START` | `messageId`, `role: "assistant"` | Chat answer begins. |
| `TEXT_MESSAGE_CONTENT` | `messageId`, `delta` | Append `delta` to the answer. The first delta is often `"\n\n"`; trim it. The answer is German Markdown. |
| `TEXT_MESSAGE_END` | `messageId` | Answer complete. |
| `RUN_FINISHED` | `threadId`, `runId` | Success; the stream ends. |
| `RUN_ERROR` | `message` | The pipeline failed; the stream ends. HTTP status is still 200. Show an error message. |

Order within a run: `RUN_STARTED`, then reasoning, then sub-agent tool
calls, then all `command_builder` tool calls, then the text message, then
`RUN_FINISHED`. So by the time the chat text starts, all MapCommands have
already arrived.

### `toolCallName` values

| `toolCallName` | Meaning | Suggested status text (from `utils/toolStatusMessages.js`) |
|---|---|---|
| `location_extractor` | Resolve place names (address search) | Suche Gebiet... |
| `layer_resolver` | Find the matching map layer | Suche passende Kartenebene... |
| `action_detector` | Classify the intended action | Bestimme gewünschte Aktion... |
| `filter_agent` | Query matching features (OGC API Features) | Filtere Daten... |
| `udp_analysis` | Urban Data Platform analysis. **The result arrives 1–3 min after `TOOL_CALL_END`.** | Analysiere Daten der Urban Data Platform (dauert 1–3 Minuten)... |
| `command_builder` | **A MapCommand.** Execute `TOOL_CALL_RESULT.content`. | (no status) |

The `content` of the sub-agent results (all except `command_builder`) is
internal and may change. You can show or log it, but don't build logic
on it.

For `command_builder`, `toolCallId` equals the MapCommand's `commandId`.

---

## 4. MapCommand protocol

Every MapCommand has the same envelope:

```json
{
  "commandId": "3f70f268-b5fa-49d1-9a9e-19393f14b926",
  "type": "APPLY_FILTER",
  "payload": { ... }
}
```

`type` is a closed enum; dispatch on it. Each command can be executed on
its own, and commands are always meant to be executed in the order they
arrive. Log and ignore unknown types; don't fail on them.

| `type` | Sent today? | Purpose |
|---|---|---|
| [`ACTIVATE_LAYER`](#activate_layer) | yes | Make portal layers visible |
| [`DEACTIVATE_LAYER`](#deactivate_layer) | yes | Hide portal layers |
| [`NAVIGATE_TO_LOCATION`](#navigate_to_location) | yes | Center the map on one or more places, with markers |
| [`ZOOM_TO_EXTENT`](#zoom_to_extent) | yes | Fit the view to a bounding box |
| [`DRAW_BOUNDARY`](#draw_boundary) | yes | Draw an area outline (e.g. a district) |
| [`APPLY_FILTER`](#apply_filter) | yes | Show only matching features of a layer, as an overlay |
| [`CLEAR_FILTER`](#clear_filter) | yes | Remove a filter overlay |
| [`ADD_RESULT_LAYER`](#add_result_layer) | yes | Add one layer of a UDP analysis result |
| [`CLEAR_RESULT_LAYERS`](#clear_result_layers) | yes | Remove all UDP result layers |
| [`OPEN_TOOL`](#open_tool) | yes | Open a portal menu tool (print, legend, contact, …) |
| [`HIGHLIGHT_FEATURE`](#highlight_feature) | not yet | Highlight one feature |
| [`CLEAR_HIGHLIGHTS`](#clear_highlights) | not yet | Remove highlights |
| [`SET_LANGUAGE`](#set_language) | not yet | Switch the UI language |

"Not yet" types are part of the schema so a dispatcher can handle all of
them, but no current backend code path sends them.

### Typical sequences

| User intent | Commands, in order |
|---|---|
| Show a layer at a place ("Zeige … in Altona") | (`DEACTIVATE_LAYER`), `ACTIVATE_LAYER`, `DRAW_BOUNDARY`, `NAVIGATE_TO_LOCATION`, `ZOOM_TO_EXTENT`. Each one appears only if it applies; e.g. no `DRAW_BOUNDARY` for a street address without an area. |
| Filter / spatial restriction ("Fahrradstationen in Wandsbek", "nur Gymnasien") | (`DEACTIVATE_LAYER` if the layer is currently visible), `APPLY_FILTER`, (`ZOOM_TO_EXTENT`) |
| Remove a filter ("zeig wieder alle") | `CLEAR_FILTER`, `ACTIVATE_LAYER` (same layer id) |
| UDP analysis | `CLEAR_RESULT_LAYERS`, `ADD_RESULT_LAYER` × n (`context` layers first, then `result`), `ZOOM_TO_EXTENT` |
| Open a tool ("öffne die Legende", "Zeig mir die Legende", "Drucke die Karte") | `OPEN_TOOL` |
| Small talk / unclear request | no commands, only the text answer |

### Shared types

```ts
type Coordinates = { lon: number; lat: number };          // WGS84
type LocationKind = "bezirk" | "stadtteil" | "address";
type GeoJSONFeatureCollection = { type: "FeatureCollection"; features: Feature[] }; // WGS84
```

### `ACTIVATE_LAYER`

```json
{"commandId": "be5b7385-…", "type": "ACTIVATE_LAYER", "payload": {"layerIds": ["27764"]}}
```

| Field | Type | Notes |
|---|---|---|
| `layerIds` | `string[]` (≥1) | Portal layer ids (`config.json` / `services.json`). Set each one visible. Never hides other layers. |

### `DEACTIVATE_LAYER`

```json
{"commandId": "…", "type": "DEACTIVATE_LAYER", "payload": {"layerIds": ["27764"]}}
```

| Field | Type | Notes |
|---|---|---|
| `layerIds` | `string[]` (≥1) | Set each one invisible. Sent when the user explicitly asks to hide a layer, or right before an `APPLY_FILTER` on a layer that is currently visible (so the full layer and the filtered overlay are never shown together). |

### `NAVIGATE_TO_LOCATION`

```json
{
  "commandId": "…", "type": "NAVIGATE_TO_LOCATION",
  "payload": {
    "locations": [
      {"name": "Wandsbek", "coordinates": {"lon": 10.114523636, "lat": 53.642769612}, "kind": "bezirk"}
    ],
    "showMarkers": true,
    "zoom": null
  }
}
```

| Field | Type | Notes |
|---|---|---|
| `locations` | `{name, coordinates, kind?}[]` (≥1) | Center on these; with several, fit all of them. `kind` may be missing or `null` (e.g. a street hit). |
| `showMarkers` | `boolean` (default `true`) | Place a marker per location. |
| `zoom` | `int \| null` | Explicit zoom level. `null` means use your own default. Usually followed by `ZOOM_TO_EXTENT`, which sets the final view. |

State: if `locations` has exactly one entry with a `kind`, set
`lastLocation = {name, kind}`.

### `ZOOM_TO_EXTENT`

```json
{"commandId": "c0797127-…", "type": "ZOOM_TO_EXTENT",
 "payload": {"bbox": [10.0193, 53.5593, 10.2101, 53.7261], "paddingRatio": 0.1}}
```

| Field | Type | Notes |
|---|---|---|
| `bbox` | `[minLon, minLat, maxLon, maxLat]` | WGS84. Fit the view to it. |
| `paddingRatio` | `number` (default `0.1`) | Relative padding around the box. |

### `DRAW_BOUNDARY`

```json
{
  "commandId": "…", "type": "DRAW_BOUNDARY",
  "payload": {
    "geometry": {"type": "Polygon", "coordinates": [[[10.0193, 53.5610], [10.0233, 53.7261], [10.2101, 53.7244], [10.2054, 53.5593], [10.0193, 53.5610]]]},
    "style": "outline",
    "label": "Wandsbek",
    "kind": "bezirk"
  }
}
```

| Field | Type | Notes |
|---|---|---|
| `geometry` | GeoJSON `Polygon` / `MultiPolygon` | WGS84. |
| `style` | `"outline"` (default) \| `"highlight"` | `highlight` = outline plus a light fill. |
| `label` | `string?` | Name of the area. |
| `kind` | `LocationKind?` | |

There is only one boundary overlay: a new `DRAW_BOUNDARY` replaces the
previous one. State: if `kind` is set, set `lastLocation = {name: label, kind}`.

### `APPLY_FILTER`

```json
{
  "commandId": "3f70f268-b5fa-49d1-9a9e-19393f14b926",
  "type": "APPLY_FILTER",
  "payload": {
    "layerId": "27764",
    "features": {
      "type": "FeatureCollection",
      "features": [
        {"type": "Feature", "id": 12, "geometry": {"type": "Point", "coordinates": [10.0267, 53.5650]},
         "properties": {"name": "StadtRad-Station U Uhlandstraße / Eilenau"}},
        "… 76 more"
      ]
    },
    "label": "Wandsbek",
    "totalMatched": 77,
    "truncated": false,
    "filterSpec": {"conditions": [], "area": {"name": "Wandsbek", "kind": "bezirk"}}
  }
}
```

| Field | Type | Notes |
|---|---|---|
| `layerId` | `string` | The portal layer that was filtered. Use it as the overlay's key. |
| `features` | GeoJSON FeatureCollection | **The actual matching features**, already queried by the backend. Draw them as an ad-hoc overlay; don't re-filter the portal layer yourself. Features have only the properties the backend selected (usually `name`). |
| `label` | `string?` | Short description for UI/legend. |
| `totalMatched` | `int?` | Number of matches reported by the server (may be missing). |
| `truncated` | `boolean` | `true` if `totalMatched` > number of features sent (the backend limits how many it fetches). Worth telling the user. |
| `filterSpec` | `{conditions, area?}` or `null` | **Opaque to the frontend.** Store it under `layerId` and send it back in `state.filters` (see [section 5](#5-conversation-state-round-trip)). `null` means: delete any stored filter for this layer. `conditions[]` = `{field, operator, value}`, with `operator` ∈ `eq ne lt lte gt gte contains`. |

There is one overlay per `layerId`: a new `APPLY_FILTER` for the same
layer replaces the old overlay and its `filterSpec`.

### `CLEAR_FILTER`

```json
{"commandId": "9b644d61-…", "type": "CLEAR_FILTER", "payload": {"layerId": "27764"}}
```

Remove the overlay for `layerId` and delete its stored `filterSpec`.
This doesn't restore the full layer; the backend always sends an
`ACTIVATE_LAYER` right after it to do that.

### `ADD_RESULT_LAYER`

```json
{
  "commandId": "…", "type": "ADD_RESULT_LAYER",
  "payload": {
    "layerKey": "result",
    "label": "Schulen mit mehr als 500 Schülern",
    "role": "result",
    "features": {"type": "FeatureCollection", "features": ["…"]},
    "featureCount": 42,
    "totalFeatures": 42,
    "truncated": false,
    "geometryType": "Point"
  }
}
```

| Field | Type | Notes |
|---|---|---|
| `layerKey` | `string` | Unique within one analysis. Use it as the overlay key. |
| `label` | `string` | Display name. |
| `role` | `"result"` \| `"context"` | `result` = the answer, draw prominently. `context` = reference geometry (e.g. the district the analysis covers), draw as an outline underneath. |
| `features` | GeoJSON FeatureCollection | WGS84. |
| `featureCount` / `totalFeatures` | `int` | Features sent / features in the full result. |
| `truncated` | `boolean` | `featureCount < totalFeatures`. |
| `geometryType` | `string?` | e.g. `Point`, `Polygon`. |

### `CLEAR_RESULT_LAYERS`

```json
{"commandId": "…", "type": "CLEAR_RESULT_LAYERS", "payload": {}}
```

Remove all `ADD_RESULT_LAYER` overlays. Every new analysis starts with this command.

### `OPEN_TOOL`

```json
{"commandId": "…", "type": "OPEN_TOOL", "payload": {"toolName": "CONTACT", "prefill": null}}
```

| Field | Type | Notes |
|---|---|---|
| `toolName` | `"PRINT" \| "LEGEND" \| "SHARE" \| "ABOUT" \| "CONTACT"` | Open that menu panel. Masterportal mapping: `print`, `legend`, `shareView` (secondary menu), `about` (main menu), `contact` (secondary menu). If the portal has no such module configured, do nothing. |
| `prefill` | `{subject?, message?}` or `null` | Meant for pre-filling the contact form. **Defined in the schema but never set yet**; always `null`/missing today. |

### `HIGHLIGHT_FEATURE`

*Not sent yet.* `payload: {layerId: string, featureId: string}`.

### `CLEAR_HIGHLIGHTS`

*Not sent yet.* `payload: {}`.

### `SET_LANGUAGE`

*Not sent yet.* `payload: {languageCode: string}`, one of
`portal_context.languages`.

---

## 5. Conversation state round trip

The backend is stateless, so the frontend holds the conversation memory
and sends it with every request:

```
 turn 1 request:  state = {}                       messages = [U1]
 turn 1 stream:   APPLY_FILTER(layerId 27764, filterSpec {area: Wandsbek})
                  → frontend stores filters["27764"] = filterSpec
                  → assistant text A1 shown in chat

 turn 2 request:  state = { activeLayerIds: <read from map>,
                            filters: [{layerId: "27764", filterSpec}],
                            lastLocation: {name: "Wandsbek", kind: "bezirk"} }
                  messages = [U1, A1, U2]
```

The frontend's bookkeeping, driven by the commands it executes:

| Command | State update |
|---|---|
| `APPLY_FILTER` with `filterSpec` | `filters[layerId] = filterSpec` |
| `APPLY_FILTER` with `filterSpec: null`, or `CLEAR_FILTER` | `delete filters[layerId]` |
| `DRAW_BOUNDARY` with `kind` | `lastLocation = {name: label, kind}` |
| `NAVIGATE_TO_LOCATION` with one location that has a `kind` | `lastLocation = {name, kind}` |
| "New chat" | new `threadId`, `filters = {}`, `lastLocation = null`, empty history |

`activeLayerIds` is never tracked like this; read it from the map just
before sending each request.

---

## 6. Example 1: "Zeige mir Fahrradstationen in Wandsbek"

Recorded live against the running backend on 2026-10-13 (took 2 min 24 s).
Feature lists and the reasoning text are shortened (`…`); everything else
is exactly what came over the wire.

**Request:** the JSON shown in [section 2](#2-request-post-apiquery)
(first turn, empty state).

**Response stream:**

```text
data: {"type":"RUN_STARTED","threadId":"3f6c2a1e-0b7d-4c55-9a51-2f1d8e6b7c90","runId":"a1b2c3d4-0001-4000-8000-000000000001"}

data: {"type":"REASONING_START","messageId":"064d1c2c-c448-4e3e-a842-79cd8e6d5478"}
data: {"type":"REASONING_MESSAGE_CONTENT","messageId":"064d1c2c-…","delta":"We need answer in German probably. User: \"Zeige mir Fahrradstationen in Wandsbek\". Need call tools. …"}
data: {"type":"REASONING_END","messageId":"064d1c2c-…"}

── sub-agent 1: where is "Wandsbek"? ─────────────────────────────────────
data: {"type":"TOOL_CALL_START","toolCallId":"0d4dd6ed-d068-4672-a53d-560f68e3ad46","toolCallName":"location_extractor"}
data: {"type":"TOOL_CALL_ARGS","toolCallId":"0d4dd6ed-…","delta":"{\"query\": \"Wandsbek\"}"}
data: {"type":"TOOL_CALL_END","toolCallId":"0d4dd6ed-…"}
data: {"type":"TOOL_CALL_RESULT","messageId":"09fa7b79-…","toolCallId":"0d4dd6ed-…","content":"[{\"name\": \"Wandsbek\", \"latitude\": 53.642769612, \"longitude\": 10.114523636, \"kind\": \"bezirk\", \"boundary\": {…}, \"extent\": […], …}]"}

── sub-agent 2: which layer means "Fahrradstationen"? ────────────────────
data: {"type":"TOOL_CALL_START","toolCallId":"44355500-c7a8-4442-acee-f5c34ceba22a","toolCallName":"layer_resolver"}
data: {"type":"TOOL_CALL_ARGS","toolCallId":"44355500-…","delta":"{\"query\": \"Fahrradstationen\"}"}
data: {"type":"TOOL_CALL_END","toolCallId":"44355500-…"}
data: {"type":"TOOL_CALL_RESULT","messageId":"34a120aa-…","toolCallId":"44355500-…","content":"{\"name\": \"StadtRAD-Stationen Hamburg\", \"layer_id\": \"27764\", \"typ\": \"OAF\", …}"}

── sub-agent 3: what should happen? ──────────────────────────────────────
data: {"type":"TOOL_CALL_START","toolCallId":"107ad825-c40d-440c-9a99-8512c01deefa","toolCallName":"action_detector"}
data: {"type":"TOOL_CALL_ARGS","toolCallId":"107ad825-…","delta":"{\"query\": \"Zeige mir Fahrradstationen in Wandsbek\"}"}
data: {"type":"TOOL_CALL_END","toolCallId":"107ad825-…"}
data: {"type":"TOOL_CALL_RESULT","messageId":"b10f0a43-…","toolCallId":"107ad825-…","content":"{\"action_type\": \"Zeigen\", \"confidence\": 0.85, \"deactivate_layer_ids\": [], \"clear_filter_layer_ids\": []}"}

── sub-agent 4: query the stations inside Wandsbek ───────────────────────
data: {"type":"TOOL_CALL_START","toolCallId":"24f94c89-1c02-4df1-8f54-0ee315580f28","toolCallName":"filter_agent"}
data: {"type":"TOOL_CALL_ARGS","toolCallId":"24f94c89-…","delta":"{\"query\": \"Fahrradstationen in Wandsbek\"}"}
data: {"type":"TOOL_CALL_END","toolCallId":"24f94c89-…"}
data: {"type":"TOOL_CALL_RESULT","messageId":"920d76f7-…","toolCallId":"24f94c89-…","content":"{\"layer_id\": \"27764\", \"number_matched\": 77, \"label\": \"Wandsbek\", \"area_kind\": \"bezirk\", \"conditions\": [], \"feature_count\": 77}"}

── MapCommand 1  ← EXECUTE ───────────────────────────────────────────────
data: {"type":"TOOL_CALL_START","toolCallId":"3f70f268-b5fa-49d1-9a9e-19393f14b926","toolCallName":"command_builder"}
data: {"type":"TOOL_CALL_ARGS","toolCallId":"3f70f268-…","delta":"{\"type\": \"APPLY_FILTER\"}"}
data: {"type":"TOOL_CALL_END","toolCallId":"3f70f268-…"}
data: {"type":"TOOL_CALL_RESULT","messageId":"fb1008fd-…","toolCallId":"3f70f268-b5fa-49d1-9a9e-19393f14b926","content":"{\"commandId\": \"3f70f268-b5fa-49d1-9a9e-19393f14b926\", \"type\": \"APPLY_FILTER\", \"payload\": {\"layerId\": \"27764\", \"features\": {\"type\": \"FeatureCollection\", \"features\": [ …77 features… ]}, \"label\": \"Wandsbek\", \"totalMatched\": 77, \"truncated\": false, \"filterSpec\": {\"conditions\": [], \"area\": {\"name\": \"Wandsbek\", \"kind\": \"bezirk\"}}}}"}

── MapCommand 2  ← EXECUTE ───────────────────────────────────────────────
data: {"type":"TOOL_CALL_START","toolCallId":"c0797127-b738-4f6c-933b-f066d9cc656b","toolCallName":"command_builder"}
data: {"type":"TOOL_CALL_ARGS","toolCallId":"c0797127-…","delta":"{\"type\": \"ZOOM_TO_EXTENT\"}"}
data: {"type":"TOOL_CALL_END","toolCallId":"c0797127-…"}
data: {"type":"TOOL_CALL_RESULT","messageId":"0756efa0-…","toolCallId":"c0797127-b738-4f6c-933b-f066d9cc656b","content":"{\"commandId\": \"c0797127-b738-4f6c-933b-f066d9cc656b\", \"type\": \"ZOOM_TO_EXTENT\", \"payload\": {\"bbox\": [10.019343661276467, 53.55932075528492, 10.210073331682324, 53.72614994420011], \"paddingRatio\": 0.1}}"}

── chat answer ───────────────────────────────────────────────────────────
data: {"type":"TEXT_MESSAGE_START","messageId":"8aa089e0-94c7-4946-a6a0-67470a597738","role":"assistant"}
data: {"type":"TEXT_MESSAGE_CONTENT","messageId":"8aa089e0-…","delta":"\n\n"}
data: {"type":"TEXT_MESSAGE_CONTENT","messageId":"8aa089e0-…","delta":"Auf"}
data: {"type":"TEXT_MESSAGE_CONTENT","messageId":"8aa089e0-…","delta":" der"}
   … 18 more chunks …
data: {"type":"TEXT_MESSAGE_END","messageId":"8aa089e0-…"}

data: {"type":"RUN_FINISHED","threadId":"3f6c2a1e-0b7d-4c55-9a51-2f1d8e6b7c90","runId":"a1b2c3d4-0001-4000-8000-000000000001"}
```

(In the real stream each `data:` line is followed by a blank line; the
`──` lines are annotations.)

**What the frontend does:**

| Event | Frontend action |
|---|---|
| `TOOL_CALL_START location_extractor` | Status: "Suche Gebiet..." |
| `TOOL_CALL_START layer_resolver` | Status: "Suche passende Kartenebene..." |
| `TOOL_CALL_START action_detector` | Status: "Bestimme gewünschte Aktion..." |
| `TOOL_CALL_START filter_agent` | Status: "Filtere Daten..." |
| `TOOL_CALL_RESULT` → `APPLY_FILTER` | Draw 77 station points as an overlay for layer `27764`; store `filters["27764"] = {conditions: [], area: {name: "Wandsbek", kind: "bezirk"}}` |
| `TOOL_CALL_RESULT` → `ZOOM_TO_EXTENT` | Fit the map to Wandsbek's bounding box |
| `TEXT_MESSAGE_*` | Chat shows: *"Auf der Karte sind jetzt 77 Fahrradstationen in Wandsbek eingeblendet."* |
| `RUN_FINISHED` | Clear the busy state |

Note: because the user named an area, this ran as a spatial filter
(`APPLY_FILTER`) rather than `ACTIVATE_LAYER`, which would show all of
Hamburg's stations. Whether the planner calls `filter_agent` is an LLM
decision, so the same question can sometimes produce the
"show a layer at a place" sequence from [section 4](#typical-sequences)
instead. Frontends must handle both.

---

## 7. Example 2: follow-up "Zeig wieder alle Fahrradstationen"

Recorded live right after example 1 (took 11 s). This shows the state
round trip: the backend knows about the Wandsbek filter only because the
frontend sent it back.

**Request:**

```json
{
  "threadId": "3f6c2a1e-0b7d-4c55-9a51-2f1d8e6b7c90",
  "runId": "a1b2c3d4-0002-4000-8000-000000000002",
  "messages": [
    {"id": "m-1", "role": "user", "content": "Zeige mir Fahrradstationen in Wandsbek"},
    {"id": "m-2", "role": "assistant", "content": "Auf der Karte sind jetzt 77 Fahrradstationen in Wandsbek eingeblendet."},
    {"id": "m-3", "role": "user", "content": "Zeig wieder alle Fahrradstationen"}
  ],
  "state": {
    "activeLayerIds": [],
    "filters": [
      {"layerId": "27764", "filterSpec": {"conditions": [], "area": {"name": "Wandsbek", "kind": "bezirk"}}}
    ],
    "lastLocation": {"name": "Wandsbek", "kind": "bezirk"}
  },
  "tools": [],
  "context": [],
  "forwardedProps": {
    "portal_context": {
      "layers": [
        {"id": "453", "name": "Geobasiskarten (farbig)", "typ": "WMS", "url": "https://geodienste.hamburg.de/HH_WMS_Geobasiskarten", "collection": null},
        {"id": "27764", "name": "StadtRAD-Stationen Hamburg", "typ": "WFS", "url": "https://geodienste.hamburg.de/HH_WFS_Stadtrad", "collection": null}
      ],
      "languages": ["de", "en"]
    }
  }
}
```

**Response stream** (reasoning shortened):

```text
data: {"type":"RUN_STARTED","threadId":"3f6c2a1e-…","runId":"a1b2c3d4-0002-4000-8000-000000000002"}
data: {"type":"REASONING_START", …}  …  {"type":"REASONING_END", …}

data: {"type":"TOOL_CALL_START","toolCallId":"02cd3768-1002-473e-9a67-21411e068673","toolCallName":"action_detector"}
data: {"type":"TOOL_CALL_ARGS","toolCallId":"02cd3768-…","delta":"{\"query\": \"Zeig wieder alle Fahrradstationen\", \"clearFilterLayerIds\": [\"27764\"]}"}
data: {"type":"TOOL_CALL_END","toolCallId":"02cd3768-…"}
data: {"type":"TOOL_CALL_RESULT","toolCallId":"02cd3768-…","content":"{\"action_type\": \"Zeigen\", \"confidence\": 0.85, \"deactivate_layer_ids\": [], \"clear_filter_layer_ids\": [\"27764\"]}", …}

data: {"type":"TOOL_CALL_START","toolCallId":"9b644d61-560f-4b40-ad46-7536e1917486","toolCallName":"command_builder"}
data: {"type":"TOOL_CALL_ARGS","toolCallId":"9b644d61-…","delta":"{\"type\": \"CLEAR_FILTER\"}"}
data: {"type":"TOOL_CALL_END","toolCallId":"9b644d61-…"}
data: {"type":"TOOL_CALL_RESULT","toolCallId":"9b644d61-…","content":"{\"commandId\": \"9b644d61-560f-4b40-ad46-7536e1917486\", \"type\": \"CLEAR_FILTER\", \"payload\": {\"layerId\": \"27764\"}}", …}

data: {"type":"TOOL_CALL_START","toolCallId":"be5b7385-5b4e-436b-ba08-f01d191e3498","toolCallName":"command_builder"}
data: {"type":"TOOL_CALL_ARGS","toolCallId":"be5b7385-…","delta":"{\"type\": \"ACTIVATE_LAYER\"}"}
data: {"type":"TOOL_CALL_END","toolCallId":"be5b7385-…"}
data: {"type":"TOOL_CALL_RESULT","toolCallId":"be5b7385-…","content":"{\"commandId\": \"be5b7385-5b4e-436b-ba08-f01d191e3498\", \"type\": \"ACTIVATE_LAYER\", \"payload\": {\"layerIds\": [\"27764\"]}}", …}

data: {"type":"TEXT_MESSAGE_START","messageId":"f8b92a33-…","role":"assistant"}
   … chunks: "Ich habe die Einschränkung auf Wandsbek aufgehoben. Auf der Karte sind jetzt wieder alle StadtRAD-Stationen in Hamburg eingeblendet." …
data: {"type":"TEXT_MESSAGE_END","messageId":"f8b92a33-…"}
data: {"type":"RUN_FINISHED", …}
```

**What the frontend does:** remove the Wandsbek overlay and
`delete filters["27764"]` (`CLEAR_FILTER`), then make the full layer
`27764` visible (`ACTIVATE_LAYER`). On the next request,
`activeLayerIds` will contain `"27764"` (read from the map) and
`filters` will be empty.

---

## 8. Errors

| Situation | What you get |
|---|---|
| Body isn't a valid `RunAgentInput` (missing `threadId`, wrong types, …) | HTTP **422**, FastAPI validation error JSON `{"detail": [ … ]}` |
| Last message isn't a non-empty user message; `portal_context` invalid (both or neither mode, one URL only, empty/oversized `layers`, a layer without `id`); `state` exceeds size limits | HTTP **400**, `{"detail": "the last message must be a non-empty user query"}` (or similar) |
| Something fails while the run is in progress (LLM down, geodata service error, …) | HTTP **200** stream with `RUN_ERROR` `{"message": "…"}`, then the stream ends. MapCommands executed before the error stay applied. |
| Backend not running / CORS / network | `fetch` rejects (`TypeError`) |
| The request can't be done (e.g. no matching layer) | Not an error: a normal run with no MapCommands and a text answer asking the user to clarify. |

Other endpoint: `GET /health` → `{"status": "ok"}`. Note the backend
needs a few minutes after startup to build its layer index before it
accepts requests.
