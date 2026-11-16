"""Unit tests for ``WfsBoundaries`` with synthetic WFS responses."""

import httpx

from cwym_agent.geodata.adapters.wfs_boundaries import WfsBoundaries


def _square(x: float) -> dict:
    return {"type": "Polygon", "coordinates": [[[x, 0], [x + 1, 0], [x + 1, 1], [x, 1], [x, 0]]]}


def _feature(name: str | None, x: float) -> dict:
    return {"type": "Feature", "properties": {"n": name}, "geometry": _square(x)}


def _boundaries(features: list[dict], status: int = 200, prefixes=()) -> tuple[WfsBoundaries, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, json={"features": features})

    source = WfsBoundaries(
        "http://x/wfs", {"area": ("ns:areas", "n")}, httpx.Client(transport=httpx.MockTransport(handler)), prefixes
    )
    return source, requests


def test_matches_by_name_key():
    source, requests = _boundaries([_feature("Alt-Stadt", 0), _feature("Neustadt", 5)])

    assert source.outline("alt stadt", "area") == _square(0)
    params = requests[0].url.params
    assert (params["typeNames"], params["srsName"], params["outputFormat"]) == (
        "ns:areas",
        "EPSG:4326",
        "application/json",
    )


def test_strip_name_prefixes():
    source, _ = _boundaries([_feature("Nord", 0)], prefixes=("Stadt-",))

    assert source.outline("Stadt-Nord", "area") is not None


def test_same_name_features_are_merged():
    source, _ = _boundaries([_feature("Nord", 0), _feature("Nord", 5), _feature("Süd", 9)])

    outline = source.outline("Nord", "area")

    assert outline["type"] == "MultiPolygon"
    assert len(outline["coordinates"]) == 2


def test_unknown_kind_and_name_are_none():
    source, _ = _boundaries([_feature("Nord", 0)])

    assert source.outline("Nord", "other") is None
    assert source.outline("Süd", "area") is None


def test_failure_is_none_and_not_cached():
    source, requests = _boundaries([], status=503)

    assert source.outline("Nord", "area") is None
    assert source.outline("Nord", "area") is None
    assert len(requests) == 2


def test_outline_is_cached():
    source, requests = _boundaries([_feature("Nord", 0)])

    source.outline("Nord", "area")
    source.outline("Nord", "area")

    assert len(requests) == 1


def test_collection_outline_of_an_empty_collection_is_none():
    source, _ = _boundaries([])

    assert source.collection_outline("ns:areas") is None
