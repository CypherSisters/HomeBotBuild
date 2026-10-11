#!/usr/bin/env python3
"""Dry-run the library curation: classify every book and write a plan file.

Reads a *copy* of the Calibre metadata.db plus the Project Gutenberg catalog
CSV and the offline-pack popularity file, classifies each book with
``puffpai.books.classify``, parses the comics with ``puffpai.books.comics``,
prints a human review report, and writes ``curation_plan.jsonl`` -- one JSON
object per book -- for ``apply_curation.py`` to replay on the appliance.

This script never writes to the library database.

    python scripts/books/curation_report.py \
        --db /path/metadata.db --catalog /path/pg_catalog.csv \
        --popularity /path/popularity.json --out-plan /path/curation_plan.jsonl
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import os
import random
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src"))

from puffpai.books.categories import CATEGORIES, UMBRELLA_TAGS  # noqa: E402
from puffpai.books.classify import classify, is_fiction  # noqa: E402
from puffpai.books.comics import parse_comic  # noqa: E402

SAMPLE_SEED = 20260919


def calibre_safe(values):
    """Strip commas from multi-value text.

    Calibre splits multi-value text columns on commas, so no value may
    contain one ("Zola, Emile, 1840-1902" became three tags). Keep the
    " -- " subdivisions, de-duplicate, preserve order.
    """
    out = []
    for value in values:
        cleaned = " ".join(str(value).replace(",", " ").split())
        if cleaned and cleaned not in out:
            out.append(cleaned)
    return out


def load_catalog(path):
    """gutenberg id (str) -> {subjects, locc, bookshelves, title, authors}."""
    csv.field_size_limit(min(sys.maxsize, 2**31 - 1))
    catalog = {}
    with open(path, encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            text_id = (row.get("Text#") or "").strip()
            if not text_id:
                continue
            catalog[text_id] = {
                "title": row.get("Title") or "",
                "authors": row.get("Authors") or "",
                "subjects": [s.strip() for s in (row.get("Subjects") or "").split(";") if s.strip()],
                "locc": [s.strip() for s in (row.get("LoCC") or "").split(";") if s.strip()],
                "bookshelves": [s.strip() for s in (row.get("Bookshelves") or "").split(";") if s.strip()],
            }
    return catalog


def load_books(db_path):
    """Read every book with its tags, authors, identifiers and path."""
    con = sqlite3.connect("file:%s?mode=ro" % db_path.replace("?", "%3f"), uri=True)
    con.row_factory = sqlite3.Row
    books = {}
    for row in con.execute("SELECT id, title, path FROM books"):
        books[row["id"]] = {
            "book_id": row["id"],
            "title": row["title"] or "",
            "path": row["path"] or "",
            "tags": [],
            "authors": "",
            "gid": None,
        }
    for row in con.execute(
        "SELECT l.book AS book, t.name AS name FROM books_tags_link l JOIN tags t ON t.id = l.tag"
    ):
        book = books.get(row["book"])
        if book is not None:
            book["tags"].append(row["name"])
    authors = collections.defaultdict(list)
    for row in con.execute(
        "SELECT l.book AS book, a.name AS name FROM books_authors_link l JOIN authors a ON a.id = l.author"
    ):
        authors[row["book"]].append(row["name"])
    for book_id, names in authors.items():
        if book_id in books:
            books[book_id]["authors"] = " & ".join(names)
    for row in con.execute("SELECT book, type, val FROM identifiers"):
        book = books.get(row["book"])
        if book is None or not row["val"]:
            continue
        val = str(row["val"])
        if "gutenberg.org" in val:
            book["gid"] = val.rstrip("/").rsplit("/", 1)[-1]
        elif row["type"] in ("gutenberg", "pgid") and val.isdigit():
            book["gid"] = val
    con.close()
    return books


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--popularity", required=True)
    parser.add_argument("--out-plan", required=True)
    args = parser.parse_args(argv)
    # Titles carry stray private-use characters; never die on the console.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover - old interpreters
        pass

    catalog = load_catalog(args.catalog)
    with open(args.popularity, encoding="utf-8") as handle:
        popularity = json.load(handle)
    books = load_books(args.db)

    umbrella = set(UMBRELLA_TAGS)
    per_category = collections.Counter()
    two_cats = 0
    uncategorized_by_locc = collections.Counter()
    gutenberg_total = 0
    gutenberg_uncategorized = 0
    catalog_misses = 0
    history_fiction = 0
    scifi_science_pairs = 0
    biography_rows = []
    survival_rows = []
    categorized_samples = []
    uncategorized_samples = []
    comic_rows = []
    personal_rows = []
    plan_lines = []

    for book in books.values():
        tags = book["tags"]
        is_comic = "Comics" in tags
        is_gutenberg = "Project Gutenberg" in tags
        subjects = [t for t in tags if t not in umbrella]

        series = index = None
        clean_title = book["title"]
        categories = []
        # Gutenberg books carry the clean catalog subjects in the plan;
        # everything else carries its own non-umbrella tags.
        plan_subjects = subjects

        if is_comic:
            info = parse_comic(book["title"], book["path"])
            series, index, clean_title = info.series, info.index, info.clean_title
            categories = ["Comics"]
            comic_rows.append((series, index, book["title"]))
        else:
            meta = catalog.get(book["gid"] or "")
            if meta is None:
                if is_gutenberg:
                    catalog_misses += 1
                meta = {"subjects": subjects, "locc": [], "bookshelves": [], "authors": book["authors"]}
                # popularity.json still knows the LCC class for pack books.
                pop = popularity.get(book["gid"] or "")
                if pop and pop.get("lcc"):
                    meta["locc"] = [pop["lcc"]]
            else:
                # Classify from the CATALOG subjects: the book's own Calibre
                # tags are the same strings mangled by comma splitting.
                meta = dict(meta)
                plan_subjects = meta["subjects"]
            fiction = is_fiction(
                subjects=meta["subjects"], locc=meta["locc"], bookshelves=meta["bookshelves"]
            )
            categories = classify(
                title=book["title"],
                authors=book["authors"] or meta.get("authors", ""),
                subjects=meta["subjects"],
                locc=meta["locc"],
                bookshelves=meta["bookshelves"],
            )
            if fiction and "History" in categories:
                history_fiction += 1
            if "Science Fiction" in categories and "Science & Nature" in categories:
                scifi_science_pairs += 1
            if "Biography & Letters" in categories:
                biography_rows.append((book["title"], meta["subjects"]))
            if "Survival & Preparedness" in categories:
                survival_rows.append((book["title"], meta["subjects"]))
            if is_gutenberg:
                gutenberg_total += 1
                if not categories:
                    gutenberg_uncategorized += 1
                    for raw in meta["locc"] or ["(no LoCC)"]:
                        uncategorized_by_locc[raw] += 1
                    uncategorized_samples.append((book["title"], meta["subjects"], meta["locc"]))
                else:
                    categorized_samples.append((book["title"], categories))
            else:
                personal_rows.append((book["title"], categories, subjects[:3]))

        for name in categories:
            per_category[name] += 1
        if len(categories) == 2:
            two_cats += 1

        plan_lines.append(
            {
                "book_id": book["book_id"],
                "categories": categories,
                "subjects": calibre_safe(plan_subjects),
                "series": series,
                "series_index": index,
                "clean_title": clean_title,
            }
        )

    with open(args.out_plan, "w", encoding="utf-8") as handle:
        for line in plan_lines:
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")

    rng = random.Random(SAMPLE_SEED)
    print("=== books ===")
    print("total books        : %d" % len(books))
    print("gutenberg books    : %d" % gutenberg_total)
    print("gutenberg not in catalog: %d" % catalog_misses)
    print("plan rows written  : %d -> %s" % (len(plan_lines), args.out_plan))

    print("\n=== count per category ===")
    for name in CATEGORIES:
        print("%-40s %6d" % (name, per_category[name]))
    unused = [n for n in per_category if n not in CATEGORIES]
    if unused:
        print("!! categories outside the vocabulary: %s" % unused)

    print("\nbooks with 2 categories : %d" % two_cats)
    pct = 100.0 * gutenberg_uncategorized / gutenberg_total if gutenberg_total else 0.0
    print("gutenberg uncategorized : %d (%.2f%%)" % (gutenberg_uncategorized, pct))

    print("\n=== acceptance checks ===")
    print("History AND is_fiction                  : %d" % history_fiction)
    print("(Science Fiction, Science & Nature) pairs: %d" % scifi_science_pairs)
    print("Biography & Letters total                : %d" % len(biography_rows))
    print("Survival & Preparedness total            : %d" % len(survival_rows))

    print("\n=== 12 random Biography & Letters members (catalog subjects) ===")
    for title, subs in rng.sample(biography_rows, min(12, len(biography_rows))):
        print("%-46s | %s" % (title[:46], "; ".join(subs[:4]) or "-"))

    print("\n=== 15 random Survival & Preparedness members ===")
    for title, subs in rng.sample(survival_rows, min(15, len(survival_rows))):
        print("%-46s | %s" % (title[:46], "; ".join(subs[:4]) or "-"))

    print("\n=== uncategorized by LoCC class (top 25) ===")
    for raw, count in uncategorized_by_locc.most_common(25):
        print("%-12s %6d" % (raw, count))

    print("\n=== 40 random categorized samples ===")
    for title, cats in rng.sample(categorized_samples, min(40, len(categorized_samples))):
        print("%-60s -> %s" % (title[:60], ", ".join(cats)))

    print("\n=== 25 random uncategorized samples ===")
    for title, subs, locc in rng.sample(uncategorized_samples, min(25, len(uncategorized_samples))):
        print("%-50s | LoCC=%s | %s" % (title[:50], ",".join(locc) or "-", "; ".join(subs[:4]) or "-"))

    print("\n=== comics ===")
    parsed = [r for r in comic_rows if r[0]]
    print("comics: %d, parsed into a series: %d (%.1f%%)" % (
        len(comic_rows), len(parsed), 100.0 * len(parsed) / len(comic_rows) if comic_rows else 0.0))
    by_series = collections.defaultdict(list)
    for series, index, _title in parsed:
        by_series[series].append(index)
    print("%-46s %5s %8s %8s" % ("series", "count", "first", "last"))
    for series in sorted(by_series, key=lambda s: (-len(by_series[s]), s)):
        idx = sorted(by_series[series])
        print("%-46s %5d %8s %8s" % (series[:46], len(idx), idx[0], idx[-1]))
    unparsed = [r[2] for r in comic_rows if not r[0]]
    print("unparsed comic titles (%d): %s" % (len(unparsed), unparsed[:20]))

    print("\n=== personal (non-comic) books: %d ===" % len(personal_rows))
    for title, cats, subs in sorted(personal_rows):
        print("%-55s -> %-45s | %s" % (title[:55], ", ".join(cats) or "(none)", "; ".join(subs)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
