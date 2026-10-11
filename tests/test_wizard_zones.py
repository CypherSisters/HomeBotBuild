"""Tests for the wizard's zone steps: Zone 1 (Vault) and Zone 3 (Bridge).

These two steps are where a stranger sets the lock on their own data and decides
what their bot is allowed to reach, so the properties worth guarding are not
"does it return a dict" but:

* **A weak PIN is refused**, and the refusal explains itself without scolding.
* **The recovery code comes from :mod:`secrets`**, carries the entropy the
  docstring claims, and reads back the way a person actually writes it down.
* **Neither manifest block can leak secret material** - the vault block is not
  even given the PIN or the code, and nothing derived from them appears.
* **The Bridge never loosens.** A sealed build forces Drawbridge Up, and no
  combination of posture and egress tier comes back looser than it went in.
* **Zone 3 stays policy-only.** No network discovery, enforced by checking what
  the module is allowed to import.

Run:
    pytest tests/test_zones.py
"""
from __future__ import annotations

import ast
import inspect
import json
import math
import os

import pytest

from puffpai import build_manifest, zone_bridge, zone_vault

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")


def _module_imports(module) -> set[str]:
    """Top-level module names imported anywhere in a module's source."""
    tree = ast.parse(inspect.getsource(module))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


# ── Zone 1: PIN policy ───────────────────────────────────────────────────────

@pytest.mark.parametrize("pin", [
    "7hx2Qm9v",        # mixed, eight characters
    "correcthorse",    # long passphrase-ish
    "4820591736",      # ten digits, no run, no repeat
    "R4chel!2026",
])
def test_validate_pin_accepts_reasonable_choices(pin):
    ok, reason = zone_vault.validate_pin(pin)
    assert ok, f"{pin!r} rejected: {reason}"
    assert reason == ""


@pytest.mark.parametrize("pin", [
    "",             # nothing at all
    "1234",         # the classic, and too short
    "12345",        # still too short
    "abc",          # too short even mixed
    "000000",       # all the same
    "1111111111",   # all the same, long
    "aaaaaa",       # all the same, letters
    "123456",       # ascending run
    "654321",       # descending run
    "abcdef",       # ascending run, letters
    "121212",       # repeated two-character block
    "123123",       # repeated three-character block
    "password",     # famously common
    "qwerty",
    "1234 56",      # contains a space
    "12345\t6",     # contains whitespace
    "pinéé",  # non-ASCII, cannot be typed everywhere
    "4820591",      # seven digits: under the digits-only floor
])
def test_validate_pin_rejects_weak_or_unusable(pin):
    ok, reason = zone_vault.validate_pin(pin)
    assert not ok, f"{pin!r} was accepted and should not have been"
    assert reason, "a rejection must say why"
    assert reason.isascii(), "reasons reach a Windows console"
    # Helpful, not scolding, and never echoes the PIN back at the user.
    assert pin.strip() not in reason or not pin.strip()
    for word in ("stupid", "bad", "wrong", "idiot", "terrible"):
        assert word not in reason.lower()


def test_validate_pin_digit_floor_is_higher_than_mixed_floor():
    """Digits alone have a smaller alphabet, so the policy demands more of them."""
    assert zone_vault._MIN_NUMERIC_PIN_LENGTH > zone_vault._MIN_PIN_LENGTH
    # A digits-only PIN at exactly the mixed floor must still be refused.
    digits_at_mixed_floor = "4820591736"[:zone_vault._MIN_PIN_LENGTH]
    assert zone_vault.validate_pin(digits_at_mixed_floor)[0] is False
    assert zone_vault.validate_pin("7hx2Qm9v")[0] is True


def test_validate_pin_bounds_absurd_input():
    ok, reason = zone_vault.validate_pin("a1B2c3" * 40)
    assert not ok
    assert reason.isascii()


def test_validate_pin_tolerates_non_string():
    ok, reason = zone_vault.validate_pin(None)  # type: ignore[arg-type]
    assert not ok and reason


# ── Zone 1: recovery code ────────────────────────────────────────────────────

def test_zone_vault_never_imports_random():
    imports = _module_imports(zone_vault)
    assert "random" not in imports, "recovery codes must never come from random"
    assert "secrets" in imports
    assert imports <= {"__future__", "secrets", "time", "typing",
                       "puffpai.setup_ops", "puffpai.security",
                       "puffpai.security.vault"}


def test_recovery_code_draws_its_entropy_from_secrets(monkeypatch):
    """Proves the randomness source: patch secrets.token_bytes and the code
    changes.

    The code is ``secrets.token_bytes`` handed to the vault's formatter,
    because the VAULT owns the format -- see ``zone_vault``'s recovery-code
    section. The property under test is the only one that matters here: every
    bit in the code comes from :mod:`secrets`, never :mod:`random`.
    """
    from puffpai.security import vault as vault_mod

    seen: list[int] = []

    def fake_token_bytes(n):
        seen.append(n)
        return b"\x00" * n

    monkeypatch.setattr(zone_vault.secrets, "token_bytes", fake_token_bytes)
    code = zone_vault.generate_recovery_code()

    assert seen == [vault_mod._RECOVERY_CODE_BYTES]
    assert code == vault_mod._format_recovery_code(
        b"\x00" * vault_mod._RECOVERY_CODE_BYTES)
    assert set(code.replace("-", "")) <= set(zone_vault.RECOVERY_ALPHABET)


def test_recovery_code_format_is_hand_transcribable():
    code = zone_vault.generate_recovery_code()
    groups = code.split("-")
    assert len(groups) == zone_vault.RECOVERY_GROUPS
    assert all(len(g) == zone_vault.RECOVERY_GROUP_LENGTH for g in groups)
    assert code == code.upper()
    assert code.isascii()
    assert all(ch in zone_vault.RECOVERY_ALPHABET for ch in code.replace("-", ""))


def test_the_recovery_alphabet_is_the_vaults_own():
    """One source of truth, and it is the half that holds the key.

    A hand-transcription alphabet (no 0/O, no 1/I, no vowels) defined by
    ``zone_vault`` for itself would be nicer and would be fiction:
    ``security/vault.py`` mints base32 and ``recover_with_code`` normalises the
    typed code to ``[A-Z2-7]``, so a code in another alphabet could be shown to
    an owner, written down, typed back, confirmed -- and then open nothing. The
    tradeoff recorded here on purpose: 0/O and 1/I are absent because they are
    not base32 digits, while S/5 and B/8 remain confusable. Improving that
    means changing the VAULT's encoding and everything that has already
    redeemed one, not re-deciding it here.
    """
    from puffpai.security import vault as vault_mod

    alphabet = zone_vault.RECOVERY_ALPHABET
    assert len(set(alphabet)) == len(alphabet), "no duplicate symbols"
    for ch in "0189":
        assert ch not in alphabet
    # Exactly the characters the vault's own normaliser keeps: anything else
    # would be dropped on the way to the KDF.
    kept = vault_mod._normalize_recovery_code(
        "".join(chr(c) for c in range(0x20, 0x7F)))
    assert set(alphabet) == set(kept)


def test_recovery_code_entropy_matches_the_documented_number():
    expected = round(
        zone_vault.RECOVERY_CODE_LENGTH * math.log2(len(zone_vault.RECOVERY_ALPHABET)), 1
    )
    assert zone_vault.RECOVERY_CODE_ENTROPY_BITS == expected
    # Close enough to the AES-128 key it sits in front of that the cipher, not
    # the handwritten code, is the weak link.
    assert 110 <= zone_vault.RECOVERY_CODE_ENTROPY_BITS <= 128


def test_recovery_codes_do_not_repeat():
    codes = {zone_vault.generate_recovery_code() for _ in range(250)}
    assert len(codes) == 250


# ── Zone 1: confirmation ─────────────────────────────────────────────────────

def test_confirmation_accepts_the_exact_code():
    code = zone_vault.generate_recovery_code()
    assert zone_vault.verify_recovery_confirmation(code, code)


@pytest.mark.parametrize("mangle", [
    lambda c: c.lower(),                       # typed in lower case
    lambda c: c.replace("-", " "),             # spaces instead of hyphens
    lambda c: c.replace("-", ""),              # ran it all together
    lambda c: f"  {c}  ",                      # leading/trailing whitespace
    lambda c: c.replace("-", "\n"),            # copied off a card, one line each
    lambda c: c.lower().replace("-", " . "),   # stray punctuation
])
def test_confirmation_forgives_how_a_person_writes_it_down(mangle):
    code = zone_vault.generate_recovery_code()
    assert zone_vault.verify_recovery_confirmation(code, mangle(code))


@pytest.mark.parametrize("wrong", [
    "",
    "   ",
    "-----",
    "QQQQQ-QQQQQ-QQQQQ-QQQQQ-QQQQQ",
])
def test_confirmation_rejects_wrong_or_empty(wrong):
    code = zone_vault.generate_recovery_code()
    assert not zone_vault.verify_recovery_confirmation(code, wrong)


def test_confirmation_rejects_a_single_character_slip():
    code = zone_vault.generate_recovery_code()
    flipped = "K" if code[0] != "K" else "M"
    assert not zone_vault.verify_recovery_confirmation(code, flipped + code[1:])


def test_confirmation_rejects_a_truncated_code():
    code = zone_vault.generate_recovery_code()
    assert not zone_vault.verify_recovery_confirmation(code, code[:-1])
    assert not zone_vault.verify_recovery_confirmation("", code)


# ── Zone 1: the manifest block leaks nothing ─────────────────────────────────

_VAULT_BLOCK_KEYS = {
    "configured", "sealed", "method", "recovery_issued", "recovery_confirmed",
    "recovery_escrow", "configured_at", "key_derivation", "cipher",
}


def test_vault_block_cannot_be_handed_a_secret_at_all():
    """The strongest guarantee available: the function never takes one."""
    params = inspect.signature(zone_vault.vault_manifest_block).parameters
    for name in params:
        for banned in ("pin", "code", "secret", "password", "salt", "hash", "key"):
            assert banned not in name.lower(), f"parameter {name!r} could carry a secret"


def test_vault_block_records_only_status_flags():
    block = zone_vault.vault_manifest_block(
        configured=True, sealed=True, method="pin",
        recovery_issued=True, recovery_confirmed=True,
    )
    assert set(block) == _VAULT_BLOCK_KEYS
    assert block["configured"] is True
    assert block["sealed"] is True
    assert block["method"] == "pin"
    assert isinstance(block["configured_at"], float)


def test_vault_block_serializes_without_any_secret_material():
    pin = "7hx2Qm-real-pin"
    code = zone_vault.generate_recovery_code()
    block = zone_vault.vault_manifest_block(recovery_issued=True, recovery_confirmed=True)
    blob = json.dumps(block).upper()

    assert pin.upper() not in blob
    assert code.upper() not in blob
    assert code.replace("-", "").upper() not in blob
    for group in code.split("-"):
        assert group not in blob
    # And nothing that would let someone verify a guess offline.
    for marker in ("SALT", "DIGEST", "HASH_", "_HASH", "VERIFIER", "CIPHERTEXT"):
        assert marker not in blob


def test_vault_block_unconfigured_has_no_timestamp_and_is_not_sealed():
    block = zone_vault.vault_manifest_block(configured=False)
    assert block["configured"] is False
    assert block["sealed"] is False
    assert block["configured_at"] is None


def test_vault_block_unknown_method_is_not_silently_accepted():
    block = zone_vault.vault_manifest_block(method="fingerprint-of-my-cat")
    assert block["method"] == "unspecified"


# ── Zone 1: copy and sealing ─────────────────────────────────────────────────

def test_recovery_screen_states_the_three_hard_facts():
    screen = " ".join(zone_vault.recovery_notice_lines()).lower()
    assert "only way back" in screen
    assert "cannot recover it" in screen
    assert "no reset email" in screen and "there is no us" in screen


def test_all_wizard_copy_is_ascii():
    text = " ".join([
        zone_vault.RECOVERY_NOTICE_TITLE,
        zone_vault.RECOVERY_SHOWN_ONCE_WARNING,
        zone_vault.RECOVERY_CONFIRM_PROMPT,
        zone_vault.RECOVERY_CONFIRM_FAILED,
        zone_vault.RECOVERY_UNWIRED_WARNING,
        *zone_vault.RECOVERY_NOTICE_LINES,
        *zone_vault.PIN_INTRO_LINES,
        *zone_vault.recovery_notice_lines(),
    ])
    assert text.isascii()


def test_the_escrow_is_live_so_the_recovery_code_means_something():
    """The code is redeemable now; the unwired warning is retired."""
    assert zone_vault.RECOVERY_ESCROW_AVAILABLE is True
    screen = " ".join(zone_vault.recovery_notice_lines())
    assert zone_vault.RECOVERY_UNWIRED_WARNING not in screen
    assert zone_vault.vault_manifest_block()["recovery_escrow"] == "wrapped-key"


def test_the_ceremony_can_never_ship_without_the_mechanism_behind_it():
    """The gate. A flag flipped without a real escrow must fail right here.

    This is the test that stops the wizard promising a way back in that does not
    exist: it does not trust RECOVERY_ESCROW_AVAILABLE, it checks the vault
    actually carries the methods that redeem a code.
    """
    from puffpai.security.vault import Vault

    screen = " ".join(zone_vault.recovery_notice_lines())
    block = zone_vault.vault_manifest_block()

    if zone_vault.RECOVERY_ESCROW_AVAILABLE:
        assert block["recovery_escrow"] == "wrapped-key"
        assert zone_vault.RECOVERY_UNWIRED_WARNING not in screen
        # ADAPTED FOR HOMEBOTBUILD: BuildBot's vault spelled the escrow
        # create_with_recovery/add_recovery_code/redeem_recovery_code. This
        # build's vault spells the same mechanism initialize + authenticate +
        # generate_recovery_code + recover_with_code (the code wraps the same
        # data key the PIN wraps). The gate is unchanged in substance: the
        # ceremony may not ship unless the methods that redeem a code exist.
        for method in ("initialize", "authenticate", "generate_recovery_code",
                       "recover_with_code"):
            assert callable(getattr(Vault, method, None)),                 f"escrow claims to be live but Vault.{method} does not exist"
    else:
        assert block["recovery_escrow"] == "none"
        assert zone_vault.RECOVERY_UNWIRED_WARNING in screen


def test_seal_vault_with_recovery_produces_a_code_that_actually_opens_the_vault(tmp_path):
    """End to end: the wizard's own path, through the real crypto."""
    from puffpai.security.vault import Vault

    handle = Vault(vault_dir=str(tmp_path / "wizard"))
    block, code = zone_vault.seal_vault_with_recovery("7hx2Qm4T", vault=handle)

    assert block["recovery_issued"] is True
    assert block["recovery_escrow"] == "wrapped-key"
    # ADAPTED: this build's Vault has no `is_unlocked` property (that name is a
    # method taking a session id); `has_live_session` is the fail-closed read.
    assert not handle.has_live_session, "the wizard leaves the vault sealed"
    # The code is a separate return value, never a field a caller might persist.
    assert code not in json.dumps(block)
    assert zone_vault.verify_recovery_confirmation(code, code.lower().replace("-", " "))

    # ADAPTED: recover_with_code() is this build's redeem_recovery_code() - it
    # spends the code, sets a new PIN and hands back a live session.
    recovering = Vault(vault_dir=str(tmp_path / "wizard"))
    session = recovering.recover_with_code(code, "a-new-strong-pin")
    assert recovering.is_unlocked(session.session_id)


class _FakeVault:
    """Stands in for the real vault so tests never run a 480k-iteration KDF.

    ADAPTED FOR HOMEBOTBUILD: shaped like THIS build's vault
    (initialize/authenticate/generate_recovery_code/logout) rather than
    BuildBot's create/lock, because that is the API
    :func:`puffpai.setup_ops.create_vault` now drives. ``locked`` still means
    what it meant: the wizard did not leave a session open behind it.
    """

    class _Session:
        session_id = "fake-session"

    def __init__(self) -> None:
        self.created_with: list[str] = []
        self.locked = False

    def initialize(self, password: str) -> None:
        self.created_with.append(password)

    def authenticate(self, password: str):
        return self._Session()

    def generate_recovery_code(self, session_id: str) -> str:
        return "FAKE-CODE"

    def logout(self, session_id: str) -> None:
        self.locked = True


def test_seal_vault_hands_the_pin_straight_through_and_keeps_nothing():
    fake = _FakeVault()
    block = zone_vault.seal_vault("7hx2Qm4T", vault=fake)

    assert fake.created_with == ["7hx2Qm4T"]
    assert fake.locked, "the vault is left sealed, not open"
    assert set(block) == _VAULT_BLOCK_KEYS
    assert "7hx2Qm4T" not in json.dumps(block)


def test_seal_vault_refuses_a_weak_pin_before_touching_the_vault():
    fake = _FakeVault()
    with pytest.raises(ValueError):
        zone_vault.seal_vault("1234", vault=fake)
    assert fake.created_with == [], "a rejected PIN must never reach the vault"


# ── Zone 3: the four postures ────────────────────────────────────────────────

_POSTURE_IDS = ["open_yard", "fenced", "moat", "drawbridge_up"]


def test_shipped_posture_file_is_valid_and_complete():
    menu = zone_bridge.posture_menu(zone_bridge.load_postures())
    assert [p["id"] for p in menu] == _POSTURE_IDS   # loosest first
    assert zone_bridge.load_postures() is not zone_bridge._FALLBACK_POSTURES, \
        "the shipped config/bridge_postures.json should be the one that loads"


@pytest.mark.parametrize("source", ["file", "fallback"])
def test_every_posture_is_coherent_and_ascii(source):
    data = (zone_bridge.load_postures() if source == "file"
            else zone_bridge._FALLBACK_POSTURES)
    menu = zone_bridge.posture_menu(data)
    assert len(menu) == 4
    for entry in menu:
        assert entry["label"] and entry["summary"] and entry["costs"]
        allows = entry["allows"]
        for axis in ("lan_devices", "actions", "egress"):
            assert allows.get(axis), f"{entry['id']} does not say what it does about {axis}"
        text = " ".join([entry["label"], entry["summary"], entry["costs"], *allows.values()])
        assert text.isascii()


def test_a_broken_posture_file_falls_back_rather_than_offering_a_partial_menu(tmp_path):
    partial = tmp_path / "bridge_postures.json"
    partial.write_text(json.dumps({
        "schema": "bridge_postures/v1",
        "postures": [{"id": "open_yard", "label": "Open Yard"}],
    }), encoding="utf-8")
    assert zone_bridge.load_postures(str(partial)) is zone_bridge._FALLBACK_POSTURES
    assert zone_bridge.load_postures(str(tmp_path / "nope.json")) is zone_bridge._FALLBACK_POSTURES


def test_strictness_ordering_comes_from_code_not_from_the_file(tmp_path):
    """A hand-edited file can change the wording but never the strictness."""
    tampered = tmp_path / "bridge_postures.json"
    doc = json.loads(json.dumps(zone_bridge._FALLBACK_POSTURES))
    for entry in doc["postures"]:
        entry["strictness"] = 99 if entry["id"] == "open_yard" else 0
        entry["recommendable"] = True
    tampered.write_text(json.dumps(doc), encoding="utf-8")

    menu = zone_bridge.posture_menu(zone_bridge.load_postures(str(tampered)))
    assert [p["id"] for p in menu] == _POSTURE_IDS
    assert [p["strictness"] for p in menu] == [0, 1, 2, 3]
    assert menu[0]["recommendable"] is False, "open_yard stays un-suggestable"


def test_zone_bridge_is_policy_only_and_cannot_discover_anything():
    """The device-classification cascade must not exist in the open base."""
    imports = _module_imports(zone_bridge)
    assert imports <= {"__future__", "json", "os", "time", "typing", "puffpai.config"}, \
        f"unexpected import in a policy-only module: {imports}"
    for banned in ("socket", "subprocess", "urllib", "requests", "http.client",
                   "ipaddress", "scapy", "zeroconf", "netifaces", "asyncio"):
        assert banned not in imports


# ── Zone 3: recommendation ───────────────────────────────────────────────────

@pytest.mark.parametrize("ha,fw,lan", [
    (a, b, c) for a in (False, True) for b in (False, True) for c in (False, True)
])
def test_recommendation_never_suggests_open_yard_and_always_shows_all_four(ha, fw, lan):
    rec = zone_bridge.posture_for_environment(ha, fw, lan)
    assert rec["recommended"] in _POSTURE_IDS
    assert rec["recommended"] != "open_yard"
    assert [p["id"] for p in rec["postures"]] == _POSTURE_IDS
    assert rec["reason"] and rec["reason"].isascii()
    assert rec["default"] == "fenced"


def test_an_owner_with_no_security_gear_still_configures_the_bridge():
    """The whole point of the step: no gear is not a reason to skip it."""
    rec = zone_bridge.posture_for_environment(False, False, False)
    assert len(rec["postures"]) == 4
    assert rec["recommended"] == "moat"
    assert "no firewall" in rec["reason"].lower()
    # And it is offered as a suggestion, never applied.
    assert "not a setting" in rec["note"]


def test_home_assistant_gets_a_posture_that_can_still_reach_the_house():
    rec = zone_bridge.posture_for_environment(True, False, False)
    assert rec["recommended"] == "fenced"
    assert "home assistant" in rec["reason"].lower()


def test_existing_gear_does_not_buy_a_looser_bot():
    rec = zone_bridge.posture_for_environment(False, True, True)
    assert rec["recommended"] == "fenced"


def test_posture_summary_lines_are_printable_ascii():
    lines = zone_bridge.posture_summary_lines(
        zone_bridge.posture_for_environment(True, False, False)
    )
    assert lines and all(line.isascii() for line in lines)


# ── Zone 3: reconciliation with run_egress ───────────────────────────────────

@pytest.mark.parametrize("posture", _POSTURE_IDS)
def test_sealed_egress_forces_drawbridge_up(posture):
    result, why = zone_bridge.reconcile_with_egress(posture, "sealed")
    assert result == "drawbridge_up"
    assert why and why.isascii()
    if posture != "drawbridge_up":
        assert "stricter" in why.lower()


def test_island_egress_will_not_stand_behind_open_yard():
    result, why = zone_bridge.reconcile_with_egress("open_yard", "island")
    assert result == "fenced"
    assert "stricter" in why.lower()


def test_island_egress_leaves_a_stricter_choice_alone():
    assert zone_bridge.reconcile_with_egress("moat", "island")[0] == "moat"
    assert zone_bridge.reconcile_with_egress("drawbridge_up", "island")[0] == "drawbridge_up"


def test_enumerated_egress_permits_the_posture_the_owner_picked():
    for posture in _POSTURE_IDS:
        assert zone_bridge.reconcile_with_egress(posture, "enumerated")[0] == posture


@pytest.mark.parametrize("posture", _POSTURE_IDS + ["", "  ", "OPEN_YARD", "wide_open", None])
@pytest.mark.parametrize("egress", ["sealed", "island", "enumerated", "", "off", None])
def test_reconciliation_never_loosens_and_never_returns_junk(posture, egress):
    result, why = zone_bridge.reconcile_with_egress(posture, egress)  # type: ignore[arg-type]
    assert result in zone_bridge._STRICTNESS
    assert why and why.isascii()

    key = str(posture or "").strip().lower()
    if key in zone_bridge._STRICTNESS:
        assert zone_bridge._STRICTNESS[result] >= zone_bridge._STRICTNESS[key], \
            "reconciliation loosened a posture the owner chose"
    else:
        # Unrecognised input fails closed to the default, not to the loosest.
        assert result != "open_yard"


def test_unknown_egress_applies_the_safe_floor_not_no_floor():
    result, why = zone_bridge.reconcile_with_egress("open_yard", "who-knows")
    assert result == "fenced"
    assert "did not recognise" in why.lower()


def test_unchanged_reconciliation_still_explains_itself():
    result, why = zone_bridge.reconcile_with_egress("fenced", "island")
    assert result == "fenced"
    assert "nothing was changed" in why.lower()


# ── Zone 3: the manifest block ───────────────────────────────────────────────

def test_bridge_block_records_the_reconciled_truth_not_the_request():
    block = zone_bridge.bridge_manifest_block("open_yard", run_egress="sealed")
    assert block["posture"] == "drawbridge_up"
    assert block["requested_posture"] == "open_yard"
    assert block["reconciled"] is True
    assert block["chosen_by"] == "override"
    assert block["strictness"] == 3
    assert block["enforced_by"] == "Drawbridge"


def test_bridge_block_without_egress_keeps_the_choice():
    block = zone_bridge.bridge_manifest_block(
        "fenced",
        environment={"home_assistant": True, "external_firewall": False, "managed_lan": False},
    )
    assert block["posture"] == "fenced"
    assert block["reconciled"] is False
    assert block["run_egress"] is None
    assert block["environment"] == {
        "home_assistant": True, "external_firewall": False, "managed_lan": False,
    }
    assert block["chosen_by"] == "owner"


def test_bridge_block_fails_closed_on_an_unknown_posture():
    block = zone_bridge.bridge_manifest_block("anything_goes")
    assert block["posture"] == "fenced"
    assert block["note"]


def test_bridge_block_environment_is_always_three_plain_booleans():
    block = zone_bridge.bridge_manifest_block("fenced", environment={"home_assistant": "yes"})
    env = block["environment"]
    assert set(env) == {"home_assistant", "external_firewall", "managed_lan"}
    assert all(isinstance(v, bool) for v in env.values())


# ── Both blocks against the contract guard ───────────────────────────────────

def test_zone_blocks_carry_no_contract_disabling_key():
    blocks = [
        zone_vault.vault_manifest_block(recovery_issued=True, recovery_confirmed=True),
        zone_bridge.bridge_manifest_block("moat", run_egress="island"),
    ]
    for block in blocks:
        for key in block:
            assert key not in build_manifest._FORBIDDEN_KEYS
        for key in block.get("allows", {}):
            assert key not in build_manifest._FORBIDDEN_KEYS


def test_zone_blocks_survive_sanitize_manifest_untouched():
    """The contract guard must have nothing to strip out of either block."""
    vault_block = zone_vault.vault_manifest_block(recovery_issued=True, recovery_confirmed=True)
    bridge_block = zone_bridge.bridge_manifest_block(
        "fenced", run_egress="island",
        environment={"home_assistant": True, "external_firewall": False, "managed_lan": True},
    )
    manifest = build_manifest.assemble_manifest({"hardware_class": "mid"})
    manifest["vault"] = vault_block
    manifest["bridge"] = bridge_block

    assert build_manifest.validate_manifest(manifest) == []
    cleaned = build_manifest.sanitize_manifest(manifest)
    assert cleaned["vault"] == vault_block
    assert cleaned["bridge"] == bridge_block


def test_a_written_manifest_holding_both_blocks_has_no_secret_in_it(tmp_path):
    pin = "7hx2Qm4T"
    code = zone_vault.generate_recovery_code()
    manifest = build_manifest.assemble_manifest({"hardware_class": "mid"})
    manifest["vault"] = zone_vault.vault_manifest_block(
        recovery_issued=True, recovery_confirmed=True
    )
    manifest["bridge"] = zone_bridge.bridge_manifest_block("moat", run_egress="island")

    target = str(tmp_path / "build_manifest.json")
    build_manifest.write_manifest(manifest, target)
    on_disk = open(target, "r", encoding="utf-8").read().upper()

    assert pin.upper() not in on_disk
    assert code.replace("-", "").upper() not in on_disk
    for group in code.split("-"):
        assert group not in on_disk


def test_vault_and_bridge_summary_lines_are_ascii():
    lines = zone_vault.vault_summary_lines(
        zone_vault.vault_manifest_block(recovery_issued=True, recovery_confirmed=True)
    )
    lines += zone_vault.vault_summary_lines(zone_vault.vault_manifest_block(configured=False))
    assert lines and all(line.isascii() for line in lines)


def test_zone_module_sources_are_pure_ascii():
    """These modules print to a Windows console; keep the source honest too."""
    for name in ("zone_vault.py", "zone_bridge.py"):
        text = open(os.path.join(_SRC, "puffpai", name), "r", encoding="utf-8").read()
        assert text.isascii(), f"{name} contains a non-ASCII character"
    postures = os.path.join(os.path.dirname(_SRC), "config", "bridge_postures.json")
    assert open(postures, "r", encoding="utf-8").read().isascii()
