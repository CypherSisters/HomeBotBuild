"use client";

/**
 * Live transcript tile (design 4b.1) — what Puff is saying RIGHT NOW, falling
 * back to the last finished exchange, plus quick-action pills that hand a
 * prompt to the chat page.
 *
 * LIVE, AND WHOLE. A finished exchange from chat history, clamped to three
 * lines, would cut a long answer off exactly where the spoken reply says
 * "I've put the rest on the screen", and miss an answer still being spoken.
 * So:
 *   • a turn in flight (typed, Talk page, or a wake-word turn at the box, via
 *     liveTurnSlice) outranks history and streams into the tile, and
 *   • the body is a scroll region, not a clamp. The whole reply is readable
 *     here; nothing is ever "continued" somewhere else.
 *
 * Prompt handoff: pills write localStorage "puffpai_chat_prefill" and
 * navigate to /dashboard/chat, which consumes+clears that key to prefill its
 * composer.
 *
 * ONE CONVERSATION. Voice, Home Assistant, Talk and the Chat page share one
 * household thread; the fallback exchange comes from it, and "Continue in
 * chat" opens it (/dashboard/chat?c=home-owner).
 */

import { useRouter } from "next/navigation";
import type { Conversation, ChatMessage } from "@/app/_interfaces/api";
import ThinkingBlock from "@/app/_components/ThinkingBlock";
import { useAppSelector } from "@/app/_stores/hooks";
import { isLiveTurnVisible } from "@/app/_stores/liveTurnSlice";
import { useAutoFollow } from "@/app/_lib/hooks/useAutoFollow";
import { HOUSEHOLD_THREAD_ID, chatThreadHref } from "@/app/_lib/householdThread";
import { useShowThinking } from "@/app/_lib/hooks/useShowThinking";
import DesignTile, { FREDOKA, JBM, NUNITO } from "./DesignTile";

/* Three, not four: at the kiosk's tile height a fourth pill wraps to a
   second row and eats a line of transcript. */
const QUICK_ACTIONS = [
  "Plan this week's meals",
  "Lock the house",
  "Summarise today",
];

/** Latest user→assistant exchange in the household thread (else the newest conversation). */
function lastExchange(conversations: Conversation[]): {
  user: ChatMessage | null;
  assistant: ChatMessage | null;
} {
  const conv =
    conversations.find((c) => c.id === HOUSEHOLD_THREAD_ID) ?? conversations[0];
  if (!conv || conv.messages.length === 0) return { user: null, assistant: null };
  const msgs = conv.messages;
  let ai = -1;
  for (let i = msgs.length - 1; i >= 0; i--) {
    if (msgs[i].role === "assistant" && msgs[i].content.trim()) {
      ai = i;
      break;
    }
  }
  if (ai === -1) {
    // No assistant reply yet — show the trailing user message alone.
    const u = [...msgs].reverse().find((m) => m.role === "user") ?? null;
    return { user: u, assistant: null };
  }
  let user: ChatMessage | null = null;
  for (let i = ai - 1; i >= 0; i--) {
    if (msgs[i].role === "user") {
      user = msgs[i];
      break;
    }
  }
  return { user, assistant: msgs[ai] };
}

/** Same derivation the topbar OperatorChip uses, so the two chips agree:
 *  one-word names give two letters ("Operator" → "OP"), multi-word names
 *  give first + last initial. */
function initialsOf(name: string): string {
  const words = name.trim().split(/\s+/).filter(Boolean);
  if (words.length === 0) return "OP";
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return (words[0][0] + words[words.length - 1][0]).toUpperCase();
}

export default function TranscriptTile({
  conversations,
  profileName,
}: {
  conversations: Conversation[];
  profileName: string;
}) {
  const router = useRouter();
  const { user, assistant } = lastExchange(conversations);

  /* A turn in flight beats history: it is what is happening in the room. */
  const live = useAppSelector((s) => s.liveTurn);
  const showThinking = useShowThinking();
  const liveOn = isLiveTurnVisible(live);

  const userText = liveOn ? live.user : (user?.content ?? "");
  const replyText = liveOn ? live.reply : (assistant?.content ?? "");
  const streaming = liveOn && live.streaming;
  const thinkingText = liveOn && showThinking ? live.thinking : "";
  const { ref: bodyRef, onScroll } = useAutoFollow<HTMLDivElement>(
    streaming ? replyText : null
  );

  // Real provenance only: the assistant message's retrieval sources. A live
  // turn has none yet — the sources land with the finished message.
  const sourceLibs = liveOn
    ? []
    : Array.from(
        new Set((assistant?.sources ?? []).map((s) => s.library).filter(Boolean))
      );

  const sendPrompt = (prompt: string) => {
    try {
      localStorage.setItem("puffpai_chat_prefill", prompt);
    } catch {
      /* private mode — chat just opens empty */
    }
    router.push("/dashboard/chat");
  };

  const msgStyle = {
    fontFamily: NUNITO,
    fontWeight: 500,
    fontSize: 15,
    lineHeight: 1.35,
  } as const;

  return (
    <DesignTile
      icon="graphic_eq"
      iconColor="var(--pink)"
      title="Live transcript"
      img="/design-cloud-3.jpg"
      padding="14px 20px"
      stat={
        <button
          type="button"
          onClick={() => router.push(chatThreadHref(HOUSEHOLD_THREAD_ID))}
          className="cursor-pointer rounded-full border px-3 transition hover:opacity-80"
          style={{
            height: 28,
            borderColor: "var(--line)",
            background: "var(--card2)",
            color: "var(--text)",
            fontFamily: NUNITO,
            fontWeight: 700,
            fontSize: 13,
          }}
        >
          Continue in chat
        </button>
      }
    >
      <div className="flex h-full min-h-0 flex-col">
        {/* The transcript body scrolls; the tile's own chrome does not. */}
        <div
          ref={bodyRef}
          onScroll={onScroll}
          tabIndex={0}
          className="puff-scroll flex min-h-0 flex-1 flex-col gap-2.5 overflow-y-auto"
        >
          {!userText && !replyText && !thinkingText ? (
            <div
              style={{
                fontFamily: NUNITO,
                fontWeight: 500,
                fontSize: 16,
                color: "var(--text2)",
              }}
            >
              No conversations yet. Say{" "}
              <span style={{ color: "var(--pink)", fontWeight: 700 }}>
                &ldquo;Hey Puff&rdquo;
              </span>{" "}
              or tap a suggestion below.
            </div>
          ) : (
            <>
              {userText && (
                <div className="flex flex-none items-start gap-3">
                  <div
                    className="grid h-8 w-8 flex-none place-items-center rounded-xl"
                    style={{
                      background: "var(--chip)",
                      fontFamily: NUNITO,
                      fontWeight: 800,
                      fontSize: 13,
                      color: "var(--text2)",
                    }}
                  >
                    {/* A wake-word turn was spoken at the box, not typed by
                        whoever this screen belongs to — say so honestly. */}
                    {liveOn && live.source === "voice"
                      ? "HEY"
                      : initialsOf(profileName)}
                  </div>
                  <div className="pt-0.5" style={{ ...msgStyle, color: "var(--text2)" }}>
                    {userText}
                  </div>
                </div>
              )}
              {(replyText || thinkingText) && (
                <div className="flex items-start gap-3">
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img
                    src="/design-cloud-1.jpg"
                    alt="Puff"
                    className="h-8 w-8 flex-none rounded-xl object-cover"
                  />
                  <div className="min-w-0 flex-1">
                    {thinkingText && (
                      <div className="mb-2">
                        <ThinkingBlock
                          text={thinkingText}
                          collapsed={replyText.length > 0}
                          maxHeight="30cqh"
                          fontSize={12}
                        />
                      </div>
                    )}
                    <div
                      aria-live="polite"
                      style={{
                        ...msgStyle,
                        color: "var(--text)",
                        whiteSpace: "pre-wrap",
                      }}
                    >
                      {replyText}
                      {streaming && <span className="puff-caret" aria-hidden />}
                    </div>
                    {sourceLibs.length > 0 && (
                      <div
                        className="mt-2 flex items-center gap-2"
                        style={{
                          fontFamily: NUNITO,
                          fontWeight: 500,
                          fontSize: 13,
                          color: "var(--text3)",
                        }}
                      >
                        <span
                          className="inline-block h-1.5 w-1.5 flex-none rounded-full"
                          style={{ background: "var(--magenta)" }}
                        />
                        <span className="truncate">
                          Answered from {sourceLibs.slice(0, 3).join(", ")}
                        </span>
                      </div>
                    )}
                  </div>
                </div>
              )}
            </>
          )}
        </div>
        {/* Quick actions are the first thing to go when the tile is short:
            @container hides the row below ~3 grid rows of height so the
            transcript itself never gets clipped mid-sentence. */}
        <div className="puff-quick-actions mt-3 flex flex-none flex-wrap gap-2">
          {QUICK_ACTIONS.map((q) => (
            <button
              key={q}
              type="button"
              onClick={() => sendPrompt(q)}
              className="h-9 cursor-pointer rounded-full border px-4 transition hover:opacity-80"
              style={{
                borderColor: "var(--line)",
                background: "var(--card2)",
                color: "var(--text)",
                fontFamily: NUNITO,
                fontWeight: 700,
                fontSize: 14,
              }}
            >
              {q}
            </button>
          ))}
        </div>
      </div>
    </DesignTile>
  );
}
