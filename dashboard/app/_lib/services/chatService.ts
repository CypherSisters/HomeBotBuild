import { apiGet, apiPost, API_URL } from '../api';
import type { Conversation, ChatSource } from '../../_interfaces/api';
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
}

/** Read an SSE body, line by line, until the stream ends or `done` arrives. */
async function consumeSse(
  res: Response,
  onEvent: (ev: StreamEvent) => void
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
          await consumeSse(res, handle);
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
      await consumeSse(legacy, handle);
      onDone(finalMeta());
    } catch (err) {
      opts?.onFailure?.(err);
      onDone(finalMeta());
    }
  })();
}
