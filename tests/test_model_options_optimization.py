"""Ollama request options: context size and the resident keep_alive pin.

Two properties:

Context size. A ``ChatOllama`` (or hand-built payload) with no ``num_ctx``
    makes ollama size the runner from the model's trained context
    (``num_ctx=262144 n_ctx_train=131072`` in its own journal), start a second
    ``llama-server`` at ``-c 131072`` for a 141-token prompt, and evict every
    resident model to fit it (unified memory drops from ~41 GB to ~8 GB in
    seconds). The same compound turn takes ~36 s cold against ~10 s warm.

The keep_alive pin. ``keep_alive`` comes from one place. A literal "30m" on
    any path, while the appliance config says ``-1``, resets the resident pin
    to a 30-minute eviction timer.
"""

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from puffpai.inference import model_options  # noqa: E402

_SRC = os.path.join(os.path.dirname(__file__), '..', 'src')


def _iter_python_sources():
    for root, dirs, files in os.walk(_SRC):
        dirs[:] = [d for d in dirs if not d.startswith('.bak-')]
        for name in files:
            if name.endswith('.py'):
                path = os.path.join(root, name)
                with open(path, encoding='utf-8') as fh:
                    yield path, fh.read()


# ── every ChatOllama construction carries the four options ──────────────────

def test_every_chatollama_construction_carries_the_four_options():
    """Grep over src/: no ChatOllama may be built without the options.

    It is deliberately textual rather than behavioural because the construction is
    unreachable in this dev environment (``_create_llm`` returns None on
    Python 3.14+ by design), and because the failure mode is a silently
    MISSING kwarg, which only source inspection can see.
    """
    constructions = []
    for path, source in _iter_python_sources():
        for match in re.finditer(r'ChatOllama\(', source):
            # Skip a mention inside a docstring/comment: require the call to
            # be an assignment or return, not prose.
            line_start = source.rfind('\n', 0, match.start()) + 1
            line = source[line_start:match.start()]
            if line.lstrip().startswith('#') or '``' in line:
                continue
            # Capture the balanced argument list.
            depth, i = 0, match.end() - 1
            while i < len(source):
                if source[i] == '(':
                    depth += 1
                elif source[i] == ')':
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            constructions.append((path, source[match.end():i]))

    assert constructions, "no ChatOllama construction found in src/ — has it moved?"

    for path, args in constructions:
        if 'langchain_chat_kwargs()' in args:
            # Delegated to the one source of truth; the options are asserted
            # directly by the tests below.
            continue
        for option in model_options.LANGCHAIN_REQUIRED_OPTIONS:
            assert f'{option}=' in args, (
                f"{path}: ChatOllama built without {option!r} and without "
                f"**model_options.langchain_chat_kwargs(). A missing num_ctx "
                f"makes ollama start a 131072-context runner and evict the "
                f"resident models.")


def test_langchain_chat_kwargs_supplies_all_four_options():
    kwargs = model_options.langchain_chat_kwargs()
    for option in model_options.LANGCHAIN_REQUIRED_OPTIONS:
        assert option in kwargs, f"langchain_chat_kwargs() dropped {option!r}"
    assert kwargs['reasoning'] is False
    assert isinstance(kwargs['num_ctx'], int) and kwargs['num_ctx'] > 0
    assert isinstance(kwargs['num_predict'], int) and kwargs['num_predict'] > 0


def test_langchain_chat_kwargs_does_not_hardcode_context(monkeypatch):
    """num_ctx must follow config, not a literal 8192."""
    monkeypatch.setattr(model_options, 'num_ctx', lambda: 6144)
    assert model_options.langchain_chat_kwargs()['num_ctx'] == 6144


def test_langchain_chat_kwargs_survives_a_version_without_reasoning(monkeypatch):
    """An older langchain-ollama must lose the option, not raise."""
    monkeypatch.setattr(model_options, '_langchain_supported_fields',
                        lambda: {'num_ctx', 'num_predict', 'keep_alive'})
    kwargs = model_options.langchain_chat_kwargs()
    assert 'reasoning' not in kwargs
    assert kwargs['num_ctx'] == model_options.num_ctx()


# ── one source of truth for the keep_alive pin ─────────────────────────────

def test_keep_alive_follows_config(monkeypatch):
    monkeypatch.delenv(model_options.KEEP_ALIVE_ENV, raising=False)

    class _Cfg:
        keep_alive = -1
    monkeypatch.setattr(model_options, '_ollama_cfg', lambda: _Cfg())
    assert model_options.keep_alive() == -1


def test_keep_alive_minus_one_is_a_number_not_a_string(monkeypatch):
    """Ollama's Go duration parser rejects the STRING "-1"."""
    monkeypatch.delenv(model_options.KEEP_ALIVE_ENV, raising=False)

    class _Cfg:
        keep_alive = "-1"
    monkeypatch.setattr(model_options, '_ollama_cfg', lambda: _Cfg())
    value = model_options.keep_alive()
    assert value == -1 and isinstance(value, int)


def test_keep_alive_env_overrides_config(monkeypatch):
    class _Cfg:
        keep_alive = -1
    monkeypatch.setattr(model_options, '_ollama_cfg', lambda: _Cfg())
    monkeypatch.setenv(model_options.KEEP_ALIVE_ENV, '30m')
    assert model_options.keep_alive() == '30m'


def test_keep_alive_falls_back_conservatively_when_config_is_broken(monkeypatch):
    """A config failure must never silently pin a multi-GB model forever."""
    monkeypatch.delenv(model_options.KEEP_ALIVE_ENV, raising=False)
    monkeypatch.setattr(model_options, '_ollama_cfg', lambda: None)
    assert model_options.keep_alive() == model_options._FALLBACK_KEEP_ALIVE
    assert model_options.keep_alive() != -1


def test_on_demand_pin_is_never_the_resident_pin(monkeypatch):
    """The 5.97 GB vision model must not be held forever."""
    monkeypatch.delenv(model_options.KEEP_ALIVE_ENV, raising=False)

    class _Cfg:
        keep_alive = -1
    monkeypatch.setattr(model_options, '_ollama_cfg', lambda: _Cfg())
    assert model_options.keep_alive_on_demand() == \
        model_options.DEFAULT_ON_DEMAND_KEEP_ALIVE


def test_on_demand_pin_never_exceeds_a_duration_resident_pin(monkeypatch):
    monkeypatch.setenv(model_options.KEEP_ALIVE_ENV, '5m')
    assert model_options.keep_alive_on_demand() == '5m'


def test_ollama_chat_default_keep_alive_is_the_configured_pin(monkeypatch):
    """api_server._ollama_chat defaults to the configured pin, not "30m"."""
    import requests

    from puffpai import api_server as mod

    captured = {}

    def _fake_post(url, json=None, timeout=None, stream=None, **kw):
        captured['payload'] = json
        return object()

    monkeypatch.setattr(requests, 'post', _fake_post)
    monkeypatch.setattr(model_options, 'keep_alive', lambda: -1)

    mod._ollama_chat([{"role": "user", "content": "hi"}])
    assert captured['payload']['keep_alive'] == -1, \
        "_ollama_chat must send the configured resident pin, not a literal"


def test_ollama_chat_still_honours_an_explicit_keep_alive(monkeypatch):
    import requests

    from puffpai import api_server as mod

    captured = {}
    monkeypatch.setattr(
        requests, 'post',
        lambda url, json=None, timeout=None, stream=None, **kw:
        captured.update(payload=json) or object())

    mod._ollama_chat([{"role": "user", "content": "hi"}], keep_alive="5m")
    assert captured['payload']['keep_alive'] == "5m"


def test_ollama_backend_resolves_a_none_keep_alive(monkeypatch):
    from puffpai.inference.ollama_backend import OllamaBackend
    monkeypatch.setattr(model_options, 'keep_alive', lambda: -1)
    backend = OllamaBackend(model="test-model")
    assert backend._keep_alive == -1


def test_no_module_hardcodes_a_keep_alive_literal():
    """Every keep_alive value must come from model_options."""
    offenders = []
    for path, source in _iter_python_sources():
        if os.path.normpath(path).endswith(os.path.join('inference', 'model_options.py')):
            continue  # the one place the fallback literal is allowed to live
        for lineno, line in enumerate(source.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith('#') or stripped.startswith('#:'):
                continue
            if 'keep_alive' not in line and 'KEEP_ALIVE' not in line:
                continue
            if re.search(r'(keep_alive|KEEP_ALIVE)\s*[:=]\s*["\']\d+[smh]["\']',
                         line):
                offenders.append(f"{path}:{lineno}: {stripped}")
    assert not offenders, (
        "hardcoded keep_alive literals reintroduce the split-pin defect:\n"
        + "\n".join(offenders))


def test_appliance_config_shape_yields_the_resident_pin(monkeypatch):
    """The box's puffpai_config.json ollama section must resolve to -1.

    This is the load-bearing assumption of the single keep_alive source: that
    ``keep_alive: -1`` in config/puffpai_config.json reaches
    ``OllamaConfig.keep_alive`` as the integer -1 (a JSON number; ollama's Go
    duration parser rejects the string "-1"). Values below are the shape of
    the appliance's ollama section.
    """
    from puffpai.config import OllamaConfig, _safe_init

    appliance_section = {
        "url": "http://localhost:11434",
        "model": "gpt-oss:20b",
        "temperature": 0.6,
        "num_ctx": 8192,
        "num_predict": 1024,
        "keep_alive": -1,
    }
    cfg = _safe_init(OllamaConfig, appliance_section)
    assert cfg.keep_alive == -1 and isinstance(cfg.keep_alive, int)
    assert cfg.num_ctx == 8192
    assert cfg.num_predict == 1024

    monkeypatch.delenv(model_options.KEEP_ALIVE_ENV, raising=False)
    monkeypatch.setattr(model_options, '_ollama_cfg', lambda: cfg)
    assert model_options.keep_alive() == -1
    assert model_options.num_ctx() == 8192
    assert model_options.num_predict() == 1024
    kwargs = model_options.langchain_chat_kwargs()
    assert kwargs['num_ctx'] == 8192
    assert kwargs['num_predict'] == 1024
    assert kwargs['keep_alive'] == -1
    assert kwargs['reasoning'] is False


# ── no hand-built /api/chat payload may omit num_ctx ───────────────────────
# Guarding ChatOllama is not enough: helpers in other modules build their own
# /api/chat payload, and a payload that sends NO "options" dict at all is
# invisible to a sweep for "paths that send num_ctx", because there is no
# num_ctx token in the file to grep for.


def _ollama_payload_literals():
    """Every dict LITERAL in src/ that is an ollama chat payload.

    The unit is the payload literal, not the enclosing function and not the
    module. A function is too narrow — ``ollama_backend._post_chat(payload)``
    and ``engine._post`` receive a payload built by a sibling, so they would
    report as missing everything. A module is too coarse — ``api_server``
    contains ``_ollama_chat`` (which does send num_ctx), so a module-level
    check would stay green while a sibling payload in the same module omits it.

    A payload literal is identified by carrying both a ``"model"`` and a
    ``"messages"`` key, which every /api/chat body has and nothing else does.
    """
    literals = []
    for path, source in _iter_python_sources():
        for match in re.finditer(r'"messages"\s*:', source):
            # walk back to the opening brace of the enclosing dict literal
            depth, start = 0, None
            for i in range(match.start(), -1, -1):
                ch = source[i]
                if ch == '}':
                    depth += 1
                elif ch == '{':
                    if depth == 0:
                        start = i
                        break
                    depth -= 1
            if start is None:
                continue
            depth, end = 0, None
            for i in range(start, len(source)):
                ch = source[i]
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            if end is None:
                continue
            body = source[start:end]
            if '"model"' not in body:
                continue
            # Some callers assign payload["keep_alive"] just AFTER the
            # literal, so carry a trailing window for that check only.
            window = source[start:min(len(source), end + 500)]
            if (path, body, window) not in literals:
                literals.append((path, body, window))
    return literals


#: Payload literals that are allowed to skip a check, each with its reason.
#: vision.py drives qwen2.5vl:7b, a DIFFERENT model whose image-token
#: budget has no measurement on this box. Capping its context
#: on a guess could truncate an image and break OCR — trading
#: a CONFIRMED capability for a speed win. num_ctx exempt only;
#: it still must carry keep_alive.
#: ui/chat_view the legacy Tkinter UI, present in the tree but not run
#: on the appliance. Editing dead code adds surface for no
#: measured gain.
#: agent_graph its _backend_chat legacy fallback posts to
#: /v1/chat/completions, which SILENTLY IGNORES "options" (see
#: api_server), so it cannot carry num_ctx no matter what is
#: added to the payload. Changing it is a TRANSPORT change
#: (native /api/chat returns a different response shape), on
#: a path unreachable while a backend is injected.
def _is_exempt(path, vision_ok=True):
    norm = os.path.normpath(path)
    if vision_ok and norm.endswith(os.path.join('puffpai', 'vision.py')):
        return True
    if norm.endswith(os.path.join('ui', 'chat_view.py')):
        return True
    return norm.endswith(os.path.join('puffpai', 'agent_graph.py'))


def test_the_api_chat_site_detector_actually_finds_sites():
    """A guard whose detector finds nothing would pass forever."""
    sites = _ollama_payload_literals()
    assert len(sites) >= 4, (
        "detector found %d /api/chat sites; it should see at least the "
        "summariser, trivia, ingestion and vision paths" % len(sites))


def test_no_hand_built_api_chat_payload_omits_num_ctx():
    """A payload with no num_ctx makes ollama over-allocate.

    What ollama logs when a caller sends no num_ctx, seconds after
    ``[Chat] Route: agent for: tell me the weather``::

        ollama WARN "requested context size too large for model"
                    num_ctx=262144 n_ctx_train=131072 -> -c 131072 runner

    262144 is 131072 x 2 (OLLAMA_NUM_PARALLEL): with no num_ctx at all,
    ollama falls back to the model's whole trained context, starts a second
    copy of gpt-oss:20b that does not match the resident -c 8192 one, and
    evicts qwen3:30b-a3b to fit it.

    Vision is EXEMPT: it drives a different model (qwen2.5vl:7b) whose image
    token budget has no measurement on this box, and capping its context on a
    guess could truncate an image and break OCR, trading a CONFIRMED
    capability for a speed win, which is not a trade this project makes.
    """
    offenders = []
    for path, body, _window in _ollama_payload_literals():
        if _is_exempt(path):
            continue
        if 'num_ctx' in body or 'chat_options(' in body:
            continue
        offenders.append('%s: /api/chat payload with no num_ctx' % path)
    assert not offenders, (
        "a hand-built /api/chat payload without num_ctx makes ollama size the "
        "runner from the model's trained context and evict the resident "
        "set:\n" + "\n".join(offenders))


def test_no_hand_built_api_chat_payload_omits_keep_alive():
    offenders = []
    for path, _body, window in _ollama_payload_literals():
        if _is_exempt(path, vision_ok=False):
            continue
        if 'keep_alive' in window or 'KEEP_ALIVE' in window:
            continue
        offenders.append('%s: /api/chat payload with no keep_alive' % path)
    assert not offenders, (
        "a payload with no keep_alive resets the resident pin to the server "
        "default for whichever model it touches:\n" + "\n".join(offenders))


def test_chat_options_always_carries_num_ctx_from_config(monkeypatch):
    monkeypatch.setattr(model_options, 'num_ctx', lambda: 6144)
    monkeypatch.setattr(model_options, 'num_predict', lambda: 777)
    opts = model_options.chat_options()
    assert opts['num_ctx'] == 6144
    assert opts['num_predict'] == 777
    opts = model_options.chat_options(temperature=0.1, num_predict_override=300)
    assert opts['num_ctx'] == 6144
    assert opts['num_predict'] == 300
    assert opts['temperature'] == 0.1


# ── options are PER MODEL ROLE, not one size for every model ────────────────
# If every path sent the configured chat num_ctx, it would reach
# ``nomic-embed-text`` (n_ctx_train 2048) and ollama would log:
#
# level=WARN... requested context size too large for model
# num_ctx=8192 n_ctx_train=2048
#
# These tests check a CHAT payload against the chat rules and an EMBEDDINGS
# payload against the embedding rules, and keep the two distinct.

_CHAT_PAYLOAD_KEYS = ("temperature", "num_ctx", "num_predict")


def _chat_payload():
    """The shape ``api_server._summarize_tool_result`` and friends send."""
    return {
        "model": "gpt-oss:20b",
        "stream": False,
        "messages": [{"role": "user", "content": "ping"}],
        "keep_alive": model_options.keep_alive(),
        "options": model_options.chat_options(),
    }


def _embeddings_payload(model="nomic-embed-text"):
    """The shape ``embeddings.OllamaEmbeddingFunction`` sends to /api/embed."""
    payload = {"model": model, "input": ["ping"]}
    payload.update(model_options.embedding_payload(model))
    return payload


def test_chat_payload_carries_the_chat_options():
    options = _chat_payload()["options"]
    for key in _CHAT_PAYLOAD_KEYS:
        assert key in options, f"chat payload lost {key!r}"
    assert options["num_ctx"] == model_options.num_ctx()


def test_embeddings_payload_never_exceeds_the_model_train_window():
    """Never 8192 asked of a model trained at 2048."""
    for name, trained in model_options.EMBEDDING_TRAIN_WINDOW.items():
        payload = _embeddings_payload(name)
        asked = payload["options"]["num_ctx"]
        assert asked <= trained, (
            f"embeddings payload for {name!r} asks num_ctx={asked} against "
            f"n_ctx_train={trained}, which makes ollama WARN "
            f"'requested context size too large for model'.")
        assert asked >= 1


def test_embeddings_payload_is_not_the_chat_payload():
    """An embedder generates nothing and samples nothing."""
    options = _embeddings_payload()["options"]
    assert "num_predict" not in options, (
        "an embedding request must not carry a generation budget")
    assert "temperature" not in options
    assert set(options) == {"num_ctx"}


def test_embeddings_payload_still_carries_the_resident_pin():
    payload = _embeddings_payload()
    assert payload["keep_alive"] == model_options.keep_alive()


def test_embedding_tag_and_registry_prefixes_resolve(monkeypatch):
    assert (model_options.embedding_num_ctx("nomic-embed-text:latest")
            == model_options.EMBEDDING_TRAIN_WINDOW["nomic-embed-text"])
    assert (model_options.embedding_num_ctx("registry.ollama.ai/library/"
                                            "nomic-embed-text:v1.5")
            == model_options.EMBEDDING_TRAIN_WINDOW["nomic-embed-text"])


def test_unknown_embedder_gets_the_conservative_default():
    assert (model_options.embedding_num_ctx("some-new-embedder")
            == min(model_options.DEFAULT_EMBEDDING_NUM_CTX,
                   model_options.num_ctx()))


def test_embedding_num_ctx_never_exceeds_the_configured_chat_context(monkeypatch):
    """A deployment that lowers the appliance context lowers this too."""
    monkeypatch.setattr(model_options, 'num_ctx', lambda: 1024)
    assert model_options.embedding_num_ctx("bge-m3") == 1024


def test_embed_call_sites_do_not_send_chat_options():
    """Grep guard: no /api/embed payload may reach for chat_options."""
    offenders = []
    for path, source in _iter_python_sources():
        for match in re.finditer(r'api/embed(?:dings)?', source):
            window = source[max(0, match.start() - 1200):match.start() + 600]
            if 'chat_options(' in window:
                offenders.append(path)
    assert not offenders, (
        f"{offenders}: an /api/embed payload is being built with "
        f"chat_options(). Use model_options.embedding_payload() — the chat "
        f"num_ctx is larger than an embedder's trained window.")
