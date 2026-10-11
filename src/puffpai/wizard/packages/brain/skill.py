"""Brain: the model this machine runs, and the check that it answers.

One card for two decisions, and the split falls where the lifecycle already
puts it:

* :func:`propose` fits the catalog to this machine -- the recommendation, the
  alternatives, the routing tier and what that tier costs, and an honest size
  for anything that would have to be downloaded. Read-only.
* :func:`apply` records the choice and, only if the owner said yes on the card
  and the posture allows a download, pulls the model **through the path that
  already exists** (``brain_health.pull_model``, gated by
  ``gates.may_download_models``). No outbound call site of its own: nothing
  here calls ``requests``, so the egress call-site guard has nothing to
  register.
* :func:`verify` asks the Ollama this build is configured for whether the model
  is there.

**What blocks.** Brain is one of the two skills that may block, and the
wizard uses that allowance for nothing: a missing model ends with
``ready: false``, a pull command on the closing screen and exit code 0. So
verify returns a **gap**, never an exception.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""
from __future__ import annotations

#: A model name the owner may type instead of picking. Ollama's own shape, and
#: the **tag is required**: ``qwen3:30b-a3b``, ``library/llama3.2:3b``. The tag
#: is what makes this a model name rather than a word -- a pattern that
#: accepted any bare word would take a bot called Stargazer as a model, and
#: nothing would say so until verify could not find it on the Ollama.
MODEL_PATTERN = (r"^(?:[A-Za-z0-9][A-Za-z0-9._-]{0,60}/){0,2}"
                 r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}:[A-Za-z0-9._-]{1,40}$")

_GB = 1024 ** 3


# -- what the run knows about this machine ------------------------------------

def _machine(ctx) -> dict:
    """Census, address and installed models, from the run or from the section."""
    shared = getattr(ctx, "shared", None) or {}
    census = shared.get("census")
    url = shared.get("ollama_url")
    installed = shared.get("installed_models")
    if census is None or url is None:
        section = {}
        try:
            section = ctx.read("machine")
        except Exception:  # pragma: no cover - a package always declares it
            section = {}
        census = census if census is not None else section.get("census")
        url = url if url is not None else section.get("ollama_url")
        if installed is None:
            installed = section.get("installed_models")
    return {"census": census or {}, "url": str(url or ""),
            "installed": installed}


def _menu(ctx, machine: dict, gates: dict) -> dict:
    from puffpai import model_catalog, setup_ops

    installed = machine["installed"]
    if installed is None:
        installed = setup_ops.list_models(machine["url"]) if machine["url"] else None
    menu = model_catalog.fit_menu(
        machine["census"], installed_models=list(installed or []),
        prefer_hosted_tier=bool(ctx.non_interactive
                                and not getattr(ctx.args, "model", None)))
    options = list(menu.get("options") or [])
    if not gates.get("may_download_models", True):
        # An offline build is never offered a model it would have to download,
        # and the card says why rather than quietly showing a shorter list.
        options = [opt for opt in options if opt.get("installed")]
    menu = dict(menu)
    menu["options"] = options
    menu["installed_models"] = installed
    return menu


def _default_model(ctx, menu: dict) -> tuple[str, bool]:
    """The model the card proposes, and whether the owner named it.

    What was applied first (the propose rule, CONVERTING.md), then the flag,
    then the fit. A card re-drawn after an apply shows the model that was
    recorded, not the recommendation it was recorded instead of.
    """
    applied = ctx.applied("choice", default={})
    applied_model = str((applied or {}).get("model") or "")
    if applied_model:
        return applied_model, str((applied or {}).get("chosen_by")) == "owner"
    stated = getattr(ctx.args, "model", None)
    if stated:
        return str(stated), True
    rec = menu.get("recommended") or {}
    on_menu = {str(opt.get("id")) for opt in menu.get("options") or ()}
    if rec.get("id") and str(rec["id"]) in on_menu:
        return str(rec["id"]), False
    return str(getattr(getattr(ctx.cfg, "ollama", None), "model", "") or ""), False


# -- propose ------------------------------------------------------------------

def propose(ctx) -> dict:
    """Fit the catalog to this machine. Reads, measures, downloads nothing."""
    from puffpai import brain_health, cli

    cli.enter_step("The brain")
    gates = (getattr(ctx, "shared", None) or {}).get("gates") or {}
    machine = _machine(ctx)
    menu = _menu(ctx, machine, gates)
    options = menu.get("options") or []
    model, by_owner = _default_model(ctx, menu)
    chosen_opt = next((opt for opt in options if str(opt.get("id")) == model),
                      None)
    # Installed is a fact about the Ollama, not about the fitted menu: a model
    # the owner named that the catalog would not have offered on this machine
    # still costs nothing to use if it is already there.
    installed = bool((chosen_opt or {}).get("installed")) or (
        bool(model) and model in (menu.get("installed_models") or []))
    may_download = bool(gates.get("may_download_models", True))
    disk_gb = (chosen_opt or {}).get("disk_gb")

    fields = [{"id": "found", "kind": "info", "label": "Ollama",
               "current": _ollama_line(machine)}]
    choices = [_model_option(opt, menu) for opt in options][:40]
    # Every model this Ollama already has is a real answer, whether or not the
    # catalog knows it. Offering them is what makes "type your own" the rare
    # case rather than the only way to name what is installed.
    offered = {item["value"] for item in choices}
    for name in (machine["installed"] or ()):
        name = str(name)
        if name and name not in offered and len(choices) < 60:
            offered.add(name)
            choices.append({"value": name, "label": f"{name} (installed here)"})
    if choices:
        fields.append({
            "id": "model", "kind": "choice", "label": "The brain",
            "options": choices,
            "current": model if (chosen_opt or model in offered) else "",
            "free_text": True, "pattern": MODEL_PATTERN,
            "help": "Pick one of these, or type the full name of a model you "
                    "already have -- name and tag, like llama3.2:3b."})
        if model and model not in offered:
            fields.append({
                "id": "unknown_model", "kind": "info",
                "label": "Nothing here knows that model",
                "current": f"'{model}' is not installed on the Ollama at "
                           f"{machine['url'] or 'the recorded address'} and it "
                           f"is not in this build's catalog. It is recorded as "
                           f"asked for, and your bot cannot answer until a "
                           f"model by that exact name exists."})
    else:
        fields.append({
            "id": "none", "kind": "info", "label": "Nothing to pick",
            "current": ("You chose an offline build and nothing is installed "
                        "here yet. Carry a brain over with `puffpai build "
                        "import`, or re-run setup where downloads are allowed.")
            if not may_download else
            ("No model in the catalog fits what this machine reported. "
             f"{menu.get('message') or ''}").strip()})

    if model:
        fields.append({"id": "tier", "kind": "info", "label": "What it can do",
                       "current": _tier_line(model, machine["census"])})
    if not installed and may_download and model:
        fields.append({
            "id": "download_now", "kind": "toggle",
            "label": "Download it now?", "current": False,
            "help": (brain_health.download_estimate(disk_gb)
                     or "This build cannot size that model in advance.") +
                    ". Leave this off and the closing screen gives you the "
                    "command to run yourself."})

    pulling = bool(not installed and may_download and model)
    card = {
        "skill": "brain", "section": "brain",
        "title": "What does the thinking",
        "decided": _decided(model, installed, may_download, disk_gb),
        "status": "ready" if (model and (installed or may_download))
                  else "needs_you",
        "consequential": pulling,
        "consequential_reasons": ["heavy_bytes"] if pulling else [],
        "estimate": _estimate(installed, disk_gb, pulling),
        "fields": fields,
    }
    return {"card": card, "menu": menu, "machine": machine,
            "model": model, "by_owner": by_owner, "gates": gates,
            # What the apply would share (CONVERTING.md). The model is what the
            # Ollama config entry below this card needs to run at all.
            "shared_preview": {"chosen_model": model}}


def _ollama_line(machine: dict) -> str:
    installed = machine["installed"]
    where = machine["url"] or "no address recorded"
    if installed is None:
        return (f"Could not reach Ollama at {where}. Install it from "
                f"https://ollama.com; the brain below is still the one to get.")
    if not installed:
        return f"Ollama is running at {where}, with no models installed yet."
    return f"Ollama is up at {where}, {len(installed)} model(s) installed."


def _model_option(opt: dict, menu: dict) -> dict:
    rec = str((menu.get("recommended") or {}).get("id") or "")
    if opt.get("installed"):
        avail = "installed"
    elif opt.get("disk_gb"):
        avail = f"about {opt.get('disk_gb')} GB to download"
    else:
        avail = "download"
    mark = " (recommended)" if rec and str(opt.get("id")) == rec else ""
    return {"value": str(opt.get("id")),
            "label": f"{opt.get('label')} ({opt.get('params')}, "
                     f"{opt.get('fit')}, {avail}){mark}"}


def _tier_line(model: str, census: dict) -> str:
    from puffpai import model_profile

    tier = model_profile.routing_tier(model)
    line = (f"Routing tier: {tier} "
            f"({model_profile.basis_label(model, census)}). "
            f"{model_profile.limits_sentence(model)}")
    if not model_profile.supports(model, "planning"):
        line += (" What that costs you: this brain does single steps well and "
                 "will not try to chain them. That is a real limit, not a bug.")
    return line


def _decided(model: str, installed: bool, may_download: bool,
             disk_gb) -> str:
    if not model:
        return "No brain picked yet, so this build cannot answer anything."
    if installed:
        return f"{model}, already on this box."
    if not may_download:
        return f"{model} is the fit for this machine and is not on this box."
    size = f" (about {disk_gb:g} GB)" if disk_gb else ""
    return f"{model}{size}, not on this box yet."


def _estimate(installed: bool, disk_gb, pulling: bool) -> dict:
    """Honest, or it says unknown. Never a number nobody measured.

    Size comes from the catalog, which is calibrated against real pulls, so a
    download says ``calibrated`` and carries the bytes. Time does not: how long
    a download takes is a property of the owner's connection, and this build
    has not measured it, so no seconds are offered at all.
    """
    if installed or not pulling:
        return {"bytes": 0, "seconds": 0, "basis": "measured"}
    try:
        size = float(disk_gb or 0)
    except (TypeError, ValueError):
        size = 0.0
    if size <= 0:
        return {"bytes": 0, "seconds": None, "basis": "unknown"}
    return {"bytes": int(size * _GB), "seconds": None, "basis": "calibrated"}


# -- apply --------------------------------------------------------------------

def apply(ctx, answers: dict) -> dict:
    """Record the choice, pull only if asked and allowed, report readiness."""
    from puffpai import model_profile

    answers = answers or {}
    proposal = ctx.proposal or {}
    gates = proposal.get("gates") or (
        (getattr(ctx, "shared", None) or {}).get("gates") or {})
    machine = proposal.get("machine") or _machine(ctx)
    menu = proposal.get("menu") or _menu(ctx, machine, gates)
    options = menu.get("options") or []

    model = str(answers.get("model") or proposal.get("model")
                or _default_model(ctx, menu)[0] or "")
    by_owner = bool(answers.get("model")) or bool(proposal.get("by_owner"))
    if answers.get("model"):
        by_owner = str(answers["model"]) != str(
            (menu.get("recommended") or {}).get("id") or "")

    chosen_opt = next((opt for opt in options if str(opt.get("id")) == model),
                      None)
    brain_choice = {
        "model": model,
        "disk_gb": (chosen_opt or {}).get("disk_gb"),
        "recommended": (menu.get("recommended") or {}).get("id"),
        "chosen_by": "owner" if by_owner else "recommendation",
        "routing_tier": model_profile.routing_tier(model),
        "supports_planning": model_profile.supports(model, "planning"),
        "profile_basis": model_profile.basis(model),
    }

    ready, result = _ready(ctx, machine["url"], model, brain_choice, gates,
                           bool(answers.get("download_now")))
    brain_choice["ready"] = bool(ready.get("ready"))
    brain_choice["brain_status"] = ready.get("status")

    result.update({
        "section": "brain",
        "data": {"choice": brain_choice, "model": model,
                 "ready": brain_choice["ready"]},
        "shared": {"chosen_model": model, "brain_choice": brain_choice,
                   "brain_ready": ready},
    })
    return result


def _ready(ctx, url: str, model: str, brain_choice: dict, gates: dict,
           download_now: bool) -> tuple[dict, dict]:
    """The Brain-ready block, and whatever gap goes with it.

    The same four outcomes as ``cli._setup_brain_ready_step``, with its yes/no
    arriving as a card answer instead.
    """
    from puffpai import brain_health, setup_ops

    state = setup_ops.brain_status(url, model)
    status = str(state.get("status"))
    pull = brain_health.pull_command(model, url)
    estimate = brain_health.download_estimate(brain_choice.get("disk_gb"))

    if status == brain_health.CONNECTED:
        return ({"ready": True, "status": status, "pull_command": "",
                 "reason": ""}, {})

    if status == brain_health.UNREACHABLE:
        reason = (f"Nothing is answering at {url}, so this build has no brain "
                  f"to reach.")
        return ({"ready": False, "status": status, "pull_command": pull,
                 "reason": reason, "download_estimate": estimate,
                 "remedy": brain_health.unreachable_remedy(url)},
                {"gap": _gap("Your bot has no brain to talk to.", reason,
                             "It cannot answer anything until Ollama is "
                             "running at that address.")})

    installed = state.get("installed") or []
    reason = (f"This build is set to use '{model}', which is not installed on "
              f"the Ollama at {url} ({len(installed)} model(s) are).")
    block = {"ready": False, "status": status, "pull_command": pull,
             "reason": reason, "download_estimate": estimate}

    if not gates.get("may_download_models", True):
        return (block, {"gap": _gap(
            "The brain this build needs is not on this box.", reason,
            "You chose an offline build, so nothing was fetched. Carry it over "
            "with `puffpai build import`, or run the pull command yourself "
            "when this box is allowed online.")})

    if not download_now:
        return (block, {"gap": _gap(
            "The brain this build needs is not downloaded.", reason,
            "Your build is finished and cannot answer until the pull command "
            "on the closing screen has run.")})

    # The one download this skill starts, through the path that already exists.
    outcome = brain_health.pull_model(model, url)
    if outcome.get("ok"):
        return ({"ready": True, "status": brain_health.CONNECTED,
                 "pull_command": "", "reason": ""},
                {"note": str(outcome.get("message") or "")})
    return (block, {"gap": _gap(
        "The download did not finish.", str(outcome.get("message") or reason),
        "Your bot cannot answer until the model is on the Ollama it is "
        "configured for.")})


def _gap(what: str, why: str, impact: str) -> dict:
    return {"skill": "brain", "what": what, "why": why, "impact": impact,
            "fix": {"kind": "command", "id": "setup.rerun", "args": {}}}


# -- verify -------------------------------------------------------------------

def verify(ctx) -> dict:
    """Ask the Ollama this build is configured for whether the model is there.

    Not the proposal, and not the pull's own report: the question is whether a
    model is installed on the server the finished bot reads from, which is the
    single fact that decides whether it answers anything.
    """
    from puffpai import brain_health, setup_ops

    section = ctx.own()
    model = str(section.get("model") or
                (section.get("choice") or {}).get("model") or "")
    url = ""
    try:
        url = str((ctx.read("machine") or {}).get("ollama_url") or "")
    except Exception:  # pragma: no cover - machine is declared in the manifest
        url = ""
    url = url or str((getattr(ctx, "shared", None) or {}).get("ollama_url") or "")

    if not model:
        return {"status": "gap", "evidence": "no model is recorded",
                "gap": _gap("No brain is recorded for this build.",
                            "the brain step did not settle on a model",
                            "Your bot cannot answer anything.")}

    state = setup_ops.brain_status(url, model)
    status = str(state.get("status"))
    if status == brain_health.CONNECTED:
        return {"status": "pass",
                "evidence": f"{model} is installed on the Ollama at {url}"}
    if status == brain_health.UNREACHABLE:
        return {"status": "gap",
                "evidence": f"nothing answered at {url}",
                "gap": _gap("Your bot has no brain to talk to.",
                            f"nothing answered at {url} when the build checked",
                            "It cannot answer anything until Ollama is running "
                            "there.")}
    return {"status": "gap",
            "evidence": f"{url} answered, and {model} is not installed there",
            "gap": _gap("The brain this build needs is not installed.",
                        f"'{model}' is not on the Ollama at {url}",
                        "Your bot cannot answer until it is pulled: "
                        f"{brain_health.pull_command(model, url)}")}
