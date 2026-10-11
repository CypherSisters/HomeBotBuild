# Hardware

HomeBot runs on any 64-bit Windows 10/11 or Linux machine with Python 3.10+
and [Ollama](https://ollama.com/download). No special board is needed. The
setup wizard takes a census of the machine (CPU, RAM, disk, GPU, OS) and offers
only the models its RAM can hold. This page gives the numbers, the machines we
have actually run it on, and what we have not tried.

## Minimum hardware

| | Minimum | Notes |
|---|---|---|
| **OS** | 64-bit Windows 10/11, or 64-bit Linux | macOS: `install.sh` runs, and says HomeBot is tested on Linux and Windows only. |
| **RAM** | **2 GB total** | The smallest model's floor. Below it the wizard offers no model: it prints "This machine is below the floor of our smallest local model." and carries on with no brain (`src/puffpai/model_catalog.py:322-327`, `src/puffpai/setup_cli.py:423-444`). The ladder below says what each step up buys. |
| **CPU** | 64-bit | We have run this repo's installers on x86-64 only. |
| **GPU** | optional | The model menu is fitted to total RAM alone. The census reads the GPU and its VRAM and prints them, but they do not change the menu. If you know your GPU can carry a bigger model, `puffpai setup --model <tag>` overrides the menu. |
| **Disk** | the model, plus the install | Models are 1 to 19 GB (below). We have not measured a clean install's own footprint. Knowledge packs come on top and can be large. |
| **Software** | Python 3.10+, git, Ollama | Node 20.19+ only if you build the dashboard. |
| **Network** | the internet, once, to install | See [First install needs the internet](#first-install-needs-the-internet). |

The 2 GB floor is the smallest model's, not the whole system's. The OS, the
backend and the dashboard need memory of their own, and nobody has run HomeBot
on a machine anywhere near that small. What happens below the floor is tested
with a simulated 1 GB census (`tests/test_wizard_buildbot.py`), not on real
hardware.

### What each amount of RAM gets you

The ladder the wizard fits against (`config/model_catalog.json`). The first
RAM figure is the floor to run the model at all; the one in brackets is where
it is comfortable, with room for the knowledge index. What it can do comes from
each model's profile (`config/model_profiles.json`).

| Total RAM | Model | Download | What it can do |
|---|---|---|---|
| 2 GB (3) | `llama3.2:1b` | 1 GB | Set phrasings only. |
| 3 GB (4) | `qwen2.5:1.5b-instruct` | 1 GB | Set phrasings only. |
| 5 GB (8) | `qwen2.5:3b-instruct` or `llama3.2:3b` | 2 GB | One thing per message, no planning ahead. |
| 8 GB (16) | `qwen2.5:7b-instruct` | 5 GB | One thing per message; a chain of tasks in one breath loses a step. |
| 24 GB (32) | `qwen3:30b-a3b` | 19 GB | Multi-step requests, and it picks its own tools. The Founders Edition brain. |

Only one board has been benchmarked: the Jetson AGX Orin 64 GB, for the 30B
and the 7B (`config/model_catalog.json`). Every other model carries no numbers,
and no number is extrapolated to other machines. No wizard screen shows a
benchmark yet: the dashboard's setup marks every model as untested on your
hardware.

## First install needs the internet

Installing needs the internet once: to clone the repo, for the Python packages
the installer fetches, for Ollama itself, and to pull a model (1 to 19 GB).
Neither `install.ps1` nor `install.sh` installs Ollama or downloads a model
unless you ask (`-YesInstallOllama` / `--yes-install-ollama`, `-Pull TAG` /
`--pull TAG`). Inside the wizard, a model download goes through the
`model_pull` switch, which ships shut like every other. With it shut, the
wizard says so and does not download; open it with `egress_doors.model_pull`
in the config or `PUFFPAI_EGRESS_MODEL_PULL=1` (the dashboard has no switch
for it), or run `ollama pull <model>` yourself. With it open, the wizard
asks before it downloads. Building the dashboard (`npm ci`) needs the internet
too.

After the install, every way out has a name and a switch, and the Bridge ships
closed: [`WHAT_CAN_LEAVE_YOUR_HOUSE.md`](WHAT_CAN_LEAVE_YOUR_HOUSE.md).

## What we have run it on

| Machine | What was run | What that does not cover |
|---|---|---|
| One Windows 11 laptop: x86-64, 64 GB of RAM, NVIDIA laptop GPU | A clean install in a clean folder (`install.ps1`, then the wizard), and the full test suite (results in the [README](../README.md)) | Voice. Any machine with less memory. |
| A `python:3.12-slim` container: Debian, x86-64, CPU only | `install.sh`, then the wizard, start to finish from a clean copy ([`INSTALL_LINUX.md`](INSTALL_LINUX.md)) | No display, audio, GPU or Ollama in the image, so no voice, kiosk or real model. |
| Jetson AGX Orin 64 GB | The Founders Edition build this repo was cut from runs there, and it is the only board with benchmark numbers | This repo's own installers. `scripts/install_orin.sh` has not been run on a fresh Orin ([`INSTALL_ORIN.md`](INSTALL_ORIN.md)). |

## Not tested yet

- A second real machine of any kind.
- Windows 10. The one Windows machine we have run is Windows 11.
- Linux on real hardware, with a desktop, audio or a GPU. The Linux run above
  was a headless container.
- Anything near the 2 GB floor.
- A real model on CPU alone. The Windows laptop has a GPU, and the container
  had no Ollama.
- Voice on Windows, and voice on any Linux machine but the Orin.
- arm64 beyond the Orin (Raspberry Pi and the like), Windows on ARM, and macOS.

If you run HomeBot on any of these, a report is welcome: open an issue with the
census the wizard printed.

## Optional hardware

None of these is required.

| Component | For | What we ran | Notes |
|---|---|---|---|
| **Microphone** | voice | ReSpeaker XVF3800, on the Orin | Install the `voice` extra (plus `libportaudio2` on Linux). `PUFF_CAPTURE_DEVICE` picks the capture device (`src/puffpai/voice/service.py:103`). On Windows it installs with `-WithVoice`; voice on Windows is untested. |
| **Speaker** | spoken replies | the XVF3800's own output, on the Orin | Linux: any PulseAudio sink, chosen with `PUFF_SINK` / `PUFFPAI_PULSE_SINK`. Pulse owns the mixer, so volume goes through Pulse and never `amixer` (`systemd/puffpai-api.service.d/10-pulse-volume.conf`). Windows: Piper has no Windows wheel, so spoken replies fall back to the browser voice in the dashboard. |
| **Home Assistant** | smart-home control | HAOS on a separate machine on the LAN | Any reachable HA with a long-lived token (`config/home_assistant_config.json.example`). HomeBot never installs HA. |
| **Zigbee coordinator** | pairing Zigbee devices | through Home Assistant's ZHA | HomeBot pairs through HA, not through a radio of its own (`src/puffpai/zigbee_pairing.py`). |
| **Display** | the kiosk / wall display | one HDMI monitor on the Orin | Any browser and screen. Read [`KIOSK_MODE.md`](KIOSK_MODE.md) first: the animated dashboard left open in a desktop browser on the Orin can pin its GPU at 99% and trip the board's over-current protection. |
| **GPU** | faster answers | NVIDIA on the Windows laptop; the Orin's built-in GPU | Ollama uses a supported GPU when there is one. The wizard's menu ignores it. |
| **Zone 3 network edge** | a DNS resolver for the house that records what was asked, and blocks only once you turn protection on | on the Orin, monitor-only | Linux only: Docker Engine with the compose plugin, port 53 free, a LAN IP, and your router's DNS setting. Ships inert ([`ZONE3_SETUP.md`](ZONE3_SETUP.md)). |
| **UPS** | riding out a power cut | none | Not tested, not integrated, no code path. Once your router points the house at Zone 3 for DNS, a power cut there takes the whole household's name resolution with it (`systemd/puffpai-zone3.service`), which is the argument for one. |

## The Jetson AGX Orin appliance

The Founders Edition appliance is kept here as an appendix,
[`INSTALL_ORIN.md`](INSTALL_ORIN.md); the complete appliance is
[`CypherSisters/HomeBotFounders`](https://github.com/CypherSisters/HomeBotFounders).
Its scripts install the full Founders Edition model plan rather than the wizard's one
fitted brain, so they want far more machine.

- **Board:** Jetson AGX Orin 64 GB developer kit on JetPack 6.2.2 (Ubuntu
  22.04, L4T r36.5) (`scripts/provision_orin.sh:5`). `scripts/install_orin.sh`
  refuses anything that is not `aarch64` with `/etc/nv_tegra_release`.
- **Models:** four pinned, about 39 GB of weights: `gpt-oss:20b` 13.8 GB,
  `qwen3:30b-a3b` 18.6 GB, `qwen2.5vl:7b` 6.0 GB and `nomic-embed-text` 0.3 GB
  (`scripts/provision_orin.sh:428-431`). The plan assumes 64 GB of RAM.
  `scripts/install_ubuntu.sh`, the API-only path for the same plan, refuses
  below 16 GB and warns below 32 GB, where the two big models no longer fit in
  memory together (`scripts/install_ubuntu.sh:61-66`).
- **Storage:** an NVMe mounted at `/data`. The provisioner refuses below 80 GB
  free (`PUFFPAI_MIN_FREE_GB`, `scripts/provision_orin.sh:36`) and warns when
  there is no NVMe, because the dev kit's 64 GB eMMC cannot hold the models,
  ChromaDB and the ZIM packs. It builds `/data/puffpai/` (models, knowledge,
  network, config, logs, chat_history, app_data, chromadb, home) plus `vault/`
  at mode `0700`. Partition, format and mount the NVMe by hand, first; no
  script does it.
- **Power:** the appliance plan assumes the board capped at `MODE_50W`; the
  planner's context size is sized for that mode (`src/puffpai/inference/model_options.py`).
  Nothing in the repo sets the mode; set it yourself with `nvpmodel -m` before
  benchmarking.
- **Status:** the appliance scripts have not been run on a fresh Orin.
  [`INSTALL_ORIN.md`](INSTALL_ORIN.md) marks every step it could not script
  `UNVERIFIED ON A FRESH BOX`.
