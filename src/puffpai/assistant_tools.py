from langchain_core.tools import tool
import os
import json
import sys
from datetime import datetime, timedelta

# Spoken word-numbers → digits, for matching spoken labels against
# stored ones ("seven minute timer" vs "7 minutes timer").
_WORD_NUM_MAP = {
    'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6,
    'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10, 'fifteen': 15,
    'twenty': 20, 'thirty': 30, 'forty-five': 45, 'forty five': 45,
    'sixty': 60, 'ninety': 90,
}
import re
import requests
import logging
from puffpai.inference import model_options as _model_options
from puffpai.emergency_prep import EmergencyPrep
from puffpai.offline_guard import requires_internet, requires_local_service, get_network_status
from puffpai.app_input_parser import (
    parse_natural_date as _parse_natural_date,
    parse_natural_time as _parse_natural_time,
    parse_duration as _parse_duration,
    parse_flexible_input as _parse_flexible_input,
)

_logger = logging.getLogger(__name__)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# -- Vault tools: the turn they run in can never be persisted --------
#
# ``vault_unlock_and_get`` takes 'item label | PIN' and returns the DECRYPTED
# value; ``vault_display_document`` turns a sensitive-document request into a
# PIN challenge. Either way the turn holds a secret -- the PIN in the user
# half, the value in the assistant half -- so the api_server has to be told
# BEFORE the tool does its work (a wrong PIN is still a PIN), and regardless of
# which loop invoked it: the LangGraph tool node, the ReAct fallback, the
# legacy loop in this file, the Toolsmith bridge, or a direct ``.invoke()``.
#
# Membership is an explicit SET, never a match on the tool's output: a tool is
# in here because of what it CAN return, and a string test would both miss a
# reworded reply and seal ordinary turns that happen to say "password".
#
# The reach into api_server is the lazy ``sys.modules`` lookup this file
# already uses for _get_vault / _get_rag_context: importing it outright would
# be a cycle, and a tool module that only worked inside the server would break
# every CLI and test that builds AssistantTools on its own.
VAULT_PRIVATE_TOOLS = frozenset({
    "vault_unlock_and_get",
    "vault_display_document",
})

#: What a loop says instead of running a vault tool it cannot protect.
VAULT_PRIVACY_REFUSAL = (
    "I can't do that right now: the guard that keeps a vault secret out of "
    "the transcript isn't answering, so nothing was unlocked.")


def mark_vault_tool_turn_private(tool_name: str = "") -> bool:
    """Tell the running api_server that this turn holds a vault secret.

    True means the turn is protected -- or that there is no api_server in this
    process to protect it, which is a CLI/test run with no transcript, no live
    panel and no save-back. False means a server IS running and the mark did
    not take: the one case a vault tool must treat as "do not answer".
    """
    api = sys.modules.get('puffpai.api_server')
    if api is None:
        # Started as ``python src/puffpai/api_server.py``: the very same module
        # is registered as __main__ and not under its package name, so the
        # lookup above misses the server that IS running.
        main = sys.modules.get('__main__')
        if main is not None and hasattr(main, 'note_vault_secret_tool'):
            api = main
    if api is None:
        return True
    hook = getattr(api, 'note_vault_secret_tool', None)
    if hook is None:
        _logger.error("api_server exposes no note_vault_secret_tool hook; "
                      "refusing %s", tool_name or "vault tool")
        return False
    try:
        return bool(hook(tool_name))
    except Exception as exc:
        _logger.error("vault turn-privacy hook failed for %s: %s",
                      tool_name or "vault tool", exc)
        return False


def note_vault_tool_calls(names) -> bool:
    """Boundary hook: mark the turn if any of ``names`` is a vault tool.

    An agent loop calls this BEFORE it invokes, and this is the LOAD-BEARING
    layer, not a belt to the tool body's braces: langgraph runs tool bodies on
    a pool thread, so the boundary is the last code that runs on the request's
    own thread with the call still in front of it. It also covers calls that
    never reach a tool body at all -- an argument-schema error whose message
    echoes the arguments, a duplicate the loop answers from the stored prior
    result.

    Returns True when the turn is protected or there was nothing to protect.
    FALSE MEANS DO NOT INVOKE: a vault tool is about to run and its turn could
    not be made private.
    """
    try:
        for name in (names or ()):
            if name in VAULT_PRIVATE_TOOLS:
                return bool(mark_vault_tool_turn_private(str(name)))
    except Exception as exc:
        _logger.error("vault tool boundary mark failed: %s", exc)
        return False
    return True


_DECISION_TOOL_RE = re.compile(r"TOOL:\s*(\w+)")


def _redact_tool_decision(decision: str) -> str:
    """A model decision line, safe to print.

    ``INPUT: wifi password | 4821`` is the PIN, and the legacy loop below
    printed it straight to the journal.

    INPUT is replaced UNCONDITIONALLY, not only when the named tool is one
    of VAULT_PRIVATE_TOOLS: the MODEL writes that name, and "TOOL: web_search / INPUT: wifi password | 4821" is a
    decision it can emit at any time, on the way to a tool that never touches
    the vault. The argument is the model's copy of the utterance either way.

    The tool NAME stays, because knowing which tool ran is the whole point of
    the line.
    """
    try:
        text = decision or ""
        return re.sub(r"(INPUT:\s*)(.*)", r"\1[protected]", text)
    except Exception:
        return "[redacted]"


# ── writable state for the device-discovery memo ────────────────────
#
# ``ha_discover_new_devices`` remembers which entities it has already reported.
# The memo cannot live in <repo>/ha_seen_devices.json: on the appliance that is
# /opt/puffpai/ha_seen_devices.json, inside the code tree the systemd unit
# mounts read-only, where a write raises "[Errno 30] Read-only file system"
# and takes the whole turn down with it. It resolves through the same canon
# every other state writer uses (puffpai.statedir / $PUFFPAI_DATA_DIR).

SEEN_DEVICES_FILENAME = "ha_seen_devices.json"

#: The legacy in-tree location. Read once, in place, when nothing has been
#: written to the new path yet, so an upgraded box does not report every device
#: in the house as new. Never written to.
_LEGACY_SEEN_DEVICES_PATH = os.path.join(_REPO_ROOT, SEEN_DEVICES_FILENAME)

_seen_devices_dir_cache = None


def _seen_devices_dir():
    """State directory, resolved on first use and cached.

    Lazy for the reason :mod:`puffpai.statedir` documents: resolving imports
    :mod:`puffpai.config`, and doing that at module scope drags the secrets
    provider into this module's import graph.
    """
    global _seen_devices_dir_cache
    if _seen_devices_dir_cache is None:
        from puffpai.statedir import resolve_runtime_dir
        _seen_devices_dir_cache = resolve_runtime_dir(
            os.path.join(_REPO_ROOT, "data"))
    return _seen_devices_dir_cache


def ha_seen_devices_path():
    """Absolute path of the memo. ``SEEN_DEVICES_PATH`` overrides (tests/ops)."""
    override = globals().get("SEEN_DEVICES_PATH")
    if isinstance(override, str) and override:
        return override
    return os.path.join(_seen_devices_dir(), SEEN_DEVICES_FILENAME)


def reset_seen_devices_cache():
    """Forget the resolved state dir. For tests that move ``$PUFFPAI_DATA_DIR``."""
    global _seen_devices_dir_cache
    _seen_devices_dir_cache = None


def _needs_confirmation_reply(exc) -> str:
    """Render a refused always-confirm actuation for the LLM tool surface.

    These tools run on the *legacy* PuffPAIAgent, which chat still falls back to
    whenever the product team does not handle a turn. Its arguments are a string
    the model wrote, so anything reachable from here is reachable from text in a
    document, a web page or a calendar invite the model happened to read. The
    bridge refuses those actuations; this only turns the refusal into something
    the model can act on sensibly.

    The wording deliberately does **not** describe how a confirmation is
    presented to the bridge. There is nothing the model could put in a tool
    argument that would satisfy the gate — the token is an in-process object, not
    a value — and naming the mechanism would only invite it to try, filling the
    logs with spoof warnings. The one correct next step is to ask the person.
    """
    proposal = getattr(exc, "proposal", None) or {}
    target = proposal.get("entity_id") or "that device"
    domains = ", ".join(proposal.get("domains") or []) or "safety-critical"
    return (f"Not done — {target} is in the always-confirm class ({domains}), "
            "which never actuates on an assistant's say-so. Tell the person what "
            "you were about to do and ask them to confirm it themselves, at the "
            "dashboard or out loud. Do not retry this tool.")


# Spoken-math → arithmetic rewrites for the calculate tool. The model often
# passes the user's own words ("847 times 62") straight through, especially on
# the ReAct fallback path where INPUT: is free text.
_MATH_WORD_OPS = [
    (re.compile(r"\bmultiplied\s+by\b"), "*"),
    (re.compile(r"\btimes\b"), "*"),
    (re.compile(r"\bdivided\s+by\b"), "/"),
    (re.compile(r"\bover\b"), "/"),
    (re.compile(r"\bplus\b"), "+"),
    (re.compile(r"\bminus\b"), "-"),
    (re.compile(r"\bmodulo\b"), "%"),
    (re.compile(r"\bmod\b"), "%"),
    (re.compile(r"\bto\s+the\s+power\s+of\b"), "**"),
    (re.compile(r"\bsquared\b"), "**2"),
    (re.compile(r"\bcubed\b"), "**3"),
    (re.compile(r"\^"), "**"),
    # 847 x 62 / (2+3)x(4+5) — both sides must be space/paren so hex
    # literals like 0x10 pass through to ast.parse untouched.
    (re.compile(r"(?<=[\s)])x(?=[\s(])"), "*"),
    (re.compile(r"[×⋅]"), "*"),
    (re.compile(r"÷"), "/"),
]

_MATH_LEADING_WORDS = re.compile(
    r"^(?:what\s+is|what's|whats|calculate|compute|evaluate|solve|"
    r"how\s+much\s+is)\b[:\s]*")

_MATH_THOUSANDS_COMMA = re.compile(r"(\d),(?=\d{3}(?:\D|$))")

_MATH_PERCENT_OF = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|percent)\s+of\s+")


def normalize_math_expression(expression: str) -> str:
    """Turn spoken math into an expression ast.parse can read.

    'What's 847 times 62?' → '847 * 62'; '15% of 80' → '(15/100)* 80'.
    Already-symbolic input passes through unchanged apart from lowercasing.
    """
    s = (expression or "").strip().strip("'\"").lower()
    s = _MATH_LEADING_WORDS.sub("", s)
    s = s.rstrip(" ?.!=")
    s = _MATH_THOUSANDS_COMMA.sub(r"\1", s)
    s = _MATH_PERCENT_OF.sub(r"(\1/100)*", s)
    for pattern, replacement in _MATH_WORD_OPS:
        s = pattern.sub(replacement, s)
    return s.strip()


# Descriptions for indexed knowledge libraries
LIBRARY_DESCRIPTIONS = {
    "RationalWiki": "Critical encyclopedia covering pseudoscience, authoritarianism, and logical fallacies",
    "Best of Wikipedia": "Curated collection of Wikipedia's highest-quality featured articles",
    "Wikispecies": "Comprehensive taxonomy directory of all living species",
    "Ready.gov": "US government emergency preparedness guides and disaster planning",
    "Explain XKCD": "Detailed explanations of XKCD webcomics covering science, math, and tech",
    "TED surveillance": "TED Talks exploring surveillance, privacy, and government monitoring",
    "Wikipedia": "Full offline English Wikipedia encyclopedia",
    "Amateur Radio Q&A": "Stack Exchange Q&A for ham radio operators and enthusiasts",
    "Canadian prepper": "Cold-climate emergency preparedness and winter survival guides",
    "TED military": "TED Talks on military topics, veterans, and defense policy",
    "Wikisource": "Library of free public-domain source texts and historical documents",
    "Water Treatment Library": "Comprehensive guides on water purification, filtration, and treatment methods",
    "Physics by Wikipedia": "Wikipedia articles covering physics concepts and discoveries",
    "TED solar energy": "TED Talks on solar power, renewable energy, and sustainability",
    "WikEM": "Emergency medicine reference with clinical decision support",
    "OpenStreetMap Wiki": "Documentation for the open-source mapping project",
    "Homebrewing Q&A": "Stack Exchange Q&A for home beer and wine brewing",
    "TED justice system": "TED Talks on criminal justice, reform, and the legal system",
    "TED human rights": "TED Talks on human rights, equality, and social justice",
    "MDWiki Medical Encyclopedia": "Offline medical encyclopedia for clinical reference",
    "TED democracy": "TED Talks on democracy, governance, and civic participation",
    "MediaWiki": "Documentation for the MediaWiki software platform",
    "Wikipedia in simple English": "Wikipedia written in simplified English for accessibility",
    "Urban Prepper": "Urban survival and emergency preparedness for city environments",
    "Biology Courses": "University-level biology course materials and textbooks",
    "Medical Library": "General medical reference library for health information",
    "Wikivoyage": "Free worldwide travel guide with destination information",
    "WikiSummaries": "Concise summaries of Wikipedia's most important articles",
    "History by Wikipedia": "Wikipedia articles covering historical events and periods",
    "Gardening & Landscaping Q&A": "Stack Exchange Q&A for gardening and landscaping",
    "Information Security Q&A": "Stack Exchange Q&A for cybersecurity and infosec",
    "Engineering Q&A": "Stack Exchange Q&A for engineering topics",
    "Medical Sciences Q&A": "Stack Exchange Q&A for medical science questions",
    "TED plants": "TED Talks on botany, agriculture, and plant science",
    "Medicine LibreTexts": "Open-access medical textbooks and educational materials",
    "Home Improvement Q&A": "Stack Exchange Q&A for DIY and home repair",
    "TED law": "TED Talks on law, legal systems, and regulation",
    "Military Medicine": "Field medicine, combat casualty care, and military health references",
    "Prunelle books": "Curated book collection for offline reading",
    "A Library of Knots": "Illustrated guide to knot-tying techniques and applications",
    "Incognito cat": "Privacy and digital security guide for personal protection",
    "TED water": "TED Talks on water access, conservation, and purification",
    "TED maps": "TED Talks on cartography, geography, and spatial data",
    "TED sociology": "TED Talks on sociology, culture, and human behavior",
    "TED coronavirus": "TED Talks on COVID-19, pandemics, and public health response",
    "Knots by Wikipedia": "Wikipedia articles on knot types and their uses",
}


class AssistantTools:
    def __init__(self, ai_name="PuffPAI", user_name=None):
        from puffpai.config import get_config
        _cfg = get_config()

        self.ai_name = ai_name
        self.user_name = user_name

        # Use centralized LLM backend (llama.cpp → Ollama → error)
        try:
            from puffpai.config import get_llm_backend
            self._backend = get_llm_backend()
        except RuntimeError:
            # No backend available — create a stub that returns error messages
            self._backend = None

        # HA bridge (single source of truth for Home Assistant)
        from puffpai.ha_bridge import get_bridge
        self._ha_bridge = get_bridge()

        # Automation engine (Node-RED) config from centralized config
        self.automation_url = _cfg.nodered.url
        self.automation_timeout = _cfg.nodered.timeout

        self.tools = self.create_tools()
        self.tool_dict = {t.name: t for t in self.tools}

    def _llm_chat(self, prompt: str) -> str:
        """Send a prompt to the LLM backend and return the response text."""
        if self._backend is None:
            return "[Error] No LLM backend available. Configure models.llm_gguf_path or start Ollama."
        return self._backend.chat([{"role": "user", "content": prompt}])

    def _llm_chat_json(self, prompt: str, model: str | None = None) -> str:
        """Like :meth:`_llm_chat` but asks Ollama for JSON-CONSTRAINED output and
        can target a specific model. The default prose model (gpt-oss) is flaky
        at structured output, so tools that need machine-parseable results (meal
        plans, etc.) route here — qwen3 with ``format=json`` returns valid JSON
        reliably and fast. Degrades to the normal prose path on ANY error, so a
        wrong model name or an Ollama hiccup only ever costs the JSON guarantee,
        never the call itself."""
        try:
            import requests as _rq
            url = getattr(self._backend, "_url", None) or "http://localhost:11434"
            _model_name = model or getattr(self._backend, "_model", None)
            payload = {
                "model": _model_name,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "format": "json",
                "options": {
                    "temperature": 0.6,
                    "num_ctx": _model_options.planner_num_ctx(),
                    "num_predict": 1500,
                },
            }
            # num_ctx is the PLANNER role's context (4096 on the appliance):
            # this path targets qwen3, a DIFFERENT model from the prose one,
            # and that is what its resident runner uses; the value comes from
            # model_options.planner_num_ctx, not a literal.
            ka = getattr(self._backend, "_keep_alive", None)
            if ka is None:
                # No backend (or an old one): fall back to the shared pin
                # rather than sending nothing, which would reset the model's
                # residency to the ollama server default.
                ka = _model_options.keep_alive()
            payload["keep_alive"] = ka
            # A thinking model otherwise spends its whole token budget
            # reasoning and returns EMPTY content; suppressing it makes the
            # JSON emit directly — ~11s and reliable instead of ~40s empty.
            # The right lever is PER FAMILY (qwen3 takes a boolean, gpt-oss an
            # effort level), so it comes from the shared resolver rather than
            # a literal: model_options.thinking_control.
            payload.update(_model_options.thinking_control(_model_name))
            r = _rq.post(f"{url}/api/chat", json=payload, timeout=45)
            r.raise_for_status()
            return r.json()["message"]["content"]
        except Exception:
            return self._llm_chat(prompt)

    def create_tools(self):
        """Create the assistant's tool suite."""
        
        @tool
        def list_indexed_libraries(dummy: str = "") -> str:
            """List all indexed knowledge base libraries with descriptions. Call this when user asks what libraries are indexed or available."""
            # Inside the API server process, answer from the same inventory
            # GET /knowledge/libraries serves (ZIM packs discovered on disk +
            # manifest collections). The JSON manifest below only knows about
            # ChromaDB ingestions, which on a ZIM+FTS box is just the user's
            # uploads — that answered "what libraries do you have?" with two
            # stray upload filenames.
            _api = sys.modules.get('puffpai.api_server')
            if _api is not None and hasattr(_api, '_get_indexed_libraries_direct'):
                try:
                    return _api._get_indexed_libraries_direct()
                except Exception:
                    pass
            try:
                file_path = os.path.join(os.path.dirname(__file__), 'indexed_libraries.json')
                with open(file_path, 'r') as f:
                    collections = json.load(f)

                if not collections:
                    return "No libraries are currently indexed."

                # Deduplicate by library name, preserving order
                seen = set()
                unique = []
                for coll in collections:
                    name = coll['library']
                    if name not in seen and not name.endswith('.pdf'):
                        seen.add(name)
                        unique.append(name)
                unique.sort(key=str.lower)

                lines = []
                for name in unique:
                    desc = LIBRARY_DESCRIPTIONS.get(name, "")
                    if desc:
                        lines.append(f"- {name}: {desc}")
                    else:
                        lines.append(f"- {name}")
                return f"Indexed libraries ({len(unique)} total):\n" + "\n".join(lines)
            except FileNotFoundError:
                return "No indexed libraries found. The indexed_libraries.json file does not exist."
            except Exception as e:
                return f"Error reading libraries: {e}"
        
        # --- File sandboxing: only allow access to safe directories ---
        _ALLOWED_READ_DIRS = [
            os.path.expanduser("~/Documents"),
            os.path.join(_REPO_ROOT, "data"),
            os.path.join(_REPO_ROOT, "config"),
        ]
        _ALLOWED_WRITE_DIRS = [
            os.path.expanduser("~/Documents"),
            os.path.join(_REPO_ROOT, "data"),
        ]

        def _is_path_allowed(filepath: str, allowed_dirs: list) -> bool:
            """Check if a file path is within any allowed directory."""
            try:
                real = os.path.realpath(filepath)
                return any(
                    real.startswith(os.path.realpath(d) + os.sep) or
                    real == os.path.realpath(d)
                    for d in allowed_dirs
                )
            except (OSError, ValueError):
                return False

        @tool
        def read_file(filepath: str) -> str:
            """Read contents of a local file. Use this when the user asks to read a file. Files must be in ~/Documents or the data/ directory."""
            try:
                filepath = filepath.strip().strip("'\"")

                if "~" in filepath or "documents" in filepath.lower():
                    docs_path = os.path.expanduser("~/Documents")
                    if "documents" in filepath.lower() and not os.path.exists(filepath):
                        filename = os.path.basename(filepath)
                        filepath = os.path.join(docs_path, filename)

                if not _is_path_allowed(filepath, _ALLOWED_READ_DIRS):
                    return (
                        f"Access denied: cannot read '{filepath}'. "
                        f"Readable locations: ~/Documents, data/, config/"
                    )

                with open(filepath, 'r', encoding='utf-8') as f:
                    content = f.read()
                    return content[:1000] + ("..." if len(content) > 1000 else "")
            except Exception as e:
                return f"Error reading file: {e}"

        @tool
        def write_file(filepath_and_content: str) -> str:
            """Write content to a file. Format: 'filename|content'. Just use the filename like 'myfile.txt|content'. If user mentions 'documents folder', use 'DOCUMENTS/filename.txt|content'. Files must be in ~/Documents or the data/ directory."""
            try:
                parts = filepath_and_content.split('|', 1)
                if len(parts) != 2:
                    return "Error: Use format 'filepath|content'"
                filepath, content = parts

                filepath = filepath.strip().strip("'\"")

                if "DOCUMENTS/" in filepath or "Documents" in filepath or "documents" in filepath.lower():
                    docs_path = os.path.expanduser("~/Documents")
                    filename = os.path.basename(filepath)
                    filepath = os.path.join(docs_path, filename)
                elif filepath.startswith("C:\\Users\\") and "\\Documents\\" in filepath:
                    docs_path = os.path.expanduser("~/Documents")
                    filename = os.path.basename(filepath)
                    filepath = os.path.join(docs_path, filename)

                if not _is_path_allowed(filepath, _ALLOWED_WRITE_DIRS):
                    return (
                        f"Access denied: cannot write to '{filepath}'. "
                        f"Writable locations: ~/Documents, data/"
                    )

                parent_dir = os.path.dirname(filepath)
                if parent_dir and not os.path.exists(parent_dir):
                    os.makedirs(parent_dir, exist_ok=True)

                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write(content)
                return f"Successfully wrote to {filepath}"
            except Exception as e:
                return f"Error writing file: {e}"
        
        @tool
        def save_prep_item(item: str) -> str:
            """Save a preparedness checklist item. Use this when user mentions emergency supplies, survival items, or preparedness tasks."""
            checklist_file = os.path.join(_REPO_ROOT, "preparedness_checklist.json")
            
            try:
                if os.path.exists(checklist_file):
                    with open(checklist_file, 'r') as f:
                        checklist = json.load(f)
                else:
                    checklist = []
                
                checklist.append({
                    'item': item,
                    'added': datetime.now().isoformat(),
                    'completed': False
                })
                
                with open(checklist_file, 'w') as f:
                    json.dump(checklist, f, indent=2)
                
                return f"Added to preparedness checklist: {item}"
            except Exception as e:
                return f"Error: {e}"
        
        @tool
        def calculate(expression: str) -> str:
            """Evaluate mathematical expressions. Use this for any math calculations. Example: '5 + 3 * 2'"""
            import ast
            import operator as _op

            _SAFE_OPS = {
                ast.Add:      _op.add,
                ast.Sub:      _op.sub,
                ast.Mult:     _op.mul,
                ast.Div:      _op.truediv,
                ast.FloorDiv: _op.floordiv,
                ast.Mod:      _op.mod,
                ast.Pow:      _op.pow,
                ast.USub:     _op.neg,
                ast.UAdd:     _op.pos,
            }
            _SAFE_FUNCS = {
                'abs': abs, 'round': round, 'min': min, 'max': max,
                'pow': pow,
            }
            _MAX_EXPONENT = 1000  # prevent 10**999999 memory bombs

            def _safe_eval(node):
                if isinstance(node, ast.Expression):
                    return _safe_eval(node.body)
                if isinstance(node, ast.Constant):
                    if isinstance(node.value, (int, float)):
                        return node.value
                    raise ValueError(f"Unsupported constant: {node.value!r}")
                if isinstance(node, ast.BinOp):
                    op_fn = _SAFE_OPS.get(type(node.op))
                    if op_fn is None:
                        raise ValueError(f"Unsupported operator: {type(node.op).__name__}")
                    left = _safe_eval(node.left)
                    right = _safe_eval(node.right)
                    if isinstance(node.op, ast.Pow) and isinstance(right, (int, float)) and abs(right) > _MAX_EXPONENT:
                        raise ValueError(f"Exponent too large: {right}")
                    return op_fn(left, right)
                if isinstance(node, ast.UnaryOp):
                    op_fn = _SAFE_OPS.get(type(node.op))
                    if op_fn is None:
                        raise ValueError(f"Unsupported unary: {type(node.op).__name__}")
                    return op_fn(_safe_eval(node.operand))
                if isinstance(node, ast.Call):
                    if not isinstance(node.func, ast.Name):
                        raise ValueError("Only simple function calls allowed")
                    fn = _SAFE_FUNCS.get(node.func.id)
                    if fn is None:
                        raise ValueError(f"Function not allowed: {node.func.id}")
                    args = [_safe_eval(a) for a in node.args]
                    return fn(*args)
                raise ValueError(f"Unsupported expression: {type(node).__name__}")

            try:
                tree = ast.parse(normalize_math_expression(expression),
                                 mode='eval')
                result = _safe_eval(tree)
                return str(result)
            except Exception as e:
                return f"Calculation error: {e}"
        
        @tool
        def write_code(spec: str, language: str = "", context: str = "") -> str:
            """Write or generate code, scripts, functions, automations,
            regexes, Node-RED flows, systemd units, or programs for the user.

            Use this whenever the user asks you to write, generate, create,
            fix, or refactor code — in any language — including non-imperative
            asks ("I need a script that...", "help me write a program to...",
            "can you build me an automation for..."). Do NOT write the code
            yourself: this tool calls the appliance's dedicated local coding
            model, which is far better at it than you are. Pass the user's
            request through as-is.

            This tool only WRITES code. It never runs it, saves it to a file,
            or changes anything on this appliance. If the user then wants the
            code RUN or TESTED, that is a separate step: put it in the sandbox
            with write_file and run it with workspace_exec, which is a
            bubblewrap jail with no network and no path outside the workspace.
            Never run generated code any other way.

            Args:
                spec: What the user wants written, in their own words.
                language: Optional language ("python", "bash", "javascript").
                context: Optional existing code the user pasted, passed to the
                    model verbatim so it edits what they actually have.
            """
            from puffpai.inference import code_model
            if not code_model.is_configured():
                return ("A coding model is not configured on this appliance "
                        "yet, so I can't write code. Tell the user plainly — "
                        "do not write the code yourself.")
            result = code_model.generate_code(
                spec, language or None, context or None)
            if result.error:
                return result.explanation
            if result.unparsed:
                # Never fabricate the missing block: show what was really said.
                return ("The coding model answered without a code block. This "
                        "is exactly what it said:\n\n" + result.explanation)
            return result.as_markdown()

        @tool
        def search_knowledge_base(query: str) -> str:
            """Search the offline knowledge base for information. Use this when user asks about factual information."""
            # Inside the API server process, use its full retrieval stack
            # (manifest collections + user uploads + ZIM full-text shards) —
            # the same inventory the RAG chat route searches. sys.modules
            # gate, not an import: standalone UIs must not pull in Flask.
            _api = sys.modules.get('puffpai.api_server')
            if _api is not None and hasattr(_api, '_get_rag_context'):
                try:
                    ctx = _api._get_rag_context(query, n_results=6)
                except Exception:
                    ctx = None
                if isinstance(ctx, dict) and ctx.get('context_text'):
                    return ctx['context_text'][:2400]

            if not hasattr(self, '_dual_zone_rag'):
                try:
                    from puffpai.knowledge.rag import DualZoneRAG
                    self._dual_zone_rag = DualZoneRAG()
                except Exception:
                    self._dual_zone_rag = None

            if self._dual_zone_rag is None:
                return "Knowledge base not available. Ensure ChromaDB is configured."

            try:
                # query() returns a RAGResponse, not a list — iterate .results.
                response = self._dual_zone_rag.query(query, n_results=3)
                results = getattr(response, 'results', response)
                if not results:
                    return "No relevant information found."
                parts = []
                for r in results:
                    source_label = f"[{r.source.value}]" if hasattr(r, 'source') else ""
                    text = r.text[:300] if hasattr(r, 'text') else str(r)[:300]
                    parts.append(f"{source_label} {text}")
                return "\n\n".join(parts)
            except Exception as e:
                return f"Error: {e}"

        @tool
        def web_search(query: str) -> str:
            """Search the internet for REAL-TIME information using DuckDuckGo. You MUST use this tool for ANY question about: current events, news, weather, prices, stock market, sports scores, recent happenings, what is happening now, today, this week, this year, or any topic where your training data might be outdated. NEVER answer questions about current/recent events from memory — ALWAYS search first."""
            # DuckDuckGo consent check (ToS risk for a commercial product).
            # Resolved through the egress-door registry so this door is
            # enumerated alongside every other one in /system/status. The
            # registry honours the pre-existing top-level "web_search_consent"
            # key when egress_doors.web_search is absent, so an owner who
            # already opted in is not silently opted back out.
            from puffpai.security.egress_doors import EgressBlocked, require_open
            try:
                require_open("web_search")
            except EgressBlocked as blocked:
                return f"[Web Search Disabled] {blocked.user_message}"
            ns = get_network_status()
            # Probe through the door we just opened, never a public resolver.
            if not ns.has_internet(door_id="web_search"):
                return "[Offline] Web search requires an internet connection. Try again when online."
            try:
                from ddgs import DDGS

                ddgs = DDGS()
                results = list(ddgs.text(query, max_results=5))

                if not results:
                    return "No search results found for that query."

                formatted = []
                for i, r in enumerate(results, 1):
                    title = r.get('title', 'No title')
                    body = r.get('body', 'No description')
                    url = r.get('href', '')
                    formatted.append(f"{i}. {title}\n   {body[:200]}\n   Source: {url}")

                return f"Web search results for '{query}':\n\n" + "\n\n".join(formatted)
            except ImportError:
                return "Web search is not available. Install with: pip install ddgs"
            except Exception as e:
                return f"Web search error: {e}"

        @tool
        def ha_list_devices(filter_type: str = "all") -> str:
            """List controllable devices in Home Assistant. Use filter_type 'light', 'switch', 'media_player', or 'all'."""
            from puffpai.ha_bridge import HABridgeError
            if "=" in filter_type:
                filter_type = filter_type.split("=")[-1].strip("'\"")

            try:
                domain = None if filter_type == "all" else filter_type
                devices = self._ha_bridge.list_devices(domain=domain)
            except HABridgeError as exc:
                return f"Error: {exc}"

            if devices:
                lines = [f"{d.friendly_name} ({d.entity_id}): {d.state}" for d in devices[:20]]
                return f"Found {len(devices)} controllable devices:\n" + "\n".join(lines)
            return "No controllable devices found. Make sure Home Assistant has lights, switches, or media players configured."
        
        @tool
        def ha_get_device_info(entity_id: str) -> str:
            """Get detailed information about a specific Home Assistant device. Use entity_id like 'light.living_room' or 'switch.bedroom'."""
            from puffpai.ha_bridge import HABridgeError
            if "=" in entity_id:
                entity_id = entity_id.split("=")[-1].strip("'\"")

            try:
                dev = self._ha_bridge.get_device(entity_id)
            except HABridgeError as exc:
                return f"Error: {exc}"

            info = [
                f"Entity ID: {dev.entity_id}",
                f"State: {dev.state}",
                f"Last Changed: {dev.last_changed}",
                f"\nAttributes:",
            ]
            for key, value in dev.attributes.items():
                info.append(f"  {key}: {value}")
            return "\n".join(info)
        
        @tool
        def ha_control_device(entity_id_and_action: str) -> str:
            """Control a Home Assistant device. Format: 'entity_id|action' where action is 'turn_on', 'turn_off', or 'toggle'.

            Safety-critical targets (cameras, covers/garage doors, locks, alarm
            panels, valves, water heaters, sirens) are never actuated from here;
            the tool reports that a person has to confirm.
            """
            from puffpai.ha_bridge import ConfirmationRequired, HABridgeError
            try:
                if "=" in entity_id_and_action:
                    entity_id_and_action = entity_id_and_action.split("=")[-1].strip("'\"")

                entity_id_and_action = entity_id_and_action.strip("'\"")
                parts = entity_id_and_action.split('|')
                if len(parts) != 2:
                    return "Error: Use format 'entity_id|action' (e.g., 'light.bedroom|turn_on')"

                entity_id, action = parts[0].strip(), parts[1].strip().lower()

                result = self._ha_bridge.control_device(entity_id, action)
                return f"Successfully executed {action} on {entity_id}"
            except ConfirmationRequired as exc:
                # Must precede HABridgeError — ConfirmationRequired subclasses it,
                # and a refusal reported as a generic "Error:" reads to the model
                # as a transient fault worth retrying.
                return _needs_confirmation_reply(exc)
            except HABridgeError as exc:
                return f"Error: {exc}"
            except Exception as e:
                return f"Error: {e}"
                    
        @tool
        def ha_block_device_internet(device_name: str) -> str:
            """Block internet access for a smart device. This adds firewall rules to prevent the device from contacting external servers."""
            blocked_devices_file = os.path.join(_REPO_ROOT, "blocked_devices.json")
            
            try:
                if os.path.exists(blocked_devices_file):
                    with open(blocked_devices_file, 'r') as f:
                        blocked = json.load(f)
                else:
                    blocked = []
                
                blocked.append({
                    'device': device_name,
                    'blocked_at': datetime.now().isoformat(),
                    'reason': 'User requested privacy protection'
                })
                
                with open(blocked_devices_file, 'w') as f:
                    json.dump(blocked, f, indent=2)
                
                return f"Added {device_name} to blocked devices list. NOTE: Actual firewall blocking not yet implemented - you'll need to configure your firewall/router manually to block this device's MAC address or IP from internet access."
            except Exception as e:
                return f"Error: {e}"
        
        @tool
        def ha_discover_new_devices() -> str:
            """Discover new devices that have appeared on the Home Assistant network."""
            from puffpai.ha_bridge import HABridgeError
            try:
                devices = self._ha_bridge.list_devices()
            except HABridgeError as exc:
                return f"Error: {exc}"

            current_ids = [d.entity_id for d in devices]

            seen_file = ha_seen_devices_path()
            # The legacy copy is read in place, exactly once — only when the
            # new path holds nothing yet — so an upgrade does not announce the
            # whole house as newly discovered. It is never written back.
            read_from = seen_file if os.path.exists(seen_file)                 else _LEGACY_SEEN_DEVICES_PATH
            seen_devices = set()
            try:
                if os.path.exists(read_from):
                    with open(read_from, 'r') as f:
                        seen_devices = set(json.load(f))
            except (OSError, ValueError) as exc:
                _logger.warning("device memo unreadable at %s: %s", read_from, exc)

            new_ids = [eid for eid in current_ids if eid not in seen_devices]

            # A memo that cannot be written is a worse report next time, not a
            # failed turn: the owner still gets the answer they asked for.
            try:
                os.makedirs(os.path.dirname(seen_file), exist_ok=True)
                with open(seen_file, 'w') as f:
                    json.dump(current_ids, f, indent=2)
            except OSError as exc:
                _logger.warning("could not write the device memo to %s: %s",
                                seen_file, exc)

            if new_ids:
                by_id = {d.entity_id: d for d in devices}
                lines = [f"  - {by_id[eid].friendly_name} ({eid})" for eid in new_ids[:10]]
                return f"Found {len(new_ids)} new controllable devices:\n" + "\n".join(lines)
            return "No new controllable devices discovered."

        @tool
        def ha_scan_hardware(dummy: str = "") -> str:
            """Scan the Home Assistant host for new USB hardware (e.g. Zigbee dongle, Z-Wave stick).

            Use this when the user says things like: 'scan the network', 'scan for new devices',
            'look for new hardware', 'I just plugged something in', 'detect my dongle'.
            This looks at USB hardware plugged into the HA host — NOT at IP network devices
            (for that, use privacy_scan_network).
            """
            try:
                from puffpai.zigbee_manager import ZigbeeManager
                mgr = ZigbeeManager()
                devices = mgr.list_usb_devices()
                matches = mgr.find_zigbee_dongles(devices)
            except Exception as exc:
                return f"Hardware scan failed: {exc}"

            if not matches:
                if not devices:
                    return ("No new USB hardware detected on the Home Assistant host. "
                            "If you just plugged in a dongle, wait a few seconds and try again.")
                lines = [f"Found {len(devices)} USB device(s), but none look like a known Zigbee coordinator:"]
                for d in devices[:5]:
                    desc = d.description or d.manufacturer or "unknown"
                    lines.append(f"  - {desc} (vid:{d.vid} pid:{d.pid}) at {d.device_path or 'n/a'}")
                return "\n".join(lines)

            best = matches[0]
            extra = ""
            if len(matches) > 1:
                extra = f" (+{len(matches) - 1} other candidate(s))"
            already = " — already managed by HA" if mgr.is_zha_installed() else ""
            return (
                f"A new device was detected — it looks like a {best.spec.name} Zigbee "
                f"coordinator{extra}. It is connected at {best.device.device_path or 'unknown path'} "
                f"with radio type '{best.spec.radio_type}'{already}. "
                f"Would you like me to manage it? (say 'yes' and I'll add it to Home Assistant)"
            )

        @tool
        def ha_add_zigbee_dongle(dongle_identifier: str = "") -> str:
            """Add a detected Zigbee USB dongle as a managed device in Home Assistant (installs ZHA).

            Call this after ha_scan_hardware found a dongle and the user confirmed.
            The identifier argument is optional — if omitted, the first detected dongle is used.
            """
            try:
                from puffpai.zigbee_manager import ZigbeeManager
                mgr = ZigbeeManager()

                if mgr.is_zha_installed():
                    return ("Zigbee is already managed in Home Assistant (ZHA integration is "
                            "already installed). No action needed.")

                matches = mgr.find_zigbee_dongles()
                if not matches:
                    return ("I can't find a Zigbee dongle plugged into the Home Assistant host. "
                            "Please plug it in and run a hardware scan first.")

                # If the user specified a hint (e.g. "sonoff", "znp"), filter
                chosen = matches[0]
                hint = (dongle_identifier or "").strip().lower()
                if hint:
                    for m in matches:
                        blob = f"{m.spec.name} {m.spec.radio_type} {m.device.device_path or ''}".lower()
                        if hint in blob:
                            chosen = m
                            break

                result = mgr.install_zha(chosen)
                if result.get("success"):
                    if result.get("step") == "already_installed":
                        return "Zigbee is already managed in Home Assistant."
                    return (
                        f"Confirmed: {chosen.spec.name} is now a managed device in Home Assistant. "
                        f"The ZHA integration has been installed on port "
                        f"{chosen.device.device_path}."
                    )
                # Failure — surface the step where it got stuck
                step = result.get("step")
                err = result.get("error")
                hint_msg = ""
                if step in ("no_path",):
                    hint_msg = " The dongle was detected but HA did not report a device path yet — try again in a few seconds."
                elif step == "unknown_form":
                    hint_msg = (" HA asked for extra information we didn't anticipate. "
                                "Open Settings → Devices & Services in HA and finish the flow manually.")
                return f"Couldn't finish adding the Zigbee dongle at step '{step}': {err}.{hint_msg}"
            except Exception as exc:
                return f"Zigbee add failed: {exc}"

        @tool
        def ha_provision_propose(kind: str = "script", object_id: str = "",
                                 yaml_or_json: str = "",
                                 reason: str = "") -> str:
            """PROPOSE a new Home Assistant script or automation for the owner to approve.

            Use this when something can only be done by a Home Assistant
            administrator (for example opening a Zigbee pairing window) and a
            small, single-purpose script would let PuffPAI do it without
            holding administrator rights. ``yaml_or_json`` is the body of the
            script/automation.

            This tool PROPOSES ONLY. It never writes anything to Home
            Assistant: it files one consent request showing the owner the exact
            configuration, and the owner approves it with their vault unlocked.
            Never claim the change has been made.
            """
            from puffpai.ha_provisioning import (ProvisioningError,
                                                 propose_ha_change)
            body = (yaml_or_json or "").strip()
            if not body:
                return ("Tell me what the script should contain and I'll put "
                        "it in front of the owner for approval.")
            payload = None
            try:
                payload = json.loads(body)
            except (ValueError, TypeError):
                try:
                    import yaml as _yaml
                    payload = _yaml.safe_load(body)
                except Exception:
                    payload = None
            if not isinstance(payload, dict):
                return ("I couldn't read that as Home Assistant configuration "
                        "— give me a JSON or YAML object.")
            try:
                result = propose_ha_change(kind, object_id, payload, reason,
                                           requested_by="steward")
            except ProvisioningError as exc:
                return f"I can't propose that: {exc}"
            except Exception as exc:
                return f"I couldn't file that proposal: {exc}"
            return result.get("message", "Proposal filed for your approval.")

        @tool
        def ha_run_scene(scene_name: str) -> str:
            """Run a Home Assistant scene or script by name. Use for 'activate', 'run', 'trigger' commands. Discovers scenes dynamically from HA.

            A scene containing a safety-critical member (camera, cover, lock,
            alarm panel, valve, water heater, siren) is not run from here; the
            tool reports that a person has to confirm.
            """
            from puffpai.ha_bridge import ConfirmationRequired, HABridgeError

            if "=" in scene_name:
                scene_name = scene_name.split("=")[-1].strip("'\"")
            if "|" in scene_name:
                scene_name = scene_name.split("|")[-1].strip("'\"")

            scene_name = scene_name.strip("'\"").replace("_", " ")

            for word in ['activate', 'enable', 'run', 'trigger', 'start', 'turn on']:
                if scene_name.lower().startswith(word + ' '):
                    scene_name = scene_name[len(word) + 1:]
                    break

            query = scene_name.lower().strip()

            try:
                scenes = self._ha_bridge.get_scenes()
            except HABridgeError as exc:
                return f"Error fetching scenes: {exc}"

            candidates = {}
            for s in scenes:
                candidates[s.friendly_name.lower()] = s.entity_id
                candidates[s.entity_id.split('.', 1)[1].replace('_', ' ')] = s.entity_id

            entity_id = candidates.get(query)
            if not entity_id:
                for key, eid in candidates.items():
                    if query in key or key in query:
                        entity_id = eid
                        break

            if not entity_id:
                available = sorted(set(candidates.values()))
                return (f"Scene or script '{scene_name}' not found. "
                        f"Available: {', '.join(available[:10])}")

            try:
                self._ha_bridge.activate_scene(entity_id)
                return f"Successfully activated {entity_id}"
            except ConfirmationRequired as exc:
                # A scene is a wrapper. The bridge expands its member list, so a
                # scene containing a camera or a garage door is refused here even
                # though "scene" is not itself a safety-critical domain — and a
                # scene whose members cannot be read is refused too.
                return _needs_confirmation_reply(exc)
            except HABridgeError as exc:
                return f"Error: {exc}"

        @tool
        def ha_find_entity(name_query: str) -> str:
            """Find a Home Assistant entity (device) by partial friendly name or entity ID. Useful before controlling a device when you are not sure of its exact entity_id."""
            from puffpai.ha_bridge import HABridgeError

            if "=" in name_query:
                name_query = name_query.split("=")[-1].strip("'\"")
            name_query = name_query.strip("'\"")

            try:
                matches = self._ha_bridge.find_devices(name_query)
            except HABridgeError as exc:
                return f"Error: {exc}"

            if not matches:
                return f"No entities found matching '{name_query}'."
            lines = [f"{d.friendly_name} ({d.entity_id}): {d.state}" for d in matches[:15]]
            return f"Found {len(matches)} match(es):\n" + "\n".join(lines)

        @tool
        def ha_set_light(light_params: str) -> str:
            """Set light brightness or colour temperature. Format: 'entity_id|brightness|color_temp' where brightness is 0-100 (percent) and color_temp is 'warm', 'cool', or a mireds value. Examples: 'light.bedroom|50' or 'light.living_room|80|warm'."""
            from puffpai.ha_bridge import ConfirmationRequired, HABridgeError

            if "=" in light_params:
                light_params = light_params.split("=")[-1].strip("'\"")
            light_params = light_params.strip("'\"")

            parts = [p.strip() for p in light_params.split('|')]
            if len(parts) < 2:
                return "Error: Use format 'entity_id|brightness' or 'entity_id|brightness|color_temp'"

            entity_id = parts[0]
            color_temp = parts[2] if len(parts) > 2 else None

            try:
                brightness_val = int(float(parts[1].rstrip('%')))
                brightness_val = max(0, min(100, brightness_val))
            except ValueError:
                return f"Invalid brightness '{parts[1]}'. Use 0-100."

            try:
                self._ha_bridge.set_light(
                    entity_id,
                    brightness_pct=brightness_val,
                    color_temp=color_temp,
                )
            except ConfirmationRequired as exc:
                # ``set_light`` hardcodes the *service* domain as ``light`` but
                # the entity id is whatever arrived, so this catches a
                # safety-critical target dressed up as a brightness change.
                return _needs_confirmation_reply(exc)
            except HABridgeError as exc:
                return f"Error: {exc}"

            msg = f"Set {entity_id} to {brightness_val}% brightness"
            if color_temp:
                msg += f" with {color_temp} colour temperature"
            return msg

        @tool
        def ha_set_temperature(thermostat_params: str) -> str:
            """Set a thermostat temperature. Format: 'entity_id|temperature' where temperature is in the unit configured in Home Assistant (usually Fahrenheit or Celsius). Example: 'climate.living_room|72'."""
            from puffpai.ha_bridge import ConfirmationRequired, HABridgeError

            if "=" in thermostat_params:
                thermostat_params = thermostat_params.split("=")[-1].strip("'\"")
            thermostat_params = thermostat_params.strip("'\"")

            parts = [p.strip() for p in thermostat_params.split('|')]
            if len(parts) < 2:
                return "Error: Use format 'entity_id|temperature' e.g. 'climate.living_room|72'"

            entity_id = parts[0]
            try:
                temperature = float(parts[1].replace('°', '').strip())
            except ValueError:
                return f"Invalid temperature '{parts[1]}'"

            try:
                self._ha_bridge.set_climate(entity_id, temperature=temperature)
                return f"Set {entity_id} temperature to {temperature}"
            except ConfirmationRequired as exc:
                # Same shape as ``set_light``: the service domain is hardcoded
                # ``climate``, the entity id is not. A ``water_heater`` target
                # lands here rather than on the wire.
                return _needs_confirmation_reply(exc)
            except HABridgeError as exc:
                return f"Error: {exc}"

        @tool
        def ha_list_cameras(dummy: str = "") -> str:
            """List all camera entities Home Assistant exposes, marking which are already on the PuffPAI dashboard.

            Use this when the user asks about their cameras, a new camera on their
            Reolink hub/NVR, or what's on the dashboard.
            """
            from puffpai import dashboard_registry as _dreg
            from puffpai.ha_bridge import HABridgeError
            try:
                cams = _dreg.find_camera_entity(self._ha_bridge, "")
            except HABridgeError as exc:
                return f"Error: {exc}"
            if not cams:
                return "Home Assistant exposes no camera entities."
            on_dash = {x["entity_id"] for x in _dreg.list_cameras()}
            lines = []
            for d in cams:
                mark = " [on dashboard]" if d.entity_id in on_dash else " [not on dashboard]"
                lines.append(f"  - {d.friendly_name} ({d.entity_id}){mark}")
            return f"Found {len(cams)} camera(s):\n" + "\n".join(lines)

        @tool
        def dashboard_add_camera(camera_name: str) -> str:
            """Add a camera to the PuffPAI dashboard as a live tile.

            Use this when the user asks to add a camera (e.g. a new Reolink
            camera) to their dashboard. Pass the camera's name or entity_id.
            Bridge resolves it against Home Assistant (only real entities can
            be added) and Sentinel's consent gate must allow it.
            """
            from puffpai import dashboard_registry as _dreg
            camera_name = camera_name.strip().strip("'\"")
            _ok, msg = _dreg.add_camera_validated(
                self._ha_bridge, camera_name, added_by="cortex-agent",
                task_text=f"dashboard_add_camera({camera_name})")
            return msg

        @tool
        def dashboard_add_widget(widget_spec: str) -> str:
            """Add a widget to the PuffPAI dashboard: news feeds, calendar, and more.

            Format: 'type | key=value | key=value'. Examples:
              'news | source=npr'
              'news | url=https://example.com/feed.xml | title=My Feed'
              'calendar | days_ahead=7'
            Known news sources: npr, bbc, hacker news, ars technica,
            the guardian, the verge. For cameras use dashboard_add_camera.
            """
            from puffpai import dashboard_widgets as _dwid
            parts = [p.strip() for p in widget_spec.split("|")]
            wtype = parts[0].strip().strip("'\"").lower()
            props: dict = {}
            for p in parts[1:]:
                if "=" in p:
                    k, v = p.split("=", 1)
                    props[k.strip().lower()] = v.strip().strip("'\"")
            # Forgiving: 'news npr' or a bare source name after the type
            if wtype.startswith("news ") and "source" not in props and "url" not in props:
                props["source"] = wtype[5:].strip()
                wtype = "news"
            _ok, msg, _w = _dwid.add_widget(wtype, props, added_by="cortex-agent")
            return msg

        @tool
        def dashboard_remove_widget(widget_name: str) -> str:
            """Remove a widget from the PuffPAI dashboard by its title or type (e.g. 'npr news', 'calendar')."""
            from puffpai import dashboard_widgets as _dwid
            _ok, msg = _dwid.remove_widget(widget_name)
            return msg

        @tool
        def dashboard_list_widgets(dummy: str = "") -> str:
            """List everything on the PuffPAI dashboard (widgets and camera tiles) and the available widget types."""
            from puffpai import dashboard_widgets as _dwid
            widgets = _dwid.list_widgets(include_cameras=True)
            types = ", ".join(sorted(_dwid.WIDGET_CATALOG))
            if not widgets:
                return f"The dashboard has no widgets yet. Available types: {types}."
            lines = []
            for w in widgets:
                title = w["props"].get("title") or w["type"]
                lines.append(f"  - {title} ({w['type']}, id {w['id']})")
            return (f"{len(widgets)} widget(s) on the dashboard:\n"
                    + "\n".join(lines)
                    + f"\nAvailable types: {types}.")

        @tool
        def ha_approve_pending_device(device_and_mode: str = "") -> str:
            """Approve a newly-detected Home Assistant device that is waiting for the user's OK.

            Use when the user says things like 'approve the new device',
            'manage that device', 'add the new device', 'yes lock it down',
            'approve the new sensor with balanced access'. The argument is the
            device name or entity_id, optionally followed by a security mode
            (lockdown / balanced / open). If only one device is pending you may
            leave it empty. Default security mode is balanced.
            """
            from puffpai.ha_device_watcher import get_watcher
            watcher = get_watcher()

            raw = (device_and_mode or "").strip().strip("'\"")
            lower = raw.lower()

            # Pull an embedded security mode out of the phrase. Only match at
            # the END of the phrase (or "open access"/"open mode" anywhere):
            # a bare substring check turned "approve the OPEN sensor" or
            # device names containing "open" into open-mode approvals.
            mode = "balanced"
            m = re.search(
                r'\b(locked[ -]?down|lock (?:it )?down|lockdown|balanced|'
                r'open(?:[ -](?:access|mode))?)\s*$', lower)
            if not m:
                m = re.search(r'\bopen[ -](?:access|mode)\b', lower)
            if m:
                kw = m.group(0).strip()
                mode = "open" if kw.startswith("open") else (
                    "balanced" if kw == "balanced" else "lockdown")
                lower = lower[:m.start()] + " " + lower[m.end():]
            name_q = " ".join(lower.replace("_", " ").split()).strip()

            pending = watcher.get_pending()
            if not pending:
                return "There are no new devices waiting for approval."

            target = None
            if not name_q:
                if len(pending) == 1:
                    target = pending[0]
                else:
                    opts = "\n".join(f"  - {d.friendly_name} ({d.entity_id})"
                                     for d in pending[:8])
                    return ("Which device would you like to approve? These are "
                            "waiting:\n" + opts)
            else:
                # Exact entity_id match wins.
                for d in pending:
                    if d.entity_id.lower() == name_q.replace(" ", "_") or \
                            d.entity_id.lower() == name_q:
                        target = d
                        break
                if target is None:
                    toks = name_q.split()
                    for d in pending:
                        hay = f"{d.friendly_name} {d.entity_id}".lower().replace("_", " ")
                        if all(t in hay for t in toks):
                            target = d
                            break

            if target is None:
                opts = "\n".join(f"  - {d.friendly_name} ({d.entity_id})"
                                 for d in pending[:8])
                return (f"I couldn't find a pending device matching '{name_q}'. "
                        "Devices waiting for approval:\n" + opts)

            # A model's
            # choice is never an approval. The person approves on the dashboard.
            return (f"{target.friendly_name} ({target.entity_id}) is waiting for "
                    f"your approval. I can't approve it myself: tap Approve on "
                    f"the dashboard to let me manage it.")

        @tool
        def sentinel_approve_device(device_name: str = "") -> str:
            """Tell the person how to approve an open consent request. APPROVES NOTHING.

            Approvals come only from the person's own words, handled
            deterministically in the current turn (say "yes" / "approve
            pairing" right after PuffPAI asks), or from the dashboard Approve
            button. A model decision is never an approval, so this tool only
            lists what is waiting.
            """
            from puffpai.security import consent_requests as _creq
            open_reqs = _creq.list_open()
            if not open_reqs:
                return "There are no open consent requests."
            spoken = {"zigbee_pair", "ha_provision"}
            lines = []
            for r in open_reqs[:6]:
                how = ('say "yes" right after I ask, or tap Approve on the '
                       'dashboard') if r.get("action") in spoken else \
                    "tap Approve on the dashboard"
                lines.append(f"  - {r.get('label', r['entity_id'])}: {how}")
            return ("I can't approve these myself. Waiting for you:\n"
                    + "\n".join(lines))

        @tool
        def sentinel_deny_device(device_name: str = "") -> str:
            """Record the USER's refusal of a Sentinel consent request.

            ONLY call this after the user explicitly declined a Sentinel
            permission question. Leave the argument empty if only one
            request is open.
            """
            from puffpai.security import consent_requests as _creq
            req = _creq.find_open(device_name)
            if req is None:
                return "There are no open Sentinel consent requests matching that."
            _creq.resolve(req["entity_id"], approved=False, by="user_via_chat")
            return (f"Understood — {req.get('label', req['entity_id'])} stays "
                    f"unapproved. Sentinel logged the denial.")

        @tool
        def sentinel_list_requests(dummy: str = "") -> str:
            """List Sentinel's open consent requests (devices waiting for the user's permission)."""
            from puffpai.security import consent_requests as _creq
            open_reqs = _creq.list_open()
            if not open_reqs:
                return "No open Sentinel consent requests."
            lines = [f"  - {r.get('label', r['entity_id'])} ({r['entity_id']}): "
                     f"{r.get('action')} — {r.get('reason','')}" for r in open_reqs[:10]]
            return f"{len(open_reqs)} open consent request(s):\n" + "\n".join(lines)

        @tool
        def dashboard_remove_camera(camera_name: str) -> str:
            """Remove a camera tile from the PuffPAI dashboard (does not touch Home Assistant)."""
            from puffpai import dashboard_registry as _dreg
            q = camera_name.strip().strip("'\"").lower()
            for entry in _dreg.list_cameras():
                hay = f"{entry.get('label','')} {entry['entity_id']}".lower().replace("_", " ")
                if q == entry["entity_id"].lower() or all(
                        t in hay for t in q.replace("_", " ").split()):
                    _dreg.remove_camera(entry["entity_id"])
                    return f"Removed {entry.get('label', entry['entity_id'])} from the dashboard."
            return f"No dashboard camera matches '{camera_name}'."

        @tool
        def privacy_block_device(device_info: str) -> str:
            """Block a device from internet access. Use IP address or device description."""
            if "|" in device_info:
                device_info = device_info.split("|")[-1].strip("'\"")
            device_info = device_info.strip("'\"")

            import re as _re
            ip_match = _re.search(r'\d+\.\d+\.\d+\.\d+', device_info)
            if not ip_match:
                return f"Cannot find IP address in: {device_info}. Please provide an IP."

            ip = ip_match.group(0)

            # Try the automation engine first, fall back to local firewall
            from puffpai.automation_client import AutomationClient
            automation = AutomationClient()
            ok, data = automation.call("block_device", {
                "action": "block", "ip": ip,
                "reason": "User requested via PuffPAI",
            })
            if ok:
                return f"✅ Blocked {ip} from internet access (via Node-RED)"

            # Fallback: direct firewall
            from puffpai.firewall_manager import get_firewall_backend
            fw = get_firewall_backend()
            fw_result = fw.block_device(ip, "User requested via PuffPAI")
            if fw_result.get('success'):
                return f"✅ Blocked {ip} from internet access (local firewall)"
            return f"❌ Failed to block {ip}: {fw_result.get('message', 'Unknown error')}"
        
        @tool
        def privacy_unblock_device(ip_address: str) -> str:
            """Unblock a device and restore its internet access."""

            if "=" in ip_address:
                ip_address = ip_address.split("=")[-1].strip("'\"")
            ip_address = ip_address.strip("'\"")

            # Try the automation engine first
            from puffpai.automation_client import AutomationClient
            automation = AutomationClient()
            ok, data = automation.call("block_device", {
                "action": "unblock", "ip": ip_address,
            })
            if ok:
                return f"✅ Unblocked {ip_address} - internet access restored (via Node-RED)"

            # Fallback: direct firewall
            from puffpai.firewall_manager import get_firewall_backend
            fw = get_firewall_backend()
            fw_result = fw.unblock_device(ip_address)
            if fw_result.get('success'):
                return f"✅ Unblocked {ip_address} - internet access restored (local firewall)"
            return f"❌ Failed to unblock {ip_address}: {fw_result.get('message', 'Unknown error')}"
        
        @tool
        def privacy_lockdown_mode(enable: str = "true") -> str:
            """Enable or disable network lockdown mode (blocks ALL internet traffic)."""

            if "=" in enable:
                enable = enable.split("=")[-1].strip("'\"")
            enable_bool = enable.lower() in ['true', 'on', 'enable', '1', 'yes']

            # Try the automation engine first
            from puffpai.automation_client import AutomationClient
            automation = AutomationClient()
            ok, data = automation.call("block_device", {
                "action": "lockdown", "enable": enable_bool,
            })
            if ok:
                mode = "enabled" if enable_bool else "disabled"
                return f"🔒 Network lockdown mode {mode} (via Node-RED)"

            # Fallback: direct firewall
            from puffpai.firewall_manager import get_firewall_backend
            fw = get_firewall_backend()
            fw_result = fw.lockdown_mode(enable_bool)
            if fw_result.get('success'):
                mode = "enabled" if enable_bool else "disabled"
                return f"🔒 Network lockdown mode {mode} (local firewall)"
            return f"❌ Failed to change lockdown mode: {fw_result.get('message', 'Unknown error')}"

        @tool
        def privacy_scan_network(dummy: str = "") -> str:
            """Scan the local network for all connected devices and identify suspicious activity."""
            import subprocess
            import re as _re

            try:
                result = subprocess.run(
                    ['arp', '-a'], capture_output=True, text=True, shell=False
                )
                output = result.stdout

                devices = []
                for line in output.split('\n'):
                    ip_match = _re.search(r'(\d+\.\d+\.\d+\.\d+)', line)
                    mac_match = _re.search(
                        r'([0-9a-fA-F]{2}[:-]){5}([0-9a-fA-F]{2})', line
                    )
                    if ip_match:
                        devices.append({
                            'ip': ip_match.group(1),
                            'mac': mac_match.group(0) if mac_match else 'unknown',
                            'raw': line.strip()
                        })

                # Try the automation engine for enriched analysis
                from puffpai.automation_client import AutomationClient
                automation = AutomationClient()
                ok, data = automation.call("network_scan", {
                    'devices': devices, 'count': len(devices),
                })
                if ok and isinstance(data, dict):
                    total = data.get('total_devices', len(devices))
                    trusted = data.get('trusted_count', 0)
                    needs_review = data.get('needs_review_count', 0)

                    result_text = (
                        f"Network scan complete (via Node-RED).\n\n"
                        f"Total devices: {total}\n"
                        f"Trusted: {trusted}\n"
                        f"Needs review: {needs_review}"
                    )
                    if needs_review > 0:
                        result_text += "\n\nDevices needing review:"
                        for device in data.get('needs_review', []):
                            result_text += (
                                f"\n- {device.get('ip', '?')} "
                                f"({device.get('type', 'Unknown')})"
                            )
                    return result_text

                # Fallback: return raw device list
                if not devices:
                    return "No devices found on network."

                result_text = f"Network scan complete (local).\n"
                result_text += f"Found {len(devices)} devices:\n\n"
                for d in devices:
                    result_text += f"- IP: {d['ip']}  MAC: {d['mac']}\n"

                return result_text

            except Exception as e:
                return f"Network scan error: {str(e)}"

        # ── Media Server (Jellyfin + Calibre-Web on Pi 4) ───────────────────

        @tool
        def media_search_music(query: str) -> str:
            """Search the music library by artist, album, genre, or free text. Returns matching tracks."""
            from puffpai.media_bridge import get_media_bridge, MediaBridgeError
            if "=" in query:
                query = query.split("=")[-1].strip("'\"")
            query = query.strip("'\"")

            try:
                bridge = get_media_bridge()
                items = bridge.search_music(query)
            except MediaBridgeError as exc:
                return f"Error: {exc}"

            if not items:
                return f"No music found for '{query}'."
            lines = [f"{i.display} [{i.item_id[:8]}]" for i in items[:15]]
            return f"Found {len(items)} tracks:\n" + "\n".join(lines)

        @tool
        def media_search_movies(query: str) -> str:
            """Search the movie library by title, genre, or year."""
            from puffpai.media_bridge import get_media_bridge, MediaBridgeError
            if "=" in query:
                query = query.split("=")[-1].strip("'\"")
            query = query.strip("'\"")

            # Map genre words ("sci-fi", "comedies") to Jellyfin's genre filter.
            # A plain title search for "sci-fi" matches no movie *title*, so genre
            # queries came back empty. Only fires when the whole query IS a genre
            # term, so a film literally named "Drama" still searches by title.
            _GENRES = {
                "action": "Action", "adventure": "Adventure",
                "animation": "Animation", "anime": "Animation",
                "comedy": "Comedy", "comedies": "Comedy", "crime": "Crime",
                "documentary": "Documentary", "documentaries": "Documentary",
                "drama": "Drama", "dramas": "Drama", "family": "Family",
                "fantasy": "Fantasy", "history": "History",
                "historical": "History", "horror": "Horror",
                "musical": "Music", "mystery": "Mystery",
                "romance": "Romance", "romantic": "Romance",
                "rom com": "Romance", "romcom": "Romance",
                "sci-fi": "Science Fiction", "scifi": "Science Fiction",
                "sci fi": "Science Fiction", "science fiction": "Science Fiction",
                "thriller": "Thriller", "thrillers": "Thriller", "war": "War",
                "western": "Western", "westerns": "Western",
            }
            _gq = query.lower().strip()
            for _sfx in (" movies", " movie", " films", " film", " flicks"):
                if _gq.endswith(_sfx):
                    _gq = _gq[: -len(_sfx)].strip()
                    break
            genre = _GENRES.get(_gq, "")

            try:
                bridge = get_media_bridge()
                if genre:
                    items = bridge.search_movies("", genre=genre)
                else:
                    items = bridge.search_movies(query)
            except MediaBridgeError as exc:
                return f"Error: {exc}"

            if not items:
                return f"No movies found for '{query}'."
            lines = [f"{i.display} [{i.item_id[:8]}]" for i in items[:15]]
            return f"Found {len(items)} movies:\n" + "\n".join(lines)

        @tool
        def media_search_books(query: str) -> str:
            """Search the book library by title or author."""
            from puffpai.media_bridge import get_media_bridge, MediaBridgeError
            if "=" in query:
                query = query.split("=")[-1].strip("'\"")
            query = query.strip("'\"")

            try:
                bridge = get_media_bridge()
                items = bridge.search_books(query)
            except MediaBridgeError as exc:
                return f"Error: {exc}"

            if not items:
                return f"No books found for '{query}'."
            lines = [f"{i.title} by {i.artist} [{i.item_id}]" for i in items[:15]]
            return f"Found {len(items)} books:\n" + "\n".join(lines)

        # ── Librarian: the Calibre catalogue + Calibre-web's own shelves ──
        # Every one of these is a thin call into puffpai.books.librarian, which
        # answers ONLY from real rows (see its module docstring) and performs a
        # single-book shelf change immediately while asking first about a bulk
        # one. Nothing here composes a sentence of its own.

        @tool
        def books_find(query: str) -> str:
            """Search the household BOOK/comic library (Calibre) by title, author, category or comic series. Format: 'plain words', or 'author=Stephen King', 'category=survival', 'series=Ultimate X-Men', 'comics=yes'."""
            from puffpai.books import librarian as _lib
            kwargs, plain = _lib.parse_args(query)
            return _lib.do_find(plain, author=kwargs.get("author", ""),
                                category=kwargs.get("category", ""),
                                subject=kwargs.get("subject", ""),
                                series=kwargs.get("series", ""),
                                comics_only=_lib.parse_flag(kwargs.get("comics")),
                                mine_only=_lib.parse_flag(kwargs.get("mine")))

        @tool
        def books_recommend(query: str) -> str:
            """Recommend something to read from the household book library. Format: '' for anything, or 'category=mystery', 'author=Dickens', 'like=Dracula', 'shelf=to read'."""
            from puffpai.books import librarian as _lib
            kwargs, plain = _lib.parse_args(query)
            category = kwargs.get("category", "") or plain
            return _lib.do_recommend(category=category,
                                     author=kwargs.get("author", ""),
                                     like=kwargs.get("like", ""),
                                     shelf=kwargs.get("shelf", ""))

        @tool
        def books_shelves(shelf: str = "") -> str:
            """List the household's book shelves, or what is on ONE of them. Pass the shelf name, or nothing for all shelves."""
            from puffpai.books import librarian as _lib
            _kwargs, plain = _lib.parse_args(shelf)
            return _lib.do_shelves(plain)

        @tool
        def books_shelf_add(book_and_shelf: str) -> str:
            """Put a book on a book shelf. Format: 'book title|shelf name' (e.g. 'Dracula|to read'). Creates the shelf if it doesn't exist."""
            from puffpai.books import librarian as _lib
            what, shelf = _lib.parse_pair(book_and_shelf)
            if not shelf:
                return "Which shelf should it go on?"
            return _lib.do_shelf_add(what, shelf)

        @tool
        def books_shelf_remove(book_and_shelf: str) -> str:
            """Take a book off a book shelf. Format: 'book title|shelf name'."""
            from puffpai.books import librarian as _lib
            what, shelf = _lib.parse_pair(book_and_shelf)
            if not shelf:
                return "Which shelf should it come off?"
            return _lib.do_shelf_remove(what, shelf)

        @tool
        def books_shelf_move(book_from_to: str) -> str:
            """Move a book from one shelf to another. Format: 'book title|from shelf|to shelf' (the from shelf may be empty)."""
            parts = [p.strip() for p in str(book_from_to or "").split("|")]
            from puffpai.books import librarian as _lib
            if len(parts) < 2 or not parts[0]:
                return "Tell me the book and which shelf to move it to."
            if len(parts) == 2:
                return _lib.do_shelf_move(parts[0], "", parts[1])
            return _lib.do_shelf_move(parts[0], parts[1], parts[2])

        @tool
        def books_show(book: str) -> str:
            """Pull a book or comic up on the dashboard/kiosk screen in the reader. Pass the title exactly as the user said it."""
            from puffpai.books import librarian as _lib
            said = _lib.do_show(f"show me {book}".strip())
            return said or _lib.do_find(str(book or "").strip())

        @tool
        def media_play(item_id_and_client: str) -> str:
            """Play ONE song. Pass the song name (and artist) EXACTLY as the user said it — never a placeholder like 'song title'. For whole playlists use media_play_playlist instead. Format: 'song name' or 'song name|session_id'."""
            from puffpai.media_bridge import get_media_bridge, MediaBridgeError
            if "=" in item_id_and_client:
                item_id_and_client = item_id_and_client.split("=")[-1].strip("'\"")
            item_id_and_client = item_id_and_client.strip("'\"")

            parts = item_id_and_client.split("|")
            item_id = parts[0].strip()
            session_id = parts[1].strip() if len(parts) > 1 else ""

            try:
                bridge = get_media_bridge()
                # Not a full 32-char Jellyfin ID? Treat it as a search query.
                if len(item_id) != 32:
                    matches = bridge.search_music(item_id, limit=1)
                    if not matches:
                        return f"Error: no track found matching '{item_id}'."
                    item_id = matches[0].item_id
                result = bridge.play_music(item_id, session_id)
                return f"Playing: {result}"
            except MediaBridgeError as exc:
                return f"Error: {exc}"

        @tool
        def media_now_playing(dummy: str = "") -> str:
            """Show what's currently playing on all devices and sources."""
            # Asking JELLYFIN alone would answer a live World Radio / TuneIn
            # stream (real mpv process, real Pulse sink-input) with "Nothing
            # is playing right now." puffpai.now_playing reads every source from its actual
            # player state; the Jellyfin detail below is kept as the richer
            # answer whenever Jellyfin is in fact the thing playing.
            from puffpai.media_bridge import get_media_bridge, MediaBridgeError
            from puffpai.now_playing import describe_playback, observed_playback

            try:
                entries = observed_playback()
            except Exception:                             # pragma: no cover
                entries = []
            jellyfin_only = bool(entries) and all(
                e.get("source") == "Jellyfin" for e in entries)
            # Pass `entries` back in rather than re-probing: observed_playback
            # can spend real wall clock probing, and one spoken turn must not
            # pay that twice.
            if entries and not jellyfin_only:
                return describe_playback(entries=entries)

            try:
                bridge = get_media_bridge()
                sessions = bridge.now_playing()
            except MediaBridgeError as exc:
                # Jellyfin being unreachable is not evidence of silence.
                return (describe_playback(entries=entries) if entries
                        else f"Error: {exc}")

            lines = []
            for s in sessions:
                if s.now_playing:
                    status = "paused" if s.is_paused else "playing"
                    mins = s.position_seconds // 60
                    secs = s.position_seconds % 60
                    lines.append(
                        f"{s.device_name}: {s.now_playing.display} "
                        f"({status}, {mins}:{secs:02d} in)"
                    )
            # Idle sessions are noise (especially spoken aloud) — only
            # mention devices that actually have something loaded.
            if not lines:
                # Never claim silence without the cross-source check.
                return describe_playback(entries=entries)
            return "\n".join(lines)

        @tool
        def media_control(session_and_action: str) -> str:
            """Control playback: pause, resume, stop, next, or previous. To resume/unpause paused music just pass 'resume'. Pass the action alone (e.g. 'resume') — the active player is picked automatically. Optional format 'session_id|action' to target a specific session."""
            from puffpai.media_bridge import get_media_bridge, MediaBridgeError
            if "=" in session_and_action:
                session_and_action = session_and_action.split("=")[-1].strip("'\"")
            session_and_action = session_and_action.strip("'\"")

            parts = session_and_action.split("|")
            if len(parts) == 2:
                session_id, action = parts[0].strip(), parts[1].strip()
            else:
                session_id, action = "", parts[0].strip()
            if not action:
                return "Error: pass an action: pause, resume, stop, next, or previous"

            try:
                bridge = get_media_bridge()
                result = bridge.playback_control(session_id, action)
                return result
            except MediaBridgeError as exc:
                return f"Error: {exc}"

        @tool
        def media_library_stats(dummy: str = "") -> str:
            """Show media library statistics: total tracks, albums, movies, books."""
            from puffpai.media_bridge import get_media_bridge, MediaBridgeError

            try:
                bridge = get_media_bridge()
                stats = bridge.library_stats()
            except MediaBridgeError as exc:
                return f"Error: {exc}"

            return (
                f"Music: {stats['music_tracks']} tracks, "
                f"{stats['music_albums']} albums, "
                f"{stats['music_artists']} artists\n"
                f"Movies: {stats['movies']}\n"
                f"Books: {stats['books']}"
            )

        @tool
        def media_recommend(mood: str) -> str:
            """Recommend music, movies, or books based on a mood or context description."""
            from puffpai.media_bridge import get_media_bridge, MediaBridgeError
            if "=" in mood:
                mood = mood.split("=")[-1].strip("'\"")
            mood = mood.strip("'\"")

            try:
                bridge = get_media_bridge()
                stats = bridge.library_stats()
                # Search for relevant items based on mood keywords
                music = bridge.search_music(mood, limit=5)
                movies = bridge.search_movies(mood, limit=3)
            except MediaBridgeError as exc:
                return f"Error: {exc}"

            # Build a prompt for the LLM to make recommendations
            available = []
            if music:
                available.append("Music: " + ", ".join(m.display for m in music))
            if movies:
                available.append("Movies: " + ", ".join(m.display for m in movies))

            if not available:
                return f"No items matching mood '{mood}' found in the library."

            prompt = (
                f"The user wants media for this mood: '{mood}'. "
                f"Available in their library:\n" + "\n".join(available) +
                "\n\nPick the best match and explain why in 1-2 sentences."
            )
            return self._llm_chat(prompt)

        # ── Media Server: playlists & preferences ───────────────────────────

        def _media_find_track(bridge, query: str):
            """Resolve a free-text song query to the best matching track."""
            matches = bridge.search_music(query.strip(), limit=1)
            return matches[0] if matches else None

        def _taste_playlist_reply(name: str, artists: list) -> str:
            """Build the playlist and say what really happened, out loud.

            Both taste tools compose their answer here so a partial result —
            seven of nine artists found — is always reported as a partial
            result, and never as a plain success.
            """
            from puffpai.media_bridge import get_media_bridge
            try:
                built = get_media_bridge().build_taste_playlist(name, artists)
            except Exception as exc:            # build_ never raises; belt+braces
                return f"Error: {exc}"
            missing = built.get("missing") or []
            matched = built.get("matched") or []
            if not built.get("ok"):
                if missing and not matched:
                    return ("None of those artists are in your music library: "
                            + ", ".join(missing[:8])
                            + ". I didn't make a playlist.")
                return (f"I couldn't make that playlist: "
                        f"{built.get('error') or 'unknown error'}.")
            total = len(matched) + len(missing)
            if built.get("partial"):
                # A playlist EXISTS with part of the tracks in it. Saying so is
                # the only reply that does not invite a duplicate retry.
                parts = [f"Made '{built['name']}' with the first "
                         f"{built['track_count']} tracks; "
                         f"the rest didn't save."]
            else:
                parts = [f"Made '{built['name']}' with {built['track_count']} "
                         f"tracks from {len(matched)} of your {total} artists."]
            if missing:
                parts.append("Not in your library: "
                             + ", ".join(missing[:8]) + ".")
            if built.get("similar_added"):
                parts.append(f"Added {built['similar_added']} similar tracks.")
            parts.append(f"Say 'play my {built['name']} playlist' to hear it.")
            return " ".join(parts)

        @tool
        def media_play_playlist(playlist_name: str, shuffle: bool = False) -> str:
            """Play an entire playlist. Pass the playlist name the user said, verbatim (partial names OK). Set shuffle=True if the user asks for shuffle/random order. If this returns an Error, tell the user what it says — do NOT claim music is playing."""
            from puffpai.media_bridge import get_media_bridge, MediaBridgeError
            if "=" in playlist_name:
                playlist_name = playlist_name.split("=")[-1].strip("'\"")
            playlist_name = playlist_name.strip("'\"")
            if isinstance(shuffle, str):
                shuffle = shuffle.strip().lower() in ("true", "1", "yes", "on")
            try:
                bridge = get_media_bridge()
                return bridge.play_playlist(playlist_name, shuffle=shuffle)
            except MediaBridgeError as exc:
                return f"Error: {exc}"

        @tool
        def media_list_playlists(dummy: str = "") -> str:
            """List all music playlists with their track counts."""
            from puffpai.media_bridge import get_media_bridge, MediaBridgeError
            try:
                bridge = get_media_bridge()
                playlists = bridge.list_playlists()
            except MediaBridgeError as exc:
                return f"Error: {exc}"
            if not playlists:
                return "No playlists found."
            lines = [
                f"{p.title} ({p.extra.get('track_count', 0)} tracks)"
                for p in playlists
            ]
            return f"{len(playlists)} playlists:\n" + "\n".join(lines)

        @tool
        def media_playlist_tracks(playlist: str) -> str:
            """Show the tracks in a playlist. Give the playlist name (partial is OK)."""
            from puffpai.media_bridge import get_media_bridge, MediaBridgeError
            if "=" in playlist:
                playlist = playlist.split("=")[-1].strip("'\"")
            playlist = playlist.strip("'\"")

            try:
                bridge = get_media_bridge()
                tracks = bridge.playlist_tracks(playlist, limit=100)
            except MediaBridgeError as exc:
                return f"Error: {exc}"
            if not tracks:
                return f"Playlist '{playlist}' is empty."
            lines = [t.display for t in tracks[:40]]
            more = f"\n...and {len(tracks) - 40} more" if len(tracks) > 40 else ""
            return f"{len(tracks)} tracks:\n" + "\n".join(lines) + more

        @tool
        def media_playlist_create(name_and_songs: str) -> str:
            """Create a new playlist. Format: 'playlist name' or 'playlist name|song one; song two' to seed it with tracks."""
            from puffpai.media_bridge import get_media_bridge, MediaBridgeError
            if "=" in name_and_songs.split("|")[0]:
                name_and_songs = name_and_songs.split("=", 1)[-1].strip("'\"")
            name_and_songs = name_and_songs.strip("'\"")

            parts = name_and_songs.split("|")
            name = parts[0].strip()
            if not name:
                return "Error: playlist name is required."

            try:
                bridge = get_media_bridge()
                found, missing = [], []
                if len(parts) > 1:
                    for q in parts[1].split(";"):
                        q = q.strip()
                        if not q:
                            continue
                        track = _media_find_track(bridge, q)
                        (found.append(track) if track else missing.append(q))
                bridge.create_playlist(name, [t.item_id for t in found])
            except MediaBridgeError as exc:
                return f"Error: {exc}"

            msg = f"Created playlist '{name}' with {len(found)} track(s)."
            if found:
                msg += "\n" + "\n".join(t.display for t in found)
            if missing:
                msg += "\nNot found: " + "; ".join(missing)
            return msg

        @tool
        def media_playlist_add(playlist_and_song: str) -> str:
            """Add a song to a playlist. Format: 'playlist name|song search terms'."""
            from puffpai.media_bridge import get_media_bridge, MediaBridgeError
            playlist_and_song = playlist_and_song.strip("'\"")
            parts = playlist_and_song.split("|")
            if len(parts) != 2:
                return "Error: use format 'playlist name|song search terms'."
            playlist, song_q = parts[0].strip(), parts[1].strip()

            try:
                bridge = get_media_bridge()
                track = _media_find_track(bridge, song_q)
                if not track:
                    return f"Error: no track found matching '{song_q}'."
                bridge.playlist_add(playlist, [track.item_id])
                return f"Added {track.display} to '{playlist}'."
            except MediaBridgeError as exc:
                return f"Error: {exc}"

        @tool
        def media_playlist_remove(playlist_and_song: str) -> str:
            """Remove a song from a playlist. Format: 'playlist name|song title'."""
            from puffpai.media_bridge import get_media_bridge, MediaBridgeError
            playlist_and_song = playlist_and_song.strip("'\"")
            parts = playlist_and_song.split("|")
            if len(parts) != 2:
                return "Error: use format 'playlist name|song title'."
            playlist, song_q = parts[0].strip(), parts[1].strip().lower()

            try:
                bridge = get_media_bridge()
                tracks = bridge.playlist_tracks(playlist, limit=1000)
                hits = [t for t in tracks if song_q in t.title.lower()]
                if not hits:
                    return f"Error: no track matching '{song_q}' in '{playlist}'."
                entry_ids = [t.extra["entry_id"] for t in hits if t.extra.get("entry_id")]
                bridge.playlist_remove(playlist, entry_ids)
                removed = "\n".join(t.display for t in hits)
                return f"Removed from '{playlist}':\n{removed}"
            except MediaBridgeError as exc:
                return f"Error: {exc}"

        @tool
        def media_list_artists(query: str = "") -> str:
            """List the artists in the Jellyfin music library.

            Use for "what artists do I have", "who is in my music library",
            "do I have any Coldplay". Pass a name to narrow the list.
            """
            from puffpai.media_bridge import get_media_bridge
            try:
                artists = get_media_bridge().list_artists(query=query.strip())
            except Exception as exc:
                return f"Error: {exc}"
            if not artists:
                return ("No artists matched." if query.strip()
                        else "There are no artists in the music library yet.")
            names = sorted(a["name"] for a in artists)
            head = ", ".join(names[:15])
            more = (f", and {len(names) - 15} more"
                    if len(names) > 15 else "")
            return (f"{len(names)} artist{'s' if len(names) != 1 else ''} "
                    f"in your library: {head}{more}.")

        @tool
        def media_playlist_from_artists(spec: str) -> str:
            """Build a Jellyfin playlist from a list of artists, plus similar
            music from the library. Format: 'playlist name|artist one, artist
            two, artist three'.

            Use for "make me a playlist of Coldplay, Glass Animals and Alt-J".
            """
            name, _, artist_text = str(spec or "").strip("'\"").partition("|")
            name = name.strip()
            if not name or not artist_text.strip():
                return ("Error: use format 'playlist name|artist one, "
                        "artist two'.")
            from puffpai.media_bridge import parse_artist_names
            return _taste_playlist_reply(name,
                                         parse_artist_names(artist_text))

        @tool
        def media_playlist_from_document(spec: str) -> str:
            """Build a Jellyfin playlist from the artists listed in a file the
            user sent (a screenshot of their stations, a typed list). Format:
            'document name' or 'document name|playlist name'.

            Use for "make a playlist from the list I sent you".
            """
            doc_ref, _, name = str(spec or "").strip("'\"").partition("|")
            doc_ref, name = doc_ref.strip(), name.strip()
            if not doc_ref:
                return "Error: name the document to read the artists from."
            from puffpai import document_lookup
            entry = document_lookup.resolve(doc_ref)
            if not entry:
                titles = ", ".join(document_lookup.known_titles(3))
                return (f"I don't have a file called '{doc_ref}'."
                        + (f" The most recent ones are {titles}."
                           if titles else ""))
            # The FULL text, not fetch_my_document's 6000-char display cut: a
            # list of favourites is exactly the kind of file whose tail matters.
            body = document_lookup.text_of(entry, limit=200000)
            if not body.strip():
                return (f"I have {entry.get('filename')}, but no readable text "
                        f"was extracted from it, so there are no artist names "
                        f"to work from.")
            from puffpai.media_bridge import parse_artist_names
            artists = parse_artist_names(body)
            if not artists:
                return (f"I read {entry.get('filename')} but couldn't pick any "
                        f"artist names out of it.")
            if not name:
                import os as _os
                stem = _os.path.splitext(str(entry.get('title')
                                             or entry.get('filename')))[0]
                name = f"{stem} Mix"
            return _taste_playlist_reply(name, artists)

        @tool
        def media_favorite(song_and_flag: str) -> str:
            """Mark a song as a favorite (or unmark it). Format: 'song search terms' or 'song search terms|off'."""
            from puffpai.media_bridge import get_media_bridge, MediaBridgeError
            song_and_flag = song_and_flag.strip("'\"")
            parts = song_and_flag.split("|")
            song_q = parts[0].strip()
            unfavorite = len(parts) > 1 and parts[1].strip().lower() in ("off", "no", "remove", "false")

            try:
                bridge = get_media_bridge()
                track = _media_find_track(bridge, song_q)
                if not track:
                    return f"Error: no track found matching '{song_q}'."
                bridge.set_favorite(track.item_id, favorite=not unfavorite)
                verb = "Unfavorited" if unfavorite else "Favorited"
                return f"{verb}: {track.display}"
            except MediaBridgeError as exc:
                return f"Error: {exc}"

        @tool
        def media_list_favorites(dummy: str = "") -> str:
            """List the user's favorite music tracks."""
            from puffpai.media_bridge import get_media_bridge, MediaBridgeError
            try:
                bridge = get_media_bridge()
                favs = bridge.list_favorites(limit=40)
            except MediaBridgeError as exc:
                return f"Error: {exc}"
            if not favs:
                return "No favorite tracks yet."
            return f"{len(favs)} favorites:\n" + "\n".join(t.display for t in favs)

        # ── Media Hub — the non-Jellyfin sources ────────────────────
        # Bodies live in puffpai.media_hub; these are registration shims.

        @tool
        def media_radio_play(query: str) -> str:
            """Play internet radio. Search by station NAME, GENRE, COUNTRY or LANGUAGE, or any combination — 'kexp', 'jazz', 'french jazz', 'news from japan', 'stations in spanish'. Repeating the same genre gives a DIFFERENT station each time on purpose; a station asked for by name always plays that station. Pass the household's own words; do not substitute a station you happen to know."""
            from puffpai.media_hub import tool_radio_play
            return tool_radio_play(query)

        @tool
        def media_radio_stations(query: str = "") -> str:
            """Find internet radio stations WITHOUT playing one. Use when asked what stations exist, what they can listen to, or to find stations by genre, country or language ('what jazz stations are there', 'radio from Ireland'). Returns a numbered shortlist with country and bitrate plus any saved favorites; the household can then say 'play the second one'."""
            from puffpai.media_hub import tool_radio_stations
            return tool_radio_stations(query)

        @tool
        def media_radio_favorites(action: str = "list", name: str = "") -> str:
            """Manage the household's saved radio stations. action='list' names them, action='save' saves whatever is playing on the appliance right now, action='remove' with a name drops one. Use for 'what are my favorite stations', 'save this station', 'remove X from my favorites'."""
            from puffpai.media_hub import tool_radio_favorites
            return tool_radio_favorites(action, name)

        @tool
        def media_radio_stop(dummy: str = "") -> str:
            """Stop internet radio playback on the appliance screen."""
            from puffpai.media_hub import tool_radio_stop
            return tool_radio_stop()

        @tool
        def media_sources_overview(dummy: str = "") -> str:
            """Summarize every audio source on PuffPAI at once — Jellyfin, internet radio, and Spotify Connect. Use when asked what is playing, what music options exist, or why a source is not showing up."""
            from puffpai.media_hub import tool_sources_overview
            return tool_sources_overview()

        # ── Spotify voice control — bodies live in puffpai.spotify_control ──
        # These drive the SIGNED-IN shared account (docker-compose.spotify-voice),
        # not the zeroconf casting device. Play music by voice, no phone needed.

        @tool
        def spotify_play(query: str) -> str:
            """Play music on Spotify through PuffPAI by voice. Give a playlist name, artist, album, song, or genre (e.g. 'Discover Weekly', 'play the Beatles', 'jazz'). Starts on PuffPAI's signed-in Spotify speaker."""
            from puffpai.spotify_control import tool_spotify_play
            return tool_spotify_play(query)

        @tool
        def spotify_pause(dummy: str = "") -> str:
            """Pause Spotify playback on PuffPAI."""
            from puffpai.spotify_control import tool_spotify_pause
            return tool_spotify_pause()

        @tool
        def spotify_resume(dummy: str = "") -> str:
            """Resume paused Spotify playback on PuffPAI."""
            from puffpai.spotify_control import tool_spotify_resume
            return tool_spotify_resume()

        @tool
        def spotify_next(dummy: str = "") -> str:
            """Skip to the next Spotify track on PuffPAI."""
            from puffpai.spotify_control import tool_spotify_next
            return tool_spotify_next()

        @tool
        def spotify_whats_playing(dummy: str = "") -> str:
            """Say what's currently playing on PuffPAI's Spotify, and whether Spotify is set up and signed in."""
            from puffpai.spotify_control import tool_spotify_status
            return tool_spotify_status()

        # ── Zone 3 / the Bridge: network edge security ─────────────────────
        #
        # Every tool below goes through puffpai.zone3.engine.get_engine(), the
        # SAME singleton the /zone3/* HTTP routes and the dashboard use, so
        # the spoken answers and the dashboard describe the same AdGuard
        # engine: one engine, one answer.
        #
        # Read tools answer. State-changing tools PROPOSE and stop: flipping the
        # household's DNS filter is the owner's call, and a tool the LLM can
        # call is exactly the surface a prompt injection reaches. The owner
        # confirms on the dashboard, which mints and redeems a single-use
        # consent ticket server-side (POST /zone3/confirm -> POST /zone3/...),
        # and that confirmation path is the only thing that actuates.

        def _zone3_engine():
            """The shared engine, or None when Zone 3 is off."""
            from puffpai.zone3.engine import get_engine
            return get_engine()

        def _zone3_off_message() -> str:
            return ("Zone 3 — the Bridge — isn't running, so I'm not watching "
                    "or filtering any network traffic right now.")

        def _zone3_posture() -> tuple[bool, str]:
            """(enforcing, one-line description of the posture)."""
            from puffpai.zone3.engine import enforce_source, resolve_enforce
            enforcing = resolve_enforce()
            where = enforce_source()
            if enforcing:
                return True, f"Enforcing — rules are live (set in {where})."
            return False, (f"Observing — everything is logged, nothing is "
                           f"blocked (set in {where}).")

        @tool
        def zone3_status(dummy: str = "") -> str:
            """Zone 3 (the Bridge) status: whether PuffPAI is observing or enforcing at the network edge, whether the AdGuard sinkhole is reachable, and how many devices are managed. Use for 'zone 3 status', 'is my network being filtered', 'network edge status'."""
            try:
                engine = _zone3_engine()
                if engine is None:
                    return _zone3_off_message()
                status = engine.get_zone3_status()
                enforcing, posture = _zone3_posture()

                lines = ["Zone 3 — the Bridge", posture]

                ad = status.get("adguard")
                if not ad:
                    lines.append("AdGuard: not configured, so nothing is being "
                                 "filtered or logged.")
                elif ad.get("reachable"):
                    version = ad.get("version") or "unknown version"
                    lines.append(
                        f"AdGuard: reachable ({version}), protection "
                        f"{'on' if ad.get('protection_enabled') else 'off'}, "
                        f"{ad.get('active_puffpai_rules', 0)} PuffPAI rules "
                        f"active.")
                    upstreams = ad.get("upstreams") or []
                    if upstreams:
                        lines.append("Upstream DNS: " + ", ".join(upstreams[:3]))
                else:
                    why = ad.get("error")
                    lines.append(
                        "AdGuard: NOT reachable" + (f" ({why})" if why else "") +
                        " — so nothing is being filtered right now, whatever "
                        "the rules say.")

                lines.append(
                    f"Managed devices: {status.get('managed_devices', 0)} "
                    f"({status.get('safety_critical_count', 0)} safety-"
                    f"critical, always allowed).")
                modes = status.get("mode_distribution") or {}
                if modes:
                    lines.append("Modes: " + ", ".join(
                        f"{count} {mode}" for mode, count in sorted(modes.items())))
                return "\n".join(lines)
            except Exception as e:
                return f"I couldn't read Zone 3 status: {e}"

        @tool
        def zone3_discover_devices(dummy: str = "") -> str:
            """Scan the network for smart devices and classify each one, with the security mode PuffPAI would suggest. Use for 'find new devices', 'what smart devices are unmanaged', 'scan for devices to secure'."""
            try:
                engine = _zone3_engine()
                if engine is None:
                    return _zone3_off_message()
                devices = engine.discover_devices()
                if not devices:
                    return "I didn't find any devices on the network."

                managed = [d for d in devices if d.get("already_managed")]
                unmanaged = [d for d in devices if not d.get("already_managed")]
                lines = [f"{len(devices)} device(s) on the network — "
                         f"{len(managed)} managed, {len(unmanaged)} not."]

                if unmanaged:
                    lines.append("")
                    lines.append(f"Not yet secured ({len(unmanaged)}):")
                    for d in unmanaged[:20]:
                        mfr = d.get("manufacturer") or "unknown maker"
                        safety = " — safety-critical" if d.get("is_safety_critical") else ""
                        lines.append(
                            f"  {d.get('ip', '?')} · {mfr} · "
                            f"{d.get('category', 'unknown')}{safety} · "
                            f"suggested {str(d.get('suggested_mode', 'balanced')).upper()}")
                    if len(unmanaged) > 20:
                        lines.append(f"  ...and {len(unmanaged) - 20} more.")

                if managed:
                    lines.append("")
                    lines.append(f"Already secured ({len(managed)}):")
                    for d in managed[:20]:
                        mfr = d.get("manufacturer") or "unknown maker"
                        lines.append(f"  {d.get('ip', '?')} · {mfr} · "
                                     f"{str(d.get('current_mode', 'unknown')).upper()}")
                    if len(managed) > 20:
                        lines.append(f"  ...and {len(managed) - 20} more.")

                if unmanaged:
                    lines.append("")
                    lines.append("Say which one you'd like me to secure, and "
                                 "I'll put the proposal in front of you first.")
                return "\n".join(lines)
            except Exception as e:
                return f"The device scan failed: {e}"

        @tool
        def zone3_secure_device(device_info: str) -> str:
            """Propose securing a smart device at the network edge. Input: the device's IP address, or 'all' for every unmanaged device. While Zone 3 is enforcing this needs the owner's confirmation, so this returns the proposal rather than acting."""
            try:
                engine = _zone3_engine()
                if engine is None:
                    return _zone3_off_message()
                target = (device_info or "").strip()
                if not target:
                    return "Which device? Give me an IP address, or say 'all'."

                enforcing, posture = _zone3_posture()

                if target.lower() == "all":
                    unmanaged = engine.get_unmanaged_devices()
                    if not unmanaged:
                        return "Every device on the network is already managed."
                    if enforcing:
                        return (
                            f"That would put {len(unmanaged)} device(s) under "
                            f"BALANCED filtering and start blocking their "
                            f"telemetry. Zone 3 is enforcing, so this one is "
                            f"yours to approve — confirm it on the Firewall "
                            f"page and I'll apply it. Nothing has changed yet.")
                    results = []
                    for dev in unmanaged:
                        # recorded_by="tool", push omitted (False): this path
                        # writes the LEDGER and nothing else. Calling
                        # sync_dns() here would let a chat turn rewrite the
                        # household's AdGuard rule block and its client entries
                        # with no consent step anywhere in the call.
                        r = engine.secure_device(dev["ip"], dev["mac"],
                                                 dev.get("hostname", ""),
                                                 recorded_by="tool")
                        mark = "ok" if r.get("success") else "failed"
                        results.append(f"  {dev['ip']} — {mark}")
                    return (f"Recorded a policy for {len(unmanaged)} device(s). "
                            f"{posture} Nothing is actually blocked until you "
                            f"turn enforcement on.\n" + "\n".join(results))

                devices = engine.discover_devices()
                match = next((d for d in devices if d.get("ip") == target), None)
                if not match:
                    return (f"I don't see {target} on the network. Ask me to "
                            f"scan for devices first.")

                if enforcing:
                    mfr = match.get("manufacturer") or "unknown maker"
                    mode = str(match.get("suggested_mode", "balanced")).upper()
                    return (
                        f"Proposal: put {target} ({mfr}, "
                        f"{match.get('category', 'unknown')}) into {mode} mode "
                        f"and start blocking its telemetry. Zone 3 is "
                        f"enforcing, so that needs your confirmation — approve "
                        f"it on the Firewall page. Nothing has changed yet.")

                r = engine.secure_device(target, match.get("mac", ""),
                                         match.get("hostname", ""),
                                         recorded_by="tool")  # ledger only
                if r.get("success"):
                    return (f"Recorded {target} as "
                            f"{str(r.get('mode', 'balanced')).upper()} with "
                            f"{r.get('domains_recorded', 0)} domain(s) on its "
                            f"list. {posture} I have not pushed anything to "
                            f"AdGuard — nothing is blocked until you "
                            f"approve it on the Firewall page and turn "
                            f"enforcement on.")
                return f"I couldn't secure {target}: {r.get('message', 'unknown error')}"
            except Exception as e:
                return f"Securing that device failed: {e}"

        @tool
        def zone3_change_mode(mode_change: str) -> str:
            """Propose a different security mode for a managed device. Input: 'IP_ADDRESS MODE' where MODE is lockdown, balanced, open or always_allow — e.g. '192.168.1.50 lockdown'. While Zone 3 is enforcing this needs the owner's confirmation, so this returns the proposal rather than acting."""
            try:
                parts = (mode_change or "").strip().split()
                if len(parts) < 2:
                    return ("Tell me both parts: an IP address and a mode, like "
                            "'192.168.1.50 lockdown'. Modes are lockdown, "
                            "balanced, open, always_allow.")
                ip, mode_str = parts[0], parts[1].lower()

                engine = _zone3_engine()
                if engine is None:
                    return _zone3_off_message()

                from puffpai.zone3.policy_engine import SecurityMode
                try:
                    mode = SecurityMode(mode_str)
                except ValueError:
                    return (f"{mode_str} isn't a mode I know. Use lockdown, "
                            f"balanced, open or always_allow.")

                enforcing, posture = _zone3_posture()
                if enforcing:
                    return (
                        f"Proposal: change {ip} to {mode_str.upper()}. Zone 3 "
                        f"is enforcing, so that changes what actually reaches "
                        f"the internet and needs your confirmation — approve it "
                        f"on the Firewall page. Nothing has changed yet.")

                result = engine.change_device_mode(ip, mode,
                                                   recorded_by="tool")
                if result.get("success"):
                    return (f"Recorded {ip} as {mode_str.upper()}. {posture} "
                            f"Nothing is actually blocked until you turn "
                            f"enforcement on.")
                return f"I couldn't change {ip}: {result.get('message', 'unknown error')}"
            except Exception as e:
                return f"Changing that device's mode failed: {e}"

        @tool
        def zone3_enforce(setting: str = "") -> str:
            """Propose turning Zone 3 enforcement on or off — the switch between logging outbound traffic and actually blocking it. Input: 'on' or 'off'. This never flips the switch itself; it returns the proposal for the owner to confirm. Use for 'turn on enforcement', 'start blocking telemetry', 'stop filtering my network'."""
            try:
                engine = _zone3_engine()
                if engine is None:
                    return _zone3_off_message()

                want = (setting or "").strip().lower()
                if want in ("on", "enable", "enabled", "true", "yes", "enforce"):
                    want_on = True
                elif want in ("off", "disable", "disabled", "false", "no",
                              "observe", "monitor"):
                    want_on = False
                else:
                    _, posture = _zone3_posture()
                    return (f"{posture} Tell me 'on' or 'off' and I'll put the "
                            f"proposal in front of you.")

                enforcing, posture = _zone3_posture()
                if enforcing == want_on:
                    return f"Zone 3 is already there. {posture}"

                # The same preview the HTTP 409 renders, from the same helper,
                # so the spoken proposal and the dashboard proposal cannot
                # describe different changes (rule LINES, comments included,
                # versus recorded domains would be two numbers for one act).
                from puffpai.zone3.engine import enforce_preview
                preview = enforce_preview(engine)
                managed = preview["managed"]
                critical = preview["critical"]
                rules = preview["rules"]

                if want_on:
                    listing = ("\n" + "\n".join(f"  {ln}"
                                                for ln in preview["lines"])
                               if preview["lines"] else "")
                    return (
                        f"Proposal (needs your approval on the Firewall "
                        f"page): turn Zone 3 enforcement ON. That makes "
                        f"{rules} rule(s) live across {managed} managed "
                        f"device(s) — their telemetry and ad domains start "
                        f"being refused at DNS. {critical} safety-critical "
                        f"device(s) stay always-allowed. Here is exactly what "
                        f"goes live:{listing}\n"
                        f"Everything else keeps working normally. I can't flip "
                        f"this myself: approve it on the Firewall page and it "
                        f"takes effect, and it survives a restart. Nothing has "
                        f"changed yet.")
                return (
                    f"Proposal (needs your approval on the Firewall page): "
                    f"turn Zone 3 enforcement OFF. Every rule goes "
                    f"back to commented-out and AdGuard protection goes off, so "
                    f"{managed} device(s) stop being filtered — PuffPAI keeps "
                    f"logging what they reach out to, and blocks none of it. "
                    f"Approve it on the Firewall page. Nothing has changed yet.")
            except Exception as e:
                return f"I couldn't work out the enforcement proposal: {e}"

        # ── the Bridge inventory: what is on this network, and what it talks to

        def _bridge_parts():
            """(inventory, ledger) or (None, None) when the Bridge is off."""
            try:
                from puffpai.bridge_inventory import get_inventory
                from puffpai.bridge_ledger import get_ledger
                return get_inventory(), get_ledger()
            except Exception:
                return None, None

        def _bridge_find(inventory, needle: str):
            """Best device match for a name, IP or MAC. None when no match."""
            want = (needle or "").strip().lower()
            if not want:
                return None
            devices = inventory.devices()
            fields = ("ip", "mac", "key", "hostname", "ha_name", "adguard_name",
                      "display_name", "manufacturer")
            for device in devices:               # exact first
                for field in fields:
                    value = str(device.get(field) or "").lower()
                    if value and value == want:
                        return device
            for device in devices:               # then substring
                for field in fields:
                    value = str(device.get(field) or "").lower()
                    if value and want in value:
                        return device
            return None

        @tool
        def bridge_list_devices(filter_word: str = "") -> str:
            """List every device PuffPAI can see on the home network, grouped by kind, calling out anything new in the last 24 hours. Optional input: a word to filter by (a name, a maker, a category like 'camera', or an IP). Use for 'what devices are on my network', 'what's on my network', 'list my devices', 'any new devices'."""
            try:
                inventory, _ = _bridge_parts()
                if inventory is None:
                    return ("The Bridge device inventory isn't running, so I "
                            "can't see what's on the network.")
                devices = inventory.devices()
                if not devices:
                    return ("The device inventory is empty — I haven't managed "
                            "a scan yet. Ask me to refresh it.")

                needle = (filter_word or "").strip().lower()
                if needle:
                    devices = [
                        d for d in devices
                        if any(needle in str(d.get(f) or "").lower()
                               for f in ("ip", "mac", "hostname", "ha_name",
                                         "ha_area", "adguard_name",
                                         "manufacturer", "category",
                                         "display_name"))]
                    if not devices:
                        return f"Nothing on the network matches '{filter_word}'."

                from datetime import datetime, timedelta, timezone
                cutoff = (datetime.now(timezone.utc) - timedelta(hours=24))
                new_keys = set()
                for d in devices:
                    raw = str(d.get("first_seen") or "").replace("Z", "+00:00")
                    try:
                        seen = datetime.fromisoformat(raw)
                    except ValueError:
                        continue
                    if seen.tzinfo is None:
                        seen = seen.replace(tzinfo=timezone.utc)
                    if seen >= cutoff:
                        new_keys.add(d.get("key"))

                groups: dict = {}
                for d in devices:
                    groups.setdefault(d.get("category") or "other", []).append(d)

                header = f"{len(devices)} device(s) on the network"
                if needle:
                    header += f" matching '{filter_word}'"
                if new_keys:
                    header += f" — {len(new_keys)} new in the last 24 hours"
                lines = [header + "."]

                for category in sorted(groups):
                    members = groups[category]
                    lines.append("")
                    lines.append(f"{category.replace('_', ' ').title()} "
                                 f"({len(members)}):")
                    for d in sorted(members,
                                    key=lambda x: str(x.get("display_name") or "")):
                        name = d.get("display_name") or d.get("hostname") or d.get("ip")
                        bits = [str(d.get("ip") or "no IP")]
                        if d.get("manufacturer"):
                            bits.append(str(d["manufacturer"]))
                        if d.get("ha_area"):
                            bits.append(str(d["ha_area"]))
                        if d.get("is_gateway"):
                            bits.append("the router")
                        if d.get("is_self"):
                            bits.append("this is PuffPAI")
                        flag = "  NEW " if d.get("key") in new_keys else "  "
                        lines.append(f"{flag}{name} — " + " · ".join(bits))
                return "\n".join(lines)
            except Exception as e:
                return f"I couldn't read the device inventory: {e}"

        @tool
        def bridge_device_telemetry(device: str) -> str:
            """What one device on the network has been reaching out to — its top domains with what each is for, how many telemetry attempts it made, and how many were blocked or only monitored. Input: the device's name, IP address or MAC. Use for 'what is my TV talking to', 'who is my doorbell calling', 'what does the thermostat send out'."""
            try:
                inventory, ledger = _bridge_parts()
                if inventory is None or ledger is None:
                    return ("The Bridge telemetry ledger isn't running, so I "
                            "can't say what anything is talking to.")
                want = (device or "").strip()
                if not want:
                    return "Which device? Give me a name, an IP or a MAC."

                match = _bridge_find(inventory, want)
                if match is None:
                    return (f"I don't have '{want}' in the device inventory. "
                            f"Ask me what's on the network to see the names I "
                            f"know.")

                name = match.get("display_name") or match.get("ip") or want
                key = match.get("mac") or match.get("key") or match.get("ip") or ""
                summary = ledger.device_summary(key, 24)
                total = summary.get("total_queries", 0)
                if not total:
                    return (f"{name} hasn't shown up in the last 24 hours of "
                            f"DNS traffic — either it's been quiet, or its "
                            f"queries aren't reaching PuffPAI's resolver.")

                lines = [
                    f"{name} ({match.get('ip', 'no IP')}) — {total} DNS "
                    f"lookup(s) in the last 24 hours.",
                    f"Telemetry, ads and tracking: "
                    f"{summary.get('telemetry_attempts', 0)} attempt(s). "
                    f"{summary.get('blocked', 0)} blocked, "
                    f"{summary.get('monitored', 0)} logged-not-blocked, "
                    f"{summary.get('allowed', 0)} ordinary.",
                ]
                top = summary.get("top_domains") or []
                if top:
                    lines.append("")
                    lines.append("Most-contacted:")
                    for row in top[:10]:
                        lines.append(
                            f"  {row.get('domain')} — {row.get('count')}x "
                            f"({str(row.get('category', 'other')).lower()})")
                return "\n".join(lines)
            except Exception as e:
                return f"I couldn't read that device's telemetry: {e}"

        @tool
        def bridge_ha_buckets(dummy: str = "") -> str:
            """The smart devices Home Assistant manages, grouped by integration and sorted by how much each one talks to a vendor cloud. These are the Zigbee, Z-Wave and cloud-connected devices that have no IP address of their own, so they never appear on the plain network roll. Use for 'how many smart devices talk to the cloud', 'what does home assistant talk to', 'what smart devices do I have'."""
            try:
                from puffpai import bridge_ha
                rows = bridge_ha.buckets()
                if not rows:
                    return ("Home Assistant hasn't handed me its device list "
                            "yet — ask me to refresh the Bridge.")
                totals = bridge_ha.counts()
                lines = [
                    f"Home Assistant manages {totals['devices']} device(s) "
                    f"({totals['entities']} entities) in {totals['buckets']} "
                    f"group(s); {totals['cloud_buckets']} reach a vendor cloud.",
                ]
                # Nine rows plus a header plus a tail line is the twelve this
                # tool is allowed; a long tail is summarised, never truncated
                # silently.
                for row in rows[:9]:
                    where = f", {row['lookups']} lookup(s)/24h" if row["lookups"] else ""
                    lines.append(
                        f"- {row['integration']} ({row['tier'].replace('_', ' ')}): "
                        f"{row['device_count']} device(s), "
                        f"{row['entity_count']} entities{where}")
                if len(rows) > 9:
                    rest = rows[9:]
                    lines.append(f"- and {len(rest)} smaller group(s): "
                                 + ", ".join(r["integration"] for r in rest[:8]))
                if totals["watching_24h"]:
                    lines.append(
                        f"{totals['watching_24h']} of the hub's 24h lookups "
                        "were telemetry, ads or tracking.")
                return "\n".join(lines)
            except Exception as e:
                return f"I couldn't read the managed device list: {e}"

        @tool
        def bridge_status(dummy: str = "") -> str:
            """Zone 3 (Bridge) health: Zone 3 posture, how many devices are in the inventory, how fresh that inventory is, and how much telemetry has been recorded. Use for 'is the bridge working', 'how fresh is the device list', 'bridge status'."""
            try:
                inventory, ledger = _bridge_parts()
                if inventory is None:
                    return ("The Bridge isn't running — no device inventory, no "
                            "telemetry ledger.")
                inv = inventory.status()
                lines = ["The Bridge — Zone 3"]

                try:
                    _, posture = _zone3_posture()
                    lines.append(posture)
                except Exception:
                    pass

                age = inv.get("inventory_age_seconds")
                if age is None:
                    freshness = "never refreshed yet"
                elif age < 120:
                    freshness = "refreshed just now"
                elif age < 7200:
                    freshness = f"refreshed {age // 60} minute(s) ago"
                else:
                    freshness = f"refreshed {age // 3600} hour(s) ago"
                lines.append(f"Inventory: {inv.get('device_count', 0)} device(s), "
                             f"{freshness}, {inv.get('new_24h', 0)} new in the "
                             f"last 24 hours.")

                sources = inv.get("sources") or {}
                if sources:
                    healthy = [s for s, ok in sources.items() if ok]
                    broken = [s for s, ok in sources.items() if not ok]
                    lines.append("Sources working: " +
                                 (", ".join(sorted(healthy)) if healthy else "none"))
                    if broken:
                        lines.append("Sources not answering: " +
                                     ", ".join(sorted(broken)))

                if ledger is not None:
                    try:
                        led = ledger.status()
                        lines.append(
                            f"Telemetry ledger: {led.get('ledger_bytes', 0)} "
                            f"byte(s) on file, last pull "
                            f"{led.get('last_pull') or 'never'} "
                            f"({led.get('last_pull_rows', 0)} row(s)).")
                    except Exception:
                        lines.append("Telemetry ledger: not readable.")
                return "\n".join(lines)
            except Exception as e:
                return f"I couldn't read the Bridge status: {e}"

        @tool
        def bridge_refresh(dummy: str = "") -> str:
            """Re-scan the network now and refresh the device inventory. Use for 'rescan the network', 'refresh the device list', 'look for new devices now'."""
            try:
                inventory, _ = _bridge_parts()
                if inventory is None:
                    return ("The Bridge device inventory isn't running, so "
                            "there's nothing to refresh.")
                before = len(inventory.devices())
                devices = inventory.refresh()
                after = len(devices)
                added = max(0, after - before)
                if added:
                    return (f"Re-scanned. {after} device(s) on the network — "
                            f"{added} I hadn't seen before.")
                return f"Re-scanned. {after} device(s), nothing new."
            except Exception as e:
                return f"The re-scan failed: {e}"


        @tool
        def prep_add_supply(supply_info: str) -> str:
            """Add emergency supplies to inventory. Format: 'category|item|quantity|unit|expiration' (expiration optional, use YYYY-MM-DD format)"""
            
            if "=" in supply_info:
                supply_info = supply_info.split("=")[-1].strip("'\"")
            
            supply_info = supply_info.strip("'\"")
            
            parts = supply_info.split('|')
            if len(parts) < 4:
                return "Please provide: category|item|quantity|unit|expiration (expiration optional)"
            
            category = parts[0].strip()
            item = parts[1].strip()
            quantity = float(parts[2].strip())
            unit = parts[3].strip()
            expiration = parts[4].strip() if len(parts) > 4 else None
            
            prep = EmergencyPrep()
            result = prep.add_supply(category, item, quantity, unit, expiration)
            return result
        
        @tool
        def prep_view_inventory(dummy: str = "") -> str:
            """View all emergency preparedness supplies in inventory."""
            
            prep = EmergencyPrep()
            return prep.get_inventory_summary()
        
        @tool
        def prep_days_remaining(dummy: str = "") -> str:
            """Calculate how many days your emergency supplies will last."""
            
            prep = EmergencyPrep()
            days = prep.calculate_days_of_supplies()
            
            result = f"Emergency Supply Duration:\n"
            result += f"Water: {days['water_days']} days\n"
            result += f"Food: {days['food_days']} days\n"
            result += f"\nLimiting factor: {days['limiting_factor']}"
            
            return result
        
        @tool
        def prep_set_family_size(size: str) -> str:
            """Set the family size for calculating supply needs."""
            
            if "=" in size:
                size = size.split("=")[-1].strip("'\"")
            
            try:
                family_size = int(size.strip())
                prep = EmergencyPrep()
                prep.inventory["family_size"] = family_size
                prep.save_inventory()
                return f"Family size set to {family_size} people"
            except ValueError:
                return f"Invalid number: {size}"

        @tool
        def prep_set_location(location: str) -> str:
            """Set your location for emergency planning and weather alerts. Example: 'Columbus, OH' or 'Portland, ME'"""
            
            if "=" in location:
                location = location.split("=")[-1].strip("'\"")
            
            location = location.strip("'\"")
            
            prep = EmergencyPrep()
            prep.inventory["location"] = location
            prep.save_inventory()
            
            return f"Location set to {location}"
        
        @tool
        def prep_weather_alerts(use_current_location: str = "false") -> str:
            """Get active weather alerts. Set use_current_location='true'
            to detect from IP, or uses stored location."""

            from puffpai.security.egress_doors import EgressBlocked, egress_get
            from puffpai.household_geo import (UNSET_MESSAGE,
                                                household_coordinates,
                                                household_location)

            prep = EmergencyPrep()

            # --- Step 1: Determine location + coordinates ---
            # Three distinct doors are crossed below and each is checked before
            # its own socket: geoip (your public IP tells ipapi.co roughly where
            # you live), geocode (the place name goes to OpenStreetMap), and
            # weather (coordinates go to the NWS).
            if use_current_location.lower() in ['true', 'yes', 'current']:
                try:
                    geo_response = egress_get('geoip', 'https://ipapi.co/json/',
                                              timeout=5)
                    geo_data = geo_response.json()
                    location = f"{geo_data.get('city')}, {geo_data.get('region_code')}"
                    lat = geo_data.get('latitude')
                    lon = geo_data.get('longitude')
                except EgressBlocked as blocked:
                    # Do not silently pretend we detected the location — say
                    # which door is shut, then fall back to the stored one.
                    location = prep.effective_location()
                    if not location:
                        return f"[Egress door closed] {blocked.user_message}"
                    lat, lon = None, None
                except Exception:
                    location = prep.effective_location()
                    lat, lon = None, None
            else:
                location = prep.effective_location()
                lat, lon = None, None

            # --- Step 1b: the household's OWN position, if that is what was
            # asked about. Same resolver as the weather card and the
            # deterministic alerts reply (config latitude/longitude, else one
            # cached geocode through the door if open), so this tool can never
            # check a different place than the rest of the box.
            #
            # The owner's point is NEVER echoed: this tool's result goes to the
            # model, into chat and the daily briefing, and the agent loop
            # prints every tool result to stdout. From here on the numbers go
            # only to the alert sources; replies say "your saved home point".
            from_home = False
            if (not lat or not lon) and location and \
                    location.strip().lower() == household_location().lower():
                lat, lon = household_coordinates()
                from_home = lat is not None and lon is not None

            # --- Step 2: Geocode if we don't have coordinates ---
            # This calls nominatim.openstreetmap.org (REQUIRES INTERNET).
            # If offline, it will fail. Wrap in try/except so it doesn't crash.
            if not location:
                return (
                    "I have no location for this house, so I cannot look up "
                    "alerts and I am not going to guess. %s Or tell me the "
                    "place with prep_set_location." % UNSET_MESSAGE
                )
            if not lat or not lon:
                try:
                    geocode_response = egress_get(
                        'geocode',
                        'https://nominatim.openstreetmap.org/search',
                        params={'q': location, 'format': 'json', 'limit': 1},
                        headers={'User-Agent': 'PuffPAI/1.0'},
                        timeout=10
                    )
                    geocode_data = geocode_response.json()
                    if not geocode_data:
                        return f"Could not find coordinates for {location}"
                    lat = float(geocode_data[0]['lat'])
                    lon = float(geocode_data[0]['lon'])
                except EgressBlocked as blocked:
                    return (
                        f"[Egress door closed] {blocked.user_message}\n\n"
                        f"I have no coordinates for '{location}', so I cannot "
                        f"look up alerts and I am not going to guess."
                    )
                except Exception:
                    return (
                        f"⚠️ Cannot geocode '{location}' — no internet.\n"
                        f"Set coordinates manually with: prep_set_location\n\n"
                        f"To check weather manually, tune to NOAA Weather "
                        f"Radio frequencies:\n"
                        f"  162.400, 162.425, 162.450, 162.475,\n"
                        f"  162.500, 162.525, 162.550 MHz"
                    )

            # --- Step 3: Call automation weather webhook (with retry) ---
            from puffpai.automation_client import AutomationClient
            automation = AutomationClient()
            ok, data = automation.call("weather_alerts", {
                'location': location, 'lat': lat, 'lon': lon,
            })

            # --- Step 3b: Fallback to NWS API if the automation engine is down ---
            door_note = ""
            if not ok:
                try:
                    # NWS API: get alerts by point
                    point_resp = egress_get(
                        'weather',
                        'https://api.weather.gov/alerts/active',
                        params={'point': f'{lat},{lon}'},
                        headers={'User-Agent': 'PuffPAI/1.0'},
                        timeout=10,
                    )
                    if point_resp.status_code == 200:
                        nws = point_resp.json()
                        features = nws.get('features', [])
                        if not features:
                            return f"No active weather alerts for {location}"
                        data = {
                            'alert_count': len(features),
                            'alerts': [
                                {
                                    'event': f.get('properties', {}).get('event', 'Unknown'),
                                    'severity': f.get('properties', {}).get('severity', 'Unknown'),
                                    'headline': f.get('properties', {}).get('headline', ''),
                                }
                                for f in features
                            ],
                        }
                        ok = True
                except EgressBlocked as blocked:
                    door_note = f"\n{blocked.user_message}\n"
                except Exception:
                    pass

            if not ok:
                return (
                    f"⚠️ Weather alert service unavailable.\n"
                    f"Automation engine and NWS API both unreachable.\n"
                    f"{door_note}\n"
                    f"Stored location: {location}\n"
                    # Never the numbers (see Step 1b): the place is named,
                    # the point is only acknowledged.
                    + ("Checked: your saved home point\n" if from_home else "")
                    + "\n"
                    f"To check weather manually, tune to NOAA Weather "
                    f"Radio frequencies:\n"
                    f"  162.400, 162.425, 162.450, 162.475,\n"
                    f"  162.500, 162.525, 162.550 MHz"
                )

            # --- Step 4: Parse response ---
            if not isinstance(data, dict):
                return "Weather service returned invalid data"

            alert_count = data.get('alert_count', 0)

            if alert_count == 0:
                return f"No active weather alerts for {location}"

            response = f"Weather Alerts for {location} ({alert_count} active):\n\n"
            for alert in data.get('alerts', []):
                response += f"- {alert['event']} ({alert['severity']})\n"
                response += f"  {alert['headline']}\n\n"

            return response

        @tool
        def prep_recommendations(dummy: str = "") -> str:
            """Get recommendations for missing emergency supplies based on family size."""
            
            prep = EmergencyPrep()
            gaps = prep.get_recommended_supplies()
            
            if not gaps:
                return "Your emergency supplies are well-stocked!"
            
            result = f"Emergency Supply Recommendations for {prep.inventory['family_size']} people:\n\n"
            
            for category, info in gaps.items():
                result += f"{category.title()}:\n"
                result += f"  Current: {info['current']} {info['unit']}\n"
                result += f"  Recommended: {info['recommended']} {info['unit']}\n"
                result += f"  Need: {info['needed']} {info['unit']}\n"
                result += f"  Why: {info['reason']}\n\n"
            
            return result
        
        @tool
        def prep_check_expiration(days: str = "30") -> str:
            """Check for supplies expiring soon. Default checks next 30 days."""
            
            if "=" in days:
                days = days.split("=")[-1].strip("'\"")
            
            try:
                days_int = int(days.strip())
            except:
                days_int = 30
            
            prep = EmergencyPrep()
            expiring = prep.get_expiring_items(days_int)
            
            if not expiring:
                return f"No items expiring in the next {days_int} days"
            
            result = f"Items expiring in the next {days_int} days:\n\n"
            
            for item in expiring:
                result += f"- {item['quantity']} {item['unit']} of {item['item']} ({item['category']})\n"
                result += f"  Expires in {item['days_until']} days ({item['expiration']})\n\n"
            
            return result

        # ── Application Integration Tools ────────────────────────────────────

        from app_data import AppData
        app = AppData()

        @tool
        def app_set_timer(
            description: str = "Timer",
            minutes: int = 0,
            hours: int = 0,
            seconds: int = 0,
        ) -> str:
            """Set a countdown timer.

            Args:
                description: A short label for the timer (e.g. "pizza", "laundry").
                minutes: How many minutes the timer should run for.
                hours: Optional hours portion (default 0).
                seconds: Optional seconds portion (default 0).

            At least one of minutes/hours/seconds must be greater than 0.
            """
            description = (description or "Timer").strip() or "Timer"
            try:
                minutes = int(minutes or 0)
                hours = int(hours or 0)
                seconds = int(seconds or 0)
            except (TypeError, ValueError):
                return "Timer needs whole-number minutes/hours/seconds."
            if minutes == 0 and hours == 0 and seconds == 0:
                return "I need a duration. Example: minutes=10."
            return app.alarm_create_timer(description, hours, minutes, seconds)

        @tool
        def app_set_alarm(
            description: str = "Alarm",
            time: str = "",
            hour: int = -1,
            minute: int = 0,
            in_minutes: int = 0,
            repeat: str = "once",
        ) -> str:
            """Set an alarm for a specific time of day.

            Args:
                description: Short label (e.g. "wake up", "morning workout").
                time: Natural-language time like "7:30 AM", "3pm", "15:00", "noon",
                    or a relative phrase like "in 45 minutes" / "in 2 hours".
                hour: Alternative — explicit hour (0-23). Used if time is empty.
                minute: Minute (0-59), used with hour.
                in_minutes: Use for relative requests like "in 45 minutes" — set this
                    to the number of minutes from now and the alarm is created at that
                    clock time. Do NOT guess a clock time for relative requests.
                repeat: once | daily | weekdays | weekends | weekly. Use for
                    "every morning" (daily), "school days" (weekdays), etc.
                    A repeating alarm reschedules itself when dismissed.
            """
            description = (description or "Alarm").strip() or "Alarm"
            repeat = (repeat or "once").strip().lower()
            if repeat not in ("once", "daily", "weekdays", "weekends", "weekly"):
                repeat = "once"
            try:
                in_minutes = int(in_minutes or 0)
            except (TypeError, ValueError):
                in_minutes = 0
            # Relative request via explicit in_minutes: now + delta.
            # Alarms only carry a time-of-day, so anything >= 24h would
            # silently ring a day early — refuse instead.
            if in_minutes >= 1440:
                return ("Alarms can only be set up to 24 hours ahead. For "
                        "something further out, add a calendar event instead.")
            if in_minutes > 0:
                target = datetime.now() + timedelta(minutes=in_minutes)
                return app.alarm_create_alarm(description, target.hour, target.minute)
            if time:
                # Relative time string: "in 45 minutes", "in 2 hours".
                rel = re.match(
                    r'in\s+(\d+)\s*(hour|hr|h|minute|min|m|second|sec|s)s?$',
                    time.strip().lower())
                if rel:
                    n, unit = int(rel.group(1)), rel.group(2)
                    if unit.startswith(('hour', 'hr', 'h')):
                        delta = timedelta(hours=n)
                    elif unit.startswith(('minute', 'min', 'm')):
                        delta = timedelta(minutes=n)
                    else:
                        delta = timedelta(seconds=n)
                    if delta >= timedelta(hours=24):
                        return ("Alarms can only be set up to 24 hours ahead. "
                                "For something further out, add a calendar "
                                "event instead.")
                    target = datetime.now() + delta
                    return app.alarm_create_alarm(description, target.hour, target.minute)
                parsed = _parse_natural_time(time)
                if parsed:
                    return app.alarm_create_alarm(description, parsed[0], parsed[1], repeat=repeat)
                return f"I couldn't parse '{time}'. Try '7:30 AM' or '15:00'."
            if 0 <= hour <= 23:
                try:
                    return app.alarm_create_alarm(description, int(hour), int(minute or 0), repeat=repeat)
                except (TypeError, ValueError):
                    return "Hour and minute must be whole numbers."
            return "I need a time for the alarm. Example: time='7:30 AM'."

        @tool
        def app_list_alarms(dummy: str = "") -> str:
            """List all active timers and alarms. Use when user asks 'what timers do I have', 'list my alarms', 'any active timers'."""
            return app.alarm_list()

        @tool
        def app_add_event(
            title: str,
            date: str = "",
            category: str = "other",
            assigned_to: str = "all",
            priority: str = "medium",
            recurrence: str = "",
        ) -> str:
            """Add a calendar event with a specific date/time.

            Args:
                title: Event title (e.g. "Haircut", "Pickup Riley from school").
                date: When the event happens — natural language ("next friday at 3pm", "tomorrow at 2pm") or ISO ("2026-04-10T15:00"). ALWAYS include the time if the user mentioned one.
                category: One of: medical, sports, school, work, family, other.
                assigned_to: Who it's for — dad, mom, alex, emma, or all.
                priority: low, medium, or high.
                recurrence: How often this event repeats. One of: "" (no repeat), "daily", "weekdays" (Mon-Fri), "weekly", "biweekly", "monthly", "yearly". Use "weekdays" when the user says "every weekday" or "every school day".

            Returns confirmation that the event was created.
            """
            if not title:
                return "I need at least an event title."
            start = date or ""
            if not start:
                start = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%dT09:00')
            else:
                # Extract embedded time like "next monday at 2pm"
                time_embedded = None
                at_match = re.search(
                    r'\s+at\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)',
                    start, re.IGNORECASE)
                if at_match:
                    time_str = at_match.group(1)
                    time_parsed = _parse_natural_time(time_str)
                    if time_parsed:
                        time_embedded = time_parsed
                    start = start[:at_match.start()].strip()

                start = _parse_natural_date(start)
                if re.match(r'^\d{4}-\d{2}-\d{2}$', start):
                    if time_embedded:
                        start += f'T{time_embedded[0]:02d}:{time_embedded[1]:02d}'
                    else:
                        start += 'T09:00'
            rec = (recurrence or "").strip().lower()
            if rec in ("none", "no", "false"):
                rec = ""
            result = app.cal_add_event(
                title, start,
                cat=category, assigned_to=assigned_to, prio=priority,
                recurrence=rec or None,
            )
            return result + " It will appear in the Calendar app."

        @tool
        def app_add_task(
            title: str,
            due_date: str = "",
            category: str = "other",
            assigned_to: str = "all",
            priority: str = "medium",
        ) -> str:
            """Add a task/to-do/reminder to the family calendar.

            Use this for chores, errands, appointments, and reminders — NOT for groceries
            (use app_add_shopping) or pantry items (use app_add_pantry).

            Args:
                title: Task description (e.g. "Pick up Riley from school", "Buy dog food").
                due_date: Due date — natural language ("tomorrow", "next monday", "april 10") or ISO ("2026-04-10").
                category: medical, sports, school, work, family, other.
                assigned_to: dad, mom, alex, emma, or all.
                priority: low, medium, high, or urgent.
            """
            if not title:
                return "I need at least a task title."
            due = due_date
            if not due:
                due = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')
            else:
                due = _parse_natural_date(due)
            result = app.cal_add_task(title, due, cat=category, assigned_to=assigned_to, prio=priority)
            return result + " It will appear in the Calendar app."

        @tool
        def app_list_schedule(days_ahead: str = "7") -> str:
            """List upcoming calendar events, tasks, medication reminders, and bills. Use when user asks 'what is on my calendar', 'what is happening this week', 'any upcoming events'. Optionally specify how many days ahead."""
            try:
                days = int(days_ahead.strip("'\""))
            except (ValueError, AttributeError):
                days = 7
            events = app.cal_list_events(days)
            tasks = app.cal_list_tasks('pending')
            meds = app.cal_list_meds()
            bills = app.cal_list_bills('pending')
            parts = [events]
            if 'No pending' not in tasks:
                parts.append(tasks)
            if 'No medications' not in meds:
                parts.append(meds)
            if 'No pending' not in bills:
                parts.append(bills)
            return "\n\n".join(parts)

        @tool
        def app_complete_task(task_name: str) -> str:
            """Mark a task as completed. Use when user says 'I finished', 'mark X as done', 'complete the task'. Can use task title or partial match."""
            if "=" in task_name:
                task_name = task_name.split("=")[-1].strip("'\"")
            return app.cal_complete_task(task_name.strip("'\""))

        @tool
        def app_add_bill(
            name: str,
            amount: str = "0",
            due_date: str = "",
            category: str = "utilities",
        ) -> str:
            """Add a bill to track and pay.

            Args:
                name: Bill name (e.g. "Electric Bill", "Netflix").
                amount: Amount in dollars as a string (e.g. "142.50").
                due_date: Due date — natural language ("march 15", "next friday") or ISO.
                category: utilities, insurance, subscription, housing, or other.
            """
            if not name:
                return "I need at least a bill name."
            due = due_date
            if not due:
                due = (datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d')
            else:
                due = _parse_natural_date(due)
            return app.cal_add_bill(name, amount, due, cat=category)

        @tool
        def app_add_pantry(
            name: str,
            quantity: int = 1,
            unit: str = "pcs",
        ) -> str:
            """Add a FOOD item the user has at home to the kitchen pantry inventory.

            Use whenever the user mentions food they bought, things in their kitchen,
            or items they want to track in stock. NOT for items they need to BUY
            (use app_add_shopping for that).

            Args:
                name: Item name (e.g. "ground beef", "greek yogurt", "chicken breast").
                quantity: How many they have (default 1).
                unit: Unit of measurement — pcs, lbs, oz, cups, bags, cans, gallons.
            """
            if not name:
                return "I need at least an item name."
            try:
                qty = int(quantity or 1)
            except (ValueError, TypeError):
                qty = 1
            result = app.fresh_add_pantry(name, qty, unit or "pcs")
            return result + " Open FreshTable to see your pantry."

        @tool
        def app_add_shopping(
            name: str,
            quantity: int = 1,
            estimated_price: float = 0,
        ) -> str:
            """Add an item to the grocery SHOPPING LIST (things the user wants to buy).

            Use whenever the user wants to buy something, needs groceries, or mentions
            adding to their shopping/grocery list. NOT for items already at home
            (use app_add_pantry for that).

            Args:
                name: Item to buy (e.g. "milk", "eggs", "bananas").
                quantity: How many to buy (default 1).
                estimated_price: Estimated price per unit in dollars (default 0).
            """
            if not name:
                return "I need at least an item name."
            try:
                qty = int(quantity or 1)
            except (ValueError, TypeError):
                qty = 1
            try:
                price = float(estimated_price or 0)
            except (ValueError, TypeError):
                price = 0
            result = app.fresh_add_shopping(name, qty, price)
            return result + " Open FreshTable to see your shopping list."

        @tool
        def app_list_pantry(filter_text: str = "") -> str:
            """List pantry items. Use 'low' to see items running low. Pass an ITEM NAME (e.g. 'rice', 'milk') or a category name to filter — the filter matches item names and categories. Use when user asks 'what is in the pantry', 'do we have X', 'how much X do we have', 'are we out of X', 'what is running low'. Report only what this returns; if it says the item is not there, say that, and never answer a stock question from memory."""
            f = filter_text.strip("'\"") if filter_text else None
            return app.fresh_list_pantry(f)

        @tool
        def app_list_shopping(dummy: str = "") -> str:
            """List the current shopping list. Use when user asks 'what is on the shopping list', 'what do we need to buy'."""
            return app.fresh_list_shopping()

        @tool
        def app_log_workout(
            user: str,
            workout_type: str = "calisthenics",
            duration_minutes: int = 30,
            notes: str = "",
        ) -> str:
            """Log a completed workout SESSION for a family member (legacy logger).

            Use this for cardio, mobility, or anything where you don't track
            individual sets. For weight training, prefer app_log_set instead
            so progressive overload can be tracked.

            Args:
                user: Which household member did it, using the id shown in the
                    FitFam roster on this box.
                workout_type: calisthenics, kettlebell, rom, gym, or running.
                duration_minutes: How long they worked out.
                notes: Optional notes about the session.
            """
            user = (user or "").strip().lower()
            if not user:
                return ("Who worked out? Name a household member from the "
                        "FitFam roster.")
            try:
                duration = int(duration_minutes or 30)
            except (TypeError, ValueError):
                duration = 30
            wtype = (workout_type or "calisthenics").strip().lower()
            result = app.fit_log_workout(user, wtype, duration, notes or "")
            return result + " Open FitFam to see your progress."

        @tool
        def app_log_set(
            user: str,
            exercise_id: str,
            weight: float,
            reps: int,
            rpe: float = 0,
            notes: str = "",
        ) -> str:
            """Log a single working set with weight, reps, and optional RPE.

            This is the science-backed strength training logger. Use this
            instead of app_log_workout when the user is lifting weights and
            wants to track progressive overload.

            Args:
                user: Household member id, exactly as it appears in the FitFam
                    roster on this box.
                exercise_id: Exercise key from the database (e.g. "bench-press",
                    "back-squat", "deadlift", "ohp", "pullup", "barbell-row",
                    "rdl", "front-squat", "dumbbell-bench", "dumbbell-row",
                    "lat-pulldown", "lateral-raise", "tricep-pushdown",
                    "barbell-curl", "face-pull", "bulgarian-split-squat",
                    "kettlebell-swing", "calf-raise", "plank", "hanging-leg-raise").
                weight: Weight used in pounds (or 0 for bodyweight).
                reps: Number of reps completed.
                rpe: Optional Rate of Perceived Exertion 1-10 (10 = max effort,
                    8 = "could've done 2 more"). Pass 0 to omit.
                notes: Optional notes about the set.
            """
            u = (user or "").strip().lower()
            if not u:
                return ("Who did the set? Name a household member from the "
                        "FitFam roster.")
            ex = (exercise_id or "").strip().lower()
            if not ex:
                return "I need an exercise id (e.g. 'bench-press')."
            return app.fit_log_set(u, ex, weight, reps, rpe if rpe else None, notes)

        @tool
        def app_delete_workout(user: str, title: str = "") -> str:
            """Delete ONE logged workout from a family member's own history.

            The undo path: use it when someone says a workout was logged by
            mistake, for the wrong person, or should be removed. Never guess
            who — if the request does not name a person, ASK "whose workout?"
            and call this only once they answer.

            Args:
                user: Whose history to remove it from. Required.
                title: The workout title/type to remove (fuzzy matched).
                    Leave empty to remove that person's most recent entry.
            """
            u = (user or "").strip().lower()
            if not u:
                keys = app.fit_user_keys()
                return f"Whose workout? Say {', '.join(sorted(keys))}."
            wanted = (title or "").strip().strip("'\"")
            result = app.fit_delete_entry(u, title=wanted or None,
                                          last=not wanted)
            if not isinstance(result, dict):
                return "I couldn't delete that."
            if result.get('ok'):
                return (f"Deleted {result.get('name') or u}'s "
                        f"\"{result.get('removed')}\" entry.")
            reason = result.get('reason')
            if reason == 'unknown_user':
                return (f"I don't have a FitFam profile called '{u}'. "
                        f"Say {', '.join(sorted(result.get('users') or []))}.")
            if reason == 'ambiguous':
                cands = ", ".join(f'"{c}"'
                                  for c in result.get('candidates') or [])
                return f"More than one match — {cands}. Which one?"
            if reason == 'no_match':
                return f"No workout called \"{wanted}\" is in that history."
            return "There is nothing logged to delete."

        @tool
        def search_my_documents(query: str) -> str:
            """Search across documents the user has previously uploaded
            (recipes, study notes, PDFs, photos with text, etc).

            Use this for the FIRST time the user asks about something they
            uploaded — it returns short matching snippets across all
            documents. If you find the right doc but need its full content,
            then call fetch_my_document with the filename.

            Returns matching snippets with filenames so you can answer.
            Also automatically searches the offline knowledge base so the
            user gets a comprehensive answer from both sources in one step.
            """
            try:
                # BOTH upload stores, one entry point: reading only the
                # ingestion store would make every knowledge-upload
                # document invisible to search while /ingest/list listed it.
                # Filter out weak matches — anything below 0.35 relevance
                # is noise and causes the LLM to fabricate document names.
                # Shared with the "search my documents" fastpath.
                from puffpai.upload_index import search_my_documents as _search
                results = _search(query)
            except Exception:
                results = []

            # Also search the offline knowledge base (encyclopedias,
            # survival guides, TED talks, etc.) so the user gets results
            # from both their personal uploads AND the curated library.
            kb_lines = []
            try:
                from puffpai.api_server import _get_rag_context
                rag = _get_rag_context(query, n_results=5)
                if rag and rag.get('sources'):
                    kb_sources = rag['sources'][:5]
                    kb_context = rag.get('context_text', '')
                    # Pull short snippets from the RAG context
                    snippets = [s.strip() for s in kb_context.split('\n\n')
                                if s.strip() and not s.strip().startswith('[')]
                    if kb_sources:
                        kb_lines.append(
                            f"\n\n--- Knowledge Base Results ({len(kb_sources)} source"
                            f"{'s' if len(kb_sources) != 1 else ''}) ---")
                        for src in kb_sources:
                            kb_lines.append(f"  📚 {src['library']}")
                        if snippets:
                            kb_lines.append("\nRelevant excerpts:")
                            for snip in snippets[:3]:
                                # Trim per-snippet to 300 chars
                                text = snip[:300]
                                if len(snip) > 300:
                                    text += "..."
                                kb_lines.append(f"  {text}")
            except Exception:
                pass

            # One composer for both sections, so a knowledge-base excerpt can
            # never be read back as the user's own document when nothing of
            # theirs matched (the model would confabulate a document).
            from puffpai.upload_index import compose_document_answer
            return compose_document_answer(query, results, kb_lines)

        @tool
        def fetch_my_document(filename_or_id: str) -> str:
            """Return the FULL text content of a single uploaded document.

            Use this when the user asks for the complete contents of a
            specific document, after `search_my_documents` has identified
            which file they want — for example "give me the full vegetable
            lasagna recipe", "show me the entire history notes", "tell me
            everything in the roof warranty doc".

            Args:
                filename_or_id: The filename (or partial filename) of the
                    document to fetch. Case-insensitive.
            """
            try:
                from puffpai.ingestion import fetch_document_text
                doc = fetch_document_text(filename_or_id, max_chars=6000)
                if not doc:
                    # The knowledge-upload store keeps its text as chunks in
                    # a per-document collection, not in the ingestion catalog.
                    from puffpai import document_lookup
                    entry = document_lookup.resolve(filename_or_id)
                    body = document_lookup.text_of(entry) if entry else ""
                    if entry and body:
                        doc = {
                            'filename': entry.get('filename'),
                            'classification': entry.get(
                                'classification', 'document'),
                            'word_count': entry.get('word_count', 0),
                            'text': body,
                        }
            except Exception as e:
                return f"Fetch failed: {e}"
            if not doc:
                return (
                    f"No document found matching '{filename_or_id}'. "
                    f"Try search_my_documents first to see what's available."
                )
            header = (
                f"=== {doc['filename']} ({doc['classification']}, "
                f"{doc.get('word_count', 0)} words) ==="
            )
            return f"{header}\n\n{doc['text']}"

        @tool
        def list_my_documents(dummy: str = "") -> str:
            """List all documents the user has uploaded and indexed.
            Use when the user asks 'what files have I uploaded',
            'show my documents', 'what's in my library'."""
            try:
                # The SAME two-store read /ingest/list answers from. Reading
                # only the ingestion store would say "No documents uploaded
                # yet." while knowledge uploads exist.
                from puffpai.upload_index import all_uploaded_docs, is_fixture_doc
                docs = all_uploaded_docs()
            except Exception as e:
                return f"Could not list documents: {e}"
            if not docs:
                return "No documents uploaded yet."
            # Real uploads first; a leftover test fixture is listed last.
            docs = sorted(docs, key=lambda d: is_fixture_doc(
                d.get('classification')))
            lines = [f"You have {len(docs)} ingested document{'s' if len(docs) != 1 else ''}:"]
            for d in docs:
                lines.append(
                    f"  - {d.get('filename')} "
                    f"({d.get('classification', 'document')}, "
                    f"{d.get('word_count', 0)} words)"
                )
            return "\n".join(lines)

        @tool
        def app_personal_records(user: str = "") -> str:
            """List personal records (estimated 1-rep maxes) for a family member.

            Args:
                user: A household member id from the FitFam roster. Leave empty
                    for every member on this box.
            """
            users_to_check = (
                [user.strip().lower()] if user
                else sorted(app.get_data('fitfam').get('users', {}))
            )
            lines = []
            for u in users_to_check:
                records = app.fit_personal_records(u)
                if not records:
                    lines.append(f"{u.title()}: no PRs logged yet.")
                    continue
                lines.append(f"\n{u.title()}'s personal records:")
                # Sort by 1RM descending
                sorted_records = sorted(
                    records.values(),
                    key=lambda r: r.get('estimated1RM', 0),
                    reverse=True,
                )
                for r in sorted_records[:10]:
                    lines.append(
                        f"  {r['exerciseId']}: {r['weight']}lb x {r['reps']} "
                        f"(est. 1RM: {r['estimated1RM']}lb)"
                    )
            return "\n".join(lines) if lines else "No personal records yet."

        @tool
        def app_generate_workout(request: str = "") -> str:
            """Generate a workout routine for a household member.

            Use when the user says 'give me a workout', 'new routine',
            'what should I do today'. Optionally specify the member id (as
            shown in the FitFam roster), the type, and the duration, e.g.
            '<member>|calisthenics|30', or pass nothing for the first
            member on the roster.
            """
            import random

            # Exercise database (mirrors FitFam app)
            exercises = {
                'calisthenics': [
                    ('Push-Ups', 'chest', {'easy': '3x10 (knees ok)', 'mid': '4x15 standard', 'hard': '4x12 archer'}),
                    ('Pull-Ups', 'back', {'easy': '3x5 assisted', 'mid': '4x8 strict', 'hard': '4x6 weighted'}),
                    ('Dips', 'chest', {'easy': '3x8 bench dips', 'mid': '4x10 parallel', 'hard': '4x8 weighted'}),
                    ('Squats', 'legs', {'easy': '3x12 bodyweight', 'mid': '4x15 jump squats', 'hard': '4x10 pistol'}),
                    ('Plank', 'core', {'easy': '3x20 seconds', 'mid': '3x45 seconds', 'hard': '3x60 seconds'}),
                    ('Lunges', 'legs', {'easy': '3x10 each leg', 'mid': '4x12 walking', 'hard': '4x10 Bulgarian'}),
                    ('Burpees', 'full', {'easy': '3x5 no pushup', 'mid': '4x10 standard', 'hard': '4x12 with tuck jump'}),
                    ('Mountain Climbers', 'core', {'easy': '3x15 seconds', 'mid': '3x30 seconds', 'hard': '4x45 seconds'}),
                    ('Pike Push-Ups', 'shoulders', {'easy': '3x8 feet on floor', 'mid': '4x10 elevated', 'hard': '4x8 wall HSPU'}),
                    ('Leg Raises', 'core', {'easy': '3x10 bent knee', 'mid': '4x12 straight leg', 'hard': '4x10 hanging'}),
                    ('Diamond Push-Ups', 'triceps', {'easy': '3x8 on knees', 'mid': '4x12 standard', 'hard': '4x10 decline'}),
                    ('Box Jumps', 'legs', {'easy': '3x8 low box', 'mid': '4x10 medium', 'hard': '4x12 high box'}),
                ],
                'kettlebell': [
                    ('KB Swings', 'full', {'easy': '3x12 light', 'mid': '4x15 moderate', 'hard': '5x20 heavy'}),
                    ('Goblet Squats', 'legs', {'easy': '3x10', 'mid': '4x12', 'hard': '4x15 heavy'}),
                    ('KB Clean & Press', 'shoulders', {'easy': '3x6 each', 'mid': '4x8 each', 'hard': '5x10 each'}),
                    ('Turkish Get-Up', 'full', {'easy': '2x3 each light', 'mid': '3x3 each', 'hard': '4x3 each heavy'}),
                    ('KB Rows', 'back', {'easy': '3x10 each', 'mid': '4x12 each', 'hard': '4x15 each'}),
                    ('KB Deadlift', 'legs', {'easy': '3x10', 'mid': '4x12 single leg', 'hard': '4x10 heavy'}),
                    ('KB Halo', 'shoulders', {'easy': '3x8 each way', 'mid': '4x10 each', 'hard': '4x12 heavy'}),
                    ('KB Snatch', 'full', {'easy': '3x5 each', 'mid': '4x8 each', 'hard': '5x10 each'}),
                ],
                'gym': [
                    ('Bench Press', 'chest', {'easy': '3x8 light', 'mid': '4x8 moderate', 'hard': '5x5 heavy'}),
                    ('Barbell Squats', 'legs', {'easy': '3x8 light', 'mid': '4x8 moderate', 'hard': '5x5 heavy'}),
                    ('Deadlift', 'back', {'easy': '3x8 light', 'mid': '4x6 moderate', 'hard': '5x3 heavy'}),
                    ('Overhead Press', 'shoulders', {'easy': '3x10 light', 'mid': '4x8 moderate', 'hard': '5x5 heavy'}),
                    ('Barbell Rows', 'back', {'easy': '3x10', 'mid': '4x8', 'hard': '4x6 heavy'}),
                    ('Lat Pulldown', 'back', {'easy': '3x12', 'mid': '4x10', 'hard': '4x8 heavy'}),
                ],
                'rom': [
                    ('Hip Circles', 'hips', {'easy': '2x10 each', 'mid': '3x12 each', 'hard': '3x15 each'}),
                    ('Shoulder Dislocates', 'shoulders', {'easy': '2x10', 'mid': '3x12', 'hard': '3x15 narrow grip'}),
                    ('Cat-Cow Stretch', 'back', {'easy': '2x8', 'mid': '3x10', 'hard': '3x12 slow'}),
                    ('World\'s Greatest Stretch', 'full', {'easy': '2x5 each', 'mid': '3x6 each', 'hard': '3x8 each'}),
                    ('Pigeon Pose', 'hips', {'easy': '30 sec each', 'mid': '45 sec each', 'hard': '60 sec each'}),
                ],
            }

            # Duration → exercise count
            time_configs = {15: 3, 30: 5, 45: 7, 60: 9}

            # Parse input
            fields = ['user', 'type', 'duration']
            parsed = _parse_flexible_input(request, fields)
            roster = sorted(app.get_data('fitfam').get('users', {}))
            user = (parsed.get('user')
                    or (roster[0] if roster else '')).strip().lower()
            wtype = (parsed.get('type') or '').strip().lower()
            try:
                duration = int(parsed.get('duration') or 30)
            except (ValueError, TypeError):
                duration = 30

            # Default workout types per user
            if not wtype:
                # The member's own stored preference, never a hardcoded person.
                prefs = (app.get_data('fitfam').get('users', {})
                         .get(user, {}).get('prefs') or {})
                wtype = (prefs.get('type') or 'calisthenics')

            # Normalize type names
            type_aliases = {
                'calisthenics': 'calisthenics', 'bodyweight': 'calisthenics',
                'kettlebell': 'kettlebell', 'kb': 'kettlebell',
                'gym': 'gym', 'weights': 'gym', 'strength': 'gym',
                'rom': 'rom', 'mobility': 'rom', 'stretching': 'rom', 'flexibility': 'rom',
                'full': 'full',
            }
            wtype = type_aliases.get(wtype, wtype)

            # Build exercise pool
            if wtype == 'full':
                pool = []
                for cat_exercises in exercises.values():
                    pool.extend(cat_exercises)
            elif wtype in exercises:
                pool = exercises[wtype]
            else:
                pool = exercises.get('calisthenics', [])

            # Select exercises
            count = time_configs.get(duration, 5)
            level = 'mid'  # default to mid intensity
            selected = random.sample(pool, min(count, len(pool)))

            # Format routine
            name_label = user.title()
            lines = [f"{name_label}'s {duration}-Minute {wtype.title()} Routine:"]
            lines.append("")
            for i, (ex_name, muscle, levels) in enumerate(selected, 1):
                prescription = levels.get(level, levels.get('mid', ''))
                lines.append(f"{i}. {ex_name} ({muscle}) — {prescription}")

            lines.append("")
            lines.append(f"Rest 30-60 seconds between sets. Total time: ~{duration} minutes.")
            lines.append("Open FitFam to track this workout and log your progress!")

            return "\n".join(lines)

        @tool
        def app_fitness_status(user: str = "") -> str:
            """Get fitness status and progress. Use when user asks 'how is my fitness', 'what is my streak', 'how many workouts'."""
            u = user.strip("'\"") if user else None
            return app.fit_get_status(u)

        def _alarm_identifier_variants(identifier):
            """Spoken names rarely match stored labels exactly: 'seven
            minute timer' must find '7 minutes timer'. Yield the raw
            identifier plus digit-normalized and trimmed variants."""
            raw = identifier.strip("'\"").strip()
            yield raw
            norm = raw.lower()
            for word, num in _WORD_NUM_MAP.items():
                norm = re.sub(rf"\b{word}\b", str(num), norm)
            if norm != raw.lower():
                yield norm
            # "7 minute" is a substring of "7 minutes ..." — try the
            # bare number+unit. (Never a bare number: '7' would
            # substring-match unrelated items like 'wake at 7:30'.)
            m = re.search(r"(\d+)\s*(minute|min|second|sec|hour|hr)", norm)
            if m:
                yield f"{m.group(1)} {m.group(2)}"

        @tool
        def app_dismiss_alarm(identifier: str) -> str:
            """Dismiss or stop a ringing alarm or timer. Use when user says 'stop the alarm', 'dismiss the timer', 'turn off the alarm'. Pass 'all' to dismiss everything ('dismiss all my alarms'). Can match by name or ID."""
            if "=" in identifier:
                identifier = identifier.split("=")[-1].strip("'\"")
            ident = identifier.strip("'\"").strip().lower()
            if ident in ("all", "everything", "all alarms", "all timers"):
                return app.alarm_dismiss_all()
            for variant in _alarm_identifier_variants(identifier):
                result = app.alarm_dismiss(variant)
                if not result.lower().startswith(("no ", "i couldn")):
                    return result
            return result

        @tool
        def app_delete_alarm(identifier: str) -> str:
            """Permanently delete an alarm or timer. Use when user says 'delete the alarm', 'remove the pizza timer', 'get rid of that alarm'. Can match by name or ID."""
            if "=" in identifier:
                identifier = identifier.split("=")[-1].strip("'\"")
            for variant in _alarm_identifier_variants(identifier):
                result = app.alarm_delete(variant)
                if not result.lower().startswith(("no ", "i couldn")):
                    return result
            return result

        @tool
        def app_snooze_alarm(identifier: str, minutes: int = 9) -> str:
            """Snooze a ringing alarm or timer. Use when user says 'snooze',
            'five more minutes', 'snooze the alarm'. A repeating alarm keeps
            its normal schedule for tomorrow. Can match by name or ID."""
            try:
                minutes = max(1, int(minutes or 9))
            except (TypeError, ValueError):
                minutes = 9
            if "=" in identifier:
                identifier = identifier.split("=")[-1].strip("'\"")
            for variant in _alarm_identifier_variants(identifier):
                result = app.alarm_snooze(variant, minutes)
                if not result.lower().startswith(("no ", "several")):
                    return result
            return result

        @tool
        def app_pause_timer(identifier: str) -> str:
            """Pause a running countdown timer (keeps its remaining time). Use when user says 'pause the timer', 'hold the pizza timer'. Can match by name or ID."""
            if "=" in identifier:
                identifier = identifier.split("=")[-1].strip("'\"")
            for variant in _alarm_identifier_variants(identifier):
                result = app.alarm_pause(variant)
                if not result.lower().startswith(("no ", "several")):
                    return result
            return result

        @tool
        def app_resume_timer(identifier: str) -> str:
            """Resume a paused countdown timer from where it left off. Use when user says 'resume the timer', 'start it again'. Can match by name or ID."""
            if "=" in identifier:
                identifier = identifier.split("=")[-1].strip("'\"")
            for variant in _alarm_identifier_variants(identifier):
                result = app.alarm_resume(variant)
                if not result.lower().startswith(("no ", "several")):
                    return result
            return result

        @tool
        def app_add_med(
            name: str,
            dose: str = "1 dose",
            assigned_to: str = "all",
            frequency: str = "daily",
            next_time: str = "8:00 AM",
            vaulted: str = "",
            label: str = "",
        ) -> str:
            """Add a medication reminder.

            Args:
                name: Medication name (e.g. "vitamin D", "Metformin").
                dose: Dose description (e.g. "1 puff", "500mg", "2 pills").
                assigned_to: dad, mom, alex, emma, or all.
                frequency: daily, twice-daily, or weekly.
                next_time: Next dose time (e.g. "8:00 AM", "9pm").
                vaulted: "yes" if the person asked for the medication to be private/vaulted. Reminders keep working; the name is never spoken again.
                label: What to call it out loud instead of the name (e.g. "morning pills").
            """
            if not name:
                return "I need at least a medication name."
            is_vaulted = str(vaulted).strip().lower() in (
                "yes", "true", "1", "vaulted", "private")
            result = app.cal_add_med(name, dose, assigned_to,
                                     freq=frequency, next_time=next_time,
                                     vaulted=is_vaulted,
                                     label=(label.strip() or None))
            return result + " It will appear in the Calendar app."

        @tool
        def app_vault_med(med_name: str = "", label: str = "") -> str:
            """Vault a medication's NAME while keeping its reminders. Use when the user says 'vault that medication', 'keep my medication private', 'don't say the name of my medication'. med_name may be empty to vault the one just added; label is what to call it out loud (e.g. 'morning pills')."""
            if "=" in med_name:
                med_name = med_name.split("=")[-1].strip("'\"")
            return app.cal_vault_med(med_name.strip("'\" "),
                                     label=(label.strip() or None))

        @tool
        def app_take_med(med_name: str) -> str:
            """Mark a medication as taken. Use when user says 'I took my vitamin D', 'took my medicine', 'medication taken'. Can match by name or partial name."""
            if "=" in med_name:
                med_name = med_name.split("=")[-1].strip("'\"")
            return app.cal_take_med(med_name.strip("'\""))

        @tool
        def app_pay_bill(bill_name: str) -> str:
            """Mark a bill as paid. Use when user says 'paid the electric bill', 'mark rent as paid', 'I paid the water bill'. Can match by name or partial name."""
            if "=" in bill_name:
                bill_name = bill_name.split("=")[-1].strip("'\"")
            return app.cal_pay_bill(bill_name.strip("'\""))

        @tool
        def app_delete_event(event_name: str) -> str:
            """Delete a calendar event. Use when user says 'cancel my dentist appointment', 'remove the meeting', 'delete that event'. Pass the name exactly as the user said it; it matches only when every word is in the title. If it says nothing matched or asks which one, tell the user that and do NOT retry with fewer words or delete anything else."""
            if "=" in event_name:
                event_name = event_name.split("=")[-1].strip("'\"")
            return app.cal_delete_event(event_name.strip("'\""))

        @tool
        def app_delete_task(task_name: str) -> str:
            """Delete a task or reminder from the calendar. Use when user says 'remove pick up dog food from my tasks', 'delete that task', 'cancel the chore'. Pass the name exactly as the user said it; it matches only when every word is in the title. If it says nothing matched or asks which one, tell the user that and do NOT retry with fewer words or delete anything else."""
            if "=" in task_name:
                task_name = task_name.split("=")[-1].strip("'\"")
            return app.cal_delete_task(task_name.strip("'\""))

        @tool
        def app_edit_event(
            event_title: str,
            new_title: str = "",
            start: str = "",
            end: str = "",
            category: str = "",
            assigned_to: str = "",
            priority: str = "",
            location: str = "",
        ) -> str:
            """Edit fields of an existing calendar event by title.

            Args:
                event_title: Title of the event to find (e.g. "Team Meeting").
                new_title: New title (only set if renaming).
                start: New start date/time — natural language or ISO.
                end: New end date/time.
                category: medical, sports, school, work, family, other.
                assigned_to: dad, mom, alex, emma, all.
                priority: low, medium, high.
                location: Location string.
            """
            if not event_title:
                return "I need an event title to find."
            kwargs = {}
            if new_title:
                kwargs['title'] = new_title
            for key, val in [('start', start), ('end', end)]:
                if not val:
                    continue
                time_embedded = None
                at_match = re.search(
                    r'\s+at\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)',
                    val, re.IGNORECASE)
                if at_match:
                    time_parsed = _parse_natural_time(at_match.group(1))
                    if time_parsed:
                        time_embedded = time_parsed
                    val = val[:at_match.start()].strip()
                val = _parse_natural_date(val)
                if re.match(r'^\d{4}-\d{2}-\d{2}$', val):
                    if time_embedded:
                        val += f'T{time_embedded[0]:02d}:{time_embedded[1]:02d}'
                    else:
                        val += 'T09:00'
                kwargs[key] = val
            if category:
                kwargs['cat'] = category
            if assigned_to:
                kwargs['to'] = assigned_to
            if priority:
                kwargs['prio'] = priority
            if location:
                kwargs['loc'] = location
            if not kwargs:
                return "I need at least one field to update."
            result = app.cal_edit_event(event_title.strip("'\""), **kwargs)
            return result + " Changes will appear in the Calendar app."

        @tool
        def app_edit_task(
            task_title: str,
            new_title: str = "",
            due_date: str = "",
            category: str = "",
            assigned_to: str = "",
            priority: str = "",
        ) -> str:
            """Edit fields of an existing task by title.

            Args:
                task_title: Title of the task to find.
                new_title: New title (only set if renaming).
                due_date: New due date — natural language or ISO.
                category: medical, sports, school, work, family, other.
                assigned_to: dad, mom, alex, emma, all.
                priority: low, medium, high, urgent.
            """
            if not task_title:
                return "I need a task title to find."
            kwargs = {}
            if new_title:
                kwargs['title'] = new_title
            if due_date:
                kwargs['due'] = _parse_natural_date(due_date)
            if category:
                kwargs['cat'] = category
            if assigned_to:
                kwargs['to'] = assigned_to
            if priority:
                kwargs['prio'] = priority
            if not kwargs:
                return "I need at least one field to update."
            result = app.cal_edit_task(task_title.strip("'\""), **kwargs)
            return result + " Changes will appear in the Calendar app."

        @tool
        def app_create_recipe(
            name: str,
            ingredients: str,
            instructions: str = "",
        ) -> str:
            """Create a new recipe in FreshTable.

            Args:
                name: Recipe name (e.g. "Chicken Fajitas").
                ingredients: Comma-separated ingredient list.
                instructions: Step-by-step cooking instructions.
            """
            if not name:
                return "I need a recipe name."
            if not ingredients:
                return "I need ingredients for the recipe."
            result = app.fresh_create_recipe(name, ingredients, instructions or "")
            return result + " Open FreshTable to see your recipes."

        @tool
        def app_list_recipes(filter_text: str = "") -> str:
            """List saved recipes in FreshTable. Use when user asks 'what recipes do I have', 'show my recipes', 'do I have a recipe for X'. Pass a name or ingredient to filter."""
            f = filter_text.strip("'\"") if filter_text else None
            return app.fresh_list_recipes(f)

        @tool
        def app_remove_pantry(item_name: str) -> str:
            """Remove an item from the pantry. Use when user says 'remove chicken breast from the pantry', 'take eggs out of pantry', 'we used up the milk'. Can match by name or partial name."""
            if "=" in item_name:
                item_name = item_name.split("=")[-1].strip("'\"")
            return app.fresh_remove_pantry(item_name.strip("'\""))

        @tool
        def app_check_shopping(item_name: str) -> str:
            """Check off a shopping list item (mark as bought). Use when user says 'I got the milk', 'bought the eggs', 'check off bananas'. Can match by name or partial name."""
            if "=" in item_name:
                item_name = item_name.split("=")[-1].strip("'\"")
            return app.fresh_check_shopping(item_name.strip("'\""))

        @tool
        def app_list_goals(user: str = "") -> str:
            """List fitness goals for a household member, or for everyone.

            Use when the user asks 'what are my fitness goals', 'show the
            goals for <member>', 'list goals'. Optionally specify a member
            id from the FitFam roster.
            """
            u = user.strip("'\"") if user else None
            return app.fit_list_goals(u)

        @tool
        def app_trivia_stats(dummy: str = "") -> str:
            """Get trivia game history and statistics. Use when user asks about 'trivia scores', 'quiz history', 'how did we do in trivia'."""
            return app.trivia_get_stats()

        @tool
        def app_open(app_name: str) -> str:
            """Open a PuffPAI application in the browser. Available apps: trivia, calendar, alarm, freshtable, fitfam. Use when the user wants to 'play trivia', 'open the calendar', 'launch the timer app', 'open freshtable', 'open fitfam'. Pass the app name like 'trivia' or 'calendar'."""
            from puffpai.app_data import open_app, get_app_paths
            name = app_name.strip().strip("'\"").lower()
            # Normalize common variations
            aliases = {
                'trivia game': 'trivia', 'trivia': 'trivia',
                'quiz': 'trivia', 'play trivia': 'trivia',
                'calendar': 'calendar', 'schedule': 'calendar',
                'alarm': 'alarm', 'timer': 'alarm', 'alarmflow': 'alarm',
                'freshtable': 'freshtable', 'pantry': 'freshtable',
                'shopping': 'freshtable', 'grocery': 'freshtable',
                'fitfam': 'fitfam', 'fitness': 'fitfam', 'workout': 'fitfam',
            }
            app_id = aliases.get(name, name)
            available = get_app_paths()
            if app_id not in available:
                app_list = ", ".join(available.keys())
                return f"App '{name}' not found. Available apps: {app_list}"
            if open_app(app_id):
                labels = {
                    'trivia': 'Trivia Game', 'calendar': 'Family Calendar',
                    'alarm': 'AlarmFlow (Timers & Alarms)',
                    'freshtable': 'FreshTable (Pantry & Shopping)',
                    'fitfam': 'FitFam (Fitness Tracker)',
                }
                return f"Opened {labels.get(app_id, app_id)} in your browser."
            return f"Could not open {app_id}."

        @tool
        def app_trivia_question(category: str = "random") -> str:
            """Generate a trivia question for the user to answer in chat. Categories: science, history, geography, movies, music, sports, technology, nature, literature, random. Difficulty is mid by default. Use when user says 'quiz me', 'ask me a question', 'trivia question about science'."""
            import random
            # Expanded offline trivia bank matching the Trivia app's categories
            questions = {
                "science": [
                    ("What is the chemical symbol for gold?", ["Au", "Ag", "Fe", "Cu"], 0),
                    ("What planet is known as the Red Planet?", ["Venus", "Mars", "Jupiter", "Saturn"], 1),
                    ("What gas do plants absorb?", ["Oxygen", "Nitrogen", "Carbon dioxide", "Helium"], 2),
                    ("What is the hardest natural substance?", ["Quartz", "Diamond", "Topaz", "Ruby"], 1),
                    ("How many bones in the adult human body?", ["186", "206", "226", "246"], 1),
                    ("What is the speed of light in km/s?", ["200,000", "300,000", "400,000", "500,000"], 1),
                    ("Which element has atomic number 1?", ["Helium", "Hydrogen", "Lithium", "Carbon"], 1),
                    ("What organ produces insulin?", ["Liver", "Kidney", "Pancreas", "Stomach"], 2),
                ],
                "history": [
                    ("In what year did World War II end?", ["1943", "1944", "1945", "1946"], 2),
                    ("Who was the first US President?", ["Adams", "Jefferson", "Washington", "Franklin"], 2),
                    ("The Great Wall protected against which group?", ["Japanese", "Mongols", "Vikings", "Persians"], 1),
                    ("Who built the pyramids at Giza?", ["Romans", "Greeks", "Egyptians", "Persians"], 2),
                    ("When did the Berlin Wall fall?", ["1987", "1988", "1989", "1990"], 2),
                    ("Who wrote the Declaration of Independence?", ["Franklin", "Adams", "Jefferson", "Hamilton"], 2),
                    ("What year did the Titanic sink?", ["1910", "1912", "1914", "1916"], 1),
                    ("Who was the first person to walk on the moon?", ["Buzz Aldrin", "Neil Armstrong", "John Glenn", "Yuri Gagarin"], 1),
                ],
                "geography": [
                    ("What is the largest ocean on Earth?", ["Atlantic", "Indian", "Pacific", "Arctic"], 2),
                    ("What is the capital of Australia?", ["Sydney", "Melbourne", "Canberra", "Brisbane"], 2),
                    ("What is the longest river?", ["Amazon", "Nile", "Mississippi", "Yangtze"], 1),
                    ("Which country has the most time zones?", ["Russia", "USA", "France", "China"], 2),
                    ("What is the smallest country?", ["Monaco", "Liechtenstein", "Vatican City", "San Marino"], 2),
                    ("What is the driest continent?", ["Africa", "Australia", "Antarctica", "Asia"], 2),
                    ("Mount Everest is in which mountain range?", ["Alps", "Andes", "Himalayas", "Rockies"], 2),
                    ("What country has the most people?", ["USA", "India", "China", "Indonesia"], 1),
                ],
                "movies": [
                    ("Who directed Jurassic Park?", ["James Cameron", "Steven Spielberg", "George Lucas", "Ridley Scott"], 1),
                    ("What year was the first Star Wars released?", ["1975", "1977", "1979", "1980"], 1),
                    ("Who played Jack in Titanic?", ["Brad Pitt", "Leonardo DiCaprio", "Tom Cruise", "Matt Damon"], 1),
                    ("What is the highest-grossing film of all time?", ["Titanic", "Avatar", "Endgame", "Star Wars"], 1),
                    ("Who voices Woody in Toy Story?", ["Tim Allen", "Tom Hanks", "Robin Williams", "Billy Crystal"], 1),
                    ("In The Matrix, what color pill does Neo take?", ["Blue", "Red", "Green", "Yellow"], 1),
                    ("What is Darth Vader's real name?", ["Luke Skywalker", "Anakin Skywalker", "Han Solo", "Obi-Wan"], 1),
                    ("Who played Iron Man in the MCU?", ["Chris Evans", "Robert Downey Jr.", "Chris Hemsworth", "Mark Ruffalo"], 1),
                ],
                "music": [
                    ("Who is known as the King of Pop?", ["Elvis", "Michael Jackson", "Prince", "Freddie Mercury"], 1),
                    ("What band was John Lennon in?", ["Rolling Stones", "The Who", "The Beatles", "Led Zeppelin"], 2),
                    ("What instrument has 88 keys?", ["Guitar", "Violin", "Piano", "Drums"], 2),
                    ("Who sang 'Bohemian Rhapsody'?", ["Led Zeppelin", "The Beatles", "Queen", "Pink Floyd"], 2),
                    ("What genre originated in Jamaica?", ["Blues", "Jazz", "Reggae", "Soul"], 2),
                    ("How many strings on a standard guitar?", ["4", "5", "6", "7"], 2),
                    ("Who is known as the Queen of Soul?", ["Whitney Houston", "Aretha Franklin", "Diana Ross", "Tina Turner"], 1),
                    ("What country is K-pop from?", ["Japan", "South Korea", "China", "Thailand"], 1),
                ],
                "sports": [
                    ("How many players on a soccer team?", ["9", "10", "11", "12"], 2),
                    ("In which sport is a shuttlecock used?", ["Tennis", "Badminton", "Squash", "Cricket"], 1),
                    ("How many periods in an ice hockey game?", ["2", "3", "4", "5"], 1),
                    ("What country invented basketball?", ["Canada", "USA", "UK", "France"], 1),
                    ("How many points is a touchdown worth?", ["3", "5", "6", "7"], 2),
                    ("What sport uses a 'wicket'?", ["Baseball", "Golf", "Cricket", "Tennis"], 2),
                    ("Which Olympic sport uses a foil?", ["Archery", "Fencing", "Javelin", "Rowing"], 1),
                    ("How long is a marathon in miles?", ["20.2", "24.2", "26.2", "28.2"], 2),
                ],
                "technology": [
                    ("Who co-founded Apple?", ["Bill Gates", "Steve Jobs", "Jeff Bezos", "Mark Zuckerberg"], 1),
                    ("What does 'HTTP' stand for?", ["HyperText Transfer Protocol", "High Tech Transfer Program", "Hyper Terminal Transfer Protocol", "Home Text Transfer Protocol"], 0),
                    ("What year was the iPhone released?", ["2005", "2006", "2007", "2008"], 2),
                    ("What language is Android primarily written in?", ["Python", "Java", "C++", "Swift"], 1),
                    ("What does 'CPU' stand for?", ["Central Process Unit", "Central Processing Unit", "Computer Personal Unit", "Central Program Utility"], 1),
                    ("Who created Linux?", ["Bill Gates", "Steve Wozniak", "Linus Torvalds", "Dennis Ritchie"], 2),
                    ("What does 'AI' stand for?", ["Automatic Intelligence", "Artificial Intelligence", "Applied Intelligence", "Automated Integration"], 1),
                    ("What company makes the PlayStation?", ["Microsoft", "Nintendo", "Sony", "Sega"], 2),
                ],
                "nature": [
                    ("What is the fastest land animal?", ["Lion", "Cheetah", "Horse", "Gazelle"], 1),
                    ("How many legs does a spider have?", ["6", "8", "10", "12"], 1),
                    ("What is the largest mammal?", ["Elephant", "Blue whale", "Giraffe", "Hippo"], 1),
                    ("What type of animal is a Komodo dragon?", ["Dinosaur", "Lizard", "Snake", "Crocodile"], 1),
                    ("Which bird can mimic human speech?", ["Eagle", "Parrot", "Owl", "Crow"], 1),
                    ("What is the tallest tree species?", ["Oak", "Redwood", "Pine", "Maple"], 1),
                    ("How many hearts does an octopus have?", ["1", "2", "3", "4"], 2),
                    ("What is the largest land animal?", ["Hippo", "Rhino", "Elephant", "Giraffe"], 2),
                ],
                "literature": [
                    ("Who wrote Romeo and Juliet?", ["Dickens", "Shakespeare", "Austen", "Twain"], 1),
                    ("What is the first Harry Potter book?", ["Chamber of Secrets", "Prisoner of Azkaban", "Philosopher's Stone", "Goblet of Fire"], 2),
                    ("Who wrote 1984?", ["Aldous Huxley", "George Orwell", "Ray Bradbury", "H.G. Wells"], 1),
                    ("What animal is Moby Dick?", ["Shark", "Whale", "Dolphin", "Squid"], 1),
                    ("Who wrote The Great Gatsby?", ["Hemingway", "Fitzgerald", "Steinbeck", "Faulkner"], 1),
                    ("What is Sherlock Holmes' address?", ["221A Baker St", "221B Baker St", "221C Baker St", "222B Baker St"], 1),
                    ("Who wrote Pride and Prejudice?", ["Bronte", "Austen", "Shelley", "Eliot"], 1),
                    ("In The Hobbit, what is Bilbo's last name?", ["Gamgee", "Took", "Baggins", "Brandybuck"], 2),
                ],
            }

            cat = category.strip().lower().replace(" ", "_")
            cat_aliases = {
                'pop_culture': 'movies', 'pop culture': 'movies',
                'general': 'random', 'trivia': 'random',
            }
            cat = cat_aliases.get(cat, cat)

            if cat == 'random' or cat not in questions:
                # Pick from any category
                all_qs = []
                for qs in questions.values():
                    all_qs.extend([(q, cat_name) for q in qs
                                   for cat_name in [k for k, v in questions.items() if qs is v][:1]])
                q_data, cat = random.choice(all_qs)
            else:
                q_data = random.choice(questions[cat])

            question, options, answer_idx = q_data
            letters = ['A', 'B', 'C', 'D']
            opts_str = "  ".join(f"({letters[i]}) {opt}" for i, opt in enumerate(options))
            correct = letters[answer_idx]

            # NO stage direction here. "Ask the user to pick A, B, C, or D. Do
            # NOT reveal the answer yet" would be this tool talking to the
            # orchestrating model, and it reaches the user verbatim on every
            # single question. An instruction to the model belongs in the
            # prompt; a tool result is the thing a person reads.
            return (f"[Trivia — {cat.replace('_', ' ').title()}]\n"
                    f"{question}\n{opts_str}\n"
                    f"[answer={correct}]")

        @tool
        def app_trivia_generate(
            topic: str,
            difficulty: str = "medium",
            grade_level: str = "",
        ) -> str:
            """Generate a brand new trivia question about ANY topic using the local LLM.

            Use this when the user wants a custom trivia question on a topic that's
            not in the static bank, OR when they want to study a specific subject
            (like school curriculum). Examples: "quiz me on the Civil War",
            "quiz me on 8th grade biology", "trivia about Greek mythology".

            Args:
                topic: What the question should be about (e.g. "World War 2", "8th grade biology", "Greek mythology").
                difficulty: easy, medium, or hard (default medium).
                grade_level: Optional school grade level for studying (e.g. "8th grade", "high school AP biology").
            """
            from puffpai.config import get_config
            import requests as _requests
            import re as _re
            cfg = get_config()
            difficulty = (difficulty or "medium").lower().strip()
            if difficulty not in ("easy", "medium", "hard"):
                difficulty = "medium"
            audience = grade_level.strip() if grade_level else "a curious adult"

            prompt = (
                f"Write ONE multiple-choice trivia question about: {topic}.\n"
                f"Difficulty: {difficulty}. Audience: {audience}.\n\n"
                f"Format your response EXACTLY like this with no extra text:\n"
                f"QUESTION: <the question>\n"
                f"A) <option>\n"
                f"B) <option>\n"
                f"C) <option>\n"
                f"D) <option>\n"
                f"ANSWER: <single letter A, B, C, or D>\n"
                f"FACT: <one short interesting fact about the answer>"
            )
            _model_name = cfg.ollama.model
            # Diagnosability: the reason string
            # below is handed to trivia_game.parse_question, which DISCARDS it,
            # so without a log line a real failure is indistinguishable from
            # any other. Every exit through _no_question emits exactly one
            # WARNING line first.
            _eval_count = {"n": None}

            # The reason string a caller may show. Everything below returns
            # either a parseable question or one of these, never "".
            def _no_question(why, code="unspecified"):
                _logger.warning(
                    "trivia_generate_failed reason=%s model=%s eval_count=%s "
                    "detail=%r", code, _model_name, _eval_count["n"], str(why))
                return f"I couldn't generate a question right now: {why}"

            try:
                request = {
                    "model": _model_name,
                    "stream": False,
                    "messages": [{"role": "user", "content": prompt}],
                    # num_ctx and the pin come from the shared resolver;
                    # without num_ctx ollama sizes the runner from the
                    # model's trained context and starts a second copy.
                    "keep_alive": _model_options.keep_alive(),
                    "options": _model_options.chat_options(
                        temperature=0.7,
                        # 300 was the measured failure on gpt-oss:20b: the
                        # low-effort reasoning preamble consumed the entire
                        # budget and content came back EMPTY. A family that
                        # reasons regardless of the flag gets a budget with
                        # room for the preamble AND the question; every other
                        # family keeps the tight 300. Generation still stops
                        # at the model's own stop token, so the wider cap
                        # costs nothing when it is not needed.
                        num_predict_override=_model_options
                        .structured_num_predict(_model_name, 300)),
                }
                # Per-family, not a literal: qwen3 takes a boolean, gpt-oss
                # takes an effort LEVEL and ignores a boolean outright, so a
                # plain "think": False changes nothing for it.
                request.update(_model_options.thinking_control(_model_name))
                resp = _requests.post(
                    f"{cfg.ollama.url}/api/chat",
                    json=request,
                    timeout=60,
                )
                payload = resp.json()
            except Exception as e:
                return _no_question(e, "request_failed")

            # An ollama error comes back as {"error": "..."} with NO "message"
            # key, usually on a 404 ("model not found"). A plain
            # .get("message", {}).get("content", "") turns every one of those
            # into an EMPTY STRING, which the caller cannot tell apart from
            # "the model wrote something I could not parse", so a topic round
            # would fall back to the bundled bank with no idea why. Status and
            # error are checked first, and nothing below this point can return "".
            if not isinstance(payload, dict):
                return _no_question(
                    "the model answered in a shape I don't understand",
                    "bad_response_shape")
            if isinstance(payload.get("eval_count"), int):
                _eval_count["n"] = payload["eval_count"]
            if payload.get("error"):
                return _no_question(str(payload["error"])[:200], "model_error")
            if getattr(resp, "status_code", 200) >= 400:
                return _no_question(
                    f"the model service answered {resp.status_code}",
                    "http_%s" % resp.status_code)
            message = payload.get("message") or {}
            # ONLY ``content`` is ever parsed. ``thinking`` is the model's
            # hidden channel: scratch work, not an answer, and showing it to a
            # player would be presenting reasoning as a quiz.
            content = (message.get("content") or "").strip()
            if not content:
                # A reasoning model that spends its whole num_predict budget
                # thinking returns an empty content with a full "thinking". Say
                # which of the two happened; both are actionable, and neither is
                # "here is a question".
                if (message.get("thinking") or "").strip():
                    return _no_question(
                        "the model was still thinking when it ran out of room "
                        "to answer",
                        "empty_content_reasoning_exhausted")
                return _no_question("the model returned nothing",
                                    "empty_content")

            # Parse the structured response
            q_match = _re.search(r"QUESTION:\s*(.+?)(?:\n|$)", content)
            opts = _re.findall(r"^([ABCD])\)\s*(.+?)$", content, _re.MULTILINE)
            ans_match = _re.search(r"ANSWER:\s*([ABCD])", content)
            fact_match = _re.search(r"FACT:\s*(.+?)(?:\n|$)", content)

            if not (q_match and len(opts) >= 4 and ans_match):
                # NOT the raw content. Unlabelled prose carries no [answer=] tag,
                # so the caller reads it as "no question" regardless, and
                # anything that DID reach a user this way was the model's
                # scratch work presented as a quiz.
                return _no_question(
                    "the model did not answer in the question format",
                    "unparseable_format")

            question = q_match.group(1).strip()
            options_str = "  ".join(f"({l}) {t.strip()}" for l, t in opts[:4])
            correct = ans_match.group(1)
            fact = fact_match.group(1).strip() if fact_match else ""

            label = f"Trivia — {topic}" + (f" ({audience})" if grade_level else "")
            # No stage direction -- see the bank tool above.
            return (
                f"[{label}]\n"
                f"{question}\n{options_str}\n"
                f"[answer={correct}|fact={fact}]"
            )

        # ── Automation tools (Node-RED webhook-backed) ──────────────

        # Helper to unpack AutomationClient.call() which returns a
        # (success, data) tuple
        def _automation_call(webhook_name, payload, primary_key=None):
            try:
                from puffpai.automation_client import (
                    AutomationClient, switch_refusal)
                # A flow that reaches the internet with its Bridge switch off
                # is not started; the sentence names the switch.
                refused = switch_refusal(webhook_name)
                if refused:
                    return refused
                client = AutomationClient()
                success, data = client.call(webhook_name, payload)
                if success and isinstance(data, dict):
                    if primary_key and primary_key in data:
                        return str(data[primary_key])
                    return str(data.get("data", data))
                return f"{webhook_name} unavailable: {data}"
            except Exception as e:
                return f"{webhook_name} error: {e}"

        @tool
        def auto_daily_briefing() -> str:
            """Get a full daily briefing including weather, calendar, and news. Use when user says 'daily briefing', 'morning update', 'brief me'."""
            return _automation_call("daily_briefing",
                             {"date": datetime.now().strftime("%Y-%m-%d")},
                             primary_key="briefing")

        @tool
        def auto_weather_forecast(location: str = "") -> str:
            """Get the real current weather, temperature, and air quality (US AQI, pollution/smog, pollen) at the home location. ONLY use when user explicitly asks about weather, temperature, rain, forecast, air quality, AQI, pollution, smog, pollen, or whether the air outside is safe. The home location comes from config — NEVER ask the user for a city or ZIP; leave `location` empty. Do NOT use for calendar, tasks, events, or scheduling."""
            # Reads the SAME sources as the dashboard weather card, so the
            # model answers from real data instead of inventing a forecast:
            # HA OpenWeatherMap sensors + the cached open-meteo environment
            # fetch (incl. the mold/dust/pollen/asthma breakdown).
            from puffpai.household_geo import household_location
            cfg_loc = household_location() or "your home"
            parts = []
            try:
                def _wst(eid):
                    try:
                        s = self._ha_bridge.get_entity_state(eid).get("state")
                        return None if s in (None, "unknown", "unavailable") else s
                    except Exception:
                        return None
                temp = _wst("sensor.openweathermap_temperature")
                cond = (_wst("sensor.openweathermap_weather")
                        or _wst("sensor.openweathermap_condition"))
                hum = _wst("sensor.openweathermap_humidity")
                if temp is not None:
                    parts.append(f"currently {round(float(temp))}F")
                if cond:
                    parts.append(str(cond))
                if hum is not None:
                    parts.append(f"humidity {round(float(hum))}%")
            except Exception:
                pass
            unset_note = ""
            try:
                from puffpai import dashboard_widgets as _dwid
                env = _dwid.fetch_environment()
                # Unknown coordinates is its own honest answer, not a
                # "forecast cache failed": see puffpai.household_geo.
                unset_note = env.get("unset") or ""
                if env.get("high") is not None:
                    parts.append(f"high {env['high']}F / low {env['low']}F today")
                if env.get("us_aqi") is not None:
                    label = env.get("aqi_label") or ""
                    parts.append(f"air quality AQI {round(env['us_aqi'])}"
                                 + (f" ({label})" if label else ""))
                for name, r in (env.get("air_breakdown") or {}).items():
                    if isinstance(r, dict) and r.get("level", 0) >= 2:
                        parts.append(f"{name} risk {r['label'].lower()}"
                                     f" ({r['detail']})")
            except Exception:
                pass
            if not parts and unset_note:
                return (f"I can't get the forecast for {cfg_loc}: {unset_note} "
                        "Tell the user that plainly, do NOT invent a forecast.")
            if not parts:
                return (f"REAL weather data for {cfg_loc} is unavailable right "
                        "now (sensors and forecast cache both failed). Tell the "
                        "user honestly — do NOT invent a forecast.")
            note = ""
            if location and cfg_loc.split(",")[0].lower() not in location.lower():
                note = (f" (Note: I only have home weather for {cfg_loc}, "
                        f"not {location}.)")
            return f"Weather for {cfg_loc}: " + "; ".join(parts) + "." + note

        @tool
        def auto_news_summary(topics: str = "general") -> str:
            """Get a summary of today's news. ONLY use when user explicitly asks about news or headlines. Do NOT use for calendar, tasks, or scheduling."""
            return _automation_call("daily_news",
                             {"topics": topics, "date": datetime.now().strftime("%Y-%m-%d")},
                             primary_key="news")

        @tool
        def auto_meal_plan(days: int = 7, dietary_preferences: str = "",
                           confirm: bool = False, preserve: str = "") -> str:
            """Plan the week's dinners and save them to the FreshTable meal plan. Use when the user asks to plan meals, 'what should I cook this week', or meal prep. REPLACES every day it plans, so it refuses to overwrite an existing plan: leave confirm=False and relay the question it returns. Pass preserve='sunday,monday' to leave days that already have a dish alone, those days are neither generated nor written."""
            import json as _json
            import re as _re
            # A confirmation that lived only in the chat fastpath would let
            # the agent route call this tool and rewrite a week of the
            # household's decisions with no question asked. The refusal
            # belongs in the TOOL, where every caller meets
            # it; only the guarded fastpath passes confirm=True, and only
            # after the owner has answered the question.
            if not confirm:
                try:
                    _existing = AppData()._load('freshtable').get('mealPlan') or []
                    _day_count = len({str(e.get("day", "")).lower()
                                      for e in _existing
                                      if isinstance(e, dict) and e.get("day")})
                except Exception:
                    _day_count = 0
                if _day_count:
                    try:
                        from puffpai.agents import meal_plan_guard as _mpg
                        _mpg.set_pending(_day_count, "auto_meal_plan",
                                         dietary_preferences)
                    except Exception:
                        pass
                    return (f"This will replace your current {_day_count}-day "
                            f"plan and rebuild the shopping list. Say 'yes, "
                            f"replace the plan' to continue.")
            prefs = f" Dietary preferences: {dietary_preferences}. Every dish must satisfy this." if dietary_preferences else ""
            n_days = max(1, min(int(days or 7), 7))
            # A day the household has already decided is neither generated
            # nor written. `preserve` names those days; the plan is generated
            # for the REMAINING days only and laid onto them in order.
            _keep = {d.strip().lower() for d in _re.split(r"[,;/]|\s+",
                                                          str(preserve or ""))
                     if d.strip()}
            _targets = [d for d in AppData._MEAL_DAYS[:n_days]
                        if d not in _keep]
            if _keep and not _targets:
                return ("Every day in the plan is already set — nothing was "
                        "changed.")
            if _keep:
                n_days = len(_targets)
            _for_days = (("one for each of: " + ", ".join(_targets) + ". ")
                         if _keep else "one for each day starting monday. ")
            prompt = (
                f"Plan {n_days} DIFFERENT family dinners, " + _for_days +
                "Varied proteins, home-cook-friendly, 5-8 core "
                f"ingredients each.{prefs} Return ONLY valid JSON in EXACTLY this "
                f"form ({n_days} items in the array): "
                "{\"meals\":[{\"day\":\"monday\",\"dish\":\"Dish Name\","
                "\"ingredients\":[\"item1\",\"item2\"]}]}"
            )
            plan = None
            try:
                # qwen3 + format=json returns {"meals":[...]}; be robust to a bare
                # array or prose-wrapped output too, then fall back to recipes.
                raw = self._llm_chat_json(prompt, model="qwen3:30b-a3b")
                obj = _json.loads(raw)
                if isinstance(obj, dict):
                    plan = obj.get("meals") or obj.get("plan") or obj.get("dinners")
                elif isinstance(obj, list):
                    plan = obj
                if not isinstance(plan, list) or not plan:
                    m = _re.search(r"\[.*\]", raw, _re.S)
                    if m:
                        plan = _json.loads(m.group(0))
            except Exception:
                plan = None
            if not isinstance(plan, list) or not plan:
                # Offline fallback: rotate saved recipes
                data = AppData()._load('freshtable')
                recipes = data.get('recipes', [])
                if not recipes:
                    return ("I couldn't generate a plan and there are no saved "
                            "recipes to fall back on.")
                plan = [{"day": d, "dish": recipes[i % len(recipes)]["name"],
                         "recipeId": recipes[i % len(recipes)]["id"],
                         "ingredients": []}
                        for i, d in enumerate(_targets)]
            app = AppData()
            # This loop REPLACES every day it touches and then rebuilds the
            # whole shopping list from the result, so a mistake can lose a
            # week of the household's own decisions. Take a generational
            # backup here, before the first overwrite, so
            # every caller — fastpath and agent route alike — is recoverable.
            # Best-effort by design: a backup that cannot be written must not
            # stop the plan the user asked for.
            try:
                from puffpai import app_data_merge as _adm
                _before = app._load('freshtable')
                if _before.get('mealPlan') or _before.get('shoppingList'):
                    _adm.rotate_backup(app.data_path('freshtable'), _before)
            except Exception:
                pass
            saved = []
            for index, entry in enumerate(plan[:7]):
                day = str(entry.get("day", "")).lower()
                dish = entry.get("dish") or entry.get("meal") or ""
                if _keep:
                    # The model is asked for the remaining days but labels them
                    # however it likes; the days it was given are authoritative
                    # so a preserved day can never be written over.
                    if index >= len(_targets):
                        break
                    day = _targets[index]
                if not dish or day not in AppData._MEAL_DAYS or day in _keep:
                    continue
                ing = entry.get("ingredients") or []
                notes = ("ingredients: " + ", ".join(str(x) for x in ing[:10])) if ing else None
                app.fresh_set_meal(day, "dinner", dish, notes=notes,
                                   recipe_id=entry.get("recipeId"))
                saved.append(f"{day.capitalize()}: {dish}")
            if not saved:
                return "The meal plan came back empty — try again."
            app.fresh_grocery_from_plan()
            return ("Dinner plan saved to FreshTable:\n  - "
                    + "\n  - ".join(saved)
                    + "\nGrocery list updated from the plan. Say 'order my "
                      "groceries' when you're ready.")

        @tool
        def meal_set(meal_spec: str) -> str:
            """Set ONE meal in the FreshTable plan. Format: 'day | meal | dish | ingredients: a, b, c' (ingredients optional). Example: 'wednesday | dinner | Chicken tacos | ingredients: tortillas, chicken thighs, salsa'."""
            parts = [p.strip() for p in meal_spec.split("|")]
            if len(parts) < 3:
                return "Format: 'day | breakfast/lunch/dinner | dish | ingredients: ...'"
            notes = parts[3] if len(parts) > 3 else None
            return AppData().fresh_set_meal(parts[0], parts[1], parts[2], notes=notes)

        @tool
        def meal_plan_show(dummy: str = "") -> str:
            """Show the current FreshTable meal plan for the week."""
            plan = AppData().fresh_get_meal_plan()
            if not plan:
                return ("No meals planned yet. Ask me to 'plan this week's "
                        "meals' and I'll fill the week.")
            lines = [f"  - {e['day'].capitalize()} {e['meal']}: {e['dish']}"
                     for e in plan]
            return f"Meal plan ({len(plan)} entries):\n" + "\n".join(lines)

        @tool
        def grocery_order(dummy: str = "") -> str:
            """Build the grocery order draft from the shopping list. Use when the user says 'order my groceries', 'grocery run', 'put in my grocery order'. Produces a cart-ready list - the USER reviews and checks out (PuffPAI never spends money)."""
            app = AppData()
            app.fresh_grocery_from_plan()
            order = app.fresh_build_grocery_order()
            if not order["items"]:
                return ("The shopping list is empty - plan meals first or add "
                        "items, then I'll build the grocery order.")
            lines = []
            for i in order["items"][:20]:
                link = " - " + i["url"] if i.get("url") else ""
                lines.append(f"  - {i['quantity']}x {i['name']}{link}")
            more = ("" if order["item_count"] <= 20
                    else f"\n  ...plus {order['item_count'] - 20} more "
                         "(see FreshTable).")
            store = order.get("store") or ""
            where = f" for {store}" if store else ""
            if order["items"][0].get("url"):
                tail = ("Each link opens that item's search at the store you "
                        "configured - add to cart and check out there")
            else:
                tail = ("No store is configured, so the draft is the list "
                        "itself - set grocery_store_name and "
                        "grocery_search_url if you want per-item order links")
            return (f"Grocery order draft ready{where} "
                    f"({order['item_count']} items). {tail} "
                    f"(I never place the payment myself):\n"
                    + "\n".join(lines) + more)

        @tool
        def auto_grocery_list(store: str = "") -> str:
            """Rebuild the grocery list from the meal plan and show it. ONLY use when the user asks about groceries. (Local — replaced the dead automation webhook.)"""
            app = AppData()
            app.fresh_grocery_from_plan()
            return app.fresh_list_shopping()

        @tool
        def list_pending_suggestions(dummy: str = "") -> str:
            """List what PuffPAI noticed about recently uploaded documents and is waiting to hear back on. Use when the user asks 'what did I just send you', 'anything waiting on me', 'what did you find in that file', or asks about a document they uploaded. Read-only — accepting a suggestion happens on the dashboard, never here."""
            from puffpai import suggestions as _suggestions
            pending = _suggestions.list_pending()
            if not pending:
                return ("Nothing waiting — I haven't found anything in a "
                        "recent upload that needs your say-so.")
            lines = []
            for s in pending[:10]:
                extra = ""
                if s["action"]["type"] == "calendar_import":
                    n = len(s["action"]["params"].get("candidates") or [])
                    extra = f" ({n} calendar event(s) ready to review)"
                # message embeds the uploader-chosen doc_name; cap it so a
                # pathological filename can't bloat the agent's tool context.
                msg = str(s.get("message") or "")[:120]
                lines.append(f"  - {msg}{extra}")
            more = ("" if len(pending) <= 10
                    else f"\n  …plus {len(pending) - 10} more.")
            return (f"{len(pending)} thing(s) waiting on you:\n"
                    + "\n".join(lines) + more
                    + "\nTell them to accept or dismiss these on the dashboard "
                      "— I don't add anything on my own.")

        @tool
        def auto_health_check() -> str:
            """Run a system health check on PuffPAI services. Use when user asks 'is everything working', 'system status', 'health check', 'diagnostics'."""
            return _automation_call("health_check",
                             {"timestamp": datetime.now().isoformat()},
                             primary_key="report")

        # ── Product-agent team: see the roster / route a task to one agent ──
        # Descriptions are plain-English and read aloud, so keep them crisp.
        _TEAM_DESCRIPTIONS = {
            "concierge": "your main assistant that understands what you ask and hands it to the right specialist",
            "steward": "home control for lights, thermostat, scenes, cameras, and media playback",
            "archivist": "memory and knowledge that remembers what you've told it and searches your documents and library",
            "sentinel": "the security guard that watches the network, approves or blocks devices, and vetoes anything unsafe",
            "majordomo": "the scheduler that runs your briefings, automations, and timed tasks",
            "caretaker": "the system watchdog that checks PuffPAI's own services are healthy and can restart them",
            "toolsmith": "web and workspace tools that fetch and search the web and work with sandboxed files",
        }
        # Canonical read-only default intent + payload key for each agent.
        _TEAM_ROUTES = {
            "concierge": ("chat", "text"),
            "archivist": ("query", "query"),
            "steward": ("list_devices", None),
            "sentinel": ("security_status", None),
            "majordomo": ("list_tasks", None),
            "caretaker": ("health", None),
            "toolsmith": ("web_search", "query"),
        }
        _TEAM_ALIASES = {
            "sentinel_agent": "sentinel", "security": "sentinel", "guard": "sentinel",
            "home": "steward", "house": "steward", "lights": "steward",
            "memory": "archivist", "knowledge": "archivist", "librarian": "archivist",
            "scheduler": "majordomo", "automation": "majordomo", "automations": "majordomo",
            "butler": "majordomo", "watchdog": "caretaker", "system": "caretaker",
            "health": "caretaker", "tools": "toolsmith", "web": "toolsmith",
            "brain": "concierge", "assistant": "concierge", "router": "concierge",
        }

        def _team_bus():
            """Reach the ONE live agent bus the running server already uses."""
            try:
                from puffpai.api_server import _get_agent_team
                return _get_agent_team()
            except Exception:
                return None

        def _resolve_agent_name(raw, valid):
            key = (raw or "").strip().strip("'\"").lower().replace(" ", "_")
            if key in valid:
                return key
            if key in _TEAM_ALIASES and _TEAM_ALIASES[key] in valid:
                return _TEAM_ALIASES[key]
            try:
                from puffpai.agents.preferences import display_name
                for n in valid:
                    if display_name(n).lower().replace(" ", "_") == key:
                        return n
            except Exception:
                pass
            return None

        def _agent_reply_to_speech(name, reply):
            """Turn an agent's reply dict into one spoken sentence."""
            disp = name
            try:
                from puffpai.agents.preferences import display_name
                disp = display_name(name)
            except Exception:
                pass
            if not isinstance(reply, dict):
                return str(reply)
            if reply.get("denied"):
                return (f"The {disp} was not allowed to do that: "
                        f"{reply.get('reason', 'no reason given')}.")
            if reply.get("aborted") == "budget":
                return f"The {disp} ran out of its time or action budget before finishing."
            if reply.get("ok") is False:
                return (f"The {disp} couldn't handle that: "
                        f"{reply.get('error', 'unknown error')}.")
            for k in ("reply", "answer", "report", "summary", "message", "note"):
                v = reply.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
            for k in ("devices", "tasks", "alerts", "results", "queue"):
                v = reply.get(k)
                if isinstance(v, list):
                    labels = []
                    for it in v[:5]:
                        if isinstance(it, dict):
                            labels.append(str(it.get("friendly_name")
                                              or it.get("name")
                                              or it.get("title")
                                              or it.get("id") or "").strip())
                        else:
                            labels.append(str(it))
                    labels = [x for x in labels if x]
                    tail = (", including " + ", ".join(labels)) if labels else ""
                    return f"The {disp} reported {len(v)} {k}{tail}."
            # Health-check reply shape (caretaker): {"ok","healthy","total","checks"}
            if "healthy" in reply:
                tot = reply.get("total")
                checks = reply.get("checks") or []
                if reply.get("healthy"):
                    n = tot if tot is not None else (len(checks) or None)
                    return (f"The {disp} reports everything is healthy"
                            + (f", all {n} checks passing." if n else "."))
                bad = [c for c in checks if isinstance(c, dict)
                       and not c.get("ok", c.get("healthy", True))]
                names = ", ".join(str(c.get("name") or c.get("id") or "?")
                                  for c in bad[:4]) if bad else ""
                tail = f" ({names})" if names else ""
                return (f"The {disp} reports a problem: "
                        f"{len(bad) or 'some'} of {tot or len(checks)} checks are "
                        f"failing{tail}.")
            for k in ("status", "context", "health", "state"):
                v = reply.get(k)
                if v:
                    import json as _json
                    return (f"Here's what the {disp} reported: "
                            + _json.dumps(v, default=str)[:500])
            # Last resort: read back the simple fields instead of nothing.
            extras = {k: v for k, v in reply.items()
                      if k != "ok" and not k.startswith("_")
                      and isinstance(v, (str, int, float, bool))}
            if extras:
                import json as _json
                return (f"The {disp} responded: "
                        + _json.dumps(extras, default=str)[:400])
            return f"The {disp} handled that but returned nothing more to read aloud."

        @tool
        def list_agents(dummy: str = "") -> str:
            """List PuffPAI's on-device agent team and what each one does for the user.

            Use when the user asks about "your agents", "the agent team", "who's on
            your team", "which agent handles X", "what can your agents do", or names
            a sub-agent (dispatcher, majordomo, steward, sentinel, archivist,
            caretaker, toolsmith, concierge). Read aloud, conversational.
            """
            bus = _team_bus()
            if bus is not None:
                try:
                    names = sorted(bus.agents().keys())
                except Exception:
                    names = []
            else:
                names = []
            if not names:
                try:
                    from puffpai.agents import roster_summary
                    names = [e["name"] for e in roster_summary()]
                except Exception:
                    names = list(_TEAM_DESCRIPTIONS.keys())
            try:
                from puffpai.agents.preferences import display_name as _dn
            except Exception:
                _dn = lambda n: n
            lines = []
            for n in names:
                desc = _TEAM_DESCRIPTIONS.get(n, "a specialist agent on the team")
                disp = _dn(n)
                label = disp if disp.lower() == n.lower() else f"{disp} ({n})"
                lines.append(f"  - {label}: {desc}")
            head = f"There are {len(lines)} agents on your PuffPAI team:"
            return head + "\n" + "\n".join(lines)

        @tool
        def ask_agent(agent_and_task: str) -> str:
            """Route a task to ONE named PuffPAI agent and speak back its reply.

            Format: 'agent_name | task text'. Examples:
              'archivist | what did I say about the dentist'
              'caretaker | is everything healthy'
              'steward | what devices do you see'
              'sentinel | any security alerts'
            Use when the user says "ask the <agent>...", "have the <agent>...",
            "tell the <agent>...", or "route this to <agent>". For open-ended work
            name the concierge, which plans and delegates. Valid agents: concierge,
            steward, archivist, sentinel, majordomo, caretaker, toolsmith.
            """
            parts = (agent_and_task or "").split("|", 1)
            raw_agent = parts[0]
            task = (parts[1].strip() if len(parts) > 1 else "").strip()

            bus = _team_bus()
            if bus is None:
                return ("The agent team isn't running right now, so I can't route "
                        "that to a specific agent.")
            try:
                valid = set(bus.agents().keys())
            except Exception:
                valid = set(_TEAM_ROUTES.keys())

            name = _resolve_agent_name(raw_agent, valid)
            if name is None:
                return ("I couldn't tell which agent you meant. Valid agents are: "
                        + ", ".join(sorted(valid)) + ".")

            intent, payload_key = _TEAM_ROUTES.get(name, ("chat", "text"))
            payload = {payload_key: task} if payload_key else {}

            # Egress needs BOTH the owner's chat opt-in AND the specific door
            # for this intent. The intent is known here, so the door checked is
            # the exact one — a household that opened web_search but not
            # web_fetch gets exactly that. The Toolsmith re-checks the same door
            # before any socket opens; this is the caller-side half.
            allow_egress = False
            if name == "toolsmith" and intent in ("web_search", "web_fetch"):
                try:
                    from puffpai.security.egress_policy import chat_egress_allowed
                    allow_egress, _why = chat_egress_allowed(intent)
                except Exception:
                    allow_egress = False

            try:
                from puffpai.agents import Envelope, Budget
                env = Envelope(
                    intent=intent,
                    payload=payload,
                    requester="owner",
                    origin_agent="api_server",
                    budget=Budget(max_actions=4, max_seconds=20.0,
                                  allow_egress=allow_egress),
                    state_changing=False,
                )
                reply = bus.send(env, to=name)
            except Exception as exc:
                return f"I reached the {name} but the request failed: {exc}"
            return _agent_reply_to_speech(name, reply)

        @tool
        def describe_capabilities(area: str = "") -> str:
            """Describe what PuffPAI can access and do — her own capabilities.

            Use when the user asks "what can you do", "what can you access",
            "what are your capabilities", "what can you help me with", or asks
            about one area ("what can you do with my home / cameras / calendar").
            Reads the capability registry — the single source of truth for which
            tool owns each capability and which surfaces (voice, dashboard, app)
            expose it — so the answer is declared fact, not a guess. Voice, the
            dashboard, and the app pages are surfaces of ONE system, not
            separate abilities.
            """
            from puffpai import capability_registry as _capreg
            return _capreg.describe_for(area or "")

        @tool
        def show_on_dashboard(topic: str) -> str:
            """Pull a knowledge article or TED talk up on the dashboard screen.

            Use when the user says "show me / pull up the <talk|video|article>
            about <topic>" and wants to SEE it on the dashboard, not just hear
            it. Searches the offline knowledge base, picks the best match, and
            displays it. Example arg: 'the TED talk about surveillance'.
            """
            from puffpai import dashboard_display as _dd
            item = _dd.show_query(topic, source="agent")
            if not item:
                return f"I couldn't find anything about '{topic}' to show."
            if item.get("deferred"):
                return item["message"]
            return f"Pulling up \"{item['title']}\" on your dashboard."

        @tool
        def clear_dashboard(dummy: str = "") -> str:
            """Clear whatever PuffPAI has pulled up on the dashboard screen."""
            from puffpai import dashboard_display as _dd
            had = _dd.clear_display(source="agent")
            return ("Cleared the dashboard." if had
                    else "The dashboard is already clear.")

        # ── Camera alerts: recent motion/person/doorbell events from HA ──
        def _discover_camera_sensors():
            """Map each real HA camera to its alert binary_sensors (motion/person/etc).

            Closed-world: only entities Home Assistant actually exposes can match.
            Returns {camera_entity_id: {"cam": DeviceInfo, "sensors": [entity_id, ...]}}.
            """
            _STREAM = ("_fluent", "_sub", "_main", "_clear", "_snapshots",
                       "_snapshots_clear", "_balanced")

            def _base(eid: str) -> str:
                obj = eid.split(".", 1)[1] if "." in eid else eid
                for s in _STREAM:
                    if obj.endswith(s):
                        return obj[: -len(s)]
                return obj

            try:
                cams = self._ha_bridge.list_devices(domain="camera",
                                                    controllable_only=False)
            except TypeError:
                cams = [d for d in self._ha_bridge.list_devices()
                        if d.entity_id.startswith("camera.")]
            cam_bases = [(c, _base(c.entity_id)) for c in cams]
            result = {c.entity_id: {"cam": c, "sensors": []} for c in cams}
            if not cams:
                return result

            _KW = ("motion", "person", "people", "visitor", "vehicle", "pet",
                   "package", "sound", "doorbell", "cry", "animal", "face",
                   "occupancy")
            _CLASSES = ("motion", "occupancy", "presence", "sound")

            for raw in self._ha_bridge.get_all_states():
                eid = raw.get("entity_id", "")
                if not eid.startswith("binary_sensor."):
                    continue
                obj = eid.split(".", 1)[1]
                dc = (raw.get("attributes") or {}).get("device_class", "") or ""
                if dc not in _CLASSES and not any(k in obj for k in _KW):
                    continue
                # Assign to the camera whose base name is the longest prefix.
                # Prefix-only: real Reolink sensors are binary_sensor.<base>_motion
                # /_person/... so a strict prefix is exact. (A token-overlap
                # fallback would mis-assign living_room_motion to a
                # dining_room_door camera on the shared word "room".)
                owner, owner_len = None, -1
                for c, base in cam_bases:
                    if base and obj.startswith(base) and len(base) > owner_len:
                        owner, owner_len = c, len(base)
                if owner is not None:
                    result[owner.entity_id]["sensors"].append(eid)
            return result

        def _summarize_camera_alerts(groups) -> str:
            """Speakable recent-alert summary for the given camera groups (REAL HA)."""
            from datetime import datetime, timezone, timedelta

            WINDOW_HOURS = 12
            start_iso = (datetime.now(timezone.utc)
                         - timedelta(hours=WINDOW_HOURS)).isoformat()
            try:
                cur = {r.get("entity_id"): str(r.get("state", "")).lower()
                       for r in self._ha_bridge.get_all_states()}
            except Exception:
                cur = {}

            def _clock(ts):
                try:
                    return (datetime.fromisoformat(ts).astimezone()
                            .strftime("%I:%M %p").lstrip("0"))
                except Exception:
                    return None

            phrases, any_history = [], False
            for info in groups.values():
                cam = info["cam"]
                sensors = info["sensors"]
                name = cam.friendly_name.replace(" Fluent", "").strip() or cam.entity_id
                if not sensors:
                    phrases.append(f"{name} has no motion sensors reporting.")
                    continue

                cur_on = any(cur.get(sid) == "on" for sid in sensors)
                count, last_on = 0, None
                for sid in sensors:
                    try:
                        hist = self._ha_bridge.get_history(sid, start=start_iso)
                    except Exception:
                        continue
                    any_history = True
                    series = hist[0] if hist and isinstance(hist[0], list) else []
                    for pt in series:
                        if str(pt.get("state", "")).lower() == "on":
                            count += 1
                            ts = pt.get("last_changed") or pt.get("last_updated")
                            try:
                                dt = datetime.fromisoformat(ts)
                            except Exception:
                                dt = None
                            if dt and (last_on is None or dt > last_on):
                                last_on = dt

                if cur_on:
                    phrases.append(f"{name} is detecting motion right now.")
                elif count > 0:
                    ev = {1: "once", 2: "twice"}.get(count, f"{count} times")
                    t = _clock(last_on.isoformat()) if last_on else None
                    tail = f", most recently at {t}" if t else ""
                    phrases.append(
                        f"{name} detected motion {ev} in the last "
                        f"{WINDOW_HOURS} hours{tail}.")
                else:
                    phrases.append(f"{name} has been quiet.")

            body = " ".join(phrases)
            if not any_history and not any(v["sensors"] for v in groups.values()):
                return body
            if not any_history:
                return ("I couldn't reach the camera history, so this is the "
                        "current status only. " + body)
            return body

        @tool
        def camera_recent_alerts(dummy: str = "") -> str:
            """Summarize recent alerts from all cameras: motion, person, and doorbell/visitor detections.

            Use this when the user asks things like "any alerts from my cameras?",
            "did the camera see anything?", "anyone at the door?", or "any activity
            on the cameras?". Reads real Home Assistant motion/person/visitor sensors
            and their recent history, and answers conversationally.
            """
            from puffpai.ha_bridge import HABridgeError
            try:
                groups = _discover_camera_sensors()
            except HABridgeError as exc:
                return f"Error reaching Home Assistant: {exc}"
            if not groups:
                return "Home Assistant exposes no camera entities."
            return _summarize_camera_alerts(groups)

        @tool
        def camera_alerts_for(camera_name: str) -> str:
            """Recent alerts for a single named camera (e.g. 'front door' or 'driveway').

            Use when the user asks about one specific camera's activity. Pass the
            camera's name or entity_id; resolved against real Home Assistant entities.
            """
            from puffpai import dashboard_registry as _dreg
            from puffpai.ha_bridge import HABridgeError
            camera_name = camera_name.strip().strip("'\"")
            try:
                matches = _dreg.find_camera_entity(self._ha_bridge, camera_name)
                groups = _discover_camera_sensors()
            except HABridgeError as exc:
                return f"Error reaching Home Assistant: {exc}"
            if not matches:
                return f"I couldn't find a camera matching '{camera_name}'."
            wanted = {d.entity_id for d in matches}
            groups = {eid: g for eid, g in groups.items() if eid in wanted}
            if not groups:
                return f"I couldn't find a camera matching '{camera_name}'."
            return _summarize_camera_alerts(groups)

        # ── Voice vault tools: shared state + helpers ─────────────────────────
        import time as _vault_time
        from collections import deque as _vault_deque
        _vault_rl = {"attempts": _vault_deque(), "lock_until": 0.0}
        _VAULT_RL_WINDOW = 300.0
        _VAULT_RL_MAX = 5
        _VAULT_RL_LOCKOUT = 300.0
        _VAULT_HIDDEN_FILES = {"auto_detected_pii.json"}

        def _vault_rl_check():
            now = _vault_time.monotonic()
            if now < _vault_rl["lock_until"]:
                return False, int(_vault_rl["lock_until"] - now)
            q = _vault_rl["attempts"]
            while q and (now - q[0]) > _VAULT_RL_WINDOW:
                q.popleft()
            if len(q) >= _VAULT_RL_MAX:
                _vault_rl["lock_until"] = now + _VAULT_RL_LOCKOUT
                q.clear()
                return False, int(_VAULT_RL_LOCKOUT)
            return True, 0

        def _vault_rl_note():
            _vault_rl["attempts"].append(_vault_time.monotonic())

        def _vault_tool_instance():
            _api = sys.modules.get('puffpai.api_server')
            if _api is not None and hasattr(_api, '_get_vault'):
                try:
                    v = _api._get_vault()
                    if v is not None:
                        return v
                except Exception:
                    pass
            try:
                from puffpai.security.vault import Vault
                return Vault()
            except Exception:
                return None

        def _vault_friendly_label(stem: str) -> str:
            special = {"ha_token": "Home Assistant token",
                       "service_secrets": "saved service credentials"}
            key = stem.lower()
            if key in special:
                return special[key]
            return stem.replace('_', ' ').replace('-', ' ').strip()

        def _vault_list_stems(vault):
            stems = []
            try:
                d = vault._vault_dir
                if not d.exists():
                    return stems
                for fname in sorted(os.listdir(d)):
                    if fname.startswith('.'):
                        continue
                    if fname in _VAULT_HIDDEN_FILES:
                        continue
                    if fname.startswith('doc_'):
                        continue
                    if not (d / fname).is_file():
                        continue
                    stems.append(fname[:-5] if fname.endswith('.json') else fname)
            except OSError:
                pass
            return stems

        def _vault_extract_value(data):
            if isinstance(data, str):
                return data
            if isinstance(data, dict):
                for k in ("value", "token", "secret", "password", "key"):
                    v = data.get(k)
                    if isinstance(v, str) and v:
                        return v
                strs = [v for v in data.values() if isinstance(v, str) and v]
                if len(strs) == 1:
                    return strs[0]
            return None

        @tool
        def vault_list(dummy: str = "") -> str:
            """List the LABELS of items stored in the user's encrypted vault — names only, never any secret value. Use when the user asks 'what's in my vault', 'what passwords or secrets do you have for me', 'what credentials are stored'. No PIN required; reads labels only."""
            vault = _vault_tool_instance()
            if vault is None:
                return ("Your vault is unavailable right now, so I can't list what's "
                        "stored. Nothing has been unlocked.")
            if not getattr(vault, "is_initialized", False):
                return ("Your vault isn't set up yet, so there's nothing stored. You can "
                        "create it with a PIN from the vault page on your dashboard.")
            stems = _vault_list_stems(vault)
            if not stems:
                return "Your vault is set up but empty — there are no stored items yet."
            labels = [_vault_friendly_label(s) for s in stems]
            try:
                vault._audit.log(action="vault_voice_list", user="voice",
                                 result="success", details=f"{len(labels)} labels")
            except Exception:
                pass
            if len(labels) == 1:
                body = f"your {labels[0]}"
            else:
                body = (", ".join(f"your {l}" for l in labels[:-1])
                        + f", and your {labels[-1]}")
            n = len(labels)
            return (f"You have {n} protected item{'s' if n != 1 else ''} in your vault: "
                    f"{body}. I can read one aloud if you unlock it with your PIN.")

        @tool
        def vault_status(dummy: str = "") -> str:
            """Report whether the encrypted vault is LOCKED or UNLOCKED right now, plus HOW MANY items it protects — never their labels and never a value. Use when the user asks 'is the vault locked', 'is my vault locked', 'vault status', 'what is my vault status'. No PIN required."""
            from puffpai.agents import household_reads
            return household_reads.vault_status(_vault_tool_instance())

        @tool
        def vault_unlock_and_get(item_and_pin: str) -> str:
            """Unlock the encrypted vault with a SPOKEN PIN and read ONE secret aloud. Input format: 'item label | PIN' (for example 'router password | 1234'). Verifies the PIN against the vault; on success returns the item's value to be spoken; on a wrong PIN returns a generic denial without revealing whether the item exists. Use only when the user explicitly asks to hear a stored secret, password, or credential and provides their PIN."""
            # Everything past this line is secret: the argument carries the
            # spoken PIN, and a successful read returns the decrypted value.
            # Mark the turn BEFORE any of it happens, so a WRONG PIN -- whose
            # user half is still the PIN -- is suppressed exactly like a right
            # one. Fail closed: no mark, no secret.
            if not mark_vault_tool_turn_private("vault_unlock_and_get"):
                return ("I can't read a stored secret right now: the guard "
                        "that keeps it out of the transcript isn't answering. "
                        "Nothing was unlocked.")
            raw = item_and_pin or ""
            if "|" not in raw:
                return ("To read a stored secret I need both the item and your PIN, like "
                        "'router password | your PIN'. I won't repeat the PIN back.")
            label_part, pin = raw.rsplit("|", 1)
            label = label_part.strip().strip("'\"").lower()
            pin = pin.strip()
            if not label or not pin:
                return "I need both the item name and your PIN, as 'item name | PIN'."

            allowed, wait = _vault_rl_check()
            if not allowed:
                return (f"Too many unlock attempts. For safety I've paused vault access "
                        f"for about {max(1, wait // 60)} more minute(s). Try again later.")

            vault = _vault_tool_instance()
            if vault is None:
                return ("Your vault is unavailable right now, so I can't unlock anything. "
                        "No secret was retrieved.")
            try:
                from puffpai.security.vault import VaultError, AuthenticationError
            except Exception:
                return ("Your vault module can't be loaded, so access is denied. "
                        "No secret was retrieved.")
            if not getattr(vault, "is_initialized", False):
                return "Your vault isn't set up yet, so there's nothing to unlock."

            _vault_rl_note()
            session = None
            try:
                try:
                    session = vault.authenticate(pin)
                except (AuthenticationError, VaultError):
                    try:
                        vault._audit.log(action="vault_voice_unlock", user="voice",
                                         result="failed", details=f"item={label[:60]}")
                    except Exception:
                        pass
                    return "That PIN didn't match — access denied."

                stems = _vault_list_stems(vault)
                norm = {s.lower().replace('_', ' ').replace('-', ' ').strip(): s
                        for s in stems}
                if label in ("home assistant token", "ha token", "homeassistant token") \
                        and "ha_token" in stems:
                    norm.setdefault(label, "ha_token")
                target = norm.get(label)
                if target is None:
                    for spoken, stem in norm.items():
                        if label in spoken or spoken in label:
                            target = stem
                            break
                if target is None:
                    try:
                        vault._audit.log(action="vault_voice_unlock", user="voice",
                                         result="not_found", details=f"item={label[:60]}")
                    except Exception:
                        pass
                    known = ", ".join(_vault_friendly_label(s) for s in stems) or "nothing"
                    return (f"Your PIN worked, but I couldn't find an item called "
                            f"'{label}'. What I have is: {known}.")

                try:
                    data = vault.read_secure_session(f"{target}.json", session.session_id)
                except (VaultError, AuthenticationError):
                    return ("I unlocked the vault but couldn't decrypt that item. "
                            "No value was retrieved.")
                value = _vault_extract_value(data)
                if not value:
                    return ("That item is stored but has no readable value. "
                            "Nothing was retrieved.")
                try:
                    vault._audit.log(action="vault_voice_unlock", user="voice",
                                     result="success",
                                     details=f"item={_vault_friendly_label(target)}")
                except Exception:
                    pass
                return f"Your {_vault_friendly_label(target)} is: {value}"
            finally:
                if session is not None:
                    try:
                        vault.logout(session.session_id)
                    except Exception:
                        pass

        @tool
        def vault_display_document(description: str) -> str:
            """Set up showing a PROTECTED document from the private vault on the
            dashboard screen — e.g. 'a birth certificate', 'my passport',
            'my Social Security card'. This ONLY recognizes the document TYPE and
            asks the user for their PIN; it NEVER takes or sees a PIN, NEVER looks
            in the vault, and NEVER confirms what is stored. The PIN arrives on
            the user's NEXT turn and is captured by a secure fastpath, not by this
            tool. Use when the user asks to SEE a sensitive/vaulted document."""
            # The challenge is issued from a model-chosen ``description`` while
            # the live-buffer seal is decided from ``heard``, so a request the
            # seal did not recognize gets its challenge onto the wall and into
            # the transcript. Marking here clears both. Fail closed.
            if not mark_vault_tool_turn_private("vault_display_document"):
                return ("I can't set up a protected document right now: the "
                        "guard that keeps this turn out of the transcript "
                        "isn't answering.")
            from puffpai import dashboard_display as _dd
            # Strictly a vault-display setup: only proceed for a recognized
            # sensitive doc TYPE. Never render anything itself.
            if _dd.classify_source(description) != "vault":
                return ("That doesn't look like a protected vault document. For "
                        "your own uploads or the library, just ask me to show "
                        "or read it.")
            item = _dd.show_query(description, source="agent")
            if item and item.get("deferred") == "vault_challenge":
                return item["message"]
            # Fallback: still only the challenge, never a display or a lookup.
            _dd.set_pending_vault(description)
            return _dd.VAULT_CHALLENGE_MSG

        return [
            list_indexed_libraries,
            read_file,
            write_file,
            save_prep_item,
            calculate,
            search_knowledge_base,
            web_search,
            write_code,
            ha_list_devices,
            ha_get_device_info,
            ha_control_device,
            ha_block_device_internet,
            ha_discover_new_devices,
            ha_scan_hardware,
            ha_add_zigbee_dongle,
            # Proposal only — files a consent request, never writes to HA.
            ha_provision_propose,
            ha_run_scene,
            ha_find_entity,
            ha_set_light,
            ha_set_temperature,
            ha_approve_pending_device,
            # Dashboard camera tiles (Bridge lookup + Sentinel consent gate)
            ha_list_cameras,
            dashboard_add_camera,
            dashboard_remove_camera,
            # Modular dashboard widgets (news, calendar, ...)
            dashboard_add_widget,
            dashboard_remove_widget,
            dashboard_list_widgets,
            # Sentinel consent-request loop (user grants/denies in chat)
            sentinel_approve_device,
            sentinel_deny_device,
            sentinel_list_requests,
            privacy_block_device,
            privacy_unblock_device,
            privacy_lockdown_mode,
            privacy_scan_network,
            # Media Server — Jellyfin + Calibre-Web
            media_search_music,
            media_search_movies,
            media_search_books,
            # Librarian (Calibre catalogue + Calibre-web shelves)
            books_find,
            books_recommend,
            books_shelves,
            books_shelf_add,
            books_shelf_remove,
            books_shelf_move,
            books_show,
            media_play,
            media_now_playing,
            media_control,
            media_library_stats,
            media_recommend,
            media_play_playlist,
            media_list_playlists,
            media_playlist_tracks,
            media_playlist_create,
            media_playlist_add,
            media_playlist_remove,
            media_list_artists,
            media_playlist_from_artists,
            media_playlist_from_document,
            media_favorite,
            media_list_favorites,
            # Media Hub — radio / Spotify visibility
            media_radio_play,
            media_radio_stations,
            media_radio_favorites,
            media_radio_stop,
            media_sources_overview,
            # Spotify voice control (signed-in shared account)
            spotify_play,
            spotify_pause,
            spotify_resume,
            spotify_next,
            spotify_whats_playing,
            # Zone 3 — Network Edge Security
            zone3_discover_devices,
            zone3_secure_device,
            zone3_change_mode,
            zone3_status,
            zone3_enforce,
            bridge_list_devices,
            bridge_device_telemetry,
            bridge_ha_buckets,
            bridge_status,
            bridge_refresh,
            prep_add_supply,
            prep_days_remaining,
            prep_set_family_size,
            prep_view_inventory,
            prep_set_location,
            prep_weather_alerts,
            prep_check_expiration,
            prep_recommendations,
            # Application integration tools
            app_set_timer,
            app_set_alarm,
            app_list_alarms,
            app_dismiss_alarm,
            app_delete_alarm,
            app_snooze_alarm,
            app_add_event,
            app_add_task,
            app_list_schedule,
            app_complete_task,
            app_add_med,
            app_take_med,
            app_pay_bill,
            app_delete_event,
            app_delete_task,
            app_vault_med,
            app_edit_event,
            app_edit_task,
            app_add_bill,
            app_add_pantry,
            app_add_shopping,
            app_list_pantry,
            app_list_shopping,
            app_create_recipe,
            app_list_recipes,
            app_remove_pantry,
            app_check_shopping,
            search_my_documents,
            fetch_my_document,
            list_my_documents,
            app_log_workout,
            app_log_set,
            app_delete_workout,
            app_personal_records,
            app_generate_workout,
            app_fitness_status,
            app_list_goals,
            app_trivia_stats,
            # NOTE: app_open intentionally removed from agent tools — agents
            # were calling it in infinite loops after every action. Direct
            # app launching is handled by router.detect_app_launch() instead.
            app_trivia_question,
            app_trivia_generate,
            # Automation tools
            auto_daily_briefing,
            auto_weather_forecast,
            auto_news_summary,
            auto_meal_plan,
            auto_grocery_list,
            # Meal planning + grocery ordering (local-first)
            meal_set,
            meal_plan_show,
            grocery_order,
            auto_health_check,
            # Proactive suggestions from document uploads
            list_pending_suggestions,
            # Agent-team introspection + routing (voice: "list your agents",
            # "ask the archivist ...")
            list_agents,
            ask_agent,
            describe_capabilities,
            show_on_dashboard,
            clear_dashboard,
            # Camera alerts — recent motion/person/doorbell events from HA history
            camera_recent_alerts,
            camera_alerts_for,
            # Encrypted vault — spoken labels + PIN-unlock retrieval
            vault_list,
            vault_status,
            vault_unlock_and_get,
            # Zone-1 protected-document display (challenge-response; PIN never
            # reaches this tool — it is captured by the api_server fastpath).
            vault_display_document,
        ]
                
    def build_context_prompt(self, conversation_history):
        """Build conversation context for agent"""
        if not conversation_history:
            return ""

        context_parts = []
        recent = conversation_history[-5:]
        for i, msg in enumerate(recent):
            context_parts.append(f"User: {msg['user']}")
            # Give the most recent message more context (follow-up questions
            # often refer to the previous answer's data)
            limit = 800 if i == len(recent) - 1 else 400
            asst = msg['assistant']
            truncated = asst[:limit] + "..." if len(asst) > limit else asst
            context_parts.append(f"Assistant: {truncated}")

        return "\n".join(context_parts)

    # ── Media source trustworthiness reference ────────────────────────────────
    # Curated from AllSides, Media Bias/Fact Check, Ad Fontes Media
    MEDIA_TRUST = {
        'Associated Press':     {'bias': 'Center',       'factual': 5},
        'Reuters':              {'bias': 'Center',       'factual': 5},
        'PBS NewsHour':         {'bias': 'Center',       'factual': 5},
        'NPR':                  {'bias': 'Center-Left',  'factual': 5},
        'BBC News':             {'bias': 'Center',       'factual': 5},
        'The Guardian':         {'bias': 'Left-Center',  'factual': 4},
        'The New York Times':   {'bias': 'Left-Center',  'factual': 4},
        'The Washington Post':  {'bias': 'Left-Center',  'factual': 4},
        'The Wall Street Journal': {'bias': 'Center-Right', 'factual': 4},
        'ProPublica':           {'bias': 'Left-Center',  'factual': 5},
        'The Intercept':        {'bias': 'Left',         'factual': 4},
        'Politico':             {'bias': 'Center-Left',  'factual': 4},
        'The Hill':             {'bias': 'Center',       'factual': 4},
        'USA Today':            {'bias': 'Center-Left',  'factual': 4},
        'ABC News':             {'bias': 'Center-Left',  'factual': 4},
        'CBS News':             {'bias': 'Center-Left',  'factual': 4},
        'NBC News':             {'bias': 'Center-Left',  'factual': 4},
        'CNN':                  {'bias': 'Left',         'factual': 3},
        'MSNBC':                {'bias': 'Left',         'factual': 3},
        'Fox News':             {'bias': 'Right',        'factual': 2},
        'Breitbart':            {'bias': 'Far Right',    'factual': 1},
        'The Daily Wire':       {'bias': 'Right',        'factual': 2},
        'Newsmax':              {'bias': 'Far Right',    'factual': 1},
        'OAN':                  {'bias': 'Far Right',    'factual': 1},
        'The Epoch Times':      {'bias': 'Far Right',    'factual': 1},
        'InfoWars':             {'bias': 'Far Right',    'factual': 0},
        'Jacobin':              {'bias': 'Far Left',     'factual': 3},
        'Mother Jones':         {'bias': 'Left',         'factual': 4},
        'The Nation':           {'bias': 'Left',         'factual': 4},
        'Reason':               {'bias': 'Right-Libertarian', 'factual': 4},
        'The Economist':        {'bias': 'Center-Right', 'factual': 5},
        'Al Jazeera':           {'bias': 'Center-Left',  'factual': 4},
        'Democracy Now!':       {'bias': 'Left',         'factual': 4},
    }

    def _assess_source_trust(self, source_name):
        """Return trust info for a news source, or None if unknown."""
        for known, info in self.MEDIA_TRUST.items():
            if known.lower() in source_name.lower() or source_name.lower() in known.lower():
                stars = info['factual']
                label = 'Highly Reliable' if stars >= 4 else 'Mixed Reliability' if stars >= 3 else 'Low Reliability' if stars >= 1 else 'Unreliable'
                return {'name': known, 'bias': info['bias'], 'factual': stars, 'label': label}
        return None

    def _safe_invoke(self, tool_name, arg=''):
        """Invoke a tool by name, returning the result or a short error."""
        try:
            return self.tool_dict[tool_name].invoke(arg)
        except Exception as e:
            return f"Unavailable: {e}"

    def daily_briefing(self, user_name=None, location=None):
        """
        Generate a comprehensive daily briefing by gathering data from
        all sources and having the LLM contextualize and analyze it.
        Bypasses the agent loop for reliability.
        """
        print("[Daily Briefing] Gathering data from all sources...")
        sections = {}

        # Resolve location from argument, config, or default
        if not location:
            try:
                from puffpai.household_geo import household_location
                location = household_location()
            except Exception:
                location = ''

        # 1. Weather
        print(f"[Daily Briefing] Fetching weather for {location}...")
        weather_result = self._safe_invoke('auto_weather_forecast', location)
        if 'unavailable' in weather_result.lower() or 'could not' in weather_result.lower():
            # Try the prep weather alerts as fallback
            weather_result = self._safe_invoke('prep_weather_alerts', 'false')
        sections['weather'] = weather_result

        # 2. Calendar / Schedule
        print("[Daily Briefing] Fetching today's schedule...")
        sections['schedule'] = self._safe_invoke('app_list_schedule', '7')

        # 3. Top News Headlines via web search (gracefully handle disabled)
        print("[Daily Briefing] Fetching top news...")
        news_available = True
        news_result = self._safe_invoke('web_search', 'top news headlines today')
        if '[Web Search Disabled]' in news_result or '[Offline]' in news_result:
            sections['news'] = "Web search is currently offline. No live news today."
            news_available = False
        else:
            sections['news'] = news_result

        # 4. Emergency Prep — expiring supplies
        print("[Daily Briefing] Checking emergency supplies...")
        sections['prep'] = self._safe_invoke('prep_check_expiration')

        # 5. Network Security Status
        print("[Daily Briefing] Checking network security...")
        sections['security'] = self._safe_invoke('zone3_status')

        # 6. Fitness status
        print("[Daily Briefing] Checking fitness status...")
        sections['fitness'] = self._safe_invoke('app_fitness_status')

        # 7. Annotate news with source trustworthiness
        trust_notes = []
        if news_available:
            for name, info in self.MEDIA_TRUST.items():
                if name.lower() in sections.get('news', '').lower():
                    stars = info['factual']
                    label = ('Highly Reliable' if stars >= 4
                             else 'Mixed Reliability' if stars >= 3
                             else 'Low Reliability')
                    trust_notes.append(
                        f"  {name}: {info['bias']} bias, {label} "
                        f"({stars}/5 factual accuracy)")
        trust_section = ("\n".join(trust_notes) if trust_notes
                         else "  No recognized sources to rate.")

        # 8. Build the briefing prompt — adapt based on available data
        user_greeting = f" for {user_name}" if user_name else ""
        location_ctx = (f" The user is located in {location}."
                        if location else "")

        # Build data sections dynamically
        data_blocks = [
            f"=== WEATHER ===\n{sections['weather']}",
            f"=== TODAY'S SCHEDULE & TASKS ===\n{sections['schedule']}",
        ]

        if news_available:
            data_blocks.append(
                f"=== TOP NEWS HEADLINES ===\n{sections['news']}")
            data_blocks.append(
                f"=== SOURCE TRUSTWORTHINESS RATINGS ===\n{trust_section}")

        data_blocks.extend([
            f"=== EMERGENCY PREP STATUS ===\n{sections['prep']}",
            f"=== NETWORK SECURITY STATUS ===\n{sections['security']}",
            f"=== FITNESS STATUS ===\n{sections['fitness']}",
        ])

        data_section = "\n\n".join(data_blocks)

        # Adapt news instructions
        if news_available:
            news_instructions = (
                "Cover the top 3-5 news stories, noting source trustworthiness.")
        else:
            news_instructions = (
                "Web search is offline — skip the news section and focus on "
                "other data.")

        briefing_prompt = f"""You are {self.ai_name}, delivering a comprehensive daily briefing{user_greeting}.{location_ctx}

Below is RAW DATA gathered from PuffPAI's integrated systems. CONTEXTUALIZE and ANALYZE this data. Think like a personal assistant briefing someone at the start of their day.

IMPORTANT: You MUST generate a briefing. Use whatever data IS available. Never refuse.

{data_section}

=== BRIEFING STRUCTURE ===
Cover ALL of these topics, using smooth transitions:

1. **Weather**: Start with today's weather and any alerts.
2. **Schedule**: What's on the calendar today? Any tasks due or bills coming up?
3. **News**: {news_instructions}
4. **Home & Security**: Network security status — any issues or alerts?
5. **Emergency Prep**: Any supplies expiring soon? Inventory status?
6. **Fitness**: Workout streak, recent activity, goals progress.
7. **Priority for Today**: End with the single most important thing to focus on.

FORMATTING: Use clear sections with bold headers for each topic. Keep it concise but comprehensive — around 400-600 words total."""

        print("[Daily Briefing] Generating contextualized briefing...")
        result = self._llm_chat(briefing_prompt)

        # Guard against LLM refusal
        if result and len(result) > 50 and 'sorry' not in result[:30].lower():
            return result

        # Fallback: return raw data in a readable format
        print("[Daily Briefing] LLM refused, returning structured fallback")
        fallback_parts = [
            "**Daily Briefing**\n",
            f"**Weather:**\n{sections['weather']}\n",
            f"**Schedule:**\n{sections['schedule']}\n",
        ]
        if news_available:
            fallback_parts.append(f"**News:**\n{sections['news']}\n")
        fallback_parts.extend([
            f"**Emergency Prep:**\n{sections['prep']}\n",
            f"**Network Security:**\n{sections['security']}\n",
            f"**Fitness:**\n{sections['fitness']}",
        ])
        return "\n".join(fallback_parts)

    def _filter_tools_by_intent(self, query: str) -> list:
        """Return a focused subset of tools for a query — REGISTRY-DRIVEN.

        Small open-source models choke on 80-tool catalogs. Binding is now
        driven by the capability registry (the single source of truth): each
        capability declares its own trigger keywords, so the tool that OWNS a
        capability is bound when the user's words hit it. This retired the
        former ~400-line hand-maintained keyword wall — to change routing, edit
        a capability's ``keywords`` in capability_registry.py, not this file.
        The ``len < 3`` floor below is the only safety net needed.
        """
        from puffpai import capability_registry as _capreg
        names = set(_capreg.tools_for_query(query, set(self.tool_dict.keys())))
        # Stateful bind (not word-driven): open Sentinel consent requests mean a
        # bare "yes"/"go ahead" may be answering one — always offer consent tools.
        try:
            from puffpai.security import consent_requests as _creq_filter
            if _creq_filter.list_open():
                names.update(["sentinel_approve_device", "sentinel_deny_device",
                              "sentinel_list_requests"])
        except Exception:
            pass
        filtered = [t for t in self.tools if t.name in names]
        if len(filtered) < 3:  # safety floor
            filtered = [t for t in self.tools if t.name in (
                "search_knowledge_base", "list_indexed_libraries", "web_search")]
        return filtered

    def run(self, query, conversation_history=None, max_steps=5):
        """Multi-step agent loop with conversation context.

        Delegates to PuffPAIAgent (LangGraph StateGraph) for proper
        tool orchestration.  Falls back to the legacy regex loop if
        LangGraph import fails.
        """
        try:
            from puffpai.agent_graph import PuffPAIAgent
            from puffpai.config import get_config

            _cfg = get_config()
            # Filter tools by intent — small models can't handle 80 tools
            filtered_tools = self._filter_tools_by_intent(query)

            agent = PuffPAIAgent(
                tools=filtered_tools,
                ai_name=self.ai_name,
                user_name=self.user_name,
                ollama_url=_cfg.ollama.url,
                model=_cfg.ollama.model,
                temperature=_cfg.ollama.temperature,
                max_steps=max_steps,
            )
            return agent.run(query, conversation_history)
        except ImportError:
            print("[Agent] LangGraph not available, using legacy loop")
        except Exception as e:
            print(f"[Agent] LangGraph agent failed ({e}), falling back to legacy")

        # ── Legacy fallback (original regex-based loop) ─────────────────
        tool_descriptions = "\n".join([
            f"- {t.name}: {t.description}" for t in self.tools
        ])

        context_info = ""
        if conversation_history:
            context_info = f"\n\n[Previous Conversation]\n{self.build_context_prompt(conversation_history)}\n"

        collected_results = []

        for step in range(max_steps):
            results_so_far = ""
            if collected_results:
                results_so_far = "\n\n[Tool Results So Far]\n" + "\n".join(
                    [f"- {r['tool']}: {r['result'][:300]}" for r in collected_results]
                )

            tool_names_list = ", ".join([t.name for t in self.tools])

            user_ctx = f" You are chatting with {self.user_name}." if self.user_name else ""
            today_str = datetime.now().strftime('%Y-%m-%d')
            tomorrow_str = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')
            day_name = datetime.now().strftime('%A')
            decision_prompt = f"""You are {self.ai_name}, an AI assistant.{user_ctx} Today is {day_name}, {today_str}. Tomorrow is {tomorrow_str}.

You have ONLY these tools (use the EXACT name):

{tool_descriptions}

VALID TOOL NAMES: {tool_names_list}
{context_info}
{results_so_far}

RULES:
1. ONLY use tool names from the VALID TOOL NAMES list above. NEVER invent or guess tool names.
2. If a tool returned "not found", pick a DIFFERENT tool from the valid list or give an ANSWER.
3. NEVER call the same tool more than twice.
4. NEVER hallucinate data. Use tools or say you don't know.
5. When you have enough information, give the final answer.
6. For ANY question about current events, news, prices, weather, sports, what is happening now/today/recently, or anything time-sensitive — you MUST use web_search. Your training data is outdated. NEVER answer these from memory.
7. For calendar tools, use PIPE-separated values. Example: app_add_task INPUT: Pickup Sudafed|{tomorrow_str}|other|dad|medium
8. For dates, use ISO format ({today_str}) or natural words like 'tomorrow'. The tools understand both. IMPORTANT: If the user mentions a TIME, you MUST include it! Use 'next monday at 3pm' or ISO with time like '{tomorrow_str}T15:00'. Example: app_add_event INPUT: Dentist|next friday at 2pm|medical|dad|high
9. For FOOD/GROCERY items: use app_add_pantry (items we HAVE at home) or app_add_shopping (items we NEED to buy). Do NOT use app_add_task for food items.
10. For trivia questions: use app_trivia_question, or app_trivia_generate when the user names a topic ("trivia about space", "quiz me on the Civil War"). Do NOT reveal the answer and NEVER print the [answer=...] tag — let the user guess first.

User query: {query}

If you need a tool, respond EXACTLY (one line each):
TOOL: tool_name
INPUT: tool_input

If you have enough info to answer, respond EXACTLY:
ANSWER: your final response

Your decision (step {step + 1}/{max_steps}):"""

            print(f"\n[Agent] Step {step + 1} — analyzing...")
            decision = self._llm_chat(decision_prompt)
            print(f"[Agent] Decision: {_redact_tool_decision(decision)[:200]}...")

            if "TOOL:" in decision:
                try:
                    tool_match = re.search(r'TOOL:\s*(\w+)', decision)
                    input_match = re.search(r'INPUT:\s*(.*?)(?:\n|$)', decision, re.DOTALL)

                    if not tool_match:
                        print(f"[Agent] TOOL: found but no tool name parseable, skipping")
                        collected_results.append({
                            'tool': 'unknown',
                            'input': '',
                            'result': 'Could not parse tool name from response.'
                        })
                        continue

                    if tool_match:
                        tool_name = tool_match.group(1)
                        tool_input = input_match.group(1).strip() if input_match else ""

                        print(f"[Agent] Using tool: {tool_name} (step {step+1})")

                        # Resolve tool name — exact match first, then fuzzy
                        resolved_name = None
                        if tool_name in self.tool_dict:
                            resolved_name = tool_name
                        else:
                            # Normalize: strip underscores and lowercase for comparison
                            normalized = tool_name.lower().replace('_', '')
                            for valid_name in self.tool_dict:
                                if valid_name.lower().replace('_', '') == normalized:
                                    resolved_name = valid_name
                                    print(f"[Agent] Fuzzy matched '{tool_name}' → '{valid_name}'")
                                    break
                            # Substring fallback
                            if not resolved_name:
                                for valid_name in self.tool_dict:
                                    if normalized in valid_name.replace('_', '') or valid_name.replace('_', '') in normalized:
                                        resolved_name = valid_name
                                        print(f"[Agent] Substring matched '{tool_name}' → '{valid_name}'")
                                        break

                        if resolved_name:
                            if not note_vault_tool_calls([resolved_name]):
                                # FAIL CLOSED: a vault tool whose turn cannot
                                # be made private does not run at all.
                                print("[Agent] Tool refused: privacy guard")
                                collected_results.append({
                                    'tool': resolved_name,
                                    'input': '[protected]',
                                    'result': VAULT_PRIVACY_REFUSAL,
                                })
                                continue
                            _vault_tool = resolved_name in VAULT_PRIVATE_TOOLS
                            result = self.tool_dict[resolved_name].invoke(tool_input)
                            collected_results.append({
                                'tool': resolved_name,
                                # A vault tool's INPUT is the PIN, and this
                                # record is fed back into the model's prompt.
                                'input': ('[protected]' if _vault_tool
                                          else tool_input),
                                'result': str(result)
                            })
                            print("[Agent] Tool result: " + (
                                "[protected]" if _vault_tool
                                else f"{str(result)[:200]}..."))
                            continue  # Loop to check if more tools needed
                        else:
                            valid_names = list(self.tool_dict.keys())
                            print(f"[Agent] Tool '{tool_name}' not found. Valid: {valid_names}")
                            collected_results.append({
                                'tool': tool_name,
                                'input': tool_input,
                                'result': f"Tool '{tool_name}' does not exist. Valid tools: {', '.join(valid_names)}"
                            })
                            continue
                except Exception as e:
                    print(f"[Agent] Error: {e}")
                    break

            # ANSWER or final step — synthesize all results
            if collected_results:
                synthesis_prompt = f"""You are {self.ai_name}, a voice-first AI assistant. Present the tool results as a natural spoken response.

VOICE RULES (very important):
- Write in natural spoken sentences as if talking to someone in the room.
- No bullet points, no asterisks, no markdown, no numbered lists.
- If listing devices, say them conversationally: "I found three devices: the living room light, the bedroom switch, and the kitchen fan."
- For confirmations keep it brief: "Done, I've turned the living room lights off."
- If there is an error, explain it simply in one sentence.
- Do NOT mention tool names or internal process. Just present the information naturally.
- Show actual data from results but weave it into spoken language.
{context_info}

User question: {query}

Tool results:
{chr(10).join([f"{r['result']}" for r in collected_results])}

Your spoken response:"""
                return self._llm_chat(synthesis_prompt)
            else:
                if "ANSWER:" in decision:
                    return decision.split("ANSWER:", 1)[1].strip()
                return decision

        # Exhausted steps — synthesize what we have
        if collected_results:
            synthesis_prompt = f"""You are {self.ai_name}, a voice-first AI assistant. Present these results in natural spoken language — no markdown, no bullet points.

User question: {query}

Results:
{chr(10).join([f"{r['result']}" for r in collected_results])}

Your spoken response:"""
            return self._llm_chat(synthesis_prompt)

        return "I wasn't able to determine the right approach. Could you rephrase your question?"
