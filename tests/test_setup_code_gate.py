"""First-run setup is not open to the LAN any more.

THE HOLE. Before completion the backend's ``_setup_gate`` has nothing to
refuse — that is what the wizard IS. That was sound while the only caller was
on this host, and it stopped being sound because three things line up:

  * ``dashboard/package.json`` served the UI on every interface;
  * pairing enforcement defaults to ``"knowledge"``
    (``dashboard/app/_lib/server/pairing.ts``), which does not cover
    ``/api/pai/setup/*``;
  * the dashboard's proxy attaches the appliance's API key server-side.

So the first device on the network to open the dashboard on a fresh box could
name the bot, write the Home Assistant token, and call ``POST /setup/vault`` —
whose reply carries the vault RECOVERY CODE.

THE CONTROL, pinned here: a console-only code required on every state-changing
``/setup/*`` call whose effective client is not this machine; loopback callers
(the CLI wizard, a local curl) unaffected; five wrong answers locks out AND
rotates; the code never reaches a log or a manifest; it is destroyed when setup
completes.

No network, no real state directory (every test gets its own tmp_path), no
sleeping — the lockout clock is moved, not waited on.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path

import pytest

#: A LAN peer. Flask's test client defaults to 127.0.0.1, so a test that wants
#: to look remote has to say so with environ_base.
REMOTE = {"REMOTE_ADDR": "192.168.1.66"}
LOOPBACK = {"REMOTE_ADDR": "127.0.0.1"}


@pytest.fixture
def scratch(tmp_path):
    """One test, its own install: temp state dir AND temp config dir."""
    from puffpai.config import CONFIG_DIR_ENV, get_config

    repo_config = Path(__file__).resolve().parents[1] / "config"
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    for name in ("puffpai_config.json", "bridge_postures.json",
                 "capability_catalog.json", "model_catalog.json",
                 "model_profiles.json"):
        source = repo_config / name
        if source.exists():
            shutil.copy2(source, config_dir / name)

    previous = {key: os.environ.get(key)
                for key in ("PUFFPAI_DATA_DIR", CONFIG_DIR_ENV)}
    os.environ["PUFFPAI_DATA_DIR"] = str(tmp_path)
    os.environ[CONFIG_DIR_ENV] = str(config_dir)
    get_config(_force_reload=True)
    try:
        yield tmp_path
    finally:
        for key, value in previous.items():
            if value is not None:
                os.environ[key] = value
            else:
                os.environ.pop(key, None)
        get_config(_force_reload=True)


@pytest.fixture
def client(scratch):
    from puffpai import api_server
    api_server._vault_instance = None
    api_server._vault_sessions.clear()
    api_server.app.config.update(TESTING=True)
    return api_server.app.test_client()


@pytest.fixture
def code(scratch):
    from puffpai.security import setup_code
    value = setup_code.current()
    assert value, "setup should be incomplete in a fresh scratch install"
    return value


def _auth():
    from puffpai import api_server
    return {"Authorization": f"Bearer {api_server._API_KEY}"}


def _headers(setup=None, client_addr=None):
    headers = dict(_auth())
    if setup is not None:
        headers["X-Setup-Code"] = setup
    if client_addr is not None:
        headers["X-PuffPAI-Client-Addr"] = client_addr
    return headers


class TestTheRemotePeerIsRefused:
    def test_a_lan_peer_without_the_code_is_refused(self, client, code):
        resp = client.post("/setup/identity", json={"bot_name": "Bot"},
                           headers=_auth(), environ_base=REMOTE)
        assert resp.status_code == 403
        body = resp.get_json()
        assert body["error"] == "setup_code_required"
        assert body["setup_code_required"] is True
        # The refusal must not hand over what it is asking for.
        assert code not in json.dumps(body)

    def test_a_lan_peer_with_the_code_is_allowed(self, client, code):
        resp = client.post("/setup/identity", json={"bot_name": "Bot"},
                           headers=_headers(setup=code), environ_base=REMOTE)
        assert resp.status_code == 200, resp.get_json()

    @pytest.mark.parametrize("typed", ["{code}", "{lower}", "{spaced}"])
    def test_the_code_is_read_the_way_a_person_types_it(self, client, code,
                                                        typed):
        """Case and separators are noise; a person reading it off a console
        should not be failed for typing it in lower case."""
        variants = {"{code}": code, "{lower}": code.lower(),
                    "{spaced}": code.replace("-", " ")}
        resp = client.post("/setup/identity", json={"bot_name": "Bot"},
                           headers=_headers(setup=variants[typed]),
                           environ_base=REMOTE)
        assert resp.status_code == 200, resp.get_json()

    def test_reads_stay_open_without_a_code(self, client, code):
        """GET /setup/status must answer — it is how the wizard learns a code
        is needed at all."""
        resp = client.get("/setup/status?ollama=0", headers=_auth(),
                          environ_base=REMOTE)
        assert resp.status_code == 200
        assert resp.get_json()["setup_code_required"] is True


class TestLoopbackNeedsNoCode:
    def test_a_loopback_peer_is_exempt(self, client, code):
        resp = client.post("/setup/identity", json={"bot_name": "Bot"},
                           headers=_auth(), environ_base=LOOPBACK)
        assert resp.status_code == 200, resp.get_json()

    def test_status_tells_a_loopback_caller_no_code_is_needed(self, client,
                                                              code):
        resp = client.get("/setup/status?ollama=0", headers=_auth(),
                          environ_base=LOOPBACK)
        assert resp.get_json()["setup_code_required"] is False


class TestTheForwardedAddressCannotBeSpoofed:
    """The dashboard proxy is always a loopback PEER, so the backend has to
    take its word for who the real client is — and only its word."""

    def test_a_remote_peer_claiming_loopback_is_still_remote(self, client, code):
        """The peer check runs FIRST: a LAN device that sets the forwarded
        header buys nothing."""
        resp = client.post("/setup/identity", json={"bot_name": "Bot"},
                           headers=_headers(client_addr="127.0.0.1"),
                           environ_base=REMOTE)
        assert resp.status_code == 403
        assert resp.get_json()["error"] == "setup_code_required"

    def test_a_loopback_peer_forwarding_a_lan_client_is_remote(self, client,
                                                               code):
        """This is the dashboard case: the proxy is on the box, its client is
        not. Without the header this request would be indistinguishable from
        somebody standing at the machine."""
        resp = client.post("/setup/identity", json={"bot_name": "Bot"},
                           headers=_headers(client_addr="192.168.1.66"),
                           environ_base=LOOPBACK)
        assert resp.status_code == 403

    @pytest.mark.parametrize("claimed", ["unknown", "", "   ", "localhost",
                                         "127.0.0.1 ,10.0.0.1", "not-an-ip",
                                         "::ffff:10.0.0.1"])
    def test_an_unparseable_forwarded_address_is_remote(self, client, code,
                                                        claimed):
        """Fail closed on ambiguity. 'unknown' is what the proxy sends when it
        genuinely cannot see the peer, and a hostname is never accepted — a
        word must not be able to assert locality."""
        resp = client.post("/setup/identity", json={"bot_name": "Bot"},
                           headers=_headers(client_addr=claimed),
                           environ_base=LOOPBACK)
        # An empty/whitespace header is absent as far as the check goes, so
        # only the values that actually claim something are graded.
        expected = 200 if not claimed.strip() else 403
        assert resp.status_code == expected, (claimed, resp.get_json())

    def test_a_loopback_peer_forwarding_loopback_is_local(self, client, code):
        resp = client.post("/setup/identity", json={"bot_name": "Bot"},
                           headers=_headers(client_addr="::1"),
                           environ_base=LOOPBACK)
        assert resp.status_code == 200, resp.get_json()


class TestLockoutAndRotation:
    def test_five_wrong_codes_lock_out_and_rotate(self, client, code, scratch):
        from puffpai.security import setup_code as sc
        wrong = "ZZZZ-ZZZZ"
        for attempt in range(sc.MAX_ATTEMPTS - 1):
            resp = client.post("/setup/identity", json={"bot_name": "Bot"},
                               headers=_headers(setup=wrong),
                               environ_base=REMOTE)
            assert resp.status_code == 403
            assert resp.get_json()["reason"] == "bad_code", attempt

        resp = client.post("/setup/identity", json={"bot_name": "Bot"},
                           headers=_headers(setup=wrong), environ_base=REMOTE)
        body = resp.get_json()
        assert resp.status_code == 403
        assert body["reason"] == "locked_out"
        assert body["retry_after"] >= 1

        # ROTATED: the value the attacker was working on is dead, and it does
        # not come back when the lockout expires.
        assert sc.current() != code

        # Even the RIGHT old code is refused while locked out.
        resp = client.post("/setup/identity", json={"bot_name": "Bot"},
                           headers=_headers(setup=code), environ_base=REMOTE)
        assert resp.get_json()["reason"] == "locked_out"

    def test_the_new_code_works_once_the_lockout_expires(self, client, code,
                                                         scratch):
        from puffpai.security import setup_code as sc
        for _ in range(sc.MAX_ATTEMPTS):
            client.post("/setup/identity", json={"bot_name": "Bot"},
                        headers=_headers(setup="ZZZZ-ZZZZ"),
                        environ_base=REMOTE)
        rotated = sc.current()
        # Move the clock rather than sleep through the backoff.
        state = json.loads(Path(sc._state_path()).read_text(encoding="utf-8"))
        state["locked_until"] = 0
        Path(sc._state_path()).write_text(json.dumps(state), encoding="utf-8")

        resp = client.post("/setup/identity", json={"bot_name": "Bot"},
                           headers=_headers(setup=rotated),
                           environ_base=REMOTE)
        assert resp.status_code == 200, resp.get_json()

    def test_a_good_code_clears_the_attempt_counter(self, client, code,
                                                    scratch):
        from puffpai.security import setup_code as sc
        for _ in range(sc.MAX_ATTEMPTS - 1):
            client.post("/setup/identity", json={"bot_name": "Bot"},
                        headers=_headers(setup="ZZZZ-ZZZZ"),
                        environ_base=REMOTE)
        client.post("/setup/identity", json={"bot_name": "Bot"},
                    headers=_headers(setup=code), environ_base=REMOTE)
        state = json.loads(Path(sc._state_path()).read_text(encoding="utf-8"))
        assert state["attempts"] == 0
        assert sc.current() == code, "a correct answer must not rotate the code"

    def test_the_backoff_grows_across_rotations(self, scratch):
        from puffpai.security import setup_code as sc
        waits = []
        for _ in range(3):
            for _ in range(sc.MAX_ATTEMPTS):
                ok, reason, wait = sc.verify("ZZZZ-ZZZZ")
            waits.append(wait)
            state = json.loads(Path(sc._state_path()).read_text(encoding="utf-8"))
            state["locked_until"] = 0
            Path(sc._state_path()).write_text(json.dumps(state),
                                              encoding="utf-8")
        assert waits == sorted(waits) and waits[0] < waits[-1], waits


class TestTheCodeIsNotLeaked:
    def test_nothing_logs_the_code(self, client, code, caplog, scratch):
        with caplog.at_level(logging.DEBUG):
            client.post("/setup/identity", json={"bot_name": "Bot"},
                        headers=_auth(), environ_base=REMOTE)
            client.post("/setup/identity", json={"bot_name": "Bot"},
                        headers=_headers(setup="ZZZZ-ZZZZ"),
                        environ_base=REMOTE)
            client.post("/setup/identity", json={"bot_name": "Bot"},
                        headers=_headers(setup=code), environ_base=REMOTE)
        assert code not in caplog.text
        assert code.lower() not in caplog.text.lower()

    def test_no_response_body_carries_the_code(self, client, code):
        for path in ("/setup/status?ollama=0", "/setup/defaults",
                     "/setup/posture"):
            resp = client.get(path, headers=_auth(), environ_base=REMOTE)
            assert code not in resp.get_data(as_text=True), path

    def test_the_manifest_never_holds_the_code(self, client, code, scratch):
        from puffpai import build_manifest
        manifest = build_manifest.assemble_manifest(
            {"total_ram_gb": 8},
            identity={"name": "Bot", "setup_code": code})
        cleaned = build_manifest.sanitize_manifest(manifest)
        assert code not in json.dumps(cleaned)
        assert "setup_code" not in json.dumps(cleaned)

    def test_the_banner_is_printable_on_a_legacy_windows_console(self, scratch):
        """cp1252 is still the default code page on a stock Windows console.
        The first banner used box-drawing characters and raised
        UnicodeEncodeError there, so the one channel that publishes the code
        printed nothing at all and remote setup was impossible."""
        from puffpai.security import setup_code as sc
        lines: list[str] = []
        assert sc.announce(print_line=lines.append)
        for line in lines:
            line.encode("cp1252")        # raises if a character cannot be shown
            assert line.isascii(), line

    def test_the_state_file_is_not_world_readable(self, scratch, code):
        """0600. A no-op on Windows, where chmod only toggles read-only, so the
        assertion is made where the bit exists."""
        from puffpai.security import setup_code as sc
        path = sc._state_path()
        assert os.path.isfile(path)
        if os.name != "nt":
            assert (os.stat(path).st_mode & 0o077) == 0


class TestItEndsWithTheWizard:
    def test_completing_setup_destroys_the_code(self, client, code, scratch):
        from puffpai.security import setup_code as sc
        resp = client.post("/setup/complete", headers=_headers(setup=code),
                           environ_base=REMOTE)
        assert resp.status_code == 200, resp.get_json()
        assert not os.path.exists(sc._state_path())
        assert sc.current() is None
        assert sc.is_required() is False

    def test_after_completion_a_remote_write_is_refused_by_the_old_gate(
            self, client, code, scratch):
        """The setup-code gate stands down and _setup_gate takes over; the
        request is still refused, with the reason that is now true."""
        client.post("/setup/complete", headers=_headers(setup=code),
                    environ_base=REMOTE)
        resp = client.post("/setup/identity", json={"bot_name": "Bot"},
                           headers=_auth(), environ_base=REMOTE)
        assert resp.status_code == 409
        assert resp.get_json()["error"] == "setup_complete"


class TestEveryStateChangingSetupRouteIsCovered:
    def test_no_post_route_under_setup_escapes_the_gate(self, client, code):
        """A before_request hook, not a per-route call: several /setup/* POSTs
        never called _setup_gate, and a route added tomorrow must be covered
        without anybody remembering to cover it."""
        from puffpai import api_server
        checked = 0
        for rule in api_server.app.url_map.iter_rules():
            path = str(rule.rule)
            if not path.startswith("/setup"):
                continue
            if "POST" not in (rule.methods or set()):
                continue
            if "<" in path:            # no parameterised setup routes today
                continue
            resp = client.post(path, json={}, headers=_auth(),
                               environ_base=REMOTE)
            assert resp.status_code == 403, f"{path} -> {resp.status_code}"
            assert resp.get_json()["error"] == "setup_code_required", path
            checked += 1
        assert checked >= 10, f"only {checked} setup POST routes were found"


class TestTheProxyForwardsTheClientAddress:
    """The backend's exemption is only as good as the proxy's honesty, so the
    proxy's two obligations are asserted in its source."""

    #: The header policy moved out of the route and into its own module
    #: (``app/_lib/server/proxyHeaders.ts``), so the invariant now spans two
    #: files: the route computes the address and hands it over, and the header
    #: module writes it into the upstream headers. Both halves are asserted,
    #: because either one alone can be true while the header is never set.
    def _route(self):
        return (Path(__file__).resolve().parents[1] / "dashboard" / "app" /
                "api" / "pai" / "[...path]" / "route.ts").read_text(
                    encoding="utf-8")

    def _headers_module(self):
        return (Path(__file__).resolve().parents[1] / "dashboard" / "app" /
                "_lib" / "server" / "proxyHeaders.ts").read_text(
                    encoding="utf-8")

    def test_it_sets_the_client_address_header(self):
        module = self._headers_module()
        assert "x-puffpai-client-addr" in module.lower()
        # Written from the argument the route computed, and from nothing else.
        assert "headers[CLIENT_ADDR_HEADER] = clientAddr;" in module
        # ...and the route really does compute it and pass it, so the argument
        # is the proxy's own claim rather than something off the wire.
        route = self._route()
        assert "upstreamHeaders(" in route
        assert "clientAddr(req)," in route

    def test_it_never_takes_the_address_from_the_client(self):
        """clientAddr must not read req.ip or x-forwarded-for: both are
        client-settable here, and reading either would let a LAN device buy the
        loopback exemption with a header."""
        text = self._route()
        start = text.index("function clientAddr")
        body = text[start:text.index("\n}", start)]
        assert ".ip" not in body
        assert "x-forwarded-for" not in body.lower()

    def test_it_forwards_the_setup_code_header(self):
        """It is on the allowlist, and the allowlist is what gets relayed.

        The header is no longer read by name: ``proxyHeaders.ts`` keeps an
        ALLOWLIST of the client headers that may cross to the backend, each
        rule scoped to the paths the backend reads it on, and copies exactly
        those. So the invariant is that x-setup-code is on it, scoped to the
        first-run families, and that the list is what the relay loop consults.
        The per-path behaviour is asserted in
        dashboard/app/_lib/server/__tests__/proxy-headers.test.mjs.
        """
        module = self._headers_module()
        assert 'header: "x-setup-code",' in module
        assert 'upstream: "X-Setup-Code",' in module
        assert "scope: isSetupCodePath," in module
        assert "for (const rule of FORWARDED_CLIENT_HEADERS)" in module
        assert "if (!rule.scope(path)) continue;" in module
        assert "req.headers.get(rule.header)" in module
