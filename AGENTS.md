# AGENTS.md

Operating guide for AI coding agents (and humans) changing HomeBotBuild. Load it
at the start of a session. The reference architecture is
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md); the user-facing overview is
[`README.md`](README.md). When this file and the code disagree, the code wins:
fix this file in the same change.

## 1. What this repository is

HomeBotBuild is HomeBot for an ordinary Windows or Linux PC: a local-first
household assistant (Python backend, Next.js dashboard, optional voice) plus a
setup wizard that builds a bot from the owner's stated goal. The wizard's card
packages are vendored from **BuildBot**, the open-source base (the three-zone,
seven-agent build; every build starts at the wizard). HomeBot is the first full
build. HomeBot Founders Edition, the full build for a Jetson AGX Orin, lives in
a sister repository with its own guide.

Every PuffPAI has three zones:

* **The Vault**: personal data, encrypted, caught automatically and kept out of
  the AI's memory; even the AI needs the owner's permission to look.
* **The Brain**: local models, agents, tools and knowledge.
* **The Bridge**: the only way to the outside. Every way out has a name and a
  switch, and the Bridge ships closed.

## 2. Promises to the owner (your change must keep all of them)

1. **Local first.** Chat, apps, knowledge and the wizard work with no internet
   once a model is present. No cloud model is called.
2. **Zero telemetry, no subscriptions.** Nothing phones home, ever.
3. **The Bridge ships closed.** Every registered outbound connection defaults
   shut and has its own switch; the shipped config opens none.
4. **The Vault is the owner's.** Encrypted at rest, PIN-gated, and a turn that
   carries vault content is never saved to chat history.
5. **Humans consent, agents do not.** State changes pass Drawbridge (internal
   id `sentinel`); consent is recorded only from a person.
6. **The wizard applies only what the owner accepted**, writes an honest
   record (the build manifest), and says what it could not do instead of
   pretending.
7. **Nothing is written into the checkout.** State and secrets live in the
   per-user data directory.
8. **Honest reporting.** Status, capabilities and egress pages describe what
   the code does, not what it hopes to do.

## 3. Repository map

| Path | What it holds |
|---|---|
| `src/puffpai/api_server.py` | Flask backend: every HTTP route and the `before_request` gates (large; grep for `@app.route('/<prefix>`) |
| `src/puffpai/cli.py`, `setup_cli.py`, `prove_cli.py` | `puffpai` verbs; the terminal wizard conversation |
| `src/puffpai/wizard/` | Wizard engine: `runner.py` (schedule, run record), `service.py` (browser face), `host.py` (shim that keeps vendored packages byte-identical), `cards.py`, `manifest.py`, `packages/<skill>/` (the 12 BuildBot cards), `CONVERTING.md` |
| `src/puffpai/setup_ops.py` | The one implementation of every setup action (vault seal, posture, storage, sources); called by the CLI, the cards and `/setup/*` |
| `src/puffpai/build_manifest.py` | The build record and its contract guard |
| `src/puffpai/prove.py`, `prove_live.py` | Static and live proof of a build |
| `src/puffpai/security/` | Vault, egress registry and ledger, consent, setup code, endpoint gate, PII classifiers, zones |
| `src/puffpai/agents/` | The seven agents, `bus.py` (veto), `envelope.py` (budgets), `planning_loop.py`, `dispatcher.py` |
| `src/puffpai/assistant_tools.py` | `AssistantTools`: the LLM tool layer (LangChain `@tool` functions) |
| `src/puffpai/capability_registry.py` | Declared capabilities, their authority tools and routing words |
| `src/puffpai/knowledge/` | ZIM reader, FTS5 shards, ChromaDB RAG, document registry |
| `src/puffpai/ingestion.py` | Upload pipeline with PII screening into the Vault |
| `src/puffpai/source_registry.py`, `source_fetch.py` | Vetted knowledge sources and the only wizard download path |
| `src/puffpai/app_registry.py`, `persona_scaffold.py` | App and pack manifests; owner persona scaffolding |
| `src/puffpai/skills/` | Sandboxed, consent-installed skills |
| `src/puffpai/ha_bridge.py` | Every Home Assistant call, with the action gates |
| `src/puffpai/voice/` | Optional wake word, STT, TTS loop |
| `src/puffpai/zone3/`, `firewall_manager.py` | Optional network edge (Linux, opt-in) |
| `src/puffpai/persona.txt` | Persona prompt, `{ai_name}` placeholder; shipped assistant name "PuffPAI" |
| `dashboard/` | Next.js app; `middleware.ts` (pairing gate), `app/api/pai/[...path]/route.ts` (keyed proxy), `app/_lib/server/` (pairing, proxy policy), `app/_lib/services/` (typed clients) |
| `apps/`, `packs/` | Shipped app and pack manifests; `_examples/` shows the format |
| `config/` | Shipped defaults (`puffpai_config.json`, `source_registry.json`, catalogs); copied into the data dir on a normal install |
| `tests/` | pytest suite; `tests/golden/` wizard goldens |
| `install.ps1`, `install.sh` | Installers; `systemd/`, `docker-compose.*.yml`, `searxng/`, `nodered_flows/` optional services |
| `scripts/gen_egress_doc.py` | Regenerates `docs/WHAT_CAN_LEAVE_YOUR_HOUSE.md` from the registry |

## 4. Install and run

Prerequisites: Python 3.10+, [Ollama](https://ollama.com/download), Node 20.19+
only for the dashboard. Details: [`docs/INSTALL_WINDOWS.md`](docs/INSTALL_WINDOWS.md),
[`docs/INSTALL_LINUX.md`](docs/INSTALL_LINUX.md), [`docs/HARDWARE.md`](docs/HARDWARE.md).

Windows (PowerShell):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 -WithDashboard   # -WithVoice -WithDev -DryRun ...
.\venv\Scripts\Activate.ps1
puffpai setup                       # terminal wizard
python start_api.py                 # backend on 127.0.0.1:8000
cd dashboard; npm run start         # dashboard on 127.0.0.1:3003, /onboarding is the browser wizard
puffpai prove                       # once the backend runs: the live half of the proof
```

Linux:

```bash
bash install.sh --with-dashboard     # --with-voice --with-dev --with-services [--enable] --dry-run ...
source venv/bin/activate
puffpai setup
python start_api.py
(cd dashboard && npm run start)
puffpai prove
```

The installers create `./venv`, `pip install` the checkout, mint an API key into
`<data dir>/homebot.env` (and `dashboard/.env.local` with `--with-dashboard`),
and never install Ollama or pull a model unless asked (`-YesInstallOllama`,
`-Pull TAG` / `--yes-install-ollama`, `--pull TAG`).

Other verbs: `puffpai setup --yes` (defaults, never resumes), `--dry-run`,
`puffpai setup-code` (reprint the first-run code, local only), `puffpai chat`,
`puffpai serve`, `puffpai persona new|list`, `puffpai apps list|rescan`,
`puffpai skills submit|list|status`. Full wizard flags: [`docs/WIZARD.md`](docs/WIZARD.md).
A browser on another device must first pair with the dashboard (a pairing code
from an already-paired device, or `node scripts/show-pairing-code.js` run in
`dashboard/`) and then enter the first-run setup code.

## 5. Testing

Always isolate the suite from the machine's real install: import-time code
resolves the API key and state directories.

```bash
# Git Bash or Linux, from the repository root
export PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1
export PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring   # never touch the OS keyring
export PUFFPAI_API_KEY=test-key-not-a-secret                  # throwaway key
export PUFFPAI_DATA_DIR="$(mktemp -d)" PUFFPAI_STATE_DIR="$(mktemp -d)"
python -m pytest -q -p no:cacheprovider tests/test_wizard_*.py
```

```powershell
$env:PYTHONPATH="src"; $env:PYTHON_KEYRING_BACKEND="keyring.backends.null.Keyring"
$env:PUFFPAI_API_KEY="test-key-not-a-secret"
$env:PUFFPAI_DATA_DIR=(New-Item -ItemType Directory -Force "$env:TEMP\pp-test\data").FullName
$env:PUFFPAI_STATE_DIR=(New-Item -ItemType Directory -Force "$env:TEMP\pp-test\state").FullName
python -m pytest -q -p no:cacheprovider (Get-ChildItem tests\test_wizard_*.py).FullName
```

* **CI** (`.github/workflows/ci.yml`) runs on Linux, Python 3.10 and 3.12:
  `tests/test_wizard_*.py tests/test_setup_api_*.py tests/test_release_*.py
  tests/test_config.py tests/test_household.py` with `-x --timeout=300`, plus
  `npx tsc --noEmit` and `npm run build` in `dashboard/`. Run that subset before
  you finish.
* **The full suite is large** (over 15,000 tests) and has a known set of
  failures (see the README's test status). Do not expect zero. Run the files
  your change touches **before and after** and compare: a failure that existed
  before is not yours; a new one is.
* `tests/conftest.py` opens exactly two switches for the suite
  (`TEST_OPEN_DOORS = {"web_fetch", "web_search"}`). Do not widen it.
* Dashboard: `npx tsc --noEmit` in `dashboard/`, and the plain-node tests under
  `dashboard/app/**/__tests__/*.test.mjs` (`npm run test:pairing`,
  `test:proxy`, `test:proxy-headers`, `test:nokey`, `test:bundle`, ...).
* Tests must be order-independent and must not reach the network.

## 6. Contracts and invariants

Each line: the rule, where it is enforced, what pins it. Breaking one is a
security or honesty regression, not a style issue.

1. **The Bridge ships closed.** All seventeen built-in entries in `DOORS` default
   shut; resolution is env, then owner overlay (`<data>/state/egress_doors.json`),
   then config `egress_doors`, then default; an unreadable overlay or junk value
   means shut. *Enforced:* `security/egress_doors.py` (`egress_allowed`,
   `_resolve_flag`). *Pinned:* `tests/test_release_doors_shut.py`, the egress
   registry tests.
2. **One way out.** Backend code reaches the internet only through
   `egress_doors.egress_request`/`egress_get`/`app_egress_request`, or a call
   site that asks `egress_allowed`/`require_open` before any socket (web search,
   web fetch, model pull, embedding download). Hosts must be declared, resolve
   public, and the connection is pinned to the checked address. *Pinned:*
   `test_no_outbound_call_site_outside_the_registry` and
   `test_every_wan_hostname_in_the_tree_belongs_to_a_door` (they scan
   `src/puffpai` Python only). Known exceptions are listed in
   ARCHITECTURE section 9; do not add to them.
3. **Every egress decision is recorded**, allowed and refused, with host, path
   and byte count and never a query string, body or token.
   *Enforced:* `security/egress_ledger.py`.
4. **Loopback by default.** Backend binds `127.0.0.1` unless
   `PUFFPAI_API_BIND` says otherwise; dashboard `npm run start` binds
   `127.0.0.1:3003`. *Enforced:* `start_api.py`, `security/netaddr.py`,
   `dashboard/package.json`. *Pinned:* `tests/test_release_bind.py`.
5. **API key on every route** except `_PUBLIC_ENDPOINTS`; loopback-only
   exemptions live in `security/endpoint_gate.LOCAL_ONLY_ENDPOINTS`. The key
   never enters the checkout or the browser bundle, and a missing key is a
   refusal to start, not a generated one. *Enforced:* `api_server._check_api_key`,
   `_load_or_create_api_key`. *Pinned:* `tests/test_release_api_key.py`,
   `dashboard/app/_lib/__tests__/no-browser-api-key.test.mjs`,
   `dashboard/scripts/check-no-secret-in-bundle.js`.
6. **First-run setup code.** While setup is incomplete, a non-loopback caller
   needs `X-Setup-Code` for state-changing `/setup/*` and for every method under
   `/wizard`, `/build/prove`, `/build/self`, plus persona writes. The code is
   shown only on the console, never logged, never in the manifest, has no HTTP
   route, rotates after five wrong answers with growing lockout, and is
   destroyed by `setup_ops.mark_complete` (every completion path).
   *Enforced:* `security/setup_code.py`, `api_server._enforce_setup_code`.
   *Pinned:* `tests/test_setup_code_gate.py`, `tests/test_wizard_setup_code_gate.py`.
7. **Post-setup vault gate.** After completion, writes under `/setup`, `/wizard`
   and `/persona` (except `POST /persona/preview`) need a live vault session in
   `X-Vault-Token` (409 otherwise); `POST /setup/vault` is refused outright.
   *Enforced:* `api_server._enforce_post_setup_gate`, `_setup_gate`.
   *Pinned:* `tests/test_setup_post_complete_gate.py`.
8. **Identity contract, dashboard to backend.** The proxy builds upstream
   headers from scratch: it relays only `X-Vault-Token` (on `vault`, `setup`,
   `wizard`, `persona`) and `X-Setup-Code` (on `setup`, `wizard`, `persona`,
   `build/prove`, `build/self`); it sets `X-PuffPAI-Client-Addr: unknown`, and
   exactly one of `X-Puff-Device-Id` (HMAC of the verified pairing cookie) or
   `X-Puff-Anonymous`. The backend believes a device id only from loopback or a
   `PUFFPAI_TRUSTED_PROXY_IPS` peer; anonymous callers get a single-use session
   and no vault answers. *Enforced:* `dashboard/app/_lib/server/proxyHeaders.ts`
   (`upstreamHeaders`), `api_server._get_chat_client_id`,
   `_setup_client_is_local`. *Pinned:* `proxy-headers.test.mjs`,
   `tests/test_chat_session_identity.py`, `tests/test_chat_anonymous_session.py`.
9. **Pairing decides who, the allowlist decides what.** `middleware.ts` and
   `requirePairing` gate proxied routes (`PUFF_PAIRING_ENFORCE`, default
   `knowledge`, which still gates the whole wizard surface and persona writes);
   `PROXY_ALLOWED_PREFIXES` 404s anything else. *Pinned:* `pairing-gate.test.mjs`,
   `wizard-gate.test.mjs`, `proxy-guard.test.mjs`.
10. **Vault crypto and privacy.** AES-256-GCM; random 32-byte data key wrapped
    by an scrypt key from the PIN, recovery code as a second wrap; sessions
    expire; a vault-unlocked chat window is per device key. Turns that carry
    vault content are marked private and not persisted. *Enforced:*
    `security/vault.py`, `api_server._consume_vault_turn_private`,
    `assistant_tools.VAULT_PRIVATE_TOOLS`. *Pinned:* `tests/test_vault_encryption.py`,
    `tests/test_vault_*_private.py`, `tests/test_setup_api_vault.py`.
11. **PII screening.** Chat turns are redacted before storage; uploads are
    screened before indexing and refused (nothing indexed) when high or
    critical PII meets a locked vault. *Enforced:* `api_server._prepare_user_message`,
    `security/scrub_and_vault.py`, `ingestion.PiiVaultRefusal`.
    *Pinned:* `tests/test_scrub_and_vault.py`, the unified-knowledge tests.
12. **Structural veto.** Every `state_changing` envelope is reviewed by the
    registered reviewer (`sentinel`) before delivery; no reviewer, a crash or an
    unknown verdict is a deny. Budgets default to `allow_egress=False`.
    *Enforced:* `agents/bus.py`, `agents/sentinel_agent.py`, `agents/envelope.py`.
    *Pinned:* `tests/test_agent_team.py`, `tests/test_sentinel_veto_shape.py`.
13. **Consent and action gates.** Per-device consent defaults pending; consent
    requests close only from a person (UI route or chat tools that act on an
    already-open request); safety-critical HA domains need the in-process
    `ha_bridge.HUMAN_CONFIRMED` token (a JSON `true` is refused); untargeted and
    broadcast service calls are refused. *Enforced:* `security/consent_gate.py`,
    `security/consent_requests.py`, `ha_bridge.py`. *Pinned:*
    `tests/test_consent_gate.py`, `tests/test_consent_decision_body.py`,
    `tests/test_ha_bridge_confirmation_gate.py`, `tests/test_ha_untargeted_gate.py`,
    `tests/test_ha_broadcast_gate.py`.
14. **The build manifest is the record, not a switchboard.** `write_manifest`
    always sanitizes: `_FORBIDDEN_KEYS` (contract switches) and `_SECRET_KEYS`
    are stripped wherever they appear; the `contracts` block is regenerated; no
    field is ever concatenated into a prompt as an instruction.
    *Enforced:* `build_manifest.py`. *Pinned:* `tests/test_wizard_buildbot.py`,
    `tests/test_wizard_zones.py`, `tests/test_wizard_golden.py`.
15. **Wizard runner rules.** One writer per manifest section; reserved sections
    (`run`, `posture`, `egress`, `brain`, `prove`) only from shipped packages;
    only Brain and Secure may stop a build; the run record is scrubbed of
    secrets; cards carry no secret values. *Enforced:* `wizard/runner.py`,
    `wizard/manifest.py`, `wizard/cards.py`. *Pinned:* `tests/test_wizard_*.py`.
16. **Apps and packs are data.** `app.json`/`pack.json` are parsed and
    validated, never imported or executed; code-shaped keys (`handler`,
    `module`, `exec`, `script`, ...) are refused; a declared host is a claim
    until registered as an `app:<id>:<n>` switch. *Enforced:* `app_registry.py`.
    *Pinned:* `tests/test_persona_scaffold.py`, `tests/test_app_registry_staleness.py`,
    the app-contract tests.
17. **Skills run only as approved bytes** in a bubblewrap jail whose only way
    out is its own `skill:<id>:0` switch, keyed to the approved host set.
    *Enforced:* `skills/`. *Pinned:* `tests/test_skills_*.py`.
18. **Knowledge sources come only from the registry**, need a license, a
    posture that allows online sourcing, an acknowledged size and the
    `knowledge_pack_downloads` switch open; a Kiwix pack follows only Kiwix's
    redirect chain and must match Kiwix's published sha256; fetched content is
    data, never prompt text. *Enforced:* `source_registry.py`,
    `source_fetch.py`, `wizard/integrate_fetch.py`. *Pinned:*
    `tests/test_wizard_sources.py`, `tests/test_wizard_consent.py`,
    `tests/test_knowledge_build_sources.py`, `tests/test_source_fetch_kiwix.py`.
19. **Capabilities are honest.** Every declared tool exists; intent lists stay
    in sync. *Enforced:* `capability_registry.validate`. *Pinned:*
    `tests/test_capability_contract.py`, `tests/test_skills_registry.py`.
20. **State never lands in the checkout.** Paths go through
    `config.resolve_state_dir` / `statedir.resolve_runtime_dir`.
    *Pinned:* `tests/test_release_state_paths.py`.

## 7. Conventions

* Python 3.10+, Black line length 100, type hints on signatures. Optional
  dependencies (voice, OCR, chromadb extras) are imported lazily inside the
  function that needs them; their absence disables one surface only.
* Comments and docstrings explain what the code does and why, in the present
  tense. No history, dates, ticket ids or names.
* Refuse, do not degrade silently: a refusal returns a sentence a person can
  act on, and never echoes a PIN, code or token back.
* Fail closed on ambiguity (unreadable file, junk flag, missing header).
* User-facing text says "switch", "open", "closed" for the Bridge.
* Internal agent ids (`sentinel`, `concierge`, ...) are what code routes on;
  display names come from `agents/preferences.py` and are cosmetic.
* Installer console strings are ASCII only.
* Dashboard: browser code calls same-origin `/api/pai/...` through
  `app/_lib/services/*.ts`; no `NEXT_PUBLIC_*` secret; the automation service is
  `automationService.ts` (proxy prefix `automation`, backend
  `/automation/status|trigger/<name>|flows`, engine Node-RED).

## 8. Extending

[`docs/EXTENDING.md`](docs/EXTENDING.md) has the owner-facing recipes
(documents, app or persona, skills, agent names). The checklists below are for
changing the code.

**New app** (`apps/<id>/app.json`, schema `puffpai_app/v1`; example
`apps/_examples/sky_log`): id `^[a-z][a-z0-9_]*$` and not reserved
(`persona_scaffold.is_reserved_id`); `data.default` shape; `vocabulary.keywords`
that do not collide with core phrasing; `surfaces` from the closed taxonomy;
`doors` block with `purpose` and `data_sent_plain` (empty `hosts` if none);
`credential.kind` of `none|api_key|oauth2`; `requires`. Check: validates via
`app_registry.validate_manifest`, `puffpai apps rescan` shows it, the app page
renders at `/dashboard/apps/<id>`, no code-shaped keys.

**New pack** (`packs/<id>/pack.json`, `puffpai_pack/v1`; example
`packs/_examples/stargazing`): lists installed app ids only; carries no data,
vocabulary or code. Check: `app_registry.validate_pack`.

**New skill**: owner-authored; three files (`manifest.json`, `skill.py`,
`tests_fixtures.json`), see EXTENDING section C. Code changes to `skills/` must
keep: approval of exact bytes, no network except via the broker, public hosts
only, Windows refusal without bubblewrap. Run `tests/test_skills_*.py`.

**New assistant tool**: add a `@tool` inside `AssistantTools.create_tools`;
declare it in `capability_registry.CAPABILITIES` (authority or `also_tools`,
`keywords`, owning `agent`, `surfaces`; `dispatch_phrases` only if unambiguous);
if it can return vault content add it to `VAULT_PRIVATE_TOOLS`; if it changes
state route it through the Steward on the bus or `ha_bridge` with its gates; if
it needs the internet, see "outbound connection". Check:
`capability_registry.validate(live_tool_names)` is empty.

**New capability**: a `Capability` row with one authority tool; if it is a
Steward intent, keep `planning_loop.CATALOGUE`, `steward.STATE_CHANGING_INTENTS`
and `concierge.RESOLVABLE_INTENTS` consistent (`tests/test_capability_contract.py`).

**New wizard card** (`src/puffpai/wizard/packages/<id>/`, read
`wizard/CONVERTING.md`): `manifest.json` (`puffpai_package/v1`, one `writes`
section, `reads`, `egress`, `estimate`, empty `secrets`) and `skill.py` with
`propose` (read-only), `apply` (writes only its section), `verify` (observes
the world). Add a `Step` to `runner.SCHEDULE` whose `requires` keys are written
earlier. Keep vendored packages byte-identical to BuildBot: put host-specific
behaviour in `wizard/host.py`. Update `tests/golden/` deliberately.

**New knowledge source**: a row in `config/source_registry.json` with `id`,
`label`, `kind` (`zim|corpus|docs`), `domain`, `tags`, `license` (required),
`license_url`, `source_url`, `filename`, `size_mb`, `what_it_is`, `pin` and
`pin_reason`. No source code change and no new fetch path. A Kiwix pack also
gets a `knowledge_pack` row in `config/capability_catalog.json` (`available`,
size, license and `source_ids`), the same entry in
`knowledge/build_sources.PACK_SOURCE_IDS`, and a line in
[`docs/KNOWLEDGE_PACKS.md`](docs/KNOWLEDGE_PACKS.md); `tests/test_knowledge_pack_catalog.py`
checks that they agree.

**New outbound connection**: add a `Door` to `egress_doors.DOORS` (`id`,
`hosts`, `purpose`, `data_sent`, `config_flag`, `env_var`, `consent_kind`,
`enforcement`, `settings_hint`, `third_party` when someone else receives it,
`default=False`); call it through `egress_request`/`egress_get`; if a library
opens its own sockets, call `require_open` before importing it and add the id
to `_FLAG_HONOURED_EXTERNAL`; keep it absent from the shipped config;
regenerate `docs/WHAT_CAN_LEAVE_YOUR_HOUSE.md` with
`python scripts/gen_egress_doc.py`; add a test that a shut switch opens no
socket. The dashboard server must not grow new outbound fetches.

## 9. Never do this

* Never open a socket, import an HTTP client for a public host, or add a
  hostname outside the egress registry.
* Never ship a switch open, add a default-open flag, or treat an unreadable
  file as consent.
* Never log, echo, persist or return a PIN, recovery code, setup code, API key
  or token; never add a route that returns the setup code.
* Never relay a client-supplied `X-Puff-Device-Id`, `X-Puff-Anonymous`,
  `X-PuffPAI-Client-Addr`, `Authorization` or cookie through the proxy.
* Never ship a non-empty default for `PUFFPAI_TRUSTED_PROXY_IPS` or a non-zero
  default for `api_server.trusted_proxy_hops`.
* Never let a model, a manifest field, an app declaration or fetched content
  become an instruction, or grant consent on a person's behalf.
* Never bypass the bus or `ha_bridge` for a state change, or pass
  `HUMAN_CONFIRMED` without real evidence of a person.
* Never write state, secrets or generated files into the checkout, or use the
  real OS keyring or data dir in tests.
* Never delete or weaken a test to make it pass; never claim a failing test is
  someone else's without comparing before and after.
* Never describe the Linux firewall backend as shipped or on: it is opt-in and
  off by default.

## 10. More reading

[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md), [`docs/WIZARD.md`](docs/WIZARD.md),
[`docs/EXTENDING.md`](docs/EXTENDING.md), [`docs/CONFIG.md`](docs/CONFIG.md),
[`docs/WHAT_CAN_LEAVE_YOUR_HOUSE.md`](docs/WHAT_CAN_LEAVE_YOUR_HOUSE.md),
[`SECURITY.md`](SECURITY.md), [`CONTRIBUTING.md`](CONTRIBUTING.md).
