"""What this build's knowledge was promised, and what it actually has.

The wizard records two different kinds of wish in the build manifest and neither
of them was ever reconciled against the disk:

* ``knowledge.sources`` - the vetted registry rows that were *offered*, and the
  ones that were *fetched*. A fetched row lands as a file under the data dir
  (or in the ZIM folder), with a provenance sidecar beside it. Nothing read that
  back, so a source could be downloaded and still be invisible everywhere.
* ``knowledge.packs`` - catalog ids the owner picked during the build interview
  (``pack:wikipedia``). These are wishes, not downloads: choosing one moves no
  bytes at all. The manifest recorded the wish, the dashboard counted indexed
  libraries, and the gap between the two was never stated anywhere.

This module is the reconciliation, and it is read-only: it opens no socket,
downloads nothing, and indexes nothing. It reads the manifest, the shipped
registry, the download folders, and the pack index, and gives every source and
pack exactly one of four statuses:

``indexed``
    Bytes are on this disk and the knowledge store has them.
``fetched_not_indexed``
    Bytes are on this disk and the knowledge store does not. For a ``docs`` row
    that is the permanent, honest answer rather than a backlog: those rows are
    landing pages and API catalogs, so they are reported as ``live_source`` -
    something the bot can cite and reach, not a shelf of indexed documents.
``selected_not_downloaded``
    The owner chose it and nothing was downloaded. Carries the size and the
    exact command that would download it.
``offered``
    The wizard proposed it and the owner did not take it. A suggestion.

Copyright (c) Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from puffpai.logging_config import get_logger

logger = get_logger(__name__)

SCHEMA = "knowledge_build_sources/v1"

STATUS_INDEXED = "indexed"
STATUS_FETCHED_NOT_INDEXED = "fetched_not_indexed"
STATUS_SELECTED_NOT_DOWNLOADED = "selected_not_downloaded"
STATUS_OFFERED = "offered"

#: Bytes that can be indexed, versus a reference the bot reaches live. A ``docs``
#: registry row is a landing page - NASA's API catalog, the Astropy docs index -
#: and calling one "indexed knowledge" would be a lie told by a progress bar.
FORM_DOCUMENTS = "documents"
FORM_LIVE_SOURCE = "live_source"

#: The only command in this build that downloads a vetted source. There is no
#: `puffpai sources fetch`; the wizard's sourcing step is the download path, and
#: the size acknowledgement is a separate flag on purpose.
FETCH_COMMAND = "puffpai setup --yes --fetch-source {source_id} --accept-source-size"

#: Interview pack ids mapped onto the registry rows that would actually satisfy
#: them. Only mappings we are confident about: a pack with no row here is
#: reported as unmapped rather than pointed at an approximation. Each entry
#: matches the ``source_ids`` of the same pack in ``config/capability_catalog.json``.
PACK_SOURCE_IDS: dict[str, tuple[str, ...]] = {
    "pack:wikipedia": ("kiwix_wikipedia_en_nopic",),
    "pack:wikivoyage": ("kiwix_wikivoyage_maxi",),
    "pack:wikispecies": ("kiwix_wikispecies_maxi",),
    "pack:stackexchange_science": (
        "kiwix_astronomy_stackexchange",
        "kiwix_space_stackexchange",
    ),
    "pack:wikem": ("kiwix_wikem_maxi",),
    "pack:medicine_libretexts": ("kiwix_libretexts_medicine",),
    "pack:water_treatment": ("kiwix_zimgit_water",),
    "pack:knots": ("kiwix_zimgit_knots",),
    "pack:ready_gov": ("kiwix_ready_gov_en",),
    "pack:openstreetmap_wiki": ("kiwix_osm_wiki_maxi",),
    "pack:wikipedia_maxi": ("kiwix_wikipedia_en_maxi",),
    "pack:wikipedia_mini": ("kiwix_wikipedia_en_mini",),
    "pack:wikipedia_top": ("kiwix_wikipedia_en_top",),
    "pack:wikipedia_simple": ("kiwix_wikipedia_simple",),
    "pack:wikipedia_history": ("kiwix_wikipedia_history",),
    "pack:wikipedia_physics": ("kiwix_wikipedia_physics",),
    "pack:mediawiki": ("kiwix_mediawiki",),
    "pack:wikisource": ("kiwix_wikisource",),
    "pack:wiktionary": ("kiwix_wiktionary",),
    "pack:wikibooks": ("kiwix_wikibooks",),
    "pack:wikiversity": ("kiwix_wikiversity",),
    "pack:wikivoyage_nopic": ("kiwix_wikivoyage_nopic",),
    "pack:wikispecies_nopic": ("kiwix_wikispecies_nopic",),
    "pack:wikem_nopic": ("kiwix_wikem_nopic",),
    "pack:mdwiki": ("kiwix_mdwiki",),
    "pack:openstreetmap_wiki_nopic": ("kiwix_osm_wiki_nopic",),
    "pack:explainxkcd": ("kiwix_explainxkcd",),
    "pack:rationalwiki": ("kiwix_rationalwiki_maxi",),
    "pack:rationalwiki_nopic": ("kiwix_rationalwiki_nopic",),
    "pack:incognito_cat": ("kiwix_incognito_cat",),
    "pack:gutenberg": ("kiwix_gutenberg",),
    "pack:survivor_library": ("kiwix_survivor_library",),
    "pack:ifixit": ("kiwix_ifixit",),
    "pack:diy_stackexchange": ("kiwix_diy_stackexchange",),
    "pack:electronics_stackexchange": ("kiwix_electronics_stackexchange",),
    "pack:physics_stackexchange": ("kiwix_physics_stackexchange",),
    "pack:gardening_stackexchange": ("kiwix_gardening_stackexchange",),
    "pack:softwareengineering_stackexchange": (
        "kiwix_softwareengineering_stackexchange",
    ),
    "pack:security_stackexchange": ("kiwix_security_stackexchange",),
    "pack:mechanics_stackexchange": ("kiwix_mechanics_stackexchange",),
    "pack:outdoors_stackexchange": ("kiwix_outdoors_stackexchange",),
    "pack:cooking_stackexchange": ("kiwix_cooking_stackexchange",),
    "pack:engineering_stackexchange": ("kiwix_engineering_stackexchange",),
    "pack:ham_stackexchange": ("kiwix_ham_stackexchange",),
    "pack:homebrew_stackexchange": ("kiwix_homebrew_stackexchange",),
    "pack:medicalsciences_stackexchange": (
        "kiwix_medicalsciences_stackexchange",
    ),
    "pack:sustainability_stackexchange": (
        "kiwix_sustainability_stackexchange",
    ),
    "pack:ready_gov_es": ("kiwix_ready_gov_es",),
    "pack:urban_prepper": ("kiwix_urban_prepper",),
    "pack:canadian_prepper_winter": ("kiwix_canadian_prepper_winter",),
    "pack:military_medicine": ("kiwix_military_medicine",),
    "pack:food_preparation": ("kiwix_zimgit_food",),
    "pack:field_medicine": ("kiwix_zimgit_medicine",),
    "pack:post_disaster": ("kiwix_zimgit_post_disaster",),
    "pack:ted_coronavirus": ("kiwix_ted_coronavirus",),
    "pack:ted_democracy": ("kiwix_ted_democracy",),
    "pack:ted_human_rights": ("kiwix_ted_human_rights",),
    "pack:ted_justice_system": ("kiwix_ted_justice_system",),
    "pack:ted_law": ("kiwix_ted_law",),
    "pack:ted_maps": ("kiwix_ted_maps",),
    "pack:ted_military": ("kiwix_ted_military",),
    "pack:ted_plants": ("kiwix_ted_plants",),
    "pack:ted_solar_energy": ("kiwix_ted_solar_energy",),
    "pack:ted_surveillance": ("kiwix_ted_surveillance",),
    "pack:ted_water": ("kiwix_ted_water",),
    "pack:prunelle_books": ("kiwix_prunelle_books",),
}

SYNC_COMMAND = "puffpai knowledge sync"


def _size_label(size_mb: Any) -> str:
    """Plain words for a download size. Unknown stays unknown."""
    try:
        value = float(size_mb)
    except (TypeError, ValueError):
        return "large download"
    if value <= 0:
        return "large download"
    if value >= 1024:
        return f"about {value / 1024:.1f} GB"
    return f"about {value:.0f} MB"


def _load_registry(registry: Optional[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    if registry is None:
        try:
            from puffpai import source_registry
            registry = source_registry.load_registry()
        except Exception as exc:  # noqa: BLE001 - a missing registry is not fatal here
            logger.warning("Could not load the source registry: %s", exc)
            return {}
    rows = registry.get("sources") if isinstance(registry, dict) else None
    out: dict[str, dict[str, Any]] = {}
    for row in rows or []:
        if isinstance(row, dict) and row.get("id"):
            out[str(row["id"])] = row
    return out


def _sources_dir(data_dir: Optional[str]) -> str:
    if data_dir:
        return os.path.join(data_dir, "sources")
    try:
        from puffpai.config import get_config
        return os.path.join(get_config().DATA_DIR, "sources")
    except Exception:  # noqa: BLE001
        return ""


def _zim_dir(zim_dir: Optional[str]) -> str:
    if zim_dir is not None:
        return zim_dir
    try:
        from puffpai.config import get_config
        return str(get_config().knowledge.zim_dir or "")
    except Exception:  # noqa: BLE001
        return ""


def on_disk_sources(*directories: str) -> dict[str, dict[str, Any]]:
    """Source ids whose artifact is present here, from the provenance sidecars.

    The sidecar is the join between "the registry says this exists" and "this
    machine has it": :mod:`puffpai.source_fetch` writes one beside every file it
    downloads. A sidecar whose artifact has since been deleted does not count -
    that is a record of a download, not a download.

    Windows note: paths are rebuilt from the directory being scanned and the
    sidecar's own filename, never from the ``path`` recorded inside it, so a
    record written on another machine (or before a data dir moved) still
    resolves here.
    """
    from puffpai import source_fetch

    found: dict[str, dict[str, Any]] = {}
    for directory in directories:
        if not directory or not os.path.isdir(directory):
            continue
        for record in source_fetch.provenance_records(directory):
            source_id = str(record.get("source_id") or "")
            filename = os.path.basename(str(record.get("filename") or ""))
            if not source_id or not filename:
                continue
            path = os.path.join(directory, filename)
            if not os.path.exists(path):
                continue
            found[source_id] = {
                "filename": filename,
                "directory": directory,
                "bytes": int(record.get("bytes") or 0),
                "fetched_at": str(record.get("fetched_at") or ""),
                "revoked_at": record.get("revoked_at"),
            }
    return found


#: What a pack record's ``status`` says when the pack is fully in the knowledge
#: store. Named here rather than imported: this build's store is the FTS5 shard
#: set, and the word is part of the record shape callers hand in, not a detail
#: of any one indexer.
STATUS_COMPLETE = "complete"


def _pack_records(pack_records: Optional[list[dict]],
                  zim_dir: str = "") -> list[dict]:
    """The packs this machine has learned, as records.

    A caller may hand in its own list (the tests, and anything that has already
    read the store). Otherwise this observes the disk the way HomeBotBuild's
    knowledge store actually works: a ``.zim`` with an FTS shard beside it is
    indexed, and one without is not. Never raises - an unreadable folder means
    nothing is known to be indexed, which is the honest floor.
    """
    if pack_records is not None:
        return list(pack_records)
    if not zim_dir:
        return []
    try:
        from puffpai.knowledge import fts_index
        from puffpai.knowledge.discovery import discover_zim_files

        fts_dir = fts_index.resolve_fts_dir()
        records: list[dict] = []
        for path in discover_zim_files(zim_dirs=[zim_dir]):
            shard = fts_index.shard_path_for(path, fts_dir) if fts_dir else ""
            if not shard or not os.path.exists(shard):
                continue
            provenance = _provenance_source_id(path)
            records.append({"status": STATUS_COMPLETE,
                            "filename": os.path.basename(path),
                            "path": path, "source_id": provenance})
        return records
    except Exception as exc:  # noqa: BLE001 - no readable store means nothing indexed
        logger.debug("No pack records available: %s", exc)
        return []


def _provenance_source_id(path: str) -> str:
    """The source id a downloaded pack carries in its sidecar, or ""."""
    try:
        from puffpai import source_fetch
        record = source_fetch.read_provenance(path) or {}
        return str(record.get("source_id") or "")
    except Exception:  # noqa: BLE001 - provenance is a bonus, never a blocker
        return ""


def _indexed_ids_and_files(records: list[dict]) -> tuple[set[str], set[str]]:
    """(source ids, filenames) the pack records report as fully indexed."""
    ids: set[str] = set()
    names: set[str] = set()
    for record in records:
        if str(record.get("status")) != STATUS_COMPLETE:
            continue
        source_id = str(record.get("source_id") or "")
        if source_id:
            ids.add(source_id)
        filename = os.path.basename(str(record.get("filename") or ""))
        if filename:
            names.add(filename.lower())
    return ids, names


#: ``knowledge.sources.skipped`` values that mean **nothing was ever offered**:
#: the owner skipped knowledge setup (``--skip-knowledge``) or skipped the
#: sourcing step itself. Neither run showed the owner a catalog, so neither run
#: has anything the owner "chose". The other reasons - ``non_interactive``,
#: ``offline_build`` - are runs where a plan exists and was not carried out, and
#: those stay reported exactly as they are.
SOURCING_NEVER_OFFERED = ("skipped", "owner")


def sourcing_skipped(manifest: Optional[dict[str, Any]]) -> str:
    """The skip reason when this build never offered a source, else ``""``.

    One reader for the whole product. It is what keeps ``prove``, this module's
    ``source_status`` and the closing screen from telling an owner who skipped
    knowledge setup that packs "were chosen during the build and never
    downloaded" - nothing was chosen, because nothing was offered.
    """
    knowledge = (manifest or {}).get("knowledge") or {}
    block = knowledge.get("sources") or {}
    reason = str(block.get("skipped") or "") if isinstance(block, dict) else ""
    return reason if reason in SOURCING_NEVER_OFFERED else ""


def _manifest_sources(manifest: Optional[dict[str, Any]]) -> tuple[list[str], list[str], list[str]]:
    """(offered ids, fetched ids, pack ids) as the manifest recorded them.

    A build whose sourcing was skipped reports no offered ids and no packs:
    what is on disk is still read and still reported, because that is an
    observation, but nothing is carried forward as "chosen".
    """
    knowledge = (manifest or {}).get("knowledge") or {}
    block = knowledge.get("sources") or {}
    skipped = bool(sourcing_skipped(manifest))
    offered = ([] if skipped
               else [str(i) for i in (block.get("offered") or []) if i])
    fetched: list[str] = []
    for entry in block.get("fetched") or []:
        if isinstance(entry, dict):
            if entry.get("id"):
                fetched.append(str(entry["id"]))
        elif entry:
            fetched.append(str(entry))
    packs = ([] if skipped
             else [str(p) for p in (knowledge.get("packs") or []) if p])
    return offered, fetched, packs


def _source_item(source_id: str, entry: dict[str, Any], status: str,
                 *, selected_by: list[str], disk: Optional[dict[str, Any]],
                 pack_index_note: str = "") -> dict[str, Any]:
    kind = str(entry.get("kind") or "")
    form = FORM_LIVE_SOURCE if kind == "docs" else FORM_DOCUMENTS
    item: dict[str, Any] = {
        "id": source_id,
        "label": str(entry.get("label") or source_id),
        "kind": kind or "unknown",
        "form": form,
        "status": status,
        "domain": str(entry.get("domain") or ""),
        "license": str(entry.get("license") or ""),
        "license_url": str(entry.get("license_url") or ""),
        "commercial_use": bool(entry.get("commercial_use", False)),
        "what_it_is": str(entry.get("what_it_is") or ""),
        "source_url": str(entry.get("source_url") or ""),
        "size_mb": entry.get("size_mb"),
        "size_label": _size_label(entry.get("size_mb")),
        "selected_by": list(selected_by),
        "command": "",
        "note": "",
    }
    if status == STATUS_SELECTED_NOT_DOWNLOADED:
        item["command"] = FETCH_COMMAND.format(source_id=source_id)
        item["note"] = (
            f"Chosen during the build. Nothing was downloaded: this is "
            f"{item['size_label']}."
        )
    elif status == STATUS_OFFERED:
        item["command"] = FETCH_COMMAND.format(source_id=source_id)
        item["note"] = "Suggested during the build. Not chosen, not downloaded."
    elif status == STATUS_FETCHED_NOT_INDEXED and form == FORM_LIVE_SOURCE:
        item["note"] = (
            "A live source, not a shelf of documents: the address and what it "
            "covers are on file, and nothing was indexed."
        )
    elif status == STATUS_FETCHED_NOT_INDEXED:
        item["command"] = SYNC_COMMAND
        item["note"] = pack_index_note or (
            "Downloaded and waiting. It is indexed on the next start, or now "
            "with the command."
        )
    if pack_index_note:
        # "The record says it was downloaded and it is not here" beats every
        # other reading of fetched_not_indexed, so it is the last word.
        item["note"] = pack_index_note
        item["command"] = ""
    if disk:
        item["filename"] = disk.get("filename", "")
        item["bytes"] = disk.get("bytes", 0)
        item["fetched_at"] = disk.get("fetched_at", "")
    return item


def source_status(
    manifest: Optional[dict[str, Any]] = None,
    *,
    registry: Optional[dict[str, Any]] = None,
    data_dir: Optional[str] = None,
    zim_dir: Optional[str] = None,
    pack_records: Optional[list[dict]] = None,
) -> dict[str, Any]:
    """Every source and pack this build knows about, with one honest status each.

    Reads only: the manifest, the shipped registry, the two download folders,
    and the pack index. Never raises - a missing manifest is a build that chose
    nothing, which is a real answer rather than an error.
    """
    if manifest is None:
        try:
            from puffpai import build_manifest
            manifest = build_manifest.read_manifest()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not read the build manifest: %s", exc)
            manifest = None

    rows = _load_registry(registry)
    offered, fetched, packs = _manifest_sources(manifest)

    sources_dir = _sources_dir(data_dir)
    zim_folder = _zim_dir(zim_dir)
    disk = on_disk_sources(sources_dir, zim_folder)

    records = _pack_records(pack_records, zim_folder)
    indexed_ids, indexed_files = _indexed_ids_and_files(records)

    # Which registry rows each chosen pack would need.
    selected_by: dict[str, list[str]] = {}
    unmapped_packs: list[str] = []
    for pack in packs:
        mapped = PACK_SOURCE_IDS.get(pack)
        if not mapped:
            unmapped_packs.append(pack)
            continue
        for source_id in mapped:
            selected_by.setdefault(source_id, []).append(pack)

    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    ordered = list(dict.fromkeys(
        list(disk.keys()) + fetched + list(selected_by.keys()) + offered))
    for source_id in ordered:
        if source_id in seen:
            continue
        seen.add(source_id)
        entry = rows.get(source_id) or {"id": source_id}
        present = disk.get(source_id)
        on_this_disk = present is not None
        kind = str(entry.get("kind") or "")
        is_live = kind == "docs"

        if on_this_disk or source_id in fetched:
            if not is_live and (
                    source_id in indexed_ids
                    or (on_this_disk
                        and str(present.get("filename", "")).lower() in indexed_files)):
                # Chunks are in the knowledge store. That stays true even if the
                # pack file has since been deleted from the folder.
                status = STATUS_INDEXED
            elif not on_this_disk:
                # The manifest says it was fetched and the file is not here:
                # deleted, or the data dir moved. Not indexed either way.
                status = STATUS_FETCHED_NOT_INDEXED
            elif is_live:
                status = STATUS_FETCHED_NOT_INDEXED
            elif source_id in indexed_ids or \
                    str(present.get("filename", "")).lower() in indexed_files:
                status = STATUS_INDEXED
            else:
                status = STATUS_FETCHED_NOT_INDEXED
        elif source_id in selected_by:
            status = STATUS_SELECTED_NOT_DOWNLOADED
        else:
            status = STATUS_OFFERED

        note = ""
        if status == STATUS_FETCHED_NOT_INDEXED and not on_this_disk:
            note = ("The build record says this was downloaded, and the file is "
                    "not in the knowledge folders on this machine.")
        items.append(_source_item(source_id, entry, status,
                                  selected_by=selected_by.get(source_id, []),
                                  disk=present, pack_index_note=note))

    for pack in unmapped_packs:
        items.append({
            "id": pack,
            "label": pack.split(":", 1)[-1].replace("_", " ").title(),
            "kind": "pack",
            "form": FORM_DOCUMENTS,
            "status": STATUS_SELECTED_NOT_DOWNLOADED,
            "domain": "",
            "license": "",
            "license_url": "",
            "commercial_use": False,
            "what_it_is": "",
            "source_url": "",
            "size_mb": None,
            "size_label": "large download",
            "selected_by": [pack],
            "command": SYNC_COMMAND,
            "note": ("Chosen during the build. This build has no vetted "
                     "download for it: put the .zim in the knowledge folder, "
                     "then run the command."),
        })

    counts = {
        STATUS_INDEXED: 0,
        STATUS_FETCHED_NOT_INDEXED: 0,
        STATUS_SELECTED_NOT_DOWNLOADED: 0,
        STATUS_OFFERED: 0,
    }
    for item in items:
        counts[item["status"]] = counts.get(item["status"], 0) + 1

    return {
        "schema": SCHEMA,
        "items": items,
        "counts": counts,
        "packs_chosen": list(packs),
        "zim_dir": zim_folder,
        "sources_dir": sources_dir,
    }


__all__ = [
    "SCHEMA",
    "STATUS_INDEXED",
    "STATUS_FETCHED_NOT_INDEXED",
    "STATUS_SELECTED_NOT_DOWNLOADED",
    "STATUS_OFFERED",
    "FORM_DOCUMENTS",
    "FORM_LIVE_SOURCE",
    "FETCH_COMMAND",
    "PACK_SOURCE_IDS",
    "on_disk_sources",
    "source_status",
]
