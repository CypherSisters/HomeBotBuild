"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import {
  mdiBookOpenPageVariant,
  mdiUpload,
  mdiFileDocument,
  mdiDatabase,
  mdiMagnify,
  mdiDelete,
  mdiCloudUpload,
  mdiRefresh,
  mdiAlertCircle,
  mdiInboxFull,
  mdiClose,
  mdiCheckCircle,
  mdiLoading,
  mdiEarth,
  mdiPlayCircle,
  mdiForumOutline,
  mdiMedicalBag,
  mdiInformationOutline,
  mdiShieldCheckOutline,
  mdiFlask,
  mdiAirplane,
  mdiBookMultipleOutline,
  mdiArrowLeft,
  mdiChevronRight,
  mdiFormatListBulleted,
  mdiOpenInNew,
  mdiTextSearch,
} from "@mdi/js";
import SectionMain from "../../_components/Section/Main";
import SectionTitleLineWithButton from "../../_components/Section/TitleLineWithButton";
import CardBox from "../../_components/CardBox";
import CardBoxWidget from "../../_components/CardBox/Widget";
import Icon from "../../_components/Icon";
import Button from "../../_components/Button";
import PairDeviceCard from "../../_components/PairDeviceCard";
import {
  getLibraries,
  searchKnowledge,
  searchTitles,
  uploadDocument,
  deleteLibrary,
  getArticle,
  getMainPage,
  searchFulltext,
} from "../../_lib/services/knowledgeService";
import {
  historyInit,
  historyPush,
  historyBack,
  historyForward,
  canGoBack,
  canGoForward,
  currentEntry,
  ARTICLE_BOX_STYLE,
  articleFragmentId,
  isNavigableArticleHref,
  linkTargetFromClick,
  shouldPreferBrowse,
  titleFromZimUrl,
  type BrowseEntry,
} from "../../_lib/knowledgeBrowse";
import BrowseToolbar from "./_components/BrowseToolbar";
import ArticleIndex from "./_components/ArticleIndex";
import type { TitleSearchResult, FulltextResult } from "../../_lib/services/knowledgeService";
import type { KnowledgeLibrary, LibraryArticle, ArticleContent } from "../../_interfaces/api";
import SourceView from "./SourceView";
import { zoneBadge, chunkCount, formatChunks, formatSize } from "./libraryMeta";
import { containerMaxW } from "../../_lib/config";
// "My Documents" tab — the user's real /knowledge uploads (canonical
// pipeline). The legacy app/dashboard/documents/page.tsx (/ingest backend)
// is left intact but no longer surfaced here.
import MyDocuments from "./MyDocuments";
import BuildSources from "./BuildSources";

/* ═══════════════════════════════════════════════════════════════
   TYPES & CONSTANTS
   ═══════════════════════════════════════════════════════════════ */

type SearchResult = {
  text: string;
  score: number;
  source: string;
  zone: 1 | 2;
  library?: string;
};

type ViewMode = "shelves" | "viewer";
type ViewerTab = "browse" | "search" | "about";

type CategoryKey =
  | "encyclopedia"
  | "video"
  | "qa"
  | "medical"
  | "reference"
  | "survival"
  | "science"
  | "travel"
  | "books"
  | "uploads";

const CATEGORIES: Record<
  CategoryKey,
  { label: string; color: string; icon: string; description: string }
> = {
  encyclopedia: {
    label: "Encyclopedias",
    color: "#00f0ff",
    icon: mdiEarth,
    description: "Wikipedia, Wikisource, and other encyclopedic references",
  },
  video: {
    label: "Video Library",
    color: "#ff2d6f",
    icon: mdiPlayCircle,
    description: "TED Talks and video content — watch and listen offline",
  },
  qa: {
    label: "Q&A Forums",
    color: "#39ff14",
    icon: mdiForumOutline,
    description: "Stack Exchange communities and expert answers",
  },
  medical: {
    label: "Medical",
    color: "#ff6b6b",
    icon: mdiMedicalBag,
    description: "Medical encyclopedias, treatment guides, and health references",
  },
  reference: {
    label: "Reference",
    color: "#fbbf24",
    icon: mdiInformationOutline,
    description: "Emergency preparedness, maps, and technical documentation",
  },
  survival: {
    label: "Survival & Preparedness",
    color: "#f97316",
    icon: mdiShieldCheckOutline,
    description: "Prepper guides, water treatment, and self-sufficiency resources",
  },
  science: {
    label: "Science & Education",
    color: "#a78bfa",
    icon: mdiFlask,
    description: "Physics, biology, history, and academic courses",
  },
  travel: {
    label: "Travel",
    color: "#2dd4bf",
    icon: mdiAirplane,
    description: "Travel guides, maps, and destination information",
  },
  books: {
    label: "Books & Collections",
    color: "#c084fc",
    icon: mdiBookMultipleOutline,
    description: "Interactive books, specialty collections, and unique resources",
  },
  uploads: {
    label: "Your Documents",
    color: "#94a3b8",
    icon: mdiFileDocument,
    description: "Files you've uploaded and indexed into the knowledge base",
  },
};

/* ═══════════════════════════════════════════════════════════════
   HELPERS
   ═══════════════════════════════════════════════════════════════ */

function categorizeLibrary(lib: KnowledgeLibrary): CategoryKey {
  const n = lib.name.toLowerCase();
  const c = (lib.collection || "").toLowerCase();

  // TED talks → video
  if (n.startsWith("ted ") || c.includes("_ted_") || c.startsWith("ted_")) return "video";

  // Medical (must come BEFORE qa & science to catch "Medical Sciences Q&A" and "Medicine LibreTexts")
  if (
    n.includes("wikem") ||
    n.includes("mdwiki") ||
    n.includes("medical") ||
    n.includes("medicine") ||
    c.includes("_medicine_") ||
    c.includes("_med_") ||
    c.includes("medicalsciences")
  )
    return "medical";

  // Stack Exchange / Q&A
  if (n.includes("q&a") || c.includes("stackexchange")) return "qa";

  // Survival / Prepper
  if (
    n.includes("prepper") ||
    n.includes("water treatment") ||
    n.includes("ready.gov")
  )
    return "survival";

  // Science & Education
  if (
    n.includes("physics") ||
    n.includes("biology") ||
    n.includes("history") ||
    n.includes("courses") ||
    c.includes("libretexts")
  )
    return "science";

  // Travel
  if (n.includes("wikivoyage")) return "travel";

  // Books & special
  if (
    n.includes("prunelle") ||
    n.includes("knots") ||
    n.includes("incognito")
  )
    return "books";

  // User uploads (has uploaded_at or zone field in raw data)
  if (lib.added && !c.startsWith("kiwix_")) return "uploads";

  // Reference (OpenStreetMap, MediaWiki, XKCD)
  if (
    n.includes("openstreetmap") ||
    n.includes("mediawiki") ||
    n.includes("xkcd") ||
    n.includes("explain")
  )
    return "reference";

  // Encyclopedias — Wikipedia, RationalWiki, Wikispecies, Wikisource, WikiSummaries
  if (
    n.includes("wikipedia") ||
    n.includes("rationalwiki") ||
    n.includes("wikispecies") ||
    n.includes("wikisource") ||
    n.includes("wikisummaries") ||
    n.includes("wiki")
  )
    return "encyclopedia";

  return "reference";
}

/** Find the shelf entry a search result belongs to. Full-text results key on
 *  library_id; title results key on collection. ZIM rows carry both. */
function findLibrary(
  libraries: KnowledgeLibrary[],
  id: string,
): KnowledgeLibrary | undefined {
  if (!id) return undefined;
  return libraries.find((l) => l.library_id === id || l.collection === id);
}

/* ═══════════════════════════════════════════════════════════════
   SUB-COMPONENTS
   ═══════════════════════════════════════════════════════════════ */

function BookCard({
  lib,
  category,
  onClick,
}: {
  lib: KnowledgeLibrary;
  category: CategoryKey;
  onClick: () => void;
}) {
  const cat = CATEGORIES[category];
  const zone = zoneBadge(lib.zone);
  const chunks = chunkCount(lib);
  const size = formatSize(lib.size);
  return (
    <div
      className="kb-book"
      style={{ "--book-color": cat.color } as React.CSSProperties}
      onClick={onClick}
      title={`Open ${lib.name}${lib.fts_ready ? " — full-text searchable" : ""}`}
    >
      {lib.fts_ready && (
        <span className="kb-book-fts" title="Full-text searchable">
          <Icon path={mdiTextSearch} size="12" />
        </span>
      )}
      <div className="kb-book-inner">
        <div className="kb-book-icon">
          <Icon path={cat.icon} size="28" />
        </div>
        <div className="kb-book-title">{lib.name}</div>
        <div className="kb-book-meta">
          <span className={`kb-zone-pill ${zone.cls}`}>{zone.short}</span>
          {chunks > 0 ? (
            <span className="kb-book-chunks">{formatChunks(chunks)} chunks</span>
          ) : size ? (
            <span className="kb-book-chunks">{size}</span>
          ) : null}
        </div>
      </div>
    </div>
  );
}

function ShelfRow({
  category,
  libraries,
  onBookClick,
}: {
  category: CategoryKey;
  libraries: KnowledgeLibrary[];
  onBookClick: (lib: KnowledgeLibrary) => void;
}) {
  const cat = CATEGORIES[category];
  if (libraries.length === 0) return null;

  return (
    <div className="kb-shelf">
      <div
        className="kb-shelf-label"
        style={{ "--shelf-color": cat.color, color: cat.color } as React.CSSProperties}
      >
        <span className="kb-shelf-label-dot" />
        <Icon path={cat.icon} size="18" />
        {cat.label}
        <span style={{ color: "#475569", fontWeight: 400, fontSize: "0.75rem", letterSpacing: 0 }}>
          — {libraries.length} {libraries.length === 1 ? "volume" : "volumes"}
        </span>
      </div>
      <div className="kb-books-row">
        {libraries.map((lib) => (
          <BookCard
            key={lib.collection || lib.name}
            lib={lib}
            category={category}
            onClick={() => onBookClick(lib)}
          />
        ))}
      </div>
    </div>
  );
}

/* ═══════════════════════════════════════════════════════════════
   LIBRARY VIEWER COMPONENT
   ═══════════════════════════════════════════════════════════════ */

function LibraryViewer({
  library,
  category,
  onClose,
  initialArticleUrl,
}: {
  library: KnowledgeLibrary;
  category: CategoryKey;
  onClose: () => void;
  /** Article to open straight away — set when arriving from a search result. */
  initialArticleUrl?: string | null;
}) {
  const cat = CATEGORIES[category];
  const isVideo = category === "video";
  const isUpload = category === "uploads" || library.is_upload === true;
  const zone = zoneBadge(library.zone);
  const chunks = chunkCount(library);
  const sizeLabel = formatSize(library.size);

  const [tab, setTab] = useState<ViewerTab>("browse");
  const [selectedArticle, setSelectedArticle] = useState<ArticleContent | null>(null);
  const [articleLoading, setArticleLoading] = useState(false);
  /** The pack's own front page, kept so Home is instant and offline-cheap. */
  const [mainArticle, setMainArticle] = useState<ArticleContent | null>(null);
  const [openingMain, setOpeningMain] = useState(!isUpload && !isVideo && !initialArticleUrl);
  /** Title index visible instead of an article. */
  const [showIndex, setShowIndex] = useState(isVideo);
  const [history, setHistory] = useState(historyInit());
  /** Anchor to land on once the next article has rendered. */
  const [pendingFragment, setPendingFragment] = useState<string | null>(null);
  const [linkNotice, setLinkNotice] = useState<string | null>(null);
  const articleRef = useRef<HTMLDivElement | null>(null);

  /** Scroll the article body to an anchor inside it, or to its top. The
   *  container, never the document: in the show-me overlay this page is an
   *  iframe and window scrolling would move the wrong thing. */
  const scrollToFragment = useCallback((frag: string | null) => {
    const root = articleRef.current;
    if (!root) return;
    if (frag) {
      try {
        const id = articleFragmentId(frag);
        const esc = typeof CSS !== "undefined" && CSS.escape ? CSS.escape(id) : id;
        const el = root.querySelector(`#${esc}`) || root.querySelector(`[name="${esc}"]`);
        if (el) {
          (el as HTMLElement).scrollIntoView({ block: "start" });
          return;
        }
      } catch {
        /* a fragment that is not a valid selector just means "top". */
      }
    }
    root.scrollIntoView({ block: "start" });
  }, []);

  /** Fetch one article and put it on screen. `push` is false for Back/Forward,
   *  which are moving inside the stack rather than adding to it. */
  const loadEntry = useCallback(
    async (entry: BrowseEntry, push: boolean) => {
      setArticleLoading(true);
      setLinkNotice(null);
      try {
        const content =
          mainArticle && entry.url === mainArticle.url
            ? mainArticle
            : await getArticle(library.collection, entry.url);
        if (!content) {
          setLinkNotice("That page isn't in this library.");
          return;
        }
        setSelectedArticle(content);
        setShowIndex(false);
        setPendingFragment(entry.fragment || null);
        if (push) setHistory((h) => historyPush(h, { ...entry, title: content.title || entry.title }));
      } finally {
        setArticleLoading(false);
      }
    },
    [library.collection, mainArticle],
  );

  // Load article content by url — shared by the title index, the in-library
  // search tab, and deep links arriving from the global search results.
  const openArticleUrl = useCallback(
    async (url: string, fragment?: string | null) => {
      if (!url) return;
      setTab("browse");
      await loadEntry({ url, title: titleFromZimUrl(url), fragment: fragment || null }, true);
    },
    [loadEntry],
  );

  const openArticle = (article: LibraryArticle) =>
    loadEntry({ url: article.url, title: article.title }, true);

  // Opening a library opens the library: a ZIM pack has a real front page, so
  // ask for it first and show it. No main page (or a video pack, which has
  // none) falls through to the title index.
  useEffect(() => {
    if (isUpload || isVideo) return;
    let live = true;
    (async () => {
      const main = await getMainPage(library.collection);
      if (!live) return;
      // Fetched even when arriving on a deep-linked article, so Home works
      // from a search result too. It is only SHOWN when nothing else was
      // asked for.
      if (main) setMainArticle(main);
      if (!initialArticleUrl) {
        // A main page that is really a JavaScript app (Project Gutenberg's
        // C/Home) renders near-empty here, so it is not a front door. The
        // title index is. Home stays in the toolbar regardless.
        if (main && !shouldPreferBrowse(main.html)) {
          setSelectedArticle(main);
          setHistory(historyPush(historyInit(), { url: main.url, title: main.title }));
        } else {
          setShowIndex(true);
        }
      }
      setOpeningMain(false);
    })();
    return () => {
      live = false;
    };
  }, [library.collection, isUpload, isVideo, initialArticleUrl]);

  // Deep link from a search result. Opened ONCE per url: openArticleUrl's
  // identity changes when the main page lands, and without this guard that
  // would yank the reader back to the deep-linked article mid-browse.
  const deepLinkedRef = useRef<string | null>(null);
  useEffect(() => {
    if (!initialArticleUrl || deepLinkedRef.current === initialArticleUrl) return;
    deepLinkedRef.current = initialArticleUrl;
    openArticleUrl(initialArticleUrl);
  }, [initialArticleUrl, openArticleUrl]);

  // Land on the requested anchor once the new HTML is in the DOM.
  useEffect(() => {
    if (!selectedArticle) return;
    scrollToFragment(pendingFragment);
    if (pendingFragment) setPendingFragment(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedArticle]);

  /** One delegated handler for every link in the article body: the HTML is
   *  injected, so there is nothing to attach listeners to individually. */
  const onArticleClick = useCallback(
    (e: React.MouseEvent<HTMLDivElement>) => {
      const node = e.target as unknown as Parameters<typeof linkTargetFromClick>[0];
      const target = linkTargetFromClick(node);
      if (!target) {
        // The inert href="#" the backend puts on internal links would
        // otherwise jump the page to the top.
        // An href the sanitiser should already have removed (javascript:,
        // data:, ...) is refused here too, as a backstop.
        const a = (e.target as HTMLElement)?.closest?.("a");
        const href = a ? a.getAttribute("href") || "" : "";
        if (a && (href === "#" || !isNavigableArticleHref(href))) e.preventDefault();
        return;
      }
      if (target.kind === "external") return; // ExternalLinkGuard owns these.
      e.preventDefault();
      if (target.kind === "anchor") {
        scrollToFragment(target.fragment);
        return;
      }
      loadEntry(
        { url: target.url, title: titleFromZimUrl(target.url), fragment: target.fragment },
        true,
      );
    },
    [loadEntry, scrollToFragment],
  );

  const goBack = () => {
    const next = historyBack(history);
    if (next === history) return;
    setHistory(next);
    const entry = currentEntry(next);
    if (entry) loadEntry(entry, false);
  };

  const goForward = () => {
    const next = historyForward(history);
    if (next === history) return;
    setHistory(next);
    const entry = currentEntry(next);
    if (entry) loadEntry(entry, false);
  };

  const goHome = () => {
    if (!mainArticle) return;
    setSelectedArticle(mainArticle);
    setShowIndex(false);
    setLinkNotice(null);
    setPendingFragment(null);
    setHistory((h) => historyPush(h, { url: mainArticle.url, title: mainArticle.title }));
  };

  const currentTitle = showIndex
    ? library.name
    : selectedArticle?.title || currentEntry(history)?.title || library.name;

  return (
    <div className="kb-viewer">
      {/* Header */}
      <div className="mb-6 flex items-center gap-4">
        <button
          onClick={onClose}
          className="flex items-center gap-1 rounded-lg px-3 py-2 text-sm text-slate-400 transition hover:bg-white/5 hover:text-white"
        >
          <Icon path={mdiArrowLeft} size="18" />
          Back to Library
        </button>
      </div>

      <CardBox className="mb-6">
        <div className="flex items-start gap-4">
          <div
            className="flex h-14 w-14 flex-shrink-0 items-center justify-center rounded-xl"
            style={{
              background: `${cat.color}15`,
              border: `1px solid ${cat.color}30`,
            }}
          >
            <span style={{ color: cat.color }}>
              <Icon path={cat.icon} size="28" className="drop-shadow-lg" />
            </span>
          </div>
          <div className="flex-1">
            <div className="flex items-start justify-between">
              <h2 className="text-xl font-bold text-white">{library.name}</h2>
              <button
                onClick={() => {
                  const url = `/dashboard/knowledge/viewer?collection=${encodeURIComponent(library.collection)}&name=${encodeURIComponent(library.name)}&category=${encodeURIComponent(category)}`;
                  window.open(url, '_blank', 'noopener');
                }}
                className="ml-3 flex flex-shrink-0 items-center gap-1 rounded-lg bg-white/5 px-3 py-1.5 text-xs text-slate-400 transition hover:bg-white/10 hover:text-white"
                title="Open in new window"
              >
                <Icon path={mdiOpenInNew} size="14" />
                New Window
              </button>
            </div>
            <p className="mt-1 text-sm text-slate-400">
              {cat.description}
            </p>
            <div className="mt-3 flex flex-wrap gap-3">
              <span className={`kb-zone-pill ${zone.cls}`}>{zone.long}</span>
              {chunks > 0 && (
                <span className="kb-zone-pill" style={{ background: "var(--card2)", color: "var(--text2)", border: "1px solid var(--line)" }}>
                  {chunks.toLocaleString()} chunks indexed
                </span>
              )}
              {library.fts_ready && (
                <span className="kb-zone-pill kb-zone-pill-fts">
                  Full-text searchable
                </span>
              )}
              {isVideo && (
                <span className="kb-zone-pill" style={{ background: "rgba(255,45,111,0.15)", color: "#ff6b8a", border: "1px solid rgba(255,45,111,0.25)" }}>
                  Video Content
                </span>
              )}
            </div>
          </div>
        </div>
      </CardBox>

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
          className={`kb-tab ${tab === "search" ? "kb-tab-active" : ""}`}
          onClick={() => setTab("search")}
        >
          <span className="flex items-center gap-2">
            <Icon path={mdiMagnify} size="16" />
            Search
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

      {/* Tab Content */}
      {tab === "browse" && isUpload && <SourceView library={library} />}
      {tab === "browse" && !isUpload && (
        <CardBox>
          <BrowseToolbar
            title={currentTitle}
            hasHome={!!mainArticle}
            canBack={canGoBack(history)}
            canForward={canGoForward(history)}
            browseOpen={showIndex}
            onHome={goHome}
            onBack={goBack}
            onForward={goForward}
            onToggleBrowse={() => setShowIndex((v) => !v || !selectedArticle)}
          />

          {linkNotice && (
            <div className="mb-3 flex items-center gap-2 rounded-lg border border-amber-500/20 bg-amber-500/5 px-3 py-2">
              <Icon path={mdiAlertCircle} size="16" className="text-amber-400" />
              <span className="flex-1 text-xs text-slate-300">{linkNotice}</span>
              <button
                onClick={() => setLinkNotice(null)}
                className="text-slate-500 hover:text-slate-300"
                style={{ minHeight: 44, minWidth: 44 }}
                title="Dismiss"
              >
                <Icon path={mdiClose} size="16" />
              </button>
            </div>
          )}

          {showIndex ? (
            <ArticleIndex
              collection={library.collection}
              libraryName={library.name}
              isVideo={isVideo}
              onOpen={openArticle}
            />
          ) : openingMain ? (
            <div className="flex items-center justify-center py-12">
              <Icon path={mdiLoading} size="24" className="animate-spin text-cyan-400" />
              <span className="ml-3 text-sm text-slate-400">Opening {library.name}...</span>
            </div>
          ) : selectedArticle ? (
            // Article view. One click handler for the whole body: the HTML is
            // injected, so internal links can only be caught by delegation.
            <div ref={articleRef} onClick={onArticleClick}>
              <h3 className="mb-4 text-lg font-bold text-white">
                {selectedArticle.title}
              </h3>
              {/* For video libraries, render a native video player + description */}
              {isVideo && selectedArticle.html ? (() => {
                // Extract video source URL from the HTML (proxy or direct backend paths)
                const videoMatch = selectedArticle.html.match(/src=["']([^"']*?\.webm)["']/i)
                  || selectedArticle.html.match(/src=["']([^"']*?\.mp4)["']/i)
                  || selectedArticle.html.match(/src=["']([^"']*?\.ogv)["']/i);
                const posterMatch = selectedArticle.html.match(/(?:poster=["']|<img[^>]*src=["'])([^"']*?thumbnail[^"']*?)["']/i)
                  || selectedArticle.html.match(/poster=["']([^"']*?)["']/i);
                const videoUrl = videoMatch?.[1] || null;
                const posterUrl = posterMatch?.[1] || null;

                // Extract subtitle tracks
                const subtitleMatches = Array.from(selectedArticle.html.matchAll(/src=["']([^"']*?subs_(\w+)\.vtt[^"']*?)["']/gi));

                // Extract description text (strip HTML tags)
                const descMatch = selectedArticle.html.match(/<div[^>]*class="[^"]*talk-description[^"]*"[^>]*>([\s\S]*?)<\/div>/i)
                  || selectedArticle.html.match(/<p[^>]*>([\s\S]*?)<\/p>/i);
                const descHtml = descMatch?.[1] || '';
                const descText = descHtml.replace(/<[^>]*>/g, '').trim();

                return (
                  <div>
                    {videoUrl ? (
                      <div className="mb-6 overflow-hidden rounded-lg bg-black">
                        <video
                          controls
                          preload="metadata"
                          poster={posterUrl || undefined}
                          className="w-full"
                          style={{ maxHeight: '500px' }}
                          crossOrigin="anonymous"
                        >
                          <source src={videoUrl} type="video/webm" />
                          {subtitleMatches.map(([, url, lang]) => (
                            <track
                              key={lang}
                              kind="subtitles"
                              src={url}
                              srcLang={lang}
                              label={lang.toUpperCase()}
                            />
                          ))}
                          Your browser does not support the video tag.
                        </video>
                      </div>
                    ) : (
                      <div className="mb-6 rounded-lg bg-slate-800/50 p-6 text-center text-slate-400">
                        <Icon path={mdiPlayCircle} size="48" className="mx-auto mb-2 text-pink-400" />
                        <p>Video content available — playback requires a compatible decoder.</p>
                      </div>
                    )}
                    {descText && (
                      <p className="text-sm leading-relaxed text-slate-300">{descText}</p>
                    )}
                  </div>
                );
              })() : (
                <div
                  className="kb-article-content"
                  style={ARTICLE_BOX_STYLE}
                  dangerouslySetInnerHTML={{ __html: selectedArticle.html }}
                />
              )}
            </div>
          ) : (
            <div className="py-8 text-center">
              <Icon path={mdiInboxFull} size="36" className="mx-auto mb-3 text-slate-600" />
              <p className="text-sm text-slate-400">Nothing open. Browse A-Z to pick a page.</p>
            </div>
          )}

          {articleLoading && (
            <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
              <div className="flex items-center gap-3 rounded-xl bg-slate-900 px-6 py-4">
                <Icon path={mdiLoading} size="20" className="animate-spin text-cyan-400" />
                <span className="text-sm text-white">Loading article...</span>
              </div>
            </div>
          )}
        </CardBox>
      )}

      {tab === "search" && (
        <LibrarySearchTab library={library} onOpenArticle={openArticleUrl} />
      )}

      {tab === "about" && (
        <CardBox>
          <div className="space-y-4">
            <div className="grid grid-cols-2 gap-4">
              <div>
                <div className="text-xs font-medium uppercase tracking-wider text-slate-500">
                  Library Name
                </div>
                <div className="mt-1 text-sm text-slate-200">{library.name}</div>
              </div>
              <div>
                <div className="text-xs font-medium uppercase tracking-wider text-slate-500">
                  Collection ID
                </div>
                <div className="mt-1 font-mono text-sm text-slate-400">
                  {library.collection}
                </div>
              </div>
              <div>
                <div className="text-xs font-medium uppercase tracking-wider text-slate-500">
                  Zone
                </div>
                <div className="mt-1 text-sm text-slate-200">{zone.long}</div>
              </div>
              <div>
                <div className="text-xs font-medium uppercase tracking-wider text-slate-500">
                  Full-Text Index
                </div>
                <div className="mt-1 text-sm text-slate-200">
                  {library.fts_ready
                    ? "Built — body search available"
                    : chunks > 0
                      ? `Not built — ${chunks.toLocaleString()} chunks indexed for RAG`
                      : "Not built — title browsing only"}
                </div>
              </div>
              {sizeLabel && (
                <div>
                  <div className="text-xs font-medium uppercase tracking-wider text-slate-500">
                    Size
                  </div>
                  <div className="mt-1 text-sm text-slate-200">{sizeLabel}</div>
                </div>
              )}
              <div>
                <div className="text-xs font-medium uppercase tracking-wider text-slate-500">
                  Category
                </div>
                <div className="mt-1 flex items-center gap-2 text-sm" style={{ color: cat.color }}>
                  <Icon path={cat.icon} size="16" />
                  {cat.label}
                </div>
              </div>
            </div>

            <div className="rounded-lg border border-white/5 bg-white/[0.02] p-4">
              <div className="mb-2 text-xs font-medium uppercase tracking-wider text-slate-500">
                LLM Access
              </div>
              <p className="text-sm text-slate-300">
                PuffPAI has full access to this library through its RAG system. Ask any question and
                it can retrieve relevant information, cite sources, and
                {isVideo
                  ? " offer to play related TED Talk videos."
                  : " present articles from this collection."}
              </p>
            </div>
          </div>
        </CardBox>
      )}
    </div>
  );
}

/* Search tab within the library viewer. Uses the library's own full-text
   shard when one exists, and falls back to the cross-library RAG search
   (filtered down to this library) when it does not. */
function LibrarySearchTab({
  library,
  onOpenArticle,
}: {
  library: KnowledgeLibrary;
  onOpenArticle: (url: string) => void;
}) {
  const ftsId = library.library_id || library.collection;
  const useFts = library.fts_ready === true && !!ftsId;

  const [query, setQuery] = useState("");
  const [ftsResults, setFtsResults] = useState<FulltextResult[]>([]);
  const [results, setResults] = useState<any[]>([]);
  const [searching, setSearching] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (timerRef.current) clearTimeout(timerRef.current);
    if (!query.trim()) {
      setFtsResults([]);
      setResults([]);
      setSearching(false);
      return;
    }
    setSearching(true);
    timerRef.current = setTimeout(async () => {
      try {
        if (useFts) {
          const res = await searchFulltext(query, { maxResults: 30, library: ftsId });
          setFtsResults(res.results);
          setResults([]);
        } else {
          const res = await searchKnowledge(query);
          // Filter results to this library
          const filtered = res.filter(
            (r: any) =>
              r.library?.toLowerCase() === library.name.toLowerCase() ||
              r.source?.toLowerCase() === library.name.toLowerCase()
          );
          setResults(filtered.length > 0 ? filtered : res.slice(0, 5));
          setFtsResults([]);
        }
      } catch {
        setFtsResults([]);
        setResults([]);
      } finally {
        setSearching(false);
      }
    }, 400);
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, [query, library.name, useFts, ftsId]);

  const empty = !searching && query.trim() && ftsResults.length === 0 && results.length === 0;

  return (
    <CardBox>
      <div className="mb-4 flex items-center gap-3">
        <Icon path={useFts ? mdiTextSearch : mdiMagnify} size="20" className="text-slate-500" />
        <input
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder={
            useFts
              ? `Full-text search inside ${library.name}...`
              : `Search knowledge from ${library.name}...`
          }
          className="kb-viewer-search"
        />
        {searching && (
          <Icon path={mdiLoading} size="18" className="animate-spin text-cyan-400" />
        )}
      </div>

      {!query.trim() && (
        <div className="py-8 text-center">
          <Icon path={mdiMagnify} size="36" className="mx-auto mb-3 text-slate-600" />
          <p className="text-sm text-slate-400">
            {useFts
              ? "Search every word of every article in this pack."
              : "Search for any topic. PuffPAI's RAG system will find relevant content."}
          </p>
        </div>
      )}

      {empty && (
        <div className="py-8 text-center text-sm text-slate-500">
          No matches in this library.
        </div>
      )}

      {ftsResults.length > 0 && (
        <div className="kb-hits">
          {ftsResults.map((r) => (
            <button
              key={r.path}
              onClick={() => onOpenArticle(r.path)}
              className="kb-hit"
            >
              <span className="kb-hit-title">{r.title}</span>
              <span
                className="kb-hit-snippet"
                dangerouslySetInnerHTML={{ __html: r.snippet }}
              />
            </button>
          ))}
        </div>
      )}

      {results.length > 0 && (
        <div className="space-y-3">
          {results.map((result, idx) => (
            <div
              key={idx}
              className="rounded-lg border border-white/5 bg-white/[0.02] p-4"
            >
              <div className="mb-2 flex items-center gap-2">
                <span className="text-xs font-medium text-cyan-400">
                  {result.library || result.source || "Knowledge Base"}
                </span>
              </div>
              <p className="text-sm leading-relaxed text-slate-300">{result.text}</p>
            </div>
          ))}
        </div>
      )}
    </CardBox>
  );
}

/* ═══════════════════════════════════════════════════════════════
   ABSORPTION TABS — design language pill row (Library | My Documents)
   ═══════════════════════════════════════════════════════════════ */

function AbsorbTabs<T extends string>({
  tabs,
  active,
  onSelect,
}: {
  tabs: { key: T; label: string }[];
  active: T;
  onSelect: (key: T) => void;
}) {
  return (
    <div className="mb-6 flex flex-wrap items-center gap-3">
      {tabs.map((t) => {
        const isActive = t.key === active;
        return (
          <button
            key={t.key}
            type="button"
            onClick={() => onSelect(t.key)}
            className="rounded-full px-6 py-3 transition hover:scale-[1.02]"
            style={
              isActive
                ? {
                    background:
                      "linear-gradient(140deg, var(--pink), var(--magenta))",
                    color: "#fff",
                    font: "700 15px var(--font-nunito), sans-serif",
                    boxShadow: "0 10px 26px var(--glow1)",
                    border: "1px solid transparent",
                  }
                : {
                    background: "var(--card2)",
                    border: "1px solid var(--line)",
                    color: "var(--text2)",
                    font: "700 15px var(--font-nunito), sans-serif",
                    backdropFilter: "blur(14px)",
                    WebkitBackdropFilter: "blur(14px)",
                  }
            }
          >
            {t.label}
          </button>
        );
      })}
    </div>
  );
}

/* ═══════════════════════════════════════════════════════════════
   MAIN PAGE COMPONENT
   ═══════════════════════════════════════════════════════════════ */

export default function KnowledgePage() {
  // Absorption tab: Library (this page) | My Documents (embedded page)
  const [pageTab, setPageTab] = useState<"library" | "documents">("library");

  // Deep link: /dashboard/knowledge?tab=documents. Read after mount (not via
  // useSearchParams) to keep the page prerender/Suspense-free.
  useEffect(() => {
    if (new URLSearchParams(window.location.search).get("tab") === "documents") {
      setPageTab("documents");
    }
  }, []);
  // --- Data state ---
  const [libraries, setLibraries] = useState<KnowledgeLibrary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // --- View state ---
  const [viewMode, setViewMode] = useState<ViewMode>("shelves");
  const [selectedLibrary, setSelectedLibrary] = useState<KnowledgeLibrary | null>(null);
  // Article the viewer should jump straight to (set from a search result).
  const [pendingArticle, setPendingArticle] = useState<string | null>(null);

  // --- Search state ---
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<SearchResult[]>([]);
  const [titleResults, setTitleResults] = useState<TitleSearchResult[]>([]);
  const [ftsResults, setFtsResults] = useState<FulltextResult[]>([]);
  const [ftsLibraryCount, setFtsLibraryCount] = useState(0);
  const [searching, setSearching] = useState(false);
  const searchTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // --- Upload state ---
  const [showUploadArea, setShowUploadArea] = useState(false);
  const [uploadZone, setUploadZone] = useState<1 | 2>(2);
  const [uploading, setUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // --- Delete confirmation state ---
  const [deleteTarget, setDeleteTarget] = useState<KnowledgeLibrary | null>(null);
  const [deleting, setDeleting] = useState(false);

  // --- Toast state ---
  const [toast, setToast] = useState<{
    message: string;
    type: "success" | "error";
  } | null>(null);

  const showToast = (message: string, type: "success" | "error") => {
    setToast({ message, type });
    setTimeout(() => setToast(null), 4000);
  };

  // --- Fetch libraries ---
  const fetchLibraries = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await getLibraries();
      setLibraries(data);
    } catch (err: any) {
      setError(err.message || "Knowledge service unavailable");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchLibraries();
  }, [fetchLibraries]);

  // --- Debounced global search ---
  // Three independent passes, each allowed to fail on its own: full-text over
  // article bodies (primary), title match across ZIM packs, RAG semantic.
  useEffect(() => {
    if (searchTimer.current) clearTimeout(searchTimer.current);

    if (!searchQuery.trim()) {
      setSearchResults([]);
      setTitleResults([]);
      setFtsResults([]);
      setFtsLibraryCount(0);
      setSearching(false);
      return;
    }

    setSearching(true);
    searchTimer.current = setTimeout(async () => {
      const [fts, titles, rag] = await Promise.allSettled([
        searchFulltext(searchQuery, { maxResults: 30 }),
        searchTitles(searchQuery, 50),
        searchKnowledge(searchQuery),
      ]);

      if (fts.status === "fulfilled") {
        setFtsResults(fts.value.results);
        setFtsLibraryCount(fts.value.librariesSearched.length);
      } else {
        setFtsResults([]);
        setFtsLibraryCount(0);
      }
      setTitleResults(titles.status === "fulfilled" ? titles.value.results : []);
      setSearchResults(rag.status === "fulfilled" ? rag.value : []);
      setSearching(false);
    }, 400);

    return () => {
      if (searchTimer.current) clearTimeout(searchTimer.current);
    };
  }, [searchQuery]);

  // --- Upload handler ---
  const handleUpload = async (files: FileList | null) => {
    if (!files || files.length === 0) return;

    const file = files[0];
    const ext = file.name.split(".").pop()?.toLowerCase();
    const allowed = [
      "txt", "md", "pdf", "json", "csv", "tsv",
      "docx", "doc", "pptx", "rtf", "xlsx", "xls",
      "html", "xml", "yaml", "yml",
      "jpg", "jpeg", "png", "gif", "bmp", "tiff", "tif", "webp",
      "py", "js", "ts", "log", "ini", "cfg", "toml",
    ];
    if (!ext || !allowed.includes(ext)) {
      showToast(
        `Unsupported file type .${ext}. Supported: documents, spreadsheets, images, code files`,
        "error"
      );
      return;
    }

    setUploading(true);
    setUploadProgress(`Indexing ${file.name}...`);
    try {
      const res = await uploadDocument(file, uploadZone);
      if (res.success) {
        showToast(
          `"${file.name}" indexed into ${uploadZone === 1 ? "the Vault" : "the Brain"}`,
          "success"
        );
        setShowUploadArea(false);
        await fetchLibraries();
      } else {
        showToast("Upload failed. Please try again.", "error");
      }
    } catch (err: any) {
      showToast(err.message || "Upload failed", "error");
    } finally {
      setUploading(false);
      setUploadProgress(null);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  };

  // --- Delete handler ---
  const handleDelete = async (lib: KnowledgeLibrary) => {
    setDeleting(true);
    try {
      const res = await deleteLibrary(lib.name);
      if (res.success) {
        setLibraries((prev) => prev.filter((l) => l.name !== lib.name));
        showToast(`Removed "${lib.name}" from library`, "success");
      } else {
        showToast("Delete failed. Please try again.", "error");
      }
    } catch (err: any) {
      showToast(err.message || "Delete failed", "error");
    } finally {
      setDeleting(false);
      setDeleteTarget(null);
    }
  };

  // --- Drop handlers ---
  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(true);
  };
  const handleDragLeave = () => setDragOver(false);
  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(false);
    handleUpload(e.dataTransfer.files);
  };

  // --- Open library ---
  const openLibrary = (lib: KnowledgeLibrary) => {
    setPendingArticle(null);
    setSelectedLibrary(lib);
    setViewMode("viewer");
  };

  /** Open a search hit: its library if we know it, otherwise a stand-in row
   *  built from the hit itself so the article still resolves. */
  const openResult = (libraryId: string, libraryName: string, articleUrl?: string) => {
    const lib =
      findLibrary(libraries, libraryId) ||
      ({
        name: libraryName || libraryId,
        collection: libraryId,
        library_id: libraryId,
        zone: "knowledge",
      } as KnowledgeLibrary);
    setPendingArticle(articleUrl || null);
    setSelectedLibrary(lib);
    setViewMode("viewer");
  };

  const closeViewer = () => {
    setViewMode("shelves");
    setSelectedLibrary(null);
    setPendingArticle(null);
  };

  // --- Derived data ---
  const totalChunks = libraries.reduce((sum, lib) => sum + chunkCount(lib), 0);
  const searchableCount = libraries.filter((lib) => lib.fts_ready).length;

  // Group libraries by category
  const shelves: Record<CategoryKey, KnowledgeLibrary[]> = {
    encyclopedia: [],
    video: [],
    qa: [],
    medical: [],
    reference: [],
    survival: [],
    science: [],
    travel: [],
    books: [],
    uploads: [],
  };
  libraries.forEach((lib) => {
    const cat = categorizeLibrary(lib);
    shelves[cat].push(lib);
  });

  // Order categories for display (non-empty first)
  const shelfOrder: CategoryKey[] = [
    "encyclopedia",
    "video",
    "qa",
    "medical",
    "science",
    "reference",
    "survival",
    "travel",
    "books",
    "uploads",
  ];

  // --- Skeleton loader ---
  const SkeletonShelf = () => (
    <div className="mb-8">
      <div className="mb-3 h-4 w-40 animate-pulse rounded bg-slate-800" />
      <div className="flex gap-4">
        {[1, 2, 3, 4].map((i) => (
          <div
            key={i}
            className="h-52 w-40 animate-pulse rounded-lg bg-slate-800/50"
          />
        ))}
      </div>
    </div>
  );

  const absorbTabs = (
    <AbsorbTabs
      tabs={[
        { key: "library" as const, label: "Library" },
        { key: "documents" as const, label: "My Documents" },
      ]}
      active={pageTab}
      onSelect={(key) => setPageTab(key)}
    />
  );

  /* ═══════════════════════════════════════════════════════════════
     RENDER — MY DOCUMENTS (absorbed page, rendered in place)
     ═══════════════════════════════════════════════════════════════ */

  if (pageTab === "documents") {
    return (
      <div className={`px-6 pt-6 ${containerMaxW}`}>
        {absorbTabs}
        <MyDocuments />
      </div>
    );
  }

  /* ═══════════════════════════════════════════════════════════════
     RENDER — LIBRARY VIEWER
     ═══════════════════════════════════════════════════════════════ */

  if (viewMode === "viewer" && selectedLibrary) {
    return (
      <SectionMain>
        {/* Toast */}
        {toast && (
          <div
            className={`fixed top-4 right-4 z-50 flex items-center gap-2 rounded-lg px-4 py-3 text-sm font-medium text-white shadow-lg ${
              toast.type === "success" ? "bg-green-600" : "bg-red-600"
            }`}
          >
            <Icon
              path={toast.type === "success" ? mdiCheckCircle : mdiAlertCircle}
              size="18"
            />
            {toast.message}
          </div>
        )}

        <LibraryViewer
          key={selectedLibrary.collection || selectedLibrary.name}
          library={selectedLibrary}
          category={categorizeLibrary(selectedLibrary)}
          onClose={closeViewer}
          initialArticleUrl={pendingArticle}
        />
      </SectionMain>
    );
  }

  /* ═══════════════════════════════════════════════════════════════
     RENDER — BOOKSHELF VIEW
     ═══════════════════════════════════════════════════════════════ */

  return (
    <SectionMain>
      {/* Toast Notification */}
      {toast && (
        <div
          className={`fixed top-4 right-4 z-50 flex items-center gap-2 rounded-lg px-4 py-3 text-sm font-medium text-white shadow-lg transition-all ${
            toast.type === "success" ? "bg-green-600" : "bg-red-600"
          }`}
        >
          <Icon
            path={toast.type === "success" ? mdiCheckCircle : mdiAlertCircle}
            size="18"
          />
          {toast.message}
        </div>
      )}

      {/* Delete Confirmation Modal */}
      {deleteTarget && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
          <CardBox className="w-full max-w-md">
            <h3 className="mb-2 text-lg font-semibold text-white">
              Remove from Library
            </h3>
            <p className="mb-4 text-sm text-slate-400">
              Remove <strong className="text-slate-200">{deleteTarget.name}</strong> and
              all indexed chunks? This cannot be undone.
            </p>
            <div className="flex justify-end gap-2">
              <Button
                label="Cancel"
                color="white"
                small
                onClick={() => setDeleteTarget(null)}
                disabled={deleting}
              />
              <Button
                label={deleting ? "Removing..." : "Remove"}
                color="danger"
                small
                icon={mdiDelete}
                onClick={() => handleDelete(deleteTarget)}
                disabled={deleting}
              />
            </div>
          </CardBox>
        </div>
      )}

      {/* Absorption tabs: Library | My Documents */}
      {absorbTabs}

      {/* Page Header */}
      <SectionTitleLineWithButton
        icon={mdiBookOpenPageVariant}
        title="Knowledge Base"
        main
      >
        <div className="flex gap-2">
          <Button
            label="Add to Library"
            icon={mdiUpload}
            color="info"
            small
            roundedFull
            onClick={() => setShowUploadArea(!showUploadArea)}
          />
          <Button
            icon={mdiRefresh}
            color="white"
            small
            onClick={fetchLibraries}
          />
        </div>
      </SectionTitleLineWithButton>

      {/* Upload Area */}
      {showUploadArea && (
        <CardBox className="mb-6">
          <div className="mb-4 flex items-center justify-between">
            <h3 className="font-semibold text-white">Add Document to Knowledge Base</h3>
            <button onClick={() => setShowUploadArea(false)}>
              <Icon
                path={mdiClose}
                size="20"
                className="text-slate-500 hover:text-slate-300"
              />
            </button>
          </div>

          {/* Zone Selector */}
          <div className="mb-4">
            <label className="mb-2 block text-sm font-medium text-slate-300">
              Where should this be stored?
            </label>
            <div className="flex gap-3">
              <button
                onClick={() => setUploadZone(2)}
                className={`flex-1 rounded-lg border-2 p-3 text-center text-sm transition ${
                  uploadZone === 2
                    ? "border-cyan-500 bg-cyan-500/10 text-cyan-300"
                    : "border-white/10 text-slate-400 hover:border-white/20"
                }`}
              >
                <div className="font-medium">The Brain</div>
                <div className="mt-1 text-xs opacity-70">
                  Open access — available to all queries
                </div>
              </button>
              <button
                onClick={() => setUploadZone(1)}
                className={`flex-1 rounded-lg border-2 p-3 text-center text-sm transition ${
                  uploadZone === 1
                    ? "border-purple-500 bg-purple-500/10 text-purple-300"
                    : "border-white/10 text-slate-400 hover:border-white/20"
                }`}
              >
                <div className="font-medium">The Vault</div>
                <div className="mt-1 text-xs opacity-70">
                  PIN-gated — encrypted, private data
                </div>
              </button>
            </div>
            <p className="mt-2 text-xs text-slate-500">
              PuffPAI auto-detects PII (passwords, SSN, medical records) and will recommend Vault storage when appropriate.
            </p>
          </div>

          {/* Drop Zone */}
          <div
            onDragOver={handleDragOver}
            onDragLeave={handleDragLeave}
            onDrop={handleDrop}
            className={`relative flex cursor-pointer flex-col items-center justify-center rounded-xl border-2 border-dashed p-8 transition-colors ${
              dragOver
                ? "border-cyan-400 bg-cyan-400/5"
                : "border-white/10 hover:border-cyan-400/30"
            }`}
            onClick={() => fileInputRef.current?.click()}
          >
            <Icon
              path={mdiCloudUpload}
              size="48"
              className={`mb-2 ${dragOver ? "text-cyan-400" : "text-slate-600"}`}
            />
            {uploading ? (
              <div className="text-center">
                <div className="mb-2 flex items-center gap-2 text-sm font-medium text-cyan-400">
                  <Icon path={mdiLoading} size="16" className="animate-spin" />
                  {uploadProgress}
                </div>
                <div className="mx-auto h-2 w-48 overflow-hidden rounded-full bg-slate-800">
                  <div
                    className="h-full animate-pulse rounded-full bg-cyan-500"
                    style={{ width: "60%" }}
                  />
                </div>
              </div>
            ) : (
              <>
                <p className="text-sm font-medium text-slate-300">
                  Drop files here or click to browse
                </p>
                <p className="mt-1 text-xs text-slate-500">
                  PDF, Word, Excel, PowerPoint, images, code, and more
                </p>
              </>
            )}
            <input
              ref={fileInputRef}
              type="file"
              className="hidden"
              accept=".txt,.md,.pdf,.json,.csv,.tsv,.docx,.doc,.pptx,.rtf,.xlsx,.xls,.html,.xml,.yaml,.yml,.jpg,.jpeg,.png,.gif,.bmp,.tiff,.tif,.webp,.py,.js,.ts,.log,.ini,.cfg,.toml"
              onChange={(e) => handleUpload(e.target.files)}
              disabled={uploading}
            />
          </div>

          {/* Phone route into the same pipeline: QR → /send?code=... */}
          <PairDeviceCard />
        </CardBox>
      )}

      {/* Stats Row */}
      <div className="mb-6 grid grid-cols-1 gap-6 sm:grid-cols-3">
        {loading ? (
          <>
            {[1, 2, 3].map((i) => (
              <CardBox key={i}>
                <div className="animate-pulse">
                  <div className="mb-3 h-4 w-20 rounded bg-slate-800" />
                  <div className="h-8 w-16 rounded bg-slate-800" />
                  <div className="mt-2 h-3 w-24 rounded bg-slate-800" />
                </div>
              </CardBox>
            ))}
          </>
        ) : (
          <>
            <CardBoxWidget
              trendLabel="Indexed"
              trendType="success"
              trendColor="success"
              icon={mdiBookOpenPageVariant}
              iconColor="info"
              number={libraries.length}
              label="Libraries"
            />
            <CardBoxWidget
              trendLabel={totalChunks > 0 ? `${formatChunks(totalChunks)} chunks` : "Full-text"}
              trendType="info"
              trendColor="info"
              icon={mdiDatabase}
              iconColor="success"
              number={searchableCount}
              label="Searchable"
            />
            <CardBoxWidget
              trendLabel="Dual-Zone"
              trendType="warning"
              trendColor="warning"
              icon={mdiFileDocument}
              iconColor="warning"
              number={Object.values(shelves).filter((s) => s.length > 0).length}
              label="Categories"
            />
          </>
        )}
      </div>

      {/* Global Search */}
      <CardBox className="mb-6">
        <div className="flex items-center gap-3">
          <Icon path={mdiMagnify} size="20" className="text-slate-500" />
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder={
              searchableCount > 0
                ? `Search the full text of ${searchableCount} indexed libraries...`
                : "Search across all knowledge..."
            }
            /* .kb-viewer-search is remapped to the theme glass in
               css/_theme.css; a bare input here inherits main.css's
               `.dark input[type="text"]` navy box, which is illegible in day. */
            className="kb-viewer-search"
          />
          {searching && (
            <Icon
              path={mdiLoading}
              size="18"
              className="animate-spin text-cyan-400"
            />
          )}
          {searchQuery && (
            <button onClick={() => setSearchQuery("")}>
              <Icon
                path={mdiClose}
                size="16"
                className="text-slate-500 hover:text-slate-300"
              />
            </button>
          )}
        </div>
      </CardBox>

      {/* ═══ PRIMARY: full-text hits over article bodies ═══ */}
      {ftsResults.length > 0 && (
        <CardBox className="mb-6">
          <div className="kb-results-head">
            <Icon path={mdiTextSearch} size="16" />
            <span className="kb-results-head-title">Full text</span>
            <span className="kb-results-head-count">
              {ftsResults.length} passage{ftsResults.length !== 1 ? "s" : ""}
              {ftsLibraryCount > 0 && ` · ${ftsLibraryCount} indexed librar${ftsLibraryCount !== 1 ? "ies" : "y"}`}
            </span>
          </div>
          <div className="kb-hits">
            {ftsResults.map((r, idx) => (
              <button
                key={`${r.library_id}-${r.path}-${idx}`}
                onClick={() => openResult(r.library_id, r.library_name, r.path)}
                className="kb-hit"
              >
                <span className="kb-hit-head">
                  {/* title is RAW server data — text node only */}
                  <span className="kb-hit-title">{r.title}</span>
                  <span className="kb-hit-rel" title="Relevance within its library">
                    {Math.round((Number(r.relevance) || 0) * 100)}
                  </span>
                </span>
                {/* snippet is server-escaped apart from <b> highlight markers */}
                <span
                  className="kb-hit-snippet"
                  dangerouslySetInnerHTML={{ __html: r.snippet }}
                />
                <span className="kb-hit-lib">{r.library_name}</span>
              </button>
            ))}
          </div>
        </CardBox>
      )}

      {/* Secondary: title matches */}
      {titleResults.length > 0 && (
        <CardBox className="mb-6">
          <div className="kb-results-head">
            <Icon path={mdiMagnify} size="16" />
            <span className="kb-results-head-title">Titles</span>
            <span className="kb-results-head-count">
              {titleResults.length} article{titleResults.length !== 1 ? "s" : ""} named for this query
            </span>
          </div>
          <div className="divide-y divide-white/5">
            {titleResults.map((result, idx) => {
              const isVid = result.library_name.toLowerCase().startsWith("ted ") ||
                result.collection.includes("_ted_") ||
                result.collection.startsWith("ted_");
              return (
                <button
                  key={`${result.collection}-${result.url}-${idx}`}
                  onClick={() =>
                    openResult(result.collection, result.library_name, result.url)
                  }
                  className="flex w-full items-center gap-3 px-3 py-3 text-left transition hover:bg-white/5"
                >
                  <Icon
                    path={isVid ? mdiPlayCircle : mdiFileDocument}
                    size="18"
                    className={isVid ? "flex-shrink-0 text-pink-400" : "flex-shrink-0 text-cyan-400"}
                  />
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-medium text-white">
                      {result.title}
                    </p>
                    <p className="mt-0.5 truncate text-xs text-slate-500">
                      {result.library_name}
                    </p>
                  </div>
                  <Icon path={mdiChevronRight} size="16" className="flex-shrink-0 text-slate-600" />
                </button>
              );
            })}
          </div>
        </CardBox>
      )}

      {/* Secondary: RAG semantic matches */}
      {searchResults.length > 0 && (
        <CardBox className="mb-6">
          <div className="kb-results-head">
            <Icon path={mdiDatabase} size="16" />
            <span className="kb-results-head-title">Semantic</span>
            <span className="kb-results-head-count">
              {searchResults.length} RAG context match{searchResults.length !== 1 ? "es" : ""}
            </span>
          </div>
          <div className="space-y-3">
            {searchResults.map((result, idx) => (
              <div
                key={idx}
                className="rounded-lg border border-white/5 bg-white/[0.02] p-4"
              >
                <div className="mb-2 flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <span className={`kb-zone-pill ${zoneBadge(result.zone).cls}`}>
                      {zoneBadge(result.zone).short}
                    </span>
                    <span className="text-xs text-slate-400">
                      {result.source || result.library}
                    </span>
                  </div>
                  {result.score != null && (
                    <span className="rounded-full bg-green-900/30 px-2 py-0.5 text-xs font-medium text-green-400">
                      {(result.score * 100).toFixed(0)}% match
                    </span>
                  )}
                </div>
                <p className="text-sm leading-relaxed text-slate-300">
                  {result.text}
                </p>
              </div>
            ))}
          </div>
        </CardBox>
      )}

      {/* Nothing matched anywhere */}
      {!searching &&
        searchQuery.trim().length > 1 &&
        ftsResults.length === 0 &&
        titleResults.length === 0 &&
        searchResults.length === 0 && (
          <CardBox className="mb-6">
            <div className="py-6 text-center">
              <p className="text-sm text-slate-400">
                No matches for &ldquo;{searchQuery}&rdquo;.
              </p>
              <p className="mt-1 text-xs text-slate-600">
                {searchableCount} of {libraries.length} libraries have a full-text index;
                the rest are searchable by title only.
              </p>
            </div>
          </CardBox>
        )}

      {/* Dual-Zone Explainer */}
      <div className="mb-6 grid grid-cols-1 gap-4 md:grid-cols-2">
        <CardBox className="border-l-4 border-l-cyan-500">
          <h3 className="mb-1 font-semibold text-cyan-400">
            Zone 2 · The Brain
          </h3>
          <p className="text-sm text-slate-400">
            Open access. Offline encyclopedias, documentation, and reference materials
            available to all queries.
          </p>
        </CardBox>
        <CardBox className="border-l-4 border-l-purple-500">
          <h3 className="mb-1 font-semibold text-purple-400">
            Zone 1 · The Vault
          </h3>
          <p className="text-sm text-slate-400">
            Requires vault PIN. Your personal files, notes, and encrypted data — accessed
            only when authenticated.
          </p>
        </CardBox>
      </div>

      {/* Error State */}
      {error && (
        <CardBox className="mb-6">
          <div className="flex flex-col items-center justify-center py-8 text-center">
            <Icon path={mdiAlertCircle} size="48" className="mb-3 text-red-400" />
            <h3 className="mb-1 font-semibold text-slate-300">
              Knowledge service unavailable
            </h3>
            <p className="mb-4 text-sm text-slate-500">{error}</p>
            <Button
              label="Retry"
              icon={mdiRefresh}
              color="info"
              small
              onClick={fetchLibraries}
            />
          </div>
        </CardBox>
      )}

      {/* ═══ THE BOOKSHELF ═══ */}
      {!error && (
        <>
          {/* What the wizard chose for this build, against what is actually on
               this disk. Above the shelf because an empty shelf with five
               chosen sources is exactly the gap it explains. */}
          <BuildSources libraryCount={libraries.length} />

          <SectionTitleLineWithButton
            icon={mdiBookOpenPageVariant}
            title="The Library"
          />

          {loading ? (
            <>
              <SkeletonShelf />
              <SkeletonShelf />
            </>
          ) : libraries.length === 0 ? (
            <CardBox>
              <div className="flex flex-col items-center justify-center py-12 text-center">
                <Icon
                  path={mdiInboxFull}
                  size="48"
                  className="mb-3 text-slate-600"
                />
                <h3 className="mb-1 font-semibold text-slate-400">
                  Your library is empty
                </h3>
                <p className="mb-4 text-sm text-slate-500">
                  Upload a document to place your first book on the shelf.
                </p>
                <Button
                  label="Add Your First Book"
                  icon={mdiUpload}
                  color="info"
                  small
                  onClick={() => setShowUploadArea(true)}
                />
              </div>
            </CardBox>
          ) : (
            <div>
              {shelfOrder.map((catKey) => {
                const libs = shelves[catKey];
                if (libs.length === 0) return null;
                return (
                  <ShelfRow
                    key={catKey}
                    category={catKey}
                    libraries={libs}
                    onBookClick={openLibrary}
                  />
                );
              })}

              {/* Compact list view for quick management */}
              <div className="mt-8">
                <div className="mb-3 flex items-center gap-2 text-xs font-medium uppercase tracking-wider text-slate-500">
                  <Icon path={mdiFormatListBulleted} size="14" />
                  All Libraries — Quick Manage
                </div>
                <CardBox>
                  <div className="divide-y divide-white/5">
                    {libraries.map((lib) => {
                      const cat = categorizeLibrary(lib);
                      const catInfo = CATEGORIES[cat];
                      return (
                        <div
                          key={lib.collection || lib.name}
                          className="flex items-center justify-between px-2 py-3"
                        >
                          <div className="flex items-center gap-3">
                            <span style={{ color: catInfo.color }}>
                              <Icon path={catInfo.icon} size="16" />
                            </span>
                            <span className="text-sm font-medium text-slate-200">
                              {lib.name}
                            </span>
                            <span className={`kb-zone-pill ${zoneBadge(lib.zone).cls}`}>
                              {zoneBadge(lib.zone).short}
                            </span>
                            {lib.fts_ready && (
                              <span className="kb-fts-tag" title="Full-text searchable">
                                <Icon path={mdiTextSearch} size="12" />
                                FTS
                              </span>
                            )}
                          </div>
                          <div className="flex items-center gap-4">
                            <span className="text-xs text-slate-500">
                              {chunkCount(lib) > 0
                                ? `${formatChunks(chunkCount(lib))} chunks`
                                : formatSize(lib.size)}
                            </span>
                            <button
                              onClick={() => openLibrary(lib)}
                              className="text-slate-500 hover:text-cyan-400"
                              title="Open"
                            >
                              <Icon path={mdiOpenInNew} size="16" />
                            </button>
                            <button
                              onClick={(e) => {
                                e.stopPropagation();
                                setDeleteTarget(lib);
                              }}
                              className="text-slate-600 hover:text-red-400"
                              title="Remove"
                            >
                              <Icon path={mdiDelete} size="16" />
                            </button>
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </CardBox>
              </div>
            </div>
          )}
        </>
      )}
    </SectionMain>
  );
}
