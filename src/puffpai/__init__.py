"""
PuffPAI - Privacy-Forward Personal AI

A local, offline AI assistant with smart home integration,
RAG knowledge base, and RLHF learning capabilities.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

__version__ = "1.0.0"
__author__ = "Cypher Sisters"
__email__ = "team@cyphersisters.dev"

#: The one name every outside request from this build carries. Several free
#: services (MusicBrainz, Nominatim, the US weather service, Wikipedia, Open
#: Food Facts, Crossref, Open Library) ask for a descriptive one; it names the
#: product and where to read about it, and nothing about the household.
USER_AGENT = (f"PuffPAI-HomeBot/{__version__} "
              f"(+https://github.com/CypherSisters/HomeBotBuild)")

# Lazy imports -- heavy modules loaded only when accessed.
# This allows importing lightweight submodules (config, security, etc.)
# without pulling in the full app and all its dependencies.


def __getattr__(name):
    if name == "DualZoneRAG":
        from .knowledge.rag import DualZoneRAG
        return DualZoneRAG
    if name == "AssistantTools":
        from .assistant_tools import AssistantTools
        return AssistantTools
    raise AttributeError(f"module 'puffpai' has no attribute {name!r}")


__all__ = [
    "DualZoneRAG",
    "AssistantTools",
]
