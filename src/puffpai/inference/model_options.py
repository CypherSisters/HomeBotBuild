"""One source of truth for the Ollama request options every call path sends.

Why this module exists (measured on the appliance):

Options scattered across five call paths drift apart from the configured
values, and every disagreement costs real wall clock:

* ``ChatOllama`` built with **no** ``num_ctx``, ``num_predict``,
  ``keep_alive`` or reasoning flag makes Ollama pick the model's own trained
  context (``journalctl -u ollama``: ``requested context size too large for
  model num_ctx=262144 n_ctx_train=131072``) and start a SECOND
  ``llama-server`` for ``gpt-oss:20b`` at ``-c 131072`` for a 141-token
  prompt. A different context size is a different runner, so ollama evicts
  every resident model to make room: ``tegrastats`` shows RAM fall 41,376 MB
  -> 7,816 MB in four seconds, then an ~18 s cold reload off NVMe. Measured
  cost: the same compound turn takes **35.69 s cold vs 10.10 s / 9.54 s
  warm**, about 26 s of pure model-swap.
* A hardcoded ``keep_alive="30m"`` riding along on someone else's request
  resets the configured resident pin (``-1``) back to a 30-minute eviction
  timer, so the resident set churns.

Every value below comes from ``config.OllamaConfig`` so the appliance config
(``num_ctx`` 8192, ``num_predict`` 1024, ``keep_alive`` -1) is honoured
identically on every path. Nothing here hardcodes a context size.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

#: Env override for the resident pin, for a dev box that does not want a
#: multi-GB model held forever. Accepts ``-1``/an integer number of seconds
#: (sent as a JSON number) or an ollama duration string such as ``"30m"``.
KEEP_ALIVE_ENV = "PUFFPAI_OLLAMA_KEEP_ALIVE"

#: What an ON-DEMAND model gets instead of the resident pin. The vision model
#: (``qwen2.5vl:7b``, a 5.97 GB blob) is used only when an image needs OCR
#: escalation; pinning it forever would spend 5.97 GB of the appliance's 62.8 GB on
#: a path that may not run for days. This is a deliberate, documented
#: exception to the resident pin, not a stray literal — see
#: ``keep_alive_on_demand``.
DEFAULT_ON_DEMAND_KEEP_ALIVE = "30m"

#: Fallback used only when the config object cannot be loaded at all (import
#: cycles in tests, a broken config file). Deliberately the CONSERVATIVE value:
#: a failure to read config must never silently pin a model forever.
_FALLBACK_KEEP_ALIVE: int | str = "30m"
_FALLBACK_NUM_CTX = 8192
_FALLBACK_NUM_PREDICT = 512


def _ollama_cfg():
    """Return ``config.get_config().ollama``, or ``None`` if unavailable."""
    try:
        from puffpai.config import get_config
        return get_config().ollama
    except Exception:  # pragma: no cover - config is present in every real run
        logger.debug("model_options: config unavailable, using fallbacks",
                     exc_info=True)
        return None


def _coerce_keep_alive(raw: object) -> int | str | None:
    """Normalise a keep_alive value.

    Ollama's Go duration parser rejects the STRING ``"-1"``; the pin must be
    sent as a JSON number. Any all-digit (optionally signed) value is therefore
    returned as an ``int``; anything else is passed through as a duration
    string.
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    text = str(raw).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return text


def keep_alive() -> int | str:
    """The resident pin every ordinary call path must send.

    Resolution order: ``PUFFPAI_OLLAMA_KEEP_ALIVE`` env, then
    ``config.ollama.keep_alive``, then the conservative fallback. On the
    appliance this resolves to ``-1`` (resident forever).
    """
    env = _coerce_keep_alive(os.environ.get(KEEP_ALIVE_ENV))
    if env is not None:
        return env
    cfg = _ollama_cfg()
    if cfg is not None:
        value = _coerce_keep_alive(getattr(cfg, "keep_alive", None))
        if value is not None:
            return value
    return _FALLBACK_KEEP_ALIVE


def keep_alive_on_demand() -> int | str:
    """The pin for a model that is NOT part of the resident working set.

    Used by the vision escalation path. Kept as its own named accessor so the
    choice is explicit and greppable rather than a ``"30m"`` literal buried in
    a payload dict. If the resident pin is itself a duration (a dev box), the
    on-demand value is never longer than it.
    """
    resident = keep_alive()
    if isinstance(resident, str):
        return resident
    return DEFAULT_ON_DEMAND_KEEP_ALIVE


def keep_alive_code() -> int | str:
    """The pin for the ON-DEMAND coding model (``config.ollama.code_keep_alive``).

    Its own accessor rather than :func:`keep_alive_on_demand` because the
    coding model is the largest on-demand blob on the appliance: it is loaded for a
    burst of "write me a ..." turns and must hand its RAM back soon after,
    which is a shorter window than the vision path wants. Falls back to the
    generic on-demand pin when the key is missing (an older config file).
    """
    cfg = _ollama_cfg()
    if cfg is not None:
        value = _coerce_keep_alive(getattr(cfg, "code_keep_alive", None))
        if value is not None:
            return value
    return keep_alive_on_demand()


def num_ctx() -> int:
    """Configured context window. Never hardcoded — see the module docstring."""
    cfg = _ollama_cfg()
    if cfg is not None:
        value = getattr(cfg, "num_ctx", None)
        if isinstance(value, int) and value > 0:
            return value
    return _FALLBACK_NUM_CTX


#: Fallback for :func:`planner_num_ctx` when config cannot be read. Matches the
#: planner/classifier default in the appliance config.
_FALLBACK_PLANNER_NUM_CTX = 4096


def planner_num_ctx() -> int:
    """Context for the SCHEMA-CONSTRAINED planner / classifier / JSON model.

    A third model-role accessor, alongside :func:`num_ctx` (prose chat) and
    :func:`embedding_num_ctx` (embedder). It exists so the THIRD resident
    ``llama-server`` (``-c 4096``) is governed by config rather than by
    literals in ``agents/engine.py`` (``CLASSIFY_NUM_CTX``/``PLAN_NUM_CTX``),
    a default argument and ``assistant_tools._llm_chat_json``.

    The value is deliberately NOT the chat ``num_ctx``. This role runs a
    different model tag (qwen3 on the appliance), so it is a different runner
    either way — raising it to 8192 would not merge the runners, it would only
    grow KV cache on a box pinned at MODE_50W, and the 4096 is the measured
    choice documented in ``agents/engine`` (equal classify/plan sizes, which
    is what avoids a double ~40 s cold start). What changed is that one config
    key now governs all three call sites instead of three literals drifting
    apart. It is still bounded by the chat context, so a deployment that
    lowers the appliance's window lowers this too.
    """
    cfg = _ollama_cfg()
    value = None
    if cfg is not None:
        raw = getattr(cfg, "planner_num_ctx", None)
        if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
            value = raw
    if value is None:
        value = _FALLBACK_PLANNER_NUM_CTX
    return max(1, min(int(value), int(num_ctx())))


def num_predict() -> int:
    """Configured generation budget."""
    cfg = _ollama_cfg()
    if cfg is not None:
        value = getattr(cfg, "num_predict", None)
        if isinstance(value, int) and value > 0:
            return value
    return _FALLBACK_NUM_PREDICT


#: The four options a ``ChatOllama`` construction must carry. Asserted by
#: ``tests/test_model_options_optimization.py`` with a grep over ``src/``
#: so a future edit cannot quietly drop one and reintroduce the 131072-context
#: eviction described in the module docstring.
LANGCHAIN_REQUIRED_OPTIONS = ("num_ctx", "num_predict", "keep_alive", "reasoning")


def langchain_chat_kwargs(**overrides) -> dict:
    """Options for ``ChatOllama(**kwargs)``, filtered to what it accepts.

    ``reasoning=False`` is the LangChain spelling of the ``"think": False``
    that :mod:`puffpai.inference.ollama_backend` sends. Its measured effect
    on the appliance, quoted from that module: *"gpt-oss:20b spent 1380
    characters and HALF its wall clock thinking before a two-sentence answer
    — 26.7s -> 13.5s with this set, same answer quality."* This is where the
    agent path gets it.

    langchain-ollama moves fast, so every key is checked against the installed
    ``ChatOllama``'s own field set before it is passed. A version that drops
    one of these loses that option and logs it, rather than raising and taking
    the whole agent path down to the ReAct fallback. On the appliance's
    Python 3.10.12 with langchain-ollama 1.1.0 all four fields are present.
    """
    kwargs: dict = {
        "num_ctx": num_ctx(),
        "num_predict": num_predict(),
        "keep_alive": keep_alive(),
        "reasoning": False,
    }
    kwargs.update(overrides)

    supported = _langchain_supported_fields()
    if supported is None:
        return kwargs
    accepted = {}
    for key, value in kwargs.items():
        if key in supported:
            accepted[key] = value
        else:
            logger.warning(
                "model_options: installed langchain-ollama does not accept "
                "%r; dropping it. Agent turns lose that option.", key)
    return accepted


def _langchain_supported_fields():
    """Field names the installed ``ChatOllama`` accepts, or ``None`` if unknown."""
    try:
        from langchain_ollama import ChatOllama
    except Exception:  # pragma: no cover - exercised only without the package
        return None
    fields = getattr(ChatOllama, "model_fields", None)
    if isinstance(fields, dict) and fields:
        return set(fields)
    fields = getattr(ChatOllama, "__fields__", None)
    if isinstance(fields, dict) and fields:
        return set(fields)
    return None


def chat_options(*, temperature=None, num_predict_override=None,
                 num_ctx_override=None) -> dict:
    """The ``options`` block every native ``/api/chat`` payload must carry.

    Configuring ``ChatOllama`` is not enough: helpers in other modules build
    their own ``/api/chat`` payload by hand, and a payload with no
    ``options`` dict AT ALL (``api_server._summarize_tool_result``) carries no
    ``num_ctx`` token to grep for, so it has to call this.

    Measured consequence of a payload without it, on the appliance:

        05:21:23  [Chat] Route: agent for: tell me the weather
        05:21:26  ollama WARN "requested context size too large for model"
                  num_ctx=262144 n_ctx_train=131072  ->  -c 131072 runner

    262144 is 131072 x 2 (OLLAMA_NUM_PARALLEL), i.e. the caller sent no
    ``num_ctx`` and ollama fell back to the model's whole trained context.
    That runner does not match the resident ``-c 8192`` one, so ollama starts
    a second copy of gpt-oss:20b and evicts qwen3:30b-a3b to fit it.
    """
    options = {
        "temperature": 0.7 if temperature is None else temperature,
        "num_ctx": num_ctx() if num_ctx_override is None else num_ctx_override,
        "num_predict": (num_predict() if num_predict_override is None
                        else num_predict_override),
    }
    return options


# ── Embedding models are NOT chat models ────────────────────────────────────
# Every path above sends the CONFIGURED chat context, and that is right for a
# chat model and wrong for an embedder: ollama logs
#
#     05:54:23 level=WARN ... requested context size too large for model
#               num_ctx=8192 n_ctx_train=2048
#
# when an embedder is asked for the chat context. ``nomic-embed-text`` is
# trained at 2048 tokens, so asking for 8192 makes ollama clamp AND re-plan
# the runner, a needless reload of a model the appliance wants pinned, and a
# WARN line in the journal on every restart.
#
# The rule this module enforces: options are PER MODEL ROLE. A chat model
# gets :func:`chat_options`; the embedding model gets :func:`embedding_options`
# — a context no larger than its own trained window, no ``num_predict`` (an
# embedder generates nothing), and the same resident pin from
# :func:`keep_alive`.

#: Trained context window (``n_ctx_train``) of the embedders PuffPAI can be
#: configured to use. Keys are the bare model name, without an ollama tag.
#: ``nomic-embed-text``'s 2048 is the value ollama itself reported on the
#: appliance in the WARN line quoted above.
EMBEDDING_TRAIN_WINDOW: dict = {
    "nomic-embed-text": 2048,
    "nomic-embed-text-v1.5": 2048,
    "mxbai-embed-large": 512,
    "all-minilm": 256,
    "snowflake-arctic-embed": 512,
    "bge-m3": 8192,
    "bge-large": 512,
}

#: Used for an embedder this table does not know. Deliberately the SMALL,
#: conservative value: over-asking is what produced the WARN and the reload,
#: while under-asking only means a long chunk is truncated the way the
#: embedder would have truncated it anyway.
DEFAULT_EMBEDDING_NUM_CTX = 2048


def _models_cfg():
    """Return ``config.get_config().models``, or ``None`` if unavailable."""
    try:
        from puffpai.config import get_config
        return get_config().models
    except Exception:  # pragma: no cover - config is present in every real run
        logger.debug("model_options: models config unavailable", exc_info=True)
        return None


def _bare_model_name(model: object) -> str:
    """``"nomic-embed-text:latest"`` -> ``"nomic-embed-text"``."""
    text = str(model or "").strip().lower()
    if not text:
        return ""
    # An ollama reference may carry a registry path and a tag.
    text = text.split("/")[-1]
    return text.split(":")[0]


def embedding_model() -> str:
    """The configured Ollama embedding model name, or the known default."""
    cfg = _models_cfg()
    if cfg is not None:
        value = getattr(cfg, "ollama_embedding_model", None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "nomic-embed-text"


def embedding_num_ctx(model: object = None) -> int:
    """Context to request for *model*, never above its trained window.

    Also bounded by the configured chat ``num_ctx``: a deployment that has
    deliberately lowered the appliance's context should not find the embedder
    quietly asking for more than the chat model does.
    """
    name = _bare_model_name(model if model is not None else embedding_model())
    trained = EMBEDDING_TRAIN_WINDOW.get(name, DEFAULT_EMBEDDING_NUM_CTX)
    return max(1, min(int(trained), int(num_ctx())))


def embedding_options(model: object = None) -> dict:
    """The ``options`` block an ``/api/embed`` payload must carry.

    Deliberately NOT :func:`chat_options`: no ``num_predict`` (nothing is
    generated) and no ``temperature`` (nothing is sampled), and a ``num_ctx``
    that respects the embedder's own trained window.
    """
    return {"num_ctx": embedding_num_ctx(model)}


def embedding_payload(model: object = None) -> dict:
    """The pin + options every embedding request should carry, as one dict."""
    return {"keep_alive": keep_alive(), "options": embedding_options(model)}


# ── Hidden reasoning is not one switch ──────────────────────────────────────
# A literal ``"think": False`` in every raw ``/api/chat`` payload is the
# right lever for a qwen3-family model, and it is a NO-OP for the model the
# appliance actually runs: ``gpt-oss:20b`` is a harmony-format model whose
# ``think`` parameter is a reasoning EFFORT LEVEL (``"low"``/``"medium"``/
# ``"high"``), not a boolean. Ollama ignores a boolean there, so gpt-oss keeps
# reasoning, spends the trivia writer's whole 300-token budget on the hidden
# channel and returns an EMPTY ``content``:
#
#     eval time = 15265.26 ms / 300 tokens   ("play trivia about space")
#     eval time = 15578.23 ms / 300 tokens   ("quiz me, hard")
#
# exactly the cap, no natural stop, while a plain weather answer on the SAME
# model stops naturally at 180 tokens. The switch is per-family, and it lives
# here rather than as a literal in five payload builders.

#: Families whose ``think`` parameter is a BOOLEAN on/off switch. ``False``
#: genuinely suppresses the hidden channel for these.
BOOLEAN_THINKING_FAMILIES = (
    "qwen3",
    "deepseek-r1",
    "qwq",
    "magistral",
    "exaone-deep",
    "cogito",
    "smollm3",
)

#: Families that LOOK like a thinking family by prefix but are not. Checked
#: FIRST, so the longest, most specific name wins. ``qwen3-coder`` starts with
#: ``qwen3`` yet its model card states it supports only non-thinking mode and
#: never emits ``<think>`` blocks — sending it ``think`` is at best noise and
#: at worst the 400 the empty-dict default exists to avoid.
NON_THINKING_FAMILIES = (
    "qwen3-coder",
    "qwen2.5-coder",
)

#: Families whose ``think`` parameter is a reasoning EFFORT LEVEL string.
#: Sending ``False`` here is silently ignored — the model reasons anyway.
EFFORT_THINKING_FAMILIES = (
    "gpt-oss",
)

#: The effort we ask an effort-level family for. ``"low"`` is the floor the
#: harmony format exposes: there is no "off". It still emits a reasoning
#: preamble, which is why :func:`structured_num_predict` exists.
DEFAULT_THINKING_EFFORT = "low"

#: Budget floor for a SHORT structured answer from a family that reasons
#: regardless of the flag. The measured failure was 300 tokens consumed
#: entirely by the preamble; the same model's ordinary prose answers finish
#: around 180-291 tokens, so 1024 leaves roughly 700 tokens of preamble head
#: room plus a full question. Chosen as the first power of two comfortably
#: above the observed preamble, and cheap because generation stops at the
#: model's own stop token, not at the cap.
MIN_REASONING_NUM_PREDICT = 1024


def model_family(model: object) -> str:
    """``"gpt-oss:20b"`` -> ``"gpt-oss"``. Registry path and tag are dropped."""
    return _bare_model_name(model)


def reasons_regardless(model: object) -> bool:
    """True when *model*'s family reasons even at its lowest setting.

    Callers that ask for a SHORT structured answer must widen their token
    budget for these, or the reasoning preamble eats the whole allowance and
    ``content`` comes back empty.
    """
    family = model_family(model)
    return any(family.startswith(prefix) for prefix in EFFORT_THINKING_FAMILIES)


def thinking_control(model: object) -> dict:
    """The payload fragment that governs hidden reasoning for *model*.

    Returns one of:

    * ``{"think": False}`` — boolean families (qwen3, deepseek-r1, ...).
    * ``{"think": "low"}`` — effort-level families (gpt-oss); see
      :data:`DEFAULT_THINKING_EFFORT`.
    * ``{}`` — everything else.

    The empty dict is also the answer for an UNKNOWN model, deliberately: a
    model with no thinking capability answers ``/api/chat`` with a 400 when
    ``think`` is present, and the raw callers this feeds have no
    probe-and-retry. Sending nothing costs a non-reasoning model nothing,
    while sending the wrong key costs it the whole call.

    Merge it into the payload with ``payload.update(thinking_control(m))`` —
    never assume the key is there.
    """
    family = model_family(model)
    if not family:
        return {}
    if any(family.startswith(p) for p in NON_THINKING_FAMILIES):
        return {}
    if any(family.startswith(p) for p in EFFORT_THINKING_FAMILIES):
        return {"think": DEFAULT_THINKING_EFFORT}
    if any(family.startswith(p) for p in BOOLEAN_THINKING_FAMILIES):
        return {"think": False}
    return {}


def structured_num_predict(model: object, desired: int) -> int:
    """Token budget for a SHORT structured answer from *model*.

    For a family that reasons regardless of the flag, raises *desired* to
    :data:`MIN_REASONING_NUM_PREDICT` so the low-effort preamble AND the
    answer both fit. Every other family keeps the caller's own number.
    """
    desired = int(desired)
    if reasons_regardless(model):
        return max(desired, MIN_REASONING_NUM_PREDICT)
    return desired
