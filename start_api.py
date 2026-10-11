"""PuffPAI backend entrypoint — the one systemd (puffpai-api.service) runs.

Logging is configured HERE because this is the only entrypoint on the Linux
appliance path. ``puffpai.service.run_service()`` calls ``setup_logging()`` for
the Windows scheduled-task path only. Without the call below the ``puffpai``
logger has level NOTSET and zero handlers, so every ``logger.info()`` in the
backend is discarded and only WARNING+ escapes via ``logging.lastResort``. Werkzeug access lines still show
up in journald because werkzeug configures its own logger.
"""

import sys, os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_STATE_DIR = os.path.join(_REPO_ROOT, "data")

#: Overrides the default INFO level, e.g. Environment=PUFFPAI_LOG_LEVEL=DEBUG.
_LEVEL_ENV = "PUFFPAI_LOG_LEVEL"


def _resolve_log_dir() -> str:
    """Directory for the rotating log file, under the WRITABLE state root.

    Must go through :func:`puffpai.config.resolve_state_dir` like every other
    module that writes state. The unit runs ProtectSystem=strict with
    ReadWritePaths=/data/puffpai, so a package-relative path here would fail
    with EROFS on the appliance while looking fine in a dev checkout.
    """
    try:
        from puffpai.config import get_config, resolve_state_dir
        cfg = get_config()
        state_dir = resolve_state_dir(
            cfg.paths.state_dir,
            repo_root=cfg.REPO_ROOT,
            default=_DEFAULT_STATE_DIR,
        )
    except Exception:
        # Logging must never be what stops the service booting, and this runs
        # before any handler exists, so there is nowhere to report it yet.
        state_dir = os.environ.get(
            "PUFFPAI_DATA_DIR", "").strip() or _DEFAULT_STATE_DIR
    return os.path.join(state_dir, "logs")


#: The bind classifier, and the sentence that goes with it, live in
#: ``puffpai.security.netaddr`` so this file and ``api_server``'s ``__main__``
#: cannot disagree about which binds are safe.
from puffpai.security.netaddr import (           # noqa: E402
    NON_LOOPBACK_BIND_WARNING, is_loopback_bind)


def _restrict(path: str, mode: int) -> None:
    """Best-effort tighten of permissions; never fatal.

    The log is not a transcript-free file: INFO carries LLM reply fragments,
    uploaded filenames and HA device names, so it should not be world-readable
    even though /data/puffpai is already 0750. A no-op on Windows, where chmod
    only toggles the read-only bit.
    """
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def configure_logging():
    """Attach console + rotating-file handlers to the ``puffpai`` logger.

    Returns the log file path actually configured, or None if only the console
    handler could be set up (unwritable directory — the service still runs and
    journald still receives everything).
    """
    from puffpai.logging_config import setup_logging

    level = os.environ.get(_LEVEL_ENV, "").strip() or "INFO"

    log_file = None
    log_dir = _resolve_log_dir()
    try:
        os.makedirs(log_dir, exist_ok=True)
        _restrict(log_dir, 0o750)
        log_file = os.path.join(log_dir, "puffpai.log")
    except OSError:
        # setup_logging() degrades to console-only; stderr goes to journald,
        # so we lose the on-disk copy but not the diagnostics.
        pass

    setup_logging(level=level, log_file=log_file)

    # setup_logging() swallows a file-handler failure to keep the console
    # alive, so confirm rather than assume the file is really there before
    # reporting it. Rotated copies inherit their mode from the unit's UMask,
    # which is why puffpai-api.service sets it too — this chmod covers only
    # the file we just created (and the dev case, which has no unit).
    if log_file and os.path.exists(log_file):
        _restrict(log_file, 0o640)
    else:
        log_file = None
    return log_file


def _load_install_env():
    """Load ``<state>/homebot.env`` BEFORE anything imports ``api_server``.

    ``install.sh`` / ``install.ps1`` mint ``<state>/homebot.env`` (0600) holding
    the API key and then close with ``python start_api.py``. ``api_server``
    resolves the key at IMPORT time and treats a miss as fatal (deliberately:
    silently minting a replacement breaks every paired client), so without this
    loader the documented first command dies with a RuntimeError.

    ORDER MATTERS. This must run before the ``from puffpai.api_server import``
    below, which is why it is a separate step at the top of main() rather than
    something the config layer does lazily.

    On Linux the systemd unit carries ``EnvironmentFile=-<state>/
    homebot.env``; those variables are therefore already in ``os.environ`` and
    :func:`puffpai.envfile.load_state_env` will NOT override them. The Windows
    Task Scheduler recipe needs no ``setx`` for the key either — the task
    inherits nothing but the user environment and this loader supplies the rest.
    (``setx PUFFPAI_DATA_DIR`` is still needed when the state directory is NOT
    the default, because that is the variable that says WHERE to find the file.)

    Returns ``(path, names)``; never raises.
    """
    try:
        from puffpai.envfile import load_state_env
        return load_state_env()
    except Exception as exc:               # a bad env file must not block boot
        print(f"[Startup] env file not loaded ({exc}); "
              f"relying on the ambient environment")
        return "", []


def main():
    env_path, env_names = _load_install_env()
    log_file = configure_logging()

    from puffpai.logging_config import get_logger
    logger = get_logger(__name__)
    logger.info(
        "PuffPAI API starting (log level %s, file %s)",
        os.environ.get(_LEVEL_ENV, "").strip() or "INFO",
        log_file or "disabled",
    )
    # NAMES only, never values. "which variables did this file actually
    # supply" is the first question when a keyed client gets a 401.
    if env_names:
        logger.info("env file %s supplied: %s", env_path, ", ".join(env_names))
    else:
        logger.info("env file %s supplied nothing (absent, empty, or every "
                    "variable already set in the environment)", env_path)

    from puffpai.api_server import app, start_background_services

    # WHICH tier of the secrets chain answered for the API key — the name of
    # the source, never the key. A stale OS-keychain entry from another install
    # can shadow the key the installer minted; without this line every client
    # using the documented value gets a 401 with nothing on the console to
    # explain it.
    try:
        from puffpai.api_server import api_key_source
        source = api_key_source()
    except Exception:
        source = "unknown"
    logger.info("API key source: %s", source)
    print(f"[Security] API key source: {source}")

    # The first-run setup code, if setup is not finished. Printed here because
    # the console is the one channel a person standing at the box definitely
    # has, and it is the ONLY place the code is shown.
    try:
        from puffpai.security import setup_code
        setup_code.announce(print_line=print)
    except Exception as exc:
        logger.warning("first-run setup code unavailable: %s", exc)
    start_background_services()  # Sentinel + warm-up + chat history

    # Bind address. DEFAULT IS LOOPBACK.
    #
    # This build ships no host packet filter, so whatever this binds to is the
    # real boundary — the API key is the only thing in front of the port, and
    # HomeBotBuild installs on arbitrary hardware: laptops on café wifi, a
    # spare desktop on a flat-share LAN, a machine with no router in front of
    # it at all. 0.0.0.0 on that population offers the household's whole
    # backend to every peer on the network and leans entirely on one shared
    # secret. Loopback is the posture that is right for most installs, so it
    # is the one that is free.
    #
    # Nothing in the DEFAULT install needs more:
    #
    #   * the dashboard  — browser talks to the Next server, which proxies
    #     server-side to http://localhost:8000
    #     (dashboard/app/api/pai/[...path]/route.ts);
    #   * the kiosk      — same host, same proxy;
    #   * the voice service — same host, loopback.
    #
    # Three optional deployments DO need a wider bind. Each gets the flag
    # rather than the default:
    #
    #   * Node-RED in Docker. The container reaches the host through
    #     host.docker.internal / the host-gateway address, which is NOT
    #     loopback (docker-compose.nodered.yml:49). Bind to the docker bridge
    #     address — PUFFPAI_API_BIND=172.17.0.1 — rather than to 0.0.0.0, so
    #     the port is offered to containers and not to the LAN.
    #   * Home Assistant on ANOTHER box, using the Ollama/OpenAI conversation
    #     endpoints. Set PUFFPAI_API_BIND to this box's LAN address, and list
    #     HA's IP in $PUFFPAI_TRUSTED_LOCAL_PEERS (the variable named by
    #     endpoint_gate.TRUSTED_PEERS_ENV, security/endpoint_gate.py:82) —
    #     HA's integration cannot attach our API key.
    #   * Any deliberately off-box client (a phone browser hitting the backend
    #     directly rather than the dashboard). Same flag; give it the key.
    #
    # HA WEBHOOKS do not need this: they are inbound to Home Assistant, not to
    # us. Prefer a specific interface address to 0.0.0.0 in every case above.
    host = os.environ.get("PUFFPAI_API_BIND", "").strip()
    if not host:
        try:
            from puffpai.config import get_config
            host = (get_config().api_server.host or "").strip()
        except Exception:
            host = ""
    host = host or "127.0.0.1"
    # Port follows config (api_server.port), with $PUFFPAI_API_PORT on top of
    # it. 8000 is a busy port on a general-purpose machine — this is a
    # portability requirement, not a preference: with a hard-coded port a
    # second install on the same box could never start.
    port = 8000
    try:
        from puffpai.config import get_config
        port = int(get_config().api_server.port)
    except Exception:
        pass
    env_port = os.environ.get("PUFFPAI_API_PORT", "").strip()
    if env_port:
        try:
            port = int(env_port)
        except ValueError:
            logger.warning("PUFFPAI_API_PORT=%r is not a number; using %d",
                           env_port, port)
    if not is_loopback_bind(host):
        # One loud line, every boot, naming the exact exposure. This build has
        # no host packet filter, so a non-loopback bind means the API key is
        # the only control in front of the port. The sentence itself lives in
        # security/netaddr.py, shared with api_server's __main__ block: "NOT
        # loopback" must mean the same thing however the backend was started.
        logger.warning(NON_LOOPBACK_BIND_WARNING, host, port)
    logger.info("PuffPAI API listening on %s:%d", host, port)
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
