"""No city-specific code in the core (CHAT-5, ADR-002).

Everything specific to one city lives in its profile
(``profiles/<city>/profile.yaml``) and its adapters
(``cwym_agent/geodata/adapters/<city>_*.py``). This test fails when
Hamburg - the city name, its constants or its service domains - shows up
anywhere else in ``src/``, including docstrings and comments, so the core
stays usable for the next city without a hunt for leftovers.

Allowed: the Hamburg adapters, the adapter registry that imports them,
and the stubs (a declared Hamburg test fixture). Lowercase identifiers
like the adapter names (``hamburg_addr_search``) and the default profile
name are references to those, not Hamburg-specific code, and don't match.
"""

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "cwym_agent"

CITY_PATTERN = re.compile(r"Hamburg|HAMBURG|hamburg\.de")

ALLOWED = (
    "geodata/adapters/hamburg_*.py",
    "geodata/adapters/__init__.py",
    "stubs/*.py",
)


def _allowed(path: Path) -> bool:
    relative = path.relative_to(SRC)
    return any(relative.match(pattern) for pattern in ALLOWED)


def test_no_hamburg_outside_its_profile_and_adapters():
    hits = [
        f"{path.relative_to(SRC)}:{number}: {line.strip()}"
        for path in sorted(SRC.rglob("*.py"))
        if not _allowed(path)
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if CITY_PATTERN.search(line)
    ]

    assert not hits, (
        "City-specific text outside the profile and the city's adapters - move it to "
        "profiles/hamburg/profile.yaml or a Hamburg adapter, or word it generically:\n" + "\n".join(hits)
    )


def test_the_guard_sees_the_adapters():
    """Guards the guard: it scans the right directory and the allowlist
    matches real files."""

    adapters = [p for p in SRC.rglob("*.py") if _allowed(p)]
    assert any(p.name == "hamburg_addr_search.py" for p in adapters)
    assert any(CITY_PATTERN.search(p.read_text(encoding="utf-8")) for p in adapters)
