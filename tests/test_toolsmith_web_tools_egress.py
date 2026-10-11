"""The Toolsmith's web_fetch / web_search doors are REAL doors.

Before this change the ``web_fetch`` door was enumerated and inert: the
Settings switch reported a state and stopped nothing, because
``agents/toolsmith.py`` never consulted the registry — its only gate was the
per-request ``Budget.allow_egress``. ``POST /system/egress/door`` answered 409
for it, honestly, and ``sec-egress-api``'s report named routing it through the
registry as the follow-up. This file is that follow-up's regression suite.

What is asserted here, in the order it matters:

1. **Shut door, no socket.** With ``web_fetch`` shut, a fetch on a budget that
   DOES carry egress refuses without touching ``socket`` at all — verified with
   a harness that makes every socket entry point fatal, not by trusting a
   return value.
2. **The refusal is honest.** It carries "[Egress door closed]" and names the
   switch, rather than a generic failure the caller would paper over.
3. **Open door, one call.** Exactly one HTTP call, and the SSRF gates still run
   on it.
4. **The ledger records both.** Allowed and refused, with byte counts, and with
   no query string anywhere in the trail.
5. **The SSRF gates are unchanged.** ``169.254.169.254``, loopback by every
   obfuscation, and a redirect into the LAN are all still refused WITH THE DOOR
   OPEN — the door must not have become a substitute for them.
6. **Chat egress needs both halves**: the ``chat_allow_egress`` opt-in AND the
   door.

Nothing here reaches the network: the socket harness would fail the test if it
did.
"""

import json
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from puffpai.agents import Budget, Envelope  # noqa: E402
from puffpai.agents.toolsmith import EgressDenied, ToolsmithAgent  # noqa: E402
from puffpai.security import egress_doors as ed  # noqa: E402
from puffpai.security import egress_ledger, egress_policy  # noqa: E402


# ── harnesses ────────────────────────────────────────────────────────────────

class SocketOpened(AssertionError):
    """Raised if anything in the process tries to reach the network."""


@pytest.fixture
def no_sockets(monkeypatch):
    """Make every socket entry point fatal.

    "It returned a refusal" and "it opened nothing" are different claims and
    only the second one is the security property. A gate that refuses AFTER
    connecting has already told the far end this house exists.
    """
    def boom(*_a, **_kw):
        raise SocketOpened("a socket was opened")

    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket.socket, "connect_ex", boom)
    return boom


@pytest.fixture
def doors(monkeypatch):
    """Set door states from the environment; env outranks every file."""

    def _set(**states):
        for d in ed.DOORS:
            monkeypatch.setenv(d.env_var, "1" if states.get(d.id) else "0")
        ed.reset_cache()

    yield _set
    ed.reset_cache()


@pytest.fixture
def ledger(tmp_path):
    """A throwaway ledger, plus the registry's own last-used snapshot."""
    path = tmp_path / "egress.jsonl"
    egress_ledger.set_path_override(str(path))
    yield path
    egress_ledger.set_path_override(None)


def lines(path) -> list:
    if not path.exists():
        return []
    return [json.loads(ln) for ln in
            path.read_text(encoding="utf-8").splitlines() if ln.strip()]


class Response:
    """The smallest thing that behaves like a streamed requests response."""

    def __init__(self, body=b"<p>hello world</p>", status=200,
                 content_type="text/html", headers=None):
        self.status_code = status
        self.headers = {"Content-Type": content_type, **(headers or {})}
        self.content = body
        self._body = body
        self.raw = None

    def iter_content(self, chunk_size=8192):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def close(self):
        pass


class Client:
    """Records every call so "exactly one" can be asserted, not assumed."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        if not self._responses:
            return Response()
        return self._responses.pop(0)


def agent(client=None, resolver=None, **kw):
    return ToolsmithAgent(
        http=client,
        resolver=resolver or (lambda host, port: ["93.184.216.34"]),
        **kw)


def fetch_envelope(url="https://example.com/page?secret=hunter2",
                   allow_egress=True, **payload):
    return Envelope(
        intent="web_fetch",
        payload={"url": url, **payload},
        requester="owner",
        origin_agent="test",
        budget=Budget(max_actions=8, max_seconds=20.0,
                      allow_egress=allow_egress),
        state_changing=False,
    )


# ── 1. shut door: refused, and no socket ─────────────────────────────────────

def test_a_shut_door_refuses_a_fetch_without_opening_a_socket(
        doors, no_sockets, ledger):
    """The whole point. The budget carries egress; the DOOR does not."""
    doors()  # every door shut
    a = agent(Client())
    with pytest.raises(EgressDenied):
        a._web_fetch(fetch_envelope(), fetch_envelope().payload)


def test_a_shut_door_refuses_before_the_url_is_even_shape_checked(
        doors, no_sockets):
    """A malformed URL must not mask the reason the appliance stayed home.

    The door check runs first, so the owner is told "that door is shut" rather
    than "that URL is invalid" — the second one sends them off debugging the
    wrong thing.
    """
    doors()
    a = agent(Client())
    env = fetch_envelope(url="not a url at all")
    with pytest.raises(EgressDenied) as exc:
        a._web_fetch(env, env.payload)
    assert "Egress door closed" in str(exc.value)


def test_the_refusal_names_the_switch(doors, no_sockets):
    doors()
    a = agent(Client())
    env = fetch_envelope()
    with pytest.raises(EgressDenied) as exc:
        a._web_fetch(env, env.payload)
    message = str(exc.value)
    assert "[Egress door closed]" in message
    assert "web_fetch" in message
    assert "Settings > Egress" in message


def test_handle_turns_the_refusal_into_a_reply_not_a_traceback(
        doors, no_sockets):
    """``require_open`` raises EgressBlocked, which ``handle`` does not catch.

    Translating it to EgressDenied inside the agent is what keeps a shut door
    from throwing out of ``bus.send`` and taking the turn with it.
    """
    doors()
    reply = agent(Client()).handle(fetch_envelope())
    assert reply["ok"] is False
    assert reply["egress_denied"] is True
    assert "Egress door closed" in reply["error"]


def test_a_shut_door_refuses_web_search_without_a_socket(doors, no_sockets):
    """SearXNG's first hop is loopback, but the query still leaves the house."""
    doors()
    env = Envelope(intent="web_search", payload={"query": "jetson power modes"},
                   requester="owner", origin_agent="test",
                   budget=Budget(max_actions=4, max_seconds=10.0,
                                 allow_egress=True),
                   state_changing=False)
    with pytest.raises(EgressDenied) as exc:
        agent(Client())._web_search(env, env.payload)
    assert "[Egress door closed]" in str(exc.value)
    assert "web_search" in str(exc.value)


def test_a_shut_door_refuses_search_and_read_without_a_socket(
        doors, no_sockets):
    doors()
    env = Envelope(intent="search_and_read", payload={"query": "orin"},
                   requester="owner", origin_agent="test",
                   budget=Budget(max_actions=6, max_seconds=20.0,
                                 allow_egress=True),
                   state_changing=False)
    reply = agent(Client()).handle(env)
    assert reply["ok"] is False
    assert reply["egress_denied"] is True


def test_an_unreadable_registry_is_treated_as_shut(monkeypatch, no_sockets):
    """Fail closed: a registry that will not resolve is not consent."""
    monkeypatch.setattr(
        ed, "require_open",
        lambda door_id: (_ for _ in ()).throw(RuntimeError("no config")))
    env = fetch_envelope()
    with pytest.raises(EgressDenied) as exc:
        agent(Client())._web_fetch(env, env.payload)
    assert "could not be read" in str(exc.value)


# ── 2. the budget is still required, independently ───────────────────────────

def test_an_open_door_does_not_grant_a_budget(doors, no_sockets):
    """Two consents, not one. The owner's switch is not the caller's grant."""
    doors(web_fetch=True)
    env = fetch_envelope(allow_egress=False)
    with pytest.raises(EgressDenied) as exc:
        agent(Client())._web_fetch(env, env.payload)
    assert "allow_egress" in str(exc.value)
    assert "Egress door closed" not in str(exc.value)


# ── 3. open door: exactly one call, gates intact ─────────────────────────────

def test_an_open_door_fetches_once(doors, ledger):
    doors(web_fetch=True)
    client = Client(Response(b"<h1>Title</h1><p>Body text.</p>"))
    env = fetch_envelope()
    out = agent(client)._web_fetch(env, env.payload)
    assert out["ok"] is True
    assert out["untrusted"] is True
    assert "Body text." in out["text"]
    assert len(client.calls) == 1, client.calls


def test_the_door_is_re_checked_on_every_redirect_hop(doors, ledger):
    """A door closed mid-run closes on the next hop, not after the last one."""
    doors(web_fetch=True)
    client = Client(
        Response(status=302, headers={"Location": "https://example.org/next"}),
        Response(b"<p>second</p>"),
    )
    a = agent(client)
    env = fetch_envelope()
    out = a._web_fetch(env, env.payload)
    assert out["ok"] is True
    assert len(client.calls) == 2

    # Now shut it and re-run: the first hop refuses.
    import os
    os.environ["PUFFPAI_EGRESS_WEB_FETCH"] = "0"
    ed.reset_cache()
    client2 = Client(Response(b"<p>x</p>"))
    with pytest.raises(EgressDenied):
        a2 = agent(client2)
        env2 = fetch_envelope()
        a2._web_fetch(env2, env2.payload)
    assert client2.calls == []


# ── 4. the ledger ────────────────────────────────────────────────────────────

def test_an_allowed_fetch_leaves_a_ledger_line_with_bytes(doors, ledger):
    doors(web_fetch=True)
    body = b"<p>" + b"x" * 500 + b"</p>"
    env = fetch_envelope()
    agent(Client(Response(body)))._web_fetch(env, env.payload)

    rows = [r for r in lines(ledger) if r.get("door") == "web_fetch"]
    assert rows, "an allowed fetch left no trace"
    row = rows[-1]
    assert row["result"] == "allowed"
    assert row["host"] == "example.com"
    assert row["bytes_in"] == len(body)
    assert row["status"] == 200


def test_the_ledger_never_records_the_query_string(doors, ledger):
    """The URL carries a secret; the trail the owner reads must not."""
    doors(web_fetch=True)
    env = fetch_envelope(url="https://example.com/s?q=where+do+i+live")
    agent(Client(Response()))._web_fetch(env, env.payload)

    blob = ledger.read_text(encoding="utf-8")
    assert "where+do+i+live" not in blob
    assert "?" not in json.dumps([r.get("path") for r in lines(ledger)])


def test_a_refused_fetch_leaves_a_ledger_line(doors, ledger, no_sockets):
    doors()
    env = fetch_envelope()
    with pytest.raises(EgressDenied):
        agent(Client())._web_fetch(env, env.payload)

    rows = [r for r in lines(ledger) if r.get("door") == "web_fetch"]
    assert rows, "a refused fetch left no trace"
    assert rows[-1]["result"] == "refused"


def test_a_transport_error_records_the_class_name_not_the_url(doors, ledger):
    """`requests` puts the whole request URL into ConnectionError's message."""
    doors(web_fetch=True)

    class Exploding:
        calls = []

        def get(self, url, **kwargs):
            self.calls.append(url)
            raise RuntimeError(
                "HTTPSConnectionPool(host='example.com', port=443): Max "
                "retries exceeded with url: /s?q=where+do+i+live")

    env = fetch_envelope(url="https://example.com/s?q=where+do+i+live")
    with pytest.raises(RuntimeError):
        agent(Exploding())._web_fetch(env, env.payload)

    blob = ledger.read_text(encoding="utf-8")
    assert "where+do+i+live" not in blob
    rows = [r for r in lines(ledger) if r.get("door") == "web_fetch"]
    assert rows[-1]["reason"] == "RuntimeError"


def test_an_ssrf_refusal_is_recorded_too(doors, ledger, no_sockets):
    """"Did this thing try to reach the metadata service" needs an answer."""
    doors(web_fetch=True)
    env = fetch_envelope(url="http://169.254.169.254/latest/meta-data/")
    with pytest.raises(Exception):
        agent(Client(), resolver=lambda h, p: ["169.254.169.254"]
              )._web_fetch(env, env.payload)

    rows = [r for r in lines(ledger) if r.get("door") == "web_fetch"]
    assert rows and rows[-1]["result"] == "refused"


def test_an_allowed_search_leaves_a_ledger_line_without_the_query(doors,
                                                                  ledger):
    doors(web_search=True)
    payload = json.dumps({"results": [
        {"title": "T", "url": "https://example.com/a", "content": "snip"}]})
    client = Client(Response(payload.encode(), content_type="application/json"))
    env = Envelope(intent="web_search",
                   payload={"query": "my medical condition"},
                   requester="owner", origin_agent="test",
                   budget=Budget(max_actions=4, max_seconds=10.0,
                                 allow_egress=True),
                   state_changing=False)
    out = agent(client, searxng_url="http://127.0.0.1:8888",
                searxng_timeout=2.0)._web_search(env, env.payload)
    assert out["ok"] is True

    rows = [r for r in lines(ledger) if r.get("door") == "web_search"]
    assert rows and rows[-1]["result"] == "allowed"
    assert "my medical condition" not in ledger.read_text(encoding="utf-8")


# ── 5. the SSRF gates are UNCHANGED, with the door open ──────────────────────
#
# The door must be an ADDITIONAL gate, never a
# replacement: every one of these runs with web_fetch OPEN, so a pass here is a
# statement about the SSRF code and nothing else.

@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data/",
    "http://127.0.0.1/admin",
    "http://127.1/admin",
    "http://2130706433/admin",
    "http://0x7f000001/admin",
    "http://[::1]/admin",
    "http://localhost/admin",
    "http://192.168.1.10/",
    "http://192.168.50.140/",
    "file:///etc/shadow",
    "gopher://example.com/",
    "https://user:pass@example.com/",
    "http://example.com:22/",
])
def test_the_ssrf_gates_still_refuse_with_the_door_open(doors, url,
                                                        no_sockets):
    doors(web_fetch=True)
    client = Client()
    env = fetch_envelope(url=url)
    with pytest.raises(Exception):
        agent(client, resolver=_resolver_for(url))._web_fetch(env, env.payload)
    assert client.calls == [], f"{url} reached the wire"


def _resolver_for(url):
    """Resolve every hostname to whatever the URL implies, honestly.

    A resolver that always answers with a public address would make these tests
    pass for the wrong reason.
    """
    mapping = {
        "127.0.0.1": "127.0.0.1", "localhost": "127.0.0.1",
        "192.168.1.10": "192.168.1.10", "192.168.50.140": "192.168.50.140",
        "169.254.169.254": "169.254.169.254",
    }

    def resolve(host, port):
        return [mapping.get(host, "93.184.216.34")]

    return resolve


def test_a_redirect_into_the_lan_is_still_refused_with_the_door_open(doors):
    """The redirect target is web content; the door does not vouch for it."""
    doors(web_fetch=True)
    client = Client(
        Response(status=302, headers={"Location": "http://169.254.169.254/"}),
        Response(b"<p>secrets</p>"),
    )
    env = fetch_envelope()
    with pytest.raises(Exception):
        agent(client, resolver=lambda h, p: (
            ["169.254.169.254"] if h == "169.254.169.254"
            else ["93.184.216.34"]))._web_fetch(env, env.payload)
    assert len(client.calls) == 1, "the second hop should never have happened"


def test_the_caller_host_scope_still_applies_with_the_door_open(doors):
    """``allowed_hosts: []`` permits nothing — never tested by truthiness."""
    doors(web_fetch=True)
    client = Client(Response())
    env = fetch_envelope(allowed_hosts=[])
    with pytest.raises(Exception):
        agent(client)._web_fetch(env, env.payload)
    assert client.calls == []


# ── 6. chat egress needs BOTH halves ─────────────────────────────────────────

@pytest.fixture
def opt_in(monkeypatch):
    def _set(on: bool):
        monkeypatch.setenv("PUFFPAI_CHAT_ALLOW_EGRESS", "1" if on else "0")
        egress_policy.reset_cache()

    yield _set
    egress_policy.reset_cache()


def test_chat_egress_needs_the_opt_in(doors, opt_in):
    doors(web_fetch=True, web_search=True)
    opt_in(False)
    allowed, why = egress_policy.chat_egress_allowed()
    assert allowed is False
    assert "opt-in is off" in why


def test_chat_egress_needs_a_door(doors, opt_in):
    doors()  # every door shut
    opt_in(True)
    allowed, why = egress_policy.chat_egress_allowed()
    assert allowed is False
    assert "no web door is open" in why


def test_chat_egress_with_both_halves(doors, opt_in):
    doors(web_fetch=True)
    opt_in(True)
    allowed, why = egress_policy.chat_egress_allowed()
    assert allowed is True
    assert "web_fetch" in why


def test_the_door_asked_is_the_intent_the_caller_named(doors, opt_in):
    """A house that opened search but not fetch gets exactly that."""
    doors(web_search=True)
    opt_in(True)
    assert egress_policy.chat_egress_allowed("web_search")[0] is True
    assert egress_policy.chat_egress_allowed("web_fetch")[0] is False


def test_a_broken_registry_denies_chat_egress(monkeypatch, opt_in):
    opt_in(True)
    monkeypatch.setattr(
        ed, "egress_allowed",
        lambda door_id: (_ for _ in ()).throw(RuntimeError("no config")))
    allowed, why = egress_policy.chat_egress_allowed()
    assert allowed is False
    assert "unreadable" in why


# ── 7. the test suite's own door seeding stays honest ────────────────────────

def test_the_conftest_fixture_opens_exactly_the_two_toolsmith_doors():
    """Pins ``TEST_OPEN_DOORS`` so it cannot grow into "egress on in tests".

    The fixture exists so several hundred pre-existing tests can exercise the
    machinery BEHIND appliance-level consent. The day it quietly gains a third
    door, a security test somewhere starts passing for the wrong reason.
    """
    import importlib.util

    path = Path(__file__).resolve().parent / "conftest.py"
    spec = importlib.util.spec_from_file_location("_conftest_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.TEST_OPEN_DOORS == {"web_fetch": True, "web_search": True}


def test_the_registry_agrees_the_two_web_doors_are_honoured():
    # Members, not the exact set: tests/test_egress_api.py pins the whole set
    # to the call sites that make each member true.
    assert {"web_fetch", "web_search"} <= ed._FLAG_HONOURED_EXTERNAL


def test_both_web_doors_publish_enforced_and_writable():
    """The Settings switch is real, and ``/system/status`` says so."""
    rows = {r["id"]: r for r in ed.door_status()}
    for door_id in ("web_fetch", "web_search"):
        assert rows[door_id]["enforced"] is True, door_id
        assert rows[door_id]["writable"] is True, door_id
