# Install HomeBot on Linux

Any 64-bit Linux with Python 3.10+. For the Jetson AGX Orin appliance use
[`INSTALL_ORIN.md`](INSTALL_ORIN.md) instead.

**What "verified" means here.** `install.sh` and then the `puffpai setup` wizard
have been run start to finish in a `python:3.12-slim` container (Debian trixie,
x86-64, CPU only, no compiler) from a clean copy of this repo: the pip install
resolves, the editable install imports, the wizard completes and writes a
manifest. That is a headless container, which is the honest limit of the claim, it is **not** a desktop test. No display server, no audio device, no GPU, no
Ollama in the image, and therefore nothing about voice, the kiosk or a real
model's behaviour is covered by it.

## The one command

```bash
git clone https://github.com/CypherSisters/HomeBotBuild.git
cd HomeBotBuild
bash install.sh
```

It checks Python (3.10+, the floor in `setup.py`), offers the one `apt-get`
line that needs sudo (and only offers, `--no-apt` prints it instead), creates
`venv/`, installs `requirements.txt` and then this checkout in editable mode,
mints a per-install API key into your state directory, and looks for Ollama. It
never installs Ollama and never downloads a model unless you ask.

| Flag | Effect |
|---|---|
| `--dry-run` | print every command, change nothing |
| `--with-dashboard` | `npm ci && npm run build` in `dashboard/` (Node 20.19+) |
| `--with-voice` | also install the `[voice]` extra (and add `libportaudio2` to the apt line) |
| `--with-dev` | also install the `[dev]` extra (pytest, mypy, flake8) |
| `--with-services` | **generate** systemd user units; does not enable them |
| `--enable` | with `--with-services`, also enable and start the API unit |
| `--with-zone3` | check Zone 3's requirements and wire `PUFFPAI_BIND_IP` |
| `--no-apt` | never offer the apt line; print it |
| `--yes-install-ollama` | allow the official Ollama install script to run |
| `--pull TAG` | allow one `ollama pull TAG` at the end |

## Next: the wizard

Two surfaces, one wizard. Either is a complete setup; pick one.

In the terminal:

```bash
source venv/bin/activate
puffpai setup
```

Or in the browser: start the API and the dashboard (below) and open
<http://localhost:3003>. Until setup is marked complete the dashboard redirects
you to `/onboarding`, which asks the same questions.

### What the two surfaces need to know about each other

- The backend loads `<state>/homebot.env` at start, so a key you minted with the
  installer is already in place and you do not export anything by hand.
- **A key you set in the environment wins** over one stored in the OS keychain.
  That order is deliberate: an operator with a shell can always override what the
  machine remembers.
- On first run the console prints a **one-time setup code**. The browser wizard
  always asks for it, even on the same machine: the dashboard cannot prove where
  a request came from, so it does not assume. `puffpai setup` in a terminal on
  the machine needs no code, and `puffpai setup-code` prints it again.
- The dashboard listens on **127.0.0.1 by default**, so other devices in the
  house cannot reach it. Use `npm run start:lan` when you want them to.

## Distro packages

Only the venv and the compiler-needing extras want system packages. The base
install needs none.

```bash
# Debian / Ubuntu
sudo apt-get install -y python3-venv python3-dev build-essential
sudo apt-get install -y libportaudio2            # only with --with-voice
sudo apt-get install -y tesseract-ocr poppler-utils   # only with the [ocr] extra

# Fedora / RHEL:  sudo dnf install -y python3-devel gcc gcc-c++ make
# Arch:           sudo pacman -S --needed base-devel python
# openSUSE:       sudo zypper install -y python3-devel gcc gcc-c++ make
```

## State directory and the API key

Everything HomeBot writes goes to `$PUFFPAI_DATA_DIR` when set, otherwise
`$XDG_DATA_HOME/puffpai` (i.e. `~/.local/share/puffpai`). Nothing secret is
written into the checkout.

The installer creates `homebot.env` there, mode 0600, on first run and never
touches it again:

```
PUFFPAI_DATA_DIR=/home/you/.local/share/puffpai
PUFFPAI_API_KEY=<64 hex characters>
```

That key authorises **every** request that reaches the backend. It is not
printed; read it when a client needs it:

```bash
grep PUFFPAI_API_KEY ~/.local/share/puffpai/homebot.env
```

## Running it

```bash
set -a; . ~/.local/share/puffpai/homebot.env; set +a
python start_api.py
```

The backend refuses to start with no resolvable API key, deliberate, and the
error names where to fix it.

`PUFFPAI_API_BIND` defaults to `127.0.0.1`, and `install.sh --with-services`
writes that into the generated unit. The dashboard, the kiosk and voice all
reach the backend over loopback, so the default install needs nothing wider.
Widen it only for a client that is genuinely off this machine, and name an
interface rather than `0.0.0.0`:

```bash
# off-box Home Assistant: this box's LAN address, plus HA's address as a peer
PUFFPAI_API_BIND=192.0.2.10 PUFFPAI_TRUSTED_LOCAL_PEERS=192.0.2.20 python start_api.py

# Node-RED in a container: the Docker bridge, so the port is offered to
# containers and not to the LAN
PUFFPAI_API_BIND=172.17.0.1 python start_api.py
```

Any non-loopback bind logs one SECURITY line every boot naming the exposure.
There is no host packet filter in this build: the API key is the only control
in front of that port.

Port 8000 is the default; `PUFFPAI_API_PORT` moves it.

Dashboard, once built, in a second shell:

```bash
cd dashboard
npm run start        # http://localhost:3003
```

## systemd user units

`--with-services` writes two units under `~/.config/systemd/user`, with the
real paths of this checkout and this state directory baked in:

```
~/.config/systemd/user/homebot-api.service
~/.config/systemd/user/homebot-dashboard.service
```

They are **generated, not enabled**. Turn them on when you are ready:

```bash
systemctl --user daemon-reload
systemctl --user enable --now homebot-api.service
systemctl --user enable --now homebot-dashboard.service   # after a dashboard build
sudo loginctl enable-linger "$USER"     # keep them running with nobody logged in
journalctl --user -u homebot-api -f
```

User units, not system units, on purpose: a system unit means root, a service
account and a hardened sandbox whose `ReadWritePaths` have to match your
layout. The appliance build does exactly that, see `systemd/` and
[`INSTALL_ORIN.md`](INSTALL_ORIN.md), but it is not what a generic Linux box
should get by default.

Re-running `install.sh --with-services` overwrites both files; edits belong in
a drop-in (`systemctl --user edit homebot-api`).

## Zone 3 (the network edge), opt-in

`--with-zone3` changes nothing on your network. It checks Docker, checks that
port 53 is free, detects your LAN IPv4, writes it into the env file as
`PUFFPAI_BIND_IP`, and prints the commands.

Requirements: Docker Engine + the compose plugin, port 53 free (on Ubuntu,
`systemd-resolved` usually holds it), and a LAN IPv4 address on this box.

```bash
set -a; . ~/.local/share/puffpai/homebot.env; set +a
docker compose -f docker-compose.zone3.yml up -d
python scripts/zone3_bootstrap_adguard.py
```

It ships inert twice over: nothing points at the resolver until you change one
field on your router, and protection is off in the seeded configuration. Read
[`ZONE3_SETUP.md`](ZONE3_SETUP.md) before the router step.

## Ollama

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

Ollama in a container or on another machine is fine: set
`PUFFPAI_OLLAMA_URL`. Which model to pull is the wizard's answer, not a fixed
one: it fits the model menu to your total RAM. It reports your GPU and VRAM but
does not size by them ([`HARDWARE.md`](HARDWARE.md)).

## Optional containers

`docker-compose.media.yml` (Jellyfin + Calibre-Web), `docker-compose.nodered.yml`
(automations), `docker-compose.spotify.yml`, `searxng/`. Each is independently
optional; [`.env.example`](../.env.example) documents every variable and what
greys out when a container is absent.
