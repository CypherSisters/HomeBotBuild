/**
 * Which backend paths /api/pai is allowed to carry the appliance's key to.
 *
 * WHY THIS EXISTS, GIVEN THAT PAIRING ALREADY GATES THE ROUTE
 * -----------------------------------------------------------
 * The pairing cookie answers "may this device use the proxy at all". It does
 * not answer "for what". app/api/pai/[...path] is a catch-all: it joins
 * whatever segments arrive onto the backend origin and attaches
 * `Authorization: Bearer <PUFFPAI_API_KEY>`. So a paired device — a kid's
 * phone, a laptop that pairs once and is later borrowed or compromised, an
 * XSS payload running on the dashboard's own origin — reaches EVERY endpoint
 * the backend has or will ever have, not just the ones the UI exposes. That is
 * a much wider blast radius than the dashboard needs.
 *
 * This is the second half of least privilege: pairing decides WHO, the list
 * below decides WHAT. Anything off it gets 404 — not 403, so a prober learns
 * nothing about which backend paths exist.
 *
 * It is defence in depth, not a replacement for the gate. Removing the pairing
 * check and keeping this list would leave the allowlisted surface wide open to
 * the whole LAN; the two are stacked deliberately.
 *
 * ADDING A PREFIX
 * ---------------
 * Add the first backend path segment, alphabetically, with a comment naming
 * the caller. app/_lib/server/__tests__/proxy-guard.test.mjs re-derives the
 * prefixes the dashboard actually calls from source and fails if one is
 * missing, so a new feature cannot silently 404 — but the fix is a deliberate
 * addition here, after asking whether that call belongs in a browser at all.
 *
 * Pure module: no next/server import, so the plain-node tests can import it
 * directly under Node's TypeScript type stripping.
 *
 * puff puff pass
 */

/**
 * First backend path segment → allowed. Exact match, case-sensitive: every
 * backend route is lowercase, so accepting "HA" would only ever be a way to
 * spell a blocked path differently on a case-insensitive comparison.
 */
export const PROXY_ALLOWED_PREFIXES: readonly string[] = [
  "agents", //        app/_lib/services/agentService.ts      /agents/status|activity|task
  "announcements", // app/_components/AnnouncementToast.tsx  /announcements
  "api", //           NowPlayingTile, Connect*Card           /api/media|spotify/* (backend's own /api/* routes)
  "apps", //          public/*-app.html, appService.ts       /apps/<app>/data|action|updated, /apps/generate|ocr
  "automation", //    app/_lib/services/automationService.ts /automation/status|trigger
  "bridge", //        app/dashboard/bridge/* + firewall integrations   /bridge/*
  // First-run proofs. Every backend /build/* route today is /build/prove*;
  // the pairing gate covers /api/pai/build/prove and /build/self at the
  // shipped default, and the backend asks for the first-run code on them.
  "build", //         app/_lib/services/buildService.ts      /build/prove
  "capabilities", //  settings/SkillsCapabilityCard.tsx      /capabilities/extend (read-only)
  "chat", //          app/_lib/services/chatService.ts       /chat/history|send|save
  "dashboard", //     dashboard/page.tsx, WidgetBoard.tsx    /dashboard/widgets|cameras|weather
  "devices", //       DeviceDiscoveryToast, haService        /devices/pending
  "firewall", //      app/_lib/services/firewallService.ts   /firewall/*
  "fitness", //       app/dashboard/page.tsx                 /fitness/program
  "ha", //            app/_lib/services/haService.ts         /ha/*
  "hatch", //         app/dashboard/hatch/*                  /hatch/certificate
  "health", //        app/_lib/api.ts                        /health
  // The household roster and every member's picture. GET/POST/PUT/DELETE plus
  // the multipart avatar upload — the proxy relays bodies as a stream, so the
  // form boundary crosses untouched. Read of a member's avatar is a plain GET
  // of the same prefix. No method dimension exists in this list by design: the
  // pairing gate decides who, this decides what.
  "household", //     useHousehold, /dashboard/household     /household, /household/<id>/avatar*
  "ingest", //        app/dashboard/chat|documents           /ingest/* (incl. /ingest/images for the avatar picker)
  "knowledge", //     knowledgeService, /send, Shortcut      /knowledge/*
  // Persona writes are paired at the shipped default (pairing.ts
  // PERSONA_PREFIXES); the list read stays open for the kiosk.
  "persona", //       app/_lib/services/personaService.ts    /persona/list|preview|scaffold|<id>
  // Offline directions: saved route records and engine status, read-only.
  // Records carry no home point; the page drops destination lat/lon.
  "maps", //          app/dashboard/directions/*             /maps/routes/<id>|latest, /maps/status
  "profile", //       profileService, householdService       /profile, /profile/ai, /profile/avatar*
  "sentinel", //      app/_lib/services/consentService.ts    /sentinel/consent/*
  // First-run setup. The backend refuses state-changing /setup/* calls from a
  // non-local client without the console setup code, and after completion
  // without a live vault session (api_server._enforce_setup_code/_setup_gate).
  "setup", //         app/_lib/services/setupService.ts      /setup/*
  // Sandboxed skills: the list, the exact bytes of a package awaiting approval
  // (the consent card's "view code"), and the owner's off/remove verdicts.
  // Owner-facing and read-heavy, so it belongs in a browser. Installing is NOT
  // reachable from here: a skill is only ever installed by resolving a consent
  // request under "sentinel", which is where the human yes is recorded.
  "skills", //        app/_lib/services/skillService.ts      /skills, /skills/<id>/draft|disable
  "system", //        systemService, trivia-app.html         /system/status
  // "Cast to..." on the TV control bar: list the TVs linked in Home Assistant
  // and the channels, cast, stop. No codes are typed here (TVs are linked
  // once in HA). Names and states only come back; no stream URL, cast id,
  // TV address or credential.
  // The keyless /cast/<id> stream proxy is NOT reachable through here.
  "tvcast", //        dashboard/_components/TvCastPanel.tsx  /tvcast/*
  "v1", //            app/_lib/services/chatService.ts       /v1/chat/completions
  "vault", //         app/_lib/services/vaultService.ts      /vault/*
  "voice", //         app/_lib/services/voiceService.ts      /voice/*
  // The card wizard: paired at the shipped default AND coded while first-run
  // setup is open, reads included (pairing.ts WIZARD_PREFIXES, backend
  // _SETUP_CODE_PREFIXES).
  "wizard", //        app/_lib/services/wizardService.ts     /wizard/*
  "workflows", //     app/_lib/services/automationService.ts /workflows (Node-RED admin flow list)
  "zone3", //         app/dashboard/firewall zone3 policy + enforce     /zone3/*
];

const ALLOWED = new Set(PROXY_ALLOWED_PREFIXES);

/**
 * Segment sanity, applied to EVERY segment, not just the first.
 *
 * Next has already percent-decoded each catch-all segment by the time we see
 * it, so this runs on the real bytes that would be re-joined into the upstream
 * URL. A segment containing "/" or "\" means the client encoded a separator to
 * make the reconstructed path differ from the one the pairing gate inspected;
 * "." and ".." mean traversal; a control character or NUL means someone is
 * probing the upstream parser. None has a legitimate spelling in a backend
 * path, so all fail closed. (The route also rejects encoded separators
 * explicitly, before this runs — the check is duplicated on purpose.)
 */
const SAFE_SEGMENT = /^[A-Za-z0-9._~@:-]+$/;

function segmentIsSane(seg: string): boolean {
  if (!seg) return false; // empty segment: "//" collapsed, or a trailing slash
  if (seg === "." || seg === "..") return false;
  if (seg.length > 256) return false;
  return SAFE_SEGMENT.test(seg);
}

/**
 * True if the proxy may forward these already-decoded path segments.
 *
 * Fails closed on anything it does not positively recognise: no segments at
 * all, an unlisted first segment, or any segment that is not a plain path
 * component.
 */
export function isAllowedProxyPath(segments: readonly string[]): boolean {
  if (!Array.isArray(segments) || segments.length === 0) return false;
  for (const seg of segments) {
    if (typeof seg !== "string" || !segmentIsSane(seg)) return false;
  }
  return ALLOWED.has(segments[0]);
}

/** 404, deliberately: a prober learns nothing about the backend's real shape. */
export function notAllowedResponse(): Response {
  return new Response(JSON.stringify({ error: "not_found" }), {
    status: 404,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
  });
}
