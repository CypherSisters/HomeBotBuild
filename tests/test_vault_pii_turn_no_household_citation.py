"""A PII turn must never cite a household upload.

``my ssn is 219-09-9999.`` gets the correct honesty notice (``vaulted: []``,
the "did not keep it" wording), but retrieval can still match
``patient_record.txt`` at relevance 0.751, above the
``_RAG_UPLOAD_RELEVANCE_MIN`` (0.70) floor, so the upload legitimately
matches. Tuning the floor cannot close this: a message that was just scrubbed
of PII has no business surfacing a citation at all. The guard is at the
``/chat/send`` choke point: when the scrub gate detects PII on a turn, that
turn's ``sources`` are cleared, regardless of what retrieval found.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import puffpai.api_server as api  # noqa: E402

_SSN_META = {
    'detected': True,
    'summary': '1 SSN',
    'types': ['ssn'],
    'severity': 'critical',
    'vaulted': [],
    'redacted_message': 'my ssn is [REDACTED-SSN].',
    'handles': 0,
    'scrubbed_not_vaulted': 1,
    'status': 'VAULT_LOCKED',
}

_PATIENT_RECORD_SOURCE = {
    'file_id': 'patient_record.txt',
    'library': 'Your upload: patient_record.txt',
    'relevance': 0.751,
    'source_type': 'user_upload',
    'type': 'user_upload',
}


# ── unit: the gate itself ───────────────────────────────────────────────

def test_pii_scrubbed_this_turn_true_when_detected():
    assert api._pii_scrubbed_this_turn(_SSN_META) is True


def test_pii_scrubbed_this_turn_false_when_nothing_detected():
    assert api._pii_scrubbed_this_turn(None) is False
    assert api._pii_scrubbed_this_turn({'detected': False}) is False
    assert api._pii_scrubbed_this_turn({}) is False


def test_pii_scrubbed_this_turn_true_even_when_it_was_vaulted():
    """Any PII on the turn skips citations, vaulted or not — the vaulted
    branch of _apply_pii_honesty says nothing extra, but a household
    document has no more business riding along on a vaulted-SSN turn than
    an unvaulted one."""
    vaulted_meta = dict(_SSN_META, vaulted=['ssn'], status='OK')
    assert api._pii_scrubbed_this_turn(vaulted_meta) is True


# ── integration: the /chat/send choke point ─────────────────────────────

def _install_fakes(monkeypatch, *, pii_meta, sources):
    monkeypatch.setattr(
        api, '_prepare_user_message',
        lambda message, *, source, user_id=None, session_id=None:
            (message, message, pii_meta))
    monkeypatch.setattr(
        api, '_process_chat_query',
        # **kw, not a fixed keyword list: the real _process_chat_query grew
        # `conversation_id` in the calendar-nl merge, and a
        # double that pins the old signature turns a signature drift into a
        # TypeError inside the route, which surfaces as a bare
        # "'tuple' object has no attribute 'get_json'" here.
        lambda msg, hist=None, **kw: {
            'response': 'Got it',
            'sources': list(sources),
            'suggestions': [],
        })


def test_ssn_sentence_returns_no_sources_and_the_honest_reply(monkeypatch):
    _install_fakes(monkeypatch, pii_meta=_SSN_META,
                   sources=[_PATIENT_RECORD_SOURCE])
    with api.app.test_request_context(
            '/chat/send', method='POST',
            json={'message': 'my ssn is 219-09-9999.'}):
        resp = api.chat_send()
    data = resp.get_json()
    assert data['sources'] == [], (
        "patient_record.txt rode along on a PII turn: "
        f"{data['sources']}")
    assert 'did not keep it' in data['response']
    assert 'patient_record' not in data['response']


def test_a_clean_knowledge_question_keeps_its_sources(monkeypatch):
    """The gate must not blank sources on a turn with no PII at all
    (moon-landing regression check — no uploads either way, but the
    zim_fts sources must survive untouched)."""
    moon_sources = [{'file_id': 'apollo11', 'source_type': 'zim_fts',
                     'type': 'zim_fts', 'relevance': 0.9}]
    _install_fakes(monkeypatch, pii_meta={'detected': False},
                   sources=moon_sources)
    with api.app.test_request_context(
            '/chat/send', method='POST',
            json={'message': 'tell me about the moon landing'}):
        resp = api.chat_send()
    data = resp.get_json()
    assert data['sources'] == moon_sources
    assert all(s['source_type'] == 'zim_fts' for s in data['sources'])
    assert not any('upload' in s.get('source_type', '') for s in data['sources'])


def test_a_real_upload_question_with_no_pii_is_still_cited(monkeypatch):
    """A genuine document question — no PII on the turn — keeps its
    citation; only a PII-bearing turn loses its sources."""
    real_upload = {'file_id': 'Strength Program.docx',
                   'library': 'Your upload: Strength Program.docx',
                   'relevance': 0.84, 'source_type': 'user_upload',
                   'type': 'user_upload'}
    _install_fakes(monkeypatch, pii_meta={'detected': False},
                   sources=[real_upload])
    with api.app.test_request_context(
            '/chat/send', method='POST',
            json={'message': "whats in my strength program document"}):
        resp = api.chat_send()
    data = resp.get_json()
    assert data['sources'] == [real_upload]
