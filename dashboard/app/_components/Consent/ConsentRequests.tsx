"use client";

/**
 * Drawbridge consent requests — the owner-facing approve/deny surface.
 * (Drawbridge is the display name; `sentinel` is the agent id the API uses.)
 *
 * Drawbridge blocks privacy-sensitive actions (putting a camera on the
 * dashboard, for one) and files a consent request instead of failing. This
 * renders those requests where the owner lands, with the two buttons that resolve them.
 *
 * Deliberately quiet: an empty queue renders nothing at all, and a pending
 * request is amber/informational, never a red alarm — it's a question, not a
 * fault.
 *
 * A first-run consent pass files one request per controllable entity, so the
 * queue can open at 40+. Past GROUP_THRESHOLD the rows collapse into device-type
 * groups with bulk verdicts. The asymmetry is on purpose: bulk DENY is one
 * click because denying is the safe direction, while "Approve everything" has
 * to be confirmed with a sentence saying what it actually grants. Per-row
 * Approve/Deny never goes away.
 *
 * puff puff pass
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  mdiShieldAccountOutline,
  mdiCheck,
  mdiClose,
  mdiChevronRight,
  mdiChevronDown,
} from "@mdi/js";
import Icon from "../Icon";
import SkillCodeModal, { PermissionList } from "../Skills/SkillCodeModal";
import type { SkillManifest } from "@/app/_lib/services/skillService";
import {
  getConsentRequests,
  resolveConsentRequest,
  resolveConsentRequestsBulk,
  groupConsentRequests,
  domainNoun,
  emitConsentChanged,
  onConsentChanged,
  getWaitingForPin,
  type ConsentRequest,
  type ConsentBulkResult,
  type WaitingForPin,
} from "@/app/_lib/services/consentService";

const POLL_MS = 30_000;

/* ── Copy helpers ──────────────────────────────────────────────── */

const ACTION_COPY: Record<string, string> = {
  dashboard_add_camera: "Show this camera on the dashboard",
  skill_install: "Install a skill",
};

/** The one action whose card carries code the owner is being asked to trust. */
const SKILL_INSTALL = "skill_install";

function actionLabel(action: string): string {
  if (ACTION_COPY[action]) return ACTION_COPY[action];
  // Fall back to a readable version of whatever Drawbridge filed.
  return action.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
}

function relativeTime(iso: string): string {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "";
  const mins = Math.floor((Date.now() - then) / 60_000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

/* ── Data hook ─────────────────────────────────────────────────── */

export function useConsentRequests() {
  const [requests, setRequests] = useState<ConsentRequest[]>([]);
  const [loadFailed, setLoadFailed] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setRequests(await getConsentRequests());
      setLoadFailed(false);
    } catch {
      // Backend offline is already reported by the page-level banner; don't
      // add a second scary message here.
      setLoadFailed(true);
    }
  }, []);

  useEffect(() => {
    refresh();
    const iv = setInterval(refresh, POLL_MS);
    const off = onConsentChanged(refresh);
    return () => {
      clearInterval(iv);
      off();
    };
  }, [refresh]);

  return { requests, loadFailed, refresh };
}

/* ── A skill install, which is an approval of code ─────────────── */

/**
 * The extra half of a `skill_install` card: what the skill may reach, stated in
 * plain words, and a way to read the code BEFORE saying yes.
 *
 * Approve/Deny are untouched — the verdict buttons are the same ones every
 * other consent request uses. What is added here is only what a person needs
 * in order to give that verdict honestly: the registrable domain in bold (the
 * name a person recognises), the subdomain muted beside it, the empty
 * permission lists said out loud, and the exact bytes one click away.
 */
function SkillInstallDetail({ request }: { request: ConsentRequest }) {
  const [showCode, setShowCode] = useState(false);
  const ctx = request.context || {};
  const manifest = (ctx.manifest as SkillManifest | undefined) || null;
  const skillId =
    String(ctx.skill_id || manifest?.id || request.entity_id.replace(/^skill:/, ""));
  const testLog = typeof ctx.test_log_tail === "string" ? ctx.test_log_tail : "";
  const shas: Record<string, string> =
    ctx.sha256 && typeof ctx.sha256 === "object" ? ctx.sha256 : {};
  const codeSha = shas["skill.py"] || "";

  return (
    <div className="mt-2 rounded-md border border-white/5 bg-white/[0.02] px-2.5 py-2">
      <PermissionList manifest={manifest} />
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={() => setShowCode(true)}
          className="rounded-md border border-[#8899aa]/25 px-2.5 py-1 text-[11px] font-medium text-[#8899aa] transition hover:border-[#8899aa]/50 hover:text-white"
        >
          View code
        </button>
        {codeSha && (
          <span className="font-mono text-[10px] text-[#8899aa]/60">
            skill.py sha256 {codeSha.slice(0, 12)}
          </span>
        )}
      </div>
      {showCode && (
        <SkillCodeModal
          skillId={skillId}
          title={manifest?.name ? `${manifest.name} — code` : "Skill code"}
          testLogTail={testLog}
          onClose={() => setShowCode(false)}
        />
      )}
    </div>
  );
}

/* ── Vault PIN on the card ─────────────────────────────────────── */

/**
 * Inline PIN for an approved Home Assistant change while the vault is locked.
 * The PIN lives only in this component's state, is masked, never logged or
 * stored, goes only to the existing server-side PIN check (the route's `pin`
 * field), and is cleared the moment it is sent. The server owns the 3-try
 * limit; this shows its message and stops asking when it says so.
 */
export function PinEntry({
  entityId,
  prompt,
  onDone,
  tone = "card",
}: {
  entityId: string;
  prompt: string;
  /** Called when the server no longer needs a PIN (done or voided). */
  onDone?: (message: string) => void;
  /** "card": the consent card's dark look. "theme": the page theme's CSS
   *  variables, readable on light and dark themes (the firewall page). */
  tone?: "card" | "theme";
}) {
  const themed = tone === "theme";
  const [pin, setPin] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState(prompt);

  const submit = async (e?: React.FormEvent) => {
    e?.preventDefault();
    if (!pin || busy) return;
    const sending = pin;
    setPin("");                     // cleared before anything else can see it
    setBusy(true);
    try {
      const res = await resolveConsentRequest(entityId, true, undefined, sending);
      const text = res.message || "Sent.";
      setMessage(text);
      if (!res.needs_pin) {
        onDone?.(text);
        emitConsentChanged();
      }
    } catch (err: any) {
      setMessage(err?.message || "That didn't go through. Try again.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={submit} className="mt-2 flex flex-wrap items-center gap-2" autoComplete="off">
      <p
        className={themed ? "w-full text-[11px]" : "w-full text-[11px] text-[#ffcc66]"}
        style={themed ? { color: "var(--lemon)" } : undefined}
      >
        {message}
      </p>
      <input
        type="password"
        inputMode="numeric"
        autoComplete="off"
        name={`vault-pin-${entityId}`}
        aria-label="Vault PIN"
        placeholder="Vault PIN"
        value={pin}
        onChange={(ev) => setPin(ev.target.value)}
        disabled={busy}
        className={
          themed
            ? "w-32 rounded-md border px-2 py-1 text-xs"
            : "w-32 rounded-md border border-white/15 bg-black/30 px-2 py-1 text-xs text-white"
        }
        style={
          themed
            ? { borderColor: "var(--line)", background: "var(--card2)", color: "var(--text)" }
            : undefined
        }
      />
      <button
        type="submit"
        disabled={busy || !pin}
        className={
          themed
            ? "rounded-md border px-2.5 py-1 text-[11px] font-medium transition disabled:opacity-40"
            : "rounded-md border border-[#39ff14]/30 bg-[#39ff14]/10 px-2.5 py-1 text-[11px] font-medium text-[#39ff14] transition hover:bg-[#39ff14]/20 disabled:opacity-40"
        }
        style={
          themed
            ? { borderColor: "var(--line)", background: "var(--chip)", color: "var(--mint)" }
            : undefined
        }
      >
        {busy ? "Checking…" : "Finish"}
      </button>
    </form>
  );
}

/** Approved changes still waiting for the PIN (e.g. after a page reload). */
export function WaitingPinList() {
  const [waiting, setWaiting] = useState<WaitingForPin[]>([]);
  const [done, setDone] = useState<Record<string, string>>({});

  const refresh = useCallback(async () => {
    try {
      setWaiting(await getWaitingForPin());
    } catch {
      /* the page-level banner reports a backend outage */
    }
  }, []);

  useEffect(() => {
    refresh();
    const iv = setInterval(refresh, POLL_MS);
    const off = onConsentChanged(refresh);
    return () => {
      clearInterval(iv);
      off();
    };
  }, [refresh]);

  const shown = waiting.filter((w) => !done[w.entity_id]);
  if (shown.length === 0 && Object.keys(done).length === 0) return null;
  return (
    <div className="mb-3 space-y-2">
      {Object.entries(done).map(([id, msg]) => (
        <div key={id} className="rounded-lg border border-white/5 bg-white/[0.02] px-3 py-2 text-xs text-[#8899aa]">
          {msg}
        </div>
      ))}
      {shown.map((w) => (
        <div key={w.entity_id} className="rounded-lg border border-[#ffcc66]/25 bg-[#ffcc66]/[0.05] px-3 py-2.5">
          <p className="text-xs font-medium text-white">Approved, waiting for your vault PIN</p>
          <p className="mt-0.5 truncate text-xs text-[#8899aa]">{w.name}</p>
          <PinEntry
            entityId={w.entity_id}
            prompt={`Enter your vault PIN to finish. This approval lasts about ${Math.max(1, Math.round(w.expires_in / 60))} more minutes.`}
            onDone={(msg) => setDone((d) => ({ ...d, [w.entity_id]: msg }))}
          />
        </div>
      ))}
    </div>
  );
}

/* ── One request ───────────────────────────────────────────────── */

export function ConsentRequestRow({
  request,
  onResolved,
}: {
  request: ConsentRequest;
  /** Called after a successful verdict, with the backend's message (if any). */
  onResolved?: (approved: boolean, message: string | null) => void;
}) {
  const [busy, setBusy] = useState<null | "approve" | "deny">(null);
  const [error, setError] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<string | null>(null);
  const [pinPrompt, setPinPrompt] = useState<string | null>(null);

  const decide = async (approve: boolean) => {
    setBusy(approve ? "approve" : "deny");
    setError(null);
    try {
      const res = await resolveConsentRequest(request.entity_id, approve);
      const message =
        res.message ||
        (approve ? "Approved." : "Denied. Nothing was added.");
      if (approve && res.needs_pin) {
        setPinPrompt(message);    // approved; finish with the PIN right here
        return;
      }
      setOutcome(message);
      onResolved?.(approve, res.message ?? null);
      // Refresh every consent-aware surface (this list, the camera tiles).
      emitConsentChanged();
    } catch (e: any) {
      setError(e?.message || "Could not record that. Try again.");
    } finally {
      setBusy(null);
    }
  };

  if (outcome) {
    return (
      <div className="rounded-lg border border-white/5 bg-white/[0.02] px-3 py-2 text-xs text-[#8899aa]">
        {outcome}
      </div>
    );
  }

  if (pinPrompt) {
    return (
      <div className="rounded-lg border border-[#ffcc66]/25 bg-[#ffcc66]/[0.05] px-3 py-2.5">
        <p className="text-xs font-medium text-white">{request.label}</p>
        <PinEntry
          entityId={request.entity_id}
          prompt={pinPrompt}
          onDone={(msg) => setOutcome(msg)}
        />
      </div>
    );
  }

  return (
    <div className="rounded-lg border border-[#ff9500]/20 bg-[#ff9500]/[0.04] px-3 py-2.5">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <p className="text-xs font-medium text-white">
            {actionLabel(request.action)}
          </p>
          <p className="mt-0.5 truncate text-xs text-[#8899aa]">
            {request.label}{" "}
            <span className="font-mono text-[10px] text-[#8899aa]/60">
              {request.entity_id}
            </span>
          </p>
          <p className="mt-1 text-[11px] text-[#8899aa]/70">
            {request.reason}
            {request.created_at ? ` · asked ${relativeTime(request.created_at)}` : ""}
          </p>
          {request.action === SKILL_INSTALL && <SkillInstallDetail request={request} />}
        </div>
        <div className="flex flex-shrink-0 gap-2">
          <button
            type="button"
            onClick={() => decide(true)}
            disabled={busy !== null}
            className="flex items-center gap-1 rounded-md border border-[#39ff14]/30 bg-[#39ff14]/10 px-2.5 py-1 text-[11px] font-medium text-[#39ff14] transition hover:bg-[#39ff14]/20 disabled:opacity-40"
          >
            <Icon path={mdiCheck} size="12" />
            {busy === "approve" ? "Approving…" : "Approve"}
          </button>
          <button
            type="button"
            onClick={() => decide(false)}
            disabled={busy !== null}
            className="flex items-center gap-1 rounded-md border border-[#8899aa]/25 px-2.5 py-1 text-[11px] font-medium text-[#8899aa] transition hover:border-[#8899aa]/50 hover:text-white disabled:opacity-40"
          >
            <Icon path={mdiClose} size="12" />
            {busy === "deny" ? "Denying…" : "Deny"}
          </button>
        </div>
      </div>
      {error && (
        <p className="mt-2 text-[11px] text-[#ff2d6f]">{error}</p>
      )}
    </div>
  );
}

/* ── Bulk plumbing ─────────────────────────────────────────────── */

/** Above this many open requests, one-row-at-a-time stops being usable. */
const GROUP_THRESHOLD = 6;

interface BulkRun {
  approve: boolean;
  /** What the run covers, for the progress line ("13 media players"). */
  scope: string;
  done: number;
  total: number;
}

const BTN_APPROVE =
  "flex items-center gap-1 rounded-md border border-[#39ff14]/30 bg-[#39ff14]/10 px-2.5 py-1 text-[11px] font-medium text-[#39ff14] transition hover:bg-[#39ff14]/20 disabled:opacity-40";
const BTN_DENY =
  "flex items-center gap-1 rounded-md border border-[#8899aa]/25 px-2.5 py-1 text-[11px] font-medium text-[#8899aa] transition hover:border-[#8899aa]/50 hover:text-white disabled:opacity-40";

/* ── One domain group ──────────────────────────────────────────── */

function ConsentGroup({
  domain,
  label,
  requests,
  open,
  onToggle,
  onBulk,
  disabled,
}: {
  domain: string;
  label: string;
  requests: ConsentRequest[];
  open: boolean;
  onToggle: () => void;
  onBulk: (requests: ConsentRequest[], approve: boolean, scope: string) => void;
  disabled: boolean;
}) {
  const n = requests.length;
  const noun = domainNoun(domain, n);
  const scope = `${n} ${noun}`;

  return (
    <div className="rounded-lg border border-white/5 bg-white/[0.02]">
      <div className="flex flex-wrap items-center gap-2 px-3 py-2">
        <button
          type="button"
          onClick={onToggle}
          aria-expanded={open}
          className="flex min-w-0 flex-1 items-center gap-1.5 text-left"
        >
          <span className="text-[#8899aa]">
            <Icon path={open ? mdiChevronDown : mdiChevronRight} size="14" />
          </span>
          <span className="text-xs font-medium text-white">{label}</span>
          <span className="rounded-full border border-[#ff9500]/30 px-1.5 text-[10px] text-[#ff9500]">
            {n}
          </span>
        </button>
        <div className="flex flex-shrink-0 gap-2">
          <button
            type="button"
            disabled={disabled}
            onClick={() => onBulk(requests, true, scope)}
            className={BTN_APPROVE}
          >
            <Icon path={mdiCheck} size="12" />
            Approve all {scope}
          </button>
          <button
            type="button"
            disabled={disabled}
            onClick={() => onBulk(requests, false, scope)}
            className={BTN_DENY}
          >
            <Icon path={mdiClose} size="12" />
            Deny all {n}
          </button>
        </div>
      </div>
      {open && (
        <div className="space-y-2 border-t border-white/5 px-3 py-2">
          {requests.map((r) => (
            <ConsentRequestRow key={`${r.entity_id}:${r.action}`} request={r} />
          ))}
        </div>
      )}
    </div>
  );
}

/* ── Outcome of a bulk run ─────────────────────────────────────── */

function BulkOutcome({
  result,
  onDismiss,
}: {
  result: ConsentBulkResult;
  onDismiss: () => void;
}) {
  const ok = result.succeeded.length;
  const verb = result.approved ? "Approved" : "Denied";
  const clean = result.failed.length === 0;

  return (
    <div
      className={`rounded-lg border px-3 py-2.5 ${
        clean
          ? "border-white/5 bg-white/[0.02]"
          : "border-[#ff2d6f]/25 bg-[#ff2d6f]/[0.04]"
      }`}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="text-xs text-white">
            {verb} {ok}
            {result.failed.length > 0
              ? ` of ${ok + result.failed.length}`
              : ""}
            .
            {clean && result.approved
              ? " You can revoke any of them later."
              : ""}
          </p>
          {result.skipped.length > 0 && (
            <p className="mt-1 text-[11px] text-[#8899aa]/70">
              {result.skipped.length} had already been answered elsewhere.
            </p>
          )}
          {result.failed.length > 0 && (
            <>
              <p className="mt-1.5 text-[11px] text-[#ff2d6f]">
                {result.failed.length} could not be recorded. They are still in
                the list — try them again.
              </p>
              <ul className="mt-1 space-y-0.5">
                {result.failed.map((f) => (
                  <li key={f.entity_id} className="text-[11px] text-[#8899aa]">
                    <span className="font-mono text-[10px] text-[#8899aa]/60">
                      {f.entity_id}
                    </span>{" "}
                    — {f.message}
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
        <button
          type="button"
          onClick={onDismiss}
          className="flex-shrink-0 text-[11px] text-[#8899aa] transition hover:text-white"
        >
          Dismiss
        </button>
      </div>
    </div>
  );
}

/* ── Banner (renders nothing when the queue is empty) ──────────── */

export default function ConsentRequestsBanner() {
  return (
    <>
      <WaitingPinList />
      <ConsentRequestsQueue />
    </>
  );
}

function ConsentRequestsQueue() {
  const { requests, loadFailed } = useConsentRequests();
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [bulk, setBulk] = useState<BulkRun | null>(null);
  const [result, setResult] = useState<ConsentBulkResult | null>(null);
  const [confirmAll, setConfirmAll] = useState(false);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const runBulk = useCallback(
    async (targets: ConsentRequest[], approve: boolean, scope: string) => {
      if (targets.length === 0) return;
      setConfirmAll(false);
      setResult(null);
      setBulk({ approve, scope, done: 0, total: targets.length });
      // Awaited here but nothing else in the dashboard waits on it: the rest of
      // the page keeps rendering and polling while this drains.
      const res = await resolveConsentRequestsBulk(targets, approve, {
        onProgress: (done, total) => {
          if (alive.current) setBulk((b) => (b ? { ...b, done, total } : b));
        },
      });
      // Tell the other consent-aware surfaces (this list, the camera tiles)
      // even if we've unmounted — the verdicts landed either way.
      emitConsentChanged();
      if (!alive.current) return;
      setBulk(null);
      setResult(res);
    },
    []
  );

  // Nothing to show and nothing to report: stay out of the way entirely.
  if (loadFailed) return null;
  if (requests.length === 0 && !bulk && !result) return null;

  const grouped = requests.length > GROUP_THRESHOLD;
  const groups = grouped ? groupConsentRequests(requests) : [];
  const busy = bulk !== null;

  const toggle = (domain: string) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(domain)) next.delete(domain);
      else next.add(domain);
      return next;
    });

  return (
    <div className="mb-4 rounded-xl border border-[#ff9500]/25 bg-[#ff9500]/[0.05] p-4">
      <div className="mb-1.5 flex items-center gap-2">
        <span style={{ color: "var(--puff-gold)" }}>
          <Icon path={mdiShieldAccountOutline} size="16" />
        </span>
        <h3 className="text-sm font-semibold text-white">
          Drawbridge needs your permission
        </h3>
        {requests.length > 0 && (
          <span className="rounded-full border border-[#ff9500]/30 px-1.5 text-[10px] text-[#ff9500]">
            {requests.length}
          </span>
        )}
      </div>
      <p className="mb-2.5 text-[11px] text-[#8899aa]">
        PuffPAI asks before it can control a device. Approve the ones you want
        it to manage — you can change any answer later.
      </p>

      {bulk && (
        <div className="mb-2.5 rounded-lg border border-white/5 bg-white/[0.02] px-3 py-2 text-xs text-[#8899aa]">
          {bulk.approve ? "Approving" : "Denying"} {bulk.scope} — {bulk.done} of{" "}
          {bulk.total}…
        </div>
      )}

      {result && !bulk && (
        <div className="mb-2.5">
          <BulkOutcome result={result} onDismiss={() => setResult(null)} />
        </div>
      )}

      {grouped && (
        <div className="mb-2.5 flex flex-wrap items-center gap-2 rounded-lg border border-white/5 bg-white/[0.02] px-3 py-2">
          {confirmAll ? (
            <>
              <p className="min-w-0 flex-1 text-[11px] text-white">
                This lets PuffPAI control all {requests.length} devices. You can
                revoke any of them later.
              </p>
              <button
                type="button"
                disabled={busy}
                onClick={() =>
                  runBulk(requests, true, `all ${requests.length} devices`)
                }
                className={BTN_APPROVE}
              >
                <Icon path={mdiCheck} size="12" />
                Yes, approve all {requests.length}
              </button>
              <button
                type="button"
                onClick={() => setConfirmAll(false)}
                className={BTN_DENY}
              >
                Cancel
              </button>
            </>
          ) : (
            <>
              <p className="min-w-0 flex-1 text-[11px] text-[#8899aa]">
                {requests.length} requests, grouped by device type.
              </p>
              <button
                type="button"
                disabled={busy}
                onClick={() => setConfirmAll(true)}
                className={BTN_APPROVE}
              >
                <Icon path={mdiCheck} size="12" />
                Approve everything
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() =>
                  runBulk(requests, false, `all ${requests.length} requests`)
                }
                className={BTN_DENY}
              >
                <Icon path={mdiClose} size="12" />
                Deny everything
              </button>
            </>
          )}
        </div>
      )}

      <div className="space-y-2">
        {grouped
          ? groups.map((g) => (
              <ConsentGroup
                key={g.domain}
                domain={g.domain}
                label={g.label}
                requests={g.requests}
                open={expanded.has(g.domain)}
                onToggle={() => toggle(g.domain)}
                onBulk={runBulk}
                disabled={busy}
              />
            ))
          : requests.map((r) => (
              <ConsentRequestRow key={`${r.entity_id}:${r.action}`} request={r} />
            ))}
      </div>
    </div>
  );
}
