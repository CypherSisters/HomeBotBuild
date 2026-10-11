"use client";

import React, { ReactNode } from "react";

type EmptyProps = {
  /**
   * The app's own structure with zero rows — an empty week grid, an empty
   * alarm list. An empty Calendar must still look like a calendar; a
   * shared grey box is not an empty state.
   */
  skeleton?: ReactNode;
  /** One short line saying what is missing. */
  line: string;
  /** One utterance that fills it, e.g. `say "add milk to the list"`. */
  say?: string;
  /** Optional control, e.g. "Add the first one". */
  action?: ReactNode;
};

export function EmptyState({ skeleton, line, say, action }: EmptyProps) {
  return (
    <div className="puff-state">
      {skeleton ? (
        <div className="puff-state-skeleton" aria-hidden="true">
          {skeleton}
        </div>
      ) : null}
      <div className="puff-state-msg">
        <span>{line}</span>
        {say ? <span className="puff-state-say">{say}</span> : null}
        {action}
      </div>
    </div>
  );
}

type ErrorProps = {
  /** Same skeleton as the empty state: the app's shape survives a dead backend. */
  skeleton?: ReactNode;
  /**
   * What actually failed, in this app's words. Never the shared
   * "PuffPAI backend unreachable" sentence rendered byte-identically
   * across pages.
   */
  line: string;
  /** The raw error, shown small after the line. Optional. */
  detail?: string | null;
  /** Retry handler. Rendered as a >=48px control. */
  onRetry?: () => void;
  retryLabel?: string;
};

export function ErrorState({ skeleton, line, detail, onRetry, retryLabel = "Try again" }: ErrorProps) {
  return (
    <div className="puff-state">
      {skeleton ? (
        <div className="puff-state-skeleton" aria-hidden="true">
          {skeleton}
        </div>
      ) : null}
      <div className="puff-state-msg puff-state-msg-error" role="status">
        <span>{line}</span>
        {detail ? <span className="puff-state-say">{detail}</span> : null}
        {onRetry ? (
          <button type="button" className="puff-tap" onClick={onRetry}>
            {retryLabel}
          </button>
        ) : null}
      </div>
    </div>
  );
}

/**
 * LoadingState — the app's structure with a low-contrast skeleton over it.
 *
 * `animate-pulse` is in the motion governor's ambient bucket
 * (css/_theme.css:240+), so it freezes while the wall is idle and unmounts
 * the moment data lands. Nothing here loops forever.
 */
export function LoadingState({ skeleton, line = "Loading" }: { skeleton?: ReactNode; line?: string }) {
  return (
    <div className="puff-state">
      {skeleton ? (
        <div className="puff-state-skeleton animate-pulse" aria-hidden="true">
          {skeleton}
        </div>
      ) : null}
      <div className="puff-state-msg">
        <span>{line}</span>
      </div>
    </div>
  );
}

/**
 * RowSkeleton — a generic "list-shaped nothing" for apps whose structure is
 * a list. Apps with a richer shape (a week grid, a board) pass their own.
 */
export function RowSkeleton({ rows = 4 }: { rows?: number }) {
  return (
    <div className="flex flex-col gap-2">
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="puff-row">
          <span className="puff-row-initial">&nbsp;</span>
          <span className="puff-row-label">&nbsp;</span>
          <span className="puff-row-meta">&nbsp;</span>
        </div>
      ))}
    </div>
  );
}
