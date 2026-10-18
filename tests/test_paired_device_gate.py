"""Paired devices: the pairing store's derived ids, and who may assert one.

The dashboard proxy verifies the pairing cookie (and that the device is still
in devices.json) on every request, strips any client copy of
X-Puff-Device-Id and asserts the derived id itself. The backend believes that
header only from the proxy's own peer (loopback, or a named proxy address):
a caller anywhere else on the home network, API key or not, cannot assert a
device and is refused by every "paired device" gate.

Each test builds a real pairing store in a temp folder (no network).
"""
from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from puffpai import api_server
from puffpai.security import paired_devices

pytestmark = pytest.mark.real_pairing

AUTH = {"Authorization": f"Bearer {api_server._API_KEY}"}
SECRET = "ab" * 32
DEVICE_ID = "Kt7bQ2xY9mZa"


def _derived(device_id: str, secret: str = SECRET) -> str:
    """What pairing.ts deviceSessionId() asserts, computed independently."""
    return hmac.new(bytes.fromhex(secret), f"puff-chat-session:v1:{device_id}".encode(),
                    hashlib.sha256).hexdigest()[:32]


@pytest.fixture
def store(tmp_path, monkeypatch):
    folder = tmp_path / "pairing"
    folder.mkdir()
    (folder / "pairing.json").write_text(json.dumps({
        "version": 1, "secret": SECRET, "code": "ABCDEFGH",
        "createdAt": "2026-10-01T00:00:00Z", "codeRotatedAt": "2026-10-01T00:00:00Z"}),
        encoding="utf-8")
    (folder / "devices.json").write_text(json.dumps({"version": 1, "devices": [
        {"id": DEVICE_ID, "name": "Kitchen tablet", "createdAt": "2026-10-01T00:00:00Z",
         "lastSeenAt": None}]}), encoding="utf-8")
    monkeypatch.setenv("PUFF_PAIRING_STATE", str(folder / "pairing.json"))
    monkeypatch.delenv("PUFF_PAIRED_DEVICES", raising=False)
    monkeypatch.setenv("PUFFPAI_STATE_DIR", str(tmp_path / "state"))
    paired_devices.forget()
    api_server.app.config.update(TESTING=True)
    return folder


@pytest.fixture
def client():
    return api_server.app.test_client()


def _as(device: str) -> dict:
    return {**AUTH, "X-Puff-Device-Id": device}


LAN = {"REMOTE_ADDR": "192.168.1.50"}
MADE_UP = ("deviceaaaa000001", "a" * 32, "0123456789abcdef0123456789abcdef")


# ── the check itself ────────────────────────────────────────────────────────

def test_the_derived_id_of_a_listed_device_is_paired(store):
    hit = paired_devices.lookup(_derived(DEVICE_ID))
    assert hit and hit["id"] == DEVICE_ID and hit["name"] == "Kitchen tablet"


@pytest.mark.parametrize("made_up", MADE_UP)
def test_a_made_up_id_is_not_paired(store, made_up):
    assert paired_devices.lookup(made_up) is None


def test_the_raw_device_id_is_not_accepted_in_place_of_the_derived_one(store):
    assert paired_devices.lookup(DEVICE_ID) is None


def test_an_id_derived_under_another_secret_is_not_paired(store):
    assert paired_devices.lookup(_derived(DEVICE_ID, "cd" * 32)) is None


# ── every gate that says "paired device" ───────────────────────────────────

def test_a_direct_lan_caller_still_cannot_assert_a_device(store):
    with api_server.app.test_request_context(
            "/things", headers=_as(_derived(DEVICE_ID)),
            environ_base={"REMOTE_ADDR": "192.168.1.50"}):
        assert api_server._get_chat_client_id() == "peer:192.168.1.50"  # noqa: SLF001


@pytest.mark.parametrize("device", (_derived(DEVICE_ID),) + MADE_UP)
def test_a_lan_caller_with_the_key_opens_no_paired_only_route(store, client, device):
    assert client.get("/things", headers=_as(device),
                      environ_base=LAN).status_code == 403
    got = client.post("/developer/terminal/open", headers=_as(device), json={},
                      environ_base=LAN)
    assert got.status_code == 403
    from puffpai import outside_api
    with api_server.app.test_request_context("/outside", headers=_as(device),
                                            environ_base=LAN):
        assert outside_api.paired_device_id() == ""


# ── the label for logs ──────────────────────────────────────────────────────

def test_the_log_label_is_the_name_and_a_short_tag_never_the_id(store):
    sid = _derived(DEVICE_ID)
    shown = paired_devices.label(f"device:{sid}")
    assert shown.startswith("Kitchen tablet (device ") and shown.endswith(")")
    assert sid not in shown and DEVICE_ID not in shown
    assert paired_devices.label(f"device:{sid}") == shown          # stable
    assert paired_devices.label("device:" + "a" * 32) == paired_devices.UNKNOWN_LABEL


def test_the_terminal_audit_line_names_the_device(store):
    from puffpai import developer_terminal
    sid = _derived(DEVICE_ID)
    from puffpai.security.audit_log import AuditLog
    developer_terminal._audit("opened", "opened", f"device:{sid}")  # noqa: SLF001
    log = AuditLog()._log_file  # noqa: SLF001
    rows = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    shown = rows[-1]["extra"]["device"]
    assert shown == paired_devices.label(f"device:{sid}")
    assert shown.startswith("Kitchen tablet (device ") and "looked like a key" not in shown
    assert sid not in log.read_text(encoding="utf-8")
