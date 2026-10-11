/**
 * "Indexed — searchable now" is a CLAIM. This is how it gets checked.
 *
 * WHY
 * ---
 * An upload that returns 200 has been stored. That is not the same thing as
 * being retrievable: embedding can be unavailable (the backend answers
 * /knowledge/search with 503 and an explanation), the indexer can lag, or the
 * document can land in a store the search path does not read. The dashboard used
 * to say "uploaded" and leave the household to discover the gap by asking Puff a
 * question and being told nothing was found.
 *
 * So after every upload the UI asks the same question the household will ask —
 * a real /knowledge/search for the document's own name — and only says the
 * document is searchable once that search has come back with it. If it has not
 * within the window, the UI says what actually happened instead of promising.
 *
 * /knowledge/search is the one door on purpose: the backend unified it over both
 * upload stores, so a document uploaded on the Documents page, the Knowledge
 * page or Send to Puff is confirmed through the same call that chat uses to cite
 * it. Confirming through the page's own narrow list endpoint would re-create
 * exactly the split this is meant to prove is gone.
 *
 * puff puff pass
 */

/** What the poll concluded. Nothing here is optimistic. */
export type IndexConfirmation =
  /** A real search for this document's name returned it. Safe to claim. */
  | { status: "searchable" }
  /**
   * The search worked but has not returned this document yet. NOT an error and
   * not a success — the honest words for it are "stored, not searchable yet".
   */
  | { status: "pending" }
  /** The search itself could not answer. `message` is the backend's own words. */
  | { status: "error"; message: string };

/** Shape of the /knowledge/search answer this reads. Extra keys are ignored. */
interface SearchAnswer {
  success?: boolean;
  message?: string;
  sources?: { title?: string; library?: string }[];
  results?: { library?: string; text?: string }[];
}

/**
 * The part of a filename worth searching for: no directories, no extension.
 * "Mum's lasagna (final).pdf" -> "Mum's lasagna (final)".
 */
export function documentQuery(filename: string): string {
  const base = filename.split(/[\\/]/).pop() ?? filename;
  return base.replace(/\.[A-Za-z0-9]{1,8}$/, "").trim() || base;
}

/** Does this answer actually mention the document we just uploaded? */
function mentions(answer: SearchAnswer, filename: string): boolean {
  const base = (filename.split(/[\\/]/).pop() ?? filename).toLowerCase();
  const stem = documentQuery(filename).toLowerCase();
  const haystacks = [
    ...(answer.sources ?? []).flatMap((s) => [s.title ?? "", s.library ?? ""]),
    ...(answer.results ?? []).map((r) => r.library ?? ""),
  ].map((s) => s.toLowerCase());
  // Matched on NAME, not on "there were some results": a semantic search for a
  // recipe will happily return an unrelated recipe, and counting that as
  // confirmation would make the green line meaningless.
  return haystacks.some((h) => h.includes(base) || (stem.length >= 3 && h.includes(stem)));
}

/**
 * Poll /knowledge/search for `filename` until it comes back, or the window ends.
 *
 * @param filename  the uploaded file's name, as the backend recorded it
 * @param opts.base the API base ("/api/pai"); passed in so no module here
 *                  hardcodes a second copy of it
 * @param opts.timeoutMs total budget. 5 s: long enough for a small document to
 *                  finish embedding, short enough that nobody watches a spinner.
 */
export async function confirmSearchable(
  filename: string,
  opts: { base: string; timeoutMs?: number; intervalMs?: number } = { base: "/api/pai" },
): Promise<IndexConfirmation> {
  const timeoutMs = opts.timeoutMs ?? 5000;
  const intervalMs = opts.intervalMs ?? 700;
  const deadline = Date.now() + timeoutMs;
  const q = encodeURIComponent(documentQuery(filename));
  let lastMessage = "";

  for (;;) {
    try {
      const res = await fetch(`${opts.base}/knowledge/search?q=${q}&n=10`, {
        cache: "no-store",
      });
      const data = (await res.json().catch(() => ({}))) as SearchAnswer;
      if (!res.ok) {
        // 503 here is the embedding model missing — a standing condition, not a
        // slow index, so there is no point spending the rest of the window.
        const msg =
          data.message ||
          (res.status === 503
            ? "Search is unavailable on this box, so the document cannot be found yet."
            : `Search failed (HTTP ${res.status}).`);
        return { status: "error", message: msg };
      }
      if (mentions(data, filename)) return { status: "searchable" };
      lastMessage = data.message || "";
    } catch {
      lastMessage = "Could not reach the search service.";
    }
    if (Date.now() + intervalMs >= deadline) {
      return lastMessage && lastMessage !== "Could not reach the search service."
        ? { status: "pending" }
        : lastMessage
          ? { status: "error", message: lastMessage }
          : { status: "pending" };
    }
    await new Promise((r) => setTimeout(r, intervalMs));
  }
}

/** The one line every upload surface shows, so all three say the same thing. */
export const SEARCHABLE_EVERYWHERE =
  "Anything you upload here is searchable everywhere — the Knowledge page, the Documents page and chat with Puff all read the same store.";

/** Wording for each outcome, so no page invents its own optimistic phrasing. */
export function confirmationLine(c: IndexConfirmation | null): string {
  if (!c) return "";
  if (c.status === "searchable") return "Indexed — searchable now";
  if (c.status === "pending")
    // Two real causes, and the UI cannot tell them apart from here: the index is
    // still catching up, or the backend deliberately kept this document out of
    // search because it looks like personal detail. Both are named rather than
    // papered over with a spinner that eventually turns green.
    return "Stored, but a search for it did not return it — either the index is still catching up, or it was kept out of search because it looks like personal information.";
  return c.message;
}
