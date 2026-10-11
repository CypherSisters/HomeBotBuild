"""Renaming the agents, and saying honestly what else can be added.

Two things are pinned here:

1. Display names and presets can be written, not only read by
   ``agents/preferences.py`` -- otherwise "rename your agents" means
   hand-editing the JSON file the running service also reads -- and
   ``GET /agents/status`` carries a display name for its legacy rows, not bare
   ids.
2. The dashboard has an honest way to know what this machine can actually be
   extended with, so it does not offer skills and a shell tool on a machine
   where neither can run (no bubblewrap on Windows).

The invariant that matters for the first: renaming is COSMETIC. Internal ids are
what the bus routes on, what the Sentinel reviewer is wired by and what the
credential scopes spell, and no route here touches them.

Run: pytest tests/test_agent_rename_and_extend_api.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from puffpai import api_server as mod                       # noqa: E402
from puffpai.agents import preferences as prefs             # noqa: E402


@pytest.fixture()
def config(tmp_path, monkeypatch):
    """A throwaway puffpai_config.json, with a neighbouring key to protect."""
    path = tmp_path / "puffpai_config.json"
    path.write_text(json.dumps({
        "identity": {"name": "Puff"},
        "agents": {"planning_loop_enabled": True,
                   "egress_allowlist": ["example.com"]},
    }), encoding="utf-8")
    monkeypatch.setattr(prefs, "_config_path", lambda: str(path))
    prefs.reset_cache()
    yield path
    prefs.reset_cache()


@pytest.fixture()
def client(config):
    mod.app.config["TESTING"] = True
    mod._rate_limit_store.clear()
    with mod.app.test_client() as c:
        yield c, {"X-API-Key": mod._API_KEY}


def _agents(path):
    return json.loads(path.read_text(encoding="utf-8"))["agents"]


# ===========================================================================
# 1. GET /agents/names
# ===========================================================================

class TestNamesRead:

    def test_ids_names_and_presets_are_all_reported(self, client):
        c, headers = client
        body = c.get('/agents/names', headers=headers).get_json()
        assert body['success'] is True
        assert body['ids'] == list(prefs.AGENT_IDS)
        assert body['names']['sentinel'] == 'Drawbridge'
        assert 'default' in body['presets']
        assert body['max_name_length'] == prefs.MAX_SET_NAME_LEN
        assert [row['id'] for row in body['agents']] == list(prefs.AGENT_IDS)

    def test_it_needs_the_key(self, client):
        c, _ = client
        assert c.get('/agents/names').status_code == 401


# ===========================================================================
# 2. PUT /agents/names
# ===========================================================================

class TestNamesWrite:

    def test_one_name_is_stored_and_read_back(self, client, config):
        c, headers = client
        resp = c.put('/agents/names', headers=headers,
                     json={'names': {'caretaker': 'Housekeeper'}})
        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert resp.get_json()['names']['caretaker'] == 'Housekeeper'
        assert _agents(config)['names'] == {'caretaker': 'Housekeeper'}
        again = c.get('/agents/names', headers=headers).get_json()
        assert again['names']['caretaker'] == 'Housekeeper'
        # Every other agent keeps its word.
        assert again['names']['concierge'] == 'Concierge'

    def test_the_rest_of_the_config_survives_the_write(self, client, config):
        c, headers = client
        c.put('/agents/names', headers=headers,
              json={'names': {'archivist': 'Librarian'}})
        raw = json.loads(config.read_text(encoding="utf-8"))
        assert raw['identity'] == {'name': 'Puff'}
        assert raw['agents']['planning_loop_enabled'] is True
        assert raw['agents']['egress_allowlist'] == ['example.com']

    def test_a_preset_can_be_chosen(self, client, config):
        c, headers = client
        resp = c.put('/agents/names', headers=headers,
                     json={'preset': 'default'})
        assert resp.status_code == 200
        assert _agents(config)['name_preset'] == 'default'

    def test_null_clears_an_override(self, client, config):
        c, headers = client
        c.put('/agents/names', headers=headers,
              json={'names': {'caretaker': 'Housekeeper'}})
        resp = c.put('/agents/names', headers=headers,
                     json={'names': {'caretaker': None}})
        assert resp.status_code == 200
        assert resp.get_json()['names']['caretaker'] == 'Caretaker'
        assert _agents(config)['names'] == {}

    def test_names_are_trimmed_and_collapsed(self, client, config):
        c, headers = client
        resp = c.put('/agents/names', headers=headers,
                     json={'names': {'steward': '  House   Hand \t'}})
        assert resp.get_json()['names']['steward'] == 'House Hand'

    @pytest.mark.parametrize('bad,reason', [
        ('x' * 25, 'too long'),
        ('', 'empty'),
        ('   ', 'blank'),
        ('\x07\x07', 'control characters only'),
        (42, 'not text'),
        ('PuffPAI', 'the product'),
        ('puff', 'the product'),
        ('Admin', 'a privileged role'),
        ('root', 'a privileged role'),
    ])
    def test_a_refused_name_is_a_400_and_writes_nothing(self, client, config,
                                                        bad, reason):
        c, headers = client
        before = config.read_text(encoding="utf-8")
        resp = c.put('/agents/names', headers=headers,
                     json={'names': {'toolsmith': bad}})
        assert resp.status_code == 400, reason
        assert resp.get_json()['error']
        assert config.read_text(encoding="utf-8") == before

    def test_an_unknown_agent_id_is_refused(self, client, config):
        c, headers = client
        resp = c.put('/agents/names', headers=headers,
                     json={'names': {'butler': 'Jeeves'}})
        assert resp.status_code == 400
        assert 'butler' in resp.get_json()['error']
        assert 'names' not in _agents(config)

    def test_an_unknown_preset_is_refused(self, client):
        c, headers = client
        resp = c.put('/agents/names', headers=headers,
                     json={'preset': 'no_such_preset'})
        assert resp.status_code == 400

    @pytest.mark.parametrize('body', [{}, [1, 2], 'x', 5])
    def test_a_body_with_nothing_to_change_is_a_400_not_a_500(self, client,
                                                              body):
        c, headers = client
        resp = c.put('/agents/names', headers=headers, json=body)
        assert resp.status_code == 400

    def test_it_needs_the_key(self, client):
        c, _ = client
        assert c.put('/agents/names',
                     json={'names': {'steward': 'Hand'}}).status_code == 401


class TestRenamingIsCosmetic:

    def test_the_internal_ids_never_change(self, client):
        c, headers = client
        c.put('/agents/names', headers=headers,
              json={'names': {'sentinel': 'Portcullis',
                              'concierge': 'Front Desk'}})
        assert prefs.AGENT_IDS[2] == 'sentinel'
        assert prefs.display_names()['sentinel'] == 'Portcullis'
        from puffpai.agents.registry import REVIEWER_NAME, roster_summary
        assert REVIEWER_NAME == 'sentinel'
        assert {row['name'] for row in roster_summary()} >= set(prefs.AGENT_IDS)
        assert {row['display_name'] for row in roster_summary()} >= {
            'Portcullis', 'Front Desk'}


# ===========================================================================
# 3. /agents/status carries the display name
# ===========================================================================

class TestStatusCarriesDisplayNames:

    def test_every_row_has_a_display_name(self, client):
        c, headers = client
        c.put('/agents/names', headers=headers,
              json={'names': {'sentinel': 'Portcullis'}})
        body = c.get('/agents/status', headers=headers).get_json()
        for row in body['agents']:
            assert row['display_name'], row
        legacy = {row['agent']: row['display_name'] for row in body['agents']}
        assert legacy['sentinel'] == 'Portcullis'
        # An id that is not an agent keeps reading as itself, as before.
        assert legacy['cortex'] == 'cortex'
        for row in body.get('team') or []:
            assert row['display_name']


# ===========================================================================
# 4. GET /capabilities/extend
# ===========================================================================

class TestCapabilitiesExtend:

    def test_it_reports_what_can_and_cannot_work_here(self, client):
        c, headers = client
        body = c.get('/capabilities/extend', headers=headers).get_json()
        assert body['ok'] is True
        assert body['apps']['generic'] is True
        assert body['knowledge']['unified'] is True
        assert body['agents']['names_editable'] is True
        assert body['agents']['names_route'] == '/agents/names'
        assert isinstance(body['skills']['sandbox_available'], bool)
        assert isinstance(body['skills']['enabled'], bool)
        assert isinstance(body['workspace_exec']['available'], bool)

    def test_no_sandbox_helper_means_both_sandboxed_things_read_false(
            self, client, monkeypatch):
        """The Windows case, forced: no bubblewrap, so skills and the shell tool
        must report unavailable WITH a reason, never 'enabled' with a button
        that fails."""
        monkeypatch.setattr("puffpai.skills.sandbox.sandbox_available",
                            lambda: False)
        monkeypatch.setattr("puffpai.agents.toolsmith.BWRAP",
                            "/nonexistent/bwrap")
        c, headers = client
        body = c.get('/capabilities/extend', headers=headers).get_json()
        assert body['skills']['sandbox_available'] is False
        assert body['skills']['enabled'] is False
        assert body['skills']['reason']
        assert body['workspace_exec']['available'] is False
        assert body['workspace_exec']['reason']

    def test_a_broken_probe_reads_as_unavailable_not_as_available(
            self, client, monkeypatch):
        def _boom():
            raise RuntimeError("probe exploded")
        monkeypatch.setattr("puffpai.skills.dispatch.available", _boom)
        c, headers = client
        body = c.get('/capabilities/extend', headers=headers).get_json()
        assert body['skills']['enabled'] is False
        assert body['skills']['reason']

    def test_installed_apps_are_named(self, client):
        c, headers = client
        body = c.get('/capabilities/extend', headers=headers).get_json()
        assert 'calendar' in body['apps']['installed']

    def test_it_needs_the_key(self, client):
        c, _ = client
        assert c.get('/capabilities/extend').status_code == 401


# ===========================================================================
# 5. The module-level rules, directly
# ===========================================================================

class TestPreferenceRules:

    def test_reserved_names_are_refused_however_they_are_spelled(self):
        for spelling in ('PuffPAI', '  puffpai ', 'HOMEBOT', 'Root'):
            assert prefs.check_name(spelling) is not None, spelling

    def test_an_ordinary_name_is_accepted(self):
        assert prefs.check_name('Housekeeper') is None
        assert prefs.check_name('Rosie the Riveter') is None

    def test_set_names_without_a_config_path_refuses_rather_than_lying(
            self, monkeypatch):
        monkeypatch.setattr(prefs, "_config_path", lambda: None)
        prefs.reset_cache()
        with pytest.raises(prefs.NameRefused):
            prefs.set_names(names={'steward': 'Hand'})
