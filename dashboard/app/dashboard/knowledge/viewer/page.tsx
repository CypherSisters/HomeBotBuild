"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import { useSearchParams } from "next/navigation";
import {
  mdiLoading,
  mdiPlayCircle,
  mdiFormatListBulleted,
  mdiMagnify,
  mdiInformationOutline,
  mdiArrowLeft,
  mdiChevronRight,
  mdiFileDocument,
  mdiInboxFull,
  mdiBookOpenPageVariant,
} from "@mdi/js";
import Icon from "../../../_components/Icon";
import CardBox from "../../../_components/CardBox";
import {
  getLibraries,
  browseLibrary,
  getArticle,
} from "../../../_lib/services/knowledgeService";
import type { KnowledgeLibrary, LibraryArticle, ArticleContent } from "../../../_interfaces/api";
import { zoneBadge, chunkCount, formatSize } from "../libraryMeta";
import { ARTICLE_BOX_STYLE } from "../../../_lib/knowledgeBrowse";
import SourceView from "../SourceView";

/* ═══════════════════════════════════════════════════════════════
   STANDALONE LIBRARY VIEWER
   Opens a single library in its own browser tab so the user
   can browse/watch while continuing to use the main UI.
   ═══════════════════════════════════════════════════════════════ */

type ViewerTab = "browse" | "search" | "about";

export default function StandaloneViewer() {
  const params = useSearchParams();
  const collection = params.get("collection") || "";
  const name = params.get("name") || "";
  const categoryParam = params.get("category") || "";
  // Deep link: ?article=<zim url> auto-opens one article/video (used by the
  // voice "show me X" overlay, which lands straight on the talk).
  const articleParam = params.get("article") || "";
  // ?kind=video is a backend hint that this collection is a video shelf, so
  // the viewer renders the native <video> even when the library name/collection
  // don't follow the "ted*" convention (future video sources).
  const kindParam = params.get("kind") || "";

  const [library, setLibrary] = useState<KnowledgeLibrary | null>(null);
  const [loading, setLoading] = useState(true);

  // Fetch the library info
  useEffect(() => {
    (async () => {
      try {
        const libs = await getLibraries();
        const found = libs.find((l) => l.collection === collection);
        if (found) {
          setLibrary(found);
        } else {
          // Create a minimal library object from URL params
          setLibrary({
            name: name || collection,
            collection,
            zone: 2,
            zone_label: "The Brain",
            chunk_count: 0,
            size: "",
            added: "",
            library_id: "",
          } as KnowledgeLibrary);
        }
      } catch {
        setLibrary({
          name: name || collection,
          collection,
          zone: 2,
          zone_label: "The Brain",
          chunk_count: 0,
          size: "",
          added: "",
          library_id: "",
        } as KnowledgeLibrary);
      } finally {
        setLoading(false);
      }
    })();
  }, [collection, name]);

  if (loading) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-gray-950">
        <Icon path={mdiLoading} size="32" className="animate-spin text-cyan-400" />
      </div>
    );
  }

  if (!library) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-gray-950 text-white">
        Library not found.
      </div>
    );
  }

  // Video shelves: the backend's ?kind=video hint is authoritative; the name /
  // collection heuristics are the fallback. Real TED collections are named
  // ted_mul_<topic>_<date> (e.g. ted_mul_surveillance_2025-08), so a bare
  // "ted" prefix on the collection is what actually catches them — the old
  // startsWith("ted ") / includes("_ted_") predicates missed every one.
  const isVideo =
    kindParam === "video" ||
    library.name.toLowerCase().startsWith("ted") ||
    (library.collection || "").toLowerCase().startsWith("ted") ||
    (library.collection || "").toLowerCase().includes("_ted_");

  return (
    <div className="min-h-screen bg-gray-950 p-6">
      <div className="mx-auto max-w-5xl">
        {/* Header */}
        <div className="mb-6 flex items-center gap-3">
          <span className="text-cyan-400">
            <Icon path={mdiBookOpenPageVariant} size="28" />
          </span>
          <h1 className="text-2xl font-bold text-white">{library.name}</h1>
          {isVideo && (
            <span className="rounded-full bg-pink-500/15 px-3 py-1 text-xs font-medium text-pink-400 ring-1 ring-pink-500/25">
              Video
            </span>
          )}
        </div>

        {/* Viewer */}
        <ViewerContent
          library={library}
          isVideo={isVideo}
          initialArticle={articleParam}
        />
      </div>
    </div>
  );
}

function ViewerContent({
  library,
  isVideo,
  initialArticle,
}: {
  library: KnowledgeLibrary;
  isVideo: boolean;
  initialArticle?: string;
}) {
  const isUpload = library.is_upload === true;
  const [tab, setTab] = useState<ViewerTab>("browse");
  const [articles, setArticles] = useState<LibraryArticle[]>([]);
  const [articlesLoading, setArticlesLoading] = useState(true);
  const [articlesError, setArticlesError] = useState<string | null>(null);
  const [articleSearch, setArticleSearch] = useState("");
  const [selectedArticle, setSelectedArticle] = useState<ArticleContent | null>(null);
  const [articleLoading, setArticleLoading] = useState(false);
  const [page, setPage] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [totalArticles, setTotalArticles] = useState(0);
  const searchTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const fetchArticles = useCallback(
    async (p: number = 0, search?: string) => {
      setArticlesLoading(true);
      setArticlesError(null);
      try {
        const res = await browseLibrary(library.collection, p, 50, search);
        setArticles(res.articles);
        setHasMore(res.has_more);
        setTotalArticles(res.total);
        setPage(p);
      } catch (err: any) {
        setArticlesError(err.message || "Could not load articles.");
      } finally {
        setArticlesLoading(false);
      }
    },
    [library.collection]
  );

  useEffect(() => {
    if (isUpload) {
      setArticlesLoading(false);
      return;
    }
    fetchArticles(0);
  }, [fetchArticles, isUpload]);

  // Debounced search
  useEffect(() => {
    if (searchTimerRef.current) clearTimeout(searchTimerRef.current);
    if (tab !== "browse" || isUpload) return;
    searchTimerRef.current = setTimeout(() => {
      fetchArticles(0, articleSearch || undefined);
    }, 400);
    return () => {
      if (searchTimerRef.current) clearTimeout(searchTimerRef.current);
    };
  }, [articleSearch, tab, fetchArticles]);

  const openArticle = async (art: LibraryArticle) => {
    setArticleLoading(true);
    const content = await getArticle(library.collection, art.url);
    setArticleLoading(false);
    if (content) setSelectedArticle(content);
  };

  // Deep link: when ?article= is present, auto-open that one article/video
  // once (the voice "show me X" overlay lands straight on the talk).
  const deepLinkedRef = useRef(false);
  useEffect(() => {
    if (!initialArticle || deepLinkedRef.current || isUpload) return;
    deepLinkedRef.current = true;
    setArticleLoading(true);
    getArticle(library.collection, initialArticle, { fresh: true }).then((content) => {
      setArticleLoading(false);
      if (content) setSelectedArticle(content);
    });
  }, [initialArticle, isUpload, library.collection]);

  return (
    <div>
      {/* Tabs */}
      <div className="mb-4 flex gap-1 border-b border-white/5 pb-0">
        <button
          className={`kb-tab ${tab === "browse" ? "kb-tab-active" : ""}`}
          onClick={() => setTab("browse")}
        >
          <span className="flex items-center gap-2">
            <Icon
              path={isUpload ? mdiFileDocument : isVideo ? mdiPlayCircle : mdiFormatListBulleted}
              size="16"
            />
            {isUpload ? "Document" : isVideo ? "Watch" : "Browse"}
          </span>
        </button>
        <button
          className={`kb-tab ${tab === "about" ? "kb-tab-active" : ""}`}
          onClick={() => setTab("about")}
        >
          <span className="flex items-center gap-2">
            <Icon path={mdiInformationOutline} size="16" />
            About
          </span>
        </button>
      </div>

      {/* Browse Tab */}
      {tab === "browse" && isUpload && <SourceView library={library} />}
      {tab === "browse" && !isUpload && (
        <CardBox>
          <div className="mb-4">
            <input
              type="text"
              value={articleSearch}
              onChange={(e) => setArticleSearch(e.target.value)}
              placeholder={`Search within ${library.name}...`}
              className="kb-viewer-search"
            />
          </div>

          {selectedArticle ? (
            <div>
              <button
                onClick={() => setSelectedArticle(null)}
                className="mb-4 flex items-center gap-1 text-sm text-cyan-400 hover:text-cyan-300"
              >
                <Icon path={mdiArrowLeft} size="16" />
                Back to articles
              </button>
              <h3 className="mb-4 text-lg font-bold text-white">
                {selectedArticle.title}
              </h3>
              {isVideo && selectedArticle.html ? (() => {
                const videoMatch = selectedArticle.html.match(/src=["']([^"']*?\.webm)["']/i)
                  || selectedArticle.html.match(/src=["']([^"']*?\.mp4)["']/i);
                const posterMatch = selectedArticle.html.match(/(?:poster=["']|<img[^>]*src=["'])([^"']*?thumbnail[^"']*?)["']/i);
                const videoUrl = videoMatch?.[1] || null;
                const posterUrl = posterMatch?.[1] || null;
                const subtitleMatches = Array.from(selectedArticle.html.matchAll(/src=["']([^"']*?subs_(\w+)\.vtt[^"']*?)["']/gi));
                const descMatch = selectedArticle.html.match(/<p[^>]*>([\s\S]*?)<\/p>/i);
                const descText = (descMatch?.[1] || '').replace(/<[^>]*>/g, '').trim();

                return (
                  <div>
                    {videoUrl ? (
                      <div className="mb-6 overflow-hidden rounded-lg bg-black">
                        <video controls autoPlay preload="metadata" poster={posterUrl || undefined} className="w-full" style={{ maxHeight: '500px' }} crossOrigin="anonymous">
                          <source src={videoUrl} type="video/webm" />
                          {subtitleMatches.map(([, url, lang]) => (
                            <track key={lang} kind="subtitles" src={url} srcLang={lang} label={lang.toUpperCase()} />
                          ))}
                        </video>
                      </div>
                    ) : (
                      <div className="mb-6 rounded-lg bg-slate-800/50 p-6 text-center text-slate-400">
                        <Icon path={mdiPlayCircle} size="48" className="mx-auto mb-2 text-pink-400" />
                        <p>Video content available.</p>
                      </div>
                    )}
                    {descText && <p className="text-sm leading-relaxed text-slate-300">{descText}</p>}
                  </div>
                );
              })() : (
                <div className="kb-article-content" style={ARTICLE_BOX_STYLE} dangerouslySetInnerHTML={{ __html: selectedArticle.html }} />
              )}
            </div>
          ) : articlesLoading ? (
            <div className="flex items-center justify-center py-12">
              <Icon path={mdiLoading} size="24" className="animate-spin text-cyan-400" />
              <span className="ml-3 text-sm text-slate-400">Loading articles...</span>
            </div>
          ) : articlesError ? (
            <div className="py-8 text-center">
              <p className="text-sm text-slate-300">Could not load articles for this library.</p>
            </div>
          ) : articles.length === 0 ? (
            <div className="py-8 text-center">
              <Icon path={mdiInboxFull} size="36" className="mx-auto mb-3 text-slate-600" />
              <p className="text-sm text-slate-400">No articles found.</p>
            </div>
          ) : (
            <div>
              <p className="mb-3 text-xs text-slate-500">
                Showing {articles.length} of {totalArticles.toLocaleString()} articles
              </p>
              <div className="divide-y divide-white/5">
                {articles.map((art) => (
                  <button
                    key={art.url}
                    onClick={() => openArticle(art)}
                    disabled={articleLoading}
                    className="kb-article-item"
                  >
                    <span className="flex items-center gap-3">
                      <Icon
                        path={isVideo ? mdiPlayCircle : mdiFileDocument}
                        size="18"
                        className={isVideo ? "text-pink-400" : "text-cyan-400"}
                      />
                      <span className="text-left text-sm text-slate-200">{art.title}</span>
                    </span>
                    <Icon path={mdiChevronRight} size="16" className="text-slate-600" />
                  </button>
                ))}
              </div>
              {(hasMore || page > 0) && (
                <div className="mt-4 flex justify-center gap-3">
                  {page > 0 && (
                    <button onClick={() => fetchArticles(page - 1, articleSearch || undefined)} className="rounded bg-white/5 px-4 py-2 text-xs text-cyan-400 hover:bg-white/10">
                      Previous
                    </button>
                  )}
                  {hasMore && (
                    <button onClick={() => fetchArticles(page + 1, articleSearch || undefined)} className="rounded bg-white/5 px-4 py-2 text-xs text-cyan-400 hover:bg-white/10">
                      Next
                    </button>
                  )}
                </div>
              )}
            </div>
          )}
        </CardBox>
      )}

      {/* About Tab */}
      {tab === "about" && (
        <CardBox>
          <h3 className="mb-3 text-sm font-semibold text-white">Library Details</h3>
          <dl className="grid grid-cols-2 gap-3 text-sm">
            <dt className="text-slate-500">Collection</dt>
            <dd className="text-slate-300 font-mono text-xs">{library.collection}</dd>
            <dt className="text-slate-500">Zone</dt>
            <dd className="text-slate-300">{zoneBadge(library.zone).long}</dd>
            {chunkCount(library) > 0 && (
              <>
                <dt className="text-slate-500">Indexed Chunks</dt>
                <dd className="text-slate-300">{chunkCount(library).toLocaleString()}</dd>
              </>
            )}
            {formatSize(library.size) && (
              <>
                <dt className="text-slate-500">Size</dt>
                <dd className="text-slate-300">{formatSize(library.size)}</dd>
              </>
            )}
            {library.fts_ready && (
              <>
                <dt className="text-slate-500">Full-Text Index</dt>
                <dd className="text-slate-300">Built</dd>
              </>
            )}
            <dt className="text-slate-500">Total Articles</dt>
            <dd className="text-slate-300">{totalArticles.toLocaleString()}</dd>
          </dl>
        </CardBox>
      )}
    </div>
  );
}
