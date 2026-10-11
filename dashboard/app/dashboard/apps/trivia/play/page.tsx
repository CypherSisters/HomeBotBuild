"use client";

import { useRouter } from "next/navigation";
import { mdiArrowLeft } from "@mdi/js";
import Icon from "@/app/_components/Icon";

export default function TriviaPlayPage() {
  const router = useRouter();

  return (
    <div className="relative flex h-screen flex-col bg-[var(--bg)]">
      {/* Minimal top bar */}
      <div className="flex items-center gap-3 px-4 py-2 bg-[var(--railBg)] backdrop-blur-sm border-b border-[var(--line)] z-10">
        <button
          onClick={() => router.push("/dashboard/apps/trivia")}
          className="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm font-medium text-[var(--text2)] transition hover:bg-[var(--card)] hover:text-[var(--text)]"
        >
          <Icon path={mdiArrowLeft} size="18" />
          Back to Stats
        </button>
        <span className="text-sm font-semibold text-[var(--text)]">
          Trivia
        </span>
      </div>

      {/* Game iframe */}
      <iframe
        src="/trivia-app.html"
        className="flex-1 w-full border-0"
        title="Trivia"
        allow="autoplay"
      />
    </div>
  );
}
