# Offline maps: RUNBOOK

Valhalla (routes) and Photon (places) for one region, running on the
HomeBot machine as two Docker services (linux/arm64 or linux/amd64), with
data releases that are built next to the live data and swapped in
atomically.

**The build ships a Texas example region.** Everything below uses Texas
files; "Map region" at the end says how to build for another region.

Plain steps, in order. The first full build runs on the HomeBot machine,
overnight, while the models are idle. The machine downloads nothing in the
first run: every file is downloaded on a workstation and copied over.
Measured times and memory below come from a Jetson AGX Orin; a
different machine differs.

## Paths used below

| Name | What it is |
|---|---|
| `$HOMEBOT_DIR` | your HomeBotBuild checkout; this folder is `$HOMEBOT_DIR/deploy/maps/` |
| `$PUFFPAI_DATA_DIR` | the API service's data dir (install.sh: `~/.local/share/puffpai` unless you chose another) |
| `$MAPS_ROOT` | where map builds live: `$PUFFPAI_MAPS_ROOT` when set, else `$PUFFPAI_DATA_DIR/maps` |

The scripts and the API work these out the same way; set them in your shell
before the steps, for example:

```bash
export HOMEBOT_DIR=~/homebotbuild
export PUFFPAI_DATA_DIR="${PUFFPAI_DATA_DIR:-$HOME/.local/share/puffpai}"
export MAPS_ROOT="${PUFFPAI_MAPS_ROOT:-$PUFFPAI_DATA_DIR/maps}"
```

`$MAPS_ROOT` needs about 20 GB free during a build; put it (or the whole
data dir) on a disk with room. Run every step as the user the API service
runs as; the containers run as that user too.

## What is in this folder

| File | What it does |
|---|---|
| `docker-compose.maps.yml` | `puffpai-valhalla` on 127.0.0.1:8002, `puffpai-photon` on 127.0.0.1:2322. Memory caps 2 GB and 3 GB. Restart unless-stopped. Loopback only. |
| `build_release.sh` | Builds a new release from two local files. Never touches the live data. Go/no-go checks first. |
| `activate_release.sh` | Swaps a finished release in, checks it, rolls back by itself if a check fails. |
| `maps_common.sh` | Shared settings: pinned images, ports, smoke landmarks. |
| `pbf_header.py` | Reads the map data date out of the .pbf file. |
| `refresh.sh` | The map data refresh: downloads through the "Map data updates" Bridge connection (ships closed), then build + activate. See "The monthly refresh". |
| `crontab.maps-refresh` | The one crontab line for the refresh (nightly 3:30 AM; the job acts only when an update is due). |

## Data layout

```
$MAPS_ROOT/
  bin/photon-1.3.0.jar           the Photon program (checked by sha256)
  incoming/                      the downloaded source files
  images/                        the two Docker image files (first run only)
  releases/<YYYYMMDD-HHMM>/      one folder per build
      valhalla/                  valhalla.json, tiles.tar, admins.sqlite
      photon/photon_data/        the Texas place index
      META.json                  data date, build time, checksums, versions, memory peaks
      READY                      written last, only if every smoke check passed
  current  -> releases/<id>      what is being served
  previous -> releases/<id>      the last good one, kept for rollback
  logs/build-<id>.log
```

`META.json` field `data_date` (YYYY-MM-DD) is the date the screen and the
printed directions show ("Map data from <date>"): the day Geofabrik cut the
Texas file. The full time is in `data_timestamp`.

Two one-line status files tell you how the last run ended (read them with
`cat`): `build.status` and `activate.status`, both in `$MAPS_ROOT/`.

## Pinned versions

| Piece | Pin |
|---|---|
| Valhalla 3.9.1 | `ghcr.io/valhalla/valhalla:3.9.1`, multi-arch index `sha256:4fd114eb1d26b8fc2f43bcb3df234c1b63a0bf13c277405e5943d5361dc575de` (the default); linux/arm64 manifest `sha256:abd5743085e7ba902cbce443e788ac76830252ae63f8cff2f6f7c63c31b912e4` |
| JRE for Photon | `eclipse-temurin:21.0.12.1_1-jre-noble`, multi-arch index `sha256:000fd431958bc81a24abe1e8e5f0f0fd3ae365a594bd50aadb20696805f9408c` (the default); linux/arm64 manifest `sha256:ad32e01f3e9051e9f69ee3f41bc4826b580569a9f738368bb64b6d2a148824ff` |
| Photon 1.3.0 jar | sha256 `a89707c0045e4807b2a1180e132e68e108d998709f48b6c94b98a6e281f571a5` (matches the GitHub release) |

The default pins are the multi-arch indexes, which resolve to the right image
on arm64 and amd64 when Docker pulls. After an offline `docker save` +
`docker load` (step 4 and part 2 step 3) only that architecture's manifest
digest resolves, so set `VALHALLA_IMAGE` and `JRE_IMAGE` to it. For arm64
that is the manifest digest above; for amd64 read it from the index with
`docker buildx imagetools inspect ghcr.io/valhalla/valhalla:3.9.1` (and the
same for the JRE), the `linux/amd64` line.

## First run, part 1: on a workstation

Downloads, total about 6.2 GB. Use a plain User-Agent; never an email or name.

```bash
mkdir -p ~/Downloads/puffpai-maps && cd ~/Downloads/puffpai-maps
UA='puffpai-maps/1.0'

# 1. Texas road and map data from Geofabrik: about 724 MB, refreshed daily.
curl -fL -A "$UA" -o texas-latest.osm.pbf     https://download.geofabrik.de/north-america/us/texas-latest.osm.pbf
curl -fL -A "$UA" -o texas-latest.osm.pbf.md5 https://download.geofabrik.de/north-america/us/texas-latest.osm.pbf.md5
md5sum -c texas-latest.osm.pbf.md5     # must say OK. If not, Geofabrik updated in between: download both again.

# 2. Place data (Photon dump of the whole USA) from GraphHopper: about 5.1 GB, refreshed weekly.
curl -fL -A "$UA" -o photon-dump-usa-1.0-latest.jsonl.zst     https://download1.graphhopper.com/public/north-america/usa/photon-dump-usa-1.0-latest.jsonl.zst
curl -fL -A "$UA" -o photon-dump-usa-1.0-latest.jsonl.zst.md5 https://download1.graphhopper.com/public/north-america/usa/photon-dump-usa-1.0-latest.jsonl.zst.md5
md5sum -c photon-dump-usa-1.0-latest.jsonl.zst.md5

# 3. Photon program: 98 MB.
curl -fL -A "$UA" -o photon-1.3.0.jar https://github.com/komoot/photon/releases/download/1.3.0/photon-1.3.0.jar
echo "a89707c0045e4807b2a1180e132e68e108d998709f48b6c94b98a6e281f571a5  photon-1.3.0.jar" | sha256sum -c

# 4. The two Docker images for the HomeBot machine's architecture, saved to
#    files: about 230 MB and 104 MB. ARCH is arm64 or amd64.
ARCH=arm64
docker pull --platform linux/$ARCH ghcr.io/valhalla/valhalla:3.9.1
docker pull --platform linux/$ARCH eclipse-temurin:21.0.12.1_1-jre-noble
docker save --platform linux/$ARCH -o valhalla-3.9.1-$ARCH.tar ghcr.io/valhalla/valhalla:3.9.1
docker save --platform linux/$ARCH -o temurin-21-jre-$ARCH.tar eclipse-temurin:21.0.12.1_1-jre-noble
```

Why the place data is 5.1 GB: nobody publishes a Texas-only Photon file.
GraphHopper's smallest one is the whole USA. The build keeps only the
places tagged `US-TX` (about 6% of the file, an estimate from Texas's share of
US map data), so the machine ends up with a small Texas index. The alternatives
are heavier: the ready-made USA index is a 10.5 GB download and 2 to 3
times the disk and memory, and building places from the 724 MB Texas file
needs a Nominatim database server (PostgreSQL, hours of import, more memory
than a home AI machine can spare next to the models). A lighter copy is possible by
cutting the file to Texas on the workstation first (roughly 0.3 to 0.5 GB to
copy, estimate), but on a home network the full copy takes only a few
minutes, so this RUNBOOK copies the whole file and the machine does the cut.

Copy to the HomeBot machine (about 6.2 GB over the home network). Replace
`<user>@<machine>` with the API service's user and the machine's address,
and `<maps-root>` with its `$MAPS_ROOT`:

```bash
ssh <user>@<machine> 'mkdir -p <maps-root>/{bin,incoming,images,releases,logs}'
scp texas-latest.osm.pbf texas-latest.osm.pbf.md5 \
    photon-dump-usa-1.0-latest.jsonl.zst photon-dump-usa-1.0-latest.jsonl.zst.md5 \
    <user>@<machine>:<maps-root>/incoming/
scp photon-1.3.0.jar <user>@<machine>:<maps-root>/bin/
scp valhalla-3.9.1-$ARCH.tar temurin-21-jre-$ARCH.tar <user>@<machine>:<maps-root>/images/
```

## First run, part 2: on the HomeBot machine (as the API service's user)

1. **This folder comes with the checkout** at `$HOMEBOT_DIR/deploy/maps/`.
   Nothing in it starts by itself.

2. **Check the copies arrived whole:**
   ```bash
   cd $MAPS_ROOT/incoming && md5sum -c *.md5
   echo "a89707c0045e4807b2a1180e132e68e108d998709f48b6c94b98a6e281f571a5  $MAPS_ROOT/bin/photon-1.3.0.jar" | sha256sum -c
   ```

3. **Load the images** (no download; adds about 1.4 GB on the root disk)
   and prove they are the pinned ones. Each `inspect` must print an ID; an
   error means the file is not the pinned image. The digests below are the
   arm64 ones; on amd64 use the amd64 digests (Pinned versions). Keep the two
   values exported for every later step, so the scripts use the loaded images:
   ```bash
   docker load -i $MAPS_ROOT/images/valhalla-3.9.1-$ARCH.tar
   docker load -i $MAPS_ROOT/images/temurin-21-jre-$ARCH.tar
   export VALHALLA_IMAGE=ghcr.io/valhalla/valhalla:3.9.1@sha256:abd5743085e7ba902cbce443e788ac76830252ae63f8cff2f6f7c63c31b912e4
   export JRE_IMAGE=eclipse-temurin:21.0.12.1_1-jre-noble@sha256:ad32e01f3e9051e9f69ee3f41bc4826b580569a9f738368bb64b6d2a148824ff
   docker image inspect --format '{{.Id}}' "$VALHALLA_IMAGE"
   docker image inspect --format '{{.Id}}' "$JRE_IMAGE"
   ```

4. **Optional "before" measurement.** Record free memory and container use
   before the maps services exist, to compare afterwards:
   ```bash
   free -m | tee $MAPS_ROOT/logs/measure-before.txt
   docker stats --no-stream >> $MAPS_ROOT/logs/measure-before.txt
   ```

5. **Build, overnight, while the models are idle** (nobody talking to Puff).
   The script refuses to start (NO-GO) unless: no other maps build is
   running, free memory is at least 10 GB (8 GB build cap plus 2 GB
   margin), and `$MAPS_ROOT` has at least 20 GB free.
   Start it detached (`--detach`): it runs in its own session, so closing
   the terminal or a dropped ssh connection cannot stop it. It prints the
   log and status paths and returns at once.
   ```bash
   bash $HOMEBOT_DIR/deploy/maps/build_release.sh --detach \
     $MAPS_ROOT/incoming/texas-latest.osm.pbf \
     $MAPS_ROOT/incoming/photon-dump-usa-1.0-latest.jsonl.zst
   tail -f $MAPS_ROOT/logs/build-*.out      # watch; Ctrl+C stops watching, not the build
   cat $MAPS_ROOT/build.status             # STARTING, then RUNNING <id> pid <n>, then the result
   ```
   The result line is one of: `READY <id>` (go to step 6), `NOGO` (a
   pre-check refused; the reason follows), `FAILED` (the reason follows;
   nothing changed), `INTERRUPTED` (stopped by a signal; nothing changed).
   The Texas place cut must hold at least 200,000 places (a real cut is
   around a million); fewer means a wrong or truncated download, and the
   build fails.
   Expected on a Jetson AGX Orin. **These are estimates**; the real numbers land in
   `META.json` (`build_seconds`, `peak_mem_mb`):

   | Step | Time (estimate) | Memory |
   |---|---|---|
   | Valhalla road tiles, 4 threads, nice/ionice | 30 to 75 min | capped at 8 GB (expect 3 to 6 GB) |
   | Cut the USA place file to Texas (streams about 60 GB uncompressed, stores about 3 to 4 GB) | 10 to 25 min | small |
   | Photon import of Texas | 15 to 45 min | capped at 4 GB (3 GB heap) |
   | Smoke checks on spare loopback ports (18002, 12322) | under 10 min | 2 GB + 3 GB caps |
   | **Total** | **about 1 to 2.5 hours** | **peak at or under 8 GB** (steps run one at a time) |

   Disk: about 15 GB in use during the build, about 4 to 7 GB per finished
   release (estimate), two releases kept.

   The smoke checks route the Texas State Capitol (Austin) to downtown Waco
   by car, on foot and by bike, and search "Alamo" and "Texas State
   Capitol". Only if all pass does it write `READY`. It ends with
   `release <id> READY ... Activate with: activate_release.sh <id>`.
   Any failure: it says FAIL, deletes the half-built release, and the live
   maps (if any) are untouched.

6. **Activate** (detached too, about a minute):
   ```bash
   bash $HOMEBOT_DIR/deploy/maps/activate_release.sh --detach <id>
   cat $MAPS_ROOT/activate.status          # wait for a result line
   ```
   It points `current` at the release, recreates both containers, waits
   for both to be healthy, then checks one drive and one search. Result:
   `OK <id>`; `ROLLED_BACK <id> -> releases/<old>` (a check failed or it
   was interrupted, and it went back to the last good release by itself);
   `FAILED` (first activation did not finish: maps are off, nothing is
   served; or the current release is unhealthy with nothing to fall back
   to); `ROLLBACK_FAILED` (maps are down: see "Roll back by hand").
   On success it deletes releases older than the new one, except `previous`.
   Running it again with the id that is already current is safe: it
   recreates the engines on it and verifies (`OK <id> (already current,
   verified)`), and falls back to `previous` if that fails.

7. **Verify:**
   ```bash
   docker ps --filter name=puffpai-valhalla --filter name=puffpai-photon   # both "(healthy)"
   ss -ltn | grep -E ':8002|:2322'                                          # 127.0.0.1 only, never 0.0.0.0
   grep data_date $MAPS_ROOT/current/META.json
   . $HOMEBOT_DIR/deploy/maps/maps_common.sh && run_checks 8002 2322 all    # 3 routes + 2 searches, all "ok"
   ```
   No firewall change is needed: both ports are loopback only.

8. **Optional "after" measurement:** repeat step 4 with
   `measure-after.txt`, plus `docker stats --no-stream puffpai-valhalla
   puffpai-photon`, and compare free memory.

## Roll back by hand

The quick way: activate the previous release (it still has `READY`):
```bash
readlink $MAPS_ROOT/current $MAPS_ROOT/previous
bash $HOMEBOT_DIR/deploy/maps/activate_release.sh --detach <id shown by previous>
```
If the scripts themselves are broken:
```bash
cd $MAPS_ROOT
ln -sfn releases/<good id> .current.tmp && mv -Tf .current.tmp current
docker compose -f $HOMEBOT_DIR/deploy/maps/docker-compose.maps.yml up -d --force-recreate valhalla photon
```
To switch the maps off completely:
`docker compose -f $HOMEBOT_DIR/deploy/maps/docker-compose.maps.yml down`.
Directions then answer that the map engines are not running; nothing else
depends on these two services.

## Interruptions (Ctrl+C, kill, ssh drop, kill -9)

- **Build, Ctrl+C / `kill` / hang-up:** it stops at once, removes its
  containers and the half-built release folder, writes `INTERRUPTED`, and
  never touches `current`. Started with `--detach`, a dropped connection
  does not stop it at all.
- **Activation, Ctrl+C / `kill` / hang-up:** if it had not changed anything
  yet, it writes `INTERRUPTED` and stops. Otherwise it switches back to the
  last good release and restarts the engines on it (`ROLLED_BACK`), or on a
  first activation turns the maps off cleanly (`FAILED`).
- **kill -9** (or a power cut) cannot be caught:
  - build: the status file keeps saying `RUNNING ... pid <n>`, but
    `ps -p <n>` shows no such process. Just start the build again: it first
    removes the leftover build containers and any release folder without
    `READY`. `current` was never touched.
  - activation: `current` may already point at the new release while the
    engines are half restarted. Run the same command again
    (`activate_release.sh --detach <same id>`): it brings the engines up on
    `current`, verifies, and falls back to `previous` if that fails.

## If something goes wrong

- **NO-GO: RAM.** A model or another job is using the memory. Try later.
  Do not lower the margin to force it.
- **Valhalla step exits 137** (killed at the 8 GB cap): rerun with fewer
  threads: `BUILD_CPUS=2 bash .../build_release.sh --detach ...`.
- **Valhalla step exits 139** (crash in the "enhance" stage): a known
  intermittent Valhalla race at more than 1 thread, reported on arm64
  (github.com/valhalla/valhalla/issues/6331, closed without a fix). Rerun;
  if it repeats, `VALHALLA_THREADS=1` (slower, about 2 to 3 times).
- **Photon will not start and mentions `vm.max_map_count`:** many stock
  kernels have 65530. Photon's embedded search engine runs on loopback inside its
  container, where that check should not apply. If it does, raising it is
  a host setting change; make it deliberately.
- **Stopping Photon takes up to 10 to 30 seconds.** It is slow to shut
  down; activation waits for it. Harmless.

## The monthly refresh

`refresh.sh` (Python: `puffpai.maps_refresh`) downloads the same two files
on the HomeBot machine through the "Map data updates" Bridge connection (switch
`map_data_updates`), then runs the same two scripts. It **ships closed**:
until the owner switches it on in Settings, the job contacts nobody and
writes `OFF`. Hosts, and the only hosts it can reach:

- `download.geofabrik.de` (Texas .pbf and .md5; `-latest` redirects to a dated file on the same host)
- `download1.graphhopper.com` (Photon USA dump and .md5)

Engine or jar version changes are NOT covered (they would need `github.com`,
`ghcr.io`, the Docker registry hosts): those stay a workstation download, as
in the first run.

When it runs: cron starts it every night at 3:30 AM, and it decides whether
an update is DUE from its own status file, before reading the switch or
touching the network. Due means no completed check in 28 days (a check
completes with `OK`, or with `SKIPPED` because nothing newer exists). A
`FAILED` run is retried the next night; after 5 failed nights in a row it
stops and waits until 28 days after the first failure, and the status
reason says the date. A night with nothing due writes nothing and prints
nothing. `run-now` skips the due check, never the switch.

What one due run does: reads the switch (off: writes `OFF`, stops); checks free
disk (32 GB); fetches both `.md5` files; skips (`SKIPPED`) if they match the
last build, or if the road file is not more than 2 days newer than
`current/META.json` `data_timestamp`; streams both files into
`$MAPS_ROOT/downloads/<stamp>/` (resumes a partial copy; size bounds;
md5 must match, or the file is deleted and the run is `FAILED`); runs
`build_release.sh` (not detached: the job itself runs from cron); on `READY`
deletes the downloads and runs `activate_release.sh`. A failed build keeps
the downloads for ONE retry. `current` is only ever changed by
`activate_release.sh`, which rolls back by itself.

Result: `$MAPS_ROOT/refresh.status` (JSON: `last_run`, `result` =
OK, SKIPPED, OFF, FAILED or RUNNING, `reason` in plain words, `data_date`,
`last_ok`, `last_checked`, `fail_streak`; the API adds `next_check`). Logs: `$MAPS_ROOT/logs/refresh-*.log` (last 12 kept),
plus the scripts' own `build-*.log` and `activate-*.log`. API (key
required): `GET /maps/refresh/status`.

**Install (once, as the API service's user):** the crontab line carries two
placeholders, filled in here from the paths above.
```bash
chmod 755 $HOMEBOT_DIR/deploy/maps/refresh.sh
crontab -l > /tmp/puffpai.cron
grep -q 'deploy/maps/refresh.sh' /tmp/puffpai.cron \
  || sed -e "s#@HOMEBOT_DIR@#$HOMEBOT_DIR#g" -e "s#@DATA_DIR@#$PUFFPAI_DATA_DIR#g" \
       $HOMEBOT_DIR/deploy/maps/crontab.maps-refresh >> /tmp/puffpai.cron
crontab /tmp/puffpai.cron && rm /tmp/puffpai.cron
crontab -l | grep refresh.sh        # one line: 30 3 * * * PUFFPAI_DATA_DIR=... /bin/bash .../deploy/maps/refresh.sh run ...
bash $HOMEBOT_DIR/deploy/maps/refresh.sh status   # connection.open false, result NEVER
```
Nightly at 3:30 AM; the build refuses to start unless the models leave it
enough memory. No config change: the switch ships closed and only the owner
opens it.

**Run now (once, only after the owner has switched "Map data updates" on):**
```bash
bash $HOMEBOT_DIR/deploy/maps/refresh.sh run-now   # refuses (exit 3) while the switch is off
bash $HOMEBOT_DIR/deploy/maps/refresh.sh status    # RUNNING, then the result
```

To force the switch shut for both the API and this job, set
`PUFFPAI_EGRESS_MAP_DATA_UPDATES=0` in the API service's env file: for an
install.sh install that is `$PUFFPAI_DATA_DIR/homebot.env`; for the appliance
units in `systemd/` it is `/etc/puffpai/api.env` (or `Environment=` in the
puffpai-api unit). The job reads that one variable from each file that
exists (BOM, `export `, quotes and spaces are fine), and any value that is
not "on" wins. The job treats NO readable env file, or one it can't read,
as forced shut: status `FAILED`, "can't read the box's service settings" or
"is missing", nothing downloaded. The installer creates the file; do not
remove it.

## Map region

The build ships a **Texas example region**. The engines and the build
scripts work for any region; four things name Texas and change with it:

1. **The two source files.** Download your region's road file from Geofabrik
   (for example `https://download.geofabrik.de/europe/germany-latest.osm.pbf`,
   with its `.md5`) and the GraphHopper Photon dump that covers it (the
   country or continent dump under `https://download1.graphhopper.com/public/`).
   Pass both to `build_release.sh` exactly as in step 5; the script takes any
   file names.
2. **The build settings**, as environment variables for `build_release.sh`:
   `PHOTON_REGION_ISO` (the ISO 3166-2 subdivision code places must carry,
   default `US-TX`; the cut keeps every dump line that contains that code in
   quotes, so it must be a subdivision code such as `DE-BY`, not a bare
   country code), `PHOTON_MIN_PLACES`
   (the smallest believable place count, default 200000), and the smoke
   checks `SMOKE_FROM` / `SMOKE_TO` (two `lat,lon` points inside the region,
   on roads) and `SMOKE_SEARCHES` (two well-known names there, `|`-separated).
   Set the same `SMOKE_*` values when running `activate_release.sh`.
3. **The monthly refresh.** `SOURCES` in `src/puffpai/maps_refresh.py` holds
   the two download URLs and their size bounds. Change them to your region's
   files; if the hosts change, the `map_data_updates` entry's host list in
   `security/egress_doors.py` must change with them (and the Bridge document
   be regenerated), or every download is refused.
4. **Town names in answers.** `src/puffpai/maps_texas_places.txt` and
   `maps_texas_places_ambiguous.txt` are the towns Puff recognises by name,
   and replies say a town with ", Texas". Replace the two lists for your
   region; until then, place and address searches still work, but a bare
   town name may not be recognised and a town is still said with ", Texas".

**Building elsewhere and copying:** Valhalla road tiles built on an x86 PC
and served by the arm64 Valhalla (under emulation) give identical routes in
testing on a small extract, but the tile files are not byte-identical
across the two architectures and Valhalla does not document
cross-architecture tiles. Building on the HomeBot machine itself is the
supported path.
