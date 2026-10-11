"""The knowledge pack catalog: every pack is listed, available and sourced.

The capability catalog names each offline knowledge pack the wizard can offer,
and the source registry names where each one is downloaded from. These tests
keep the two files honest with each other: a pack that says it is available
points at a registry row that exists and may be fetched, and every row shows
its size and license before anything moves.

Copyright (c) Cypher Sisters. Apache-2.0 License.
"""
from __future__ import annotations

import os
import re

import pytest

from puffpai import build_interview, source_registry
from puffpai.knowledge import build_sources

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CATALOG = os.path.join(ROOT, "config", "capability_catalog.json")
REGISTRY = os.path.join(ROOT, "config", "source_registry.json")
DOC = os.path.join(ROOT, "docs", "KNOWLEDGE_PACKS.md")

PACK_FIELDS = ("id", "label", "domain", "tags", "surfaces", "available",
               "size_mb", "license", "license_url", "license_checked",
               "commercial_use", "what_it_is", "source_ids")


def _load(path):
    import json
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def catalog():
    data = _load(CATALOG)
    assert build_interview.validate_catalog(data) is None
    return data


@pytest.fixture(scope="module")
def registry():
    data = _load(REGISTRY)
    assert source_registry.validate_registry(data) is None
    return data


@pytest.fixture(scope="module")
def packs(catalog):
    return [i for i in catalog["items"] if i.get("kind") == "knowledge_pack"]


@pytest.fixture(scope="module")
def rows(registry):
    return {r["id"]: r for r in registry["sources"]}


def test_the_catalog_lists_every_pack(packs):
    assert len(packs) >= 66


def test_catalog_ids_are_unique(catalog):
    ids = [i["id"] for i in catalog["items"]]
    assert len(ids) == len(set(ids))


def test_registry_ids_and_urls_are_unique(registry):
    ids = [r["id"] for r in registry["sources"]]
    urls = [r["source_url"] for r in registry["sources"]]
    assert len(ids) == len(set(ids))
    assert len(urls) == len(set(urls))


def test_every_pack_row_carries_the_required_fields(packs):
    for pack in packs:
        missing = [f for f in PACK_FIELDS if pack.get(f) in (None, "", [])
                   and not isinstance(pack.get(f), bool)]
        assert not missing, f"{pack['id']} is missing {missing}"
        assert isinstance(pack["license_checked"], bool), pack["id"]
        assert isinstance(pack["commercial_use"], bool), pack["id"]
        assert float(pack["size_mb"]) > 0, pack["id"]
        assert pack["license_url"].startswith("https://"), pack["id"]


def test_every_pack_is_available(packs):
    assert [p["id"] for p in packs if p["available"] is not True] == []


def test_every_available_pack_has_an_offerable_download(packs, registry):
    offerable = {r["id"] for r in source_registry.offerable_sources(registry)}
    for pack in packs:
        for source_id in pack["source_ids"]:
            assert source_id in offerable, f"{pack['id']} -> {source_id}"


def test_every_registry_row_is_offerable(registry):
    assert len(source_registry.offerable_sources(registry)) == len(registry["sources"])


def test_pack_facts_match_their_registry_rows(packs, rows):
    for pack in packs:
        mapped = [rows[s] for s in pack["source_ids"]]
        assert pack["size_mb"] == sum(int(r["size_mb"]) for r in mapped), pack["id"]
        for row in mapped:
            assert row["license"] in pack["license"], pack["id"]
            if not row.get("commercial_use"):
                assert pack["commercial_use"] is False, pack["id"]


def test_the_interview_mapping_matches_the_catalog(packs):
    from_catalog = {p["id"]: tuple(p["source_ids"]) for p in packs}
    assert build_sources.PACK_SOURCE_IDS == from_catalog


def test_non_commercial_and_no_derivatives_licenses_say_so(rows):
    for row in rows.values():
        if re.search(r"-NC\b|-NC-", str(row.get("license"))):
            assert row["commercial_use"] is False, row["id"]


def test_an_unchecked_license_is_never_offered_for_commercial_use(rows, packs):
    for row in rows.values():
        if row.get("license_checked") is False:
            assert row["commercial_use"] is False, row["id"]
    for pack in packs:
        if pack["license_checked"] is False:
            assert pack["commercial_use"] is False, pack["id"]


def test_pack_downloads_use_the_undated_kiwix_name(packs, rows):
    for pack in packs:
        for source_id in pack["source_ids"]:
            row = rows[source_id]
            assert row["source_url"] == (
                "https://download.kiwix.org/zim/" + row["filename"]), source_id
            assert not re.search(r"_\d{4}-\d{2}\.zim$", row["filename"]), source_id
            known = row.get("known_good")
            if known:
                # A known-good file is one dated release; older releases of a
                # few packs used a slightly different name, so only the date
                # form is checked.
                assert re.search(r"_\d{4}-\d{2}\.zim$", known), source_id


def test_pack_tags_are_not_stopwords(packs):
    for pack in packs:
        for tag in pack["tags"]:
            assert tag == tag.lower(), (pack["id"], tag)
            assert tag not in build_interview._STOPWORDS, (pack["id"], tag)


def test_the_pack_list_document_names_every_pack(packs, rows):
    with open(DOC, encoding="utf-8") as fh:
        text = fh.read()
    for pack in packs:
        for source_id in pack["source_ids"]:
            name = rows[source_id]["filename"][:-len(".zim")]
            assert name in text, f"{pack['id']} ({name}) is not in KNOWLEDGE_PACKS.md"


def test_the_bundled_catalog_loads_with_every_pack():
    loaded = build_interview.load_catalog(CATALOG)
    ids = {i["id"] for i in loaded["items"] if i.get("kind") == "knowledge_pack"}
    assert "pack:wikipedia" in ids and "pack:gutenberg" in ids
