<#
    HomeBotBuild installer (Windows, PowerShell 5.1+).

    Creates a venv in .\venv, installs this checkout with pip, mints a
    per-install API key into your state directory, and tells you what to run
    next. It never installs Ollama and never downloads a model unless you ask
    for that on the command line, and it never writes a secret into the
    checkout.

    ASCII only in every console string: the legacy Windows console garbles
    em dashes and curly quotes.

    Usage:
        powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 [options]
#>

[CmdletBinding()]
param(
    [switch]$WithDashboard,
    [switch]$WithVoice,
    [switch]$WithDev,
    [switch]$WithStartup,
    [switch]$WithZone3,
    [switch]$YesInstallOllama,
    [string]$Pull = "",
    [switch]$DryRun,
    [switch]$Help
)

$ErrorActionPreference = "Stop"

$PyMinMajor   = 3
$PyMinMinor   = 10       # setup.py: python_requires=">=3.10"
$NodeMinMajor = 20       # dashboard/package.json engines

$RepoDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvDir = Join-Path $RepoDir "venv"

function Say  ($m) { Write-Host $m }
function Step ($m) { Write-Host ""; Write-Host ("== " + $m) }
function Fail ($m) { Write-Host ""; Write-Host ("ERROR: " + $m); exit 1 }

function Invoke-Step {
    param([string]$Exe, [string[]]$Arguments, [string]$WorkDir = $RepoDir)
    $line = "$Exe $($Arguments -join ' ')"
    if ($DryRun) { Say ("  [dry-run] " + $line); return }
    Say ("  + " + $line)
    Push-Location $WorkDir
    try {
        & $Exe @Arguments
        if ($LASTEXITCODE -ne 0) { Fail ("command failed (exit $LASTEXITCODE): " + $line) }
    } finally { Pop-Location }
}

if ($Help) {
    @"
Usage: powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 [options]

  -WithDashboard      also build the Next.js dashboard (needs Node 20.19+)
  -WithVoice          also install the [voice] extra (mic, wake word, Whisper)
  -WithDev            also install the [dev] extra (pytest, mypy, flake8)
  -WithStartup        print a Task Scheduler recipe for starting HomeBot at
                      logon. It creates NOTHING: a scheduled task is a change
                      to your machine, so you run the command yourself.
  -WithZone3          not available on Windows; prints why and exits that step
  -YesInstallOllama   allow this script to install Ollama via winget
                      (off by default; nothing is installed silently)
  -Pull TAG           allow this script to run "ollama pull TAG" at the end
                      (off by default; no model is ever downloaded silently)
  -DryRun             print every command that would run and change nothing
  -Help               this text

State (config, vault, logs, the API key) goes to %PUFFPAI_DATA_DIR% when set,
otherwise %LOCALAPPDATA%\PuffPAI. Nothing secret is ever written into the
checkout and no key is ever printed.
"@ | Write-Host
    exit 0
}

# --- 1. Python -------------------------------------------------------------
Step "Python (need $PyMinMajor.$PyMinMinor or newer)"

$Python = $null
$PythonPre = @()
$probe = "import sys; raise SystemExit(0 if sys.version_info[:2] >= ($PyMinMajor, $PyMinMinor) else 1)"
foreach ($cand in @(@("py", @("-3")), @("python", @()), @("python3", @())))  {
    $exe = $cand[0]; $pre = $cand[1]
    if (Get-Command $exe -ErrorAction SilentlyContinue) {
        & $exe @pre -c $probe 2>$null
        if ($LASTEXITCODE -eq 0) { $Python = $exe; $PythonPre = $pre; break }
    }
}
if (-not $Python) {
    Fail "no Python >= $PyMinMajor.$PyMinMinor on PATH. Get it from https://www.python.org/downloads/ and tick 'Add python.exe to PATH'."
}
Say ("  found: " + (& $Python @PythonPre --version 2>&1))

# --- 2. git ----------------------------------------------------------------
Step "git"
if (Get-Command git -ErrorAction SilentlyContinue) {
    Say ("  found: " + (git --version))
} else {
    Say "  NOT FOUND. git is needed to update this checkout (not to run it)."
    Say "  Get it from https://git-scm.com/download/win  (or: winget install Git.Git)"
}

# --- 3. venv + pip ---------------------------------------------------------
Step "Virtual environment at venv\"
$VenvPy = Join-Path $VenvDir "Scripts\python.exe"
if (Test-Path $VenvPy) {
    Say "  venv\ already exists; reusing it."
} else {
    Invoke-Step $Python ($PythonPre + @("-m", "venv", $VenvDir))
}

Step "Installing HomeBot"
$extras = @()
if ($WithVoice) { $extras += "voice" }
if ($WithDev)   { $extras += "dev" }
$target = "."
if ($extras.Count -gt 0) { $target = ".[" + ($extras -join ",") + "]" }

Invoke-Step $VenvPy @("-m", "pip", "install", "--upgrade", "pip")
Invoke-Step $VenvPy @("-m", "pip", "install", "-r", "requirements.txt")
Invoke-Step $VenvPy @("-m", "pip", "install", "-e", $target)

if ($WithVoice) {
    Say ""
    Say "  Voice on Windows: the mic, wake word and Whisper wheels are installed,"
    Say "  but Piper speech-out has no Windows wheel. Text-to-speech falls back to"
    Say "  the browser voice in the dashboard. See docs/INSTALL_WINDOWS.md."
}

# --- 4. state dir + API key ------------------------------------------------
Step "State directory and API key"

$StateDir = $env:PUFFPAI_DATA_DIR
if (-not $StateDir) {
    $localApp = $env:LOCALAPPDATA
    if (-not $localApp) { $localApp = Join-Path $env:USERPROFILE "AppData\Local" }
    $StateDir = Join-Path $localApp "PuffPAI"
}
Say ("  state dir: " + $StateDir)
$EnvFile = Join-Path $StateDir "homebot.env"

if ($DryRun) {
    Say ("  [dry-run] would create " + $StateDir + " and mint " + $EnvFile + " if absent")
} elseif (Test-Path $EnvFile) {
    Say "  env file exists; leaving the existing key alone."
} else {
    New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
    # Temp, never the checkout: nothing this installer generates belongs in a
    # tree the user may commit.
    $minter = Join-Path ([System.IO.Path]::GetTempPath()) ("homebot_mint_" + [System.Guid]::NewGuid().ToString("N") + ".py")
    @'
import os, secrets, sys
env_file, state_dir = sys.argv[1], sys.argv[2]
os.environ.setdefault("PUFFPAI_API_KEY", "x" * 40)  # import-time placeholder
key = secrets.token_hex(32)
try:
    from puffpai.api_server import validate_api_key
except Exception as exc:
    print("  note: could not load the backend to re-check the key (%s)" % exc)
else:
    key = validate_api_key(key, source="the installer-minted key")
fd = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
with os.fdopen(fd, "w") as fh:
    fh.write("# HomeBot per-install environment. The API key below authorises\n")
    fh.write("# every request that reaches the backend -- do not share this file.\n")
    fh.write("PUFFPAI_DATA_DIR=%s\n" % state_dir)
    fh.write("PUFFPAI_API_KEY=%s\n" % key)
print("  minted a new API key into the environment file (not shown here)")
'@ | Set-Content -Path $minter -Encoding ASCII
    try {
        $env:PUFFPAI_DATA_DIR = $StateDir
        Invoke-Step $VenvPy @($minter, $EnvFile, $StateDir)
        # NTFS has no chmod: restrict the file to this user explicitly, the way
        # 0600 does on Linux. Inherited ACLs are removed first.
        icacls $EnvFile /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null
    } finally {
        Remove-Item $minter -ErrorAction SilentlyContinue
    }
}
Say ("  read it when a client needs it:  Select-String PUFFPAI_API_KEY '" + $EnvFile + "'")

# --- 5. Ollama -------------------------------------------------------------
Step "Ollama (the local model runtime)"

$OllamaOk = $false
if (Get-Command ollama -ErrorAction SilentlyContinue) {
    Say ("  found: " + ((ollama --version 2>&1) | Select-Object -First 1))
    $OllamaOk = $true
} else {
    $url = $env:PUFFPAI_OLLAMA_URL
    if (-not $url) { $url = $env:OLLAMA_HOST }
    if (-not $url) { $url = "http://127.0.0.1:11434" }
    if ($url -notmatch '^https?://') { $url = "http://" + $url }
    try {
        Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 -Uri ($url + "/api/tags") | Out-Null
        Say ("  no 'ollama' on PATH, but something answers at " + $url)
        $OllamaOk = $true
    } catch { }
}

if (-not $OllamaOk) {
    Say "  NOT FOUND. HomeBot needs a local model runtime; it will not install one for you."
    Say "  Official instructions: https://ollama.com/download/windows"
    Say "    winget install Ollama.Ollama"
    if ($YesInstallOllama) {
        Say "  -YesInstallOllama given, so installing via winget now."
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            Invoke-Step "winget" @("install", "--id", "Ollama.Ollama", "-e", "--accept-package-agreements", "--accept-source-agreements")
        } else {
            Say "  winget is missing; use the download page above."
        }
    }
}

if ($Pull -ne "") {
    Step "Model download (you asked for this with -Pull)"
    Invoke-Step "ollama" @("pull", $Pull)
}

# --- 6. dashboard ----------------------------------------------------------
if ($WithDashboard) {
    Step "Dashboard (Node >= $NodeMinMajor.19)"
    if (Get-Command node -ErrorAction SilentlyContinue) {
        Say ("  found: node " + (node --version))
        $dash = Join-Path $RepoDir "dashboard"
        Invoke-Step "npm.cmd" @("ci") $dash
        Invoke-Step "npm.cmd" @("run", "build") $dash
    } else {
        Say "  node NOT FOUND. Install Node 20.19+ or 22.12+ from https://nodejs.org"
        Say "  then: cd dashboard; npm ci; npm run build"
    }

    # The dashboard is a SEPARATE process that reaches the backend over HTTP, so
    # it needs the same API key. Without it every page loads empty rather than
    # failing loudly. The key already exists in $EnvFile; read it back from
    # there instead of minting a second one, and never print it.
    $DashEnv = Join-Path $RepoDir "dashboard\.env.local"
    $DashExample = Join-Path $RepoDir "dashboard\.env.local.example"
    if ($DryRun) {
        Say ("  [dry-run] would write " + $DashEnv + " from .env.local.example with the minted key")
    } elseif (Test-Path $DashEnv) {
        Say "  dashboard\.env.local exists; leaving your copy alone."
    } elseif (-not (Test-Path $EnvFile)) {
        Say ("  no " + $EnvFile + " yet, so dashboard\.env.local was not written.")
        Say "  copy dashboard\.env.local.example by hand and fill in the key."
    } else {
        $keyLine = @(Get-Content $EnvFile | Where-Object { $_ -match '^PUFFPAI_API_KEY=' })
        $keyLine = if ($keyLine.Count -gt 0) { $keyLine[0] } else { $null }
        $filled = Get-Content $DashExample | ForEach-Object {
            if ($_ -match '^PUFFPAI_API_KEY=' -and $keyLine) { $keyLine } else { $_ }
        }
        Set-Content -Path $DashEnv -Value $filled -Encoding ASCII
        # NTFS has no chmod: restrict to this user, the way 0600 does on Linux.
        icacls $DashEnv /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null
        if ($keyLine -and $keyLine -ne 'PUFFPAI_API_KEY=') {
            Say ("  wrote " + $DashEnv + " with the key from your state directory")
            Say ("  (not shown here; it is the same secret as in " + $EnvFile + ")")
        } else {
            Say ("  wrote " + $DashEnv + ", but found no PUFFPAI_API_KEY in")
            Say ("  " + $EnvFile + " -- fill the key in by hand or the dashboard gets 401s.")
        }
    }
}

# --- 7. Zone 3 -------------------------------------------------------------
if ($WithZone3) {
    Step "Zone 3 (the Bridge)"
    Say "  Zone 3 is Linux only. It runs AdGuard Home in Docker bound to port 53"
    Say "  on this box's LAN address, and it is driven by systemd units this"
    Say "  installer does not have a Windows equivalent for. Run it on a Linux"
    Say "  machine, or on a Raspberry Pi beside this one. Nothing was changed."
}

# --- 8. startup ------------------------------------------------------------
if ($WithStartup) {
    Step "Start HomeBot at logon (recipe only -- nothing was created)"
    $pyw = Join-Path $VenvDir "Scripts\pythonw.exe"
    $start = Join-Path $RepoDir "start_api.py"
    @"
  Run this yourself, in a PowerShell window, if you want it:

    schtasks /Create /TN "HomeBot API" /SC ONLOGON /RL LIMITED ``
      /TR "'$pyw' '$start'"

  Remove it again with:

    schtasks /Delete /TN "HomeBot API" /F

  The task inherits your user environment, so set PUFFPAI_DATA_DIR as a user
  variable first if your state directory is not the default:

    setx PUFFPAI_DATA_DIR "$StateDir"
"@ | Write-Host
}

# --- 9. what to do next ----------------------------------------------------
@"

Done. Activate the environment and run the wizard:

    .\venv\Scripts\Activate.ps1
    puffpai setup

That is the front door: it looks at your hardware, picks a brain that fits,
asks what you are building, and writes a build manifest.

Afterwards:
    puffpai chat          talk to your bot
    python start_api.py   the local API (set PUFFPAI_API_BIND=127.0.0.1 for loopback)
"@ | Write-Host

if ($WithDashboard) {
    @"
    cd dashboard; npm run start       the web dashboard on http://localhost:3003
      (dashboard\.env.local is already written, with the key from the env file)
"@ | Write-Host
}

if ((-not $OllamaOk) -and (-not $YesInstallOllama)) {
    @"

Note: no Ollama was found. 'puffpai setup' still runs and still writes a
manifest; your bot just has no brain to answer with until Ollama is up.
"@ | Write-Host
}
