"""The terminal, the browser and the chat say the same things about learning,
each in its own words, and none of them uses jargon (commands included)."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from learning_support import (ARTICLES, FakeRag, fake_embedder,  # noqa: E402
                              jargon_in, make_book)

from puffpai import cli  # noqa: E402
from puffpai.knowledge import learn, learning_api, progress, reading_helper  # noqa: E402
from puffpai.knowledge.auto_index import STATUS_COMPLETE, PackIndex  # noqa: E402

BOOK = "vikidia_en_all_nopic_2026_09a"
NAME = "Vikidia (text only)"


@pytest.fixture
def shelf(tmp_path, monkeypatch):
    fts = learn.books_dir()
    make_book(fts, BOOK, ARTICLES, labels={"book_name": NAME, "source_id": "kiwix:v",
                                           "source_label": NAME})
    zim = tmp_path / "vikidia.zim"
    zim.write_bytes(b"x")
    PackIndex().mark("u1", filename=zim.name, path=str(zim), book_id=BOOK, name=NAME,
                     status=STATUS_COMPLETE, quick="complete", meaning=learn.M_PENDING,
                     source_id="kiwix:v", docs=33)
    w = progress.Writer(BOOK, NAME)
    w.update(force=True, stage="not_started", quick_ready=True,
             meaning={"chars_total": 11_000_000})
    return {"zim": str(zim)}


def _run(capsys, *argv):
    code = cli.main(["knowledge", *argv])
    out = capsys.readouterr().out
    for line in out.splitlines():
        assert not jargon_in(line), line
    return code, out


# -- the routes --------------------------------------------------------------------------

@pytest.fixture
def web():
    from puffpai import api_server
    api_server.app.config.update(TESTING=True)
    return api_server.app.test_client(), {"Authorization": f"Bearer {api_server._API_KEY}"}


def test_the_ability_row_counts_a_quick_ready_book_as_learned(shelf):
    from puffpai.knowledge import find_more
    rows = find_more.ability_rows()
    assert rows[0]["status"] == "available"
