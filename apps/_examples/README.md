# Example apps

Everything under `apps/_examples/` is **not installed**. The registry skips any
folder whose name starts with `_` or `.` (`app_registry.load_manifests`), so
these sit in the tree as something to copy, not something that runs.

## Installing one

```
cp -r apps/_examples/sky_log apps/sky_log
curl -X POST -H "X-API-Key: $KEY" http://127.0.0.1:8000/apps/rescan
```

That is the whole install. No script runs, nothing is imported, nothing is
executed — a manifest is parsed as JSON and used as lists and strings. After the
rescan the app is listed by `GET /apps/installed`, and its data document is
served immediately:

```
GET  /apps/sky_log/data        -> the "data.default" shape from the manifest
PUT  /apps/sky_log/data        -> merge your keys into it
GET  /apps/sky_log/updated     -> last_updated, for change polling
```

The first `GET` creates `<state>/app_data/sky_log.json` holding exactly the
declared default. `POST /apps/<id>/action` is **not** generic: those actions are
methods on the shipped apps' own store, and an installed app has none.

## Writing your own

* `id` — 2–32 characters, lowercase, starts with a letter, `[a-z0-9_]`. It is
  the URL segment and the data-file name, so it is validated before either use.
* `surfaces` — a **closed set** (see `app_registry.surface_taxonomy`). An app
  declares membership of a surface this build already knows how to gate; it
  cannot invent one. This is why the example is an observing log rather than the
  workshop inventory it started as: nothing in the taxonomy yet describes
  "an inventory of things in a room", and claiming `daily.kitchen` for a box of
  drill bits would be a manifest that lies about what it is. Add the surface to
  the taxonomy first, then the app.
* `data.default` — a starting *shape*, not a dataset (32 KB ceiling). Ship
  content as knowledge, under that gate.
* `doors` — where the app wants to reach, declared in the owner's words. A
  declared door is a claim, never a grant.
* Nothing here may name code to run; a manifest with such a field is refused.

A malformed manifest is skipped and reported by `GET /apps/installed` under
`problems`, with the folder named — the other apps keep working.
