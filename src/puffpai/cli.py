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
import sys

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


def _cmd_version(args: argparse.Namespace) -> int:
    """Print PuffPAI version."""
    from puffpai import __version__
    print(f"PuffPAI v{__version__}")
    return 0


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


def _setup_arguments(sub: argparse.ArgumentParser) -> None:
    """Attach the setup wizard's flags, importing it lazily.

    Lazy so that building the parser for `puffpai version` does not drag in the
    census, the catalogs and the vault.
    """
    from puffpai import setup_cli
    setup_cli.add_arguments(sub)


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="puffpai",
        description="PuffPAI — Privacy-first, offline-capable AI assistant",
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

    # persona - an app and a pack of the owner's own, written from their words.
    sub = subparsers.add_parser(
        "persona",
        help="Scaffold a persona of your own (one app + one pack), or list them")
    sub.add_argument("persona_action", nargs="?", choices=["new", "list"],
                     default="list",
                     help="'new' (write one) or 'list' (the ones you have made)")
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
    sub.set_defaults(func=_cmd_persona)

    # apps
    sub = subparsers.add_parser(
        "apps", help="What apps and packs this build carries (and rescan)")
    sub.add_argument("apps_action", nargs="?", choices=["list", "rescan"],
                     default="list",
                     help="'list' (what is installed) or 'rescan' (re-read the "
                          "folders - this IS the install mechanism)")
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

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 0

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
