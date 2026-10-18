import { apiGet, apiPost, apiPostRaw, API_URL } from '../api';
import type { Conversation, ChatSource, MissingCardData, FixCardData } from '../../_interfaces/api';
// The SSE dialect parser lives in a pure module so it can be unit-tested
// without pulling the fetch client in (app/_lib/__tests__/stream-parse).
// Re-exported here because every call site imports it from this service.
import {
  parseStreamEvent,
  type StreamEvent,
  type StreamMeta,
} from '../chatStream';

export { parseStreamEvent };
export type { StreamEvent, StreamMeta };

export async function getChatHistory(): Promise<Conversation[]> {
  const res = await apiGet<{ success: boolean; conversations: Conversation[] }>('/chat/history');
  return res.conversations || [];
}

/**
 * ONE thread, only the messages newer than `since` (the Chat page's cheap
 * poll of the household thread). `null` = the box holds no such thread yet.
 */
export async function getThread(id: string, since?: string): Promise<Conversation | null> {
  const q = `c=${encodeURIComponent(id)}${since ? `&since=${encodeURIComponent(since)}` : ''}`;
  const res = await apiGet<{ success: boolean; conversation: Conversation | null }>(
    `/chat/history?${q}`
  );
  return res.conversation ?? null;
}

export interface ChatSendResult {
  response: string;
  conversation_id: string;
  timestamp: string;
  sources: ChatSource[];
  suggestions: string[];
  /**
   * The backend answered but stored nothing (a vault PIN or a vault answer).
   * This route persists its own turns, so nothing here has to act on it — it
   * is surfaced so a caller that mirrors the turn anywhere durable can stop.
   */
  private: boolean;
}

export async function sendMessage(message: string, conversationId?: string): Promise<ChatSendResult> {
  const raw = await apiPost<any>('/chat/send', { message, conversation_id: conversationId });
  return {
    response: raw.response,
    conversation_id: raw.conversation_id,
    timestamp: raw.timestamp,
    sources: raw.sources || [],
    suggestions: raw.suggestions || [],
    private: raw.private === true,
  };
}

/**
 * Persist a user+assistant pair after streaming (so it shows in history).
 *
 * NOT for every turn. A turn the backend declared `private` must never be
 * posted here: the vault PIN and the answer read out of the vault were kept
 * out of the transcript by the serving route, and this call is how they were
 * getting onto disk anyway. The backend refuses such a save-back as well, but
 * the client is the half that knows it streamed one. See chat/page.tsx.
 */
export async function saveChat(
  conversationId: string,
  userMessage: string,
  assistantMessage: string
): Promise<void> {
  await apiPost('/chat/save', {
    conversation_id: conversationId,
    user_message: userMessage,
    assistant_message: assistantMessage,
  });
}

export interface StreamOptions {
  /**
   * Reasoning chunks, when the backend is sending them and the owner has the
   * setting on. Omit it and nothing changes: `show_thinking` is only asked for
   * when a handler exists to show it.
   */
  onThinking?: (chunk: string) => void;
  /** Ask the backend for reasoning. Ignored by backends that don't do it. */
  showThinking?: boolean;
  /**
   * Which streaming route to use. Default 'openai' is the route the Chat page
   * has always used and must keep: it is the only one that returns
   * sources/suggestions (puffpai_meta), and unlike /chat/stream it does not
   * persist the exchange itself — the Chat page saves it explicitly, so
   * switching that page over would write every turn to history twice.
   *
   * 'chat-stream' is the appliance's own route: it persists the turn, and it
   * is the one that carries `thinking`. Used by the Talk page, which replaced
   * a /chat/send call that already persisted.
   */
  route?: 'openai' | 'chat-stream';
  /** Which surface the stored turn is tagged with on /chat/stream. */
  surface?: 'chat' | 'talk';
  /**
   * Fired the moment the backend ACCEPTS the request (2xx, body about to
   * stream). This is the line a caller must not cross when deciding to retry:
   * once the backend has the turn it is running it — tool calls and all — so
   * re-sending would execute those actions twice. See the Talk page and
   * app/dashboard/chat/page.tsx for the house rule.
   */
  onAccepted?: () => void;
  /**
   * The stream failed (network error, abort, or no route answered). NOT fired
   * on a clean finish. onDone still follows, so a caller that ignores this
   * behaves exactly as before.
   */
  onFailure?: (err: unknown) => void;
  /** Abort the in-flight request — a timeout must cancel, not duplicate. */
  signal?: AbortSignal;
  /** The tool loop's extra events (steps, Yes/No questions, cards). Omit it
   *  and the stream reads exactly as before. */
  steps?: StreamSteps;
}

/** A question the bot's tool loop asks before it runs something (a command,
 *  a remove). The loop waits until the owner answers with approveAsk. */
export interface ChatAsk {
  id: string;
  title: string;
  command?: string;
  folder?: string;
  detail?: string;
}

/** The tool loop's extra events: a plain line while a tool runs, and a
 *  question waiting for a Yes or No. */
export interface StreamSteps {
  onProgress?: (line: string) => void;
  onAsk?: (ask: ChatAsk) => void;
  onAnswered?: (id: string, yes: boolean) => void;
  /** A command started a screen on this computer (127.0.0.1 only). */
  onOpen?: (urls: string[]) => void;
  /** A command failed for want of a program, package or key. */
  onMissing?: (card: MissingCardData) => void;
  /** Something switched off or not saved yet: a "fix it now" card. */
  onFix?: (fix: FixCardData) => void;
  /** The whole answer so far, when the bot said a line twice and kept it once. */
  onReplace?: (text: string) => void;
  /** The backend's own words when it would not answer (a JSON error). */
  onProblem?: (message: string) => void;
}

/** Hand one SSE `data:` payload's tool-loop fields to `steps`. Content,
 *  thinking and meta stay with parseStreamEvent; this reads only the rest. */
function readSteps(data: string, steps: StreamSteps): void {
  let parsed: any;
  try {
    parsed = JSON.parse(data.trim());
  } catch {
    return;
  }
  if (!parsed || typeof parsed !== 'object') return;
  if (parsed.puffpai_progress?.line) {
    steps.onProgress?.(String(parsed.puffpai_progress.line));
  }
  if (parsed.puffpai_ask?.id) {
    const a = parsed.puffpai_ask;
    steps.onAsk?.({
      id: String(a.id),
      title: String(a.title || 'May I go ahead?'),
      command: a.command ? String(a.command) : undefined,
      folder: a.folder ? String(a.folder) : undefined,
      detail: a.detail ? String(a.detail) : undefined,
    });
  }
  if (parsed.puffpai_answered?.id) {
    steps.onAnswered?.(String(parsed.puffpai_answered.id), !!parsed.puffpai_answered.yes);
  }
  if (Array.isArray(parsed.puffpai_open?.urls)) {
    steps.onOpen?.(parsed.puffpai_open.urls.map(String));
  }
  if (parsed.puffpai_missing && typeof parsed.puffpai_missing === 'object') {
    const m = parsed.puffpai_missing;
    const list = (v: unknown) => (Array.isArray(v) ? v.map(String).slice(0, 12) : []);
    steps.onMissing?.({
      plan: String(m.plan || ''),
      line: String(m.line || ''),
      steps: list(m.steps),
      manual: list(m.manual),
      keys: Array.isArray(m.keys)
        ? m.keys.slice(0, 12).map((k: any) => ({ name: String(k?.name || ''), get_url: String(k?.get_url || '') }))
        : [],
      thing: String(m.thing || ''),
    });
  }
  if (parsed.puffpai_fix && typeof parsed.puffpai_fix === 'object') {
    const f = parsed.puffpai_fix;
    const kind = String(f.kind || '');
    if (kind === 'switch_on' || kind === 'setting' || kind === 'key' || kind === 'assemble_yes') {
      steps.onFix?.({
        kind,
        label: String(f.label || ''),
        service: String(f.service || ''),
        name: String(f.name || ''),
        detail: String(f.detail || ''),
        key: String(f.key || ''),
        input: String(f.input || ''),
        get_url: String(f.get_url || ''),
        position: !!f.position,
      });
    }
  }
  if (typeof parsed.puffpai_meta?.answer === 'string' && parsed.puffpai_meta.answer) {
    steps.onReplace?.(parsed.puffpai_meta.answer);
  }
}

/** What one tap on a "fix it now" card did. `again` means ask the last
 *  question again; `position` means a rough position is needed too. */
export interface FixResult {
  ok: boolean;
  line: string;
  again: boolean;
  position: boolean;
}

/** One tap on a "fix it now" card (POST /fix/apply). */
export async function applyFix(
  fix: FixCardData,
  value?: { town?: string; latitude?: string; longitude?: string },
): Promise<FixResult> {
  const body =
    fix.kind === 'switch_on'
      ? { kind: fix.kind, service: fix.service }
      : { kind: fix.kind, key: fix.key, value: value?.town || '', latitude: value?.latitude || '', longitude: value?.longitude || '' };
  try {
    const reply = await apiPostRaw<any>('/fix/apply', body);
    const d = reply.data || {};
    return {
      ok: !!d.ok,
      line: String(d.line || d.message || (reply.ok ? 'Done.' : 'That did not work.')),
      again: !!d.again,
      position: !!d.position,
    };
  } catch {
    return { ok: false, line: 'No answer from this computer.', again: false, position: false };
  }
}

/** The card an assembled app's own page asked for (POST /fix/card). The app
 *  id is the one this dashboard page shows, never the frame's; the backend
 *  gives the words, and only for a switch the app's own sources sit behind. */
export async function fixCardForApp(
  appId: string,
  ask: { kind: string; service?: string; name?: string },
): Promise<FixCardData | null> {
  try {
    const reply = await apiPostRaw<any>('/fix/card', {
      app_id: appId,
      kind: ask.kind,
      service: ask.service || '',
      name: ask.name || '',
    });
    const f = reply.ok && reply.data && reply.data.fix;
    if (!f || typeof f !== 'object') return null;
    const kind = String(f.kind || '');
    if (kind !== 'switch_on' && kind !== 'key') return null;
    return {
      kind,
      label: String(f.label || ''),
      service: String(f.service || ''),
      name: String(f.name || ''),
      detail: String(f.detail || ''),
      key: String(f.key || ''),
      input: String(f.input || ''),
      get_url: '',
      position: false,
    };
  } catch {
    return null;
  }
}

/** The owner's Yes or No to a question from the tool loop. */
export async function approveAsk(id: string, yes: boolean): Promise<boolean> {
  try {
    const raw = await apiPost<any>('/chat/approve', { id, yes });
    return !!raw?.success;
  } catch {
    return false;
  }
}

/** Read an SSE body, line by line, until the stream ends or `done` arrives. */
async function consumeSse(
  res: Response,
  onEvent: (ev: StreamEvent) => void,
  steps?: StreamSteps
): Promise<boolean> {
  const reader = res.body?.getReader();
  if (!reader) return false;
  const decoder = new TextDecoder();
  // A chunk boundary can land mid-line; the old code split each chunk on its
  // own and silently dropped whatever straddled the seam. Keep the tail.
  let buffer = '';
  let sawEvent = false;

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop() ?? '';
    for (const line of lines) {
      if (!line.startsWith('data:')) continue;
      if (steps) readSteps(line.slice(5), steps);
      const ev = parseStreamEvent(line.slice(5));
      if (!ev) continue;
      sawEvent = true;
      onEvent(ev);
      if (ev.done) return true;
    }
  }
  if (buffer.startsWith('data:')) {
    const ev = parseStreamEvent(buffer.slice(5));
    if (ev) {
      sawEvent = true;
      onEvent(ev);
    }
  }
  return sawEvent;
}

/**
 * Stream a reply token by token.
 *
 * `opts.route` picks the backend route (see StreamOptions); 'chat-stream'
 * falls back to the OpenAI one when the box is too old to have it, so the
 * caller sees one behaviour either way. Never throws: failure is reported as
 * onDone() with no content, which is what every call site already treats as
 * "fall back to sendMessage".
 */
export function streamMessage(
  message: string,
  conversationId: string | undefined,
  onChunk: (text: string) => void,
  onDone: (meta?: StreamMeta) => void,
  opts?: StreamOptions
) {
  let meta: StreamMeta | undefined;
  let convId: string | undefined;
  // Sticky, because the two facts can arrive on different lines: the OpenAI
  // route puts `private` in the puffpai_meta chunk and then ends the stream
  // with a separate `[DONE]`.
  let isPrivate = false;
  let sawTerminal = false;

  const handle = (ev: StreamEvent) => {
    if (ev.content) onChunk(ev.content);
    if (ev.thinking && opts?.onThinking) opts.onThinking(ev.thinking);
    if (ev.meta) meta = ev.meta;
    if (ev.conversationId) convId = ev.conversationId;
    if (ev.private) isPrivate = true;
    if (ev.done) sawTerminal = true;
  };

  /** Never invent a meta object: a caller that got none must see none. */
  const finalMeta = (): StreamMeta | undefined => {
    if (!convId && !meta && !isPrivate && !sawTerminal) return undefined;
    return {
      sources: [],
      suggestions: [],
      ...meta,
      ...(convId ? { conversationId: convId } : {}),
      private: isPrivate,
      complete: sawTerminal,
    };
  };

  const post = (path: string, body: unknown) =>
    fetch(`${API_URL}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: opts?.signal,
    });

  const legacyBody = {
    model: 'puffpai',
    messages: [{ role: 'user', content: message }],
    stream: true,
    // The conversation this turn belongs to, so a "yes" or a PIN typed here
    // answers the ask Puff made in it (on any surface of the household
    // thread). `client_saves`: this page posts the finished turn to
    // /chat/save itself, so the backend must not store it a second time.
    ...(conversationId ? { conversation_id: conversationId } : {}),
    client_saves: true,
  };

  (async () => {
    try {
      if (opts?.route === 'chat-stream') {
        const res = await post('/chat/stream', {
          message,
          conversation_id: conversationId,
          ...(opts.surface ? { surface: opts.surface } : {}),
          // Only ever asked for when the caller both wants it and can show it.
          ...(opts.showThinking && opts.onThinking ? { show_thinking: true } : {}),
        });
        if (res.ok) {
          opts.onAccepted?.();
          await consumeSse(res, handle, opts.steps);
          onDone(finalMeta());
          return;
        }
        // Falls through to the OpenAI route: a box that predates /chat/stream.
        // Nothing was accepted, so nothing is running upstream.
      }
      const legacy = await post('/v1/chat/completions', legacyBody);
      if (!legacy.ok) {
        opts?.onFailure?.(new Error(legacy.statusText || 'stream refused'));
        onDone();
        return;
      }
      opts?.onAccepted?.();
      await consumeSse(legacy, handle, opts?.steps);
      onDone(finalMeta());
    } catch (err) {
      opts?.onFailure?.(err);
      onDone(finalMeta());
    }
  })();
}

/** One earlier turn of a thing page's talk. */
export interface ThingTurn {
  user: string;
  assistant: string;
}

/** "Try it" and "Ask" on a thing's page: the same stream as the chat (its
 *  steps and Yes/No questions), with that thing loaded first. *history* is
 *  the page's last few turns, oldest first, so "Yes, run it." keeps its
 *  meaning. Never throws: a failure ends in onDone() with no meta. */
export function streamThingAsk(
  thingId: string,
  text: string,
  history: ThingTurn[],
  onChunk: (text: string) => void,
  onDone: (meta?: StreamMeta) => void,
  steps?: StreamSteps
) {
  let meta: StreamMeta | undefined;
  const handle = (ev: StreamEvent) => {
    if (ev.content) onChunk(ev.content);
    if (ev.meta) meta = ev.meta;
  };
  (async () => {
    try {
      // No Authorization header: the /api/pai proxy attaches the backend key
      // server-side. The browser never holds it.
      const res = await fetch(`${API_URL}/things/${encodeURIComponent(thingId)}/ask`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text, history }),
      });
      if (!res.ok) {
        const said = await res.json().catch(() => null);
        if (said && typeof said.message === 'string' && said.message) steps?.onProblem?.(said.message);
        onDone();
        return;
      }
      await consumeSse(res, handle, steps);
      onDone(meta);
    } catch {
      onDone(meta);
    }
  })();
}
