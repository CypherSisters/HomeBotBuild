"""The browser wizard's whole run, end to end, in the shapes IT sends.

``dashboard/app/onboarding/OnboardingWizard.tsx`` walks thirteen steps and
``dashboard/app/_lib/services/setupService.ts`` turns each one into exactly one
request. This file replays that sequence against the Flask test client - same
order, same bodies, same keys read back out of each response - so a backend
change that is harmless to the terminal wizard but breaks the browser one fails
here instead of on somebody's first boot.

What it pins, beyond "every step answers ok":

* **The blocks the wizard carries.** ``build_manifest.assemble_manifest``
  defaults every section it is not given and reads NOTHING back off disk, so
  the manifest is only as truthful as what the caller sends. The wizard keeps
  the ``manifest_block`` each route hands it and posts the lot at the review
  step; if a route stops returning its block, the committed manifest silently
  loses the owner's answer. That is the regression these assertions catch.
* **The Zone 3 card's inputs.** ``GET /setup/bridge`` carries ``zone3_support``
  and ``edge_options``; the card fails closed without them.
* **No secret in the record.** The vault PIN, the recovery code and a Home
  Assistant token are all handed to the backend in this run. None of the three
  may appear anywhere in the manifest it writes.

Run:
    pytest tests/test_setup_api_e2e.py
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

#: Passes ``zone_vault``'s strength rules: long, mixed, no run and no repeat.
WIZARD_PIN = "Kestrel-Loom-7742"

#: Never a real credential, and it has to survive a substring search of the
#: manifest, so it is a shape nothing else would produce.
FAKE_HA_TOKEN = "eyJfake_token_for_a_test_only_2f1c9e"


@pytest.fixture
def scratch(tmp_path):
    """One test, its own install: temp state dir AND temp config dir.

    Same reasoning as ``tests/test_setup_api_routes.py``: ``$PUFFPAI_DATA_DIR``
    alone is not enough, because the wizard writes through
    ``setup_ops.write_main_config`` / ``set_ha_config``, which resolve to
    ``cfg.CONFIG_DIR`` - the REPO's ``config/`` unless ``$PUFFPAI_CONFIG_DIR``
    says otherwise. The shipped config is copied in so the wizard merges into
    the same base a real install has.
    """
    from puffpai.config import CONFIG_DIR_ENV, get_config

    repo_config = Path(__file__).resolve().parents[1] / "config"
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    for name in ("puffpai_config.json", "bridge_postures.json",
                 "capability_catalog.json", "model_catalog.json",
                 "model_profiles.json"):
        source = repo_config / name
        if source.exists():
            shutil.copy2(source, config_dir / name)

    previous = {key: os.environ.get(key)
                for key in ("PUFFPAI_DATA_DIR", CONFIG_DIR_ENV)}
    os.environ["PUFFPAI_DATA_DIR"] = str(tmp_path)
    os.environ[CONFIG_DIR_ENV] = str(config_dir)
    get_config(_force_reload=True)
    try:
        yield tmp_path
    finally:
        for key, value in previous.items():
            if value is not None:
                os.environ[key] = value
            else:
                os.environ.pop(key, None)
        get_config(_force_reload=True)


@pytest.fixture
def client():
    """A test client with the process-global vault handle dropped.

    ``api_server._get_vault()`` caches ONE Vault bound to whatever directory
    was resolved first; each test here seals its own vault in its own scratch
    dir, so without the reset the second test unlocks the first one's.
    """
    from puffpai import api_server
    api_server._vault_instance = None
    api_server._vault_sessions.clear()
    api_server._setup_recovery_pending.clear()
    api_server.app.config.update(TESTING=True)
    return api_server.app.test_client()


def _auth():
    from puffpai import api_server
    return {"Authorization": f"Bearer {api_server._API_KEY}"}


class Wizard:
    """The browser's side of the conversation, and only that.

    Every method is one step of ``OnboardingWizard.commit()``: the same route,
    the same body, and the same field read back out. ``sections`` is the
    wizard's own accumulator - the blocks it carries to the manifest commit.
    """

    def __init__(self, client):
        self.client = client
        self.sections: dict[str, dict] = {}
        self.recovery_code = ""

    # -- transport ---------------------------------------------------------
    def get(self, path):
        resp = self.client.get(path, headers=_auth())
        return resp.status_code, resp.get_json() or {}

    def post(self, path, body):
        resp = self.client.post(path, json=body, headers=_auth())
        return resp.status_code, resp.get_json() or {}

    def ok(self, path, body=None):
        """A step of the wizard. Refuses anything the browser would refuse."""
        status, data = (self.get(path) if body is None else self.post(path, body))
        assert status == 200, f"{path} -> {status}: {data}"
        assert data.get("ok") is not False, f"{path} answered not-ok: {data}"
        return data

    def record(self, key, block):
        if isinstance(block, dict):
            self.sections[key] = block

    # -- the steps, in the order the wizard walks them ---------------------
    def status(self):
        return self.ok("/setup/status?ollama=0")

    def identity(self, name="HomeBot"):
        persona = self.ok("/setup/defaults").get("persona")
        data = self.ok("/setup/identity", {"bot_name": name, "persona": persona})
        self.record("identity", {"identity": data.get("persona"), "name": name})
        return data

    def posture(self):
        options = self.ok("/setup/posture")
        build = next((o["id"] for o in options["build_network"] if o.get("default")),
                     options["build_network"][0]["id"])
        egress = next((o["id"] for o in options["run_egress"] if o.get("default")),
                      options["run_egress"][0]["id"])
        data = self.ok("/setup/posture", {"build_network": build,
                                          "run_egress": egress,
                                          "enabled_switches": []})
        self.record("posture", data.get("manifest_block"))
        return data

    def brain(self):
        self.ok("/setup/hardware")
        menu = self.ok("/setup/brain-menu", {}).get("menu") or {}
        crew = self.ok("/setup/crew", {}).get("crew") or {}
        recommended = menu.get("recommended") or {}
        model = recommended.get("id") or (menu.get("options") or [{}])[0].get("id") or ""
        self.ok("/setup/ollama", {"model": model})
        self.record("brain", {"model": model, "url": "", "installed": False,
                              "recommended": menu.get("recommended")})
        self.record("crew", crew)
        return model

    def household(self, owner="Alex", members=("Sam",)):
        self.ok("/setup/household")
        # The wizard sends names and nothing else - no roles, no ages.
        return self.ok("/setup/household",
                       {"owner_name": owner,
                        "members": [{"name": n} for n in members]})

    def surfaces(self):
        derivation = self.ok("/setup/surfaces")
        data = self.ok("/setup/surfaces", {"goal": derivation.get("goal") or "",
                                           "add": [], "remove": []})
        self.record("surfaces", data.get("manifest_block"))
        return data

    def storage(self):
        paths = self.ok("/setup/storage").get("paths") or {}
        data = self.ok("/setup/storage", paths)
        self.record("storage", data.get("manifest_block"))
        return data

    def vault(self, pin=WIZARD_PIN):
        data = self.ok("/setup/vault", {"pin": pin})
        self.record("vault", data.get("vault"))
        self.recovery_code = data.get("recovery_code") or ""
        confirmed = self.ok("/setup/vault/confirm",
                            {"recovery_code": self.recovery_code})
        self.sections["vault"] = {**self.sections.get("vault", {}),
                                  **(confirmed.get("vault") or {})}
        return data

    def home_assistant(self, present=False, url="", token=""):
        body = ({"present": False} if not present
                else {"present": True, "url": url, "token": token})
        data = self.ok("/setup/ha", body)
        self.record("integrations", {
            "home_assistant": data.get("ha") or {"present": present},
            "media_library": {"present": False},
        })
        return data

    def bridge(self, network_edge=False):
        options = self.ok("/setup/bridge")
        posture = options.get("recommended") or options.get("default")
        data = self.ok("/setup/bridge", {"posture": posture,
                                         "network_edge": network_edge})
        self.record("bridge", data.get("bridge"))
        return options, data

    def review(self):
        """The review step previews with the blocks; GET is only a status."""
        return self.ok("/setup/manifest", dict(self.sections))

    def build_it(self):
        written = self.ok("/setup/manifest", {**self.sections, "commit": True})
        self.ok("/setup/complete", {})
        return written


def _full_run(client, *, ha=False, network_edge=False):
    wizard = Wizard(client)
    assert wizard.status().get("complete") is False
    wizard.identity()
    wizard.posture()
    wizard.brain()
    wizard.household()
    wizard.surfaces()
    wizard.storage()
    wizard.vault()
    if ha:
        wizard.home_assistant(True, "http://127.0.0.1:8123", FAKE_HA_TOKEN)
    else:
        wizard.home_assistant(False)
    wizard.bridge(network_edge=network_edge)
    # The knowledge step is skippable and the wizard sends nothing when the
    # folder box is empty, which is what this run does.
    wizard.review()
    return wizard, wizard.build_it()


# ── 1. the whole run ────────────────────────────────────────────────────────

class TestFullRun:

    def test_every_step_answers_ok_and_the_wizard_closes(self, scratch, client):
        wizard, written = _full_run(client)
        assert written.get("written") is True
        assert written.get("path")
        assert wizard.status().get("complete") is True

    def test_the_manifest_records_what_the_owner_answered(self, scratch, client):
        """The point of carrying the blocks: an empty commit records nothing.

        ``assemble_manifest`` defaults every section it is not given, so a
        review step that posted ``{commit: true}`` alone would write a manifest
        with no name, no model, a defaulted posture and an EMPTY surface
        allowlist - which means "nothing extra is loaded".
        """
        wizard, written = _full_run(client)
        manifest = written["manifest"]

        assert manifest["build"]["name"] == "HomeBot"
        assert manifest["brain"]["model"], "the chosen model was not recorded"
        assert manifest["surfaces"]["allowlist"], "the allowlist was lost"
        assert manifest["posture"]["run_egress"] in ("sealed", "island", "enumerated")
        assert manifest["vault"]["sealed"] is True
        assert manifest["vault"]["recovery_confirmed"] is True
        assert manifest["bridge"]["posture"]
        assert manifest["storage"]["state_dir"]
        assert manifest["crew"]["max_specialists"] is not None
        assert manifest["integrations"]["home_assistant"]["present"] is False
        assert written.get("warnings") == []

    def test_the_review_step_previews_without_writing(self, scratch, client):
        """GET /setup/manifest is a STATUS; the preview is the POST."""
        wizard = Wizard(client)
        wizard.identity()
        wizard.posture()
        status = wizard.ok("/setup/manifest")
        assert status.get("exists") is False

        preview = wizard.review()
        assert preview["written"] is False
        assert preview["manifest"]["build"]["name"] == "HomeBot"
        # Still nothing on disk: a preview that wrote would be a commit.
        assert wizard.ok("/setup/manifest").get("exists") is False

    def test_a_second_vault_is_refused_once_setup_is_complete(self, scratch, client):
        _full_run(client)
        resp = client.post("/setup/vault", json={"pin": WIZARD_PIN},
                           headers=_auth())
        assert resp.status_code in (403, 409)
        assert (resp.get_json() or {}).get("ok") is False


# ── 2. the shapes the browser reads ─────────────────────────────────────────

class TestShapesTheBrowserReads:

    def test_every_block_route_returns_its_manifest_block(self, scratch, client):
        """Four routes carry the block the wizard cannot rebuild itself."""
        wizard = Wizard(client)
        assert isinstance(wizard.posture().get("manifest_block"), dict)
        assert isinstance(wizard.storage().get("manifest_block"), dict)
        assert isinstance(wizard.surfaces().get("manifest_block"), dict)
        assert isinstance(wizard.household().get("manifest_block"), dict)
        # /setup/surfaces writes NOTHING, so its block is the only record of
        # the owner's edits - losing it ships a build with no surfaces.
        assert wizard.sections["surfaces"]["allowlist"]

    def test_vault_and_bridge_answer_with_their_blocks(self, scratch, client):
        wizard = Wizard(client)
        wizard.posture()
        created = wizard.vault()
        assert created["vault"]["sealed"] is True
        assert created.get("recovery_code")
        assert created.get("recovery_code_notice")
        options, saved = wizard.bridge()
        assert isinstance(saved.get("bridge"), dict)
        assert saved["bridge"].get("posture")

    def test_the_bridge_step_carries_the_zone3_card(self, scratch, client):
        """The Zone 3 opt-in renders from `zone3_support` + `edge_options`.

        Reading a key the backend does not answer is not a safe default here:
        the card would be permanently disabled on a box that can run Zone 3,
        with a reason nobody wrote.
        """
        wizard = Wizard(client)
        options = wizard.ok("/setup/bridge")
        support = options.get("zone3_support")
        assert isinstance(support, dict)
        assert isinstance(support.get("supported"), bool)
        assert support.get("reasons"), "a verdict with no sentence to show"
        kinds = (options.get("edge_options") or {}).get("kinds") or []
        assert any(k.get("id") == "zone3" for k in kinds)
        assert [p["id"] for p in options.get("postures") or []]

    def test_the_posture_step_offers_both_axes_and_the_door_table(self, scratch, client):
        wizard = Wizard(client)
        options = wizard.ok("/setup/posture")
        for axis in ("build_network", "run_egress"):
            assert options[axis] and all(o.get("id") and o.get("label")
                                         for o in options[axis])
        # The wizard only shows the switches for the "enumerated" tier, so the
        # id has to keep existing.
        assert any(o["id"] == "enumerated" for o in options["run_egress"])
        assert isinstance(options.get("egress_switches"), list)

    def test_identity_echoes_the_persona_the_wizard_sends_back(self, scratch, client):
        wizard = Wizard(client)
        data = wizard.identity("Sprocket")
        assert data["bot_name"] == "Sprocket"
        assert data["persona"] == wizard.sections["identity"]["identity"]


# ── 3. Home Assistant, and where a token may not turn up ────────────────────

class TestHomeAssistantBranch:

    def test_the_present_branch_finishes_and_keeps_the_token_off_the_record(
            self, scratch, client):
        """The token is saved to the box's own config and NOWHERE else.

        ``/setup/ha/test`` is allowed to report the house-local address as
        unreachable - nothing answers on 127.0.0.1:8123 in a test run - and the
        wizard carries on either way, because a token the owner has to fix
        later is not a reason to trap them in the step.
        """
        wizard = Wizard(client)
        probe = client.post("/setup/ha/test",
                            json={"url": "http://127.0.0.1:8123",
                                  "token": FAKE_HA_TOKEN}, headers=_auth())
        assert probe.status_code in (200, 400)
        assert FAKE_HA_TOKEN not in probe.get_data(as_text=True)

        wizard, written = _full_run(client, ha=True)
        ha = written["manifest"]["integrations"]["home_assistant"]
        assert ha["present"] is True
        assert ha["token_configured"] is True

        blob = json.dumps(written["manifest"])
        assert FAKE_HA_TOKEN not in blob
        assert WIZARD_PIN not in blob
        assert wizard.recovery_code and wizard.recovery_code not in blob

    def test_the_absent_branch_is_a_complete_answer(self, scratch, client):
        """No Home Assistant is a finished build, not a degraded one."""
        wizard = Wizard(client)
        data = wizard.home_assistant(False)
        assert data["configured"] is False
        assert data["ha"]["present"] is False
        assert data["ha"]["token_configured"] is False

    def test_an_off_house_address_is_refused_before_the_token_is_written(
            self, scratch, client):
        resp = client.post("/setup/ha", json={"present": True,
                                              "url": "http://example.com:8123",
                                              "token": FAKE_HA_TOKEN},
                           headers=_auth())
        assert resp.status_code == 400
        body = resp.get_json() or {}
        assert body["ok"] is False
        assert FAKE_HA_TOKEN not in resp.get_data(as_text=True)
