"""Roster definition + ``build_team`` factory for the product-agent team.

The roster encodes, for each agent, its class,
secret scope, model class, workflow family, and which embryo agent it evolves
from. :func:`build_team` constructs the bus, instantiates every entry, registers
them, and designates Sentinel as the policy reviewer (the veto).

The roster is the count. Nothing here, or in anything reading
:func:`roster_summary`, should be written against a fixed number of agents.

Agents are constructed cheaply — heavy engines (HABridge, DualZoneRAG, Node-RED,
DeviceSentinel) are built lazily on first use inside each agent — so building the
team is safe to do at startup behind a flag without paying for the whole stack.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

from dataclasses import dataclass

from puffpai.logging_config import get_logger
from puffpai.security.audit_log import AuditLog
from puffpai.security.secrets_provider import AgentScope, SecretsProvider

from .archivist import ArchivistAgent
from .base import BaseAgent
from .bus import AgentBus
from .caretaker import CaretakerAgent
from .concierge import ConciergeAgent
from .majordomo import MajordomoAgent
from .sentinel_agent import SentinelAgent
from .steward import StewardAgent
from .toolsmith import ToolsmithAgent

logger = get_logger(__name__)

#: Distinguishes "caller omitted this engine" from "caller passed None".
_BUILD_DEFAULT = object()


def _shared_vault_singleton():
    """Return the process-wide shared Vault, or None if unavailable.

    The Archivist's Zone-1 gate must VERIFY that a claimed vault session is
    actually unlocked (``vault.is_unlocked(session_id)``), which only works
    against the ONE Vault instance that holds the live sessions — the singleton
    ``api_server._get_vault()`` returns, the same one ``/vault/unlock`` calls
    ``authenticate()`` on. A fresh ``Vault()`` has no sessions, so it would make
    every ``is_unlocked`` check fail (a documented past gotcha); never build one
    here.

    The import is lazy and swallowed on failure: ``build_team`` is only ever
    called at runtime (via ``api_server._get_agent_team`` or
    ``agent_events.get_bus``), by which point ``api_server`` is already imported,
    so this is a cheap ``sys.modules`` lookup — and doing it lazily avoids a
    circular import at module load. If it cannot be resolved, the Archivist is
    left with ``vault=None`` and its gate stays fail-closed (Zone-2 only), i.e.
    exactly the pre-fix behavior — never an open Zone-1.
    """
    try:
        from puffpai.api_server import _get_vault
        return _get_vault()
    except Exception:
        return None


#: What a person is told each agent does: ``id -> (role, one-line blurb)``.
#: The one place this text lives; the wizard's Crew card reads it through
#: :class:`RosterEntry`. Edit freely: nothing routes on these strings. The
#: wording is the product's own, from the dashboard's agent roles and the crew
#: lines in ``puffpai.hatch``. A blurb does not repeat the agent's name, so an
#: agent renamed in the config (``agents.names``) never reads stale.
AGENT_ROLES: dict[str, tuple[str, str]] = {
    "concierge": ("Front desk and routing",
                  "Works the front desk. Everything you ask goes through it "
                  "first."),
    "steward": ("Home and tools",
                "Minds the house: the lights and the tools that move things."),
    "sentinel": ("Privacy and security",
                 "Keeps watch. Nothing changes in this house without its "
                 "review."),
    "archivist": ("Archive and knowledge",
                  "Keeps the archive: what we know, and what stays locked in "
                  "the Vault."),
    "majordomo": ("Orchestration and scheduling",
                  "Runs the schedule: plans, routines, and the long game."),
    "caretaker": ("System health and repairs",
                  "Looks after the box itself: health checks and quiet "
                  "repairs."),
    "toolsmith": ("Web and workspace tools",
                  "Runs the outside errands. Useful, and never handed the "
                  "keys."),
}


@dataclass(frozen=True)
class RosterEntry:
    """One row of the roster."""

    name: str
    cls: type[BaseAgent]
    scope: AgentScope
    model_class: str
    workflow: str
    evolves_from: str

    @property
    def display_name(self) -> str:
        """What a person sees; ``name`` stays the id everything routes on.

        Read from the name preset (``agents.preferences``), the one place names
        live, so the reviewer ``sentinel`` shows as Drawbridge and an owner's
        rename reaches the Crew card too.
        """
        from .preferences import display_name
        return display_name(self.name)

    @property
    def role(self) -> str:
        return AGENT_ROLES.get(self.name, ("", ""))[0]

    @property
    def blurb(self) -> str:
        return AGENT_ROLES.get(self.name, ("", ""))[1]


# The authoritative roster. Order is significant.
ROSTER: tuple[RosterEntry, ...] = (
    RosterEntry("concierge", ConciergeAgent, AgentScope.CONCIERGE,
                "7-8B resident + 70B escalation", "WF-1", "Cortex"),
    RosterEntry("steward", StewardAgent, AgentScope.STEWARD,
                "7-8B tool-calling", "WF-2", "Bridge"),
    RosterEntry("sentinel", SentinelAgent, AgentScope.SENTINEL,
                "small classifier + 70B incident analysis", "WF-3", "Sentinel"),
    RosterEntry("archivist", ArchivistAgent, AgentScope.ARCHIVIST,
                "embedder + 70B synthesis", "WF-4", "(new — split from Cortex)"),
    RosterEntry("majordomo", MajordomoAgent, AgentScope.MAJORDOMO,
                "70B planning + cheap task-workers", "WF-5", "(new — automation layer)"),
    RosterEntry("caretaker", CaretakerAgent, AgentScope.CARETAKER,
                "mostly no LLM; small model for summaries", "WF-6",
                "(new — watchdog)"),
    # WF-7 is the generic tool tier: universal composable tools a planner can
    # assemble into new behaviour, instead of a hand-coded intent per request.
    # It is a separate agent rather than more Steward surface because it reads
    # the public web, and that untrusted text must not execute inside the
    # process holding HA_TOKEN. Its own tools are deterministic — no model runs
    # inside it; the reasoning happens in the planner that calls it.
    RosterEntry("toolsmith", ToolsmithAgent, AgentScope.TOOLSMITH,
                "n/a — deterministic tools", "WF-7",
                "(new — generic tool tier)"),
)

REVIEWER_NAME = "sentinel"


def build_team(config=None, *, secrets: SecretsProvider | None = None,
               audit: AuditLog | None = None, engines: dict | None = None) -> AgentBus:
    """Construct the bus + every agent in :data:`ROSTER` and return the wired bus.

    Args:
        config: optional PuffPAIConfig (passed to the Caretaker for probe targets).
        secrets: optional shared :class:`SecretsProvider` (else each agent uses the
            module default). Scope enforcement is identical either way.
        audit: optional shared :class:`AuditLog` for the bus + security agents.
        engines: optional dict of pre-built engines for injection/testing, keyed
            by ``{"ha_bridge", "media_bridge", "rag", "vault", "automation_client",
            "device_sentinel", "concierge_engine", "http", "resolver",
            "workspace_root"}``. Anything omitted is built lazily by the owning
            agent on first use.

    Returns:
        A :class:`~puffpai.agents.bus.AgentBus` with every roster agent registered
        and Sentinel designated as reviewer.
    """
    engines = engines or {}
    audit = audit or AuditLog()
    bus = AgentBus(audit=audit, reviewer_name=REVIEWER_NAME)

    # The Concierge's engine follows the same rule as every other engine here:
    # omitted means "build the default", passed means "use exactly this".
    # ``.get()`` alone could not express that — it cannot tell an omitted key
    # from an explicit ``None`` — so a sentinel does, and ``None`` stays the way
    # a caller (or a test) asks for the engine-less routing-only Concierge.
    # Construction is cheap and makes no network call; every model call is
    # bounded and falls back to the deterministic path, so an unreachable
    # runtime degrades routing rather than breaking it.
    concierge_engine = engines.get("concierge_engine", _BUILD_DEFAULT)
    if concierge_engine is _BUILD_DEFAULT:
        from .engine import build_engine
        concierge_engine = build_engine()

    # The Archivist's Zone-1 gate needs a REAL vault to verify a session is
    # unlocked. Production callers (api_server._get_agent_team, agent_events.
    # get_bus) build the team with no engines, so an OMITTED "vault" key falls
    # back to the shared vault singleton — the one holding live sessions — rather
    # than leaving the Archivist vault-less (which fail-closes Zone-1 shut even
    # for a genuinely unlocked session). An explicit "vault" (including None,
    # which tests use to exercise the locked path) is honored as-is.
    vault_engine = engines.get("vault", _BUILD_DEFAULT)
    if vault_engine is _BUILD_DEFAULT:
        vault_engine = _shared_vault_singleton()

    agents: list[BaseAgent] = [
        ConciergeAgent(secrets=secrets, engine=concierge_engine),
        StewardAgent(secrets=secrets, ha_bridge=engines.get("ha_bridge"),
                     media_bridge=engines.get("media_bridge")),
        SentinelAgent(secrets=secrets, audit=audit,
                      device_sentinel=engines.get("device_sentinel")),
        ArchivistAgent(secrets=secrets, rag=engines.get("rag"),
                       vault=vault_engine, audit=audit),
        MajordomoAgent(secrets=secrets,
                       automation_client=engines.get("automation_client")),
        CaretakerAgent(secrets=secrets, config=config),
        # Engines are injectable here for the same reason as everywhere else,
        # but the Toolsmith's are the HTTP transport and the DNS resolver — the
        # two things its SSRF defences depend on — so tests exercise every
        # refusal path with no network and no name server.
        ToolsmithAgent(secrets=secrets, http=engines.get("http"),
                       resolver=engines.get("resolver"),
                       workspace_root=engines.get("workspace_root")),
    ]
    for agent in agents:
        bus.register(agent)
    bus.set_reviewer(REVIEWER_NAME)

    logger.info("product-agent team built: %s (reviewer=%s)",
                ", ".join(a.name for a in agents), REVIEWER_NAME)
    return bus


def roster_summary() -> list[dict]:
    """Serializable roster (for /agents/status and docs).

    ``name`` stays the internal id every caller routes on; ``display_name`` is
    what this build calls the agent. Additive on purpose — a UI may show the
    display name, but nothing looks an agent up by it.
    """
    from .preferences import display_name
    return [
        {
            "name": e.name,
            "display_name": display_name(e.name),
            "scope": e.scope.value,
            "model_class": e.model_class,
            "workflow": e.workflow,
            "evolves_from": e.evolves_from,
        }
        for e in ROSTER
    ]


__all__ = ["AGENT_ROLES", "ROSTER", "RosterEntry", "REVIEWER_NAME", "build_team", "roster_summary"]
