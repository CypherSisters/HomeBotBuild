"""Accepting a skill package the household wrote, without ever running it.

Without this module, the only way a skill could reach ``_drafts/`` would be
:func:`puffpai.skills.install.stage_example` -- a copy of something that shipped
inside ``src/`` -- and a developer's own skill would have to be hand-placed on
the machine by somebody with a shell. That is not "easy to attach your own
program"; it is "ask an administrator".

This module is the missing door, and it is written as a **parser for hostile
input**, because that is exactly what it is: bytes from the network becoming
files on disk next to code the appliance will later be asked to approve.

Four properties, in the order they are enforced:

1. **Nothing is written until everything is checked.** Every shape rule below
   runs against bytes in memory. The first violation raises and no path under
   ``_drafts/`` has been touched -- so a half-written draft, which would be a
   draft whose consent card describes files that are not the files on disk, is
   not a state this code can produce.
2. **Nothing is executed, imported or unpacked to a path the caller chose.** A
   zip member's name is never handed to ``extract``; it is validated and then
   the member's bytes are written under a name this module built. ``zipfile``'s
   own sanitising is not relied on, because relying on it would make the
   security argument "some other library probably handles that".
3. **The layout is exactly the three package files.** Not "the package files
   plus anything harmless": a ``data/`` file would be bytes on disk that the
   consent card does not show, ``installed.json`` does not hash and
   ``sandbox.stage`` does not copy into the jail -- i.e. unreviewed bytes
   travelling under an approved skill's name. So the ceilings on file count and
   total size are cheap early bounds, and the layout rule is the real one.
4. **Then the SAME validation the example path uses.** The staged copy goes
   through :func:`puffpai.skills.manifest.validate_dir` before it is allowed to
   become a draft, and from there through the identical propose -> card ->
   owner-approval -> install flow. Submitting is not installing, and it is not
   a weaker kind of installing.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import io
import logging
import os
import re
import shutil
import stat
import zipfile

logger = logging.getLogger(__name__)

#: The id a submission may claim. Deliberately checked IN ADDITION to
#: :data:`puffpai.skills.manifest.ID_RE` rather than instead of it: this is the
#: cheap gate on a path segment, that one is the gate on a skill id, and a
#: submission has to pass both. The effective rule is therefore the narrower of
#: the two (2-32 chars, must start with a letter) -- an id this regex would
#: allow but the manifest's would not is refused by ``validate_dir``.
SUBMIT_ID_RE = re.compile(r"^[a-z0-9_]{2,40}$")

#: Ceilings. Checked before the layout rule so a 10 000-entry zip is refused by
#: counting, not by parsing. ``MAX_TOTAL_BYTES`` is measured on the UNCOMPRESSED
#: bytes and enforced while reading, so a header that understates a member's
#: size buys nothing.
MAX_FILES = 32
MAX_TOTAL_BYTES = 256 * 1024

#: The raw upload ceiling. A zip of <=256 KB of text is comfortably under this;
#: a larger upload is refused without being parsed at all.
MAX_UPLOAD_BYTES = 256 * 1024

#: Extensions a submission may contain at all. Kept even though the layout rule
#: below is stricter, because it is the check that produces a message an author
#: can act on ("drop the .zip") instead of a bare layout refusal.
ALLOWED_EXTENSIONS = (".py", ".json", ".md", ".txt")

#: Path segments refused outright wherever they appear.
DENIED_SEGMENTS = frozenset({"__pycache__", ".git", ".svn", "node_modules"})

MAX_PATH_LEN = 128
MAX_SEGMENT_LEN = 64
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: Leading bytes of the archive formats a submission must not smuggle a second
#: package inside. The extension whitelist already refuses these names; this is
#: the check for the one named ``notes.txt``. Belt and braces, cheap, and the
#: reason a "no nested archives" claim is a property rather than a hope about
#: filenames.
_ARCHIVE_MAGIC = (
    b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08",   # zip
    b"\x1f\x8b",                                    # gzip
    b"BZh",                                         # bzip2
    b"\xfd7zXZ\x00",                                # xz
    b"7z\xbc\xaf\x27\x1c",                          # 7z
    b"Rar!\x1a\x07",                                # rar
)

#: tar has no leading magic; its marker sits at offset 257 of the first header.
_TAR_MAGIC_OFFSET = 257
_TAR_MAGIC = b"ustar"


class SubmissionRefused(ValueError):
    """A submitted package this build will not write to disk. Carries no path."""


# ── shape checks, all in memory ─────────────────────────────────────

def _refuse(message: str) -> "SubmissionRefused":
    return SubmissionRefused(message)


def check_id(skill_id) -> str:
    """The submitted id, or raise. Never a path fragment, never case-folded.

    Deliberately does NOT lower-case what it was given. Quietly accepting
    ``Tide_Clock`` as ``tide_clock`` would mean the id in the refusal message,
    the id on the consent card and the id in the manifest could be three
    different strings; a submitter who typed a capital gets told so.
    """
    value = str(skill_id or "").strip()
    if not SUBMIT_ID_RE.match(value):
        raise _refuse(
            "A skill id is 2 to 40 characters of lowercase letters, digits and "
            "underscores, and nothing else.")
    from puffpai.skills.manifest import ID_RE
    if not ID_RE.match(value):
        raise _refuse(
            f"'{value}' is not a usable skill id here: it must start with a "
            f"letter and be at most 32 characters.")
    return value


def _check_segment(segment: str, whole: str) -> None:
    if segment in ("", ".", ".."):
        raise _refuse(
            f"'{whole}' is not a plain relative path inside the package.")
    if len(segment) > MAX_SEGMENT_LEN:
        raise _refuse(f"'{whole}' has a path component that is too long.")
    if segment.lower() in DENIED_SEGMENTS:
        raise _refuse(
            f"'{whole}' contains '{segment}', which a skill package never "
            f"carries. Send source, not build output.")
    if not _SEGMENT_RE.match(segment):
        raise _refuse(
            f"'{whole}' has a path component with characters a skill package "
            f"may not use; letters, digits, dot, dash and underscore only.")


def safe_relpath(raw) -> str:
    """Validate one member name and return it in canonical ``a/b`` form.

    Refuses, in this order: a non-string or empty name, an over-long name, a
    backslash (a zip member separator is ``/``; anything else is a name meant to
    be read differently by two systems), a drive letter, an absolute path, a
    UNC path, ``.``/``..``/empty segments, a denied segment, and any character
    outside a deliberately small set. Returns a path that is relative by
    construction -- it never returns something the caller must re-check.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise _refuse("The package contains an entry with no name.")
    name = raw.strip()
    if len(name) > MAX_PATH_LEN:
        raise _refuse("The package contains an entry whose name is too long.")
    if "\x00" in name:
        raise _refuse("The package contains an entry with a NUL in its name.")
    if "\\" in name:
        raise _refuse(
            f"'{name}' uses a backslash. Package paths use '/' so that this "
            f"appliance and your machine read them the same way.")
    if re.match(r"^[A-Za-z]:", name):
        raise _refuse(f"'{name}' is an absolute path with a drive letter.")
    if name.startswith("/"):
        raise _refuse(f"'{name}' is an absolute path.")
    if os.path.isabs(name):                      # pragma: no cover - belt
        raise _refuse(f"'{name}' is an absolute path.")
    parts = name.split("/")
    for part in parts:
        _check_segment(part, name)
    if not parts[-1].lower().endswith(ALLOWED_EXTENSIONS):
        raise _refuse(
            f"'{name}' is not one of the file kinds a skill package may "
            f"carry ({', '.join(ALLOWED_EXTENSIONS)}).")
    return "/".join(parts)


def _check_not_archive(name: str, raw: bytes) -> None:
    for magic in _ARCHIVE_MAGIC:
        if raw.startswith(magic):
            raise _refuse(
                f"'{name}' is an archive with a text file's name. A skill "
                f"package carries source, never another package.")
    if raw[_TAR_MAGIC_OFFSET:_TAR_MAGIC_OFFSET + len(_TAR_MAGIC)] == _TAR_MAGIC:
        raise _refuse(
            f"'{name}' is a tar archive with a text file's name. A skill "
            f"package carries source, never another package.")


def check_bytes(name: str, raw: bytes) -> bytes:
    """Per-member content checks, run as each member is collected.

    Before the layout rule, on purpose: "that .txt is a zip" is a more useful
    refusal than "that .txt is not one of the three package files", and a
    reader should not have to know which check happens to fire first to know
    that a nested archive is refused.
    """
    _check_not_archive(name, raw)
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        raise _refuse(f"'{name}' is not readable UTF-8 text.") from None
    return raw


def check_layout(files: dict) -> dict:
    """The final shape gate: exactly the three package files, valid UTF-8.

    Stricter than :func:`puffpai.skills.manifest.validate_dir`, which tolerates
    a ``data/`` subdirectory on an already-installed skill. A SUBMISSION may not
    create one: ``data/`` bytes are not shown on the consent card, not hashed
    into ``installed.json`` and not staged into the jail, so accepting them
    would put unreviewed bytes on disk under an approved skill's name.
    """
    from puffpai.skills.manifest import PACKAGE_FILES

    if not files:
        raise _refuse("The package is empty.")
    for name, raw in files.items():
        check_bytes(name, raw)
    extra = sorted(set(files) - set(PACKAGE_FILES))
    if extra:
        raise _refuse(
            f"A skill package is exactly {', '.join(PACKAGE_FILES)}. "
            f"{', '.join(extra)} would sit on disk without appearing on the "
            f"consent card, so the package is refused rather than trimmed.")
    missing = [name for name in PACKAGE_FILES if name not in files]
    if missing:
        raise _refuse(f"The package has no {', '.join(missing)}.")
    return files


# ── the two collectors ──────────────────────────────────────────────

def collect_from_mapping(mapping) -> dict:
    """``{relpath: text}`` from a JSON body -> ``{name: bytes}``. Writes nothing."""
    if not isinstance(mapping, dict) or not mapping:
        raise _refuse(
            "Send files as an object: "
            '{"manifest.json": "...", "skill.py": "...", '
            '"tests_fixtures.json": "..."}.')
    if len(mapping) > MAX_FILES:
        raise _refuse(f"A skill package carries at most {MAX_FILES} files.")
    out: dict[str, bytes] = {}
    total = 0
    for raw_name, value in mapping.items():
        name = safe_relpath(raw_name)
        if name in out:
            raise _refuse(f"'{name}' appears twice in the package.")
        if isinstance(value, str):
            blob = value.encode("utf-8")
        elif isinstance(value, (bytes, bytearray)):
            blob = bytes(value)
        else:
            raise _refuse(f"'{name}' is not text.")
        total += len(blob)
        if total > MAX_TOTAL_BYTES:
            raise _refuse(
                f"A skill package is at most {MAX_TOTAL_BYTES // 1024} KB in "
                f"total; this one is larger.")
        out[name] = check_bytes(name, blob)
    return check_layout(out)


def collect_from_zip(raw) -> dict:
    """``{name: bytes}`` from zip bytes, or raise. Extracts nothing to disk.

    Every refusal here happens before a single byte is written anywhere: the
    members are read into memory, under names this function built, with the
    total capped as it reads. A member is never passed to ``extract`` or
    ``extractall``, so there is no path for ``zipfile``'s own idea of a safe
    name to become this module's.
    """
    if not isinstance(raw, (bytes, bytearray)) or not raw:
        raise _refuse("That upload had no bytes in it.")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise _refuse(
            f"That upload is larger than {MAX_UPLOAD_BYTES // 1024} KB, which "
            f"is more than a skill package can be.")
    try:
        archive = zipfile.ZipFile(io.BytesIO(bytes(raw)))
    except (zipfile.BadZipFile, OSError, ValueError):
        raise _refuse("That upload is not a readable zip file.") from None

    with archive:
        infos = archive.infolist()
        members = [info for info in infos if not info.is_dir()]
        if len(members) > MAX_FILES:
            raise _refuse(f"A skill package carries at most {MAX_FILES} files.")
        declared = sum(max(0, int(info.file_size or 0)) for info in members)
        if declared > MAX_TOTAL_BYTES:
            raise _refuse(
                f"A skill package is at most {MAX_TOTAL_BYTES // 1024} KB "
                f"unpacked; this one says it is larger.")

        out: dict[str, bytes] = {}
        total = 0
        for info in members:
            if info.flag_bits & 0x1:
                raise _refuse(
                    "That zip is encrypted. A package whose bytes cannot be "
                    "read cannot be shown on a consent card.")
            mode = info.external_attr >> 16
            # Only judge the mode when the entry actually carries file-TYPE
            # bits. A zip written by ``writestr`` stores permission bits alone
            # (0o600 << 16), and treating "no type recorded" as "not a regular
            # file" would refuse every ordinary zip -- a refusal so broad it
            # would have been switched off rather than fixed.
            if (mode & 0o170000) and not stat.S_ISREG(mode):
                kind = "a symlink" if stat.S_ISLNK(mode) else "not a regular file"
                raise _refuse(
                    f"'{info.filename}' is {kind}. A skill package is regular "
                    f"files only -- a link is a way to have the card show one "
                    f"thing and the jail read another.")
            name = safe_relpath(info.filename)
            if name in out:
                raise _refuse(f"'{name}' appears twice in the package.")
            remaining = MAX_TOTAL_BYTES - total
            try:
                with archive.open(info, "r") as member:
                    blob = member.read(remaining + 1)
            except (zipfile.BadZipFile, OSError, RuntimeError, ValueError):
                raise _refuse(
                    f"'{name}' could not be read out of that zip.") from None
            if len(blob) > remaining:
                # The header understated the size, or the whole set is over.
                # Either way the cap is enforced on what was actually read.
                raise _refuse(
                    f"A skill package is at most {MAX_TOTAL_BYTES // 1024} KB "
                    f"unpacked; this one is larger.")
            total += len(blob)
            out[name] = check_bytes(name, blob)
    return check_layout(out)


def collect_from_dir(folder) -> dict:
    """``{name: bytes}`` from a directory on THIS machine (the CLI's path).

    Reads only the three package files and refuses a symlink for any of them.
    Nothing else in the folder is read, so pointing the CLI at a working tree
    with notes and a virtualenv in it submits the package and not the tree.
    """
    from puffpai.skills.manifest import PACKAGE_FILES

    path = os.path.abspath(str(folder or ""))
    if not os.path.isdir(path) or os.path.islink(path):
        raise _refuse(f"{path} is not a directory.")
    out: dict[str, bytes] = {}
    total = 0
    for name in PACKAGE_FILES:
        full = os.path.join(path, name)
        if os.path.islink(full):
            raise _refuse(f"'{name}' is a symlink; regular files only.")
        if not os.path.isfile(full):
            raise _refuse(f"That folder has no {name}.")
        size = os.path.getsize(full)
        total += size
        if total > MAX_TOTAL_BYTES:
            raise _refuse(
                f"A skill package is at most {MAX_TOTAL_BYTES // 1024} KB in "
                f"total; this one is larger.")
        with open(full, "rb") as handle:
            out[name] = check_bytes(name, handle.read(MAX_TOTAL_BYTES + 1))
    return check_layout(out)


# ── the one writer ──────────────────────────────────────────────────

def _incoming_dir(skill_id: str) -> str:
    """A private staging path under the drafts tree, never the draft itself."""
    import uuid

    from puffpai.skills import store as _store
    return os.path.join(_store.drafts_root(),
                        f".incoming-{skill_id}-{uuid.uuid4().hex[:12]}")


def submit(skill_id, files, *, replace: bool = False) -> dict:
    """Write a checked package into ``_drafts/<id>/``. Installs nothing.

    *files* is ``{name: bytes}`` from one of the collectors above -- already
    shape-checked, so by the time anything is written the only thing left to
    decide is whether the manifest itself is acceptable, which
    ``validate_dir`` answers about the files as they will actually sit on disk.

    An id that is already INSTALLED is refused outright; *replace* only ever
    covers an existing DRAFT. Replacing an installed skill is a consent card
    for new bytes, which is what proposing again gives you -- and doing it by
    overwriting would be a way to swap approved code for unapproved code
    without one.
    """
    from puffpai.skills import manifest as _manifest
    from puffpai.skills import store as _store

    sid = check_id(skill_id)
    if not isinstance(files, dict) or not files:
        raise _refuse("The package is empty.")

    if os.path.isdir(_store.skill_dir(sid)):
        raise _refuse(
            f"'{sid}' is already installed. Remove it first, or submit under a "
            f"different id -- replacing installed code needs its own consent "
            f"card, not an overwrite.")
    draft = _store.draft_dir(sid)
    if os.path.exists(draft):
        if not replace:
            raise _refuse(
                f"There is already a draft called '{sid}' waiting for "
                f"approval. Submit with replace=1 if you meant to overwrite it.")
        if not os.path.isdir(draft) or os.path.islink(draft):
            raise _refuse(f"'{sid}' is in the drafts tree but is not a draft.")

    staging = _incoming_dir(sid)
    try:
        os.makedirs(staging, exist_ok=False)
        for name, blob in files.items():
            target = os.path.join(staging, name)
            # Re-derived from the validated name, and proven to land inside the
            # staging directory before opening it. The check is redundant given
            # ``safe_relpath``; it is here because "the path is safe because a
            # function two screens up said so" is the reasoning that produces
            # zip-slip, and a second, local proof costs one comparison.
            if os.path.dirname(os.path.realpath(target)) != os.path.realpath(staging):
                raise _refuse(f"'{name}' does not stay inside the package.")
            with open(target, "wb") as handle:
                handle.write(blob)
        # The SAME validation the example path runs, against the bytes on disk.
        validated, _shas = _manifest.validate_dir(staging)
        if validated["id"] != sid:
            raise _refuse(
                f"The manifest calls this skill '{validated['id']}' but it was "
                f"submitted as '{sid}'. They have to agree.")
        if os.path.exists(draft):
            shutil.rmtree(draft, ignore_errors=False)
        os.replace(staging, draft)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    logger.info("skills: accepted a submitted draft %s (%d files)", sid,
                len(files))
    return {"ok": True, "id": sid, "dir": draft, "manifest": validated,
            "files": sorted(files)}


def submit_and_propose(skill_id, files, *, replace: bool = False,
                       requested_by: str = "owner") -> dict:
    """Submit, then file ONE consent request. Installs nothing. Raises on refusal."""
    from puffpai.skills import install as _install

    staged = submit(skill_id, files, replace=replace)
    proposed = _install.propose_install(staged["id"], requested_by=requested_by)
    return {"id": staged["id"], "submitted": True,
            "files": staged["files"], "manifest": staged["manifest"],
            "proposed": bool(proposed.get("proposed")),
            "ok": bool(proposed.get("ok")),
            "awaiting_approval": bool(proposed.get("awaiting_approval")),
            "message": proposed.get("message") or "",
            "error": proposed.get("error") or ""}


def manifest_id_of(files) -> str:
    """The id the submitted manifest.json claims, for a body that omitted one.

    Parses JSON and reads one string. Nothing is imported and nothing runs: a
    manifest is data, and this is the only thing read out of it before
    ``validate_dir`` reads all of it.
    """
    import json

    raw = (files or {}).get("manifest.json")
    if not raw:
        raise _refuse("The package has no manifest.json.")
    try:
        loaded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise _refuse("manifest.json will not parse as JSON.") from None
    if not isinstance(loaded, dict) or not isinstance(loaded.get("id"), str):
        raise _refuse("manifest.json does not name an id.")
    return check_id(loaded["id"])


__all__ = [
    "ALLOWED_EXTENSIONS", "DENIED_SEGMENTS", "MAX_FILES", "MAX_TOTAL_BYTES",
    "MAX_UPLOAD_BYTES", "SUBMIT_ID_RE", "SubmissionRefused", "check_bytes",
    "check_id", "check_layout", "collect_from_dir", "collect_from_mapping",
    "collect_from_zip", "manifest_id_of", "safe_relpath", "submit",
    "submit_and_propose",
]
