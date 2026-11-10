"""Unit tests for ``cwym_agent.language``."""

import pytest

from cwym_agent.language import (
    _ENGLISH_MARKERS,
    _GERMAN_MARKERS,
    TurnLanguage,
    detect_language,
    normalize_language,
    resolve_language,
)


@pytest.mark.parametrize(
    "text, expected",
    [
        # CWYM's own queries
        ("Zeige mir Fahrradstationen in Wandsbek", "de"),
        ("Filtere Krankenhäuser in Wandsbek", "de"),
        ("Wo ist die Mönckebergstraße?", "de"),
        ("Kannst du mir nur die Gymnasien zeigen?", "de"),
        ("Öffne die Legende", "de"),
        ("zeig wieder alle", "de"),
        ("Wie viele StadtRAD-Stationen gibt es in Altona?", "de"),
        ("Show me bike stations in Wandsbek", "en"),
        ("Where are the hospitals in Altona?", "en"),
        ("Open the legend", "en"),
        ("How many schools are there in Eimsbüttel?", "en"),
        ("Filter hospitals with more than 100 beds", "en"),
        ("Remove the filter", "en"),
        ("Danke", "de"),
        ("Thanks", "en"),
        ("Und dort?", "de"),
        # ASCEND's misdetections, fixed
        ("An der Hasenleite 5", "de"),
        ("Who owns Münchner Wohnen?", "en"),
        ("Zoom zum Projektgebiet", "de"),
        ("also show PV", "en"),
        # a tie between counted words: the umlaut decides
        ("Show me die Gebäude mit PV", "de"),
        ("Tell me about Hugo-Wolf-Straße 57", "en"),
    ],
)
def test_detects_german_and_english(text, expected):
    assert detect_language(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "", "ok", "Altona?", "27764", "Hoheluft-West", "West", "in 1960",
        "Mönckebergstraße", "Highlight Fernwärme", "Hallo Welt Hello World",
        # other languages are not forced into German or English
        "Mostra gli edifici a Harthof",
        "No hay edificios",
        "Bonjour, quels bâtiments du quartier?",
    ],
)
def test_inconclusive_messages(text):
    assert detect_language(text) is None


def test_marker_lists_share_no_word():
    assert not _GERMAN_MARKERS & _ENGLISH_MARKERS


@pytest.mark.parametrize(
    "code, expected",
    [("en", "en"), ("en-US", "en"), ("DE", "de"), ("de_CH", "de"), ("platt", None), ("", None), (None, None)],
)
def test_normalize_language(code, expected):
    assert normalize_language(code) == expected


class TestResolveLanguage:
    BOTH = ["de", "en"]

    def test_the_message_decides(self):
        assert resolve_language("Show me hospitals", [], "de", self.BOTH) == TurnLanguage("en", "detected")
        assert resolve_language("Zeig mir Krankenhäuser", [], "en", self.BOTH) == TurnLanguage("de", "detected")

    def test_inconclusive_message_follows_the_users_last_conclusive_message(self):
        history = [
            ("user", "Zeig mir Schulen"),
            ("assistant", "Ich zeige dir die Schulen."),
            ("user", "Show me hospitals in Altona"),
            ("assistant", "Here are the hospitals."),
            ("user", "ok"),
            ("assistant", "..."),
        ]

        assert resolve_language("Wandsbek?", history, "de", self.BOTH) == TurnLanguage("en", "history")

    def test_assistant_messages_do_not_count(self):
        history = [("assistant", "Here are the hospitals in Altona.")]

        assert resolve_language("27764", history, None, self.BOTH) == TurnLanguage("de", "default")

    def test_then_the_ui_language(self):
        assert resolve_language("Altona?", [("user", "ok")], "en-GB", self.BOTH) == TurnLanguage("en", "ui")

    def test_then_the_profile_default(self):
        assert resolve_language("Altona?", [], "platt", self.BOTH) == TurnLanguage("de", "default")
        assert resolve_language("Altona?", [], None, ["en", "de"]) == TurnLanguage("en", "default")

    def test_only_the_profiles_languages_count(self):
        german_only = ["de"]

        assert resolve_language("Show me hospitals", [("user", "Show me schools")], "en", german_only) == (
            TurnLanguage("de", "default")
        )
