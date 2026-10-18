// The review screen's four refusals, proven without a browser.
//
// This screen is where an owner finds out what a
// build is about to do, so the parts that could lie are the parts worth
// proving:
//
//  1. `basis` is about the TIME. A row's bytes are a size the registry
//     declares, so they are summed even when nobody has timed the download;
//     what is refused is a confident MINUTE count. One unknown row and the
//     total says "about 96 GB declared · time unknown" — it neither drops the
//     row nor prints a time nobody measured.
//  2. [Build it] is disabled until every consequential row has been opened
//     once, and the label beside it says how many are left.
//  3. Staleness is ONE collapsed banner with a count, never a badge per row.
//  4. A skill with no card yet is LISTED as a plain row ("set in the
//     step-by-step setup"), never hidden.
//
// Exits non-zero on failure so it can sit in `npm run test:gates`.
//
// Run from the dashboard directory:
//   npm run test:gates
//
// Copyright (c) Cypher Sisters. Apache-2.0 License.

import type { WizardCard } from "../app/_lib/services/wizardService";
import {
  acknowledgedIds,
  canBuild,
  cardId,
  decidedLine,
  fieldForError,
  formatBytes,
  gateLabel,
  groupFields,
  inSpecOrder,
  infoBlock,
  notConvertedRows,
  pendingAccepts,
  reasonText,
  rowCost,
  staleBanner,
  totalLine,
  unopenedConsequential,
} from "../app/onboarding/review/reviewModel";

// A GB is 1024 MB everywhere in this wizard (cards.format_bytes and its mirror
// in reviewModel.formatBytes). The fixtures below are stated in those units, so
// a size an owner reads on a row is the same number the backend put in the
// card's own words.
const GB = 1024 ** 3;

let failures = 0;

function check(name: string, ok: boolean, detail = "") {
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${name}${detail ? ` — ${detail}` : ""}`);
  if (!ok) failures += 1;
}

function card(
  skill: string,
  extra: Partial<WizardCard> = {},
): WizardCard {
  return {
    schema: "wizard_card/v1",
    skill,
    section: skill,
    part: "",
    title: skill,
    decided: "something",
    status: "ready",
    consequential: false,
    consequential_reasons: [],
    estimate: { bytes: 0, seconds: 0, basis: "measured" },
    fields: [],
    gaps: [],
    ...extra,
  };
}

// The owner's real build: a brain that knows its own size, packs that do not
// know how long they will take, and a vault with no PIN on it yet.
const BRAIN = card("brain", {
  title: "What does the thinking",
  estimate: { bytes: 4.7 * GB, seconds: 600, basis: "calibrated" },
  consequential: true,
  consequential_reasons: ["heavy_bytes"],
});
const INTEGRATE = card("integrate", {
  title: "Getting it in",
  estimate: { bytes: 96 * GB, seconds: null, basis: "unknown" },
  consequential: true,
  consequential_reasons: ["heavy_bytes"],
});
const SECURE = card("secure", {
  title: "What is locked, what gets out",
  status: "needs_you",
  consequential: true,
  consequential_reasons: ["vault_pin_unset", "runtime_egress"],
});
const DISCOVER = card("discover", {
  title: "What this machine is",
  estimate: { bytes: 0, seconds: 3, basis: "measured" },
});
const PURPOSE = card("purpose", { title: "What it is for" });

const BUILD = [DISCOVER, PURPOSE, BRAIN, INTEGRATE, SECURE];

console.log("\nthe total sums declared bytes and refuses to invent a time");
{
  const line = totalLine([BRAIN, INTEGRATE]);
  check("bytes are still summed", line.startsWith("about 101 GB declared"), line);
  check("and the total says where the number came from",
    line.includes("declared"), line);
  check("and the time says unknown", line.includes("time unknown"), line);
  check(
    "no confident minute count survives the unknown row",
    !/\d+ (min|sec|hr)/.test(line),
    line,
  );
  const known = totalLine([BRAIN, DISCOVER]);
  check("all-known rows do get a time", /\d+ min/.test(known), known);
  check("and their bytes", known.startsWith("about 4.7 GB declared"), known);
  const nothing = totalLine([PURPOSE]);
  check("a build with no download says so", nothing === "nothing to download · no wait", nothing);

  // The row's own line carries the same honesty ("96 GB · time unknown").
  check("an unknown row states both", rowCost(INTEGRATE.estimate) === "96 GB · time unknown",
    rowCost(INTEGRATE.estimate));
  check("a measured row with no bytes states its time", rowCost(DISCOVER.estimate) === "3 sec",
    rowCost(DISCOVER.estimate));
  check("bytes read as a person reads them", formatBytes(4.7 * GB) === "4.7 GB",
    formatBytes(4.7 * GB));
  check("and a GB is 1024 MB, as the backend says it", formatBytes(1024 ** 2) === "1 MB",
    formatBytes(1024 ** 2));
  check("and nothing is not 0 GB", formatBytes(0) === "", `"${formatBytes(0)}"`);
}

console.log("\n[Build it] is gated on the heavy rows being read");
{
  check("nothing opened: refused", !canBuild(BUILD, []));
  check("three rows to read", unopenedConsequential(BUILD, []).length === 3);
  check("the label counts them", gateLabel(3) === "Open the 3 marked rows first", gateLabel(3));
  check("one left is not '1 rows'", gateLabel(1) === "Open the 1 marked row first", gateLabel(1));
  check(
    "opening the light rows changes nothing",
    !canBuild(BUILD, ["discover", "purpose"]),
    unopenedConsequential(BUILD, ["discover", "purpose"]).join(","),
  );
  check(
    "two of three heavy rows is still refused",
    !canBuild(BUILD, ["brain", "integrate"]),
  );
  check(
    "every heavy row read: allowed",
    canBuild(BUILD, ["brain", "integrate", "secure"]),
  );
  check("and the label is gone", gateLabel(0) === "", `"${gateLabel(0)}"`);
  check(
    "a build with no heavy rows is allowed from the start",
    canBuild([DISCOVER, PURPOSE], []),
  );
  // Every heavy row says WHY it is heavy, in words, not a coloured dot.
  check(
    "the vault row names both reasons",
    reasonText(SECURE.consequential_reasons) ===
      "the vault PIN is not set · turns on internet access while it runs",
    reasonText(SECURE.consequential_reasons),
  );
  check("a light row claims nothing", reasonText(DISCOVER.consequential_reasons) === "");

  // [Build it] accepts the proposals nobody opened, and never re-applies a row
  // the owner already answered.
  const pending = pendingAccepts(BUILD, ["brain", "integrate", "secure", "purpose"]);
  check("only the unopened light rows are accepted", pending.join(",") === "discover", pending.join(","));
  check(
    "a heavy row is never accepted unseen",
    !pendingAccepts(BUILD, []).includes("brain"),
    pendingAccepts(BUILD, []).join(","),
  );
}

console.log("\nstaleness is one banner with a count");
{
  check("nothing stale: no banner", staleBanner([]) === null);
  check("undefined: no banner", staleBanner(undefined) === null);
  check("three stale", staleBanner(["prove", "context", "handoff"]) === "3 things need rechecking",
    String(staleBanner(["prove", "context", "handoff"])));
  check("one stale is singular", staleBanner(["prove"]) === "1 thing needs rechecking",
    String(staleBanner(["prove"])));
  check(
    "the same skill twice is one thing",
    staleBanner(["prove", "prove"]) === "1 thing needs rechecking",
    String(staleBanner(["prove", "prove"])),
  );
}

console.log("\na skill with no card is listed, not hidden");
{
  // The backend names them; this file keeps no list of the twelve, which is
  // the whole point - one source of truth for what the wizard is made of.
  const plain = notConvertedRows([{ skill: "prove", title: "Proof it works" }]);
  check("the backend's rows are the rows", plain.length === 1, String(plain.length));
  check("in the owner's words", plain[0].title === "Proof it works", plain[0].title);
  check("with the skill id kept", plain[0].skill === "prove", plain[0].skill);
  check(
    "and an honest note",
    plain.every((row) => row.note === "set in the step-by-step setup"),
  );
  check("nothing to list is no rows", notConvertedRows([]).length === 0);
  check("a backend that says nothing lists nothing",
    notConvertedRows(undefined).length === 0);
}

console.log("\na skill that asks more than one question gets one row each");
{
  const vault = card("secure", { part: "vault", consequential: true,
    consequential_reasons: ["vault_pin_unset"] });
  const bridge = card("secure", { part: "bridge" });
  check("the id carries the part", cardId(vault) === "secure:vault", cardId(vault));
  check("a single-question skill keeps its plain id",
    cardId(DISCOVER) === "discover", cardId(DISCOVER));
  check("the two rows are never the same row", cardId(vault) !== cardId(bridge));
  check("only the opened heavy row is acknowledged",
    acknowledgedIds([vault, bridge], ["secure:vault"]).join(",") === "secure:vault",
    acknowledgedIds([vault, bridge], ["secure:vault"]).join(","));
  check("an unopened heavy row is acknowledged by nobody",
    acknowledgedIds([vault, bridge], []).length === 0);
}

console.log("\nthe rest of the screen's arithmetic");
{
  const shuffled = inSpecOrder([SECURE, DISCOVER, BRAIN],
    ["discover", "brain", "secure"]);
  check("rows are drawn in the backend's order",
    shuffled.map((c) => c.skill).join(",") === "discover,brain,secure",
    shuffled.map((c) => c.skill).join(","));
  check("a row the order does not name is drawn last, never dropped",
    inSpecOrder([card("mystery"), DISCOVER], ["discover"])
      .map((c) => c.skill).join(",") === "discover,mystery");

  // A refusal that names a field is shown beside that field, and one that
  // names nothing this card has stays a card-level error.
  check("a refusal finds its field",
    fieldForError("'vault_pin' takes at most 128 characters.", ["vault_pin", "doors"]) === "vault_pin");
  check("an unrelated refusal stays card-level",
    fieldForError("This card has no field called 'nope'.", ["vault_pin"]) === null);
  check("a refusal with no field name stays card-level",
    fieldForError("Nothing was changed.", ["vault_pin"]) === null);

  // Product rules that reach the screen as strings.
  const words = [
    ...Object.values(BUILD.map((c) => c.title)),
    totalLine(BUILD),
    gateLabel(3),
    String(staleBanner(["prove"])),
    reasonText(SECURE.consequential_reasons),
  ];
  // The retired brand word, assembled rather than spelled: this repo's rule is
  // that it is never written out, and a proof is not exempt from it.
  const RETIRED_BRAND = ["h", "a", "t", "c", "h"].join("");
  check("no banned word", !words.some((w) => w.toLowerCase().includes(RETIRED_BRAND)));
  check("no row says 'configured'", !words.some((w) => w.toLowerCase().includes("configured")));
}

console.log("\nthe screen says how much of the build has been decided");
{
  check("a fresh build says nothing has been applied",
    decidedLine(14, []) === "Nothing has been applied. Open a row to change what it decided.",
    decidedLine(14, []));
  check("and after three applies it counts them",
    decidedLine(14, ["discover", "brain", "secure:posture"])
      === "3 of 14 decided so far. Open a row to change what it decided.",
    decidedLine(14, ["discover", "brain", "secure:posture"]));
  check("the same row twice is still one row",
    decidedLine(14, ["discover", "discover"]).startsWith("1 of 14"),
    decidedLine(14, ["discover", "discover"]));
}

console.log("\ninfo lines are a quiet block, never a label per sentence");
{
  // The starter catalog read "SUGGESTED / Knowledge packs: / SUGGESTED /
  // - Science…": a label over every line of a list nobody can answer.
  const block = infoBlock([
    { id: "matched", label: "From that goal, the starter catalog suggests", current: "" },
    { id: "matched_1", label: "Suggested", current: "Knowledge packs:" },
    { id: "matched_2", label: "Suggested", current: "- Science" },
  ]);
  check("one small label for the block",
    block.heading === "From that goal, the starter catalog suggests", block.heading);
  check("and none on the lines under it",
    block.lines.every((line) => line.label === ""),
    block.lines.map((line) => line.label).join("|"));
  check("every line still has its words",
    block.lines.map((line) => line.text).join("|") === "Knowledge packs:|- Science",
    block.lines.map((line) => line.text).join("|"));

  // Genuinely different info rows keep their own labels.
  const mixed = infoBlock([
    { id: "machine", label: "What was found", current: "Windows - 64 GB" },
    { id: "memory_gb", label: "Memory", current: "64 GB (measured on this machine)" },
  ]);
  check("distinct rows keep their labels",
    mixed.heading === "" && mixed.lines.map((line) => line.label).join("|")
      === "What was found|Memory",
    `${mixed.heading}::${mixed.lines.map((line) => line.label).join("|")}`);

  // And a run of info fields is one block, with answerable fields left alone.
  const groups = groupFields([
    { id: "a", kind: "info" },
    { id: "b", kind: "info" },
    { id: "model", kind: "choice" },
    { id: "c", kind: "info" },
  ]);
  check("consecutive info fields group, answerable ones do not",
    groups.map((g) => (g.kind === "info" ? `info:${g.fields.length}` : g.field.id)).join(",")
      === "info:2,model,info:1",
    groups.map((g) => (g.kind === "info" ? `info:${g.fields.length}` : g.field.id)).join(","));
}

console.log(failures === 0 ? "\nwizard review: PASS" : `\nwizard review: ${failures} FAILURE(S)`);
if (failures > 0) process.exit(1);
