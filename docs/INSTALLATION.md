# Installation

One command per OS. Pick your page:

* **Windows**, [INSTALL_WINDOWS.md](INSTALL_WINDOWS.md) (`install.ps1`)
* **Linux**, [INSTALL_LINUX.md](INSTALL_LINUX.md) (`install.sh`)
* **Jetson AGX Orin appliance**, [INSTALL_ORIN.md](INSTALL_ORIN.md)
  (`scripts/install_orin.sh`); this is the appendix, not the normal path

Hardware notes: [HARDWARE.md](HARDWARE.md). Settings and every environment
variable the runtime reads: [CONFIG.md](CONFIG.md). Docker Compose variables:
[`.env.example`](../.env.example).

---

## Development setup

Same installers, plus the `dev` extra:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 -WithDev
```
```bash
bash install.sh --with-dev
```

Or by hand, from a checkout:

```bash
python3 -m venv venv
. venv/bin/activate                  # Windows: .\venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install -r requirements.txt
pip install -e ".[dev]"
python start_api.py                  # serves :8000
```

The relative `paths.*` defaults in `config/puffpai_config.json` are fine when
you run from a checkout you own; set `PUFFPAI_DATA_DIR` to keep state out of
the tree (the installers do this for you). The dashboard runs separately:

```bash
cd dashboard && npm install && npm run dev      # :3003
```

Tests: `python -m pytest -q tests`.
