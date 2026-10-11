import { apiGet, apiDelete, apiPut } from '../api';
import { rewriteArticleAssetUrls } from '../knowledgeBrowse';
import type { KnowledgeLibrary, LibraryArticle, ArticleContent } from '../../_interfaces/api';

export async function getLibraries(): Promise<KnowledgeLibrary[]> {
  const res = await apiGet<{ success: boolean; libraries: KnowledgeLibrary[] }>('/knowledge/libraries');
  return res.libraries || [];
}

/** One source or pack this build chose, offered or downloaded. */
export type BuildSourceStatus =
  | 'indexed'
  | 'fetched_not_indexed'
  | 'selected_not_downloaded'
  | 'offered';

export type BuildSource = {
  id: string;
  label: string;
  kind: string;
  /** 'documents' = bytes that can be indexed; 'live_source' = an address the
   *  bot reads, such as an API catalog. A live source is never counted as
   *  indexed documents. */
  form: 'documents' | 'live_source';
  status: BuildSourceStatus;
  domain: string;
  license: string;
  license_url: string;
  commercial_use: boolean;
  what_it_is: string;
  source_url: string;
  size_mb: number | null;
  size_label: string;
  selected_by: string[];
  command: string;
  note: string;
  filename?: string;
  bytes?: number;
  fetched_at?: string;
};

/** Read-only: what the wizard chose against what is actually on this disk.
 *  Downloads nothing — a not-downloaded source comes back with the command. */
export async function getBuildSources(): Promise<BuildSource[]> {
  try {
    const res = await apiGet<{ success: boolean; items: BuildSource[] }>(
      '/knowledge/sources',
    );
    return res.items || [];
  } catch {
    return [];
  }
}

export async function searchKnowledge(query: string): Promise<any[]> {
  const res = await apiGet<{ success: boolean; results: any[] }>(`/knowledge/search?q=${encodeURIComponent(query)}`);
  return res.results || [];
}

export type TitleSearchResult = {
  title: string;
  url: string;
  collection: string;
  library_name: string;
  mime_type: string;
};

export async function searchTitles(
  query: string,
  limit: number = 50,
): Promise<{ results: TitleSearchResult[]; total: number }> {
  const res = await apiGet<{
    success: boolean;
    results: TitleSearchResult[];
    total: number;
  }>(`/knowledge/search/titles?q=${encodeURIComponent(query)}&limit=${limit}`);
  return { results: res.results || [], total: res.total || 0 };
}

export type FulltextResult = {
  library_id: string;
  library_name: string;
  /** Article url accepted by /knowledge/library/<id>/article?url=... */
  path: string;
  /** RAW text — render as a text node, never as HTML. */
  title: string;
  /** Server-side HTML-escaped except for <b> highlight markers, so this one
   *  is safe to inject with dangerouslySetInnerHTML. */
  snippet: string;
  /** 0..1, only comparable within one library's hits. */
  relevance: number;
};

/** Full-text (SQLite FTS5) search over article bodies in indexed ZIM packs.
 *  Libraries without a built shard simply do not contribute results. */
export async function searchFulltext(
  query: string,
  opts: { maxResults?: number; library?: string | string[] } = {},
): Promise<{ results: FulltextResult[]; total: number; librariesSearched: string[] }> {
  const params = new URLSearchParams({ q: query });
  params.set('max_results', String(opts.maxResults ?? 20));
  const libs = opts.library === undefined ? [] : ([] as string[]).concat(opts.library);
  libs.filter(Boolean).forEach((lib) => params.append('library', lib));

  const res = await apiGet<{
    success: boolean;
    results: FulltextResult[];
    total: number;
    libraries_searched: string[];
  }>(`/knowledge/search/fulltext?${params.toString()}`);

  return {
    results: res.results || [],
    total: res.total || 0,
    librariesSearched: res.libraries_searched || [],
  };
}

export async function uploadDocument(
  file: File,
  zone: 1 | 2,
  title?: string,
): Promise<{ success: boolean }> {
  const formData = new FormData();
  formData.append('file', file);
  formData.append('zone', String(zone));
  // Optional human label. Omitted entirely when blank — an empty string would
  // suppress the label Puff derives for herself.
  if (title && title.trim()) formData.append('title', title.trim());

  // Same-origin through the authenticated proxy: the server attaches the
  // backend API key, so this works from any LAN device (a phone on /send, a
  // laptop browser) and not just the kiosk that had a key in localStorage.
  // No Authorization header — the client holds no credential.
  const res = await fetch('/api/pai/knowledge/upload', {
    method: 'POST',
    body: formData,
  });
  return res.json();
}

/**
 * Titles for uploads, keyed by collection id.
 *
 * The backend puts `title` on /ingest/list records but NOT on /knowledge/libraries, and My Documents reads the
 * latter. Without this merge a rename would save server-side and then vanish
 * from the page on the next reload. The ids line up: an /ingest/list `id` is
 * the same slug as a library's `collection`.
 *
 * Read-only and best-effort — a failure just means filenames, never an error.
 */
export async function getUploadTitles(): Promise<Record<string, string>> {
  try {
    const res = await apiGet<{
      documents?: { id?: string; title?: string; filename?: string }[];
    }>('/ingest/list');
    const out: Record<string, string> = {};
    for (const doc of res.documents || []) {
      const id = (doc.id || '').trim();
      const title = (doc.title || '').trim();
      // The backend falls back to the filename, so storing that is harmless:
      // the render site's own fallback produces the identical string.
      if (id && title) out[id] = title;
    }
    return out;
  } catch {
    return {};
  }
}

/** Rename what a stored upload is called. The collection id never changes —
 *  only the label, so every existing link, article URL and source embed keeps
 *  working. A blank title is rejected here rather than sent. */
export async function renameLibrary(
  collection: string,
  title: string,
): Promise<{ success: boolean }> {
  return apiPut(`/knowledge/library/${encodeURIComponent(collection)}`, {
    title: title.trim(),
  });
}

export async function deleteLibrary(name: string): Promise<{ success: boolean }> {
  return apiDelete(`/knowledge/library/${encodeURIComponent(name)}`);
}

export async function browseLibrary(
  collection: string,
  page: number = 0,
  limit: number = 50,
  search?: string,
): Promise<{ articles: LibraryArticle[]; total: number; has_more: boolean }> {
  const params = new URLSearchParams({ page: String(page), limit: String(limit) });
  if (search) params.set('q', search);
  const res = await apiGet<{
    success: boolean;
    articles: LibraryArticle[];
    total: number;
    has_more: boolean;
  }>(`/knowledge/library/${encodeURIComponent(collection)}/articles?${params}`);
  return { articles: res.articles || [], total: res.total || 0, has_more: res.has_more || false };
}

/** One page of the ZIM title index, with everything the browser UI needs to
 *  keep walking it.
 *
 *  `total` is deliberately `number | null`: on a pack with millions of entries
 *  the backend answers with null rather than paying for a full count, and the
 *  UI has to say nothing instead of "0 articles".
 *  `nextAfter` is a keyset cursor — the only way to page deep into a title
 *  index without the offset scan that made this request hang in the first
 *  place. Use it when present; fall back to page numbers when it is not. */
export type ArticlePage = {
  articles: LibraryArticle[];
  total: number | null;
  hasMore: boolean;
  nextAfter: number | null;
  source: string;
};

export async function browseArticlePage(
  collection: string,
  opts: {
    page?: number;
    limit?: number;
    /** Fast title search inside this library. */
    q?: string;
    /** Jump the index to the first title starting with this text. */
    prefix?: string;
    /** Keyset cursor from a previous page's nextAfter. */
    after?: number | null;
    signal?: AbortSignal;
  } = {},
): Promise<ArticlePage> {
  const params = new URLSearchParams({
    page: String(opts.page ?? 0),
    limit: String(opts.limit ?? 50),
  });
  if (opts.q) params.set('q', opts.q);
  if (opts.prefix) params.set('prefix', opts.prefix);
  if (opts.after !== undefined && opts.after !== null) params.set('after', String(opts.after));

  const res = await apiGet<{
    success: boolean;
    articles: LibraryArticle[];
    total: number | null;
    has_more: boolean;
    next_after?: number | null;
    source?: string;
  }>(`/knowledge/library/${encodeURIComponent(collection)}/articles?${params}`, {
    signal: opts.signal,
  });

  return {
    articles: res.articles || [],
    total: typeof res.total === 'number' ? res.total : null,
    hasMore: res.has_more || false,
    nextAfter: typeof res.next_after === 'number' ? res.next_after : null,
    source: res.source || '',
  };
}

/** The library's own front page (a wiki's real main page), or null when the
 *  pack has none. A 404 here is a normal answer, not a failure. */
export async function getMainPage(collection: string): Promise<ArticleContent | null> {
  try {
    const res = await apiGet<{ success: boolean; article: ArticleContent }>(
      `/knowledge/library/${encodeURIComponent(collection)}/main`,
    );
    if (!res.success || !res.article) return null;
    if (res.article.html) res.article.html = rewriteAssetUrls(res.article.html);
    return res.article;
  } catch {
    return null;
  }
}

/** Same-origin URL for embedding an uploaded document's original file
 *  (PDF viewer). Proxied through the Next server so the browser doesn't need
 *  to send an API key on the <embed>/<iframe> request. */
export function librarySourceEmbedUrl(collection: string): string {
  return `/api/knowledge/source?name=${encodeURIComponent(collection)}`;
}

/** Fetch a user-uploaded document's text — the retained original reconstructed
 *  from the indexed chunks when no viewable original file is available. */
export async function getLibrarySourceText(
  collection: string,
): Promise<{ text: string; note: string } | null> {
  try {
    const res = await apiGet<{ success: boolean; text: string; note: string }>(
      `/knowledge/library/${encodeURIComponent(collection)}/source?format=text`,
    );
    if (!res.success) return null;
    return { text: res.text || '', note: res.note || '' };
  } catch {
    return null;
  }
}

/**
 * Rewrite asset URLs to the range-enabled same-origin asset route.
 *
 * The backend emits these already prefixed with the keyed proxy path,
 * "/api/pai/knowledge/library/<col>/asset?path=...". The optional
 * "(?:/api/pai)?" is load-bearing: without it the regex matched only the
 * "/knowledge/library/.../asset" tail and left "/api/pai" in front, so the
 * result was "/api/pai/api/knowledge/asset?..." — a doubly-prefixed 404 that
 * made TED videos, posters and subtitles silently fail to load (black player,
 * 0:00). Consuming the prefix yields the correct
 * "/api/knowledge/asset?collection=<col>&path=<path>", which streams with HTTP
 * range support so the <video> plays and seeks.
 *
 * Shared by getArticle and getMainPage: a wiki main page is full of images and
 * would otherwise render with broken ones.
 */
function rewriteAssetUrls(html: string): string {
  // Pure and tested in knowledgeBrowse.ts; a malformed "%" in one asset name
  // no longer throws and blanks the whole article.
  return rewriteArticleAssetUrls(html);
}

export async function getArticle(
  collection: string,
  url: string,
  opts?: { fresh?: boolean },
): Promise<ArticleContent | null> {
  try {
    const res = await apiGet<{ success: boolean; article: ArticleContent }>(
      `/knowledge/library/${encodeURIComponent(collection)}/article?url=${encodeURIComponent(url)}`,
      // Deep-linked "show me X" opens the same article URL repeatedly; skip the
      // HTTP cache so a re-show never renders a stale copy on iframe remount.
      opts?.fresh ? { cache: "no-store" } : undefined,
    );
    if (!res.article) return null;
    if (res.article.html) res.article.html = rewriteAssetUrls(res.article.html);
    return res.article;
  } catch {
    return null;
  }
}
