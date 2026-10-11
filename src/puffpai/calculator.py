"""Deterministic arithmetic for the ``utility.calculate`` capability.

WHY. Without it, three phrasings of the same sum go three different ways:

* ``whats 847 times 63``      -> ``intent:"plan"``, ``result:null``, "Sorry, I
  couldn't complete that one." The contraction breaks intent classification
  into a failed planner call and the user gets an apology instead of 53,361.
* ``calculate 15 percent tip on 84 dollars`` -> correct ($12.60).
* ``what is 847 multiplied by 63``          -> correct, but through the LLM,
  with RAG sources attached (a "Mental Math" Wikibooks page). Right by luck.

An appliance that advertises a calculator must not answer arithmetic by
sampling a language model. This module is the deterministic floor: it parses
the words a person actually says, evaluates with a shunting-yard parser over a
fixed operator table, and returns ``None`` for anything it cannot parse with
certainty so the existing paths keep those turns.

**No ``eval``, no ``exec``, no ``compile``.** The only characters that ever
reach the evaluator are digits, ``. , ( )`` and the operator symbols below;
everything else either maps to an operator through :data:`WORD_OPERATORS` or
makes the whole parse fail. That is a security property, not a style choice:
this input arrives from chat and from voice transcription.
"""

from __future__ import annotations

import re

#: Spoken/typed operator words, longest first so "multiplied by" is consumed
#: before "by" and "divided by" before "divided".
WORD_OPERATORS: "tuple[tuple[str, str], ...]" = (
    ("multiplied by", "*"),
    ("multiply by", "*"),
    ("divided by", "/"),
    ("divide by", "/"),
    ("to the power of", "^"),
    ("raised to", "^"),
    ("times", "*"),
    ("multiplied", "*"),
    ("plus", "+"),
    ("added to", "+"),
    ("add", "+"),
    ("minus", "-"),
    ("subtract", "-"),
    ("less", "-"),
    ("over", "/"),
    ("mod", "%"),
    ("modulo", "%"),
)

#: Words that carry no arithmetic meaning and are dropped before parsing.
_FILLER = (
    "what is", "whats", "what's", "what does", "how much is", "how much are",
    "calculate", "compute", "work out", "figure out", "tell me",
    "please", "equal", "equals", "come to", "the answer to", "the result of",
    "for me", "quick", "quickly", "give me",
)

_NUM_RE = re.compile(r'\d+(?:\.\d+)?')

#: precedence, right_associative
_OPS = {
    "+": (1, False),
    "-": (1, False),
    "*": (2, False),
    "/": (2, False),
    "%": (2, False),
    "^": (3, True),
}


class CalculatorError(ValueError):
    """The expression could not be parsed or evaluated with certainty."""


def _strip_filler(text: str) -> str:
    out = " " + re.sub(r'\s+', ' ', text.lower().strip()) + " "
    for word in _FILLER:
        out = out.replace(" " + word + " ", " ")
    return out.strip()


def normalize(text: str) -> str:
    """Turn a spoken sum into a symbolic expression string.

    ``"whats 847 times 63"`` -> ``"847 * 63"``. Percent-of is expanded here
    because "15 percent of 84" is two operations, not one operator:
    ``(15 / 100) * 84``.
    """
    low = _strip_filler(text or "")
    if not low:
        return ""
    # Percent-of / percent-tip, before the plain operator words: "15 percent
    # of 84" and "15% tip on 84 dollars" are the same arithmetic.
    low = re.sub(
        r'(\d+(?:\.\d+)?)\s*(?:%|percent|per cent)\s*'
        r'(?:tip\s*)?(?:of|on|off)\s+',
        lambda m: "( " + m.group(1) + " / 100 ) * ", low)
    for word, symbol in WORD_OPERATORS:
        low = re.sub(r'(?<![a-z])' + re.escape(word) + r'(?![a-z])',
                     " " + symbol + " ", low)
    # Common symbol spellings. "x" only between digits, so "box" survives.
    low = low.replace("×", " * ").replace("÷", " / ")
    low = re.sub(r'(?<=\d)\s*x\s*(?=\d)', " * ", low)
    # Thousands separators, currency and unit nouns people say out loud.
    low = re.sub(r'(?<=\d),(?=\d{3}\b)', "", low)
    low = re.sub(r'[$£€]', " ", low)
    low = re.sub(r'\b(dollars?|bucks?|cents?|degrees?|feet|foot|inches|'
                 r'inch|pounds?|lbs?|kg|grams?|miles?|km|percent|per cent)\b',
                 " ", low)
    low = low.replace("=", " ")
    return re.sub(r'\s+', ' ', low).strip(" ?.!,")


def tokenize(expression: str) -> list:
    """Split a symbolic expression into numbers, operators and parentheses.

    Raises :class:`CalculatorError` on any character that is not part of the
    fixed grammar — the whole-input allowlist that stands in for a sandbox.
    """
    tokens: list = []
    i = 0
    text = expression or ""
    while i < len(text):
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        match = _NUM_RE.match(text, i)
        if match:
            tokens.append(("num", float(match.group(0))))
            i = match.end()
            continue
        if ch in _OPS:
            tokens.append(("op", ch))
            i += 1
            continue
        if ch in "()":
            tokens.append((ch, ch))
            i += 1
            continue
        raise CalculatorError(f"unexpected character {ch!r}")
    return tokens


def _to_rpn(tokens: list) -> list:
    """Shunting-yard: infix tokens -> reverse Polish, with unary minus."""
    output: list = []
    stack: list = []
    prev_kind = None
    for kind, value in tokens:
        if kind == "num":
            output.append(("num", value))
        elif kind == "op":
            if value == "-" and prev_kind in (None, "op", "("):
                # Unary minus: 0 - x, so the evaluator needs no special case.
                output.append(("num", 0.0))
                stack.append("-")
            else:
                prec, right = _OPS[value]
                while stack and stack[-1] != "(":
                    top_prec, _ = _OPS[stack[-1]]
                    if top_prec > prec or (top_prec == prec and not right):
                        output.append(("op", stack.pop()))
                    else:
                        break
                stack.append(value)
        elif kind == "(":
            stack.append("(")
        elif kind == ")":
            while stack and stack[-1] != "(":
                output.append(("op", stack.pop()))
            if not stack:
                raise CalculatorError("unbalanced parentheses")
            stack.pop()
        prev_kind = kind
    while stack:
        top = stack.pop()
        if top == "(":
            raise CalculatorError("unbalanced parentheses")
        output.append(("op", top))
    return output


def _eval_rpn(rpn: list) -> float:
    stack: list = []
    for kind, value in rpn:
        if kind == "num":
            stack.append(value)
            continue
        if len(stack) < 2:
            raise CalculatorError("incomplete expression")
        b = stack.pop()
        a = stack.pop()
        if value == "+":
            stack.append(a + b)
        elif value == "-":
            stack.append(a - b)
        elif value == "*":
            stack.append(a * b)
        elif value == "/":
            if b == 0:
                raise CalculatorError("division by zero")
            stack.append(a / b)
        elif value == "%":
            if b == 0:
                raise CalculatorError("division by zero")
            stack.append(a % b)
        elif value == "^":
            if abs(b) > 64 or abs(a) > 1e9:
                raise CalculatorError("exponent out of range")
            stack.append(a ** b)
        else:                                            # pragma: no cover
            raise CalculatorError(f"unknown operator {value!r}")
    if len(stack) != 1:
        raise CalculatorError("incomplete expression")
    result = stack[0]
    if result != result or result in (float("inf"), float("-inf")):
        raise CalculatorError("result is not a finite number")
    return result


def evaluate(expression: str) -> float:
    """Evaluate a SYMBOLIC expression (no words). Raises on anything unsure."""
    tokens = tokenize(expression)
    if not tokens:
        raise CalculatorError("nothing to evaluate")
    if not any(kind == "op" for kind, _ in tokens):
        # A bare number is not a calculation; refusing keeps this off "what is
        # 42" and every other sentence that merely contains a number.
        raise CalculatorError("no operator")
    return _eval_rpn(_to_rpn(tokens))


def format_number(value: float) -> str:
    """``53361.0`` -> ``"53,361"``; ``12.6`` -> ``"12.6"``."""
    if abs(value - round(value)) < 1e-9:
        return f"{int(round(value)):,}"
    text = f"{value:,.6f}".rstrip("0").rstrip(".")
    return text or "0"


def solve(text: str) -> "tuple[str, str] | None":
    """``"whats 847 times 63"`` -> ``("847 * 63", "53,361")``, else ``None``.

    ``None`` is the honest "I am not sure this is arithmetic" answer and the
    caller falls through to its existing routing. Never raises.
    """
    try:
        expression = normalize(text)
        if not expression:
            return None
        value = evaluate(expression)
    except (CalculatorError, ValueError, OverflowError, ZeroDivisionError,
            RecursionError):
        return None
    return expression, format_number(value)
