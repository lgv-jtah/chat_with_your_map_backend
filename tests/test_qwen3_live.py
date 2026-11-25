"""Opt-in end-to-end test against the real Qwen3/vLLM server.

Skipped by default so ``pytest`` stays fast, deterministic and
network-independent; set ``CWYM_LIVE_TESTS=1`` (and ensure the server at
``CWYM_QWEN3_BASE_URL``/the default address is reachable) to run it,
e.g. when validating a new model deployment or after changing the
prompts in ``Qwen3Client``.
"""

import os

import pytest

from cwym_agent.city_profile import load_profile
from cwym_agent.components.master_agent import plan_request
from cwym_agent.llm.qwen3_client import Qwen3Client
from cwym_agent.settings import Qwen3Settings

pytestmark = pytest.mark.skipif(
    os.environ.get("CWYM_LIVE_TESTS") != "1",
    reason="Set CWYM_LIVE_TESTS=1 to run tests against the real Qwen3/vLLM server.",
)


@pytest.fixture
def client() -> Qwen3Client:
    return Qwen3Client(Qwen3Settings())


def test_plan_resolves_location_and_layer_tools(client: Qwen3Client):
    reasoning_chunks: list[str] = []

    result = client.plan(
        "Zeige mir Fahrradstationen in Altona",
        **plan_request(load_profile("hamburg"), "de"),
        streaming_callback=lambda chunk: reasoning_chunks.append(chunk.content),
    )

    assert "".join(reasoning_chunks).strip() != ""
    agent_names = {step["agent_name"] for step in result["steps"]}
    assert agent_names, "expected at least one sub-agent to be called"
    # "<layer> in <area>" may also plan filter_agent - its tool description
    # asks for exactly that ("Schulen in Wandsbek").
    assert agent_names <= {"location_extractor", "layer_resolver", "action_detector", "filter_agent"}


def test_generate_produces_non_empty_german_text(client: Qwen3Client):
    content_chunks: list[str] = []

    result = client.generate(
        "Nutzeranfrage: \"Zeige mir Fahrradstationen in Altona\"\n"
        "Aufgelöste Informationen: Ort: Altona; Kartenebene: StadtRAD-Stationen Hamburg\n"
        "Formuliere eine kurze, natürliche deutsche Antwort, die dem "
        "Nutzer mitteilt, was auf der Karte angezeigt wird.",
        streaming_callback=lambda chunk: content_chunks.append(chunk.content),
    )

    assert result.strip() != ""
    assert "".join(content_chunks) == result


_ANALYSIS_QUESTIONS = [
    ("munich", "Wie verteilen sich die Energieklassen?"),
    ("munich", "Wie viele Gebäude gehören Münchner Wohnen?"),
    ("hamburg", "Wie viele Schulen in Altona?"),
    ("hamburg", "Wie viele Krankenhäuser gibt es?"),
]


@pytest.mark.parametrize(("city", "question"), _ANALYSIS_QUESTIONS)
def test_analysis_question_plans_filter_agent_with_operation(client: Qwen3Client, city: str, question: str):
    # Hamburg's "Schulen in Altona" may legitimately go to udp_analysis.
    allow_udp = (city, question) == ("hamburg", "Wie viele Schulen in Altona?")
    request = plan_request(load_profile(city), "de")
    plans: list[list[dict]] = []

    for _ in range(3):
        steps = client.plan(question, **request)["steps"]
        plans.append(steps)
        if any(s["agent_name"] == "filter_agent" and s.get("args", {}).get("operation") for s in steps):
            return
        if allow_udp and any(s["agent_name"] == "udp_analysis" for s in steps):
            return

    pytest.fail(
        "no filter_agent with operation in 3 attempts"
        + (" (udp_analysis would also be accepted for this question)" if allow_udp else "")
        + f": {plans}"
    )
