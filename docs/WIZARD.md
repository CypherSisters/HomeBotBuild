# The setup wizard (`puffpai setup`)

The wizard is a run of cards. Each card measures or asks, proposes, and writes
only once you accept. It writes the config, the Home Assistant config, the
Vault, your persona files and the build record, and it applies nothing to your
network.

Run it: `py -3 -m puffpai setup` (Windows) / `python -m puffpai setup` (Linux),
or open the dashboard, which sends you to `/onboarding` until setup is done.

Where it lives: the cards are packages in `src/puffpai/wizard/packages/`, run
in order by `src/puffpai/wizard/runner.py` (`SCHEDULE`). This build's own steps
(storage, household, Home Assistant) and the order they sit in come from
`src/puffpai/wizard/host.py` (`setup_schedule`). The packages come from
BuildBot, our bot-building tool, unchanged.

## The cards, in the order they run

| # | Card | Asks | Writes |
|---|---|---|---|
| 1 | Posture | Internet while you build? What may the finished bot reach? Which Bridge switches? | `egress_doors` in `config/puffpai_config.json` |
| 2 | This machine | Nothing, unless detection fails (`--stated-ram`). CPU, RAM, disk, GPU/VRAM, OS, and where Ollama answers | the `machine` section of the run record |
| 3 | Purpose | The bot's name and what it is for, in your words. The goal is matched against the starter catalog and the match is shown, not asked | the interview, `identity.name` |
| 4 | Its place | Whether the bot gets a page of its own, the id it lives under (proposed from the name you just typed), and the lists it keeps | `<data dir>/apps/<id>/app.json` + `<data dir>/packs/<id>/pack.json`, if you say yes |
| 5 | The brain | Which model. The catalog is fitted to this machine's RAM: a recommendation, the alternatives, and an honest download size. Downloads only if you say yes and the posture allows it | the choice; the runner writes `ollama.model`/`ollama.url` after the Vault card |
| 6 | The crew | Nothing. It counts the seven agents in `agents/registry.py` on this machine and shows who owns what | the `crew` section |
| 7 | Surfaces | Which pages load. A page not on the list is not loaded | the surface allowlist |
| 8 | Household | Your name, other household members | `profiles.json` (household store) |
| 9 | The Vault | A PIN, twice. The recovery code is shown once and you type it back | the encrypted vault |
| 10 | Storage | Four folders | `paths.*`, `media_server.media_mount` in the config |
| 11 | Sources | What the bot should learn from. A plan only; nothing is downloaded | the `knowledge` plan |
| 12 | Integrate | Nothing new: it fetches what the plan chose, indexes it, and says what really landed. A source over the size threshold is left for later, with its fetch command | knowledge packs, the `knowledge` block |
| 13 | The Bridge | A posture; what is at your network edge | `pending_apply`, the manifest |
| 14 | Home Assistant | Do you run one? Address, token, Zigbee hub | `config/home_assistant_config.json` |
| 15 | Tools | Home Assistant reconciled, plus what the bot can reach for and what it cannot. A keyed API is recorded as "declared, no key path yet" | the `tools` section |

Then four entries that ask nothing:

* **The manifest.** `<data dir>/config/build_manifest.json`, assembled from the
  sections above.
* **Context.** What the build told the model about itself: the name, the goal,
  your lists, read off disk. Read-only, and the same text reaches every chat
  and voice path.
* **Prove.** Checks what was just written: a reserved persona id, a persona
  nothing can show, packs chosen and never downloaded, a start command that
  does not exist on this machine. `puffpai prove`, once the bot is running,
  does the half this cannot.
* **Handoff.** How to start this install, the dashboard address, and every gap
  any card recorded, each with the one command that fixes it. The first device
  pairing code is printed once on the console and never stored in the run
  record.

Optional answers: every member after the owner, the goal (defaults to
HomeBot's), surface removals, the media folder (empty means "this household has
none" and is recorded as that), Home Assistant entirely, the edge admin URL,
the PIN (`--skip-vault` leaves the build unprotected and says so).

## The run record and resume

The run is written to `<config dir>/wizard_run.json` after every card, with
secrets dropped by key name and by value. A card that fails records a gap
instead of vanishing.

A setup that stopped part-way is offered back on its own screen before the
first card:

* **Continue.** What blocks is checked again first (the Vault on disk, the
  model in Ollama), then it picks up where it stopped.
* **Start over.** The old record is kept beside the new one, not deleted.
* `--yes` never resumes. It sets the old record aside and starts from the
  beginning, because nobody is there to choose.

## In the browser

`/onboarding` walks the same build step by step. `/onboarding/review` shows the
whole build on one screen: one row per card, in the runner's order (the screen
gets the order from `GET /wizard/cards`, it keeps no list of its own), each row
with a decision, a cost and a status.

* **Build it** stays disabled until every row that matters has been opened
  once. The button says how many are left.
* The total time never adds a row whose time is unknown; it says "time
  unknown" instead.
* A secret typed into a card lives only in the page until it is applied, then
  it is cleared.

The `/wizard/*` routes need a paired device, and the first-run code while setup
is not finished. The review screen is type-checked and its logic is tested by
`dashboard/scripts/wizard-review-proof.ts`. It opens in a browser, but it has not
been walked end to end there, and before the first-run code is entered it shows
only "This box would not say what it found".

## Its place: a goal we ship nothing for

When nothing in the catalog matches your goal, the wizard says "Nothing we ship
matches that yet" and offers a blank persona, scaffolded from your own words.

Saying yes writes two files and nothing else:

* `<data dir>/apps/<id>/app.json`: one declarative app. Its lists are the ones
  you name, its vocabulary comes from those names and the real words in your
  goal, and its page is the generic app page (`/dashboard/apps/<id>`).
* `<data dir>/packs/<id>/pack.json`: a one-app pack, so the persona has a name.

Both land under your **data directory** (`$PUFFPAI_DATA_DIR`, else the per-user
state folder), never in the checkout. Installing is rescanning: no script runs,
nothing is imported, and the manifest is checked by the real validator before
anything is written, so a refused persona leaves no half-written folder. A
reserved id (`apps`, `vault` and the like) is refused. The Surfaces card then
adds the app's own surface to the allowlist with a reason that names the app.

Scripted runs: `--scaffold` writes one whether or not the catalog matched, and
`--lists "tools,projects"` names its lists and implies `--scaffold`. A run with
`--yes` and neither flag scaffolds nothing.

## Personas and apps outside the wizard

```
puffpai persona new workshop --name "Workshop" --goal "track tools and projects"     --lists "tools,projects"
puffpai persona list        # the personas you have made, and where they live
puffpai apps list           # every installed app and pack, and both folders read
puffpai apps rescan         # re-read the folders; this IS the install mechanism
```

Once an app is installed, chat and voice can keep its lists with no model
running: *add a torque wrench to the tools list*, *what's on the tools list*,
*remove a torque wrench from the tools list*, *how many things are on the tools
list*. Nothing is reported as saved until the document has been read back off
disk with the item in it. A sentence that does not say which list gets asked
which one. The five shipped household apps keep their own phrasing: "add oat
milk to the shopping list" is still FreshTable's sentence.

## The Bridge card: your network edge

"What protects your network edge today?" has three answers and all three are
complete:

* **nothing**: no Pi-hole, no filtering router. The posture is your fence, and
  the Bridge zone is fully configured. The manifest records `edge.declared:
  true`, because not configured is not the same as configured to zero.
* **zone3**: HomeBot's own DNS sinkhole, Linux + Docker only, **opt-in**.
  `setup_ops.zone3_support()` decides whether this machine can run it,
  read-only: `sys.platform`, `shutil.which("docker")`, and a parse of `ss -lntu`
  / `netstat` output for a port-53 listener (never a bind; "unknown" is reported
  as unknown). Choosing it **applies nothing**. It writes a `pending_apply` item
  with the commands for you to run and starts in `monitor-first` mode: it
  watches, it does not block. Read [`ZONE3_SETUP.md`](ZONE3_SETUP.md) first,
  above all *Step 3: Test Safely (No Router Changes), Option A*, which changes
  nothing beyond your own machine.
* **external**: Pi-hole, AdGuard, a firewall appliance, a home lab. Recorded as
  data: a kind and, optionally, an admin URL. The URL must be a LAN or loopback
  address (checked offline with `puffpai.security.netaddr`); a hostname is
  accepted with a note saying it was not looked up. **It is never fetched.**

## `pending_apply`

Decisions the wizard made but will not apply itself, written to
`<data dir>/data/setup_pending_apply.json` and printed plainly on the closing
screen, commands and all. Nothing in it has happened. It holds: the Zone 3
commands, the consent-gate re-ask tier a Moat/Drawbridge posture implies, an
unnamed owner profile, and an unread vault recovery code from a `--yes` run.

## The manifest

`<data dir>/config/build_manifest.json`. No secret ever reaches it:
`build_manifest.sanitize_manifest` strips `_SECRET_KEYS` (the PIN, the recovery
code, the HA token, any `password`/`api_key`) on the way to disk.

`bridge.edge` = `{kind, declared, label, summary, external_kind, external_url,
external_url_note, contacted, zone3:{requested, supported, reasons[], mode,
commands[], doc, doc_sections[], applied}}`.

## The three zones: what a manifest can never turn off

`build_manifest._FORBIDDEN_KEYS` is the guard. A manifest, whether hand-edited,
imported from a bundle, or posted by a browser, may not carry
`disable_vault`/`vault_enabled`, `disable_consent`/`bypass_consent`/`skip_consent`,
`disable_gates`/`disable_action_gates`/`allow_unattended_actions`,
`disable_sentinel`, `disable_drawbridge`, `telemetry_enabled`/`enable_telemetry`/
`phone_home`, `disable_egress_gate`/`egress_gate_enabled`/`allow_all_egress`/
`egress_allow_all`/`bypass_egress`, or `allow_prompt_injection`. The Vault, the
consent gate, the action gates and zero telemetry are contracts, not settings.
The Bridge posture and the edge answer are policy within them.

## Flags

`--yes` takes every default and asks nothing (the recovery code is minted but
never printed, because an unattended run's output is a file; no source is
selected). `--dry-run` walks everything and writes no build record. Answer flags
skip the matching question:
`--build-network --run-egress --egress-door --stated-ram --ollama-url --model
--pull --name --bot-name --owner-name --member --goal --scaffold --lists
--add-surface --drop-surface --state-dir --knowledge-dir --vault-dir
--media-dir --zim-dir --skip-vault --vault-pin --vault-password
--bridge-posture --skip-bridge --has-firewall --has-managed-lan --edge
--edge-kind --edge-url --ha-url --ha-token --skip-ha --skip-knowledge
--skip-sourcing --no-suggestions --no-auto-index --fetch-source
--accept-source-size --own-data --skip-own-data`. A scripted run fetches a source
only with `--accept-source-size`.

`PUFFPAI_SETUP_STDIN_SECRETS=1` is **test-only**: the PIN and token prompts read
stdin instead of the console, so the interactive wizard can be scripted (on
Windows `getpass` reads the console device and ignores a pipe). It is implied
when stdin is not a TTY. Nothing is echoed either way.

## Re-running

There is no per-card flag; answer the ones you want and let the flags skip the
rest. The wizard is safe to re-run: it never re-seals an existing vault, and it
merges rather than replaces the config. Examples: change the Bridge only,
`puffpai setup --yes --bridge-posture moat --edge external --edge-kind pihole`;
change the edge answer only, `puffpai setup --yes --edge none`; move the vault
folder, `puffpai setup --yes --vault-dir /srv/homebot/vault`. Add `--dry-run`
first to see what it would do without writing.
