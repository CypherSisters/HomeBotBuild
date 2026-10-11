"use client";

/**
 * The title index of one offline library: the A-Z pages of an encyclopaedia.
 *
 * Three ways in, because a pack can hold eight million titles and scrolling is
 * not one of them:
 *   letter bar   prefix=A  — jump the index
 *   starts with  prefix=<typed>, debounced
 *   find a title q=<typed>, the backend's fast title search
 *
 * Paging prefers the keyset cursor (next_after) the API hands back, because
 * offset paging deep into a ZIM index is a scan that can hang this
 * request forever. Page numbers are the fallback for packs answering without
 * a cursor.
 *
 * Every request carries a 20s deadline. A failure says what happened in one
 * line and offers Retry; it never again claims that browsing "is not
 * available", which was a lie about a backend timeout.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  mdiAlertCircle,
  mdiChevronRight,
  mdiFileDocument,
  mdiInboxFull,
  mdiLoading,
  mdiPlayCircle,
  mdiRefresh,
} from "@mdi/js";
import Icon from "../../../_components/Icon";
import { browseArticlePage } from "../../../_lib/services/knowledgeService";
import { normalizePrefix } from "../../../_lib/knowledgeBrowse";
import type { LibraryArticle } from "../../../_interfaces/api";

const LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789".split("");
const PAGE_SIZE = 50;
const REQUEST_TIMEOUT_MS = 20_000;

const chip = (active: boolean): React.CSSProperties => ({
  minWidth: 44,
  minHeight: 44,
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  borderRadius: 8,
  border: `1px solid ${active ? "rgba(0,240,255,0.35)" : "var(--line)"}`,
  background: active ? "rgba(0,240,255,0.10)" : "var(--card2)",
  color: active ? "#e2e8f0" : "var(--text2)",
  fontSize: "0.85rem",
  fontWeight: 600,
  cursor: "pointer",
});

type Query = { prefix: string; q: string };

export default function ArticleIndex({
  collection,
  libraryName,
  isVideo,
  onOpen,
}: {
  collection: string;
  libraryName: string;
  isVideo: boolean;
  onOpen: (article: LibraryArticle) => void;
}) {
  const [query, setQuery] = useState<Query>({ prefix: "", q: "" });
  const [prefixInput, setPrefixInput] = useState("");
  const [titleInput, setTitleInput] = useState("");

  const [articles, setArticles] = useState<LibraryArticle[]>([]);
  const [total, setTotal] = useState<number | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  /** One entry per page visited in this run: the `after` cursor that produced
   *  it (null = start). Prev walks back down it. */
  const [cursors, setCursors] = useState<(number | null)[]>([null]);
  const [pageIdx, setPageIdx] = useState(0);

  const abortRef = useRef<AbortController | null>(null);
  const lastRef = useRef<{ q: Query; after: number | null; pageIdx: number }>({
    q: { prefix: "", q: "" },
    after: null,
    pageIdx: 0,
  });

  const run = useCallback(
    async (q: Query, after: number | null, idx: number) => {
      lastRef.current = { q, after, pageIdx: idx };
      abortRef.current?.abort();
      const ctrl = new AbortController();
      abortRef.current = ctrl;
      const timer = setTimeout(() => ctrl.abort(), REQUEST_TIMEOUT_MS);

      setLoading(true);
      setError(null);
      try {
        const res = await browseArticlePage(collection, {
          page: idx,
          limit: PAGE_SIZE,
          prefix: q.prefix || undefined,
          q: q.q || undefined,
          after,
          signal: ctrl.signal,
        });
        if (ctrl.signal.aborted) return;
        setArticles(res.articles);
        setTotal(res.total);
        setHasMore(res.hasMore);
        setPageIdx(idx);
        // Record the cursor that opens the NEXT page, so Next never has to
        // guess and Prev can walk back to exactly where it was.
        setCursors((prev) => {
          const next = prev.slice(0, idx + 1);
          next[idx + 1] = res.nextAfter;
          return next;
        });
      } catch (err: any) {
        if (ctrl.signal.aborted && err?.name === "AbortError") {
          // Aborted by us (timeout) or superseded by a newer request. Only the
          // timeout should surface, and only if this is still the live one.
          if (abortRef.current === ctrl) {
            setError(`No answer from the library index after ${REQUEST_TIMEOUT_MS / 1000} seconds.`);
            setLoading(false);
          }
          return;
        }
        setError(err?.message || "The library index could not be read.");
      } finally {
        clearTimeout(timer);
        if (abortRef.current === ctrl) setLoading(false);
      }
    },
    [collection],
  );

  // First page, and a reload whenever the query changes.
  useEffect(() => {
    setCursors([null]);
    run(query, null, 0);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [collection, query.prefix, query.q]);

  useEffect(() => () => abortRef.current?.abort(), []);

  // Debounced "starts with". Typing here drops any title search: they are two
  // different questions and answering both at once answers neither.
  useEffect(() => {
    const t = setTimeout(() => {
      const p = normalizePrefix(prefixInput);
      setQuery((cur) => (cur.prefix === p && !cur.q ? cur : { prefix: p, q: "" }));
    }, 300);
    return () => clearTimeout(t);
  }, [prefixInput]);

  useEffect(() => {
    const t = setTimeout(() => {
      const s = titleInput.trim();
      if (!s) {
        setQuery((cur) => (cur.q ? { prefix: "", q: "" } : cur));
        return;
      }
      setQuery((cur) => (cur.q === s ? cur : { prefix: "", q: s }));
    }, 300);
    return () => clearTimeout(t);
  }, [titleInput]);

  const pickLetter = (letter: string) => {
    setTitleInput("");
    setPrefixInput(letter);
    setQuery({ prefix: letter, q: "" });
  };

  const goNext = () => run(query, cursors[pageIdx + 1] ?? null, pageIdx + 1);
  const goPrev = () => run(query, cursors[pageIdx - 1] ?? null, pageIdx - 1);

  return (
    <div>
      {/* Letter bar. First in the panel and never behind a toggle: page one of
          a ZIM title index is punctuation ("!!", "!important"), so the letters
          are the real way in. */}
      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          gap: 6,
          marginBottom: 10,
          alignItems: "center",
        }}
      >
        <span
          style={{
            color: "var(--text3)",
            fontSize: "0.7rem",
            fontWeight: 600,
            letterSpacing: "0.08em",
            textTransform: "uppercase",
            marginRight: 4,
          }}
        >
          Jump to
        </span>
        <button
          type="button"
          style={chip(!query.prefix && !query.q)}
          onClick={() => {
            setTitleInput("");
            setPrefixInput("");
            setQuery({ prefix: "", q: "" });
          }}
          title="Start of the index"
        >
          All
        </button>
        {LETTERS.map((l) => (
          <button
            key={l}
            type="button"
            style={chip(query.prefix === l)}
            onClick={() => pickLetter(l)}
          >
            {l}
          </button>
        ))}
      </div>

      {/* Two questions, two boxes */}
      <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginBottom: 12 }}>
        <input
          type="text"
          value={prefixInput}
          onChange={(e) => setPrefixInput(e.target.value)}
          placeholder="Starts with..."
          className="kb-viewer-search"
          style={{ flex: "1 1 180px", minHeight: 44 }}
        />
        <input
          type="text"
          value={titleInput}
          onChange={(e) => setTitleInput(e.target.value)}
          placeholder={`Find a title in ${libraryName}`}
          className="kb-viewer-search"
          style={{ flex: "2 1 240px", minHeight: 44 }}
        />
      </div>

      {loading ? (
        <div className="flex items-center justify-center py-12">
          <Icon path={mdiLoading} size="24" className="animate-spin text-cyan-400" />
          <span className="ml-3 text-sm text-slate-400">Reading the index...</span>
        </div>
      ) : error ? (
        <div className="flex flex-wrap items-center gap-3 rounded-lg border border-amber-500/20 bg-amber-500/5 px-4 py-3">
          <Icon path={mdiAlertCircle} size="20" className="text-amber-400" />
          <span className="flex-1 text-sm text-slate-300">{error}</span>
          <button
            type="button"
            onClick={() => run(lastRef.current.q, lastRef.current.after, lastRef.current.pageIdx)}
            className="flex items-center gap-1 rounded-lg px-3 text-sm text-cyan-400 hover:bg-white/5"
            style={{ minHeight: 44 }}
          >
            <Icon path={mdiRefresh} size="16" />
            Retry
          </button>
        </div>
      ) : articles.length === 0 ? (
        <div className="py-8 text-center">
          <Icon path={mdiInboxFull} size="36" className="mx-auto mb-3 text-slate-600" />
          <p className="text-sm text-slate-400">
            {query.q || query.prefix ? "Nothing here by that name." : "No titles in this library."}
          </p>
        </div>
      ) : (
        <div>
          <div className="mb-3 text-xs text-slate-500">
            {typeof total === "number"
              ? `${articles.length} of ${total.toLocaleString()} articles`
              : `${articles.length} titles`}
            {pageIdx > 0 ? ` (page ${pageIdx + 1})` : ""}
          </div>
          <div className="space-y-2">
            {articles.map((article) => (
              <div
                key={`${article.index}-${article.url}`}
                className="kb-article-item"
                style={{ minHeight: 44, display: "flex", alignItems: "center" }}
                onClick={() => onOpen(article)}
              >
                <div className="flex w-full items-center justify-between">
                  <div className="flex items-center gap-3">
                    <Icon
                      path={isVideo ? mdiPlayCircle : mdiFileDocument}
                      size="16"
                      className={isVideo ? "text-pink-400" : "text-cyan-400"}
                    />
                    <span className="text-sm font-medium text-slate-200">{article.title}</span>
                  </div>
                  <Icon path={mdiChevronRight} size="16" className="text-slate-600" />
                </div>
              </div>
            ))}
          </div>

          <div className="mt-4 flex items-center justify-center gap-3">
            {pageIdx > 0 && (
              <button
                type="button"
                onClick={goPrev}
                className="rounded-lg px-4 text-sm text-cyan-400 hover:bg-white/5"
                style={{ minHeight: 44 }}
              >
                Previous
              </button>
            )}
            {hasMore && (
              <button
                type="button"
                onClick={goNext}
                className="rounded-lg px-4 text-sm text-cyan-400 hover:bg-white/5"
                style={{ minHeight: 44 }}
              >
                Next
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
