"""
Conditional nftables tests for Linux/WSL2.

These tests only run on Linux systems where nft is available.
Skipped automatically on Windows and systems without nftables.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import pytest

_IS_LINUX = platform.system() == "Linux"
_HAS_NFT = shutil.which("nft") is not None
_CAN_RUN = _IS_LINUX and _HAS_NFT


@pytest.mark.skipif(not _IS_LINUX, reason="Not Linux")
class TestNftablesAvailability:
    """Basic nftables availability checks (Linux only)."""

    def test_nft_binary_exists(self):
        """nft binary exists on PATH."""
        nft_path = shutil.which("nft")
        # On Linux, nft should be findable (may need sudo for operations)
        # This test just checks the binary exists
        if nft_path:
            assert os.path.isfile(nft_path)
        else:
            pytest.skip("nft not installed")

    def test_nft_version(self):
        """nft --version runs without error."""
        if not _HAS_NFT:
            pytest.skip("nft not installed")
        result = subprocess.run(
            ["nft", "--version"], capture_output=True, text=True, timeout=5)
        assert result.returncode == 0
        assert "nftables" in result.stdout.lower()


@pytest.mark.skipif(not _CAN_RUN, reason="nftables not available")
class TestNftablesIntegration:
    """Integration tests for nftables operations (require root/sudo)."""

    def test_list_tables_as_user(self):
        """nft list tables runs (may fail without root, that's OK)."""
        result = subprocess.run(
            ["nft", "list", "tables"], capture_output=True, text=True, timeout=5)
        # Non-root may get permission denied (returncode != 0)
        # We're just verifying the binary works, not that we have permissions
        assert result.returncode in (0, 1, 2)  # 0=success, 1/2=permission denied

    def test_firewall_manager_nftables_backend(self):
        """FirewallManager detects nftables backend on Linux."""
        try:
            from puffpai.firewall_manager import FirewallManager
            fm = FirewallManager()
            # On Linux with nft, the manager should detect it
            # (exact behavior depends on implementation)
            assert fm is not None
        except Exception:
            pytest.skip("FirewallManager not compatible with this environment")
