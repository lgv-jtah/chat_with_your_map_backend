"""Read the run traces back: ``python -m cwym_agent.traces``.

Works on the JSONL file ``CWYM_TRACE_FILE`` points at (including its
rotated ``.1``, ``.2``, ... siblings) or on any file given with
``--file`` - e.g. an export copied off the server, or plain
``docker compose logs`` output (non-trace lines are skipped).

  show <id>     every turn whose threadId or runId starts with <id>, as
                a readable conversation (``--json`` for the raw records)
  queries       one CSV row per turn - the real user queries, with
                outcome - for building a test set

Runs inside the container too (``make trace ID=...``), so the server
needs no extra tools.
"""

import argparse
import csv
import json
import os
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path


def _trace_files(path: Path) -> list[Path]:
    """The file plus its rotated siblings, oldest first."""

    rotated = sorted(
        (p for p in path.parent.glob(path.name + ".*") if p.suffix.lstrip(".").isdigit()),
        key=lambda p: int(p.suffix.lstrip(".")),
        reverse=True,
    )
    return rotated + ([path] if path.exists() else [])


def _records(lines: Iterable[str]) -> Iterator[dict]:
    for line in lines:
        # `docker compose logs` prefixes lines with "service  | ".
        start = line.find("{")
        if start < 0:
            continue
        try:
            record = json.loads(line[start:])
        except ValueError:
            continue
        if isinstance(record, dict) and record.get("type") == "run_trace":
            yield record


def load(files: list[Path]) -> list[dict]:
    records: list[dict] = []
    for file in files:
        with file.open(encoding="utf-8", errors="replace") as handle:
            records.extend(_records(handle))
    return sorted(records, key=lambda r: r.get("startedAt") or "")


def _text(value) -> str:
    if isinstance(value, dict) and "chars" in value:
        return f"<{value['chars']} chars, not logged>"
    return "" if value is None else str(value)


def _clip(value, limit: int = 300) -> str:
    """One line of JSON, cut for the readable view - ``--json`` has it all."""

    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + f"... (+{len(text) - limit})"


def _format_turn(record: dict) -> str:
    summary = record.get("summary") or {}
    request = record.get("request") or {}
    lines = [
        f"── {record.get('startedAt')}  run {record.get('runId')}  "
        f"{record.get('status')}  {record.get('durationMs', 0) / 1000:.1f}s",
        f"  Nutzer:   {_text(request.get('query'))}",
    ]
    if request.get("resolvedQuery"):
        lines.append(f"  (als:     {_text(request.get('resolvedQuery'))})")
    state = request.get("mapState") or {}
    if state.get("activeLayerIds") or state.get("filters"):
        lines.append(f"  Karte:    aktiv={state.get('activeLayerIds')} filter={json.dumps(state.get('filters'), ensure_ascii=False)}")
    for call in record.get("llmCalls") or []:
        usage = call.get("usage") or {}
        lines.append(
            f"  LLM:      {call.get('label')} {call.get('durationMs', 0) / 1000:.1f}s "
            f"completion={usage.get('completionTokens')}/{call.get('maxTokens')} "
            f"finish={call.get('finishReason')}" + (f" ERROR {call['error']}" if call.get("error") else "")
        )
        if call.get("label") == "plan":
            lines.append(f"            tool calls: {json.dumps(call.get('toolCalls'), ensure_ascii=False)}")
    for step in record.get("steps") or []:
        lines.append(
            f"  Schritt:  {step.get('name')} {_clip(step.get('args'))} "
            f"({(step.get('durationMs') or 0) / 1000:.1f}s)"
        )
        lines.append(f"            -> {_clip(step.get('result'))}")
    lines.append(f"  Befehle:  {summary.get('commands')}")
    lines.extend(f"  Hinweis:  {_text(note)}" for note in record.get("notes") or [])
    lines.append(f"  Antwort:  {_text(record.get('answer'))}")
    if record.get("error"):
        lines.append(f"  FEHLER:   {record['error'].get('type')}: {_text(record['error'].get('message'))}")
        if record["error"].get("traceback"):
            lines.append("            " + record["error"]["traceback"].replace("\n", "\n            "))
    return "\n".join(lines)


def _matches(record: dict, prefix: str) -> bool:
    return any((record.get(key) or "").startswith(prefix) for key in ("threadId", "runId"))


def cmd_show(records: list[dict], prefix: str, as_json: bool, out) -> int:
    matching = [r for r in records if _matches(r, prefix)]
    if not matching:
        print(f"No trace with a threadId/runId starting with {prefix!r}.", file=sys.stderr)
        return 1
    if as_json:
        for record in matching:
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
        return 0
    # A runId prefix finds one turn - show its whole conversation.
    thread_ids = {r.get("threadId") for r in matching}
    conversation = [r for r in records if r.get("threadId") in thread_ids]
    for thread_id in sorted(thread_ids, key=str):
        out.write(f"Unterhaltung {thread_id}\n")
        for record in (r for r in conversation if r.get("threadId") == thread_id):
            marker = "▶ " if any(record is m for m in matching) and len(matching) < len(conversation) else ""
            out.write(marker + _format_turn(record) + "\n")
    return 0


def cmd_queries(records: list[dict], out) -> int:
    writer = csv.writer(out)
    writer.writerow(
        ["startedAt", "threadId", "runId", "turn", "query", "resolvedQuery", "status", "durationS",
         "steps", "commands", "planFinishReason", "answer"]
    )
    turn_in_thread: dict[str, int] = {}
    for record in records:
        thread_id = record.get("threadId") or ""
        turn_in_thread[thread_id] = turn_in_thread.get(thread_id, 0) + 1
        request = record.get("request") or {}
        summary = record.get("summary") or {}
        writer.writerow(
            [
                record.get("startedAt"),
                thread_id,
                record.get("runId"),
                turn_in_thread[thread_id],
                _text(request.get("query")),
                _text(request.get("resolvedQuery")),
                record.get("status"),
                round((record.get("durationMs") or 0) / 1000, 1),
                " ".join(summary.get("steps") or []),
                " ".join(c or "" for c in summary.get("commands") or []),
                summary.get("planFinishReason"),
                _text(record.get("answer")),
            ]
        )
    return 0


def main(argv: list[str] | None = None, out=None) -> int:
    out = out or sys.stdout
    parser = argparse.ArgumentParser(prog="python -m cwym_agent.traces", description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--file",
        action="append",
        type=Path,
        help="trace JSONL / log file to read (repeatable); default: $CWYM_TRACE_FILE and its rotated files",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    show = sub.add_parser("show", help="show the conversation of a threadId/runId (prefix)")
    show.add_argument("id")
    show.add_argument("--json", action="store_true", help="print the raw trace records")
    sub.add_parser("queries", help="CSV of all turns (query, outcome, answer)")
    args = parser.parse_args(argv)

    if args.file:
        files = args.file
    else:
        default = os.environ.get("CWYM_TRACE_FILE")
        if not default:
            parser.error("no --file given and CWYM_TRACE_FILE is not set")
        files = _trace_files(Path(default))
    missing = [f for f in files if not f.exists()]
    if missing:
        parser.error(f"not found: {', '.join(map(str, missing))}")

    records = load(files)
    if args.command == "show":
        return cmd_show(records, args.id, args.json, out)
    return cmd_queries(records, out)


if __name__ == "__main__":
    sys.exit(main())
