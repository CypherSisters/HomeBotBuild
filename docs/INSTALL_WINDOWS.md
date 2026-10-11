# Install HomeBot on Windows

Windows 10/11 x64, PowerShell 5.1+, the `py` launcher. No administrator rights
are needed for the install itself.

## The one command

```powershell
git clone https://github.com/CypherSisters/HomeBotBuild.git
cd HomeBotBuild
powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1
```

It checks Python (3.10+, the floor in `setup.py`), checks git, creates `venv\`,
installs `requirements.txt` and then this checkout in editable mode, mints a
per-install API key into your state directory, and looks for Ollama. It never
installs Ollama and never downloads a model unless you ask.

| Flag | Effect |
|---|---|
| `-DryRun` | print every command, change nothing |
| `-WithDashboard` | `npm ci; npm run build` in `dashboard\` (Node 20.19+) |
| `-WithVoice` | also install the `[voice]` extra |
| `-WithDev` | also install the `[dev]` extra (pytest, mypy, flake8) |
| `-WithStartup` | print a Task Scheduler recipe. It creates nothing |
| `-WithZone3` | prints why Zone 3 is Linux-only and changes nothing |
| `-YesInstallOllama` | allow `winget install Ollama.Ollama` to run |
| `-Pull TAG` | allow one `ollama pull TAG` at the end |

## Next: the wizard

Two surfaces, one wizard. Either is a complete setup; pick one.

In the terminal:

```powershell
.\venv\Scripts\Activate.ps1
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

## The manual path

```powershell
py -3 -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -e .            # add ".[dev]" for the tests
puffpai setup
```

## State directory and the API key

Everything HomeBot writes goes to `%PUFFPAI_DATA_DIR%` when that variable is
set, otherwise `%LOCALAPPDATA%\PuffPAI`. Nothing secret is written into the
checkout.

The installer creates `homebot.env` there on first run and never touches it
again:

```
PUFFPAI_DATA_DIR=C:\Users\you\AppData\Local\PuffPAI
PUFFPAI_API_KEY=<64 hex characters>
```

That key authorises **every** request that reaches the backend, so the file is
ACL-restricted to your user (`icacls /inheritance:r`). The installer does not
print it. Read it when a client needs it:

```powershell
Select-String PUFFPAI_API_KEY "$env:LOCALAPPDATA\PuffPAI\homebot.env"
```

To move the state directory, set the variable **before** installing:

```powershell
setx PUFFPAI_DATA_DIR "D:\HomeBot"
```

## Running it

```powershell
$env:PUFFPAI_API_KEY = (Select-String PUFFPAI_API_KEY "$env:LOCALAPPDATA\PuffPAI\homebot.env").Line.Split('=')[1]
python start_api.py
```

The backend refuses to start with no resolvable API key, that is deliberate,
and the error names where to fix it. Port 8000 is the default;
`PUFFPAI_API_PORT` moves it if something else already owns that port.

`PUFFPAI_API_BIND` defaults to `127.0.0.1`. The dashboard, the kiosk and voice
all reach the backend over loopback, so the default install needs nothing
wider. Widen it only for a client that is genuinely off this machine, and name
an interface rather than `0.0.0.0`:

```powershell
$env:PUFFPAI_API_BIND = "192.0.2.10"        # this box's LAN address, for off-box Home Assistant
$env:PUFFPAI_TRUSTED_LOCAL_PEERS = "192.0.2.20"   # HA's address; its integration cannot attach our API key
```

For Node-RED in a container, bind the Docker bridge address instead
(`$env:PUFFPAI_API_BIND = "172.17.0.1"`) so the port is offered to containers
and not to the LAN. Any non-loopback bind logs one SECURITY line every boot
naming the exposure: this build ships no host packet filter, so the API key is
the only control in front of that port.

Dashboard, once built, in a second PowerShell window:

```powershell
cd dashboard
npm run start
```

It serves <http://localhost:3003>. Put the same API key into
`dashboard\.env.local` first (`-WithDashboard` writes it for you from
`dashboard\.env.local.example`, with the key the installer minted).

## Start at logon

The installer creates no scheduled task, no service and no registry entry. If
you want HomeBot to start with you, run this yourself:

```powershell
schtasks /Create /TN "HomeBot API" /SC ONLOGON /RL LIMITED `
  /TR "'C:\path\to\HomeBotBuild\venv\Scripts\pythonw.exe' 'C:\path\to\HomeBotBuild\start_api.py'"
```

Remove it with `schtasks /Delete /TN "HomeBot API" /F`. The task inherits your
user environment, so `setx PUFFPAI_DATA_DIR ...` first if your state directory
is not the default.

## Ollama

HomeBot needs a local model runtime and will not install one behind your back.

```powershell
winget install Ollama.Ollama          # or https://ollama.com/download/windows
```

Ollama in WSL or on another machine is fine: set `PUFFPAI_OLLAMA_URL` to it.
Which model to pull is the wizard's answer, not a fixed one: it fits the model
menu to your total RAM. It reports your GPU and VRAM but does not size by them
([`HARDWARE.md`](HARDWARE.md)).

## What is different on Windows

| Feature | Status |
|---|---|
| Chat, dashboard, apps, knowledge, Home Assistant | full |
| Voice input (mic, wake word, Whisper) | works with `-WithVoice`; the wheels are Windows-native |
| Voice output (Piper) | no Windows wheel, speech-out falls back to the browser voice in the dashboard |
| Volume control | PulseAudio only, so the system mixer lever is inactive; the module reports "not available on this platform" rather than failing |
| systemd units | not applicable, use Task Scheduler above |
| Zone 3 network edge | not available; run it on a Linux machine or a Raspberry Pi |
| Media, Node-RED, SearXNG containers | need Docker Desktop; see [`.env.example`](../.env.example) |

## Troubleshooting

* **"running scripts is disabled"**, use the full command above with
  `-ExecutionPolicy Bypass`; it applies to that one process only.
* **`pip` fails building a wheel**, install the Microsoft C++ Build Tools, or
  drop the extra that pulled it (the base install needs no compiler).
* **`puffpai` is not recognised**, activate the venv first.
* **Long-path errors during `npm ci`**, enable long paths:
  `git config --system core.longpaths true`, and set
  `HKLM\SYSTEM\CurrentControlSet\Control\FileSystem\LongPathsEnabled` to 1.
