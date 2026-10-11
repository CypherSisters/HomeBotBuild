"""the household's favorite stations are ONE list.

Before this change the dashboard's World Radio hearts lived in browser
localStorage and the backend kept its own ``radio_favorites`` in the mediahub
doc. Two lists, neither visible to the other: "play my favorites" by voice did
not know about the hearts, and the hearts did not survive a browser profile.

This exposes the BACKEND list through the existing app-action mechanism
(``POST /apps/mediahub/action``), which is API-key gated and bound to the
mediahub app, so the dashboard reads and writes the same rows the voice tools
do. Every row arriving that way is untrusted browser input that will later be
handed to mpv, so the validation here is the point: scheme,
control characters, indoor addresses, field caps, the favorites cap.

Real ``AppData`` on a temp dir. No docker, no network, no DNS — the storage
time check is deliberately syntactic (see ``test_import_never_resolves_dns``).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from puffpai import media_hub  # noqa: E402
from puffpai import radio_player  # noqa: E402

MAX = media_hub.RADIO_FAVORITES_MAX

ACTIONS = ("mediahub_favorites_list", "mediahub_favorites_add",
           "mediahub_favorites_remove", "mediahub_favorites_import")


_DEFAULT_URL = object()


def _station(name="Jazz FM", uuid="uuid-jazz", url=_DEFAULT_URL, **extra):
    if url is _DEFAULT_URL:
        url = "https://stream.example/" + "".join(
            ch for ch in (name if isinstance(name, str) else "x") if ch.isalnum())
    row = {
        "stationuuid": uuid,
        "name": name,
        "url_resolved": url,
        "country": "France", "countrycode": "FR", "codec": "MP3",
        "bitrate": 128, "tags": "jazz,blues", "homepage": "https://jazz.example",
        "favicon": "https://jazz.example/f.png",
    }
    row.update(extra)
    return row


@pytest.fixture()
def app(tmp_path, monkeypatch):
    from puffpai import app_data as app_data_module
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(app_data_module, "_data_dir_cache",
                        str(tmp_path / "app_data"), raising=False)
    return app_data_module.AppData()


@pytest.fixture(scope="module")
def api():
    from unittest.mock import MagicMock
    from tests.modpatch import patched_modules

    src = str(REPO_ROOT / "src" / "puffpai")
    if src not in sys.path:
        sys.path.insert(0, src)
    mock_fw = MagicMock()
    mock_fw.get_status.return_value = {"lockdown": False, "blocked_ips": []}
    with patched_modules({"firewall_manager":
                          MagicMock(FirewallManager=lambda: mock_fw)}):
        import importlib
        if "api_server" in sys.modules:
            mod = importlib.reload(sys.modules["api_server"])
        else:
            mod = importlib.import_module("api_server")
        mod.app.config["TESTING"] = True
        yield mod


@pytest.fixture()
def call(api, app, monkeypatch):
    """POST one action the way the dashboard must, return the result object."""
    monkeypatch.setattr(api, "_get_app_data", lambda: app)

    def _post(action, params=None, app_name="mediahub"):
        with api.app.test_client() as c:
            resp = c.post(f"/apps/{app_name}/action",
                          json={"action": action, "params": params or {}},
                          headers={"X-API-Key": api._API_KEY})
        return resp
    return _post


def _result(resp):
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["success"] is True
    return body["result"]


# ═════════════════════════════════════════════════════════════════════════════
#  the allowlist and the gate
# ═════════════════════════════════════════════════════════════════════════════

def test_the_four_actions_are_bound_to_mediahub_only(api):
    for action in ACTIONS:
        assert action in api._APP_ACTIONS["mediahub"]
        assert action in api._ALLOWED_APP_ACTIONS
        for name, actions in api._APP_ACTIONS.items():
            if name != "mediahub":
                assert action not in actions


def test_an_unknown_mediahub_action_is_still_refused(call):
    assert call("mediahub_favorites_wipe").status_code == 403


def test_a_favorites_action_on_another_app_is_refused(call):
    assert call("mediahub_favorites_list", app_name="trivia").status_code == 403


def test_the_route_needs_the_api_key(api, app, monkeypatch):
    monkeypatch.setattr(api, "_get_app_data", lambda: app)
    assert "app_action" not in api._PUBLIC_ENDPOINTS
    with api.app.test_client() as c:
        resp = c.post("/apps/mediahub/action",
                      json={"action": "mediahub_favorites_list", "params": {}})
    assert resp.status_code == 401


# ═════════════════════════════════════════════════════════════════════════════
#  happy paths
# ═════════════════════════════════════════════════════════════════════════════

def test_list_starts_empty_and_reports_the_cap(call):
    assert _result(call("mediahub_favorites_list")) == {
        "ok": True, "favorites": [], "max": MAX}


def test_add_then_list_returns_the_wire_shape(call):
    out = _result(call("mediahub_favorites_add", {"station": _station()}))
    assert out["ok"] is True
    row = out["favorites"][0]
    for key in ("stationuuid", "name", "url_resolved", "country", "countrycode",
                "codec", "bitrate", "tags", "homepage", "favicon"):
        assert key in row
    assert row["stationuuid"] == "uuid-jazz"
    assert row["name"] == "Jazz FM"
    assert row["url_resolved"] == "https://stream.example/JazzFM"
    assert row["bitrate"] == 128
    assert _result(call("mediahub_favorites_list"))["favorites"] == out["favorites"]


def test_add_is_idempotent_and_keeps_position(call):
    call("mediahub_favorites_add", {"station": _station("A", "u-a")})
    call("mediahub_favorites_add", {"station": _station("B", "u-b")})
    before = [r["stationuuid"] for r in
              _result(call("mediahub_favorites_list"))["favorites"]]
    out = _result(call("mediahub_favorites_add",
                       {"station": _station("A renamed", "u-a")}))
    assert [r["stationuuid"] for r in out["favorites"]] == before
    row = [r for r in out["favorites"] if r["stationuuid"] == "u-a"][0]
    assert row["name"] == "A renamed"


def test_identity_is_the_uuid_when_present(call):
    call("mediahub_favorites_add", {"station": _station("A", "u-a")})
    out = _result(call("mediahub_favorites_add",
                       {"station": _station("A", "u-a",
                                            url="https://other.example/s")}))
    assert len(out["favorites"]) == 1
    assert out["favorites"][0]["url_resolved"] == "https://other.example/s"


def test_identity_falls_back_to_the_url(call):
    same = "https://stream.example/one"
    call("mediahub_favorites_add", {"station": _station("A", "", url=same)})
    out = _result(call("mediahub_favorites_add",
                       {"station": _station("A again", "", url=same.upper()
                                            .replace("HTTPS://STREAM.EXAMPLE",
                                                     "https://Stream.Example"))}))
    assert len(out["favorites"]) == 1


def test_remove_by_id_and_by_url_and_missing_is_ok(call):
    call("mediahub_favorites_add", {"station": _station("A", "u-a")})
    call("mediahub_favorites_add",
         {"station": _station("B", "", url="https://stream.example/b")})
    out = _result(call("mediahub_favorites_remove", {"id": "u-a"}))
    assert [r["name"] for r in out["favorites"]] == ["B"]
    out = _result(call("mediahub_favorites_remove",
                       {"url": "https://stream.example/b"}))
    assert out["favorites"] == []
    out = _result(call("mediahub_favorites_remove", {"id": "not-here"}))
    assert out == {"ok": True, "favorites": []}


# ═════════════════════════════════════════════════════════════════════════════
#  import — the once-per-browser lift out of localStorage
# ═════════════════════════════════════════════════════════════════════════════

def test_import_merges_without_disturbing_existing_order(call):
    call("mediahub_favorites_add", {"station": _station("A", "u-a")})
    out = _result(call("mediahub_favorites_import", {"stations": [
        _station("A copy", "u-a"),            # already present -> skipped
        _station("B", "u-b"),
        _station("C", "u-c"),
        _station("B again", "u-b"),           # dupe inside the batch
    ]}))
    assert out["ok"] is True
    assert out["added"] == 2 and out["skipped"] == 2
    assert out["ignored"] == 0 and out["full"] is False
    assert [r["stationuuid"] for r in out["favorites"]] == ["u-a", "u-b", "u-c"]
    assert [r["name"] for r in out["favorites"]] == ["A", "B", "C"]


def test_import_skips_invalid_rows_and_counts_them(call):
    out = _result(call("mediahub_favorites_import", {"stations": [
        _station("Good", "u-good"),
        _station("Bad", "u-bad", url="file:///etc/passwd"),
        "not a dict",
        {},
    ]}))
    assert out["added"] == 1 and out["skipped"] == 3
    assert [r["name"] for r in out["favorites"]] == ["Good"]


def test_import_stops_at_the_cap(call):
    out = _result(call("mediahub_favorites_import", {"stations": [
        _station(f"S{i}", f"u-{i}") for i in range(MAX + 5)]}))
    assert out["added"] == MAX and out["skipped"] == 5
    assert out["ignored"] == 0 and out["full"] is True
    assert len(out["favorites"]) == MAX


def test_import_counts_rows_past_the_consideration_limit_as_ignored(call):
    out = _result(call("mediahub_favorites_import", {"stations": [
        _station(f"S{i}", f"u-{i}") for i in range(150)]}))
    assert out["added"] == MAX
    assert out["skipped"] == 100 - MAX      # examined and refused for the cap
    assert out["ignored"] == 50             # never looked at
    assert out["added"] + out["skipped"] + out["ignored"] == 150
    assert out["full"] is True
    assert len(out["favorites"]) == MAX


def test_an_import_that_fits_is_not_flagged_full(call):
    out = _result(call("mediahub_favorites_import", {"stations": [
        _station("A", "u-a"), {"name": "no url"}]}))
    assert out["added"] == 1 and out["skipped"] == 1
    assert out["ignored"] == 0 and out["full"] is False


def test_import_needs_a_list(call):
    out = _result(call("mediahub_favorites_import", {"stations": "nope"}))
    assert out["ok"] is False and out["error"] == "invalid_stations"


def test_import_never_resolves_dns(call, monkeypatch):
    """40 rows must not mean 40 DNS lookups on a box behind a closed door.

    The full check (which does resolve) still runs at PLAY time in
    ``radio_player.play``; storage time is syntactic on purpose.
    """
    def _boom(host):
        raise AssertionError(f"storage-time validation resolved {host!r}")
    monkeypatch.setattr(radio_player, "_resolve", _boom)
    out = _result(call("mediahub_favorites_import", {"stations": [
        _station(f"S{i}", f"u-{i}") for i in range(MAX)]}))
    assert out["added"] == MAX


# ═════════════════════════════════════════════════════════════════════════════
#  the cap
# ═════════════════════════════════════════════════════════════════════════════

def test_add_past_the_cap_is_refused(call):
    for i in range(MAX):
        call("mediahub_favorites_add", {"station": _station(f"S{i}", f"u-{i}")})
    out = _result(call("mediahub_favorites_add",
                       {"station": _station("One too many", "u-extra")}))
    assert out == {"ok": False, "error": "favorites_full", "max": MAX}
    assert len(_result(call("mediahub_favorites_list"))["favorites"]) == MAX


def test_updating_an_existing_favorite_works_at_the_cap(call):
    for i in range(MAX):
        call("mediahub_favorites_add", {"station": _station(f"S{i}", f"u-{i}")})
    out = _result(call("mediahub_favorites_add",
                       {"station": _station("S0 renamed", "u-0")}))
    assert out["ok"] is True
    assert len(out["favorites"]) == MAX


# ═════════════════════════════════════════════════════════════════════════════
#  validation — every row here is browser input headed for a media player
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "smb://nas/share/x.mp3",
    "av://v4l2:/dev/video0",
    "--script=/tmp/evil.lua",
    "-ao=pcm",
    "javascript:alert(1)",
    "https://127.0.0.1:8000/apps",
    "http://192.168.1.50/printer",
    "http://[::1]/x",
    # inet_aton-style literals for the same indoor addresses
    "http://2130706433/",
    "http://0x7f000001/",
    "http://0x7f.1/",
    "http://0177.0.0.1/",
    "http://127.1/",
    "http://10.1/",
    "http://3232235777/printer",
    "http://0x0a.0x00.0x00.0x01/",
    "http://127.0.0.1.5.6/",
    # credentials in the URL
    "http://user:pw@stream.example/x",
    "https://user@stream.example/x",
    "http://localhost:8000/x",
    "http://box.local/x",
    "https://stream.example/x\r\nHost: evil",
    "",
    "   ",
])
def test_a_refused_url_is_never_stored(call, url):
    out = _result(call("mediahub_favorites_add",
                       {"station": _station(url=url)}))
    assert out == {"ok": False, "error": "invalid_station"}
    assert _result(call("mediahub_favorites_list"))["favorites"] == []


@pytest.mark.parametrize("name", ["", "   ", "\r\n\t", "x" * 121])
def test_a_bad_name_is_refused(call, name):
    out = _result(call("mediahub_favorites_add", {"station": _station(name=name)}))
    assert out == {"ok": False, "error": "invalid_station"}


def test_control_characters_are_stripped_from_the_name(call):
    out = _result(call("mediahub_favorites_add",
                       {"station": _station(name="Jazz\r\n\x00 FM")}))
    assert out["favorites"][0]["name"] == "Jazz FM"


@pytest.mark.parametrize("station", [None, "string", 42, [], {"name": "x"}])
def test_junk_instead_of_a_station_is_refused(call, station):
    out = _result(call("mediahub_favorites_add", {"station": station}))
    assert out == {"ok": False, "error": "invalid_station"}


def test_oversize_string_fields_are_capped(call):
    out = _result(call("mediahub_favorites_add", {"station": _station(
        tags="t" * 900, country="c" * 200, codec="MP3" * 50,
        countrycode="FR" * 40)}))
    row = out["favorites"][0]
    assert len(row["tags"]) == 300
    assert len(row["country"]) == 80
    assert len(row["codec"]) == 20


def test_a_non_http_homepage_or_favicon_is_dropped_not_stored(call):
    out = _result(call("mediahub_favorites_add", {"station": _station(
        homepage="javascript:alert(1)", favicon="data:image/png;base64,AAAA")}))
    row = out["favorites"][0]
    assert row["homepage"] == "" and row["favicon"] == ""


def test_an_oversize_homepage_is_dropped(call):
    out = _result(call("mediahub_favorites_add", {"station": _station(
        homepage="https://e.example/" + "p" * 600)}))
    assert out["favorites"][0]["homepage"] == ""


@pytest.mark.parametrize("bitrate,expected", [
    ("128", 128), ("junk", 0), (-5, 0), (99999, 0), (None, 0), ({}, 0)])
def test_bitrate_is_clamped(call, bitrate, expected):
    out = _result(call("mediahub_favorites_add",
                       {"station": _station(bitrate=bitrate)}))
    assert out["favorites"][0]["bitrate"] == expected


def test_unknown_keys_are_dropped(call, app):
    call("mediahub_favorites_add", {"station": _station(
        evil="<script>", votes=99, click_url="http://x.example")})
    stored = app.get_data("mediahub")["radio_favorites"][0]
    assert "evil" not in stored and "click_url" not in stored
    wire = _result(call("mediahub_favorites_list"))["favorites"][0]
    assert "evil" not in wire


def test_an_oversize_row_is_refused(call):
    out = _result(call("mediahub_favorites_add",
                       {"station": _station(tags="t" * 200000)}))
    assert out == {"ok": False, "error": "invalid_station"}


def test_an_unknown_param_is_a_four_hundred(call):
    assert call("mediahub_favorites_add",
                {"station": _station(), "where": 0}).status_code == 400


# ═════════════════════════════════════════════════════════════════════════════
#  TuneIn identity — ONE convention: the "tunein:" prefix on stationuuid
# ═════════════════════════════════════════════════════════════════════════════

def test_a_tunein_row_keeps_its_prefixed_identity(call, app):
    out = _result(call("mediahub_favorites_add", {"station": _station(
        "Local AM", "tunein:s12345", url="https://tunein.example/s12345")}))
    assert out["favorites"][0]["stationuuid"] == "tunein:s12345"
    assert app.get_data("mediahub")["radio_favorites"][0]["source"] == "tunein"


def test_a_tunein_source_without_the_prefix_is_normalised(call):
    out = _result(call("mediahub_favorites_add", {"station": _station(
        "Local AM", "s999", source="tunein")}))
    assert out["favorites"][0]["stationuuid"] == "tunein:s999"


# ═════════════════════════════════════════════════════════════════════════════
#  interop with the voice tools — one list, both directions
# ═════════════════════════════════════════════════════════════════════════════

def test_a_favorite_added_by_the_dashboard_is_visible_to_voice(call, app,
                                                               monkeypatch):
    call("mediahub_favorites_add", {"station": _station("Jazz FM", "u-jazz")})
    said = media_hub.tool_radio_favorites("list", app_data=app)
    assert "Jazz FM" in said

    played = []
    monkeypatch.setattr(radio_player, "play",
                        lambda url, verify=True: played.append(url) or (True, ""))
    station = media_hub._fav_station(media_hub.list_favorites(app)[0])
    url, note = media_hub.station_stream_url(station)
    assert url == "https://stream.example/JazzFM"
    assert note is None


def test_a_station_saved_by_voice_shows_up_in_the_wire_list(call, app):
    app.mediahub_report_radio("playing", station=_station("Jazz FM", "u-jazz"),
                              source="appliance")
    assert "Saved" in media_hub.tool_radio_favorites("save", app_data=app)
    rows = _result(call("mediahub_favorites_list"))["favorites"]
    assert rows[0]["name"] == "Jazz FM"
    assert rows[0]["stationuuid"] == "u-jazz"
    assert rows[0]["url_resolved"] == "https://stream.example/JazzFM"


def test_voice_and_dashboard_share_one_stored_shape(call, app):
    call("mediahub_favorites_add", {"station": _station("Dash", "u-dash")})
    media_hub.add_favorite(_station("Voice", "u-voice"), app)
    stored = app.get_data("mediahub")["radio_favorites"]
    assert {frozenset(r) for r in stored} == {frozenset(stored[0])}
    assert media_hub.remove_favorite("Dash", app) == "Dash"
    assert [r["stationuuid"] for r in
            _result(call("mediahub_favorites_list"))["favorites"]] == ["u-voice"]


def test_the_writes_go_through_the_closed_key_writer(call, app, monkeypatch):
    keys = []
    real = app.mediahub_set
    monkeypatch.setattr(app, "mediahub_set",
                        lambda key, value: keys.append(key) or real(key, value))
    call("mediahub_favorites_add", {"station": _station()})
    call("mediahub_favorites_import", {"stations": [_station("B", "u-b")]})
    call("mediahub_favorites_remove", {"id": "uuid-jazz"})
    assert keys == ["radio_favorites"] * 3


# ═════════════════════════════════════════════════════════════════════════════
#  An IPv4 literal does not have to be a dotted quad
# ═════════════════════════════════════════════════════════════════════════════
#
# ipaddress.ip_address() only parses dotted quads, so every inet_aton form of
# 127.0.0.1 used to fall through to the hostname branch — stored outright in
# the no-DNS mode, and passed at play time by any resolver that accepts them.

_INDOOR_LITERALS = [
    "http://2130706433/",          # 127.0.0.1
    "http://0x7f000001/",          # 127.0.0.1, hex
    "http://0x7f.1/",              # 127.0.0.1, hex + short form
    "http://0177.0.0.1/",          # 127.0.0.1, octal
    "http://127.1/",               # short form
    "http://10.1/",                # 10.0.0.1
    "http://3232235777/",          # 192.168.1.1
    "http://0xc0.0xa8.1.1/",       # 192.168.1.1
    "http://127.0.0.1.5.6/",       # numeric-looking, unparseable -> refuse
    "http://999999999999/",        # numeric-looking, out of range -> refuse
]

_CREDENTIALED = [
    "http://user:pw@stream.example/x",
    "https://user@stream.example/x",
    "https://user:pw@127.0.0.1/x",
]

#: Real stations whose hostnames merely START with digits, or read like hex.
_NUMERIC_LOOKING_HOSTS = [
    "https://1.fm/stream", "https://101.ru/x", "https://977music.com/x",
    "https://0nlinehits.example/x", "https://abc.de/x",
]


@pytest.mark.parametrize("url", _INDOOR_LITERALS + _CREDENTIALED)
@pytest.mark.parametrize("resolve_host", [True, False])
def test_the_player_refuses_these_in_both_modes(url, resolve_host, monkeypatch):
    monkeypatch.setattr(radio_player, "_resolve",
                        lambda host: ["93.184.216.34"])   # public, if asked
    assert radio_player.stream_url_refusal(url, resolve_host=resolve_host)


@pytest.mark.parametrize("url", _NUMERIC_LOOKING_HOSTS + ["http://134744072/x"])
@pytest.mark.parametrize("resolve_host", [True, False])
def test_ordinary_hosts_are_still_allowed(url, resolve_host, monkeypatch):
    monkeypatch.setattr(radio_player, "_resolve",
                        lambda host: ["93.184.216.34"])
    assert radio_player.stream_url_refusal(url, resolve_host=resolve_host) is None


@pytest.mark.parametrize("url", _NUMERIC_LOOKING_HOSTS)
def test_a_station_on_a_digit_host_is_still_storable(call, url):
    out = _result(call("mediahub_favorites_add", {"station": _station(url=url)}))
    assert out["ok"] is True


# ═════════════════════════════════════════════════════════════════════════════
#  Two different stations may share a name
# ═════════════════════════════════════════════════════════════════════════════

def test_two_stations_with_the_same_name_coexist(call):
    call("mediahub_favorites_add",
         {"station": _station("Radio Nova", "u-1", url="https://a.example/1")})
    out = _result(call("mediahub_favorites_add",
                       {"station": _station("Radio Nova", "u-2",
                                            url="https://b.example/2")}))
    assert [r["stationuuid"] for r in out["favorites"]] == ["u-2", "u-1"]
    assert [r["url_resolved"] for r in out["favorites"]] == [
        "https://b.example/2", "https://a.example/1"]


def test_import_keeps_a_namesake_that_is_a_different_station(call):
    call("mediahub_favorites_add",
         {"station": _station("Radio Nova", "u-1", url="https://a.example/1")})
    out = _result(call("mediahub_favorites_import", {"stations": [
        _station("Radio Nova", "u-2", url="https://b.example/2")]}))
    assert out["added"] == 1 and len(out["favorites"]) == 2


def test_voice_save_dedupes_by_identity_not_name(app):
    media_hub.add_favorite(_station("Radio Nova", "u-1"), app)
    media_hub.add_favorite(_station("Radio Nova", "u-2"), app)
    assert len(media_hub.list_favorites(app)) == 2
    # ...while the voice remover still works by name, on the first match.
    assert media_hub.remove_favorite("Radio Nova", app) == "Radio Nova"
    assert len(media_hub.list_favorites(app)) == 1


# ═════════════════════════════════════════════════════════════════════════════
#  The rest of the row is input too
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("link", [
    "http://127.0.0.1/f.png", "http://2130706433/f.png",
    "http://user:pw@e.example/f.png", "http://box.local/f.png",
    "file:///etc/passwd", "javascript:alert(1)"])
def test_a_link_field_gets_the_same_check_as_the_stream(call, link):
    out = _result(call("mediahub_favorites_add",
                       {"station": _station(homepage=link, favicon=link)}))
    row = out["favorites"][0]
    assert row["homepage"] == "" and row["favicon"] == ""


@pytest.mark.parametrize("name", [{"a": 1}, ["x"], 7, True])
def test_a_non_string_name_makes_the_row_invalid(call, name):
    out = _result(call("mediahub_favorites_add", {"station": _station(name=name)}))
    assert out == {"ok": False, "error": "invalid_station"}


def test_non_string_text_fields_are_dropped_never_stringified(call):
    out = _result(call("mediahub_favorites_add", {"station": _station(
        tags={"a": 1}, country=["FR"], codec=7, countrycode={"x": "y"})}))
    row = out["favorites"][0]
    assert row["tags"] == "" and row["country"] == "" and row["codec"] == ""


def test_unicode_format_controls_are_stripped(call):
    out = _result(call("mediahub_favorites_add", {"station": _station(
        name="Jazz‮ FM​", tags="jazz⁦,blues")}))
    row = out["favorites"][0]
    assert row["name"] == "Jazz FM"
    assert "⁦" not in row["tags"]


def test_a_stream_url_carrying_a_bidi_control_is_refused(call):
    out = _result(call("mediahub_favorites_add",
                       {"station": _station(url="https://stream.example/‮x")}))
    assert out == {"ok": False, "error": "invalid_station"}


@pytest.mark.parametrize("uuid,expected", [
    ("tunein:s12345", "tunein:s12345"),
    ("TuneIn:S12345", "tunein:s12345"),
    ("S999", "tunein:s999"),
])
def test_a_tunein_uuid_is_normalised_to_one_form(call, uuid, expected):
    station = _station("Local AM", uuid, url="https://tunein.example/x")
    if not uuid.lower().startswith("tunein:"):
        station["source"] = "tunein"
    out = _result(call("mediahub_favorites_add", {"station": station}))
    assert out["favorites"][0]["stationuuid"] == expected


@pytest.mark.parametrize("uuid,source", [
    ("tunein:abc", ""), ("tunein:", ""), ("tunein:s12/../x", ""),
    ("", "tunein"), ("not-a-guide", "tunein"),
])
def test_a_malformed_tunein_identity_is_refused(call, uuid, source):
    out = _result(call("mediahub_favorites_add", {"station": _station(
        "Local AM", uuid, url="https://tunein.example/x", source=source)}))
    assert out == {"ok": False, "error": "invalid_station"}


# ═════════════════════════════════════════════════════════════════════════════
#  V-F1 · a favorite with no catalog uuid still has a stable id on the wire
# ═════════════════════════════════════════════════════════════════════════════

def test_a_uuidless_favorite_gets_a_stable_synthetic_id(call):
    url = "https://stream.example/one"
    first = _result(call("mediahub_favorites_add",
                         {"station": _station("A", "", url=url)}))
    ident = first["favorites"][0]["stationuuid"]
    assert ident.startswith("url:") and len(ident) == len("url:") + 16
    again = _result(call("mediahub_favorites_list"))["favorites"][0]
    assert again["stationuuid"] == ident


def test_the_synthetic_id_removes_the_right_row(call):
    call("mediahub_favorites_add",
         {"station": _station("A", "", url="https://stream.example/one")})
    call("mediahub_favorites_add", {"station": _station("B", "u-b")})
    rows = _result(call("mediahub_favorites_list"))["favorites"]
    synthetic = [r for r in rows if r["name"] == "A"][0]["stationuuid"]
    out = _result(call("mediahub_favorites_remove", {"id": synthetic}))
    assert [r["name"] for r in out["favorites"]] == ["B"]


def test_a_synthetic_id_is_not_stored_as_a_catalog_uuid(call, app):
    call("mediahub_favorites_add",
         {"station": _station("A", "", url="https://stream.example/one")})
    assert app.get_data("mediahub")["radio_favorites"][0]["uuid"] == ""


def test_a_listed_row_can_be_sent_straight_back_without_duplicating(call):
    """The page hearts a row it got from us, synthetic id and all."""
    call("mediahub_favorites_add",
         {"station": _station("A", "", url="https://stream.example/one")})
    row = _result(call("mediahub_favorites_list"))["favorites"][0]
    out = _result(call("mediahub_favorites_add", {"station": row}))
    assert len(out["favorites"]) == 1
    out = _result(call("mediahub_favorites_import", {"stations": [row]}))
    assert out["added"] == 0 and len(out["favorites"]) == 1
