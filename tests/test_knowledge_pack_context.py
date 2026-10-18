"""Downloaded books reach chat answers, labelled with the book's own name.

A book downloaded and indexed with `puffpai knowledge
sync` sat in the `puffpai_knowledge` collection, and the chat lookup read only
the collections listed in indexed_libraries.json (a file a fresh build does
not have) plus uploads. The bot answered from memory and cited nothing.
"""
from __future__ import annotations

import os
import tempfile

_TMP_DATA_DIR = tempfile.mkdtemp(prefix="puffpai_pack_context_test_")
os.environ.setdefault("PUFFPAI_DATA_DIR", _TMP_DATA_DIR)

import pytest  # noqa: E402

from puffpai.knowledge import pack_context  # noqa: E402


class FakeCollection:
    def __init__(self, name="puffpai_knowledge", docs=None, fail=False, space=None):
        self.name = name
        self.metadata = {"hnsw:space": space} if space else {"zone": name}
        self._docs = docs if docs is not None else [
            ("Stegosaurus was a herbivore, so they only ate plants.",
             {"title": "Stegosaurus", "source_id": "kiwix:vikidia_en_all:nopic",
              "source_label": "Vikidia (text only)", "source_license": "CC-BY-SA-3.0"},
             0.68)]
        self.fail = fail
        self.queries = []

    def count(self):
        return len(self._docs)

    def query(self, **kw):
        self.queries.append(kw)
        if self.fail:
            raise RuntimeError("store is broken")
        return {"documents": [[d for d, _m, _s in self._docs]],
                "metadatas": [[m for _d, m, _s in self._docs]],
                "distances": [[s for _d, _m, s in self._docs]]}


class FakeRag:
    def __init__(self, collection):
        self.knowledge_collection = collection


def _rel(distance, space):
    return 1 - distance / 2 if space == "l2" else 1 - distance


def test_a_downloaded_book_chunk_comes_back_labelled_with_its_name():
    hits = pack_context.pack_hits(FakeRag(FakeCollection()), "what did it eat",
                                  query_emb=[[0.1]], relevance=_rel)
    assert len(hits) == 1
    hit = hits[0]
    assert hit["library"] == "Vikidia (text only)"
    assert hit["source_id"] == "kiwix:vikidia_en_all:nopic"
    assert hit["title"] == "Stegosaurus" and hit["is_user_upload"] is False
    assert hit["relevance"] == pytest.approx(0.66)


def test_it_is_not_counted_twice_when_the_manifest_lists_it():
    assert pack_context.pack_hits(FakeRag(FakeCollection()), "q", relevance=_rel,
                                  listed=["puffpai_knowledge"]) == []


@pytest.mark.parametrize("collection", [
    FakeCollection(docs=[]), FakeCollection(fail=True), None])
def test_an_empty_or_broken_store_adds_nothing(collection):
    assert pack_context.pack_hits(FakeRag(collection), "q", relevance=_rel) == []


def test_an_unlabelled_chunk_says_so_rather_than_inventing_a_name():
    docs = [("text", {}, 0.5)]
    hit = pack_context.pack_hits(FakeRag(FakeCollection(docs=docs)), "q",
                                 relevance=_rel)[0]
    assert hit["library"] == pack_context.UNLABELLED

