"""The Concierge's brain, schema-constrained local inference.

Everything the agent team needs a *model* for funnels through three methods.
Without an engine (``engine=None``) routing is regex-only and the planning
loop has nothing to plan with. This is that engine.

Two jobs, two model slots, on purpose:

* :meth:`OllamaEngine.classify_intent` — pick one Steward intent from a closed
  menu. Runs **only** when no deterministic rule matched (see
  ``concierge._resolve``), so the fast path stays fast. Small model, tiny output.
* :meth:`OllamaEngine.plan_steps` — propose an ordered plan for the bounded
  plan→act→observe→revise loop. Bigger model, still bounded.

The load-bearing technique is **constrained decoding at the emission step**.
Ollama's native ``format`` field takes a JSON Schema and enforces it *in the
decoder*, so:

* an intent outside the menu is not "discouraged by the prompt", it is
  unrepresentable — the ``enum`` cannot emit one;
* a plan step naming a capability the code does not implement is likewise
  unrepresentable, because ``agent`` and ``intent`` are enums built from the
  catalogue the caller passes in.

This matters more here than on a frontier model: small local models are
documented to emit malformed or invented tool calls at a rate that would make a
multi-hop team unreliable. The decoder refuses first and the parse-time drop
in ``planning_loop._parse`` is the backstop between a hallucinated capability
and a dropped step.

The one concession to the "constraint tax" (schema-forced output suppresses
step-by-step reasoning, measurably): the plan schema puts a ``reasoning`` string
*before* ``steps``, and JSON object properties are emitted in schema order — so
the model reasons in prose first and commits to structure second, inside one
call. Two calls would double latency on an edge box; this buys most of the
benefit for free.

**This module never raises into a caller.** Both public methods return ``None``
on any failure (transport, timeout, malformed reply, disabled), because every
call site treats ``None`` as "no engine" and falls back to deterministic
behaviour. A missing brain must degrade the appliance, never break it.

Deliberately NOT implemented: prose chat. :meth:`OllamaEngine.run` returns
``None`` so the legacy ``/chat/send`` pipeline (RAG, tools, persona) stays the
authoritative answerer. Swapping that out is a separate, much larger decision
than giving the Concierge a router and a planner.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import json
import os
import re
import time

import requests

from puffpai.inference import model_options as _model_options
from puffpai.logging_config import get_logger

logger = get_logger(__name__)

#: Kill-switch, mirroring ``PUFFPAI_AGENT_TEAM_ENABLED``. Set to 0 to run the
#: team engine-less (regex-only routing, no planner) without a code change.
ENGINE_ENV = "PUFFPAI_ENGINE_ENABLED"

#: Model slots. Split because the two jobs have opposite cost profiles: intent
#: classification is a menu pick that must feel instant and runs on the
#: interactive path; planning is rare, harder, and may take seconds. This is the
#: roster's "7-8B resident + escalation" split, made real.
CLASSIFY_MODEL_ENV = "PUFFPAI_CLASSIFY_MODEL"
PLAN_MODEL_ENV = "PUFFPAI_PLAN_MODEL"
#: Both slots default to the same tag, chosen by measurement on the appliance
#: (31 canned utterances + 3 planning goals). Summary of why:
#:
#: * ``qwen3:30b-a3b`` — best AND fastest at both jobs. It is a mixture of
#:   experts (~3B active), so a "30B" model classifies in ~1.3s, beating the
#:   dense 7B. 0.88 accuracy on the population the engine actually sees, and it
#:   hijacked 0 of 9 clear-cut knowledge questions.
#: * ``qwen2.5:7b-instruct`` — slower and less accurate (0.69), and it pulled 3
#:   of 12 non-house questions into house intents.
#: * ``gpt-oss:20b`` — unusable here: 8 empty replies under a JSON schema and
#:   *every* planning call returned nothing. The documented Harmony
#:   structured-output fragility, reproduced on this hardware.
#:
#: One tag for both slots is also the cheaper deployment: the plan model is the
#: classify model already resident, so the engine costs one model's memory, not
#: two. Override per slot with the env vars above on hardware that cannot hold
#: this one; :meth:`OllamaEngine.health` reports whether the tags are pulled.
DEFAULT_CLASSIFY_MODEL = "qwen3:30b-a3b"
DEFAULT_PLAN_MODEL = "qwen3:30b-a3b"

DEFAULT_OLLAMA_URL = "http://localhost:11434"

#: Wall-clock ceilings (seconds). The classify budget is deliberately brutal:
#: it sits on the interactive path, and a slow answer there is worse than no
#: answer, because "no answer" costs nothing — the deterministic fallback is
#: already computed.
CLASSIFY_TIMEOUT = 20.0
PLAN_TIMEOUT = 90.0

#: Token ceilings. An enum pick needs a handful; a plan needs room to reason
#: first (see the module docstring) but must not run away.
CLASSIFY_NUM_PREDICT = 32
PLAN_NUM_PREDICT = 700
# Both slots use the SAME num_ctx on purpose. Ollama keys a loaded runner to
# its context size: with classify at 1024 and plan at 4096, an idle→interaction
# sequence loads the 18 GB engine at 1024 and then immediately reloads it at
# 4096, a double cold start (~40 s) measured on a Jetson AGX Orin. Equal
# sizes mean one runner serves both slots. Classify wastes a little KV headroom
# and cares not at all.
# The value (4096 on the appliance) comes from the one role accessor, so
# config governs every runner instead of this module and assistant_tools each
# carrying their own literal.
CLASSIFY_NUM_CTX = _model_options.planner_num_ctx()
PLAN_NUM_CTX = CLASSIFY_NUM_CTX

#: Sentinel choice added to every classification menu. Without it an enum forces
#: a house intent onto every unmatched message — and unmatched messages are
#: mostly *not* house work ("how do I purify water", "what's the capital of
#: France"). Forcing a pick there would hijack the knowledge path, which is the
#: opposite of the fix this engine exists to make.
NO_INTENT = "none"

#: Keep the engine's weights resident permanently (-1 = never unload; a JSON
#: number, because Go's duration parser rejects the string "-1"). The engine is
#: the appliance's brain: every interaction, and soon every voice exchange,
#: pays its cold start first, and on a Jetson AGX Orin that measures ~19 s for
#: the 30B MoE. A per-request value overrides the server-level
#: ``OLLAMA_KEEP_ALIVE`` *for that model*, so pinning here also protects a tag
#: shared with the legacy chat pipeline from being evicted by a shorter value
#: riding along on someone else's request. RAM math on a 64 GB appliance: engine
#: at 4 k ctx + chat model at 8 k ctx + vision + embedder ≈ 40 GB — everything
#: stays resident with headroom.
#: Resolved from config rather than written here, so the pin has exactly one
#: source of truth (``config.ollama.keep_alive``, ``-1`` on the appliance).
#: A config edit reaches this path too instead of being silently overridden
#: by a local literal.
#:
#: IMPORTANT: the pin must not be frozen at import time (``KEEP_ALIVE =
#: _model_options.keep_alive()`` evaluated once, at module load). The coder's
#: on-demand load evicts qwen3:30b-a3b after its 10-minute window, and the NEXT
#: planner/classify turn must reload it PINNED (``-1``, "Forever"), not on the
#: ollama daemon's 30-minute default. ``_chat_json`` below calls
#: ``_model_options.keep_alive()`` fresh on every call, through the same
#: one-place options builder every other call path uses, so a resolved value
#: can never go stale between calls.
KEEP_ALIVE = _model_options.keep_alive()

#: ``agent.intent`` at the head of a catalogue line, e.g.
#: "  steward.camera_snapshot target=<device name>".
_CATALOGUE_RE = re.compile(r"^\s*([a-z_]+)\.([a-z_]+)\b")


class OllamaEngine:
    """Schema-constrained local inference for the Concierge and planning loop.

    Args:
        url: Ollama base URL. Defaults to config, then ``$OLLAMA_URL``, then
            localhost.
        classify_model / plan_model: model tags for the two slots.
        http: transport with a ``.post``/``.get`` (defaults to :mod:`requests`).
            Injected by tests so the whole engine is exercisable with no network
            and no model — the failure paths especially, which are the ones that
            must hold on a bad night.
    """

    def __init__(self, *, url: str | None = None,
                 classify_model: str | None = None,
                 plan_model: str | None = None,
                 classify_timeout: float = CLASSIFY_TIMEOUT,
                 plan_timeout: float = PLAN_TIMEOUT,
                 http=None) -> None:
        self._url = (url or _default_url()).rstrip("/")
        self._classify_model = (classify_model
                                or os.environ.get(CLASSIFY_MODEL_ENV, "").strip()
                                or DEFAULT_CLASSIFY_MODEL)
        self._plan_model = (plan_model
                            or os.environ.get(PLAN_MODEL_ENV, "").strip()
                            or DEFAULT_PLAN_MODEL)
        self._classify_timeout = classify_timeout
        self._plan_timeout = plan_timeout
        self._http = http or requests
        #: Per-model memo: does this tag accept the ``think`` field? Ollama 400s
        #: for models without thinking support, so the first call learns it
        #: instead of paying a failed round-trip every time.
        self._think_ok: dict[str, bool] = {}

    # ── public API (the contract three call sites expect) ────────────

    def classify_intent(self, text: str, choices) -> str | None:
        """Pick one intent from *choices*, or ``None`` for "not house work".

        Called by ``concierge._engine_refine_intent`` only after every
        deterministic rule missed. The returned string is re-checked against
        ``RESOLVABLE_INTENTS`` by the caller — belt and braces, since the enum
        already makes an off-menu answer unrepresentable.
        """
        menu = [str(c) for c in (choices or []) if str(c).strip()]
        if not menu or not (text or "").strip():
            return None
        # Deduplicated so a caller that already lists the sentinel cannot
        # produce a schema with a repeated enum value.
        allowed = sorted(set(menu) - {NO_INTENT}) + [NO_INTENT]
        schema = {
            "type": "object",
            "properties": {"intent": {"type": "string", "enum": allowed}},
            "required": ["intent"],
        }
        messages = [
            {"role": "system", "content": (
                "You route one household request to exactly one capability of a "
                "home assistant appliance.\n"
                "Answer with the single best-fitting capability name.\n"
                f"Answer {NO_INTENT!r} when the request is not an action on this "
                "house — general knowledge, chat, cooking, advice, maths, or "
                "anything about the wider world is "
                f"{NO_INTENT!r}. Reading a device's state IS house work."
            )},
            {"role": "user", "content": str(text)[:600]},
        ]
        data = self._chat_json(self._classify_model, messages, schema,
                               timeout=self._classify_timeout,
                               num_predict=CLASSIFY_NUM_PREDICT,
                               num_ctx=CLASSIFY_NUM_CTX, label="classify")
        if not isinstance(data, dict):
            return None
        choice = data.get("intent")
        if not isinstance(choice, str) or choice == NO_INTENT:
            return None
        return choice if choice in menu else None

    def plan_steps(self, goal: str, catalogue_lines) -> list[dict] | None:
        """Propose an ordered plan for *goal*. ``None`` when unusable.

        The returned list feeds ``planning_loop._parse``, which independently
        re-checks every step against the real catalogue, the deny-list, target
        resolution and the confirm classes. Nothing here is trusted; the schema
        only stops the model wasting a turn on a capability that cannot exist.
        """
        lines = [str(line) for line in (catalogue_lines or [])]
        pairs = _catalogue_pairs(lines)
        if not (goal or "").strip() or not pairs:
            return None
        agents = sorted({a for a, _ in pairs})
        intents = sorted({i for _, i in pairs})
        schema = _plan_schema(agents, intents)
        messages = [
            {"role": "system", "content": (
                "You plan actions for a home assistant appliance.\n"
                "Use ONLY these capabilities — any other pairing is discarded:\n"
                + "\n".join(lines)
                + "\n\nRules:\n"
                  "- Think briefly in 'reasoning', then list the steps.\n"
                  "- Fewest steps that achieve the goal; at most 6.\n"
                  "- 'target' names the device in the user's words; omit it "
                  "when the capability takes no device.\n"
                  "- Prefer asking the household over guessing.\n"
                  "- Steps run in order and each one is reviewed for safety "
                  "before it runs."
            )},
            {"role": "user", "content": f"Goal: {str(goal)[:600]}"},
        ]
        data = self._chat_json(self._plan_model, messages, schema,
                               timeout=self._plan_timeout,
                               num_predict=PLAN_NUM_PREDICT,
                               num_ctx=PLAN_NUM_CTX, label="plan")
        if not isinstance(data, dict):
            return None
        steps = data.get("steps")
        if not isinstance(steps, list):
            return None
        why = str(data.get("reasoning") or "")[:400]
        if why:
            logger.debug("planner reasoning: %s", why)
        return [s for s in steps if isinstance(s, dict)]

    def revise_plan(self, goal: str, observations, remaining,
                    catalogue_lines=None) -> list[dict] | None:
        """Amend a *remaining* plan from what already happened. ``None`` if unusable.

        This is :meth:`plan_steps`' sibling for the *observe→revise* half of the
        loop, and it is deliberately built as a **pure planning call**: same
        schema, same catalogue-derived ``agent``/``intent`` enums, same
        constrained decode. A revised step is therefore the same shape as an
        initial one and survives ``planning_loop._parse`` — and, like an initial
        plan, is re-checked by the loop against the catalogue, the deny-list, the
        confirm classes and the run's *already sealed* egress scope. Nothing this
        returns is trusted; the schema only stops the model wasting a turn.

        THE SECURITY BOUNDARY. ``revise_plan`` is the "content-drives-action"
        vector. The *observations* it is handed are **safe scalars only** — for
        each prior step: the intent that ran, a target the plan itself already
        named, ok/failed, empty?/count, and a COARSE ``error_class`` — and
        nothing else. It never receives a fetched page, a document or vault body,
        a file's contents, a search snippet, or any free text (not even an error
        *message*), because any of those is attacker-influenceable and would let
        a hostile result rewrite the plan. So the model here decides the plan's
        *structure* from outcomes; it never sees the bytes. It also never
        fetches: choosing to add a fetch cannot widen egress, because the scope
        was frozen before the run began and the loop refuses any revised fetch to
        a host outside it. ``planning_loop._observe`` is the choke point that
        guarantees the scalars-only contract; this method must never be handed,
        or reach for, anything richer.

        *catalogue_lines* is optional so a caller that predates it degrades to
        ``None`` (no revision) rather than raising.
        """
        lines = [str(line) for line in (catalogue_lines or [])]
        pairs = _catalogue_pairs(lines)
        if not (goal or "").strip() or not pairs:
            return None
        agents = sorted({a for a, _ in pairs})
        intents = sorted({i for _, i in pairs})
        schema = _plan_schema(agents, intents)
        messages = [
            {"role": "system", "content": (
                "You are REVISING an in-progress plan for a home assistant "
                "appliance.\n"
                "Use ONLY these capabilities — any other pairing is discarded:\n"
                + "\n".join(lines)
                + "\n\nFor each step already run you are told ONLY its outcome "
                  "flags — never any content it returned. 'empty' or 'count=0' "
                  "means the step succeeded but found nothing; a non-ok step "
                  "did not succeed. You cannot see any page, document or file "
                  "text, so do not assume any exists — decide the new plan "
                  "STRUCTURE from the outcomes alone.\n\nRules:\n"
                  "- Think briefly in 'reasoning', then list the revised "
                  "remaining steps.\n"
                  "- Fewest steps that still reach the goal; at most 6.\n"
                  "- Drop a step that can no longer work; substitute a "
                  "different capability, or ask the household, when that is "
                  "better than guessing.\n"
                  "- 'target' names the device in the user's words; omit it "
                  "when the capability takes no device.\n"
                  "- Prefer asking the household over guessing.\n"
                  "- Each step is reviewed for safety before it runs."
            )},
            {"role": "user", "content": (
                f"Goal: {str(goal)[:600]}\n\n"
                f"Already run:\n{_render_observations(observations)}\n\n"
                f"Still pending:\n{_render_remaining(remaining)}"
            )},
        ]
        data = self._chat_json(self._plan_model, messages, schema,
                               timeout=self._plan_timeout,
                               num_predict=PLAN_NUM_PREDICT,
                               num_ctx=PLAN_NUM_CTX, label="revise")
        if not isinstance(data, dict):
            return None
        steps = data.get("steps")
        if not isinstance(steps, list):
            return None
        why = str(data.get("reasoning") or "")[:400]
        if why:
            logger.debug("revision reasoning: %s", why)
        return [s for s in steps if isinstance(s, dict)]

    def run(self, text, conversation_history=None):
        """Deliberately not a chat engine — always ``None``.

        ``concierge._converse`` calls this on every turn and attaches the result
        as an additive ``answer`` field. Returning ``None`` keeps the legacy
        ``/chat/send`` pipeline (RAG, tools, persona) authoritative, which is
        the intended scope. It is defined rather than omitted so the call
        site does not take an ``AttributeError`` — caught, but logged as an
        engine failure on every single turn, which would read as a fault.
        """
        return None

    def health(self) -> dict:
        """Reachability + whether the two configured models are actually pulled."""
        info: dict = {"ok": False, "url": self._url,
                      "classify_model": self._classify_model,
                      "plan_model": self._plan_model}
        try:
            resp = self._http.get(f"{self._url}/api/tags", timeout=5)
            resp.raise_for_status()
            tags = {m.get("name") for m in (resp.json() or {}).get("models", [])}
        except Exception as exc:
            info["error"] = str(exc)[:200]
            return info
        info["ok"] = True
        info["classify_model_present"] = self._classify_model in tags
        info["plan_model_present"] = self._plan_model in tags
        return info

    # ── transport ───────────────────────────────────────────────────

    def _chat_json(self, model: str, messages: list, schema: dict, *,
                   timeout: float, num_predict: int, num_ctx: int,
                   label: str) -> dict | None:
        """One schema-constrained chat call. Returns a dict, or ``None``.

        Swallows every exception by design (see module docstring). The elapsed
        time is logged for each call because latency, not correctness, is the
        thing most likely to make this engine the wrong choice on an edge box.
        """
        if not engine_enabled():
            return None
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "format": schema,
            "keep_alive": _model_options.keep_alive(),
            "options": {
                # Deterministic: the same house question must route the same way
                # twice, and a reproducible wrong answer is fixable where a
                # flaky one is not.
                "temperature": 0,
                "num_ctx": num_ctx,
                "num_predict": num_predict,
            },
        }
        started = time.monotonic()
        try:
            content = self._post_chat(model, payload, timeout)
        except Exception as exc:
            logger.warning("engine %s call failed (%s): %s", label, model, exc)
            return None
        elapsed = time.monotonic() - started
        if content is None:
            return None
        try:
            data = json.loads(content)
        except (TypeError, ValueError):
            # With ``format`` set this should be unreachable; if a runtime ever
            # ignores the schema, degrade rather than hand junk to the caller.
            logger.warning("engine %s returned non-JSON in %.2fs: %.120s",
                           label, elapsed, content)
            return None
        logger.info("engine %s ok in %.2fs (%s)", label, elapsed, model)
        return data if isinstance(data, dict) else None

    def _post_chat(self, model: str, payload: dict, timeout: float) -> str | None:
        """POST /api/chat, retrying once without ``think`` if unsupported.

        Hybrid reasoning models (qwen3, gpt-oss) emit a long thinking block by
        default, which on this hardware costs more than the answer. ``think:
        false`` suppresses it — but models without thinking support reject the
        field outright, so support is probed once per tag and memoised.
        """
        body = dict(payload)
        if self._think_ok.get(model, True):
            body["think"] = False
        resp = self._http.post(f"{self._url}/api/chat", json=body, timeout=timeout)
        if getattr(resp, "status_code", 200) >= 400 and "think" in body:
            detail = _error_text(resp)
            if "think" in detail.lower():
                self._think_ok[model] = False
                body.pop("think")
                logger.debug("model %s rejects think; retrying without", model)
                resp = self._http.post(f"{self._url}/api/chat", json=body,
                                       timeout=timeout)
            else:
                self._think_ok.setdefault(model, True)
        resp.raise_for_status()
        data = resp.json()
        message = (data or {}).get("message") or {}
        content = message.get("content")
        return content if isinstance(content, str) else None


# ── helpers ─────────────────────────────────────────────────────────

def _error_text(resp) -> str:
    """Best-effort body text from an error response, never raising."""
    try:
        return json.dumps(resp.json())
    except Exception:
        try:
            return str(resp.text)
        except Exception:
            return ""


def _plan_schema(agents: list[str], intents: list[str]) -> dict:
    """The one schema both planning and revision emit.

    ``plan_steps`` and ``revise_plan`` MUST produce the identical shape,
    constrained by the identical catalogue-derived enums, so a revised step
    survives ``planning_loop._parse`` exactly as an initial step does. Kept in
    one place so the two can never drift apart.

    Property ORDER is emission order: ``reasoning`` first, structure second, so
    the model reasons in prose before it commits to JSON (see the module
    docstring on the constraint tax).
    """
    return {
        "type": "object",
        "properties": {
            "reasoning": {"type": "string"},
            "steps": {
                "type": "array",
                "maxItems": 6,
                "items": {
                    "type": "object",
                    "properties": {
                        "agent": {"type": "string", "enum": agents},
                        "intent": {"type": "string", "enum": intents},
                        "target": {"type": "string"},
                        "why": {"type": "string"},
                    },
                    "required": ["agent", "intent", "why"],
                },
            },
        },
        "required": ["reasoning", "steps"],
    }


#: The only ``error_class`` labels a revision prompt may carry — a closed set of
#: COARSE categories, never a free-text error message (which could echo fetched
#: content). Anything else an observation names is rendered as ``error``.
_OBS_ERROR_LABELS = frozenset({
    "ok", "denied", "egress_denied", "offline", "aborted", "http_error",
    "error",
})


def _render_observations(observations) -> str:
    """Compact, SCALAR-ONLY rendering of the loop's step observations.

    Defence in depth for the revision security boundary. The loop's
    ``_observe`` builder is the choke point that guarantees no raw content
    reaches the planner, but this renderer independently emits ONLY a fixed set
    of clipped scalar fields — so even a stray free-text value added upstream
    one day could not ride into the prompt through here.
    """
    if not isinstance(observations, (list, tuple)) or not observations:
        return "  (nothing has run yet)"
    out = []
    for obs in observations:
        if not isinstance(obs, dict):
            continue
        step = str(obs.get("step") or "")[:120]
        status = str(obs.get("status") or "")[:20]
        bits = [f"status={status}", f"ok={'true' if obs.get('ok') else 'false'}"]
        empty = obs.get("empty")
        if empty is not None:
            bits.append(f"empty={'true' if empty else 'false'}")
        count = obs.get("count")
        if isinstance(count, int) and not isinstance(count, bool):
            bits.append(f"count={count}")
        err = str(obs.get("error_class") or "")
        if err and err != "ok":
            bits.append(f"error={err if err in _OBS_ERROR_LABELS else 'error'}")
        out.append(f"  - {step} [{' '.join(bits)}]")
    return "\n".join(out) or "  (nothing has run yet)"


def _render_remaining(remaining) -> str:
    """The pending step labels, one per line. Labels are ``describe()`` strings."""
    if not isinstance(remaining, (list, tuple)) or not remaining:
        return "  (nothing pending)"
    return "\n".join(f"  - {str(s)[:120]}" for s in remaining) \
        or "  (nothing pending)"


def _catalogue_pairs(lines) -> list[tuple[str, str]]:
    """Extract ``(agent, intent)`` pairs from rendered catalogue lines.

    Parsed from the caller's own rendering rather than imported from
    ``planning_loop``: the lines are the contract this method is handed, and
    keeping the engine free of the loop's internals means the loop can change
    its catalogue without the engine going stale or a circular import appearing.
    """
    pairs = []
    for line in lines:
        match = _CATALOGUE_RE.match(line)
        if match:
            pairs.append((match.group(1), match.group(2)))
    return pairs


def _default_url() -> str:
    """Ollama URL from config, then ``$OLLAMA_URL``, then localhost."""
    try:
        from puffpai.config import get_config
        url = getattr(getattr(get_config(), "ollama", None), "url", None)
        if url:
            return str(url)
    except Exception as exc:  # config must never gate the engine
        logger.debug("config unavailable for ollama url: %s", exc)
    return os.environ.get("OLLAMA_URL", "").strip() or DEFAULT_OLLAMA_URL


def engine_enabled() -> bool:
    """Whether the engine may make model calls at all (default: yes)."""
    return os.environ.get(ENGINE_ENV, "1").strip().lower() in ("1", "true", "yes")


def build_engine(**kwargs) -> OllamaEngine | None:
    """Construct the engine, or ``None`` when disabled. Never raises.

    Construction makes no network call — the first model call does — so this is
    safe at startup.
    """
    if not engine_enabled():
        logger.info("concierge engine disabled by %s", ENGINE_ENV)
        return None
    try:
        return OllamaEngine(**kwargs)
    except Exception as exc:
        logger.warning("concierge engine unavailable: %s", exc)
        return None


__all__ = [
    "CLASSIFY_MODEL_ENV",
    "DEFAULT_CLASSIFY_MODEL",
    "DEFAULT_PLAN_MODEL",
    "ENGINE_ENV",
    "NO_INTENT",
    "PLAN_MODEL_ENV",
    "OllamaEngine",
    "build_engine",
    "engine_enabled",
]
