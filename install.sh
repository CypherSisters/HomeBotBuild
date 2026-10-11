#!/usr/bin/env bash
# HomeBotBuild installer (Linux).
#
# Creates a venv in ./venv, installs this checkout with pip, mints a per-install
# API key into your state directory, and tells you what to run next. It never
# installs Ollama and never downloads a model unless you ask for that on the
# command line, and it never writes a secret into the checkout.
#
# For the Jetson Orin appliance build use scripts/install_orin.sh instead; this
# script is the generic-hardware path.
#
# ASCII only in every string printed here: this file is read by people whose
# terminals are not all UTF-8.

set -euo pipefail

PY_MIN_MAJOR=3
PY_MIN_MINOR=10          # setup.py: python_requires=">=3.10"
NODE_MIN_MAJOR=20        # dashboard/package.json engines

DRY_RUN=0
WITH_DASHBOARD=0
WITH_VOICE=0
WITH_DEV=0
WITH_SERVICES=0
ENABLE_SERVICES=0
WITH_ZONE3=0
NO_APT=0
YES_OLLAMA=0
PULL_TAG=""

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$REPO_DIR/venv"

usage() {
    cat <<'EOF'
Usage: bash install.sh [options]

  --with-dashboard      also build the Next.js dashboard (needs Node 20.19+)
  --with-voice          also install the [voice] extra (mic, wake word, Whisper)
  --with-dev            also install the [dev] extra (pytest, mypy, flake8)
  --with-services       GENERATE systemd USER units for the API and dashboard
                        under ~/.config/systemd/user (written, not enabled)
  --enable              with --with-services, also enable and start them
  --with-zone3          print what Zone 3 needs and wire docker-compose.zone3.yml
  --no-apt              never offer the apt line; print it instead
  --yes-install-ollama  allow this script to install Ollama via its official
                        script (off by default; nothing is installed silently)
  --pull TAG            allow this script to run "ollama pull TAG" at the end
                        (off by default; no model is ever downloaded silently)
  --dry-run             print every command that would run and change nothing
  -h, --help            this text

State (config, vault, logs, the API key) goes to $PUFFPAI_DATA_DIR when set,
otherwise $XDG_DATA_HOME/puffpai (i.e. ~/.local/share/puffpai). Nothing secret
is ever written into the checkout and no key is ever printed.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --with-dashboard) WITH_DASHBOARD=1 ;;
        --with-voice)     WITH_VOICE=1 ;;
        --with-dev)       WITH_DEV=1 ;;
        --with-services)  WITH_SERVICES=1 ;;
        --enable)         ENABLE_SERVICES=1 ;;
        --with-zone3)     WITH_ZONE3=1 ;;
        --no-apt)         NO_APT=1 ;;
        --yes-install-ollama) YES_OLLAMA=1 ;;
        --pull)           shift; PULL_TAG="${1:-}" ;;
        --dry-run)        DRY_RUN=1 ;;
        -h|--help)        usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

say()  { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
fail() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

run() {
    if [ "$DRY_RUN" = "1" ]; then
        printf '  [dry-run] %s\n' "$*"
        return 0
    fi
    printf '  + %s\n' "$*"
    "$@"
}

case "$(uname -s)" in
    Linux) : ;;
    Darwin)
        say "macOS: this script runs, but HomeBot is tested on Linux and Windows."
        say "Voice, Zone 3 and the systemd units below are Linux-only." ;;
    *)  fail "this installer is for Linux. On Windows run install.ps1 instead." ;;
esac

# --- 1. Python -------------------------------------------------------------
step "Python (need ${PY_MIN_MAJOR}.${PY_MIN_MINOR} or newer)"

PYTHON=""
for cand in python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then
        if "$cand" -c "import sys; raise SystemExit(0 if sys.version_info[:2] >= ($PY_MIN_MAJOR, $PY_MIN_MINOR) else 1)" 2>/dev/null; then
            PYTHON="$cand"
            break
        fi
    fi
done
[ -n "$PYTHON" ] || fail "no python >= ${PY_MIN_MAJOR}.${PY_MIN_MINOR} on PATH. Install it and re-run."
say "  found: $("$PYTHON" --version 2>&1) at $(command -v "$PYTHON")"

# --- 2. git ----------------------------------------------------------------
step "git"
if command -v git >/dev/null 2>&1; then
    say "  found: $(git --version)"
else
    say "  NOT FOUND. git is needed to update this checkout (not to run it)."
    say "  Debian/Ubuntu: sudo apt-get install -y git"
fi

# --- 3. distro + apt line --------------------------------------------------
step "System packages"

DISTRO="unknown"
if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    DISTRO="$(. /etc/os-release && printf '%s' "${ID_LIKE:-$ID}")"
fi

APT_LINE="sudo apt-get install -y python${PY_MIN_MAJOR}-venv python${PY_MIN_MAJOR}-dev build-essential"
[ "$WITH_VOICE" = "1" ] && APT_LINE="$APT_LINE libportaudio2"
case "$DISTRO" in
    *debian*|*ubuntu*)
        if [ "$NO_APT" = "1" ]; then
            say "  Debian/Ubuntu detected. --no-apt given, so run this yourself if the venv step fails:"
            say "    $APT_LINE"
        else
            say "  Debian/Ubuntu detected. This is the only command that needs sudo:"
            say "    $APT_LINE"
            if [ "$DRY_RUN" = "1" ]; then
                say "  [dry-run] would ask before running it"
            else
                printf '  Run it now? [y/N] '
                read -r reply </dev/tty || reply=""
                case "$reply" in
                    [yY]*) run sh -c "$APT_LINE" ;;
                    *) say "  Skipped. Re-run it yourself if the venv step fails." ;;
                esac
            fi
        fi
        ;;
    *fedora*|*rhel*)
        say "  Fedora/RHEL family detected. Manual equivalent:"
        say "    sudo dnf install -y python3-devel gcc gcc-c++ make" ;;
    *arch*)
        say "  Arch family detected. Manual equivalent:"
        say "    sudo pacman -S --needed base-devel python" ;;
    *suse*)
        say "  openSUSE detected. Manual equivalent:"
        say "    sudo zypper install -y python3-devel gcc gcc-c++ make" ;;
    *)
        say "  Unrecognised distro. You need a C toolchain and the venv module"
        say "  for your Python; install them the way your distro does." ;;
esac

# --- 4. venv + pip ---------------------------------------------------------
step "Virtual environment at venv/"
if [ -x "$VENV_DIR/bin/python" ]; then
    say "  venv/ already exists; reusing it."
else
    run "$PYTHON" -m venv "$VENV_DIR"
fi

VPY="$VENV_DIR/bin/python"
[ "$DRY_RUN" = "1" ] && VPY="$PYTHON"   # only used to print commands

step "Installing HomeBot"
EXTRAS=""
[ "$WITH_VOICE" = "1" ] && EXTRAS="${EXTRAS}voice,"
[ "$WITH_DEV" = "1" ]   && EXTRAS="${EXTRAS}dev,"
[ "$WITH_ZONE3" = "1" ] && EXTRAS="${EXTRAS}zone3,"
TARGET="."
[ -n "$EXTRAS" ] && TARGET=".[${EXTRAS%,}]"

run "$VENV_DIR/bin/python" -m pip install --upgrade pip
( cd "$REPO_DIR" && run "$VENV_DIR/bin/python" -m pip install -r requirements.txt )
( cd "$REPO_DIR" && run "$VENV_DIR/bin/python" -m pip install -e "$TARGET" )

# --- 5. state dir + API key ------------------------------------------------
step "State directory and API key"

STATE_DIR="${PUFFPAI_DATA_DIR:-}"
if [ -z "$STATE_DIR" ]; then
    STATE_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/puffpai"
fi
say "  state dir: $STATE_DIR"
ENV_FILE="$STATE_DIR/homebot.env"

if [ "$DRY_RUN" = "1" ]; then
    say "  [dry-run] would create $STATE_DIR and mint $ENV_FILE (0600) if absent"
elif [ -f "$ENV_FILE" ]; then
    say "  $ENV_FILE exists; leaving the existing key alone."
else
    mkdir -p "$STATE_DIR"
    chmod 700 "$STATE_DIR" 2>/dev/null || true
    # The key is minted AND validated by the backend's own rule
    # (api_server.validate_api_key), so the installer cannot write a value the
    # service would then refuse to start with. It is never echoed.
    PUFFPAI_DATA_DIR="$STATE_DIR" "$VENV_DIR/bin/python" - "$ENV_FILE" "$STATE_DIR" <<'PY'
import os, secrets, sys
env_file, state_dir = sys.argv[1], sys.argv[2]
os.environ.setdefault("PUFFPAI_API_KEY", "x" * 40)  # import-time placeholder
key = secrets.token_hex(32)
try:
    from puffpai.api_server import validate_api_key
except Exception as exc:                 # backend deps not importable yet
    print("  note: could not load the backend to re-check the key (%s)" % exc)
else:
    # The service's own rule decides, so the installer cannot write a value
    # the backend would then refuse to start with.
    key = validate_api_key(key, source="the installer-minted key")
fd = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as fh:
    fh.write("# HomeBot per-install environment. 0600 on purpose: the API key\n")
    fh.write("# below authorises every request that reaches the backend.\n")
    fh.write("PUFFPAI_DATA_DIR=%s\n" % state_dir)
    fh.write("PUFFPAI_API_KEY=%s\n" % key)
print("  minted a new API key into the environment file (not shown here)")
PY
fi
say "  read it when a client needs it:  grep PUFFPAI_API_KEY $ENV_FILE"

# --- 6. Ollama -------------------------------------------------------------
step "Ollama (the local model runtime)"

OLLAMA_URL="${PUFFPAI_OLLAMA_URL:-${OLLAMA_HOST:-http://127.0.0.1:11434}}"
case "$OLLAMA_URL" in http*) : ;; *) OLLAMA_URL="http://$OLLAMA_URL" ;; esac

OLLAMA_OK=0
if command -v ollama >/dev/null 2>&1; then
    say "  found: $(ollama --version 2>&1 | head -1)"
    OLLAMA_OK=1
elif command -v curl >/dev/null 2>&1 && curl -fsS --max-time 3 "$OLLAMA_URL/api/tags" >/dev/null 2>&1; then
    say "  no 'ollama' binary here, but something answers at $OLLAMA_URL"
    say "  (a container or another machine is fine: set PUFFPAI_OLLAMA_URL)"
    OLLAMA_OK=1
fi

if [ "$OLLAMA_OK" = "0" ]; then
    say "  NOT FOUND. HomeBot needs a local model runtime; it will not install one for you."
    say "  Official instructions: https://ollama.com/download"
    say "    Linux: curl -fsSL https://ollama.com/install.sh | sh"
    if [ "$YES_OLLAMA" = "1" ]; then
        say "  --yes-install-ollama given, so running the official script now."
        if command -v curl >/dev/null 2>&1; then
            run sh -c "curl -fsSL https://ollama.com/install.sh | sh"
        else
            say "  curl is missing; install curl, then run the line above yourself."
        fi
    fi
fi

if [ -n "$PULL_TAG" ]; then
    step "Model download (you asked for this with --pull)"
    run ollama pull "$PULL_TAG"
fi

# --- 7. dashboard ----------------------------------------------------------
if [ "$WITH_DASHBOARD" = "1" ]; then
    step "Dashboard (Node >= ${NODE_MIN_MAJOR}.19)"
    if command -v node >/dev/null 2>&1; then
        say "  found: node $(node --version)"
        ( cd "$REPO_DIR/dashboard" && run npm ci && run npm run build )
    else
        say "  node NOT FOUND. Install Node 20.19+ or 22.12+ from https://nodejs.org"
        say "  then: cd dashboard && npm ci && npm run build"
    fi

    # The dashboard is a SEPARATE process that talks to the backend over HTTP,
    # so it needs the same API key -- and without it every page loads empty
    # instead of failing loudly, which is a miserable first five minutes. The
    # key already exists in $ENV_FILE; read it back from there rather than
    # minting a second one, and never echo it.
    DASH_ENV="$REPO_DIR/dashboard/.env.local"
    DASH_EXAMPLE="$REPO_DIR/dashboard/.env.local.example"
    if [ "$DRY_RUN" = "1" ]; then
        say "  [dry-run] would write $DASH_ENV from .env.local.example with the minted key"
    elif [ -f "$DASH_ENV" ]; then
        say "  dashboard/.env.local exists; leaving your copy alone."
    elif [ ! -f "$ENV_FILE" ]; then
        say "  no $ENV_FILE yet, so dashboard/.env.local was not written."
        say "  copy dashboard/.env.local.example by hand and fill in the key."
    else
        # awk reads the key out of $ENV_FILE itself, so the value never becomes
        # a shell variable or an argv entry -- no process table, no shell trace,
        # no log line. A missing key line leaves the placeholder empty.
        # umask in a subshell so the file is 0600 from creation and the rest of
        # this script keeps the caller's umask.
        ( umask 077
          awk -v envfile="$ENV_FILE" '
            BEGIN {
              while ((getline line < envfile) > 0)
                if (line ~ /^PUFFPAI_API_KEY=/) key = line
            }
            /^PUFFPAI_API_KEY=/ { print (key == "" ? $0 : key); next }
            { print }
          ' "$DASH_EXAMPLE" > "$DASH_ENV" )
        chmod 600 "$DASH_ENV" 2>/dev/null || true
        if grep -q '^PUFFPAI_API_KEY=.' "$DASH_ENV" 2>/dev/null; then
            say "  wrote $DASH_ENV (0600) with the key from your state directory"
            say "  (not shown here; it is the same secret as in $ENV_FILE)"
        else
            say "  wrote $DASH_ENV (0600), but found no PUFFPAI_API_KEY in"
            say "  $ENV_FILE -- fill the key in by hand or the dashboard gets 401s."
        fi
    fi
fi

# --- 8. systemd USER units -------------------------------------------------
if [ "$WITH_SERVICES" = "1" ]; then
    step "systemd user units"
    UNIT_DIR="$HOME/.config/systemd/user"
    if ! command -v systemctl >/dev/null 2>&1; then
        say "  systemctl NOT FOUND (no systemd on this box). Skipping."
        say "  Run the backend yourself: $VENV_DIR/bin/python $REPO_DIR/start_api.py"
    elif [ "$DRY_RUN" = "1" ]; then
        say "  [dry-run] would write $UNIT_DIR/homebot-api.service and homebot-dashboard.service"
    else
        mkdir -p "$UNIT_DIR"
        cat > "$UNIT_DIR/homebot-api.service" <<EOF
[Unit]
# Generated by install.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ). Regenerate by
# re-running install.sh --with-services; edits here are overwritten.
Description=HomeBot API (Flask backend)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$REPO_DIR
Environment=PYTHONUNBUFFERED=1
Environment=PUFFPAI_DATA_DIR=$STATE_DIR
# Loopback only. Set PUFFPAI_API_BIND=0.0.0.0 here if Home Assistant or another
# machine has to reach the backend; every route but /health is behind the API
# key, and this install has no host packet filter of its own.
Environment=PUFFPAI_API_BIND=127.0.0.1
EnvironmentFile=-$ENV_FILE
ExecStart=$VENV_DIR/bin/python $REPO_DIR/start_api.py
Restart=on-failure
RestartSec=10
UMask=0027

[Install]
WantedBy=default.target
EOF
        cat > "$UNIT_DIR/homebot-dashboard.service" <<EOF
[Unit]
# Generated by install.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ).
Description=HomeBot dashboard (Next.js)
After=homebot-api.service
Wants=homebot-api.service

[Service]
Type=simple
WorkingDirectory=$REPO_DIR/dashboard
Environment=NODE_ENV=production
Environment=PORT=3003
EnvironmentFile=-$ENV_FILE
ExecStart=/usr/bin/env npm run start
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
EOF
        say "  wrote $UNIT_DIR/homebot-api.service"
        say "  wrote $UNIT_DIR/homebot-dashboard.service"
        if [ "$ENABLE_SERVICES" = "1" ]; then
            run systemctl --user daemon-reload
            run systemctl --user enable --now homebot-api.service
            say "  enable the dashboard too once you have built it:"
            say "    systemctl --user enable --now homebot-dashboard.service"
            say "  to keep them running with nobody logged in: sudo loginctl enable-linger $USER"
        else
            say "  NOT enabled. Start them when you are ready:"
            say "    systemctl --user daemon-reload"
            say "    systemctl --user enable --now homebot-api.service"
        fi
    fi
fi

# --- 9. Zone 3 -------------------------------------------------------------
if [ "$WITH_ZONE3" = "1" ]; then
    step "Zone 3 (the Bridge) - network edge, Linux only"
    say "  Zone 3 runs AdGuard Home in Docker as the house DNS resolver. It"
    say "  ships INERT: nothing points at it until you change one field on your"
    say "  router, and protection is off in the seeded config."
    say ""
    say "  Requirements, all of which must be true before it will start:"
    if command -v docker >/dev/null 2>&1; then
        say "    [ok]   docker: $(docker --version 2>&1 | head -1)"
    else
        say "    [MISSING] docker - install Docker Engine + the compose plugin"
    fi
    if command -v ss >/dev/null 2>&1 && ss -lntu 2>/dev/null | grep -qE '[:.]53 '; then
        say "    [BUSY] port 53 is already listening here (systemd-resolved?)."
        say "           Free it first: see docs/ZONE3_SETUP.md"
    else
        say "    [ok]   port 53 appears free"
    fi
    BIND_IP="${PUFFPAI_BIND_IP:-}"
    if [ -z "$BIND_IP" ] && command -v ip >/dev/null 2>&1; then
        BIND_IP="$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -1)"
    fi
    if [ -n "$BIND_IP" ]; then
        say "    [ok]   LAN IPv4 for PUFFPAI_BIND_IP: $BIND_IP"
        if [ "$DRY_RUN" = "1" ]; then
            say "  [dry-run] would append PUFFPAI_BIND_IP=$BIND_IP to $ENV_FILE"
        elif [ -f "$ENV_FILE" ] && ! grep -q '^PUFFPAI_BIND_IP=' "$ENV_FILE"; then
            printf 'PUFFPAI_BIND_IP=%s\n' "$BIND_IP" >> "$ENV_FILE"
            say "  wrote PUFFPAI_BIND_IP into $ENV_FILE"
        fi
    else
        say "    [MISSING] could not detect a LAN IPv4. Set PUFFPAI_BIND_IP yourself."
    fi
    say ""
    say "  Then, when you have read docs/ZONE3_SETUP.md and mean it:"
    say "    set -a; . $ENV_FILE; set +a"
    say "    docker compose -f $REPO_DIR/docker-compose.zone3.yml up -d"
    say "    $VENV_DIR/bin/python $REPO_DIR/scripts/zone3_bootstrap_adguard.py"
fi

# --- 10. what to do next ---------------------------------------------------
cat <<EOF

Done. Activate the environment and run the wizard:

    source venv/bin/activate
    puffpai setup

That is the front door: it looks at your hardware, picks a brain that fits,
asks what you are building, and writes a build manifest.

Afterwards:
    puffpai chat      talk to your bot
    python start_api.py    the local API (PUFFPAI_API_BIND=127.0.0.1 for loopback)
EOF

if [ "$WITH_DASHBOARD" = "1" ]; then
    cat <<EOF
    cd dashboard && npm run start     the web dashboard on http://localhost:3003
      (dashboard/.env.local is already written, with the key from $ENV_FILE)
EOF
fi

if [ "$OLLAMA_OK" = "0" ] && [ "$YES_OLLAMA" = "0" ]; then
    cat <<EOF

Note: no Ollama was found. 'puffpai setup' still runs and still writes a
manifest; your bot just has no brain to answer with until Ollama is up.
EOF
fi
