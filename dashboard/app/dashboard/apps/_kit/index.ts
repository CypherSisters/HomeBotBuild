/**
 * The shared kit for the five HomeBot app routes.
 *
 * All five pages render their frame, their states and their rows from here,
 * so the wall and the phone get one vocabulary instead of five private
 * design systems. Everything paints from theme tokens: no `dark:` utility
 * and no literal hex crosses this boundary.
 */
export { default as AppShell } from "./AppShell";
export { default as AppButton } from "./AppButton";
export type { Tone } from "./AppButton";
export { EmptyState, ErrorState, LoadingState, RowSkeleton } from "./States";
export { StatRow, ListRow, Caption, Chip, SampleBadge } from "./Rows";
export type { Stat } from "./Rows";
export { T, tint, PERSON_TOKENS, personColor, initialOf, ROLE_PLACEHOLDERS } from "./tokens";
export type { ToneName } from "./tokens";
