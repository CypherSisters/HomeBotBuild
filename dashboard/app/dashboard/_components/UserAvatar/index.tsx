/* eslint-disable @next/next/no-img-element */
// Why disabled:
// When a call site supplies an explicit (user-uploaded) image URL we render
// it with a plain <img>; next/image would force remote-loader / domain config
// for what is a purely local-first app.

import React, { ReactNode } from "react";

type Props = {
  username: string;
  /** Optional explicit avatar image (e.g. a user-uploaded photo). When set it
   *  is rendered directly; otherwise a deterministic local initials avatar is
   *  generated — no network request either way. */
  imageUrl?: string;
  className?: string;
  children?: ReactNode;
};

// Dark cyber-theme gradient pairs. A hash of the seed picks one so a given
// user always gets the same colors, with no external service involved.
const GRADIENTS: [string, string][] = [
  ["#22d3ee", "#8b5cf6"], // cyan -> violet
  ["#ec4899", "#f59e0b"], // pink -> amber
  ["#10b981", "#06b6d4"], // emerald -> cyan
  ["#a855f7", "#ec4899"], // purple -> pink
  ["#3b82f6", "#22d3ee"], // blue -> cyan
  ["#f43f5e", "#a855f7"], // rose -> purple
];

function hashString(value: string): number {
  let hash = 0;
  for (let i = 0; i < value.length; i++) {
    hash = (hash * 31 + value.charCodeAt(i)) | 0;
  }
  return Math.abs(hash);
}

function getInitials(username: string): string {
  const name = (username.split("@")[0] ?? "").trim();
  if (!name) return "?";
  const parts = name.split(/[\s._-]+/).filter(Boolean);
  if (parts.length >= 2) {
    return (parts[0][0] + parts[1][0]).toUpperCase();
  }
  return name.slice(0, 2).toUpperCase();
}

export default function UserAvatar({
  username,
  imageUrl,
  className = "",
  children,
}: Props) {
  // NOT useId(): its value depends on tree position, and this component's
  // position differs between the server and client passes (hydration error
  // seen on every dashboard load). The username hash is identical on both
  // sides; duplicate ids across same-user avatars reference identical
  // gradient defs, which is harmless.
  const gradientId = `puffgrad-${hashString(username).toString(36)}`;
  const [from, to] = GRADIENTS[hashString(username) % GRADIENTS.length];
  const initials = getInitials(username);

  return (
    <div className={className}>
      {imageUrl ? (
        <img
          src={imageUrl}
          alt={username ?? undefined}
          className="block h-auto w-full max-w-full rounded-full bg-gray-100 dark:bg-slate-800"
        />
      ) : (
        <svg
          viewBox="0 0 100 100"
          role="img"
          aria-label={username || undefined}
          className="block h-full w-full max-w-full rounded-full"
        >
          <defs>
            <linearGradient id={gradientId} x1="0" y1="0" x2="1" y2="1">
              <stop offset="0%" stopColor={from} />
              <stop offset="100%" stopColor={to} />
            </linearGradient>
          </defs>
          <rect width="100" height="100" fill={`url(#${gradientId})`} />
          <text
            x="50"
            y="50"
            dominantBaseline="central"
            textAnchor="middle"
            fontSize="42"
            fontWeight="600"
            fontFamily="system-ui, -apple-system, sans-serif"
            fill="#ffffff"
          >
            {initials}
          </text>
        </svg>
      )}
      {children}
    </div>
  );
}
