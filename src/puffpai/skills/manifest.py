"""Validation of one skill package: the manifest, and the bytes beside it.

Every rule here is a REFUSAL rule. A manifest this module will not accept is a
skill that does not exist -- there is no partially-valid skill, because a
partially-valid skill would be one whose permissions were read from a file the
validator had already stopped trusting.

Three of the rules are load-bearing and are worth stating outright:

* **Trigger collision** (:func:`trigger_collision`). A trigger that a capability
  already declares -- or that :func:`puffpai.capability_registry.find` binds to
  ANY capability -- is refused. This is what makes "a skill can never answer
  'turn off the lights'" a property of the installer rather than a hope about
  dispatch order. It is deliberately stricter than it needs to be: ``find`` is
  an eager ranking function that names a best guess for almost any household
  sentence, so anything it claims is left to the appliance.
* **Size** (:data:`MAX_SKILL_PY_BYTES`, :data:`MAX_SKILL_PY_LINES`). The consent
  card shows the code. Code an owner will not read is code an owner cannot
  consent to, so a skill that will not fit on the card is refused rather than
  summarised.
* **Shape on disk** (:func:`validate_dir`). Regular files only, no symlinks, no
  subdirectory except ``data/``. Checked at propose AND at install, because a
  symlink planted between the two is exactly the TOCTOU the sha256 gate is
  there to catch and this is the cheaper half of that check.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import hashlib
import json
import os
import re

SCHEMA = "puffpai_skill/v1"

ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")

#: The consent card shows every byte of ``skill.py``. Both ceilings are about
#: what a person will actually read before approving, not about resources.
MAX_SKILL_PY_BYTES = 16 * 1024
MAX_SKILL_PY_LINES = 200

MAX_MANIFEST_BYTES = 8 * 1024
MAX_FIXTURES_BYTES = 64 * 1024
MAX_LABEL_LEN = 80
MAX_DESCRIPTION_LEN = 300
MAX_TRIGGERS = 12
MAX_TRIGGER_LEN = 80
MAX_HOSTS = 16
MAX_SOURCE_URLS = 8

#: Ceilings the manifest may go UNDER and never over. The runner and the
#: broker clamp to these as well, so a
#: manifest that lied about a limit still cannot exceed it.
MAX_TIMEOUT_S = 60
MAX_REQUESTS = 8
MAX_RESPONSE_BYTES = 512 * 1024
MAX_QUERY_BYTES = 2048

DEFAULT_LIMITS = {"timeout_s": 20, "max_requests": 4,
                  "max_response_bytes": 256 * 1024}

#: Phase 1: hand-authored only. ``puffpai`` is a skill that shipped with the
#: appliance; ``owner`` is one the household wrote. Phase 3 adds signed
#: third-party authors, which is a different consent card.
ALLOWED_AUTHORS = ("puffpai", "owner")

#: Files a draft/installed skill dir may contain. ``tests.py`` is deliberately
#: NOT here: the trusted runner owns the test harness (a skill that shipped its
#: own tests would be grading its own homework), so the manifest's ``tests`` key
#: may only ever be absent or the literal ``["tests.py"]`` naming the harness
#: the HOST runs.
PACKAGE_FILES = ("manifest.json", "skill.py", "tests_fixtures.json")
STATE_FILES = ("installed.json", "state.json")
ALLOWED_SUBDIRS = ("data",)


class SkillManifestError(ValueError):
    """A manifest (or the bytes beside it) that this build will not accept."""


def normalise(text) -> str:
    """Lower-case, collapse whitespace, drop trailing punctuation.

    Used for trigger equality on BOTH sides -- the declared trigger and the
    utterance -- so "Where is the ISS?" and "where is the iss" are the same
    string and nothing else is.
    """
    if not isinstance(text, str):
        return ""
    low = " ".join(text.strip().lower().split())
    return low.rstrip(" ?.!,")


def _ascii_text(value, limit: int) -> bool:
    return (isinstance(value, str) and value.strip() != ""
            and len(value) <= limit and value.isascii() and value.isprintable())


def trigger_collision(trigger: str, *, skip_origin: str = "") -> str | None:
    """The capability a trigger would collide with, or None.

    Two tests, and the second is the wide one:

    1. exact match against any capability's ``phrases`` or ``dispatch_phrases``
       (normalised on both sides);
    2. :func:`puffpai.capability_registry.find` returns anything at all.

    *skip_origin* excludes one skill's own registry row, so re-validating an
    installed skill does not find itself.

    Fails CLOSED: if the registry cannot be consulted, every trigger collides.
    A skill whose triggers could not be checked against the appliance's own
    phrasings is a skill that might answer "turn off the lights".
    """
    said = normalise(trigger)
    if not said:
        return "(empty)"
    try:
        from puffpai import capability_registry as registry
    except Exception:  # pragma: no cover - ships together
        return "(registry unavailable)"
    try:
        for cap in registry.all_capabilities():
            if skip_origin and getattr(cap, "origin", "") == skip_origin:
                continue
            for phrase in (*(cap.phrases or ()), *(cap.dispatch_phrases or ())):
                if normalise(phrase) == said:
                    return cap.id
        found = registry.find(said)
        if found is not None and not (skip_origin
                                      and getattr(found, "origin", "") == skip_origin):
            # The skip is only ever for the skill's OWN row. Written this way
            # round because the natural `origin != skip_origin` reads the same
            # and is silently wrong: every declared capability has an empty
            # origin, so with the default `skip_origin=""` it skipped the entire
            # appliance and refused nothing.
            return found.id
    except Exception:
        return "(registry unavailable)"
    return None


def _validate_permissions(block, skill_id: str) -> None:
    if not isinstance(block, dict):
        raise SkillManifestError(
            f"Skill '{skill_id}' does not declare a permissions block. A skill "
            f"with unstated reach is refused.")
    unknown = set(block) - {"network_hosts", "files", "ha_domains", "secrets"}
    if unknown:
        raise SkillManifestError(
            f"Skill '{skill_id}' declares unknown permissions "
            f"{sorted(unknown)!r}; this build reads network_hosts, files, "
            f"ha_domains and secrets.")

    hosts = block.get("network_hosts", [])
    if not isinstance(hosts, list):
        raise SkillManifestError(f"Skill '{skill_id}': network_hosts is not a list.")
    if len(hosts) > MAX_HOSTS:
        raise SkillManifestError(
            f"Skill '{skill_id}' names {len(hosts)} hosts; {MAX_HOSTS} is the "
            f"most one skill may reach.")
    from puffpai.security import egress_doors
    seen: list[str] = []
    for raw in hosts:
        host = raw.strip().rstrip(".").lower() if isinstance(raw, str) else ""
        if not host or not egress_doors._is_registrable_hostname(host):
            raise SkillManifestError(
                f"Skill '{skill_id}' names {raw!r} as a host. A door host is a "
                f"registrable NAME -- never an address, a wildcard, a scheme or "
                f"a port.")
        if host not in seen:
            seen.append(host)

    files = block.get("files", [])
    if not isinstance(files, list) or not set(map(str, files)) <= set(ALLOWED_SUBDIRS):
        raise SkillManifestError(
            f"Skill '{skill_id}' asks for files {files!r}. The only path a "
            f"skill may write is its own 'data' directory.")

    for key in ("ha_domains", "secrets"):
        value = block.get(key, [])
        if not isinstance(value, list) or value:
            raise SkillManifestError(
                f"Skill '{skill_id}' declares {key}={value!r}. Phase 1 skills "
                f"touch neither Home Assistant nor the vault; that is the next "
                f"tier and it needs its own consent card.")


def _validate_limits(block, skill_id: str) -> dict:
    if block is None:
        return dict(DEFAULT_LIMITS)
    if not isinstance(block, dict):
        raise SkillManifestError(f"Skill '{skill_id}': limits is not an object.")
    ceilings = {"timeout_s": MAX_TIMEOUT_S, "max_requests": MAX_REQUESTS,
                "max_response_bytes": MAX_RESPONSE_BYTES}
    out = dict(DEFAULT_LIMITS)
    for key, value in block.items():
        if key not in ceilings:
            raise SkillManifestError(
                f"Skill '{skill_id}' declares unknown limit {key!r}.")
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise SkillManifestError(
                f"Skill '{skill_id}': limit {key} must be a positive integer.")
        if value > ceilings[key]:
            raise SkillManifestError(
                f"Skill '{skill_id}': limit {key}={value} is over the ceiling "
                f"{ceilings[key]}.")
        out[key] = value
    return out


def validate(data, *, skip_origin: str = "") -> dict:
    """Return the manifest in canonical form, or raise :class:`SkillManifestError`."""
    if not isinstance(data, dict):
        raise SkillManifestError("The manifest is not a JSON object.")

    if str(data.get("schema") or "") != SCHEMA:
        raise SkillManifestError(
            f"The manifest says schema {data.get('schema')!r}; this build "
            f"reads {SCHEMA!r}.")

    skill_id = data.get("id")
    if not isinstance(skill_id, str) or not ID_RE.match(skill_id):
        raise SkillManifestError(
            f"Skill id {skill_id!r} is not usable: 2 to 32 characters, "
            f"lowercase, starting with a letter, letters/digits/underscore.")

    if not _ascii_text(data.get("name"), MAX_LABEL_LEN):
        raise SkillManifestError(f"Skill '{skill_id}' has no usable name.")
    if not _ascii_text(data.get("description"), MAX_DESCRIPTION_LEN):
        raise SkillManifestError(f"Skill '{skill_id}' has no usable description.")

    version = data.get("version", 1)
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise SkillManifestError(f"Skill '{skill_id}' has a non-numeric version.")

    author = data.get("author")
    if author not in ALLOWED_AUTHORS:
        raise SkillManifestError(
            f"Skill '{skill_id}' names author {author!r}. This build installs "
            f"only {' or '.join(ALLOWED_AUTHORS)} skills; a third-party skill "
            f"needs a signature this build cannot check yet.")

    triggers = data.get("triggers")
    if not isinstance(triggers, list) or not triggers:
        raise SkillManifestError(f"Skill '{skill_id}' declares no triggers.")
    if len(triggers) > MAX_TRIGGERS:
        raise SkillManifestError(
            f"Skill '{skill_id}' declares {len(triggers)} triggers; "
            f"{MAX_TRIGGERS} is the most.")
    clean_triggers: list[str] = []
    for raw in triggers:
        if not _ascii_text(raw, MAX_TRIGGER_LEN):
            raise SkillManifestError(
                f"Skill '{skill_id}' has an unusable trigger {raw!r}.")
        said = normalise(raw)
        if len(said.split()) < 2:
            raise SkillManifestError(
                f"Skill '{skill_id}' declares the one-word trigger {raw!r}. A "
                f"single word is a mention, not a request.")
        collides = trigger_collision(said, skip_origin=skip_origin)
        if collides:
            raise SkillManifestError(
                f"Skill '{skill_id}' claims the trigger {raw!r}, which this "
                f"appliance already answers ({collides}). A skill never takes "
                f"a phrase the household already means something by.")
        if said not in clean_triggers:
            clean_triggers.append(said)

    inputs = data.get("inputs", {"type": "object", "properties": {}, "required": []})
    if not isinstance(inputs, dict) or inputs.get("type") != "object":
        raise SkillManifestError(
            f"Skill '{skill_id}': inputs must be an object schema.")
    properties = inputs.get("properties", {})
    required = inputs.get("required", [])
    if not isinstance(properties, dict) or not isinstance(required, list):
        raise SkillManifestError(
            f"Skill '{skill_id}': inputs.properties/required are malformed.")

    _validate_permissions(data.get("permissions"), skill_id)
    limits = _validate_limits(data.get("limits"), skill_id)

    tests = data.get("tests")
    if tests is not None and tests != ["tests.py"]:
        raise SkillManifestError(
            f"Skill '{skill_id}' declares tests={tests!r}. The host owns the "
            f"test harness; a skill ships fixtures, never a test file it wrote "
            f"itself. Use the literal [\"tests.py\"] or leave it out.")

    source_urls = data.get("source_urls", [])
    if not isinstance(source_urls, list) or len(source_urls) > MAX_SOURCE_URLS:
        raise SkillManifestError(f"Skill '{skill_id}': source_urls is malformed.")
    for url in source_urls:
        if not isinstance(url, str) or not url.startswith("https://"):
            raise SkillManifestError(
                f"Skill '{skill_id}': source url {url!r} is not https.")

    permissions = data.get("permissions")
    hosts = tuple(dict.fromkeys(
        h.strip().rstrip(".").lower() for h in permissions.get("network_hosts", [])))

    return {
        "schema": SCHEMA,
        "id": skill_id,
        "name": data["name"],
        "description": data["description"],
        "version": version,
        "author": author,
        "triggers": clean_triggers,
        "inputs": {"type": "object", "properties": properties,
                   "required": [str(r) for r in required]},
        "permissions": {"network_hosts": list(hosts),
                        "files": list(permissions.get("files", [])),
                        "ha_domains": [], "secrets": []},
        "limits": limits,
        "tests": ["tests.py"],
        "source_urls": [str(u) for u in source_urls],
    }


def validate_fixtures(data, skill_id: str) -> dict:
    """The canned broker answers and the assertions the HOST will make.

    Shape::

        {"responses": {"<url>": {"json": {...}}},
         "cases": [{"inputs": {},
                    "expect": {"speech_contains": ["..."],
                               "data": {"field": value}}}]}

    Zero cases is a failure, and so is a case that asserts nothing: a test that
    cannot fail is the classic model-written-test problem, and it
    is refused here rather than "passed" later.
    """
    if not isinstance(data, dict):
        raise SkillManifestError(
            f"Skill '{skill_id}': tests_fixtures.json is not an object.")
    responses = data.get("responses")
    if not isinstance(responses, dict) or not responses:
        raise SkillManifestError(
            f"Skill '{skill_id}': tests_fixtures.json carries no canned "
            f"responses, so its tests would have to reach the network.")
    for url in responses:
        if not isinstance(url, str) or not url.startswith("https://"):
            raise SkillManifestError(
                f"Skill '{skill_id}': fixture url {url!r} is not https.")
    cases = data.get("cases")
    if not isinstance(cases, list) or not cases:
        raise SkillManifestError(
            f"Skill '{skill_id}': tests_fixtures.json declares no cases. A "
            f"skill with no test is not installable.")
    speech_asserts = 0
    data_asserts = 0
    for case in cases:
        if not isinstance(case, dict):
            raise SkillManifestError(
                f"Skill '{skill_id}': a fixture case is not an object.")
        if not isinstance(case.get("inputs", {}), dict):
            raise SkillManifestError(
                f"Skill '{skill_id}': a fixture case has bad inputs.")
        expect = case.get("expect")
        if not isinstance(expect, dict):
            raise SkillManifestError(
                f"Skill '{skill_id}': a fixture case asserts nothing.")
        speech = expect.get("speech_contains", [])
        if not isinstance(speech, list):
            raise SkillManifestError(
                f"Skill '{skill_id}': speech_contains is not a list.")
        speech_asserts += len([s for s in speech if isinstance(s, str) and s])
        expected_data = expect.get("data", {})
        if not isinstance(expected_data, dict):
            raise SkillManifestError(
                f"Skill '{skill_id}': expect.data is not an object.")
        data_asserts += len(expected_data)
    if speech_asserts < 1 or data_asserts < 1:
        raise SkillManifestError(
            f"Skill '{skill_id}': the fixtures must assert at least one "
            f"sentence fragment AND one data field, or the test proves "
            f"nothing.")
    return data


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_dir(path: str, *, skip_origin: str = "") -> tuple[dict, dict]:
    """Validate a draft/installed skill directory. Returns ``(manifest, sha256s)``.

    Refuses anything that is not a regular file, every symlink (checked with
    ``islink``, so a symlink to a regular file is still refused), and every
    subdirectory other than ``data/``.
    """
    if not os.path.isdir(path):
        raise SkillManifestError(f"{path} is not a skill directory.")

    entries = sorted(os.listdir(path))
    for name in entries:
        full = os.path.join(path, name)
        if os.path.islink(full):
            raise SkillManifestError(
                f"'{name}' is a symlink. A skill package is regular files "
                f"only -- a link is a way to have the card show one thing and "
                f"the jail read another.")
        if os.path.isdir(full):
            if name not in ALLOWED_SUBDIRS:
                raise SkillManifestError(
                    f"'{name}/' is not an allowed directory; the only one is "
                    f"'data/'.")
            continue
        if not os.path.isfile(full):
            raise SkillManifestError(f"'{name}' is not a regular file.")
        if name not in PACKAGE_FILES and name not in STATE_FILES:
            raise SkillManifestError(
                f"'{name}' is not part of a skill package "
                f"({', '.join(PACKAGE_FILES)}).")

    for required in PACKAGE_FILES:
        if not os.path.isfile(os.path.join(path, required)):
            raise SkillManifestError(f"the package has no {required}.")

    manifest_path = os.path.join(path, "manifest.json")
    if os.path.getsize(manifest_path) > MAX_MANIFEST_BYTES:
        raise SkillManifestError("manifest.json is too large to be a manifest.")
    try:
        with open(manifest_path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError) as exc:
        raise SkillManifestError(f"manifest.json will not parse: {exc}") from exc
    if not skip_origin and isinstance(raw, dict) and isinstance(raw.get("id"), str):
        # Derive the skip from the package's own id. Without it, validating an
        # INSTALLED skill finds the registry row that skill itself contributed
        # and refuses the package for colliding with itself -- which made every
        # run after the first install fail. The collision rule exists to stop a
        # skill taking a phrase SOMETHING ELSE already means; a skill is always
        # allowed to keep its own.
        skip_origin = f"skill:{raw['id']}"
    manifest = validate(raw, skip_origin=skip_origin)

    code_path = os.path.join(path, "skill.py")
    size = os.path.getsize(code_path)
    if size > MAX_SKILL_PY_BYTES:
        raise SkillManifestError(
            f"skill.py is {size} bytes; the ceiling is {MAX_SKILL_PY_BYTES}. "
            f"The consent card shows every byte, and code nobody will read is "
            f"code nobody can approve.")
    try:
        with open(code_path, "r", encoding="utf-8") as handle:
            source = handle.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise SkillManifestError(
            f"skill.py is not readable UTF-8 text: {exc}") from exc
    lines = source.count("\n") + (0 if source.endswith("\n") else 1)
    if lines > MAX_SKILL_PY_LINES:
        raise SkillManifestError(
            f"skill.py is {lines} lines; the ceiling is {MAX_SKILL_PY_LINES}.")

    from puffpai.skills import lint as _lint
    if not _lint.has_run_entrypoint(source):
        raise SkillManifestError(
            "skill.py defines no run(inputs, ctx); there is nothing to call.")

    fixtures_path = os.path.join(path, "tests_fixtures.json")
    if os.path.getsize(fixtures_path) > MAX_FIXTURES_BYTES:
        raise SkillManifestError("tests_fixtures.json is too large.")
    try:
        with open(fixtures_path, "r", encoding="utf-8") as handle:
            fixtures = json.load(handle)
    except (OSError, ValueError) as exc:
        raise SkillManifestError(
            f"tests_fixtures.json will not parse: {exc}") from exc
    validate_fixtures(fixtures, manifest["id"])

    for url in fixtures.get("responses", {}):
        host = url.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0].lower()
        if host not in manifest["permissions"]["network_hosts"]:
            raise SkillManifestError(
                f"tests_fixtures.json cans a response for {host}, which the "
                f"manifest does not declare. The fixtures may not describe "
                f"reach the card would not show.")

    shas = {name: sha256_file(os.path.join(path, name)) for name in PACKAGE_FILES}
    return manifest, shas


__all__ = [
    "ALLOWED_AUTHORS", "ALLOWED_SUBDIRS", "DEFAULT_LIMITS", "ID_RE",
    "MAX_HOSTS", "MAX_QUERY_BYTES", "MAX_REQUESTS", "MAX_RESPONSE_BYTES",
    "MAX_SKILL_PY_BYTES", "MAX_SKILL_PY_LINES", "MAX_TIMEOUT_S",
    "PACKAGE_FILES", "SCHEMA", "STATE_FILES", "SkillManifestError",
    "normalise", "sha256_file", "trigger_collision", "validate",
    "validate_dir", "validate_fixtures",
]
