"""The console renderer: draws any card, collects any answer.

*CLI and dashboard are renderers of the same card object, neither
owns a question*. So there is nothing skill-shaped in this file. It knows the
field kinds in :mod:`puffpai.wizard.cards` and nothing about Discover, Brain or
the vault; a skill that needs a new kind of question adds it to the card
contract, where both renderers learn it at once.

Two properties worth stating:

* **A secret is never echoed and never returned to the log.** ``secret_entry``
  reads with :func:`getpass.getpass`, so nothing appears on the screen and
  nothing lands in a scrollback buffer. The runner logs
  :func:`puffpai.wizard.cards.redact_answers`, never the answers themselves.
* **Non-interactive accepts the proposal.** ``--yes`` returns no answers at
  all, which is the same thing as "what you proposed is what I want" -- not a
  set of defaults typed in on the owner's behalf.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import getpass
import sys
from typing import Any, Callable

from puffpai.wizard import cards, fixes

#: The fence around one rendered card. Printed, like Prove's own markers, so a
#: block can be lifted out whole -- by an owner reading a long wizard, and by
#: the equivalence test that compares the rest of the output exactly.
BEGIN_MARK = "--- card ---"
END_MARK = "--- end card ---"

_STATUS_WORDS = {
    "ready": "ready",
    "needs_you": "needs you",
    "gap": "has a gap",
    "blocked": "blocked",
    "not_built": "not built yet",
}

_REASON_WORDS = {
    "heavy_bytes": "this downloads a lot",
    "destructive_rerun": "running it again destroys something",
    "runtime_egress": "it turns on network access after the build",
    "vault_pin_unset": "the vault PIN is not set",
}


#: Punctuation a Windows console cannot print, and the ASCII that means the
#: same thing. A card may carry any printable text -- it is also rendered in a
#: browser -- so the folding belongs here, at the console, and not in the card.
_ASCII_FOLD = str.maketrans({
    "—": "-", "–": "-", "‒": "-", "−": "-",
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"',
    "…": "...", "·": "-", "•": "-", " ": " ",
    "«": '"', "»": '"', "→": "->", "≥": ">=",
    "≤": "<=", "×": "x",
})


def ascii_only(text: str) -> str:
    """Fold to ASCII. A non-ASCII byte garbles a Windows console, every time."""
    folded = str(text or "").translate(_ASCII_FOLD)
    if folded.isascii():
        return folded
    return folded.encode("ascii", "replace").decode("ascii")


def strip_block(lines: list[str]) -> list[str]:
    """Every line outside the card fences. Used by the equivalence test."""
    out: list[str] = []
    inside = False
    for line in lines:
        if line.strip() == BEGIN_MARK:
            inside = True
            continue
        if line.strip() == END_MARK:
            inside = False
            continue
        if not inside:
            out.append(line)
    return out


def card_lines(card: dict) -> list[str]:
    """What the card says, before anything is asked. Pure, so it is testable.

    The heading is the **owner's words**, and the skill id is
    small type after them -- never the label on its own. A console has no small
    type, so the id is set apart in brackets at the end of the line and the
    title is underscored on the next.
    """
    title = str(card["title"])
    lines = [f"{title}   [{cards.card_id(card)}]", "=" * len(title)]
    if card.get("decided"):
        lines.append(f"  {card['decided']}")
    lines.append(f"  status: {_STATUS_WORDS.get(card['status'], card['status'])}")
    estimate = card.get("estimate") or {}
    if estimate.get("bytes"):
        lines.append(f"  to download: {_gb(estimate['bytes'])}")
    if estimate.get("basis") == "unknown":
        lines.append("  how long: unknown until it starts")
    elif estimate.get("seconds"):
        lines.append(f"  about {int(estimate['seconds'])}s "
                     f"({estimate['basis']})")
    for reason in card.get("consequential_reasons") or ():
        lines.append(f"  worth reading: {_REASON_WORDS.get(reason, reason)}")
    # A run of info fields is one quiet block: a label is printed once, and a
    # line repeating the label above it prints its text alone, so the starter
    # catalog's suggestions do not print "Suggested" over every sentence of a
    # list nobody can answer.
    last_info_label = ""
    headed = False
    for field in card.get("fields") or ():
        if field["kind"] == "info":
            label = str(field.get("label") or "")
            show = not headed and label != last_info_label
            lines.extend(_field_lines(field, repeat_label=show))
            # A label with nothing beside it heads the lines under it, which
            # then need no label of their own.
            headed = headed or not field.get("current")
            last_info_label = label or last_info_label
            continue
        last_info_label, headed = "", False
        lines.extend(_field_lines(field))
    for gap in card.get("gaps") or ():
        lines.append(f"  gap: {gap['what']} {gap['why']}".rstrip())
        command = fixes.fix_line(gap)
        if command:
            lines.append(f"    fix: {command}")
    return [ascii_only(line) for line in lines]


def _gb(size: float) -> str:
    """One unit convention for the whole wizard (``cards.format_bytes``)."""
    return cards.format_bytes(size) or f"{int(size)} bytes"


def _shown(field: dict) -> str:
    kind = field["kind"]
    value = field.get("proposed")
    if kind == "toggle":
        return "yes" if value else "no"
    if kind == "multi_choice":
        return ", ".join(value or ()) or "nothing yet"
    if kind == "secret_entry":
        return "not set"
    if value in (None, ""):
        return "not set"
    return str(value)


def _field_lines(field: dict, *, repeat_label: bool = True) -> list[str]:
    if field["kind"] == "info":
        lines = [f"  {field['label']}"] if repeat_label else []
        if field.get("current"):
            lines.append(f"    {field['current']}")
        return lines
    lines = [f"  {field['label']}: {_shown(field)}"]
    if field.get("help"):
        lines.append(f"    {field['help']}")
    # The menu is part of the card, not part of the prompt. A ``--yes`` run
    # never reaches the prompt, and an owner reading the transcript afterwards
    # still has to be able to see what they were offered and did not pick.
    for line in _option_lines(field):
        lines.append(line)
    return lines


#: How many options a console prints before it says how many more there are.
MAX_SHOWN_OPTIONS = 12


def _option_lines(field: dict) -> list[str]:
    options = field.get("options") or ()
    if not options:
        return []
    lines = [f"    - {option['label']}"
             for option in options[:MAX_SHOWN_OPTIONS]]
    extra = len(options) - MAX_SHOWN_OPTIONS
    if extra > 0:
        lines.append(f"    - and {extra} more")
    if field.get("free_text"):
        lines.append("    - or type your own")
    return lines


class ConsoleShowOnce:
    """The console as a place a value can be shown once and never stored.

    Two methods and no state. It is handed lines and prints them; it is handed
    a prompt and reads one line back. It keeps no copy of either, which is what
    makes it usable for a recovery code: the only record of what crossed this
    object is the owner's eyes and whatever they wrote down.

    The block is fenced with the same marks a card is, so everything this
    renderer puts on the screen can be lifted out whole by one rule.
    """

    def __init__(self, *, out: Callable[[str], None] = print,
                 ask: Callable[[str], str | None] | None = None) -> None:
        self._out = out
        self._ask = ask or _read_line

    @staticmethod
    def _safe(text: Any) -> str:
        """Printable, capped, and still indented.

        ``cards.plain`` collapses runs of spaces, which is right for a label on
        a card and wrong here: the code is shown indented on purpose. So this
        drops the characters that could repaint a terminal and keeps the shape.
        """
        raw = ascii_only(text if isinstance(text, str) else str(text))
        clean = "".join(ch for ch in raw
                        if ch == " " or (ch.isprintable() and ch != "\x7f"))
        return clean[:cards.MAX_TEXT_CAP]

    def show(self, lines) -> None:
        self._out(BEGIN_MARK)
        for line in lines or ():
            self._out(self._safe(line))
        self._out(END_MARK)

    def ask(self, prompt: str) -> str | None:
        return self._ask(self._safe(prompt)[:cards.MAX_LABEL])


#: The two answers the resume screen takes, and what a blank line means.
RESUME_CHOICES = ("continue", "restart")


def how_long_ago(seconds: float) -> str:
    """"12 minutes ago". Plain words, no library, no locale to get wrong."""
    seconds = max(0.0, float(seconds or 0))
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds >= size:
            count = int(seconds // size)
            return f"{count} {unit}{'s' if count != 1 else ''} ago"
    return "a moment ago"


def resume_lines(summary: dict) -> list[str]:
    """The stopped-run screen, before anything is asked. Pure, so it is tested.

    One screen, shown once, before the first card. It says three things and no
    more: when the last setup stopped, which question it stopped on, and what
    was already decided -- because "continue" is only a fair offer if the owner
    can see what they would be keeping.
    """
    when = how_long_ago(summary.get("age_s"))
    stopped = cards.plain(summary.get("stopped_title"), cards.MAX_TITLE) \
        or "an unnamed step"
    lines = [f"A setup from {when} stopped at {stopped}.",
             "Continue from there, or start over?", ""]
    decided = [row for row in (summary.get("decided") or ())
               if isinstance(row, dict)]
    if decided:
        lines.append("  Already decided:")
        for row in decided:
            title = cards.plain(row.get("title"), cards.MAX_TITLE) or "a step"
            lines.append(f"    - {title}")
    else:
        lines.append("  Nothing was decided before it stopped.")
    lines.append("")
    lines.append("  Continuing re-checks the vault and the brain against this "
                 "machine and asks again if they are not there.")
    lines.append("  Starting over keeps a copy of the old record and asks "
                 "everything from the top.")
    return [ascii_only(line) for line in lines]


def resume_screen(summary: dict, *, ask: Callable[[str], str | None] | None = None,
                  out: Callable[[str], None] = print) -> str:
    """Show the screen and return ``"continue"`` or ``"restart"``.

    **Fail closed on silence.** End-of-file, a stopped pipe, Ctrl-C at the
    prompt and an answer this screen does not understand all mean *restart*:
    continuing skips questions, and skipping a question nobody answered is the
    one outcome this screen exists to prevent.
    """
    reader = ask or _read_line
    out(BEGIN_MARK)
    for line in resume_lines(summary):
        out(line)
    raw = reader("  continue or start over -- continue / over")
    out(END_MARK)
    answer = (raw or "").strip().lower()
    if answer in ("continue", "c", "resume", "yes", "y"):
        return "continue"
    return "restart"


def render(card: dict, *, non_interactive: bool = False,
           ask: Callable[[str], str] | None = None,
           ask_secret: Callable[[str], str] | None = None,
           out: Callable[[str], None] = print) -> dict[str, Any]:
    """Show *card*, collect answers, return only what the owner changed.

    Answering is opt-in per field: an empty line keeps what was proposed, which
    is why the return value is the changes and not the whole card's worth of
    values. A refused answer is said and re-asked once; a second refusal keeps
    the proposal rather than looping at somebody who wants to move on.
    """
    card = cards.validate_card(card)
    # Rendering registers nothing anywhere. The face a skill may show a value
    # once through is the runner's, handed to each step on its RunContext
    # (``Runner.show_once``). A console face installed by drawing a card would
    # last for the rest of the *process*, and a long-lived server could inherit
    # it.
    out(BEGIN_MARK)
    for line in card_lines(card):
        out(line)
    answers: dict[str, Any] = {}
    if not non_interactive:
        reader = ask or _read_line
        secret_reader = ask_secret or _read_secret
        for field in card["fields"]:
            if field["kind"] in cards.READ_ONLY_KINDS:
                continue
            value = _ask_field(card, field, reader, secret_reader, out)
            if value is not None:
                answers[field["id"]] = value
    out(END_MARK)
    return answers


def _ask_field(card: dict, field: dict, reader, secret_reader, out) -> Any:
    kind = field["kind"]
    prompt = _prompt_for(field)
    for attempt in (1, 2):
        raw = (secret_reader if kind == "secret_entry" else reader)(prompt)
        if raw is None:
            return None
        raw = raw if kind == "secret_entry" else raw.strip()
        if not raw:
            return None
        try:
            return cards.validate_answers(card, {field["id"]: _parse(field, raw)})[
                field["id"]]
        except cards.CardError as exc:
            if attempt == 2:
                out(ascii_only(f"    Keeping what was proposed: {exc}"))
                return None
            out(ascii_only(f"    {exc}"))
    return None  # pragma: no cover - the loop returns first


def _prompt_for(field: dict) -> str:
    return ascii_only(_prompt_text(field))


def _prompt_text(field: dict) -> str:
    kind = field["kind"]
    if kind in ("choice", "multi_choice"):
        offered = ", ".join(option["value"] for option in field["options"])
        typed = ", or type one" if field.get("free_text") else ""
        many = " (comma separated)" if kind == "multi_choice" else ""
        return (f"  {field['label']}{many} -- {offered}{typed} "
                f"(blank to keep)")
    if kind == "toggle":
        return f"  {field['label']} -- yes or no (blank to keep)"
    if kind == "number":
        return (f"  {field['label']} -- {field['min']} to {field['max']} "
                f"(blank to keep)")
    if kind == "secret_entry":
        return f"  {field['label']} (not shown as you type, blank to skip)"
    return f"  {field['label']} (blank to keep)"


def _parse(field: dict, raw: str) -> Any:
    """Text off a terminal into the type the card asked for. Never raises."""
    kind = field["kind"]
    if kind == "toggle":
        lowered = raw.lower()
        if lowered in ("y", "yes", "true", "1"):
            return True
        if lowered in ("n", "no", "false", "0"):
            return False
        return raw
    if kind == "number":
        try:
            return float(raw) if "." in raw else int(raw)
        except ValueError:
            return raw
    if kind == "multi_choice":
        return [part.strip() for part in raw.split(",") if part.strip()]
    return raw


#: What the owner types after. On its own short line, because the question is
#: printed above it: ``input(prompt)`` writes no newline, so without it three
#: questions answered with Enter run together on one unreadable visual line.
ANSWER_MARK = "  > "


def _stdin_is_tty() -> bool:
    """Is there a real terminal on the other end of stdin?"""
    try:
        return bool(sys.stdin is not None and sys.stdin.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def _read_line(prompt: str) -> str | None:
    try:
        print(prompt)
        return input(ANSWER_MARK)
    except (EOFError, KeyboardInterrupt):
        return None


def _read_secret(prompt: str) -> str | None:
    """Read one secret. Never echoes on a terminal, and never blocks off one.

    ``getpass`` on Windows reads the console handle directly
    (``msvcrt.getwch``) whenever stdin looks like the process's own -- which a
    pipe or a redirected file does -- so a scripted ``puffpai setup`` asked a
    keyboard nobody was at and waited forever. With no tty the line reader is
    used instead: there is no echo to worry about on a pipe (nothing is drawn on
    a screen), and end-of-file is "no answer", which the caller treats as
    leaving the proposal alone. It never hangs.
    """
    try:
        print(prompt)
        if not _stdin_is_tty():
            return input(ANSWER_MARK)
        return getpass.getpass(ANSWER_MARK)
    except (EOFError, KeyboardInterrupt):
        return None
    except Exception:
        # No terminal to turn echo off on, and not a pipe either. Refusing is
        # the honest answer: a secret typed in the clear on a screen is a secret
        # in somebody's scrollback.
        return None


__all__ = ["BEGIN_MARK", "END_MARK", "RESUME_CHOICES", "ConsoleShowOnce",
           "ascii_only", "card_lines", "how_long_ago", "render",
           "resume_lines", "resume_screen", "strip_block"]
