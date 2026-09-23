"""Opt-in end-to-end quality test for the real geodata services.

Skipped by default (set ``CWYM_LIVE_TESTS=1`` to run) - this exercises
the real Hamburg layer catalog fetch, real embedding model, real cross-
encoder reranker, and the real Qwen3 server, so it's slow (fetches and
embeds ~6,500 documents, a few minutes) and network-dependent. This is
the actual acceptance test for the feature though: pure text/embedding
similarity cannot bridge "Fahrradstationen" to "StadtRAD" (verified
empirically - see ``real_data_services.py``'s module docstring), so
this must be tested against the real model, not a mock.
"""

import os

import pytest

from cwym_agent.geodata.address_resolver import AddressResolver
from cwym_agent.geodata.real_data_services import RealDataServices

pytestmark = pytest.mark.skipif(
    os.environ.get("CWYM_LIVE_TESTS") != "1",
    reason="Set CWYM_LIVE_TESTS=1 to run tests against the real geodata services.",
)


@pytest.fixture(scope="module")
def real_data_services() -> RealDataServices:
    service = RealDataServices()
    service.warm_up()
    return service


def test_colloquial_bike_sharing_term_resolves_to_stadtrad(real_data_services: RealDataServices):
    result = real_data_services.resolve_layer("Fahrradstationen")

    assert result is not None
    assert "stadtrad" in result.name.lower()


def test_hospital_term_resolves_to_hamburg_hospitals_not_train_station(
    real_data_services: RealDataServices,
):
    result = real_data_services.resolve_layer("Krankenhäuser")

    assert result is not None
    assert "krankenh" in result.name.lower()


def test_kindergarten_term_resolves_to_hamburg_not_neighboring_state(
    real_data_services: RealDataServices,
):
    result = real_data_services.resolve_layer("Kindergärten")

    assert result is not None
    assert "niedersachsen" not in result.name.lower()
    assert "schleswig" not in result.name.lower()


def test_district_names_resolve_to_district_center_not_a_street():
    resolver = AddressResolver()

    wandsbek = resolver.resolve("Wandsbek")
    assert wandsbek is not None
    assert wandsbek.name == "Wandsbek"

    altona = resolver.resolve("Altona")
    assert altona is not None
    assert altona.name == "Altona"


def test_layer_scoping_excludes_layers_outside_allowed_ids(
    real_data_services: RealDataServices,
):
    """Section 8: scoping to a portal's own layer ids must actually
    restrict resolution, not just filter cosmetically - resolving with
    an allowed set that excludes the real answer should come back empty
    or with a different (also-allowed) layer, never the excluded one."""

    unscoped = real_data_services.resolve_layer("Fahrradstationen")
    assert unscoped is not None
    assert unscoped.layer_id == "27764"

    scoped_out = real_data_services.resolve_layer(
        "Fahrradstationen", allowed_layer_ids={"18105"}
    )
    assert scoped_out is None or scoped_out.layer_id != "27764"

    scoped_in = real_data_services.resolve_layer(
        "Fahrradstationen", allowed_layer_ids={"27764", "18105"}
    )
    assert scoped_in is not None
    assert scoped_in.layer_id == "27764"


def test_bare_hamburg_query_uses_city_center(real_data_services: RealDataServices):
    result = real_data_services.resolve_locations("Hamburg")

    assert result == [result[0]]
    assert result[0].name == "Hamburg"
    assert result[0].latitude == 53.5511
    assert result[0].longitude == 9.9937
