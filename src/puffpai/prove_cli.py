"""`puffpai prove`: ask the running build to prove itself.

BuildBot's ``prove`` verb, unchanged except for the module it lives in and the
port resolver, which is local here rather than shared with `puffpai serve`.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""
from __future__ import annotations

import argparse
import os
import sys


def _serve_port(args: argparse.Namespace, configured: int) -> int:
    """The port to ask on: ``--port``, then the environment, then config."""
    if getattr(args, "port", None):
        return int(args.port)
    for name in ("PUFFPAI_PORT", "PORT"):
        raw = os.environ.get(name)
        if raw is None or not str(raw).strip():
            continue
        try:
            return int(str(raw).strip())
        except ValueError:
            print(f"  Ignoring {name}={raw!r}: that is not a port number.")
    return int(configured)

def _api_key_source() -> str:
    """WHERE this build's API key comes from. Never the key itself.

    Answered in the same order ``api_server._load_or_create_api_key`` resolves
    it, and it stops at the first hit rather than minting anything: this command
    reports, it does not change the install it is describing.
    """
    from puffpai.config import get_state_dir

    if (os.environ.get("PUFFPAI_API_KEY") or "").strip():
        return "the PUFFPAI_API_KEY environment variable"
    try:
        from puffpai.security.secrets_provider import get_secret
        if (get_secret("PUFFPAI_API_KEY") or "").strip():
            return "a stored secret (vault or OS keyring)"
    except Exception:
        pass
    path = os.path.join(get_state_dir(), "api_key.txt")
    if os.path.isfile(path):
        return f"the key file {path}"
    return "nowhere yet - one is minted the first time the backend starts"


def _api_key_value(env_name: str = "PUFFPAI_API_KEY") -> str:
    """The key itself, resolved the way the backend resolves it. Never printed.

    Same order as :func:`_api_key_source`, and like it this mints nothing: a
    build with no key yet gets "" and the request goes out without a header,
    which the backend answers with a 401 that Prove reports as such.
    """
    from puffpai.config import get_state_dir

    value = (os.environ.get(env_name) or "").strip()
    if value:
        return value
    try:
        from puffpai.security.secrets_provider import get_secret
        value = (get_secret("PUFFPAI_API_KEY") or "").strip()
        if value:
            return value
    except Exception:
        pass
    try:
        with open(os.path.join(get_state_dir(), "api_key.txt"),
                  encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


#: What an older backend - one without the /build/prove/* routes - gets told.
PROVE_OLD_BACKEND = ("This bot is running a build from before `puffpai prove` "
                     "running, then run this again.")

#: Printed above a local check, so nobody reads it as a verdict on the bot at
#: --url. It is the folder this shell is in, and nothing more.
PROVE_LOCAL_LABEL = ("checked from this folder's build, not from a running "
                     "bot")


def _cmd_prove(args: argparse.Namespace) -> int:
    """Ask the running build to prove itself, and record what it answered.

    **The build being proved is the one that is running**, so both halves run
    inside it: this command asks the backend at ``--url`` for its own static
    record, asks it to run the live half against itself, prints what came back,
    and sends back the one answer a server cannot observe - whether the owner
    can see the bot in the dashboard's left menu.

    It writes nothing locally. Running the static half here would grade the
    machine the command was typed on, judge the running bot's answers against a
    name read from this shell's config, and drop a ``wizard_run.json`` into
    whatever folder that config resolved to. A backend
    that does not answer at all gets a local check instead, labelled as such.
    """
    from puffpai import prove, prove_live
    from puffpai.config import get_config

    cfg = get_config()
    url = (getattr(args, "url", None) or "").strip()
    if not url:
        host = str(cfg.api_server.host or "127.0.0.1")
        if host in ("0.0.0.0", "::", ""):
            host = "127.0.0.1"
        url = f"http://{host}:{_serve_port(args, cfg.api_server.port)}"
    key_env = (getattr(args, "api_key_env", None) or "PUFFPAI_API_KEY").strip()
    api_key = _api_key_value(key_env)
    timeout = float(getattr(args, "timeout", 120) or 120)

    def ask(path: str, body: dict | None = None):
        return prove_live.request_json(url, path, api_key=api_key,
                                       method="POST", body=body or {},
                                       timeout=timeout)

    try:
        static = ask("/build/prove/static")
        live_payload = ask("/build/prove/live")
    except prove_live.Unreachable as exc:
        if exc.status in (404, 405):
            print(PROVE_OLD_BACKEND)
            return 2
        if exc.status == 401:
            print(f"The backend at {url} refused this install's API key.")
            return 2
        return _prove_locally(exc, url)

    record = live_payload if isinstance(live_payload, dict) else {}
    if not record.get("ran"):
        record = static if isinstance(static, dict) else {}
    live = record.get("live") if isinstance(record.get("live"), dict) else {}

    print()
    print("Proof it works")
    print("=" * 60)
    _print_prove_checks(record.get("checks"))
    _print_prove_gaps(record.get("gaps"))
    if not live or not live.get("ran"):
        note = str((live or {}).get("note") or "")
        print(f"  ..   {note or 'the live half did not run'}")
        print("Start the backend, then run this again.")
        return 2
    _print_prove_checks(live.get("checks"))
    for turn in live.get("transcript") or []:
        print()
        print(f"  ? {turn.get('question')}")
        print(f"    {turn.get('reply') or '(nothing came back)'}")

    owner = _prove_owner_answer(args, record, live)
    if owner:
        try:
            ask("/build/prove/owner", {"owner": owner})
        except prove_live.Unreachable as exc:
            print(f"note: the backend did not record your answer ({exc}).")
    print()
    print(f"  owner: {owner or live.get('owner')}")
    failed = [c for c in list(record.get("checks") or [])
              + list(live.get("checks") or [])
              if c.get("result") == prove.FAIL]
    return 1 if failed else 0


def _print_prove_checks(checks) -> None:
    for check in checks or []:
        if not isinstance(check, dict):
            continue
        print(f"  {_prove_mark(str(check.get('result') or ''))} "
              f"{check.get('title')}: {check.get('reason')}")


def _print_prove_gaps(gaps) -> None:
    """Fix lines, rebuilt here from the typed fix. Never printed off the wire.

    The backend renders the same line, and it still is not the line printed:
    what arrives over HTTP is data, and a command an owner is invited to paste
    is decided by the registry in this process.
    """
    from puffpai.wizard import fixes

    shown = [(fixes.safe_gap(gap) or {}, fixes.fix_line(gap))
             for gap in gaps or [] if isinstance(gap, dict)]
    shown = [(gap, command) for gap, command in shown if command]
    if not shown:
        return
    print()
    print("  Not finished yet:")
    for gap, command in shown:
        # The line names what it fixes; a bare command under the wrong check
        # reads as the fix for that check.
        print(f"    {gap.get('what') or 'One thing is not finished.'}")
        print(f"      fix: {command}")


def _prove_owner_answer(args: argparse.Namespace, record: dict,
                        live: dict) -> str:
    """Ask the owner the one question no backend can answer for itself."""
    from puffpai import prove_live

    if getattr(args, "yes", False) or not sys.stdin.isatty():
        return ""
    dashboard = str(live.get("dashboard_url") or "")
    question = str(live.get("owner_question") or "").strip() or (
        f"Do you see {record.get('bot_name') or 'your bot'} in the left menu? "
        f"[y/n/skip]")
    print()
    print(f"Open {dashboard}" if dashboard
          else "Open the dashboard address this install prints.")
    try:
        answer = (input(f"{question} ") or "").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer.startswith("y"):
        return prove_live.OWNER_CONFIRMED
    if answer.startswith("n"):
        return prove_live.OWNER_REFUTED
    return prove_live.OWNER_UNPROVEN


def _prove_locally(exc: Exception, url: str) -> int:
    """The backend is not answering. Check this folder's build, and say so.

    Nothing is written: a static check run here is a check of the folder the
    command was typed in, and recording it as this install's proof is how a
    caller's machine came to be graded as somebody else's bot.
    """
    from puffpai import prove

    print()
    print("Proof it works")
    print("=" * 60)
    print(f"  ..   not run: backend not answering at {url} ({exc})")
    try:
        from puffpai import build_manifest
        manifest = build_manifest.read_manifest() or {}
    except Exception:
        manifest = {}
    if not manifest:
        print("  There is no build in this folder either, so there is nothing")
        print("  to check from here. Start the bot and run this again.")
        return 2
    print(f"  Falling back to a static check - {PROVE_LOCAL_LABEL}.")
    record = prove.static_prove()
    _print_prove_checks(record.get("checks"))
    _print_prove_gaps(record.get("gaps"))
    print(f"  ({PROVE_LOCAL_LABEL}; nothing was written down.)")
    return 2


def _prove_mark(result: str) -> str:
    return {"pass": "ok  ", "fail": "FAIL", "gap": "gap ",
            "not_applicable": "--  "}.get(result, "?   ")


__all__ = ["_cmd_prove", "PROVE_OLD_BACKEND", "PROVE_LOCAL_LABEL"]
