# CWYM-Agent — Drafted fixes for the architecture doc

Each fix below says which doc sections it touches, then gives replacement text and/or code.
Assumptions are marked **[assumption]**. Check them against the real code before merging.

## Review outcome (checked against the real code)

Every fix below was checked directly against the actual implementation rather than taken
on faith. Several **[assumption]** markers turned out to be wrong: the doc's sub-agent
identifiers `layer_search`/`address_resolver` don't exist in the real code — the actual
identifiers used everywhere (`sub_agents.py`, `AgentExecutorComponent`'s dispatch,
`execution_log` keys) are `layer_resolver`/`location_extractor`. The doc conflates the
*service class names* (`LayerSearchService`, `AddressResolver`) with the *sub-agent plan
identifiers*, which are deliberately different strings.

| # | Verdict |
|---|---|
| 1 | Real problem (commands really do arrive as one burst, not streamed as sub-agents finish) — but the proposed `CommandStream`/concurrent-execution rewrite is large scope. Also uses the wrong dict keys (`r["layer_search"]`, `r["address_resolver"]`) and an unimplemented action label (`"Ausblenden"` — real `_ACTION_KEYWORDS` has no hide/deactivate action). **Deferred** — not implemented. |
| 2 | **Already implemented.** `ChatWithYourMap.vue`'s `requestMapCommandFromBackend` already resolves `toolCallName` via a `toolCallId` map, exactly as described. No action taken. |
| 3 | Legitimate idea, but genuinely new scope (depends on an unbuilt `languages` field and `SET_LANGUAGE` detection), not a fix to existing broken behavior. **Deferred**, by explicit choice. |
| 4 | Only makes sense once Fix 3's `turn_summary`-carrying pipeline shape exists. **Deferred with Fix 3.** |
| 5 | `MasterAgentComponent` already has `@component`; the `plan_json`→`plan` rename is only needed *if* Fix 3 is adopted — not an independent bug today. **Deferred with Fix 3.** |
| 6 | The SSRF gap is real and independently verified: `PortalCatalogResolver` fetched any client-supplied URL with zero scheme/host/size validation. **Implemented** — a scoped guard (reject non-http(s) schemes, credentials-in-URL, `localhost`, loopback/link-local/private/reserved IP literals, oversized responses; optional hostname allowlist). The rest of Fix 6 (async rewrite, single-flight locking, a `LayerCatalog` return type) assumes the *original* Section 8 design (a portal-scoped embedding index) that this codebase deliberately deviated from — the real `PortalCatalogResolver` is a cheap JSON fetch + TTL cache returning a plain `set[str]`, so that part doesn't apply and wasn't implemented. |
| 7 | Both findings independently verified: `ApplyFilterPayload` had no `totalMatched`/`truncated`, and `filter_agent`'s own `TOOL_CALL_RESULT` duplicated the full `FeatureCollection` that `APPLY_FILTER` already carries. **Implemented**, using the real field name `number_matched` (not the doc's `numberMatched`/camelCase-in-Python assumption) and the real sub-agent name `filter_agent`. |
| 8 | The gap is real — `toolStatusMessages.js` had no entry for `filter_agent` — but the doc's proposed table uses the wrong names for the other three (`layer_search`, `address_resolver`; real names `layer_resolver`, `location_extractor` already had correct entries). **Implemented**: added only the missing `filter_agent` entry, left the three correct existing entries alone. |
| 9 | The existing §9 "Implementation note" (added earlier, independently) already covers this — same `S_INTERSECTS`/`x-ogc-role: "primary-geometry"`/no-LLM/live-verified-narrowing content, in more detail. **Already satisfied.** No action taken. |

Full detail: `ImplementationLog.md`, "Review of cwym-architecture-fixes.md".

| # | Fix | Touches |
|---|---|---|
| 1 | Emit MapCommands as sub-agents finish (really "as ready") | §1, §3, §4, §7 |
| 2 | Frontend: resolve `toolCallName` via `toolCallId` | §5 (Frontend execution) |
| 3 | Branching pipeline: `ConditionalRouter` + `BranchJoiner` | §1, §4, §7 |
| 4 | `TextResponseComponent` gets a compact `turn_summary` instead of raw commands | §4, §5 |
| 5 | Component signatures match what the endpoint passes | §4 |
| 6 | SSRF guard + single-flight cold start in `PortalCatalogResolver` | §8 |
| 7 | Bounded `APPLY_FILTER` payloads | §5, §9 |
| 8 | Consistent sub-agent naming in the status-line table | §3, §7 |
| 9 | Clean up §9 wording | §9 |

---

## Fix 1 — Emit commands as sub-agents finish

**Problem.** §3 says `ACTIVATE_LAYER` can run before `NAVIGATE_TO_LOCATION` has finished resolving. In the current code, commands are only built after *all* sub-agents and the aggregator have run, so they arrive as a burst.

**Change.** `CommandBuilderComponent` stops being a pipeline stage. It becomes `CommandStream`, a plain per-run helper object, like the bridge. `AgentExecutorComponent` owns it and feeds it each sub-agent result as that result arrives.

Each command family declares two kinds of input:
- **Hard inputs:** the command cannot exist without them.
- **Soft inputs:** the command waits for them only if they are in this turn's plan.

A family fires as soon as its inputs have arrived. This dataflow rule also covers the ordering constraint §3 anticipates (e.g. `DEACTIVATE_LAYER` before a replacing `ACTIVATE_LAYER`): the ordering lives in the server-side rule, never in a client-side sort key.

Independent sub-agents run concurrently. Dependent ones run in a later wave: FilterAgent needs LayerSearchService's layer and AddressResolver's boundary.

### `command_stream.py`

```python
import threading, uuid
from dataclasses import dataclass, field
from typing import Callable
from jsonschema import Draft202012Validator

MAP_COMMAND_VALIDATOR = Draft202012Validator(load_schema("map-command.json"))

HIDE_ACTIONS = {"Ausblenden"}  # [assumption] use action_detector's real labels


@dataclass(frozen=True)
class CommandRule:
    name: str
    requires: frozenset[str]                        # hard inputs
    waits_for: frozenset[str] = frozenset()         # soft inputs (only if planned)
    excluded_by: frozenset[str] = frozenset()       # rule is off if any of these is planned
    build: Callable[[dict], list[dict]] = field(default=lambda r: [])

    def applies(self, planned: set[str]) -> bool:
        return self.requires <= planned and not (self.excluded_by & planned)

    def ready(self, planned: set[str], arrived: set[str]) -> bool:
        return self.requires <= arrived and (self.waits_for & planned) <= arrived


def _cmd(type_: str, payload: dict) -> dict:
    return {"commandId": str(uuid.uuid4()), "type": type_, "payload": payload}


def build_layer_commands(r: dict) -> list[dict]:
    layer_ids = [l["id"] for l in r["layer_search"]["layers"]]
    action = (r.get("action_detector") or {}).get("action_type", "Zeigen")
    kind = "DEACTIVATE_LAYER" if action in HIDE_ACTIONS else "ACTIVATE_LAYER"
    return [_cmd(kind, {"layerIds": layer_ids})] if layer_ids else []


def build_location_commands(r: dict) -> list[dict]:
    loc = r["address_resolver"]
    if loc.get("boundary"):                         # area: outline it, then fit it
        return [
            _cmd("DRAW_BOUNDARY", {"geometry": loc["boundary"], "label": loc["name"]}),
            _cmd("ZOOM_TO_EXTENT", {"bbox": bbox_of(loc["boundary"])}),
        ]
    return [_cmd("NAVIGATE_TO_LOCATION", {"locations": [
        {"name": loc["name"], "coordinates": loc["coordinates"]}
    ]})]


def build_filter_commands(r: dict) -> list[dict]:
    f = r["filter_agent"]
    if not f or not f.get("features", {}).get("features"):
        return []                                   # nothing to filter by → zero commands
    return [_cmd("APPLY_FILTER", {
        "layerId": f["layerId"], "features": f["features"], "label": f.get("label"),
        "totalMatched": f.get("totalMatched"), "truncated": f.get("truncated", False),
    })]


DEFAULT_RULES = (
    # A filter turn shows only the filtered overlay (§9), so layer commands are off
    # whenever filter_agent is planned, whether or not it finds anything.
    CommandRule("layers", frozenset({"layer_search"}), frozenset({"action_detector"}),
                excluded_by=frozenset({"filter_agent"}), build=build_layer_commands),
    CommandRule("location", frozenset({"address_resolver"}), build=build_location_commands),
    CommandRule("filter", frozenset({"filter_agent"}), build=build_filter_commands),
)


class CommandStream:
    """Per-run. Collects sub-agent results, fires each command rule once its
    inputs have arrived, validates and emits each MapCommand as its own
    TOOL_CALL_* cycle. Thread-safe: executor threads call on_result()."""

    def __init__(self, bridge, planned: set[str], rules=DEFAULT_RULES):
        self.bridge = bridge
        self.planned = planned
        self.rules = [r for r in rules if r.applies(planned)]
        self.results: dict[str, dict | None] = {}
        self.failed: set[str] = set()
        self.emitted: list[dict] = []
        self._fired: set[str] = set()
        self._lock = threading.Lock()

    def on_result(self, agent_name: str, result: dict | None, ok: bool = True):
        with self._lock:
            self.results[agent_name] = result if ok else None
            if not ok:
                self.failed.add(agent_name)
            arrived = set(self.results)
            for rule in self.rules:
                if rule.name in self._fired or not rule.ready(self.planned, arrived):
                    continue
                self._fired.add(rule.name)
                if rule.requires & self.failed:
                    continue                        # a hard input failed → no commands
                for cmd in rule.build(self.results):
                    self._emit(cmd)

    def emit_direct(self, cmd: dict):
        """For the chrome route (Fix 3): commands that need no sub-agent."""
        with self._lock:
            self._emit(cmd)

    def _emit(self, cmd: dict):
        MAP_COMMAND_VALIDATOR.validate(cmd)         # closed enum + per-type payload
        self.emitted.append(cmd)
        if self.bridge:
            self.bridge.tool_call(cmd["commandId"], "command_builder",
                                  {"type": cmd["type"]}, cmd)
```

### `AGUIEventBridge`: keep each tool-call cycle contiguous

Sub-agents now emit from several threads. Without a lock, two cycles' `START/ARGS/END/RESULT` events could interleave. Add to `AGUIEventBridge`:

```python
    def __init__(self, run_id: str):
        self.run_id = run_id
        self._q: queue.Queue = queue.Queue()
        self._cycle_lock = threading.Lock()

    def tool_call(self, call_id: str, name: str, args: dict, result):
        with self._cycle_lock:                      # one cycle = four adjacent events
            self.emit(ToolCallStartEvent(toolCallId=call_id, toolCallName=name))
            self.emit(ToolCallArgsEvent(toolCallId=call_id, delta=json.dumps(args)))
            self.emit(ToolCallEndEvent(toolCallId=call_id))
            self.emit(ToolCallResultEvent(toolCallId=call_id, content=json.dumps(result)))
```

### `AgentExecutorComponent`

```python
from concurrent.futures import ThreadPoolExecutor, as_completed

DEPENDENT_AGENTS = {"filter_agent"}  # needs layer_search + address_resolver results


@component
class AgentExecutorComponent:
    @component.output_types(execution_log=list, commands=list)
    def run(self, plan: dict, layer_catalog: "LayerCatalog",
            bridge: AGUIEventBridge | None = None):
        steps = plan["steps"]
        stream = CommandStream(bridge, {s["agent_name"] for s in steps})
        log: list[dict] = []

        waves = [
            [s for s in steps if s["agent_name"] not in DEPENDENT_AGENTS],
            [s for s in steps if s["agent_name"] in DEPENDENT_AGENTS],
        ]
        for wave in filter(None, waves):
            with ThreadPoolExecutor(max_workers=len(wave)) as pool:
                futures = {
                    pool.submit(self._run_sub_agent, step, layer_catalog,
                                dict(stream.results)): step
                    for step in wave
                }
                for fut in as_completed(futures):
                    step = futures[fut]
                    name, call_id = step["agent_name"], str(uuid.uuid4())
                    try:
                        result = fut.result()
                        ok = True
                    except Exception as e:          # one failed sub-agent ≠ failed run
                        result, ok = {"error": str(e)}, False
                    if bridge:
                        bridge.tool_call(call_id, name, step["args"],
                                         tool_result_view(name, result))  # see Fix 7
                    log.append({"agent_name": name, "ok": ok, "result": result})
                    stream.on_result(name, result if ok else None, ok=ok)

        return {"execution_log": log, "commands": stream.emitted}
```

### Replacement text for §3 "Streaming order and execution order are the same thing"

> **Commands are emitted as soon as their inputs exist.** `AgentExecutorComponent` runs independent sub-agents concurrently and passes each result to a per-run `CommandStream` the moment it arrives. Each command family declares its hard inputs (the command can't exist without them) and soft inputs (waited for only if they're in this turn's plan). E.g. layer commands need `layer_search` and wait for `action_detector` only if it was planned. A family fires once, as soon as its inputs are in. So `ACTIVATE_LAYER` can reach the map while `address_resolver` is still resolving, and well before NLG starts.
>
> The frontend executes each command on receipt, with no batching or reordering. Any ordering constraint between commands is expressed server-side, either as an input dependency between rules or as the order of commands within one rule's output. Never via a client-side sort key.
>
> The bridge serialises each `TOOL_CALL_*` cycle, so events of two commands never interleave, even though they come from different threads.

In §1, remove `CommandBuilderComponent` from the pipeline diagram and table. Add a note to `AgentExecutorComponent` saying it emits MapCommands via `CommandStream` as sub-agents finish.

---

## Fix 2 — Frontend: resolve the tool name via `toolCallId`

AG-UI's `TOOL_CALL_RESULT` carries `toolCallId` and `content`, but not `toolCallName`. The client has to remember the name from `TOOL_CALL_START`.

Replaces the "Frontend execution" snippet in §5:

```js
// agUiEventHandler.js
const toolCallNames = new Map(); // toolCallId -> toolCallName, per run

export function handleAgUiEvent(event, { executeSingleCommand, showStatus, clearStatus }) {
  switch (event.type) {
    case "TOOL_CALL_START":
      toolCallNames.set(event.toolCallId, event.toolCallName);
      if (event.toolCallName !== "command_builder") {
        showStatus(STATUS_TEXT[event.toolCallName] ?? "Verarbeite Anfrage...");
      }
      break;

    case "TOOL_CALL_RESULT": {
      const name = toolCallNames.get(event.toolCallId);
      toolCallNames.delete(event.toolCallId);
      if (name !== "command_builder") break;
      try {
        executeSingleCommand(JSON.parse(event.content));
      } catch (err) {
        // one bad command must not break the rest of the stream
        console.error("MapCommand failed", err, event.content);
      }
      break;
    }

    case "RUN_FINISHED":
    case "RUN_ERROR":
      toolCallNames.clear();
      clearStatus();
      break;
  }
}
```

Because `commandId` is reused as `toolCallId`, the map key is also the command's identity. That makes logging and debugging correlate cleanly.

---

## Fix 3 — Branching pipeline for chrome and clarify turns

A Haystack graph is static. To skip the executor on some turns, `MasterAgentComponent` emits a `route` in its plan. A `ConditionalRouter` then picks one of three branches, and a `BranchJoiner` merges them back into one `turn_summary` input for `TextResponseComponent`.

### Plan contract additions (`MasterAgentComponent` output `plan`)

```jsonc
// route: "orchestrate" (default) | "chrome" | "clarify"
{ "route": "orchestrate", "steps": [ ... ] }
{ "route": "chrome", "chrome_commands": [ { "type": "OPEN_TOOL", "payload": { "toolName": "PRINT" } } ] }
{ "route": "clarify", "clarify": { "reason": "missing_language" } }
```

### Wiring

```python
from haystack import Pipeline
from haystack.components.routers import ConditionalRouter
from haystack.components.joiners import BranchJoiner

routes = [
    {"condition": "{{ plan.route == 'chrome' }}",  "output": "{{ plan }}",
     "output_name": "chrome", "output_type": dict},
    {"condition": "{{ plan.route == 'clarify' }}", "output": "{{ plan }}",
     "output_name": "clarify", "output_type": dict},
    {"condition": "{{ True }}",                    "output": "{{ plan }}",   # default
     "output_name": "orchestrate", "output_type": dict},
]

p = Pipeline()
p.add_component("master_agent",   MasterAgentComponent(llm))
# unsafe=True passes the plan dict through as a native object. Acceptable here because
# the templates are fixed in code, never user input. Verify on your Haystack version.
p.add_component("router",         ConditionalRouter(routes, unsafe=True))
p.add_component("agent_executor", AgentExecutorComponent())
p.add_component("aggregator",     ResultAggregatorComponent())
p.add_component("chrome",         ChromeCommandComponent())
p.add_component("clarify",        ClarifyComponent())
p.add_component("summary_joiner", BranchJoiner(dict))
p.add_component("text_response",  TextResponseComponent(llm))

p.connect("master_agent.plan",           "router.plan")
p.connect("router.orchestrate",          "agent_executor.plan")
p.connect("agent_executor.execution_log","aggregator.execution_log")
p.connect("agent_executor.commands",     "aggregator.commands")
p.connect("router.chrome",               "chrome.plan")
p.connect("router.clarify",              "clarify.plan")
p.connect("aggregator.turn_summary",     "summary_joiner.value")
p.connect("chrome.turn_summary",         "summary_joiner.value")
p.connect("clarify.turn_summary",        "summary_joiner.value")
p.connect("summary_joiner.value",        "text_response.turn_summary")
```

Components on branches that don't fire never run, because their mandatory `plan` input never arrives. It is fine for the endpoint to pass `bridge` / `layer_catalog` to all of them unconditionally.

### Chrome and clarify components

```python
@component
class ChromeCommandComponent:
    """OPEN_TOOL / SET_LANGUAGE: nothing to resolve, emit directly."""

    @component.output_types(turn_summary=dict)
    def run(self, plan: dict, portal_languages: list[str],
            bridge: AGUIEventBridge | None = None):
        stream = CommandStream(bridge, planned=set())
        for proposed in plan.get("chrome_commands", []):
            if (proposed["type"] == "SET_LANGUAGE"
                    and proposed["payload"].get("languageCode") not in portal_languages):
                # Unknown/unsupported target → ask instead of act (§5)
                return {"turn_summary": {"kind": "clarify", "reason": "missing_language",
                                         "options": portal_languages}}
            stream.emit_direct({"commandId": str(uuid.uuid4()), **proposed})
        return {"turn_summary": {"kind": "result",
                                 "commands": [summarize_command(c) for c in stream.emitted]}}


@component
class ClarifyComponent:
    @component.output_types(turn_summary=dict)
    def run(self, plan: dict, portal_languages: list[str]):
        c = plan.get("clarify", {})
        options = portal_languages if c.get("reason") == "missing_language" else c.get("options", [])
        return {"turn_summary": {"kind": "clarify", "reason": c.get("reason"), "options": options}}
```

`portal_languages` comes from the frontend (see Fix 6's `PortalContext`). `portalLanguage.languages` lives in `config.js`, which the backend deliberately doesn't parse (§8), and the frontend already knows the value.

### Replacement text for §7 "UI-chrome intents … can bypass sub-agent orchestration"

> **Routing.** `MasterAgentComponent`'s plan carries a `route`.
> - `orchestrate` (default) runs the executor → aggregator branch.
> - `chrome` runs `ChromeCommandComponent`, which emits `OPEN_TOOL` / `SET_LANGUAGE` directly with no orchestration latency.
> - `clarify` runs `ClarifyComponent`, which emits zero MapCommands and hands a clarification reason to NLG.
>
> A `ConditionalRouter` picks the branch and a `BranchJoiner` merges them into `TextResponseComponent.turn_summary`. A `SET_LANGUAGE` whose target isn't among the portal's configured languages is downgraded to a clarify turn inside `ChromeCommandComponent`, not guessed.

---

## Fix 4 — Compact `turn_summary` for NLG

`TextResponseComponent` must never see raw GeoJSON. It also needs things commands don't carry: layer *names*, filter conditions, why nothing happened, and what to ask. `ResultAggregatorComponent` now builds that summary.

```python
def summarize_command(cmd: dict, layer_names: dict[str, str] | None = None) -> dict:
    t, p = cmd["type"], cmd["payload"]
    names = layer_names or {}
    if t in ("ACTIVATE_LAYER", "DEACTIVATE_LAYER"):
        return {"type": t, "layers": [names.get(i, i) for i in p["layerIds"]]}
    if t == "APPLY_FILTER":
        return {"type": t, "layer": names.get(p["layerId"], p["layerId"]),
                "label": p.get("label"),
                "shown": len(p["features"].get("features", [])),
                "totalMatched": p.get("totalMatched"), "truncated": p.get("truncated", False)}
    if t == "NAVIGATE_TO_LOCATION":
        return {"type": t, "locations": [l["name"] for l in p["locations"]]}
    if t in ("DRAW_BOUNDARY", "ZOOM_TO_EXTENT"):
        return {"type": t, "label": p.get("label")}     # no geometry, no bbox
    return {"type": t, "payload": p}                     # OPEN_TOOL, SET_LANGUAGE: tiny


@component
class ResultAggregatorComponent:
    @component.output_types(turn_summary=dict)
    def run(self, execution_log: list, commands: list):
        by_agent = {e["agent_name"]: e for e in execution_log}
        layer_names = {l["id"]: l["name"]                # [assumption] layer_search returns names
                       for l in (by_agent.get("layer_search", {}).get("result") or {}).get("layers", [])}
        filter_res = (by_agent.get("filter_agent") or {}).get("result") or {}
        return {"turn_summary": {
            "kind": "result" if commands else "no_result",
            "commands": [summarize_command(c, layer_names) for c in commands],
            "filters": filter_res.get("conditions", []),   # the validated {field, operator, value} list
            "failures": [e["agent_name"] for e in execution_log if not e["ok"]],
        }}


@component
class TextResponseComponent:
    @component.output_types(response_text=str)
    def run(self, turn_summary: dict, query: str, bridge: AGUIEventBridge | None = None):
        prompt = self._build_response_prompt(turn_summary, query)
        # streaming body unchanged from §4
        ...
```

`_build_response_prompt` branches on `kind`:
- `result`: describe what the map now shows.
- `no_result`: say plainly that nothing matching was found. If `failures` is non-empty, say that a lookup failed rather than that nothing exists.
- `clarify`: ask one German question, offering `options` if present.

---

## Fix 5 — Signatures match the endpoint

- `AgentExecutorComponent.run` now accepts `layer_catalog` (Fix 1) and forwards it to LayerSearchService inside `_run_sub_agent`.
- `MasterAgentComponent` needs `@component` / `@component.output_types(plan=dict)` and returns `{"plan": ...}`. The router expects `plan`, not `plan_json`.
- `CommandBuilderComponent` is removed from the pipeline (Fix 1).

Updated endpoint run data:

```python
master_pipeline.run(data={
    "master_agent":   {"query": request.query, "bridge": bridge},
    "agent_executor": {"bridge": bridge, "layer_catalog": catalog},
    "chrome":         {"bridge": bridge, "portal_languages": request.portal_context.languages},
    "clarify":        {"portal_languages": request.portal_context.languages},
    "text_response":  {"query": request.query, "bridge": bridge},
})
```

---

## Fix 6 — `PortalCatalogResolver`: SSRF guard and single-flight cold start

The backend now fetches URLs supplied by the client. Restrict what it will fetch, and make sure that concurrent first requests for the same portal build the catalog only once.

```python
import asyncio, hashlib, httpx
from urllib.parse import urlsplit
from pydantic import BaseModel, HttpUrl
from fastapi import HTTPException

class PortalContext(BaseModel):
    config_json_url: HttpUrl
    services_json_url: HttpUrl
    languages: list[str] = ["de"]            # forwarded from config.js portalLanguage (Fix 3)

class QueryRequest(BaseModel):
    query: str
    portal_context: PortalContext


class PortalContextRejected(ValueError):
    pass


class PortalCatalogResolver:
    def __init__(self, allowed_hosts: set[str], max_bytes: int = 50 * 1024 * 1024):
        self.allowed_hosts = allowed_hosts           # e.g. {"geoportal-hamburg.de"} from settings
        self.max_bytes = max_bytes
        self._cache: dict[str, "LayerCatalog"] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._http = httpx.AsyncClient(follow_redirects=False,   # no redirect to other hosts
                                       timeout=httpx.Timeout(15.0))

    def _check(self, url: str) -> str:
        u = urlsplit(str(url))
        if u.scheme != "https" or u.username or u.password or u.port not in (None, 443):
            raise PortalContextRejected(f"unsupported URL: {url}")
        if u.hostname not in self.allowed_hosts:
            raise PortalContextRejected(f"host not allowed: {u.hostname}")
        return str(url)

    async def _fetch_json(self, url: str):
        async with self._http.stream("GET", url) as r:
            r.raise_for_status()
            buf = bytearray()
            async for chunk in r.aiter_bytes():
                buf += chunk
                if len(buf) > self.max_bytes:
                    raise PortalContextRejected(f"response too large: {url}")
        return json.loads(buf)

    async def get_or_build(self, ctx: PortalContext) -> "LayerCatalog":
        cfg, svc = self._check(ctx.config_json_url), self._check(ctx.services_json_url)
        key = hashlib.sha256(f"{cfg}\n{svc}".encode()).hexdigest()
        if (cat := self._cache.get(key)) and not cat.expired():
            return cat
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:                             # single-flight: one build per portal
            if (cat := self._cache.get(key)) and not cat.expired():
                return cat
            cat = await self._build(cfg, svc)        # fetch + cross-reference + embed (§8),
            self._cache[key] = cat                   # conditional GET on refresh
            return cat
```

In the endpoint, validate **before** opening the stream, so a rejected context is a plain 400 and not a half-open SSE response:

```python
try:
    catalog = await portal_catalog_resolver.get_or_build(request.portal_context)
except PortalContextRejected as e:
    raise HTTPException(status_code=400, detail=str(e))
```

### Addition to §8

> **Fetch policy.** `PortalCatalogResolver` only fetches `https` URLs on an allowlist of hosts (configured, currently just Hamburg's portal host). It follows no redirects and caps response size. Concurrent cold-start requests for the same portal share one build (per-key lock).

---

## Fix 7 — Bounded `APPLY_FILTER` payloads

Two changes:

**1. Cap the features.** FilterAgent requests at most `MAX_FILTER_FEATURES` (e.g. 1000; tune for rendering) via OAF's `limit`. It reports `numberMatched` when the server provides it. This is optional in OGC API Features, so `totalMatched` may be null.

Schema addition to the `APPLY_FILTER` payload:

```json
"totalMatched": { "type": ["integer", "null"],
  "description": "numberMatched from the OAF response, if the server reports it" },
"truncated":    { "type": "boolean", "default": false,
  "description": "true if more features matched than were sent" }
```

**2. Don't send the features twice.** FilterAgent's own sub-agent `TOOL_CALL_RESULT` shouldn't carry the full FeatureCollection, because `APPLY_FILTER` already does. `tool_result_view` (used in Fix 1's executor) trims it:

```python
def tool_result_view(agent_name: str, result: dict | None) -> dict | None:
    if agent_name == "filter_agent" and result and "features" in result:
        fc = result["features"]
        return {k: v for k, v in result.items() if k != "features"} | {
            "featureCount": len(fc.get("features", []))}
    return result
```

When `truncated` is true, NLG mentions it (via `turn_summary`, Fix 4), e.g. "…die ersten 1000 von 3412 Treffern".

---

## Fix 8 — Consistent naming in the status-line table

**[assumption]** The plan's `agent_name` values are the snake_case forms below. Replace the §7 example and the §3 `layer_resolver` mention with the real values.

| `toolCallName` | Status text |
|---|---|
| `layer_search` | "Suche passende Kartenebene..." |
| `address_resolver` | "Suche Ort..." |
| `filter_agent` | "Filtere Daten..." |
| `action_detector` | "Analysiere Anfrage..." |
| `command_builder` | *(not shown — execution, not resolution)* |

Also rename in §7 "Still open": "whether LayerSearchService's vector-search step…" (was "Layer Resolver").

---

## Fix 9 — §9 wording

Replace the paragraph starting "When the same turn also resolved a location…" with:

> **Spatial scoping.** When the same turn resolved a location with an area boundary (AddressResolver's `LocationMatch.boundary`), FilterAgent ANDs a `S_INTERSECTS(<geometry field>, <boundary as WKT>)` predicate into the CQL2 filter. The geometry field is the one marked `x-ogc-role: "primary-geometry"` in the collection's `/queryables` response. No LLM is involved in this part.
>
> This server-side scoping replaces the earlier approach of client-side masking via an OpenLayers style function on an already-loaded layer.
>
> Verified live against Hamburg's API: scoping strictly narrows results (`0 < scoped < citywide`). Exact counts aren't asserted, because AddressResolver's "Wandsbek" resolves to the Bezirk rather than the Stadtteil of the same name. That comes from its district-selection logic, not from FilterAgent.

In the "Filter turn stands alone" paragraph, you can now point to the mechanism: layer commands are excluded whenever `filter_agent` is in the plan (`CommandRule.excluded_by`, Fix 1). This holds whether or not `action_detector` ran and whether or not FilterAgent found anything.
