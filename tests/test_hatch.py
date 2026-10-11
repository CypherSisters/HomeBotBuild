"""Tests for the Hatch ritual (puffpai.hatch).

Covers the three contract-bearing behaviours:

* the pre-flight REFUSAL path (real-probe failures block; the lying /health
  is structurally absent because run_preflight never consults HTTP at all),
* the REPLAY path (never renames by accident; hatch date written once),
* the two-name-store write (config identity + ai_settings agree afterwards).

All probes are injected — no network, no Piper, no real config touched.
"""

import json
import os

import pytest

import puffpai.hatch as hatch


# ── fixtures ────────────────────────────────────────────────────────

OK_PROBES = {
    "config_writable": (True, "writable"),
    "state_writable": (True, "writable"),
    "roster": (True, "crew of 7 loaded"),
    "tts": (True, "piper loaded"),
    "ollama": (True, "reachable"),
}

SELF_CHECK_OK = {
    "checks": [],
    "summary": {"status": "ok", "ok": 5, "warn": 0, "fail": 0},
}


@pytest.fixture
def hatch_env(tmp_path, monkeypatch):
    """Isolated stores: hatch state, config json, ai_settings under tmp."""
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))

    config_path = tmp_path / "config" / "puffpai_config.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(json.dumps({
        "identity": {"name": "PuffPAI", "avatar_style": "notionists",
                     "avatar_seed": "PuffPAI"},
        "agents": {"name_preset": "default"},
    }), encoding="utf-8")
    settings_path = tmp_path / "data" / "ai_settings.json"

    monkeypatch.setattr(hatch, "_config_json_path", lambda: str(config_path))
    monkeypatch.setattr(hatch, "_ai_settings_path", lambda: str(settings_path))
    # set_identity_name refreshes the process config singleton; don't let a
    # test reload the REAL repo config into the shared singleton.
    import puffpai.config as config_mod
    monkeypatch.setattr(config_mod, "get_config",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError(
                            "config untouched in tests")))
    # display_names() caches the agents config section module-wide; clear it
    # on both sides so this fixture neither sees nor leaves stale names.
    from puffpai.agents import preferences
    preferences.reset_cache()
    yield {"config": config_path, "settings": settings_path,
           "tmp": tmp_path}
    preferences.reset_cache()


def _preflight_ok():
    return hatch.run_preflight(probes=dict(OK_PROBES),
                               self_check_result=SELF_CHECK_OK)


def _do_hatch(name=None, **kwargs):
    kwargs.setdefault("preflight", _preflight_ok())
    return hatch.perform_hatch(name, **kwargs)


# ── pre-flight ──────────────────────────────────────────────────────

class TestPreflight:
    def test_all_ok(self):
        result = _preflight_ok()
        assert result["ok"] is True
        assert result["blocking"] == []
        assert {c["id"] for c in result["checks"]} >= {
            "config_writable", "state_writable", "agents_roster",
            "voice_tts", "ollama", "self_check"}

    def test_tts_failure_blocks_when_voice_required(self):
        probes = dict(OK_PROBES, tts=(False, "no model"))
        result = hatch.run_preflight(probes=probes,
                                     self_check_result=SELF_CHECK_OK)
        assert result["ok"] is False
        assert [c["id"] for c in result["blocking"]] == ["voice_tts"]

    def test_tts_failure_warns_when_voice_optional(self):
        probes = dict(OK_PROBES, tts=(False, "no model"))
        result = hatch.run_preflight(require_voice=False, probes=probes,
                                     self_check_result=SELF_CHECK_OK)
        assert result["ok"] is True
        voice = next(c for c in result["checks"] if c["id"] == "voice_tts")
        assert voice["status"] == "warn"

    def test_ollama_down_never_blocks(self):
        # The ritual is scripted — no model speaks in it.
        probes = dict(OK_PROBES, ollama=(False, "connection refused"))
        result = hatch.run_preflight(probes=probes,
                                     self_check_result=SELF_CHECK_OK)
        assert result["ok"] is True
        check = next(c for c in result["checks"] if c["id"] == "ollama")
        assert check["status"] == "warn"

    def test_self_check_fail_blocks(self):
        sc = {"checks": [{"id": "state_path:vault", "title": "vault dir",
                          "status": "fail", "detail": "not writable"}],
              "summary": {"status": "fail", "ok": 0, "warn": 0, "fail": 1}}
        result = hatch.run_preflight(probes=dict(OK_PROBES),
                                     self_check_result=sc)
        assert result["ok"] is False
        assert result["blocking"][0]["id"] == "self_check:state_path:vault"

    def test_roster_failure_blocks(self):
        probes = dict(OK_PROBES, roster=(False, "import broke"))
        result = hatch.run_preflight(probes=probes,
                                     self_check_result=SELF_CHECK_OK)
        assert result["ok"] is False
        assert "agents_roster" in [c["id"] for c in result["blocking"]]


# ── refusal ─────────────────────────────────────────────────────────

class TestRefusal:
    def test_perform_hatch_refuses_and_names_the_fault(self, hatch_env):
        probes = dict(OK_PROBES, tts=(False, "piper model missing"))
        bad = hatch.run_preflight(probes=probes,
                                  self_check_result=SELF_CHECK_OK)
        with pytest.raises(hatch.HatchRefused) as excinfo:
            hatch.perform_hatch("Nimbus", preflight=bad)
        assert "piper model missing" in str(excinfo.value)
        # A refused hatch must leave no trace: still unhatched.
        assert hatch.load_state() is None
        assert not os.path.exists(hatch_env["settings"])

    def test_refusal_carries_structured_blocking(self):
        probes = dict(OK_PROBES, config_writable=(False, "read-only fs"))
        bad = hatch.run_preflight(probes=probes,
                                  self_check_result=SELF_CHECK_OK)
        with pytest.raises(hatch.HatchRefused) as excinfo:
            hatch.perform_hatch("Nimbus", preflight=bad)
        assert excinfo.value.blocking[0]["id"] == "config_writable"


# ── first hatch ─────────────────────────────────────────────────────

class TestFirstHatch:
    def test_needs_a_name(self, hatch_env):
        with pytest.raises(hatch.HatchNameRequired):
            _do_hatch(None)

    def test_blank_name_is_not_a_name(self, hatch_env):
        with pytest.raises(hatch.HatchNameRequired):
            _do_hatch("\x00\x01   ")

    def test_writes_state_and_both_stores(self, hatch_env):
        result = _do_hatch("Nimbus")
        assert result["replay"] is False
        assert result["name"] == "Nimbus"
        assert result["hatch_date"] == result["hatched_at"][:10]

        state = hatch.load_state()
        assert state["name"] == "Nimbus"
        assert state["replays"] == 0

        config = json.loads(hatch_env["config"].read_text(encoding="utf-8"))
        assert config["identity"]["name"] == "Nimbus"
        # a raw-JSON merge must not eat sibling keys or sections
        assert config["identity"]["avatar_seed"] == "PuffPAI"
        assert config["agents"] == {"name_preset": "default"}

        settings = json.loads(
            hatch_env["settings"].read_text(encoding="utf-8"))
        assert settings["ai_name"] == "Nimbus"

    def test_script_speaks_the_name_and_the_sovereignty_line(self, hatch_env):
        result = _do_hatch("Nimbus")
        script = result["script"]
        assert script[0]["text"] == "Hi. I'm Nimbus. I live here now."
        sovereignty = next(l for l in script if l["id"] == "sovereignty")
        assert "stays in this box" in sovereignty["text"]

    def test_script_introduces_the_whole_crew(self, hatch_env):
        from puffpai.agents.preferences import AGENT_IDS
        script = _do_hatch("Nimbus")["script"]
        crew_ids = {l["id"].split(":", 1)[1] for l in script
                    if l["id"].startswith("crew:")}
        assert crew_ids == set(AGENT_IDS)

    def test_script_has_no_share_prompts(self, hatch_env):
        # The moment belongs to the household — never "film this".
        script = _do_hatch("Nimbus")["script"]
        text = " ".join(l["text"].lower() for l in script)
        for banned in ("film", "record this", "share", "post", "camera",
                       "video", "social"):
            assert banned not in text

    def test_script_lines_are_single_short_utterances(self, hatch_env):
        for line in _do_hatch("Nimbus")["script"]:
            assert line["text"].strip()
            assert len(line["text"]) < 500  # nabu voice_max_length


# ── replay ──────────────────────────────────────────────────────────

class TestReplay:
    def test_replay_keeps_name_and_hatch_date(self, hatch_env):
        first = _do_hatch("Nimbus")
        replay = _do_hatch(None)
        assert replay["replay"] is True
        assert replay["name"] == "Nimbus"
        assert replay["hatched_at"] == first["hatched_at"]
        assert replay["replays"] == 1
        again = _do_hatch(None)
        assert again["replays"] == 2

    def test_replay_never_renames_by_accident(self, hatch_env):
        _do_hatch("Nimbus")
        replay = _do_hatch("Stratus")  # name passed but rename NOT
        assert replay["name"] == "Nimbus"
        assert replay.get("name_ignored") is True
        config = json.loads(hatch_env["config"].read_text(encoding="utf-8"))
        assert config["identity"]["name"] == "Nimbus"

    def test_explicit_rename_updates_everything_but_the_date(self, hatch_env):
        first = _do_hatch("Nimbus")
        renamed = _do_hatch("Stratus", rename=True)
        assert renamed["name"] == "Stratus"
        assert renamed["hatched_at"] == first["hatched_at"]
        state = hatch.load_state()
        assert state["name"] == "Stratus"
        assert "renamed_at" in state
        config = json.loads(hatch_env["config"].read_text(encoding="utf-8"))
        assert config["identity"]["name"] == "Stratus"
        settings = json.loads(
            hatch_env["settings"].read_text(encoding="utf-8"))
        assert settings["ai_name"] == "Stratus"

    def test_replay_script_uses_replay_voice(self, hatch_env):
        _do_hatch("Nimbus")
        script = _do_hatch(None)["script"]
        assert "Hi again" in script[0]["text"]
        assert "Nimbus" in script[0]["text"]

    def test_replay_is_refused_too_when_the_box_is_broken(self, hatch_env):
        # Replays make the same promise, so they take the same pre-flight.
        _do_hatch("Nimbus")
        probes = dict(OK_PROBES, state_writable=(False, "disk full"))
        bad = hatch.run_preflight(probes=probes,
                                  self_check_result=SELF_CHECK_OK)
        with pytest.raises(hatch.HatchRefused):
            hatch.perform_hatch(None, preflight=bad)
        assert hatch.load_state()["replays"] == 0


# ── name hygiene ────────────────────────────────────────────────────

class TestCleanName:
    def test_strips_control_characters(self):
        assert hatch.clean_name("Nim\x00bus\n\n  Jr ") == "Nim bus Jr"

    def test_caps_length(self):
        assert len(hatch.clean_name("N" * 200)) == hatch.MAX_NAME_LEN

    def test_non_string_is_empty(self):
        assert hatch.clean_name(None) == ""
        assert hatch.clean_name(42) == ""

    def test_symbol_only_is_not_a_name(self):
        assert hatch.clean_name("!!!###") == ""
        assert hatch.clean_name("---") == ""

    def test_path_separators_become_spaces(self):
        cleaned = hatch.clean_name("../../etc/passwd")
        assert "/" not in cleaned
        assert hatch.clean_name("C:\\Windows\\evil") == "C: Windows evil"

    def test_unicode_and_apostrophes_survive(self):
        assert hatch.clean_name("Böna") == "Böna"
        assert hatch.clean_name("Puff O'Malley") == "Puff O'Malley"


# ── transactional identity + atomic state ───────────────────────────

class TestTransactionalIdentity:
    def _block_settings(self, hatch_env, monkeypatch):
        """Point the ai_settings store somewhere unwritable (under a file)."""
        blocker = hatch_env["tmp"] / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        monkeypatch.setattr(
            hatch, "_ai_settings_path",
            lambda: str(blocker / "nested" / "ai_settings.json"))

    def test_first_hatch_failure_leaves_no_trace(self, hatch_env,
                                                 monkeypatch):
        self._block_settings(hatch_env, monkeypatch)
        with pytest.raises(hatch.HatchStateError):
            _do_hatch("Nimbus")
        # No half-commit: unhatched AND the config still names the old Puff.
        assert hatch.load_state() is None
        config = json.loads(hatch_env["config"].read_text(encoding="utf-8"))
        assert config["identity"]["name"] == "PuffPAI"

    def test_rename_failure_rolls_back_config(self, hatch_env, monkeypatch):
        _do_hatch("Nimbus")
        self._block_settings(hatch_env, monkeypatch)
        with pytest.raises(hatch.HatchStateError):
            _do_hatch("Stratus", rename=True)
        config = json.loads(hatch_env["config"].read_text(encoding="utf-8"))
        assert config["identity"]["name"] == "Nimbus"  # rolled back
        assert hatch.load_state()["name"] == "Nimbus"  # state untouched

    def test_first_hatch_state_write_failure_rolls_back_identity(
            self, hatch_env):
        # hatch.json as a directory: identity lands, the state write cannot.
        os.makedirs(hatch._state_path())
        with pytest.raises(hatch.HatchStateError):
            _do_hatch("Nimbus")
        # Never renamed-but-unhatched: both stores back to prior state.
        config = json.loads(hatch_env["config"].read_text(encoding="utf-8"))
        assert config["identity"]["name"] == "PuffPAI"
        assert not os.path.exists(hatch_env["settings"])  # restored-absent
        assert hatch.load_state() is None

    def test_rename_state_write_failure_rolls_back_identity(
            self, hatch_env, monkeypatch):
        _do_hatch("Nimbus")
        real_write = hatch._write_state

        def boom(state):
            raise OSError("disk full")

        monkeypatch.setattr(hatch, "_write_state", boom)
        with pytest.raises(hatch.HatchStateError):
            _do_hatch("Stratus", rename=True)
        monkeypatch.setattr(hatch, "_write_state", real_write)
        config = json.loads(hatch_env["config"].read_text(encoding="utf-8"))
        assert config["identity"]["name"] == "Nimbus"
        settings = json.loads(
            hatch_env["settings"].read_text(encoding="utf-8"))
        assert settings["ai_name"] == "Nimbus"
        assert hatch.load_state()["name"] == "Nimbus"

    def test_failed_write_leaves_no_orphan_tmp(self, hatch_env, monkeypatch):
        _do_hatch("Nimbus")
        real_dump = hatch.json.dump

        def boom(*args, **kwargs):
            raise RuntimeError("mid-json")

        monkeypatch.setattr(hatch.json, "dump", boom)
        with pytest.raises(RuntimeError):
            _do_hatch(None)
        monkeypatch.setattr(hatch.json, "dump", real_dump)
        assert not os.path.exists(f"{hatch._state_path()}.tmp")

    def test_crashed_state_write_leaves_prior_state_readable(
            self, hatch_env, monkeypatch):
        _do_hatch("Nimbus")
        real_dump = hatch.json.dump

        def boom(*args, **kwargs):
            raise RuntimeError("disk vanished mid-json")

        monkeypatch.setattr(hatch.json, "dump", boom)
        with pytest.raises(RuntimeError):
            _do_hatch(None)
        # restore explicitly — undo() would also strip hatch_env's redirects
        monkeypatch.setattr(hatch.json, "dump", real_dump)
        # tmp+rename: the crash hit the tmp file, never the real one.
        state = hatch.load_state()
        assert state is not None and state["name"] == "Nimbus"


# ── cross-process lock ──────────────────────────────────────────────

class TestHatchFileLock:
    def _lock_path(self):
        return f"{hatch._state_path()}.lock"

    def test_held_lock_raises_busy(self, hatch_env, monkeypatch):
        monkeypatch.setattr(hatch, "_LOCK_TIMEOUT_S", 0.2)
        lock = self._lock_path()
        os.makedirs(os.path.dirname(lock), exist_ok=True)
        with open(lock, "w", encoding="utf-8"):
            pass
        try:
            with pytest.raises(hatch.HatchBusy):
                _do_hatch("Nimbus")
            assert hatch.load_state() is None
        finally:
            os.remove(lock)

    def test_stale_lock_is_broken(self, hatch_env):
        import time as _time
        lock = self._lock_path()
        os.makedirs(os.path.dirname(lock), exist_ok=True)
        with open(lock, "w", encoding="utf-8"):
            pass
        old = _time.time() - 600
        os.utime(lock, (old, old))
        result = _do_hatch("Nimbus")  # a crashed process must not brick this
        assert result["name"] == "Nimbus"
        assert not os.path.exists(lock)

    def test_lock_released_after_hatch(self, hatch_env):
        _do_hatch("Nimbus")
        assert not os.path.exists(self._lock_path())


# ── first-boot state dirs ───────────────────────────────────────────

class TestEnsureStateDirs:
    def test_creates_missing_product_state_dirs(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "fresh"))
        hatch._ensure_state_dirs()
        from puffpai.self_check import STATE_PATHS
        for _check_id, _module, _holds, resolver in STATE_PATHS:
            assert os.path.isdir(resolver())
