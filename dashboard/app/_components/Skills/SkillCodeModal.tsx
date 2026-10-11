"use client";

/**
 * "View code" — the whole point of the skill consent flow.
 *
 * An approval of a skill is an approval of BYTES: the backend re-hashes every
 * file at install time and refuses if it no longer matches what this modal
 * showed. So this renders the payload of `GET /skills/<id>/draft` verbatim —
 * plain text in a <pre>, no markdown, no syntax highlighter, no trimming —
 * with the sha256 the backend computed from that same read printed beside each
 * file. If the text and the hash could ever come from two different reads, the
 * card could be made to lie by a write in between; they do not.
 *
 * It is deliberately readable rather than pretty: line numbers, monospace, and
 * the host list stated before the code, because the two questions the owner is
 * actually answering are "what does it reach" and "what does it do".
 *
 * The draft route serves the DRAFTS tree, so an already-installed skill has no
 * code view here. That is said in a sentence rather than hidden behind a
 * spinner or a dead button.
 *
 * puff puff pass
 */

import { useEffect, useState } from "react";
import { mdiClose } from "@mdi/js";
import Icon from "../Icon";
import OverlayLayer from "../OverlayLayer";
import {
  getSkillDraft,
  splitHost,
  type SkillDraft,
  type SkillManifest,
} from "@/app/_lib/services/skillService";

const MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace";

/**
 * The backend's own words, when they are the owner's business.
 *
 * The expected failure here is the draft route's 404 for an already-installed
 * skill, and it answers with a state path
 * ("/data/puffpai/state/skills/_drafts/x is not a skill directory"). The
 * sentence beside it already explains that case in English; repeating the
 * appliance's filesystem layout on a household screen adds nothing a person
 * can act on. A transport failure ("Failed to fetch") has no path in it and IS
 * worth showing, so the test is for a path rather than for a status code the
 * fetch helper has already thrown away.
 */
function ownerFacingError(message: string): string {
  return message.includes("/") ? "" : message;
}

/** Host list with the registrable domain carrying the weight. */
export function HostList({ hosts, size = 12 }: { hosts: string[]; size?: number }) {
  if (!hosts.length) {
    return (
      <span style={{ color: "var(--text2)", font: `600 ${size}px var(--font-nunito), sans-serif` }}>
        no network at all
      </span>
    );
  }
  return (
    <span style={{ font: `${size}px ${MONO}` }}>
      {hosts.map((h, i) => {
        const { prefix, domain } = splitHost(h);
        return (
          <span key={h}>
            {i > 0 && <span style={{ color: "var(--text2)" }}>, </span>}
            <span style={{ color: "var(--text2)" }}>{prefix}</span>
            <span style={{ color: "var(--text)", fontWeight: 800 }}>{domain}</span>
          </span>
        );
      })}
    </span>
  );
}

/**
 * The plain-language permission list.
 *
 * The empty lists matter as much as the full one: a skill that declares no
 * files, no Home Assistant domains and no secrets is a skill that cannot touch
 * the house, and that is worth a sentence rather than an absence.
 */
export function PermissionList({ manifest }: { manifest: SkillManifest | null }) {
  const perms = manifest?.permissions;
  const hosts = perms?.network_hosts ?? [];
  const quiet: string[] = [];
  if (!perms?.files?.length) quiet.push("no files");
  if (!perms?.ha_domains?.length) quiet.push("no Home Assistant");
  if (!perms?.secrets?.length) quiet.push("no passwords");

  return (
    <ul
      className="m-0 list-none p-0"
      style={{ font: "600 12px var(--font-nunito), sans-serif", color: "var(--text2)" }}
    >
      <li>
        Reaches <HostList hosts={hosts} /> over https, and nothing else.
      </li>
      {perms?.files?.length ? (
        <li>Files: {perms.files.join(", ")}</li>
      ) : null}
      {perms?.ha_domains?.length ? (
        <li>Home Assistant: {perms.ha_domains.join(", ")}</li>
      ) : null}
      {perms?.secrets?.length ? <li>Secrets: {perms.secrets.join(", ")}</li> : null}
      {quiet.length === 3 ? (
        <li>No files, no Home Assistant, no passwords.</li>
      ) : quiet.length ? (
        <li>{quiet.join(", ")}.</li>
      ) : null}
      {manifest?.limits && (
        <li>
          At most {manifest.limits.max_requests} request
          {manifest.limits.max_requests === 1 ? "" : "s"} per run, {manifest.limits.timeout_s}s
          time limit.
        </li>
      )}
      {manifest?.triggers?.length ? (
        <li>
          Answers when you say:{" "}
          <span style={{ font: `11px ${MONO}`, color: "var(--text)" }}>
            {manifest.triggers.map((t) => `“${t}”`).join("; ")}
          </span>
        </li>
      ) : null}
    </ul>
  );
}

/** One file: its hash, then its exact bytes, numbered. */
function FileBlock({ name, text, sha256, bytes }: {
  name: string;
  text: string;
  sha256: string;
  bytes: number;
}) {
  // Split, never re-join: what is displayed is what was hashed, line for line.
  const lines = text.split("\n");
  const width = String(lines.length).length;

  return (
    <section className="mb-4">
      <header
        className="flex flex-wrap items-baseline justify-between gap-2 px-3 py-2"
        style={{
          background: "var(--card2)",
          border: "1px solid var(--line)",
          borderBottom: "none",
          borderRadius: "10px 10px 0 0",
        }}
      >
        <span style={{ font: `700 12px ${MONO}`, color: "var(--text)" }}>{name}</span>
        <span style={{ font: `10px ${MONO}`, color: "var(--text2)" }}>
          {bytes} bytes · sha256 {sha256}
        </span>
      </header>
      <pre
        className="m-0 overflow-auto px-3 py-2"
        style={{
          font: `11px/1.55 ${MONO}`,
          color: "var(--text)",
          background: "var(--card)",
          border: "1px solid var(--line)",
          borderRadius: "0 0 10px 10px",
          maxHeight: 360,
          whiteSpace: "pre",
          tabSize: 4,
        }}
      >
        {lines.map((line, i) => (
          <div key={i}>
            <span
              aria-hidden
              style={{
                color: "var(--text2)",
                opacity: 0.5,
                userSelect: "none",
                display: "inline-block",
                width: `${width + 1}ch`,
              }}
            >
              {i + 1}
            </span>
            {line}
          </div>
        ))}
      </pre>
    </section>
  );
}

export default function SkillCodeModal({
  skillId,
  title,
  testLogTail,
  onClose,
}: {
  skillId: string;
  title: string;
  /** The tail of the sandboxed test run, when the caller has it (consent card). */
  testLogTail?: string;
  onClose: () => void;
}) {
  const [draft, setDraft] = useState<SkillDraft | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let live = true;
    setLoading(true);
    getSkillDraft(skillId)
      .then((d) => {
        if (live) {
          setDraft(d);
          setError(null);
        }
      })
      .catch((e: any) => {
        if (live) setError(e?.message || "Could not read that package.");
      })
      .finally(() => {
        if (live) setLoading(false);
      });
    return () => {
      live = false;
    };
  }, [skillId]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <OverlayLayer onClick={onClose} className="cursor-pointer">
      <div
        role="dialog"
        aria-modal="true"
        aria-label={`Code for ${title}`}
        onClick={(e) => e.stopPropagation()}
        className="z-50 w-11/12 cursor-auto overflow-auto lg:w-4/5 xl:w-3/5"
        style={{
          maxHeight: "calc(100dvh - 96px)",
          background: "var(--card)",
          border: "1px solid var(--line)",
          borderRadius: 16,
          padding: 16,
        }}
      >
        <div className="mb-3 flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 style={{ font: "800 15px var(--font-nunito), sans-serif", color: "var(--text)" }}>
              {title}
            </h2>
            <p style={{ font: "600 11px var(--font-nunito), sans-serif", color: "var(--text2)" }}>
              These are the exact bytes PuffPAI hashed. Approval is of these bytes; if
              they change, the skill stops running.
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            style={{ color: "var(--text2)" }}
          >
            <Icon path={mdiClose} size="18" />
          </button>
        </div>

        {loading && (
          <p style={{ font: "600 12px var(--font-nunito), sans-serif", color: "var(--text2)" }}>
            Reading the package&hellip;
          </p>
        )}

        {!loading && error && (
          <p
            className="px-3 py-2"
            style={{
              font: "600 12px var(--font-nunito), sans-serif",
              color: "var(--text2)",
              background: "var(--card2)",
              border: "1px solid var(--line)",
              borderRadius: 10,
            }}
          >
            The code view is served from the pending-approval copy of a skill, and this
            one is no longer there — usually because it is already installed. Its files
            are re-hashed before every run, so an installed skill that no longer matches
            what you approved switches itself off.
            {ownerFacingError(error) ? ` (${ownerFacingError(error)})` : ""}
          </p>
        )}

        {!loading && draft && (
          <>
            <div
              className="mb-4 px-3 py-2"
              style={{ background: "var(--card2)", border: "1px solid var(--line)", borderRadius: 10 }}
            >
              <p
                className="mb-1"
                style={{ font: "700 12px var(--font-nunito), sans-serif", color: "var(--text)" }}
              >
                {draft.manifest?.name} v{draft.manifest?.version} · by {draft.manifest?.author}
              </p>
              <p
                className="mb-2"
                style={{ font: "600 12px var(--font-nunito), sans-serif", color: "var(--text2)" }}
              >
                {draft.manifest?.description}
              </p>
              <PermissionList manifest={draft.manifest} />
              {draft.lint?.length ? (
                <p
                  className="mt-2"
                  style={{ font: "600 11px var(--font-nunito), sans-serif", color: "var(--pink)" }}
                >
                  Worth a look: {draft.lint.join("; ")}
                </p>
              ) : null}
            </div>

            {draft.files?.map((f) => (
              <FileBlock
                key={f.name}
                name={f.name}
                text={f.text}
                sha256={f.sha256}
                bytes={f.bytes}
              />
            ))}
          </>
        )}

        {!loading && testLogTail ? (
          <section>
            <p
              className="mb-1"
              style={{ font: "700 12px var(--font-nunito), sans-serif", color: "var(--text)" }}
            >
              Test run, in the sandbox, with canned answers
            </p>
            <pre
              className="m-0 overflow-auto px-3 py-2"
              style={{
                font: `11px/1.55 ${MONO}`,
                color: "var(--text2)",
                background: "var(--card2)",
                border: "1px solid var(--line)",
                borderRadius: 10,
                maxHeight: 160,
                whiteSpace: "pre-wrap",
              }}
            >
              {testLogTail}
            </pre>
          </section>
        ) : null}
      </div>
    </OverlayLayer>
  );
}
