"""
PuffPAI Centralized Configuration
==================================
Single source of truth for all config loading.

    from puffpai.config import get_config

    cfg = get_config()
    cfg.ollama.url        # "http://localhost:11434"
    cfg.REPO_ROOT         # absolute path to the repo root
    cfg.ha.enabled        # True/False

The config is loaded once (singleton) and shared across the whole process.
Missing config files are handled gracefully with sensible defaults.
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ── Ollama endpoint ────────────────────────────────────────────────────────
# 11434 is the port the official Ollama installer binds (and therefore what a
# fresh Jetson/Orin appliance listens on). The laptop dev box has historically
# run Ollama on 11435; it keeps working because config/puffpai_config.json
# pins "ollama.url" there. Resolution order, highest first:
#
#   1. $PUFFPAI_OLLAMA_URL          (env override — use this on dev boxes)
#   2. ollama.url in puffpai_config.json
#   3. DEFAULT_OLLAMA_URL           (11434)
#
# Anything needing the endpoint should read cfg.ollama.url (or import
# DEFAULT_OLLAMA_URL for a literal default) rather than hardcoding a port.
DEFAULT_OLLAMA_URL = "http://localhost:11434"
OLLAMA_URL_ENV = "PUFFPAI_OLLAMA_URL"
#: Env override for tools.searxng_timeout (dev/test boxes only; see
#: ToolsConfig.searxng_timeout docstring). Unlike SEARXNG host, the timeout
#: is not a security-relevant value, so an env override is safe here.
SEARXNG_TIMEOUT_ENV = "PUFFPAI_SEARXNG_TIMEOUT"

#: Env var naming the directory holding the config files the app READS AND the
#: setup wizard WRITES (``puffpai_config.json``, ``home_assistant_config.json``,
#: ``build_manifest.json``). Unset — the normal case — it is ``<repo>/config``
#: exactly as before. An installed build that keeps the code tree read-only, and
#: every wizard run that must not touch a neighbouring install, sets this.
CONFIG_DIR_ENV = "PUFFPAI_CONFIG_DIR"


def _find_repo_root() -> str:
    """Walk up from this file (src/puffpai/config.py) to the repo root."""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@dataclass
class OllamaConfig:
    url: str = DEFAULT_OLLAMA_URL
    # qwen2.5:7b-instruct fits an 8 GB GPU at 8k ctx (about 6 GiB total,
    # 2 GiB headroom, ~49 tok/s); a 9B model does NOT fit at 8k ctx there
    # (6.6 GB weights + ~0.7 KV + desktop baseline > 8 GiB).
    model: str = "qwen2.5:7b-instruct"
    temperature: float = 0.6
    num_ctx: int = 8192
    num_predict: int = 512
    # Context for the SCHEMA-CONSTRAINED planner/classifier model (a different
    # model tag from the prose one above — qwen3 on the appliance). It gets its
    # own key because it is its own runner: ollama keys a loaded runner to its
    # context size, and the 4096 here is a MEASURED choice, not a leftover —
    # see puffpai.agents.engine (equal classify/plan sizes avoid a double cold
    # start) and inference/model_options.planner_num_ctx. It is
    # config-governed rather than a literal repeated in several modules.
    planner_num_ctx: int = 4096
    # How long Ollama keeps this model loaded after a request. A per-request
    # value overrides the server's OLLAMA_KEEP_ALIVE. -1 (number) = resident
    # forever — what the appliance wants; "30m" is the conservative default
    # for dev machines that share RAM with everything else.
    keep_alive: int | str = "30m"
    # ── Vision (image-understanding) escalation — see puffpai.vision ─────────
    # Local Ollama vision model used to read messy/handwritten/angled photos
    # that plain tesseract OCR garbles. vision_mode:
    #   "off"     never call the VLM (tesseract-only, legacy behaviour)
    #   "auto"    tesseract first, escalate to the VLM only on weak OCR text
    #   "primary" VLM first for images, tesseract as the fallback
    vision_model: str = "qwen2.5vl:7b"
    vision_mode: str = "auto"
    # ── Coding model (on-demand, never pinned) — see puffpai.inference.code_model
    #: Ollama tag of the local coding model; empty string = feature OFF.
    code_model: str = ""
    #: How long Ollama keeps the coding model loaded after a coding request.
    code_keep_alive: str = "10m"
    #: Context window for a coding request (its own runner, its own size).
    code_num_ctx: int = 16384
    #: Token budget for one generated code block plus its short explanation.
    code_num_predict: int = 2048
    #: Sampling temperature for code — low, because code is not prose.
    code_temperature: float = 0.2


@dataclass
class AutomationWebhooks:
    """Webhook paths on the automation engine (Node-RED).

    Every flow exposes its HTTP entry point as /webhook/<name>.
    """
    block_device: str = "/webhook/block-device"
    network_scan: str = "/webhook/network-scan"
    weather_alerts: str = "/webhook/weather-alerts"
    puffpai_voice: str = "/webhook/puffpai_voice"
    daily_news: str = "/webhook/daily-news"
    calendar_sync: str = "/webhook/calendar-sync"
    meal_planner: str = "/webhook/meal-planner"
    grocery_list: str = "/webhook/grocery-list"
    daily_briefing: str = "/webhook/daily-briefing"
    daily_weather: str = "/webhook/daily-weather"
    media_now_playing: str = "/webhook/media-now-playing"
    health_check: str = "/webhook/health-check"
    good_morning: str = "/webhook/good-morning"
    leaving_home: str = "/webhook/leaving-home"
    security_check: str = "/webhook/security-check"


@dataclass
class NodeRedConfig:
    """Node-RED automation engine (Apache-2.0)."""
    url: str = "http://localhost:1880"
    timeout: int = 10
    retries: int = 2
    # Path used for the reachability probe. Node-RED serves its editor on "/".
    health_path: str = "/"
    webhooks: AutomationWebhooks = field(default_factory=AutomationWebhooks)


@dataclass
class ChromaDBConfig:
    path: str = "./kiwix_rag_db"
    # The fiction-framing heuristic (puffpai.fiction_context)
    # mis-tags real-world subjects as fictional (e.g. "black panther" ->
    # Marvel, "star wars program" SDI -> Star Wars universe, "gotham new
    # york nickname" -> DC, "vulcan planet volcano" -> Star Trek). Disabled
    # by default until the heuristic is fixed; set true in the "chromadb"
    # section of puffpai_config.json to restore the old behavior.
    fiction_framing: bool = False


@dataclass
class ApiServerConfig:
    #: LOOPBACK by default. This build ships no host packet filter, so the bind
    #: address IS the boundary and the API key is the only control in front of
    #: the port. Nothing in the default install needs more than loopback (the
    #: dashboard proxies server-side; the kiosk and voice service are same-host).
    #: Widen it deliberately — preferably to a specific interface, not 0.0.0.0 —
    #: when Node-RED runs in Docker or Home Assistant lives on another box. See
    #: the bind comment in ``start_api.py`` for the per-case flag.
    host: str = "127.0.0.1"
    port: int = 8000
    #: Number of reverse-proxy hops to trust in X-Forwarded-For. 0 (default)
    #: means the header is never read and request.remote_addr stays the socket
    #: peer. Set this ONLY when a proxy you control fronts the API; with clients
    #: connecting directly (the appliance's normal deployment) a non-zero value
    #: lets any client forge its own IP. No env override on purpose: enabling
    #: proxy trust must be a deliberate edit to puffpai_config.json.
    trusted_proxy_hops: int = 0


@dataclass
class NabuConfig:
    enabled: bool = True
    ha_voice_pipeline: str = "puffpai_voice"
    wake_word: str = "hey puffpai"
    voice_max_length: int = 500


@dataclass
class IdentityConfig:
    name: str = "PuffPAI"
    avatar_style: str = "thumbs"
    avatar_seed: str = "PuffPAI"


@dataclass
class HAConfig:
    """Home Assistant connection config."""
    # A static IPv4 (DHCP-reserved on the router) avoids the
    # mDNS/IPv6-link-local flakiness of homeassistant.local from WSL2/containers.
    url: str = "http://homeassistant.local:8123"
    # repr=False so the token cannot ride along in a config dump. With INFO
    # logging live on the appliance, one `logger.info("cfg=%s", cfg)` would
    # otherwise put the long-lived HA token in journald and in the rotating
    # log file. Same treatment on every secret field below.
    token: str = field(
        default="YOUR_LONG_LIVED_ACCESS_TOKEN_HERE", repr=False)
    enabled: bool = False
    #: HA script the admin defines to run ``zha.permit`` (admin-only) on behalf
    #: of this non-admin user; "" disables the fallback and pairing stays refused.
    zigbee_permit_script: str = "script.puffpai_zigbee_permit"
    #: The admin-defined counterpart that runs ``zha.permit duration: 0``; "" disables the early close.
    zigbee_permit_close_script: str = "script.puffpai_zigbee_permit_close"
    #: How long the open script's fixed window lasts, for the pairing watcher's bounded watch.
    zigbee_permit_script_seconds: int = 120


@dataclass
class HaEventsConfig:
    """HA WebSocket event listener — the agent team's senses (Phase 1).

    Subscribes to ``state_changed`` and forwards only the watched entities to
    :func:`puffpai.agent_events.submit_agent_event`. Read-only: the listener
    never calls an HA service, so the worst case of a misconfigured pattern is
    noise in the event log, not an action on the house.
    """
    enabled: bool = True
    #: fnmatch glob patterns; an entity must match one of these to be forwarded.
    #: Empty by default: the owner lists their own entities (for example
    #: ``"binary_sensor.front_door_person"`` or ``"binary_sensor.*_motion"``),
    #: and with none the listener forwards nothing.
    watch_entities: list[str] = field(default_factory=list)
    #: per-entity cool-down; a camera re-triggering every second must not spam.
    debounce_seconds: int = 30


@dataclass
class DispatcherConfig:
    """Majordomo task dispatcher — scheduled work actually running (Phase 2).

    A daemon thread wakes every ``tick_seconds``, runs the tasks whose due time
    has arrived, and sends each through the shared bus so Sentinel's veto and
    the audit ledger apply to scheduled work exactly as to a typed message. A
    task that fails ``max_consecutive_failures`` times in a row is disabled.
    """
    enabled: bool = True
    #: how often the queue is checked; the scheduling granularity.
    tick_seconds: int = 30
    #: consecutive failures that disable a task so it cannot spin forever.
    max_consecutive_failures: int = 5


@dataclass
class ModelConfig:
    """Centralized model paths and inference parameters."""
    llm_gguf_path: str = ""
    vosk_model_dir: str = "models/vosk-model-small-en-us"
    piper_model_path: str = "models/en_US-lessac-medium.onnx"
    wake_word_model_path: str = "models/hey_puffpai.tflite"
    # ── Embeddings ────────────────────────────────────────────────────────
    # embedding_backend selects the ChromaDB embedding function built by
    # puffpai.embeddings.get_embedding_function():
    #   "chroma_default"        -> chromadb's own ONNX all-MiniLM-L6-v2 (the
    #                              default: base install, no extra package)
    #   "sentence_transformers" -> embedding_model below (all-MiniLM-L6-v2)
    #                              via PyTorch; needs the embeddings-legacy extra
    #   "ollama"                -> ollama_embedding_model via /api/embed
    #
    # The first two are the SAME model and the same vector space, so moving
    # between them is safe. "ollama" is a different space: flipping to it means
    # pulling the model, setting this flag, and re-indexing everything in one
    # pass — querying old collections with a new model does not error, it just
    # returns wrong neighbours.
    embedding_backend: str = "chroma_default"
    embedding_model: str = "all-MiniLM-L6-v2"
    ollama_embedding_model: str = "nomic-embed-text"
    llm_n_ctx: int = 2048
    llm_n_gpu_layers: int = 0
    # ── Speech-to-text (voice) ────────────────────────────────────────────
    # stt_engine selects the backend built by puffpai.voice.stt.create_stt():
    #   "whisper" -> faster-whisper (CTranslate2); far better on natural speech
    #   "vosk"    -> Vosk (kept for rollback / no-faster-whisper environments)
    # If "whisper" fails to load it falls back to Vosk automatically.
    stt_engine: str = "whisper"
    # faster-whisper model + compute. base.en int8 CPU measured on a Jetson AGX Orin at
    # ~1.3s/utterance and ~4% WER (small.en ~2% WER but ~3.6s — too slow for a
    # voice turn). GPU is unavailable here: this box's CTranslate2 wheel reports
    # cuda_device_count=0, so keep device="cpu". vosk_model_dir above is the
    # rollback engine; point it at vosk-model-en-us-0.22 (full) for best Vosk.
    whisper_model: str = "base.en"
    whisper_device: str = "cpu"
    whisper_compute: str = "int8"
    # ── Confidence gate ───────────────────────────────────────────────────
    # A transcript below these bars is treated as "didn't catch that": Puff says
    # "I didn't catch that — say it again?" and re-listens instead of routing to
    # the planner/RAG/LLM (which otherwise burns a full brain call on nothing and
    # can speak a wrong answer). Thresholds calibrated on the appliance (Piper →
    # 16k → faster-whisper base.en int8):
    #   real speech, incl. noisy/distant : avg_logprob -0.30 … -0.47, no_speech ≤ 0.08
    #   hallucinated fragments / garbage : avg_logprob -0.68 … -1.07, no_speech up to 0.34
    # -0.65 sits in that gap: it flags the garbage while passing every real
    # command (and valid partials like "set a timer") with margin. "Say it again?"
    # is a cheap, graceful miss, so we bias slightly toward re-asking. Loosen
    # toward -0.8 if real far-field commands get wrongly re-asked; tighten toward
    # -0.55 to catch more borderline noise. Set to a very low value (e.g. -5.0) to
    # effectively disable the logprob arm and gate on no_speech / empty only.
    stt_min_avg_logprob: float = -0.65     # whisper: flag if mean avg_logprob below
    stt_max_no_speech_prob: float = 0.5    # whisper: flag if max no_speech_prob above
    vosk_min_word_confidence: float = 0.5  # vosk: flag if mean word conf below


@dataclass
class PathConfig:
    """Centralized data and storage paths."""
    zim_dirs: list[str] = field(default_factory=list)
    zim_files: list[str] = field(default_factory=list)
    chromadb_path: str = "./kiwix_rag_db"
    # Directory holding the SQLite FTS5 shards built by
    # puffpai.knowledge.fts_build_cli (one .db per ZIM pack). Left empty it is
    # derived at runtime by knowledge.fts_index.resolve_fts_dir(): a sibling
    # "fts" directory next to the first zim_dir.
    fts_dir: str = ""
    # ── writable runtime state (see resolve_state_dir) ──────────────────
    # These three name directories the service WRITES to at runtime. On a
    # hardened systemd unit (ProtectSystem=strict) the code tree is read-only,
    # so an appliance must set them to ABSOLUTE paths under a ReadWritePaths
    # root (e.g. /data/puffpai/...). Relative values are dev convenience and
    # resolve against REPO_ROOT — i.e. inside the code tree.
    #: Writable state root; config counterpart of $PUFFPAI_DATA_DIR.
    state_dir: str = ""
    #: Directory holding agent_events.jsonl. Falls back to state_dir.
    agent_events_dir: str = ""
    #: Directory holding the Agent Team feed (agent-activity.jsonl) and
    #: task-queue.jsonl. Falls back to state_dir. Read by
    #: :mod:`puffpai.agent_activity`, which api_server and the consent loop
    #: share so the feed's reader and its writers never split across two files.
    agent_logs_dir: str = ""
    #: Directory holding audit.jsonl and its rotations.
    security_logs_dir: str = ""
    vault_data_dir: str = "vault_data"
    chat_history_dir: str = "chat_history"
    feedback_data_path: str = "feedback_data.jsonl"


@dataclass
class KnowledgeConfig:
    """Knowledge-pack auto-indexing settings and the owner's data folders.

    ``zim_dir`` is the folder PuffPAI watches for ``.zim`` knowledge packs; when
    ``auto_index`` is on, new packs found there are indexed in the background on
    startup. Empty ``zim_dir`` disables the watch (manual ``knowledge sync`` still
    works with an explicit path).

    ``data_dir`` is the owner's one data folder (``knowledge/my_data.py``):
    downloads and the owner's own files (``<data_dir>/My files``). It is THE
    setting; ``zim_dir`` is written with the same value for older readers, and
    is used when ``data_dir`` is empty. ``linked_folders`` are the owner's own
    folders, read in place (``{id, path, name, added_at}``); ``previous_dirs``
    are earlier data folders, still read; files over ``max_file_mb`` are
    skipped.
    """
    zim_dir: str = ""
    auto_index: bool = True
    data_dir: str = ""
    linked_folders: list = field(default_factory=list)
    previous_dirs: list = field(default_factory=list)
    max_file_mb: int = 25


@dataclass
class Zone3Config:
    """Zone 3 (Network Edge) — DNS sinkhole and device policy config.

    AdGuard Home is the sinkhole; the Pi-hole fields below are retained only
    for the legacy controller and are inactive. adguard_enforce=False keeps the
    controller in monitor-first mode (rules recorded and visible, nothing
    blocked).

    AdGuard runs on the appliance itself (docker-compose.zone3.yml +
    puffpai-zone3.service), so the default URL is loopback.

    ``enabled`` ships False. Zone 3 is the one component that can take a whole
    household off the internet, so it starts off and a person turns it on.
    """
    enabled: bool = False
    # AdGuard Home (active path) — on this appliance, reached over loopback.
    adguard_url: str = "http://127.0.0.1:3000"
    adguard_username: str = ""
    # load via secrets_provider, not this file; repr=False keeps it out of dumps
    adguard_password: str = field(default="", repr=False)
    adguard_enforce: bool = False   # monitor-first; flip after log review
    # Pi-hole (legacy, inactive)
    pihole_url: str = "http://localhost:8080"
    pihole_password: str = field(default="puffpai", repr=False)
    pihole_timeout: int = 10
    auto_discover: bool = True
    default_mode: str = "balanced"  # lockdown, balanced, open


@dataclass
class MediaServerConfig:
    """Media server (Jellyfin + Calibre-Web, see docker-compose.media.yml)."""
    enabled: bool = False
    jellyfin_url: str = "http://localhost:8096"
    jellyfin_api_key: str = field(default="", repr=False)
    calibre_db_path: str = "/mnt/media/books/metadata.db"
    calibre_web_url: str = "http://localhost:8083"
    #: Calibre-web's OWN database (users + shelves). Shelves are Calibre-web's,
    #: not Calibre's: a "to read" shelf made by voice must be the same shelf the
    #: household sees in the web reader, so we write this file directly.
    #: Empty = not configured: book shelves report "not connected".
    calibre_web_db_path: str = ""
    #: Offline popularity sidecar for recommendations, built from the Project
    #: Gutenberg catalogue: {"<gutenberg id>": {"rank": n, "lcc": ".."}}. Absent
    #: file or empty value → recommendations fall back to random-within-category.
    books_popularity_path: str = ""
    media_mount: str = "/mnt/media"
    pi_host: str = "media.local"
    pi_ssh_user: str = "puffpai"
    timeout: int = 10


@dataclass
class ToolsConfig:
    """Toolsmith generic-tool tier settings.

    ``searxng_url`` is the base URL of a SELF-HOSTED SearXNG instance the owner
    runs for ``toolsmith.web_search``. It defaults to a loopback address because
    the appliance runs SearXNG itself. Nothing here
    requires it to be running: ``web_search`` degrades gracefully (a structured
    "offline" reply) when SearXNG cannot be reached.

    This value is OWNER-configured and is never user- or model-controlled. It is
    the single fixed host ``web_search`` is permitted to reach through the
    Toolsmith's SearXNG pinhole, and that non-controllability is exactly what
    makes the pinhole safe — see :meth:`puffpai.agents.toolsmith.ToolsmithAgent._web_search`.
    No environment override on purpose: like ``trusted_proxy_hops``, changing the
    one host the appliance may reach for search should be a deliberate edit to
    puffpai_config.json, not a variable an operator can set from anywhere.
    """
    searxng_url: str = "http://127.0.0.1:8888"
    #: Seconds before an unreachable SearXNG is treated as offline. A real
    #: SearXNG relay can take ~10s to return a full result set, so a 5s limit
    #: cuts off searches that would have succeeded. Overridable via
    #: $PUFFPAI_SEARXNG_TIMEOUT (see SEARXNG_TIMEOUT_ENV) for dev/test boxes.
    searxng_timeout: float = 20.0


@dataclass
class AgentsConfig:
    """Agent Team settings."""

    #: Record one ``turn_handled`` activity event per (chat turn, agent) so the
    #: Agent Team page reflects who actually answered; set false to stop the
    #: chat path writing to the feed without a redeploy.
    activity_from_chat: bool = True


@dataclass
class SkillsConfig:
    """Sandboxed skill packages.

    ``enabled`` ships **False**: skills are the one subsystem that runs code
    the appliance did not ship, so they start off and a person turns them on
    after seeing them work on the box. The environment
    variable ``PUFFPAI_SKILLS_DISABLED=1`` overrides this to off and cannot be
    overridden back from configuration -- an operator's switch in the unit file
    must not be reversible by anything the running service can write.
    """
    enabled: bool = False
    #: Wall-clock ceiling for one skill run, clamped again by the manifest
    #: validator and by the runner. Three clamps, one value: a manifest that
    #: lies about its limit still cannot exceed this.
    run_timeout_s: int = 20


@dataclass
class MapsConfig:
    """Offline directions: the two map engines on this
    box and the map build's META.json. Both engines must be loopback: the home
    point is sent to them, and ``puffpai.maps_directions`` refuses any other
    host. A missing or null ``maps`` section means these defaults."""
    valhalla_url: str = "http://127.0.0.1:8002"
    photon_url: str = "http://127.0.0.1:2322"
    #: Empty means ``current/META.json`` under the maps root
    #: (``$PUFFPAI_MAPS_ROOT``, else ``maps`` under the data dir).
    meta_path: str = ""


@dataclass
class HomeConfig:
    """Where home is, roughly. Unset (``None``) means unset: no default place.

    Written by ``puffpai home set``; see :mod:`puffpai.home_location`, which
    validates and rounds on every read. Only a rounded position (one decimal,
    about 11 km), the town label and the IANA time zone are ever stored.
    """
    label: str = ""
    latitude: float | None = None
    longitude: float | None = None
    timezone: str = ""
    source: str = ""
    set_at: str = ""


@dataclass
class PuffPAIConfig:
    """Root configuration object. All fields have safe defaults."""
    REPO_ROOT: str = ""
    SRC_DIR: str = ""
    CONFIG_DIR: str = ""
    #: Writable state root: ``$PUFFPAI_DATA_DIR`` when set, else REPO_ROOT.
    #: Added for the setup wizard, which needs one honest answer to "where do
    #: this install's files go" before any section of config exists. Existing
    #: readers are unaffected; nothing else reads it.
    DATA_DIR: str = ""

    ollama: OllamaConfig = field(default_factory=OllamaConfig)
    nodered: NodeRedConfig = field(default_factory=NodeRedConfig)
    chromadb: ChromaDBConfig = field(default_factory=ChromaDBConfig)
    api_server: ApiServerConfig = field(default_factory=ApiServerConfig)
    nabu: NabuConfig = field(default_factory=NabuConfig)
    identity: IdentityConfig = field(default_factory=IdentityConfig)
    ha: HAConfig = field(default_factory=HAConfig)
    ha_events: HaEventsConfig = field(default_factory=HaEventsConfig)
    dispatcher: DispatcherConfig = field(default_factory=DispatcherConfig)
    models: ModelConfig = field(default_factory=ModelConfig)
    paths: PathConfig = field(default_factory=PathConfig)
    zone3: Zone3Config = field(default_factory=Zone3Config)
    maps: MapsConfig = field(default_factory=MapsConfig)
    media_server: MediaServerConfig = field(default_factory=MediaServerConfig)
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    agents: AgentsConfig = field(default_factory=AgentsConfig)
    skills: SkillsConfig = field(default_factory=SkillsConfig)
    knowledge: KnowledgeConfig = field(default_factory=KnowledgeConfig)
    web_search_consent: bool = False
    #: The household's town, for the words Puff says ("in Denver"). Empty
    #: means "not set": the code has no default town, and every consumer says
    #: so plainly instead of assuming one.
    location: str = ""
    #: Where the house is, for the weather card and severe-weather alerts.
    #: The owner's home point (``puffpai.home_point``, set in Settings and kept
    #: in the data dir) is layered over the config file at load; the tracked
    #: config ships none. When neither is set ``household_geo`` may geocode
    #: ``location`` through the ``geocode`` egress switch (if the owner opened
    #: it) and otherwise reports the position as unknown, never a default.
    latitude: float | None = None
    longitude: float | None = None
    home: HomeConfig = field(default_factory=HomeConfig)
    _raw: dict[str, Any] = field(default_factory=dict, repr=False)


#: Settings files a fresh install does not have yet (Home Assistant and the
#: media server are optional). Their absence is not a problem to report.
_OPTIONAL_CONFIG_FILES = frozenset({"home_assistant_config.json",
                                    "media_server_config.json"})


def _load_json(path: str) -> dict[str, Any]:
    """Load a JSON file, returning {} on any failure.

    An optional file (:data:`_OPTIONAL_CONFIG_FILES`) that is absent is
    quiet: debug, not a warning. A file that is there but cannot be read is
    still a warning.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError as exc:
        if os.path.basename(path) in _OPTIONAL_CONFIG_FILES:
            logger.debug("No %s yet: %s", os.path.basename(path), exc)
        else:
            logger.warning("Could not load %s: %s", path, exc)
        return {}
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not load %s: %s", path, exc)
        return {}


def _coordinate(raw: dict[str, Any], key: str, limit: float) -> float | None:
    """``raw[key]`` as a float within ``[-limit, limit]``, else ``None``.

    Anything unusable (missing, ``null``, a non-number, out of range) is
    logged and treated as unset, so a typo in the config file turns into an
    honest "coordinates not set" rather than a crash or a wrong place.
    """
    value = raw.get(key)
    if value is None or value == "":
        return None
    # The value is never logged: a coordinate in a log is the house's
    # position in a log, whether or not it parsed.
    try:
        if isinstance(value, bool):
            raise TypeError("bool is not a coordinate")
        f = float(value)
    except (TypeError, ValueError):
        logger.warning("config %s is not a number; treating as unset", key)
        return None
    if f != f or f < -limit or f > limit:
        logger.warning("config %s is out of range; treating as unset", key)
        return None
    return f


def config_file_coordinates(raw: dict[str, Any]) -> tuple[float, float] | None:
    """``(lat, lon)`` from the JSON config's own keys, or ``None``.

    Half a pair, or exactly 0/0, is unset (logged without the values).
    """
    latitude = _coordinate(raw, "latitude", 90.0)
    longitude = _coordinate(raw, "longitude", 180.0)
    if (latitude is None) != (longitude is None):
        logger.warning("config sets only one of latitude/longitude; treating "
                       "the house's coordinates as unset")
        return None
    if latitude is None or longitude is None:
        return None
    if latitude == 0.0 and longitude == 0.0:
        # The placeholder people type for "none"; nobody lives at Null Island.
        logger.warning("config latitude/longitude are both 0; treating the "
                       "house's coordinates as unset")
        return None
    return latitude, longitude


def _home_coordinates(raw: dict[str, Any]) -> tuple[float | None, float | None]:
    """The house's coordinates: the owner's home point, else the config file.

    The owner overlay (``puffpai.home_point``) wins when it holds a usable
    point. Any failure reading it degrades to the config file, never to a
    crash: config loading must not break boot.
    """
    try:
        from puffpai import home_point as _home_point
        owner = _home_point.read()
    except Exception as exc:  # pragma: no cover - defensive, boot must survive
        logger.warning("home point overlay unavailable (%s); using the "
                       "config file", type(exc).__name__)
        owner = None
    if owner is not None:
        return owner["latitude"], owner["longitude"]
    pair = config_file_coordinates(raw)
    return pair if pair is not None else (None, None)


def _safe_init(cls: Any, data: dict[str, Any]) -> Any:
    """Create a dataclass instance, ignoring unknown keys."""
    valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
    return cls(**{k: v for k, v in data.items() if k in valid_keys})


#: ``paths.*`` keys naming a directory or file the service WRITES at runtime.
#: Every one of them ships RELATIVE, which resolves inside the code tree — and
#: the appliance unit mounts that tree read-only (ProtectSystem=strict with
#: ReadWritePaths=/data/puffpai), so a relative value is an EROFS at runtime
#: that looks fine in a dev checkout. When $PUFFPAI_DATA_DIR names the writable
#: root, :func:`anchor_relative_state_paths` re-roots these there.
#:
#: NOT listed, on purpose: ``zim_dirs``/``zim_files`` (read-only knowledge
#: sources an operator points wherever the library lives) and ``state_dir``
#: itself (empty means "derive from $PUFFPAI_DATA_DIR", and filling it in would
#: defeat the ``env_subdir`` argument callers pass to
#: :func:`resolve_state_dir`).
RELOCATABLE_PATH_KEYS: tuple[str, ...] = (
    "chromadb_path",      # the RAG/knowledge index
    "fts_dir",            # SQLite FTS5 shards
    "agent_events_dir",   # agent_events.jsonl
    "agent_logs_dir",     # agent-activity.jsonl + task-queue.jsonl
    "security_logs_dir",  # audit.jsonl and its rotations
    "vault_data_dir",     # Zone 1
    "chat_history_dir",
    "feedback_data_path",
)


def _anchor(value: str, root: str) -> str:
    """Join *value* under *root* unless it is empty or already absolute."""
    text = os.path.expanduser(str(value or "").strip())
    if not text or os.path.isabs(text):
        return text
    return os.path.normpath(os.path.join(root, text))


def anchor_relative_state_paths(
    path_cfg: "PathConfig",
    chroma_cfg: "ChromaDBConfig",
    *,
    repo_root: str,
    env_var: str = "PUFFPAI_DATA_DIR",
) -> None:
    """Re-root every RELATIVE writable-state path under ``$PUFFPAI_DATA_DIR``.

    In place, and only when the env var is set — i.e. only on a deployment that
    has declared a writable state root. Dev mode (env unset) is untouched, so a
    checkout still keeps its state beside the code.

    ABSOLUTE configured paths are never rewritten: an operator who wrote
    ``/mnt/big/chroma`` meant it. Empty values are left empty, because empty
    means "the module derives this at runtime".

    This is what makes the SHIPPED config correct on the appliance instead of
    merely documented-as-wrong: install_orin.sh no longer has to talk the owner
    through editing eight keys by hand before the Vault will write.
    """
    root = os.environ.get(env_var, "").strip()
    if not root:
        return
    root = os.path.expanduser(root)
    if not os.path.isabs(root):
        root = os.path.join(repo_root, root)
    root = os.path.normpath(root)

    for key in RELOCATABLE_PATH_KEYS:
        setattr(path_cfg, key, _anchor(getattr(path_cfg, key, ""), root))
    # The legacy "chromadb" section is a second name for the same directory;
    # leaving it relative would send whichever reader still uses it to a
    # read-only path.
    chroma_cfg.path = _anchor(chroma_cfg.path, root)


#: Shipped config files a relocated config dir is seeded with. Templates, not
#: state: ``puffpai_config.json`` and the model profiles are what the repo
#: carries, and a fresh install needs a writable copy of them to edit.
_SEED_GLOB = "*.json"


def _seed_config_dir(target: str, repo_root: str) -> None:
    """Copy the shipped ``config/*.json`` into a fresh relocated config dir.

    Runs only when *target* does not exist yet, and copies only files that are
    not already there — the owner's edits are never overwritten. Best-effort:
    an unwritable destination leaves the install reading defaults, which is the
    same state a checkout with no config is in, so it must not be fatal.
    """
    import glob
    import shutil
    source = os.path.join(repo_root, "config")
    if os.path.isdir(target) or not os.path.isdir(source):
        return
    try:
        os.makedirs(target, exist_ok=True)
        for path in glob.glob(os.path.join(source, _SEED_GLOB)):
            dest = os.path.join(target, os.path.basename(path))
            if not os.path.exists(dest):
                shutil.copy2(path, dest)
        logger.info("seeded %s from the shipped config in %s", target, source)
    except OSError as exc:
        logger.warning("could not seed config directory %s: %s", target, exc)


def _config_dir_for(repo_root: str) -> str:
    """The config directory. ``$PUFFPAI_CONFIG_DIR``, else data-dir, else repo.

    Precedence, highest first:

    1. ``$PUFFPAI_CONFIG_DIR`` (:data:`CONFIG_DIR_ENV`) — honoured verbatim; a
       relative value resolves against *repo_root*.
    2. ``<data dir>/config`` for a NON-DEV install, seeded once from the shipped
       ``config/*.json``. "Non-dev" is decided by exactly the rule
       :func:`resolve_state_dir` already uses — ``$PUFFPAI_DATA_DIR`` is set, or
       ``<repo>/data`` holds no runtime state — so config and state cannot
       disagree about which install this is.
    3. ``<repo>/config`` — the dev checkout that already has state in-tree.

    WHY 2 EXISTS. ``CONFIG_DIR`` is not read-only: ``setup_ops.set_ha`` writes
    the Home Assistant long-lived token into ``home_assistant_config.json``
    there (via ``ha_token_source.write_ha_config``). With the old default that
    put a live household credential inside the git checkout, where it showed up
    as an untracked file one ``git add -A`` away from being published — and on a
    packaged install it would be a write into the program directory. A token is
    state; it belongs with the state.
    """
    override = os.environ.get(CONFIG_DIR_ENV, "").strip()
    if override:
        override = os.path.expanduser(override)
        if not os.path.isabs(override):
            override = os.path.join(repo_root, override)
        return os.path.normpath(override)

    in_tree = os.path.join(repo_root, "config")
    if os.environ.get(STATE_DIR_ENV, "").strip() or not _dev_checkout_has_state(
            repo_root):
        relocated = os.path.normpath(
            os.path.join(_state_root(), "config"))
        if os.path.normpath(relocated) != os.path.normpath(in_tree):
            _seed_config_dir(relocated, repo_root)
            return relocated
    return in_tree


def resolve_config_dir(repo_root: str | None = None) -> str:
    """The config directory, resolved FRESH — what every config writer must use.

    :data:`PuffPAIConfig.CONFIG_DIR` is the same answer, but it is frozen into
    the singleton at first load, so a module that resolves its own path through
    it inherits whatever the environment said then. Anything that WRITES a
    config file (the agent display names, the hatch's identity, the profile
    manager's identity) calls this instead: reader and writer then name the same
    file, and a relocated install never writes into the code tree — which on a
    git checkout means a tracked, shipped file going dirty the first time an
    owner renames an agent, and on a packaged install means a write into the
    program directory.

    Resolution, including the seed-from-shipped step, is :func:`_config_dir_for`.
    """
    return _config_dir_for(repo_root or _find_repo_root())


def _build_config(repo_root: str | None = None) -> PuffPAIConfig:
    """Build a PuffPAIConfig from disk."""
    if repo_root is None:
        repo_root = _find_repo_root()

    src_dir = os.path.join(repo_root, "src", "puffpai")
    config_dir = _config_dir_for(repo_root)

    raw = _load_json(os.path.join(config_dir, "puffpai_config.json"))

    ollama_d = raw.get("ollama", {})
    # Automation engine (Node-RED): the "nodered" section.
    nodered_d = dict(raw.get("nodered") or {})
    webhooks_d = nodered_d.pop("webhooks", None) or {}
    # Accept "base_url" as an alias for "url" (the rest of the config file
    # uses "url", so that stays canonical).
    if "base_url" in nodered_d:
        nodered_d.setdefault("url", nodered_d.pop("base_url"))
    else:
        nodered_d.pop("base_url", None)
    chroma_d = raw.get("chromadb", {})
    api_d = raw.get("api_server", {})
    nabu_d = raw.get("nabu", {})
    identity_d = raw.get("identity", {})

    ha_d = _load_json(os.path.join(config_dir, "home_assistant_config.json"))
    # The token does NOT come from this file. It is resolved
    # in _overlay_secrets() by security/ha_token_source.py, which reads the
    # vault's boot-secret tier first and only falls back to this file while the
    # owner has left PUFFPAI_ALLOW_PLAINTEXT_HA_TOKEN_FALLBACK on. Dropping the
    # key here means a scrubbed config can never put the MIGRATED_TO_VAULT
    # marker into cfg.ha.token, and the plaintext value has exactly one reader
    # in the whole tree.
    ha_d.pop("token", None)
    # The event listener is appliance policy, not an HA credential, so it lives
    # in puffpai_config.json alongside the other feature sections.
    ha_events_d = raw.get("ha_events", {})
    dispatcher_d = raw.get("dispatcher", {})

    models_d = raw.get("models", {})
    paths_d = raw.get("paths", {})

    zone3_d = raw.get("zone3", {})
    media_d = _load_json(os.path.join(config_dir, "media_server_config.json"))
    tools_d = raw.get("tools", {})
    web_search_consent = bool(raw.get("web_search_consent", False))
    # No default town: an unset location is "not set", said plainly by each
    # consumer, never a city the code assumed.
    location = raw.get("location") or ""
    if not isinstance(location, str):
        location = ""
    latitude, longitude = _home_coordinates(raw)

    # Ollama endpoint: env var wins over the config file so a dev box can
    # point at its own port (11435 here) without editing tracked config.
    ollama_cfg = _safe_init(OllamaConfig, ollama_d)
    env_ollama_url = os.environ.get(OLLAMA_URL_ENV, "").strip()
    if env_ollama_url:
        ollama_cfg.url = env_ollama_url

    tools_cfg = _safe_init(ToolsConfig, tools_d)
    env_searxng_timeout = os.environ.get(SEARXNG_TIMEOUT_ENV, "").strip()
    if env_searxng_timeout:
        try:
            tools_cfg.searxng_timeout = float(env_searxng_timeout)
        except ValueError:
            logger.warning(
                "%s=%r is not a valid float; ignoring", SEARXNG_TIMEOUT_ENV, env_searxng_timeout
            )

    # Build PathConfig, syncing chromadb_path from legacy section if not in paths
    path_cfg = _safe_init(PathConfig, paths_d)
    if not paths_d.get("chromadb_path") and chroma_d.get("path"):
        path_cfg = PathConfig(
            **{k: v for k, v in paths_d.items() if k in PathConfig.__dataclass_fields__},
            chromadb_path=chroma_d["path"],
        )

    chroma_cfg = _safe_init(ChromaDBConfig, chroma_d)
    anchor_relative_state_paths(path_cfg, chroma_cfg, repo_root=repo_root)

    return PuffPAIConfig(
        REPO_ROOT=repo_root,
        SRC_DIR=src_dir,
        CONFIG_DIR=config_dir,
        DATA_DIR=resolve_state_dir("", repo_root=repo_root, default=repo_root),
        ollama=ollama_cfg,
        nodered=NodeRedConfig(
            **{k: v for k, v in nodered_d.items()
               if k in NodeRedConfig.__dataclass_fields__ and k != "webhooks"},
            webhooks=_safe_init(AutomationWebhooks, webhooks_d),
        ),
        chromadb=chroma_cfg,
        api_server=_safe_init(ApiServerConfig, api_d),
        nabu=_safe_init(NabuConfig, nabu_d),
        identity=_safe_init(IdentityConfig, identity_d),
        ha=_safe_init(HAConfig, ha_d),
        ha_events=_safe_init(HaEventsConfig, ha_events_d),
        dispatcher=_safe_init(DispatcherConfig, dispatcher_d),
        models=_safe_init(ModelConfig, models_d),
        paths=path_cfg,
        zone3=_safe_init(Zone3Config, zone3_d),
        maps=_safe_init(
            MapsConfig,
            raw["maps"] if isinstance(raw.get("maps"), dict) else {}),
        media_server=_safe_init(MediaServerConfig, media_d),
        tools=tools_cfg,
        agents=_safe_init(
            AgentsConfig,
            raw["agents"] if isinstance(raw.get("agents"), dict) else {}),
        skills=_safe_init(
            SkillsConfig,
            raw["skills"] if isinstance(raw.get("skills"), dict) else {}),
        knowledge=_safe_init(
            KnowledgeConfig,
            raw["knowledge"] if isinstance(raw.get("knowledge"), dict) else {}),
        web_search_consent=web_search_consent,
        location=location,
        latitude=latitude,
        longitude=longitude,
        home=_safe_init(HomeConfig, raw.get("home") if isinstance(
            raw.get("home"), dict) else {}),
        _raw=raw,
    )


def _enforce_lan_posture(cfg: PuffPAIConfig) -> PuffPAIConfig:
    """Neutralise any configured service URL the run posture forbids.

    The ``island`` posture promises "your home network, nothing beyond it", and
    until now nothing checked that promise against the addresses in the config
    files themselves. An ``ollama.url`` pointing at a public host is
    unrestricted egress that no egress door sees, carrying every prompt in the
    household; ``ha.url`` carries a long-lived token in an Authorization header.

    This NEUTRALISES rather than raises: a bad string in a config file must not
    be enough to stop the appliance booting. A violating optional service is
    switched off; one without an enable flag is reset to its safe default. The
    name-form check is used, never the resolving one - config load must never
    block on a resolver. ``/system/status`` re-checks with DNS and reports both
    what it finds and what was neutralised here.

    Never raises: posture policy must not be able to gate startup.
    """
    try:
        from puffpai.security import lan_posture
        lan_posture.record_load_violations(
            lan_posture.enforce(
                cfg, lan_posture.posture_from_config_dir(cfg.CONFIG_DIR)))
    except Exception as exc:
        logger.debug("LAN posture check unavailable: %s", exc)
    return cfg


def _overlay_secrets(cfg: PuffPAIConfig) -> PuffPAIConfig:
    """Resolve service secrets through the SecretsProvider chain
    (vault → OS keyring → env), overriding any plaintext values loaded
    from config JSON files.

    Plaintext JSON values still work as the legacy fallback so this is
    non-breaking, but once a secret exists in the keyring/vault it wins,
    so the JSON/.env copies can be emptied after rotation.
    """
    try:
        from .security.secrets_provider import get_secret
    except Exception as exc:  # never let secrets wiring break boot
        logger.warning("secrets_provider unavailable, using file config: %s", exc)
        return cfg

    def _resolve(name: str, current: str) -> str:
        try:
            val = get_secret(name)
        except Exception as exc:
            logger.debug("secret %r lookup failed: %s", name, exc)
            return current
        return val if val else current

    # The config object does not carry the Home Assistant token at
    # all. Every consumer (ha_bridge, ha_events, zigbee_manager, the
    # api_server LAN probe) resolves it through security/ha_token_source.py,
    # which reads the vault's boot-secret tier first and falls back to the
    # legacy plaintext config only while the owner allows it.
    #
    # Resolving it HERE would also be a boot hazard: the token source builds a
    # Vault, Vault.default_vault_dir() calls get_config(), and get_config() is
    # mid-build at this point -- a re-entrant loop. Keeping the credential out
    # of the config singleton removes both the loop and the copy.
    cfg.ha.token = ""
    cfg.zone3.adguard_password = _resolve(
        "ADGUARD_PASSWORD", cfg.zone3.adguard_password)
    cfg.media_server.jellyfin_api_key = _resolve(
        "JELLYFIN_API_KEY", cfg.media_server.jellyfin_api_key)
    return cfg


_instance: PuffPAIConfig | None = None


def refresh_home_point() -> None:
    """Re-layer the owner's home point onto the running config singleton.

    Called by the API after the owner saves or clears the point, so the
    weather card and the alerts pick it up at once without a restart. Only
    ``latitude``/``longitude`` change; nothing else is re-read.
    """
    cfg = get_config()
    cfg.latitude, cfg.longitude = _home_coordinates(cfg._raw or {})


def get_config(*, _force_reload: bool = False) -> PuffPAIConfig:
    """Return the global PuffPAIConfig singleton.

    Pass _force_reload=True in tests to rebuild from disk.
    """
    global _instance
    if _instance is None or _force_reload:
        _instance = _enforce_lan_posture(_overlay_secrets(_build_config()))
    return _instance


# ---------------------------------------------------------------------------
# LLM backend factory
# ---------------------------------------------------------------------------

_backend_instance: Any = None


def get_llm_backend(*, _force_reload: bool = False) -> Any:
    """Return the best available LLMBackend based on config.

    Priority:
      1. LlamaCppBackend  — if ``models.llm_gguf_path`` is set and the file exists
      2. OllamaBackend    — if ``ollama.url`` is reachable
      3. Raise RuntimeError

    The result is cached as a singleton.  Pass ``_force_reload=True`` in
    tests to rebuild.
    """
    global _backend_instance
    if _backend_instance is not None and not _force_reload:
        return _backend_instance

    cfg = get_config()

    # Attempt 1: llama.cpp with a local GGUF model
    gguf = cfg.models.llm_gguf_path
    if gguf:
        resolved = gguf if os.path.isabs(gguf) else os.path.join(cfg.REPO_ROOT, gguf)
        if os.path.isfile(resolved):
            from puffpai.inference.llamacpp_backend import LlamaCppBackend
            backend = LlamaCppBackend(
                model_path=resolved,
                n_ctx=cfg.models.llm_n_ctx,
                n_gpu_layers=cfg.models.llm_n_gpu_layers,
            )
            if backend.is_available():
                _backend_instance = backend
                return _backend_instance

    # Attempt 2: Ollama HTTP server
    from puffpai.inference.ollama_backend import OllamaBackend
    # Pass the configured limits through; without them the agent path runs
    # at the backend defaults (2048 ctx / 256 tokens / 30 s timeout, which a
    # voice-length reply at ~49 tok/s can exceed).
    ollama = OllamaBackend(url=cfg.ollama.url, model=cfg.ollama.model,
                           num_ctx=cfg.ollama.num_ctx,
                           num_predict=cfg.ollama.num_predict,
                           timeout=120,
                           keep_alive=cfg.ollama.keep_alive)
    if ollama.is_available():
        _backend_instance = ollama
        return _backend_instance

    raise RuntimeError(
        "No LLM backend available. Set models.llm_gguf_path to a valid "
        "GGUF file or start Ollama at " + cfg.ollama.url
    )


def get_voice_models() -> dict[str, str]:
    """Return resolved absolute paths for voice model assets.

    Returns a dict with keys ``vosk_model_dir``, ``piper_model_path``,
    and ``wake_word_model_path``.  Relative paths are resolved against
    ``REPO_ROOT``.
    """
    cfg = get_config()
    root = cfg.REPO_ROOT

    def _resolve(p: str) -> str:
        return p if os.path.isabs(p) else os.path.join(root, p)

    return {
        "vosk_model_dir": _resolve(cfg.models.vosk_model_dir),
        "piper_model_path": _resolve(cfg.models.piper_model_path),
        "wake_word_model_path": _resolve(cfg.models.wake_word_model_path),
    }


def get_stt_config() -> dict:
    """Return the speech-to-text engine + confidence-gate settings.

    Consumed by :func:`puffpai.voice.stt.create_stt`. ``vosk_model_dir`` is the
    resolved absolute path (rollback engine); the whisper model name is passed
    to faster-whisper as-is (it resolves against its own HF cache).
    """
    cfg = get_config()
    return {
        "engine": cfg.models.stt_engine,
        "whisper_model": cfg.models.whisper_model,
        "whisper_device": cfg.models.whisper_device,
        "whisper_compute": cfg.models.whisper_compute,
        "min_avg_logprob": cfg.models.stt_min_avg_logprob,
        "max_no_speech_prob": cfg.models.stt_max_no_speech_prob,
        "vosk_min_word_confidence": cfg.models.vosk_min_word_confidence,
        "vosk_model_dir": get_voice_models()["vosk_model_dir"],
    }


# ---------------------------------------------------------------------------
# Writable state directories
# ---------------------------------------------------------------------------
#
# The appliance runs under a hardened systemd unit:
#
#     ProtectSystem=strict
#     ReadWritePaths=/data/puffpai  (+ a few explicit /opt/puffpai/* exceptions)
#
# so /opt/puffpai/src/** is READ-ONLY at runtime. Any module that resolves
# writable state to a package-relative path therefore fails with EROFS at
# runtime while looking fine in a dev checkout. Everything that writes state
# must resolve its directory through :func:`resolve_state_dir` so an operator
# can point it at a ReadWritePaths root from config (or one env var).

#: Env var naming the writable runtime-state root. Set it in the unit file
#: (``Environment=PUFFPAI_DATA_DIR=/data/puffpai/state``) to relocate every
#: state directory that has no explicit absolute path in config.
STATE_DIR_ENV = "PUFFPAI_DATA_DIR"

#: Appended to the message when a state directory turns out to be unwritable.
UNWRITABLE_HINT = (
    "path is not writable — on a hardened systemd unit it must live under a "
    "ReadWritePaths root such as /data/puffpai (set paths.state_dir / "
    "paths.vault_data_dir / paths.security_logs_dir to an absolute path, or "
    "set $" + STATE_DIR_ENV + ")"
)


# ── the last-resort fallback: per-user, not inside the checkout ─────────────
#
# HomeBotBuild installs on arbitrary Windows and Linux hardware, and the shipped
# defaults are all package/repo-relative — so with nothing configured and
# $PUFFPAI_DATA_DIR unset, every writable directory landed INSIDE the code tree.
# On a real install that means the household's vault, chat history and indexes
# live in the program directory: unwritable under a hardened unit or Program
# Files, shared between users on a multi-user box, and destroyed by reinstalling
# over the top. The last fallback is therefore
# :func:`puffpai.platform_paths.user_data_dir` (%LOCALAPPDATA%\PuffPAI /
# ~/.local/share/puffpai).
#
# ONE exception, and it is about not surprising anybody: a dev checkout that has
# ALREADY been run has state in <repo>/data, and silently moving it would look
# to the developer like the box lost its memory. When that state is there, the
# shipped repo-relative default is kept and the fact is logged once.
#
# The question is asked ONCE per repo root and the answer is cached, so every
# caller (data dir, vault, logs, manifest) moves together or stays together — a
# split, where the vault relocated and the config did not, would be worse than
# either whole answer.

#: Names that only appear under <repo>/data once this build has actually RUN.
#: `data/workout_library.json` is SHIPPED content and is deliberately not here:
#: a fresh clone has it, and a fresh clone has decided nothing yet.
_RUNTIME_STATE_MARKERS: tuple[str, ...] = (
    "agent_activity.jsonl", "agent_events.jsonl",
    "setup_complete.json", "setup_pending_apply.json",
    "build_manifest.json", "profiles.json",
    "vault_data", "vault", "chat_history", "logs", "security_logs",
    "chroma_db", "chromadb", "state",
)

#: repo_root -> bool. Populated on first use; see `_dev_checkout_has_state`.
_dev_state_cache: dict[str, bool] = {}


def _dev_checkout_has_state(repo_root: str) -> bool:
    """True when ``<repo>/data`` already holds runtime state from a past run.

    Cached per repo root, and the "keeping it" notice is logged once.
    """
    key = os.path.normpath(repo_root or "")
    if key in _dev_state_cache:
        return _dev_state_cache[key]
    data_dir = os.path.join(key, "data")
    try:
        entries = set(os.listdir(data_dir))
    except OSError:
        entries = set()
    found = any(marker in entries for marker in _RUNTIME_STATE_MARKERS)
    _dev_state_cache[key] = found
    if found:
        logger.info(
            "keeping state in this checkout (%s already holds runtime state). "
            "New installs use the per-user directory instead; set $%s to "
            "choose explicitly.", data_dir, STATE_DIR_ENV,
        )
    return found


def _state_root() -> str:
    """The writable root this install keeps state under, with no subdir.

    ``$PUFFPAI_DATA_DIR`` when set (expanded), else the per-user state
    directory. Used by :func:`_config_dir_for` so the config directory of a
    relocated install lands beside its state instead of inside the code tree.
    """
    root = os.environ.get(STATE_DIR_ENV, "").strip()
    if root:
        return os.path.normpath(os.path.expanduser(root))
    return _user_state_default()


def _user_state_default(env_subdir: str = "") -> str:
    """The per-user state root, with *env_subdir* hung off it like the env var."""
    from puffpai import platform_paths

    base = platform_paths.user_data_dir()
    return os.path.normpath(
        os.path.join(base, env_subdir) if env_subdir else base
    )


def get_state_dir(*, create: bool = False) -> str:
    """Absolute path of the directory for *machine-local secret state*.

    This is deliberately NOT ``DATA_DIR``. ``DATA_DIR`` can default to the repo
    root, so anything written there lands inside the source checkout and is one
    stray ``git add`` away from being published. Credentials the box mints for
    itself (the app API key, the setup code, the wizard run record) live here:

        * ``$PUFFPAI_STATE_DIR`` when set (tests and packagers pin it),
        * else ``$PUFFPAI_DATA_DIR/state`` when the data dir was moved out of
          the tree on purpose - one override keeps everything together,
        * else the per-user OS state directory, which is outside any checkout
          on every platform.

    With ``create=True`` the directory is created 0700 on POSIX. On Windows the
    mode argument is ignored by the OS; the directory inherits the ACL of the
    user profile, and callers that write secrets tighten the file itself.
    """
    override = os.environ.get("PUFFPAI_STATE_DIR")
    if override:
        path = os.path.abspath(override)
    else:
        data_override = os.environ.get(STATE_DIR_ENV)
        path = (os.path.join(os.path.abspath(data_override), "state")
                if data_override else _user_state_default())
    if create:
        os.makedirs(path, mode=0o700, exist_ok=True)
        try:
            os.chmod(path, 0o700)  # no-op on Windows; meaningful on POSIX
        except OSError:  # pragma: no cover - platform dependent
            pass
    return path


def resolve_state_dir(
    configured: str | None,
    *,
    repo_root: str,
    default: str,
    env_subdir: str = "",
    env_var: str = STATE_DIR_ENV,
    legacy: str | None = None,
) -> str:
    """Resolve a directory holding WRITABLE runtime state.

    Precedence, highest first:

    1. *configured* when ABSOLUTE — honoured verbatim. This is the only form
       an appliance should use, because it is the only one that can point
       outside the read-only code tree with certainty.
    2. ``$PUFFPAI_DATA_DIR`` (*env_var*), joined with *env_subdir*. A relative
       env value is resolved against *repo_root*.
    3. *configured* when RELATIVE — joined to *repo_root* (dev convenience;
       lands inside the code tree, which is why it ranks below the env var).
    4. the per-user state directory (:func:`puffpai.platform_paths.user_data_dir`,
       with *env_subdir* appended exactly as step 2 does) — UNLESS this is a dev
       checkout that already holds runtime state in ``<repo>/data``, in which
       case *default*, the package/repo-relative fallback the module shipped
       with, is kept and the fact is logged once. See
       :func:`_dev_checkout_has_state`.

    *legacy* is an optional path a previous release wrote to. It is used only
    when steps 3/4 produced a directory that does not exist yet **and** the
    legacy directory does, so upgrading an existing box does not silently
    start from empty state. This never overrides an explicit choice (1 or 2).

    Pure function: it reads only the arguments, ``os.environ`` and the
    filesystem, so it can be unit-tested without building a config.
    """
    configured = os.path.expanduser(str(configured).strip()) if configured else ""
    if configured and os.path.isabs(configured):
        return os.path.normpath(configured)

    env_root = os.environ.get(env_var, "").strip()
    if env_root:
        env_root = os.path.expanduser(env_root)
        if not os.path.isabs(env_root):
            env_root = os.path.join(repo_root, env_root)
        return os.path.normpath(
            os.path.join(env_root, env_subdir) if env_subdir else env_root
        )

    if configured:
        resolved = os.path.normpath(os.path.join(repo_root, configured))
    elif _dev_checkout_has_state(repo_root):
        resolved = os.path.normpath(default)
    else:
        # An UPGRADE still outranks the relocation. *legacy* names where a
        # previous release wrote; if that directory is there and the shipped
        # default is not, this box has state to keep and moving to a fresh
        # per-user root would look exactly like amnesia. Asked here, against
        # the default, rather than after the relocation: the per-user root is a
        # NEW location by definition, so comparing `legacy` to it would answer
        # a different question and always relocate.
        legacy_norm = os.path.normpath(legacy) if legacy else ""
        if (legacy_norm and os.path.isdir(legacy_norm)
                and not os.path.exists(os.path.normpath(default))):
            logger.warning(
                "using legacy state directory %s (nothing at %s yet). Move it "
                "and set an absolute path in config, or $%s, to silence this.",
                legacy_norm, os.path.normpath(default), env_var,
            )
            return legacy_norm
        resolved = _user_state_default(env_subdir)

    if legacy:
        legacy = os.path.normpath(legacy)
        if (legacy != resolved and not os.path.exists(resolved)
                and os.path.isdir(legacy)):
            logger.warning(
                "using legacy state directory %s (expected %s). Move it and "
                "set an absolute path in config to silence this.",
                legacy, resolved,
            )
            return legacy

    return resolved
