# Extending HomeBot

Four things you can add to a running box, with the command that does it and what
actually happens; where a path has a limit, the limit is stated, not implied.
`export KEY=...` with your API key; `puffpai` means `py -3 -m puffpai`.

## A. Add documents

<!-- proven by tests/test_unified_knowledge_search.py::TestBothStoresAreOneTruth
     and ::TestPiiRoutingIsUnchanged -->

```bash
curl -X POST localhost:8000/ingest/upload -H "X-API-Key: $KEY" \
     -F file=@garage_manual.txt -F auto_apply=false
curl "localhost:8000/knowledge/search?q=garage+door+opener" -H "X-API-Key: $KEY"
curl "localhost:8000/knowledge/search/titles?q=garage" -H "X-API-Key: $KEY"
```

Or drop the file on **Dashboard > Knowledge**, which posts the same route. The
text is indexed **before the response returns**, no background pass, no rescan,
no restart, and is then findable through every route: `/knowledge/search`,
`/ingest/search`, `/knowledge/search/titles`, the dashboard search box, and the
citations chat puts under an answer. **No LLM is required** to add or find a
document; title and full-text search need no embedding model.

PII routing, because it changes what is stored: the text is screened before
anything is indexed; values found go to the vault and the **REDACTED** text stays
searchable (a policy becomes `Policy for [REDACTED_NAME], premium [REDACTED]`,
and that is what search returns); and if the vault is locked the upload is
**refused** and is then searchable nowhere, not through the other route either.

## B. Add an app or a persona

<!-- proven by tests/test_persona_scaffold.py: ::test_persona_new_writes_a_valid_app_and_pack_into_the_owners_dirs,
     ::test_the_cli_verbs_print_what_they_made, ::test_the_greenhouse_example_end_to_end_without_ollama -->

```bash
puffpai persona new gardenbot --name "GardenBot" \
  --goal "keep track of what we plant in the greenhouse and what we pick" \
  --lists "plantings,harvest log"
puffpai persona list
puffpai apps list
```

After setup is finished, the dashboard's Build a persona page is refused: changing
the build needs an unlocked vault, and the dashboard cannot pass the vault
session on that call yet. The commands above are not behind that gate.

That writes two files and rescans, so the app is live immediately, including in
an **already-running** build, which the CLI is a separate process from. The server
notices the new folder on its next read (it fingerprints the app and pack roots),
and the CLI also nudges a local server to re-read straight away. You never have to
restart anything. (If you hand-edit an app manifest that is already installed,
`POST /apps/rescan` or `puffpai apps rescan` is the way to pick that up.) The
files:
`$PUFFPAI_DATA_DIR/apps/gardenbot/app.json` (the app manifest),
`packs/gardenbot/pack.json` (the pack that groups it), and
`app_data/gardenbot.json` (its stored rows).

Nothing in the checkout is touched. The manifest fields that matter: `id`,
`name`, `blurb` (identity and the sentence on the page); `data.default` (the
shape of the store, e.g. `{"plantings": [], "harvest_log": []}`);
`vocabulary.keywords` (the words chat binds to this app, mined from `--goal`);
`ui.route` (`/dashboard/apps/<id>`); `surfaces` (where else it appears, e.g.
`daily.notes`).

**With no model attached, chat can still do four things with it**,
deterministically, before any model is called:

```
add tomatoes to the plantings list          -> "Added tomatoes to the plantings list"
what is on the plantings list               -> the rows, or "... is empty."
how many things are on the plantings list   -> "One thing ..."
remove tomatoes from the plantings list     -> "Took tomatoes off ..."
```

Each list is its own, an ambiguous sentence asks rather than guesses ("Which list
- plantings or harvest log?"), and the same sentences work by voice.

## C. Attach an API, or your own Python

<!-- proven by tests/test_skills_submit.py (whole file: the parser, the route,
     the CLI, the Bridge switch) and tests/test_skills_no_sandbox.py -->

A **skill** is three files. `skill.py` is one function with no imports, no files,
no environment and no network of its own, the only way out is `ctx`, and `ctx`
can only reach the hosts your manifest names. `skill.py`:

```python
"""Read one number off a JSON API and say it."""

URL = "https://api.example.org/v1/tide"


def run(inputs, ctx):
    reading = ctx.fetch_json(URL)
    height = round(float(reading["height_m"]), 2)
    return {
        "speech": "The tide is {h} metres.".format(h=height),
        "data": {"height_m": height, "source": URL},
    }
```

`manifest.json`, `triggers` must be phrases the appliance does not already
answer (a collision is refused, not competed for); `permissions.network_hosts` is
the **only** reach this skill will ever have:

```json
{"schema": "puffpai_skill/v1", "id": "tide_clock", "name": "Tide clock",
 "description": "The tide height from a public JSON API this household chose.",
 "version": 1, "author": "owner",
 "triggers": ["tide height now", "how high is the tide"],
 "inputs": {"type": "object", "properties": {}, "required": []},
 "permissions": {"network_hosts": ["api.example.org"], "files": [],
                 "ha_domains": [], "secrets": []},
 "limits": {"timeout_s": 10, "max_requests": 1, "max_response_bytes": 8192},
 "tests": ["tests.py"]}
```

`tests_fixtures.json` is **required**: a skill with no test is not installable
and fixtures that cannot fail are refused. The host owns the harness; you ship
the canned answers and the assertions it makes:

```json
{"responses": {"https://api.example.org/v1/tide": {"json": {"height_m": 1.234}}},
 "cases": [{"inputs": {},
            "expect": {"speech_contains": ["The tide is 1.23 metres."],
                       "data": {"height_m": 1.23}}}]}
```

Submit it. `POST /skills/drafts` also takes JSON `{"id": ..., "files": {"manifest.json":
"...", "skill.py": "...", "tests_fixtures.json": "..."}}`, and `?replace=1`
overwrites an existing **draft** (never an installed skill):

```bash
puffpai skills submit ./tide_clock          # a folder, or a .zip
puffpai skills list
puffpai skills status tide_clock
curl -X POST localhost:8000/skills/drafts -H "X-API-Key: $KEY" -F file=@tide_clock.zip
```

**Submitting is not installing.** It writes a draft and one consent request.
Somebody in the house then reads the **consent card**, every byte of your code, a
sha256 per file, the domains it will reach, and the result of running your
fixtures offline in the sandbox, and approves it on **Dashboard > Skills**. The
approval is of those exact bytes: change a file afterwards and it is thrown away,
not inherited. Only then does the skill's own Bridge switch (`skill:tide_clock:0`)
open, for that host set and no other.

Limits, all refusals rather than warnings: id `[a-z][a-z0-9_]{1,31}`; exactly
those three files (no `data/`, no `README.md`, bytes the card does not show are
bytes nobody approved); ≤ 256 KB total; ≤ 200 lines of `skill.py`; ≤ 16 hosts;
`https` and port 443 only, `GET` only; **a host that resolves to an address on
your own network is refused**: a skill's switch reaches the public internet only, so a skill
cannot reach your router or your NAS whatever name it declares, and there is no
LAN skill switch in this build. One refused request switches the skill off.

**Platform requirement.** Running a skill needs **bubblewrap** at `/usr/bin/bwrap`:
`sudo apt install bubblewrap` on Debian/Ubuntu (`dnf` on Fedora, `pacman` on
Arch). **Windows is not supported**, use a Linux box, or WSL2 with the backend
running inside it. Without it, submitting and getting a card still work, but the
tests are **not** run, the card says so, the skill installs **switched off with
its switch shut**, and `POST /skills/<id>/enable` refuses in one sentence. There is
no weaker fallback sandbox and there will not be one; `GET /capabilities/extend`
reports all of this as `skills.sandbox_requirements`.

## D. Rename the agents

<!-- proven by tests/test_agent_rename_and_extend_api.py (TestNamesRead, TestNamesWrite) -->

```bash
curl "localhost:8000/agents/names" -H "X-API-Key: $KEY"
curl -X PUT "localhost:8000/agents/names" -H "X-API-Key: $KEY" \
     -H 'Content-Type: application/json' \
     -d '{"preset": "default", "names": {"caretaker": "Nanny"}}'
```

Both keys are optional; `null` as a name clears that override. Renaming is
**cosmetic**: internal ids are what the bus routes on, what the Sentinel reviewer
is wired by and what credential scopes spell, and no rename touches any of them.
