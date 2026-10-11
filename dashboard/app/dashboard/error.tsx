"use client";

import { useEffect } from "react";
import Button from "@/app/_components/Button";
import CardBox from "@/app/_components/CardBox";
import { mdiAlertCircle, mdiRefresh } from "@mdi/js";
import Icon from "@/app/_components/Icon";

export default function DashboardError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    console.error("Dashboard error:", error);
  }, [error]);

  return (
    <div className="flex items-center justify-center min-h-[60vh] p-6">
      <CardBox className="max-w-md w-full text-center p-8">
        <Icon path={mdiAlertCircle} size={48} className="text-red-500 mx-auto mb-4" />
        <h2 className="text-xl font-bold mb-2">Something went wrong</h2>
        <p className="text-gray-600 dark:text-gray-400 mb-4">
          {error.message || "An unexpected error occurred"}
        </p>
        <Button
          icon={mdiRefresh}
          label="Try Again"
          color="info"
          onClick={reset}
        />
      </CardBox>
    </div>
  );
}
