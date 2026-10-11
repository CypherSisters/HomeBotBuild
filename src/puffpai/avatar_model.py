"""The household's OWN 3D avatar model — one .glb, stored under the data dir.

Why this module exists
----------------------
No avatar model ships with this repository (none we had was licensed to
redistribute), so "bring your own" is the only way to get a 3D face. Before
this, the only documented route was *drop a file into
``dashboard/public/avatars/``* — a path inside the checkout, which means: a
stranger has to have the source tree, the file is lost on every redeploy, and
it is served by the dashboard with no household gate at all.

So the model lives where every other piece of household state lives:
``$PUFFPAI_DATA_DIR/avatar_models/custom.glb`` (in-tree ``data/avatar_models/``
when the env root is unset). The backend owns the file; the dashboard reaches
it through a pairing-gated proxy. It survives restarts and redeploys because it
was never in the build.

What is checked, and what is NOT
--------------------------------
:func:`sniff_glb` reads the 12-byte glTF binary header — magic ``glTF``,
version 2, and a declared total length that matches the bytes we were handed.
That is the *container*, and it is all a byte check can honestly claim. It does
NOT prove the model has a Mixamo skeleton or ARKit/Oculus morph targets; the
engines discover that in the browser and degrade (see ``docs/AVATARS.md``).
A .gltf + separate .bin/textures set is refused on purpose: one file, embedded.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import struct
from pathlib import Path

from puffpai.logging_config import get_logger
from puffpai.statedir import resolve_runtime_dir

logger = get_logger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: One model, one name. Replacing it is an overwrite, so there is never a
#: second copy to garbage-collect and no id to leak into a URL.
MODEL_FILENAME = "custom.glb"

#: 48 MB. A Ready Player Me / Avaturn export is 3-12 MB; a Blender export with
#: 4K textures can reach 40. Past this it is not an avatar, it is a payload.
#: Kept UNDER the dashboard proxy's own 50 MB ceiling (``MAX_BODY_BYTES`` in
#: ``dashboard/app/api/pai/[...path]/route.ts``) so the refusal a household sees
#: is this module's plain message, not the proxy's generic 413.
MODEL_MAX_BYTES = 48 * 1024 * 1024

#: The glTF binary container header: magic, version, total length.
_GLB_MAGIC = b"glTF"
_GLB_VERSION = 2
_GLB_HEADER = 12

#: What ``ai_settings.avatar_3d_url`` is set to once a model is stored.
#:
#: A same-origin dashboard path on purpose: the browser's GLTFLoader fetches the
#: model itself and cannot attach an API key, so the gate has to be a cookie —
#: which is exactly what the dashboard's existing pairing-gated proxy
#: (``dashboard/app/api/pai/[...path]/route.ts``) already provides. It checks
#: ``requirePairing`` and attaches the backend key server-side, so no new route
#: was needed and an unpaired LAN device gets 401 for the model too.
MODEL_URL = "/api/pai/profile/avatar-model"


class AvatarModelError(ValueError):
    """A caller error with a stable code: ``not_a_glb``, ``too_large``,
    ``no_model``."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


# ── paths ───────────────────────────────────────────────────────────────────

def models_dir() -> Path:
    """``<data_dir>/avatar_models`` — created by the write site, not here."""
    return Path(resolve_runtime_dir(
        str(_REPO_ROOT / "data" / "avatar_models"), env_subdir="avatar_models"))


def model_path() -> Path:
    return models_dir() / MODEL_FILENAME


def has_model() -> bool:
    try:
        return model_path().is_file()
    except OSError:                          # pragma: no cover - defensive
        return False


# ── the container check ─────────────────────────────────────────────────────

def sniff_glb(data: bytes) -> bool:
    """True when *data* is a self-contained glTF 2.0 binary (.glb).

    Checked: the magic, the version, and that the header's declared total
    length equals the bytes we hold (a truncated upload is not a model). The
    first chunk must be JSON, which is where a renamed .zip or .png stops.
    """
    if not isinstance(data, (bytes, bytearray)) or len(data) < _GLB_HEADER + 8:
        return False
    magic, version, total = struct.unpack_from("<4sII", data, 0)
    if magic != _GLB_MAGIC or version != _GLB_VERSION:
        return False
    if total != len(data):
        return False
    chunk_len, chunk_type = struct.unpack_from("<I4s", data, _GLB_HEADER)
    if chunk_type != b"JSON":
        return False
    # The JSON chunk has to fit inside the file it claims to be part of.
    return _GLB_HEADER + 8 + chunk_len <= total


# ── read / write / remove ───────────────────────────────────────────────────

def save_model(data: bytes) -> dict:
    """Store *data* as the household's avatar model. Returns ``{path, bytes}``.

    Raises :class:`AvatarModelError` with ``too_large`` or ``not_a_glb``. The
    size check runs first so a 200 MB non-GLB is refused without being sniffed.
    """
    payload = bytes(data or b"")
    if len(payload) > MODEL_MAX_BYTES:
        raise AvatarModelError(
            "too_large",
            f"That file is over {MODEL_MAX_BYTES // (1024 * 1024)} MB.")
    if not sniff_glb(payload):
        raise AvatarModelError(
            "not_a_glb",
            "That file isn't a .glb (glTF binary) model. Export your avatar as "
            "glTF Binary — one file, textures embedded.")
    target = model_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    # Write beside, then replace: a half-written model must never be served,
    # and the avatar page may be fetching the old one right now.
    tmp = target.with_suffix(".glb.part")
    tmp.write_bytes(payload)
    tmp.replace(target)
    logger.info("avatar model stored (%d bytes)", len(payload))
    return {"path": str(target), "bytes": len(payload)}


def read_model() -> bytes:
    """The stored model's bytes. Raises ``AvatarModelError('no_model')``."""
    path = model_path()
    try:
        return path.read_bytes()
    except (FileNotFoundError, NotADirectoryError):
        raise AvatarModelError(
            "no_model", "No avatar model has been uploaded.") from None


def delete_model() -> bool:
    path = model_path()
    try:
        path.unlink()
        return True
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError as exc:                   # pragma: no cover - defensive
        logger.warning("avatar model delete failed: %s", exc)
        return False
