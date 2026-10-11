"""The Hatch — PuffPAI's naming ritual, run once at first boot and
replayable forever after.

A Puff is not installed, it *hatches*: the avatar wakes, asks the owner
"what's my name?", and speaks a scripted introduction — by name, through the
real local voice — before introducing its crew. The script is deterministic on
purpose, the same ground-truth discipline as ``_maybe_identity_answer`` in the
API server: no LLM writes these lines, so the Puff can never hallucinate a
cloud it does not have.

Three rules this module enforces:

* **Pre-flight before promises.** The ritual opens with "everything you tell
  me stays in this box", so the box has to actually be up. :func:`run_preflight`
  composes only the probes that tell the truth (``run_self_check``, a real
  Piper load, the agent roster, config writability) — never ``GET /health``,
  which answers ``ok`` unconditionally. On a blocking failure the hatch
  **refuses** and says plainly what is broken.
* **Replay never renames.** Re-running the ritual on a hatched build replays
  the script with the existing name; the name changes only when the owner
  explicitly asks (``rename=True``). Hatch date is written once and never
  moves.
* **One Puff, one name, two stores.** The name lives in
  ``config/puffpai_config.json`` ``identity.name`` (read at API-server import
  into the deterministic identity answers) *and* ``data/ai_settings.json``
  ``ai_name`` (what the dashboard shows live). The hatch writes both, so the
  certificate, the chat header, and the identity interceptor all name the same
  Puff.

Hatch state (name, hatch timestamp, replay count) is stored via the canonical
writable-state pattern (:func:`puffpai.statedir.resolve_runtime_dir`), so it
survives a hardened systemd unit with a read-only code tree. The two *name*
stores are different: they deliberately stay at their repo-root locations
because that is where ``puffpai.config`` and ``api_server`` actually read them
(see :func:`puffpai.profile_manager.config_path` for the full argument — an
env-relocated copy would split identity in two). Consequence, accepted and
pre-flighted: on a box whose repo tree is read-only, the hatch *refuses*
up front rather than half-committing.

All state writes are atomic (tmp + ``os.replace``) and the identity pair is
written transactionally — if the second store fails, the first is rolled
back, so a crash can never leave a Puff renamed-but-unhatched.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from puffpai import statedir
from puffpai.logging_config import get_logger

logger = get_logger(__name__)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

#: Shipped default for the hatch-state directory — ``<repo>/data``, alongside
#: profile.json / ai_settings.json. Relocated wholesale by $PUFFPAI_DATA_DIR.
_DEFAULT_STATE_DIR = os.path.join(_REPO_ROOT, "data")

#: Same cap and cleaning rules as agent display names — the name reaches a
#: screen, a TTS voice, and an SVG certificate.
from puffpai.agents.preferences import MAX_NAME_LEN  # noqa: E402

_lock = threading.Lock()

#: One specialty line per internal agent id, for the crew beat and the
#: certificate roster. Kept here (not in the roster) because these are
#: *product voice*, not machine metadata — the dashboard carries its own
#: client-side equivalents and nothing routes on these strings.
SPECIALTIES: dict[str, str] = {
    "concierge": "front desk — every request routes through them",
    "steward": "the house — lights, doors, and tools",
    "sentinel": "security watch — reviews and vetoes",
    "archivist": "archive and vault — what we know, kept on purpose",
    "majordomo": "orchestration — plans, routines, the long game",
    "caretaker": "the box itself — health checks and quiet repairs",
    "toolsmith": "outside errands — useful, never handed the keys",
}

#: Per-agent crew-introduction sentences, ``{display}`` slot filled from the
#: active name preset at speak time.
_CREW_LINES: dict[str, str] = {
    "concierge": "{display} works the front desk — everything you ask goes "
                 "through them first.",
    "steward": "{display} minds the house — the lights, the doors, and the "
               "tools that move things.",
    "sentinel": "{display} keeps watch. Nothing changes in this house "
                "without their review.",
    "archivist": "{display} keeps the archive — what we know, and what "
                 "stays locked in the vault.",
    "majordomo": "{display} runs the schedule — plans, routines, and the "
                 "long game.",
    "caretaker": "{display} looks after the box itself — health checks and "
                 "quiet repairs.",
    "toolsmith": "{display} runs the outside errands. Useful — and never "
                 "handed the keys.",
}


class HatchRefused(RuntimeError):
    """Pre-flight found the box too broken to make the sovereignty promise."""

    def __init__(self, blocking: list[dict]):
        self.blocking = blocking
        lines = "; ".join(
            f"{c.get('label', c.get('id'))}: {c.get('detail', 'failed')}"
            for c in blocking)
        super().__init__(f"refusing to hatch — {lines}")


class HatchNameRequired(ValueError):
    """First hatch needs a name; replays do not."""


class HatchStateError(RuntimeError):
    """A name-store or state write failed after pre-flight said it would not.

    Raised instead of a bare ``OSError`` so the API can answer with a
    structured error, and only after any half-written identity change has
    been rolled back.
    """


class HatchBusy(RuntimeError):
    """Another process (CLI vs API server) is mid-hatch right now."""


# ── state ───────────────────────────────────────────────────────────


def _state_dir() -> str:
    return statedir.resolve_runtime_dir(_DEFAULT_STATE_DIR, env_subdir="data")


def _state_path() -> str:
    return os.path.join(_state_dir(), "hatch.json")


def load_state() -> dict | None:
    """The hatch record, or ``None`` if this build has never hatched."""
    try:
        with open(_state_path(), "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        return raw if isinstance(raw, dict) and raw.get("name") else None
    except (OSError, ValueError):
        return None


def is_hatched() -> bool:
    return load_state() is not None


def _atomic_write_json(path: str, payload: dict) -> None:
    """tmp + fsync + ``os.replace`` — a crash never truncates the target.

    Same idiom as dashboard_registry / dashboard_widgets; the hatch writes
    the main product config, which must never be left half-written. A
    failed write also removes its own orphan tmp file.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _restore_bytes(path: str, prior: bytes | None) -> None:
    """Put a file back exactly as it was — or remove it if it did not exist."""
    if prior is None:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return
    tmp = f"{path}.tmp"
    with open(tmp, "wb") as fh:
        fh.write(prior)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _read_bytes(path: str) -> bytes | None:
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def _write_state(state: dict) -> None:
    _atomic_write_json(_state_path(), state)


def clean_name(raw) -> str:
    """A usable Puff name, or ``""`` if the input cannot become one.

    Same rules as agent display names (printable-only, collapsed whitespace,
    capped at :data:`MAX_NAME_LEN`) plus two of its own: path separators
    become spaces (a name is never a path, so none deserves to look like
    one), and a name must contain at least one letter or digit — "!!!" is
    not a name. The result reaches the dashboard, the voice, and the
    certificate.
    """
    if not isinstance(raw, str):
        return ""
    raw = raw.replace("/", " ").replace("\\", " ")
    cleaned = " ".join(
        "".join(ch if ch.isprintable() else " " for ch in raw).split())
    cleaned = cleaned[:MAX_NAME_LEN]
    if not any(ch.isalnum() for ch in cleaned):
        return ""
    return cleaned


# ── the two name stores ─────────────────────────────────────────────


def _config_json_path() -> str:
    """The puffpai_config.json this build reads AND writes.

    Through :func:`puffpai.config.resolve_config_dir`, for the same reason as
    ``agents/preferences``: :func:`set_identity_name` writes this file, and the
    old ``<repo>/config`` literal made the hatch write a file the loader does
    not read once the config dir is relocated — so naming your appliance landed
    in the tracked shipped config and the running build kept the old name.
    """
    try:
        from puffpai.config import resolve_config_dir
        return os.path.join(resolve_config_dir(), "puffpai_config.json")
    except Exception:
        return os.path.join(_REPO_ROOT, "config", "puffpai_config.json")


def _ai_settings_path() -> str:
    # Deliberately mirrors _AI_SETTINGS_PATH in api_server.py (repo-root
    # relative in this tree): the dashboard reads THAT file, so the hatch
    # must write the same one or the dashboard shows a different Puff.
    return os.path.join(_REPO_ROOT, "data", "ai_settings.json")


def set_identity_name(name: str):
    """Write *name* to both stores, transactionally, and refresh the config.

    Raw-JSON edit of puffpai_config.json (like ``preferences``) so unknown
    keys a build added by hand survive the write. Both writes are atomic
    (tmp + rename); if the second store fails, the first is rolled back to
    its prior bytes and :class:`HatchStateError` is raised — the two stores
    never disagree about who lives here.

    Returns a zero-argument **rollback callable** that restores both stores
    to their exact prior bytes; :func:`perform_hatch` invokes it when the
    hatch-state write fails after the identity landed, so a box can never
    end up renamed-but-unhatched.
    """
    name = clean_name(name)
    if not name:
        raise HatchNameRequired("a Puff needs a printable, non-empty name")

    cfg_path = _config_json_path()
    settings_path = _ai_settings_path()
    prior_cfg = _read_bytes(cfg_path)  # exact prior content, for rollback
    prior_settings = _read_bytes(settings_path)

    try:
        raw = json.loads(prior_cfg.decode("utf-8")) \
            if prior_cfg is not None else {}
        if not isinstance(raw, dict):
            raw = {}
    except ValueError:
        raw = {}
    identity = raw.get("identity")
    if not isinstance(identity, dict):
        identity = {}
        raw["identity"] = identity
    identity["name"] = name
    try:
        _atomic_write_json(cfg_path, raw)
    except OSError as exc:
        raise HatchStateError(
            f"could not write identity config ({cfg_path}): {exc}") from exc

    try:
        settings = json.loads(prior_settings.decode("utf-8")) \
            if prior_settings is not None else {}
        if not isinstance(settings, dict):
            settings = {}
    except ValueError:
        settings = {}
    settings["ai_name"] = name
    settings["updated_at"] = datetime.now(timezone.utc).isoformat()
    try:
        _atomic_write_json(settings_path, settings)
    except OSError as exc:
        try:  # roll the config back — the stores must agree
            _restore_bytes(cfg_path, prior_cfg)
        except OSError as rollback_exc:
            logger.error("identity rollback also failed: %s", rollback_exc)
        raise HatchStateError(
            f"could not write ai_settings ({settings_path}): {exc} — "
            "identity config rolled back") from exc

    def _reload_config() -> None:
        try:  # keep the in-process config singleton honest
            from puffpai.config import get_config
            get_config(_force_reload=True)
        except Exception as exc:
            logger.debug("config reload after rename skipped: %s", exc)

    _reload_config()
    logger.info("Puff identity set: %s (both name stores)", name)

    def rollback() -> None:
        try:
            _restore_bytes(settings_path, prior_settings)
            _restore_bytes(cfg_path, prior_cfg)
        except OSError as exc:
            logger.error("identity rollback failed: %s", exc)
        _reload_config()

    return rollback


def current_name() -> str | None:
    """The name this build answers to, favoring the hatch record."""
    state = load_state()
    if state:
        return state["name"]
    try:
        with open(_ai_settings_path(), "r", encoding="utf-8") as fh:
            settings = json.load(fh)
        name = clean_name(settings.get("ai_name"))
        if name and name != "PuffPAI":
            return name
    except (OSError, ValueError):
        pass
    try:
        from puffpai.config import get_config
        return clean_name(get_config().identity.name) or None
    except Exception:
        return None


# ── pre-flight ──────────────────────────────────────────────────────


def _probe_tts() -> tuple[bool, str]:
    """(available, detail) for the real local voice. Never raises."""
    try:
        from puffpai.voice.tts import PiperTTS
        tts = PiperTTS()
        if tts.is_available():
            return True, "Piper voice model loaded"
        return False, ("Piper voice not available — model missing or "
                       "piper-tts not installed (browser voice would be "
                       "the fallback)")
    except Exception as exc:
        return False, f"TTS probe failed: {exc}"


def _probe_roster() -> tuple[bool, str]:
    try:
        from puffpai.agents.preferences import AGENT_IDS
        from puffpai.agents.registry import roster_summary
        roster = roster_summary()
        missing = [e for e in roster if not e.get("display_name")]
        if len(roster) != len(AGENT_IDS) or missing:
            return False, (f"agent roster incomplete: {len(roster)} of "
                           f"{len(AGENT_IDS)} agents")
        return True, f"crew of {len(roster)} loaded with display names"
    except Exception as exc:
        return False, f"agent roster failed to load: {exc}"


def _probe_config_writable() -> tuple[bool, str]:
    """Real writability of BOTH name stores — not ``os.access`` guesses.

    Opening ``r+`` demands write permission without touching a byte;
    ``os.access`` lies on Windows ACLs and read-only mounts. The
    ai_settings store may not exist yet on a first boot, so its probe is a
    write+delete in the directory instead.
    """
    cfg = _config_json_path()
    if not os.path.isfile(cfg):
        return False, f"config file missing: {cfg}"
    try:
        with open(cfg, "r+", encoding="utf-8"):
            pass
    except OSError as exc:
        return False, f"config file not writable: {cfg} ({exc})"

    settings = _ai_settings_path()
    try:
        if os.path.isfile(settings):
            with open(settings, "r+", encoding="utf-8"):
                pass
        else:
            os.makedirs(os.path.dirname(settings), exist_ok=True)
            probe = os.path.join(os.path.dirname(settings), ".hatch_probe")
            with open(probe, "w", encoding="utf-8") as fh:
                fh.write("ok")
            os.remove(probe)
    except OSError as exc:
        return False, f"ai_settings store not writable: {settings} ({exc})"
    return True, "both identity stores are writable"


def _probe_state_writable() -> tuple[bool, str]:
    target = _state_dir()
    try:
        os.makedirs(target, exist_ok=True)
        probe = os.path.join(target, ".hatch_probe")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(probe)
        return True, f"hatch state directory writable ({target})"
    except OSError as exc:
        return False, f"hatch state directory not writable: {exc}"


def _ensure_state_dirs() -> None:
    """Best-effort creation of the product's state directories.

    On a literal first boot nothing has created ``security_logs/`` or
    ``vault_data/`` yet, and the self-check would honestly report them
    missing — blocking the hatch on directories the product itself would
    create moments later. Creating them here IS the honest probe: success
    proves the paths writable, failure still surfaces through the
    self-check as a blocking fault.
    """
    try:
        from puffpai.self_check import STATE_PATHS
    except Exception:
        return
    for _check_id, _module, _holds, resolver in STATE_PATHS:
        try:
            os.makedirs(resolver(), exist_ok=True)
        except Exception:
            pass  # the self-check will name the fault


def _probe_ollama() -> tuple[bool, str]:
    try:
        from puffpai.config import get_config
        url = get_config().ollama.url.rstrip("/") + "/api/tags"
        import requests
        resp = requests.get(url, timeout=3)
        if resp.status_code == 200:
            return True, "local model runtime reachable"
        return False, f"model runtime answered {resp.status_code}"
    except Exception as exc:
        return False, f"model runtime unreachable: {exc}"


def run_preflight(*, require_voice: bool = True, probe_ollama: bool = True,
                  self_check_result: dict | None = None,
                  probes: dict | None = None) -> dict:
    """Real probes only — every one of these can say "no".

    ``GET /health`` is deliberately absent: it returns ``ok`` unconditionally
    (the known lying health check) and can gate nothing.

    *probes* is a test-injection map ``{probe_name: (ok, detail)}`` for
    ``tts`` / ``roster`` / ``config_writable`` / ``state_writable`` /
    ``ollama``; *self_check_result* injects a pre-computed
    :func:`puffpai.self_check.run_self_check` payload. Injection exists so the
    refusal paths are testable without breaking a real box.
    """
    probes = probes or {}
    checks: list[dict] = []

    def add(check_id: str, label: str, ok: bool, detail: str, *,
            blocking: bool, warn_only: bool = False) -> None:
        status = "ok" if ok else ("warn" if warn_only else "fail")
        checks.append({
            "id": check_id, "label": label, "status": status,
            "detail": detail, "blocking": bool(blocking and not ok),
        })

    ok, detail = probes.get("config_writable", None) or _probe_config_writable()
    add("config_writable", "identity config", ok, detail, blocking=True)

    ok, detail = probes.get("state_writable", None) or _probe_state_writable()
    add("state_writable", "hatch state directory", ok, detail, blocking=True)

    ok, detail = probes.get("roster", None) or _probe_roster()
    add("agents_roster", "agent crew", ok, detail, blocking=True)

    ok, detail = probes.get("tts", None) or _probe_tts()
    add("voice_tts", "local voice (Piper)", ok, detail,
        blocking=require_voice, warn_only=not require_voice)

    if probe_ollama:
        ok, detail = probes.get("ollama", None) or _probe_ollama()
        # The ritual is scripted — it needs no model. Chat afterwards does,
        # so an unreachable runtime is a warning the owner should see.
        add("ollama", "local model runtime", ok, detail,
            blocking=False, warn_only=True)

    if self_check_result is None:
        try:
            from puffpai.self_check import run_self_check
            _ensure_state_dirs()  # a first boot has not created them yet
            self_check_result = run_self_check(probe_network=False)
        except Exception as exc:
            self_check_result = None
            add("self_check", "system self-check", False,
                f"self-check could not run: {exc}", blocking=True)

    if self_check_result is not None:
        for c in self_check_result.get("checks", []):
            if c.get("status") == "fail":
                # A hard self-check failure (unwritable state path, broken
                # vault dir) is exactly the "half the box is down" the
                # sovereignty line must not talk over.
                add(f"self_check:{c.get('id')}", str(c.get('title', c.get('id'))),
                    False, str(c.get("detail", ""))[:300], blocking=True)
        summary = self_check_result.get("summary", {})
        if summary.get("status") in ("ok", "warn"):
            add("self_check", "system self-check",
                True, f"{summary.get('ok', 0)} ok / "
                      f"{summary.get('warn', 0)} warn / "
                      f"{summary.get('fail', 0)} fail",
                blocking=False)

    blocking = [c for c in checks if c["blocking"]]
    return {
        "ok": not blocking,
        "blocking": blocking,
        "checks": checks,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ── the script ──────────────────────────────────────────────────────


def ask_line() -> str:
    """What the newly-woken (or renaming) Puff says before it has a name."""
    return "I'm awake. What's my name?"


def hatch_script(name: str, *, replay: bool = False) -> list[dict]:
    """The scripted first exchange, spoken through the real voice.

    Deterministic by design — the same ground-truth rule as the identity
    interceptor. Every line is a single utterance (single-step intent); none
    of them routes through the planner or the LLM. No share prompts, ever.
    """
    from puffpai.agents.preferences import display_names
    from puffpai.agents.registry import ROSTER

    name = clean_name(name)
    names = display_names()

    lines: list[dict] = []
    if replay:
        lines.append({"id": "intro",
                      "text": f"Hi again. I'm {name}. I still live here."})
    else:
        lines.append({"id": "intro",
                      "text": f"Hi. I'm {name}. I live here now."})
    lines.append({
        "id": "sovereignty",
        "text": ("Everything you tell me stays in this box — your words, "
                 "your files, your keys. No cloud, no accounts, nobody "
                 "listening but me."),
    })
    lines.append({"id": "crew", "text": "I didn't come alone."})
    for entry in ROSTER:
        display = names.get(entry.name, entry.name)
        template = _CREW_LINES.get(
            entry.name, "{display} is part of the crew.")
        lines.append({"id": f"crew:{entry.name}",
                      "text": template.format(display=display)})
    if replay:
        lines.append({"id": "done", "text": f"Still {name}. Still home."})
    else:
        lines.append({"id": "done",
                      "text": f"That's the household. I'm {name}, "
                              f"and I'm home."})
    return lines


# ── the ritual ──────────────────────────────────────────────────────

#: Seconds to wait for a concurrent hatch before giving up, and the age at
#: which an abandoned lock file (crashed process) is broken. Module-level so
#: tests can shrink them.
_LOCK_TIMEOUT_S = 5.0
_LOCK_STALE_S = 120.0


@contextmanager
def _hatch_file_lock():
    """Cross-process guard: the CLI and the API server must not race a hatch.

    The in-process ``_lock`` cannot see another process; this O_EXCL lock
    file in the state directory can. A lock older than ``_LOCK_STALE_S`` is
    treated as abandoned by a crashed process and broken.
    """
    lock_path = f"{_state_path()}.lock"
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    deadline = time.monotonic() + _LOCK_TIMEOUT_S
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock_path) > _LOCK_STALE_S:
                    os.remove(lock_path)
                    continue
            except OSError:
                pass  # raced its removal — loop and retry
            if time.monotonic() >= deadline:
                raise HatchBusy(
                    "another hatch is in progress — try again in a moment")
            time.sleep(0.1)
    try:
        yield
    finally:
        try:
            os.close(fd)
            os.remove(lock_path)
        except OSError:
            pass


def perform_hatch(name: str | None = None, *, rename: bool = False,
                  require_voice: bool = True, probe_ollama: bool = True,
                  preflight: dict | None = None) -> dict:
    """Run (or replay) the hatch. Refuses when pre-flight finds real faults.

    First hatch requires *name*. On an already-hatched build this replays the
    ritual with the existing name; *name* is honored only with
    ``rename=True`` — a replay must never quietly rename someone's Puff.
    """
    if preflight is None:
        preflight = run_preflight(require_voice=require_voice,
                                  probe_ollama=probe_ollama)
    if not preflight["ok"]:
        raise HatchRefused(preflight["blocking"])

    with _lock, _hatch_file_lock():
        state = load_state()
        now = datetime.now(timezone.utc)
        requested = clean_name(name) if name is not None else ""
        name_ignored = False

        if state is None:
            if not requested:
                raise HatchNameRequired(
                    "first hatch needs a name — ask the owner")
            state = {
                "version": 1,
                "name": requested,
                "hatched_at": now.isoformat(),
                "hatch_date": now.date().isoformat(),
                "replays": 0,
            }
            undo_identity = set_identity_name(requested)
            try:
                _write_state(state)
            except OSError as exc:
                # Never leave the box renamed-but-unhatched: put both name
                # stores back exactly as they were.
                undo_identity()
                raise HatchStateError(
                    f"hatch state write failed after pre-flight passed "
                    f"(identity rolled back): {exc}") from exc
            replay = False
        else:
            replay = True
            undo_identity = None
            if rename and requested and requested != state["name"]:
                undo_identity = set_identity_name(requested)
                state["name"] = requested
                state["renamed_at"] = now.isoformat()
            elif requested and requested != state["name"]:
                name_ignored = True  # replays never rename by accident
            state["replays"] = int(state.get("replays", 0)) + 1
            state["last_replayed_at"] = now.isoformat()
            try:
                _write_state(state)
            except OSError as exc:
                if undo_identity is not None:
                    # The rename never made it into the hatch record — the
                    # stores must keep agreeing with the record.
                    undo_identity()
                raise HatchStateError(
                    f"hatch state write failed after pre-flight passed"
                    f"{' (rename rolled back)' if undo_identity else ''}: "
                    f"{exc}") from exc

    result = {
        "replay": replay,
        "name": state["name"],
        "hatched_at": state["hatched_at"],
        "hatch_date": state.get("hatch_date",
                                state["hatched_at"][:10]),
        "replays": state["replays"],
        "script": hatch_script(state["name"], replay=replay),
        "ask_line": ask_line(),
        "preflight": preflight,
    }
    if name_ignored:
        result["name_ignored"] = True
        result["note"] = ("replay keeps the existing name — pass "
                          "rename=true to rename on purpose")
    return result


__all__ = [
    "SPECIALTIES",
    "HatchBusy",
    "HatchNameRequired",
    "HatchRefused",
    "HatchStateError",
    "ask_line",
    "clean_name",
    "current_name",
    "hatch_script",
    "is_hatched",
    "load_state",
    "perform_hatch",
    "run_preflight",
    "set_identity_name",
]
