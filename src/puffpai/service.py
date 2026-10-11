"""
PuffPAI Background Service — Runs the AI engine independently of the GUI.

The service hosts the Flask API server, inference engine, knowledge base,
vault, and all automation hooks. The desktop UI connects to this service
via HTTP on localhost:8000.

Usage:
    # Run directly:
    python -m puffpai.service

    # Or via the entry point:
    puffpai-service

    # Or as a Windows service via NSSM:
    nssm install PuffPAI-Backend <python> -m puffpai.service

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import os
import sys
import signal
import threading
import time
import logging

# Ensure puffpai package is importable
_src_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _src_parent not in sys.path:
    sys.path.insert(0, _src_parent)

from puffpai.logging_config import setup_logging, get_logger

logger = get_logger(__name__)

# ── Service state ────────────────────────────────────────────────────────

_shutdown_event = threading.Event()
_service_started = threading.Event()


def _signal_handler(signum, frame):
    """Handle shutdown signals gracefully."""
    logger.info("Received signal %s, shutting down...", signum)
    _shutdown_event.set()


def _check_port_available(host: str, port: int) -> bool:
    """Check if the port is available for binding."""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            s.bind((host, port))
            return True
    except OSError:
        return False


def _check_existing_service(host: str, port: int) -> bool:
    """Check if a PuffPAI service is already running."""
    import requests
    try:
        resp = requests.get(f"http://127.0.0.1:{port}/health", timeout=2)
        return resp.status_code == 200
    except Exception:
        return False


def _write_pid_file(pid_path: str):
    """Write the current process PID to a file."""
    os.makedirs(os.path.dirname(pid_path), exist_ok=True)
    with open(pid_path, 'w') as f:
        f.write(str(os.getpid()))


def _remove_pid_file(pid_path: str):
    """Remove the PID file on shutdown."""
    try:
        os.remove(pid_path)
    except OSError:
        pass


def run_service(*, host: str | None = None, port: int | None = None,
                debug: bool = False):
    """Start the PuffPAI background service.

    This function blocks until a shutdown signal is received.
    """
    # The install's homebot.env FIRST, before anything imports api_server:
    # api_server resolves the API key at import time and a miss is fatal, so
    # the file the installer minted has to already be in os.environ. Same call
    # start_api.py makes; see puffpai.envfile for the never-override rule.
    from puffpai.envfile import load_state_env
    load_state_env()

    # Rotating file log alongside console: voice/chat failures were
    # undebuggable after the fact with console-only output (the service
    # runs headless as a scheduled task). Anchored to the repo root like
    # the PID file below — the task's cwd is not guaranteed.
    _log_root = os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))
    _log_dir = os.path.join(_log_root, "data")
    os.makedirs(_log_dir, exist_ok=True)
    setup_logging(log_file=os.path.join(_log_dir, "puffpai.log"))

    from puffpai.config import get_config
    cfg = get_config()

    host = host or cfg.api_server.host
    port = port or int(os.environ.get("PORT", cfg.api_server.port))

    # ── PID file ──────────────────────────────────────────────────────
    _repo_root = os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))
    pid_path = os.path.join(_repo_root, "data", "puffpai_service.pid")

    # ── Check for existing service ────────────────────────────────────
    if _check_existing_service(host, port):
        logger.info("PuffPAI service already running on port %d", port)
        print(f"[Service] PuffPAI is already running on port {port}")
        return

    if not _check_port_available(host if host != "0.0.0.0" else "127.0.0.1", port):
        logger.error("Port %d is in use by another process", port)
        print(f"[Service] ERROR: Port {port} is in use. Stop the other process first.")
        return

    # ── Signal handlers ───────────────────────────────────────────────
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # ── Write PID ─────────────────────────────────────────────────────
    _write_pid_file(pid_path)

    # ── Start Flask ───────────────────────────────────────────────────
    from puffpai.api_server import app

    print("=" * 60)
    print("  PuffPAI Background Service")
    print("=" * 60)
    print(f"  API Server:  http://127.0.0.1:{port}")
    print(f"  Ollama:      {cfg.ollama.url} ({cfg.ollama.model})")
    print(f"  AI Identity: {cfg.identity.name}")
    print(f"  Node-RED:    {cfg.nodered.url}")
    print(f"  HA:          {'Enabled' if cfg.ha.enabled else 'Disabled'}")
    print(f"  PID:         {os.getpid()}")
    print(f"  PID File:    {pid_path}")
    print("=" * 60)
    print()
    print("The AI engine is running. Close the UI without stopping the AI.")
    print("Press Ctrl+C to stop the service.")
    print()

    _service_started.set()

    # Service mode must start the Device Sentinel (and warm-up/chat-history)
    # too; that logic otherwise runs only in api_server's __main__ path, and
    # quarantine would be silently absent here.
    try:
        from puffpai.api_server import start_background_services
        start_background_services()
    except Exception as exc:
        logger.error("Background services failed to start: %s", exc)
        print(f"[Service] WARNING: background services failed: {exc}")

    try:
        app.run(
            host=host,
            port=port,
            debug=debug,
            threaded=True,
            use_reloader=False,  # Don't reload in service mode
        )
    except Exception as e:
        logger.error("Service error: %s", e)
    finally:
        _remove_pid_file(pid_path)
        logger.info("PuffPAI service stopped")
        print("\n[Service] PuffPAI service stopped.")


def is_service_running(port: int = 8000) -> bool:
    """Check if the PuffPAI background service is reachable."""
    return _check_existing_service("127.0.0.1", port)


def start_service_background(port: int = 8000) -> threading.Thread | None:
    """Start the service in a background thread (for use by the UI).

    Returns the thread object, or None if the service is already running.
    """
    if is_service_running(port):
        logger.info("Service already running on port %d", port)
        return None

    thread = threading.Thread(
        target=run_service,
        kwargs={"port": port},
        daemon=True,
        name="PuffPAI-Service",
    )
    thread.start()

    # Wait up to 10 seconds for the service to start
    for _ in range(20):
        if is_service_running(port):
            logger.info("Background service started successfully")
            return thread
        time.sleep(0.5)

    logger.warning("Service may not have started within timeout")
    return thread


# ── CLI entry point ──────────────────────────────────────────────────────

if __name__ == "__main__":
    run_service()
