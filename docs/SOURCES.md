# Sources

Where each fact in `README.md`, the install docs, `HARDWARE.md`, `CONFIG.md`,
`ZONE3_SETUP.md`, `CONTRIBUTING.md` and `SECURITY.md` comes from.
Claim → file:line.

| Claim | Source |
|---|---|
| Wizard has two surfaces: `puffpai setup` and the dashboard's `/onboarding` | `dashboard/app/onboarding/page.tsx`, `dashboard/app/_components/OnboardingGate.tsx:6,40` |
| Seven agents in code: concierge, steward, sentinel (displayed Drawbridge), archivist, majordomo, caretaker, toolsmith | `src/puffpai/agents/registry.py:81-105` |
| `PUFFPAI_CONFIG_DIR`, default `<repo>/config` | `src/puffpai/config.py:52,582` |
| `PUFFPAI_API_BIND` defaults to loopback (three named off-box cases); `PUFFPAI_API_PORT` sits on top of `api_server.port`, bad value warned and ignored | `start_api.py:131-192` |
| `PUFFPAI_SETUP_STDIN_SECRETS` | `src/puffpai/setup_cli.py:190-195` |
| `PUFFPAI_TRUSTED_LOCAL_PEERS` (start_api.py's comment names it wrongly) | `src/puffpai/security/endpoint_gate.py:82,101-132` |
| Storage step's four dirs and their config keys | `src/puffpai/setup_ops.py:1110-1132` |
| `grocery_store_name` / `grocery_search_url`, both default `""` | `src/puffpai/app_data.py:59,70-71` |
| `PUFFPAI_FIREWALL_BACKEND`; nftables is opt-in, no packet filter ships | `src/puffpai/firewall_manager.py:39,831-838,851` |
| Zone 3 ships AdGuard Home, not Pi-hole; Pi-hole retired, controller kept for reference | `docker-compose.zone3.yml:1-56`, `src/puffpai/zone3/adguard_controller.py:1-13` |
| Monitor-first: `enforce=False` writes `! OBSERVE` comment lines and refuses AdGuard's own protection switch; resolution is env (`PUFFPAI_ZONE3_ENFORCE`) > overlay > config > off | `zone3/adguard_controller.py:15-19,302-316,415-423`, `zone3/engine.py:33,62,185-207` |
| Zone 3 config keys `adguard_url/username/password/enforce`, `enabled` ships False | `src/puffpai/config.py:355-387`; `ADGUARD_PASSWORD` at `:763` |
| :53 and :3000 published on the LAN address only, never `0.0.0.0`; healthcheck is `/login.html` | `docker-compose.zone3.yml:29-46,63-70,120-131` |
| Zone 3 apply commands and the "bring your own edge" option set | `src/puffpai/zone_bridge.py:205-234` |
| Zone 3 API: `/zone3/{status,discover,secure,setup,confirm}`, ticket-gated writes | `src/puffpai/api_server.py:20164-20171,20195,20214,20262,20429,20885` |
| Vault: AES-256-GCM; PIN → data key PBKDF2-HMAC-SHA256 at 480k iterations; the service-secret KEK wrap uses scrypt (N=2^14) with the KDF recorded in the wrap; "LUKS + TPM2.0" is a stated target, not configured | `src/puffpai/security/vault.py:1-11,237-239,319,582-594,644-680` |
| Python 3.10/3.12; Windows and Linux classifiers; `[dev]` = pytest/black/flake8/mypy, no mypy config file | `setup.py:40-53` |
| Suite size: every `tests/test_*.py` file that `pytest.ini` collects; a full single-process run takes about an hour and a half on one Windows 11 PC | `git ls-files tests` matching `test_*.py` (`pytest.ini`); a full `pytest tests` run |
| Dashboard engines Node ^20.19 \|\| >=22.12; `build`/`start` scripts; typescript devDep | `dashboard/package.json:5-12,60` |
| App fonts are vendored woff2 files under the SIL OFL, declared in `fonts.css`; no app page loads a webfont from a third party | `git ls-files dashboard/public/vendor/fonts` + `dashboard/public/vendor/fonts/fonts.css` |
| Ubuntu 22.04 / JetPack 6.2.2 / L4T r36.5; aarch64 | `scripts/provision_orin.sh:5,88-98,147` |
| 80 GB free minimum; ~39 GB of models | `scripts/provision_orin.sh:36`, `:99-106` |
| 64 GB RAM is the model plan | `scripts/provision_orin.sh:152` |
| `/opt` + `/data/puffpai` layout, vault `0700` | `scripts/provision_orin.sh:285-300,355-397` |
| `/etc/puffpai/{api,dashboard}.env` `0640 root:puffpai` | `scripts/provision_orin.sh:399-408` |
| Model tags and sizes; `gpt-oss:20b` chat, `qwen2.5vl:7b` vision | `scripts/provision_orin.sh:428-431`, `config/puffpai_config.json` |
| `qwen3:30b-a3b` classify + plan | `src/puffpai/agents/engine.py:94-95` |
| `nomic-embed-text` embeddings | `src/puffpai/config.py:274` |
| Coding model off by default, `qwen3-coder:30b` | `src/puffpai/config.py` (`code_model: str = ""`), `src/puffpai/inference/code_model.py:4` |
| `OLLAMA_MODELS`, `OLLAMA_HOST=0.0.0.0:11434` | `systemd/ollama.service.d/override.conf:16,29` |
| Backend on `0.0.0.0:8000`, hardcoded | `systemd/puffpai-api.service:19-23` |
| `PUFFPAI_DATA_DIR=/data/puffpai/state` | `systemd/puffpai-api.service:51` |
| Relative `paths.*` break under read-only `/opt` | `systemd/puffpai-api.service:94-98`, `systemd/README.md` |
| Audit log fails open; two `security_logs` dirs | `systemd/README.md`, `systemd/puffpai-api.service:100-137` |
| `CAP_NET_ADMIN` off, nftables never run | `systemd/puffpai-api.service:139-143` |
| Dashboard on `:3003`, build is a deploy step | `systemd/puffpai-dashboard.service:19-24`, `dashboard/package.json` |
| `HOME=/data/puffpai/home` for npm | `systemd/puffpai-dashboard.service:28-32` |
| Turbopack fails the whole build on an asset it cannot resolve | `dashboard/docs/AVATAR_SYSTEM.md:66-112` |
| Node-RED container, `127.0.0.1:1880`, `/opt/puffpai/.env` | `systemd/puffpai-nodered.service:32-36`, `nodered_flows/README.md` |
| SearXNG loopback `127.0.0.1:8888`, `settings.yml.example`, `openssl rand -hex 32` | `searxng/docker-compose.searxng.yml` header |
| Zone 3 ships inert twice; bootstrap script | `docker-compose.zone3.yml` header, `scripts/zone3_bootstrap_adguard.py` |
| Seven agents; Drawbridge is the Bridge agent | `src/puffpai/agents/registry.py:82-104`, `src/puffpai/agents/preferences.py:51-62` |
| Two-stage device classifier | `src/puffpai/security/device_classifier.py:1-7`, `security/device_ml.py:1-14` |
| Bridge switches default shut; env outranks overlay; doc generated | `src/puffpai/security/egress_doors.py:252-436`, `scripts/gen_egress_doc.py` |
| Voice user unit; ReSpeaker XVF3800 is sink and mic | `systemd/user/puffpai-voice.service:16`, `.service.d/10-duck.conf:29` |
| Pulse owns the mixer; `ProtectHome` hides the socket | `systemd/puffpai-api.service.d/10-pulse-volume.conf`, `config/pulse/puffpai-socket.pa` |
| HA config keys `{enabled, url, token}` | `config/home_assistant_config.json.example` |
| HA token migration order, fail-closed drop-in | `systemd/puffpai-api.service.d/20-ha-token-failclosed.conf` |
| `nvpmodel` preflight; MODE_50W pinning | `scripts/install_ubuntu.sh:216-219`, `src/puffpai/inference/model_options.py:171` |
| Kiosk URL, night blanking, `?puffMode=kiosk`; pairing gate | `kiosk/kiosk.env.example`, `docs/KIOSK_MODE.md:7`, `dashboard/app/_lib/server/pairing.ts` |
| `PUFFPAI_API_KEY` resolution order; Python 3.10+ and extras | `src/puffpai/api_server.py:616-675`, `setup.py:62,70` |
| Parity gate before cutover | `scripts/provision_orin.sh:453` |
| 2 GB RAM floor: below it the wizard offers no model, says so, and carries on with no brain | `config/model_catalog.json` (`llama3.2:1b`, `min_ram_gb: 2`), `src/puffpai/model_catalog.py:322-327`, `src/puffpai/setup_cli.py:423-444` |
| The model menu is fitted to total RAM only; the census reads the GPU and VRAM and prints them | `src/puffpai/model_catalog.py:119-128`, `src/puffpai/hardware_census.py:246-300,529-531` |
| RAM ladder (floor and comfortable), download sizes; one benched board, the Orin 64 GB, for the 30B and the 7B | `config/model_catalog.json` |
| What each model can do: set phrasings, one thing per message, multi-step | `config/model_profiles.json` (`routing_tier`, `limits_note`) |
| Below-the-floor behaviour is tested with a simulated 1 GB census | `tests/test_wizard_buildbot.py:153-157` |
| `puffpai setup --model` overrides the brain menu | `src/puffpai/setup_cli.py:1287-1288` |
| Installers never install Ollama or download a model unless asked | `install.sh:6,69-70`, `install.ps1:6,24-25` |
| macOS: `install.sh` runs and says HomeBot is tested on Linux and Windows | `install.sh:93-94` |
| Model download is the `model_pull` switch, shut by default: shut does not download, open asks first; its switch is `egress_doors.model_pull` / `PUFFPAI_EGRESS_MODEL_PULL` | `src/puffpai/security/egress_doors.py:429-446`, `src/puffpai/setup_cli.py:1028-1056`, `docs/WHAT_CAN_LEAVE_YOUR_HOUSE.md:189` |
| No wizard screen shows a benchmark; the dashboard's setup marks every model untested | `src/puffpai/setup_cli.py:430-444`, `dashboard/app/onboarding/OnboardingWizard.tsx:1034-1056` |
| `install_ubuntu.sh` refuses below 16 GB of RAM and warns below 32 GB | `scripts/install_ubuntu.sh:61-66,232-244` |
| The verified Linux install is a `python:3.12-slim` container, CPU only | `docs/INSTALL_LINUX.md:6-13` |
| The full-suite pass, fail and skip counts in `README.md` | a full `pytest tests` run split across 16 processes on one Windows 11 PC (the run log is not in this repo) |
| CI runs a named subset and the dashboard build, not the full suite | `.github/workflows/ci.yml:3-7,45-54,56-78` |
| `PUFF_CAPTURE_DEVICE`, `PUFF_SINK`, `PUFFPAI_PULSE_SINK` | `src/puffpai/voice/service.py:103,108`, `src/puffpai/audio_volume.py:43` |
| Animated dashboard pinned the Orin's GPU at 99% and tripped over-current protection | `docs/KIOSK_MODE.md:15-19` |
| A Zone 3 power cut takes the house's name resolution with it | `systemd/puffpai-zone3.service:6-10` |
