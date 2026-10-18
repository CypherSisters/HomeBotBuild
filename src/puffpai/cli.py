"""
PuffPAI Command-Line Interface

Provides headless operation for terminal-based chat, ZIM management,
firewall status, and system diagnostics.

Usage:
    python -m puffpai setup             # First-run setup wizard
    python -m puffpai chat              # Interactive text chat
    python -m puffpai ingest-zim <path> # Ingest a ZIM file
    python -m puffpai discover-zim      # List discovered ZIM files
    python -m puffpai firewall-status   # Show firewall status
    python -m puffpai version           # Print version

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Any

# The wizard's card packages came from BuildBot, where the whole wizard lived in
# this file, and they reach for it by name: ``from puffpai import cli`` then
# ``cli.enter_step``, ``cli._vault_dir``, ``cli.installed_launchers``. Re-export
# the host module's names here so those twelve packages stay BYTE-IDENTICAL to
# BuildBot's - one import line changed in twelve files is twelve places for the
# two builds to drift apart. Every name below is defined once, in
# :mod:`puffpai.wizard.host`, which aliases this tree's own implementation
# wherever there already is one.
from puffpai.wizard.host import (  # noqa: F401 - re-exported for the packages
    DASHBOARD_START_COMMAND,
    DASHBOARD_START_COMMAND_WINDOWS,
    DASHBOARD_START_STEPS,
    DEFAULT_BOT_NAME,
    MAX_STATED_RAM_GB,
    NO_STEP,
    POSTURE_URL_RULES,
    PROVE_LOCAL_LABEL,
    PROVE_OLD_BACKEND,
    RESUME_SET_ASIDE_LINE,
    RESUME_UNREADABLE_LINE,
    SetupRefused,
    VAULT_REJECTED_LINE,
    _RECOVERY_ESCAPES,
    _RECOVERY_MAX_MISMATCHES,
    _RECOVERY_MAX_SHOWS,
    _ask,
    _ask_paragraph,
    _ask_yes_no,
    _cmd_setup,
    _dashboard_url,
    _existing_vault_block,
    _maybe_scaffold_persona,
    _offer_resume,
    _ollama_address_source,
    _pick_by_id,
    _posture_violation,
    _print_closing,
    _print_packs_not_downloaded,
    _print_setup_header,
    _read_line,
    _reset_stdin_state,
    _read_pin,
    _refuse_for_posture,
    _run_setup,
    _setup_knowledge_step,
    _setup_manifest_step,
    _stdin_is_tty,
    _valid_ram_gb,
    _vault_dir,
    _vault_rejected,
    _vault_skipped,
    _with_persona_surfaces,
    _wizard_runner_parts,
    bot_name,
    current_step_label,
    dashboard_start_command,
    enter_step,
    installed_launchers,
    pair_code_command,
    pairing_block_lines,
    step_banner,
)


#: Whether stdin has run out on THIS run. Owned here because this is the module
#: the wizard packages and their tests reach for by name;
#: :func:`puffpai.wizard.host._read_line` is the only thing that sets it.
_stdin_exhausted = False

logger = logging.getLogger(__name__)


def _cmd_version(args: argparse.Namespace) -> int:
    """Print PuffPAI version."""
    from puffpai import __version__
    print(f"PuffPAI v{__version__}")
    return 0


# ---------------------------------------------------------------------------
# where - which install a command touches
# ---------------------------------------------------------------------------

def _api_key_source() -> str:
    """WHERE this build's API key comes from. Never the key itself.

    Answered in the same order ``api_server._load_or_create_api_key`` resolves
    it, and it stops at the first hit rather than minting anything: this command
    reports, it does not change the install it is describing.
    """
    if (os.environ.get("PUFFPAI_API_KEY") or "").strip():
        return "the PUFFPAI_API_KEY environment variable"
    try:
        from puffpai.security import api_key_store
        where, _key = api_key_store.stored()
        if where:
            return where
    except Exception:
        pass
    return "nowhere yet - one is minted the first time the backend starts"


def _cmd_where(args: argparse.Namespace) -> int:
    """Print what a `puffpai` command in this shell would actually touch.

    Every line is a fact an owner cannot otherwise see, and the one thing that
    is deliberately absent is the key's value: the SOURCE is what tells you
    whether two halves of an install agree, and printing the secret to a console
    puts it in a scrollback and a screenshot.
    """
    from puffpai.config import get_config, get_state_dir

    cfg = get_config()
    print("This is the install a `puffpai` command here would use:")
    print(f"  checkout:    {cfg.REPO_ROOT}")
    print(f"  state dir:   {get_state_dir()}")
    print(f"  data dir:    {cfg.DATA_DIR}")
    print(f"  config dir:  {cfg.CONFIG_DIR}")
    print(f"  vault:       {_vault_where_line(cfg)}")
    print(f"  backend:     http://{cfg.api_server.host}:{cfg.api_server.port}")
    url, source = _ollama_address_source(args, cfg)
    print(f"  Ollama:      {url} (from {source or 'the built-in default'})")
    print(f"  API key:     from {_api_key_source()} (value not printed)")
    return 0


def _vault_where_line(cfg) -> str:
    """The vault folder, and in a few words what is there. Never opens it."""
    from puffpai import developer_api
    try:
        state = developer_api.vault_folder_state(_vault_dir(cfg))
    except Exception:
        return f"{_vault_dir(cfg)} (could not be checked)"
    if not state["drive_there"]:
        what = "its drive is not plugged in"
    elif state["made"]:
        what = "made"
    elif state["made"] is None:
        what = "could not be read"
    else:
        what = "not made yet"
    return f"{state['folder']} ({what})"


def _local_clock(moment) -> str:
    """"12:25 am": this computer's own clock, not UTC (a child reads this)."""
    try:
        local = moment.astimezone()
    except Exception:  # noqa: BLE001 - a naive time is shown as it is
        local = moment
    hour = local.hour % 12 or 12
    return f"{hour}:{local.minute:02d} {'am' if local.hour < 12 else 'pm'}"


def _cmd_setup_code(args: argparse.Namespace) -> int:
    """Reprint the first-run setup code.

    LOCAL BY CONSTRUCTION: this is a shell command on the machine running
    HomeBot, which is the whole point — the code proves console access, so the
    only way to read it is to have the console. There is deliberately no HTTP
    route that returns it.

    Exit 0 when a code is printed, 1 when setup is already finished (nothing to
    print, and the code has been destroyed).
    """
    from puffpai.security import setup_code
    if not setup_code.is_required():
        print("Setup is already finished — there is no first-run code. "
              "Changing the build now needs an unlocked vault.")
        return 1
    if setup_code.announce(print_line=print) is None:
        print("Could not read or create the first-run code. Check that the "
              "state directory is writable (set $PUFFPAI_DATA_DIR to choose "
              "it).")
        return 1
    remaining = setup_code.locked_for()
    if remaining:
        print(f"  Note: remote setup is locked out for {remaining} more "
              f"seconds after too many wrong codes.")
    return 0


def _cmd_prove(args: argparse.Namespace) -> int:
    """``puffpai prove``. Lives in :mod:`puffpai.prove_cli`; imported lazily."""
    from puffpai import prove_cli
    return prove_cli._cmd_prove(args)


def _cmd_serve(args: argparse.Namespace) -> int:
    """Run the local REST API server.

    Importing :mod:`puffpai.api_server` resolves the app API key, and resolving
    an unusable one raises deliberately: that value authorises every request to
    the backend, so starting with a placeholder would be worse than not
    starting. The refusal is a written sentence naming where to fix it - print
    it, rather than letting it reach the owner as a traceback.
    """
    try:
        from puffpai.config import get_config
        from puffpai import api_server
    except ImportError as exc:
        print("The API server needs Flask. Install it with:  pip install flask")
        print(f"(import error: {exc})")
        return 1
    except RuntimeError as exc:
        print(str(exc))
        return 1

    cfg = get_config()
    host = getattr(args, "host", None) or cfg.api_server.host
    port = int(getattr(args, "port", None) or cfg.api_server.port)

    # What the team does (each install, start and error) goes to the console,
    # which is the bot's log when the launcher runs it.
    from puffpai.logging_config import setup_logging
    setup_logging("INFO")
    print(f"API server on http://{host}:{port}")
    print(f"  Ollama backend: {cfg.ollama.url} ({cfg.ollama.model})")
    print("  Press Ctrl+C to stop.")
    try:
        starter = getattr(api_server, "start_background_services", None)
        if callable(starter):
            starter()
        api_server.app.run(host=host, port=port, debug=False,
                           use_reloader=False, threaded=True)
        return 0
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0
    except Exception as exc:  # noqa: BLE001 - a start failure is a sentence
        print(f"Could not start the API server: {exc}")
        return 1


def _cmd_chat(args: argparse.Namespace) -> int:
    """Interactive text chat loop (headless)."""
    from puffpai import __version__

    print(f"PuffPAI v{__version__} — Interactive Chat")
    print("Type 'quit' or 'exit' to leave.\n")

    try:
        from puffpai.config import get_llm_backend
        backend = get_llm_backend()
        print(f"Backend: {backend.model_name}")
    except Exception as exc:
        print(f"[Warning] No LLM backend available: {exc}")
        print("Running in echo mode (no AI responses).\n")
        backend = None

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            return 0

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "bye"):
            print("Goodbye!")
            return 0

        if backend is None:
            print(f"PuffPAI: [Echo] {user_input}\n")
        else:
            try:
                response = backend.chat(
                    [{"role": "user", "content": user_input}],
                    temperature=0.7,
                )
                print(f"PuffPAI: {response}\n")
            except Exception as exc:
                print(f"PuffPAI: [Error] {exc}\n")

    return 0


def _cmd_discover_zim(args: argparse.Namespace) -> int:
    """List discovered ZIM files from configured paths."""
    from puffpai.knowledge.discovery import discover_from_config, get_zim_metadata

    zim_files = discover_from_config()
    if not zim_files:
        print("No ZIM files found in configured paths.")
        print("Configure paths.zim_dirs in config/puffpai_config.json")
        return 0

    print(f"Discovered {len(zim_files)} ZIM file(s):\n")
    for path in zim_files:
        try:
            meta = get_zim_metadata(path)
            size_mb = meta["file_size_bytes"] / (1024 * 1024)
            print(f"  {meta['filename']}")
            print(f"    Path: {meta['filepath']}")
            print(f"    Entries: {meta['entry_count']:,} | Clusters: {meta['cluster_count']:,}")
            print(f"    Size: {size_mb:.1f} MB | ZIM v{meta['major_version']}.{meta['minor_version']}")
            print()
        except Exception as exc:
            print(f"  {path}")
            print(f"    [Error reading metadata: {exc}]\n")

    return 0


def _cmd_ingest_zim(args: argparse.Namespace) -> int:
    """Ingest a ZIM file into the knowledge base."""
    import os

    zim_path = args.path
    if not os.path.isfile(zim_path):
        print(f"Error: File not found: {zim_path}")
        return 1

    print(f"Ingesting ZIM file: {zim_path}")

    try:
        from puffpai.knowledge.zim_reader import ZIMReader, ZIMTruncatedError
        from puffpai.knowledge.ingestion import KnowledgeIngestion

        try:
            with ZIMReader(zim_path) as reader:
                print(f"  Entries: {reader.entry_count:,}")
        except ZIMTruncatedError as exc:
            print(f"Error: {exc}")
            return 1

        # Delegate to KnowledgeIngestion.ingest_zim() rather than re-implementing
        # the walk here. The previous inline loop called get_article(0) with a
        # hardcoded placeholder index, so it re-ingested entry 0 under every other
        # entry's title, and iter_articles() defaulted to namespace 'A' (empty for
        # ZIM v6). ingest_zim() detects the namespace, retrieves by real index, and
        # also picks up VTT transcripts and PDFs.
        ingestion = KnowledgeIngestion()
        docs = ingestion.ingest_zim(
            zim_path,
            max_articles=args.max or 0,   # 0 = all; previously capped at 1000
        )

        print(f"  Done! Ingested {len(docs):,} documents from "
              f"{os.path.basename(zim_path)}")
        if not docs:
            print("  (nothing indexed -- pack may hold no extractable text)")
        return 0

    except Exception as exc:
        print(f"Error: {exc}")
        return 1


def _cmd_firewall_status(args: argparse.Namespace) -> int:
    """Show current firewall status."""
    try:
        from puffpai.firewall_manager import FirewallManager
        fm = FirewallManager()
        rules = fm.list_rules()
        print(f"Firewall rules: {len(rules)}")
        for rule in rules[:20]:
            print(f"  {rule}")
        if len(rules) > 20:
            print(f"  ... and {len(rules) - 20} more")
        return 0
    except Exception as exc:
        print(f"Firewall status unavailable: {exc}")
        return 1


def _cmd_hatch(args: argparse.Namespace) -> int:
    """Run (or replay) the hatch ritual from the terminal.

    The terminal has no talking avatar, so the script is printed as the lines
    the Puff would speak; the dashboard hatch page performs the same ritual
    with the real voice.
    """
    from puffpai import hatch

    require_voice = not args.no_voice
    preflight = hatch.run_preflight(require_voice=require_voice)

    # ASCII-only console output: the Windows terminal codepage mangles
    # em-dashes, and this command is exactly the one run on a fresh box.
    print("Pre-flight (real probes; /health is a liar and is not used):")
    for check in preflight["checks"]:
        mark = {"ok": "ok  ", "warn": "warn", "fail": "FAIL"}[check["status"]]
        print(f"  [{mark}] {check['label']}: {check['detail']}")
    if not preflight["ok"]:
        print("\nRefusing to hatch: fix the failures above first. "
              "A Puff does not promise 'everything stays in this box' "
              "while half the box is down.")
        return 2

    try:
        result = hatch.perform_hatch(
            args.name, rename=args.rename, require_voice=require_voice,
            preflight=preflight)
    except hatch.HatchNameRequired:
        print("\nThis build has not hatched yet. The Puff asks:")
        print(f'  "{hatch.ask_line()}"')
        print("\nName it with: py -3.14 -m puffpai hatch --name <Name>")
        return 1

    print()
    if result["replay"]:
        print(f"Replaying the hatch for {result['name']} "
              f"(hatched {result['hatch_date']}, "
              f"replay #{result['replays']}).")
        if result.get("name_ignored"):
            print("  (name argument ignored: replays never rename; "
                  "pass --rename to rename on purpose)")
    else:
        print(f"Hatched: {result['name']} on {result['hatch_date']}.")
    print()
    for line in result["script"]:
        print(f"  {result['name']}: {line['text']}")
    return 0


def _cmd_certificate(args: argparse.Namespace) -> int:
    """Generate the adoption certificate (SVG natively; PNG via Pillow)."""
    from puffpai import certificate

    fmt = "png" if args.png else "svg"
    try:
        payload = certificate.generate(fmt=fmt)
    except certificate.CertificateUnavailable as exc:
        print(f"Error: {exc}")
        return 1

    out = args.out
    if not out:
        out = f"adoption_certificate.{fmt}"
    with open(out, "wb") as fh:
        fh.write(payload)
    print(f"Certificate written: {out} ({len(payload):,} bytes, {fmt})")
    if fmt == "svg" and not certificate.png_available():
        print("  (PNG export unavailable: Pillow not installed; "
              "the SVG is the canonical artifact)")
    return 0


# ---------------------------------------------------------------------------
# persona / apps — scaffold and inspect what this build carries
# ---------------------------------------------------------------------------

def _nudge_local_rescan() -> bool:
    """Ask a server on THIS machine to re-read its apps. Best effort, silent.

    The registry notices a new app folder on its own now (it fingerprints its
    roots on every read), so this is an optimisation, not the mechanism: it
    closes the window between "the CLI wrote the folder" and "the server's next
    read", and it also covers a filesystem whose timestamps cannot be trusted.

    Deliberately narrow, because this is a CLI reaching for a credential to call
    an authenticated route:

    * loopback only -- ``127.0.0.1`` is a literal here, never the configured
      host, so this can never leave the machine;
    * the port comes from this build's own config;
    * the key comes from the same read-only resolution chain the server uses
      (vault -> OS keyring -> environment). Nothing is minted, written or
      printed, and the key never reaches stdout;
    * every failure is swallowed. No server running is the NORMAL case for
      somebody scaffolding a persona before first boot, and it must not look
      like an error.

    Returns True only when the server answered 200.
    """
    try:
        import json
        import urllib.error
        import urllib.request

        from puffpai.config import get_config
        from puffpai.security.secrets_provider import get_secret_with_source

        key, _source = get_secret_with_source('PUFFPAI_API_KEY')
        if not key:
            return False
        port = int(getattr(get_config().api_server, "port", 0) or 0)
        if not port:
            return False
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/apps/rescan",
            data=json.dumps({}).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "X-API-Key": str(key).strip()},
            method="POST")
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.status == 200
    except Exception:
        return False


def _cmd_persona(args: argparse.Namespace) -> int:
    """``puffpai persona new|list`` — scaffold a persona, or list the ones made."""
    from puffpai import app_registry, persona_scaffold

    action = getattr(args, "persona_action", "list")

    if action == "edit":
        app_id = getattr(args, "persona_id", None)
        new_id = getattr(args, "rename_id", None)
        if not app_id or not new_id:
            print("Say which persona and its new id: "
                  "puffpai persona edit <id> --rename-id <new_id>")
            return 1
        try:
            moved = persona_scaffold.rename_id(app_id, str(new_id))
        except persona_scaffold.ScaffoldError as exc:
            print(f"Nothing was changed: {exc}")
            return 1
        except OSError as exc:
            print(f"Nothing was renamed: the folders could not be moved ({exc}).")
            return 1
        for line in persona_scaffold.rename_summary_lines(moved):
            print(line)
        return 0 if moved.get("installed") else 1

    if action == "list":
        personas = persona_scaffold.personas()
        if not personas:
            print("No personas of your own yet.")
            print(f"  They live in {app_registry.user_packs_dir()}")
            print("  Make one:  puffpai persona new gardenbot "
                  "--name \"GardenBot\" --goal \"...\" "
                  "--lists \"plantings,harvest log\"")
            return 0
        print(f"Personas you have made ({app_registry.user_packs_dir()}):")
        for entry in personas:
            print(f"  {entry['id']} - {entry['display_name']}")
            if entry["blurb"]:
                print(f"      {entry['blurb']}")
            print(f"      apps: {', '.join(entry['apps']) or 'none'}")
            if entry["missing_apps"]:
                print(f"      MISSING and not installed: "
                      f"{', '.join(entry['missing_apps'])}")
        return 0

    app_id = getattr(args, "persona_id", None)
    if not app_id:
        print("Which persona? `puffpai persona new <id> --name \"Name\" "
              "--goal \"what it is for\"`")
        return 1
    try:
        result = persona_scaffold.scaffold(
            app_id,
            name=getattr(args, "name", None) or app_id,
            goal=getattr(args, "goal", None) or "",
            lists=getattr(args, "lists", None),
        )
    except persona_scaffold.ScaffoldError as exc:
        print(f"Nothing was written: {exc}")
        return 1
    for line in persona_scaffold.scaffold_summary_lines(result):
        print(line)
    if result["installed"] and _nudge_local_rescan():
        print("  The running build has picked it up.")
    return 0 if result["installed"] else 1


def _cmd_apps(args: argparse.Namespace) -> int:
    """``puffpai apps list|rescan`` — what this build carries, and re-reading it."""
    import os

    from puffpai import app_registry

    action = getattr(args, "apps_action", "list")
    if action in _APPS_LIVE_ACTIONS:
        return _cmd_apps_live(action, getattr(args, "target", None))
    if action == "rescan":
        manifests = app_registry.rescan()
        print(f"Rescanned. {len(manifests)} apps installed.")
    else:
        manifests = app_registry.current_manifests()

    records = app_registry.ui_records(manifests)
    packs = app_registry.load_packs(manifests=manifests)
    if not records:
        print("No apps installed.")
    else:
        print("Apps:")
        for record in records:
            line = f"  {record['id']:<14} {record['name']}"
            if record["route"]:
                line += f"   page {record['route']}"
            print(line)
            if record["surfaces"]:
                print(f"      surfaces: {', '.join(record['surfaces'])}")
            if record["degraded"]:
                print(f"      degraded: {record['degraded_reason']}")
    if packs:
        print("Packs:")
        for pack in packs:
            print(f"  {pack['id']:<14} {pack['display_name']} "
                  f"({', '.join(a['id'] for a in pack['apps'])})")
    print("Read from:")
    for folder in app_registry.apps_dirs():
        mark = "" if os.path.isdir(folder) else "   (not there yet)"
        print(f"  {folder}{mark}")
    for problem in app_registry.load_problems():
        print(f"  ! {problem.get('detail')}")
    return 0


# ---------------------------------------------------------------------------
# agents - the seven-agent team (status + headless demo)
# ---------------------------------------------------------------------------

def _cmd_agents(args: argparse.Namespace) -> int:
    """Show the agent roster, or run a headless demo of the bus + Drawbridge veto."""
    import tempfile

    from puffpai.agents import Budget, Envelope, build_team, roster_summary
    from puffpai.agents.preferences import display_name
    from puffpai.security.audit_log import AuditLog

    action = getattr(args, "agents_action", None) or "status"

    print("=" * 64)
    print("  PuffPAI product-agent team")
    print("=" * 64)
    for row in roster_summary():
        # The display name, not the internal id: the id is `sentinel` and the
        # agent is called Drawbridge everywhere a person reads it.
        print(f"  {row['display_name']:<11} scope={row['scope']:<10} "
              f"{row['workflow']:<5} {row['model_class']}")
    print()

    # Route the demo's audit trail to an isolated file so it is easy to show
    # and never touches the shared security log.
    audit_path = os.path.join(tempfile.mkdtemp(prefix="puffpai_agents_demo_"),
                              "audit.jsonl")
    audit = AuditLog(log_file=audit_path)
    bus = build_team(audit=audit)
    health = bus.health()
    print(f"Bus: {health['agent_count']} agents wired, "
          f"reviewer={display_name(health['reviewer'])} "
          f"(wired={health['reviewer_wired']})")

    if action == "status":
        print("\nRun `puffpai agents demo` to exercise routing and the "
              "Drawbridge veto.")
        return 0

    print("\n" + "-" * 64)
    print("  DEMO 1 - Concierge routes a message through the bus (read-only)")
    print("-" * 64)
    chat = Envelope(intent="chat", payload={"text": "give me my daily briefing"},
                    requester="owner", origin_agent="cli")
    reply = bus.send(chat, to="concierge")
    print(f"  category      : {reply.get('category')}")
    print(f"  delegated_to  : {reply.get('delegated_to')}")
    print(f"  delegate ok   : {(reply.get('delegate_reply') or {}).get('ok')}")

    print("\n" + "-" * 64)
    print("  DEMO 2 - A state-changing request Drawbridge APPROVES is delivered")
    print("-" * 64)
    ok_env = Envelope(intent="enqueue",
                      payload={"task_intent": "nightly_backup", "target": "caretaker"},
                      requester="owner", origin_agent="cli", state_changing=True,
                      budget=Budget(max_actions=2))
    reply = bus.send(ok_env, to="majordomo")
    print(f"  reply         : ok={reply.get('ok')} queued={reply.get('queued')}")
    print("  -> Drawbridge reviewed it, allowed it, and the bus delivered it.")

    print("\n" + "-" * 64)
    print("  DEMO 3 - A state-changing request Drawbridge VETOES never runs")
    print("-" * 64)
    bad_env = Envelope(intent="run_workflow", payload={"egress": True},
                       requester="owner", origin_agent="cli", state_changing=True,
                       budget=Budget(max_actions=1, allow_egress=False))
    reply = bus.send(bad_env, to="majordomo")
    print(f"  reply         : denied={reply.get('denied')} by={reply.get('by')} "
          f"verdict={reply.get('verdict')}")
    print(f"  reason        : {reply.get('reason')}")
    print("  -> The envelope was blocked at the bus; Majordomo never saw it.")

    print("\n" + "-" * 64)
    print("  Audit trail (no silent actions - every send/veto is recorded)")
    print("-" * 64)
    for entry in reversed(audit.get_recent_activity(limit=12)):
        print(f"  {entry['action']:<16} {entry['result']:<8} {entry['details']}")
    return 0


# ---------------------------------------------------------------------------
# vault - status / unlock / lock / store / get
# ---------------------------------------------------------------------------

def _vault_password(args: argparse.Namespace, prompt: str) -> str:
    """Resolve a vault password from --password (test-only) or getpass."""
    if getattr(args, "password", None):
        return str(args.password)
    import getpass
    try:
        return getpass.getpass(prompt)
    except (EOFError, KeyboardInterrupt):
        return ""


def _cmd_vault(args: argparse.Namespace) -> int:
    """Inspect and use the encrypted vault."""
    from puffpai.config import get_config
    from puffpai.security.vault import (
        Vault, VaultError, AuthenticationError,
    )

    cfg = get_config()
    vdir = _vault_dir(cfg)
    vault = Vault(vault_dir=vdir)
    action = args.vault_action
    from puffpai.security.vault import missing_words
    if action in ("location", "move"):
        # Before the missing-drive stop: "location" is how the owner sees
        # where the vault should be, and how the owner points this build at a drive
        # that came back under another letter. Neither ever makes a second
        # vault (security/vault_place.py).
        return _vault_place_command(args, vault)
    missing = missing_words(vault)
    if missing and action != "lock":
        # The vault is on a drive that is not plugged in: say so, and never
        # point at making a new one.
        print(missing)
        return 1

    if action == "status":
        return _vault_status(vdir)

    if action == "unlock":
        if not vault.is_initialized:
            print("No vault exists yet. Run `puffpai setup` to create one.")
            return 1
        try:
            vault.unlock(_vault_password(args, "Vault password: "))
        except AuthenticationError:
            print("Incorrect vault password.")
            return 1
        except VaultError as exc:
            print(f"Vault error: {exc}")
            return 1
        print("Password verified; vault unlocked for this process.")
        print("Note: CLI commands run in separate processes, so `store`/`get` "
              "each prompt for the password.")
        return 0

    if action == "lock":
        vault.lock()
        print("Vault locked.")
        return 0

    if action == "recover":
        return _vault_recover(vault)

    if action in ("store", "get"):
        if not args.name:
            print(f"Usage: puffpai vault {action} <name>")
            return 2
        if not vault.is_initialized:
            print("No vault exists yet. Run `puffpai setup` to create one.")
            return 1
        try:
            vault.unlock(_vault_password(args, "Vault password: "))
        except AuthenticationError:
            print("Incorrect vault password.")
            return 1
        except VaultError as exc:
            print(f"Vault error: {exc}")
            return 1

        try:
            if action == "store":
                import getpass
                if getattr(args, "value", None) is not None:
                    value = str(args.value)  # test-only non-interactive path
                else:
                    try:
                        value = getpass.getpass(f"Value for {args.name} (hidden): ")
                    except (EOFError, KeyboardInterrupt):
                        value = ""
                if not value:
                    print("Empty value - nothing stored.")
                    return 1
                vault.store(args.name, value)
                print(f"Stored secret {args.name!r}. (value not echoed)")
                return 0
            else:  # get
                retrieved = vault.retrieve(args.name)
                if retrieved is None:
                    print(f"No secret named {args.name!r} in the vault.")
                    return 1
                print(retrieved)
                return 0
        finally:
            vault.lock()

    print(f"Unknown vault action: {action}")
    return 2


def _vault_place_command(args: argparse.Namespace, vault) -> int:
    """``puffpai vault location``: where the vault lives. The vault stays in
    the folder setup made it in, so a folder or ``move`` is refused."""
    from puffpai.security import vault_place as vp
    target = (args.name or "").strip()
    if args.vault_action == "location" and not target:
        info = vp.location(vault)
        print(f"Vault folder : {info['folder']}")
        print(f"Vault        : {info['line']}")
        if info["is_default"]:
            print("Kept         : on this computer, in this build's own folder "
                  "(the default)")
        elif info["network"]:
            print("Kept         : in a network folder. " + vp.NETWORK_LINE)
        else:
            print("Kept         : in a folder you chose")
        return 0 if info["drive_there"] else 1
    if args.vault_action == "move" or target:
        # HomeBot's vault stays in the folder setup made it in.
        print("Your vault stays in the folder setup made it in. Moving it, or "
              "pointing this bot at another vault, is not part of this build.")
        return 1


def _vault_status(vdir: str) -> int:
    """``puffpai vault status``: where the vault is, and what to do next.

    Only commands that do what they say are offered. ``vault store`` needs a
    vault that is already made, so it is never the way to make one; a vault
    whose drive is missing is never told to make a new one (that would be a
    second, empty vault).
    """
    from puffpai import developer_api

    state = developer_api.vault_folder_state(vdir)
    print(f"Vault folder : {state['folder']}")
    if not state["drive_there"]:
        print("Vault        : Your vault drive is not plugged in.")
        print("Plug in the drive that holds this folder, then try again. Do "
              "not make a new vault: your items are in this one.")
        return 1
    if state["made"] is None:
        print("Vault        : this folder could not be read.")
        return 1
    if not state["made"]:
        print("Vault        : not made yet")
        print("Make it with `puffpai setup`. It asks you to pick a PIN and "
              "shows a recovery code once, to write down.")
        return 0
    print("Vault        : made. It stays locked here; your bot's web page "
          "and chat open it with your PIN.")
    print("Save one item : `puffpai vault store <name>` (asks for your PIN, "
          "then the item)")
    print("Read one item : `puffpai vault get <name>` (asks for your PIN)")
    print("Forgot your PIN: `puffpai vault recover` (asks for your recovery "
          "code)")
    return 0


def _vault_recover(vault) -> int:
    """``puffpai vault recover``: open the vault with the recovery code and
    choose a new PIN. The terminal twin of the Vault page's "Forgot your PIN?"
    and of ``POST /vault/recover``.

    * The code and both PINs are read without echo (:func:`_read_pin`). There
      is deliberately no flag for them: a flag puts a secret in the shell's
      history and in the list of running programs. With no terminal (input
      from a pipe) they are read as lines, still never printed.
    * The new PIN is held to the setup wizard's rules
      (:func:`puffpai.setup_ops.validate_vault_pin`) here, before anything is
      tried, and again inside the vault, after its cooldown check and before
      any key derivation.
    * Nothing typed and nothing issued is logged or stored. The new code is
      shown once, with the wizard's write-it-down ceremony
      (:func:`_recovery_ceremony`).
    * Personal details the bot filed while the vault was locked are opened
      into it as part of the recovery, the same as at any unlock.

    Cooldown after wrong codes is per process, so each run of this command
    gets the vault's free tries again. That is not a gap worth closing here:
    anyone who can run this command can already copy the vault's files.
    The cooldown protects the network path (``POST /vault/recover``).
    """
    from puffpai import setup_ops
    from puffpai.security import vault_inbox
    from puffpai.security.vault import (
        AuthenticationError, VaultError, VaultThrottled, WeakSecretError,
    )

    if not vault.is_initialized:
        print("There is no vault on this computer yet, so there is nothing to "
              "recover. Run `puffpai setup` to make one.")
        return 1
    if getattr(vault, "is_legacy", False) or not vault.has_recovery_code:
        print("This vault has no recovery code, so no code can open it. If "
              "you still know the PIN, open the Vault page, unlock it, and "
              "press New recovery code.")
        return 1

    tty = _stdin_is_tty()
    print("Open your vault with the recovery code you wrote down, and choose "
          "a new PIN.")
    print("Nothing you type here is shown or kept.")
    code = _read_pin("Recovery code: ").strip()
    if not code:
        print("No recovery code was typed. Nothing was changed.")
        return 1
    new_pin = _read_pin("New PIN: ")
    ok, reason = setup_ops.validate_vault_pin(new_pin)
    if not ok:
        print(reason or "That PIN is not allowed.")
        print("Nothing was changed, and your recovery code still works.")
        return 1
    if _read_pin("Type the new PIN once more: ") != new_pin:
        print("Those two PINs did not match. Nothing was changed, and your "
              "recovery code still works.")
        return 1

    waiting = 0
    try:
        waiting = vault_inbox.pending_count(vault.vault_dir)
    except Exception:  # noqa: BLE001 - a count is a nicety
        waiting = 0

    try:
        _session, new_code = vault.redeem_recovery_code(
            code, new_pin, policy=setup_ops.validate_vault_pin)
    except VaultThrottled as exc:
        print(f"Too many wrong codes. Wait {exc.retry_after} seconds and try "
              f"again. Nothing was changed.")
        return 1
    except WeakSecretError as exc:
        print(str(exc) or "That PIN is not allowed.")
        print("Nothing was changed, and your recovery code still works.")
        return 1
    except AuthenticationError:
        print("That recovery code does not open this vault. Check what you "
              "wrote down and try again. Nothing was changed.")
        return 1
    except VaultError:
        # The vault's own wording can name its folder; say it plainly instead.
        logger.info("Vault recovery from the terminal could not run")
        print("The vault could not be recovered on this computer right now. "
              "Nothing was changed.")
        return 1
    except Exception:  # noqa: BLE001 - a plain sentence, not a traceback
        logger.exception("Vault recovery from the terminal failed")
        print("Something went wrong on this computer and the recovery did "
              "not finish. Try it again. If your recovery code is refused, "
              "try your new PIN on the Vault page: it may already open it.")
        return 2

    try:
        print("")
        print("Your vault is open again. Your new PIN opens it from now on.")
        print("The recovery code you just used and your old PIN no longer "
              "work.")
        left = waiting
        try:
            left = vault_inbox.pending_count(vault.vault_dir)
        except Exception:  # noqa: BLE001
            pass
        if waiting and left < waiting:
            print(f"{waiting - left} personal detail(s) the bot noticed while "
                  f"the vault was locked were put into it.")
        print("Here is your NEW recovery code.")
        _recovery_ceremony(new_code, new_pin, vault, recorded=False,
                           non_interactive=not tty)
        print("")
        print("If your bot is running, open the vault again on the Vault page "
              "with your new PIN.")
        return 0
    finally:
        vault.lock()


def _recovery_ceremony(code: str, pin: str, vault, *,
                       recorded: bool = True,
                       non_interactive: bool = False) -> bool:
    """Show the recovery code and make the owner type it back. True when they did.

    The same policy the wizard's vault step runs
    (:func:`puffpai.wizard.secure_vault.show_and_confirm`), drawn on this
    terminal: declining keeps the code, a wrong answer is tried again and then
    replaced, and running out leaves the vault locked and working with the
    build record saying, honestly, that no code was confirmed.
    """
    from puffpai.wizard import secure_vault

    class _Terminal:
        @staticmethod
        def show(lines):
            print()
            for line in lines:
                print(line)

        @staticmethod
        def ask(prompt):
            return _read_line(f"{prompt}: ")

    return secure_vault.show_and_confirm(code, pin, vault, sink=_Terminal(),
                                         non_interactive=non_interactive,
                                         recorded=recorded)


# ---------------------------------------------------------------------------
# devices - Home Assistant device approval queue (feeds the consent ledger)
# ---------------------------------------------------------------------------

def _cmd_devices(args: argparse.Namespace) -> int:
    """List and rule on Home Assistant devices awaiting a human decision.

    New HA entities are recorded as *pending* in the consent ledger; a human
    approves or denies each one here. Home Assistant is optional - when it is
    disabled or unreachable, ``scan`` simply finds nothing and the other
    actions still work against whatever the ledger already holds.
    """
    from puffpai.config import get_config
    from puffpai.security.consent_gate import get_ledger

    action = args.devices_action
    cfg = get_config()

    if action == "scan":
        if not cfg.ha.enabled:
            print("Home Assistant is disabled (run `puffpai setup` and configure "
                  "HA to watch for devices). Nothing to scan.")
            return 0
        from puffpai.ha_device_watcher import get_watcher
        watcher = get_watcher()
        found = watcher.check_for_new_devices()
        if not found:
            print("No new devices found (HA unreachable, or nothing new).")
            return 0
        print(f"Found {len(found)} new device(s), all recorded as pending:")
        for d in found:
            mfg = f" [{d.manufacturer}]" if d.manufacturer else ""
            print(f"  {d.entity_id:<32} {d.friendly_name}{mfg}")
        print("Approve or deny with: puffpai devices approve|deny <entity_id>")
        return 0

    if action == "list":
        ledger = get_ledger()
        state = ledger.all()
        # Enrich with any friendly names the watcher recorded, best-effort.
        names: dict[str, str] = {}
        try:
            from puffpai.ha_device_watcher import get_watcher
            for d in get_watcher().get_pending():
                names[d.entity_id] = d.friendly_name
        except Exception:
            pass
        if not state:
            print("No devices tracked yet. Run `puffpai devices scan` (needs HA), "
                  "or approve/deny an entity id directly.")
            return 0
        order = {"pending": 0, "denied": 1, "approved": 2}
        for eid, row in sorted(state.items(),
                               key=lambda kv: (order.get(str(kv[1].get("status")), 9), kv[0])):
            status = row.get("status", "?")
            label = names.get(eid, "")
            suffix = f"  {label}" if label else ""
            print(f"  [{status:<8}] {eid}{suffix}")
        return 0

    if action in ("approve", "deny"):
        if not args.entity_id:
            print(f"Usage: puffpai devices {action} <entity_id>")
            return 2
        ledger = get_ledger()
        result = ledger.approve(args.entity_id) if action == "approve" \
            else ledger.deny(args.entity_id)
        # Keep the watcher's local queue in sync when it is available.
        try:
            from puffpai.ha_device_watcher import get_watcher
            watcher = get_watcher()
            if action == "approve":
                watcher.approve_device(args.entity_id)
            else:
                watcher.deny_device(args.entity_id)
        except Exception:
            pass
        print(f"{args.entity_id}: {result['status']}")
        return 0

    print(f"Unknown devices action: {action}")
    return 2


def _cmd_services(args: argparse.Namespace) -> int:
    """``puffpai services``: which outside services the live cards use.

    ``list`` says each card's service in plain words; ``on <name>`` lets that
    card use its service (and, if the bot was set to stay off the internet,
    lets it use only the services you said yes to); ``off <name>`` stops it.
    """
    from puffpai import feeds

    action = getattr(args, "services_action", None) or "list"
    target = getattr(args, "name", None)
    if action == "list":
        apps = feeds.feed_apps()
        if not apps:
            print("No live cards are installed, so no outside service is used "
                  "by one.")
            print("Every other outside service is listed and switched in one "
                  "place: puffpai outside")
            return 0
        for manifest, _feed in apps:
            state = feeds.service_state(str(manifest.get("id")))
            print(f"  {manifest.get('id'):<28} {state['message']}")
        print("Every other outside service is listed and switched in one place: "
              "puffpai outside")
        return 0
    if not target:
        print(f"Say which one: puffpai services {action} <name>. "
              f"`puffpai services` lists the names.")
        return 1
    from puffpai import outside_services
    if outside_services.row(target) and not target.startswith("card-"):
        # One switch place for every other outside service.
        print(f"That one is switched in Settings, Bridge switches, or with: "
              f"puffpai outside {action} {target}")
        return 1
    try:
        state = (feeds.services_on(target) if action == "on"
                 else feeds.set_allowed(target, False))
    except feeds.FeedError as exc:
        print(exc.message)
        return 1
    if state.get("changed_setting"):
        print("Your bot may now use the outside services you said yes to. "
              "Every other one stays off.")
    print(state["message"])
    if state.get("what_leaves") and action == "on":
        print(f"What leaves your computer: {state['what_leaves']}")
    return 0


#: ``puffpai apps`` actions for live-data apps and the drafts this build made.
_APPS_LIVE_ACTIONS = ("drafts", "install", "discard", "allow", "deny", "feed")


def _cmd_apps_live(action: str, target: "str | None") -> int:
    """Live-data apps: see drafts, add one, allow or stop its outside service."""
    from puffpai import feed_generator, feeds

    if action == "drafts":
        drafts = feed_generator.list_drafts()
        if not drafts:
            print("No live-data apps are waiting for you.")
            return 0
        print("Waiting for your yes:")
        for draft in drafts:
            print(f"  {draft['id']:<30} {draft['name']} - from {draft['host']}")
            review = feed_generator.load_draft(draft["id"]).get("review") or []
            for row in review:
                # The same three plain words the dashboard card uses.
                mark = {"pass": "OK", "fail": "Problem"}.get(row["result"], "Not sure")
                print(f"      {mark:<8} {row['check']}: {row['why']}")
        print("To add one:  puffpai apps install <name>")
        return 0
    if not target:
        print(f"Which app? Say it as: puffpai apps {action} <name>")
        return 2
    try:
        if action == "install":
            result = feed_generator.install_draft(target)
            print(f"Added {result['name']}. It will not fetch anything until "
                  f"you allow it.")
            print(f"  What leaves your computer: {result['service']['what_leaves']}")
            print(f"  To allow it:  puffpai apps allow {target}")
            return 0
        if action == "discard":
            removed = feed_generator.discard_draft(target)
            print("Removed that draft." if removed else "There was no draft by that name.")
            return 0 if removed else 1
        if action in ("allow", "deny"):
            state = feeds.set_allowed(target, action == "allow")
            if action == "deny":
                print(f"Stopped. {state['message']}")
                return 0
            if state["state"] == "open":
                print(f"OK. {state['message']}")
                print(f"  What leaves your computer: {state['what_leaves']}")
                return 0
            print(f"Your yes is saved. {state['message']}")
            if state.get("how_to_fix"):
                print(f"  To change that, run:  {state['how_to_fix']}")
            return 1
        # feed: fetch now if due, then show the card
        card = feeds.refresh(target, force=True)
    except (feed_generator.DraftError, feeds.FeedError) as exc:
        print(exc.message)
        return 1
    print(f"{card['name']} - from {card['service']['name']} "
          f"({card['service']['host']})")
    if card["card"] is None:
        print(f"  {card['message']}")
        if card.get("how_to_fix"):
            print(f"  To fix it, run:  {card['how_to_fix']}")
        return 1
    shown = card["card"]
    if shown["kind"] == "value":
        print(f"  {shown['label']}: {shown['value']} {shown.get('unit') or ''}".rstrip())
        if shown.get("caption"):
            print(f"  ({shown['caption']})")
    elif shown["kind"] == "image":
        for key in ("headline", "caption", "image_credit"):
            if shown.get(key):
                print(f"  {shown[key]}")
        print("  Picture saved on this computer." if shown.get("image")
              else "  No picture this time.")
    else:
        for row in shown.get("items") or []:
            detail = f" - {row['detail'][:80]}" if row.get("detail") else ""
            print(f"  * {row['title']}{detail}")
    print(f"  {card['credit']}  Fetched {card['fetched_at']}.")
    return 0



def _cmd_skills(args: argparse.Namespace) -> int:
    """``puffpai skills submit|list|status`` — the CLI twin of ``/skills``.

    ``submit`` is the only one that writes, and it writes exactly what the HTTP
    route writes: a draft, plus one consent request. It cannot install anything
    — that still needs the owner approving the card on the dashboard — and on a
    box with no bubblewrap it says so rather than implying the skill will run.
    """
    import os

    from puffpai.skills import dispatch as _dispatch
    from puffpai.skills import install as _install
    from puffpai.skills import sandbox as _sandbox
    from puffpai.skills import store as _store
    from puffpai.skills import submit as _submit

    action = getattr(args, "skills_action", "list")

    if action == "list":
        usable, reason = _dispatch.available()
        rows = _dispatch.listing()
        if not rows:
            print("No skills installed.")
        else:
            print("Skills:")
            for row in rows:
                state = "on" if row["enabled"] else "OFF"
                if not row["intact"]:
                    state = "OFF (code changed since you approved it)"
                elif row["enabled"] and not row["tested"]:
                    state = "OFF (has not passed its tests here)"
                print(f"  {row['id']:<20} {row['name']}  [{state}]")
                print(f"      reaches: {', '.join(row['hosts']) or 'nothing'}")
                print(f"      say:     {'; '.join(row['triggers'])}")
        drafts = []
        try:
            drafts = sorted(n for n in os.listdir(_store.drafts_root())
                            if not n.startswith("."))
        except OSError:
            drafts = []
        if drafts:
            print("Drafts waiting for your approval (Dashboard > Skills):")
            for name in drafts:
                print(f"  {name}")
        if not usable:
            print(f"Note: {reason}")
        return 0

    if action == "status":
        skill_id = getattr(args, "skills_path", None)
        if not skill_id:
            print("Which skill? `puffpai skills status <id>`")
            return 1
        row = next((r for r in _dispatch.listing() if r["id"] == skill_id), None)
        if row is None:
            draft = os.path.join(_store.drafts_root(), str(skill_id))
            if os.path.isdir(draft):
                print(f"{skill_id}: submitted, waiting for your approval.")
                print("  Approve it on the dashboard under Skills; nothing runs "
                      "until you do.")
                return 0
            print(f"No skill or draft called {skill_id!r}.")
            return 1
        print(f"{row['id']} - {row['name']} (v{row['version']}, "
              f"by {row['author']})")
        print(f"  {row['description']}")
        print(f"  enabled:  {row['enabled']}")
        print(f"  tested:   {row['tested']}")
        print(f"  intact:   {row['intact']}")
        print(f"  reaches:  {', '.join(row['hosts']) or 'nothing'}")
        print(f"  say:      {'; '.join(row['triggers'])}")
        print(f"  runs:     {row['runs']}")
        if row["last_error"]:
            print(f"  last error:   {row['last_error']}")
        if row["last_refusal"]:
            print(f"  last refusal: {row['last_refusal']}")
        if not _sandbox.sandbox_available():
            print(f"  NOTE: {_install.NO_SANDBOX_ENABLE_SENTENCE}")
        return 0

    # submit
    path = getattr(args, "skills_path", None)
    if not path:
        print("Which package? `puffpai skills submit <dir|zip>`")
        return 1
    try:
        if os.path.isdir(path):
            files = _submit.collect_from_dir(path)
        elif os.path.isfile(path):
            with open(path, "rb") as handle:
                files = _submit.collect_from_zip(
                    handle.read(_submit.MAX_UPLOAD_BYTES + 1))
        else:
            print(f"Nothing was written: {path} is not a folder or a zip file.")
            return 1
        skill_id = getattr(args, "id", None) or _submit.manifest_id_of(files)
        result = _submit.submit_and_propose(
            skill_id, files, replace=bool(getattr(args, "replace", False)),
            requested_by="owner")
    except _submit.SubmissionRefused as exc:
        print(f"Nothing was written: {exc}")
        return 1
    except Exception as exc:
        print(f"Nothing was installed: {exc}")
        return 1

    print(f"Submitted: {result['id']} ({', '.join(result['files'])})")
    hosts = (result["manifest"]["permissions"]["network_hosts"]
             if result.get("manifest") else [])
    print(f"  reaches: {', '.join(hosts) or 'nothing'}")
    if result.get("proposed"):
        print("  A consent card is waiting for you on the dashboard under "
              "Skills. Nothing is installed until you approve it.")
        print(f"  {result['message']}")
        return 0
    print(f"  NOT proposed: {result.get('error') or 'unknown reason'}")
    print("  The draft is on disk; fix it and submit again with --replace.")
    return 1


# ---------------------------------------------------------------------------
# setup: the console's own runner parts (the one summary, the build, and
# Toolsmith's model). The schedule's runner parts are assembled in
# :func:`puffpai.wizard.host._wizard_runner_parts`.
# ---------------------------------------------------------------------------

TOOLSMITH_QUESTION = "Who should do Toolsmith's installs?"
TOOLSMITH_KEY_LINE = "Paste the key in the web page's Settings, or here:"


def _setup_toolsmith_model_step(ctx) -> str:
    """Toolsmith's model, asked once on the console: this computer (the
    default, also for ``--yes``) or Anthropic with the owner's key.

    The key is read with no echo and goes only through
    ``toolsmith_cloud.choose_cloud``, the function /toolsmith/model uses, into
    the vault. It is never printed, logged or kept in the run record; only the
    choice word is (``shared["toolsmith_asked"]``).
    """
    shared = ctx.shared
    if shared.get("toolsmith_asked") or ctx.non_interactive:
        return "local"
    from puffpai.security import consent_tickets, keys, toolsmith_cloud
    from puffpai.wizard import render_cli
    print()
    print(TOOLSMITH_QUESTION)
    print("  1. This computer (private, slower)")
    print("  2. Anthropic (faster, your choice)")
    answer = (_ask("Pick 1 or 2", "1") or "1").strip()
    if answer not in ("2", "anthropic", "Anthropic"):
        shared["toolsmith_asked"] = "local"
        print("Toolsmith uses the model on this computer. You can change it in Settings.")
        return "local"
    shared["toolsmith_asked"] = "cloud"
    for line in render_cli.wrap(toolsmith_cloud.CONSENT_TEXT):
        print(line)
    ids = list(toolsmith_cloud.MODELS)
    for n, mid in enumerate(ids, 1):
        tag = " (suggested)" if mid == toolsmith_cloud.DEFAULT_MODEL else ""
        print(f"  {n}. Claude {toolsmith_cloud.MODELS[mid]['name']}{tag}")
    pick = (_ask("Which model", "1") or "1").strip()
    model = ids[int(pick) - 1] if pick.isdigit() and 1 <= int(pick) <= len(ids)         else toolsmith_cloud.DEFAULT_MODEL
    print(TOOLSMITH_KEY_LINE)
    key = _read_pin("Anthropic key (it will not show, Enter to do it in Settings): ")
    try:
        if not key.strip():
            print("No key yet. Toolsmith uses this computer until you add it in Settings.")
            return "local"
        try:
            keys.check_shape("anthropic", key)
        except keys.KeyRefused as exc:
            print(f"{exc.message} You can add it in Settings.")
            return "local"
        from puffpai.config import get_config
        from puffpai.security.vault import Vault
        vault = Vault(vault_dir=_vault_dir(get_config()))
        if not vault.is_initialized:
            print("Your key is kept in your vault, and there is no vault yet. "
                  "Add the key in Settings once it is set.")
            return "local"
        pin = _read_pin("Vault PIN (it opens the vault so the key can be locked in it): ")
        try:
            vault.unlock(pin)
        except Exception:  # noqa: BLE001 - a wrong PIN or a vault error, said plainly
            print("That PIN did not open the vault. Nothing was sent. "
                  "You can add the key in Settings.")
            return "local"
        finally:
            pin = ""
        try:
            got = toolsmith_cloud.choose_cloud(
                model, consent_tickets.terminal_device_id(), key=key.strip(),
                vault=vault)
        except toolsmith_cloud.CloudError as exc:
            print(f"{exc.message} Toolsmith uses this computer for now.")
            return "local"
        finally:
            vault.lock()
        print(got.get("line") or "Toolsmith's model: Anthropic")
        return "cloud"
    finally:
        key = ""


def _streamed_factory(ctx, apply, *, out=print, every_s: float = 0.25):
    """Run the factory and print its Building lines as they arrive.

    Each applier's ``ctx.progress`` appends to ``shared["factory"]["lines"]``
    while it works. This prints every new line there while the factory
    runs, then whatever the finished record holds that was not printed yet,
    so a build whose appliers stream nothing prints what it has at the end,
    as before."""
    import threading

    from puffpai.wizard import render_cli, service as wizard_service

    printed = [0]
    stop = threading.Event()
    lock = threading.Lock()

    def flush(shared) -> None:
        with lock:
            lines = wizard_service.build_lines(shared)
            for line in lines[printed[0]:]:
                for part in render_cli.wrap(line, 2, 4):
                    out(part)
            printed[0] = max(printed[0], len(lines))

    def watch() -> None:
        while not stop.wait(every_s):
            flush(ctx.shared)

    watcher = threading.Thread(target=watch, name="factory-lines", daemon=True)
    watcher.start()
    try:
        result = apply(ctx)
    finally:
        stop.set()
        watcher.join(timeout=2)
    flush(ctx.shared)
    if isinstance(result, dict) and isinstance(result.get("shared"), dict):
        flush(result["shared"])
    return result


def _settled_interview(interview, shared) -> dict:
    """The interview as the approved plan left it (see
    :func:`puffpai.wizard.proposal.settle_interview`)."""
    from puffpai.wizard import proposal
    return proposal.settle_interview(interview or {},
                                     (shared or {}).get("summary"))


def _wizard_summary_step(ctx) -> dict:
    """The one summary: show the plan, take "Looks good" or changes.

    Every download size and every outside
    service on one screen, one "Looks good", and "Change something" opens any
    item. What the owner decided goes into the run's shared notebook as
    ``summary``; the cards after this one answer from it
    (:func:`puffpai.wizard.proposal.answers_for_card`).

    ``--yes`` shows the plan and approves it with only what the command line
    named switched on: nobody read it, so nothing that downloads or reaches
    out is said yes to on their behalf.
    """
    from puffpai.wizard import proposal, render_cli, wording

    sections = ctx._sections
    added_similar: list[str] = []

    def draw(changes=None):
        # A "Similar projects also have" line the owner ticked becomes a
        # job; its finds move under it (nothing is searched again).
        for name in (changes or {}).get("similar") or ():
            if name in added_similar:
                continue
            try:
                proposal.add_similar(ctx.shared, name)
                added_similar.append(name)
            except proposal.SimilarRefused as exc:
                render_cli.say_line(str(exc))
        return proposal.summary_for(args=ctx.args, cfg=ctx.cfg,
                                    shared=ctx.shared, sections=sections,
                                    changes=changes)

    shown = draw()
    state = None
    for _attempt in range(3):
        if getattr(ctx, "console", False):
            changes = render_cli.summary_screen(
                shown, rebuild=draw, non_interactive=ctx.non_interactive)
        else:
            # Something other than this console is drawing the wizard (a
            # test's renderer): nothing is read from the keyboard, and the plan
            # is taken as proposed -- with nothing that downloads switched on
            # unless it was already on by default.
            changes = {"on": {}}
        final = draw(changes) if changes and any(
            changes.get(key) for key in ("on", "model", "reach", "similar")) else shown
        if getattr(ctx, "console", False) and not ctx.non_interactive:
            # The one town question, only when a line on the plan needs it.
            typed = render_cli.town_screen(final)
            if typed is not None:
                changes = dict(changes or {}, town=typed)
        try:
            state = proposal.decide(final, changes=changes,
                                    non_interactive=ctx.non_interactive,
                                    args=ctx.args)
            break
        except proposal.ProposalError as exc:
            # Never build from a plan that is not the one approved: say what
            # did not fit and show the plan again.
            render_cli.say_line(f"That change could not be used ({exc}). "
                                f"Here is the plan again.")
            shown = draw()
    if state is None:
        raise SetupRefused(
            "The plan could not be settled.",
            "A change to it kept not fitting what the plan offers.",
            ["Run `puffpai setup` again and say Looks good to the plan as "
             "shown."])
    mode = wording.mode_of(ctx.shared)
    name = str(ctx.shared.get("bot_name") or DEFAULT_BOT_NAME)
    if not ctx.non_interactive and getattr(ctx, "console", False):
        render_cli.say_line(wording.say("approved", mode, name=name))
    shared_out = {"summary": state,
                  "interview": proposal.settle_interview(
                      ctx.shared.get("interview"), state)}
    if added_similar:
        shared_out.update({key: ctx.shared[key] for key in (
            "needs", "needs_draft", "needs_parts") if key in ctx.shared})
    return {"shared": shared_out,
            "note": f"summary approved with "
                    f"{sum(1 for item in state['items'] if item['on'])} "
                    f"item(s) on"}



def _setup_arguments(sub: argparse.ArgumentParser) -> None:
    """Attach the setup wizard's flags, importing it lazily.

    Lazy so that building the parser for `puffpai version` does not drag in the
    census, the catalogs and the vault.
    """
    from puffpai import setup_cli
    setup_cli.add_arguments(sub)
    # Flags the wizard's cards read that the conversation's own list does not
    # carry. Added only when absent, so the two lists never register one twice.
    known = set(sub._option_string_actions)  # noqa: SLF001
    if "--age" not in known:
        sub.add_argument("--age", dest="age", default=None,
                         help="The builder's age. Only changes how the wizard "
                              "words its questions (under 13 gets the simpler "
                              "wording); only an age group is kept, never the "
                              "number, and the finished bot never reads it")
    if "--download-model" not in known:
        sub.add_argument("--download-model", dest="download_model",
                         action="store_true",
                         help="With --yes: download the brain the plan picks. "
                              "Without it a --yes run downloads nothing nobody "
                              "said yes to")
    if "--own-place" not in known:
        # Plain words: the same switch under the name an owner is shown.
        sub.add_argument("--own-place", dest="scaffold", action="store_true",
                         help="Give the bot a place of its own (a folder, a "
                              "page and its lists); the same as --scaffold")


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="puffpai",
        description="PuffPAI, a privacy-first, offline-capable AI assistant",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # version
    sub = subparsers.add_parser("version", help="Print version")
    sub.set_defaults(func=_cmd_version)

    # setup - the first-run wizard. Its steps and flags live in
    # puffpai.setup_cli (it is ~700 lines of conversation, and this parser stays
    # readable); the verb is registered here so `puffpai setup` is one program
    # with every other verb.
    sub = subparsers.add_parser(
        "setup", help="First-run setup wizard (hardware, brain, the three "
                      "zones, household, storage)")
    _setup_arguments(sub)

    # prove - ask the RUNNING build to prove itself. The build being proved is
    # the one that is running, so both halves run inside it: this verb asks the
    # backend for its own record and sends back the one answer a server cannot
    # observe (whether the owner can see the bot in the left menu).
    sub = subparsers.add_parser(
        "prove", help="Ask the running build to prove it works, and say what it "
                      "could not prove")
    sub.add_argument("--url", default=None,
                     help="The backend to ask (default: this build's own "
                          "configured address)")
    sub.add_argument("--port", type=int, default=None,
                     help="Port, when the backend is not on the configured one")
    sub.add_argument("--api-key-env", dest="api_key_env",
                     default="PUFFPAI_API_KEY",
                     help="Environment variable holding the API key to send")
    sub.add_argument("--timeout", type=float, default=120,
                     help="Seconds to wait for each half (default: 120)")
    sub.add_argument("--yes", "-y", action="store_true",
                     help="Ask the owner nothing; the one human check is "
                          "recorded as unproven")
    sub.set_defaults(func=_cmd_prove)

    # serve - run the backend. The closing screen tells the owner to run this,
    # so it has to exist as a verb of this program.
    sub = subparsers.add_parser(
        "serve", help="Run the local REST API server (dashboard + HA backend)")
    sub.add_argument("--host", default=None, help="Bind address")
    sub.add_argument("--port", type=int, default=None, help="Port")
    sub.set_defaults(func=_cmd_serve)

    # setup-code - reprint the first-run code for a browser on another device.
    sub = subparsers.add_parser(
        "setup-code",
        help="Reprint the first-run setup code (needed only to run setup from "
             "a browser on another device)")
    sub.set_defaults(func=_cmd_setup_code)

    # chat
    sub = subparsers.add_parser("chat", help="Interactive text chat (headless)")
    sub.set_defaults(func=_cmd_chat)

    # where
    sub = subparsers.add_parser(
        "where",
        help="Print which install this command touches: state dir, config dir, "
             "vault folder, backend port, Ollama address, and where the API key "
             "comes from (never the key itself)")
    sub.set_defaults(func=_cmd_where)

    # agents
    sub = subparsers.add_parser(
        "agents", help="Show the seven-agent team, or run a headless demo")
    sub.add_argument("agents_action", nargs="?", choices=["status", "demo"],
                     default="status",
                     help="'status' (roster + wiring) or 'demo' (routing + veto)")
    sub.set_defaults(func=_cmd_agents)

    # sources - find more knowledge and add it after the build
    from puffpai.knowledge import find_more as _find_more
    _find_more.add_cli_parser(subparsers)

    # knowledge - learn the books already in the knowledge folder
    sub = subparsers.add_parser(
        "knowledge",
        help="Offline knowledge: list the books, or learn new ones now")
    sub.add_argument("knowledge_action", nargs="?", choices=["list", "sync"],
                     default="list",
                     help="'list' (each book and its state) or 'sync' (learn new books now)")
    sub.add_argument("--zim-dir", default=None,
                     help="Knowledge folder to read (default: the configured one)")
    sub.set_defaults(func=_cmd_knowledge)

    # vault
    sub = subparsers.add_parser(
        "vault",
        help="Encrypted vault: status, unlock, lock, store, get, recover")
    sub.add_argument("vault_action",
                     choices=["status", "unlock", "lock", "store", "get",
                              "recover", "location", "move"],
                     help="What to do with the vault. 'recover' opens it with "
                          "your recovery code and sets a new PIN. 'location' "
                          "shows where it lives")
    sub.add_argument("name", nargs="?", default=None,
                     help="Secret name (for 'store' / 'get'), or a folder "
                          "(for 'location' / 'move')")
    sub.add_argument("--password", default=None,
                     help="Vault password (TEST/AUTOMATION ONLY - you are "
                          "normally prompted)")
    sub.add_argument("--value", default=None,
                     help="Secret value for 'store' (TEST/AUTOMATION ONLY - "
                          "normally prompted, hidden)")
    sub.set_defaults(func=_cmd_vault)

    # devices
    sub = subparsers.add_parser(
        "devices", help="Home Assistant device approval queue (consent ledger)")
    sub.add_argument("devices_action",
                     choices=["scan", "list", "approve", "deny"],
                     help="'scan' (poll HA for new devices), 'list' (tracked "
                          "devices + status), 'approve'/'deny' <entity_id>")
    sub.add_argument("entity_id", nargs="?", default=None,
                     help="Entity id for 'approve' / 'deny'")
    sub.set_defaults(func=_cmd_devices)

    # persona - an app and a pack of the owner's own, written from their words.
    sub = subparsers.add_parser(
        "persona",
        help="Scaffold a persona of your own (one app + one pack), or list them")
    sub.add_argument("persona_action", nargs="?", choices=["new", "edit", "list"],
                     default="list",
                     help="'new' (write one), 'edit' (give it a new id) or "
                          "'list' (the ones you have made)")
    sub.add_argument("persona_id", nargs="?", default=None,
                     help="The app/pack id for 'new': lowercase, letters, "
                          "digits and underscores (e.g. gardenbot)")
    sub.add_argument("--name", default=None,
                     help="Display name (e.g. \"GardenBot\")")
    sub.add_argument("--goal", default=None,
                     help="What it is for, in plain words. Recorded as the "
                          "app's blurb and mined for the words it answers to; "
                          "never put into the model's instructions")
    sub.add_argument("--lists", default=None,
                     help="Lists it should keep, comma separated "
                          "(e.g. \"plantings,harvest log\")")
    sub.add_argument("--rename-id", dest="rename_id", default=None,
                     help="For 'edit': the new id (moves the app and the pack)")
    sub.set_defaults(func=_cmd_persona)

    # apps
    sub = subparsers.add_parser(
        "apps", help="What apps and packs this build carries (and rescan)")
    sub.add_argument("apps_action", nargs="?",
                     choices=["list", "rescan", *_APPS_LIVE_ACTIONS],
                     default="list",
                     help="'list' (what is installed), 'rescan' (re-read the "
                          "folders - this IS the install mechanism), 'drafts' "
                          "(live-data apps waiting for your yes), 'install', "
                          "'discard', 'allow' or 'deny' <name> (let an app use "
                          "its outside service, or stop it), 'feed' <name> "
                          "(fetch and show its live card)")
    sub.add_argument("target", nargs="?", default=None,
                     help="the app or draft name, for install/discard/allow/"
                          "deny/feed")
    sub.set_defaults(func=_cmd_apps)

    # skills - attach your own Python. Submitting is not installing: the owner
    # still approves a consent card, and nothing runs without bubblewrap.
    sub = subparsers.add_parser(
        "skills",
        help="Submit a skill of your own for approval, or list/inspect the "
             "ones installed")
    sub.add_argument("skills_action", nargs="?",
                     choices=["list", "submit", "status"], default="list",
                     help="'submit' (offer a package for approval), 'list', "
                          "or 'status <id>'")
    sub.add_argument("skills_path", nargs="?", default=None,
                     help="For 'submit': the package folder or zip. For "
                          "'status': the skill id")
    sub.add_argument("--id", default=None,
                     help="Submit under this id instead of the manifest's "
                          "(they must still agree)")
    sub.add_argument("--replace", action="store_true",
                     help="Overwrite an existing DRAFT of the same id. Never "
                          "an installed skill")
    sub.set_defaults(func=_cmd_skills)

    # discover-zim
    sub = subparsers.add_parser("discover-zim", help="List discovered ZIM files")
    sub.set_defaults(func=_cmd_discover_zim)

    # ingest-zim
    sub = subparsers.add_parser("ingest-zim", help="Ingest a ZIM file")
    sub.add_argument("path", help="Path to ZIM file")
    sub.add_argument("--max", type=int, default=1000,
                     help="Max articles to ingest (default: 1000)")
    sub.set_defaults(func=_cmd_ingest_zim)

    # firewall-status
    sub = subparsers.add_parser("firewall-status", help="Show firewall status")
    sub.set_defaults(func=_cmd_firewall_status)

    # hatch
    sub = subparsers.add_parser(
        "hatch", help="Run (or replay) the hatch ritual")
    sub.add_argument("--name", help="Name for a first hatch (or with --rename)")
    sub.add_argument("--rename", action="store_true",
                     help="Explicitly rename an already-hatched Puff")
    sub.add_argument("--no-voice", action="store_true",
                     help="Do not require the local Piper voice in pre-flight")
    sub.set_defaults(func=_cmd_hatch)

    # certificate
    sub = subparsers.add_parser(
        "certificate", help="Generate the adoption certificate")
    sub.add_argument("--out", help="Output path (default: "
                                   "adoption_certificate.<fmt>)")
    sub.add_argument("--png", action="store_true",
                     help="PNG via Pillow instead of the native SVG")
    sub.set_defaults(func=_cmd_certificate)

    _add_catalogs_parser(subparsers)
    services = subparsers.add_parser(
        "services", help="Which outside services your live cards use: list, "
                         "on <name>, off <name>")
    services.add_argument("services_action", nargs="?", default="list",
                          choices=["list", "on", "off"])
    services.add_argument("name", nargs="?", default=None,
                          help="The live card's name, as `puffpai services` "
                               "lists it")
    services.set_defaults(func=_cmd_services)
    # home + routines (commands live in puffpai.routines_cli)
    from puffpai import routines_cli
    routines_cli.add_parsers(subparsers)
    # what leaves your computer (commands live in puffpai.outside_cli)
    from puffpai import outside_cli
    outside_cli.add_parsers(subparsers)
    # start / stop: the bot and its web page together (puffpai.start_cli)
    from puffpai import start_cli
    start_cli.add_parsers(subparsers)

    # sign-up keys (commands live in puffpai.keys_cli)
    from puffpai import keys_cli
    keys_cli.add_parsers(subparsers)
    # how the dashboard looks (commands live in puffpai.looks.cli)
    from puffpai.looks import cli as looks_cli
    looks_cli.add_parsers(subparsers)
    # add more after the build (commands live in puffpai.add_more_cli)
    from puffpai import add_more_cli
    add_more_cli.add_parsers(subparsers)
    # remove BuildBot, the locked box, integrated things (each only
    # when it is in this build)
    add_optional_parsers(subparsers)

    return parser


#: Command groups other parts of the build own. Each
#: adds its own commands when it is in this build.
OPTIONAL_PARSER_MODULES: tuple[str, ...] = ("puffpai.remove_cli",
                                          "puffpai.box_cli",
                                          "puffpai.integrate.cli")


def _optional(modname: str):
    """Another part of the build, or ``None`` when it is not in this build."""
    import importlib
    try:
        return importlib.import_module(modname)
    except ModuleNotFoundError as e:
        if e.name is not None and (e.name == modname or modname.startswith(e.name + ".")):
            return None          # not in this build yet
        raise                    # exists but broken: fail loudly


def add_optional_parsers(subparsers: Any,
                         modules: tuple[str, ...] = OPTIONAL_PARSER_MODULES) -> list[str]:
    """Call ``add_parsers(subparsers)`` of each module that is in this build.
    A module that is absent is skipped; one that is broken, or has no
    ``add_parsers``, raises. Returns the modules that added commands."""
    done: list[str] = []
    for modname in modules:
        module = _optional(modname)
        if module is None:
            continue
        module.add_parsers(subparsers)
        done.append(modname)
    return done


def _add_catalogs_parser(subparsers: Any) -> None:
    """``puffpai catalogs status|search|refresh`` (live info and connections)."""
    sub = subparsers.add_parser(
        "catalogs",
        help="Lists of free live-info websites and ways to connect things: "
             "status, search, refresh")
    sub.add_argument("catalogs_action", nargs="?", choices=["status", "search", "refresh"],
                     default="status",
                     help="'status' (what is saved), 'search <words>' (look, offline), "
                          "'refresh [list ...]' (go online and update the lists)")
    sub.add_argument("words", nargs="*", default=[],
                     help="For 'search': what the bot is for. For 'refresh': which lists "
                          "(api_seeds, apis_guru, public_apis, ha_integrations, mcp_registry)")
    sub.add_argument("--goal", default=None,
                     help="For 'search': the whole goal sentence, so things you own are noticed")
    sub.add_argument("--limit", type=int, default=5, help="How many results to show")
    sub.add_argument("--details", action="store_true",
                     help="For 'search': go online (after asking) to read whether the "
                          "directory results need a sign-up key")
    sub.add_argument("--out", default=None,
                     help="For 'refresh': save the lists in this folder instead of this "
                          "computer's own state folder")
    sub.add_argument("-y", "--yes", dest="yes", action="store_true",
                     help="For 'refresh': do not ask before going online")
    sub.set_defaults(func=_cmd_catalogs)


def _cmd_catalogs(args: argparse.Namespace) -> int:
    from puffpai.catalogs import live as catalogs_live
    if args.catalogs_action == "refresh":
        args.which = list(args.words or [])
    return catalogs_live.cli_main(args)


def _cmd_knowledge(args: argparse.Namespace) -> int:
    """List the offline books, or learn the new ones in the knowledge folder now."""
    from puffpai.knowledge import auto_index, find_more

    pack_index = auto_index.PackIndex()
    if args.knowledge_action == "list":
        records = pack_index.records()
        if not records:
            print("No books yet.")
            return 0
        for r in records:
            name = r.get("name") or r.get("filename", "?")
            print(f"  {name:<45} {r.get('status', '?')}")
        return 0

    folder = args.zim_dir or find_more.knowledge_folder()
    if not folder or not os.path.isdir(folder):
        print("No knowledge folder configured yet. Add a book first: "
              "puffpai sources add <id>")
        return 1
    results = auto_index.sync_new_packs(folder, pack_index=pack_index)
    if not results:
        print("Every book in the knowledge folder is already learned.")
        return 0
    for r in results:
        print(f"  {r.get('filename', '?')}: {r.get('status', '?')}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    # Marked home-network calls may not reach the internet (security.lan_guard).
    from puffpai.security import lan_guard
    lan_guard.install()

    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 0

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
