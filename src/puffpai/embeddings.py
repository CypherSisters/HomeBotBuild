"""
PuffPAI Embedding Functions
===========================
ChromaDB-compatible embedding functions, selected by config.

Three backends:

``chroma_default`` (DEFAULT — the one a base install can actually run)
    ChromaDB's own ``DefaultEmbeddingFunction``: all-MiniLM-L6-v2 as ONNX,
    run by ``onnxruntime``. Both ``onnxruntime`` and ``tokenizers`` are hard
    dependencies of ``chromadb``, which is in ``requirements.txt`` — so this
    backend needs **no extra package and no model pulled into Ollama**. It is
    also the same model, and therefore the same vector space, as the legacy
    sentence-transformers backend and as what every existing collection
    persisted (an empty EF config, which chroma resolves to exactly this
    function), so making it the default needs no re-index.

    The one thing it needs is the model FILES, which chroma fetches once from
    ``chroma-onnx-models.s3.amazonaws.com`` into ``~/.cache/chroma``. That is
    egress, so it has a Bridge switch (``embedding_model_download``) and the
    cache is checked before chroma gets near a socket: files present → fully
    offline, files absent and the switch off → a legible refusal, never a
    silent empty result. :func:`embedding_status` is what the status endpoints render.

``sentence_transformers`` (legacy — needs the ``embeddings-legacy`` extra)
    ChromaDB's ``SentenceTransformerEmbeddingFunction`` with
    ``all-MiniLM-L6-v2``, i.e. the same vectors as above by way of PyTorch.
    For installs that already have PyTorch. It is not the default because a
    base install does not ship ``sentence-transformers``: the first query
    would raise ImportError and RAG would return nothing at all.

``ollama``
    A GGUF embedder served by Ollama's ``/api/embed`` (default
    ``nomic-embed-text``). GPU-accelerated on the Orin and the wizard's
    upgrade path: it is offered when ``nomic-embed-text`` is already in
    Ollama's model list. It is a DIFFERENT vector space from the two backends
    above, so switching to it MUST be paired with a full re-index — a fresh
    install has nothing to re-index, an existing one does.

Switching to Ollama embeddings (once, with a re-index):

    1. back up the ChromaDB directory
    2. ``ollama pull nomic-embed-text``
    3. set ``models.embedding_backend = "ollama"`` in
       ``config/puffpai_config.json``
    4. re-index **both** halves — these cover different collections and
       skipping either leaves part of the store in the old vector space::

           python scripts/reindex.py --force          # ZIM libraries
           python scripts/reindex.py --reembed --yes  # user_uploads,
                                                      # puffpai_vault,
                                                      # puffpai_knowledge

       Both exit non-zero if any collection is left incomplete — check the
       exit code, do not just read the summary line.

**Call sites must pass vectors, not text.** Not because chroma loses the
embedding function — it persists it and does apply it on a reopened handle
(only the ``_embedding_function`` *attribute* misreports as
``DefaultEmbeddingFunction``, which makes this easy to get backwards). The
problem is what existing collections persisted: collections created before
EF pinning carry an empty config, so chroma embeds them with its own ONNX
all-MiniLM-L6-v2 — and an explicit function can no longer be attached to them
(see `open_collection`). Passing ``embeddings=`` / ``query_embeddings=`` built
by `embed_documents` makes the *configured* function authoritative no matter
what any given collection persisted. That is the invariant the backend
switch depends on.

``sentence-transformers`` is NOT in ``requirements.txt`` — it drags PyTorch in,
which on Jetson must come from NVIDIA's wheel index. It is declared as the
``embeddings-legacy`` extra in ``setup.py``::

    pip install -e ".[embeddings-legacy]"
"""

from __future__ import annotations

import importlib.util
import logging
import os
import threading
from typing import Any

import requests
from chromadb.api.types import (DefaultEmbeddingFunction, Documents,
                                EmbeddingFunction, Embeddings)

from .config import DEFAULT_OLLAMA_URL, get_config

logger = logging.getLogger(__name__)

# The model every existing collection was built with. Changing this without a
# full re-index corrupts retrieval quality silently.
LEGACY_EMBEDDING_MODEL = "all-MiniLM-L6-v2"
DEFAULT_OLLAMA_EMBEDDING_MODEL = "nomic-embed-text"

BACKEND_SENTENCE_TRANSFORMERS = "sentence_transformers"
BACKEND_OLLAMA = "ollama"
BACKEND_CHROMA_DEFAULT = "chroma_default"

#: The shipped default. Runs on a base ``pip install -r requirements.txt`` with
#: nothing else installed and nothing pulled into Ollama.
DEFAULT_BACKEND = BACKEND_CHROMA_DEFAULT

#: Door that covers chroma's one-time fetch of the ONNX model files.
ONNX_DOWNLOAD_DOOR = "embedding_model_download"


class OllamaEmbeddingFunction(EmbeddingFunction[Documents]):
    """Embed documents with a GGUF model served by Ollama's ``/api/embed``.

    ``/api/embed`` (not the older ``/api/embeddings``) takes a batch of
    strings and returns one vector per input, in order.
    """

    def __init__(self,
                 model_name: str = DEFAULT_OLLAMA_EMBEDDING_MODEL,
                 url: str | None = None,
                 timeout: int = 120) -> None:
        self._model_name = model_name
        self._url = (url or DEFAULT_OLLAMA_URL).rstrip("/")
        self._timeout = timeout

    def __call__(self, input: Documents) -> Embeddings:
        texts = [t for t in input]
        if not texts:
            # Chroma's own validation rejects an empty result, so returning []
            # here would only turn a caller bug into a confusing downstream
            # error. Say what actually happened instead.
            raise ValueError(
                "OllamaEmbeddingFunction called with no input documents")
        # Options are PER MODEL ROLE (model_options): an embedder gets
        # a context no larger than its own n_ctx_train and no num_predict.
        # Sending nothing lets ollama fall back to its server-wide default,
        # which on the appliance is the chat context (8192) and produces
        # "requested context size too large for model num_ctx=8192
        # n_ctx_train=2048" plus a needless runner re-plan.
        # keep_alive carries the configured resident pin so an embed call
        # cannot reset the embedder's residency to the ollama default.
        payload = {"model": self._model_name, "input": texts}
        try:
            from puffpai.inference import model_options as _mo
            payload.update(_mo.embedding_payload(self._model_name))
        except Exception:  # pragma: no cover - config/import failure only
            pass
        resp = requests.post(
            f"{self._url}/api/embed",
            json=payload,
            timeout=self._timeout,
        )
        resp.raise_for_status()
        vectors = (resp.json() or {}).get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise RuntimeError(
                f"Ollama /api/embed returned {len(vectors) if isinstance(vectors, list) else 'no'} "
                f"vectors for {len(texts)} inputs (model={self._model_name!r}). "
                f"Is the model pulled? `ollama pull {self._model_name}`"
            )
        return vectors

    @staticmethod
    def name() -> str:
        return "puffpai_ollama"

    def get_config(self) -> dict[str, Any]:
        return {
            "model_name": self._model_name,
            "url": self._url,
            "timeout": self._timeout,
        }

    @staticmethod
    def build_from_config(config: dict[str, Any]) -> "OllamaEmbeddingFunction":
        return OllamaEmbeddingFunction(**config)

    def default_space(self) -> str:
        # New PuffPAI collections are pinned to cosine (see
        # ingestion.get_or_create_upload_collection).
        return "cosine"


# Register so Chroma can rebuild the function when re-opening a persisted
# collection that was created with it. Harmless (and idempotent-ish) if the
# name is already taken by a previous import in the same process.
try:  # pragma: no cover - registry behaviour is a chromadb detail
    from chromadb.utils.embedding_functions import register_embedding_function
    register_embedding_function(OllamaEmbeddingFunction)
except Exception as exc:  # noqa: BLE001 - never let registration break boot
    logger.debug("OllamaEmbeddingFunction not registered with chromadb: %s", exc)


_MISSING_ST_MESSAGE = (
    "The legacy embedding backend needs sentence-transformers "
    "(and PyTorch), which are not installed by default:\n"
    '    pip install -e ".[embeddings-legacy]"\n'
    "On Jetson, install torch from NVIDIA's wheel index first. "
    "Alternatively set models.embedding_backend = \"ollama\" in "
    "config/puffpai_config.json — but that requires a full re-index "
    "of the ChromaDB store."
)


def _sentence_transformer_ef(model_name: str):
    """Build the legacy sentence-transformers embedding function.

    The availability probe is ours, deliberately. ChromaDB 1.5.8 catches the
    ImportError internally and re-raises it as a ValueError whose message is
    just "pip install sentence_transformers" — which would bury the extra name
    and the Jetson wheel-index caveat. Checking find_spec() first means our
    message is the one the operator actually sees; the ValueError catch below
    is the belt-and-braces path for a package that is importable but broken.
    """
    if importlib.util.find_spec("sentence_transformers") is None:
        raise ImportError(_MISSING_ST_MESSAGE)
    try:
        from chromadb.utils.embedding_functions import (
            SentenceTransformerEmbeddingFunction)
        return SentenceTransformerEmbeddingFunction(model_name=model_name)
    except (ImportError, ValueError) as exc:
        raise ImportError(f"{_MISSING_ST_MESSAGE}\n(underlying error: {exc})") from exc


#: Files chroma's ONNX backend needs on disk. Same list chroma itself checks.
_ONNX_FILES = ("config.json", "model.onnx", "special_tokens_map.json",
               "tokenizer_config.json", "tokenizer.json", "vocab.txt")


def onnx_model_dir() -> str:
    """Where chroma keeps (or would keep) the ONNX model files."""
    try:
        from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import (
            ONNXMiniLM_L6_V2)
        return os.path.join(ONNXMiniLM_L6_V2.DOWNLOAD_PATH,
                            ONNXMiniLM_L6_V2.EXTRACTED_FOLDER_NAME)
    except Exception:  # pragma: no cover - chromadb layout change only
        return os.path.join(os.path.expanduser("~"), ".cache", "chroma",
                            "onnx_models", "all-MiniLM-L6-v2", "onnx")


def onnx_model_present() -> bool:
    """True when the ONNX model files are already on this box (no fetch needed)."""
    folder = onnx_model_dir()
    return all(os.path.exists(os.path.join(folder, name))
               for name in _ONNX_FILES)


_MISSING_ONNX_MESSAGE = (
    "The embedding model files are not on this box yet. They are a one-time "
    "~80 MB download from chroma-onnx-models.s3.amazonaws.com, which is the "
    "'embedding_model_download' egress door. Turn it on by setting "
    "egress_doors.embedding_model_download to true in "
    "config/puffpai_config.json (or PUFFPAI_EGRESS_EMBEDDING_MODEL_DOWNLOAD=1 "
    "in the environment) and ask a knowledge question again, or copy the folder "
    "{folder} over from another machine. "
    "Until then, knowledge search has no way to compare your question to "
    "anything and will find nothing."
)


def _chroma_default_ef():
    """Build ChromaDB's own ONNX all-MiniLM-L6-v2 function.

    Refuses BEFORE constructing it when the model files are absent and the
    download door is shut: chroma fetches them itself, on its own socket, the
    first time the function is called, so this tree can only honour the door
    before chroma is handed the text. :func:`chroma_default_may_run` is that
    one answer; the function returned asks it again on every call (see
    :class:`_DoorCheckedDefaultEF`).
    """
    if not chroma_default_may_run():
        raise RuntimeError(
            _MISSING_ONNX_MESSAGE.format(folder=onnx_model_dir()))
    if not onnx_model_present():
        logger.info("Embeddings: fetching the ONNX model files once (door %r "
                    "is open)", ONNX_DOWNLOAD_DOOR)
    return _DoorCheckedDefaultEF()


def chroma_default_may_run() -> bool:
    """May chroma's own ONNX function run without fetching behind a shut door?

    True when the model files are already on disk (nothing to fetch) or the
    ``embedding_model_download`` door is open. Every other answer is "no",
    including a door check that raises: as in the registry, an ambiguity is
    never consent.

    This is the single answer for everything that lets chroma embed text with
    its default function: :func:`_chroma_default_ef`, the function it returns,
    and the degraded per-collection ``query_texts=`` fallback in
    ``api_server._get_rag_context`` (where a legacy collection embeds with
    chroma's default because it persisted no function of its own).
    """
    try:
        if onnx_model_present():
            return True
        from puffpai.security.egress_doors import egress_allowed
        return bool(egress_allowed(ONNX_DOWNLOAD_DOOR)[0])
    except Exception as exc:
        logger.debug("Egress door check for %r unavailable, so it counts as "
                     "shut: %s", ONNX_DOWNLOAD_DOOR, exc)
        return False


class _DoorCheckedDefaultEF(DefaultEmbeddingFunction):
    """chroma's ``DefaultEmbeddingFunction``, asking the door on every call.

    chroma builds a fresh ONNX function inside each ``__call__``, and that one
    re-checks the model files and downloads them if they are missing. Checked
    only at construction, a function built while the files were present (or
    the door open) would fetch later behind a door that has since been shut,
    or after the cache was cleared. Same ``name()`` and config as the parent,
    so what a collection persists does not change.
    """

    def __call__(self, input: Documents) -> Embeddings:
        if not chroma_default_may_run():
            raise RuntimeError(
                _MISSING_ONNX_MESSAGE.format(folder=onnx_model_dir()))
        return super().__call__(input)


def embedding_status(cfg=None) -> dict:
    """What the configured embedding backend can actually do, without raising.

    This is the honest-status half of the fix: a backend that cannot be built
    used to surface as an empty RAG result with no error anywhere a person
    could see it. Returns ``{backend, model, available, message}``; ``message``
    is empty when available and says exactly what to install when it is not.
    """
    cfg = cfg or get_config()
    backend = (getattr(cfg.models, "embedding_backend", "")
               or DEFAULT_BACKEND).strip().lower()
    if backend not in (BACKEND_CHROMA_DEFAULT, BACKEND_OLLAMA,
                       BACKEND_SENTENCE_TRANSFORMERS):
        return {
            "backend": backend,
            "model": "",
            "available": False,
            "message": (
                f"models.embedding_backend is set to {backend!r}, which is not "
                f"a backend this build has. Use one of: "
                f"{BACKEND_CHROMA_DEFAULT}, {BACKEND_OLLAMA}, "
                f"{BACKEND_SENTENCE_TRANSFORMERS}."),
        }

    if backend == BACKEND_OLLAMA:
        model = (getattr(cfg.models, "ollama_embedding_model", "")
                 or DEFAULT_OLLAMA_EMBEDDING_MODEL)
        installed: list | None = None
        try:
            from puffpai import brain_health
            installed = brain_health.list_models(cfg.ollama.url)
        except Exception as exc:  # pragma: no cover - probe failure only
            logger.debug("Ollama model list unavailable: %s", exc)
        if installed is None:
            return {"backend": backend, "model": model, "available": False,
                    "message": (f"Nothing answered at {cfg.ollama.url}, so the "
                                f"embedding model {model} cannot be reached. "
                                f"Start Ollama, or set "
                                f"models.embedding_backend to "
                                f"{BACKEND_CHROMA_DEFAULT!r}.")}
        try:
            from puffpai import brain_health
            present = brain_health.is_installed(model, installed)
        except Exception:  # pragma: no cover
            present = model in installed
        if not present:
            return {"backend": backend, "model": model, "available": False,
                    "message": (f"The embedding model {model} is not in Ollama "
                                f"on this box. Install it with: "
                                f"ollama pull {model}")}
        return {"backend": backend, "model": model, "available": True,
                "message": ""}

    if backend == BACKEND_SENTENCE_TRANSFORMERS:
        model = (getattr(cfg.models, "embedding_model", "")
                 or LEGACY_EMBEDDING_MODEL)
        if importlib.util.find_spec("sentence_transformers") is None:
            return {"backend": backend, "model": model, "available": False,
                    "message": _MISSING_ST_MESSAGE}
        return {"backend": backend, "model": model, "available": True,
                "message": ""}

    model = LEGACY_EMBEDDING_MODEL
    if importlib.util.find_spec("onnxruntime") is None:
        return {"backend": backend, "model": model, "available": False,
                "message": ("onnxruntime is missing, which should be "
                            "impossible on a working install — it is a "
                            "dependency of chromadb. Reinstall with: "
                            "pip install -r requirements.txt")}
    if not onnx_model_present():
        return {"backend": backend, "model": model, "available": False,
                "message": _MISSING_ONNX_MESSAGE.format(
                    folder=onnx_model_dir())}
    return {"backend": backend, "model": model, "available": True,
            "message": ""}


def get_embedding_function(cfg=None):
    """Return the ChromaDB embedding function selected by config.

    Reads ``models.embedding_backend``; defaults to ``chroma_default``, the
    one backend a base install can build with no extra package and no model
    pulled. Raises with a message a person can act on rather than returning
    something that silently embeds nothing.
    """
    cfg = cfg or get_config()
    backend = (getattr(cfg.models, "embedding_backend", "")
               or DEFAULT_BACKEND).strip().lower()

    if backend == BACKEND_CHROMA_DEFAULT:
        logger.info("Embeddings: chromadb's ONNX %s", LEGACY_EMBEDDING_MODEL)
        return _chroma_default_ef()

    if backend == BACKEND_OLLAMA:
        model = (getattr(cfg.models, "ollama_embedding_model", "")
                 or DEFAULT_OLLAMA_EMBEDDING_MODEL)
        logger.info("Embeddings: Ollama /api/embed (%s) at %s",
                    model, cfg.ollama.url)
        return OllamaEmbeddingFunction(model_name=model, url=cfg.ollama.url)

    if backend != BACKEND_SENTENCE_TRANSFORMERS:
        logger.warning(
            "Unknown models.embedding_backend=%r — falling back to %r",
            backend, DEFAULT_BACKEND)
        return _chroma_default_ef()

    model = getattr(cfg.models, "embedding_model", "") or LEGACY_EMBEDDING_MODEL
    return _sentence_transformer_ef(model)


_shared_ef = None
_shared_ef_lock = threading.Lock()


def get_shared_embedding_function():
    """Process-wide cached embedding function.

    The sentence-transformers backend loads a model into RAM, so every caller
    must share one instance — on the Orin that matters, and it is also the
    only way the read side and the write side are guaranteed to be the same
    object rather than two equal-looking ones.
    """
    global _shared_ef
    if _shared_ef is None:
        with _shared_ef_lock:
            if _shared_ef is None:
                _shared_ef = get_embedding_function()
    return _shared_ef


def embed_documents(texts, embedding_function=None):
    """Embed `texts` with the configured function, returning raw vectors.

    Call sites that write to a collection should pass the result as
    ``embeddings=`` alongside ``documents=``. See `open_collection` for why
    leaving the choice of model up to the collection is not safe.
    """
    texts = list(texts)
    if not texts:
        return []
    ef = embedding_function or get_shared_embedding_function()
    return ef(texts)


def open_collection(client, name, embedding_function=None):
    """Return an existing collection with our embedding function attached
    *where chromadb allows it*.

    Two verified chromadb 1.5.8 behaviours make this necessary, and neither
    is documented:

    1. ``client.get_collection(name)`` DOES rehydrate and apply the persisted
       embedding function — but reports it wrongly. The returned handle's
       ``_embedding_function`` attribute reads ``DefaultEmbeddingFunction``
       even when ``documents=``/``query_texts=`` are in fact routed through
       the persisted one (verified by instrumenting a stub ``/api/embed``).
       So the attribute cannot be used to reason about, or assert on, which
       model a collection actually uses.

    2. What a collection persisted is therefore the thing that matters — and
       collections created before EF pinning persist an empty config (``{}``), which
       chroma treats as "default". They embed with chroma's ONNX
       all-MiniLM-L6-v2, and an explicit ``embedding_function=`` cannot be
       attached to them: it raises ``ValueError: ... Embedding function
       conflict``. Re-pinning them is only possible by re-creating them,
       which is exactly what the full re-index does.

    So this is best-effort: it attaches where it can and returns a plain
    handle where it cannot. **Callers that write or query must not depend on
    the handle's embedding function** — pass explicit ``embeddings=`` /
    ``query_embeddings=`` built by `embed_documents`. This helper exists to
    make the *new* collections correct and to keep the failure legible.
    """
    ef = embedding_function or get_shared_embedding_function()
    try:
        return client.get_collection(name=name, embedding_function=ef)
    except ValueError as exc:
        if "embedding function" not in str(exc).lower():
            raise
        logger.debug(
            "Collection %r has a default/absent persisted embedding function; "
            "using a plain handle. Writes MUST pass explicit embeddings=.",
            name)
        return client.get_collection(name=name)


def get_or_create_collection(client, name, embedding_function=None,
                             metadata=None):
    """`client.get_or_create_collection` that survives chroma's EF conflict.

    Pins a NEW collection to our embedding function. For one that already
    exists with a default/absent persisted function, chroma raises rather than
    re-attaching (see `open_collection`), so we fall back to the plain handle
    — same best-effort contract: writes still pass explicit ``embeddings=``.
    """
    ef = embedding_function or get_shared_embedding_function()
    kwargs: dict[str, Any] = {"embedding_function": ef}
    if metadata:
        kwargs["metadata"] = metadata
    try:
        return client.get_or_create_collection(name=name, **kwargs)
    except ValueError as exc:
        if "embedding function" not in str(exc).lower():
            raise
        logger.debug(
            "Collection %r rejected an explicit embedding function; "
            "falling back to a plain handle.", name)
        return client.get_or_create_collection(name=name)
