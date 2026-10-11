"use client";

import React, { useState, useEffect, useCallback, useRef } from "react";
import {
  mdiUpload,
  mdiFileDocumentMultipleOutline,
  mdiSilverwareForkKnife,
  mdiCalendarMonth,
  mdiBookOpenPageVariant,
  mdiCartOutline,
  mdiDelete,
  mdiCheckCircle,
  mdiAlertCircle,
  mdiClose,
  mdiLoading,
  mdiImage,
  mdiFilePdfBox,
  mdiFileWordBox,
  mdiFileExcelBox,
  mdiFileDocumentOutline,
  mdiBrain,
  mdiRefresh,
} from "@mdi/js";
import SectionMain from "@/app/_components/Section/Main";
import SectionTitleLineWithButton from "@/app/_components/Section/TitleLineWithButton";
import CardBox from "@/app/_components/CardBox";
import Button from "@/app/_components/Button";
import Icon from "@/app/_components/Icon";
import {
  confirmSearchable,
  confirmationLine,
  SEARCHABLE_EVERYWHERE,
  type IndexConfirmation,
} from "@/app/_lib/indexConfirm";

const API_BASE = "/api/pai";

interface IngestedDoc {
  id: string;
  filename: string;
  /** Puff's label for the upload; absent on older records. */
  title?: string;
  classification: string;
  indexed_at: string;
  word_count: number;
}

interface IngestStats {
  document_count: number;
  chunk_count: number;
  indexed_document_count: number;
  fully_indexed: boolean;
}

interface IngestionResult {
  success: boolean;
  file_id?: string;
  filename?: string;
  file_type?: string;
  classification?: string;
  text_length?: number;
  structured_data?: Record<string, unknown>;
  actions_taken?: string[];
  error?: string;
}

const CLASSIFICATION_META: Record<
  string,
  { label: string; icon: string; color: string }
> = {
  recipe: {
    label: "Recipe",
    icon: mdiSilverwareForkKnife,
    color: "text-pink-500 bg-pink-100 dark:bg-pink-900/30",
  },
  event: {
    label: "Event",
    icon: mdiCalendarMonth,
    color: "text-cyan-500 bg-cyan-100 dark:bg-cyan-900/30",
  },
  schedule: {
    label: "Schedule",
    icon: mdiCalendarMonth,
    color: "text-cyan-500 bg-cyan-100 dark:bg-cyan-900/30",
  },
  study: {
    label: "Study Material",
    icon: mdiBookOpenPageVariant,
    color: "text-purple-500 bg-purple-100 dark:bg-purple-900/30",
  },
  shopping_list: {
    label: "Shopping List",
    icon: mdiCartOutline,
    color: "text-emerald-500 bg-emerald-100 dark:bg-emerald-900/30",
  },
  note: {
    label: "Note",
    icon: mdiFileDocumentOutline,
    color: "text-amber-500 bg-amber-100 dark:bg-amber-900/30",
  },
  unknown: {
    label: "Unknown",
    icon: mdiFileDocumentOutline,
    color: "text-gray-500 bg-gray-100 dark:bg-gray-800",
  },
};

function fileTypeIcon(filename: string): string {
  const ext = filename.toLowerCase().split(".").pop() || "";
  if (["jpg", "jpeg", "png", "gif", "webp", "bmp", "tiff"].includes(ext))
    return mdiImage;
  if (ext === "pdf") return mdiFilePdfBox;
  if (["doc", "docx"].includes(ext)) return mdiFileWordBox;
  if (["xls", "xlsx", "csv"].includes(ext)) return mdiFileExcelBox;
  return mdiFileDocumentOutline;
}

export default function DocumentsPage() {
  const [docs, setDocs] = useState<IngestedDoc[]>([]);
  const [stats, setStats] = useState<IngestStats | null>(null);
  const [reindexing, setReindexing] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [hint, setHint] = useState("");
  const [autoApply, setAutoApply] = useState(true);
  const [recentResult, setRecentResult] = useState<IngestionResult | null>(null);
  // Whether the document just uploaded can actually be FOUND. Set only by a real
  // /knowledge/search round-trip, never by the upload own 200 answer. See
  // app/_lib/indexConfirm.ts.
  const [indexState, setIndexState] = useState<IndexConfirmation | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<any[]>([]);
  const [searching, setSearching] = useState(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const loadStats = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/ingest/stats`);
      const data = await res.json();
      setStats(data);
    } catch {
      // ignore — stats are non-critical
    }
  }, []);

  const loadDocs = useCallback(async () => {
    try {
      setError(null);
      const res = await fetch(`${API_BASE}/ingest/list`);
      const data = await res.json();
      setDocs(data.documents || []);
      loadStats();
    } catch (e: any) {
      setError(e.message || "Failed to load documents");
    } finally {
      setLoading(false);
    }
  }, [loadStats]);

  const handleReindex = async () => {
    setReindexing(true);
    try {
      const res = await fetch(`${API_BASE}/ingest/reindex`, { method: "POST" });
      const data = await res.json();
      if (data.success) {
        await loadStats();
      }
    } catch (e: any) {
      setError(e.message || "Reindex failed");
    } finally {
      setReindexing(false);
    }
  };

  useEffect(() => {
    loadDocs();
  }, [loadDocs]);

  const handleUpload = async (file: File) => {
    setUploading(true);
    setError(null);
    setRecentResult(null);
    setIndexState(null);
    try {
      const formData = new FormData();
      formData.append("file", file);
      if (hint) formData.append("hint", hint);
      formData.append("auto_apply", autoApply ? "true" : "false");

      const res = await fetch(`${API_BASE}/ingest/upload`, {
        method: "POST",
        body: formData,
      });
      const result: IngestionResult = await res.json();
      setRecentResult(result);
      if (result.error) {
        setError(result.error);
      }
      // Refresh the list whether or not it was a "study" doc
      await loadDocs();
      setHint("");
      if (fileInputRef.current) fileInputRef.current.value = "";

      // Only NOW ask whether it is searchable, and only say so if the search
      // agrees. A stored document that cannot be retrieved is the failure this
      // page used to hide behind a green tick.
      if (!result.error) {
        setConfirming(true);
        setIndexState(
          await confirmSearchable(result.filename || file.name, { base: API_BASE }),
        );
        setConfirming(false);
      }
    } catch (e: any) {
      setError(e.message || "Upload failed");
    } finally {
      setUploading(false);
    }
  };

  const handleDelete = async (docId: string) => {
    if (!confirm("Delete this document?")) return;
    try {
      await fetch(`${API_BASE}/ingest/document/${docId}`, { method: "DELETE" });
      await loadDocs();
    } catch (e: any) {
      setError(e.message || "Delete failed");
    }
  };

  const handleSearch = async () => {
    if (!searchQuery.trim()) return;
    setSearching(true);
    try {
      const res = await fetch(
        `${API_BASE}/ingest/search?q=${encodeURIComponent(searchQuery)}`
      );
      const data = await res.json();
      setSearchResults(data.results || []);
    } catch (e: any) {
      setError(e.message || "Search failed");
    } finally {
      setSearching(false);
    }
  };

  return (
    <SectionMain>
      {/* ─── Header ──────────────────────────────────────────────── */}
      <div className="bokeh-container relative mb-6 overflow-hidden rounded-2xl bg-gradient-to-r from-[#5b4ade] via-[#7e57c2] to-[#9b6dff] p-6 text-white shadow-lg">
        <div className="bokeh-bubble bokeh-bubble-1" />
        <div className="bokeh-bubble bokeh-bubble-2" />
        <div className="relative z-10">
          <h1 className="text-2xl font-bold drop-shadow-sm">Your Documents</h1>
          <p className="mt-1 text-sm text-purple-100">
            Upload recipes, school flyers, study material — PuffPAI reads them
            and adds them to your apps automatically.
          </p>
          {/* One knowledge story: the backend searches both upload stores, so
              saying it here and on the Knowledge page is the truth, not a hope. */}
          <p className="mt-2 text-xs text-purple-100/90">{SEARCHABLE_EVERYWHERE}</p>
        </div>
      </div>

      {/* ─── Brain Status Banner ─────────────────────────────────── */}
      {stats && (
        <div className="mb-6 flex flex-wrap items-center gap-3 rounded-xl border border-purple-200/60 bg-gradient-to-r from-purple-50 to-indigo-50 px-4 py-3 dark:border-purple-900/40 dark:from-purple-950/30 dark:to-indigo-950/30">
          <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-purple-500/20 text-purple-500">
            <Icon path={mdiBrain} size="22" />
          </div>
          <div className="flex-1">
            <p className="text-sm font-semibold text-[#0C2F44] dark:text-white">
              {stats.indexed_document_count > 0 ? (
                <>
                  {stats.indexed_document_count} document
                  {stats.indexed_document_count === 1 ? "" : "s"} in your brain
                  <span className="ml-2 text-xs font-normal text-gray-500 dark:text-slate-400">
                    ({stats.chunk_count.toLocaleString()} searchable chunks)
                  </span>
                </>
              ) : (
                "No documents indexed yet"
              )}
            </p>
            <p className="text-xs text-gray-600 dark:text-slate-400">
              {stats.fully_indexed
                ? "PuffPAI can find anything you've uploaded by meaning, not just keywords."
                : `${stats.document_count - stats.indexed_document_count} document(s) need to be reindexed.`}
            </p>
          </div>
          {!stats.fully_indexed && stats.document_count > 0 && (
            <button
              onClick={handleReindex}
              disabled={reindexing}
              className="flex items-center gap-1 rounded-lg bg-purple-500 px-3 py-1.5 text-xs font-semibold text-white hover:bg-purple-600 disabled:opacity-60"
            >
              <Icon
                path={reindexing ? mdiLoading : mdiRefresh}
                size="14"
                className={reindexing ? "animate-spin" : ""}
              />
              Reindex
            </button>
          )}
        </div>
      )}

      {/* ─── Upload Card ─────────────────────────────────────────── */}
      <CardBox className="mb-6">
        <div className="p-5">
          <h2 className="mb-3 flex items-center gap-2 text-lg font-semibold text-[#0C2F44] dark:text-white">
            <Icon path={mdiUpload} size="20" /> Upload a Document
          </h2>

          <div className="mb-3 grid gap-3 sm:grid-cols-2">
            <div>
              <label className="mb-1 block text-xs font-medium text-gray-500 dark:text-slate-400">
                What is this? (optional hint)
              </label>
              <input
                type="text"
                value={hint}
                onChange={(e) => setHint(e.target.value)}
                placeholder="e.g. mom's lasagna recipe, school play flyer"
                className="w-full rounded-lg border border-gray-200 bg-white/80 px-3 py-2 text-sm dark:border-slate-600 dark:bg-slate-800/80 dark:text-white"
              />
            </div>
            <div className="flex items-end">
              <label className="flex items-center gap-2 text-sm text-gray-700 dark:text-slate-300">
                <input
                  type="checkbox"
                  checked={autoApply}
                  onChange={(e) => setAutoApply(e.target.checked)}
                  className="h-4 w-4 rounded accent-[#5b4ade]"
                />
                Automatically save to apps
              </label>
            </div>
          </div>

          <div className="flex items-center gap-3">
            <input
              ref={fileInputRef}
              type="file"
              accept=".jpg,.jpeg,.png,.gif,.bmp,.webp,.tiff,.pdf,.docx,.doc,.txt,.md,.xlsx,.xls,.csv"
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) handleUpload(file);
              }}
              disabled={uploading}
              className="block w-full text-sm text-gray-700 file:mr-3 file:rounded-md file:border-0 file:bg-[#5b4ade] file:px-4 file:py-2 file:text-white file:hover:bg-[#4a3aca] dark:text-slate-300"
            />
            {uploading && (
              <span className="flex items-center gap-2 text-sm text-purple-500">
                <Icon path={mdiLoading} size="18" className="animate-spin" />
                Processing…
              </span>
            )}
          </div>
          <p className="mt-2 text-xs text-gray-500 dark:text-slate-500">
            Supports images (OCR if Tesseract installed), PDF, DOCX, TXT,
            XLSX/CSV. Max 25 MB.
          </p>

          {/* Searchability, asked rather than assumed. "Checking…" while the poll
              runs, then the confirmed line or the real reason it is not there. */}
          {(confirming || indexState) && (
            <div
              className={`mt-3 flex items-start gap-2 rounded-lg border p-3 text-sm ${
                confirming
                  ? "border-purple-200 bg-purple-50 dark:border-purple-900/40 dark:bg-purple-900/20"
                  : indexState?.status === "searchable"
                    ? "border-emerald-200 bg-emerald-50 dark:border-emerald-900/40 dark:bg-emerald-900/20"
                    : "border-amber-200 bg-amber-50 dark:border-amber-900/40 dark:bg-amber-900/20"
              }`}
            >
              <Icon
                path={
                  confirming
                    ? mdiLoading
                    : indexState?.status === "searchable"
                      ? mdiCheckCircle
                      : mdiAlertCircle
                }
                size="18"
                className={
                  confirming
                    ? "animate-spin text-purple-500"
                    : indexState?.status === "searchable"
                      ? "text-emerald-500"
                      : "text-amber-500"
                }
              />
              <span className="text-gray-700 dark:text-slate-300">
                {confirming
                  ? "Checking that Puff can find it…"
                  : confirmationLine(indexState)}
              </span>
            </div>
          )}

          {/* Recent upload result */}
          {recentResult && (
            <div
              className={`mt-4 rounded-lg border p-3 text-sm ${
                recentResult.success
                  ? "border-emerald-200 bg-emerald-50 dark:border-emerald-900/40 dark:bg-emerald-900/20"
                  : "border-red-200 bg-red-50 dark:border-red-900/40 dark:bg-red-900/20"
              }`}
            >
              <div className="flex items-start gap-2">
                <Icon
                  path={recentResult.success ? mdiCheckCircle : mdiAlertCircle}
                  size="18"
                  className={
                    recentResult.success ? "text-emerald-500" : "text-red-500"
                  }
                />
                <div className="flex-1">
                  <p
                    className={
                      recentResult.success
                        ? "font-semibold text-emerald-700 dark:text-emerald-300"
                        : "font-semibold text-red-700 dark:text-red-300"
                    }
                  >
                    {recentResult.filename}
                  </p>
                  {recentResult.classification && (
                    <p className="mt-0.5 text-xs text-gray-600 dark:text-slate-400">
                      Detected as: <strong>{recentResult.classification}</strong>{" "}
                      ({recentResult.text_length} chars extracted)
                    </p>
                  )}
                  {recentResult.actions_taken &&
                    recentResult.actions_taken.length > 0 && (
                      <ul className="mt-1 space-y-0.5 text-xs text-emerald-700 dark:text-emerald-300">
                        {recentResult.actions_taken.map((a, i) => (
                          <li key={i}>✓ {a}</li>
                        ))}
                      </ul>
                    )}
                  {recentResult.error && (
                    <p className="mt-1 text-xs text-red-600 dark:text-red-400">
                      {recentResult.error}
                    </p>
                  )}
                </div>
                <button
                  onClick={() => setRecentResult(null)}
                  className="text-gray-400 hover:text-gray-600"
                  aria-label="Dismiss"
                >
                  <Icon path={mdiClose} size="16" />
                </button>
              </div>
            </div>
          )}
        </div>
      </CardBox>

      {/* ─── Search ──────────────────────────────────────────────── */}
      <CardBox className="mb-6">
        <div className="p-5">
          <h2 className="mb-3 text-lg font-semibold text-[#0C2F44] dark:text-white">
            Search Your Documents
          </h2>
          <div className="flex gap-2">
            <input
              type="text"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && handleSearch()}
              placeholder="e.g. chicken recipe, history exam, school play"
              className="flex-1 rounded-lg border border-gray-200 bg-white/80 px-3 py-2 text-sm dark:border-slate-600 dark:bg-slate-800/80 dark:text-white"
            />
            <Button label="Search" color="info" onClick={handleSearch} />
          </div>

          {searching && (
            <p className="mt-2 text-xs text-gray-500">Searching…</p>
          )}

          {!searching && searchResults.length > 0 && (
            <div className="mt-4 space-y-3">
              {searchResults.map((r) => (
                <div
                  key={r.id}
                  className="rounded-lg border border-gray-200 bg-white/80 p-3 text-sm dark:border-slate-700 dark:bg-slate-800/40"
                >
                  <div className="mb-1 flex items-center gap-2">
                    <Icon
                      path={fileTypeIcon(r.filename)}
                      size="16"
                      className="text-purple-500"
                    />
                    <span className="font-semibold text-[#0C2F44] dark:text-white">
                      {r.filename}
                    </span>
                    <span className="text-xs text-gray-500 dark:text-slate-500">
                      ({r.classification})
                    </span>
                  </div>
                  <p className="text-xs text-gray-600 dark:text-slate-400">
                    {r.snippet}
                  </p>
                </div>
              ))}
            </div>
          )}

          {!searching && searchQuery && searchResults.length === 0 && (
            <p className="mt-2 text-xs text-gray-500">
              No matches for &quot;{searchQuery}&quot;.
            </p>
          )}
        </div>
      </CardBox>

      {/* ─── Document List ───────────────────────────────────────── */}
      <SectionTitleLineWithButton
        icon={mdiFileDocumentMultipleOutline}
        title="Indexed Documents"
      >
        <Button
          label="Refresh"
          color="whiteDark"
          small
          roundedFull
          onClick={loadDocs}
        />
      </SectionTitleLineWithButton>

      {error && (
        <CardBox className="mb-4 border-l-4 border-red-400 bg-red-50 dark:bg-red-900/20">
          <p className="text-sm text-red-600 dark:text-red-400">{error}</p>
        </CardBox>
      )}

      {loading ? (
        <CardBox>
          <div className="flex items-center justify-center py-10">
            <Icon
              path={mdiLoading}
              size="32"
              className="animate-spin text-purple-500"
            />
          </div>
        </CardBox>
      ) : docs.length === 0 ? (
        <CardBox>
          <div className="py-10 text-center text-gray-500 dark:text-slate-400">
            <Icon
              path={mdiFileDocumentMultipleOutline}
              size="48"
              className="mx-auto mb-2 opacity-30"
            />
            <p>No documents yet. Upload one above to get started!</p>
            <p className="mt-1 text-xs">
              Try a recipe, a school flyer, or your study notes.
            </p>
          </div>
        </CardBox>
      ) : (
        <div className="grid gap-3 md:grid-cols-2 lg:grid-cols-3">
          {docs.map((doc) => {
            const meta =
              CLASSIFICATION_META[doc.classification] ||
              CLASSIFICATION_META.unknown;
            return (
              <CardBox key={doc.id} isHoverable>
                <div className="p-4">
                  <div className="mb-2 flex items-start justify-between gap-2">
                    <div className="flex items-center gap-2">
                      <div
                        className={`flex h-10 w-10 items-center justify-center rounded-lg ${meta.color}`}
                      >
                        <Icon path={meta.icon} size="20" />
                      </div>
                      <div className="min-w-0 flex-1">
                        <p
                          className="truncate font-semibold text-[#0C2F44] dark:text-white"
                          title={doc.filename}
                        >
                          {(doc.title || "").trim() || doc.filename}
                        </p>
                        <p className="text-xs text-gray-500 dark:text-slate-500">
                          {meta.label} · {doc.word_count} words
                        </p>
                      </div>
                    </div>
                    <button
                      onClick={() => handleDelete(doc.id)}
                      className="text-gray-400 hover:text-red-500"
                      aria-label="Delete"
                    >
                      <Icon path={mdiDelete} size="16" />
                    </button>
                  </div>
                  <p className="text-[10px] uppercase tracking-wide text-gray-400 dark:text-slate-600">
                    Indexed {new Date(doc.indexed_at).toLocaleDateString()}
                  </p>
                </div>
              </CardBox>
            );
          })}
        </div>
      )}
    </SectionMain>
  );
}
