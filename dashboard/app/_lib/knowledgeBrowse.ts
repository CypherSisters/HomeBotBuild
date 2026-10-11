/**
 * Pure browsing logic for the offline library viewer.
 *
 * A flat list gives no way to move between articles. A ZIM pack IS a website:
 * it has a main page, internal links, and a title index — so the viewer needs
 * the three things a browser has: a history stack, a way to read a clicked
 * link, and a stable prefix to jump the index by.
 *
 * Everything here is DOM-free and side-effect free so it can be unit tested in
 * plain Node (npm run test:knowledgebrowse). The React components own the
 * fetching and the rendering; this file owns the decisions.
 */

/** One stop in the viewer's history. `fragment` is the in-article anchor the
 *  link asked for, if any. */
export interface BrowseEntry {
  url: string;
  title: string;
  fragment?: string | null;
}

export interface BrowseHistory {
  entries: BrowseEntry[];
  /** Index of the entry on screen; -1 when nothing has been opened. */
  index: number;
}

/** Deep in a wiki a session can rack up hundreds of hops. The stack is only
 *  there to make Back/Forward feel like a browser, so the oldest entries are
 *  dropped rather than held forever in the kiosk's memory. */
export const HISTORY_CAP = 50;

export function historyInit(): BrowseHistory {
  return { entries: [], index: -1 };
}

/**
 * Open a new article. Anything ahead of the current position is discarded —
 * the same rule a browser uses: following a link from a Back-ed page forks a
 * new future.
 */
export function historyPush(state: BrowseHistory, entry: BrowseEntry): BrowseHistory {
  const kept = state.entries.slice(0, state.index + 1);
  kept.push(entry);
  const overflow = Math.max(0, kept.length - HISTORY_CAP);
  const entries = overflow ? kept.slice(overflow) : kept;
  return { entries, index: entries.length - 1 };
}

export function historyBack(state: BrowseHistory): BrowseHistory {
  if (state.index <= 0) return state;
  return { entries: state.entries, index: state.index - 1 };
}

export function historyForward(state: BrowseHistory): BrowseHistory {
  if (state.index < 0 || state.index >= state.entries.length - 1) return state;
  return { entries: state.entries, index: state.index + 1 };
}

export function canGoBack(state: BrowseHistory): boolean {
  return state.index > 0;
}

export function canGoForward(state: BrowseHistory): boolean {
  return state.index >= 0 && state.index < state.entries.length - 1;
}

export function currentEntry(state: BrowseHistory): BrowseEntry | null {
  if (state.index < 0 || state.index >= state.entries.length) return null;
  return state.entries[state.index];
}

/**
 * Normalise what the user typed into a `prefix=` the title index can match.
 *
 * ZIM title order is byte order over titles as stored, which are capitalised
 * ("Cheese", not "cheese"), so a lower-case prefix would land nowhere. Leading
 * space is noise from a touch keyboard; internal runs are collapsed so
 * "new  york" finds "New york..." rather than nothing.
 */
export function normalizePrefix(raw: string): string {
  const collapsed = (raw || "").replace(/\s+/g, " ").trim();
  if (!collapsed) return "";
  const capped = collapsed.slice(0, 64);
  return capped.charAt(0).toUpperCase() + capped.slice(1);
}

/** The minimum shape of a clicked node. Kept structural (not `Element`) so the
 *  walk can be tested without a DOM. */
export interface AnchorLike {
  tagName?: string;
  getAttribute(name: string): string | null;
  parentElement?: AnchorLike | null;
}

export type LinkTarget =
  /** An article inside this ZIM: load it in place, push history. */
  | { kind: "zim"; url: string; fragment: string | null }
  /** A pure in-page anchor: scroll the container, never touch the browser URL. */
  | { kind: "anchor"; fragment: string }
  /** http(s) out of the appliance: leave it to ExternalLinkGuard. */
  | { kind: "external"; href: string }
  | null;

/** How far up from the clicked node to look for the anchor. Wiki links wrap
 *  <b>, <i>, <span> and image spans, never anything like this deep. */
const ANCHOR_WALK_LIMIT = 8;

function closestAnchor(node: AnchorLike | null | undefined): AnchorLike | null {
  let cur: AnchorLike | null | undefined = node;
  for (let i = 0; cur && i < ANCHOR_WALK_LIMIT; i += 1) {
    if ((cur.tagName || "").toUpperCase() === "A") return cur;
    cur = cur.parentElement;
  }
  return null;
}

/**
 * Read a click inside the article body.
 *
 * The backend emits internal links as
 *   <a href="#" data-zim-url="C/Some_Article" data-zim-fragment="Section">
 * so the anchor itself is inert; the data attributes are the real target.
 * A click lands on whatever text node's element is on top, which for
 * `<a><b>Cheese</b></a>` is the <b> — hence the walk.
 */
export function linkTargetFromClick(node: AnchorLike | null | undefined): LinkTarget {
  const a = closestAnchor(node);
  if (!a) return null;

  const zimUrl = (a.getAttribute("data-zim-url") || "").trim();
  if (zimUrl) {
    const frag = (a.getAttribute("data-zim-fragment") || "").trim();
    return { kind: "zim", url: zimUrl, fragment: frag || null };
  }

  const href = (a.getAttribute("href") || "").trim();
  if (/^https?:\/\//i.test(href)) return { kind: "external", href };
  if (href.startsWith("#") && href.length > 1) {
    return { kind: "anchor", fragment: href.slice(1) };
  }
  return null;
}

/**
 * Inline style for the element that receives third-party article HTML.
 * Nothing inside may paint outside it: `contain: paint` clips descendants
 * and makes this box the containing block for any `position: fixed` one,
 * `overflow: hidden` clips the rest, and `isolation: isolate` keeps an
 * article z-index from stacking over the dashboard chrome. Inline, so it does
 * not depend on a stylesheet or a Tailwind class being present.
 */
export const ARTICLE_BOX_STYLE = {
  position: "relative",
  overflow: "hidden",
  contain: "paint",
  isolation: "isolate",
} as const;

/** The namespace the backend puts every article id and anchor name in, so
 *  article markup cannot clobber the dashboard's globals. Must match
 *  ID_PREFIX in src/puffpai/knowledge/zim_links.py. */
export const ARTICLE_ID_PREFIX = "zim-";

/** The element id a logical fragment ("History") lives under in rendered
 *  article HTML ("zim-History"). Idempotent, like the backend mapping. */
export function articleFragmentId(fragment: string): string {
  return fragment.startsWith(ARTICLE_ID_PREFIX) ? fragment : ARTICLE_ID_PREFIX + fragment;
}

/**
 * May the browser follow this article href on its own?
 *
 * Backstop for the backend sanitiser, which already removes every other
 * scheme: only http(s), mailto and in-page fragments are ever navigable from
 * an article. Whitespace and C0 controls are stripped first, the way a
 * browser does before reading the scheme, so "java<TAB>script:" is caught.
 */
export function isNavigableArticleHref(href: string | null | undefined): boolean {
  const probe = (href || "").replace(/[\u0000-\u0020\u007f]/g, "");
  if (!probe || probe.startsWith("#")) return true;
  const scheme = /^([A-Za-z][A-Za-z0-9+.-]*):/.exec(probe);
  if (!scheme) return true; // relative: same-origin, never a script URL
  return ["http", "https", "mailto"].includes(scheme[1].toLowerCase());
}

/** decodeURIComponent that never throws: a malformed "%" in one link
 *  falls back to the raw value, so one bad name cannot fail an article. */
export function safeDecodeURIComponent(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

/**
 * Point the backend's keyed asset URLs at the range-enabled same-origin asset
 * route: "/api/pai/knowledge/library/<col>/asset?path=<p>" becomes
 * "/api/knowledge/asset?collection=<col>&path=<p>". Each path is decoded once
 * (safely) and re-encoded, so a stray "%" breaks only its own link.
 */
export function rewriteArticleAssetUrls(html: string): string {
  return html.replace(
    /(?:\/api\/pai)?\/knowledge\/library\/([^/]+)\/asset\?path=([^"'&\s]+)/gi,
    (_match, col: string, path: string) =>
      `/api/knowledge/asset?collection=${encodeURIComponent(col)}&path=${encodeURIComponent(safeDecodeURIComponent(path))}`,
  );
}

/**
 * Is this main page worth landing on, or should the reader start at A-Z?
 *
 * Measured on the appliance: a real wiki landing page is a dense hub of
 * internal links (Wikipedia 134, Simple Wikipedia 76, iFixit 24). Project
 * Gutenberg's `C/Home` is a JavaScript datatables app with 3 — and scripts do
 * not run inside dangerouslySetInnerHTML, so it renders as a near-empty page
 * that looks like the library is broken. Under 5 links, the title index is the
 * better front door. Home stays in the toolbar either way: the page is still
 * there for anyone who asks for it.
 */
export function shouldPreferBrowse(html: string): boolean {
  if (!html) return true;
  const links = html.match(/data-zim-url=/gi);
  return (links ? links.length : 0) < 5;
}

/** Title shown in the toolbar for a link we have not loaded yet: the last ZIM
 *  path segment with underscores turned back into spaces. */
export function titleFromZimUrl(url: string): string {
  const tail = (url || "").split("/").filter(Boolean).pop() || url || "";
  let decoded = tail;
  try {
    decoded = decodeURIComponent(tail);
  } catch {
    /* a stray % in a ZIM path is not worth failing a navigation over */
  }
  return decoded.replace(/_/g, " ").replace(/\.html?$/i, "");
}
