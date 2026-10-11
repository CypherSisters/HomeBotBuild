"""Sources the registry does not have, proposed by the model. Never fetched.

The model **may** suggest a source outside the vetted
registry, **in online builds only**, and what it suggests is data on a card and
nothing else. So this module does three small things and refuses to do a
fourth:

1. :func:`registrable_hostname` - a name check that opens no socket. A URL
   whose host is an IP literal, a single label, ``localhost``, or a reserved
   internal-looking name never reaches the owner's screen at all.
2. :func:`suggest` - asks a client for names, and drops everything that does
   not survive the check. The client is injected; there is no default, so a
   build with no wired model silently suggests nothing rather than failing.
3. Labels every survivor ``not vetted: check the license yourself``.

It does not fetch, cannot fetch, and nothing it returns is written anywhere but
the ``sources`` section's ``suggested`` list.

**Hook status:** wired. :func:`default_client` builds the client in
:mod:`puffpai.wizard.sources_llm` - the build's own local model, through the
same Ollama client the rest of the backend uses - for an address and a model
this build actually has. Given neither it still returns ``None``, so a build
with no brain of its own suggests nothing rather than inventing something, and
a caller may still inject a client of its own.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""
from __future__ import annotations

import urllib.parse
from typing import Any, Callable, Optional

from puffpai.wizard import manifest as package_manifest
from puffpai.wizard import sources_llm

#: What the owner is told about every one of these, in these words.
UNVETTED_NOTE = "not vetted: check the license yourself"

MAX_SUGGESTIONS = 5

#: Names that look public and are not. A suggestion pointing at one of these is
#: a suggestion pointing back inside the house.
_RESERVED_TLDS = {"local", "localhost", "internal", "intranet", "lan", "home",
                  "corp", "test", "invalid", "example", "onion"}


def registrable_hostname(url: str) -> str:
    """The host of *url* if it is a registrable public name, else ``""``.

    No DNS, no connection: this runs inside ``propose``, which is read-only.
    ``source_fetch.guard_url`` still does the real work before any byte moves -
    this only decides whether a suggestion is fit to be *shown*.
    """
    try:
        parts = urllib.parse.urlsplit(str(url or "").strip())
    except ValueError:
        return ""
    if (parts.scheme or "").lower() not in ("http", "https"):
        return ""
    if parts.username or parts.password or "@" in (parts.netloc or ""):
        return ""
    try:
        host = (parts.hostname or "").strip().rstrip(".")
    except ValueError:
        return ""
    # The same check the package validator uses on a door host: a registrable
    # NAME, never an address, a wildcard, a scheme or a port. One rule, one
    # implementation - a suggestion is held to what a door is held to.
    if not package_manifest.is_registrable_hostname(host):
        return ""
    host = host.lower()
    if host.split(".")[-1] in _RESERVED_TLDS:
        return ""
    return host


def default_client(url: str = "", model: str = ""
                   ) -> Optional[Callable[[str], Any]]:
    """The model client Sources asks: *model* on the Ollama at *url*.

    ``None`` without both, which is what makes model suggestions absent rather
    than invented on a build whose brain is not there yet.
    """
    if not str(url or "").strip() or not str(model or "").strip():
        return None
    return sources_llm.client(url, model)


def _rows(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, str):
        # A client that hands back the model's own text: one strict parse,
        # which raises rather than guessing when the text is not a JSON list.
        raw = sources_llm.parse(raw)
    if isinstance(raw, dict):
        raw = raw.get("suggestions")
    return [item for item in (raw or []) if isinstance(item, dict)]


#: What the card says when the model was asked and nothing usable came back.
#: A quiet line, never an error: the suggestion was optional.
QUIET_NOTE = ("No suggestions this time - the local model did not answer with "
              "any. Nothing is missing from your build because of it.")


def suggest(goal: str, *, online: bool,
            client: Optional[Callable[[str], Any]] = None
            ) -> list[dict[str, Any]]:
    """Unvetted sources for a goal. Empty on an offline build, always."""
    return suggest_detail(goal, online=online, client=client)["rows"]


def suggest_detail(goal: str, *, online: bool,
                   client: Optional[Callable[[str], Any]] = None
                   ) -> dict[str, Any]:
    """``{"rows": [...], "note": str}`` - the suggestions and why there are none.

    Every row that comes back is squeezed through
    :func:`registrable_hostname`; a row without a showable host is dropped
    rather than shown with a warning, because a warning is not a filter. The
    note is set only when a client was actually asked and could not answer, so
    a build with no model wired says nothing rather than apologising.
    """
    if not online or not str(goal or "").strip():
        return {"rows": [], "note": ""}
    ask = client if client is not None else default_client()
    if ask is None:
        return {"rows": [], "note": ""}
    try:
        answer = ask(str(goal))
        items = _rows(answer)
    except Exception:  # noqa: BLE001 - a model that will not answer suggests nothing
        return {"rows": [], "note": QUIET_NOTE}

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        # Caps, control characters and the URL shape first, then the hostname.
        # A row that survives both is three short strings and nothing else.
        row = sources_llm.clean_row(item)
        if row is None:
            continue
        host = registrable_hostname(row["url"])
        if not host or host in seen:
            continue
        seen.add(host)
        out.append({
            "label": row["label"],
            "host": host,
            "url": row["url"],
            "why": row["why"],
            "note": UNVETTED_NOTE,
            "vetted": False,
            "fetchable": False,
        })
        if len(out) >= MAX_SUGGESTIONS:
            break
    return {"rows": out, "note": "" if out else QUIET_NOTE}


__all__ = ["UNVETTED_NOTE", "QUIET_NOTE", "MAX_SUGGESTIONS",
           "registrable_hostname", "default_client", "suggest",
           "suggest_detail"]
