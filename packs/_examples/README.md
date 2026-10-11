# Example packs

Skipped by the loader exactly like `apps/_examples/`: `load_packs` ignores any
folder starting with `_` or `.`.

A pack is a **list**, not a container. It names apps that must already be
installed and carries no data, no vocabulary, no UI and no code of its own. A
pack naming an app this build does not have is refused with the missing ids
named, rather than quietly installing half a persona — so install
`apps/_examples/sky_log` first, then copy this pack into `packs/` and rescan:

```
cp -r apps/_examples/sky_log  apps/sky_log
cp -r packs/_examples/stargazing packs/stargazing
curl -X POST -H "X-API-Key: $KEY" http://127.0.0.1:8000/apps/rescan
```

`GET /apps/packs` then lists it, with a privacy summary **derived from its
apps' own declarations** — the pack author does not get to speak for somebody
else's app.
