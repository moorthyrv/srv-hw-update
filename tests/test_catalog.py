import gzip
from datetime import date
from pathlib import Path

from fwtool.catalog.dell_catalog import (
    DellCatalog, catalog_paths, import_catalog, load_bundle_baselines, model_key, refresh_catalog,
)

FX = Path(__file__).parent / "fixtures" / "dell_catalog.xml"


def utf16_gz(tmp_path):
    """Real Dell catalogs are UTF-16 with a BOM, gzipped."""
    text = FX.read_text(encoding="utf-8").replace('encoding="utf-8"', 'encoding="utf-16"')
    p = tmp_path / "Catalog.xml.gz"
    with gzip.open(p, "wb") as fh:
        fh.write(text.encode("utf-16"))
    return p


def test_parse_utf16_gz_and_lookup(tmp_path):
    cat = DellCatalog.load([utf16_gz(tmp_path)])
    v = cat.versions("159", "0716", "PowerEdge R740")
    # drivers ignored, Windows/Linux duplicates collapsed, oldest first
    assert [x.version for x in v] == ["2.10.2", "2.12.2", "2.19.1"]
    assert v[-1].release_date == date(2023, 6, 1)


def test_lookup_by_system_id_or_model():
    cat = DellCatalog.load([FX])
    assert [x.version for x in cat.versions("25227", "0716", "")] == ["5.10.50.00", "7.00.00.173"]
    assert [x.version for x in cat.versions("25227", "", "PowerEdge R740")] == ["5.10.50.00", "7.00.00.173"]
    # VxRail model name does not match, but SystemID does
    assert cat.versions("25227", "0716", "VxRail P570F")
    assert [x.version for x in cat.versions("25227", "", "PowerEdge R630")] == ["2.40.40.40"]
    assert cat.versions("25227", "", "PowerEdge R999") == []
    assert cat.versions("", "0716", "") == []


def test_model_key():
    assert model_key("PowerEdge R650 XS") == "r650xs"
    assert model_key("Dell EMC PowerEdge R740xd") == "r740xd"


def test_bundle_baselines():
    b = load_bundle_baselines(FX)
    assert b["0716"] == {"159": "2.12.2", "25227": "7.00.00.173"}
    assert b["r740"] == b["0716"]


def test_import_and_archive(tmp_path):
    cache = tmp_path / "cache"
    import_catalog(FX, cache)  # plain XML gets gzipped
    assert (cache / "Catalog.xml.gz").exists()
    assert (cache / "archive" / "Catalog-26.09.01.xml.gz").exists()
    import_catalog(utf16_gz(tmp_path), cache)
    assert len(catalog_paths(cache)) == 1  # same catalog version archived once
    other = tmp_path / "other.xml"
    other.write_text(FX.read_text(encoding="utf-8").replace('version="26.09.01"', 'version="26.10.01"'), encoding="utf-8")
    import_catalog(other, cache)
    assert len(catalog_paths(cache)) == 2  # history grows with each new catalog version
    assert DellCatalog.load(catalog_paths(cache)).versions("159", "0716")


def test_refresh_uses_cache_when_fresh(tmp_path, monkeypatch):
    cache = tmp_path / "c"
    import_catalog(FX, cache)
    import requests

    def boom(*a, **k):
        raise AssertionError("should not download")

    monkeypatch.setattr(requests, "get", boom)
    assert refresh_catalog(cache, max_age_days=7) == cache / "Catalog.xml.gz"


def test_refresh_falls_back_to_cache_on_download_error(tmp_path, monkeypatch):
    cache = tmp_path / "c"
    import_catalog(FX, cache)
    import requests

    def fail(*a, **k):
        raise requests.exceptions.ConnectionError("proxy says no")

    monkeypatch.setattr(requests, "get", fail)
    assert refresh_catalog(cache, force=True) == cache / "Catalog.xml.gz"
    assert refresh_catalog(tmp_path / "empty", force=True) is None
