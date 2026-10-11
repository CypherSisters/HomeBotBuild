"""Drive ``puffpai setup`` the way a person does: read the screen, then answer.

Every other interactive test in this tree fed the wizard a fixed list of lines.
That works for exactly one schedule: this build's is eighteen steps with
uncarded runner steps (household, storage, Home Assistant) between the cards,
and the recovery ceremony reads *zero to three* extra lines depending on
whether the code that was shown once came back. A fixed list therefore lands
its answers on the wrong questions, silently, and the failure shows up six
steps later as a manifest that says something nobody typed.

So this module does not count prompts. It reads the child's stdout as it
arrives, notices when the child is *waiting* -- output that stopped on a
partial line -- and answers **the question that partial line belongs to**,
chosen from a rules table keyed on the question's own words.

Two prompt shapes exist in this build and both are handled without knowing
which is which:

* the card renderer prints the question on its own line and then reads with
  :data:`puffpai.wizard.render_cli.ANSWER_MARK` (``"  > "``) as the prompt, so
  the waiting tail is the answer mark and the question is the line above it;
* the uncarded steps (``setup_cli._ask``, ``_ask_yes_no``, ``_read_pin``) read
  with the whole question as the prompt, so the waiting tail *is* the question.

Detection is "the buffer has not grown for :data:`QUIET_S` and does not end in
a newline". That is deliberately ignorant of both shapes, so a step that grows
a new kind of prompt is still driven rather than silently skipped.

Secrets: the hidden prompts are readable from a pipe only because of the
documented test switch ``PUFFPAI_SETUP_STDIN_SECRETS=1``
(:data:`puffpai.setup_cli.STDIN_SECRETS_ENV`). Nothing is echoed either way,
and this driver keeps its own log of what it answered -- so a caller that logs
the run must redact it itself. :func:`Result.log_text` does.

Copyright (c) Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: How long the child has to be silent, mid-line, before it counts as waiting.
#: The census shells out and the Ollama probe waits on a refused connection,
#: but both of those print whole lines; only a real prompt stops mid-line.
QUIET_S = 0.4

#: Ceiling on one run. A wizard that asks nothing for this long is stuck, and
#: the transcript is dumped rather than the test simply timing out.
DEFAULT_TIMEOUT_S = 600.0

#: Ceiling on how many questions one run may ask. A loop that re-asks the same
#: refused answer forever would otherwise hang until the timeout.
MAX_PROMPTS = 400

#: How many times one question may be asked before the run counts as stuck.
#: A step that re-asks forever (a PIN confirmation the rules never match) is a
#: test bug, and it should read as one instead of running until the clock does.
MAX_SAME_QUESTION = 12

#: The recovery code as the show-once screen draws it: groups of characters,
#: hyphenated, alone on an indented line. Format-agnostic on purpose -- this
#: build mints its own code and a test that pinned one grouping would pass for
#: the wrong reason.
CODE_RE = re.compile(r"^\s{2,}([A-Z0-9]{3,6}(?:-[A-Z0-9]{3,6}){3,7})\s*$",
                     re.MULTILINE)


class WizardStuck(AssertionError):
    """The wizard asked something the rules had no answer for, or hung."""


@dataclass
class Turn:
    """One question and what was typed back."""

    question: str
    answer: str
    secret: bool = False

    @property
    def shown_answer(self) -> str:
        return "<redacted>" if self.secret else self.answer


@dataclass
class Result:
    returncode: int
    transcript: str
    turns: list[Turn] = field(default_factory=list)

    @property
    def stdout(self) -> str:
        """The name the older tests used. The child's streams are merged."""
        return self.transcript

    @property
    def stderr(self) -> str:
        return ""

    def log_text(self) -> str:
        """The prompt->answer log, with the secret answers redacted."""
        return "\n".join(f"{n:>3}. {turn.question}\n     -> {turn.shown_answer!r}"
                         for n, turn in enumerate(self.turns, 1))

    def recovery_code(self) -> str:
        """The one code the show-once screen drew, or "" if it drew none."""
        found = CODE_RE.findall(self.transcript)
        return found[0] if found else ""


@dataclass
class Ask:
    """What a rule is handed: the question, the run so far, and the repeat."""

    question: str
    transcript: str
    #: 1 the first time this exact question is asked, 2 the second, and so on.
    #: This is how a re-ask is answered differently from the first attempt --
    #: and how a paragraph prompt is ended with a blank line after its text.
    repeat: int
    #: Everything printed since the last answer: the menu this question belongs
    #: to. The linear walk asks numbered menus with the bare prompt "Choose",
    #: so the only thing that says WHICH menu is the block above it.
    context: str = ""

    def recovery_code(self) -> str:
        found = CODE_RE.findall(self.transcript)
        return found[0] if found else ""


#: A rule is ``(pattern, answer)``. *answer* is a string, a sequence of strings
#: (one per repeat of the same question, the last one sticking), or a callable
#: taking an :class:`Ask`. The first rule whose pattern is found in the
#: question wins; a question no rule matches is answered with a blank line,
#: which every prompt in this wizard reads as "keep what you proposed".
Rule = tuple[str, Any]


def resolve(rules: Sequence[Rule], ask: Ask) -> str | None:
    """The answer for *ask*, or ``None`` when no rule claims the question.

    Two passes, and the order matters. A rule that names the question wins
    outright; only if none does is the question's own menu (everything printed
    since the last answer) searched. Without the second pass a bare "Choose"
    prompt is unanswerable; with it first, a precise rule would lose to a word
    that happened to appear in the paragraph above.
    """
    for pattern, answer in rules:
        if re.search(pattern, ask.question, re.IGNORECASE):
            return _value(answer, ask)
    if _is_generic(ask.question):
        for pattern, answer in rules:
            if ask.context and re.search(pattern, ask.context, re.IGNORECASE):
                return _value(answer, ask)
    return None


#: A prompt that carries its own question is a sentence; "Choose", "Token" and
#: "Choose a model" are labels on a block printed above them. Only the labels
#: are answered from their block: letting a long prompt be matched against the
#: paragraph above it made a card whose help text mentioned downloading answer
#: the question about downloading.
MAX_GENERIC_WORDS = 4


def _is_generic(question: str) -> bool:
    return len(re.findall(r"[A-Za-z]+", question)) <= MAX_GENERIC_WORDS


def _value(answer: Any, ask: Ask) -> str:
    if callable(answer):
        return str(answer(ask) or "")
    if isinstance(answer, (list, tuple)):
        if not answer:
            return ""
        index = min(ask.repeat, len(answer)) - 1
        return _value(answer[index], ask)
    return str(answer if answer is not None else "")


def base_env(root: str | os.PathLike[str], **extra: str) -> dict[str, str]:
    """The environment every driven run needs. No keyring, no bytecode, no net.

    *root* holds the whole install: ``state`` is the data dir and ``config``
    the config dir, so a test can grep the entire tree for a secret afterwards.
    """
    root = str(root)
    env = dict(os.environ)
    env.update({
        "PUFFPAI_DATA_DIR": os.path.join(root, "state"),
        "PUFFPAI_CONFIG_DIR": os.path.join(root, "config"),
        "PYTHONPATH": os.path.join(REPO, "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
        "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
        # Documented test-only switch: on Windows getpass reads the console
        # device and ignores a pipe, so without this the hidden prompts could
        # not be scripted at all.
        "PUFFPAI_SETUP_STDIN_SECRETS": "1",
    })
    env.update(extra)
    return env


def install_root(tmp_path, name: str):
    """A whole install's worth of state, isolated to one run."""
    root = tmp_path / name
    (root / "state").mkdir(parents=True, exist_ok=True)
    (root / "config").mkdir(parents=True, exist_ok=True)
    return root


#: Questions whose answers must never be logged in the clear.
_SECRET_RE = re.compile(r"\bPIN\b|token|password|passphrase|not shown as you type",
                        re.IGNORECASE)

#: A recovery code, as it is typed back in.
_CODE_ANSWER_RE = re.compile(r"[A-Z0-9]{3,6}(?:-[A-Z0-9]{3,6}){3,7}\Z")


def _is_secret(question: str, answer: str, prior: list) -> bool:
    """Is this answer one the log must redact?

    A confirmation prompt ("Type it again") names no secret and neither does
    the prompt the recovery code is typed back at, so the question's words are
    not enough: an answer is also secret when it is code-shaped, or when it
    repeats something already given to a prompt that was.
    """
    if _SECRET_RE.search(question):
        return True
    if not answer:
        return False
    if _CODE_ANSWER_RE.match(answer):
        return True
    return any(turn.secret and turn.answer == answer for turn in prior)


def drive(rules: Sequence[Rule], *, env: dict[str, str], args: Iterable[str] = (),
          timeout: float = DEFAULT_TIMEOUT_S, cwd: str = REPO,
          on_turn: Callable[[Turn], None] | None = None,
          strict: bool = False) -> Result:
    """Run ``puffpai setup`` to completion, answering from *rules*.

    ``strict`` makes an unmatched question fatal instead of blank-answered;
    the default is blank, because "keep what you proposed" is a real answer
    and a new optional question should not break every test at once.
    """
    argv = [sys.executable, "-m", "puffpai", "setup", *args]
    proc = subprocess.Popen(
        argv, cwd=cwd, env=env, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=False, bufsize=0,
    )
    reader = _Reader(proc.stdout)
    turns: list[Turn] = []
    asked: dict[str, int] = {}
    deadline = time.monotonic() + timeout
    # Where the last answer was typed. Everything printed after it is the
    # question now being asked -- which matters because the uncarded steps
    # prompt with ``input(question)``, writing no newline, and a piped answer
    # echoes nothing: four such prompts in a row are one line on screen.
    mark = 0

    try:
        while True:
            state = reader.wait(proc, deadline)
            if state == "exited":
                break
            if state == "timeout":
                raise WizardStuck(_dump("the wizard stopped answering",
                                        reader.text, turns))
            text = reader.text
            question = _question(text, mark)
            if turns and not _said_anything_new(text, mark):
                # A fresh answer mark and not one new word: the same reader is
                # asking again, which is what a multi-line answer looks like
                # (``setup_cli._ask_paragraph``). Its documented end is a blank
                # line, and consulting the rules here would answer the question
                # above it a second time -- forever.
                answer = ""
            else:
                asked[question] = asked.get(question, 0) + 1
                ask = Ask(question=question, transcript=text,
                          repeat=asked[question], context=text[mark:])
                answer = resolve(rules, ask)
                if answer is None:
                    if strict:
                        raise WizardStuck(_dump(
                            f"no rule for: {question!r}", reader.text, turns))
                    answer = ""
            turn = Turn(question=question, answer=answer,
                        secret=_is_secret(question, answer, turns))
            turns.append(turn)
            if on_turn is not None:
                on_turn(turn)
            if len(turns) > MAX_PROMPTS:
                raise WizardStuck(_dump("the wizard would not stop asking",
                                        reader.text, turns))
            mark = len(reader.text)
            try:
                proc.stdin.write((answer + "\n").encode("utf-8"))
                proc.stdin.flush()
            except OSError:
                break
        try:
            proc.stdin.close()
        except OSError:
            pass
        code = proc.wait(timeout=max(1.0, deadline - time.monotonic()))
    except BaseException:
        proc.kill()
        proc.wait(timeout=30)
        raise
    finally:
        reader.join()

    return Result(returncode=code, transcript=reader.text, turns=turns)


def _dump(why: str, transcript: str, turns: list[Turn]) -> str:
    """Everything a failure needs: why, what was answered, what was on screen."""
    log = "\n".join(f"{n:>3}. {t.question}\n     -> {t.shown_answer!r}"
                    for n, t in enumerate(turns, 1))
    return (f"{why}\n\n--- answered so far ---\n{log}\n\n"
            f"--- last of the transcript ---\n{transcript[-6000:]}")


def _said_anything_new(text: str, mark: int) -> bool:
    """Did the child print a word since the last answer, or only a new mark?"""
    return any(line.strip().strip(" >") for line in text[mark:].splitlines())


def _question(text: str, mark: int = 0) -> str:
    """The question the child is waiting on: what it printed since *mark*.

    *mark* is where the previous answer was typed, so this is exactly the new
    output -- never the leftover answer mark of the question before it, and
    never four uncarded prompts run together on one unbroken line.

    Two shapes come out of that slice. A carded question is its own line
    followed by the renderer's answer mark, so the mark is dropped and the line
    above it is the question. An uncarded question *is* the last line, because
    it was written as the prompt. A re-prompt after a refused answer prints
    only a fresh answer mark, in which case the question is the last line with
    words on it -- the one being re-asked.
    """
    fresh = text[mark:]
    lines = [line.strip() for line in fresh.splitlines()]
    for line in reversed(lines):
        if line.strip(" >"):
            return line.lstrip("> ").strip() if line.startswith(">") else line
    # Nothing new but an answer mark: the question above it is being re-asked.
    for line in reversed([l.strip() for l in text.splitlines()]):
        if line.strip(" >"):
            return line
    return fresh.strip()


class _Reader:
    """Read the child's merged output into one growing string, in a thread."""

    def __init__(self, stream) -> None:
        self._stream = stream
        self._chunks: list[bytes] = []
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        try:
            while True:
                chunk = self._stream.read(1)
                if not chunk:
                    break
                with self._lock:
                    self._chunks.append(chunk)
        except (OSError, ValueError):
            pass
        finally:
            self._done.set()

    @property
    def text(self) -> str:
        with self._lock:
            raw = b"".join(self._chunks)
        return raw.decode("utf-8", "replace").replace("\r\n", "\n")

    def size(self) -> int:
        with self._lock:
            return len(self._chunks)

    def wait(self, proc: subprocess.Popen, deadline: float) -> str:
        """Block until the child waits for a line, exits, or the clock runs out.

        Returns ``"prompt"``, ``"exited"`` or ``"timeout"``.
        """
        last_size, quiet_since = self.size(), time.monotonic()
        while True:
            if time.monotonic() > deadline:
                return "timeout"
            time.sleep(0.05)
            size = self.size()
            if size != last_size:
                last_size, quiet_since = size, time.monotonic()
                continue
            if self._done.is_set() and proc.poll() is not None:
                return "exited"
            if time.monotonic() - quiet_since < QUIET_S:
                continue
            with self._lock:
                ended_a_line = not self._chunks or self._chunks[-1] in (b"\n", b"\r")
            if not ended_a_line:
                return "prompt"
            # Output that stopped on a whole line is a step doing work, not a
            # question. Keep waiting; the deadline is the only ceiling.
            quiet_since = time.monotonic()

    def join(self) -> None:
        self._thread.join(timeout=10)


class Screen:
    """A tee for ``sys.stdout``: what a run prints, kept and readable mid-run.

    The subprocess driver reads the child's screen to find the recovery code.
    A run driven in process needs the same thing, and ``capsys`` only hands the
    output back after the run is over -- too late to type a code back in.
    """

    def __init__(self, inner=None) -> None:
        self._inner = inner if inner is not None else sys.stdout
        self._parts: list[str] = []

    def write(self, text) -> int:
        self._parts.append(str(text))
        return len(str(text))

    def flush(self) -> None:
        pass

    @property
    def text(self) -> str:
        return "".join(self._parts)


def scripted_input(rules: Sequence[Rule], screen: Screen,
                   *, turns: list[Turn] | None = None) -> Callable[..., str]:
    """A stand-in for :func:`input` that answers from *rules*, not from a list.

    Same contract as :func:`drive`, for a run that happens in this process:
    the question is the prompt it was handed, or -- when the prompt is only the
    renderer's answer mark -- the last line with words on it on *screen*.
    """
    asked: dict[str, int] = {}
    log = turns if turns is not None else []
    mark = [0]

    def _reader(prompt: str = "") -> str:
        question = str(prompt).strip()
        continuation = False
        if not question.strip(" >"):
            # Nothing printed since the last answer and no question in the
            # prompt: this is a multi-line answer being continued, and its
            # documented end is a blank line. See :func:`drive`.
            continuation = bool(log) and not _said_anything_new(
                screen.text, mark[0])
            for line in reversed(screen.text.splitlines()):
                if line.strip().strip(" >"):
                    question = line.strip()
                    break
        here, mark[0] = mark[0], len(screen.text)
        if continuation:
            log.append(Turn(question=question, answer=""))
            return ""
        asked[question] = asked.get(question, 0) + 1
        if asked[question] > MAX_SAME_QUESTION:
            raise WizardStuck(_dump(f"asked {question!r} over and over",
                                    screen.text, log))
        ask = Ask(question=question, transcript=screen.text,
                  repeat=asked[question], context=screen.text[here:])
        answer = resolve(rules, ask) or ""
        log.append(Turn(question=question, answer=answer,
                        secret=_is_secret(question, answer, log)))
        return answer

    return _reader


def tree_text(root) -> str:
    """Every readable file under an install, concatenated. For secret hunting."""
    blob = []
    for base, _dirs, files in os.walk(str(root)):
        for name in files:
            try:
                with open(os.path.join(base, name), "r", encoding="utf-8",
                          errors="ignore") as fh:
                    blob.append(fh.read())
            except OSError:
                continue
    return "\n".join(blob)


__all__ = ["Ask", "CODE_RE", "REPO", "Result", "Rule", "Screen", "Turn",
           "WizardStuck", "base_env", "drive", "install_root", "resolve",
           "scripted_input", "tree_text"]
