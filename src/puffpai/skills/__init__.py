"""Skills — sandboxed, door-gated, consent-installed capability packages.

Phase 1 is the SUBSTRATE only: a skill is hand-authored,
staged as a draft, shown to the owner byte for byte, installed only against a
human approval, and thereafter runs **only** inside a bubblewrap jail whose
single outbound path is an egress door the owner opened for that exact host
set. Nothing in this package writes, researches or drafts code; no model
authors a skill here.

Why a package of its own rather than an extension of ``app_registry``: apps are
declarative by contract (``app_registry._FORBIDDEN_KEYS`` refuses a manifest
that names code to run) because "an app that wants to run code is the next tier
and needs a trust model that does not exist yet". This package is that trust
model, and keeping it separate means an app can never grow into a skill by
adding a key.

The four load-bearing properties, each enforced in one place:

* **Isolation** — :mod:`puffpai.skills.sandbox` builds the jail with the same
  flags as ``agents.toolsmith._jail_argv`` and no extra bind axis: the runner
  and a COPY of the skill are staged into the per-run root, so ``/work`` stays
  the only writable path and there is no caller-chosen mount.
* **Reach** — :mod:`puffpai.skills.broker` is the only way out of the jail, and
  it goes through :func:`puffpai.security.egress_doors.egress_request` against a
  door whose flag is digest-bound to the host set the owner approved.
* **Consent** — :mod:`puffpai.skills.install` proposes with a sha256 per file
  and applies only behind three gates (identity token, fresh unspent
  human-resolved approval, re-hash match).
* **Precedence** — :mod:`puffpai.skills.dispatch` answers LAST in the fastpath
  chain and only on whole-utterance equality with a trigger that collided with
  nothing already in the capability registry. A skill can never answer "turn
  off the lights".

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

__all__ = [
    "broker", "dispatch", "gap", "install", "lint", "manifest", "sandbox",
    "store", "submit",
]
