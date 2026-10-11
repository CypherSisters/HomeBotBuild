"""One definition of "the turns before this one".

Two shapes of conversation history exist in this codebase and they are not
interchangeable:

* ``[{"role": "user"|"assistant", "content": ...}]`` — the OpenAI/Ollama wire
  shape, and what unit tests tend to write by hand.
* ``[{"user": ..., "assistant": ...}]`` — the PAIRED shape every production
  caller actually passes.  ``api_server._convert_openai_messages_to_history``
  (api_server.py:2281) folds the wire shape into it, and all four chat entry
  points hand that on: ``/v1/chat/completions``, ``/api/chat`` (voice),
  ``/chat/send`` and ``/chat/stream``.

A reader that only understands ``msg["role"]`` (the meal-write fastpath, the
voice fragment guard) sees an empty conversation on every real request:
``extract_recipes`` returns ``[]`` and "Add both of those salmon recipes…"
falls back to the agent route, the exact failure the fastpath exists to
stop, while tests written in the other shape still pass.

So: one normaliser, used by both, accepting either shape.
"""

from __future__ import annotations

_ASSISTANT_ROLES = ("assistant", "ai", "puff", "bot")


def normalise_history(history) -> list:
    """``[(role, content), …]`` oldest first, from EITHER history shape.

    Roles come back lower-cased and collapsed to ``"user"``/``"assistant"``;
    anything else (a system turn, say) is dropped, as are empty contents.
    """
    out: list = []
    for msg in (history or []):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role is not None:
            role = str(role).lower()
            content = msg.get("content")
            if not str(content or "").strip():
                continue
            if role in _ASSISTANT_ROLES:
                out.append(("assistant", str(content)))
            elif role == "user":
                out.append(("user", str(content)))
            continue
        # Paired shape: one dict is a whole turn, user first.
        user = msg.get("user")
        assistant = msg.get("assistant")
        if str(user or "").strip():
            out.append(("user", str(user)))
        if str(assistant or "").strip():
            out.append(("assistant", str(assistant)))
    return out


def assistant_messages(history) -> list:
    """Just the assistant turns, oldest first."""
    return [c for role, c in normalise_history(history) if role == "assistant"]
