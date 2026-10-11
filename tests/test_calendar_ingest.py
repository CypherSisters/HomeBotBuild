"""Tests for puffpai.calendar_ingest candidate cleaning (_clean_event).

A schedule suggestion re-serves these candidates on every /pending poll, so an
unbounded field (e.g. a garbled document's location) can balloon the payload.
"""

import os
import sys

_src_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "src")
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

from puffpai.calendar_ingest import _clean_event

_BASE = {'title': 'Practice', 'start_iso': '2026-09-01T17:00:00'}


class TestCleanEventLocationCap:
    def test_huge_location_is_capped(self):
        ev = _clean_event({**_BASE, 'location': 'L' * 100_000})
        assert len(ev['location']) <= 200

    def test_normal_location_is_preserved(self):
        ev = _clean_event({**_BASE, 'location': 'Main St Gym, Room 4'})
        assert ev['location'] == 'Main St Gym, Room 4'

    def test_empty_location_is_none(self):
        assert _clean_event({**_BASE, 'location': '   '})['location'] is None

    def test_title_stays_capped(self):
        assert len(_clean_event({**_BASE, 'title': 'T' * 100_000})['title']) <= 200
