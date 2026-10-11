"""Hygiene contract for ``nodered_flows/flows.json``.

The automation flows are the one part of this repo that runs OUTSIDE the
backend: Node-RED lives in its own container, and a container never consults
``security/egress_doors.py``. So nothing in ``flows.json`` can be switched off
from Settings > Egress, and two classes of mistake in that file are therefore
unfixable at runtime rather than merely untidy:

  1. **A real address from the machine it was written on.** An IPv4 literal or
     an mDNS name that survives into a release ships one household's network
     map to everybody who clones the repo, and points a fresh install at
     a box that does not exist.
  2. **An outbound host nobody chose.** A default ``https://`` URL in a flow is
     an egress the house never agreed to and cannot see. Feeds and remote
     services must come from the environment, so that every host these flows
     reach is one somebody named in ``.env``.

Both rules are about the SHIPPED file only. Whatever an operator later types
into their own ``.env``, or into the editor on their own box, is their business.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re

import pytest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FLOWS_PATH = os.path.join(REPO_ROOT, "nodered_flows", "flows.json")

#: Hosts the shipped flows may contact over https:// with no configuration.
#:
#: The default is "nothing". A door governs what the BACKEND may reach; nothing
#: governs what a container reaches, so for a file we ship the honest default is
#: no remote host at all, and every outbound target is named by the operator in
#: ``.env``. Adding an entry here is a product decision about what leaves
#: someone's house without being asked — not a test fixup.
#:
#: ``api.weather.gov`` is the one entry, and it is a KNOWN COMPROMISE rather
#: than a clean pass: the National Weather Service host is named by the declared
#: ``weather`` door (see docs/WHAT_CAN_LEAVE_YOUR_HOUSE.md), takes no account and
#: receives only coordinates the household already chose — but because this flow
#: runs in the container it will still fetch when that door is SHUT. That gap is
#: disclosed in the generated egress doc's container carve-out. Closing it means
#: either routing the flow through the backend or making the weather flows inert
#: without configured coordinates; until one of those happens, this entry is the
#: honest record of a bypass, not a blessing.
HTTPS_HOST_ALLOWLIST: frozenset[str] = frozenset({"api.weather.gov"})

#: ``.local`` names that are a documented upstream default rather than a real
#: machine on the authors' network. Home Assistant's own install docs tell
#: people to reach it at this name, so it is the correct thing for an unset
#: ``HA_URL`` to fall back to.
MDNS_ALLOWLIST = frozenset({"homeassistant.local"})

#: Addresses that exist to be examples. RFC 5737 (documentation), RFC 3330
#: broadcast, and the loopback/any hosts a container legitimately talks to.
ADDRESS_ALLOWLIST = frozenset({
    "127.0.0.1", "0.0.0.0", "255.255.255.255",
    "192.0.2.1", "198.51.100.1", "203.0.113.1",
})

#: Strings used as *documentation of a range*, not as a host to connect to.
#: ``flows.json`` classifies scanned devices by RFC1918 prefix, and the compose
#: comment about the docker0 bridge names its subnet.
CIDR_RE = re.compile(r"\d{1,3}(?:\.\d{1,3}){0,3}/\d{1,2}")

IPV4_RE = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
MDNS_RE = re.compile(r"\b(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+local\b", re.I)
HTTPS_RE = re.compile(r"https://([A-Za-z0-9._-]+)")


@pytest.fixture(scope="module")
def flows_text() -> str:
    assert os.path.exists(FLOWS_PATH), FLOWS_PATH
    with open(FLOWS_PATH, encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def flows(flows_text: str) -> list:
    """Parsed, so a syntax error fails here and not in five other ways."""
    return json.loads(flows_text)


def _strip_cidrs(text: str) -> str:
    """Blank out ``a.b.c.d/n`` spans so a documented RANGE is not read as a host."""
    return CIDR_RE.sub(" ", text)


def test_flows_json_is_a_list_of_nodes(flows: list) -> None:
    assert isinstance(flows, list) and flows
    assert all(isinstance(n, dict) and "id" in n for n in flows)


def test_no_private_ipv4_literals(flows_text: str) -> None:
    """No RFC1918 address from the machine these flows were written on.

    Every live lookup goes through ``env.get(...)``; this keeps node comments
    from leaking a network map as well.
    """
    offenders: list[str] = []
    for match in IPV4_RE.finditer(_strip_cidrs(flows_text)):
        literal = match.group(0)
        if literal in ADDRESS_ALLOWLIST:
            continue
        try:
            addr = ipaddress.IPv4Address(literal)
        except ipaddress.AddressValueError:
            continue          # a version string like 4.0.1.2, not an address
        if addr.is_private and not addr.is_loopback:
            offenders.append(literal)
    assert not offenders, (
        "nodered_flows/flows.json carries private IPv4 literals %s. Read the "
        "host from the environment (env.get(\"HA_URL\") / \"PUFFPAI_API_URL\" / "
        "\"JELLYFIN_URL\") and describe it in words in any comment."
        % sorted(set(offenders)))


def test_no_mdns_hostnames_outside_the_allowlist(flows_text: str) -> None:
    """An mDNS name is one household's machine, not a default."""
    found = {m.group(0).lower() for m in MDNS_RE.finditer(flows_text)}
    offenders = sorted(found - MDNS_ALLOWLIST)
    assert not offenders, (
        "nodered_flows/flows.json names mDNS hosts %s. Only %s is a documented "
        "upstream default; anything else is a machine on somebody's LAN and "
        "belongs in .env." % (offenders, sorted(MDNS_ALLOWLIST)))


def test_no_unconfigured_https_egress(flows_text: str) -> None:
    """A shipped ``https://`` default is an egress no door can close."""
    offenders = sorted({
        m.group(1).lower() for m in HTTPS_RE.finditer(flows_text)
        if m.group(1).lower() not in HTTPS_HOST_ALLOWLIST
    })
    assert not offenders, (
        "nodered_flows/flows.json fetches %s with no configuration. Node-RED "
        "runs in its own container and never consults the egress doors, so "
        "this cannot be switched off from Settings > Egress. Read the URL from "
        "the environment and let an empty value stop the flow."
        % offenders)


def test_the_news_flows_are_inert_until_a_feed_is_named(flows: list) -> None:
    """The reason the allowlist above can stay empty: no feed ships enabled."""
    news = [n for n in flows
            if isinstance(n.get("func"), str) and "NEWS_RSS_URL" in n["func"]]
    assert news, "expected the news flows to read NEWS_RSS_URL"
    for node in news:
        func = node["func"]
        assert "return null" in func, (
            "node %r reads NEWS_RSS_URL but does not stop when it is empty; an "
            "unset feed must produce no outbound request at all."
            % node.get("name", node["id"]))


def test_no_secret_values_are_inlined(flows: list) -> None:
    """Tokens are read from the environment, never baked into the file."""
    for node in flows:
        func = node.get("func")
        if not isinstance(func, str):
            continue
        for secret in ("HA_TOKEN", "PUFFPAI_API_KEY", "JELLYFIN_API_KEY"):
            for hit in re.finditer(re.escape(secret) + r"\s*(=|:)\s*\"([^\"]+)", func):
                pytest.fail("node %r inlines a value for %s"
                            % (node.get("name", node["id"]), secret))
