"use client";

import { useRouter } from "next/navigation";
import { mdiArrowLeft } from "@mdi/js";
import Icon from "@/app/_components/Icon";

export default function AlarmFlowManagePage() {
  const router = useRouter();

  return (
    <div className="relative flex h-screen flex-col bg-[var(--bg)]">
      <div className="flex items-center gap-3 px-4 py-2 bg-[var(--railBg)] backdrop-blur-sm border-b border-[var(--line)] z-10">
        <button
          onClick={() => router.push("/dashboard/apps/alarmflow")}
          className="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm font-medium text-[var(--text2)] transition hover:bg-[var(--card)] hover:text-[var(--text)]"
        >
          <Icon path={mdiArrowLeft} size="18" />
          Back to Overview
        </button>
        <span className="text-sm font-semibold text-[var(--text)]">
          ⏰ AlarmFlow
        </span>
      </div>
      <iframe
        src="/alarm-app.html"
        className="flex-1 w-full border-0"
        title="AlarmFlow Timer & Alarms"
        allow="autoplay; microphone"
      />
    </div>
  );
}
