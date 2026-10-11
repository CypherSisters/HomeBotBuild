"use client";

import { useMemo } from "react";
import {
  mdiBrain,
  mdiTrophy,
  mdiStar,
  mdiLightningBolt,
  mdiHistory,
  mdiChartBar,
  mdiPlayCircle,
  mdiGamepadVariant,
  mdiEarth,
  mdiMovie,
  mdiMusic,
  mdiFootball,
  mdiLaptop,
  mdiLeaf,
  mdiBookOpenVariant,
  mdiShuffle,
  mdiSchool,
} from "@mdi/js";
import SectionTitleLineWithButton from "@/app/_components/Section/TitleLineWithButton";
import CardBox from "@/app/_components/CardBox";
import Icon from "@/app/_components/Icon";
import { useAppData } from "@/app/_lib/hooks/useAppData";
import {
  AppButton,
  AppShell,
  StatRow,
  ErrorState,
  LoadingState,
  RowSkeleton,
  EmptyState,
  T,
} from "../_kit";

/* ------------------------------------------------------------------ */
/*  Types                                                              */
/* ------------------------------------------------------------------ */

type TriviaResult = {
  date: string;
  mode: string;
  category: string;
  difficulty: string;
  score: number;
  bestStreak: number;
  totalQuestions: number;
};

type TriviaData = {
  results: TriviaResult[];
};

/* ------------------------------------------------------------------ */
/*  Constants                                                          */
/* ------------------------------------------------------------------ */

const MODE_META: Record<string, { label: string; icon: string; color: string }> = {
  brain: {
    label: "Brain",
    icon: mdiBrain,
    color: "bg-[color-mix(in_srgb,var(--lav)_16%,transparent)] text-[var(--lav)]",
  },
  speed: {
    label: "Speed",
    icon: mdiLightningBolt,
    color: "bg-[color-mix(in_srgb,var(--lemon)_16%,transparent)] text-[var(--lemon)]",
  },
  survival: {
    label: "Survival",
    icon: mdiStar,
    color: "bg-[color-mix(in_srgb,var(--puff-red)_16%,transparent)] text-[var(--puff-red)]",
  },
  duel: {
    label: "Duel",
    icon: mdiGamepadVariant,
    color: "bg-[color-mix(in_srgb,var(--sky)_16%,transparent)] text-[var(--sky)]",
  },
};

const CATEGORY_META: Record<string, { label: string; icon: string; color: string }> = {
  science: { label: "Science", icon: mdiSchool, color: "bg-[color-mix(in_srgb,var(--sky)_16%,transparent)] text-[var(--sky)]" },
  history: { label: "History", icon: mdiHistory, color: "bg-[color-mix(in_srgb,var(--lemon)_16%,transparent)] text-[var(--lemon)]" },
  geography: { label: "Geography", icon: mdiEarth, color: "bg-[color-mix(in_srgb,var(--mint)_16%,transparent)] text-[var(--mint)]" },
  movies: { label: "Movies", icon: mdiMovie, color: "bg-[color-mix(in_srgb,var(--puff-red)_16%,transparent)] text-[var(--puff-red)]" },
  music: { label: "Music", icon: mdiMusic, color: "bg-[color-mix(in_srgb,var(--lav)_16%,transparent)] text-[var(--lav)]" },
  sports: { label: "Sports", icon: mdiFootball, color: "bg-[color-mix(in_srgb,var(--lemon)_16%,transparent)] text-[var(--lemon)]" },
  technology: { label: "Technology", icon: mdiLaptop, color: "bg-[color-mix(in_srgb,var(--sky)_16%,transparent)] text-[var(--sky)]" },
  nature: { label: "Nature", icon: mdiLeaf, color: "bg-[color-mix(in_srgb,var(--mint)_16%,transparent)] text-[var(--mint)]" },
  literature: { label: "Literature", icon: mdiBookOpenVariant, color: "bg-[color-mix(in_srgb,var(--lav)_16%,transparent)] text-[var(--lav)]" },
  random: { label: "Random", icon: mdiShuffle, color: "bg-[var(--chip)] text-[var(--text2)]" },
};

const DIFFICULTY_COLORS: Record<string, string> = {
  easy: "bg-[color-mix(in_srgb,var(--mint)_16%,transparent)] text-[var(--mint)]",
  mid: "bg-[color-mix(in_srgb,var(--lemon)_16%,transparent)] text-[var(--lemon)]",
  hard: "bg-[color-mix(in_srgb,var(--puff-red)_16%,transparent)] text-[var(--puff-red)]",
};

function formatDate(iso: string): string {
  try {
    return new Date(iso).toLocaleDateString(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

/* ------------------------------------------------------------------ */
/*  Page                                                               */
/* ------------------------------------------------------------------ */

export default function TriviaPage() {
  const { data, loading, error, reload } = useAppData<TriviaData>("trivia");

  /* ---- Computed stats ---- */
  const stats = useMemo(() => {
    const results = data?.results ?? [];
    if (results.length === 0) {
      return {
        totalGames: 0,
        avgScore: 0,
        bestScore: 0,
        bestCategory: "",
        bestStreak: 0,
        favCategory: "",
      };
    }

    const totalGames = results.length;
    const avgScore = Math.round(
      results.reduce((s, r) => s + r.score, 0) / totalGames,
    );

    let bestScore = 0;
    let bestCategory = "";
    let bestStreak = 0;

    for (const r of results) {
      if (r.score > bestScore) {
        bestScore = r.score;
        bestCategory = r.category;
      }
      if (r.bestStreak > bestStreak) {
        bestStreak = r.bestStreak;
      }
    }

    /* Favorite category = most played */
    const catCounts: Record<string, number> = {};
    for (const r of results) {
      catCounts[r.category] = (catCounts[r.category] || 0) + 1;
    }
    const favCategory = Object.entries(catCounts).sort(
      (a, b) => b[1] - a[1],
    )[0]?.[0] ?? "";

    return { totalGames, avgScore, bestScore, bestCategory, bestStreak, favCategory };
  }, [data?.results]);

  /* ---- Category breakdown ---- */
  const categoryBreakdown = useMemo(() => {
    const results = data?.results ?? [];
    const map: Record<string, { count: number; totalScore: number }> = {};
    for (const r of results) {
      if (!map[r.category]) map[r.category] = { count: 0, totalScore: 0 };
      map[r.category].count += 1;
      map[r.category].totalScore += r.score;
    }
    return Object.entries(map)
      .map(([cat, val]) => ({
        category: cat,
        count: val.count,
        avgScore: Math.round(val.totalScore / val.count),
      }))
      .sort((a, b) => b.count - a.count);
  }, [data?.results]);

  const maxCatCount = Math.max(1, ...categoryBreakdown.map((c) => c.count));

  /* Sorted results (newest first) */
  const sortedResults = useMemo(
    () => {
      const results = data?.results ?? [];
      return [...results].sort(
        (a, b) => new Date(b.date).getTime() - new Date(a.date).getTime(),
      );
    },
    [data?.results],
  );

  /* ---- loading / error ---- */
  /* Loading and error keep the app's own shape — a stats page still looks
     like a stats page with no numbers in it. */
  const shellActions = (
    <AppButton
      icon={mdiPlayCircle}
      label="Quick Play"
      tone="primary"
      small
      roundedFull
      href="/dashboard/apps/trivia/play"
    />
  );

  if (loading) {
    return (
      <AppShell title="Trivia" hint="Scores and history" actions={shellActions}>
        <LoadingState skeleton={<RowSkeleton rows={5} />} line="Loading scores" />
      </AppShell>
    );
  }

  if (error) {
    return (
      <AppShell title="Trivia" hint="Scores and history" actions={shellActions}>
        <ErrorState
          skeleton={<RowSkeleton rows={5} />}
          line="Scores are not loading right now."
          detail={error}
          onRetry={reload}
        />
      </AppShell>
    );
  }

  return (
    <AppShell title="Trivia" hint="Scores and history" actions={shellActions}>
      {/* ============================================================== */}
      {/*  STATS DASHBOARD                                                */}
      {/* ============================================================== */}
      <SectionTitleLineWithButton icon={mdiTrophy} title="Stats Overview">
        <span className="puff-cap">{stats.totalGames} games played</span>
      </SectionTitleLineWithButton>

      <StatRow
        stats={[
          {
            label: "Games Played",
            icon: mdiGamepadVariant,
            value: stats.totalGames,
            tone: T.lav,
          },
          { label: "Avg Score", icon: mdiChartBar, value: stats.avgScore, tone: T.sky },
          {
            label: "Best Score",
            icon: mdiTrophy,
            value: stats.bestScore,
            tone: T.lemon,
            sub: stats.bestCategory
              ? CATEGORY_META[stats.bestCategory]?.label ?? stats.bestCategory
              : undefined,
          },
          {
            label: "Best Streak",
            icon: mdiLightningBolt,
            value: stats.bestStreak,
            tone: T.lemon,
          },
          {
            label: "Favorite",
            icon: CATEGORY_META[stats.favCategory]?.icon ?? mdiStar,
            value: CATEGORY_META[stats.favCategory]?.label || "N/A",
            tone: T.mint,
          },
        ]}
      />

      {/* ============================================================== */}
      {/*  CATEGORY BREAKDOWN                                             */}
      {/* ============================================================== */}
      <SectionTitleLineWithButton icon={mdiChartBar} title="Category Breakdown">
        <span className="text-sm text-[var(--text3)]">
          {categoryBreakdown.length} categories
        </span>
      </SectionTitleLineWithButton>

      {categoryBreakdown.length === 0 ? (
        <EmptyState
          skeleton={<RowSkeleton rows={3} />}
          line="No category stats yet."
          say={'say "quiz me"'}
        />
      ) : (
        <div className="mb-6 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {categoryBreakdown.map((cat) => {
            const meta = CATEGORY_META[cat.category];
            return (
              <CardBox key={cat.category} isHoverable>
                <div className="flex items-center gap-3">
                  <div
                    className={`flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-xl ${
                      meta?.color ?? "bg-[var(--chip)] text-[var(--text2)]"
                    }`}
                  >
                    <Icon path={meta?.icon ?? mdiStar} size="20" />
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center justify-between">
                      <span className="text-sm font-semibold text-[var(--text)]">
                        {meta?.label ?? cat.category}
                      </span>
                      <span className="text-xs text-[var(--text3)]">
                        {cat.count} games | avg {cat.avgScore}
                      </span>
                    </div>
                    <div className="mt-1.5 h-2 w-full overflow-hidden rounded-full bg-[var(--chip)]">
                      <div
                        className="h-full rounded-full bg-[var(--lav)] transition-all"
                        style={{
                          width: `${(cat.count / maxCatCount) * 100}%`,
                        }}
                      />
                    </div>
                  </div>
                </div>
              </CardBox>
            );
          })}
        </div>
      )}

      {/* ============================================================== */}
      {/*  GAME HISTORY TABLE                                             */}
      {/* ============================================================== */}
      <SectionTitleLineWithButton icon={mdiHistory} title="Game History">
        <span className="text-sm text-[var(--text3)]">
          Newest first
        </span>
      </SectionTitleLineWithButton>

      {sortedResults.length === 0 ? (
        <EmptyState
          skeleton={<RowSkeleton rows={6} />}
          line="No games yet."
          say={'say "quiz me"'}
        />
      ) : (
        <CardBox hasTable>
          <div className="puff-scroll-x">
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="border-b border-[var(--line)]">
                  <th className="px-4 py-3 font-semibold text-[var(--text2)]">
                    Date
                  </th>
                  <th className="px-4 py-3 font-semibold text-[var(--text2)]">
                    Mode
                  </th>
                  <th className="px-4 py-3 font-semibold text-[var(--text2)]">
                    Category
                  </th>
                  <th className="px-4 py-3 font-semibold text-[var(--text2)]">
                    Difficulty
                  </th>
                  <th className="px-4 py-3 text-right font-semibold text-[var(--text2)]">
                    Score
                  </th>
                  <th className="px-4 py-3 text-right font-semibold text-[var(--text2)]">
                    Streak
                  </th>
                  <th className="px-4 py-3 text-right font-semibold text-[var(--text2)]">
                    Q&apos;s
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[var(--line)]">
                {sortedResults.map((r, idx) => {
                  const mode = MODE_META[r.mode];
                  const cat = CATEGORY_META[r.category];
                  const diff = DIFFICULTY_COLORS[r.difficulty] ?? DIFFICULTY_COLORS.mid;
                  return (
                    <tr
                      key={idx}
                      className="transition hover:bg-[var(--card)]"
                    >
                      <td className="whitespace-nowrap px-4 py-3 text-[var(--text2)]">
                        {formatDate(r.date)}
                      </td>
                      <td className="px-4 py-3">
                        <span
                          className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ${
                            mode?.color ?? "bg-[var(--chip)] text-[var(--text2)]"
                          }`}
                        >
                          <Icon path={mode?.icon ?? mdiBrain} size="12" />
                          {mode?.label ?? r.mode}
                        </span>
                      </td>
                      <td className="px-4 py-3">
                        <span
                          className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ${
                            cat?.color ?? "bg-[var(--chip)] text-[var(--text2)]"
                          }`}
                        >
                          <Icon path={cat?.icon ?? mdiStar} size="12" />
                          {cat?.label ?? r.category}
                        </span>
                      </td>
                      <td className="px-4 py-3">
                        <span
                          className={`rounded-full px-2 py-0.5 text-xs font-bold uppercase ${diff}`}
                        >
                          {r.difficulty}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-right font-bold text-[var(--lav)]">
                        {r.score.toLocaleString()}
                      </td>
                      <td className="px-4 py-3 text-right text-[var(--text2)]">
                        {r.bestStreak}
                      </td>
                      <td className="px-4 py-3 text-right text-[var(--text2)]">
                        {r.totalQuestions}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </CardBox>
      )}
    </AppShell>
  );
}
