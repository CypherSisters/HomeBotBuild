"""Same-host gate for API-key-exempt endpoints.

Background
----------
``api_server._PUBLIC_ENDPOINTS`` is a hard exemption from the API key: an
endpoint listed there is answered for *anyone who can open a TCP connection to
port 8000*, and the API binds ``0.0.0.0``. Without this gate a plain LAN host
with no key, no cookie and no pairing could list, search, read and delete the
household document corpus, download the retained original of an uploaded
document (a family photo, a .docx), and drive the household LLM through
``/v1/chat/completions``.

The exemptions were not gratuitous — a browser ``<iframe src>``, an HA
conversation agent and the on-box voice service genuinely cannot attach an
``Authorization`` header. But "cannot carry the key" is not the same claim as
"may be answered for the whole subnet". Every one of those callers is on this
host, or reaches the API through the dashboard's server-side proxy, which is
also on this host.

The gate
--------
An endpoint on :data:`LOCAL_ONLY_ENDPOINTS` is answered when EITHER

* the request carries the API key (checked by the caller before we are asked), OR
* the peer is loopback — the Next.js proxy at 127.0.0.1 (which is itself behind
  ``requirePairing`` and injects the key anyway), the kiosk browser, the voice
  service, librespot — OR
* the peer is on an explicit operator allowlist that is EMPTY by default.

Everything else gets the same 401 an unlisted endpoint gets.

Why ``remote_addr`` is trustworthy here
---------------------------------------
``api_server`` only wraps the app in ``ProxyFix`` when
``ApiServerConfig.trusted_proxy_hops`` is > 0, which is off by default and has
no env override. At 0 hops ``request.remote_addr`` is the socket peer and
``X-Forwarded-For`` is never parsed, so the loopback arm cannot be forged. When
an operator DOES enable proxy trust, ``remote_addr`` becomes attacker-supplied,
so :func:`local_caller_allowed` refuses the peer arm outright rather than
trusting a spoofable value — the API key is then the only way in.
"""

from __future__ import annotations

import ipaddress
import os


#: Peers treated as "this host". Kept as literals rather than a subnet test so
#: that 127.0.0.0/8 addresses other than 127.0.0.1 do not silently widen it.
LOOPBACK_PEERS = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})

#: Endpoints that are exempt from the API key ONLY for a same-host caller.
#: Removing an entry here makes that endpoint key-only; adding one widens
#: exposure and needs the same justification table the report carries.
LOCAL_ONLY_ENDPOINTS = frozenset({
    # Document corpus. Reached from the browser as /api/pai/ingest/* — the
    # dashboard's server-side proxy, which is pairing-gated and injects the
    # key, and whose upstream is 127.0.0.1:8000.
    "ingest_upload", "ingest_list", "ingest_search", "ingest_delete",
    "ingest_reindex", "ingest_stats",
    # Retained originals of uploaded household documents, streamed as an
    # <iframe src>. Reached via the dashboard's /api/knowledge/source route
    # (server-side fetch => loopback peer) or from the kiosk browser.
    "knowledge_serve_source",
    # ZIM assets (img/css/video) inside a rendered article; same two callers.
    "knowledge_serve_asset",
    # OpenAI/Ollama-compatible surfaces. The on-box voice service posts to
    # /api/chat from 127.0.0.1; the dashboard chat goes through /api/pai.
    # A Home Assistant conversation agent on another host must either send the
    # API key (its OpenAI-compatible integration has a key field) or be named
    # in PUFFPAI_TRUSTED_LOCAL_PEERS.
    "openai_chat_completions", "ollama_chat",
    "openai_list_models", "ollama_list_models",
    # Capability inventory read by the voice service and the kiosk.
    "voice_capabilities",
})

#: Env var naming extra peers (comma-separated IPs) that may reach
#: :data:`LOCAL_ONLY_ENDPOINTS` without the key. Empty by default: an appliance
#: that has not been told otherwise trusts only itself.
TRUSTED_PEERS_ENV = "PUFFPAI_TRUSTED_LOCAL_PEERS"


def _normalise(addr: str) -> str:
    """Canonical text form of *addr*, or "" when it is not an IP.

    Collapses IPv4-mapped IPv6 ("::ffff:10.0.0.5" -> "10.0.0.5") so an operator
    entry and the observed peer compare equal regardless of which form the
    socket reported.
    """
    try:
        ip = ipaddress.ip_address(addr.strip())
    except ValueError:
        return ""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return str(ip)


def trusted_peers() -> frozenset[str]:
    """Operator-configured extra peers, normalised. Empty unless set."""
    raw = os.environ.get(TRUSTED_PEERS_ENV, "")
    return frozenset(
        n for n in (_normalise(part) for part in raw.split(",")) if n
    )


def is_loopback_peer(remote_addr: str | None) -> bool:
    """True iff *remote_addr* is this host."""
    return (remote_addr or "") in LOOPBACK_PEERS


def local_caller_allowed(endpoint: str | None,
                         remote_addr: str | None,
                         *,
                         proxy_fix_active: bool = False) -> bool:
    """True iff *endpoint* may skip the API key for this *remote_addr*.

    ``proxy_fix_active`` is the caller's ``_apply_proxy_fix`` result. When a
    reverse proxy is trusted, ``remote_addr`` is derived from a client-supplied
    header and this function refuses every peer: fail closed rather than accept
    a forgeable loopback claim.
    """
    if endpoint not in LOCAL_ONLY_ENDPOINTS:
        return False
    if proxy_fix_active:
        return False
    if is_loopback_peer(remote_addr):
        return True
    peer = _normalise(remote_addr or "")
    return bool(peer) and peer in trusted_peers()


#: Every endpoint left in ``api_server._PUBLIC_ENDPOINTS`` must appear here with
#: the reason it is answered for an unauthenticated, non-local caller. The
#: paired test asserts the two sets match exactly, so a future exemption cannot
#: be added without writing down why.
PUBLIC_ENDPOINT_JUSTIFICATIONS = {
    "health": (
        "Liveness probe for Node-RED and the systemd/kiosk start chain. Body is "
        "the constant {'status':'ok','service':'PuffPAI API'} — no household "
        "data, no version, no configuration."
    ),
    "static": (
        "Flask's built-in static endpoint. The package ships no static folder, "
        "so it serves nothing; kept public because removing a Flask builtin "
        "from the exemption list would 401 the framework's own 404 path."
    ),
    "spotify_callback": (
        "Spotify redirects the OWNER'S BROWSER here after consent, and that "
        "browser may legitimately be a LAN laptop rather than the kiosk, so "
        "neither the key nor a loopback rule can cover it. Compensating "
        "control: the handler validates a single-use CSRF state before it will "
        "exchange anything (a forged state returns 302 .../settings?spotify="
        "error). Leaks nothing on its own; config/exchange/disconnect stay "
        "key-gated."
    ),
    "mediahub_spotify_event_route": (
        "librespot's event hook posts player state with no way to carry the "
        "key. The handler itself refuses any non-loopback peer, which is the "
        "same control this module applies — kept in the handler so the sink "
        "and its test share one definition."
    ),
    "vault_view_get": (
        "The kiosk loads the ephemeral decrypted-document view as an iframe "
        "src, which cannot carry the key. Handler enforces loopback-only PLUS "
        "an unguessable single-use token, no-store, and one active slot — "
        "strictly tighter than the API key."
    ),
    "vault_view_delete": (
        "Destroys an in-memory ephemeral view only; same loopback gate as "
        "vault_view_get. No data can be read through it."
    ),
    "vault_reveal": (
        "One-time HIGH/CRITICAL vault reveal opened by a loopback browser. "
        "Handler enforces loopback-only plus a short-TTL single-use token."
    ),
    "castproxy.master": (
        "The TV (an Apple TV via AirPlay or a Roku via Play on Roku, both "
        "started through Home Assistant) fetches the cast playlist itself and "
        "cannot carry the key or a pairing cookie. Gate: a token_urlsafe(32) "
        "cast id bound to one Live TV channel and the TV's IP, revoked on Stop, "
        "after 90 s unfetched, 2 min idle or 4 h. Served playlists are rewritten "
        "to opaque paths and refused if they still match api_key|token|8096."
    ),
    "castproxy.resource": (
        "Same gate as castproxy.master, for the variant playlists and video "
        "segments it names. Keys are per-cast and only map to this channel's "
        "/videos/<id>/ paths on the box's own Jellyfin."
    ),
}
