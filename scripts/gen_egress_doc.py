#!/usr/bin/env python3
"""Generate docs/WHAT_CAN_LEAVE_YOUR_HOUSE.md from the egress-door registry.

The document is not written by hand, because a hand-written list of what the
appliance reaches goes stale and lets a new endpoint ship un-enumerated. It is
rendered from ``puffpai.security.egress_doors.DOORS``, and
``tests/test_egress_docs_honesty.py`` fails if the file on disk and the
registry disagree.

Usage::

    python scripts/gen_egress_doc.py           # rewrite the doc
    python scripts/gen_egress_doc.py --check   # exit 1 if it is out of date
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC_PATH = REPO_ROOT / "docs" / "WHAT_CAN_LEAVE_YOUR_HOUSE.md"

sys.path.insert(0, str(REPO_ROOT / "src"))


def render() -> str:
    """The whole document, as text."""
    from puffpai.security.egress_doors import (
        CONFIG_SECTION, DOORS, _FLAG_HONOURED_EXTERNAL as _HONOURED_EXTERNAL)
    from puffpai.security.egress_policy import POLICIES

    lines: list[str] = []
    add = lines.append

    add("# What can leave your house")
    add("")
    add("<!-- GENERATED FILE. Do not edit by hand.")
    add("     Source: src/puffpai/security/egress_doors.py")
    add("     Regenerate: python scripts/gen_egress_doc.py -->")
    add("")
    add("Every outbound connection this appliance can make is a **door**. A "
        "door has a name,")
    add("a fixed set of hosts, a stated purpose, a plain description of what "
        "data leaves, and")
    add("a switch. **Every door ships off.** Nothing below happens until "
        "someone in this")
    add("house turns it on.")
    add("")
    add("One carve-out, stated plainly because a list of doors implies it is the "
        "whole")
    add("story: **Node-RED runs in its own container. Flows you enable there "
        "make their")
    add("own connections and are not doors** — they are not listed below, they "
        "do not")
    add("appear in `GET /system/status`, and shutting every door on this page "
        "does not")
    add("stop them. What they may reach is whatever you named in the `.env` "
        "beside")
    add("`docker-compose.nodered.yml`; no feed or remote service ships enabled "
        "there.")
    add("See `nodered_flows/README.md`.")
    add("")
    add("## Where a door's switch lives")
    add("")
    add("Four places can set a door, and they are consulted in this order. "
        "The first one")
    add("that has an opinion wins; `GET /system/status` publishes a `source` "
        "field per door")
    add("naming which one it was, so you never have to guess why a switch "
        "will not move.")
    add("")
    add("| Order | Source | Where | Who sets it |")
    add("|---|---|---|---|")
    add("| 1 | `env` | `PUFFPAI_EGRESS_*` in the service unit | an operator, "
        "and nothing the running appliance writes can override it |")
    add("| 2 | `overlay` | `$PUFFPAI_DATA_DIR/state/egress_doors.json` | "
        "**you**, from Settings > Egress |")
    add("| 3 | `config` | `egress_doors` in `config/puffpai_config.json` | "
        "how the appliance shipped or was provisioned |")
    add("| 4 | `default` | the door registry | always **shut** |")
    add("")
    add("### Why your switches are not written into the config file")
    add("")
    add("The appliance config lives in a directory the API server cannot "
        "write. That is")
    add("deliberate: the service runs under `ProtectSystem=strict`, and "
        "letting it rewrite")
    add("the whole appliance configuration so that a checkbox could be "
        "flipped would mean")
    add("that anyone who ever found a hole in the API server could rewrite "
        "every setting on")
    add("the box. Instead your switches go into a small **overlay** file that "
        "holds nothing")
    add("but door on/off values, in the data directory the service already "
        "writes.")
    add("")
    add("The overlay outranks the config file, so a door your appliance "
        "shipped switched")
    add("off is still yours to open. It does **not** outrank the environment: "
        "a door forced")
    add("shut in the unit file stays shut, and the switch tells you so "
        "instead of pretending.")
    add("")
    add("The overlay is per-door. A door it does not mention falls through to "
        "the config")
    add("file and then to the default, which is shut. If the overlay is ever "
        "unreadable,")
    add("**every door is treated as shut**. A file we cannot read is not "
        "consent.")
    add("")
    add("The config file still looks like this, and is still the right place "
        "for a build or")
    add("a provisioning script to set defaults:")
    add("")
    add("```json")
    add(f'"{CONFIG_SECTION}": {{')
    add('  "weather": true,')
    add('  "air_quality": true,')
    add('  "news_rss": true')
    add("}")
    add("```")
    add("")
    add("`GET /system/status` returns this same table live, with each door's "
        "current state,")
    add("its `source`, and when it was last used. `POST /system/egress/door` "
        "(owner-")
    add("authenticated) is what the Settings screen calls to change one.")
    add("")

    add("## Where a door may actually go")
    add("")
    add("A door names hosts. A hostname is not a destination. It is a "
        "question asked of a")
    add("name server, and the answer decides which machine the socket "
        "actually reaches. So")
    add("before this appliance connects to a door's host it looks that name "
        "up and checks")
    add("**every** address the answer contains. A door may only reach an "
        "address on the")
    add("public internet. If any answer is on your own network, such as loopback, "
        "`10.x`, `192.168.x`,")
    add("`172.16-31.x`, a link-local `169.254.x` cloud-metadata address, a "
        "carrier-NAT,")
    add("unique-local, multicast, documentation or benchmark range, the "
        "request is refused")
    add("and nothing is sent. One bad answer out of several is enough: the "
        "client picks which")
    add("address it dials, so every answer has to be safe.")
    add("")
    add("The connection is then **pinned** to an address that just passed "
        "that check. Looking a")
    add("name up, approving it, and then handing the *name* to an HTTP "
        "client that looks it up")
    add("again is a door with a gap in it: a name server that answers "
        "\"public\" the first time")
    add("and \"your router\" the second walks straight through. Pinning "
        "closes it. The encrypted")
    add("handshake still uses the hostname, so the site sees an ordinary "
        "request and nothing")
    add("about the connection is weakened.")
    add("")
    add("This matters most for a door whose host **this appliance did not "
        "choose**. Every host")
    add("below was picked by us, and their name servers are not ours to "
        "worry about. The check")
    add("exists because a door whose hostname arrives from somewhere else, "
        "a downloadable")
    add("pack or an edited config, is a door whose author would otherwise "
        "choose the address")
    add("behind it, and could point a perfectly innocent-looking feed at "
        "the inside of your")
    add("house while the card still said nothing about you leaves.")
    add("")
    add("A refused request is written to the record below with **which kind "
        "of address it")
    add("was**, such as `loopback`, `private` or `link_local`, and never the address "
        "itself. Your own")
    add("network's addressing is a fact about your house and does not belong "
        "in a log file.")
    add("")

    add("## The doors")
    add("")
    for d in DOORS:
        add(f"### `{d.id}`{', third party' if d.third_party else ''}")
        add("")
        if d.derived:
            goes_to = ("only a host some other door you opened already "
                       "contacts. This door has none of its own")
        elif d.hosts:
            goes_to = ", ".join(f"`{h}`" for h in d.hosts)
        else:
            goes_to = "chosen per request"
        add(f"- **Goes to:** {goes_to}")
        add(f"- **What it is for:** {d.purpose}")
        add(f"- **What leaves the house:** {d.data_sent}")
        if d.third_party:
            add(f"- **Whose service:** {d.third_party}")
        add(f"- **Switch:** `{CONFIG_SECTION}.{d.config_flag}` "
            f"(env `{d.env_var}`)")
        if d.derived:
            add("- **Ships:** contacts nothing until another door is open; "
                "the switch is a hard **off**, not an on")
        else:
            add(f"- **Ships:** off")
        if d.lan:
            add("- **Reaches:** this house's own network only. Its hosts must "
                "resolve to a private address; a public one is refused.")
        if d.enforcement == "registry":
            add("- **Enforced by:** the egress-door registry. With the switch "
                "off the call is refused before any socket opens, before the "
                "hostname is even looked up. With the switch on, the host must "
                "be one of those named above AND must resolve to a public "
                "address, and the connection is pinned to it.")
        elif d.enforcement == "derived":
            add("- **Enforced by:** the other doors. It can only reach a host "
                "an open door already contacts, so with every door shut it "
                "opens no socket at all.")
        elif d.enforcement == "external":
            if d.id in _HONOURED_EXTERNAL:
                add("- **Enforced by:** the code that opens the socket, which "
                    "asks this switch first and refuses before any socket "
                    "opens, then applies its own further checks on top. The "
                    "refusal happens there rather than in the registry's "
                    "request wrapper because that code reaches the network "
                    "through a library or a separate process (the dashboard "
                    "server) the wrapper cannot wrap. **Turning "
                    "this switch off does stop it.** See the note below.")
            else:
                add("- **Enforced by:** its own gate, which is at least as "
                    "strict, and which does NOT read this switch. Turning "
                    "this switch off changes what this page reports and "
                    "nothing else. See the note below.")
        else:
            add("- **Enforced by:** *nothing yet. Enumerated, not gated.* "
                "The switch above does NOT shut this path today. It exists "
                "and this document lists it rather than hiding it. Closing "
                "the gap is tracked work.")
        if d.notes:
            add(f"- **Note:** {d.notes}")
        add("")

    # ── skills the household installed ────────────────────────────────
    #
    # Rendered from ``egress_doors.skill_doors()``, which is the same table the
    # running appliance enforces. In a fresh process -- which is what generating
    # this file is -- no skill store has been rescanned, so the list is empty and
    # the section is the RULE rather than an inventory. On a box with skills
    # installed, regenerating after a rescan lists each one by name.
    from puffpai.security.egress_doors import skill_doors as _skill_doors

    add("## Skills you install")
    add("")
    add("A skill is a small Python package somebody in this house approved (see "
        "`docs/EXTENDING.md`).")
    add("It runs in a sandbox with **no network of its own**, so it cannot open "
        "a socket at all;")
    add("it can only ask this appliance to fetch a URL for it, and that ask "
        "goes through a door")
    add("of its own named `skill:<id>:0`.")
    add("")
    add("The rules for that door, none of which a skill can change about "
        "itself:")
    add("")
    add("- **Its hosts are the ones in the skill's `manifest.json` under "
        "`permissions.network_hosts`**, shown to you on the consent card before "
        "you approve anything. A host the manifest did not name is refused even "
        "when the door is open.")
    add("- **It ships shut, like every other door**, and the switch is keyed to "
        "a digest of that exact host set. Re-propose the same skill with one "
        "more host and it is a different key — so a skill cannot widen its own "
        "reach by editing a file after you said yes.")
    add("- **`https` only, port 443 only, `GET` only**, with a cap on the URL, "
        "the query string, the number of requests per run and the size of the "
        "answer.")
    add("- **A host that resolves onto your own network is refused.** Skill "
        "doors are public-internet doors: the address check above applies to "
        "them unchanged, so a skill cannot reach your router, your NAS or "
        "anything else indoors, whatever name it declares. There is no LAN "
        "skill door in this build.")
    add("- **Switch the skill off, or remove it, and the door and your `true` "
        "for it both go** in the same breath.")
    add("- **A single refusal switches the skill off.** A skill reaching for a "
        "host you did not approve is not a skill to retry.")
    add("")
    live_skill_doors = _skill_doors()
    if not live_skill_doors:
        add("No skill is installed on the box this page was generated from, so "
            "there are no skill")
        add("doors below. `GET /system/status` is the live list.")
        add("")
    else:
        for d in live_skill_doors:
            add(f"### `{d.id}`")
            add("")
            add(f"- **Goes to:** {', '.join(f'`{h}`' for h in d.hosts)}")
            add(f"- **What it is for:** {d.purpose}")
            add(f"- **What leaves the house:** {d.data_sent}")
            add(f"- **Switch:** `{CONFIG_SECTION}.{d.config_flag}` "
                f"(env `{d.env_var}`)")
            add("- **Ships:** shut")
            add("")

    add("## Settings that are not doors")
    add("")
    add("A door says **where** this appliance may go. These say **who on the "
        "box may walk")
    add("through an open one**. They open nothing by themselves: with every "
        "web door shut,")
    add("turning one of these on changes nothing at all.")
    add("")
    for p in POLICIES:
        add(f"### `{p.id}`")
        add("")
        add(f"- **What it decides:** {p.purpose}")
        add(f"- **What leaves the house:** {p.data_effect}")
        add(f"- **Switch:** `{p.config_section}.{p.config_key}` "
            f"(env `{p.env_var}`)")
        # Deliberately NOT the doors' "**Ships:** off" wording: a test counts
        # that string to prove every DOOR ships shut, and a policy is not a
        # door. Same fact, different label, no false count.
        add(f"- **Default:** {'on' if p.default else 'off'}")
        if p.notes:
            add(f"- **Note:** {p.notes}")
        add("")
    add("These resolve through the same four tiers a door does. Environment, "
        "then the owner")
    add("overlay, then the config file, then the code default. The "
        "overlay for them is")
    add("`$PUFFPAI_DATA_DIR/state/egress_policy.json`, a separate file from "
        "the doors' own so")
    add("that changing one can never touch the other. "
        "`POST /system/egress/policy` (owner-")
    add("authenticated, body `{\"id\": …, \"value\": true|false}`) is what "
        "the Settings screen calls.")
    add("")

    add("## The record")
    add("")
    add("Every door decision, allowed **and refused**, is appended to "
        "`security_logs/egress.jsonl`")
    add("on the appliance, with the door, the host, the URL path, which part "
        "of the code asked,")
    add("and how many bytes were sent. Refusals also get one line in the "
        "system journal at")
    add("WARNING, so `journalctl -u puffpai-api | grep 'egress REFUSED'` "
        "answers \"did this thing")
    add("ever try to phone home\" without reading any file.")
    add("")
    add("The ledger never records a query string, a request body, a header or "
        "a token. Only")
    add("hosts, paths and byte counts. A request refused because its host "
        "pointed inside your")
    add("house is recorded as `resolved_private` with the KIND of address it "
        "was, never the")
    add("address. It rotates at 4 MB and keeps 3 older generations, so the "
        "whole trail is")
    add("capped at 16 MB and cannot fill the disk.")
    add("")

    # ── Zone 3's upstream resolver ────────────────────────────────────
    #
    # This section is PROSE, not a rendered DOORS entry, and that is
    # deliberate. A door in `egress_doors.py` is a switch the API server
    # enforces on its own outbound calls. This connection is not one of
    # those: it is made by the AdGuard Home container, on behalf of whatever
    # device asked, and no PuffPAI code is on that path. Adding it to DOORS
    # would put a switch in the Settings screen that does not control
    # anything, which is a worse lie than the omission it fixes.
    #
    # It is still the one new outbound path Zone 3 creates, so it is written
    # down here where a reader looking for "what leaves my house" will find
    # it, next to everything else that does.
    add("## The house's own resolver, Zone 3")
    add("")
    add("This one has no switch in the list above.")
    add("It is written down anyway, because it is a real connection out of "
        "your house.")
    add("")
    add("When **Zone 3 is switched on**, meaning the sinkhole running AND the router "
        "pointed at this")
    add("appliance, every name any device in the house looks up is answered "
        "here. Names the")
    add("appliance does not already know are asked of **Quad9**, over "
        "encrypted DNS:")
    add("")
    add("- **Where it goes:** `https://dns.quad9.net/dns-query` (DNS-over-"
        "HTTPS), falling back to")
    add("  `tls://dns.quad9.net` (DNS-over-TLS) if that is blocked. Quad9's "
        "plain addresses")
    add("  `9.9.9.9` and `149.112.112.112` are used only to find those two "
        "names in the first place.")
    add("- **What leaves the house:** the name being looked up, and nothing "
        "else. Not who asked.")
    add("  The appliance does not forward client subnet information, and it "
        "never sends a reverse")
    add("  lookup of an address on your own network upstream, so the shape "
        "of your home does not")
    add("  travel with the question. The connection is encrypted, so the "
        "network between your")
    add("  house and Quad9 cannot read the names either, which is a change "
        "in your favour: your")
    add("  internet provider's resolver could, and did.")
    add("- **Switch:** `zone3.enabled` in `config/puffpai_config.json`, and "
        "the router field that")
    add("  points the house here. **Both ship off.** With either one off, "
        "this path does not exist")
    add("  and your devices resolve names exactly the way they did before.")
    add("- **Who else is on this path:** nobody. No filter subscriptions are "
        "installed and no list")
    add("  is fetched on a schedule, so the resolver talks to Quad9 and to "
        "nothing else.")
    add("")
    add("Every question asked and answer given is written to the appliance's "
        "own query log, which")
    add("stays on the appliance for 90 days and is readable at the AdGuard "
        "console. That log is")
    add("the point: it is how you find out what your television has been "
        "saying about you.")
    add("")

    # ── Node-RED flows ────────────────────────────────────────────────
    #
    # Prose, because Node-RED makes these requests itself: the registry's
    # request wrapper is not on that path. What makes the switches real there
    # is that each flow asks the backend before it goes out, and the backend
    # refuses to start such a flow while a switch is off.
    add("## Node-RED flows, only if you import them")
    add("")
    add("The example flows in `nodered_flows/flows.json` are not loaded on a "
        "fresh install. If")
    add("you import them into Node-RED, **Node-RED itself** makes the calls "
        "below, so they follow")
    add("the `weather` and `news_rss` switches above in two places:")
    add("")
    add("- **The flow asks first.** Before each call to the internet, the "
        "flow asks the backend")
    add("  (`GET /system/egress/door/<id>`, with `PUFFPAI_API_KEY`) whether "
        "the switch is on, and")
    add("  skips the call unless the answer is a clear yes. A switch that is "
        "off, a missing key, a")
    add("  backend that does not answer or an answer it cannot read all mean "
        "no. The flow logs")
    add("  why, and a request made to the flow is answered with that reason. "
        "This covers the")
    add("  `weather_alerts` schedule too, which runs on its own every two "
        "hours.")
    add("- **The backend will not start them.** The assistant's automation "
        "tools (a daily")
    add("  briefing, for example), the scheduled tasks and "
        "`POST /automation/trigger` refuse to")
    add("  start one of these flows while any switch it needs is off, and say "
        "which switch.")
    add("")
    add("The calls:")
    add("")
    add("- **`api.weather.gov`** (the US National Weather Service), under "
        "`weather`, from the")
    add("  `daily_weather`, `daily_briefing` and `weather_alerts` flows. What "
        "leaves the house: the")
    add("  latitude and longitude the flow is given (or `WEATHER_DEFAULT_LAT` "
        "/ `WEATHER_DEFAULT_LON`),")
    add("  and a User-Agent naming PuffPAI. A forecast address outside "
        "`api.weather.gov` is not")
    add("  followed.")
    add("- **The news feed in `NEWS_RSS_URL`**, under `news_rss`, from the "
        "`daily_news` and")
    add("  `daily_briefing` flows. Unless you set another, that is "
        "`rss.nytimes.com` (the New York")
    add("  Times home page feed), which is not one of the hosts `news_rss` "
        "lists above. What leaves")
    add("  the house: only the request itself, so the publisher learns this "
        "house's public IP")
    add("  address.")
    add("")
    add("**A copy of the flows without this check does not ask.** The "
        "backend still")
    add("refuses to start it, but its `weather_alerts` schedule calls "
        "`api.weather.gov` every two")
    add("hours whatever the switch says. Re-import `nodered_flows/flows.json`, "
        "or disable or delete")
    add("the old flows in the Node-RED editor. The switches cannot stop a "
        "flow that does not ask,")
    add("so anyone who can change the flows in that editor "
        "(`127.0.0.1:1880`) can also take the")
    add("question out.")
    add("")

    add("## What is deliberately not a door")
    add("")
    add("These reach machines on your own network or on the appliance itself, "
        "so nothing")
    add("leaves the house and there is nothing to switch:")
    add("")
    add("- the local model runtime (`127.0.0.1:11434`)")
    add("- the loopback SearXNG instance (`127.0.0.1:8888`)")
    add("- Home Assistant, Calibre, Zigbee2MQTT and Node-RED on your LAN")
    add("- `workspace_exec` and `document_convert`, which run with no network "
        "namespace at all")
    add("- the grocery search link on a shopping list, when a household "
        "configures one: a URL you click, never "
        "something the appliance fetches")
    add("")
    add("Jellyfin is reached on your LAN too, but it is its own program and "
        "can reach the internet")
    add("itself. If a household adds an internet IPTV tuner to Jellyfin's "
        "Live TV (an M3U playlist")
    add("of internet channels), Jellyfin fetches those channels from the "
        "internet whenever someone")
    add("watches one, and its metadata and plugin downloads go out too when "
        "they are turned on in")
    add("Jellyfin. The `tv_free_channels` switch does not control "
        "Jellyfin-carried channels: it")
    add("covers only the kiosk player's free channels and the cast relay. To "
        "stop those fetches,")
    add("change the settings in Jellyfin itself.")
    add("")

    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="exit 1 if the doc is out of date")
    args = parser.parse_args()

    rendered = render()
    current = DOC_PATH.read_text(encoding="utf-8") if DOC_PATH.exists() else ""

    if args.check:
        if current != rendered:
            print(f"{DOC_PATH} is out of date; run: "
                  f"python scripts/gen_egress_doc.py", file=sys.stderr)
            return 1
        print(f"{DOC_PATH} is in sync with the registry.")
        return 0

    DOC_PATH.parent.mkdir(parents=True, exist_ok=True)
    DOC_PATH.write_text(rendered, encoding="utf-8", newline="\n")
    print(f"wrote {DOC_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
