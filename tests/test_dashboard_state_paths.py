"""Dashboard state-dir resolution — where the widget/camera registries and
their security ledgers land.

Both modules used to freeze their paths at import against the repo tree, which
on the hardened appliance (``ProtectSystem=strict``, only
``$PUFFPAI_DATA_DIR`` writable) means every registry save raises EROFS and
every audit/egress line is swallowed by the module's own ``except OSError``.
The paths now resolve per call through :func:`puffpai.statedir.resolve_runtime_dir`.

Convention pinned here: the ``data/`` JSON registries land at the
``$PUFFPAI_DATA_DIR`` *root* (precedent: agent_events.jsonl), the JSONL
security ledgers land under ``$PUFFPAI_DATA_DIR/security_logs`` (precedent:
security/audit_log.py).

No network and no writes inside the repo tree: every write case points the
override hook or the env at tmp_path first.
Run: py -3 -m pytest tests/test_dashboard_state_paths.py -q
"""

import json
import os

import pytest

from puffpai import dashboard_registry as dreg
from puffpai import dashboard_widgets as dw
from puffpai.config import resolve_state_dir

# (module, hook name, accessor name, filename, env_subdir)
CASES = [
    (dw, "_REGISTRY_PATH", "_registry_path", "dashboard_widgets.json", ""),
    (dw, "_EGRESS_LOG", "_egress_log_path", "widget_egress.jsonl", "security_logs"),
    (dreg, "_REGISTRY_PATH", "_registry_path", "dashboard_cameras.json", ""),
    (dreg, "_AUDIT_PATH", "_audit_path", "dashboard_tile_reviews.jsonl",
     "security_logs"),
]
IDS = [f"{m.__name__.rsplit('.', 1)[-1]}.{hook}" for m, hook, *_ in CASES]


@pytest.fixture(autouse=True)
def _no_repo_writes(monkeypatch):
    """Hooks start unset for every case; monkeypatch reverts them afterwards so
    one test's tmp_path can never leak into another's expectations."""
    for mod, hook, *_ in CASES:
        monkeypatch.setattr(mod, hook, None)


@pytest.mark.parametrize("mod,hook,accessor,filename,env_subdir", CASES, ids=IDS)
def test_unset_env_keeps_the_historical_in_tree_path(
        mod, hook, accessor, filename, env_subdir, monkeypatch):
    from puffpai import config as cfgmod

    monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
    # "Env unset keeps the in-tree path" is only true of a checkout that has
    # already RUN: config._dev_checkout_has_state looks for runtime state under
    # <repo>/data and, finding none, sends a fresh clone to the per-user
    # directory instead. This used to depend on that state being there
    # ambiently, which made the case pass on a dirty checkout and fail on a
    # clean one. State the precondition instead of inheriting it.
    monkeypatch.setitem(cfgmod._dev_state_cache,
                        os.path.normpath(mod._REPO_ROOT), True)
    subdir = env_subdir or "data"
    assert getattr(mod, accessor)() == os.path.join(
        mod._REPO_ROOT, subdir, filename)


@pytest.mark.parametrize("mod,hook,accessor,filename,env_subdir", CASES, ids=IDS)
def test_absolute_env_relocates_per_convention(
        mod, hook, accessor, filename, env_subdir, tmp_path, monkeypatch):
    root = tmp_path / "state"
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(root))
    expected = root / env_subdir / filename if env_subdir else root / filename
    assert getattr(mod, accessor)() == str(expected)


@pytest.mark.parametrize("mod,hook,accessor,filename,env_subdir", CASES, ids=IDS)
def test_paths_match_the_canonical_resolver(
        mod, hook, accessor, filename, env_subdir, tmp_path, monkeypatch):
    """A relative value joins the repo root, not the CWD; "~" expands; blanks
    mean unset. Don't re-derive those rules here — defer to the canon."""
    for env_value in ("", "   ", "relstate", "~/puff_state", str(tmp_path)):
        monkeypatch.setenv("PUFFPAI_DATA_DIR", env_value)
        default = os.path.join(mod._REPO_ROOT, env_subdir or "data")
        assert getattr(mod, accessor)() == os.path.join(
            resolve_state_dir(None, repo_root=mod._REPO_ROOT, default=default,
                              env_subdir=env_subdir),
            filename), f"env={env_value!r}"


@pytest.mark.parametrize("mod,hook,accessor,filename,env_subdir", CASES, ids=IDS)
def test_override_hook_beats_the_env(
        mod, hook, accessor, filename, env_subdir, tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "env_root"))
    pinned = str(tmp_path / "pinned" / filename)
    monkeypatch.setattr(mod, hook, pinned)
    assert getattr(mod, accessor)() == pinned


@pytest.mark.parametrize("mod,hook,accessor,filename,env_subdir", CASES, ids=IDS)
def test_hook_left_unset_falls_through_to_the_env(
        mod, hook, accessor, filename, env_subdir, tmp_path, monkeypatch):
    """The hook is opt-in: None (and an empty string) must not win."""
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(mod, hook, "")
    expected = tmp_path / env_subdir / filename if env_subdir else tmp_path / filename
    assert getattr(mod, accessor)() == str(expected)


# ── Functional: the writes actually land in the relocated dir ────────────────

def test_widget_save_writes_to_the_relocated_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))
    ok, msg, widget = dw.add_widget("calendar", {"title": "Upcoming"})

    assert ok, msg
    registry = tmp_path / "state" / "dashboard_widgets.json"
    assert registry.is_file(), "parent dir created and registry written"
    rows = json.loads(registry.read_text(encoding="utf-8"))
    assert [r["id"] for r in rows] == [widget["id"]]
    assert dw.get_widget(widget["id"]) is not None


def test_widget_egress_log_writes_under_security_logs(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))
    assert not (tmp_path / "state" / "security_logs").exists()

    dw._log_egress("https://feeds.example.invalid/rss", False, "connection refused")

    ledger = tmp_path / "state" / "security_logs" / "widget_egress.jsonl"
    assert ledger.is_file(), "security_logs subtree created on first write"
    row = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])
    assert row["kind"] == "widget_news_fetch"
    assert row["ok"] is False


def test_camera_registry_save_writes_to_the_relocated_registry(
        tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))
    dreg._save([{"entity_id": "camera.test_cam", "label": "Test Cam",
                 "added_by": "test", "added_at": dreg._utcnow()}])

    registry = tmp_path / "state" / "dashboard_cameras.json"
    assert registry.is_file(), "parent dir created and registry written"
    assert [c["entity_id"] for c in dreg.list_cameras()] == ["camera.test_cam"]
    assert dreg.remove_camera("camera.test_cam") is True
    assert json.loads(registry.read_text(encoding="utf-8")) == []


def test_camera_audit_writes_under_security_logs(tmp_path, monkeypatch):
    """Sentinel's deny trail is the only record of a refused tile — it has to
    survive the relocation, not vanish into the module's except OSError."""
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))
    assert not (tmp_path / "state" / "security_logs").exists()

    dreg._audit("deny", "camera.garden", ["no per-device consent"], "agent",
                "put the garden cam on the dashboard")

    ledger = tmp_path / "state" / "security_logs" / "dashboard_tile_reviews.jsonl"
    assert ledger.is_file(), "security_logs subtree created on first write"
    row = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])
    assert row["verdict"] == "deny"
    assert row["entity_id"] == "camera.garden"


def test_hook_pointed_writes_bypass_the_env_entirely(tmp_path, monkeypatch):
    """The ops/test override has to reach the write sites, not just the
    accessors — that is what makes it usable to keep tests out of the repo."""
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "env_root"))
    pinned = tmp_path / "pinned" / "reviews.jsonl"
    monkeypatch.setattr(dreg, "_AUDIT_PATH", str(pinned))

    dreg._audit("allow", "camera.garden", ["consent on file"], "agent")

    assert pinned.is_file()
    assert not (tmp_path / "env_root").exists()
