#!/usr/bin/env python3
"""Apply a curation plan to a real Calibre library. Run ON the appliance:

    calibre-debug scripts/books/apply_curation.py -- \
        --library /var/lib/calibre/library --plan curation_plan.jsonl --dry-run

Then again without --dry-run. Needs Calibre 9.x (tested against 9.15's API
surface) and nothing from this repo: every decision was already made by
curation_report.py and is carried in the plan file, so this script has NO
imports from puffpai.* and never needs sys.path help.

What it changes, per plan row:

* ``#subjects``   <- the book's LoC subject strings (custom column, created
                     on first run as a tags-like multiple text column)
* ``tags``        <- the umbrella tags the book already has + its categories
* ``series`` / ``series_index`` / ``title``  <- comics only, and only when the
                     filename actually parsed into a series

It never touches authors, identifiers, formats or files, and it is idempotent:
a second run computes the same target values, sees no difference, and writes
nothing.
"""

from __future__ import annotations

import argparse
import json
import sys

# Kept in sync with src/puffpai/books/categories.py by hand; duplicated on
# purpose so this script has no repo imports (see module docstring).
UMBRELLA_TAGS = ("My Library", "Comics", "Project Gutenberg")
SUBJECTS_LABEL = "subjects"
SUBJECTS_KEY = "#" + SUBJECTS_LABEL
BATCH = 2000


def load_plan(path):
    rows = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def ensure_subjects_column(library_path):
    """Create #subjects if missing and return an open legacy db object.

    The custom column has to be created through the *legacy* database object
    (``calibre.library.db``) -- ``new_api`` has no create_custom_column -- and
    the library must then be reopened for the new column to exist in the
    field metadata.
    """
    from calibre.library import db as library_db

    legacy = library_db(library_path)
    existing = set(legacy.custom_field_keys())
    if SUBJECTS_KEY not in existing:
        print("creating custom column %s ..." % SUBJECTS_KEY)
        legacy.create_custom_column(
            label=SUBJECTS_LABEL,
            name="Subjects",
            datatype="text",
            is_multiple=True,  # behaves like tags
            display={"is_names": False},
        )
        # Reopen: the freshly created column is only visible to a new handle.
        try:
            legacy.close()
        except Exception:  # pragma: no cover - close is best effort
            pass
        legacy = library_db(library_path)
        if SUBJECTS_KEY not in set(legacy.custom_field_keys()):
            raise SystemExit("failed to create %s" % SUBJECTS_KEY)
    else:
        print("custom column %s already present" % SUBJECTS_KEY)
    return legacy


def current_values(api, field, book_ids):
    """{book_id: value} for one field, tolerant of API differences."""
    getter = getattr(api, "all_field_for", None)
    if getter is not None:
        try:
            return dict(getter(field, book_ids))
        except Exception:
            pass
    return {book_id: api.field_for(field, book_id) for book_id in book_ids}


def no_commas(values):
    """Calibre splits multi-value text on commas, so #subjects values carry
    none. The plan is already clean; this is belt and braces."""
    out = []
    for value in values or ():
        cleaned = " ".join(str(value).replace(",", " ").split())
        if cleaned and cleaned not in out:
            out.append(cleaned)
    return out


def as_tuple(value):
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(value)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", required=True, help="path to the Calibre library folder")
    parser.add_argument("--plan", required=True, help="curation_plan.jsonl from curation_report.py")
    parser.add_argument("--dry-run", action="store_true", help="show the first 20 changes, write nothing")
    parser.add_argument("--limit", type=int, default=0, help="only process the first N plan rows")
    args = parser.parse_args(argv)

    rows = load_plan(args.plan)
    if args.limit:
        rows = rows[: args.limit]
    print("plan rows: %d" % len(rows))

    if args.dry_run:
        # A dry run must not create the column either.
        from calibre.library import db as library_db

        legacy = library_db(args.library)
        has_column = SUBJECTS_KEY in set(legacy.custom_field_keys())
        print("custom column %s present: %s" % (SUBJECTS_KEY, has_column))
    else:
        legacy = ensure_subjects_column(args.library)
        has_column = True
    api = legacy.new_api

    known = set(api.all_book_ids())
    umbrella = set(UMBRELLA_TAGS)

    # Build the target value for every field, skipping books that already match.
    targets = {"tags": {}, SUBJECTS_KEY: {}, "series": {}, "series_index": {}, "title": {}}
    missing = 0
    shown = 0
    stats = {"tags": 0, SUBJECTS_KEY: 0, "series": 0, "title": 0}

    ids = [r["book_id"] for r in rows if r["book_id"] in known]
    missing = len(rows) - len(ids)
    cur_tags = current_values(api, "tags", ids)
    cur_subjects = current_values(api, SUBJECTS_KEY, ids) if has_column else {}
    cur_series = current_values(api, "series", ids)
    cur_index = current_values(api, "series_index", ids)
    cur_title = current_values(api, "title", ids)

    for row in rows:
        book_id = row["book_id"]
        if book_id not in known:
            continue
        existing_tags = as_tuple(cur_tags.get(book_id))
        keep = [t for t in existing_tags if t in umbrella]
        # Umbrella tags stay, categories follow, no duplicates, stable order.
        new_tags = list(keep)
        for name in row.get("categories") or []:
            if name not in new_tags:
                new_tags.append(name)
        subjects = no_commas(row.get("subjects"))

        changes = []
        if tuple(new_tags) != existing_tags:
            targets["tags"][book_id] = new_tags
            stats["tags"] += 1
            changes.append("tags %s -> %s" % (list(existing_tags), new_tags))
        if has_column and tuple(as_tuple(cur_subjects.get(book_id))) != tuple(subjects):
            targets[SUBJECTS_KEY][book_id] = subjects
            stats[SUBJECTS_KEY] += 1
            changes.append("%s -> %d subject(s)" % (SUBJECTS_KEY, len(subjects)))
        elif not has_column and subjects:
            changes.append("%s -> %d subject(s) (column not created yet)" % (SUBJECTS_KEY, len(subjects)))

        series = row.get("series")
        if series:
            index = row.get("series_index")
            if (cur_series.get(book_id) or None) != series:
                targets["series"][book_id] = series
                stats["series"] += 1
                changes.append("series -> %s" % series)
            if index is not None and float(cur_index.get(book_id) or 0.0) != float(index):
                targets["series_index"][book_id] = float(index)
                changes.append("series_index -> %s" % index)
            clean = row.get("clean_title")
            if clean and cur_title.get(book_id) != clean:
                targets["title"][book_id] = clean
                stats["title"] += 1
                changes.append("title -> %s" % clean)

        if args.dry_run and changes and shown < 20:
            shown += 1
            print("book %s: %s" % (book_id, "; ".join(changes)))

    print("\nwould change: tags %d, %s %d, series %d, title %d"
          % (stats["tags"], SUBJECTS_KEY, stats[SUBJECTS_KEY], stats["series"], stats["title"]))
    if missing:
        print("plan rows whose book_id is no longer in the library: %d" % missing)

    if args.dry_run:
        print("dry run: nothing written")
        return 0

    # Calibre silently rewrites a comma inside a tag to a semicolon, which is
    # why the category vocabulary is comma-free. Values are still always
    # passed as LISTS, never as a joined string, and the first tags batch is
    # read back and compared against the exact tags we asked for. Any
    # difference stops the run before it can damage 60k books.
    verify_id = next(iter(targets["tags"]), None)
    verify_want = list(targets["tags"][verify_id]) if verify_id is not None else None

    for field, mapping in targets.items():
        if not mapping:
            print("%s: nothing to do" % field)
            continue
        items = list(mapping.items())
        done = 0
        for start in range(0, len(items), BATCH):
            chunk = dict(items[start:start + BATCH])
            api.set_field(field, chunk)
            done += len(chunk)
            print("%s: %d/%d" % (field, done, len(items)))
            if field == "tags" and start == 0 and verify_id in chunk:
                got = list(as_tuple(api.field_for("tags", verify_id)))
                if sorted(got) != sorted(verify_want):
                    raise SystemExit(
                        "ABORT: book %s tags came back as %s, expected %s -- "
                        "Calibre split the values; fix before continuing."
                        % (verify_id, got, verify_want)
                    )
                print("tags verified on book %s: %s" % (verify_id, got))
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
