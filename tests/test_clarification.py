"""Unit tests for asking back for an ambiguous place and reading the answer."""

from cwym_agent.geodata.clarification import question, resolve_reply

_ASKED = question(["Hoheluft-Ost", "Hoheluft-West"])
_HISTORY = [("user", "Ich möchte mir alle Grundschulen in Hoheluft anschauen"), ("assistant", _ASKED)]


class TestQuestion:
    def test_wording(self):
        assert _ASKED == (
            "Den Ort gibt es mehrfach – meinst du Hoheluft-Ost oder Hoheluft-West? "
            "Die Karte habe ich noch nicht verändert."
        )
        assert question(["A-Nord", "A-Süd", "A-West"]).startswith("Den Ort gibt es mehrfach – meinst du A-Nord, A-Süd oder A-West?")


class TestResolveReply:
    def test_full_name_rebuilds_the_original_question(self):
        assert resolve_reply("Hoheluft-West", _HISTORY) == "Ich möchte mir alle Grundschulen in Hoheluft-West anschauen"

    def test_distinguishing_part_is_enough(self):
        assert resolve_reply("West", _HISTORY) == "Ich möchte mir alle Grundschulen in Hoheluft-West anschauen"
        assert resolve_reply("ich meine die Ost-Seite", _HISTORY) == "Ich möchte mir alle Grundschulen in Hoheluft-Ost anschauen"
        assert resolve_reply("hoheluft west bitte", _HISTORY).endswith("in Hoheluft-West anschauen")

    def test_three_options(self):
        history = [("user", "Kitas in Barmbek"), ("assistant", question(["Barmbek-Nord", "Barmbek-Süd", "Barmbek-Uhlenhorst"]))]

        assert resolve_reply("Süd", history) == "Kitas in Barmbek-Süd"

    def test_unclear_or_unrelated_reply_is_left_alone(self):
        assert resolve_reply("beide", _HISTORY) == "beide"
        assert resolve_reply("Zeige mir Krankenhäuser in Altona", _HISTORY) == "Zeige mir Krankenhäuser in Altona"

    def test_only_right_after_the_question(self):
        history = [*_HISTORY, ("user", "egal"), ("assistant", "Okay.")]

        assert resolve_reply("West", history) == "West"
        assert resolve_reply("West", []) == "West"

    def test_term_not_in_the_original_question(self):
        history = [("user", "Grundschulen dort"), ("assistant", _ASKED)]

        assert resolve_reply("West", history) == "Grundschulen dort (Hoheluft-West)"
