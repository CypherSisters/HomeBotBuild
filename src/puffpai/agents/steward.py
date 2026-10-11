"""Steward — WF-2 house operations (HA + media control).

The Steward is the *only* agent that holds the Home Assistant token and the
media-server API credentials (``AgentScope.STEWARD``). Concierge cannot control a
light or fetch a camera frame itself — it asks the Steward, whose state-changing
requests are veto-reviewed by Sentinel on the way in.

Tool surface: read-only (``list_devices``, ``device_status``, ``camera_frame``,
``camera_snapshot``, media queries) plus actuators that must arrive on a
``state_changing`` envelope so Sentinel has already ruled on them
(``control_device``, ``activate_scene``, ``media_play``, ``notify_user``,
``announce``, ``set_climate``, ``siren``, ``list_add``).

The **always-confirm class** is a property of the *target*, not of the intent
name that reaches it. :data:`puffpai.ha_bridge.SAFETY_CRITICAL_DOMAINS` is the
authority and :mod:`puffpai.ha_bridge` is where it is enforced — inside the
bridge's actuating methods, the last point before a service call leaves the
appliance, so no present or future *caller* can route around it. The gate used
to live here in :class:`_GuardedHA`, which protected only the callers that came
through the Steward; the legacy agent's LLM tools and the ``/ha/*`` HTTP routes
did not. :class:`_GuardedHA` remains as the Steward's envelope adapter: it
decides whether an envelope carries a real human confirmation and shapes the
refusal into a proposal. Anything in that class can be proposed autonomously;
only a human-originated confirmation executes it (:func:`human_confirmed`).

Composition, not rewrite: this wraps the existing :class:`HABridge` and
``MediaBridge`` singletons. ``media_bridge`` is imported/wrapped but never
modified (owned by another workstream).

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import ipaddress
import os
import re
from urllib.parse import urlsplit

# The always-confirm policy lives in :mod:`puffpai.ha_bridge` — one layer below
# this one, at the last point before a service call leaves the appliance. It is
# imported (and re-exported) rather than restated here so the catalogue and the
# enforcer cannot drift: several callers actuate HA without ever passing through
# the Steward, and a policy that lived up here could not see them.
from puffpai.ha_bridge import (
    HUMAN_CONFIRMED,
    SAFETY_CRITICAL_DOMAINS,
    ConfirmationRequired,
    as_targets,
    entity_domain,
    enforce_confirmation,
    stored_instruction_domains,
    touched_domains,
)
from puffpai.logging_config import get_logger
from puffpai.security.secrets_provider import AgentScope, SecretsProvider

from .base import BaseAgent
from .envelope import Envelope

logger = get_logger(__name__)


# ── intent classification ───────────────────────────────────────────
# Declaring this here (rather than leaving it implicit in ``handle``) means the
# envelope *minter* can look up whether an intent needs ``state_changing=True``,
# and ``handle`` can refuse anything that arrived without it — i.e. anything
# that skipped Sentinel's structural veto in ``bus.send``.
STATE_CHANGING_INTENTS = frozenset({
    "control_device", "control", "activate_scene", "media_play",
    "notify_user", "announce", "set_climate", "siren", "list_add",
    # ``zigbee_pair`` writes to the consent queue and nothing else — it opens no
    # pairing window (see :meth:`StewardAgent._zigbee_pair`). It is in this set
    # because it *changes durable state*, which is what earns Sentinel's review
    # in ``bus.send``; the window itself is opened only by
    # ``puffpai.zigbee_pairing.complete_approved_pairing``, from an approval
    # surface, after a human answers the request this intent files.
    "zigbee_pair",
})

#: Never autonomous. Proposing is always allowed; executing
#: needs a human-originated confirmation on the envelope.
#:
#: This is a *convenience* list, not the authority: an intent name says nothing
#: about what it will actuate. ``control_device`` accepts any domain and derives
#: the HA service domain from the entity id, so ``control_device`` on
#: ``siren.shed_siren`` would fire ``siren.turn_on`` while sailing past a gate
#: that only knew the string ``"siren"``. :data:`SAFETY_CRITICAL_DOMAINS` is the
#: authority; this set only pins intents that are always-confirm regardless of
#: target.
CONFIRM_REQUIRED_INTENTS = frozenset({"siren"})

#: **The always-confirm authority**, re-exported from :mod:`puffpai.ha_bridge`
#: where it is defined and enforced. It lives a layer down because the
#: Steward is not the only way to reach Home Assistant: the legacy agent's LLM
#: tools and the ``/ha/*`` HTTP routes call the bridge directly, so a gate that
#: lived here could only protect the callers that chose to come through here.
#: Imported rather than restated so there is exactly one definition.
#:
#: See :data:`puffpai.ha_bridge.SAFETY_CRITICAL_DOMAINS` for why each domain is
#: in the set. Deliberately NOT in it: ``climate`` (setpoints are already
#: clamped to 50-90F by :func:`StewardAgent._temperature`), ``light``,
#: ``switch``, ``media_player``.

#: ``origin_agent`` values that represent a human entry point rather than an
#: agent. Agents stamp their own ``self.name`` when they mint an envelope, so an
#: agent cannot place itself in this set without lying about its identity.
HUMAN_ORIGINS = frozenset({"human", "user", "owner", "api", "dashboard", "voice"})


def requires_human_confirmation(*, intent: str | None = None,
                                entity_id=None,
                                domains=(),
                                state_changing: bool = True) -> bool:
    """Single source of truth for the always-confirm class.

    Callers anywhere in the team (the Steward's own gate, the planning
    loop's pre-execution check) must ask this rather than keep a private list —
    a catalogue and an enforcer that each carry their own idea of "always
    confirm" will drift, and the drift is a bypass.

    Args:
        intent: the intent name, for the small set that is always-confirm
            whatever it points at (:data:`CONFIRM_REQUIRED_INTENTS`).
        entity_id: the resolved target, if there is one. A ``str`` or an
            iterable of them.
        domains: HA domains the action is *declared* to touch, for callers that
            know the class before a target resolves.
        state_changing: reads never actuate, so they are never in the class.
            ``device_status`` on a lock is a question, not an unlocking.
    """
    if intent is not None and str(intent) in CONFIRM_REQUIRED_INTENTS:
        return True
    if not state_changing:
        return False
    targets = ([entity_id] if isinstance(entity_id, str) or entity_id is None
               else list(entity_id))
    if any(entity_domain(target) in SAFETY_CRITICAL_DOMAINS for target in targets):
        return True
    return any(str(domain).strip().lower() in SAFETY_CRITICAL_DOMAINS
               for domain in (domains or ()))


def human_confirmed(envelope: Envelope, payload: dict | None = None) -> bool:
    """The three conditions that let an always-confirm action actually run.

    1. ``state_changing=True`` on the envelope — Sentinel reviewed it;
    2. ``payload["confirmed"] is True``;
    3. ``origin_agent`` in :data:`HUMAN_ORIGINS` — a human entry point.

    An agent can satisfy the first two on its own and never the third: agents
    stamp their own ``name`` as ``origin_agent``, so an agent minting its own
    confirmation lands outside the set.
    """
    if payload is None:
        payload = getattr(envelope, "payload", None) or {}
    return (bool(getattr(envelope, "state_changing", False))
            and payload.get("confirmed") is True
            and getattr(envelope, "origin_agent", None) in HUMAN_ORIGINS)

MAX_TEXT_LEN = 500
MAX_TITLE_LEN = 120
MAX_ITEM_LEN = 255

#: Refuse setpoints outside a band a house thermostat should ever see (°F).
MIN_TEMP_F = 50.0
MAX_TEMP_F = 90.0

#: HA's documented ``climate`` hvac modes.
HVAC_MODES = frozenset({
    "off", "heat", "cool", "heat_cool", "auto", "dry", "fan_only",
})

#: States that mean "the device is not there right now".
UNAVAILABLE_STATES = frozenset({"unavailable", "unknown", "none", ""})

# ── announce: URL + service policy ──────────────────────────────────
#
# ``announce`` hands a caller-supplied URL to Home Assistant, which *fetches it*
# — so an unvalidated string here is a server-side request forgery primitive
# pointed at the HA host, on the house LAN, with HA's own credentials and
# network position. ``file:///etc/shadow``, ``http://169.254.169.254/latest/
# meta-data/`` and ``javascript:`` must never pass through untouched. The
# validation below mirrors the allow-list-don't-escape style already used for HA
# path segments (:mod:`puffpai.ha_bridge`).

#: Only these two schemes ever reach HA. Everything else — ``file``, ``data``,
#: ``javascript``, ``gopher``, ``ftp`` — is refused outright.
ALLOWED_URL_SCHEMES = frozenset({"http", "https"})

#: Hard cap on an announcement URL.
MAX_URL_LEN = 2048

#: Name forms that always resolve inside the house and so can never be a
#: legitimate *public* announcement source.
LOCAL_HOST_NAMES = frozenset({"localhost", "localhost.localdomain"})
LOCAL_HOST_SUFFIXES = (".local", ".localhost", ".internal", ".home.arpa", ".lan")

#: A final DNS label that is really a number — decimal, octal (leading zero) or
#: hex. ``http://127.1/``, ``http://2130706433/`` and ``http://0x7f000001/`` are
#: all loopback to a permissive HTTP client, but none of them parse as an
#: address under :mod:`ipaddress`, so they would otherwise be taken for names.
_NUMERIC_LABEL_RE = re.compile(r"\A(?:0[xX][0-9a-fA-F]+|[0-9]+)\Z")

#: Comma-separated extra hostnames an announcement URL may name, for a house
#: whose media host this appliance cannot derive from config.
ANNOUNCE_HOSTS_ENV = "PUFFPAI_ANNOUNCE_URL_HOSTS"

#: The one service each ``announce`` branch may call, and the full set a caller
#: is allowed to name. Checking ``service`` only with ``validate_segment``,
#: a *shape* check, would leave every service in ``assist_satellite`` and
#: ``music_assistant`` reachable: ``start_conversation`` (opens a live
#: conversation on a house satellite) and ``play_media`` (plays anything, not
#: just an announcement) among them. Allow-listed per branch instead.
ANNOUNCE_DEFAULT_SERVICE = {
    "assist_satellite": "announce",
    "media_player": "play_announcement",
}
ANNOUNCE_ALLOWED_SERVICES = {
    "assist_satellite": frozenset({"announce"}),
    "media_player": frozenset({"play_announcement"}),
}


class StewardInputError(ValueError):
    """Bad payload — rejected before any HTTP request leaves the appliance."""


class EntityUnavailable(RuntimeError):
    """The target entity exists in HA but is not currently usable."""

    def __init__(self, entity_id: str, state: str) -> None:
        super().__init__(f"device unavailable: {entity_id} (state={state!r})")
        self.entity_id = entity_id
        self.state = state


class _GuardedHA:
    """Envelope-bound proxy over the HA bridge — the Steward's half of the gate.

    The *policy* now lives in :mod:`puffpai.ha_bridge` and is enforced there, on
    the wire. This proxy's job is to translate between an :class:`Envelope` and
    that policy in both directions:

    * **inbound** — decide whether this envelope carries a real human
      confirmation (:func:`human_confirmed`: state-changing, ``confirmed=True``,
      *and* a human origin) and, only then, hand the bridge the
      :data:`HUMAN_CONFIRMED` token;
    * **outbound** — let :class:`ConfirmationRequired` propagate so
      :meth:`StewardAgent.handle` can turn it into a proposal reply, which is
      what the rest of the team expects to receive.

    It also *applies* the shared policy itself, via the same
    :func:`enforce_confirmation` function the bridge calls, rather than relying
    on the object underneath to do it. That is not duplicated policy — it is one
    function called from two places — and it matters because the bridge is
    injectable: a test double or a future non-``HABridge`` backend must not be
    able to remove the gate simply by not implementing it.

    Reads and every other bridge method pass straight through untouched.
    """

    __slots__ = ("_bridge", "_envelope")

    def __init__(self, bridge, envelope: Envelope) -> None:
        self._bridge = bridge
        self._envelope = envelope

    def __getattr__(self, name):
        # Reads (get_entity_state, get_camera_image, list_devices, ...) and any
        # future non-actuating method. The three actuators below shadow this.
        #
        # The slot guard matters: if ``_bridge`` were ever unset (an aborted
        # __init__, an unpickle), the plain ``self._bridge`` below would land
        # back in __getattr__ and recurse until the stack died — a confusing
        # crash in the one class whose failure mode should be legible.
        if name in ("_bridge", "_envelope"):
            raise AttributeError(
                f"_GuardedHA has no {name!r}: it was not constructed properly, "
                "and an unbound guard must never be used as a bridge")
        return getattr(self._bridge, name)

    # ── the actuating methods ───────────────────────────────────────
    #
    # Every method here that can reach the wire is shadowed, including the ones
    # the *real* bridge already gates. That redundancy is the point: the bridge
    # is injectable, so a method left to ``__getattr__`` is a method whose gate
    # exists only if the object underneath happens to implement it. A test
    # double or a future non-``HABridge`` backend must not be able to remove the
    # gate by simply not having one.

    def _expand(self, entity_id, domains, *, service_domain=None,
                service=None):
        """Fold in what any wrapper target would itself actuate.

        Expansion runs against *this proxy*, so the reads used to classify are
        the reads the bridge would do, and a wrapper that cannot be inspected
        fails closed here rather than one layer down. Mirrors
        ``HABridge._expand_stored_targets``, including the ``script.<object_id>``
        service form, which names the script it runs in the *service* rather
        than in any entity id.
        """
        targets = [t for t in as_targets(entity_id)
                   if entity_domain(t) in ("scene", "script", "automation")]
        name = str(service or "").strip().lower()
        if (str(service_domain or "").strip().lower() == "script"
                and name and name not in ("turn_on", "turn_off", "toggle",
                                          "reload")):
            implied = f"script.{name}"
            if implied not in targets:
                targets.append(implied)

        force = False
        reasons = []
        for target in dict.fromkeys(targets):
            found, known, why = stored_instruction_domains(self, target)
            domains |= found
            if not known:
                force = True
                if why and why not in reasons:
                    reasons.append(why)
        return force, "; ".join(reasons)

    def call_service(self, domain, service, entity_id=None, **data):
        domains = touched_domains(domain, entity_id)
        force, why = self._expand(entity_id, domains, service_domain=domain,
                                  service=service)
        self._gate(domains, f"{domain}.{service}", entity_id,
                   force=force, why=why)
        return self._bridge.call_service(domain, service, entity_id=entity_id,
                                         **self._token(), **data)

    def control_device(self, entity_id, action, **kwargs):
        domains = touched_domains(None, entity_id)
        force, why = self._expand(entity_id, domains)
        self._gate(domains, f"{entity_domain(entity_id) or '?'}.{action}",
                   entity_id, force=force, why=why)
        return self._bridge.control_device(entity_id, action,
                                           **self._token(), **kwargs)

    def activate_scene(self, entity_id):
        # ``activate_scene`` accepts ``script.*`` as well as ``scene.*`` (the
        # bridge derives the service domain from the entity id), and a script
        # has no member list — so classifying it needs its stored steps, not a
        # scene lookup. ``stored_instruction_domains`` dispatches on the domain
        # and fails closed for anything it cannot resolve through this proxy.
        domains, known, why = stored_instruction_domains(self, entity_id)
        self._gate(domains, f"{entity_domain(entity_id) or 'scene'}.turn_on",
                   entity_id, force=not known, why=why)
        return self._bridge.activate_scene(entity_id, **self._token())

    def trigger_automation(self, entity_id):
        domains, known, why = stored_instruction_domains(self, entity_id)
        self._gate(domains, "automation.trigger", entity_id,
                   force=not known, why=why)
        return self._bridge.trigger_automation(entity_id, **self._token())

    def toggle_automation(self, entity_id, enable):
        domains, known, why = stored_instruction_domains(self, entity_id)
        self._gate(domains, f"automation.{'enable' if enable else 'disable'}",
                   entity_id, force=not known, why=why)
        return self._bridge.toggle_automation(entity_id, enable,
                                              **self._token())

    # ── envelope → policy ───────────────────────────────────────────
    def _token(self) -> dict:
        """The confirmation token to forward to the bridge, if any.

        Returned as kwargs-to-splat rather than a value so that the *absence* of
        a confirmation is the absence of the argument. Two reasons:

        1. It keeps the bridge's own default (refuse) in charge — this proxy can
           only ever hand over a real token, never a falsy stand-in that some
           future ``if human_confirmed:`` might read the wrong way.
        2. The bridge is injectable. A test double or alternative backend that
           does not declare ``ENFORCES_CONFIRMATION`` never receives the token
           at all, because for such an object the keyword would land in its
           ``**kwargs`` and be forwarded to HA as if it were service data. The
           gate is not weakened by withholding it: ``_gate`` above has already
           run, using the same shared policy function the real bridge uses.
        """
        if not getattr(self._bridge, "ENFORCES_CONFIRMATION", False):
            return {}
        if not human_confirmed(self._envelope):
            return {}
        return {"human_confirmed": HUMAN_CONFIRMED}

    # ── the gate ────────────────────────────────────────────────────
    def _gate(self, domains, service: str, entity_id, *,
              force: bool = False, why: str = "") -> None:
        """Apply the shared policy, with an envelope-shaped explanation.

        The verdict comes from :func:`enforce_confirmation` — the same function
        :class:`HABridge` calls — so this cannot drift from what the wire does.
        Only the *reason text* is Steward-specific, because only the Steward
        knows what a caller would have to change to make it succeed.

        *why* carries the classifier's own account of what it could not
        determine (an unreadable script's steps, an unreadable scene's members).
        It is put first: "cannot inspect this script's steps" is the fact, and
        "confirm it from a human origin" is the remedy, and a refusal that leads
        with the remedy teaches people to click through it.
        """
        remedy = ("it needs payload confirmed=True on a state-changing "
                  "envelope from a human origin (got "
                  f"{getattr(self._envelope, 'origin_agent', None)!r})")
        enforce_confirmation(
            domains, service=service, entity_id=entity_id,
            confirmation=(HUMAN_CONFIRMED if human_confirmed(self._envelope)
                          else False),
            force=force,
            detail=f"{why}; {remedy}" if why else remedy)


class StewardAgent(BaseAgent):
    """House + media actuator."""

    name = "steward"
    scope = AgentScope.STEWARD

    def __init__(self, *, secrets: SecretsProvider | None = None,
                 ha_bridge=None, media_bridge=None,
                 announce_hosts=None) -> None:
        super().__init__(secrets=secrets)
        self._ha = ha_bridge
        self._media = media_bridge
        #: Resolved lazily from config + env; injected in tests.
        self._announce_host_cache = (
            {str(h).lower().rstrip(".") for h in announce_hosts}
            if announce_hosts is not None else None)

    # ── engine accessors (lazy, guarded) ────────────────────────────
    def _ha_bridge(self):
        """The RAW bridge. Only for non-request use (health checks).

        Request paths must use :meth:`_house` — that is where the always-confirm
        gate lives, and a raw handle in a request path is a hole.
        """
        if self._ha is None:
            from puffpai.ha_bridge import get_bridge
            self._ha = get_bridge()
        return self._ha

    def _house(self, envelope: Envelope) -> _GuardedHA:
        """The HA bridge as *this envelope* is allowed to use it.

        Bound per call rather than per agent, so the gate reads the actual
        envelope's origin and confirmation and there is no shared mutable state
        between concurrent requests.
        """
        return _GuardedHA(self._ha_bridge(), envelope)

    def _media_bridge(self):
        if self._media is None:
            from puffpai.media_bridge import get_media_bridge
            self._media = get_media_bridge()
        return self._media

    # ── request handling ────────────────────────────────────────────
    def handle(self, envelope: Envelope) -> dict:
        from puffpai.ha_bridge import HABridgeError

        intent = envelope.intent
        p = envelope.payload or {}

        # An actuating intent that arrived on a read-only envelope never went
        # through Sentinel's veto in ``bus.send``. Fail closed.
        if intent in STATE_CHANGING_INTENTS and not envelope.state_changing:
            return {
                "ok": False,
                "requires_review": True,
                "error": f"steward: {intent!r} is state-changing; the envelope must "
                         "set state_changing=True so Sentinel reviews it",
            }

        try:
            # ── read-only house queries ──
            if intent in ("list_devices", "devices"):
                ha = self._house(envelope)
                devices = ha.list_devices(domain=p.get("domain"),
                                          controllable_only=p.get("controllable_only", True))
                return {"ok": True, "devices": [self._device_dict(d) for d in devices]}
            if intent in ("device_status", "entity_state"):
                ha = self._house(envelope)
                return {"ok": True, "state": ha.get_entity_state(p["entity_id"])}
            if intent == "camera_frame":
                ha = self._house(envelope)
                envelope.budget.consume(1)  # a fetch is a metered action
                image, content_type = ha.get_camera_image(p["entity_id"])
                return {"ok": True, "content_type": content_type, "bytes": len(image)}
            if intent == "camera_snapshot":
                return self._camera_snapshot(envelope, p)
            if intent == "zigbee_pair_status":
                from puffpai import zigbee_pairing
                return zigbee_pairing.pairing_status()

            # ── state-changing house actions (veto-reviewed by the bus) ──
            if intent in ("control_device", "control"):
                ha = self._house(envelope)
                envelope.budget.consume(1)
                result = ha.control_device(p["entity_id"], p["action"],
                                           **p.get("kwargs", {}))
                return {"ok": True, "result": result}
            if intent == "activate_scene":
                return self._activate_scene(envelope, p)
            if intent == "notify_user":
                return self._notify_user(envelope, p)
            if intent == "announce":
                return self._announce(envelope, p)
            if intent == "set_climate":
                return self._set_climate(envelope, p)
            if intent == "siren":
                return self._siren(envelope, p)
            if intent == "list_add":
                return self._list_add(envelope, p)
            if intent == "zigbee_pair":
                return self._zigbee_pair(envelope, p)

            # ── media ──
            if intent == "media_status":
                return {"ok": True, "status": self._media_bridge().get_status()}
            if intent == "media_search":
                import re as _re
                media = self._media_bridge()
                kind = p.get("kind", "music")
                # Turn a spoken request ("what sci-fi movies do I have") into a
                # search term + optional genre. A title search for a genre word
                # matches nothing, so genre words route to the genre filter.
                _genres = {
                    "action": "Action", "adventure": "Adventure",
                    "animation": "Animation", "anime": "Animation",
                    "comedy": "Comedy", "comedies": "Comedy", "crime": "Crime",
                    "documentary": "Documentary",
                    "documentaries": "Documentary", "drama": "Drama",
                    "dramas": "Drama", "family": "Family", "fantasy": "Fantasy",
                    "history": "History", "historical": "History",
                    "horror": "Horror", "musical": "Music",
                    "mystery": "Mystery", "romance": "Romance",
                    "romantic": "Romance", "rom com": "Romance",
                    "romcom": "Romance", "sci-fi": "Science Fiction",
                    "scifi": "Science Fiction", "sci fi": "Science Fiction",
                    "science fiction": "Science Fiction",
                    "thriller": "Thriller", "thrillers": "Thriller",
                    "war": "War", "western": "Western", "westerns": "Western",
                }
                _q = (p.get("query", "") or "").lower().strip()
                _q = _re.sub(r"^(what|which|show me|list|do i have|do we have|"
                             r"have i got|are there|tell me|find|"
                             r"search(?: for)?)\b", " ", _q)
                _q = _re.sub(r"\b(do i have|do we have|that i have|i have|"
                             r"in my library|in the library|library|my|any|"
                             r"all|the|some)\b", " ", _q)
                for _w in ("audiobooks", "audiobook", "movies", "movie",
                           "films", "film", "flicks", "books", "book", "music",
                           "songs", "song", "albums", "album", "tracks"):
                    _q = _re.sub(r"\b" + _w + r"\b", " ", _q)
                _q = _re.sub(r"[^\w\s-]", " ", _q)
                _q = _re.sub(r"\s+", " ", _q).strip()
                genre = _genres.get(_q, "")
                term = "" if genre else _q
                limit = p.get("limit", 20)
                if kind == "movies":
                    items = media.search_movies(term, genre=genre, limit=limit)
                elif kind == "books":
                    items = media.search_books(term, limit=limit)
                else:
                    items = media.search_music(term, genre=genre, limit=limit)
                _titles, _seen = [], set()
                for _i in items:
                    _d = _i.display
                    if _d.lower() not in _seen:
                        _seen.add(_d.lower())
                        _titles.append(_d)
                return {"ok": True, "count": len(_titles), "kind": kind,
                        "genre": genre, "titles": _titles}
            if intent == "media_play":
                media = self._media_bridge()
                envelope.budget.consume(1)
                return {"ok": True, "result": media.play_music(p["item_id"],
                                                               p.get("session_id", ""))}

            return {"ok": False, "error": f"steward: unknown intent {intent!r}"}
        except ConfirmationRequired as exc:
            # The always-confirm class, refused at the choke point. Proposing is
            # always allowed, so this is an ``ok`` reply carrying the exact
            # service the household would be approving.
            return {"ok": True, "executed": False, "requires_confirmation": True,
                    "proposal": exc.proposal, "reason": exc.reason}
        except KeyError as exc:
            return {"ok": False, "error": f"steward: missing payload key {exc}"}
        except StewardInputError as exc:
            return {"ok": False, "error": f"steward: {exc}"}
        except EntityUnavailable as exc:
            return {"ok": False, "unavailable": True,
                    "entity_id": exc.entity_id, "state": exc.state,
                    "error": f"steward: {exc}"}
        except HABridgeError as exc:
            return {"ok": False, "error": f"steward: home assistant: {exc}"}

    def _activate_scene(self, envelope: Envelope, p: dict) -> dict:
        """Activate a scene — and only a scene.

        ``HABridge.activate_scene`` derives the service domain from the entity
        id and calls ``<domain>.turn_on``, so it will happily "activate"
        ``cover.garage_door``. Restricting the target to the ``scene`` domain
        turns it back into what its name claims.

        ``script.*`` is refused here as well, and that is now belt *and* braces
        rather than the only line of defence: :class:`_GuardedHA` classifies a
        script by its stored steps, so a script that reaches the bridge by some
        other route is gated on what it would actuate. This check stays because
        ``activate_scene`` should mean "activate a scene" regardless.

        Scene membership and script steps are both expanded underneath, in
        :class:`_GuardedHA`.
        """
        from puffpai.ha_bridge import validate_entity_id

        ha = self._house(envelope)
        entity_id = p["entity_id"]
        validate_entity_id(entity_id)          # malformed ids die here, as before
        if entity_domain(entity_id) != "scene":
            raise StewardInputError(
                f"activate_scene: {entity_id} is not a scene entity")
        envelope.budget.consume(1)
        return {"ok": True, "result": ha.activate_scene(entity_id)}

    # ── house tools ──────────────────────────────────────────────────
    def _notify_user(self, envelope: Envelope, p: dict) -> dict:
        """Push a notification — HA ``notify.<service>``.

        Defaults to ``notify.persistent_notification`` (always present, shows in
        the HA UI). ``service`` may name any other configured notify service
        (e.g. a mobile_app target); it is validated as a single path segment, so
        it can only ever address something inside the ``notify`` domain.
        """
        from puffpai.ha_bridge import validate_segment

        ha = self._house(envelope)
        service = validate_segment(
            p.get("service") or "persistent_notification", kind="notify service")
        message = self._clean_text(p.get("message"), field="message")
        title = self._clean_text(p.get("title"), field="title",
                                 max_len=MAX_TITLE_LEN, required=False)

        data: dict = {"message": message}
        if title:
            data["title"] = title

        envelope.budget.consume(1)
        ha.call_service("notify", service, **data)
        return {"ok": True, "service": f"notify.{service}", "message": message}

    def _announce(self, envelope: Envelope, p: dict) -> dict:
        """Speak to the house.

        * ``assist_satellite.*`` target → ``assist_satellite.announce`` with the
          text (discovered automatically when no ``entity_id`` is given).
        * ``media_player.*`` target → ``music_assistant.play_announcement``,
          which plays an *audio file*: the payload must carry ``url``. Text is
          not synthesised here — PuffPAI's own TTS path owns that.

        ``service`` names one of :data:`ANNOUNCE_ALLOWED_SERVICES` for the
        branch, and ``url`` is checked by :meth:`_announce_url` before anything
        is handed to HA to fetch.
        """
        from puffpai.ha_bridge import validate_entity_id

        ha = self._house(envelope)
        text = self._clean_text(p.get("text"), field="text")

        entity_id = p.get("entity_id") or self._find_assist_satellite(ha)
        if not entity_id:
            raise StewardInputError(
                "announce: no assist_satellite entity found; pass entity_id")
        validate_entity_id(entity_id)
        domain = entity_id.split(".")[0]

        override = p.get("service")
        if domain == "assist_satellite":
            service_domain = "assist_satellite"
            service = self._announce_service(domain, override)
            data = {"message": text}
        elif domain == "media_player":
            service_domain = "music_assistant"
            service = self._announce_service(domain, override)
            data = {"url": self._announce_url(p.get("url"))}
        else:
            raise StewardInputError(
                f"announce: cannot announce to domain {domain!r}")

        self._require_entity(ha, entity_id)
        envelope.budget.consume(1)
        ha.call_service(service_domain, service, entity_id=entity_id, **data)
        return {"ok": True, "service": f"{service_domain}.{service}",
                "entity_id": entity_id, "text": text}

    def _camera_snapshot(self, envelope: Envelope, p: dict) -> dict:
        """Grab a still from a camera.

        Deliberately *not* HA's ``camera.snapshot`` service: that writes a file
        on the HA host into an ``allowlist_external_dirs`` path, which this
        appliance cannot read back and which the house has not configured. The
        authenticated ``camera_proxy`` fetch returns the bytes to PuffPAI
        directly, so the image is usable in the same request that asked for it.

        Read-only with respect to the house; metered like ``camera_frame``.
        Set ``include_image`` to get base64 back alongside the metadata.
        """
        ha = self._house(envelope)
        entity_id = p["entity_id"]
        self._require_entity(ha, entity_id, domains={"camera"})

        envelope.budget.consume(1)
        image, content_type = ha.get_camera_image(entity_id)
        result = {"ok": True, "entity_id": entity_id, "source": "camera_proxy",
                  "content_type": content_type, "bytes": len(image)}
        if p.get("include_image"):
            import base64
            result["image_b64"] = base64.b64encode(image).decode("ascii")
        return result

    def _set_climate(self, envelope: Envelope, p: dict) -> dict:
        """Thermostat setpoint and/or mode.

        ``climate.set_hvac_mode`` then ``climate.set_temperature`` — each is a
        separate metered action. Out-of-band temperatures and unknown hvac modes
        are refused here rather than forwarded to HA.
        """
        ha = self._house(envelope)
        entity_id = p["entity_id"]

        temperature = (self._temperature(p.get("temperature"))
                       if p.get("temperature") is not None else None)
        hvac_mode = (self._hvac_mode(p.get("hvac_mode"))
                     if p.get("hvac_mode") is not None else None)
        if temperature is None and hvac_mode is None:
            raise StewardInputError(
                "set_climate: needs 'temperature' and/or 'hvac_mode'")

        self._require_entity(ha, entity_id, domains={"climate"})

        services: list[str] = []
        if hvac_mode is not None:
            envelope.budget.consume(1)
            ha.call_service("climate", "set_hvac_mode", entity_id=entity_id,
                            hvac_mode=hvac_mode)
            services.append("climate.set_hvac_mode")
        if temperature is not None:
            envelope.budget.consume(1)
            ha.call_service("climate", "set_temperature", entity_id=entity_id,
                            temperature=temperature)
            services.append("climate.set_temperature")

        return {"ok": True, "entity_id": entity_id, "services": services,
                "temperature": temperature, "hvac_mode": hvac_mode}

    def _siren(self, envelope: Envelope, p: dict) -> dict:
        """Sound or silence a siren — **always-confirm class**.

        Per the safety envelope, a siren is never autonomous. The agent
        team may *propose* one: an unconfirmed call validates the target, makes
        no HTTP request, and returns ``executed=False`` with the exact service
        it would run, so Concierge can ask the household.

        Execution requires all three, and the agent team can satisfy only the
        first two on its own:

        1. ``state_changing=True`` on the envelope (Sentinel reviewed it),
        2. ``payload["confirmed"] is True``,
        3. ``origin_agent`` in :data:`HUMAN_ORIGINS` — a human entry point.
           Agents stamp their own ``name`` as ``origin_agent``, so an agent
           minting its own confirmation lands outside this set and is refused.
        """
        from puffpai.ha_bridge import validate_entity_id

        ha = self._house(envelope)
        entity_id = p["entity_id"]
        action = p.get("action", "turn_on")
        if action not in ("turn_on", "turn_off"):
            raise StewardInputError(
                f"siren: action must be turn_on or turn_off, not {action!r}")
        validate_entity_id(entity_id)
        if entity_id.split(".")[0] != "siren":
            raise StewardInputError(f"siren: {entity_id} is not a siren entity")

        proposal = {"service": f"siren.{action}", "entity_id": entity_id}
        if not self._human_confirmed(envelope, p):
            return {
                "ok": True, "executed": False, "requires_confirmation": True,
                "proposal": proposal,
                "reason": "siren is always-confirm: needs payload confirmed=True "
                          f"from a human origin (got {envelope.origin_agent!r})",
            }

        self._require_entity(ha, entity_id, domains={"siren"})
        envelope.budget.consume(1)
        ha.call_service("siren", action, entity_id=entity_id)
        return {"ok": True, "executed": True, **proposal}

    def _add_to_shopping(self, envelope: Envelope, item: str) -> dict:
        """Write one item to the CANONICAL shopping list and read it back.

        The canonical list is FreshTable's ``shoppingList`` — the rows the
        FreshTable app and the dashboard tile render, the rows
        ``app_add_shopping``/``app_list_shopping`` use, and the rows the
        document ingester writes a scanned list into. Nothing in this tree ever
        READ Home Assistant's ``shopping_list``, so the chat write that went
        there was invisible everywhere the household actually looks. HA is
        mirrored afterwards, best-effort, for the companion app.

        The write, the read-back and the wording all live in
        :mod:`puffpai.agents.shopping_writes` so the Steward, the Concierge's
        renderer and the chat fastpath cannot drift into three accounts of one
        list.
        """
        from puffpai.agents import shopping_writes
        try:
            ha = self._house(envelope)
        except Exception as exc:      # HA down must not block the app write
            logger.debug("no house bridge for the shopping mirror: %s", exc)
            ha = None
        return shopping_writes.add_verified(item, ha=ha)

    def _list_add(self, envelope: Envelope, p: dict) -> dict:
        """Add an item to a household list.

        ``list="todo"`` (default when an ``entity_id`` is given) →
        ``todo.add_item`` against that ``todo.*`` entity, which surfaces in the
        HA companion app.
        ``list="shopping"`` → FreshTable's ``shoppingList``, verified by a
        read-back (see :meth:`_add_to_shopping`), mirrored to HA afterwards.
        """
        item = self._clean_text(p.get("item"), field="item",
                                max_len=MAX_ITEM_LEN)
        entity_id = p.get("entity_id")
        which = p.get("list") or ("todo" if entity_id else "shopping")
        if which not in ("todo", "shopping"):
            raise StewardInputError(
                f"list_add: list must be 'todo' or 'shopping', not {which!r}")

        if which == "todo":
            ha = self._house(envelope)
            if not entity_id:
                raise StewardInputError(
                    "list_add: list='todo' needs a todo.* entity_id")
            self._require_entity(ha, entity_id, domains={"todo"})
            envelope.budget.consume(1)
            ha.call_service("todo", "add_item", entity_id=entity_id, item=item)
            return {"ok": True, "list": "todo", "service": "todo.add_item",
                    "entity_id": entity_id, "item": item}

        envelope.budget.consume(1)
        return self._add_to_shopping(envelope, item)

    # ── Zigbee pairing: file the question, never open the window ────
    def _zigbee_pair(self, envelope: Envelope, p: dict) -> dict:
        """Ask the owner whether the Zigbee pairing window may open.

        **This handler actuates nothing.** It writes one Sentinel consent
        request and returns the sentence the owner should read. The window is
        opened only by
        :func:`puffpai.zigbee_pairing.complete_approved_pairing`, which the two
        approval surfaces call after the owner answers, and which refuses
        without that answer on the durable record.

        The intent is nevertheless in :data:`STATE_CHANGING_INTENTS`: it writes
        durable state, and an intent that writes must arrive on an envelope
        Sentinel reviewed. It is deliberately absent from the planning loop's
        CATALOGUE, so the planner cannot reach even this half of the flow — a
        pairing window is human-initiated or it does not happen.

        ``self._house`` is not used: there is no HA call to make here, and
        taking a bridge handle in a path that must not actuate would be a hole
        waiting for a future edit.
        """
        from puffpai import zigbee_pairing

        envelope.budget.consume(1)      # writing to the consent queue is an act
        reason = p.get("task_text") or p.get("text") or ""
        return zigbee_pairing.request_pairing(reason)

    # ── announce input validation (never trust the URL) ─────────────
    @staticmethod
    def _announce_service(domain: str, override) -> str:
        """The service this announce branch is allowed to call.

        A shape check is not enough here: ``validate_segment`` accepts any
        lowercase identifier, which left the whole ``assist_satellite`` /
        ``music_assistant`` service surface addressable from a payload. Only the
        services in :data:`ANNOUNCE_ALLOWED_SERVICES` get through.
        """
        if override is None:
            return ANNOUNCE_DEFAULT_SERVICE[domain]
        allowed = ANNOUNCE_ALLOWED_SERVICES[domain]
        if not isinstance(override, str) or override not in allowed:
            raise StewardInputError(
                f"announce: service for a {domain} target must be one of "
                f"{sorted(allowed)}")
        return override

    def _announce_url(self, value) -> str:
        """Return a URL that is safe to hand HA to fetch, or raise.

        Policy, most restrictive first — every rejection happens before any
        request leaves the appliance:

        * ``http``/``https`` only, so no ``file:``, ``data:`` or ``javascript:``
          and no local-file read on the HA host;
        * no credentials in the URL — those would be handed to whichever host
          actually answered;
        * at most :data:`MAX_URL_LEN` characters, and no whitespace or
          non-printable characters (request splitting, log injection);
        * the host must be either a **configured house host** (HA, the media
          server, or :data:`ANNOUNCE_HOSTS_ENV`) or a **globally routable**
          public one. A literal IP that is loopback, private, link-local
          (169.254.0.0/16, i.e. cloud metadata), multicast, reserved or
          unspecified is refused, as is a bare single-label or ``*.local``-style
          intranet name.

        Residual: a *public* DNS name whose A record points into the LAN still
        passes — this deliberately does not resolve, since resolving here and
        HA resolving later is a rebinding window, not a defence. Set
        ``$PUFFPAI_ANNOUNCE_URL_HOSTS`` and keep the house on named hosts if you
        want a closed allow-list.
        """
        if not isinstance(value, str):
            raise StewardInputError(
                "announce: music_assistant.play_announcement needs an audio "
                "'url' string; use an assist_satellite target to announce text")
        url = value.strip()
        if not url:
            raise StewardInputError("announce: url is empty")
        if len(url) > MAX_URL_LEN:
            raise StewardInputError(
                f"announce: url exceeds {MAX_URL_LEN} characters")
        if any(ch.isspace() or not ch.isprintable() for ch in url):
            raise StewardInputError(
                "announce: url contains whitespace or control characters")

        try:
            parts = urlsplit(url)
            host, _port = parts.hostname, parts.port  # .port validates the port
        except ValueError as exc:
            raise StewardInputError(f"announce: unusable url ({exc})") from None

        scheme = (parts.scheme or "").lower()
        if scheme not in ALLOWED_URL_SCHEMES:
            raise StewardInputError(
                f"announce: url scheme must be one of "
                f"{sorted(ALLOWED_URL_SCHEMES)}, not {scheme!r}")
        if parts.username or parts.password or "@" in parts.netloc:
            raise StewardInputError("announce: url must not carry credentials")
        if not host:
            raise StewardInputError("announce: url has no host")

        host = host.lower().rstrip(".")
        if host not in self._announce_hosts():
            self._reject_non_public_host(host)
        return url

    @staticmethod
    def _reject_non_public_host(host: str) -> None:
        """Refuse a host that is not a configured house host and not public."""
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            addr = None

        if addr is not None:
            # ``is_global`` already excludes loopback, private, link-local,
            # reserved and unspecified; IPv4 multicast is not in its private
            # set, so it is checked separately.
            if addr.is_multicast or not addr.is_global:
                raise StewardInputError(
                    f"announce: {host} is not a globally routable address; a "
                    "house URL must name the configured HA or media host")
            return

        # Strict parsing failed, but a host whose last label is numeric is an
        # address in disguise, not a name: ``127.1``, ``2130706433`` and
        # ``0x7f000001`` are all loopback to a permissive HTTP client while
        # ``ipaddress`` refuses them. A real DNS name never ends in an
        # all-numeric label, so this cannot reject a legitimate host.
        if _NUMERIC_LABEL_RE.match(host.rsplit(".", 1)[-1]):
            raise StewardInputError(
                f"announce: {host} is an abbreviated or obfuscated IP address, "
                "not a hostname")

        if host in LOCAL_HOST_NAMES or host.endswith(LOCAL_HOST_SUFFIXES):
            raise StewardInputError(
                f"announce: {host} is a local-network name; a house URL must "
                "name the configured HA or media host")
        if "." not in host:
            raise StewardInputError(
                f"announce: {host} is a single-label intranet name; use a "
                "configured house host or a fully-qualified public name")

    def _announce_hosts(self) -> set[str]:
        """House hosts an announcement URL may name. Cached; never raises.

        A config failure yields only the env-var entries — the fail-closed
        outcome, since every other host then has to prove it is public.
        """
        if self._announce_host_cache is not None:
            return self._announce_host_cache

        hosts: set[str] = set()
        for raw in os.environ.get(ANNOUNCE_HOSTS_ENV, "").split(","):
            entry = raw.strip().lower().rstrip(".")
            if entry:
                hosts.add(entry)
        try:
            from puffpai.config import get_config
            cfg = get_config()
            urls = [cfg.ha.url, cfg.media_server.jellyfin_url,
                    cfg.media_server.calibre_web_url]
            for url in urls:
                parsed = urlsplit(str(url or ""))
                if parsed.hostname:
                    hosts.add(parsed.hostname.lower().rstrip("."))
            pi_host = str(getattr(cfg.media_server, "pi_host", "") or "").strip()
            if pi_host:
                hosts.add(pi_host.lower().rstrip("."))
        except Exception as exc:
            logger.warning(
                "announce host allow-list: config unavailable (%s); only $%s "
                "entries and public hosts will be accepted", exc,
                ANNOUNCE_HOSTS_ENV)
        self._announce_host_cache = hosts
        return hosts

    # ── shared input / availability helpers ─────────────────────────
    @staticmethod
    def _clean_text(value, *, field: str, max_len: int = MAX_TEXT_LEN,
                    required: bool = True) -> str:
        """Strip control characters, collapse whitespace, cap the length."""
        if value is None:
            if required:
                raise StewardInputError(f"{field} is required")
            return ""
        if not isinstance(value, str):
            raise StewardInputError(f"{field} must be a string")
        cleaned = " ".join(
            "".join(ch if ch.isprintable() else " " for ch in value).split())
        if not cleaned:
            if required:
                raise StewardInputError(f"{field} is empty")
            return ""
        return cleaned[:max_len]

    @staticmethod
    def _temperature(value) -> float:
        """A numeric setpoint inside the sane band, or a refusal."""
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise StewardInputError("temperature must be a number")
        temp = float(value)
        if not MIN_TEMP_F <= temp <= MAX_TEMP_F:
            raise StewardInputError(
                f"temperature {temp} outside {MIN_TEMP_F}-{MAX_TEMP_F}F")
        return temp

    @staticmethod
    def _hvac_mode(value) -> str:
        if not isinstance(value, str) or value not in HVAC_MODES:
            raise StewardInputError(
                f"hvac_mode must be one of {sorted(HVAC_MODES)}")
        return value

    @staticmethod
    def _human_confirmed(envelope: Envelope, p: dict) -> bool:
        """Thin alias for :func:`human_confirmed` — one implementation only."""
        return human_confirmed(envelope, p)

    def _require_entity(self, ha, entity_id, *, domains: set[str] | None = None):
        """Validate *entity_id*, check its domain, and confirm it is usable.

        Raises before any service call: :class:`StewardInputError` for a wrong
        domain, ``HABridgeError`` for a malformed id or an id HA does not know,
        :class:`EntityUnavailable` when the device is offline. The house has
        hubs that come and go, so "unavailable" must be a clear answer rather
        than a service call that silently no-ops.
        """
        from puffpai.ha_bridge import validate_entity_id

        validate_entity_id(entity_id)
        if domains is not None and entity_id.split(".")[0] not in domains:
            raise StewardInputError(
                f"{entity_id} is not in domain(s) {sorted(domains)}")

        raw = ha.get_entity_state(entity_id) or {}
        state = str(raw.get("state", "")).lower()
        if state in UNAVAILABLE_STATES:
            raise EntityUnavailable(entity_id, state)
        return raw

    def _find_assist_satellite(self, ha) -> str | None:
        """First available ``assist_satellite.*`` entity, or None."""
        try:
            devices = ha.list_devices(domain="assist_satellite",
                                      controllable_only=False)
        except Exception as exc:
            logger.debug("assist_satellite discovery failed: %s", exc)
            return None
        for dev in devices or []:
            if str(getattr(dev, "state", "")).lower() not in UNAVAILABLE_STATES:
                return getattr(dev, "entity_id", None)
        return None

    @staticmethod
    def _device_dict(device) -> dict:
        """Best-effort serialization of a DeviceInfo without assuming shape."""
        for attr in ("to_dict", "_asdict"):
            fn = getattr(device, attr, None)
            if callable(fn):
                try:
                    return fn()
                except Exception:
                    break
        return {
            "entity_id": getattr(device, "entity_id", None),
            "name": getattr(device, "name", None),
            "state": getattr(device, "state", None),
        }

    def health(self) -> dict:
        base = super().health()
        try:
            base["ha_connected"] = bool(self._ha_bridge().is_connected())
        except Exception:
            base["ha_connected"] = False
        return base


__all__ = [
    "StewardAgent",
    "StewardInputError",
    "EntityUnavailable",
    "ConfirmationRequired",
    "SAFETY_CRITICAL_DOMAINS",
    "entity_domain",
    "human_confirmed",
    "requires_human_confirmation",
    "ALLOWED_URL_SCHEMES",
    "ANNOUNCE_ALLOWED_SERVICES",
    "ANNOUNCE_DEFAULT_SERVICE",
    "ANNOUNCE_HOSTS_ENV",
    "MAX_URL_LEN",
    "STATE_CHANGING_INTENTS",
    "CONFIRM_REQUIRED_INTENTS",
    "HUMAN_ORIGINS",
    "HVAC_MODES",
]
