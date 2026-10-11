/**
 * The chat stream's wire format, and nothing else.
 *
 * Pure module: no fetch client, no React, so the parser can be unit-tested
 * under Node's type stripping (app/_lib/__tests__/stream-parse.test.mjs) the
 * way the other pure helpers here are. chatService re-exports it.
 */

import type { ChatSource } from '../_interfaces/api';

/**
 * May a failed turn be re-sent down the non-streaming path?
 *
 * ONLY if the backend never accepted it. Once it answered 2xx it is running
 * the turn — tool calls, house lights, whatever the agent decided — and a
 * second copy would run all of that again. A timeout is NOT permission: the
 * slow turn is still running. (The Chat page states the same rule at its own
 * fallback; this is the version the Talk page can be tested on.)
 */
export function mayResend(outcome: { accepted?: boolean } | null): boolean {
  return !outcome?.accepted;
}

export interface StreamMeta {
  sources: ChatSource[];
  suggestions: string[];
  /**
   * Only the /chat/stream route reports it. The Talk page needs it to keep
   * one conversation across turns — without it the backend mints a new id per
   * turn and Puff loses the thread of what was just said.
   */
  conversationId?: string;
  /**
   * The backend served this turn but did NOT store it: a vault PIN, or an
   * answer read out of the vault. The client must not store it either — the
   * Chat page's save-back is how the PIN reached the transcript on disk.
   * Absent/false from a backend too old to say; see `complete`.
   */
  private?: boolean;
  /**
   * A well-formed terminal event arrived (`done:true`, or the `[DONE]`
   * sentinel). False means the stream was cut short, so `private` says
   * nothing either way and a caller that persists turns must not persist
   * this one. Every live backend ends a good stream with one of the two, so
   * this is not a version check.
   */
  complete?: boolean;
}

/** One decoded SSE `data:` payload, in whichever shape the backend sent. */
export interface StreamEvent {
  /** Reply text to append. */
  content?: string;
  /** Model reasoning to append — display only, never spoken or stored. */
  thinking?: string;
  /** The backend says the turn is over. */
  done?: boolean;
  conversationId?: string;
  meta?: StreamMeta;
  /** Terminal event: this turn must not be persisted by the client. */
  private?: boolean;
}

/**
 * Decode one `data:` payload.
 *
 * TWO SHAPES, ON PURPOSE. The dashboard streamed against the OpenAI-compatible
 * /v1/chat/completions route (`choices[0].delta.content`); the appliance's own
 * /chat/stream answers `{conversation_id, delta, thinking, done}`. Both are
 * live on different boxes, and an older backend simply never sends `thinking`,
 * so this reads whichever arrives and ignores what it doesn't recognise.
 * Returns null for a line that carries nothing (keepalives, junk, half a JSON
 * object that slipped through) — the caller treats null as "nothing happened",
 * never as an error.
 */
export function parseStreamEvent(data: string): StreamEvent | null {
  const raw = data.trim();
  if (!raw) return null;
  if (raw === '[DONE]') return { done: true };

  let parsed: any;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!parsed || typeof parsed !== 'object') return null;

  const ev: StreamEvent = {};

  // PuffPAI /chat/stream: content and thinking arrive as separate events, and
  // thinking events carry an empty `delta` alongside.
  if (typeof parsed.delta === 'string' && parsed.delta) ev.content = parsed.delta;
  if (typeof parsed.thinking === 'string' && parsed.thinking) {
    ev.thinking = parsed.thinking;
  }

  // OpenAI-compatible /v1/chat/completions. reasoning_content is what the
  // qwen-style servers put thinking in when they expose it at all.
  const odelta = parsed.choices?.[0]?.delta;
  if (odelta && typeof odelta === 'object') {
    if (typeof odelta.content === 'string' && odelta.content) {
      ev.content = (ev.content ?? '') + odelta.content;
    }
    if (typeof odelta.reasoning_content === 'string' && odelta.reasoning_content) {
      ev.thinking = (ev.thinking ?? '') + odelta.reasoning_content;
    }
  }

  if (parsed.done === true) ev.done = true;
  if (typeof parsed.conversation_id === 'string') {
    ev.conversationId = parsed.conversation_id;
  }

  // Sources/suggestions ride the final event, under puffpai_meta on the
  // OpenAI route and at the top level on /chat/stream.
  const metaSrc = parsed.puffpai_meta ?? parsed;

  // Save-back privacy. Top level on /chat/stream, inside puffpai_meta on the
  // OpenAI-compatible route (which is the one the Chat page streams from).
  // Only `true` is read: absent is an older backend, and the caller decides
  // what to do about that — see StreamMeta.complete.
  if (parsed.private === true || metaSrc?.private === true) ev.private = true;

  if (Array.isArray(metaSrc.sources) || Array.isArray(metaSrc.suggestions)) {
    ev.meta = {
      sources: Array.isArray(metaSrc.sources) ? metaSrc.sources : [],
      suggestions: Array.isArray(metaSrc.suggestions) ? metaSrc.suggestions : [],
    };
  }

  return Object.keys(ev).length > 0 ? ev : null;
}
