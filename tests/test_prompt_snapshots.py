"""Snapshots of every text the Hamburg deployment sends to the LLM.

CHAT-5 moves the city-specific parts of these texts (city name, example
places and layers, neighbouring regions, place kinds) from the code into
the city profile. For Hamburg the rendered texts must stay exactly the
same, otherwise run traces before and after the refactoring are not
comparable and the planner's behaviour may shift.

Each test renders one text with fixed inputs and compares it with
``tests/snapshots/prompts/<name>.txt``. After an intended prompt change,
regenerate the files and review the diff:

    CWYM_UPDATE_SNAPSHOTS=1 uv run pytest tests/test_prompt_snapshots.py
"""

import json
import os
from pathlib import Path

import pytest

from cwym_agent.city_profile import load_profile
from cwym_agent.components.master_agent import MasterAgentComponent, plan_request
from cwym_agent.components.text_response import TextResponseComponent
from cwym_agent.geodata import clarification
from cwym_agent.geodata.conversation_context import (
    Area,
    Condition,
    ConversationContext,
    FilterSpec,
    FilterStateIn,
    MapStateIn,
)
from cwym_agent.geodata.filter_agent import FilterAgent
from cwym_agent.geodata.portal_catalog_resolver import LayerCatalog
from cwym_agent.geodata.real_data_services import RealDataServices
from cwym_agent.geodata.types import ActionMatch, FilterResult, LayerMatch, LocationMatch
from cwym_agent.llm.qwen3_client import Qwen3Client
from cwym_agent.settings import Qwen3Settings

_PROFILE = load_profile("hamburg")
SNAPSHOT_DIR = Path(__file__).parent / "snapshots" / "prompts"
UPDATE = os.environ.get("CWYM_UPDATE_SNAPSHOTS") == "1"


def assert_snapshot(name: str, text: str) -> None:
    path = SNAPSHOT_DIR / f"{name}.txt"
    if UPDATE:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return
    if not path.is_file():
        pytest.fail(f"Snapshot {path} is missing - run with CWYM_UPDATE_SNAPSHOTS=1 to create it")
    assert text == path.read_text(encoding="utf-8"), (
        f"Rendered text differs from {path}. If the change is intended, regenerate with "
        "CWYM_UPDATE_SNAPSHOTS=1 and review the diff."
    )


class _RecordingLlm:
    """Records every prompt; answers with fixed replies."""

    def __init__(self, generate_reply: str = ""):
        self.generate_reply = generate_reply
        self.plan_prompts: list[str] = []
        self.generate_prompts: list[tuple[str, str]] = []
        self.system_prompts: list[tuple[str, str | None]] = []

    def plan(self, prompt: str, *, system_prompt, tools, streaming_callback=None) -> dict:
        self.plan_prompts.append(prompt)
        return {"steps": []}

    def generate(
        self, prompt: str, streaming_callback=None, max_tokens=None, label="generate", system_prompt=None, reasoning=True
    ) -> str:
        self.generate_prompts.append((label, prompt))
        self.system_prompts.append((label, system_prompt))
        return self.generate_reply


class _RecordingCompletions:
    def __init__(self):
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return iter(())


def _qwen3_with_recorder() -> tuple[Qwen3Client, _RecordingCompletions]:
    client = Qwen3Client(Qwen3Settings(base_url="http://test.invalid/v1", api_key="test"))
    recorder = _RecordingCompletions()
    client._client.chat.completions = recorder
    return client, recorder


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


_TWELVE_STATIONS = {
    "type": "FeatureCollection",
    "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [10.08, 53.57 + i / 1000]}, "properties": {}}
        for i in range(12)
    ],
}

_CATALOG = LayerCatalog(
    {
        "1732": LayerMatch("Schulen", "1732", 1.0),
        "27764": LayerMatch("StadtRAD-Stationen", "27764", 1.0),
    }
)

_FOLLOW_UP_CONTEXT = ConversationContext(
    map_state=MapStateIn(
        active_layer_ids=["1732", "27764"],
        filters=[
            FilterStateIn(
                layer_id="27764",
                filter_spec=FilterSpec(
                    conditions=[Condition(field="anzahl_raeder", operator="gte", value=10)],
                    area=Area(name="Wandsbek", kind="bezirk"),
                ),
            )
        ],
        last_location=Area(name="Eppendorf", kind="stadtteil"),
    ),
    history=[
        ("user", "Zeig mir Fahrradstationen in Wandsbek"),
        ("assistant", "Ich zeige dir die StadtRAD-Stationen in Wandsbek."),
    ],
)


class TestQwen3SystemPrompts:
    def test_plan_request(self):
        client, recorder = _qwen3_with_recorder()
        MasterAgentComponent(_PROFILE, llm=client).run("Zeig mir Schulen in Wandsbek")

        call = recorder.calls[0]
        assert_snapshot("plan_request", _dump({"messages": call["messages"], "tools": call["tools"]}))

    def test_generate_request(self):
        client, recorder = _qwen3_with_recorder()
        client.generate("PROMPT")

        assert_snapshot("generate_request", _dump({"messages": recorder.calls[0]["messages"]}))


class TestPlanningPrompt:
    def test_follow_up_with_map_state_and_history(self):
        llm = _RecordingLlm()
        MasterAgentComponent(_PROFILE, llm=llm).run(
            "Und wie sieht es dort mit Schulen aus?", context=_FOLLOW_UP_CONTEXT, layer_catalog=_CATALOG
        )

        assert_snapshot("planning_follow_up", llm.plan_prompts[0])


class TestResponsePrompts:
    _LOCATION = LocationMatch("Wandsbek", 53.5794, 10.0911, 0.9, kind="bezirk")
    _LAYER = LayerMatch("StadtRAD-Stationen", "27764", 0.9)

    def _render(self, aggregated: dict, commands: list, language: str | None = None, supported=None) -> str:
        llm = _RecordingLlm(generate_reply="Antwort")
        TextResponseComponent(_PROFILE, llm=llm).run(
            aggregated,
            "Zeig mir Fahrradstationen in Wandsbek",
            commands=commands,
            context=_FOLLOW_UP_CONTEXT,
            layer_catalog=_CATALOG,
            language=language,
            supported_commands=supported,
        )
        self.system_prompt = llm.system_prompts[0][1]
        return llm.generate_prompts[0][1]

    def test_success(self):
        filter_result = FilterResult(
            layer_id="27764",
            features=_TWELVE_STATIONS,
            number_matched=12,
            label="Wandsbek",
            area_kind="bezirk",
        )
        aggregated = {
            "locations": [self._LOCATION],
            "layer": self._LAYER,
            "action": ActionMatch("Zeigen", 0.85),
            "filter_result": filter_result,
        }
        prompt = self._render(aggregated, [{"type": "APPLY_FILTER", "payload": {"layerId": "27764"}}])

        assert_snapshot("response_success", prompt)

    def test_filter_failed(self):
        aggregated = {"locations": [self._LOCATION], "layer": self._LAYER, "filter_failed": True}
        prompt = self._render(aggregated, [{"type": "ACTIVATE_LAYER", "payload": {"layerId": "27764"}}])

        assert_snapshot("response_filter_failed", prompt)

    def test_no_commands(self):
        assert_snapshot("response_no_commands", self._render({}, []))


class _FakeLayerSearch:
    def search(self, query: str, allowed_layer_ids=None) -> list[LayerMatch]:
        return [
            LayerMatch("Fahrradluftstationen", "1", 0.9, "Luftpumpen für Fahrräder"),
            LayerMatch("StadtRAD-Stationen", "27764", 0.5, "Bike-Sharing"),
        ]


class _Unused:
    pass


class TestLayerResolutionPrompts:
    def test_query_expansion_and_layer_selection(self):
        llm = _RecordingLlm(generate_reply="1")
        services = RealDataServices(
            _PROFILE,
            locations=_Unused(),
            layer_search=_FakeLayerSearch(),
            filter_agent=_Unused(),
            llm=llm,
            udp_analysis=_Unused(),
        )
        services.resolve_layer("Fahrradstationen")

        prompts = dict(llm.generate_prompts)
        assert_snapshot("layer_query_expansion", prompts["layer_query_expansion"])
        assert_snapshot("layer_disambiguation", prompts["layer_disambiguation"])


class TestFilterPrompt:
    def test_condition_extraction(self):
        llm = _RecordingLlm(generate_reply="[]")
        agent = FilterAgent(
            _PROFILE.prompt.area_terms, llm=llm, queryables_cache=_Unused(), wfs_client=_Unused()
        )
        queryables = {
            "geom": {"x-ogc-role": "primary-geometry"},
            "name": {"type": "string"},
            "anzahl_betten": {"type": "integer"},
            "traeger": {"type": "string"},
        }
        agent._extract_conditions(
            "Krankenhäuser in Wandsbek mit mehr als 100 Betten",
            queryables,
            examples={"traeger": ["öffentlich", "freigemeinnützig", "privat"]},
        )

        assert_snapshot("filter_condition_extraction", llm.generate_prompts[0][1])


class TestFixedTexts:
    def test_clarification_question(self):
        assert_snapshot("clarification_question", clarification.question(["Altona", "Altona-Altstadt"], "de"))

    def test_analysis_failure_reply(self):
        assert_snapshot("analysis_failure_reply", TextResponseComponent._analysis_reply(_AnalysisWithoutAnswer(), "de"))


class _AnalysisWithoutAnswer:
    answer = None


class TestOtherCity:
    """Everything city-specific in the prompts comes from the profile: with
    another city's profile, none of Hamburg's wording is left."""

    _HAMBURG_WORDS = (
        "Hamburg", "Wandsbek", "Altona", "StadtRAD", "Bike-Sharing",
        "Niedersachsen", "Schleswig", "Stadtteil", "Bezirk",
    )

    @staticmethod
    def _profile():
        return _PROFILE.model_copy(
            update={
                "id": "teststadt",
                "name": "Teststadt",
                "official_name": "Freie Teststadt",
                "prompt": _PROFILE.prompt.model_copy(
                    update={
                        "city_genitive": "Teststadts",
                        "area_terms": ["Quartier", "Distrikt"],
                        "example_places": ["Nordviertel", "Südhafen"],
                        "neighbouring_regions": ["Umland"],
                        "layer_example": _PROFILE.prompt.layer_example.model_copy(
                            update={
                                "term": "Leihräder",
                                "layer_name": "RadLeih-Punkte",
                                "official_term": "RadLeih",
                                "official_term_reason": "das Teststädter Leihradsystem",
                            }
                        ),
                    }
                ),
                "analysis_agent": _PROFILE.analysis_agent.model_copy(
                    update={"example_question": "Wie viele RadLeih-Punkte gibt es im Nordviertel?"}
                ),
            }
        )

    def test_no_hamburg_wording_left(self):
        profile = self._profile()
        texts = []

        client, recorder = _qwen3_with_recorder()
        MasterAgentComponent(profile, llm=client).run("Zeig mir Spielplätze")
        texts.append(_dump({"messages": recorder.calls[0]["messages"], "tools": recorder.calls[0]["tools"]}))

        llm = _RecordingLlm(generate_reply="1")

        class Candidates:
            def search(self, query, allowed_layer_ids=None):
                return [LayerMatch("Spielplätze", "1", 0.9), LayerMatch("Bolzplätze", "2", 0.5)]

        RealDataServices(
            profile, locations=_Unused(), layer_search=Candidates(), filter_agent=_Unused(), llm=llm,
            udp_analysis=_Unused(),
        ).resolve_layer("Spielplätze")
        FilterAgent(profile.prompt.area_terms, llm=llm, queryables_cache=_Unused(), wfs_client=_Unused())._extract_conditions(
            "Spielplätze mit Rutsche", {"ausstattung": {"type": "string"}}
        )
        texts += [prompt for _, prompt in llm.generate_prompts]

        assert len(texts) == 4
        for text in texts:
            assert not [word for word in self._HAMBURG_WORDS if word in text], text
        joined = "\n".join(texts)
        for word in ("Teststadts", "Freie Teststadt", "Nordviertel", "Quartier, Distrikt", "Umland", "RadLeih"):
            assert word in joined

    def test_no_analysis_tool_without_analysis_agent(self):
        profile = self._profile().model_copy(update={"analysis_agent": None})

        names = [tool["function"]["name"] for tool in plan_request(profile, "de")["tools"]]

        assert "udp_analysis" not in names and "location_extractor" in names


class TestEnglish:
    """CHAT-7: an English answer. The prompts stay German; they name the
    query's/answer's language, and the reply prompt ends with a directive."""

    def test_plan_request(self):
        client, recorder = _qwen3_with_recorder()
        MasterAgentComponent(_PROFILE, llm=client).run("Show me schools in Wandsbek", language="en")

        call = recorder.calls[0]
        assert_snapshot("plan_request_en", _dump({"messages": call["messages"], "tools": call["tools"]}))

    def test_reply_prompts_and_system_prompt(self):
        responses = TestResponsePrompts()
        location = TestResponsePrompts._LOCATION
        layer = TestResponsePrompts._LAYER
        filter_result = FilterResult(
            layer_id="27764", features=_TWELVE_STATIONS, number_matched=12,
            label="Wandsbek", area_kind="bezirk",
        )
        success = {"locations": [location], "layer": layer, "action": ActionMatch("Zeigen", 0.85), "filter_result": filter_result}

        assert_snapshot("response_success_en", responses._render(success, [{"type": "APPLY_FILTER", "payload": {"layerId": "27764"}}], "en"))
        assert_snapshot("answer_system_prompt_en", responses.system_prompt)
        assert_snapshot(
            "response_filter_failed_en",
            responses._render({"locations": [location], "layer": layer, "filter_failed": True}, [{"type": "ACTIVATE_LAYER", "payload": {"layerId": "27764"}}], "en"),
        )
        assert_snapshot("response_no_commands_en", responses._render({}, [], "en"))

    def test_fixed_texts(self):
        assert_snapshot("clarification_question_en", clarification.question(["Altona", "Altona-Altstadt"], "en"))
        assert_snapshot("analysis_failure_reply_en", TextResponseComponent._analysis_reply(_AnalysisWithoutAnswer(), "en"))

    def test_german_reply_keeps_the_default_system_prompt(self):
        client, recorder = _qwen3_with_recorder()
        client.generate("PROMPT")
        default = recorder.calls[0]["messages"][0]["content"]

        responses = TestResponsePrompts()
        responses._render({}, [])
        assert responses.system_prompt == default
        responses._render({}, [], "de")
        assert responses.system_prompt == default


class TestAnalysisExtractionPrompt:
    """CHAT-11: the tier 1 extraction prompt for a declared dataset."""

    def test_rendered_for_the_fixture_dataset(self, tmp_path):
        from fixtures.datasets import buildings_profile

        from cwym_agent.geodata.tier1 import Tier1Analyzer, load_datasets

        profile = buildings_profile(tmp_path)
        llm = _RecordingLlm(generate_reply="{}")
        agent = FilterAgent(profile.prompt.area_terms, llm=llm, queryables_cache=_Unused(), wfs_client=_Unused())
        Tier1Analyzer(agent, load_datasets(profile.datasets), profile.name).analyze(
            "Wie verteilen sich die Energieklassen bei Wohnbau?", "distribution", None
        )

        assert_snapshot("analysis_extraction", dict(llm.generate_prompts)["analysis_extraction"])



class TestSuggestionsPrompt:
    """CHAT-14: clients with SHOW_SUGGESTIONS get the suggestions
    instruction - before the language directive, which stays last."""

    def test_reply_prompts(self):
        responses = TestResponsePrompts()
        aggregated = {"locations": [TestResponsePrompts._LOCATION], "layer": TestResponsePrompts._LAYER}
        commands = [{"type": "ACTIVATE_LAYER", "payload": {"layerIds": ["27764"]}}]
        supported = frozenset({"SHOW_SUGGESTIONS"})

        german = responses._render(aggregated, commands, "de", supported)
        english = responses._render(aggregated, commands, "en", supported)

        assert_snapshot("response_suggestions", german)
        assert_snapshot("response_suggestions_en", english)
        assert english.rstrip().endswith("keep that marker as it is.")


def test_suggestions_prompt_uses_the_profiles_places():
    profile = TestOtherCity._profile()
    llm = _RecordingLlm(generate_reply="Antwort")
    TextResponseComponent(profile, llm=llm).run(
        {"locations": [], "layer": LayerMatch("Spielplätze", "1", 0.9)}, "Zeig mir Spielplätze",
        commands=[{"type": "ACTIVATE_LAYER", "payload": {"layerIds": ["1"]}}],
        supported_commands=frozenset({"SHOW_SUGGESTIONS"}),
    )

    prompt = llm.generate_prompts[0][1]
    assert "Nordviertel" in prompt and "Südhafen" in prompt
    assert not [w for w in TestOtherCity._HAMBURG_WORDS if w in prompt]
