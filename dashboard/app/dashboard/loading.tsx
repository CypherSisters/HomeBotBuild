export default function DashboardLoading() {
  return (
    <div className="p-6">
      {/* Top progress bar */}
      <div className="fixed inset-x-0 top-14 z-50 h-1 overflow-hidden bg-gray-100 dark:bg-slate-800">
        {/* puff-motion: arbitrary-value animate-[...] escapes the motion
            governor's selector list, so opt in via its hook class. */}
        <div className="puff-motion h-full w-1/3 animate-[shimmer_1.5s_ease-in-out_infinite] rounded-r-full bg-gradient-to-r from-[#00B4D8] via-[#0096C7] to-[#00B4D8]" />
      </div>

      {/* Hero skeleton */}
      <div className="mb-6 animate-pulse rounded-2xl bg-gradient-to-r from-gray-200 via-gray-100 to-gray-200 p-6 dark:from-slate-800 dark:via-slate-700 dark:to-slate-800" style={{ height: 160 }} />

      {/* Section title skeleton */}
      <div className="mb-4 flex items-center gap-3">
        <div className="h-6 w-6 rounded bg-gray-200 dark:bg-slate-700" />
        <div className="h-6 w-40 rounded bg-gray-200 dark:bg-slate-700" />
      </div>

      {/* Cards skeleton */}
      <div className="mb-6 grid grid-cols-1 gap-6 sm:grid-cols-2 lg:grid-cols-4">
        {[1, 2, 3, 4].map((i) => (
          <div
            key={i}
            className="animate-pulse rounded-2xl bg-white/70 p-6 shadow-sm backdrop-blur-sm dark:bg-slate-900/50"
            style={{ animationDelay: `${i * 100}ms` }}
          >
            <div className="mb-3 flex items-center justify-between">
              <div className="h-5 w-16 rounded-full bg-gray-200 dark:bg-slate-700" />
              <div className="h-7 w-7 rounded bg-gray-200 dark:bg-slate-700" />
            </div>
            <div className="flex items-center justify-between">
              <div>
                <div className="mb-2 h-4 w-20 rounded bg-gray-200 dark:bg-slate-700" />
                <div className="h-8 w-16 rounded bg-gray-200 dark:bg-slate-700" />
              </div>
              <div className="h-12 w-12 rounded-full bg-gray-200 dark:bg-slate-700" />
            </div>
          </div>
        ))}
      </div>

      {/* Quick actions skeleton */}
      <div className="mb-4 flex items-center gap-3">
        <div className="h-6 w-6 rounded bg-gray-200 dark:bg-slate-700" />
        <div className="h-6 w-32 rounded bg-gray-200 dark:bg-slate-700" />
      </div>
      <div className="mb-6 grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-5">
        {[1, 2, 3, 4, 5].map((i) => (
          <div
            key={i}
            className="animate-pulse rounded-2xl bg-white/70 p-4 shadow-sm backdrop-blur-sm dark:bg-slate-900/50"
            style={{ animationDelay: `${i * 80}ms` }}
          >
            <div className="flex flex-col items-center gap-2">
              <div className="h-12 w-12 rounded-full bg-gray-200 dark:bg-slate-700" />
              <div className="h-4 w-16 rounded bg-gray-200 dark:bg-slate-700" />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
