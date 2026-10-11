# systemd units

System units for the appliance, plus the voice service's user unit under
`user/`.

## Appliance units (headless — Jetson AGX Orin)

| Unit | What it runs | Listens on |
|---|---|---|
| `puffpai-api.service` | `/opt/puffpai/venv/bin/python /opt/puffpai/start_api.py` — the **Flask** backend plus its background daemon threads | `0.0.0.0:8000` |
| `puffpai-dashboard.service` | `npm start` in `/opt/puffpai-dashboard` → `next start -p 3003` | `:3003` |
| `puffpai-nodered.service` | `docker compose -f docker-compose.nodered.yml up -d` — Node-RED, the automation engine | `127.0.0.1:1880` |
| `ollama.service.d/override.conf` | *Drop-in only.* The official Ollama installer ships its own unit; we only override the model directory and the bind address | `127.0.0.1:11434` |

There is no unit for the desktop GUI on the appliance, by design.

---

## Install

```bash
sudo install -D -m 0644 systemd/puffpai-api.service       /etc/systemd/system/puffpai-api.service
sudo install -D -m 0644 systemd/puffpai-dashboard.service /etc/systemd/system/puffpai-dashboard.service
sudo install -D -m 0644 systemd/puffpai-nodered.service   /etc/systemd/system/puffpai-nodered.service
sudo install -D -m 0644 systemd/ollama.service.d/override.conf \
                        /etc/systemd/system/ollama.service.d/override.conf

sudo systemctl daemon-reload
sudo systemctl enable --now ollama puffpai-api puffpai-dashboard puffpai-nodered
journalctl -u puffpai-api -f
```

The units are literal, with no placeholder substitution. They assume the layout `scripts/provision_orin.sh`
creates:

```
/opt/puffpai/              repo checkout + venv/    (dir created empty; the
│                                                    deploy step fills it)
├── src/security_logs/     SECURITY AUDIT TRAIL (yes, under src/ — see below)
├── security_logs/         dashboard tile/widget review logs
├── data/  app_data/       other repo-relative runtime writes
/opt/puffpai-dashboard/    dashboard checkout + .next/
/data/puffpai/             all writable state, on the NVMe
├── models/                GGUFs, Vosk, Piper
│   └── ollama/            OLLAMA_MODELS — owned ollama:puffpai 2770, NOT
│                          puffpai: the installer runs ollama as its own user
├── chromadb/              vector store
├── vault/                 Zone 1 — 0700
├── home/                  HOME + npm cache for the dashboard unit
├── knowledge/  network/  config/  logs/  chat_history/  app_data/
/etc/puffpai/              api.env, dashboard.env — 0640 root:puffpai
```

**Run the script three times.** It is idempotent by design, and two steps
depend on things that do not exist on the first pass:

1. first run — packages, Docker, user, `/data/puffpai`, `/opt` dirs
2. after `ollama` is installed, **before `ollama pull`** — hands
   `models/ollama` to the `ollama` user; pulling first fails with `EACCES`
3. after the code is deployed — creates the `security_logs` dirs, `data/`,
   `app_data/`, seeds the untracked JSON state files, and fixes ownership

`/data/puffpai/**` is the Orin path.

## Things that will bite you

- **The security audit log has two failure modes, and the sandbox triggers
  both.** `src/puffpai/security/audit_log.py` writes to
  **`<repo>/src/security_logs/audit.jsonl`** — note `src/`, not the repo
  root: it resolves `parent.parent.parent` from
  `src/puffpai/security/audit_log.py`, which lands in `src/`. The directory is
  untracked, so a fresh checkout does not have it. Then:

  1. `AuditLog.__init__` calls `mkdir(parents=True, exist_ok=True)` with **no
     exception handling**. On a read-only `/opt` that raises at construction —
     and `Vault.__init__` builds an `AuditLog`, so the vault fails to
     construct at all. This is a hard startup failure, not a degradation.
  2. `AuditLog.log()` catches `IOError`, prints the entry to stderr and
     *returns normally* — the caller is told the write succeeded while the
     entry is gone with only a journal line behind it. **Fails open.**

  `puffpai-api.service` lists `-/opt/puffpai/src/security_logs` in
  `ReadWritePaths` and the third `provision_orin.sh` run creates it owned by
  `puffpai`. **If you change the deploy path, the code path, or the sandbox,
  verify an audit write actually lands** — an audit trail that quietly stops
  recording is worse than one that was never there.

  The *location* is config-driven: `paths.security_logs_dir` (absolute)
  wins, then `$PUFFPAI_DATA_DIR/security_logs`, then `<repo>/security_logs`.
  To keep an existing trail from being orphaned, `<repo>/src/security_logs`
  is still used automatically while it exists and the configured
  location does not — so setting the key is **not** enough on its own:
  `systemctl stop puffpai-api`, move `audit.jsonl` (and any `audit.N.jsonl`)
  to the new directory, then start. Failure mode (2) — `log()` fails open —
  is unchanged.

- **Seven modules write repo-relative, not under `/data`** — and they do not
  agree on where the repo root is. `audit_log.py` lands in `src/`
  (above); `dashboard_registry.py` and `dashboard_widgets.py` land at the
  repo root. So there are **two** `security_logs` directories at different
  depths, and the unit needs both — the second one is not a typo. The rest:
  `app_data.py`, `emergency_prep.py`, `firewall_manager.py`, and
  `api_server.py` (`src/puffpai/indexed_libraries.json`,
  `data/library_sources/`). `ReadWritePaths` punches each through the
  read-only `/opt`, all `-` prefixed because none of these directories are
  tracked in git and an unprefixed missing path makes systemd fail namespace
  setup outright. Two of them (`dashboard_registry`, `dashboard_widgets`)
  write via atomic `os.replace`, so the *directory* must be writable, not just
  the file.

- **Two JSON state files are untracked and must be seeded.**
  `blocked_devices.json` and `src/puffpai/indexed_libraries.json` are not in
  git, so a file-level `ReadWritePaths` entry would match nothing and the
  first write would hit `EROFS` — device blocking and library-manifest
  updates silently broken. The third run seeds both with `[]`, which both readers
  already tolerate. `emergency_inventory.json` *is* tracked and is
  deliberately **not** seeded: `emergency_prep.py` `json.load()`s it whenever
  it exists, with no error handling, so an empty placeholder would be worse
  than no file.

- **`paths.*` must be absolute.** `puffpai_config.json` ships relative
  defaults (`./kiwix_rag_db`, `chat_history`, `vault_data`). Under
  `ProtectSystem=strict` with `ReadWritePaths=/data/puffpai`, those resolve
  into a read-only `/opt/puffpai` and the backend will fail to write. Point
  every `paths.*` entry (and `chromadb.path`) at `/data/puffpai/...` before
  first start. The writable-state keys are `state_dir`, `agent_events_dir`,
  `security_logs_dir` and `vault_data_dir`; all four go through
  `puffpai.config.resolve_state_dir()`, whose precedence is **absolute config
  value → `$PUFFPAI_DATA_DIR` → relative config value (against the repo root)
  → the shipped default**. A relative value is dev convenience and deliberately
  ranks *below* the env var, because relative always means "inside the
  read-only code tree" here.

- **The vault's fallback location is not writable on the appliance.** Without
  a configured location the vault resolves `<repo>/src/vault_data` (three
  parents up from `src/puffpai/security/` is `src/`, not the repo root), which
  no `ReadWritePaths` entry covers, so `Vault()` raises on `mkdir` and the
  backend logs `Vault unavailable: [Errno 30] Read-only file system` once a
  minute. Set `paths.vault_data_dir` to an absolute `/data/puffpai/...`
  path. The vault has no schema to migrate: an empty directory is a valid
  uninitialised vault, and the first `initialize(pin)` writes
  `.vault_key_salt`. Anything that was supposed to be *in* it (the HA token,
  say) still has to be put there after unlocking.

- **`$PUFFPAI_DATA_DIR` covers the modules with no `paths.*` key of their
  own** — `majordomo_tasks.jsonl`, `daily_briefing.json`,
  `caretaker_restarts.json`, the toolsmith `workspace/`,
  `sentinel_baseline.json`, `sentinel_alerts.jsonl`, `device_consent.json`,
  `sentinel_reviews.jsonl`, `consent_requests.json`,
  `zone3_dns_rules.jsonl`, `agent_events.jsonl`. The unit sets
  `PUFFPAI_DATA_DIR=/data/puffpai/state`.

  Without the variable, every one of those falls back to a built-in default of
  `/opt/puffpai/data`, which *is* writable (there is a `ReadWritePaths`
  exception for it). That is a safety net, not a reason to drop the env var:
  without it, state lives inside the deploy directory and a redeploy can
  overwrite it.

  **A box that has state at an older default location:** modules whose default
  moved adopt a file left behind at the old location — they read and write it
  *in place*, and log a warning naming both paths, so no ledger is orphaned.
  Adoption is suppressed whenever `$PUFFPAI_DATA_DIR` (or an absolute
  `paths.*` key) is set, because `resolve_state_dir()` never second-guesses an
  explicit choice. So on the appliance adoption never fires — which is fine,
  as those old paths are not writable there, but a DEV box that has been
  running with the env var unset really does have files at the old default,
  and setting the env var for the first time will start it from empty state
  unless you copy them across once.

- **`HOME` is read-only for the dashboard unless you set it.** systemd would
  hand the unit `HOME=/opt/puffpai`; npm then dies creating `$HOME/.npm`. The
  unit sets `HOME`/`npm_config_cache` to `/data/puffpai/home`.

- **Ollama's model dir is not owned by `puffpai`.** `ollama.service` runs as
  the `ollama` user, so `OLLAMA_MODELS=/data/puffpai/models/ollama` is
  `ollama:puffpai 2770`. Owning it `puffpai:puffpai` makes `ollama pull` fail
  with `EACCES`.
- **The dashboard is built at deploy, not at boot.** Run `npm ci && npm run
  build` in `/opt/puffpai-dashboard` as part of deployment. `npm start` fires
  the `prestart` hook (`scripts/ensure-fresh-build.js`), which refuses to
  serve a `.next` that is older than the source — so a missed build shows up
  as a clean unit failure rather than a stale UI. Do not add `-- -p 3003` to
  `ExecStart`; the port is already in the package's `start` script.
- **`start_api.py` hardcodes `0.0.0.0:8000`.** The `api_server.host`/`port`
  keys in `puffpai_config.json` are not read on this path. Restrict exposure
  with nftables (LAN-only 8000/3003/1880), not with config.
- **nftables enforcement needs `CAP_NET_ADMIN`**, which is commented out in
  `puffpai-api.service`. `NftablesFirewall` is untested on the appliance;
  smoke-test it first, then enable both capability lines together.
- **Loopback-only Ollama vs. the Node-RED container.** The container reaches
  the host over the Docker bridge, not `127.0.0.1`. See the caveat in
  `ollama.service.d/override.conf`.
