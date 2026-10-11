"use client";

import { useRouter } from "next/navigation";
import { mdiArrowLeft } from "@mdi/js";
import Icon from "@/app/_components/Icon";

export default function CalendarManagePage() {
  const router = useRouter();

  return (
    <div className="relative flex h-screen flex-col bg-[var(--bg)]">
      <div className="flex items-center gap-3 px-4 py-2 bg-[var(--railBg)] backdrop-blur-sm border-b border-[var(--line)] z-10">
        <button
          onClick={() => router.push("/dashboard/apps/calendar")}
          className="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm font-medium text-[var(--text2)] transition hover:bg-[var(--card)] hover:text-[var(--text)]"
        >
          <Icon path={mdiArrowLeft} size="18" />
          Back to Overview
        </button>
        <span className="text-sm font-semibold text-[var(--text)]">
          Calendar
        </span>
      </div>
      <iframe
        src="/calendar-app.html"
        className="flex-1 w-full border-0"
        title="Calendar"
        allow="autoplay; microphone"
      />
    </div>
  );
}
