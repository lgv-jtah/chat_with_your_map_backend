"""Logging setup and per-turn conversation traces.

Two things live here:

- ``configure_logging()``: the root logging setup. Every log line carries
  the AG-UI ``threadId``/``runId`` of the request it belongs to (taken
  from context variables), so one conversation can be followed through
  the interleaved output of concurrent requests. Level and format
  (``text``/``json``) come from the environment.
- ``RunTrace``: one complete, structured record per chat turn - the
  request (query, history, map state), the planner's prompt and
  reasoning, every sub-agent call with its arguments, result and
  duration, every LLM call with tokens/``finish_reason``/duration, the
  MapCommands built and the final answer, plus the outcome (``ok`` /
  ``error`` with traceback). Written as a single JSON line on the
  ``cwym_agent.trace`` logger - to stdout and, if ``CWYM_TRACE_FILE`` is
  set, to a rotating JSONL file that survives container recreation.

The trace is filled mostly by observing the AG-UI events the pipeline
already emits (``AGUIEventBridge.emit`` forwards each one to
``RunTrace.on_event``), so components don't need to know about it. The
few things that never travel as an event (the planning prompt, LLM call
stats, the request itself) are added through the module-level helpers,
which are no-ops outside a traced run.

``CWYM_TRACE_CONTENT=metadata`` replaces user-written text (query,
history, prompts, reasoning, answer, tool arguments/results) with its
length - for deployments where conversation content must not be
stored. The default ``full`` keeps everything, which is what debugging
and collecting real user queries during user tests needs.

Context variables don't cross ``loop.run_in_executor`` on their own -
the caller runs the pipeline inside ``contextvars.copy_context().run``
and calls ``start_run`` in there (see ``cwym_agent.api.main``).
"""

import contextvars
import json
import logging
import os
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

TRACE_LOGGER_NAME = "cwym_agent.trace"
TRACE_VERSION = 1

_LOG_FORMATS = ("text", "json")
_TRACE_CONTENT_MODES = ("full", "metadata")

# A single trace line should stay greppable/loadable - long prompts and
# reasoning traces are kept, but capped.
_MAX_STRING_CHARS = 20_000
_MAX_LIST_ITEMS = 200
# Geometry is large and never what a trace reader is looking for.
_GEOMETRY_KEYS = frozenset({"geometry", "boundary"})

_thread_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("cwym_thread_id", default=None)
_run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("cwym_run_id", default=None)
_current_trace: contextvars.ContextVar["RunTrace | None"] = contextvars.ContextVar("cwym_run_trace", default=None)

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


def _env_choice(name: str, default: str, choices: tuple[str, ...]) -> str:
    value = os.environ.get(name, default).strip().lower()
    if value not in choices:
        raise ValueError(f"{name}={value!r} is not one of {', '.join(choices)}")
    return value


@dataclass(frozen=True)
class LoggingSettings:
    """Logging/trace configuration, read from ``CWYM_*`` env vars."""

    level: str = field(default_factory=lambda: os.environ.get("CWYM_LOG_LEVEL", "INFO").strip().upper())
    format: str = field(default_factory=lambda: _env_choice("CWYM_LOG_FORMAT", "text", _LOG_FORMATS))
    trace_content: str = field(
        default_factory=lambda: _env_choice("CWYM_TRACE_CONTENT", "full", _TRACE_CONTENT_MODES)
    )
    # Unset/empty = no file, stdout only.
    trace_file: str | None = field(default_factory=lambda: os.environ.get("CWYM_TRACE_FILE") or None)
    trace_file_max_mb: int = field(default_factory=lambda: _env_int("CWYM_TRACE_FILE_MAX_MB", 50))
    trace_file_backups: int = field(default_factory=lambda: _env_int("CWYM_TRACE_FILE_BACKUPS", 10))


_active_settings: LoggingSettings | None = None


def _settings() -> LoggingSettings:
    return _active_settings or LoggingSettings()


# -- log record correlation --------------------------------------------------


def _short(value: str | None) -> str:
    return value[:8] if value else "-"


def _add_ids(record: logging.LogRecord) -> None:
    thread_id, run_id = _thread_id.get(), _run_id.get()
    record.thread_id = thread_id
    record.run_id = run_id
    record.ids = f"[{_short(thread_id)}/{_short(run_id)}] " if (thread_id or run_id) else ""


class ContextFilter(logging.Filter):
    """Adds ``thread_id``/``run_id`` (full) and ``ids`` (a short
    ``[thread/run] `` prefix, empty outside a request) to a record.
    ``configure_logging`` also installs a record factory doing the same,
    so every handler (uvicorn's, pytest's) sees the ids; this filter is
    the fallback for records created before that."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "ids"):
            _add_ids(record)
        return True


_record_factory_installed = False


def _install_record_factory() -> None:
    global _record_factory_installed
    if _record_factory_installed:
        return
    previous = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        _add_ids(record)
        return record

    logging.setLogRecordFactory(factory)
    _record_factory_installed = True


class JsonFormatter(logging.Formatter):
    """One JSON object per line, for log shippers."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "threadId": getattr(record, "thread_id", None),
            "runId": getattr(record, "run_id", None),
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


_TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(ids)s%(message)s"


def configure_logging(settings: LoggingSettings | None = None) -> LoggingSettings:
    """(Re)configure the root logger and the trace logger. Idempotent -
    handlers it added before are replaced, others (pytest's, uvicorn's)
    are left alone."""

    global _active_settings
    settings = settings or LoggingSettings()
    _active_settings = settings
    _install_record_factory()

    root = logging.getLogger()
    for handler in [h for h in root.handlers if getattr(h, "_cwym", False)]:
        root.removeHandler(handler)
        handler.close()

    handler = logging.StreamHandler(sys.stdout)
    handler._cwym = True  # type: ignore[attr-defined]
    handler.addFilter(ContextFilter())
    handler.setFormatter(JsonFormatter() if settings.format == "json" else logging.Formatter(_TEXT_FORMAT))
    root.addHandler(handler)
    root.setLevel(settings.level)
    # Haystack logs "Deepcopy failed for object of type 'AGUIEventBridge'"
    # at INFO for every component of every turn - expected (the bridge
    # holds a queue and must stay the same object) and pure noise.
    logging.getLogger("haystack.core.pipeline.utils").setLevel(logging.WARNING)

    # The trace logger writes bare JSON lines (no timestamp/level prefix)
    # so its output - stdout or file - can be piped straight into jq.
    trace_logger = logging.getLogger(TRACE_LOGGER_NAME)
    trace_logger.propagate = False
    trace_logger.setLevel(logging.INFO)
    for existing in [h for h in trace_logger.handlers if getattr(h, "_cwym", False)]:
        trace_logger.removeHandler(existing)
        existing.close()

    trace_handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if settings.trace_file:
        path = Path(settings.trace_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        trace_handlers.append(
            RotatingFileHandler(
                path,
                maxBytes=settings.trace_file_max_mb * 1024 * 1024,
                backupCount=settings.trace_file_backups,
                encoding="utf-8",
            )
        )
    for trace_handler in trace_handlers:
        trace_handler._cwym = True  # type: ignore[attr-defined]
        trace_handler.setFormatter(logging.Formatter("%(message)s"))
        trace_logger.addHandler(trace_handler)

    logger.info(
        "Logging configured: level=%s format=%s trace_content=%s trace_file=%s",
        settings.level,
        settings.format,
        settings.trace_content,
        settings.trace_file or "-",
    )
    return settings


# -- value shaping -------------------------------------------------------------


def _compact(value: Any) -> Any:
    """Make a value trace-sized: GeoJSON/geometry is summarized, long
    strings and lists are capped."""

    if isinstance(value, dict):
        if value.get("type") == "FeatureCollection":
            return {"type": "FeatureCollection", "featureCount": len(value.get("features") or [])}
        compacted = {}
        for key, item in value.items():
            if key in _GEOMETRY_KEYS and isinstance(item, dict):
                compacted[key] = {"type": item.get("type"), "omitted": True}
            elif key == "features" and isinstance(item, list):
                compacted[key] = {"featureCount": len(item)}
            else:
                compacted[key] = _compact(item)
        return compacted
    if isinstance(value, list | tuple):
        items = [_compact(item) for item in value[:_MAX_LIST_ITEMS]]
        if len(value) > _MAX_LIST_ITEMS:
            items.append(f"... {len(value) - _MAX_LIST_ITEMS} more")
        return items
    if isinstance(value, str) and len(value) > _MAX_STRING_CHARS:
        return value[:_MAX_STRING_CHARS] + f"... [{len(value) - _MAX_STRING_CHARS} chars truncated]"
    return value


def _redact(value: Any) -> Any:
    """``metadata`` mode: every string becomes its length and every float
    is dropped (coordinates of a typed address are as personal as the
    address); structure, integers (counts) and booleans stay."""

    if isinstance(value, dict):
        return {key: _redact(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return {"chars": len(value)}
    if isinstance(value, float):
        return None
    return value


def _parse_json(text: str | None) -> Any:
    if text is None:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text


def _event_type(event) -> str:
    event_type = getattr(event, "type", None)
    return str(getattr(event_type, "value", event_type))


def _usage_dict(usage) -> dict | None:
    if usage is None:
        return None
    return {
        "promptTokens": getattr(usage, "prompt_tokens", None),
        "completionTokens": getattr(usage, "completion_tokens", None),
    }


# -- the per-turn trace -----------------------------------------------------------


class RunTrace:
    """Collects everything about one chat turn and writes it once."""

    def __init__(self, thread_id: str | None, run_id: str | None, content: str | None = None):
        self.thread_id = thread_id
        self.run_id = run_id
        self.content = content or _settings().trace_content
        self._t0 = time.monotonic()
        self._started_at = datetime.now(UTC).isoformat(timespec="milliseconds")
        self.request: dict[str, Any] = {}
        self.planning: dict[str, Any] = {"prompt": None, "reasoning": ""}
        self.steps: list[dict[str, Any]] = []
        self._steps_by_id: dict[str, dict[str, Any]] = {}
        self._command_ids: set[str] = set()
        self.commands: list[dict[str, Any]] = []
        self.llm_calls: list[dict[str, Any]] = []
        self.notes: list[str] = []
        self._answer: list[str] = []
        self._run_error_message: str | None = None
        self._finished = False

    def _elapsed_ms(self) -> int:
        return int((time.monotonic() - self._t0) * 1000)

    # -- filled by the API layer / components ---------------------------

    def set_request(self, **fields: Any) -> None:
        self.request.update(fields)

    def set_planning_prompt(self, prompt: str) -> None:
        self.planning["prompt"] = prompt

    def note(self, text: str) -> None:
        self.notes.append(text)

    def record_llm_call(
        self,
        label: str,
        duration_s: float,
        max_tokens: int | None = None,
        usage=None,
        finish_reason: str | None = None,
        prompt: str | None = None,
        output: str | None = None,
        tool_calls: list[dict] | None = None,
        error: BaseException | None = None,
    ) -> None:
        call: dict[str, Any] = {
            "label": label,
            "startedMs": self._elapsed_ms() - int(duration_s * 1000),
            "durationMs": int(duration_s * 1000),
            "maxTokens": max_tokens,
            "usage": _usage_dict(usage),
            "finishReason": finish_reason,
        }
        if prompt is not None:
            call["prompt"] = prompt
        if output is not None:
            call["output"] = output
        if tool_calls is not None:
            call["toolCalls"] = tool_calls
        if error is not None:
            call["error"] = f"{type(error).__name__}: {error}"
        self.llm_calls.append(call)

    # -- filled from the AG-UI event stream -----------------------------

    def on_event(self, event) -> None:
        """Observe one AG-UI event. Never raises - a tracing bug must not
        break a user's turn."""

        try:
            self._on_event(event)
        except Exception:  # noqa: BLE001
            logger.debug("RunTrace: could not record event %r", event, exc_info=True)

    def _on_event(self, event) -> None:
        event_type = _event_type(event)
        if event_type == "REASONING_MESSAGE_CONTENT":
            self.planning["reasoning"] += event.delta or ""
        elif event_type == "TOOL_CALL_START":
            if event.tool_call_name == "command_builder":
                self._command_ids.add(event.tool_call_id)
                return
            step = {
                "name": event.tool_call_name,
                "args": None,
                "result": None,
                "startedMs": self._elapsed_ms(),
                "durationMs": None,
            }
            self._steps_by_id[event.tool_call_id] = step
            self.steps.append(step)
        elif event_type == "TOOL_CALL_ARGS":
            step = self._steps_by_id.get(event.tool_call_id)
            if step is not None:
                step["args"] = _parse_json(event.delta)
        elif event_type == "TOOL_CALL_RESULT":
            if event.tool_call_id in self._command_ids:
                self.commands.append(_parse_json(event.content))
                return
            step = self._steps_by_id.get(event.tool_call_id)
            if step is not None:
                step["result"] = _parse_json(event.content)
                step["durationMs"] = self._elapsed_ms() - step["startedMs"]
        elif event_type == "TEXT_MESSAGE_CONTENT":
            self._answer.append(event.delta or "")
        elif event_type == "RUN_ERROR":
            self._run_error_message = event.message

    # -- output ------------------------------------------------------------

    @property
    def answer(self) -> str:
        return "".join(self._answer)

    def build_record(self, status: str, error: BaseException | None = None) -> dict[str, Any]:
        commands = [_compact(cmd) for cmd in self.commands]
        steps = [_compact(step) for step in self.steps]
        llm_calls = [_compact(call) for call in self.llm_calls]
        plan_call = next((call for call in self.llm_calls if call["label"] == "plan"), None)

        record: dict[str, Any] = {
            "type": "run_trace",
            "version": TRACE_VERSION,
            "threadId": self.thread_id,
            "runId": self.run_id,
            "startedAt": self._started_at,
            "durationMs": self._elapsed_ms(),
            "status": status,
            "contentMode": self.content,
            # A one-glance view of the turn; the details follow below.
            "summary": {
                "steps": [step["name"] for step in self.steps],
                "commands": [cmd.get("type") if isinstance(cmd, dict) else None for cmd in self.commands],
                "planFinishReason": plan_call["finishReason"] if plan_call else None,
                "llmMs": sum(call["durationMs"] for call in self.llm_calls),
                "answerChars": len(self.answer),
            },
            "request": _compact(self.request),
            "planning": _compact(self.planning),
            "steps": steps,
            "llmCalls": llm_calls,
            "commands": commands,
            "answer": _compact(self.answer),
            "notes": self.notes,
        }
        if error is not None:
            record["error"] = {
                "type": type(error).__name__,
                "message": str(error),
                "traceback": "".join(traceback.format_exception(error)),
            }
        elif self._run_error_message is not None:
            record["error"] = {"type": None, "message": self._run_error_message, "traceback": None}

        if self.content == "metadata":
            record = self._redacted(record)
        return record

    @staticmethod
    def _redacted(record: dict[str, Any]) -> dict[str, Any]:
        request = record["request"]
        state = request.get("mapState") or {}
        record["request"] = {
            **{
                key: value if isinstance(value, int | float | bool) or value is None else _redact(value)
                for key, value in request.items()
                if key not in ("mapState", "stateIn", "history")
            },
            # Not user content: an ISO code and where it came from.
            "language": request.get("language"),
            "languageSource": request.get("languageSource"),
            "history": [
                {"role": entry.get("role"), "text": _redact(entry.get("text"))}
                for entry in request.get("history") or []
            ],
            "mapState": {
                "activeLayerIds": state.get("activeLayerIds") or [],
                "filterLayerIds": [f.get("layerId") for f in state.get("filters") or []],
                "hasLastLocation": state.get("lastLocation") is not None,
            },
        }
        record["planning"] = _redact(record["planning"])
        record["steps"] = [
            {**step, "args": _redact(step["args"]), "result": _redact(step["result"])} for step in record["steps"]
        ]
        record["llmCalls"] = [
            {key: _redact(value) if key in ("prompt", "output", "toolCalls", "error") else value
             for key, value in call.items()}
            for call in record["llmCalls"]
        ]
        record["commands"] = [
            {"type": cmd.get("type"), "commandId": cmd.get("commandId"), "payload": _redact(cmd.get("payload"))}
            if isinstance(cmd, dict) else cmd
            for cmd in record["commands"]
        ]
        record["answer"] = _redact(record["answer"])
        record["notes"] = _redact(record["notes"])
        if "error" in record:
            record["error"] = {**record["error"], "message": _redact(record["error"]["message"])}
        return record

    def finish(self, status: str = "ok", error: BaseException | None = None) -> dict[str, Any] | None:
        """Write the trace line. Only the first call writes; later ones
        return ``None``."""

        if self._finished:
            return None
        self._finished = True
        try:
            record = self.build_record(status, error)
            logging.getLogger(TRACE_LOGGER_NAME).info(json.dumps(record, ensure_ascii=False, default=str))
        except Exception:  # noqa: BLE001 - never let tracing fail a request
            logger.exception("RunTrace: could not write the trace")
            return None
        logger.info(
            "run %s in %.1fs: steps=%s commands=%s plan_finish=%s",
            status,
            record["durationMs"] / 1000,
            record["summary"]["steps"],
            record["summary"]["commands"],
            record["summary"]["planFinishReason"],
        )
        return record


# -- module-level helpers (no-ops outside a traced run) --------------------------


def start_run(thread_id: str | None, run_id: str | None) -> RunTrace:
    """Bind the ids to this context's log records and start its trace.
    Call it inside the context the run executes in."""

    _thread_id.set(thread_id)
    _run_id.set(run_id)
    trace = RunTrace(thread_id, run_id)
    _current_trace.set(trace)
    return trace


def bind_ids(thread_id: str | None, run_id: str | None) -> None:
    """Only correlate log records, without a trace (e.g. a request
    rejected before the pipeline runs)."""

    _thread_id.set(thread_id)
    _run_id.set(run_id)


def current_trace() -> RunTrace | None:
    return _current_trace.get()


def content_logged() -> bool:
    """Whether user-written text may appear in log lines/traces
    (``CWYM_TRACE_CONTENT=full``)."""

    return _settings().trace_content == "full"


def record_llm_call(label: str, duration_s: float, **fields: Any) -> None:
    trace = _current_trace.get()
    if trace is not None:
        trace.record_llm_call(label, duration_s, **fields)


def set_planning_prompt(prompt: str) -> None:
    trace = _current_trace.get()
    if trace is not None:
        trace.set_planning_prompt(prompt)


def note(text: str) -> None:
    trace = _current_trace.get()
    if trace is not None:
        trace.note(text)
