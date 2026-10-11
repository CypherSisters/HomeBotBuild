---
name: Bug report
about: Something is broken or behaving unexpectedly
title: "[BUG] "
labels: bug
assignees: ''
---

<!-- Security problem? Do not file it here: use private vulnerability
     reporting (see SECURITY.md). Before pasting anything below, take out the
     API key, tokens, PINs, setup codes and anything personal (names,
     addresses, your network). -->

## What happened

<!-- What did you expect, and what did you get instead? -->

## Steps to reproduce

1.
2.
3.

## Log excerpt

<!-- The lines around the failure from one of:
     - the terminal running `python start_api.py`, the installer, or `puffpai setup`
     - `<data dir>/logs/puffpai.log` (the data dir is %LOCALAPPDATA%\PuffPAI
       on Windows and ~/.local/share/puffpai on Linux, unless you set
       PUFFPAI_DATA_DIR)
     - Linux with the systemd user units: `journalctl --user -u homebot-api -n 100 --no-pager` -->

```
paste here
```

## `/system/status` output (optional)

<!-- `curl -s -H "X-API-Key: <your key>" http://127.0.0.1:8000/system/status`
     (the key is in `<data dir>/homebot.env`). REDACT any secret field before
     pasting. -->

```
paste here, secrets REDACTED
```

## Machine

<!-- OS and version, Python version, CPU, RAM, GPU if any, and the model the
     wizard picked (the This machine card prints most of it). -->

## Commit

<!-- `git rev-parse --short HEAD` in your checkout. -->

## Anything else

<!-- Screenshots, or context that would help. -->
