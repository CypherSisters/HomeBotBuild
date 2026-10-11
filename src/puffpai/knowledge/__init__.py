"""
PuffPAI Knowledge Package

Custom knowledge ingestion and retrieval system.
All code in this package is original -- no GPL dependencies.
"""

from .zim_reader import ZIMReader
from .ingestion import KnowledgeIngestion
from .registry import DocumentRegistry
from .discovery import discover_zim_files, get_zim_metadata, discover_from_config
from .fts_index import (
    FTSBuilder,
    FTSSearcher,
    library_id_for,
    resolve_fts_dir,
    search_fulltext,
)

__all__ = [
    "ZIMReader",
    "KnowledgeIngestion",
    "DocumentRegistry",
    "discover_zim_files",
    "get_zim_metadata",
    "discover_from_config",
    "FTSBuilder",
    "FTSSearcher",
    "library_id_for",
    "resolve_fts_dir",
    "search_fulltext",
]
