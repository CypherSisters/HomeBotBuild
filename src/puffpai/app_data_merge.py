"""Non-destructive merge, drop-guard, versioning and backups for app documents.

``PUT /apps/<name>/data`` must not hand :meth:`AppData.set_data` whatever the
browser sent and let it replace the stored document wholesale. FreshTable's
page pushes only ``{pantry, shoppingList, mealPlan}``; the canonical store also
holds ``recipes``. So a family member opening the app in a second browser,
where the page seeds a demo pantry and auto-syncs 800 ms later, would
silently delete every saved recipe.

Three layers, in the order they run:

1. **Merge** — absent top-level keys are PRESERVED. A payload says what it
   knows; it does not say what does not exist. Sending ``null`` for a key is
   the explicit way to delete it.
2. **Drop guard** — even an explicit shrink is refused when a top-level list
   that had content would lose more than half of it, or all of it. The caller
   gets a 409 naming the counts and can repeat the write with the intent
   spelled out (``?allow_shrink=1``, or ``?replace=1`` for a true replace).
3. **Versioning + backups** — every merged write bumps ``data_version`` and
   rotates the previous document into ``backups/<app>.<n>.json`` (5
   generations, 0600), so a wipe that gets through is recoverable by hand.

Deliberately path-and-dict pure: it never opens the live document itself, so
the write helper in :mod:`puffpai.app_data` stays the single writer.
"""

import json
import os

#: Keys that are bookkeeping rather than household content. They are always
#: taken from the incoming document (or regenerated) rather than merged.
_BOOKKEEPING = ("last_updated", "data_version")

#: How many previous generations of a document to keep.
BACKUP_GENERATIONS = 5


class AppDataWouldDropData(Exception):
    """A merge would have destroyed a list the store already had content in.

    ``drops`` maps the top-level key to ``{"before": n, "after": m}`` so the
    API can hand the caller the counts instead of a bare refusal.
    """

    def __init__(self, app, drops):
        self.app = app
        self.drops = drops
        detail = ", ".join(
            f"{k}: {v['before']}->{v['after']}" for k, v in sorted(drops.items()))
        super().__init__(f"{app}: refusing to drop stored data ({detail})")


def find_drops(current, payload):
    """Top-level lists the payload would empty or more than halve.

    Only keys the payload actually carries are considered — an absent key is
    preserved by :func:`merge_document` and cannot be a drop. ``None`` counts
    as deleting the key, which for a non-empty list is the largest drop there
    is.
    """
    drops = {}
    if not isinstance(current, dict) or not isinstance(payload, dict):
        return drops
    for key, old in current.items():
        if key in _BOOKKEEPING or not isinstance(old, list) or not old:
            continue
        if key not in payload:
            continue
        new = payload[key]
        if new is None:
            after = 0
        elif isinstance(new, list):
            after = len(new)
        else:
            continue
        # "More than half" — a 7-entry list may fall to 4, not to 3.
        if after == 0 or after * 2 < len(old):
            drops[key] = {"before": len(old), "after": after}
    return drops


def merge_document(current, payload):
    """``current`` updated by ``payload``: absent keys kept, ``None`` deletes.

    Returns a new dict; neither argument is mutated.
    """
    merged = dict(current) if isinstance(current, dict) else {}
    if not isinstance(payload, dict):
        return merged
    for key, value in payload.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


def next_version(current):
    """One past the stored ``data_version`` (missing or junk counts as 0)."""
    try:
        return int((current or {}).get("data_version", 0)) + 1
    except (TypeError, ValueError):
        return 1


def backup_dir(path):
    """``backups/`` beside the document, created 0700 on first use."""
    directory = os.path.join(os.path.dirname(path), "backups")
    os.makedirs(directory, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    return directory


def rotate_backup(path, document):
    """Push ``document`` in at generation 1, ageing the rest out at 5.

    Best-effort by design: a household that cannot write a backup should still
    be able to write its shopping list, so every failure here is swallowed
    (the caller's write is the thing that must not fail).
    """
    try:
        app = os.path.splitext(os.path.basename(path))[0]
        directory = backup_dir(path)
        gen = lambda n: os.path.join(directory, f"{app}.{n}.json")  # noqa: E731
        oldest = gen(BACKUP_GENERATIONS)
        if os.path.exists(oldest):
            os.remove(oldest)
        for n in range(BACKUP_GENERATIONS - 1, 0, -1):
            if os.path.exists(gen(n)):
                os.replace(gen(n), gen(n + 1))
        target = gen(1)
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, default=str, ensure_ascii=False)
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
        return target
    except (OSError, TypeError, ValueError):
        return None
