# Appendix: installing HomeBot on a Jetson AGX Orin

This is the **appliance appendix**, not the normal path. On any other machine
use [`INSTALL_LINUX.md`](INSTALL_LINUX.md) or
[`INSTALL_WINDOWS.md`](INSTALL_WINDOWS.md) and the one-command installers at the
repo root; `scripts/install_orin.sh` below is the Orin-specific procedure kept
from the Founders Edition build (system-wide `/opt/puffpai` + `/data/puffpai`,
root systemd units, NVIDIA power modes, ReSpeaker audio).

Add the appliance extras with the venv it creates:
`pip install -e ".[orin]"`.

Hardware and why: [`HARDWARE.md`](HARDWARE.md). Settings:
[`CONFIG.md`](CONFIG.md).

Every step below names the file it comes from. Steps the repo has **no script
for** are written out as manual commands and marked
`UNVERIFIED ON A FRESH BOX`, they are derived from what the code and units
require, not from a clean-install run we watched succeed.

## One-command install

`scripts/install_orin.sh` runs the whole procedure below from a checkout you
already have on the box. Clone the repo, read the script, then:

```bash
sudo bash scripts/install_orin.sh --dry-run   # prints every command, runs none
sudo bash scripts/install_orin.sh             # the default install
```

It refuses to run on anything that is not `aarch64` with `/etc/nv_tegra_release`
present, and it prints
`NOTE: this step has not been verified on a fresh box` on every phase this
document marks `UNVERIFIED ON A FRESH BOX`. Phases, in order: **preflight,
system, code, dashboard, env, ollama, services, verify**, each idempotent, and
no secret file is ever overwritten.

| Flag | Effect |
|---|---|
| `--dry-run`, `-n` | print every command that would run, execute none |
| `--yes`, `-y` | skip confirmations (unattended) |
| `--with-zone3` | also install `puffpai-zone3.service` (step 9; inert until the router points here) |
| `--with-kiosk` | also run `scripts/provision_kiosk.sh` (step 12) |
| `--skip-models` | do everything except `ollama pull` (step 4) |
| `--skip-voice` | do not install the user-level voice unit (step 10) |
| `--repo-dir PATH` | checkout to install from (default: the script's own repo) |

Two things it deliberately does **not** do: change the power mode (step 2), and
install `20-ha-token-failclosed.conf` before the HA token is in the Vault
(step 11). Node-RED flows still have to be imported by hand (step 9).

The rest of this document is the reference: every step the script takes, in the
same order, with the file and line it comes from. Read it when something fails,
or when you want to do it yourself.

---

Three scripts do the work:

- `scripts/install_orin.sh`, all of the below, in order, on a Jetson. Calls
  the other two.
- `scripts/install_ubuntu.sh`, the API-only path (dev boxes, x86_64): code,
  Ollama, models, venv, the API unit, a smoke check. Calls the next one for you.
- `scripts/provision_orin.sh`, OS packages, Docker, the service account, the
  `/data/puffpai` layout, the `/etc/puffpai` env files. **Run it three times**
  (source: `systemd/README.md`, "Run the script three times"): once up front,
  once after Ollama is installed but before any `ollama pull`, and once after
  the code is deployed.

---

## 1. OS layer

| Item | Value | Pinned in |
|---|---|---|
| Distribution | Ubuntu 22.04 (20.04 is the floor) | `scripts/provision_orin.sh:88-94` |
| JetPack | 6.2.2 | `scripts/provision_orin.sh:5` |
| L4T | r36.5 | `scripts/provision_orin.sh:5` (reports `/etc/nv_tegra_release` at `:147`) |
| Arch | `aarch64` (x86_64 accepted, not the reference) | `scripts/provision_orin.sh:95-98` |
| Python | 3.10+ | `setup.py` (`python_requires=">=3.10"`) |
| Node | `^20.19 \|\| >=22.12` | `dashboard/package.json` `engines` |

Flash JetPack, then partition, format and mount the NVMe at `/data` **by hand,
before anything else**, the provisioner deliberately does not touch partitions
(`scripts/provision_orin.sh:18-25`) and refuses to run with less than 80 GB free
(`:99-107`).

## 2. Power mode

`scripts/install_ubuntu.sh:216` prints `nvpmodel -q` and treats a missing
`nvpmodel` as an incomplete JetPack. Nothing sets the mode for you.
`src/puffpai/inference/model_options.py:172` records that the planner context
size was measured "on a box pinned at MODE_50W", so match that if you want the
shipped tuning to mean anything:

```bash
sudo nvpmodel -q            # what mode am I in
sudo nvpmodel -m <id>       # MODE_50W matches the shipped tuning
```

`UNVERIFIED ON A FRESH BOX`, the mode id differs between Orin SKUs; read
`nvpmodel -q --verbose` on your own board rather than copying an id.

## 3. Users, directories, secrets

```bash
sudo ./scripts/provision_orin.sh --dry-run   # read the plan, change nothing
sudo ./scripts/provision_orin.sh
```

Creates (`scripts/provision_orin.sh:268-408`):

| Path | Mode | Purpose |
|---|---|---|
| `puffpai:puffpai` | system account, `nologin`, in `audio` | service user; deliberately **not** in `docker` |
| `/opt/puffpai` | `0755 puffpai` | code checkout + `venv/` |
| `/opt/puffpai-dashboard` | `0755 puffpai` | built dashboard |
| `/data/puffpai/{models,knowledge,network,config,logs,chat_history,app_data,chromadb,home}` | `0750 puffpai` | all writable state |
| `/data/puffpai/vault` | `0700 puffpai` | Zone 1 |
| `/data/puffpai/models/ollama` | `2770 ollama:puffpai` | `OLLAMA_MODELS` |
| `/etc/puffpai/{api.env,dashboard.env}` | `0640 root:puffpai` | secrets, created empty, never overwritten |

Installs the OS package set: build tools, `tesseract-ocr`, `poppler-utils`,
`nftables`, `net-tools`, `libportaudio2`, `alsa-utils`, `sqlite3`, plus Docker's
compose v2 plugin when an engine is present without it (`:161-266`). It never
replaces a working engine, because JetPack's `docker.io` carries the NVIDIA
container runtime.

Node.js is **not** installed automatically (`:195-207`), adding a third-party
apt repo is left as a deliberate decision:

```bash
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt-get install -y nodejs
```

## 4. Ollama and the models

Order matters. Pulling before the second provisioning pass fails with `EACCES`
(`scripts/provision_orin.sh:413-427`):

```bash
curl -fsSL https://ollama.com/install.sh | sh
sudo install -D -m 0644 systemd/ollama.service.d/override.conf \
        /etc/systemd/system/ollama.service.d/override.conf
sudo systemctl daemon-reload && sudo systemctl restart ollama
sudo ./scripts/provision_orin.sh        # hands models/ollama to the ollama user

ollama pull gpt-oss:20b        # 13.8 GB  chat / general answering
ollama pull qwen3:30b-a3b      # 18.6 GB  agent classification + planning
ollama pull qwen2.5vl:7b       #  6.0 GB  vision
ollama pull nomic-embed-text   #  0.3 GB  embeddings
```

Where each tag is pinned in code: `config/puffpai_config.json` (`ollama.model` =
`gpt-oss:20b`, `ollama.vision_model` = `qwen2.5vl:7b`),
`src/puffpai/agents/engine.py:94-95` (`DEFAULT_CLASSIFY_MODEL` and
`DEFAULT_PLAN_MODEL` = `qwen3:30b-a3b`), `src/puffpai/config.py:274`
(`ollama_embedding_model` = `nomic-embed-text`).

An optional coding model is **off** by default: `ollama.code_model` is the empty
string (`src/puffpai/config.py`), and `src/puffpai/inference/code_model.py`
documents `qwen3-coder:30b` as the tag we tested. Pull it only if you
want the feature; it is an on-demand model, never pinned resident.

The override does two things and only two (`systemd/ollama.service.d/override.conf`):
`OLLAMA_MODELS=/data/puffpai/models/ollama`, and **`OLLAMA_HOST=127.0.0.1:11434`**.

Ollama has no authentication, no rate limit and no audit log: anything that can
open a TCP connection to 11434 can run your household model, list your models,
and `ollama rm` them. **This appliance ships with no host packet filter**, so
the bind address *is* the access control. Everything that needs Ollama, the
backend, the voice service, the CLI, runs on this host.

The one caller that cannot reach loopback is the Node-RED container's optional
`health_check` flow, which probes `OLLAMA_URL/api/tags` over the docker bridge.
With the loopback bind that single tile reports Ollama unreachable; nothing
else degrades and no automation depends on it. If you want it green, read
`systemd/ollama.service.d/20-lan-ollama.conf.example`, it binds `0.0.0.0`, and
it is only safe behind a packet filter that permits 11434 from loopback and
`172.17.0.0/16` and drops it from the LAN. Check from another machine:

```bash
curl --max-time 3 http://<this-box-lan-ip>:11434/api/tags   # must FAIL
```

`ollama.keep_alive` defaults to `"30m"`; the appliance wants the resident model
pinned with `-1` (`src/puffpai/config.py`).

## 5. Code and the Python venv

```bash
sudo -u puffpai git clone https://github.com/CypherSisters/HomeBotBuild.git /opt/puffpai
cd /opt/puffpai
sudo -u puffpai python3 -m venv venv
sudo -u puffpai venv/bin/pip install --upgrade pip wheel
sudo -u puffpai venv/bin/pip install -r requirements.txt

# Default embedding backend needs the legacy extra (pulls PyTorch; on Jetson,
# torch must come from NVIDIA's wheel index FIRST):
sudo -u puffpai venv/bin/pip install -e ".[embeddings-legacy]"
# OCR for image and scanned-PDF uploads:
sudo -u puffpai venv/bin/pip install -e ".[ocr]"

sudo ./scripts/provision_orin.sh        # third pass, see below
```

Source: `scripts/provision_orin.sh:429-441` and `setup.py` `extras_require`.

The third pass is not optional. With the code present it creates
`src/security_logs/`, `security_logs/`, `data/`, `data/library_sources/` and
`app_data/`, and seeds `blocked_devices.json` and
`src/puffpai/indexed_libraries.json` with `[]` (`:296-347`). There really are
two `security_logs` directories at different depths, because the modules compute
the repo root differently; `src/security_logs/` is the security audit trail.
If it is not writable, audit entries go to stderr and are lost, the log
**fails open** (`systemd/README.md`, "Things that will bite you").

## 6. Configuration

```bash
cp config/home_assistant_config.json.example config/home_assistant_config.json
$EDITOR config/puffpai_config.json
$EDITOR /etc/puffpai/api.env          # PUFFPAI_API_KEY=, HA_TOKEN=
$EDITOR /etc/puffpai/dashboard.env
```

Storage paths need **no** hand-editing. The units set
`PUFFPAI_DATA_DIR=/data/puffpai/state`, and the loader re-roots every *relative*
`paths.*` entry and `chromadb.path` under it
(`src/puffpai/config.py`, `anchor_relative_state_paths`). Absolute values you
write yourself are honoured verbatim, and a dev checkout with the variable unset
is unchanged.

That is what stops the once-a-minute `Vault unavailable: Read-only file system`:
the shipped values are relative and would otherwise resolve inside the read-only
`/opt` under `ProtectSystem=strict`
(`systemd/puffpai-api.service`, `systemd/README.md`). Full variable list:
[`CONFIG.md`](CONFIG.md).

`/etc/puffpai/api.env` and `/etc/puffpai/dashboard.env` already hold a
`PUFFPAI_API_KEY` that `install_orin.sh` generated for this box with
`openssl rand -hex 32`, the same value in both files plus `/opt/puffpai/.env`.
Do not replace it with a placeholder: the backend refuses to start when the
resolved key is empty, a known placeholder, or shorter than 16 characters
(`src/puffpai/api_server.py`, `validate_api_key`). What is left for you is the
third-party material we cannot mint: `HA_TOKEN`, `HA_BASE_URL`,
`HA_ACCESS_TOKEN`, `JELLYFIN_API_KEY`.

**Pairing starts at `PUFF_PAIRING_ENFORCE=knowledge`, on purpose.** Under `all`
every dashboard route is gated and the kiosk browser on this box is just another
unpaired client, with no localhost bypass by design
(`dashboard/app/_lib/server/pairing.ts`, "staged enforcement"), so a fresh box
seeded with `all` 401s the dashboard before anything can pair. Pair the first
device, then tighten:

```bash
sudo sed -i 's/^PUFF_PAIRING_ENFORCE=.*/PUFF_PAIRING_ENFORCE=all/' /etc/puffpai/dashboard.env
sudo systemctl restart puffpai-dashboard
```

## 7. The dashboard

The repository is a monorepo: the dashboard source is `dashboard/`, while
`puffpai-dashboard.service` runs from `/opt/puffpai-dashboard` with only
`.next/` writable. Copy it out of the checkout, then build:

```bash
sudo rsync -a --delete --exclude node_modules --exclude .next \
    /opt/puffpai/dashboard/ /opt/puffpai-dashboard/
sudo chown -R puffpai:puffpai /opt/puffpai-dashboard
cd /opt/puffpai-dashboard
sudo -u puffpai cp .env.production.example .env.production
sudo -u puffpai HOME=/data/puffpai/home npm ci
sudo -u puffpai HOME=/data/puffpai/home NEXT_TELEMETRY_DISABLED=1 npm run build
```

`UNVERIFIED ON A FRESH BOX`: `scripts/provision_orin.sh` creates
`/opt/puffpai-dashboard` but does not populate it; `scripts/install_orin.sh`
runs the copy and build above.

Notes that are verified:

- The build is a **deploy** step, not a boot step. `npm start` runs the
  `prestart` hook `scripts/ensure-fresh-build.js`, which refuses to serve a
  `.next` older than the source, a missed build is a clean unit failure rather
  than a stale UI (`systemd/README.md`, `dashboard/package.json`).
- `HOME` must be writable or npm dies creating `$HOME/.npm`. The unit sets
  `HOME` and `npm_config_cache` to `/data/puffpai/home`
  (`systemd/puffpai-dashboard.service:28-32`).
- Do not add `-- -p 3003` to `ExecStart`; the port is already in the package's
  `start` script.
- Turbopack will fail the **entire** build over an asset it cannot resolve from
  a `new URL(...)` reference, `dashboard/docs/AVATAR_SYSTEM.md:66-112` is the
  worked example (the audio worklet). If a build dies on an asset path, read
  that section before changing the code.

## 8. systemd units

Install and enable in this order (`systemd/README.md`, "Install"):

```bash
sudo install -D -m 0644 systemd/puffpai-api.service       /etc/systemd/system/puffpai-api.service
sudo install -D -m 0644 systemd/puffpai-dashboard.service /etc/systemd/system/puffpai-dashboard.service
sudo install -D -m 0644 systemd/puffpai-nodered.service   /etc/systemd/system/puffpai-nodered.service
sudo install -D -m 0644 systemd/puffpai-api.service.d/10-pulse-volume.conf \
                        /etc/systemd/system/puffpai-api.service.d/10-pulse-volume.conf

sudo systemctl daemon-reload
sudo systemctl enable --now ollama puffpai-api puffpai-dashboard puffpai-nodered
journalctl -u puffpai-api -f
```

`puffpai-zone3.service` is installed the same way but only after step 10.

Do **not** install `systemd/puffpai-api.service.d/20-ha-token-failclosed.conf`
until the HA token has been migrated into the Vault and verified, the drop-in
carries the full ordered procedure in its own comments, including the
`systemd-run` invocation that runs the migration as `puffpai` with the machine
credential.

`CAP_NET_ADMIN` is commented out in `puffpai-api.service:139-143`: the nftables
backend has never run for real. Enable both capability lines together, after
smoke-testing it.

## 9. Docker services

**Node-RED** (automation engine, Apache-2.0). The unit runs
`docker compose -f /opt/puffpai/docker-compose.nodered.yml up -d`. Flow secrets
come from `/opt/puffpai/.env`, which compose reads directly; it is git-ignored
and must be `0600` (`systemd/puffpai-nodered.service:32-34`). Import
`nodered_flows/flows.json` through the editor at `127.0.0.1:1880`, per
`nodered_flows/README.md`. Webhook paths are fixed (`/webhook/<name>`) and
listed in `config/puffpai_config.json`.

**SearXNG** (self-hosted metasearch behind the `web_search` switch):

```bash
sudo install -d -o puffpai -g puffpai /data/puffpai/searxng/config
sudo -u puffpai cp searxng/config/settings.yml.example /data/puffpai/searxng/config/settings.yml
openssl rand -hex 32                     # paste as secret_key, on the box
sudo docker compose -f /opt/puffpai/searxng/docker-compose.searxng.yml up -d
```

Published on `127.0.0.1:8888` only; nothing on the LAN can reach it
(`searxng/docker-compose.searxng.yml`). Never print or commit the real
`settings.yml`, it holds the secret key. `UNVERIFIED ON A FRESH BOX`, there is
no script for this; the commands are the compose file's own header instructions.

**Zone 3 / AdGuard Home** (optional, and inert until you act twice):

```bash
sudo install -D -m 0644 systemd/puffpai-zone3.service /etc/systemd/system/puffpai-zone3.service
sudo systemctl daemon-reload && sudo systemctl enable --now puffpai-zone3
# The VENV interpreter: the bootstrap imports bcrypt (requirements.txt) to
# hash the AdGuard admin password. System python3 does not have it.
sudo /opt/puffpai/venv/bin/python /opt/puffpai/scripts/zone3_bootstrap_adguard.py
```

`puffpai-zone3.service` runs as **root**, and that is the *less* privileged of
the two choices. `docker compose` needs the docker socket, and socket access is
root-equivalent either way (you can bind-mount `/` into a privileged
container). Granting it to the `puffpai` account instead, `User=puffpai
Group=docker`, would hand that root-equivalence to the account that also runs
the network-facing backend, permanently, turning a backend bug into a root
compromise. Keeping it root confines the privilege to a oneshot that runs two
fixed commands and exits. **Do not add `puffpai` to the `docker` group.**

The bootstrap completes the first-run wizard non-interactively, sets a
DoH upstream, turns the query log on and protection **off**; it is idempotent
(`docker-compose.zone3.yml` header). Nothing on the network changes until you
also point the router's DNS at this box. Then set `zone3.enabled` in
`config/puffpai_config.json`.

**Media** (optional): `docker-compose.media.yml` (Jellyfin and Calibre-Web;
its header lists the steps). It and `docker-compose.spotify.yml` **require**
`PUFFPAI_BIND_IP`, this box's LAN IPv4, and refuse to start rather than guess;
`install_orin.sh` detects it and writes it to `/opt/puffpai/.env`, printing the
value so you can confirm it. The media library location is
`PUFFPAI_MEDIA_DIR` (default `/data/puffpai/media`).

## 10. Voice

The voice service is a **user** unit, because it needs the PulseAudio session
(`systemd/user/puffpai-voice.service`):

```bash
mkdir -p ~/.config/systemd/user
cp /opt/puffpai/systemd/user/puffpai-voice.service ~/.config/systemd/user/
cp -r /opt/puffpai/systemd/user/puffpai-voice.service.d ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now puffpai-voice
sudo loginctl enable-linger <that user>     # so it survives logout
```

`UNVERIFIED ON A FRESH BOX`, there is no install script for the user unit.

Audio wiring that is verified:

- Mic: a ReSpeaker XVF3800 USB array. Pick it by name with
  `PUFF_CAPTURE_DEVICE` (default `pulse`); `arecord -l` / `aplay -l` come from
  `alsa-utils`, installed in step 3.
- Speaker: any PulseAudio sink, chosen with `PUFF_SINK`. **Pulse owns the
  hardware mixer**, setting levels with `amixer` is snapped back the next time
  Pulse touches volume (`systemd/puffpai-api.service.d/10-pulse-volume.conf`).
- The API runs with `ProtectHome=true`, which hides `/run/user` and therefore
  the Pulse socket. Either install the `10-pulse-volume.conf` drop-in
  (`BindPaths=-/run/user/1000/pulse`) or append `config/pulse/puffpai-socket.pa`
  to the session's `~/.config/pulse/default.pa`, that publishes a second socket
  at `/data/puffpai/state/pulse.sock`, which is inside the unit's
  `ReadWritePaths`. The file's own header explains why the bind-mount alone did
  not work.
- Ducking policy and its variables: `docs/VOICE_DUCKING.md` and the
  `10-duck.conf` drop-in.

## 11. Home Assistant

HA runs on its own host or container; nothing here installs it. Create a
long-lived access token in HA, then:

```bash
cp config/home_assistant_config.json.example config/home_assistant_config.json
# {"enabled": true, "url": "http://<ha-host>:8123", "token": "<long-lived token>"}
```

That plaintext token is the fallback path. The intended home for it is the
Vault: install the machine-bound service key with
`scripts/install_vault_service_key.sh`, run `scripts/migrate_ha_token.py`
(`--dry-run`, then for real, then `--scrub`), confirm
`GET /system/status` reports `"token_source": "vault-service"`, and only then
install the fail-closed drop-in. The whole ordered procedure, including the
`systemd-run` invocation, is in
`systemd/puffpai-api.service.d/20-ha-token-failclosed.conf`.

Zigbee devices are paired through HA's ZHA integration, driven by
`src/puffpai/zigbee_pairing.py`. The HA account PuffPAI uses day to day is
deliberately non-admin; provisioning actions that need more are gated per action
(`src/puffpai/ha_provisioning.py`).

## 12. Kiosk (wall display)

```bash
sudo ./scripts/provision_kiosk.sh          # --revert undoes the autologin
sudo systemctl restart gdm3                # or reboot
```

Auto-logs the service user into a minimal openbox + Chromium session on
`KIOSK_URL` (default `http://127.0.0.1:3003/dashboard`, set in
`/etc/puffpai/kiosk.env` from `kiosk/kiosk.env.example`), with a night-blanking
window. Append `?puffMode=kiosk` to the URL: it engages the dashboard's motion
governor so the page stays static. Read `docs/KIOSK_MODE.md` first: an animated
dashboard left open under full GNOME can pin the GPU at 99% and trip
over-current protection.

## 13. Verify

```bash
curl -sS http://127.0.0.1:8000/health      # /health is PUBLIC: 200 is the only pass
ollama list                                # the four tags from step 4
systemctl is-enabled puffpai-api puffpai-dashboard
/opt/puffpai/venv/bin/python -c "import chromadb; print('deps OK')"

# A KEYED call that actually touches storage. /health answers from memory and
# stays 200 on a box whose vault directory is read-only.
KEY=$(sudo sed -n 's/^PUFFPAI_API_KEY=//p' /etc/puffpai/api.env)
printf 'X-API-Key: %s' "$KEY" | curl -sS -H @- http://127.0.0.1:8000/vault/mfa-status
# expect: {"mfa_enabled":...,"vault_available":true,...}
```

`vault_available:false` means the backend cannot open its vault directory, check `PUFFPAI_DATA_DIR` in `systemd/puffpai-api.service` and that
`/data/puffpai` is writable by `puffpai`. A `401` means the key in `api.env` is
not the one the service resolved.

Then open `http://<box>:3003`, pair the first device, and ask it something.
Before you call it done, run the acceptance list printed at the end of
`scripts/provision_orin.sh` (the **"Next steps"** block, item **"Acceptance
check"**): a few questions you know the answers to, voice round trip, vault
decrypt, Home Assistant control, and the WAN-unplugged test.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `Vault unavailable: [Errno 30] Read-only file system`, once a minute | relative `paths.vault_data_dir`; make it absolute under `/data/puffpai` |
| `ollama pull` → `EACCES` | pulled before the second `provision_orin.sh` pass; re-run it |
| dashboard unit fails on start | `.next` older than the source, or missing, rebuild |
| npm fails creating `$HOME/.npm` | `HOME` not pointed at `/data/puffpai/home` |
| volume commands answer "I couldn't reach the speaker mixer" | Pulse socket not reachable under `ProtectHome=true`, step 10 |
| LAN scan returns nothing | `net-tools` missing; `device_sentinel` shells out to `arp -a` |
| answers slow, box swaps | under 32 GB RAM the chat and planner models will not co-reside |

Logs: `journalctl -u puffpai-api -n 100 --no-pager` and
`$PUFFPAI_DATA_DIR/logs/puffpai.log` (rotating, 5 MB x 3).
