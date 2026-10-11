"""The systemd entrypoint must configure logging.

Regression cover for a production blackout: puffpai-api.service runs
start_api.py, which never called setup_logging(). The 'puffpai' logger was left
at level NOTSET with zero handlers, so every logger.info() in the backend was
discarded and only WARNING+ reached journald via logging.lastResort. The
Windows path (puffpai.service.run_service) did call it, which hid the gap.
"""

import logging
import logging.handlers
import os
import sys

import pytest

# start_api.py lives at the repo root, not under src/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import start_api  # noqa: E402


@pytest.fixture(autouse=True)
def _unconfigured():
    """setup_logging() no-ops when already configured; start each test cold."""
    import puffpai.logging_config as lc
    lc._configured = False
    yield


def _file_handlers(logger):
    return [h for h in logger.handlers
            if isinstance(h, logging.handlers.RotatingFileHandler)]


class TestConfigureLogging:
    def test_attaches_handlers_at_info(self, tmp_path, monkeypatch):
        """The blackout itself: no handlers, level NOTSET, INFO discarded."""
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
        monkeypatch.delenv("PUFFPAI_LOG_LEVEL", raising=False)

        start_api.configure_logging()

        logger = logging.getLogger("puffpai")
        assert logger.level == logging.INFO
        assert logger.handlers, "entrypoint left the puffpai logger unhandled"
        assert logger.isEnabledFor(logging.INFO)

    def test_level_env_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("PUFFPAI_LOG_LEVEL", "DEBUG")

        start_api.configure_logging()

        assert logging.getLogger("puffpai").level == logging.DEBUG

    def test_blank_level_env_falls_back_to_info(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("PUFFPAI_LOG_LEVEL", "   ")

        start_api.configure_logging()

        assert logging.getLogger("puffpai").level == logging.INFO


class TestLogFileLocation:
    """ProtectSystem=strict makes the code tree read-only on the appliance."""

    def test_log_file_lands_under_state_root(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))

        log_file = start_api.configure_logging()

        assert log_file is not None
        assert os.path.isfile(log_file)
        assert os.path.realpath(log_file).startswith(os.path.realpath(tmp_path))

    def test_log_file_is_not_package_relative(self, tmp_path, monkeypatch):
        """A package-relative log dir fails with EROFS under ProtectSystem=strict."""
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))

        log_file = start_api.configure_logging()

        repo_root = os.path.realpath(start_api._REPO_ROOT)
        assert not os.path.realpath(log_file).startswith(repo_root)

    def test_writes_reach_the_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))

        log_file = start_api.configure_logging()
        logging.getLogger("puffpai.probe").info("canary-%s", 42)
        for handler in _file_handlers(logging.getLogger("puffpai")):
            handler.flush()

        assert "canary-42" in open(log_file, encoding="utf-8").read()

    @pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits only")
    def test_log_file_is_not_world_readable(self, tmp_path, monkeypatch):
        """INFO puts LLM reply fragments and device names on disk."""
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))

        log_file = start_api.configure_logging()

        assert os.stat(log_file).st_mode & 0o007 == 0, "log is world-readable"
        assert os.stat(os.path.dirname(log_file)).st_mode & 0o007 == 0

    @pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits only")
    def test_mode_survives_rotation(self, tmp_path, monkeypatch):
        """Rollover opens a NEW file under the process umask (0644 by default).

        A one-time chmod at startup would lapse the first time the log hits
        its size cap, leaving the *active* log world-readable.
        """
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
        old_umask = os.umask(0o022)  # systemd's default
        try:
            log_file = start_api.configure_logging()
            handler = _file_handlers(logging.getLogger("puffpai"))[0]
            handler.doRollover()

            assert os.stat(log_file).st_mode & 0o007 == 0
            assert os.stat(log_file + ".1").st_mode & 0o007 == 0
        finally:
            os.umask(old_umask)

    def test_unwritable_dir_degrades_to_console(self, tmp_path, monkeypatch):
        """Logging must never be the reason the service fails to boot."""
        blocker = tmp_path / "state"
        blocker.write_text("not a directory")  # makedirs() will raise
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(blocker))

        log_file = start_api.configure_logging()

        logger = logging.getLogger("puffpai")
        assert log_file is None
        assert logger.handlers, "console logging must survive a bad log dir"
        assert logger.isEnabledFor(logging.INFO)
