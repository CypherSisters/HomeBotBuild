"""Reusable first-run setup operations.

Single source of truth for the setup wizard's side effects, shared by the
``puffpai setup`` CLI command (see :mod:`puffpai.cli`) and the ``/setup/*`` REST
endpoints (see :mod:`puffpai.api_server`) so the terminal and graphical wizards
can never drift apart.

Every function here is small and honest: it checks Ollama, writes the two config
files the app reads, creates the reference vault, and records a "setup complete"
flag. Nothing here prompts, prints, or touches argparse/Flask - the front-ends
own the conversation; this module owns the effects.

Secrets discipline: a vault password and a Home Assistant token flow *in* to
``create_vault`` and ``set_ha_config``. They are written only to their intended
destinations (the encrypted vault; the gitignored HA config file). They are
never logged, never returned, and never placed in ``setup_status``.

Copyright (c) Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

import requests

from puffpai.config import get_config
from puffpai.security.lan_guard import no_proxies as _no_proxies  # marked home-network calls never use a proxy


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------

def _config_path() -> str:
    """Absolute path of the main config file the app reads."""
    cfg = get_config()
    return os.path.join(cfg.CONFIG_DIR, "puffpai_config.json")


def _ha_config_path() -> str:
    """Absolute path of the Home Assistant config file.

    Asked of :mod:`puffpai.security.ha_token_source`, which owns that file with
    :mod:`~puffpai.security.ha_vault`. The wizard does not spell the filename
    itself: that would make it a second, independent reader of a Zone-1
    credential's home -- the thing the one-source gate in
    ``tests/test_vault_migration_boot_path.py`` exists to prevent.
    """
    from puffpai.security import ha_token_source

    return ha_token_source.ha_config_path(get_config().CONFIG_DIR)


def _setup_flag_path() -> str:
    """Absolute path of the setup-complete marker.

    Lives under the data dir (honours ``PUFFPAI_DATA_DIR``) so a scratch install
    tracks its own state independently of any other install on the machine.
    """
    cfg = get_config()
    return os.path.join(cfg.DATA_DIR, "data", "setup_state.json")


def _vault_dir() -> str:
    """Resolve the vault directory from config (absolute)."""
    cfg = get_config()
    vd = cfg.paths.vault_data_dir
    return vd if os.path.isabs(vd) else os.path.join(cfg.DATA_DIR, vd)


def default_pack_dir() -> str:
    """Default folder offered for ZIM knowledge packs."""
    cfg = get_config()
    return os.path.join(cfg.DATA_DIR, "knowledge_packs")


# ---------------------------------------------------------------------------
# json helpers
# ---------------------------------------------------------------------------

def _read_json(path: str) -> dict[str, Any]:
    """Load a JSON object, returning {} on any failure."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _write_json(path: str, data: dict[str, Any]) -> None:
    """Write a JSON object with a trailing newline, creating parent dirs."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def config_template() -> dict[str, Any]:
    """Load the shipped config template, or {} if it is missing.

    HomeBotBuild ships ``config/puffpai_config.json`` in the code tree rather
    than a ``.example`` copy, so that is the template. When the wizard is
    pointed at its own config directory ($PUFFPAI_CONFIG_DIR) the shipped file
    is still the starting shape, which is how a fresh install gets the whole
    key set instead of the four sections the wizard happens to write.
    """
    cfg = get_config()
    shipped = os.path.join(cfg.REPO_ROOT, "config", "puffpai_config.json")
    example = os.path.join(cfg.REPO_ROOT, "config", "puffpai_config.example.json")
    return _read_json(example) or _read_json(shipped)


def _base_config() -> dict[str, Any]:
    """Return the config to merge into: the existing file, else the template."""
    existing = _read_json(_config_path())
    return existing or config_template()


# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------

def list_models(url: Optional[str] = None, timeout: int = 3) -> Optional[list[str]]:
    """Query Ollama's ``/api/tags``.

    Returns the installed model names, or ``None`` when Ollama is unreachable
    (an empty list means Ollama is up but has no models pulled).
    """
    from puffpai import brain_health

    cfg = get_config()
    return brain_health.list_models(url or cfg.ollama.url, timeout=timeout)


def ollama_survey(default_url: Optional[str] = None,
                  timeout: int = 2) -> list[dict[str, Any]]:
    """Every address worth trying, and what each answered.

    ``[{"url": ..., "models": [...] | None}]``, where ``None`` means nothing
    answered and ``[]`` means Ollama answered with no models pulled. Keeping the
    whole survey instead of the first hit is what lets the wizard say WHY it
    chose an address. Goes through this module's :func:`list_models` on purpose,
    so a test that has substituted the probe substitutes it for the search too.
    """
    from puffpai import brain_health

    cfg = get_config()
    return brain_health.survey(default_url or cfg.ollama.url, timeout=timeout,
                               probe=list_models)


def find_ollama(default_url: Optional[str] = None,
                timeout: int = 2) -> Optional[str]:
    """First address Ollama actually answers on, or None.

    Goes through this module's :func:`list_models` on purpose, so a test (or a
    caller) that has substituted the probe substitutes it for the search too.
    """
    from puffpai import brain_health

    cfg = get_config()
    for url in brain_health.candidate_urls(default_url or cfg.ollama.url):
        if list_models(url, timeout=timeout) is not None:
            return url
    return None


def brain_status(url: Optional[str] = None, model: Optional[str] = None,
                 timeout: int = 3) -> dict[str, Any]:
    """Is the CONFIGURED MODEL there? - not merely "is Ollama up".

    The distinction this wraps is the one that let a build with no model
    installed report ``inference: connected`` and answer chat requests with
    ``success: true`` over an error string. See :mod:`puffpai.brain_health`.
    """
    from puffpai import brain_health

    cfg = get_config()
    resolved_url = url or cfg.ollama.url
    resolved_model = model or cfg.ollama.model
    return brain_health.probe(resolved_url, resolved_model,
                              installed=list_models(resolved_url, timeout=timeout))


def check_ollama(url: Optional[str] = None, timeout: int = 3) -> dict[str, Any]:
    """Report Ollama reachability, installed models, and the selected model."""
    cfg = get_config()
    resolved = url or cfg.ollama.url
    models = list_models(resolved, timeout=timeout)
    return {
        "reachable": models is not None,
        "models": models or [],
        "selected": cfg.ollama.model,
        "url": resolved,
    }


#: Config paths that name a LAN service, mapped to the lan_posture service id.
#: Anything written through :func:`write_main_config` that lands on one of these
#: is checked against the build's run posture before it reaches disk.
_LAN_SERVICE_FIELDS: dict[tuple[str, str], str] = {
    ("ollama", "url"): "ollama",
    ("n8n", "url"): "n8n",
    ("kiwix", "url"): "kiwix",
    ("dashboard", "url"): "dashboard",
    ("ha", "url"): "home_assistant",
    ("media_server", "jellyfin_url"): "jellyfin",
    ("media_server", "calibre_web_url"): "calibre_web",
}


def _check_lan_posture(values: dict[str, Any],
                       posture: Optional[str] = None) -> None:
    """Refuse to persist a service URL the build's run posture forbids.

    This is the surface that refuses outright rather than neutralising: nothing
    is running against the value yet and the person is standing right there to
    read the message, so refusing the write is free. It also means the common
    case — someone typing an address into the wizard — never reaches the
    loader's neutralisation path at all.

    Raises :class:`~puffpai.security.lan_posture.PostureViolation`, whose
    ``user_message`` is written to be shown verbatim. Never raises for anything
    else: a resolver that is down, or a posture module that will not import,
    must not block the wizard.

    ``posture`` lets a caller that has already settled the posture state it,
    rather than letting the write path fall back to ``sealed``. That fallback is
    correct for the CLI, where the posture step always declares the answer
    before anything is written; it is wrong for a caller like the browser
    onboarding route, which has no posture question and would otherwise be able
    to write nothing but loopback. A stated posture is still normalised by
    :mod:`puffpai.security.lan_posture`, so this cannot be used to invent a
    permissive tier.
    """
    try:
        from puffpai.security import lan_posture
    except Exception:  # pragma: no cover - import guard only
        return
    for key, sub in values.items():
        if not isinstance(sub, dict):
            continue
        for field, url in sub.items():
            service_id = _LAN_SERVICE_FIELDS.get((key, field))
            if service_id and isinstance(url, str) and url.strip():
                lan_posture.require_allowed(service_id, url, posture=posture)


def write_main_config(values: dict[str, Any],
                      posture: Optional[str] = None) -> str:
    """Merge ``values`` into the main config file and write it back.

    ``values`` is a shallow map of section -> dict (e.g. ``{"ollama": {...}}``).
    Dict sections are merged key-by-key onto the existing config (or the shipped
    template on a fresh install); scalars replace. Reloads the config singleton
    so subsequent reads see the change. Returns the file path.

    ``posture`` is passed through to :func:`_check_lan_posture` and means the
    same thing it means for :func:`set_ha_config`: a caller that has already
    settled the run posture states it, instead of letting the check fall back to
    ``sealed``. That fallback is right for the CLI and wrong for the browser
    face, which never asks the posture question - it made ``/setup/ollama``
    refuse the owner's own LAN Ollama box on an ``island`` build. A stated
    posture is re-normalised by :mod:`puffpai.security.lan_posture`, so this
    cannot be used to invent a tier the build did not choose.
    """
    base = _base_config()
    for key, sub in values.items():
        if isinstance(sub, dict):
            section = base.get(key)
            if not isinstance(section, dict):
                section = {}
            section.update(sub)
            base[key] = section
        else:
            base[key] = sub
    _check_lan_posture(values, posture)
    path = _config_path()
    _write_json(path, base)
    get_config(_force_reload=True)
    return path


def set_ollama_config(model: str, url: Optional[str] = None,
                      posture: Optional[str] = None) -> str:
    """Persist the chosen Ollama model (and optionally URL). Returns config path.

    ``posture`` is the build's settled run posture; see
    :func:`write_main_config`. Omitted, the write is judged as ``sealed``, which
    allows only a loopback Ollama address.
    """
    cfg = get_config()
    ollama: dict[str, Any] = {"url": url or cfg.ollama.url}
    if model:
        # No model means none fits this machine; an empty name is not written
        # over whatever the config already says.
        ollama["model"] = model
    return write_main_config({"ollama": ollama}, posture)


# ---------------------------------------------------------------------------
# Vault
# ---------------------------------------------------------------------------

def create_vault(password: str, vault: Any = None, *,
                 with_recovery: bool = False) -> Optional[str]:
    """Create the encrypted vault, protected by ``password``.

    Goes through the reference :class:`~puffpai.security.vault.Vault`
    (PBKDF2 + Fernet) - no crypto is duplicated here. The vault is left locked;
    the password is never stored, logged, or returned. Raises
    :class:`~puffpai.security.vault.VaultError` if a vault already exists or the
    password is empty.

    ``with_recovery`` decides whether a recovery code is issued alongside the
    vault, and it is the reason this function has a return value at all. The two
    wizard faces have to behave the same way: the CLI's Zone 1 step issues a code
    and makes the owner type it back, so the browser onboarding that posts to
    ``/setup/vault`` must be able to do the same rather than quietly creating a
    PIN-only vault nobody can ever get back into. The flag is opt-in so that
    every existing caller keeps exactly the behaviour it had - a legacy caller
    creates a PIN-only vault, and a manifest written for it truthfully says
    ``recovery_issued: false``.

    Returns the recovery code when one was issued, and ``None`` otherwise. The
    code is returned for exactly one screen: it is never written to disk, never
    logged, and never placed in a manifest. A caller that stores it anywhere has
    broken the contract this step makes with the owner.

    Note for the wizard: the CLI does not call this directly. It goes through
    :func:`seal_vault_with_recovery`, which adds the stricter PIN policy a typed
    -back PIN needs (see :mod:`puffpai.zone_vault`) and which a passphrase like
    "correct horse battery staple" would fail.

    **This is the one adapter between the wizard and this build's vault.**
    BuildBot's vault offered ``create_with_recovery`` in a single call; this
    build's :class:`~puffpai.security.vault.Vault` is the fuller one (AES-GCM
    wrapped DEK, TOTP, scrub-and-vault), and it spells the same ceremony as
    ``initialize`` -> ``authenticate`` -> ``generate_recovery_code`` because
    minting a code requires a live session holding the data key. The sequence
    lives here and nowhere else, so there is still exactly one crypto path and
    one issuance behaviour underneath both faces. The session is closed before
    returning; the PIN and the code are never written here.
    """
    from puffpai.security.vault import Vault

    if vault is None:
        vault = Vault(vault_dir=_vault_dir())
    vault.initialize(password)
    code: Optional[str] = None
    session = None
    try:
        session = vault.authenticate(password)
        if with_recovery:
            code = vault.generate_recovery_code(session.session_id)
    finally:
        # Do not leave a dangling unlocked session; the normal flow re-unlocks
        # through /vault/unlock.
        if session is not None:
            try:
                vault.logout(session.session_id)
            except Exception:  # noqa: BLE001 - closing a session never fails setup
                pass
    return code


def vault_initialized(vault: Any = None) -> bool:
    """True once the vault's salt + encrypted store exist on disk."""
    from puffpai.security.vault import Vault

    if vault is None:
        vault = Vault(vault_dir=_vault_dir())
    return bool(vault.is_initialized)


# ---------------------------------------------------------------------------
# Home Assistant
# ---------------------------------------------------------------------------

def set_ha_config(url: str, token: str, enabled: bool = True,
                  posture: Optional[str] = None) -> str:
    """Record the Home Assistant URL + token. Returns the file path.

    The token is written only to the gitignored HA config file, through
    :func:`puffpai.security.ha_token_source.write_ha_config` -- the one module
    allowed to touch it -- so the wizard never opens that file itself. It is
    never logged or echoed.

    **Posture-checked before the token touches disk.** Home Assistant is the one
    integration that carries a credential, and it is written to its own file
    rather than through :func:`write_main_config`, so it missed the LAN posture
    check that every other service URL goes through:
    ``--ha-url http://8.8.8.8:8123 --ha-token <token>`` was accepted and
    written under ``island`` in the same run that refused a public Ollama
    address. The check runs here, at the write, so no caller can route around it
    -- and it runs BEFORE :func:`_write_json`, so a refusal leaves no token on
    disk. Raises
    :class:`~puffpai.security.lan_posture.PostureViolation`.
    """
    from puffpai.security import ha_token_source

    _check_lan_posture({"ha": {"url": url}}, posture)
    path = ha_token_source.write_ha_config(
        url, token, enabled=bool(enabled), config_dir=get_config().CONFIG_DIR)
    get_config(_force_reload=True)
    _reload_ha_bridge()
    return path


def _reload_ha_bridge() -> None:
    """Rebuild a running backend's Home Assistant bridge after a save.

    The bridge keeps the address, token and on/off it was built with, so one
    built before this save would keep talking to the old Home Assistant (or to
    none) until a restart. Only an existing bridge is rebuilt; a process that
    never built one (the console wizard) builds nothing here. Never raises.
    """
    import sys
    module = sys.modules.get("puffpai.ha_bridge")
    if module is None or getattr(module, "_instance", None) is None:
        return
    try:
        module.get_bridge(_force_reload=True)
    except Exception:  # noqa: BLE001 - the save stands; the next start reads it
        pass


def test_ha_connection(url: str, token: str, timeout: int = 5) -> dict[str, Any]:
    """Probe a Home Assistant instance with a candidate URL + token.

    Standalone (does not depend on the configured/enabled bridge) so the wizard
    can validate credentials before saving them. Returns ``{"ok": bool, ...}``
    with a HA version on success or a short ``error`` string on failure. Never
    raises; never includes the token in the result.
    """
    base = (url or "").rstrip("/")
    if not base or not token:
        return {"ok": False, "error": "URL and token are both required."}
    from urllib.parse import urlsplit

    from puffpai.security import lan_http, netaddr
    try:
        local_name = netaddr.is_local_name(urlsplit(base).hostname or "")
    except ValueError:
        local_name = False
    try:
        if local_name:
            # A .local name may also answer with addresses outside the house
            # (an IPv6 home gives the box a global one): the token goes only
            # to its answers inside the house (security.netaddr.house_addresses),
            # never redirected, never through a proxy.
            resp = lan_http.lan_get(f"{base}/api/config", local_names=True,
                                    headers={"Authorization": f"Bearer {token}"},
                                    timeout=timeout, allow_redirects=False)
        else:
            # Home Assistant /api/config on the LAN: the URL the owner entered,
            # already refused by security.lan_posture if it is not LAN. Registered
            # non-egress in tests/test_egress_door_registry.py::_NOT_EGRESS_LINES.
            resp = requests.get(
                f"{base}/api/config",
                headers={"Authorization": f"Bearer {token}"},
                timeout=timeout,
                # Never follow a redirect: the address was judged house-local
                # above, and a 3xx would let whatever answers there send this
                # probe (and the candidate token) somewhere that was not judged.
                allow_redirects=False,
                proxies=_no_proxies(),
            )
    except (netaddr.AddressRefused, netaddr.ResolutionError):
        return {"ok": False, "error": (
            f"{base} did not answer with an address on your home network, so "
            f"the token was not sent. Type the IP address of your Home "
            f"Assistant box instead, for example http://192.168.1.20:8123.")}
    except requests.exceptions.ConnectionError:
        return {"ok": False, "error": (
            f"Nothing answered at {base}. Check that Home Assistant is on and "
            f"that the address is right.")}
    except requests.exceptions.Timeout:
        return {"ok": False, "error": "Home Assistant did not answer in time. "
                                      "Check that it is on and try again."}
    except requests.exceptions.RequestException:
        return {"ok": False, "error": (
            "That address could not be used. Check it, for example "
            "http://homeassistant.local:8123.")}

    if 300 <= resp.status_code < 400:
        return {"ok": False, "error": (
            f"That address answered with a redirect (HTTP {resp.status_code}), "
            "so this is not Home Assistant's own address. Enter the address "
            "Home Assistant itself serves, for example http://homeassistant.local:8123.")}
    if resp.status_code in (401, 403):
        return {"ok": False, "error": (
            "Home Assistant did not accept that token. Make a new long-lived "
            "access token and paste the whole of it.")}
    if not resp.ok:
        return {"ok": False, "error": (
            f"Something answered at that address, but not as Home Assistant "
            f"does (code {resp.status_code}). Check the address.")}
    try:
        info = resp.json()
    except ValueError:
        info = {}
    return {
        "ok": True,
        "version": info.get("version", "unknown"),
        "location_name": info.get("location_name", ""),
    }


def ha_configured() -> bool:
    """True when HA is configured with a URL + token and enabled."""
    data = _read_json(_ha_config_path())
    return bool(data.get("enabled")) and bool(data.get("url")) and bool(data.get("token"))


def ha_url() -> str:
    """Configured HA URL for prefill (never the token). Empty if unset."""
    return str(_read_json(_ha_config_path()).get("url", ""))


# ---------------------------------------------------------------------------
# Knowledge
# ---------------------------------------------------------------------------

def set_knowledge_config(zim_dir: str, auto_index: bool = True,
                         create_dir: bool = False) -> str:
    """Persist the knowledge-pack folder. Returns the config path.

    This build reads its knowledge sources from ``paths.zim_dirs`` (a list -
    :mod:`puffpai.knowledge.fts_index` derives the FTS shard directory beside
    the first entry). The ``knowledge`` section is written as well, with the
    same folder as ``zim_dir`` and ``data_dir``, for the parts that find and
    add more after the build (:mod:`puffpai.knowledge.my_data`,
    :mod:`puffpai.knowledge.find_more`). ``auto_index`` is recorded there and
    in the manifest; this build's own packs are indexed on demand.

    When ``create_dir`` is set and ``zim_dir`` is given, the folder is created
    (best effort) so the user has somewhere to drop packs.
    """
    zim_dir = zim_dir or ""
    if create_dir and zim_dir:
        try:
            os.makedirs(zim_dir, exist_ok=True)
        except OSError:
            pass
    # ``data_dir`` is the one setting (knowledge/my_data.py); ``zim_dir``
    # carries the same value for older readers.
    return write_main_config({
        "paths": {"zim_dirs": [zim_dir] if zim_dir else []},
        "knowledge": {"zim_dir": zim_dir, "data_dir": zim_dir,
                      "auto_index": bool(auto_index)},
    })


def embedding_options(installed: Optional[list[str]] = None) -> dict[str, Any]:
    """What this box can embed with, and which of it is already here.

    ``default`` is the backend that works on a base install with no extra
    package and no model pulled (chromadb's own ONNX all-MiniLM-L6-v2).
    ``upgrade`` is offered only when ``nomic-embed-text`` is already in
    Ollama's model list — it is a different vector space, so it is an upgrade
    the owner chooses at setup time, never a silent switch later.
    """
    from puffpai import brain_health, embeddings

    model = embeddings.DEFAULT_OLLAMA_EMBEDDING_MODEL
    have_nomic = bool(installed) and brain_health.is_installed(
        model, list(installed or []))
    return {
        "default": embeddings.BACKEND_CHROMA_DEFAULT,
        "default_note": (
            "Search-by-meaning runs on this box with no extra install. The "
            "model files are a one-time download unless you copy them over."),
        "upgrade": embeddings.BACKEND_OLLAMA if have_nomic else "",
        "upgrade_model": model,
        "upgrade_note": (
            f"{model} is already in Ollama here, and it embeds faster on a GPU."
            if have_nomic else
            f"Faster on a GPU, but it needs `ollama pull {model}` first."),
        "files_present": embeddings.onnx_model_present(),
    }


def set_embedding_config(backend: str, model: str = "") -> str:
    """Write ``models.embedding_backend`` (and the Ollama model) explicitly.

    Written even when it matches the shipped default: the build's own record
    should say which embedder it chose rather than inheriting whatever a later
    default becomes.
    """
    from puffpai import embeddings

    values: dict[str, Any] = {"models": {"embedding_backend": backend}}
    if backend == embeddings.BACKEND_OLLAMA:
        values["models"]["ollama_embedding_model"] = (
            model or embeddings.DEFAULT_OLLAMA_EMBEDDING_MODEL)
    return write_main_config(values)


# ---------------------------------------------------------------------------
# BuildBot wizard: hardware census, brain menu, crew, interview, manifest
#
# These wrap the census/catalog/manifest modules so the CLI and the /setup/*
# endpoints drive one code path. The read steps (census, menu, crew, interview)
# are side-effect-free; the manifest is the one that writes, and it is split into
# preview (assemble + diff, no write) and commit (write) so a face can show what
# would change before it lands - the wizard never overwrites blindly.
# ---------------------------------------------------------------------------

def hardware_census() -> dict[str, Any]:
    """Run the hardware census (RAM-first). Never raises."""
    from puffpai import hardware_census as hc
    return hc.run_census()


def brain_menu(census: dict[str, Any], probe_ollama: bool = True) -> dict[str, Any]:
    """Fit the local-model menu to a census, reconciling installed Ollama tags."""
    from puffpai import model_catalog
    installed = list_models() if probe_ollama else None
    return model_catalog.fit_menu(census, installed_models=installed)


def crew_budget(census: dict[str, Any]) -> dict[str, Any]:
    """Return the specialist crew budget for a census."""
    from puffpai import crew_sizing
    return crew_sizing.crew_budget(census)


def build_interview(goal: str, census: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Propose capabilities for a goal, within the census's crew budget."""
    from puffpai import build_interview as bi
    budget = crew_budget(census) if census is not None else None
    return bi.propose(goal, crew_budget=budget)


#: Sections :func:`preview_manifest` attaches to the assembled manifest itself,
#: because :func:`build_manifest.assemble_manifest` takes no keyword for them.
#: Order is the order they are written; every one of them is still sanitised.
_ATTACHED_BLOCKS: tuple[str, ...] = (
    "vault", "bridge", "household", "persona", "interview",
)


def preview_manifest(census: dict[str, Any], **choices: Any) -> dict[str, Any]:
    """Assemble a manifest from the census + choices and diff it against any
    existing one, WITHOUT writing. Returns ``{manifest, diff, warnings}``.

    Choices are the optional sections :func:`build_manifest.assemble_manifest`
    knows: ``identity``, ``brain``, ``crew``, ``storage``, ``knowledge``,
    ``egress``, ``integrations``, ``posture``, ``surfaces``, ``provenance``.

    ``vault``, ``bridge``, ``household``, ``persona`` and ``interview`` are
    handled here rather than there: they are status blocks the zone and roster
    steps produce (see :mod:`puffpai.zone_vault` and :mod:`puffpai.zone_bridge`),
    so they are attached to the assembled manifest as top-level blocks. The CLI
    wizard attached the last three by hand after calling this function
    (``setup_cli._step_manifest``); doing it here means the browser face gets
    the same manifest for the same answers instead of one with no household in
    it. They go through ``sanitize_manifest`` on the way to disk like everything
    else, which is what keeps a PIN or a recovery code from riding along even if
    a caller tried.
    """
    from puffpai import build_manifest
    attached = {name: choices.pop(name, None) for name in _ATTACHED_BLOCKS}
    manifest = build_manifest.assemble_manifest(census, **choices)
    for name, block in attached.items():
        if block is not None:
            manifest[name] = block
    diff = build_manifest.diff_manifest(build_manifest.read_manifest(), manifest)
    warnings = build_manifest.validate_manifest(manifest)
    return {
        "manifest": build_manifest.sanitize_manifest(manifest),
        "diff": diff,
        "warnings": warnings,
    }


def commit_manifest(manifest: dict[str, Any]) -> str:
    """Sanitize and write a manifest to disk (the contract guard runs here).

    Returns the path. Call after showing :func:`preview_manifest`'s diff.
    """
    from puffpai import build_manifest
    return build_manifest.write_manifest(manifest)


def export_build_bundle(manifest: dict[str, Any], dest_dir: str,
                        payloads: Optional[dict[str, str]] = None,
                        references: Optional[dict[str, Any]] = None,
                        label: str = "") -> dict[str, Any]:
    """Write a provisioning bundle for the sneakernet path. No network."""
    from puffpai import build_bundle
    return build_bundle.export_bundle(manifest, dest_dir, payloads=payloads,
                                      references=references, label=label)


def verify_build_bundle(src_dir: str) -> tuple[bool, list[str]]:
    """Dry-run a bundle's checksums. Returns ``(ok, plain-English problems)``."""
    from puffpai import build_bundle
    return build_bundle.verify_bundle(src_dir)


def import_build_bundle(src_dir: str, dest_dir: Optional[str] = None) -> dict[str, Any]:
    """Verify a bundle whole, then apply it into this install. No network.

    Payload files land under the data dir; the manifest lands where this install
    reads its manifest from, so an imported build is the build that runs.
    """
    from puffpai import build_bundle, build_manifest
    cfg = get_config()
    target = dest_dir or cfg.DATA_DIR
    return build_bundle.import_bundle(src_dir, target,
                                      manifest_target=build_manifest.manifest_path())


# ---------------------------------------------------------------------------
# BuildBot wizard v2: posture, the three zones, surfaces, config health
#
# Same thin-wrapper discipline as the block above. Step 0 (posture) is first
# because its gates decide what every later step is allowed to offer; the zone
# wrappers exist so the CLI and the /setup/* endpoints walk Vault and Bridge
# through one code path rather than two.
# ---------------------------------------------------------------------------

def posture_options() -> dict[str, Any]:
    """The Step 0 menus: both axes plus the enumerated egress table."""
    from puffpai import build_posture
    return {
        "build_network": build_posture.build_network_options(),
        "run_egress": build_posture.run_egress_options(),
        "egress_switches": build_posture.egress_switches(),
    }


def assess_posture(build_network: Optional[str] = None,
                   run_egress: Optional[str] = None,
                   enabled_switches: Optional[list[str]] = None) -> dict[str, Any]:
    """Settle the build posture from what the owner stated, or the defaults.

    ``None`` means "the owner skipped this axis", and the result records that the
    value was defaulted rather than chosen. Never raises.
    """
    from puffpai import build_posture
    return build_posture.assess_posture(build_network, run_egress, enabled_switches)


def persist_posture(posture: dict[str, Any]) -> str:
    """Write the posture the owner just chose into the runtime config.

    The wizard recorded its enumerated egress table in the MANIFEST and nowhere
    else, while the runtime reads ``egress_doors`` out of
    ``config/puffpai_config.json`` — which the wizard seeds from the shipped
    example, which carried an explicit ``"weather": false``. An explicit config
    value outranks the manifest fallback, so
    ``--run-egress enumerated --egress-door weather`` produced a manifest saying
    the door was open and a runtime with it shut. The owner's answer has to be
    the thing the runtime honours, so it is written here, once, into the file
    the runtime and the status page both read.

    Every door the wizard did not open is written ``false`` explicitly rather
    than left out. A door table that lists only what is open cannot be read as
    a statement about what is shut, and this file is meant to be readable.

    In this build the wizard's enumerated table IS the door registry
    (:data:`puffpai.security.egress_doors.DOORS`) rather than a parallel list of
    switch names - see :func:`puffpai.build_posture.egress_switches` - so a
    switch id is a door id and the mapping is the identity. That is the whole
    Bridge-posture/door-registry adaptation: there is no second posture model to
    keep in step, and a door added to the registry appears in the wizard's table
    on its own.

    Doors the owner did not open are written ``false`` explicitly rather than
    left out. A door table that lists only what is open cannot be read as a
    statement about what is shut, and this file is meant to be readable.
    """
    from puffpai.security import egress_doors, lan_posture

    # Declare the posture to the LAN check BEFORE anything else in this run is
    # written. There is no build manifest on a first run, and "no manifest"
    # must mean `sealed` at a write path, not the ordinary default -- otherwise
    # `--run-egress sealed --ollama-url http://192.168.1.50:11434` is accepted
    # on a box whose owner just asked for sealed. Declaring it here means every
    # later step in the same run is judged against the owner's actual answer.
    lan_posture.set_active_posture(posture)

    chosen = set((posture or {}).get("enabled_switches") or [])
    doors: dict[str, Any] = {}
    for d in egress_doors.DOORS:
        doors[d.config_flag] = bool(d.id in chosen)
    path = write_main_config({"egress_doors": doors})
    egress_doors.reset_cache()
    return path


def posture_gates(posture: dict[str, Any]) -> dict[str, bool]:
    """What the posture permits later steps to OFFER. Never a decision to do."""
    from puffpai import build_posture
    return build_posture.gates(posture)


def posture_block(posture: dict[str, Any]) -> dict[str, Any]:
    """The posture, shaped for the manifest's ``posture`` block."""
    from puffpai import build_posture
    return build_posture.posture_manifest_block(posture)


def validate_vault_pin(pin: str) -> tuple[bool, str]:
    """Check a proposed vault PIN. Returns ``(ok, plain-English reason)``.

    :func:`puffpai.zone_vault.validate_pin` is the wizard's policy (length, no
    spaces, no patterns, no common choices). This build's vault enforces its own
    floor inside :meth:`~puffpai.security.vault.Vault.initialize` (four
    characters); the wizard's floor is deliberately well above it, so a PIN that
    passes here always passes the seal. That direction is the invariant: the
    wizard may be stricter than the vault, never laxer -
    ``tests/test_wizard_homebot.py`` pins it.

    The PIN is not logged, stored, or echoed back inside the reason.
    """
    from puffpai import zone_vault

    return zone_vault.validate_pin(pin)


def vault_recovery_notice() -> list[str]:
    """The 'write this code down' screen, in order, ready to print."""
    from puffpai import zone_vault
    return zone_vault.recovery_notice_lines()


def confirm_vault_recovery(shown: str, typed: str) -> bool:
    """True when the owner typed back the code we showed. Fails closed."""
    from puffpai import zone_vault
    return zone_vault.verify_recovery_confirmation(shown, typed)


def seal_vault_with_recovery(pin: str, vault: Any = None
                             ) -> tuple[dict[str, Any], str]:
    """Create the vault under ``pin`` and issue its recovery code.

    This is the wizard's vault path, and the only one it may use: the PIN-only
    :func:`puffpai.zone_vault.seal_vault` would run the whole "write this down"
    ceremony and hand back nothing to write. Returns
    ``(manifest_block, recovery_code)`` - the code is a separate return value on
    purpose, so a caller that persists the block cannot persist the code.

    The vault handle is resolved from this install's data dir when one is not
    passed, so a scratch install seals its own vault and never a neighbour's.
    Raises :class:`ValueError` for a PIN that fails
    :func:`validate_vault_pin`, and lets
    :class:`puffpai.security.vault.VaultError` through when a vault already
    exists or the PIN is under the vault's own floor.
    """
    from puffpai import zone_vault
    from puffpai.security.vault import Vault

    handle = vault if vault is not None else Vault(vault_dir=_vault_dir())
    return zone_vault.seal_vault_with_recovery(pin, vault=handle)


def rotate_vault_recovery(current_code: str, pin: str, vault: Any = None) -> str:
    """Burn a recovery code that was shown but never written down, issue another.

    The escape hatch behind "I cannot do this right now": the code already on the
    screen is treated as spent, the vault is re-keyed under the same PIN, and a
    fresh code is returned for a fresh attempt. Better than continuing with a
    code nobody copied. Lets :class:`puffpai.security.vault.VaultError` through.

    On this build's vault the two halves are separate calls:
    :meth:`~puffpai.security.vault.Vault.recover_with_code` spends the old code
    and re-keys under *pin*, then
    :meth:`~puffpai.security.vault.Vault.generate_recovery_code` mints the
    replacement from the session that recovery just opened. Same outcome as
    BuildBot's single ``redeem_recovery_code``; one code in, one code out, and
    the old one is dead either way.
    """
    from puffpai.security.vault import Vault

    handle = vault if vault is not None else Vault(vault_dir=_vault_dir())
    session = handle.recover_with_code(current_code, pin)
    try:
        new_code = handle.generate_recovery_code(session.session_id)
    finally:
        try:
            handle.logout(session.session_id)
        except Exception:  # noqa: BLE001
            pass
    return new_code


def vault_block(*, configured: bool = True, sealed: bool = True,
                method: str = "pin", recovery_issued: bool = False,
                recovery_confirmed: bool = False) -> dict[str, Any]:
    """Status-only vault record for the manifest. Takes no PIN and no code."""
    from puffpai import zone_vault
    return zone_vault.vault_manifest_block(
        configured=configured, sealed=sealed, method=method,
        recovery_issued=recovery_issued, recovery_confirmed=recovery_confirmed)


# --- the network edge: can THIS machine run Zone 3? ------------------------

def _port_53_listener() -> Optional[bool]:
    """Is something already listening on port 53 here? ``None`` = unknown.

    Read-only and non-invasive by construction. It does NOT bind the port, open
    a socket, or send a packet: binding :53 to find out whether :53 is free is
    exactly the kind of "detection" that takes a household's DNS down for the
    two seconds it takes to answer a wizard question. It reads the output of
    ``ss -lntu`` (the same check ``install.sh --with-zone3`` makes), falling
    back to ``netstat -ano``.

    ``None`` is a real answer and is reported as "unknown" rather than rounded
    to "free". Never raises.
    """
    import shutil
    import subprocess

    probes: list[list[str]] = []
    if shutil.which("ss"):
        probes.append(["ss", "-lntu"])
    if shutil.which("netstat"):
        probes.append(["netstat", "-ano"] if os.name == "nt" else ["netstat", "-lntu"])
    for argv in probes:
        try:
            out = subprocess.run(argv, capture_output=True, text=True,
                                 timeout=5, check=False).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        for line in out.splitlines():
            head = line.split()
            for field in head[:5]:
                if field.endswith(":53") or field.endswith(".53"):
                    return True
        return False
    return None


def zone3_support() -> dict[str, Any]:
    """Can HomeBot's own network edge (Zone 3) run on THIS machine?

    Returns ``{"supported": bool, "reasons": [...], ...}``. Every reason is one
    plain sentence an owner can act on. Detection is read-only and local:
    :func:`sys.platform`, ``shutil.which("docker")``, and
    :func:`_port_53_listener`. Nothing is started, nothing is bound, no address
    on the network is contacted. Never raises.

    An unsupported machine is not a lesser build. Zone 3 is one of three
    acceptable answers to the edge question, and the two others - your own gear,
    or nothing at all - leave the Bridge zone just as configured.
    """
    import shutil
    import sys

    reasons: list[str] = []
    if sys.platform != "linux":
        # One sentence, and then we stop: on Windows the docker and port checks
        # would only add noise to a verdict that is already settled.
        reasons.append(
            "Zone 3 runs AdGuard Home in Docker on Linux, and this machine "
            "reports " + sys.platform + "; your own edge gear or no edge at all "
            "are both complete answers here.")
        return {"supported": False, "reasons": reasons, "platform": sys.platform,
                "docker": bool(shutil.which("docker")), "port_53": "unchecked"}

    docker = bool(shutil.which("docker"))
    if not docker:
        reasons.append("Docker is not installed here, and Zone 3 runs in Docker.")

    listener = _port_53_listener()
    port_53 = "unknown" if listener is None else ("in_use" if listener else "free")
    if listener is True:
        reasons.append(
            "Something is already listening on port 53 here (systemd-resolved, "
            "usually); free it first - see docs/ZONE3_SETUP.md.")
    elif listener is None:
        reasons.append(
            "We could not tell whether port 53 is free (no ss or netstat here), "
            "so check it yourself before you start Zone 3.")

    # An unknown port is a caveat, not a refusal: the owner can look. A missing
    # Docker or a busy port is a refusal, because the compose file would fail.
    supported = docker and listener is not True
    if supported and not reasons:
        reasons.append("Linux with Docker, and port 53 looks free.")
    return {"supported": supported, "reasons": reasons, "platform": sys.platform,
            "docker": docker, "port_53": port_53}


def edge_options() -> dict[str, Any]:
    """The edge-security menu for this machine (Zone 3 row carries its verdict)."""
    from puffpai import zone_bridge
    return zone_bridge.edge_options(zone3_support())


def validate_edge_url(url: str) -> tuple[bool, str]:
    """Is this a LAN/loopback admin address we are willing to write down?

    ``(ok, note)``. The address is **data**: it is recorded so the build record
    says where the owner's own Pi-hole lives, and it is never fetched, resolved,
    or opened. So the check is deliberately offline - it reuses
    :func:`puffpai.security.netaddr.check` under the house posture, which is
    pure arithmetic on an address literal, and it does NOT go through
    :func:`puffpai.security.lan_http.validate_url`, whose job includes resolving
    the name (a DNS query this step has no business making).

    A public literal is refused, because writing "my edge device is at 8.8.8.8"
    into the build record is either a typo or a misunderstanding worth catching
    while the owner is standing there. A hostname is accepted with a note saying
    it was not checked: we will not look it up to find out.
    """
    from urllib.parse import urlsplit

    text = str(url or "").strip()
    if not text:
        return True, ""
    parts = urlsplit(text)
    if parts.scheme.lower() not in ("http", "https"):
        return False, "Use an http:// or https:// address for the admin page."
    host = parts.hostname
    if not host:
        return False, "That address has no host in it."
    try:
        from puffpai.security import netaddr
    except Exception:  # noqa: BLE001 - a missing helper must not block the step
        return True, "Recorded as you typed it; it was not checked."
    try:
        netaddr.check(host, posture=netaddr.HOUSE)
    except netaddr.UnparseableAddress:
        return True, ("Recorded as a name. We did not look it up - this step "
                      "never touches your network.")
    except netaddr.AddressRefused:
        return False, ("That address is not on your own network. The admin page "
                       "of your edge gear should be a LAN or loopback address.")
    except Exception:  # noqa: BLE001
        return True, "Recorded as you typed it; it was not checked."
    return True, ""


def edge_block(kind: str = "none", external_kind: str = "",
               external_url: str = "",
               support: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """The owner's edge answer, shaped for ``manifest.bridge.edge``.

    ``support`` is resolved from this machine when it is not passed, so a caller
    cannot accidentally record "Zone 3 is supported" on a box where it is not.
    """
    from puffpai import zone_bridge
    ok, note = validate_edge_url(external_url)
    return zone_bridge.edge_manifest_block(
        kind,
        external_kind=external_kind,
        external_url=external_url if ok else "",
        external_url_note=note if ok else
        f"{note} The address was not recorded.",
        support=support if support is not None else zone3_support())


def record_zone3_pending(edge: dict[str, Any]) -> Optional[str]:
    """Record the Zone 3 commands for the owner to run. Applies NOTHING.

    Called only when Zone 3 was asked for AND this machine can run it. Returns
    the pending-apply file path, or ``None`` when there was nothing to record.
    """
    zone3 = (edge or {}).get("zone3") or {}
    if not (zone3.get("requested") and zone3.get("supported")):
        return None
    return record_pending_apply("zone3_network_edge", {
        "needs": "you run these yourself; the wizard changed nothing on your network",
        "commands": list(zone3.get("commands") or []),
        "mode": zone3.get("mode", ""),
        "doc": zone3.get("doc", ""),
        "doc_sections": list(zone3.get("doc_sections") or []),
    })


def bridge_options(has_home_assistant: bool = False,
                   has_external_firewall: bool = False,
                   has_managed_lan: bool = False) -> dict[str, Any]:
    """Everything the Bridge step offers, in one dict.

    The four postures plus a suggestion for what the owner says they have (a
    suggestion, never a setting - the ladder always comes back whole), and
    alongside them ``edge_options`` and ``zone3_support``, which is what the
    dashboard's opt-in card renders. ``GET /setup/bridge`` returns this verbatim,
    so the browser wizard and the terminal wizard offer the same menu or neither
    does.
    """
    from puffpai import zone_bridge
    options = zone_bridge.posture_for_environment(
        has_home_assistant, has_external_firewall, has_managed_lan)
    # This build's setup is the HomeBot preset, which records Moat: with every
    # outside service off, nothing reaches the internet. That is the setting
    # a browser that takes ``default`` without asking must record.
    from puffpai import homebot_preset
    options["default"] = homebot_preset.BRIDGE_POSTURE
    support = zone3_support()
    options["zone3_support"] = support
    options["edge_options"] = zone_bridge.edge_options(support)
    return options


def reconcile_bridge(posture: Any, run_egress: str = "") -> Any:
    """Reconcile a Bridge answer with Step 0's egress tier. Stricter wins.

    Two shapes, because there are two callers:

    * ``reconcile_bridge("moat", "island")`` -> ``(posture, explanation)``. The
      original tuple form; unchanged.
    * ``reconcile_bridge({"posture": ..., "edge": {...}}, run_egress)`` ->
      ``{"posture", "note", "edge"}``. The whole Bridge answer, which is what
      the dashboard POSTs. ``edge`` is validated and normalised here, so a
      browser cannot record an edge answer the terminal wizard would refuse.

    Dict in, dict out; string in, tuple out. Never raises.
    """
    from puffpai import zone_bridge

    if isinstance(posture, dict):
        answer = posture
        final, note = zone_bridge.reconcile_with_egress(
            str(answer.get("posture") or ""), run_egress)
        raw = answer.get("edge") or {}
        return {
            "posture": final,
            "note": note,
            "edge": edge_block(
                kind=str(raw.get("kind") or "none"),
                external_kind=str(raw.get("external_kind") or ""),
                external_url=str(raw.get("external_url") or ""),
            ),
        }
    return zone_bridge.reconcile_with_egress(posture, run_egress)


def bridge_block(posture: str, run_egress: Optional[str] = None,
                 environment: Optional[dict[str, Any]] = None,
                 chosen_by: str = "owner",
                 edge: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """The reconciled Bridge policy, shaped for the manifest.

    ``edge`` defaults to the "nothing at my edge" block rather than to nothing
    at all, so every manifest this wizard writes says what is at the edge.
    """
    from puffpai import zone_bridge
    return zone_bridge.bridge_manifest_block(
        posture, run_egress=run_egress, environment=environment,
        chosen_by=chosen_by, edge=edge)


def derive_surfaces(proposal: dict[str, Any]) -> dict[str, Any]:
    """Propose the surface allowlist a build interview's matches imply."""
    from puffpai import build_interview as bi
    return bi.derive_surfaces(proposal)


def apply_surface_choices(derivation: dict[str, Any],
                          add: Optional[list[str]] = None,
                          remove: Optional[list[str]] = None) -> dict[str, Any]:
    """Apply the owner's additions and removals to a proposed allowlist."""
    from puffpai import build_interview as bi
    return bi.apply_surface_choices(derivation, add=add, remove=remove)


def surfaces_block(derivation: dict[str, Any]) -> dict[str, Any]:
    """The allowlist, shaped for the manifest's ``surfaces`` block."""
    from puffpai import build_interview as bi
    return bi.surfaces_manifest_block(derivation)


# ---------------------------------------------------------------------------
# Zone 2: where knowledge comes from, and what is waiting to be learned
#
# Three doors into the Brain zone and they are equals: the vetted online
# registry (:mod:`puffpai.source_registry` + :mod:`puffpai.source_fetch`), the
# owner's own folders (:mod:`puffpai.data_sources`), and whatever is already
# sitting in the knowledge folder. Same thin-wrapper discipline as everything
# above - these decide nothing, they give the CLI, the wizard packages and the
# /setup/* endpoints one shape to call.
#
# Two of these wrappers exist as much to *remove* an argument as to add one.
# ``fetch_source`` has no ``registry`` parameter and ``source_preview`` passes
# none, because that is the single argument that can widen what
# :mod:`puffpai.source_fetch` is allowed to reach. A front-end therefore has no
# way to supply it and always gets the shipped list.
#
# The three INDEX wrappers are HomeBotBuild's own. This build's knowledge store
# is the FTS5 shard set (:mod:`puffpai.knowledge.fts_index`), not BuildBot's
# ChromaDB pack index, so "waiting to be learned" is measured as "a .zim in the
# folder with no shard beside it" and there is no indexing-rate history to
# estimate from. The estimate is therefore always ``basis: unknown``, which is
# the honest answer rather than a number this build invented.
# ---------------------------------------------------------------------------

def source_search(goal: Optional[str]) -> dict[str, Any]:
    """Vetted sources matching a goal, plus the plain-words framing to show.

    Reads the shipped registry and opens no socket - this searches a curated
    list, not the web, and the result says so out loud. Never raises.
    """
    from puffpai import source_registry
    return source_registry.search_summary(goal)


def source_search_lines(result: dict[str, Any]) -> list[str]:
    """Plain-language lines for a :func:`source_search` result."""
    from puffpai import source_registry
    return source_registry.candidate_summary_lines(result)


def source_preview(source_id: str, posture: dict[str, Any],
                   dest_dir: Optional[str] = None) -> dict[str, Any]:
    """The consent screen: license, size, address, and where it would land.

    No network at all. :func:`fetch_source` refuses until a caller has been
    through here and says so, which is how "nothing bulky downloads without the
    size shown first" is enforced in code rather than trusted to a front-end.
    """
    from puffpai import source_fetch
    return source_fetch.preview(source_id, posture, None, dest_dir)


def fetch_source(source_id: str, posture: dict[str, Any], *,
                 size_acknowledged: bool = False,
                 dest_dir: Optional[str] = None) -> dict[str, Any]:
    """Download one vetted source. Refusals come back as records; never raises.

    ``size_acknowledged`` is the owner's word after reading
    :func:`source_preview`, and no front-end may set it on their behalf - a
    False here produces a refusal that names the size, which is the intended
    outcome rather than an error.

    This is the confirmed download of one registry row, so it goes through
    :func:`puffpai.source_fetch.fetch_registry_pack`: a Kiwix pack row does
    not read robots.txt, and every other guard still applies.
    """
    from puffpai import source_fetch
    if size_acknowledged:
        # Drawbridge's one policy, like every other book download.
        from puffpai.knowledge import find_more
        cand = find_more.look(source_id)
        blocked = find_more.drawbridge_block(cand, via="setup") if cand else None
        if blocked is not None:
            return {"ok": False, "status": "refused", "source_id": source_id,
                    "reason": str(blocked.get("line") or "It did not pass "
                                  "Drawbridge's checks, so it was not added."),
                    "drawbridge": blocked}
    return source_fetch.fetch_registry_pack(source_id, posture,
                                            size_acknowledged=size_acknowledged,
                                            dest_dir=dest_dir)


def fetch_source_lines(result: dict[str, Any]) -> list[str]:
    """Plain-language lines for a :func:`fetch_source` result. Metadata only."""
    from puffpai import source_fetch
    return source_fetch.fetch_summary_lines(result)


def source_manifest_entry(result: dict[str, Any]) -> dict[str, Any]:
    """One fetched source, shaped for the manifest. Metadata only, no path."""
    provenance = result.get("provenance") or {}
    return {
        "id": str(result.get("source_id") or ""),
        "label": str(provenance.get("label") or ""),
        "license": str(provenance.get("license") or ""),
        "bytes": int(result.get("bytes") or 0),
        "sha256": str(result.get("sha256") or ""),
    }


def data_location_suggestions() -> list[dict[str, Any]]:
    """Places worth offering the owner. Suggestions only - nothing is scanned."""
    from puffpai import data_sources
    return data_sources.suggested_locations()


def data_source_notes() -> dict[str, str]:
    """The three things the owner has to have read before they say yes."""
    from puffpai import data_sources
    return {
        "vault": data_sources.VAULT_NOTE,
        "consent": data_sources.CONSENT_NOTE,
        "record": data_sources.RECORD_NOTE,
    }


def survey_data_locations(paths: list[str]) -> dict[str, Any]:
    """Count what is in the folders the owner named. Opens no file."""
    from puffpai import data_sources
    return data_sources.survey(paths)


def data_survey_lines(surveyed: dict[str, Any]) -> list[str]:
    """Plain-language lines for a survey, ending in the three consent notes."""
    from puffpai import data_sources
    return data_sources.source_summary_lines(surveyed)


def approve_data_locations(surveyed: dict[str, Any],
                           approve: Optional[list[str]] = None) -> dict[str, Any]:
    """Record the per-location yeses. Everything unnamed is declined."""
    from puffpai import data_sources
    return data_sources.apply_choices(surveyed, approve)


def data_sources_block(selection: dict[str, Any]) -> dict[str, Any]:
    """The approved locations, shaped for the manifest.

    Redacted folder paths and counts by file type, nothing else - no filename,
    no subdirectory, no contents. A declined location leaves no trace at all.
    """
    from puffpai import data_sources
    return data_sources.sources_manifest_block(selection)


def pending_index_volume(zim_dir: str) -> dict[str, Any]:
    """How much unlearned material is waiting in the knowledge folder.

    HomeBotBuild's own shape of the question: a pack is "learned" when it has an
    FTS shard beside it (:func:`puffpai.knowledge.fts_index.shard_path_for`).
    Reads directory entries only - nothing is opened and nothing is indexed
    here. A missing or empty folder is a volume of zero packs, not an error.
    """
    from puffpai.knowledge import fts_index
    from puffpai.knowledge.discovery import discover_zim_files

    try:
        paths = discover_zim_files(zim_dirs=[zim_dir]) if zim_dir else []
    except Exception:  # noqa: BLE001 - an unreadable folder is "nothing waiting"
        return {"packs": 0, "filenames": [], "entries": 0}
    try:
        fts_dir = fts_index.resolve_fts_dir()
    except Exception:  # noqa: BLE001
        fts_dir = ""
    waiting: list[str] = []
    for path in paths:
        shard = ""
        if fts_dir:
            try:
                shard = fts_index.shard_path_for(path, fts_dir)
            except Exception:  # noqa: BLE001
                shard = ""
        if shard and os.path.exists(shard):
            continue
        waiting.append(os.path.basename(path))
    return {"packs": len(waiting), "filenames": waiting, "entries": 0}


def index_estimate_for(volume: dict[str, Any]) -> dict[str, Any]:
    """How long this build says learning ``volume`` will take: it does not know.

    Measured from this box's own indexing history and from nothing else, and
    this build keeps no such history, so the answer is ``basis: unknown``
    carrying no duration at all. A wizard that invents a number here is a wizard
    that lies to a person watching a progress bar.
    """
    packs = int((volume or {}).get("packs") or 0)
    return {
        "basis": "unknown",
        "packs": packs,
        "central_seconds": None,
        "basis_note": "This build has no indexing history to measure, so it "
                      "will not guess how long learning takes.",
        "notes": [],
    }


def start_indexing(zim_dir: str) -> int:
    """Hand the waiting packs to the background indexer; return the pack count.

    HomeBotBuild indexes ZIM packs with the FTS builder, which is a deliberate,
    resumable foreground command (``puffpai-fts-build``) rather than a thread
    that starts itself during setup - a multi-hour index begun behind the
    owner's back on a small box is how a wizard ends with an unusable machine.
    So nothing is started here and the honest answer is zero packs started; the
    Integrate card records what is waiting and the command that learns it.
    """
    return 0


# ---------------------------------------------------------------------------
# HomeBot: persona, storage, household profiles, Home Assistant
#
# This wizard is HomeBot's, so the persona is not a question the owner has to
# answer from a blank field - it is a default they may change. Everything below
# is the HomeBotBuild-specific half of the wizard, and it is what the /setup/*
# endpoints and the dashboard onboarding should call.
# ---------------------------------------------------------------------------

#: The persona this build ships. A default, never a lock: the owner renames the
#: bot at the identity step and the manifest records what they chose.
HOMEBOT_DEFAULTS: dict[str, Any] = {
    "persona": "homebot",
    "bot_name": "HomeBot",
    "pack": "homebot",
    "goal": ("Run our household: the calendar, meals, reminders, the lights, "
             "and answering the family's questions offline."),
}

#: The surfaces a household build starts with when a goal matched nothing. NOT
#: a ceiling and NOT a silent default: the derivation records ``defaulted``, and
#: the owner edits the list at the surfaces step. An empty allowlist would be
#: this codebase's recurring bug - absence rendered as limitation - so a build
#: with no stated goal ships the household surfaces rather than a bot that
#: refuses everything because nobody told it anything.
HOMEBOT_DEFAULT_SURFACES: tuple[str, ...] = (
    "house.lights",
    "house.climate",
    "house.sensors",
    "comms.calendar",
    "comms.timers",
    "daily.kitchen",
)

#: Why each default surface is on the list, in the owner's words.
_DEFAULT_SURFACE_REASONS: dict[str, str] = {
    "house.lights": "the lights and switches, because HomeBot runs a home",
    "house.climate": "heating and cooling, because HomeBot runs a home",
    "house.sensors": "door, motion and temperature sensors, because HomeBot runs a home",
    "comms.calendar": "the family calendar, because HomeBot runs a household",
    "comms.timers": "timers and reminders, because HomeBot runs a household",
    "daily.kitchen": "meals and the shopping list, because HomeBot runs a household",
}


def homebot_defaults() -> dict[str, Any]:
    """The preselected persona, name, pack and goal. All of it editable."""
    return dict(HOMEBOT_DEFAULTS)


def set_identity(bot_name: str) -> str:
    """Persist the bot's name. Returns the config path.

    Writes ``identity`` in the main config, which is where this build reads the
    name it answers to (and what the dashboard prints).
    """
    name = str(bot_name or "").strip() or str(HOMEBOT_DEFAULTS["bot_name"])
    return write_main_config({
        "identity": {"name": name, "avatar_seed": name},
    })


def default_surfaces_derivation(goal: str = "") -> dict[str, Any]:
    """The household allowlist, shaped like a :func:`derive_surfaces` result.

    Used when a goal matched nothing. ``defaulted`` says so out loud, so a
    summary can distinguish "nobody configured this" from "configured to zero".
    """
    from puffpai import build_interview as bi
    taxonomy = bi.surface_taxonomy()
    surfaces = [s for s in HOMEBOT_DEFAULT_SURFACES if s in taxonomy]
    return {
        "goal": str(goal or ""),
        "surfaces": surfaces,
        "reasons": {s: _DEFAULT_SURFACE_REASONS.get(s, "a household surface")
                    for s in surfaces},
        "sensitive": [s for s in surfaces
                      if bool(taxonomy.get(s, {}).get("sensitive"))],
        "defaulted": True,
        "owner_edited": False,
        "message": ("No goal was given, so this build starts with the household "
                    "surfaces. You can add or remove any of them."),
    }


def ensure_surfaces(derivation: dict[str, Any],
                    goal: str = "") -> dict[str, Any]:
    """Never hand back an EMPTY allowlist that nobody asked for.

    A derivation with surfaces is returned untouched. One with none - because
    the goal matched nothing - is replaced by the household defaults, marked
    ``defaulted``. An owner who removed every surface themselves
    (``owner_edited``) is left alone: that is a configured zero, and overriding
    it would be the wizard arguing with a person.
    """
    if (derivation or {}).get("surfaces"):
        return derivation
    if (derivation or {}).get("owner_edited"):
        return derivation
    return default_surfaces_derivation(goal or (derivation or {}).get("goal") or "")


# --- storage ---------------------------------------------------------------

#: The four directories the storage step asks about, with the config key each
#: one lands on. ``media`` may be left empty - a build with no media library is
#: a normal build, not a broken one.
STORAGE_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("state_dir", "Where this build keeps its own files (logs, indexes, state)",
     "paths.state_dir"),
    ("knowledge_dir", "Where knowledge packs (ZIM files) live",
     "paths.zim_dirs"),
    ("vault_dir", "Where the Vault's encrypted files live",
     "paths.vault_data_dir"),
    ("media_dir", "Your media library, if you have one (may be left empty)",
     "media_server.media_mount"),
)


def storage_defaults() -> dict[str, str]:
    """Default storage paths, all under this install's data dir."""
    cfg = get_config()
    root = cfg.DATA_DIR
    return {
        "state_dir": root,
        "knowledge_dir": default_pack_dir(),
        "vault_dir": _vault_dir(),
        "media_dir": "",
    }


def validate_storage_path(path: str, *, allow_empty: bool = False
                          ) -> tuple[bool, str]:
    """Is this a path this build can actually write to? ``(ok, reason)``.

    Accepts an existing writable directory, or one whose nearest existing parent
    is writable (so "I will put it here" works before the folder exists). A
    relative path is refused: the owner typed it in one directory and the service
    runs in another, and a relative answer would quietly mean two places.
    """
    text = str(path or "").strip()
    if not text:
        if allow_empty:
            return True, ""
        return False, "This one needs a path."
    text = os.path.expanduser(text)
    if not os.path.isabs(text):
        return False, ("Use a full path (it has to mean the same thing from "
                       "wherever this runs).")
    if os.path.exists(text):
        if not os.path.isdir(text):
            return False, "That path is a file, not a folder."
        return (True, "") if os.access(text, os.W_OK) else (
            False, "That folder is there, but this build cannot write to it.")
    parent = os.path.dirname(text.rstrip(os.sep)) or text
    while parent and not os.path.exists(parent):
        nxt = os.path.dirname(parent)
        if nxt == parent:
            break
        parent = nxt
    if parent and os.path.isdir(parent) and os.access(parent, os.W_OK):
        return True, ""
    return False, "Nothing this build can write to exists above that path."


def apply_storage(paths: dict[str, str], *, create: bool = True) -> dict[str, Any]:
    """Validate, create and persist the storage answers.

    Returns ``{"ok", "paths", "problems", "config_path"}``. Writes nothing when
    any answer fails validation - a half-applied storage layout is worse than a
    refused one. The keys written are the ones this codebase already relocates
    (:data:`puffpai.config.RELOCATABLE_PATH_KEYS`), so an absolute answer here
    is honoured verbatim by every reader.
    """
    answers = {key: str((paths or {}).get(key) or "").strip()
               for key, _label, _cfg in STORAGE_FIELDS}
    problems: dict[str, str] = {}
    for key, _label, _cfg in STORAGE_FIELDS:
        ok, reason = validate_storage_path(
            answers[key], allow_empty=(key == "media_dir"))
        if not ok:
            problems[key] = reason
    if problems:
        return {"ok": False, "paths": answers, "problems": problems,
                "config_path": ""}

    if create:
        for key, value in answers.items():
            if value:
                try:
                    os.makedirs(os.path.expanduser(value), exist_ok=True)
                except OSError as exc:
                    problems[key] = f"Could not create that folder: {exc}"
        if problems:
            return {"ok": False, "paths": answers, "problems": problems,
                    "config_path": ""}

    values: dict[str, Any] = {
        "paths": {
            "state_dir": answers["state_dir"],
            "vault_data_dir": answers["vault_dir"],
            "zim_dirs": [answers["knowledge_dir"]] if answers["knowledge_dir"] else [],
        },
    }
    if answers["media_dir"]:
        values["media_server"] = {"media_mount": answers["media_dir"]}
    path = write_main_config(values)
    return {"ok": True, "paths": answers, "problems": {}, "config_path": path}


def storage_block(paths: dict[str, str]) -> dict[str, Any]:
    """The storage answers, shaped for the manifest's ``storage`` block.

    Paths, not contents: the manifest records WHERE this build keeps things, so
    a rebuild lands in the same places. ``media_library`` may be empty, and an
    empty value means "this household has none", which is recorded as a stated
    absence rather than left out.
    """
    answers = paths or {}
    return {
        "state_dir": str(answers.get("state_dir") or ""),
        "knowledge_dir": str(answers.get("knowledge_dir") or ""),
        "vault_dir": str(answers.get("vault_dir") or ""),
        "media_library": str(answers.get("media_dir") or ""),
        "media_library_present": bool(str(answers.get("media_dir") or "").strip()),
    }


# --- household profiles ----------------------------------------------------

def household_status() -> dict[str, Any]:
    """Who is in the household roster right now. Never raises."""
    try:
        from puffpai import household
        members = household.members()
        return {
            "count": len(members),
            "owner": next((m["display_name"] for m in members
                           if m.get("role") == "owner"), ""),
            "names": [m["display_name"] for m in members],
        }
    except Exception as exc:  # noqa: BLE001 - a roster that will not load is not a crash
        return {"count": 0, "owner": "", "names": [], "error": str(exc)}


def create_household(owner_name: str,
                     others: Optional[list[dict[str, str]]] = None
                     ) -> dict[str, Any]:
    """Create the owner profile (and any others named), in the household store.

    The owner is whoever the first row is: this build's
    :mod:`puffpai.household` promotes the oldest profile to ``owner`` and
    refuses to let anything else claim the role, so the wizard creates the owner
    by creating the first member. Later members are ``adult`` unless a role is
    given. Returns the same shape as :func:`household_status`.
    """
    from puffpai import household

    name = str(owner_name or "").strip()
    existing = household.members()
    if name:
        if not existing:
            household.create_member(name)
        else:
            household.set_owner_name(name)
    for entry in others or []:
        label = str((entry or {}).get("name") or "").strip()
        if not label:
            continue
        try:
            household.create_member(label, (entry or {}).get("role") or "adult")
        except Exception:  # noqa: BLE001 - a duplicate name is not a failed setup
            continue
    return household_status()


def household_block(status: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """The household, shaped for the manifest. Names only - no avatars, no data."""
    state = status if status is not None else household_status()
    return {
        "profiles": int(state.get("count") or 0),
        "owner_set": bool(state.get("owner")),
    }


# --- Home Assistant, which is optional -------------------------------------

#: Offered as the address to try first. Home Assistant is OPTIONAL in this
#: build: a household without it is a complete build, not a degraded one.
DEFAULT_HA_URL = "http://homeassistant.local:8123"


def ha_block(present: bool, url: str = "", token_configured: bool = False,
             checked: Optional[bool] = None, reachable: Optional[bool] = None
             ) -> dict[str, Any]:
    """The Home Assistant answer, shaped for the manifest.

    Records presence, the address, and WHETHER a token was placed - never the
    token. ``present=False`` is a stated absence and the wizard says nothing
    further about it.
    """
    return {
        "present": bool(present),
        "url": str(url or "") if present else "",
        "token_configured": bool(token_configured) if present else False,
        "connectivity_checked": bool(checked) if checked is not None else False,
        "reachable": reachable if present and checked else None,
    }


def integrations_block(ha: dict[str, Any],
                       media_library: bool = False) -> dict[str, Any]:
    """The manifest's ``integrations`` block. Carries no secret of any kind."""
    return {
        "home_assistant": dict(ha or {}),
        "media_library": {"present": bool(media_library)},
    }


# --- Bridge posture that needs a file the wizard does not own ---------------

def pending_apply_path() -> str:
    """Where a not-yet-applied build decision is recorded."""
    cfg = get_config()
    return os.path.join(cfg.DATA_DIR, "data", "setup_pending_apply.json")


def record_pending_apply(kind: str, detail: dict[str, Any]) -> str:
    """Record a decision this wizard made but could not apply itself.

    The Bridge step writes the enumerated door table into the config the runtime
    reads (:func:`persist_posture`). Anything BEYOND that - a consent tier that
    would need the consent gate's own config, or a Zone 3 sinkhole rule - is not
    the wizard's to write, so it is recorded here and reported instead of being
    half-applied. Returns the file path. Never carries a secret.
    """
    record = _read_json(pending_apply_path())
    items = record.get("pending")
    if not isinstance(items, list):
        items = []
    items = [i for i in items if isinstance(i, dict) and i.get("kind") != kind]
    items.append({"kind": str(kind), "detail": dict(detail or {}),
                  "recorded_at": time.time()})
    record["pending"] = items
    path = pending_apply_path()
    _write_json(path, record)
    return path


def pending_apply() -> list[dict[str, Any]]:
    """Decisions recorded by :func:`record_pending_apply`, oldest first."""
    items = _read_json(pending_apply_path()).get("pending")
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


# The configs whose degradation changes *what the build is* rather than how well
# it is tuned. A fallback capability catalog builds somebody a different bot; a
# fallback model catalog offers them a different brain. Those two are worth
# stopping for. A fallback model profile or rulepack degrades the tuning of the
# build the owner asked for, which is worth saying and not worth a gate.
IDENTITY_CONFIGS = ("capability_catalog", "model_catalog")


def config_health_status() -> dict[str, Any]:
    """What the first-party configs did on this run, and whether it matters.

    ``blocking`` names the degraded configs from :data:`IDENTITY_CONFIGS`; when
    it is non-empty the wizard asks for an explicit acknowledgment before it
    commits a manifest. It never refuses: an owner with a corrupt file and no
    internet has to be able to finish, and an acknowledged, recorded degradation
    beats a dead end. The manifest records the degradation either way.
    """
    from puffpai import config_health
    events = config_health.report(degraded_only=True)
    configs = sorted({str(e.get("config")) for e in events})
    return {
        "degraded": config_health.is_degraded(),
        "configs": configs,
        "blocking": [c for c in configs if c in IDENTITY_CONFIGS],
        "reduced": any(e.get("reduced") for e in events),
        "summary": config_health.summary_lines(),
        "events": events,
    }


def manifest_status() -> dict[str, Any]:
    """Whether a build manifest exists yet, and a short summary if so."""
    from puffpai import build_manifest
    existing = build_manifest.read_manifest()
    if not existing:
        return {"exists": False, "summary": []}
    return {"exists": True, "summary": build_manifest.manifest_summary_lines(existing)}


# ---------------------------------------------------------------------------
# completion flag + status
# ---------------------------------------------------------------------------

def mark_complete() -> None:
    """Record that first-run setup has been completed, and retire its code.

    Every way setup finishes comes through here (``POST /setup/complete``, the
    ``puffpai setup`` CLI, the card wizard's manifest step), so this is the one
    place the console-only first-run code is removed. Left on disk it would be
    a credential that opens nothing today and would spring back to life if the
    completion flag were ever cleared. The flag is written first: once it is,
    ``setup_code.is_required()`` is False and the code is refused regardless.
    """
    _write_json(_setup_flag_path(), {"complete": True, "completed_at": time.time()})
    try:
        from puffpai.security import setup_code
        setup_code.destroy()
    except Exception as exc:  # noqa: BLE001 - completion must not fail on this
        import logging
        logging.getLogger(__name__).warning(
            "could not remove the first-run setup code: %s", exc)


def clear_complete() -> None:
    """Clear the setup-complete flag (used to re-run the wizard / in tests)."""
    try:
        os.remove(_setup_flag_path())
    except FileNotFoundError:
        pass


def is_complete() -> bool:
    """True once :func:`mark_complete` has been called for this data dir."""
    return bool(_read_json(_setup_flag_path()).get("complete"))


def setup_status(probe_ollama: bool = True) -> dict[str, Any]:
    """Summarise which setup steps are done - for the wizard and first-run gate.

    Contains no secrets: never the vault password, never the HA token, never a
    vault PIN or recovery code - the zone blocks it reports are status-only by
    construction. Set ``probe_ollama`` False to skip the network round-trip (the
    first-run gate only needs ``complete``).

    Alongside the v1 keys it carries what the v2 wizard decided, read back from
    the committed manifest: the build posture, the three zones, the surface
    allowlist, and whether any first-party config was degraded when this build
    was assembled.
    """
    from puffpai import build_manifest

    cfg = get_config(_force_reload=True)

    if probe_ollama:
        ollama = check_ollama()
    else:
        ollama = {"reachable": None, "models": [], "selected": cfg.ollama.model,
                  "url": cfg.ollama.url}

    manifest = build_manifest.read_manifest() or {}
    zim_dirs = list(cfg.paths.zim_dirs or [])
    knowledge = {
        "zim_dir": zim_dirs[0] if zim_dirs else "",
        "zim_dirs": zim_dirs,
    }
    recorded_vault = manifest.get("vault")
    zone_vault_state: dict[str, Any] = dict(recorded_vault) if isinstance(recorded_vault, dict) else {}
    # The manifest says what the wizard did; the disk says what is there now.
    zone_vault_state["initialized"] = vault_initialized()

    return {
        "ollama": ollama,
        "vault": {"initialized": vault_initialized()},
        "ha": {"configured": ha_configured(), "url": ha_url()},
        "knowledge": knowledge,
        "posture": manifest.get("posture"),
        "zones": {
            "vault": zone_vault_state,
            "brain": knowledge,
            "bridge": manifest.get("bridge"),
        },
        "surfaces": manifest.get("surfaces"),
        "household": household_status(),
        "storage": manifest.get("storage"),
        "integrations": manifest.get("integrations"),
        "identity": {"name": cfg.identity.name},
        "pending_apply": pending_apply(),
        "config_health": config_health_status(),
        "manifest": manifest_status(),
        "complete": is_complete(),
    }


__all__ = [
    "list_models",
    "find_ollama",
    "brain_status",
    "check_ollama",
    "write_main_config",
    "set_ollama_config",
    "create_vault",
    "vault_initialized",
    "set_ha_config",
    "test_ha_connection",
    "ha_configured",
    "ha_url",
    "set_knowledge_config",
    "embedding_options",
    "set_embedding_config",
    "default_pack_dir",
    "hardware_census",
    "brain_menu",
    "crew_budget",
    "build_interview",
    "preview_manifest",
    "commit_manifest",
    "manifest_status",
    "export_build_bundle",
    "verify_build_bundle",
    "import_build_bundle",
    "posture_options",
    "assess_posture",
    "posture_gates",
    "posture_block",
    "validate_vault_pin",
    "vault_recovery_notice",
    "confirm_vault_recovery",
    "seal_vault_with_recovery",
    "rotate_vault_recovery",
    "vault_block",
    "bridge_options",
    "reconcile_bridge",
    "bridge_block",
    "zone3_support",
    "edge_options",
    "validate_edge_url",
    "edge_block",
    "record_zone3_pending",
    "derive_surfaces",
    "apply_surface_choices",
    "surfaces_block",
    "HOMEBOT_DEFAULTS",
    "HOMEBOT_DEFAULT_SURFACES",
    "homebot_defaults",
    "set_identity",
    "default_surfaces_derivation",
    "ensure_surfaces",
    "STORAGE_FIELDS",
    "storage_defaults",
    "validate_storage_path",
    "apply_storage",
    "storage_block",
    "household_status",
    "create_household",
    "household_block",
    "DEFAULT_HA_URL",
    "ha_block",
    "integrations_block",
    "record_pending_apply",
    "pending_apply",
    "IDENTITY_CONFIGS",
    "config_health_status",
    "mark_complete",
    "clear_complete",
    "is_complete",
    "setup_status",
]
