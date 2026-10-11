"use client";

import { useEffect, useState } from "react";
import {
  mdiLoading,
  mdiFileDocumentAlertOutline,
  mdiDownload,
  mdiInformationOutline,
} from "@mdi/js";
import Icon from "../../_components/Icon";
import CardBox from "../../_components/CardBox";
import {
  librarySourceEmbedUrl,
  getLibrarySourceText,
} from "../../_lib/services/knowledgeService";
import type { KnowledgeLibrary } from "../../_interfaces/api";

/* ═══════════════════════════════════════════════════════════════
   SOURCE VIEW — original material for user-uploaded documents.
   PDFs render in the browser-native viewer via an embedded frame;
   everything else (or a document with no retained original) shows
   the text reconstructed from the indexed chunks.
   ═══════════════════════════════════════════════════════════════ */

export default function SourceView({ library }: { library: KnowledgeLibrary }) {
  const contentType = (library.source_content_type || "").toLowerCase();
  const isPdf = !!library.source_available && contentType.includes("pdf");
  // Images render as an actual <img>, not reconstructed text — so "show me my
  // photo / grandma's recipe" (usually a photo) displays the picture itself.
  const isImage = !!library.source_available && contentType.startsWith("image/");

  const [text, setText] = useState<string | null>(null);
  const [note, setNote] = useState<string>("");
  const [loading, setLoading] = useState<boolean>(!isPdf && !isImage);
  const [failed, setFailed] = useState<boolean>(false);

  useEffect(() => {
    if (isPdf || isImage) return; // embedded directly — no text fetch needed
    let active = true;
    setLoading(true);
    setFailed(false);
    getLibrarySourceText(library.collection).then((res) => {
      if (!active) return;
      if (res) {
        setText(res.text);
        setNote(res.note);
      } else {
        setFailed(true);
      }
      setLoading(false);
    });
    return () => {
      active = false;
    };
  }, [isPdf, isImage, library.collection]);

  // ── Image: show the actual picture ───────────────────────────────
  if (isImage) {
    const url = librarySourceEmbedUrl(library.collection);
    return (
      <CardBox>
        <div className="flex items-center justify-center overflow-hidden rounded-lg border border-white/5 bg-black">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={url}
            alt={library.name}
            className="max-h-[75vh] w-auto max-w-full object-contain"
          />
        </div>
        <div className="mt-3 flex items-center justify-between">
          <p className="text-xs text-slate-500">Original uploaded image.</p>
          <a
            href={url}
            download
            className="flex items-center gap-1 text-xs text-cyan-400 hover:text-cyan-300"
          >
            <Icon path={mdiDownload} size="14" />
            Download
          </a>
        </div>
      </CardBox>
    );
  }

  // ── PDF: embedded browser-native viewer ──────────────────────────
  if (isPdf) {
    const url = librarySourceEmbedUrl(library.collection);
    return (
      <CardBox>
        <div className="overflow-hidden rounded-lg border border-white/5 bg-black">
          <iframe
            src={url}
            title={library.name}
            className="h-[75vh] w-full"
          />
        </div>
        <div className="mt-3 flex items-center justify-between">
          <p className="text-xs text-slate-500">
            Original uploaded document.
          </p>
          <a
            href={url}
            download
            className="flex items-center gap-1 text-xs text-cyan-400 hover:text-cyan-300"
          >
            <Icon path={mdiDownload} size="14" />
            Download
          </a>
        </div>
      </CardBox>
    );
  }

  // ── Loading ───────────────────────────────────────────────────────
  if (loading) {
    return (
      <CardBox>
        <div className="flex items-center justify-center py-12">
          <Icon path={mdiLoading} size="24" className="animate-spin text-cyan-400" />
          <span className="ml-3 text-sm text-slate-400">Loading document...</span>
        </div>
      </CardBox>
    );
  }

  // ── Failed ────────────────────────────────────────────────────────
  if (failed || text === null) {
    return (
      <CardBox>
        <div className="py-8 text-center">
          <Icon
            path={mdiFileDocumentAlertOutline}
            size="36"
            className="mx-auto mb-3 text-amber-400"
          />
          <p className="text-sm text-slate-300">
            The source document is not available for this library.
          </p>
        </div>
      </CardBox>
    );
  }

  // ── Reconstructed text ────────────────────────────────────────────
  return (
    <CardBox>
      {note && (
        <div className="mb-4 flex items-start gap-2 rounded-lg border border-white/5 bg-white/[0.02] p-3">
          <Icon
            path={mdiInformationOutline}
            size="16"
            className="mt-0.5 flex-shrink-0 text-slate-500"
          />
          <p className="text-xs text-slate-400">{note}</p>
        </div>
      )}
      {text.trim() ? (
        <div className="kb-article-content whitespace-pre-wrap text-sm leading-relaxed text-slate-300">
          {text}
        </div>
      ) : (
        <p className="py-8 text-center text-sm text-slate-500">
          This document has no readable text content.
        </p>
      )}
    </CardBox>
  );
}
