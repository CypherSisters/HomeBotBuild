/**
 * Recognise a Bridge refusal from one of the dashboard's own server routes
 * (radio, TuneIn, the page reader). Those routes answer
 * `{ ok: false, reason: "bridge_closed" | "bridge_unavailable", switch, error }`
 * when the switch is off or could not be checked, and `error` is the sentence
 * to show: it names the switch and where to turn it on.
 *
 * Client-safe: no server imports. The server side is
 * app/_lib/server/bridgeSwitch.ts.
 */
export function bridgeRefusalMessage(body: unknown): string | null {
  if (!body || typeof body !== "object") return null;
  const b = body as { reason?: unknown; error?: unknown };
  if (b.reason !== "bridge_closed" && b.reason !== "bridge_unavailable") {
    return null;
  }
  return typeof b.error === "string" && b.error ? b.error : null;
}
