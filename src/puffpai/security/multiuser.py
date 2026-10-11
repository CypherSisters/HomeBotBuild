"""Multi-user (owner + one other adult) Zone-1 access-control primitives —
Increment 1.

This module is the shared, dependency-light core of the two-principal per-user
Zone-1 boundary. It is deliberately tiny and importable everywhere (it imports
only the stdlib) so the api_server request layer, the vault, the RAG engine, the
chat-history store, and the scheduled pipelines can all agree on:

  * WHO the household principals are (the OWNER, plus a small set of supported
    non-owner users — for Increment 1 that is exactly one id, the single
    member of :data:`SUPPORTED_NON_OWNER_USERS`),
  * whether the feature is turned on (``PUFFPAI_MULTIUSER``, DEFAULT OFF), and
  * whether a given caller may reach a given Zone-1 record (``can_access``).

IMPORTANT — this is an ACCESS-CONTROL boundary, NOT a cryptographic one.
The owner explicitly chose single-vault + owner-tag filtering over per-user Data
Encryption Keys (DEKs) for Increment 1. Consequences, stated plainly:

  * Zone-1 data is tagged with its owning principal, and reads are filtered so a
    caller only reaches their own principal's data.
  * BUT the vault still uses a SINGLE global DEK, so that one key technically
    decrypts BOTH principals' data. The separation is enforced by this code
    path, not by cryptography. Anyone who can bypass this code path (e.g. read
    the raw vault files with the unlocked DEK) sees everything.
  * FUTURE UPGRADE: per-user DEKs (each principal's Zone-1 data wrapped under a
    key only that principal can unwrap) would make the boundary cryptographic.
    That is intentionally out of scope for Increment 1.

Fail-closed posture: an unknown / unauthenticated caller is the OWNER (the
default household principal); an unknown record owner is NOT collapsed to the
owner, so a record tagged for a principal we do not recognize is reachable by
nobody. Legacy Zone-1 data written before this feature carries no owner tag and
is treated as the OWNER's (see :func:`record_owner`).

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import os

#: The default household principal. Kiosk, unpaired, and un-vouched callers are
#: all treated as the owner. This mirrors the pre-multi-user single-user world:
#: with the flag off, EVERY caller is the owner.
OWNER_USER_ID = "owner"

#: The non-owner principals Increment 1 supports: deliberately ONE id, the
#: household's second adult. A caller who presents any other user id (even with
#: a valid voucher) is normalized to the owner — we do not mint principals we
#: have not built for.
#:
#: The value below is DEPLOYMENT CONFIGURATION, not a person: a box's vouched
#: callers and its owner-tagged Zone-1 records are keyed to this exact string,
#: so changing it on a running appliance orphans that user's records. The
#: shipped default is a placeholder; making it owner-configurable, with a
#: migration for the records already keyed to it, is the follow-up.
SUPPORTED_NON_OWNER_USERS = frozenset({"alex"})


def multiuser_enabled() -> bool:
    """True iff the two-principal Zone-1 boundary is turned on.

    Reads ``PUFFPAI_MULTIUSER`` (DEFAULT OFF). Truthy values: 1, true, yes, on
    (case-insensitive). Anything else — including unset — is OFF, so the box
    keeps today's exact single-user behavior until the owner opts in.

    Read from the environment (not ``puffpai.config``) on purpose, mirroring
    ``scrub_and_vault_enabled``: this increment ships without editing the shared
    config module, and the flag is a deployment-time switch, not a per-request
    or model-controlled value.
    """
    return os.environ.get("PUFFPAI_MULTIUSER", "").strip().lower() in (
        "1", "true", "yes", "on")


def caller_principal(user_id) -> str:
    """Normalize a CALLER identity to a supported principal.

    Used for the identity of whoever is making a request. Rules (fail-closed
    toward the owner, the least-surprising default that never exposes a
    non-owner's data):

      * ``None`` / empty / unknown user id  -> :data:`OWNER_USER_ID`
      * a recognized non-owner id           -> that id (lower-cased)
      * anything naming the owner            -> :data:`OWNER_USER_ID`

    An UNKNOWN caller becomes the owner (not a new principal) so that a bogus
    ``X-Puff-User`` value can never be treated as a distinct user whose data
    lookups would then... find nothing anyway, but more importantly so the
    principal set stays exactly {owner, the one supported non-owner id} in
    Increment 1.
    """
    if user_id is None:
        return OWNER_USER_ID
    u = str(user_id).strip().lower()
    if u in SUPPORTED_NON_OWNER_USERS:
        return u
    return OWNER_USER_ID


def record_owner(user_id) -> str:
    """Normalize the OWNER TAG recorded on a Zone-1 record.

    Unlike :func:`caller_principal`, this does NOT collapse an unrecognized tag
    to the owner: a record tagged for some principal we do not recognize stays
    tagged that way and is therefore reachable by nobody (fail-closed). Only the
    ABSENCE of a tag (``None`` / empty string) means "legacy data" and is
    attributed to the OWNER — Zone-1 content written before this feature existed
    belongs to the household owner and must stay visible to them.
    """
    if user_id is None:
        return OWNER_USER_ID
    u = str(user_id).strip().lower()
    return u or OWNER_USER_ID


def can_access(record_user_id, caller_user_id) -> bool:
    """True iff ``caller_user_id`` may read a Zone-1 record tagged
    ``record_user_id``.

    The caller side is normalized with :func:`caller_principal` (unknown ->
    owner) and the record side with :func:`record_owner` (missing -> owner,
    unknown -> itself). Access is granted only on an exact principal match, so:

      * owner reads owner-tagged and legacy(untagged) records; NOT the other
        principal's.
      * the other principal reads only records tagged for them; NOT the
        owner's, NOT legacy.
      * a record tagged for an unrecognized principal is reachable by nobody.

    This function does NOT consult the feature flag: callers apply it only when
    :func:`multiuser_enabled` is true, so that with the flag off there is no
    filtering at all (today's behavior).
    """
    return record_owner(record_user_id) == caller_principal(caller_user_id)


__all__ = [
    "OWNER_USER_ID",
    "SUPPORTED_NON_OWNER_USERS",
    "multiuser_enabled",
    "caller_principal",
    "record_owner",
    "can_access",
]
