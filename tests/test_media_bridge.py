"""Tests for media_bridge.py — Jellyfin id validation at the URL sinks.

Session ids, item ids, playlist ids and playlist-entry ids all become path
segments of an API-key-authenticated Jellyfin request, and several arrive
straight from a request body. An unvalidated id is therefore an authenticated
request primitive against arbitrary Jellyfin endpoints (requests resolves dot
segments before sending). These tests pin the allow-list and, crucially,
assert that a rejected id never reaches the socket at all.
"""

import pytest

from puffpai.media_bridge import (
    MEDIA_ID_RE,
    MediaBridge,
    MediaBridgeError,
    _assert_no_traversal,
    _echo,
    _StateCache,
    is_valid_media_id,
    quote_segment,
    validate_media_id,
)


# Shapes that must never reach an HTTP request. The traversal strings are the
# actual primitive; the rest close the usual escape hatches.
BAD_IDS = [
    '../../System/Configuration',
    '../Users',
    'abc/../../x',
    'abc/def',
    '/Sessions',
    'x?api_key=1',
    'abc123?ItemIds=1',
    'abc#frag',
    'abc%2f..%2fUsers',
    'abc def',
    'abc\n',            # the \A..\Z vs ^..$ trap: $ matches before a newline
    '\nabc',
    'abc\r\n',
    'abc\x00',
    '.',
    '..',
    '',
    'a' * 65,           # over the length cap
    42,
    3.5,
    True,
    None,
    ['abc'],
    {'id': 'abc'},
    b'abc',
]

GOOD_IDS = [
    'f8a1c3d2e4b5a6978c0d1e2f3a4b5c6d',          # 32-char Jellyfin GUID
    'f8a1c3d2-e4b5-4697-8c0d-1e2f3a4b5c6d',      # dashed GUID form
    'ABC123',
    'a',
    '0',
    'a' * 64,                                     # exactly at the cap
]


class TestMediaIdValidator:
    """is_valid_media_id / validate_media_id allow-list."""

    @pytest.mark.parametrize('value', BAD_IDS)
    def test_bad_id_is_rejected(self, value):
        assert is_valid_media_id(value) is False
        with pytest.raises(MediaBridgeError) as exc:
            validate_media_id(value, kind='session_id')
        assert str(exc.value).startswith('invalid session_id: ')

    @pytest.mark.parametrize('value', GOOD_IDS)
    def test_good_id_is_accepted(self, value):
        assert is_valid_media_id(value) is True
        assert validate_media_id(value) == value

    def test_trailing_newline_is_rejected_by_the_anchor(self):
        # Guards against a regression from \Z back to $, which would match
        # before a trailing newline and let "abc\n" through.
        assert MEDIA_ID_RE.match('abc\n') is None
        assert MEDIA_ID_RE.match('abc') is not None

    def test_error_never_echoes_more_than_16_characters(self):
        with pytest.raises(MediaBridgeError) as exc:
            validate_media_id('../' * 40 + 'System/Configuration')
        msg = str(exc.value)
        assert len(msg) < 80
        assert 'System/Configuration' not in msg

    def test_error_strips_non_printable_characters(self):
        assert '\n' not in _echo('abc\ndef')
        assert '\x00' not in _echo('abc\x00def')

    def test_error_names_the_type_for_non_strings(self):
        assert _echo(42) == '<int>'
        assert _echo(None) == '<NoneType>'


class TestQuoteSegment:
    def test_valid_ids_pass_through_untouched(self):
        for value in GOOD_IDS:
            assert quote_segment(value) == value

    def test_separators_are_encoded(self):
        assert quote_segment('a/b') == 'a%2Fb'
        assert quote_segment('a?b') == 'a%3Fb'
        assert quote_segment('..') == '..'  # encoding alone is not enough --
        # which is exactly why validate_media_id runs first and _assert_no_
        # traversal runs last.


class TestAssertNoTraversal:
    @pytest.mark.parametrize('path', [
        '/Sessions/../Users',
        '/Sessions/./Playing',
        '/../System/Configuration',
        '/Playlists/../../Users/Public',
        '/Sessions/..',
    ])
    def test_dot_segments_are_rejected(self, path):
        with pytest.raises(MediaBridgeError) as exc:
            _assert_no_traversal(path)
        assert str(exc.value).startswith('invalid Jellyfin path: ')

    @pytest.mark.parametrize('path', [
        '/System/Info/Public',
        '/Sessions/abc123/Playing',
        '/Playlists/abc123/Items',
        '/UserFavoriteItems/abc123',
        '/Items?SearchTerm=a.b.c',      # dots inside a query value are fine
        '/Items?x=../y',                # ...and cannot climb the path
    ])
    def test_clean_paths_pass(self, path):
        _assert_no_traversal(path)


# ---------------------------------------------------------------------------
# Bridge-level: a rejected id must never reach the socket
# ---------------------------------------------------------------------------

class _StubResponse:
    content = b'{}'
    headers: dict = {}

    def raise_for_status(self):
        return None

    def json(self):
        return {'Items': []}


class _RecordingSession:
    """requests.Session stand-in that records every attempted call."""

    def __init__(self):
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({'method': method, 'url': url, **kwargs})
        return _StubResponse()


@pytest.fixture
def bridge():
    """A MediaBridge wired to a recording session, with no config on disk.

    __new__ + explicit attributes keeps the test independent of
    media_server_config.json and guarantees no real requests.Session exists.
    """
    b = MediaBridge.__new__(MediaBridge)
    b._jellyfin_url = 'http://jellyfin.test:8096'
    b._jellyfin_api_key = 'test-key'
    b._calibre_db = ''
    b._calibre_web_url = ''
    b._media_mount = ''
    b._timeout = 5
    b._enabled = True
    b._user_id = 'user0000000000000000000000000000'
    b._cache = _StateCache(ttl=5.0)
    b._session = _RecordingSession()
    return b


class TestNoHttpForRejectedIds:
    """Every public entry point must raise before touching the session."""

    # '' / None are excluded here: they are the documented "auto-pick the
    # active session" signal, covered by test_auto_pick_only_hits_the_fixed_
    # sessions_endpoint below.
    @pytest.mark.parametrize('bad', ['../../System/Configuration',
                                     'abc/../../x', 'x?api_key=1',
                                     'abc\n', 42])
    def test_play_music_rejects_bad_session_id(self, bridge, bad):
        with pytest.raises(MediaBridgeError):
            bridge.play_music('abc123', bad)
        assert bridge._session.calls == []

    @pytest.mark.parametrize('bad', ['../../System/Configuration',
                                     'abc/../../x', 'x?api_key=1',
                                     'abc\n', 42, None])
    def test_play_music_rejects_bad_item_id(self, bridge, bad):
        with pytest.raises(MediaBridgeError):
            bridge.play_music(bad, 'sess123')
        assert bridge._session.calls == []

    @pytest.mark.parametrize('bad', ['../../System/Configuration',
                                     'abc/../../x', 'x?api_key=1',
                                     'abc\n', 42])
    def test_playback_control_rejects_bad_session_id(self, bridge, bad):
        with pytest.raises(MediaBridgeError):
            bridge.playback_control(bad, 'pause')
        assert bridge._session.calls == []

    @pytest.mark.parametrize('empty', ['', None])
    def test_auto_pick_only_hits_the_fixed_sessions_endpoint(self, bridge,
                                                             empty):
        # An empty session_id means "pick the active session". That path may
        # issue exactly one request, to the constant /Sessions listing, and
        # then fail closed because the stub returns no controllable session.
        with pytest.raises(MediaBridgeError):
            bridge.play_music('0123456789abcdef0123456789abcdef', empty)
        assert [c['url'] for c in bridge._session.calls] == [
            'http://jellyfin.test:8096/Sessions']

    @pytest.mark.parametrize('bad', ['../../System/Configuration',
                                     'abc/../../x', 'x?api_key=1', '',
                                     'abc\n', 42, None])
    def test_set_volume_rejects_bad_session_id(self, bridge, bad):
        with pytest.raises(MediaBridgeError):
            bridge.set_volume(bad, 50)
        assert bridge._session.calls == []

    @pytest.mark.parametrize('bad', ['../../System/Configuration',
                                     'abc/../../x', 'x?api_key=1', '',
                                     'abc\n', 42, None])
    def test_set_favorite_rejects_bad_item_id(self, bridge, bad):
        with pytest.raises(MediaBridgeError):
            bridge.set_favorite(bad)
        assert bridge._session.calls == []

    def test_unknown_action_does_not_echo_raw_input(self, bridge):
        with pytest.raises(MediaBridgeError) as exc:
            bridge.playback_control('sess123', 'x' * 200)
        assert len(str(exc.value)) < 120
        assert bridge._session.calls == []

    def test_jf_request_rejects_a_traversing_path_directly(self, bridge):
        # Defence in depth: even if a future caller assembles a bad path by
        # hand, _jf_request refuses to send it.
        with pytest.raises(MediaBridgeError):
            bridge._jf_request('/Sessions/../../System/Configuration')
        assert bridge._session.calls == []


class TestValidIdsProduceTheExpectedUrl:
    """The allow-list must not break the real, valid call shapes."""

    def test_play_music_url_and_params(self, bridge):
        sid = 'f8a1c3d2e4b5a6978c0d1e2f3a4b5c6d'
        iid = '0123456789abcdef0123456789abcdef'
        bridge.play_music(iid, sid)
        assert len(bridge._session.calls) == 1
        call = bridge._session.calls[0]
        assert call['method'] == 'POST'
        assert call['url'] == f'http://jellyfin.test:8096/Sessions/{sid}/Playing'
        assert call['params'] == {'ItemIds': iid, 'PlayCommand': 'PlayNow'}

    def test_playback_control_url(self, bridge):
        sid = 'f8a1c3d2-e4b5-4697-8c0d-1e2f3a4b5c6d'
        bridge.playback_control(sid, 'pause')
        assert bridge._session.calls[0]['url'] == (
            f'http://jellyfin.test:8096/Sessions/{sid}/Playing/Pause')

    def test_set_volume_url(self, bridge):
        sid = 'f8a1c3d2e4b5a6978c0d1e2f3a4b5c6d'
        bridge.set_volume(sid, 42)
        call = bridge._session.calls[0]
        assert call['url'] == f'http://jellyfin.test:8096/Sessions/{sid}/Command'
        assert call['json']['Arguments'] == {'Volume': '42'}

    def test_set_favorite_url(self, bridge):
        iid = '0123456789abcdef0123456789abcdef'
        bridge.set_favorite(iid)
        call = bridge._session.calls[0]
        assert call['url'] == f'http://jellyfin.test:8096/UserFavoriteItems/{iid}'
        assert call['method'] == 'POST'

    def test_pick_session_validates_the_upstream_id(self, bridge, monkeypatch):
        # A Jellyfin response steering our own request path is still rejected.
        monkeypatch.setattr(bridge, '_get_sessions', lambda: [
            {'Id': '../../System/Configuration', 'SupportsRemoteControl': True,
             'LastActivityDate': '2026-08-03T00:00:00Z'},
        ])
        with pytest.raises(MediaBridgeError):
            bridge.play_music('0123456789abcdef0123456789abcdef')
        assert bridge._session.calls == []
