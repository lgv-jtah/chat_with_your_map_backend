"""AG-UI event bridge.

Haystack's ``Pipeline.run()`` is synchronous/blocking, so pipeline
components push AG-UI events into this thread-safe bridge, and the
FastAPI endpoint drains it asynchronously to build the SSE stream.

The bridge is a plain Python object - not a Haystack component - so it
requires no pipeline graph changes: components simply accept an optional
``bridge`` keyword argument at run time.
"""

import json
import queue
import uuid

from ag_ui.core import (
    ReasoningEndEvent,
    ReasoningMessageContentEvent,
    ReasoningStartEvent,
    RunErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    StateDeltaEvent,
    StateSnapshotEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
)

# Sentinel value pushed onto the queue to signal that no more events will
# follow, so the FastAPI endpoint knows when to stop draining.
_DONE = None


class AGUIEventBridge:
    """Thread-safe sink that pipeline components push AG-UI events into.

    Runs in the worker thread that executes the Haystack pipeline; the
    FastAPI endpoint drains it asynchronously from the event loop thread.
    """

    def __init__(self, thread_id: str, run_id: str):
        self.thread_id = thread_id
        self.run_id = run_id
        self._queue: queue.Queue = queue.Queue()

    def emit(self, event) -> None:
        self._queue.put(event)

    def drain(self):
        """Blocking get of the next event, or ``None`` when the run is done.

        Intended to be called from a worker thread (e.g. via
        ``loop.run_in_executor``), never directly from the event loop.
        """

        return self._queue.get()

    # -- run lifecycle -----------------------------------------------

    def run_started(self) -> None:
        self.emit(RunStartedEvent(thread_id=self.thread_id, run_id=self.run_id))

    def run_finished(self, result=None) -> None:
        self.emit(
            RunFinishedEvent(
                thread_id=self.thread_id,
                run_id=self.run_id,
                result=result,
            )
        )

    def run_error(self, message: str) -> None:
        self.emit(RunErrorEvent(message=message))

    def done(self) -> None:
        self._queue.put(_DONE)

    # -- reasoning trace ----------------------------------------------

    def reasoning(self, text: str) -> str:
        """Stream a full reasoning text as a start/content/end sequence.

        Returns the message id used, in case the caller needs it (e.g.
        for correlating with logs).
        """

        message_id = str(uuid.uuid4())
        self.emit(ReasoningStartEvent(message_id=message_id))
        self.emit(ReasoningMessageContentEvent(message_id=message_id, delta=text))
        self.emit(ReasoningEndEvent(message_id=message_id))
        return message_id

    # -- tool calls (sub-agent invocations) ----------------------------

    def tool_call(self, name: str, args: dict, result) -> str:
        """Emit a full start/args/end/result sequence for one sub-agent call.

        Returns the generated tool_call_id.
        """

        call_id = str(uuid.uuid4())
        self.emit(ToolCallStartEvent(tool_call_id=call_id, tool_call_name=name))
        self.emit(ToolCallArgsEvent(tool_call_id=call_id, delta=json.dumps(args)))
        self.emit(ToolCallEndEvent(tool_call_id=call_id))
        self.emit(
            ToolCallResultEvent(
                message_id=str(uuid.uuid4()),
                tool_call_id=call_id,
                content=json.dumps(result, default=str),
            )
        )
        return call_id

    # -- map state ------------------------------------------------------

    def state_snapshot(self, snapshot: dict) -> None:
        self.emit(StateSnapshotEvent(snapshot=snapshot))

    def state_delta(self, delta: list) -> None:
        """``delta`` is a JSON Patch (RFC 6902) list of operations."""

        self.emit(StateDeltaEvent(delta=delta))

    # -- final text response ---------------------------------------------

    def text_message(self, chunks) -> str:
        """Stream a full text response as a start/content*/end sequence.

        ``chunks`` is an iterable of string deltas. Returns the generated
        message id.
        """

        message_id = str(uuid.uuid4())
        self.emit(TextMessageStartEvent(message_id=message_id))
        for delta in chunks:
            self.emit(TextMessageContentEvent(message_id=message_id, delta=delta))
        self.emit(TextMessageEndEvent(message_id=message_id))
        return message_id
