"""On-demand local CODING model — "write me a script/function/automation".

One capability slot (``dev.code``), one entry point (:func:`generate_code`),
one model tag in config (``cfg.ollama.code_model``). The tag is deliberately
NOT hardcoded here: ``qwen2.5-coder:14b`` and ``qwen3-coder:30b`` are both
candidates, and the appliance config names the one it runs.
An EMPTY tag means the feature is off, and every caller must handle that: a
development machine may have no coding model pulled at all.

Three rules this module exists to keep:

* **On demand, never pinned.** The pin is ``config.ollama.code_keep_alive``
  (default ``"10m"``) via :func:`model_options.keep_alive_code`, so the weights
  are handed back to the resident chat/planner engines minutes after the last
  coding turn. This is the same lifecycle the vision model already has.
* **Fail soft, never fabricate.** Ollama returning 404 (tag not pulled), a
  connection error, or prose with no fenced block are all RESULTS, not
  exceptions and not invented code. ``ok`` is False and ``explanation`` says
  plainly what happened.
* **No egress.** The only host contacted is the configured Ollama URL, which
  must be loopback. There is no second URL, no fallback host, and no API key.

Why this posts ``/api/chat`` directly instead of going through
:class:`~puffpai.inference.ollama_backend.OllamaBackend`: that class hardcodes
``think: False`` on every request and discovers per-process, through a failed
round trip, that a model rejects the field. The coding model's answer to
"should ``think`` be sent at all" is a per-family FACT the codebase already
knows (:func:`model_options.thinking_control`), so it is sent correctly the
first time. This mirrors :mod:`puffpai.vision`, the other on-demand model path.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import requests

from . import model_options as _model_options

logger = logging.getLogger(__name__)

#: Wall-clock ceiling for one coding turn: (connect, read) tuple for
#: ``requests``. A qwen3-coder call can run away past 18 minutes with no
#: natural stop; this is the hard client-side kill against that: 10 s to
#: connect, 180 s to read the whole reply.
#: Closing the connection on timeout makes ollama cancel the generation
#: instead of leaving it running and holding the GPU.
DEFAULT_TIMEOUT = (10, 180)

#: The contract with the model. Everything the parser and the safety posture
#: depend on is stated here, in the imperative, once.
SYSTEM_PROMPT = """You are the coding model inside PuffPAI, a private home \
appliance that runs fully offline.

Answer with EXACTLY this shape and nothing else:

1. ONE fenced code block, opened with ``` and the language name (for example \
```python), containing the complete code and nothing but the code.
2. After the block, an explanation of AT MOST 3 sentences.

Hard rules:
- Do NOT show your reasoning, a plan, or any step-by-step thinking.
- Do NOT emit more than one fenced block. Put the whole answer in one block.
- Do NOT shell out, call the network, read or write files outside the \
directory the code is given, use eval/exec on untrusted input, or reference \
credentials, tokens or secrets — unless the request explicitly asks for it.
- If the request is ambiguous, choose the smallest reasonable reading, write \
the code, and say what you assumed in the explanation.
"""

#: The first fenced block. ``info`` is the language hint on the opening fence.
_FENCE_RE = re.compile(
    r"```[ \t]*(?P<info>[^\n`]*)\n(?P<body>.*?)```",
    re.DOTALL,
)
#: An OPENED but never closed block — the shape a truncated answer has.
_OPEN_FENCE_RE = re.compile(r"```[ \t]*(?P<info>[^\n`]*)\n(?P<body>.*)\Z",
                            re.DOTALL)


@dataclass(frozen=True)
class CodeResult:
    """One coding turn. Never raises out of :func:`generate_code`.

    ``code`` is the fenced block's contents (empty when the model answered
    with prose only — see ``unparsed``). ``explanation`` is the prose around
    it, or the plain-words reason the call could not be made.
    """

    code: str = ""
    language: str = ""
    explanation: str = ""
    model: str = ""
    ms: int = 0
    truncated: bool = False
    #: The reply had no fenced block at all; ``explanation`` holds it verbatim.
    unparsed: bool = False
    #: Machine-readable failure kind: "", "not_configured", "model_missing",
    #: "unreachable", "non_loopback", "error".
    error: str = ""

    @property
    def ok(self) -> bool:
        """True when there is real generated code to show."""
        return bool(self.code) and not self.error

    def as_markdown(self) -> str:
        """Chat-ready reply: explanation, the fenced block, then the voice note.

        The trailing sentence is load-bearing for the voice path:
        :func:`puffpai.voice_text.speakable` replaces the fenced block with
        "(code omitted)", so without it a spoken turn would say nothing about
        where the code actually went.
        """
        if not self.ok:
            return self.explanation
        parts = []
        if self.explanation:
            parts.append(self.explanation)
        parts.append(f"```{self.language}\n{self.code.rstrip()}\n```")
        if self.truncated:
            parts.append("Heads up: that answer hit the length limit, so the "
                         "code may be cut off at the end.")
        parts.append("The full code is in this chat on your dashboard — "
                     "I won't read it out loud.")
        return "\n\n".join(parts)


def _ollama_cfg():
    """``config.get_config().ollama`` or None (mirrors model_options)."""
    try:
        from puffpai.config import get_config
        return get_config().ollama
    except Exception:  # pragma: no cover - config is present in every real run
        logger.debug("code_model: config unavailable", exc_info=True)
        return None


def configured_model() -> str:
    """The coding model tag, or "" when the feature is off."""
    cfg = _ollama_cfg()
    return str(getattr(cfg, "code_model", "") or "").strip() if cfg else ""


def is_configured() -> bool:
    """True when a coding model tag is set. False = feature OFF, decline."""
    return bool(configured_model())


def is_loopback(url: str) -> bool:
    """True when *url*'s host is this machine. The only host this module may
    contact — a coding model is not a reason to open an outbound door."""
    host = (urlsplit(url or "").hostname or "").strip()
    if not host:
        return False
    if host.lower() in ("localhost", "localhost.localdomain"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _options(model: str, cfg) -> dict:
    """The ``options`` block for a coding request, from config only."""
    return {
        "temperature": float(getattr(cfg, "code_temperature", 0.2) or 0.2),
        "num_ctx": int(getattr(cfg, "code_num_ctx", 16384) or 16384),
        # A coding answer is long by nature; the reasoning-family floor still
        # applies if a thinking model is ever configured here.
        "num_predict": _model_options.structured_num_predict(
            model, int(getattr(cfg, "code_num_predict", 2048) or 2048)),
    }


def build_prompt(spec: str, language: str | None = None,
                 context: str | None = None) -> str:
    """The user message: the request, the language ask, and *context* verbatim.

    ``context`` (usually the function the user pasted) is fenced and passed
    through UNTOUCHED — the whole point of pasting it is that the model sees
    exactly what the user has, not a summary of it.
    """
    parts = [f"Request: {(spec or '').strip()}"]
    if language:
        parts.append(f"Language: {language.strip()}")
    if context and context.strip():
        parts.append("Existing code / context to work from (verbatim):\n"
                     f"```\n{context.rstrip()}\n```")
    return "\n\n".join(parts)


def parse_reply(text: str, fallback_language: str = "") -> tuple:
    """``(code, language, explanation, truncated, unparsed)`` from a raw reply.

    Only the FIRST fenced block is taken as code; anything before and after it
    is the explanation. A block that was opened and never closed is returned
    with ``truncated=True``. A reply with no fence at all yields no code and
    ``unparsed=True`` — the raw text comes back as the explanation so the user
    sees what the model actually said. Nothing here invents code.
    """
    raw = (text or "").strip()
    if not raw:
        return "", fallback_language, "", False, True
    match = _FENCE_RE.search(raw)
    truncated = False
    if match is None:
        match = _OPEN_FENCE_RE.search(raw)
        if match is None:
            return "", fallback_language, raw, False, True
        truncated = True
    info = (match.group("info") or "").strip().lower()
    code = match.group("body")
    explanation = (raw[:match.start()] + " " + raw[match.end():])
    explanation = re.sub(r"\s*\n\s*\n\s*", "\n\n", explanation).strip()
    language = info.split()[0] if info else (fallback_language or "")
    return code.strip("\n"), language, explanation, truncated, False


def generate_code(spec: str, language: str | None = None,
                  context: str | None = None,
                  *, timeout: tuple = DEFAULT_TIMEOUT) -> CodeResult:
    """Write code for *spec* with the configured on-demand coding model.

    Returns a :class:`CodeResult` in every case, including "no model
    configured", "model not installed", and "Ollama is not answering". It
    never raises into a chat turn and never returns code it did not receive.
    """
    model = configured_model()
    if not model:
        return CodeResult(
            error="not_configured",
            explanation="A coding model is not configured on this box yet, so "
                        "I can't write code for you. Once one is set up in my "
                        "settings, ask me again.")
    cfg = _ollama_cfg()
    url = str(getattr(cfg, "url", "") or "").rstrip("/")
    if not is_loopback(url):
        # Deliberate refusal, not a fallback: a coding request must never be
        # the thing that sends a household's code to another machine.
        logger.warning("code_model: refusing non-loopback Ollama URL")
        return CodeResult(
            model=model, error="non_loopback",
            explanation="My coding model is only allowed to run on this box, "
                        "and it's currently pointed somewhere else. I've left "
                        "it alone.")

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",
             "content": build_prompt(spec, language, context)},
        ],
        "stream": False,
        # On-demand pin, NOT the resident one — see keep_alive_code().
        "keep_alive": _model_options.keep_alive_code(),
        "options": {
            **_options(model, cfg),
            # Restated LITERALLY here, with the same value _options() gives,
            # so the payload itself carries num_ctx: a /api/chat body without
            # it silently takes the daemon's default context (the model's
            # whole trained window), which starts a second runner and evicts
            # the resident set. Written at the construction site so the
            # contract is visible where the payload is built.
            "num_ctx": int(getattr(cfg, "code_num_ctx", 16384) or 16384),
        },
    }
    # Per-family, never hardcoded: {} for the coder families (they have no
    # hidden channel to switch off), {"think": False} / {"think": "low"} if a
    # reasoning model is ever configured into this slot.
    payload.update(_model_options.thinking_control(model))

    started = time.monotonic()
    try:
        response = requests.post(f"{url}/api/chat", json=payload,
                                 timeout=timeout)
    except requests.exceptions.Timeout as exc:
        # A runaway generation with no natural stop. Raising here closes the
        # connection, which makes ollama cancel the in-flight generation rather
        # than leave it running and holding the GPU. Never propagates into a
        # chat turn.
        logger.warning("code_model: %s timed out after %r: %s", url, timeout,
                       exc)
        return CodeResult(
            model=model, error="timeout", truncated=True,
            ms=int((time.monotonic() - started) * 1000),
            explanation="My coding model took too long to answer, so I "
                        "stopped waiting. Try asking for something smaller, "
                        "or ask again.")
    except requests.exceptions.RequestException as exc:
        logger.warning("code_model: %s unreachable: %s", url, exc)
        return CodeResult(
            model=model, error="unreachable",
            ms=int((time.monotonic() - started) * 1000),
            explanation="I couldn't reach my local model service just now, so "
                        "I haven't written anything.")
    ms = int((time.monotonic() - started) * 1000)

    if response.status_code == 404:
        return CodeResult(
            model=model, error="model_missing", ms=ms,
            explanation=f"My coding model ({model}) isn't installed on this "
                        "box yet, so I can't write code until it's pulled.")
    if response.status_code >= 400:
        return CodeResult(
            model=model, error="error", ms=ms,
            explanation=f"My coding model returned an error "
                        f"({response.status_code}), so I have nothing to show "
                        "you rather than a guess.")
    try:
        data = response.json()
        text = data["message"]["content"]
    except Exception as exc:
        logger.warning("code_model: unreadable reply: %s", exc)
        return CodeResult(
            model=model, error="error", ms=ms,
            explanation="My coding model answered in a shape I couldn't read, "
                        "so I have nothing to show you.")

    code, lang, explanation, truncated, unparsed = parse_reply(
        text, (language or "").strip().lower())
    if str((data or {}).get("done_reason") or "") == "length":
        truncated = True
    if unparsed:
        return CodeResult(
            model=model, ms=ms, unparsed=True, truncated=truncated,
            language=lang,
            explanation=explanation or
            "My coding model answered without any code in it.")
    return CodeResult(code=code, language=lang, explanation=explanation,
                      model=model, ms=ms, truncated=truncated)
