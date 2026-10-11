"""LAN posture: the ``island`` promise, checked against the configured URLs.

``security/lan_posture`` did not exist in this build, so
``setup_ops._check_lan_posture`` -- which is called on every config write --
silently did nothing, and ``--ha-url http://8.8.8.8:8123 --ha-token <token>``
was accepted and written under any posture.

Three surfaces, three behaviours, and this file pins each: the write path
refuses outright, config load neutralises instead of raising, and
``posture_check`` reports both.

No test here opens a socket or makes a DNS query: every resolving test installs
its own resolver, and ``tests/conftest.py`` makes the default "nothing
resolves".
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from puffpai.security import lan_posture as lp  # noqa: E402

PUBLIC_ADDR = "93.184.216.34"
FAKE_TOKEN = "FAKE-HA-TOKEN-0d1e2f3a4b5c"


def _resolver(answers):
    """A resolver that answers every host with *answers*."""
    return lambda host, port: list(answers)


# ── classification ──────────────────────────────────────────────────────────

class TestClassification:

    @pytest.mark.parametrize("host,expected", [
        ("127.0.0.1", lp.LOOPBACK),
        ("127.0.1.1", lp.LOOPBACK),
        ("::1", lp.LOOPBACK),
        ("0.0.0.0", lp.LOOPBACK),
        ("localhost", lp.LOOPBACK),
        ("::ffff:127.0.0.1", lp.LOOPBACK),      # loopback wearing a costume
        ("192.168.7.50", lp.PRIVATE),
        ("10.0.0.5", lp.PRIVATE),
        ("192.168.50.1", lp.PRIVATE),
        ("fd00::1", lp.PRIVATE),
        ("homeassistant.local", lp.LAN_PRESUMED),
        ("orin", lp.LAN_PRESUMED),
        ("printer.lan", lp.LAN_PRESUMED),
        ("8.8.8.8", lp.PUBLIC),
        ("someone-elses-box.example.com", lp.PUBLIC),
        ("", lp.UNPARSEABLE),
    ])
    def test_name_form_verdicts(self, host, expected):
        assert lp.classify_host(host) == expected

    @pytest.mark.parametrize("host", [
        "169.254.169.254",    # cloud metadata
        "100.64.1.1",         # CGNAT
        "203.0.113.5",        # RFC 5737 documentation
        "198.18.0.1",         # RFC 2544 benchmark
        "2001:db8::1",        # RFC 3849 documentation
    ])
    def test_the_ranges_python_calls_private_but_a_house_does_not(self, host):
        """``ipaddress`` reports several of these as private, which is true of
        their routing and false of their meaning: nothing in a house answers on
        203.0.113.5, and 169.254.169.254 is the cloud-metadata address. Under a
        posture that promises 'your home network' they are refused."""
        assert lp.classify_host(host) == lp.PUBLIC

    def test_an_unbracketed_ipv6_literal_fails_closed(self):
        """'http://2001:db8::1:11434' parses with hostname '2001', which is a
        single label -- the most permissive class this module has."""
        assert lp.host_of("http://2001:db8::1:11434") == ""
        violation = lp.check_url("ollama", "http://2001:db8::1:11434", "island")
        assert violation is not None
        assert violation.kind == lp.UNPARSEABLE

    def test_the_worst_resolved_answer_wins(self):
        kind, detail = lp.classify_host_resolving(
            "split.example", resolver=_resolver(["192.168.1.4", PUBLIC_ADDR]))
        assert kind == lp.PUBLIC
        assert PUBLIC_ADDR in detail

    def test_a_single_label_name_a_search_domain_sends_offsite(self):
        """The whole reason the write path resolves: the name-form check says
        LAN, the resolver says otherwise, and the resolver is right."""
        assert lp.classify_host("orin") == lp.LAN_PRESUMED
        violation = lp.check_url("ollama", "http://orin:11434", "island",
                                 resolve=True,
                                 resolver=_resolver([PUBLIC_ADDR]))
        assert violation is not None
        assert violation.kind == lp.PUBLIC

    def test_resolving_may_only_make_the_verdict_worse(self):
        """A ``.local`` that is merely switched off is not a breach; a
        registered-looking domain that answers nothing today still is."""
        def _nothing(host, port):
            raise OSError("NXDOMAIN")

        assert lp.check_url("home_assistant", "http://ha.local:8123", "island",
                            resolve=True, resolver=_nothing) is None
        violation = lp.check_url("ollama", "http://brain.example.com:11434",
                                 "island", resolve=True, resolver=_nothing)
        assert violation is not None
        assert violation.kind == lp.PUBLIC


# ── absence is not a limitation ─────────────────────────────────────────────

class TestAbsenceIsNotAViolation:

    @pytest.mark.parametrize("url", ["", "   ", None])
    def test_an_unset_url_is_never_a_violation(self, url):
        assert lp.check_url("home_assistant", url, "sealed") is None

    def test_a_disabled_integration_is_not_reported(self):
        """``ha.url`` ships as homeassistant.local on a box where Home
        Assistant was never set up. Reporting that as a breach buries the ones
        that matter."""
        cfg = SimpleNamespace(
            ha=SimpleNamespace(url="http://homeassistant.local:8123",
                               enabled=False),
            ollama=SimpleNamespace(url="http://localhost:11434"))
        assert lp.check_config(cfg, "sealed") == []

    def test_the_same_url_enabled_is_reported(self):
        cfg = SimpleNamespace(
            ha=SimpleNamespace(url="http://homeassistant.local:8123",
                               enabled=True),
            ollama=SimpleNamespace(url="http://localhost:11434"))
        found = lp.check_config(cfg, "sealed")
        assert [v.service for v in found] == ["home_assistant"]


# ── posture rules ───────────────────────────────────────────────────────────

class TestPostureRules:

    def test_sealed_allows_only_this_machine(self):
        assert lp.allowed_classifications("sealed") == frozenset({lp.LOOPBACK})

    def test_island_adds_the_home_network_and_nothing_else(self):
        allowed = lp.allowed_classifications("island")
        assert lp.PRIVATE in allowed and lp.LAN_PRESUMED in allowed
        assert lp.PUBLIC not in allowed

    def test_enumerated_does_not_loosen_the_lan(self):
        assert (lp.allowed_classifications("enumerated")
                == lp.allowed_classifications("island"))

    def test_an_unknown_posture_fails_shut(self):
        assert lp.allowed_classifications("wide_open") == \
            lp.allowed_classifications("sealed")
        assert lp.allowed_classifications("") == \
            lp.allowed_classifications("sealed")

    def test_sealed_says_the_right_thing_about_a_lan_address(self):
        """Not public -- just not this machine. A message that called the
        household's own NAS 'the public internet' would not be believed."""
        violation = lp.check_url("home_assistant", "http://192.168.7.50:8123",
                                 "sealed")
        assert "on your home network but not" in violation.user_message
        assert "'island'" in violation.user_message


# ── the write path refuses, and says so in one sentence ─────────────────────

class TestTheWritePathRefuses:

    def test_a_public_ha_url_is_refused_before_the_token_lands(self, tmp_path,
                                                               monkeypatch):
        from puffpai import setup_ops
        from puffpai.config import get_config

        monkeypatch.setenv("PUFFPAI_CONFIG_DIR", str(tmp_path))
        get_config(_force_reload=True)
        with pytest.raises(lp.PostureViolation) as exc:
            setup_ops.set_ha_config("http://8.8.8.8:8123", FAKE_TOKEN,
                                    posture="island")
        assert "ha.url" in exc.value.user_message
        assert not list(tmp_path.glob("home_assistant*")), (
            "a refused write left the token on disk")

    def test_the_refusal_is_one_plain_sentence_a_person_can_act_on(self):
        violation = lp.check_url("ollama", "http://brain.example.com:11434",
                                 "island")
        message = violation.user_message
        assert "ollama.url" in message
        assert "'island'" in message
        assert "192.168" in message              # what to do instead
        assert "prompt" in message               # what it would have cost
        assert message == message.strip()

    def test_a_public_ollama_url_is_refused_on_the_shared_write_path(
            self, tmp_path, monkeypatch):
        from puffpai import setup_ops
        from puffpai.config import get_config

        monkeypatch.setenv("PUFFPAI_CONFIG_DIR", str(tmp_path))
        get_config(_force_reload=True)
        with pytest.raises(lp.PostureViolation):
            setup_ops.write_main_config(
                {"ollama": {"url": "http://someone-elses-box.example.com"}})

    def test_a_first_run_with_no_manifest_is_judged_sealed(self, tmp_path):
        """'No manifest' and 'a manifest that says island' are not the same
        fact, and collapsing them is how --run-egress sealed --ollama-url
        <public> gets accepted on a fresh box."""
        lp.clear_active_posture()
        assert lp.write_path_posture(str(tmp_path)) == "sealed"

    def test_the_posture_this_run_declared_wins_over_the_disk(self, tmp_path):
        (tmp_path / "build_manifest.json").write_text(json.dumps(
            {"posture": {"run_egress": "island"}}), encoding="utf-8")
        assert lp.write_path_posture(str(tmp_path)) == "island"
        try:
            lp.set_active_posture({"run_egress": "sealed"})
            assert lp.write_path_posture(str(tmp_path)) == "sealed"
        finally:
            lp.clear_active_posture()

    def test_a_junk_posture_cannot_be_used_to_invent_a_permissive_tier(self):
        try:
            lp.set_active_posture("wide_open")
            assert lp.active_posture() != "wide_open"
            assert lp.PUBLIC not in lp.allowed_classifications(
                lp.active_posture())
        finally:
            lp.clear_active_posture()

    def test_persist_posture_declares_it_for_the_rest_of_the_run(
            self, tmp_path, monkeypatch):
        from puffpai import setup_ops
        from puffpai.config import get_config

        monkeypatch.setenv("PUFFPAI_CONFIG_DIR", str(tmp_path))
        get_config(_force_reload=True)
        setup_ops.persist_posture(
            setup_ops.assess_posture("offline", "sealed", None))
        assert lp.active_posture() == "sealed"


# ── config load neutralises, and never raises ───────────────────────────────

class TestConfigLoadNeutralises:

    def _cfg(self, **over):
        base = SimpleNamespace(
            ollama=SimpleNamespace(url="http://localhost:11434"),
            ha=SimpleNamespace(url="http://homeassistant.local:8123",
                               enabled=True),
            media_server=SimpleNamespace(
                jellyfin_url="http://192.168.1.9:8096",
                calibre_web_url="http://192.168.1.9:8083", enabled=True),
        )
        for key, value in over.items():
            setattr(base, key, value)
        return base

    def test_a_public_ollama_url_is_reset_to_the_safe_default(self):
        cfg = self._cfg(ollama=SimpleNamespace(
            url="https://someone-elses-box.example.com"))
        violations = lp.enforce(cfg, "island")
        assert [v.service for v in violations] == ["ollama"]
        assert cfg.ollama.url == "http://localhost:11434"

    def test_a_violating_optional_integration_is_switched_off(self):
        cfg = self._cfg(ha=SimpleNamespace(url="http://8.8.8.8:8123",
                                           enabled=True))
        lp.enforce(cfg, "island")
        assert cfg.ha.enabled is False

    def test_enforce_never_raises_on_a_junk_config(self):
        cfg = SimpleNamespace(ollama=SimpleNamespace(url=None), ha=None)
        assert lp.enforce(cfg, "island") == []

    def test_load_does_not_resolve(self, monkeypatch):
        """Startup must not hang on a resolver, so the name-form check is the
        only one used here."""
        def _never(*a, **k):        # pragma: no cover - must not be reached
            raise AssertionError("config load resolved a name")

        monkeypatch.setattr(lp, "_resolve", _never)
        monkeypatch.setattr(lp, "_resolver_override", _never)
        lp.enforce(self._cfg(), "island")

    def test_the_real_loader_is_wired_to_this(self, tmp_path, monkeypatch):
        """End to end: a public ollama.url in a config file on disk does not
        survive into the config object the appliance runs on."""
        from puffpai.config import get_config

        (tmp_path / "puffpai_config.json").write_text(json.dumps(
            {"ollama": {"url": "https://someone-elses-box.example.com",
                        "model": "test-model"}}), encoding="utf-8")
        (tmp_path / "build_manifest.json").write_text(json.dumps(
            {"posture": {"run_egress": "island"}}), encoding="utf-8")
        monkeypatch.setenv("PUFFPAI_CONFIG_DIR", str(tmp_path))

        cfg = get_config(_force_reload=True)
        assert cfg.ollama.url == "http://localhost:11434"
        assert [v.service for v in lp.load_time_violations()] == ["ollama"]

    def test_a_loopback_config_is_left_completely_alone(self, tmp_path,
                                                        monkeypatch):
        from puffpai.config import get_config

        (tmp_path / "puffpai_config.json").write_text(json.dumps(
            {"ollama": {"url": "http://192.168.1.50:11434"}}), encoding="utf-8")
        (tmp_path / "build_manifest.json").write_text(json.dumps(
            {"posture": {"run_egress": "island"}}), encoding="utf-8")
        monkeypatch.setenv("PUFFPAI_CONFIG_DIR", str(tmp_path))

        cfg = get_config(_force_reload=True)
        assert cfg.ollama.url == "http://192.168.1.50:11434"
        assert lp.load_time_violations() == []


# ── the report ──────────────────────────────────────────────────────────────

class TestPostureCheckReport:

    def test_ok_only_when_nothing_is_wrong_and_nothing_was_neutralised(self):
        lp.record_load_violations([])
        cfg = SimpleNamespace(
            CONFIG_DIR="", ollama=SimpleNamespace(url="http://127.0.0.1:11434"),
            ha=SimpleNamespace(url="http://8.8.8.8:8123", enabled=False))
        block = lp.posture_check(cfg, resolve=False)
        assert block["lan_services"] == "ok"

    def test_a_neutralised_entry_is_still_reported(self, tmp_path):
        """The running process is safe; the address is still in the config file
        and will be read again on the next restart. Reporting 'ok' would be the
        same category of lie as an egress log that says enforced:false.

        ``CONFIG_DIR`` is set so the posture is read off that directory. With
        it empty, ``posture_check`` falls back to ``current_posture()``, which
        calls ``get_config()`` -- and a config BUILD records its own (empty)
        load violations, erasing what this test just recorded.
        """
        cfg = SimpleNamespace(
            CONFIG_DIR=str(tmp_path),
            ollama=SimpleNamespace(url="http://127.0.0.1:11434"),
            ha=SimpleNamespace(url="http://8.8.8.8:8123", enabled=False))
        neutralised = lp.check_url("ollama", "http://evil.example.com:11434",
                                   "island")
        lp.record_load_violations([neutralised])
        try:
            block = lp.posture_check(cfg, resolve=False)
            assert block["lan_services"] != "ok"
            assert block["lan_services"][0]["state"] == "neutralised_at_load"
        finally:
            lp.record_load_violations([])

    def test_the_report_never_carries_a_credential(self):
        cfg = SimpleNamespace(
            CONFIG_DIR="",
            ha=SimpleNamespace(url="http://8.8.8.8:8123", enabled=True,
                               token=FAKE_TOKEN),
            zone3=SimpleNamespace(url="", enabled=False,
                                  adguard_password="hunter2-not-real",
                                  adguard_url="http://8.8.8.8:3000"))
        blob = json.dumps(lp.posture_check(cfg, resolve=False))
        assert FAKE_TOKEN not in blob
        assert "hunter2-not-real" not in blob


# ── the table covers what the config actually holds ─────────────────────────

def test_every_url_field_in_the_config_is_covered_or_deliberately_not():
    """A URL field nobody checks is a hole this module was built to close, so
    a new one has to be a decision rather than an omission."""
    from puffpai import config as cfgmod

    covered = {s.config_path for s in lp.SERVICES}
    # Fields that are URLs but are not LAN service destinations this module
    # governs. Each one is here because it was looked at, not skipped.
    exempt = {
        "zone3.pihole_url",        # legacy, never deployed; controller inactive
        "ha.url_internal",         # not a field today; guards a rename
    }
    missing = []
    for section, field_name in (
            ("ollama", "url"), ("nodered", "url"), ("n8n", "url"),
            ("ha", "url"), ("zone3", "adguard_url"), ("zone3", "pihole_url"),
            ("tools", "searxng_url"), ("media_server", "jellyfin_url"),
            ("media_server", "calibre_web_url")):
        path = f"{section}.{field_name}"
        if path not in covered and path not in exempt:
            missing.append(path)
    assert missing == [], (
        "these configured URLs are not judged against the run posture: "
        + ", ".join(missing))
    assert hasattr(cfgmod, "Zone3Config")     # the names above still exist
