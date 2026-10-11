"use client";

/**
 * The review. ONE screen: the whole build, proposed.
 *
 * One dense row per skill, in the fixed skill order. Every row states a
 * decision, a cost and a status; no row says "configured", and anything
 * undecided names who has to decide it. The rows are rendered from
 * `wizard_card/v1` objects, which is what makes this screen and the CLI the
 * same wizard rather than two of them.
 *
 * Three refusals are load-bearing and all three live in ./reviewModel:
 *  - the total never sums a row whose basis is unknown;
 *  - [Build it] is disabled until every consequential row has been opened once
 *    (no second confirmation, no checkbox — the button says how many are left);
 *  - staleness is one collapsed banner, never a badge per row.
 *
 * SECRETS. A secret typed into a card lives in this component's state until it
 * is applied and is cleared immediately after. It is never written to
 * localStorage, the URL or the console, and the card it came from never carried
 * a value for it.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import {
  applyWizardCard,
  getWizardCards,
  getWizardState,
  resumeWizard,
  runWizard,
  type CardGap,
  type ResumeOffer,
  type PlainSkill,
  type WizardCard,
} from "../../_lib/services/wizardService";
// This dashboard's onboarding kit has no ConnectedTo strip (BuildBot's "which
// box is this" line), so the screen leads with its own eyebrow instead.
import { Card, Eyebrow, Lede, PuffButton, microLabel } from "../ui";
import {
  FieldRenderer,
  InfoBlock,
  answersToSend,
  initialAnswers,
  type AnswerValue,
} from "./fields";
import {
  STATUS_WORDS,
  canBuild,
  acknowledgedIds,
  cardId,
  decidedLine,
  fieldForError,
  gateLabel,
  groupFields,
  inSpecOrder,
  notConvertedRows,
  reasonText,
  rowCost,
  staleBanner,
  totalLine,
  unopenedConsequential,
} from "./reviewModel";

const MONO = "var(--font-jbmono), monospace";

/** "12 minutes ago". The same words `render_cli.how_long_ago` uses on the
 *  console, because the two renderers say the same thing to the same owner. */
function howLongAgo(seconds: number | undefined): string {
  const total = Math.max(0, Number(seconds) || 0);
  for (const [size, unit] of [
    [86400, "day"],
    [3600, "hour"],
    [60, "minute"],
  ] as const) {
    if (total >= size) {
      const count = Math.floor(total / size);
      return `${count} ${unit}${count === 1 ? "" : "s"} ago`;
    }
  }
  return "a moment ago";
}

function SkillId({ id }: { id: string }) {
  return (
    <span style={{ ...microLabel, fontSize: 10, marginLeft: 8, color: "var(--text3)" }}>
      {id}
    </span>
  );
}

function GapRow({ gap }: { gap: CardGap }) {
  const [copied, setCopied] = useState(false);
  const copy = useCallback(() => {
    // The command was rendered by the backend's fix registry. Nothing here
    // assembles one; this only moves the string somebody can already read.
    navigator.clipboard?.writeText(gap.fix_command).then(
      () => setCopied(true),
      () => setCopied(false),
    );
  }, [gap.fix_command]);
  return (
    <div style={{ padding: "10px 0", borderTop: "1px solid var(--line)" }}>
      <div style={{ color: "var(--text)", fontSize: 14, fontWeight: 700 }}>{gap.what}</div>
      <div style={{ color: "var(--text2)", fontSize: 13, marginTop: 2 }}>{gap.why}</div>
      {gap.fix_command ? (
        <div className="flex flex-wrap items-center gap-2" style={{ marginTop: 6 }}>
          <code
            style={{
              fontFamily: MONO,
              fontSize: 12,
              color: "var(--text2)",
              background: "var(--card2)",
              border: "1px solid var(--line)",
              borderRadius: 8,
              padding: "4px 8px",
              overflowWrap: "anywhere",
            }}
          >
            {gap.fix_command}
          </code>
          <button
            type="button"
            onClick={copy}
            style={{
              ...microLabel,
              fontSize: 10,
              color: "var(--text2)",
              background: "var(--card2)",
              border: "1px solid var(--line)",
              borderRadius: 999,
              padding: "4px 10px",
              cursor: "pointer",
            }}
          >
            {copied ? "copied" : "copy"}
          </button>
        </div>
      ) : null}
    </div>
  );
}

function CardPanel({
  card,
  onApplied,
}: {
  card: WizardCard;
  onApplied: (result: { card?: WizardCard; stale?: string[] }) => void;
}) {
  const [answers, setAnswers] = useState<Record<string, AnswerValue>>(() =>
    initialAnswers(card.fields),
  );
  // Applying one card re-proposes the others (a name changes the suggested
  // id). A form that kept its first proposal would show an empty id box over
  // a card that proposes one.
  const proposal = JSON.stringify(
    card.fields.map((f) => [f.id, f.proposed ?? f.current ?? null]),
  );
  useEffect(() => {
    setAnswers(initialAnswers(card.fields));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [proposal]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  // A secret only ever lives as long as this panel does.
  useEffect(
    () => () => {
      setAnswers({});
    },
    [],
  );

  const askable = card.fields.filter((field) => field.kind !== "info");
  const [namedField, setNamedField] = useState<string | null>(null);
  // The backend decides which field a refusal is about, against the card it
  // actually proposed. Reading it out of the message is the fallback for an
  // older box, not the source of truth.
  const badField =
    namedField ??
    fieldForError(
      error,
      askable.map((field) => field.id),
    );

  async function submit() {
    setBusy(true);
    setError("");
    setNamedField(null);
    const reply = await applyWizardCard(cardId(card), answersToSend(card.fields, answers));
    setBusy(false);
    if (!reply.ok || reply.data?.success === false) {
      setNamedField(reply.data?.field ?? null);
      setError(
        reply.status === 0
          ? "No answer from this box. Nothing was changed."
          : reply.data?.message || "That was refused. Nothing was changed.",
      );
      return;
    }
    // Whatever was typed is gone from state the moment it has been applied.
    setAnswers(initialAnswers(reply.data?.card?.fields || card.fields));
    onApplied({ card: reply.data?.card, stale: reply.data?.stale });
  }

  return (
    <div style={{ padding: "12px 0 4px" }}>
      {card.fields.length === 0 ? (
        <Lede>Nothing to change here. Accepting it applies what it decided.</Lede>
      ) : null}
      {/* Consecutive info fields are one quiet block; everything answerable is
          still its own field. */}
      {groupFields(card.fields).map((group, i) =>
        group.kind === "info" ? (
          <InfoBlock key={`info-${group.fields[0].id}-${i}`} fields={group.fields} />
        ) : (
          <FieldRenderer
            key={group.field.id}
            field={group.field}
            value={answers[group.field.id]}
            error={badField === group.field.id ? error : undefined}
            onChange={(next) =>
              setAnswers((prev) => ({ ...prev, [group.field.id]: next }))
            }
          />
        ),
      )}
      {card.gaps.length > 0 ? (
        <div style={{ marginTop: 14 }}>
          <Eyebrow>What is missing</Eyebrow>
          {card.gaps.map((gap, i) => (
            <GapRow key={`${gap.what}-${i}`} gap={gap} />
          ))}
        </div>
      ) : null}
      {error && !badField ? (
        <div role="alert" style={{ color: "var(--puff-red)", fontSize: 13, marginTop: 10 }}>
          {error}
        </div>
      ) : null}
      <div style={{ marginTop: 14 }}>
        <PuffButton
          label={busy ? "Applying…" : askable.length ? "Apply this" : "Accept this"}
          kind="primary"
          disabled={busy}
          onClick={submit}
        />
      </div>
    </div>
  );
}

function SkillRow({
  card,
  open,
  onToggle,
  onApplied,
}: {
  card: WizardCard;
  open: boolean;
  onToggle: () => void;
  onApplied: (result: { card?: WizardCard; stale?: string[] }) => void;
}) {
  const heavy = card.consequential;
  return (
    <div
      style={{
        borderTop: "1px solid var(--line)",
        borderLeft: heavy ? "3px solid var(--lemon)" : "3px solid transparent",
        paddingLeft: 10,
        background: heavy ? "var(--card2)" : "transparent",
      }}
    >
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        className="flex w-full flex-wrap items-baseline justify-between gap-x-4 gap-y-1 text-left"
        style={{
          background: "transparent",
          border: "none",
          padding: heavy ? "14px 8px" : "10px 8px",
          cursor: "pointer",
        }}
      >
        <span style={{ flex: "1 1 220px", minWidth: 0 }}>
          <span
            style={{
              color: "var(--text)",
              fontSize: heavy ? 17 : 15,
              fontWeight: heavy ? 800 : 700,
              letterSpacing: heavy ? ".01em" : 0,
            }}
          >
            {card.title}
          </span>
          <SkillId id={cardId(card)} />
          <span
            style={{
              display: "block",
              color: "var(--text2)",
              fontSize: heavy ? 14 : 13,
              marginTop: 2,
            }}
          >
            {card.decided || "nothing decided yet"}
          </span>
          {heavy ? (
            <span
              style={{
                display: "block",
                ...microLabel,
                fontSize: 10,
                color: "var(--lemon)",
                marginTop: 4,
              }}
            >
              {reasonText(card.consequential_reasons)}
            </span>
          ) : null}
        </span>
        {/* The cost, the status and the chevron on one baseline with a gap
            between them: at 800px the estimate ran into the status label, and
            a heavy row put "time unknown" under "READY". At phone width the
            whole cluster wraps as one, under the title. */}
        <span
          className="flex flex-wrap items-baseline justify-end gap-x-3 gap-y-1"
          style={{ flex: "0 0 auto", marginLeft: "auto", textAlign: "right" }}
        >
          <span
            style={{
              color: "var(--text2)",
              fontSize: 13,
              fontFamily: MONO,
              whiteSpace: "nowrap",
            }}
          >
            {rowCost(card.estimate)}
          </span>
          <span style={{ ...microLabel, fontSize: 10, whiteSpace: "nowrap" }}>
            {STATUS_WORDS[card.status]}
            {card.gaps.length
              ? ` · ${card.gaps.length} gap${card.gaps.length > 1 ? "s" : ""}`
              : ""}
          </span>
          <span style={{ color: "var(--text3)", fontSize: 16 }}>{open ? "×" : "›"}</span>
        </span>
      </button>
      {open ? <CardPanel card={card} onApplied={onApplied} /> : null}
    </div>
  );
}

export default function ReviewScreen() {
  const [cards, setCards] = useState<WizardCard[]>([]);
  const [refusals, setRefusals] = useState<string[]>([]);
  const [order, setOrder] = useState<string[]>([]);
  const [notConverted, setNotConverted] = useState<PlainSkill[]>([]);
  const [stale, setStale] = useState<string[]>([]);
  const [settled, setSettled] = useState<string[]>([]);
  const [opened, setOpened] = useState<string[]>([]);
  const [expanded, setExpanded] = useState<string>("");
  const [loading, setLoading] = useState(true);
  const [down, setDown] = useState("");
  const [staleOpen, setStaleOpen] = useState(false);
  const [building, setBuilding] = useState(false);
  const [buildLog, setBuildLog] = useState<string[]>([]);
  /** A setup that stopped part-way, if there is one. The same choice the CLI
   *  puts on its own screen; here it is a banner above the rows, because the
   *  review screen IS the screen and a modal over it would hide what the
   *  choice is about. */
  const [resume, setResume] = useState<ResumeOffer | null>(null);
  const [resumeNote, setResumeNote] = useState("");
  const [resuming, setResuming] = useState(false);
  const alive = useRef(true);

  const load = useCallback(async () => {
    setLoading(true);
    const reply = await getWizardCards();
    if (!alive.current) return;
    setLoading(false);
    if (!reply.ok) {
      setDown(
        reply.status === 0
          ? "No answer from this box. Nothing has been read."
          : "This box would not say what it found.",
      );
      return;
    }
    setDown("");
    setCards(reply.data?.cards || []);
    setOrder(reply.data?.order || []);
    setNotConverted(reply.data?.not_converted || []);
    setRefusals(reply.data?.refusals || []);
    setStale(reply.data?.stale || []);
    setSettled(reply.data?.applied || []);
    const state = await getWizardState();
    if (!alive.current) return;
    const offer = state.data?.resume;
    setResume(offer?.offer ? offer : null);
  }, []);

  /** [Continue] / [Start over]. The SERVER applies the rule: continuing
   *  re-verifies every card that can stop the build against this machine and
   *  asks again for the ones that do not hold — the vault is checked on disk
   *  whatever the record says. What comes back is what will be asked again, and
   *  that is what the note says. */
  async function chooseResume(choice: "continue" | "restart") {
    setResuming(true);
    const reply = await resumeWizard(choice);
    if (!alive.current) return;
    setResuming(false);
    if (!reply.ok || reply.data?.success === false) {
      setResumeNote(
        reply.status === 0
          ? "No answer from this box. Nothing was changed."
          : reply.data?.message || "That was refused. Nothing was changed.",
      );
      return;
    }
    const reasked = reply.data?.reasked || [];
    setResumeNote(
      choice === "restart"
        ? "The stopped setup was set aside. Nothing was deleted."
        : reasked.length
          ? `Picking up where it stopped. These did not hold against this machine and are asked again: ${reasked.join(", ")}.`
          : "Picking up where it stopped.",
    );
    setResume(null);
    await load();
  }

  useEffect(() => {
    alive.current = true;
    void load();
    return () => {
      alive.current = false;
    };
  }, [load]);

  const rows = useMemo(() => inSpecOrder(cards, order), [cards, order]);
  const plain = useMemo(() => notConvertedRows(notConverted), [notConverted]);
  const remaining = unopenedConsequential(rows, opened);
  const ready = canBuild(rows, opened);
  const banner = staleBanner(stale);

  function toggle(skill: string) {
    setExpanded((prev) => (prev === skill ? "" : skill));
    setOpened((prev) => (prev.includes(skill) ? prev : [...prev, skill]));
  }

  /** One apply changes more than one row.
   *
   *  Answering a card moves what every later card proposes — a goal decides the
   *  sourcing plan and the surfaces, a posture decides what the menus may
   *  offer, and whether a row is `consequential` follows from that. Replacing
   *  only the row that was applied left the others showing flags and sizes from
   *  before the answer, which is a stale "this downloads a lot" beside a row
   *  that now downloads nothing. The server re-proposes every card on
   *  `GET /wizard/cards`, so the honest refresh is all of them; the applied
   *  card is put in first so the row does not blink back to its old text while
   *  the reload is in flight. */
  function applied(id: string, result: { card?: WizardCard; stale?: string[] }) {
    if (result.card) {
      setCards((prev) => prev.map((card) => (cardId(card) === id ? result.card! : card)));
    }
    if (result.stale?.length) {
      setStale((prev) => Array.from(new Set([...prev, ...result.stale!])));
    }
    void load();
  }

  /** [Build it]: ONE call. The server applies every card nobody answered, in
   *  its own order, and refuses on its own account to accept a heavy row that
   *  is not in `acknowledged` or any row that asks for a secret. What is sent
   *  is which heavy rows the owner opened — a claim the backend checks, never
   *  an instruction it follows. */
  async function build() {
    setBuilding(true);
    setBuildLog([]);
    const reply = await runWizard(acknowledgedIds(rows, opened));
    if (!alive.current) return;
    setBuilding(false);
    if (!reply.ok || reply.data?.success === false) {
      setBuildLog([
        reply.status === 0
          ? "No answer from this box. Nothing was built."
          : reply.data?.message || "That was refused. Nothing was built.",
      ]);
      return;
    }
    setBuildLog(
      (reply.data?.results || []).map(
        (step) => `${step.card}: ${step.outcome}${step.why ? ` · ${step.why}` : ""}`,
      ),
    );
    if (reply.data?.stale?.length) {
      setStale((prev) => Array.from(new Set([...prev, ...reply.data!.stale!])));
    }
    await load();
    if (!alive.current) return;
    setExpanded("handoff");
    setOpened((prev) => (prev.includes("handoff") ? prev : [...prev, "handoff"]));
  }

  return (
    <div style={{ maxWidth: 860, margin: "0 auto", padding: "8px 0 40px" }}>
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <Eyebrow>The review · the whole build, proposed</Eyebrow>
        <Link href="/onboarding" style={{ ...microLabel, fontSize: 10, color: "var(--text2)" }}>
          Back to the step-by-step setup
        </Link>
      </div>

      <Card style={{ marginTop: 14 }}>
        <h1
          style={{
            fontFamily: "var(--font-fredoka), var(--font-nunito), sans-serif",
            fontSize: 26,
            fontWeight: 700,
            color: "var(--text)",
            margin: 0,
          }}
        >
          Everything this build is about to do
        </h1>
        <Lede>{decidedLine(rows.length, settled)}</Lede>

        {resume ? (
          <div
            data-testid="resume-banner"
            style={{
              marginTop: 12,
              padding: "10px 14px",
              borderRadius: 14,
              border: "1px solid var(--line)",
              borderLeft: "3px solid var(--lemon)",
              background: "var(--card2)",
            }}
          >
            <div style={{ fontSize: 14, fontWeight: 700, color: "var(--text)" }}>
              A setup from {howLongAgo(resume.age_s)} stopped at{" "}
              {resume.stopped_title || resume.stopped_card || "an unnamed step"}.
            </div>
            <div style={{ fontSize: 13, color: "var(--text2)", marginTop: 4 }}>
              Continue from there, or start over? Continuing re-checks the vault and the brain
              against this machine and asks again if they are not there.
            </div>
            {resume.decided?.length ? (
              <ul style={{ margin: "8px 0 0", paddingLeft: 18, fontSize: 12, color: "var(--text2)" }}>
                {resume.decided.map((row) => (
                  <li key={row.entry || row.card}>{row.title || row.card}</li>
                ))}
              </ul>
            ) : (
              <div style={{ fontSize: 12, color: "var(--text2)", marginTop: 8 }}>
                Nothing was decided before it stopped.
              </div>
            )}
            <div className="flex flex-wrap gap-2" style={{ marginTop: 10 }}>
              <PuffButton
                label="Continue from there"
                disabled={resuming}
                onClick={() => void chooseResume("continue")}
              />
              <PuffButton
                label="Start over"
                kind="ghost"
                disabled={resuming}
                onClick={() => void chooseResume("restart")}
              />
            </div>
          </div>
        ) : null}
        {resumeNote ? (
          <div style={{ marginTop: 10, fontSize: 12, color: "var(--text2)" }}>{resumeNote}</div>
        ) : null}

        {banner ? (
          <div
            style={{
              marginTop: 12,
              padding: "10px 14px",
              borderRadius: 14,
              border: "1px solid var(--line)",
              borderLeft: "3px solid var(--lemon)",
              background: "var(--card2)",
            }}
          >
            <div className="flex flex-wrap items-center justify-between gap-2">
              <button
                type="button"
                onClick={() => setStaleOpen((prev) => !prev)}
                aria-expanded={staleOpen}
                style={{
                  background: "transparent",
                  border: "none",
                  padding: 0,
                  color: "var(--text)",
                  fontSize: 14,
                  fontWeight: 700,
                  cursor: "pointer",
                }}
              >
                {banner}
              </button>
              <button
                type="button"
                onClick={() => {
                  setStale([]);
                  void load();
                }}
                style={{
                  ...microLabel,
                  fontSize: 10,
                  color: "var(--text2)",
                  background: "var(--card)",
                  border: "1px solid var(--line)",
                  borderRadius: 999,
                  padding: "4px 12px",
                  cursor: "pointer",
                }}
              >
                Recheck all
              </button>
            </div>
            {staleOpen ? (
              <div style={{ color: "var(--text2)", fontSize: 13, marginTop: 6 }}>
                {stale.join(", ")}
              </div>
            ) : null}
          </div>
        ) : null}

        {loading ? <Lede>Reading this machine…</Lede> : null}
        {down ? (
          <div style={{ marginTop: 12 }}>
            <div role="alert" style={{ color: "var(--puff-red)", fontSize: 14 }}>
              {down}
            </div>
            <div style={{ marginTop: 8 }}>
              <PuffButton label="Try again" onClick={() => void load()} />
            </div>
          </div>
        ) : null}

        <div style={{ marginTop: 16 }}>
          {rows.map((card) => (
            <SkillRow
              key={cardId(card)}
              card={card}
              open={expanded === cardId(card)}
              onToggle={() => toggle(cardId(card))}
              onApplied={(result) => applied(cardId(card), result)}
            />
          ))}
          {plain.map((row) => (
            <div
              key={row.skill}
              className="flex flex-wrap items-baseline justify-between gap-x-4"
              style={{ borderTop: "1px solid var(--line)", padding: "10px 8px 10px 10px" }}
            >
              <span>
                <span style={{ color: "var(--text2)", fontSize: 15 }}>{row.title}</span>
                <SkillId id={row.skill} />
              </span>
              <span style={{ ...microLabel, fontSize: 10 }}>{row.note}</span>
            </div>
          ))}
        </div>

        {refusals.length > 0 ? (
          <div style={{ marginTop: 14 }}>
            <Eyebrow>Steps that would not say what they found</Eyebrow>
            {refusals.map((line) => (
              <div key={line} style={{ color: "var(--text2)", fontSize: 13, marginTop: 4 }}>
                {line}
              </div>
            ))}
          </div>
        ) : null}

        <div
          style={{
            marginTop: 18,
            paddingTop: 12,
            borderTop: "1px solid var(--line)",
            color: "var(--text)",
            fontSize: 15,
          }}
        >
          Total to download: {totalLine(rows)}
        </div>

        <div className="flex flex-wrap items-center gap-3" style={{ marginTop: 12 }}>
          <PuffButton
            label={building ? "Applying…" : "Build it"}
            kind="primary"
            disabled={!ready || building || rows.length === 0}
            onClick={() => void build()}
          />
          {!ready ? (
            <span style={{ ...microLabel, fontSize: 10, color: "var(--lemon)" }}>
              {gateLabel(remaining.length)}
            </span>
          ) : null}
        </div>

        {buildLog.length > 0 ? (
          <div style={{ marginTop: 14 }} aria-live="polite">
            <Eyebrow>What ran</Eyebrow>
            {buildLog.map((line) => (
              <div
                key={line}
                style={{ fontFamily: MONO, fontSize: 12, color: "var(--text2)", marginTop: 4 }}
              >
                {line}
              </div>
            ))}
          </div>
        ) : null}
      </Card>
    </div>
  );
}
