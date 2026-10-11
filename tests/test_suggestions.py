"""Tests for the proactive suggestion engine (puffpai.suggestions) and its
/knowledge/suggestions routes.

No test here talks to a real LLM: llm_chat is always a stub.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
from unittest.mock import MagicMock, patch

from tests.modpatch import patched_modules

_src_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "src", "puffpai")
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

from puffpai import suggestions as sug


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """Point the suggestion store at a throwaway file."""
    monkeypatch.setattr(sug, '_STORE_PATH', str(tmp_path / 'suggestions.json'))
    return tmp_path / 'suggestions.json'


def _llm(reply):
    """Stub llm_chat returning a canned string."""
    return lambda messages: reply


# ===========================================================================
# Classifier
# ===========================================================================

class TestClassifyDocument:
    def test_parses_clean_json(self):
        out = sug.classify_document(
            'Practice every Tuesday', 'fall.pdf',
            llm_chat=_llm('{"kind": "schedule", "confidence": 0.9, '
                          '"summary": "Fall class schedule"}'))
        assert out == {'kind': 'schedule', 'confidence': 0.9,
                       'summary': 'Fall class schedule'}

    def test_parses_fenced_json_with_prose(self):
        reply = ('Sure! Here you go:\n```json\n'
                 '{"kind": "manual", "confidence": 0.7, "summary": "Dryer manual"}\n'
                 '```\nHope that helps.')
        out = sug.classify_document('text', 'dryer.pdf', llm_chat=_llm(reply))
        assert out['kind'] == 'manual'
        assert out['summary'] == 'Dryer manual'

    def test_unknown_kind_falls_back_to_general(self):
        out = sug.classify_document(
            'text', 'f.pdf',
            llm_chat=_llm('{"kind": "tax_return", "confidence": 0.9, "summary": "x"}'))
        assert out['kind'] == 'general'

    def test_garbage_reply_is_general(self):
        out = sug.classify_document('text', 'f.pdf', llm_chat=_llm('I am a teapot'))
        assert out == {'kind': 'general', 'confidence': 0.0, 'summary': ''}

    def test_malformed_json_is_general(self):
        out = sug.classify_document('text', 'f.pdf',
                                    llm_chat=_llm('{"kind": "schedule",,,}'))
        assert out['kind'] == 'general'

    def test_llm_exception_does_not_raise(self):
        def boom(messages):
            raise RuntimeError('ollama timed out')
        out = sug.classify_document('text', 'f.pdf', llm_chat=boom)
        assert out['kind'] == 'general'
        assert out['confidence'] == 0.0

    def test_no_llm_available(self):
        assert sug.classify_document('text', 'f.pdf', llm_chat=None)['kind'] == 'general'

    def test_empty_document(self):
        assert sug.classify_document('   ', 'f.pdf',
                                     llm_chat=_llm('{"kind":"schedule"}'))['kind'] == 'general'

    def test_confidence_is_clamped_and_coerced(self):
        out = sug.classify_document(
            'text', 'f.pdf',
            llm_chat=_llm('{"kind": "recipe", "confidence": 7, "summary": 1}'))
        assert out['confidence'] == 1.0
        out = sug.classify_document(
            'text', 'f.pdf',
            llm_chat=_llm('{"kind": "recipe", "confidence": "high"}'))
        assert out['confidence'] == 0.0

    @pytest.mark.parametrize('conf', [
        '"NaN"', '"Infinity"', '"-Infinity"', 'NaN', 'Infinity',
        '1e400', '999999', '-5', '"high"', 'null',
    ])
    def test_confidence_is_always_finite_in_unit_range(self, conf):
        """A poisoned confidence (NaN/Inf, huge, negative, non-numeric) must
        never survive as a non-finite float — that token kills the whole
        pending queue on the wire."""
        import math
        out = sug.classify_document(
            'text', 'f.pdf',
            llm_chat=_llm('{"kind": "recipe", "confidence": %s}' % conf))
        assert math.isfinite(out['confidence'])
        assert 0.0 <= out['confidence'] <= 1.0


# ===========================================================================
# Store lifecycle
# ===========================================================================

_EVENT = {'title': 'Practice', 'start_iso': '2026-09-01T17:00:00',
          'end_iso': '2026-09-01T18:00:00', 'cat': 'sports',
          'location': None, 'recurrence': None}


class TestStoreLifecycle:
    def test_queue_then_list_pending(self, store):
        rec = sug.queue_suggestion('Dryer Manual', 'How to unclog the lint trap',
                                   collection='dryer_manual',
                                   llm_chat=_llm('{"kind":"manual","confidence":0.8,'
                                                 '"summary":"Dryer manual"}'))
        assert rec['status'] == 'pending'
        assert rec['kind'] == 'manual'
        assert rec['collection'] == 'dryer_manual'
        assert rec['zone'] == 'knowledge'
        assert rec['action'] == {'type': 'none', 'params': {}}
        assert rec['message'] == ('I filed Dryer Manual in the library — '
                                  'ask me anything from it.')
        pending = sug.list_pending()
        assert [p['id'] for p in pending] == [rec['id']]

    def test_expires_at_is_48h_after_created(self, store):
        rec = sug.queue_suggestion('doc', 'text', llm_chat=_llm('nope'))
        created = datetime.fromisoformat(rec['created_at'])
        expires = datetime.fromisoformat(rec['expires_at'])
        assert expires - created == timedelta(hours=48)

    def test_accept_moves_to_accepted_and_leaves_pending_list(self, store):
        rec = sug.queue_suggestion('doc', 'text', llm_chat=_llm('nope'))
        got, err = sug.accept(rec['id'])
        assert err is None
        assert got['status'] == 'accepted'
        assert sug.list_pending() == []

    def test_dismiss(self, store):
        rec = sug.queue_suggestion('doc', 'text', llm_chat=_llm('nope'))
        got, err = sug.dismiss(rec['id'])
        assert err is None and got['status'] == 'dismissed'
        assert sug.list_pending() == []

    def test_accept_twice_is_not_pending(self, store):
        rec = sug.queue_suggestion('doc', 'text', llm_chat=_llm('nope'))
        sug.accept(rec['id'])
        got, err = sug.accept(rec['id'])
        assert err == 'not_pending'
        assert got['status'] == 'accepted'

    def test_unknown_id(self, store):
        got, err = sug.accept('no-such-id')
        assert got is None and err == 'not_found'

    def test_lazy_expiry_on_read(self, store, monkeypatch):
        rec = sug.queue_suggestion('doc', 'text', llm_chat=_llm('nope'))
        # Jump the clock past the TTL — the next read must expire it.
        later = datetime.now(timezone.utc) + timedelta(hours=49)
        monkeypatch.setattr(sug, '_utcnow', lambda: later)
        assert sug.list_pending() == []
        assert sug.get(rec['id'])['status'] == 'expired'
        # Expiry is persisted, not recomputed per read.
        monkeypatch.undo()
        with open(store, encoding='utf-8') as f:
            assert json.load(f)[0]['status'] == 'expired'

    def test_expired_suggestion_cannot_be_accepted(self, store, monkeypatch):
        rec = sug.queue_suggestion('doc', 'text', llm_chat=_llm('nope'))
        monkeypatch.setattr(sug, '_utcnow',
                            lambda: datetime.now(timezone.utc) + timedelta(hours=49))
        got, err = sug.accept(rec['id'])
        assert err == 'not_pending' and got['status'] == 'expired'

    def test_corrupt_store_file_is_tolerated(self, store):
        store.write_text('{not json', encoding='utf-8')
        assert sug.list_pending() == []
        rec = sug.queue_suggestion('doc', 'text', llm_chat=_llm('nope'))
        assert len(sug.list_pending()) == 1 and rec['status'] == 'pending'

    def test_async_queue_lands_in_store(self, store):
        t = sug.queue_suggestion_async(
            'doc', 'text', llm_chat=_llm('{"kind":"recipe","confidence":0.5,'
                                         '"summary":"Chili"}'))
        t.join(timeout=5)
        pending = sug.list_pending()
        assert len(pending) == 1 and pending[0]['kind'] == 'recipe'

    def test_async_queue_swallows_failure(self, store, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError('store on fire')
        monkeypatch.setattr(sug, 'build_suggestion', boom)
        t = sug.queue_suggestion_async('doc', 'text', llm_chat=_llm('nope'))
        t.join(timeout=5)
        assert sug.list_pending() == []

    def test_poisoned_confidence_record_round_trips_strict_json(self, store):
        """A NaN confidence from the LLM must not land as a non-finite float:
        the stored record has to survive json.dumps(allow_nan=False), or the
        whole queue breaks in the browser."""
        rec = sug.queue_suggestion(
            'doc', 'text',
            llm_chat=_llm('{"kind":"recipe","confidence":"NaN","summary":"x"}'))
        assert rec['confidence'] == 0.0
        json.dumps(rec, allow_nan=False)  # must not raise
        # And what actually persisted round-trips too.
        with open(store, encoding='utf-8') as f:
            stored = json.load(f)
        json.dumps(stored, allow_nan=False)

    def test_add_sanitizes_a_directly_poisoned_record(self, store):
        rec = sug.add({'id': 'x', 'confidence': float('inf'), 'status': 'pending',
                       'action': {'type': 'none', 'params': {}}})
        import math
        assert math.isfinite(rec['confidence'])
        json.dumps(sug.list_pending(), allow_nan=False)  # must not raise


# ===========================================================================
# Schedule kind — calendar candidates
# ===========================================================================

class TestScheduleSuggestions:
    def test_schedule_carries_candidates(self, store, monkeypatch):
        monkeypatch.setattr(
            'puffpai.calendar_ingest.ingest_text',
            lambda filename, text, llm_chat=None: {
                'method': 'llm', 'events': [_EVENT, dict(_EVENT)],
                'notes': ['Events below were read by the AI.']})
        rec = sug.queue_suggestion(
            'Fall class schedule', 'practice text',
            llm_chat=_llm('{"kind":"schedule","confidence":0.95,"summary":"Practices"}'))
        assert rec['kind'] == 'schedule'
        assert rec['action']['type'] == 'calendar_import'
        assert len(rec['action']['params']['candidates']) == 2
        assert rec['action']['params']['method'] == 'llm'
        assert rec['message'] == ('You sent me Fall class schedule — I found '
                                  '2 events. Want me to add them to your calendar?')

    def test_single_event_is_singular(self, store, monkeypatch):
        monkeypatch.setattr('puffpai.calendar_ingest.ingest_text',
                            lambda filename, text, llm_chat=None: {
                                'method': 'ics', 'events': [_EVENT], 'notes': []})
        rec = sug.queue_suggestion('game.ics', 'x',
                                   llm_chat=_llm('{"kind":"schedule","confidence":1}'))
        assert 'I found 1 event.' in rec['message']

    def test_zero_events_downgrades_to_general(self, store, monkeypatch):
        monkeypatch.setattr('puffpai.calendar_ingest.ingest_text',
                            lambda filename, text, llm_chat=None: {
                                'method': 'llm', 'events': [], 'notes': ['none found']})
        rec = sug.queue_suggestion('Maybe a schedule.pdf', 'x',
                                   llm_chat=_llm('{"kind":"schedule","confidence":0.6}'))
        assert rec['kind'] == 'general'
        assert rec['action'] == {'type': 'none', 'params': {}}
        assert 'calendar' not in rec['message']

    def test_extraction_failure_downgrades(self, store, monkeypatch):
        def boom(filename, text, llm_chat=None):
            raise RuntimeError('extractor died')
        monkeypatch.setattr('puffpai.calendar_ingest.ingest_text', boom)
        rec = sug.queue_suggestion('sched.pdf', 'x',
                                   llm_chat=_llm('{"kind":"schedule","confidence":0.9}'))
        assert rec['kind'] == 'general'
        assert rec['action']['type'] == 'none'


# ===========================================================================
# Recipe kind — FreshTable candidates
# ===========================================================================

_RECIPE = {'name': 'Pancakes', 'ingredients': '2 cups flour, 2 eggs, 1 cup milk',
           'instructions': 'Mix and cook.', 'yield': '4 servings',
           'prep_time': '15 minutes'}


class TestRecipeSuggestions:
    def test_recipe_carries_candidates(self, store, monkeypatch):
        monkeypatch.setattr(
            'puffpai.recipe_ingest.ingest_text',
            lambda filename, text, llm_chat=None: {
                'method': 'llm', 'recipes': [_RECIPE, dict(_RECIPE)],
                'notes': ['These were read by the AI.']})
        rec = sug.queue_suggestion(
            'grandmas card.jpg', 'recipe text',
            llm_chat=_llm('{"kind":"recipe","confidence":0.95,"summary":"Pancakes"}'))
        assert rec['kind'] == 'recipe'
        assert rec['action']['type'] == 'recipe_import'
        assert len(rec['action']['params']['recipes']) == 2
        assert rec['action']['params']['method'] == 'llm'
        assert rec['message'] == ('You sent me grandmas card.jpg — I found '
                                  '2 recipes. Want me to save them to FreshTable?')

    def test_single_recipe_is_singular(self, store, monkeypatch):
        monkeypatch.setattr('puffpai.recipe_ingest.ingest_text',
                            lambda filename, text, llm_chat=None: {
                                'method': 'llm', 'recipes': [_RECIPE], 'notes': []})
        rec = sug.queue_suggestion('soup.pdf', 'x',
                                   llm_chat=_llm('{"kind":"recipe","confidence":1}'))
        assert 'I found 1 recipe.' in rec['message']
        assert 'save it to FreshTable' in rec['message']

    def test_zero_recipes_downgrades_to_generic_recipe(self, store, monkeypatch):
        monkeypatch.setattr('puffpai.recipe_ingest.ingest_text',
                            lambda filename, text, llm_chat=None: {
                                'method': 'llm', 'recipes': [], 'notes': ['none']})
        rec = sug.queue_suggestion('maybe recipe.pdf', 'x',
                                   llm_chat=_llm('{"kind":"recipe","confidence":0.6}'))
        # Still a recipe (searchable in the library), but no import action.
        assert rec['kind'] == 'recipe'
        assert rec['action'] == {'type': 'none', 'params': {}}
        assert 'searchable' in rec['message']

    def test_extraction_failure_downgrades(self, store, monkeypatch):
        def boom(filename, text, llm_chat=None):
            raise RuntimeError('extractor died')
        monkeypatch.setattr('puffpai.recipe_ingest.ingest_text', boom)
        rec = sug.queue_suggestion('recipe.pdf', 'x',
                                   llm_chat=_llm('{"kind":"recipe","confidence":0.9}'))
        assert rec['kind'] == 'recipe'
        assert rec['action']['type'] == 'none'


# ===========================================================================
# Routes
# ===========================================================================

@pytest.fixture(scope="module")
def api_app():
    mock_fw = MagicMock()
    mock_fw.get_status.return_value = {"lockdown": False, "blocked_ips": []}
    with patched_modules(
            {"firewall_manager": MagicMock(FirewallManager=lambda: mock_fw)}):
        import importlib
        if "api_server" in sys.modules:
            mod = importlib.reload(sys.modules["api_server"])
        else:
            mod = importlib.import_module("api_server")
        mod.app.config['TESTING'] = True
        yield mod.app, mod._API_KEY, mod


@pytest.fixture()
def client(api_app):
    app, api_key, mod = api_app
    mod._rate_limit_store.clear()
    with app.test_client() as c:
        yield c, {'X-API-Key': api_key}, mod


class TestSuggestionRoutes:
    def test_requires_api_key(self, client, store):
        c, _headers, _ = client
        assert c.get('/knowledge/suggestions/pending').status_code == 401
        assert c.post('/knowledge/suggestions/x/accept').status_code == 401
        assert c.post('/knowledge/suggestions/x/dismiss').status_code == 401

    def test_pending_lists_only_pending(self, client, store, monkeypatch):
        c, headers, _ = client
        monkeypatch.setattr('puffpai.calendar_ingest.ingest_text',
                            lambda filename, text, llm_chat=None: {
                                'method': 'ics', 'events': [_EVENT], 'notes': []})
        kept = sug.queue_suggestion('sched.ics', 'x',
                                    llm_chat=_llm('{"kind":"schedule","confidence":0.9}'))
        gone = sug.queue_suggestion('other.pdf', 'x', llm_chat=_llm('nope'))
        sug.dismiss(gone['id'])

        resp = c.get('/knowledge/suggestions/pending', headers=headers)
        assert resp.status_code == 200
        body = resp.get_json()
        assert [s['id'] for s in body['suggestions']] == [kept['id']]
        assert body['suggestions'][0]['action']['params']['candidates'] == [_EVENT]

    def test_accept_returns_full_record(self, client, store, monkeypatch):
        c, headers, _ = client
        monkeypatch.setattr('puffpai.calendar_ingest.ingest_text',
                            lambda filename, text, llm_chat=None: {
                                'method': 'ics', 'events': [_EVENT], 'notes': ['n']})
        rec = sug.queue_suggestion('sched.ics', 'x',
                                   llm_chat=_llm('{"kind":"schedule","confidence":0.9}'))
        resp = c.post(f"/knowledge/suggestions/{rec['id']}/accept", headers=headers)
        assert resp.status_code == 200
        got = resp.get_json()['suggestion']
        assert got['status'] == 'accepted'
        assert got['action']['type'] == 'calendar_import'
        assert got['action']['params']['candidates'] == [_EVENT]
        assert c.get('/knowledge/suggestions/pending',
                     headers=headers).get_json()['suggestions'] == []

    def test_dismiss_returns_ok(self, client, store):
        c, headers, _ = client
        rec = sug.queue_suggestion('doc', 'x', llm_chat=_llm('nope'))
        resp = c.post(f"/knowledge/suggestions/{rec['id']}/dismiss", headers=headers)
        assert resp.status_code == 200 and resp.get_json() == {'ok': True}

    def test_unknown_id_404(self, client, store):
        c, headers, _ = client
        for verb in ('accept', 'dismiss'):
            resp = c.post(f'/knowledge/suggestions/nope-{verb}/{verb}', headers=headers)
            assert resp.status_code == 404
            assert 'error' in resp.get_json()

    def test_already_resolved_409(self, client, store):
        c, headers, _ = client
        rec = sug.queue_suggestion('doc', 'x', llm_chat=_llm('nope'))
        c.post(f"/knowledge/suggestions/{rec['id']}/accept", headers=headers)
        resp = c.post(f"/knowledge/suggestions/{rec['id']}/dismiss", headers=headers)
        assert resp.status_code == 409
        assert resp.get_json()['status'] == 'accepted'


class TestUploadHook:
    """The /knowledge/upload success path must queue a suggestion."""

    @pytest.fixture()
    def upload_env(self, client, store, tmp_path, monkeypatch):
        c, headers, mod = client
        # Keep the manifest and retained-source copies out of the repo.
        monkeypatch.setattr(mod, '_SRC_DIR', str(tmp_path))
        monkeypatch.setattr(mod, '_LIBRARY_SOURCES_DIR', str(tmp_path / 'sources'))
        monkeypatch.setattr(mod, '_get_query_embedder',
                            lambda: (lambda chunks: [[0.1]] * len(chunks)))

        collection = MagicMock()
        collection.count.return_value = 99
        monkeypatch.setattr(mod, '_get_dual_zone_rag', lambda: MagicMock())
        monkeypatch.setattr('puffpai.ingestion.get_or_create_upload_collection',
                            lambda *a, **kw: collection)

        # Capture the async hand-off instead of spawning a thread.
        captured = {}
        monkeypatch.setattr(sug, 'queue_suggestion_async',
                            lambda **kw: captured.update(kw))
        return c, headers, captured

    def _post(self, c, headers, body=b'Practice is Tuesday at 5pm.'):
        import io as _io
        return c.post('/knowledge/upload', headers=headers,
                      content_type='multipart/form-data',
                      data={'file': (_io.BytesIO(body), 'practice.txt'),
                            'library': 'Fall class schedule',
                            'zone': 'knowledge'})

    def test_successful_upload_queues_suggestion(self, upload_env):
        c, headers, captured = upload_env
        resp = self._post(c, headers)
        assert resp.status_code == 200 and resp.get_json()['success'] is True
        assert captured['doc_name'] == 'Fall class schedule'
        assert captured['zone'] == 'knowledge'
        assert 'Practice is Tuesday' in captured['text']
        assert callable(captured['llm_chat'])

    def test_classifier_sees_redacted_text(self, upload_env, monkeypatch):
        """A vaulted doc is still indexed — and the REDACTED text is what the
        suggestion pass should classify."""
        c, headers, captured = upload_env
        from puffpai import ingestion as _ing
        monkeypatch.setattr(
            _ing, 'screen_text_for_pii',
            lambda text, filename, **kw: _ing.PiiScreenResult(
                text_for_index='[REDACTED] practice Tuesday',
                pii_detected=True, pii_vaulted=True, pii_types=['ssn']))
        assert self._post(c, headers).status_code == 200
        assert captured['text'] == '[REDACTED] practice Tuesday'

    def test_hook_failure_does_not_fail_the_upload(self, upload_env, monkeypatch):
        c, headers, _ = upload_env
        def boom(**kw):
            raise RuntimeError('classifier module missing')
        monkeypatch.setattr(sug, 'queue_suggestion_async', boom)
        resp = self._post(c, headers)
        assert resp.status_code == 200 and resp.get_json()['success'] is True


# ===========================================================================
# Chat tool surface
# ===========================================================================

class TestSuggestionTool:
    def _tool(self):
        # AssistantTools.__init__ builds an LLM backend and ~90 tools; the bodies
        # are closures over self, so binding create_tools to a stand-in with
        # just the bridge exercises the real tool. Same trick as
        # test_ha_bridge_confirmation_gate.
        from puffpai.assistant_tools import AssistantTools
        stand_in = type('_Stand', (), {'_ha_bridge': MagicMock()})()
        flat = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'src', 'puffpai')
        sys.path.insert(0, flat)
        try:
            made = AssistantTools.create_tools(stand_in)
        finally:
            sys.path.remove(flat)
        return next(t for t in made if t.name == 'list_pending_suggestions')

    def test_empty(self, store):
        assert 'Nothing waiting' in self._tool().invoke({'dummy': ''})

    def test_lists_schedule_with_event_count(self, store, monkeypatch):
        monkeypatch.setattr('puffpai.calendar_ingest.ingest_text',
                            lambda filename, text, llm_chat=None: {
                                'method': 'ics', 'events': [_EVENT], 'notes': []})
        sug.queue_suggestion('Fall class schedule', 'x',
                             llm_chat=_llm('{"kind":"schedule","confidence":0.9}'))
        out = self._tool().invoke({'dummy': ''})
        assert 'Fall class schedule' in out
        assert '1 calendar event(s) ready to review' in out

    def test_pathological_doc_name_is_capped_in_tool_output(self, store):
        """doc_name is uploader-controlled and flows into the surfaced message;
        a 5000-char name must not bloat the agent's tool context."""
        sug.queue_suggestion('A' * 5000, 'x', llm_chat=_llm('nope'))
        out = self._tool().invoke({'dummy': ''})
        assert 'A' * 5000 not in out
        # The single surfaced line is bounded (120-char message cap + framing).
        line = next(l for l in out.splitlines() if l.strip().startswith('- '))
        assert len(line) <= 130


# ── Store location: $PUFFPAI_DATA_DIR ─────────────────────────────────────
#
# _STORE_PATH must not be a module constant hardcoded to <repo>/app_data. The
# appliance mounts the code tree read-only (ProtectSystem=strict), so the store
# could never be written there — and _save() creates the parent and then writes,
# so the failure surfaced as an OSError out of an otherwise-successful upload.
# It now resolves lazily through puffpai.statedir.resolve_runtime_dir with the
# same default and env_subdir as puffpai.app_data, so suggestions.json stays
# beside the app data it is about. Cross-module coherence is pinned in
# tests/test_state_paths_appdata.py; these cover the module on its own.


class TestStoreLocation:
    """The store follows $PUFFPAI_DATA_DIR, and the override hook still wins."""

    @staticmethod
    def _reload(monkeypatch, env_value):
        import importlib
        if env_value is None:
            monkeypatch.delenv('PUFFPAI_DATA_DIR', raising=False)
        else:
            monkeypatch.setenv('PUFFPAI_DATA_DIR', str(env_value))
        importlib.reload(sug)
        return sug

    @pytest.fixture(autouse=True)
    def _restore_module(self, monkeypatch):
        """Undo the env change, then reload so no cached path leaks onward."""
        yield
        import importlib
        monkeypatch.undo()
        importlib.reload(sug)

    def test_unset_env_keeps_the_historical_in_repo_path(self, monkeypatch):
        """Byte-identical to the constant this replaced: <repo>/app_data/…."""
        mod = self._reload(monkeypatch, None)
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(mod.__file__))))
        assert mod._store_path() == os.path.join(
            repo_root, 'app_data', 'suggestions.json')

    def test_absolute_env_relocates_the_store(self, monkeypatch, tmp_path):
        mod = self._reload(monkeypatch, tmp_path)
        assert mod._store_path() == os.path.join(
            str(tmp_path), 'app_data', 'suggestions.json')

    def test_relative_env_joins_the_repo_root_not_the_cwd(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        mod = self._reload(monkeypatch, 'relstate')
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(mod.__file__))))
        assert mod._store_path() == os.path.join(
            repo_root, 'relstate', 'app_data', 'suggestions.json')

    def test_tilde_env_is_expanded(self, monkeypatch):
        mod = self._reload(monkeypatch, '~/puff_state')
        assert '~' not in mod._store_path()
        assert mod._store_path() == os.path.join(
            os.path.expanduser('~'), 'puff_state', 'app_data', 'suggestions.json')

    def test_whitespace_only_env_falls_back_to_the_default(self, monkeypatch):
        mod = self._reload(monkeypatch, '   ')
        assert mod._store_path() == os.path.join(
            mod._DEFAULT_DATA_DIR, 'suggestions.json')

    def test_store_fixture_style_override_still_wins(self, monkeypatch, tmp_path):
        """The `store` fixture above assigns _STORE_PATH; that must beat the env."""
        mod = self._reload(monkeypatch, tmp_path)
        pinned = tmp_path / 'pinned' / 'suggestions.json'
        monkeypatch.setattr(mod, '_STORE_PATH', str(pinned))

        sug.queue_suggestion('Doc', 'x', llm_chat=_llm('nope'))

        assert pinned.is_file()
        assert not (tmp_path / 'app_data' / 'suggestions.json').exists()

    def test_queue_and_read_round_trip_under_the_env_dir(self, monkeypatch, tmp_path):
        mod = self._reload(monkeypatch, tmp_path)
        sug.queue_suggestion('Fall class schedule', 'x',
                             llm_chat=_llm('{"kind":"manual","confidence":0.9}'))
        assert (tmp_path / 'app_data' / 'suggestions.json').is_file()
        assert mod._load(), 'the queued suggestion must read back from the same path'
