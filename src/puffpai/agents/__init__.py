"""PuffPAI product-agent team (six agents, hub-and-spoke + policy sidecar).

Implements PUFFPAI_PRODUCT_AGENT_TEAM_PLAN: Concierge (WF-1), Steward (WF-2),
Sentinel (WF-3), Archivist (WF-4), Majordomo (WF-5), Caretaker (WF-6). Agents
compose the existing engines (agent_graph, ha_bridge, media_bridge,
device_sentinel, vault, rag, user_memory, automation_client) rather than replacing them.

Public entry point::

    from puffpai.agents import build_team
    bus = build_team()
    reply = bus.send(envelope, to="concierge")

Every state-changing envelope is structurally veto-reviewed by Sentinel inside
the bus, and every send is audited. See :mod:`puffpai.agents.bus`.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

from .base import BaseAgent
from .bus import AgentBus
from .envelope import Budget, BudgetExceeded, Envelope, ReviewVerdict
from .registry import ROSTER, REVIEWER_NAME, RosterEntry, build_team, roster_summary

__all__ = [
    "AgentBus",
    "BaseAgent",
    "Budget",
    "BudgetExceeded",
    "Envelope",
    "ReviewVerdict",
    "ROSTER",
    "REVIEWER_NAME",
    "RosterEntry",
    "build_team",
    "roster_summary",
]
