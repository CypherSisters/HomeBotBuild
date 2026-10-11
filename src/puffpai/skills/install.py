"""Propose, and then -- behind three gates -- install. Nothing else writes here.

The shape is copied from :mod:`puffpai.ha_provisioning`, which is the
appliance's existing "an agent may ASK; only the owner's own yes may DO"
machine, because a second shape would be a second thing to audit:

* :func:`propose_install` writes a consent request and **nothing else**. The
  request carries the manifest, a sha256 per file, the lint findings and the
  test log, so what the owner approves is an artifact, not a category.
* :func:`apply_approved_install` is the only writer, and all three gates must
  hold:

  1. *human_confirmed* is the in-process :data:`puffpai.ha_bridge.HUMAN_CONFIRMED`
     object. JSON has no syntax for object identity, so no request body and no
     tool argument can supply it;
  2. a fresh, human-resolved, UNSPENT approval exists in the durable queue --
     re-read from disk, never taken from the caller;
  3. the draft's files still hash to the sha256 recorded on the card. This is
     the TOCTOU gate: approval is of bytes, so bytes that moved between the card
     and the install void the approval rather than inheriting it.

Only after all three does the door flag get written -- for the digest of the
host set that was ON THE CARD. An edited manifest produces a different digest
and therefore a shut door, which is why "a skill widened its own reach after
approval" is not a failure mode that needs detecting.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

INSTALL_ACTION = "skill_install"

#: Six hours. A standing offer to install code should not survive the day.
REQUEST_TTL_SECONDS = 6 * 3600

#: Same window as a provisioning write: an approval older than this is not a
#: mandate for something happening now.
APPROVAL_FRESHNESS_SECONDS = 300.0

HUMAN_RESOLVERS = frozenset({"user_via_chat", "user_via_ui", "user_via_voice"})

#: The two plain sentences for a box with no bubblewrap. Constants rather than
#: inline strings because the dashboard, the CLI and three tests all quote them,
#: and a sentence the owner reads is part of the contract.
NO_SANDBOX_INSTALL_SENTENCE = (
    "This box has no sandbox for skills, so I couldn't run its tests and I "
    "won't run skill code without one.")
NO_SANDBOX_ENABLE_SENTENCE = (
    "I can't switch that skill on: this box has no sandbox for skills "
    "(bubblewrap at /usr/bin/bwrap), and I won't run skill code without one.")

_spent_approvals: set = set()
_lock = threading.RLock()

#: Multi-label public suffixes this build knows. Not a public-suffix list, and
#: not pretending to be: it exists so the card says "bbc.co.uk" rather than
#: "co.uk" for the handful of shapes a household is likely to meet. When in
#: doubt it renders MORE of the name, never less -- an over-long domain on the
#: card is a cosmetic problem; a truncated one is a misleading one.
_MULTI_SUFFIXES = frozenset({
    "co.uk", "org.uk", "ac.uk", "gov.uk", "co.jp", "com.au", "net.au",
    "org.au", "co.nz", "com.br", "co.in", "com.mx", "co.za",
})


def reset_for_tests() -> None:
    with _lock:
        _spent_approvals.clear()


def registrable_domain(host: str) -> str:
    """The eTLD+1 a person would recognise: ``api.wheretheiss.at`` -> ``wheretheiss.at``."""
    labels = [p for p in str(host or "").strip().lower().strip(".").split(".") if p]
    if len(labels) < 2:
        return ".".join(labels)
    if len(labels) >= 3 and ".".join(labels[-2:]) in _MULTI_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _entity_id(skill_id: str) -> str:
    return f"skill:{skill_id}"


def stage_example(example_id: str) -> dict:
    """Copy a shipped example skill into ``_drafts/`` so it can be proposed.

    The examples under ``src/puffpai/skills_examples/`` are hand-written and
    read-only; staging COPIES one into the drafts tree, where it goes through
    exactly the same validation, card and approval as anything else. There is no
    "shipped skills are already trusted" path -- an example that nobody approved
    is not installed.
    """
    from puffpai.skills import manifest as _manifest
    from puffpai.skills import store as _store

    example_id = str(example_id or "").strip().lower()
    if not _manifest.ID_RE.match(example_id):
        raise _manifest.SkillManifestError(f"{example_id!r} is not a usable example id.")
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "skills_examples")
    source = os.path.join(root, example_id)
    if not os.path.isdir(source):
        raise _manifest.SkillManifestError(f"there is no example skill called {example_id!r}.")

    manifest, _shas = _manifest.validate_dir(source)
    target = _store.draft_dir(manifest["id"])
    if os.path.isdir(target):
        shutil.rmtree(target)
    os.makedirs(target, exist_ok=True)
    for name in _manifest.PACKAGE_FILES:
        shutil.copyfile(os.path.join(source, name), os.path.join(target, name),
                        follow_symlinks=False)
    logger.info("skills: staged the example skill %s as a draft", manifest["id"])
    return {"ok": True, "id": manifest["id"], "dir": target,
            "manifest": manifest}


def draft_bytes(skill_id: str) -> dict:
    """The exact hashed bytes of a draft, for the consent card's "view code".

    Reads the file, hashes what it read, and returns both. A card that showed
    text from one read and a hash from another would be a card that can be made
    to lie by a write in between.
    """
    from puffpai.skills import lint as _lint
    from puffpai.skills import manifest as _manifest
    from puffpai.skills import store as _store

    folder = _store.draft_dir(skill_id)
    manifest, _shas = _manifest.validate_dir(folder)
    files = []
    for name in _manifest.PACKAGE_FILES:
        with open(os.path.join(folder, name), "rb") as handle:
            raw = handle.read()
        import hashlib
        files.append({
            "name": name,
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "text": raw.decode("utf-8", "replace"),
        })
    source = next(f["text"] for f in files if f["name"] == "skill.py")
    hosts = manifest["permissions"]["network_hosts"]
    return {
        "ok": True,
        "id": manifest["id"],
        "manifest": manifest,
        "files": files,
        "lint": _lint.lint_source(source),
        "hosts": hosts,
        "domains": sorted({registrable_domain(h) for h in hosts}),
        "state": "draft",
    }


def _reason_text(manifest: dict, shas: dict, lint_findings, test_note: str) -> str:
    hosts = manifest["permissions"]["network_hosts"]
    domains = sorted({registrable_domain(h) for h in hosts})
    if domains:
        reach = (f"It will reach {', '.join(domains)} "
                 f"(exactly: {', '.join(hosts)}) over https, and nothing else.")
    else:
        reach = "It reaches no network host at all."
    lines = [
        f"Install a new skill, {manifest['name']} ({manifest['id']}). "
        f"{manifest['description']}",
        f"It answers when you say: "
        f"{'; '.join(repr(t) for t in manifest['triggers'])}.",
        reach,
        ("It runs in a sandbox with no environment, no Home Assistant, no "
         "vault, no files of yours, and no network except that one door. "
         "It reads; it cannot change anything in your house."),
        f"Requests per run: at most {manifest['limits']['max_requests']}; "
        f"time limit {manifest['limits']['timeout_s']}s.",
        test_note,
        "The code is on the dashboard under Skills; this approval is for these "
        "exact bytes:",
    ]
    for name in sorted(shas):
        lines.append(f"  {name}  sha256 {shas[name]}")
    if lint_findings:
        lines.append("Automatic review flagged:")
        for finding in lint_findings[:10]:
            lines.append(f"  - {finding}")
    return "\n".join(lines)


def test_draft(skill_id: str) -> dict:
    """Run a draft's fixtures in the jail, offline. Never reaches the network."""
    from puffpai.skills import dispatch as _dispatch
    from puffpai.skills import store as _store

    return _dispatch.run_package(_store.draft_dir(skill_id), mode="test")


def propose_install(skill_id: str, *, requested_by: str = "toolsmith") -> dict:
    """File ONE consent request for a draft. Installs nothing. Never raises."""
    from puffpai.security import consent_requests as _creq
    from puffpai.skills import lint as _lint
    from puffpai.skills import manifest as _manifest
    from puffpai.skills import store as _store

    try:
        folder = _store.draft_dir(skill_id)
        manifest, shas = _manifest.validate_dir(folder)
    except Exception as exc:
        return {"ok": False, "proposed": False, "error": str(exc),
                "message": f"I can't propose that skill: {exc}"}

    with open(os.path.join(folder, "skill.py"), "r", encoding="utf-8") as handle:
        findings = _lint.lint_source(handle.read())

    tested = test_draft(manifest["id"])
    if tested.get("ok"):
        test_note = (f"I ran its own tests in the sandbox with canned answers "
                     f"and they passed ({tested.get('speech', '')}).")
    elif tested.get("sandbox_unavailable"):
        test_note = ("I could NOT run its tests here -- this box has no "
                     "sandbox -- so it will install switched off.")
    else:
        return {"ok": False, "proposed": False,
                "error": str(tested.get("error") or "the tests failed"),
                "message": ("That skill does not pass its own tests, so I "
                            "haven't asked you to approve it: "
                            f"{tested.get('error') or 'unknown failure'}")}

    hosts = manifest["permissions"]["network_hosts"]
    from puffpai.security import egress_doors as _doors
    digest = _doors.skill_door_flag(manifest["id"], 0, hosts)

    request = _creq.create_request(
        entity_id=_entity_id(manifest["id"]),
        label=f"Skill: {manifest['name']}",
        action=INSTALL_ACTION,
        reason=_reason_text(manifest, shas, findings, test_note),
        requested_by=str(requested_by or "toolsmith")[:40],
        context={"skill_id": manifest["id"], "manifest": manifest,
                 "sha256": shas, "hosts": list(hosts), "door_flag": digest,
                 "lint": findings[:10],
                 "test_ok": bool(tested.get("ok")),
                 "test_log_tail": str(tested.get("error")
                                      or tested.get("speech") or "")[:500]},
        ttl_seconds=REQUEST_TTL_SECONDS,
    )
    logger.info("skills: proposed %s (awaiting the owner)", manifest["id"])
    return {"ok": True, "proposed": True, "installed": False,
            "awaiting_approval": True, "request": request,
            "id": manifest["id"],
            "message": (f"The {manifest['name']} skill is ready for your "
                        f"approval. It reaches "
                        f"{', '.join(sorted({registrable_domain(h) for h in hosts})) or 'nothing'}"
                        f" and nothing else. The code and the test results are "
                        f"on the dashboard under Skills.")}


def _recent_approval(preferred: dict | None = None) -> dict | None:
    """A fresh, human-resolved, unspent approval from the durable queue, or None."""
    from puffpai.security import consent_requests as _creq

    now = datetime.now(timezone.utc)
    best = None
    best_at = None
    for req in _creq._load():
        if not isinstance(req, dict):
            continue
        if req.get("action") != INSTALL_ACTION or req.get("status") != "approved":
            continue
        if req.get("resolved_by") not in HUMAN_RESOLVERS:
            continue
        stamp = req.get("resolved_at")
        if not isinstance(stamp, str) or stamp in _spent_approvals:
            continue
        try:
            when = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if (now - when).total_seconds() > APPROVAL_FRESHNESS_SECONDS:
            continue
        if (preferred is not None and preferred.get("resolved_at") == stamp
                and preferred.get("entity_id") == req.get("entity_id")):
            return req
        if best_at is None or when > best_at:
            best, best_at = req, when
    return best


def apply_approved_install(request: dict | None = None, *,
                           human_confirmed=False) -> dict:
    """Install an APPROVED draft. The only writer in this package. Never raises."""
    from puffpai.ha_bridge import is_human_confirmation
    from puffpai.skills import manifest as _manifest
    from puffpai.skills import store as _store

    if not is_human_confirmation(human_confirmed,
                                 where="skills.install.apply_approved_install"):
        logger.warning("skills: refused an install with no in-process human "
                       "confirmation token")
        return {"ok": False, "installed": False,
                "error": "human confirmation token required",
                "message": ("I can't install a skill unless it comes from you "
                            "approving it, not from me.")}

    approval = _recent_approval(request)
    if approval is None:
        logger.warning("skills: refused an install with no fresh approval")
        return {"ok": False, "installed": False,
                "error": "no approved skill install on record",
                "message": ("I don't have your approval on record for that "
                            "skill, so I haven't installed it.")}

    context = approval.get("context") or {}
    skill_id = str(context.get("skill_id") or "")
    recorded = context.get("sha256")
    if not skill_id or not isinstance(recorded, dict) or not recorded:
        return {"ok": False, "installed": False,
                "error": "the approved record is not applicable",
                "message": "That approval doesn't name a skill I can install."}

    stamp = approval.get("resolved_at")
    try:
        draft = _store.draft_dir(skill_id)
        manifest, shas = _manifest.validate_dir(draft)
    except Exception as exc:
        with _lock:
            _spent_approvals.add(stamp)
        return {"ok": False, "installed": False, "error": str(exc),
                "message": f"I can't install that skill: {exc}"}

    if shas != recorded or manifest["id"] != skill_id:
        # Bytes moved between the card and now. Void the approval: an owner who
        # said yes to one file has not said yes to another.
        with _lock:
            _spent_approvals.add(stamp)
        logger.warning("skills: refused %s -- the draft no longer matches the "
                       "approved hashes", skill_id)
        return {"ok": False, "installed": False, "error": "sha256 mismatch",
                "message": ("The skill's files changed after you approved "
                            "them, so I've thrown that approval away. Nothing "
                            "was installed -- ask me again if you still want "
                            "it.")}

    with _lock:
        if stamp in _spent_approvals:
            return {"ok": False, "installed": False,
                    "error": "approval already spent",
                    "message": "That approval was already used."}
        _spent_approvals.add(stamp)

    target = _store.skill_dir(skill_id)
    if os.path.isdir(target):
        _store.remove(skill_id)
    try:
        os.replace(draft, target)          # atomic on the same filesystem
    except OSError as exc:
        logger.warning("skills: install of %s failed: %s", skill_id, exc)
        return {"ok": False, "installed": False, "error": str(exc),
                "message": f"I couldn't put that skill in place: {exc}"}

    hosts = manifest["permissions"]["network_hosts"]
    from puffpai.security import egress_doors as _doors
    flag = _doors.skill_door_flag(skill_id, 0, hosts)
    record = {
        "approved_at": stamp,
        "approved_by": approval.get("resolved_by"),
        "request_entity": approval.get("entity_id"),
        "sha256": shas,
        "version": manifest["version"],
        "hosts": list(hosts),
        "door_flag": flag,
        "installed_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(os.path.join(target, "installed.json"), "w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=2, sort_keys=True)
    _store.write_state(skill_id, dict(_store.DEFAULT_STATE,
                                      enabled=True,
                                      last_test={"ok": False, "at": "",
                                                 "log_tail": ""}))
    _store.rescan()

    # The test runs BEFORE the door is opened, and the door only opens if it
    # passed. It is an offline fixture run (``FixtureBroker``), so it needs no
    # door of its own -- and a skill that cannot be tested here must not end up
    # with an open door as a side effect of the order these two lines were in.
    tested = test_draft_installed(skill_id)
    _store.note_test(skill_id, ok=bool(tested.get("ok")),
                     log_tail=str(tested.get("error") or tested.get("speech") or ""))

    door_opened = False
    door_note = ""
    if not tested.get("ok"):
        # "Installed but switched off" is now a fact on disk rather than a
        # sentence in a message. On a box with no bubblewrap the test cannot be
        # run at all, so this is the state every install lands in there --
        # switched off, no door, and ``enable_installed`` refuses to move it.
        _store.set_enabled(skill_id, False,
                           reason=str(tested.get("error")
                                      or "it has not passed its own tests here")[:300])
    elif hosts:
        try:
            from puffpai.security import egress_admin as _admin
            _admin.set_door_open(_doors.skill_door_id(skill_id, 0), True)
            door_opened = True
        except Exception as exc:
            door_note = str(exc)
            logger.warning("skills: %s installed but its door stayed shut: %s",
                           skill_id, exc)
    _store.rescan()

    _log_activity("skill_installed",
                  f"Installed the skill '{manifest['name']}' ({skill_id}); "
                  f"door {'open' if door_opened else 'shut'} for "
                  f"{', '.join(hosts) or 'no host'}")

    message = (f"{manifest['name']} is installed. Say "
               f"\"{manifest['triggers'][0]}\" and I'll use it.")
    if tested.get("sandbox_unavailable"):
        message = (f"{manifest['name']} is installed but switched OFF, and it "
                   f"will stay off on this box. {NO_SANDBOX_INSTALL_SENTENCE}")
    elif not tested.get("ok"):
        message = (f"{manifest['name']} is installed but switched off until it "
                   f"passes its own tests here"
                   + (f": {tested.get('error')}" if tested.get("error") else "."))
    if hosts and not door_opened:
        message += (" Its door is still shut, so it can't reach "
                    f"{', '.join(hosts)} until you open it in Settings > Egress.")
    return {"ok": True, "installed": True, "id": skill_id,
            "door_opened": door_opened, "door_note": door_note,
            "sandbox_unavailable": bool(tested.get("sandbox_unavailable")),
            "test_ok": bool(tested.get("ok")), "message": message}


def installed_bytes(skill_id: str) -> dict:
    """The exact bytes of an INSTALLED skill, beside the hashes that were approved.

    The same shape as :func:`draft_bytes`, plus what only an installed skill
    has: ``sha256_recorded`` per file (out of ``installed.json`` -- what the
    owner said yes to), a freshly computed ``sha256`` of the bytes in this very
    read, and ``intact`` -- so "is the code on this box still the code I
    approved" is answerable by looking, not by trusting a stored boolean.

    Deliberately LENIENT about the manifest and strict about the files: a skill
    whose manifest no longer validates is exactly the skill an owner most needs
    to be able to read, so the manifest is reported as ``null`` with a reason
    rather than turning the whole view into a 404. Anything that is not a plain
    regular file -- a symlink, a directory, a missing member -- raises, and the
    route answers 404. Raises for an unknown skill.
    """
    import hashlib

    from puffpai.skills import lint as _lint
    from puffpai.skills import manifest as _manifest
    from puffpai.skills import store as _store

    folder = _store.skill_dir(skill_id)      # raises on an unusable id
    if os.path.islink(folder) or not os.path.isdir(folder):
        raise FileNotFoundError(f"no skill called {skill_id!r} is installed")
    sid = os.path.basename(folder)

    record = _store.read_installed(sid)
    recorded = record.get("sha256")
    if not isinstance(recorded, dict):
        recorded = {}

    files = []
    for name in _manifest.PACKAGE_FILES:
        path = os.path.join(folder, name)
        if os.path.islink(path) or not os.path.isfile(path):
            raise FileNotFoundError(f"{name} is missing from the {sid} skill")
        with open(path, "rb") as handle:
            raw = handle.read()
        fresh = hashlib.sha256(raw).hexdigest()
        approved = str(recorded.get(name) or "")
        files.append({
            "name": name,
            "bytes": len(raw),
            "sha256": fresh,
            "sha256_recorded": approved,
            "matches": bool(approved) and approved == fresh,
            "text": raw.decode("utf-8", "replace"),
        })

    manifest = None
    manifest_error = ""
    try:
        raw_manifest = json.loads(
            next(f["text"] for f in files if f["name"] == "manifest.json"))
        manifest = _manifest.validate(raw_manifest, skip_origin=f"skill:{sid}")
    except Exception as exc:
        manifest_error = str(exc)[:300]

    if manifest is not None:
        hosts = list(manifest["permissions"]["network_hosts"])
    else:
        # Fall back to the hosts on the approved record, never to the file:
        # what an unreadable manifest CLAIMS today is not what was consented to.
        hosts = [h for h in (record.get("hosts") or []) if isinstance(h, str)]

    source = next(f["text"] for f in files if f["name"] == "skill.py")
    intact, reason = _store.integrity(sid)
    return {
        "ok": True,
        "id": sid,
        "manifest": manifest,
        "manifest_error": manifest_error,
        "files": files,
        "lint": _lint.lint_source(source),
        "hosts": hosts,
        "domains": sorted({registrable_domain(h) for h in hosts}),
        "state": "installed",
        "installed": True,
        "intact": bool(intact),
        "integrity_reason": reason,
        "enabled": bool(_store.read_state(sid).get("enabled")),
        "approved_at": record.get("approved_at") or "",
        "door_flag": str(record.get("door_flag") or ""),
    }


def enable_installed(skill_id: str) -> dict:
    """Switch an installed skill back ON -- only if it is still what was approved.

    Two checks, both against the record written at install time and not against
    anything the skill says about itself now:

    1. **the bytes** -- every file still hashes to the sha256 in
       ``installed.json``; and
    2. **the reach** -- the door digest its manifest asks for today equals the
       ``door_flag`` recorded when the owner approved it. A skill that quietly
       grew a host would otherwise be re-enabled into a door nobody consented
       to.

    Either check failing is ``needs_reapproval``: no state is written, no flag
    is asserted, and the skill stays off. Re-approving means proposing again
    and seeing the card again, which is the only thing that can grant reach.

    Never raises. Returns a dict carrying ``status`` -- the HTTP code the route
    should answer with.
    """
    from puffpai.security import egress_admin as _admin
    from puffpai.security import egress_doors as _doors
    from puffpai.skills import store as _store

    try:
        folder = _store.skill_dir(skill_id)
    except Exception as exc:
        return {"ok": False, "status": 404, "error": "unknown skill",
                "message": f"I don't have a skill by that name: {exc}"}
    if os.path.islink(folder) or not os.path.isdir(folder):
        return {"ok": False, "status": 404, "error": "unknown skill",
                "message": (f"I don't have a skill called "
                            f"\"{str(skill_id)[:60]}\" installed.")}
    sid = os.path.basename(folder)

    # Third check, and the cheapest: there is no point re-verifying bytes that
    # nothing on this box can run. Deliberately AFTER the 404 arm above (an
    # unknown id gets the answer it deserves) and BEFORE anything writes state
    # or asserts a door flag -- a skill that cannot run must not end up with an
    # open door as a consolation prize.
    from puffpai.skills import sandbox as _sandbox
    if not _sandbox.sandbox_available():
        logger.warning("skills: refused to enable %s -- no sandbox on this box",
                       sid)
        return {"ok": False, "status": 409, "error": "no_sandbox",
                "id": sid, "enabled": False,
                "message": NO_SANDBOX_ENABLE_SENTENCE}

    intact, reason = _store.integrity(sid)
    if not intact:
        logger.warning("skills: refused to enable %s -- %s", sid, reason)
        return {"ok": False, "status": 409, "error": "needs_reapproval",
                "id": sid, "enabled": False,
                "message": (f"I can't switch that skill back on: {reason}. "
                            f"The code on this box isn't the code you "
                            f"approved, so it has to be approved again before "
                            f"it can run.")}

    record = _store.load(sid)
    if record is None:
        return {"ok": False, "status": 409, "error": "needs_reapproval",
                "id": sid, "enabled": False,
                "message": ("I can't switch that skill back on: its manifest "
                            "is not readable, so I can't tell what it would "
                            "reach. It has to be approved again.")}

    installed = _store.read_installed(sid)
    hosts = record["manifest"]["permissions"]["network_hosts"]
    recorded_flag = str(installed.get("door_flag") or "")
    expected_flag = _doors.skill_door_flag(sid, 0, hosts) if hosts else ""
    if expected_flag != recorded_flag:
        logger.warning("skills: refused to enable %s -- door digest %r is not "
                       "the approved %r", sid, expected_flag, recorded_flag)
        return {"ok": False, "status": 409, "error": "needs_reapproval",
                "id": sid, "enabled": False,
                "message": ("I can't switch that skill back on: the reach it "
                            "asks for now isn't the reach you approved "
                            + (f"({', '.join(hosts)})." if hosts
                               else "(it asked for none).")
                            + " It has to be approved again.")}

    _store.set_enabled(sid, True, reason="")

    door_opened = not hosts
    door_note = ""
    if hosts:
        try:
            # The ONLY flag re-asserted is the approved digest's: the door id
            # is built from the manifest this function just matched against
            # installed.json, and the rescan inside set_enabled pruned every
            # other skill_<id>_* key first.
            _admin.set_door_open(_doors.skill_door_id(sid, 0), True)
            door_opened = True
        except Exception as exc:
            door_note = str(exc)
            logger.warning("skills: %s enabled but its door stayed shut: %s",
                           sid, exc)

    name = record["manifest"]["name"]
    _log_activity("skill_enabled",
                  f"Switched the skill '{name}' ({sid}) back on after "
                  f"re-checking its code and its reach; door "
                  f"{'open' if door_opened else 'shut'} for "
                  f"{', '.join(hosts) or 'no host'}")

    triggers = record["manifest"]["triggers"]
    message = (f"{name} is back on. Say \"{triggers[0]}\" and I'll use it."
               if triggers else f"{name} is back on.")
    if hosts and not door_opened:
        message += (" Its door is still shut, so it can't reach "
                    f"{', '.join(hosts)} until you open it in Settings > "
                    f"Egress.")
    return {"ok": True, "status": 200, "id": sid, "enabled": True,
            "door_opened": door_opened, "door_note": door_note,
            "message": message}


def test_draft_installed(skill_id: str) -> dict:
    """Run the INSTALLED skill's fixtures, offline, right after installing."""
    from puffpai.skills import dispatch as _dispatch
    from puffpai.skills import store as _store

    return _dispatch.run_package(_store.skill_dir(skill_id), mode="test")


def _log_activity(kind: str, detail: str) -> None:
    try:
        from puffpai.agent_activity import log_activity
        log_activity("toolsmith", kind, detail, "done", {})
    except Exception as exc:  # pragma: no cover - the ledger never fails a call
        logger.debug("skills: activity not recorded (%s)", exc)


__all__ = [
    "APPROVAL_FRESHNESS_SECONDS", "HUMAN_RESOLVERS", "INSTALL_ACTION",
    "NO_SANDBOX_ENABLE_SENTENCE", "NO_SANDBOX_INSTALL_SENTENCE",
    "REQUEST_TTL_SECONDS", "apply_approved_install", "draft_bytes",
    "enable_installed", "installed_bytes", "propose_install",
    "registrable_domain", "reset_for_tests", "stage_example",
    "test_draft", "test_draft_installed",
]
