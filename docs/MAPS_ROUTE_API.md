# Maps route API

Contract between the backend directions code and the dashboard pages that
build the kiosk steps view, the print page and the QR code.

The build ships a Texas example region; `deploy/maps/RUNBOOK.md` ("Map
region") says how to point it at another region's extract.

## Where the answers come from

`src/puffpai/maps_directions.py` answers "directions to X", "how do I get to X",
"how far is X" and "nearest X" (by car, on foot, by bike) from two engines on the
box, both on loopback:

| Engine | Config key (`maps` section) | Default |
|---|---|---|
| Valhalla (routing) | `maps.valhalla_url` | `http://127.0.0.1:8002` |
| Photon (place search) | `maps.photon_url` | `http://127.0.0.1:2322` |
| Map build metadata | `maps.meta_path` | `current/META.json` under the maps root, `$PUFFPAI_MAPS_ROOT` or `<data dir>/maps` (field `data_date`) |

A missing or null `maps` section means the defaults. A non-loopback engine URL is
refused (it reads as "the offline maps aren't running"). No Bridge connection is
used: the calls go through `security/lan_http.py`.

Every answered route is stored as a record and pushed to the kiosk. The home
point is the start of every route but is **never** in a record, a reply, a log,
a link or a screen item. The origin is always the label `"Home"`.

## Route record

Stored at `$PUFFPAI_DATA_DIR/maps/routes/<route_id>.json`
(`maps/routes/` under the per-user data dir when `PUFFPAI_DATA_DIR` is
unset). Same
convention as the home point file (`$PUFFPAI_DATA_DIR/config/home_point.json`),
through `statedir.resolve_runtime_dir`. Written atomically; the
newest 50 are kept.

```json
{
  "id": "3f9c0a1b2c3d4e5f",
  "created_at": "2026-10-11T18:04:05.123456+00:00",
  "mode": "drive",
  "origin": {"label": "Home"},
  "destination": {
    "label": "the Target on Example Road",
    "address": "100 Example Road, Testville",
    "lat": 31.150000,
    "lon": -99.650000
  },
  "distance_mi": 4.2,
  "duration_s": 720,
  "steps": [
    {"n": 1, "text": "Drive north on Elm Street.", "distance_mi": 0.5, "duration_s": 60},
    {"n": 2, "text": "Turn right onto Example Road.", "distance_mi": 3.6, "duration_s": 600},
    {"n": 3, "text": "Your destination is on the right.", "distance_mi": 0.0, "duration_s": 0}
  ],
  "data_date": "2026-09-30",
  "traffic_note": "Times assume normal traffic.",
  "omaps_url": "https://omaps.app/v2/nav?origin=currentLocation&destination=31.150000,-99.650000&destination_name=Target&mode=drive"
}
```

Field notes:

- `id`: 16 lower-case hex characters. The API refuses anything else with 400.
- `mode`: `drive`, `walk` or `bike`.
- `destination.address`: house number, street and city as Photon has them; may be
  `""`.
- `data_date`: `null` when META.json is missing. Show "Map data date unknown",
  never a guess.
- `traffic_note`: the string above for `drive`, `null` for `walk` and `bike`.
- `omaps_url`: the Organic Maps link for the QR code. **Needs a real phone scan
  test** before it is called working.
  - Form: `https://omaps.app/v2/nav?origin=currentLocation&destination=<lat>,<lon>&destination_name=<name>&mode=<drive|walk|bike>`.
  - Why this form: omaps.app/api documents
    `origin=currentLocation` only on `v2/nav`, so the phone starts from where
    it is and the home point never goes into a link or a QR code. The older
    `/route` examples always carry a start point (`sll`/`saddr`), and the
    `v2/dir` example carries a fixed `origin` with coordinates, so neither was
    used.
  - Mode: `v2/nav` documents a `mode` parameter, but its only example value is
    `drive`. `walk` and `bike` follow the same naming and are unproven until the
    phone scan test; if Organic Maps ignores them, the phone falls back to its
    own default mode.
  - `v2/nav` may start turn by turn guidance straight away on the phone. That
    is expected.
  - There is no Google link.
- The first step usually names the street the house is on (that is what turn by
  turn directions say). It is a street name, not coordinates.

## Endpoints

All three are behind the existing API key (`X-API-Key` or `Authorization:
Bearer`), like every non-public endpoint. The dashboard reaches them through its
server-side proxy; `maps` is on the proxy allowlist.

| Method and path | Answer |
|---|---|
| `GET /maps/routes/latest` | 200 with the newest record; 404 `{"error": "No routes yet."}` |
| `GET /maps/routes/<route_id>` | 200 with the record; 400 `{"error": "Not a route id."}` for a bad id; 404 `{"error": "No such route."}` |
| `GET /maps/status` | 200 `{"valhalla": "up"\|"down", "photon": "up"\|"down", "live": bool, "data_date": str\|null, "home_point_set": bool}` |

`/maps/status` checks each engine with a short loopback request (Valhalla
`/status`, Photon `/api?q=texas&limit=1`). `home_point_set` is a yes or no only.

The capability registry row `maps.directions` is probed the same way: `live` when
both engines answer, `offline` otherwise.

## Kiosk display event

When a route is answered (by voice or chat), the backend calls
`dashboard_display.set_display` with a new kind, `directions`. The kiosk's
existing poll of `GET /dashboard/display` then returns:

```json
{
  "current": {
    "kind": "directions",
    "url": "/dashboard/directions/3f9c0a1b2c3d4e5f/print",
    "title": "Directions to the Target on Example Road",
    "source": "voice",
    "meta": {"route_id": "3f9c0a1b2c3d4e5f", "mode": "drive"},
    "at": "2026-10-11T18:04:05+00:00",
    "seq": 42
  },
  "seq": 42
}
```

The dashboard renders `kind: "directions"` by fetching
`/maps/routes/<meta.route_id>` (steps, totals, data date, QR from `omaps_url`
with `app/_lib/qr.ts`). `url` is the print page the dashboard serves at
`/dashboard/directions/<route_id>/print`; a viewer that does not know the kind
yet can fall back to it. `source` is `voice` or `chat`.

## One conversation

Directions turns are ordinary turns in the household thread (`home-owner`).
Chat gets the full answer (summary line, numbered steps, traffic note, map data
date, the phone link, the print page path). A spoken turn says two sentences
(distance and time, then the first turn), held to `VOICE_MAX_SENTENCES`, then
"I've put the steps on the screen and in our chat."; the transcript and the
household thread keep the full numbered steps, so the phone's Send to Puff card
sees them. Home Assistant's voice assistant (non-streamed `POST
/v1/chat/completions`, not the dashboard's streamed call) is a voice surface
too: it hears the same two sentences, and the thread and the kiosk get the full
steps. "What about walking?" and "and by bike?" reroute the last
destination in the same conversation (kept in memory for 10 minutes). A
follow-up needs a follow-up phrase ("what about", "and by", "how about", "same
place by", "... instead"); a bare "walk" or "car" does not reroute.

## What is claimed (positive evidence)

- **Strong phrasings are claimed outright:** "directions to X" (with or
  without "walking/driving/biking"), "how do I drive/walk/bike to X", and
  "nearest/closest/next X" when X is a category or brand from the tables in
  `maps_directions.py`.
- **Weak phrasings need evidence that X is a place:** "how far is X", "how do I
  get to X", "how far are we from X", "how long to get to X", and "nearest X"
  with an X that is not a category or brand. Evidence is either static (X ends
  in a place word like park, airport or library; X is a street address like
  "1100 Congress Ave"; X is a Texas city or town from
  `src/puffpai/maps_texas_places.txt`, about 370 names written from general
  knowledge, not an official list), or a Photon hit whose name strongly
  matches X (at least 0.8 of X's words are in the name, and X covers at least
  half the name).
- Towns whose name usually means something else are listed in
  `src/puffpai/maps_texas_places_ambiguous.txt` (first names like Melissa and
  Allen, places outside Texas like Paris and Princeton, words like Fate and
  Humble). For those, a weak phrasing is claimed only with a Texas qualifier
  ("how far is Melissa, Texas", "Paris TX"); a strong phrasing ("directions to
  Paris") is still claimed. A town destination is always said and shown with
  its state ("Paris, Texas is 280 miles away"), in the reply, the record's
  `destination.label` and the kiosk title.
- With no evidence the turn is not claimed and normal answering runs. That
  includes having no home point, or Photon being down, when evidence would
  have needed Photon. So "how do I get to my calendar" never asks for a home
  point.
- Never claimed on any phrasing: "how do I get to sleep", "directions for this
  recipe", "how far is the moon", two places that are not home ("how far is
  Austin from Dallas"), times and app things ("how far is it to Christmas",
  "how do I get to the dashboard"), and things inside a building ("where's the
  nearest exit", "nearest bathroom", "the closest outlet in this room").
- Typed addresses match either spelling of street words (St/Street,
  Ave/Avenue, Rd/Road, Dr/Drive, Blvd/Boulevard, Hwy/Highway, Fwy/Freeway,
  Pkwy/Parkway, Ln/Lane, Ct/Court, N/S/E/W).

## Time limits

The directions decision is made once per request: `/chat/stream` checks it
first and may reach it again through the voice pipeline, so the outcome is
kept for the request and a hung engine is waited on once per turn.

Photon calls time out after 3 seconds and Valhalla after 5. A whole directions
turn waits on the engines for at most 6 seconds, then says "The offline maps
aren't running right now".

## Honest misses (fixed sentences)

| Case | Reply |
|---|---|
| No home point set | "I need your home point before I can give directions. Paste your home point into the Home point card in Settings." |
| Place not in the map | "I don't have that place in the offline map." |
| Engines not answering | "The offline maps aren't running right now, so I can't give directions yet." |
| No road route | "I found <place>, but I can't find a road route there in the offline map." |

No miss writes a route record or changes the screen.
