"""A locked-path record moved to another name is refused.

The unattended tier stores one AES-GCM file per record with no associated
data, so the cipher alone does not tie a file to its name. The binding is in
the record: every reader checks the name written inside it (``service_id``
for a sign-up key, ``name`` for a tool server's headers) before using it. A
file copied over another record's name opens, and is then refused.
"""

from __future__ import annotations

import os
import shutil

import pytest

from keys_support import (canary, fast_vault, install_vendor,
                          machine_key_file, reset_keys, set_posture)
from puffpai.security import egress_doors, keys

pytestmark = pytest.mark.keyring_stub


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for d in egress_doors.DOORS:
        monkeypatch.delenv(d.env_var, raising=False)
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    reset_keys()
    set_posture("enumerated")
    yield
    reset_keys()

