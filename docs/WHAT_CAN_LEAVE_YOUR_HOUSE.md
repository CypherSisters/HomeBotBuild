# What can leave your house

<!-- GENERATED FILE. Do not edit by hand.
     Source: src/puffpai/security/egress_doors.py
     Regenerate: python scripts/gen_egress_doc.py -->

Every outbound connection this appliance can make is a **door**. A door has a name,
a fixed set of hosts, a stated purpose, a plain description of what data leaves, and
a switch. **Every door ships off.** Nothing below happens until someone in this
house turns it on.

One carve-out, stated plainly because a list of doors implies it is the whole
story: **Node-RED runs in its own container. Flows you enable there make their
own connections and are not doors** — they are not listed below, they do not
appear in `GET /system/status`, and shutting every door on this page does not
stop them. What they may reach is whatever you named in the `.env` beside
`docker-compose.nodered.yml`; no feed or remote service ships enabled there.
See `nodered_flows/README.md`.

## Where a door's switch lives

Four places can set a door, and they are consulted in this order. The first one
that has an opinion wins; `GET /system/status` publishes a `source` field per door
naming which one it was, so you never have to guess why a switch will not move.

| Order | Source | Where | Who sets it |
|---|---|---|---|
| 1 | `env` | `PUFFPAI_EGRESS_*` in the service unit | an operator, and nothing the running appliance writes can override it |
| 2 | `overlay` | `$PUFFPAI_DATA_DIR/state/egress_doors.json` | **you**, from Settings > Egress |
| 3 | `config` | `egress_doors` in `config/puffpai_config.json` | how the appliance shipped or was provisioned |
| 4 | `default` | the door registry | always **shut** |

### Why your switches are not written into the config file

The appliance config lives in a directory the API server cannot write. That is
deliberate: the service runs under `ProtectSystem=strict`, and letting it rewrite
the whole appliance configuration so that a checkbox could be flipped would mean
that anyone who ever found a hole in the API server could rewrite every setting on
the box. Instead your switches go into a small **overlay** file that holds nothing
but door on/off values, in the data directory the service already writes.

The overlay outranks the config file, so a door your appliance shipped switched
off is still yours to open. It does **not** outrank the environment: a door forced
shut in the unit file stays shut, and the switch tells you so instead of pretending.

The overlay is per-door. A door it does not mention falls through to the config
file and then to the default, which is shut. If the overlay is ever unreadable,
**every door is treated as shut**. A file we cannot read is not consent.

The config file still looks like this, and is still the right place for a build or
a provisioning script to set defaults:

```json
"egress_doors": {
  "weather": true,
  "air_quality": true,
  "news_rss": true
}
```

`GET /system/status` returns this same table live, with each door's current state,
its `source`, and when it was last used. `POST /system/egress/door` (owner-
authenticated) is what the Settings screen calls to change one.

## Where a door may actually go

A door names hosts. A hostname is not a destination. It is a question asked of a
name server, and the answer decides which machine the socket actually reaches. So
before this appliance connects to a door's host it looks that name up and checks
**every** address the answer contains. A door may only reach an address on the
public internet. If any answer is on your own network, such as loopback, `10.x`, `192.168.x`,
`172.16-31.x`, a link-local `169.254.x` cloud-metadata address, a carrier-NAT,
unique-local, multicast, documentation or benchmark range, the request is refused
and nothing is sent. One bad answer out of several is enough: the client picks which
address it dials, so every answer has to be safe.

The connection is then **pinned** to an address that just passed that check. Looking a
name up, approving it, and then handing the *name* to an HTTP client that looks it up
again is a door with a gap in it: a name server that answers "public" the first time
and "your router" the second walks straight through. Pinning closes it. The encrypted
handshake still uses the hostname, so the site sees an ordinary request and nothing
about the connection is weakened.

This matters most for a door whose host **this appliance did not choose**. Every host
below was picked by us, and their name servers are not ours to worry about. The check
exists because a door whose hostname arrives from somewhere else, a downloadable
pack or an edited config, is a door whose author would otherwise choose the address
behind it, and could point a perfectly innocent-looking feed at the inside of your
house while the card still said nothing about you leaves.

A refused request is written to the record below with **which kind of address it
was**, such as `loopback`, `private` or `link_local`, and never the address itself. Your own
network's addressing is a fact about your house and does not belong in a log file.

## The doors

### `weather`

- **Goes to:** `api.open-meteo.com`, `api.weather.gov`
- **What it is for:** Daily forecast and active National Weather Service alerts.
- **What leaves the house:** Approximate latitude and longitude of the house (or the place name you set), and nothing else.
- **Switch:** `egress_doors.weather` (env `PUFFPAI_EGRESS_WEATHER`)
- **Ships:** off
- **Enforced by:** the egress-door registry. With the switch off the call is refused before any socket opens, before the hostname is even looked up. With the switch on, the host must be one of those named above AND must resolve to a public address, and the connection is pinned to it.
- **Note:** Also asked by the Node-RED flows daily_weather, daily_briefing and weather_alerts, if you import them: each one asks the backend (GET /system/egress/door/weather) before every call to api.weather.gov and skips the call unless the answer is a clear yes. The backend refuses to start those flows while this switch is off.

### `air_quality`

- **Goes to:** `air-quality-api.open-meteo.com`
- **What it is for:** US AQI, pollutants, pollen and the asthma/mold breakdown on the weather card.
- **What leaves the house:** Approximate latitude and longitude of the house.
- **Switch:** `egress_doors.air_quality` (env `PUFFPAI_EGRESS_AIR_QUALITY`)
- **Ships:** off
- **Enforced by:** the egress-door registry. With the switch off the call is refused before any socket opens, before the hostname is even looked up. With the switch on, the host must be one of those named above AND must resolve to a public address, and the connection is pinned to it.

### `news_rss`

- **Goes to:** `feeds.npr.org`, `feeds.bbci.co.uk`, `news.ycombinator.com`, `feeds.arstechnica.com`, `www.theguardian.com`, `www.theverge.com`
- **What it is for:** Headlines for the news widget, from the six feeds in FEED_DIRECTORY.
- **What leaves the house:** Nothing but the request itself — no query, no household data. The publisher learns this house's IP address and that it asked for their feed.
- **Switch:** `egress_doors.news_rss` (env `PUFFPAI_EGRESS_NEWS_RSS`)
- **Ships:** off
- **Enforced by:** the egress-door registry. With the switch off the call is refused before any socket opens, before the hostname is even looked up. With the switch on, the host must be one of those named above AND must resolve to a public address, and the connection is pinned to it.
- **Note:** Also asked by the Node-RED flows daily_news and daily_briefing, if you import them: each one asks the backend (GET /system/egress/door/news_rss) before fetching the feed in its NEWS_RSS_URL setting and skips the fetch unless the answer is a clear yes. That feed is not one of the hosts above; unless you set another, it is rss.nytimes.com. The backend refuses to start those flows while this switch is off.

### `geocode`

- **Goes to:** `nominatim.openstreetmap.org`
- **What it is for:** Turn a place name into coordinates for the weather-alert tool.
- **What leaves the house:** The place name being looked up — which for this appliance is usually where the household lives.
- **Switch:** `egress_doors.geocode` (env `PUFFPAI_EGRESS_GEOCODE`)
- **Ships:** off
- **Enforced by:** the egress-door registry. With the switch off the call is refused before any socket opens, before the hostname is even looked up. With the switch on, the host must be one of those named above AND must resolve to a public address, and the connection is pinned to it.
- **Note:** Kept separate from the weather door on purpose: weather sends coordinates you already chose, geocoding sends the place name itself to a third party. Different disclosure, different switch.

### `geoip`

- **Goes to:** `ipapi.co`
- **What it is for:** Guess the household's location from its public IP address when the user asks for 'here' and no location is stored.
- **What leaves the house:** Your public IP address, by the act of connecting. ipapi.co learns roughly where this house is.
- **Switch:** `egress_doors.geoip` (env `PUFFPAI_EGRESS_GEOIP`)
- **Ships:** off
- **Enforced by:** the egress-door registry. With the switch off the call is refused before any socket opens, before the hostname is even looked up. With the switch on, the host must be one of those named above AND must resolve to a public address, and the connection is pinned to it.
- **Note:** Stays OFF by default, including after a config migration. The stored location covers the family's actual use; this door exists only for the 'detect it for me' path and discloses the house's IP to a company nobody in the household chose.

### `radio_browser`

- **Goes to:** `all.api.radio-browser.info`, `de1.api.radio-browser.info`
- **What it is for:** Look up internet radio stations for the World Radio player.
- **What leaves the house:** The station or genre being searched for and, when the dashboard player starts a station, that station's id (radio-browser counts plays to rank its list).
- **Switch:** `egress_doors.radio_browser` (env `PUFFPAI_EGRESS_RADIO_BROWSER`)
- **Ships:** off
- **Enforced by:** the egress-door registry. With the switch off the call is refused before any socket opens, before the hostname is even looked up. With the switch on, the host must be one of those named above AND must resolve to a public address, and the connection is pinned to it.
- **Note:** radio-browser ONLY. TuneIn has its own switch ('tunein' below), because one 'yes' to a volunteer database is not a 'yes' to a commercial one. Opening this door sends nothing to TuneIn. The dashboard's own radio routes (/api/radio/search and /api/radio/click) ask this switch through the backend before any request and contact only the hosts listed here. They do not pin the looked-up address the way this registry's wrapper does; the hosts are fixed and chosen by this appliance.

### `tunein`, third party

- **Goes to:** `opml.radiotime.com`
- **What it is for:** Find local AM/FM call letters and the stations radio-browser does not carry, for the World Radio player.
- **What leaves the house:** The station name or genre you asked for, sent to TuneIn at opml.radiotime.com — a commercial company, not a volunteer database, and not one anybody in this house picked. Like any request, it also shows TuneIn this house's public IP address. Nothing else about the household goes with it. Radio works with this shut: radio-browser still does the searching, and stations you already saved still play.
- **Whose service:** TuneIn (Radio Time), a commercial radio-directory company
- **Switch:** `egress_doors.tunein` (env `PUFFPAI_EGRESS_TUNEIN`)
- **Ships:** off
- **Enforced by:** the egress-door registry. With the switch off the call is refused before any socket opens, before the hostname is even looked up. With the switch on, the host must be one of those named above AND must resolve to a public address, and the connection is pinned to it.
- **Note:** Split out of the radio_browser door on purpose. Same capability — find a station to tune — but not the same disclosure, so it does not get to share the same switch; see the geocode door for the precedent ('different disclosure, different switch'). Consulted only when a NAME search comes back with little or nothing from radio-browser. With it shut, the reply says TuneIn was NOT looked at rather than implying it was searched and found nothing. The dashboard's TuneIn routes (/api/tunein/search and /api/tunein/resolve) ask this switch through the backend before any request and contact only the host listed here. A station whose stream TuneIn names as a playlist on the broadcaster's own server is skipped rather than fetched, because that server is not a host this switch names. Those routes do not pin the looked-up address the way this registry's wrapper does.

### `web_search`

- **Goes to:** `duckduckgo.com`, `html.duckduckgo.com`, `lite.duckduckgo.com`, `links.duckduckgo.com`
- **What it is for:** Ad-hoc web search from a chat turn, via the ddgs library.
- **What leaves the house:** The search query, verbatim — to DuckDuckGo on the chat tool's path, and to whichever engines your own SearXNG relays to on the Toolsmith's. Whatever the person typed that led to the search leaves the house either way.
- **Switch:** `egress_doors.web_search` (env `PUFFPAI_EGRESS_WEB_SEARCH`)
- **Ships:** off
- **Enforced by:** the code that opens the socket, which asks this switch first and refuses before any socket opens, then applies its own further checks on top. The refusal happens there rather than in the registry's request wrapper because that code reaches the network through a library or a separate process (the dashboard server) the wrapper cannot wrap. **Turning this switch off does stop it.** See the note below.
- **Note:** Enforced at TWO call sites, both of which ask egress_allowed('web_search') before a socket opens: assistant_tools.web_search (ddgs opens its own sockets, so egress_request cannot wrap them; the gate runs before ddgs is imported) and agents/toolsmith._web_search. The Toolsmith's first hop is the owner's own SearXNG on loopback, which is not itself egress — but that container then queries upstream engines on the household's behalf, so the query leaves the house all the same and belongs behind the same switch. Gating it here means one switch turns off ALL web search, which is what an owner means when they turn web search off. Honours the pre-existing top-level web_search_consent key when the new flag is absent, so an owner who already opted in is not silently opted back out.

### `web_fetch`

- **Goes to:** chosen per request
- **What it is for:** The Toolsmith reading a public page the owner or a planned run named.
- **What leaves the house:** The URL requested. The Toolsmith is the one agent that reads attacker-controlled text, and it holds no household credential.
- **Switch:** `egress_doors.web_fetch` (env `PUFFPAI_EGRESS_WEB_FETCH`)
- **Ships:** off
- **Enforced by:** the code that opens the socket, which asks this switch first and refuses before any socket opens, then applies its own further checks on top. The refusal happens there rather than in the registry's request wrapper because that code reaches the network through a library or a separate process (the dashboard server) the wrapper cannot wrap. **Turning this switch off does stop it.** See the note below.
- **Note:** Enforced at the call site in agents/toolsmith.py, which asks egress_allowed('web_fetch') at the top of _web_fetch and again at the top of _fetch_url (the one hop loop both fetch paths, web_fetch and search_and_read, reach the wire through), BEFORE any URL shape check, DNS resolution or socket. Not rerouted through egress_request because this door has no fixed host list to validate against and the Toolsmith's own gates are stricter than the wrapper's: a per-request Budget(allow_egress) that defaults False, plus per-hop SSRF validation that refuses loopback, link-local, private and IPv6-mapped addresses and re-validates every redirect before a socket opens. Both are required: this switch is the household's standing consent, the budget is the individual request's, and neither substitutes for the other.

### `page_reader`

- **Goes to:** chosen per request
- **What it is for:** Show a web page someone opens from the dashboard as plain text in the in-app reader, instead of leaving the screen.
- **What leaves the house:** The address of the page, sent to the site that hosts it. That site also learns this house's public IP address. No cookies, no referring page and no browser name that identifies the house go with it.
- **Switch:** `egress_doors.page_reader` (env `PUFFPAI_EGRESS_PAGE_READER`)
- **Ships:** off
- **Enforced by:** the code that opens the socket, which asks this switch first and refuses before any socket opens, then applies its own further checks on top. The refusal happens there rather than in the registry's request wrapper because that code reaches the network through a library or a separate process (the dashboard server) the wrapper cannot wrap. **Turning this switch off does stop it.** See the note below.
- **Note:** Enforced at the call site in the dashboard server: /api/reader asks the backend whether this switch is open (GET /system/egress/door/page_reader) before it looks up or connects to anything, and refuses when the answer is no, missing or unreadable. With the switch on, the fetch still refuses every loopback, private and link-local address, re-checks each redirect, connects to the address it checked, and hands back plain text only. Kept apart from web_fetch on purpose: that switch is the Toolsmith's standing consent to read pages for a request or a planned run, this one is a person at the screen opening a link. Different reader, different switch.

### `tv_free_channels`, third party

- **Goes to:** `boot.pluto.tv`
- **What it is for:** Playing the free internet channels the owner set aside (mostly Pluto TV) on the kiosk screen, or casting them to a TV in the house. For a cast the box fetches the channel and relays it, so the TV never holds the session.
- **What leaves the house:** For Pluto TV: a request for an anonymous viewing session (no account, no name), which Pluto ties to the house's public IP address and rough location, the way the Pluto app on any TV does. Then, for every free channel: requests for that channel's video and ads from the servers its playlist names, from the house's IP address.
- **Whose service:** Pluto TV (Paramount) and the free channels' video and ad servers
- **Switch:** `egress_doors.tv_free_channels` (env `PUFFPAI_EGRESS_TV_FREE_CHANNELS`)
- **Ships:** off
- **Enforced by:** the code that opens the socket, which asks this switch first and refuses before any socket opens, then applies its own further checks on top. The refusal happens there rather than in the registry's request wrapper because that code reaches the network through a library or a separate process (the dashboard server) the wrapper cannot wrap. **Turning this switch off does stop it.** See the note below.
- **Note:** Enforced at two call sites. tv_cast.py calls require_open("tv_free_channels") before the Pluto session request and before EVERY relayed playlist or segment fetch, so closing the switch stops a cast that is already running, and records its hops in the egress ledger by host. A live channel fetches a segment every few seconds, so within one cast each host and outcome is written the first time, then at most once every 10 minutes with a count of the fetches in between, and any count still open is written when the cast ends. Not routed through egress_request because the video hosts come from the channel's playlists; tv_cast instead uses netaddr's pinned PUBLIC-posture session (every address checked, LAN and loopback refused), https on port 443 only, and re-validates each redirect hop. The Pluto session token stays in the API process's memory: it is never logged, never written to disk, and never sent to Home Assistant or the TV (both only see an opaque /cast/<id> link that dies on Stop). tv_player.py asks the same switch before it starts the kiosk player for a free channel, refuses while it is closed, and records each start in the ledger. Before the container starts it checks only the channel's own address from the list (https, and not an address inside the house). While this switch is open, the player container then fetches the playlist's own redirects and segments itself, on the host network, with no address check, no https check and no ledger line, so a playlist could point it at a device on your network. The relay used for casting checks every hop; the kiosk player does not. Closing the switch does not end a channel already playing; Stop does.

### `map_data_updates`, third party

- **Goes to:** `download.geofabrik.de`, `download1.graphhopper.com`
- **What it is for:** Downloads new map data for the region this box was built for (the Texas example region unless you built another) about once a month so directions stay current. Your questions and trips never go out.
- **What leaves the house:** Only requests for the region's public map files: the road map from download.geofabrik.de and the place list from download1.graphhopper.com. Like any download, it shows those two sites this house's public IP address. No place you asked about, no trip and no home location goes with it. Directions keep working with this shut, on the map data already on the box.
- **Whose service:** Geofabrik (road map files) and GraphHopper (place files), both publishing OpenStreetMap data
- **Switch:** `egress_doors.map_data_updates` (env `PUFFPAI_EGRESS_MAP_DATA_UPDATES`)
- **Ships:** off
- **Enforced by:** the egress-door registry. With the switch off the call is refused before any socket opens, before the hostname is even looked up. With the switch on, the host must be one of those named above AND must resolve to a public address, and the connection is pinned to it.
- **Note:** The ONLY caller is puffpai.maps_refresh, run from a nightly puffpai cron entry (deploy/maps/refresh.sh) that acts only when an update is due, about every 28 days. It streams the two files through egress_request (every redirect re-checked against these two hosts), checks each against the .md5 published beside it on the same host, then hands them to deploy/maps/build_release.sh and activate_release.sh, which never touch the live maps until a new build has passed its checks. With the switch off the job contacts nobody and records OFF. GET and HEAD requests only; no credential, cookie or identifier is sent (User-Agent PuffPAI-maps/1.0).

### `knowledge_pack_downloads`, third party

- **Goes to:** `download.kiwix.org`, `lb.download.kiwix.org`, `mirror.download.kiwix.org`, `dumps.wikimedia.org`, `ftp.nluug.nl`, `ftpmirror.your.org`, `mirror-sites-in.mblibrary.info`, `ny.mirror.driftle.ss`, `wi.mirror.driftle.ss`, `api.nasa.gov`, `docs.astropy.org`, `medlineplus.gov`
- **What it is for:** Download a knowledge pack the owner picked from the vetted source list, with the checksum Kiwix publishes for it, so the library can be read offline afterwards.
- **What leaves the house:** Only requests for the public pack files the owner confirmed, their checksums and the sites' robots.txt. Like any download, it shows those sites this house's public IP address. No question, document or household data goes with it.
- **Whose service:** Kiwix (download.kiwix.org) and its official mirrors; NASA, Astropy and MedlinePlus for their three rows
- **Switch:** `egress_doors.knowledge_pack_downloads` (env `PUFFPAI_EGRESS_KNOWLEDGE_PACK_DOWNLOADS`)
- **Ships:** off
- **Enforced by:** the egress-door registry. With the switch off the call is refused before any socket opens, before the hostname is even looked up. With the switch on, the host must be one of those named above AND must resolve to a public address, and the connection is pinned to it.
- **Note:** The ONLY caller is puffpai.source_fetch (the setup wizard's Integrate step and setup_ops.fetch_source), and only for a row of the shipped source registry whose license, size and address the owner confirmed. Every hop goes through egress_request with follow_redirects=False, so source_fetch applies its own rule on each redirect: a Kiwix download may go download.kiwix.org, then lb.download.kiwix.org, then one listed mirror serving that same path, and nowhere else. The file is checked against the .sha256 lb.download.kiwix.org publishes for it before it is installed.

### `connectivity_probe`

- **Goes to:** only a host some other door you opened already contacts. This door has none of its own
- **What it is for:** offline_guard.has_internet() — 'is there a WAN link at all?', asked before an online feature reports itself offline.
- **What leaves the house:** Nothing this door chooses. The probe opens a TCP connection to a host that is ALREADY behind a door you turned on, so it can tell you nothing a door you opened does not already tell that same host. With every door shut it contacts nobody and answers 'offline'.
- **Switch:** `egress_doors.connectivity_probe` (env `PUFFPAI_EGRESS_CONNECTIVITY_PROBE`)
- **Ships:** contacts nothing until another door is open; the switch is a hard **off**, not an on
- **Enforced by:** the other doors. It can only reach a host an open door already contacts, so with every door shut it opens no socket at all.
- **Note:** It has no hosts of its own. Probing fixed public hosts such as 1.1.1.1, 8.8.8.8 or www.google.com would contact parties nobody in the household chose, on an appliance that never needs the cloud. Instead probe_targets() returns hosts drawn from the doors that are open right now, so a fully shut appliance opens zero external sockets. The flag is a hard OFF switch, not an on switch: set it false and has_internet() stops probing entirely and reports offline. Leaving it unset does not open anything: the open doors decide.

### `model_pull`, third party

- **Goes to:** `registry.ollama.ai`
- **What it is for:** Download a language model into Ollama on this box — the setup wizard's 'get the brain' step, and nothing else.
- **What leaves the house:** The name of the model being downloaded, and this house's IP address by the act of connecting. No household data, no question anyone asked. Gigabytes come back the other way.
- **Whose service:** Ollama (registry.ollama.ai)
- **Switch:** `egress_doors.model_pull` (env `PUFFPAI_EGRESS_MODEL_PULL`)
- **Ships:** off
- **Enforced by:** the code that opens the socket, which asks this switch first and refuses before any socket opens, then applies its own further checks on top. The refusal happens there rather than in the registry's request wrapper because that code reaches the network through a library or a separate process (the dashboard server) the wrapper cannot wrap. **Turning this switch off does stop it.** See the note below.
- **Note:** Enforced at the call site, brain_health.pull_model, which asks egress_allowed('model_pull') before the POST. It cannot go through egress_request because OUR socket goes to Ollama at config `ollama.url` — loopback, or the LAN address the owner configured — and it is OLLAMA that then fetches from registry.ollama.ai. The box egresses even though this process does not, which is exactly why the row says registry.ollama.ai and not the Ollama URL: the door names where the data actually goes. The wizard offers the download only with this door open and prints the manual command otherwise.

### `embedding_model_download`, third party

- **Goes to:** `chroma-onnx-models.s3.amazonaws.com`
- **What it is for:** Fetch the sentence-embedding model files ChromaDB needs, once (~80 MB). Without them the knowledge library can be listed and read but not searched by meaning.
- **What leaves the house:** Nothing but the request itself — no query, no household data. Amazon S3 learns this house's IP address and that it asked for a public model file.
- **Whose service:** Amazon S3 (ChromaDB's public model bucket)
- **Switch:** `egress_doors.embedding_model_download` (env `PUFFPAI_EGRESS_EMBEDDING_MODEL_DOWNLOAD`)
- **Ships:** off
- **Enforced by:** the code that opens the socket, which asks this switch first and refuses before any socket opens, then applies its own further checks on top. The refusal happens there rather than in the registry's request wrapper because that code reaches the network through a library or a separate process (the dashboard server) the wrapper cannot wrap. **Turning this switch off does stop it.** See the note below.
- **Note:** Enforced at the call site, embeddings._chroma_default_ef, which checks whether the files are already on disk and asks egress_allowed('embedding_model_download') only when they are not. chromadb opens its own socket for this download, so egress_request cannot wrap it; the gate runs before the embedding function is constructed. Once the files are cached this door is never consulted again, and a box that got them by sneakernet never opens it at all. The same check runs again on every call of the built function, and before the knowledge search's degraded fallback (used when the configured embedder fails) lets chroma embed a question with a collection's own function: with the files absent and this door shut, that fallback is skipped rather than allowed to download.

### `spotify`, third party

- **Goes to:** `accounts.spotify.com`, `api.spotify.com`
- **What it is for:** Control Spotify by voice and from the dashboard: connect the Spotify account, then play, pause, skip and search on the appliance's own Spotify speaker.
- **What leaves the house:** The Spotify app credentials and account token you connected, what is being played or searched for, and the playback commands. Spotify also learns this house's public IP address.
- **Whose service:** Spotify AB
- **Switch:** `egress_doors.spotify` (env `PUFFPAI_EGRESS_SPOTIFY`)
- **Ships:** off
- **Enforced by:** the egress-door registry. With the switch off the call is refused before any socket opens, before the hostname is even looked up. With the switch on, the host must be one of those named above AND must resolve to a public address, and the connection is pinned to it.
- **Note:** Covers every call spotify_control.py makes: connecting the account, refreshing its token, and each Web API request. With the switch off none of them is looked up or dialled, and the reply says Spotify is switched off. Not covered: the librespot containers (docker-compose.spotify.yml and docker-compose.spotify-voice.yml). They are optional services you start yourself, and while one runs it signs in to Spotify and streams from it on its own, whatever this switch says. This switch cannot stop them; stopping the container is the switch for that path.

## Skills you install

A skill is a small Python package somebody in this house approved (see `docs/EXTENDING.md`).
It runs in a sandbox with **no network of its own**, so it cannot open a socket at all;
it can only ask this appliance to fetch a URL for it, and that ask goes through a door
of its own named `skill:<id>:0`.

The rules for that door, none of which a skill can change about itself:

- **Its hosts are the ones in the skill's `manifest.json` under `permissions.network_hosts`**, shown to you on the consent card before you approve anything. A host the manifest did not name is refused even when the door is open.
- **It ships shut, like every other door**, and the switch is keyed to a digest of that exact host set. Re-propose the same skill with one more host and it is a different key — so a skill cannot widen its own reach by editing a file after you said yes.
- **`https` only, port 443 only, `GET` only**, with a cap on the URL, the query string, the number of requests per run and the size of the answer.
- **A host that resolves onto your own network is refused.** Skill doors are public-internet doors: the address check above applies to them unchanged, so a skill cannot reach your router, your NAS or anything else indoors, whatever name it declares. There is no LAN skill door in this build.
- **Switch the skill off, or remove it, and the door and your `true` for it both go** in the same breath.
- **A single refusal switches the skill off.** A skill reaching for a host you did not approve is not a skill to retry.

No skill is installed on the box this page was generated from, so there are no skill
doors below. `GET /system/status` is the live list.

## Settings that are not doors

A door says **where** this appliance may go. These say **who on the box may walk
through an open one**. They open nothing by themselves: with every web door shut,
turning one of these on changes nothing at all.

### `chat_allow_egress`

- **What it decides:** Whether an ad-hoc chat turn may carry an egress budget at all — that is, whether asking PuffPAI a question in chat can make her look something up online.
- **What leaves the house:** Nothing leaves the house because of this setting alone. It decides whether a chat turn may use the web doors you have opened; with every web door shut it changes nothing.
- **Switch:** `agents.chat_allow_egress` (env `PUFFPAI_CHAT_ALLOW_EGRESS`)
- **Default:** off
- **Note:** Read by api_server._chat_envelope_budget and by assistant_tools.ask_agent, which are the only two places a chat envelope can acquire allow_egress. Both AND it with the relevant door, so this opt-in can never widen reach past the doors: it gates WHO may walk through, never WHERE.

These resolve through the same four tiers a door does. Environment, then the owner
overlay, then the config file, then the code default. The overlay for them is
`$PUFFPAI_DATA_DIR/state/egress_policy.json`, a separate file from the doors' own so
that changing one can never touch the other. `POST /system/egress/policy` (owner-
authenticated, body `{"id": …, "value": true|false}`) is what the Settings screen calls.

## The record

Every door decision, allowed **and refused**, is appended to `security_logs/egress.jsonl`
on the appliance, with the door, the host, the URL path, which part of the code asked,
and how many bytes were sent. Refusals also get one line in the system journal at
WARNING, so `journalctl -u puffpai-api | grep 'egress REFUSED'` answers "did this thing
ever try to phone home" without reading any file.

The ledger never records a query string, a request body, a header or a token. Only
hosts, paths and byte counts. A request refused because its host pointed inside your
house is recorded as `resolved_private` with the KIND of address it was, never the
address. It rotates at 4 MB and keeps 3 older generations, so the whole trail is
capped at 16 MB and cannot fill the disk.

## The house's own resolver, Zone 3

This one has no switch in the list above.
It is written down anyway, because it is a real connection out of your house.

When **Zone 3 is switched on**, meaning the sinkhole running AND the router pointed at this
appliance, every name any device in the house looks up is answered here. Names the
appliance does not already know are asked of **Quad9**, over encrypted DNS:

- **Where it goes:** `https://dns.quad9.net/dns-query` (DNS-over-HTTPS), falling back to
  `tls://dns.quad9.net` (DNS-over-TLS) if that is blocked. Quad9's plain addresses
  `9.9.9.9` and `149.112.112.112` are used only to find those two names in the first place.
- **What leaves the house:** the name being looked up, and nothing else. Not who asked.
  The appliance does not forward client subnet information, and it never sends a reverse
  lookup of an address on your own network upstream, so the shape of your home does not
  travel with the question. The connection is encrypted, so the network between your
  house and Quad9 cannot read the names either, which is a change in your favour: your
  internet provider's resolver could, and did.
- **Switch:** `zone3.enabled` in `config/puffpai_config.json`, and the router field that
  points the house here. **Both ship off.** With either one off, this path does not exist
  and your devices resolve names exactly the way they did before.
- **Who else is on this path:** nobody. No filter subscriptions are installed and no list
  is fetched on a schedule, so the resolver talks to Quad9 and to nothing else.

Every question asked and answer given is written to the appliance's own query log, which
stays on the appliance for 90 days and is readable at the AdGuard console. That log is
the point: it is how you find out what your television has been saying about you.

## Node-RED flows, only if you import them

The example flows in `nodered_flows/flows.json` are not loaded on a fresh install. If
you import them into Node-RED, **Node-RED itself** makes the calls below, so they follow
the `weather` and `news_rss` switches above in two places:

- **The flow asks first.** Before each call to the internet, the flow asks the backend
  (`GET /system/egress/door/<id>`, with `PUFFPAI_API_KEY`) whether the switch is on, and
  skips the call unless the answer is a clear yes. A switch that is off, a missing key, a
  backend that does not answer or an answer it cannot read all mean no. The flow logs
  why, and a request made to the flow is answered with that reason. This covers the
  `weather_alerts` schedule too, which runs on its own every two hours.
- **The backend will not start them.** The assistant's automation tools (a daily
  briefing, for example), the scheduled tasks and `POST /automation/trigger` refuse to
  start one of these flows while any switch it needs is off, and say which switch.

The calls:

- **`api.weather.gov`** (the US National Weather Service), under `weather`, from the
  `daily_weather`, `daily_briefing` and `weather_alerts` flows. What leaves the house: the
  latitude and longitude the flow is given (or `WEATHER_DEFAULT_LAT` / `WEATHER_DEFAULT_LON`),
  and a User-Agent naming PuffPAI. A forecast address outside `api.weather.gov` is not
  followed.
- **The news feed in `NEWS_RSS_URL`**, under `news_rss`, from the `daily_news` and
  `daily_briefing` flows. Unless you set another, that is `rss.nytimes.com` (the New York
  Times home page feed), which is not one of the hosts `news_rss` lists above. What leaves
  the house: only the request itself, so the publisher learns this house's public IP
  address.

**A copy of the flows without this check does not ask.** The backend still
refuses to start it, but its `weather_alerts` schedule calls `api.weather.gov` every two
hours whatever the switch says. Re-import `nodered_flows/flows.json`, or disable or delete
the old flows in the Node-RED editor. The switches cannot stop a flow that does not ask,
so anyone who can change the flows in that editor (`127.0.0.1:1880`) can also take the
question out.

## What is deliberately not a door

These reach machines on your own network or on the appliance itself, so nothing
leaves the house and there is nothing to switch:

- the local model runtime (`127.0.0.1:11434`)
- the loopback SearXNG instance (`127.0.0.1:8888`)
- Home Assistant, Calibre, Zigbee2MQTT and Node-RED on your LAN
- `workspace_exec` and `document_convert`, which run with no network namespace at all
- the grocery search link on a shopping list, when a household configures one: a URL you click, never something the appliance fetches

Jellyfin is reached on your LAN too, but it is its own program and can reach the internet
itself. If a household adds an internet IPTV tuner to Jellyfin's Live TV (an M3U playlist
of internet channels), Jellyfin fetches those channels from the internet whenever someone
watches one, and its metadata and plugin downloads go out too when they are turned on in
Jellyfin. The `tv_free_channels` switch does not control Jellyfin-carried channels: it
covers only the kiosk player's free channels and the cast relay. To stop those fetches,
change the settings in Jellyfin itself.

