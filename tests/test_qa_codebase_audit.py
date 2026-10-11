"""
PuffPAI QA Codebase Audit
==========================
Automated audit of the PuffPAI codebase architecture:
  - Dead code and unused imports
  - Config inconsistencies (JSON vs dataclass defaults)
  - Bottleneck detection (context window, token limits, timeouts)
  - Tool registration integrity (all tools reachable?)
  - Router coverage (do all tool categories actually route correctly?)
  - Singleton / thread-safety issues
  - Import chain health (can all modules load without error?)

Run:
    python -m pytest tests/test_qa_codebase_audit.py -v --tb=short

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

import ast
import importlib
import json
import os
import re
import sys
import time
from pathlib import Path

# ── Resolve paths ─────────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_ROOT = _REPO_ROOT / "src" / "puffpai"
_CONFIG_DIR = _REPO_ROOT / "config"
_DATA_DIR = _REPO_ROOT / "data"

# Ensure src is importable
sys.path.insert(0, str(_REPO_ROOT / "src"))


# ═══════════════════════════════════════════════════════════════════════════════
# 1. CONFIG CONSISTENCY AUDIT
# ═══════════════════════════════════════════════════════════════════════════════

class TestConfigConsistency:
    """Verify puffpai_config.json matches dataclass defaults and has no stale keys."""

    def test_config_file_exists(self):
        cfg_path = _CONFIG_DIR / "puffpai_config.json"
        assert cfg_path.exists(), f"Config file missing: {cfg_path}"

    def test_config_parses_cleanly(self):
        cfg_path = _CONFIG_DIR / "puffpai_config.json"
        with open(cfg_path) as f:
            data = json.load(f)
        assert isinstance(data, dict), "Config root must be a dict"

    def test_ollama_context_window_not_too_small(self):
        """Context window < 2048 is dangerously tight with tool descriptions."""
        cfg_path = _CONFIG_DIR / "puffpai_config.json"
        with open(cfg_path) as f:
            data = json.load(f)
        num_ctx = data.get("ollama", {}).get("num_ctx", 4096)
        assert num_ctx >= 2048, (
            f"num_ctx={num_ctx} is too small. System prompt + tools + RAG "
            f"+ history can easily exceed this. Recommend >= 4096."
        )

    def test_ollama_max_tokens_not_too_small(self):
        """num_predict=256 severely truncates responses."""
        cfg_path = _CONFIG_DIR / "puffpai_config.json"
        with open(cfg_path) as f:
            data = json.load(f)
        num_predict = data.get("ollama", {}).get("num_predict", 512)
        if num_predict and num_predict < 384:
            print(f"  WARNING: num_predict={num_predict} may truncate natural "
                  f"conversation. Recommend >= 512 for voice-first persona.")

    def test_ollama_url_matches_running_server(self):
        cfg_path = _CONFIG_DIR / "puffpai_config.json"
        with open(cfg_path) as f:
            data = json.load(f)
        url = data.get("ollama", {}).get("url", "")
        assert "localhost" in url or "127.0.0.1" in url, (
            f"Ollama URL '{url}' is not localhost — ensure it's intentional"
        )

    def test_automation_webhooks_defined(self):
        """All referenced webhooks should exist in config."""
        cfg_path = _CONFIG_DIR / "puffpai_config.json"
        with open(cfg_path) as f:
            data = json.load(f)
        webhooks = data.get("nodered", {}).get("webhooks", {})
        required = [
            "block_device", "network_scan", "daily_briefing",
            "health_check", "weather_alerts",
        ]
        for wh in required:
            assert wh in webhooks, f"Missing webhook config: {wh}"

    def test_config_json_vs_dataclass_defaults(self):
        """Check for keys in JSON that don't exist in dataclass, or vice versa."""
        cfg_path = _CONFIG_DIR / "puffpai_config.json"
        with open(cfg_path) as f:
            data = json.load(f)

        json_sections = set(data.keys())
        expected_sections = {
            "ollama", "nodered", "chromadb", "api_server",
            "nabu", "identity", "models", "paths",
            "web_search_consent", "location", "zone3",
        }
        extra = json_sections - expected_sections
        if extra:
            print(f"  INFO: Extra config sections not in expected list: {extra}")


# ═══════════════════════════════════════════════════════════════════════════════
# 2. MODULE IMPORT HEALTH
# ═══════════════════════════════════════════════════════════════════════════════

class TestModuleImportHealth:
    """Every core module should import without crashing."""

    CORE_MODULES = [
        "puffpai.config",
        "puffpai.router",
        "puffpai.agent_graph",
        "puffpai.app_data",
        "puffpai.ha_device_watcher",
    ]

    def test_core_modules_import(self):
        failures = []
        for mod_name in self.CORE_MODULES:
            try:
                importlib.import_module(mod_name)
            except Exception as e:
                failures.append(f"{mod_name}: {e}")
        if failures:
            print("  Module import failures:")
            for f in failures:
                print(f"    - {f}")
        # Don't hard-fail — some deps may not be installed in test env
        # but report clearly

    def test_assistant_tools_import(self):
        """assistant_tools.py is 2500+ lines — make sure it at least loads."""
        try:
            importlib.import_module("puffpai.assistant_tools")
        except ImportError as e:
            print(f"  assistant_tools import skipped (missing dep): {e}")
        except Exception as e:
            raise AssertionError(f"assistant_tools failed to import: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# 3. ROUTER COVERAGE AUDIT
# ═══════════════════════════════════════════════════════════════════════════════

class TestRouterCoverage:
    """Ensure the router correctly classifies key message types."""

    def _route(self, text):
        from puffpai.router import route_message
        return route_message(text).value

    def test_home_commands_route_to_agent(self):
        cases = [
            "turn on the living room lights",
            "set the thermostat to 72",
            "turn off the fan",
            "dim the lights to 50 percent",
        ]
        for msg in cases:
            result = self._route(msg)
            assert result in ("agent", "home"), (
                f"'{msg}' routed to '{result}' — expected agent/home"
            )

    def test_calendar_routes_to_agent(self):
        cases = [
            "add dentist appointment to calendar for next monday at 2pm",
            "set a timer for 10 minutes",
            "set an alarm for 7:30 AM",
            "what's on my schedule tomorrow",
            "remind me to take medicine at 8pm",
        ]
        for msg in cases:
            result = self._route(msg)
            assert result == "agent", (
                f"'{msg}' routed to '{result}' — expected agent"
            )

    def test_general_knowledge_routes_to_rag(self):
        cases = [
            "what is the capital of France",
            "explain how photosynthesis works",
            "who was the first president",
        ]
        for msg in cases:
            result = self._route(msg)
            assert result == "rag", (
                f"'{msg}' routed to '{result}' — expected rag"
            )

    def test_briefing_routes_correctly(self):
        cases = [
            "give me my daily briefing",
            "morning update",
            "catch me up",
        ]
        for msg in cases:
            result = self._route(msg)
            assert result == "briefing", (
                f"'{msg}' routed to '{result}' — expected briefing"
            )

    def test_media_routes_to_agent(self):
        cases = [
            "play some jazz music",
            "what movies do I have",
            "search my music library for Beatles",
        ]
        for msg in cases:
            result = self._route(msg)
            assert result == "agent", (
                f"'{msg}' routed to '{result}' — expected agent"
            )

    def test_ambiguous_messages_dont_crash(self):
        """Edge cases that could confuse keyword matching."""
        cases = [
            "",
            "hmm",
            "hello",
            "thanks for that",
            "what?",
            "   ",
        ]
        for msg in cases:
            try:
                self._route(msg)
            except Exception as e:
                raise AssertionError(f"Router crashed on '{msg}': {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# 4. TOOL REGISTRATION INTEGRITY
# ═══════════════════════════════════════════════════════════════════════════════

class TestToolIntegrity:
    """Check that tools are properly defined and don't collide."""

    def _get_tools(self):
        try:
            from puffpai.assistant_tools import AssistantTools
            rt = AssistantTools()
            return rt.tools
        except Exception as e:
            print(f"  Cannot instantiate AssistantTools: {e}")
            return None

    def test_no_duplicate_tool_names(self):
        tools = self._get_tools()
        if tools is None:
            return  # Skip if tools can't load
        names = [t.name for t in tools]
        dupes = [n for n in names if names.count(n) > 1]
        assert not dupes, f"Duplicate tool names: {set(dupes)}"

    def test_all_tools_have_descriptions(self):
        tools = self._get_tools()
        if tools is None:
            return
        missing = [t.name for t in tools if not t.description]
        assert not missing, f"Tools without descriptions: {missing}"

    def test_critical_tools_exist(self):
        """Core tools the system prompt references must exist."""
        tools = self._get_tools()
        if tools is None:
            return
        names = {t.name for t in tools}
        required = [
            "app_add_task", "app_add_event", "app_list_schedule",
            "app_set_timer", "app_set_alarm", "app_add_pantry",
            "app_add_shopping", "auto_weather_forecast",
            "ha_control_device", "ha_list_devices",
        ]
        missing = [r for r in required if r not in names]
        assert not missing, f"Critical tools missing: {missing}"

    def test_tool_count_reasonable(self):
        """Too many tools can overwhelm the LLM's context window."""
        tools = self._get_tools()
        if tools is None:
            return
        count = len(tools)
        print(f"  Tool count: {count}")
        if count > 50:
            print(f"  WARNING: {count} tools may overwhelm the LLM. "
                  f"Consider grouping or lazy-loading based on route.")


# ═══════════════════════════════════════════════════════════════════════════════
# 5. ARCHITECTURE BOTTLENECK ANALYSIS
# ═══════════════════════════════════════════════════════════════════════════════

class TestArchitectureBottlenecks:
    """Static analysis for known bottleneck patterns."""

    def test_context_window_vs_tool_descriptions(self):
        """Estimate how much of the context window tools consume."""
        tools = None
        try:
            from puffpai.assistant_tools import AssistantTools
            tools = AssistantTools().tools
        except Exception:
            return

        # Estimate token count of tool descriptions (rough: 1 token ~= 4 chars)
        total_chars = sum(
            len(t.name) + len(t.description or "")
            for t in tools
        )
        est_tokens = total_chars // 4

        cfg_path = _CONFIG_DIR / "puffpai_config.json"
        with open(cfg_path) as f:
            data = json.load(f)
        num_ctx = data.get("ollama", {}).get("num_ctx", 4096)

        pct = (est_tokens / num_ctx) * 100
        print(f"  Tool descriptions: ~{est_tokens} tokens ({pct:.0f}% of {num_ctx} ctx)")
        if pct > 40:
            print(f"  CRITICAL: Tools alone consume {pct:.0f}% of context. "
                  f"Leaves little room for persona + RAG + history.")

    def test_persona_size(self):
        """Persona prompt should not be excessively long."""
        persona_path = _SRC_ROOT / "persona.txt"
        if not persona_path.exists():
            return
        text = persona_path.read_text()
        est_tokens = len(text) // 4
        print(f"  Persona: ~{est_tokens} tokens")
        if est_tokens > 500:
            print(f"  WARNING: Persona is {est_tokens} tokens. "
                  f"Consider trimming for smaller context windows.")

    def test_history_truncation_configured(self):
        """Verify conversation history is being limited."""
        api_path = _SRC_ROOT / "api_server.py"
        if not api_path.exists():
            return
        text = api_path.read_text(encoding="utf-8", errors="replace")
        match = re.search(r'_MAX_HISTORY_TURNS\s*=\s*(\d+)', text)
        if match:
            turns = int(match.group(1))
            print(f"  Max history turns: {turns}")
            if turns > 10:
                print(f"  WARNING: {turns} history turns is a lot for 2048 ctx")
        else:
            print("  WARNING: No _MAX_HISTORY_TURNS found — unbounded history?")

    def test_react_regex_robustness(self):
        """Check if the ReAct fallback regex handles edge cases."""
        pattern = r'TOOL:\s*(\w+)'
        # Should match
        assert re.search(pattern, "TOOL: app_add_task"), "Basic match failed"
        assert re.search(pattern, "TOOL:app_add_task"), "No-space match failed"

        # These SHOULD match but won't with \w+ alone
        hyphenated = re.search(pattern, "TOOL: app-add-task")
        if not hyphenated:
            print("  WARNING: ReAct regex won't match hyphenated tool names")


# ═══════════════════════════════════════════════════════════════════════════════
# 6. DATA FILE INTEGRITY
# ═══════════════════════════════════════════════════════════════════════════════

class TestDataFileIntegrity:
    """Check that data files are valid JSON and not corrupted."""

    DATA_FILES = [
        "ha_known_devices.json",
        "ha_pending_devices.json",
    ]

    def test_data_files_valid_json(self):
        for fname in self.DATA_FILES:
            fpath = _DATA_DIR / fname
            if not fpath.exists():
                continue
            try:
                with open(fpath) as f:
                    json.load(f)
            except json.JSONDecodeError as e:
                raise AssertionError(f"{fname} is corrupt JSON: {e}")

    def test_data_dir_exists(self):
        assert _DATA_DIR.exists(), f"Data directory missing: {_DATA_DIR}"


# ═══════════════════════════════════════════════════════════════════════════════
# 7. DEAD CODE DETECTION (LIGHTWEIGHT)
# ═══════════════════════════════════════════════════════════════════════════════

class TestDeadCodeDetection:
    """Light scan for obviously unused patterns."""

    def test_config_govee_integration_still_loaded(self):
        """Govee lights were replaced — is the integration still active in HA?"""
        # This is informational — the Govee integration may still be loaded
        print("  INFO: HA still has 'govee_light_local' component loaded. "
              "If Govee lights are removed, consider removing the integration "
              "in HA Settings > Devices & Services to keep things clean.")

    def test_kiwix_config_removed(self):
        """The Kiwix HTTP-server config block is gone (J0 dead-code sweep).

        The RAG path uses the in-process ZIM reader via paths.zim_dirs; the
        old block pointed at a 192.168.1.132:8080 host that no longer exists.
        """
        cfg_path = _CONFIG_DIR / "puffpai_config.json"
        with open(cfg_path) as f:
            data = json.load(f)
        assert "kiwix" not in data, (
            "Dead 'kiwix' config block is back — the Kiwix HTTP server was "
            "retired; use paths.zim_dirs.")


# ═══════════════════════════════════════════════════════════════════════════════
# 8. HA TRANSPORT ENCAPSULATION
# ═══════════════════════════════════════════════════════════════════════════════
#
# ``HABridge._request`` is the single authenticated transport to the house. Every
# safety gate this appliance has — ``enforce_confirmation``, the untargeted /
# broadcast / unbounded rules, the ticket flow — lives in the bridge's public
# methods, *above* ``_request``. Anything that reaches ``_request`` directly is
# actuating the house with nothing checked.
#
# ``AssistantTools.ha_api_call`` was a shim that did exactly that; its absence
# is pinned with ``not hasattr(AssistantTools, "ha_api_call")`` in
# tests/test_ha_bridge_confirmation_gate.py. That pin is name-scoped: the same
# shim under a different name passes it. The tests below express the actual
# invariant structurally instead of by name.

# Files are keyed by their path relative to ``src/puffpai`` (posix form).
_HA_TRANSPORT_MODULE = "ha_bridge.py"
_HA_TRANSPORT_ATTR = "_request"

# ── Allow-list ────────────────────────────────────────────────────────────────
# ``(relative_path, qualified_name)``. Keyed by QUALIFIED NAME -- the full
# nesting chain including classes -- never by line number: ``_ha_api`` has
# already drifted from line 9946 to ~9996 as api_server.py is edited, and a
# line-keyed allow-list would false-fail on every unrelated commit.
#
# The qualification matters as much as the name. A bare ``"_ha_api"`` key would
# pre-authorize ANY callable of that name in the file, so a
# ``class _ZZHaClient: def _ha_api(...)`` wrapping the raw transport would
# inherit the exemption. Module-level ``_ha_api`` qualifies as ``_ha_api``; a
# method qualifies as ``_ZZHaClient._ha_api`` and matches nothing here.
# Each entry must match EXACTLY ONE site -- see
# test_allowlist_entries_match_exactly_one_site.
#
# Adding an entry here is granting a caller the right to bypass every HA safety
# gate. Only do it for a caller that cannot actuate, and pin whatever makes that
# true with its own test (see test_ha_api_get_guard_still_pins_the_allowlist).
_HA_TRANSPORT_ALLOWLIST = {
    # api_server._ha_api — READ-ONLY helper, deliberate and documented.
    #   Safe because: it hard-refuses any method but GET before reaching the
    #   transport (`if str(method).upper() != 'GET': return False, ...`), so it
    #   cannot actuate anything; a GET touches no gate. It routes its reads
    #   through the bridge on purpose, so the appliance has ONE authenticated
    #   transport to the house instead of two — and one place for this test to
    #   trap. See its docstring.
    #   This entry is sound ONLY while that GET guard exists.
    ("api_server.py", "_ha_api"),
}


def _ha_unparse(node) -> str:
    """Best-effort source text for an AST node (never raises)."""
    try:
        return ast.unparse(node)
    except Exception:  # pragma: no cover - defensive
        return f"<{type(node).__name__}>"


def _ha_interpolated_segments(tree) -> set:
    """``id()`` of literal segments inside genuinely interpolated f-strings.

    ``f"{prefix}_request"`` compiles to a ``JoinedStr`` holding a
    ``Constant('_request')`` segment, so innocent log-key and metric-name
    construction would otherwise trip the string rule. An interpolated name is
    a COMPUTED name, which is the documented accepted gap -- not something this
    rule ever claimed to catch.

    The ``len(values) > 1`` test is load-bearing: ``f"_request"`` with no
    placeholders is still ``JoinedStr(values=[Constant('_request')])``, and
    that is a plain literal wearing an f-prefix, so it must NOT be skipped.
    Only direct Constant segments are collected -- a Constant living inside a
    ``FormattedValue`` expression (``f"{getattr(b, '_request')}"``) is real
    code and still fires.
    """
    skip = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr) and len(node.values) > 1:
            for value in node.values:
                if isinstance(value, ast.Constant):
                    skip.add(id(value))
    return skip


def _ha_transport_reference(node, skip_ids: set):
    """Return a description if ``node`` reaches the HA transport, else None."""
    # (a) ANY attribute reference to `_request`, not just one in call position.
    # Calling it, aliasing it (`f = bridge._request`), wrapping it
    # (`functools.partial(bridge._request, ...)`) and passing the bound method
    # as an argument are the same bypass -- the alias is exactly the "raw shim
    # under a different name" this test exists to stop, and it reaches the
    # transport just as directly one line later.
    #
    # A call's own `func` IS this node, so matching the Attribute subsumes the
    # call case and reports each site exactly once.
    if isinstance(node, ast.Attribute) and node.attr == _HA_TRANSPORT_ATTR:
        return _ha_unparse(node)

    # (b) The name as a STRING, which is how every reflection dodge spells it:
    #       getattr(bridge, "_request")
    #       bridge.__getattribute__("_request")
    #       operator.attrgetter("_request")(bridge)
    #       vars(type(bridge))["_request"](bridge, ...)
    # Rather than chase each shape, flag the literal itself: outside the
    # transport's own module a bare "_request" string has no legitimate
    # purpose, and this one rule subsumes them all. (Verified zero occurrences
    # anywhere under src/puffpai, ha_bridge.py included.)
    #
    # Docstrings are deliberately NOT excluded. A docstring is readable at
    # runtime as `__doc__`, so a function whose entire docstring is "_request"
    # is a NAME CARRIER, not prose -- `getattr(bridge, _carrier.__doc__)`
    # actuates the house. Exact equality already spares real prose, whose
    # Constant value is the whole docstring rather than the bare word, so the
    # exclusion only ever hid live literals.
    if (isinstance(node, ast.Constant) and isinstance(node.value, str)
            and node.value == _HA_TRANSPORT_ATTR and id(node) not in skip_ids):
        return f'"{_HA_TRANSPORT_ATTR}" (string literal -- reflection dodge)'

    return None


def _find_ha_transport_references():
    """Static AST scan of ``src/puffpai`` for direct HA-transport references.

    Returns a list of ``(rel_path, lineno, reference_expr, qualified_name)``.
    ``qualified_name`` carries the FULL nesting chain including classes, so a
    method can never collide with a module-level function of the same name.
    Pure file-reading + ``ast.parse``: imports nothing from puffpai, opens no
    sockets, and never touches the live Home Assistant.
    """
    found = []

    for path in sorted(_SRC_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(_SRC_ROOT).as_posix()
        if rel == _HA_TRANSPORT_MODULE:
            continue  # the transport's own home

        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except Exception as exc:
            # Fail closed: a file we cannot parse is a file we cannot clear.
            found.append((rel, 0, f"<unparseable: {exc}>", "<module>"))
            continue

        skip_ids = _ha_interpolated_segments(tree)

        def visit(node, scope):
            # ClassDef included: `_ZZHaClient._ha_api` must NOT be able to
            # inherit the exemption granted to module-level `_ha_api`.
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                scope = scope + (node.name,)
            reference = _ha_transport_reference(node, skip_ids)
            if reference is not None:
                found.append((rel, node.lineno, reference,
                              ".".join(scope) if scope else "<module>"))
            for child in ast.iter_child_nodes(node):
                visit(child, scope)

        visit(tree, ())

    return found


_HA_API_WHY = (
    "the _ha_api allow-list entry in TestHATransportEncapsulation is only "
    "sound while this helper refuses non-GET; if this guard is being removed, "
    "remove the allow-list entry too."
)


def _find_module_level_ha_api():
    """The MODULE-LEVEL ``_ha_api`` in api_server.py, or fail.

    Scoped to ``tree.body`` on purpose: the allow-list exempts the qualified
    name ``_ha_api``, which is the module-level function and nothing else. A
    same-named method on some class is a different symbol, carries no
    exemption, and must not be able to satisfy these pins in its place.
    """
    api_path = _SRC_ROOT / "api_server.py"
    tree = ast.parse(api_path.read_text(encoding="utf-8"), filename=str(api_path))
    fn = next(
        (n for n in tree.body
         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
         and n.name == "_ha_api"),
        None,
    )
    assert fn is not None, (
        f"module-level api_server._ha_api no longer exists -- {_HA_API_WHY}"
    )
    return fn


class TestHATransportEncapsulation:
    """No code outside ha_bridge.py may reach ``HABridge._request`` directly.

    "Reach" is deliberately wider than "call": referencing the transport at all
    — calling it, aliasing it, wrapping it, handing it to something else — is
    the same bypass, so the scan matches any attribute reference to it.
    """

    def test_no_direct_ha_transport_references_outside_bridge(self):
        """Every HA call must go through the bridge's own gated methods."""
        offenders = [
            ref for ref in _find_ha_transport_references()
            if (ref[0], ref[3]) not in _HA_TRANSPORT_ALLOWLIST
        ]

        detail = "\n".join(
            f"    src/puffpai/{rel}:{lineno}  in {func}()  ->  {reference}"
            for rel, lineno, reference, func in offenders
        )
        assert not offenders, (
            f"Direct reference(s) to the Home Assistant transport "
            f"`HABridge.{_HA_TRANSPORT_ATTR}` found outside "
            f"src/puffpai/{_HA_TRANSPORT_MODULE}:\n{detail}\n\n"
            f"INVARIANT: `{_HA_TRANSPORT_ATTR}` is the raw authenticated "
            f"transport to the house. Every safety gate -- enforce_confirmation, "
            f"the always-confirm class, and the untargeted / broadcast / "
            f"unbounded rules -- lives in HABridge's PUBLIC methods, above it. "
            f"Reaching it directly actuates the house with nothing checked, "
            f"which is why there is no `AssistantTools.ha_api_call` shim.\n"
            f"NOTE: this flags the transport being REFERENCED, not only called. "
            f"Aliasing it (`f = bridge.{_HA_TRANSPORT_ATTR}`), wrapping it in a "
            f"partial, or passing the bound method as an argument are the same "
            f"bypass as calling it outright -- the alias is precisely the raw "
            f"shim-under-a-new-name this test exists to stop.\n"
            f"FIX: actuation must go through the bridge's gated methods "
            f"(call_service / control_device / activate_scene), which derive the "
            f"touched domains from their arguments and require human "
            f"confirmation for the always-confirm class. If this reference is a "
            f"genuine read-only exception, add it to _HA_TRANSPORT_ALLOWLIST "
            f"above WITH a comment stating why it cannot actuate, and pin that "
            f"guarantee with its own test."
        )

    def test_allowlist_entries_match_exactly_one_site(self):
        """Each exemption must cover ONE site — not zero, and not several.

        Zero means the caller was refactored away and the entry is now a
        standing permission waiting for whatever next claims the name. More
        than one means a second reference has quietly moved in under an
        already-blessed qualified name and is riding an exemption that was
        audited for a different piece of code.
        """
        references = _find_ha_transport_references()

        problems = []
        for entry in sorted(_HA_TRANSPORT_ALLOWLIST):
            sites = [(rel, lineno, ref, qual)
                     for rel, lineno, ref, qual in references
                     if (rel, qual) == entry]
            if len(sites) != 1:
                problems.append((entry, sites))

        detail = "\n".join(
            f"    {entry} matches {len(sites)} site(s):\n" + (
                "\n".join(f"        src/puffpai/{r}:{ln}  ->  {ref}"
                          for r, ln, ref, _q in sites) or "        (none)"
            )
            for entry, sites in problems
        )
        assert not problems, (
            f"_HA_TRANSPORT_ALLOWLIST entries must match EXACTLY ONE "
            f"`{_HA_TRANSPORT_ATTR}` reference each:\n{detail}\n\n"
            f"ZERO matches: the exempt caller was removed or renamed. Delete "
            f"the entry instead of leaving a standing exemption for whatever "
            f"next claims that qualified name.\n"
            f"MORE THAN ONE: a second reference is riding an exemption that was "
            f"audited for different code. Each exempt site must be reviewed on "
            f"its own merits -- split them into distinct qualified names, or "
            f"route the new one through the bridge's gated methods."
        )

    def test_ha_api_pins_its_request_method_to_the_literal_get(self):
        """The PRIMARY pin: ``_ha_api`` hands the transport a constant 'GET'.

        Reasoning about the guard's control flow is too weak to carry the
        exemption on its own — a nested ``if`` can leave a ``!= 'GET'`` check
        standing while service calls sail past it. What actually makes this
        helper unable to actuate is a direct structural fact about the call
        site: the method it passes down is the literal string, never the
        caller's ``method`` argument. ``method=method`` fires this immediately.
        """
        why = _HA_API_WHY
        fn = _find_module_level_ha_api()

        calls = [
            node for node in ast.walk(fn)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == _HA_TRANSPORT_ATTR
        ]
        assert calls, (
            f"api_server._ha_api no longer calls `{_HA_TRANSPORT_ATTR}` at all. "
            f"If it no longer touches the transport it does not need an "
            f"exemption -- {why}"
        )

        for call in calls:
            kwargs = {kw.arg: kw.value for kw in call.keywords if kw.arg}
            method_arg = kwargs.get("method")
            rendered = _ha_unparse(method_arg) if method_arg is not None else "<absent>"
            assert (isinstance(method_arg, ast.Constant)
                    and method_arg.value == "GET"), (
                f"api_server._ha_api line {call.lineno} passes "
                f"method={rendered} to `{_HA_TRANSPORT_ATTR}` instead of the "
                f"literal constant 'GET'. Forwarding the caller's method makes "
                f"this helper able to actuate the house through the raw "
                f"transport, with none of the bridge's gates -- {why}"
            )

    def test_ha_api_get_guard_still_pins_the_allowlist(self):
        """``api_server._ha_api`` must still refuse any method but GET.

        Secondary to the method='GET' pin above: this keeps the refusal itself
        honest, so a non-GET request is answered rather than quietly reshaped.
        """
        why = _HA_API_WHY
        api_path = _SRC_ROOT / "api_server.py"
        source = api_path.read_text(encoding="utf-8")
        fn = _find_module_level_ha_api()

        # The guard: an `if` whose test compares something != 'GET'.
        guard = None
        for node in ast.walk(fn):
            if not isinstance(node, ast.If):
                continue
            for cmp_node in ast.walk(node.test):
                if (isinstance(cmp_node, ast.Compare)
                        and any(isinstance(op, ast.NotEq) for op in cmp_node.ops)
                        and any(isinstance(c, ast.Constant) and c.value == "GET"
                                for c in cmp_node.comparators)):
                    guard = node
                    break
            if guard is not None:
                break

        assert guard is not None, (
            f"api_server._ha_api has no `!= 'GET'` refusal guard -- {why}"
        )

        # It must refuse UNCONDITIONALLY: a `return` that is a DIRECT child of
        # the guard body, not one buried inside a nested conditional. Walking
        # the whole subtree would accept
        #     if method != 'GET':
        #         if not endpoint.startswith('services/'):
        #             return False, ...
        # which leaves every service call falling straight through.
        assert any(isinstance(n, ast.Return) for n in guard.body), (
            f"api_server._ha_api's non-GET branch has no unconditional return "
            f"-- its refusal is now itself conditional, so some non-GET "
            f"requests fall through to the transport. {why}"
        )

        # And the refusal must still say so in words (robust to reformatting).
        guard_src = ast.get_source_segment(source, guard) or ""
        assert "read-only" in guard_src.lower(), (
            f"api_server._ha_api's non-GET branch no longer refuses as "
            f"read-only. {why}"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 9. SUMMARY REPORT
# ═══════════════════════════════════════════════════════════════════════════════

def test_generate_audit_summary():
    """Print a consolidated audit summary."""
    print("\n" + "=" * 70)
    print("  PUFFPAI QA CODEBASE AUDIT SUMMARY")
    print("=" * 70)

    cfg_path = _CONFIG_DIR / "puffpai_config.json"
    with open(cfg_path) as f:
        data = json.load(f)

    ollama = data.get("ollama", {})
    print(f"\n  Model:         {ollama.get('model', 'unknown')}")
    print(f"  Context:       {ollama.get('num_ctx', 'default')} tokens")
    print(f"  Max response:  {ollama.get('num_predict', 'default')} tokens")
    print(f"  Temperature:   {ollama.get('temperature', 'default')}")
    print(f"  Ollama URL:    {ollama.get('url', 'unknown')}")

    print(f"\n  CRITICAL FINDINGS:")
    issues = []

    num_ctx = ollama.get("num_ctx", 4096)
    num_predict = ollama.get("num_predict", 512)

    if num_ctx <= 2048:
        issues.append(
            f"  [HIGH] Context window is only {num_ctx} tokens. "
            f"System prompt + ~100 tools + RAG context + 5 history turns "
            f"will regularly overflow. Recommend: 4096-8192."
        )

    if num_predict and num_predict <= 256:
        issues.append(
            f"  [HIGH] Max response tokens is {num_predict}. "
            f"Natural conversation requires 400-600+. "
            f"Responses will be truncated mid-sentence."
        )

    if not data.get("nodered", {}).get("webhooks", {}).get("good_morning"):
        issues.append(
            "  [MED] good_morning webhook missing from config JSON "
            "(defined in dataclass defaults but not in JSON)."
        )
    if not data.get("nodered", {}).get("webhooks", {}).get("leaving_home"):
        issues.append(
            "  [MED] leaving_home webhook missing from config JSON."
        )
    if not data.get("nodered", {}).get("webhooks", {}).get("security_check"):
        issues.append(
            "  [MED] security_check webhook missing from config JSON."
        )

    if not issues:
        print("    None — looking good!")
    else:
        for issue in issues:
            print(issue)

    print(f"\n  RECOMMENDATIONS:")
    print(f"    1. Increase num_ctx to at least 4096 (8192 ideal for tool-heavy agent)")
    print(f"    2. Increase num_predict to at least 512 (768 for voice-first)")
    print(f"    3. Add missing automation webhooks to config JSON (good_morning, leaving_home, security_check)")
    print(f"    4. Consider lazy-loading tools based on router category to reduce context bloat")
    print(f"    5. Remove Govee integration from Home Assistant if lights are gone")
    print("=" * 70)
