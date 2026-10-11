"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import MsIcon from "../../_components/MsIcon";
import {
  checkAvatarFile,
  clearAvatar,
  getIngestImages,
  setAvatarFromUpload,
  thumbSrc,
  uploadAvatarFile,
  type IngestImage,
} from "../../_lib/services/householdService";

/**
 * Two ways to give somebody a face, because the appliance holds pictures the
 * computer does not.
 *
 * The old control was a template FormFilePicker: it put the chosen File in
 * React state and the form submit sent only name/email, so picking a photo
 * from Downloads did nothing at all. This one posts. "This device" is a real
 * multipart POST; "Puff's files" lists what has already been sent to the box
 * (Send to Puff, the documents page) and picks by upload id — the kiosk has no
 * file manager, so that is the only route that works on the wall display.
 */

export type AvatarPickerProps = {
  /** Member id, or "profile" for the owner alias. */
  target: string;
  /** Used for the initials placeholder and the alt text. */
  name: string;
  /** Ready-to-render, cache-busted src, or null when there is no picture. */
  src: string | null;
  size?: number;
  /** Re-read the roster so every surface picks the new picture up. */
  onChanged: () => void | Promise<void>;
};

export function initialsOf(name: string): string {
  const words = name.trim().split(/\s+/).filter(Boolean);
  if (words.length === 0) return "??";
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return (words[0][0] + words[words.length - 1][0]).toUpperCase();
}

const actionStyle: React.CSSProperties = {
  font: "600 12px var(--font-nunito), sans-serif",
  color: "var(--text2)",
  background: "var(--card)",
  border: "1px solid var(--line)",
  borderRadius: 10,
  padding: "5px 10px",
};

export default function AvatarPicker({
  target,
  name,
  src,
  size = 64,
  onChanged,
}: AvatarPickerProps) {
  const fileRef = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [picking, setPicking] = useState(false);

  const run = useCallback(
    async (fn: () => Promise<unknown>) => {
      setBusy(true);
      setError(null);
      try {
        await fn();
        await onChanged();
      } catch (err: any) {
        setError(err?.message || "That didn't work.");
      } finally {
        setBusy(false);
      }
    },
    [onChanged],
  );

  const onFile = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = ""; // so picking the same file twice still fires
    if (!file) return;
    const bad = checkAvatarFile(file);
    if (bad) {
      setError(bad);
      return;
    }
    run(() => uploadAvatarFile(target, file));
  };

  return (
    <div className="flex items-center" style={{ gap: 12 }}>
      <div
        className="grid flex-none place-items-center overflow-hidden"
        style={{
          width: size,
          height: size,
          borderRadius: 14,
          background: src
            ? "var(--card)"
            : "linear-gradient(140deg, var(--pink), var(--lav))",
          border: "1px solid var(--line)",
          font: `800 ${Math.round(size / 3)}px var(--font-nunito), sans-serif`,
          color: "#fff",
          opacity: busy ? 0.5 : 1,
        }}
      >
        {src ? (
          /* eslint-disable-next-line @next/next/no-img-element */
          <img
            src={src}
            alt={`${name} picture`}
            width={size}
            height={size}
            style={{ width: size, height: size, objectFit: "cover" }}
          />
        ) : (
          initialsOf(name)
        )}
      </div>

      <div className="flex flex-col" style={{ gap: 6 }}>
        <div className="flex flex-wrap items-center" style={{ gap: 6 }}>
          <input
            ref={fileRef}
            type="file"
            accept="image/jpeg,image/png,image/webp"
            onChange={onFile}
            className="hidden"
          />
          <button
            type="button"
            disabled={busy}
            onClick={() => fileRef.current?.click()}
            className="flex cursor-pointer items-center gap-1.5"
            style={actionStyle}
          >
            <MsIcon name="upload" size={15} style={{ color: "var(--sky)" }} />
            {busy ? "Working…" : "This device"}
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => setPicking(true)}
            className="flex cursor-pointer items-center gap-1.5"
            style={actionStyle}
          >
            <MsIcon name="photo_library" size={15} style={{ color: "var(--pink)" }} />
            Puff&rsquo;s files
          </button>
          {src && (
            <button
              type="button"
              disabled={busy}
              onClick={() => run(() => clearAvatar(target))}
              className="cursor-pointer"
              style={actionStyle}
              aria-label={`Remove ${name} picture`}
            >
              Remove
            </button>
          )}
        </div>
        {error ? (
          <div style={{ font: "600 12px var(--font-nunito), sans-serif", color: "var(--pink)" }}>
            {error}
          </div>
        ) : (
          <div style={{ font: "500 11px var(--font-nunito), sans-serif", color: "var(--text2)" }}>
            JPEG, PNG or WebP &middot; up to 8 MB
          </div>
        )}
      </div>

      {picking && (
        <PuffFilesModal
          onClose={() => setPicking(false)}
          onPick={(uploadId) => {
            setPicking(false);
            run(() => setAvatarFromUpload(target, uploadId));
          }}
        />
      )}
    </div>
  );
}

/** What to call a picture: Puff's label, else the raw file name. */
function imageLabel(img: IngestImage): string {
  return (img.title || "").trim() || img.filename;
}

/** Grid of images already on the appliance. Thumbnails only — ids do the work. */
function PuffFilesModal({
  onClose,
  onPick,
}: {
  onClose: () => void;
  onPick: (uploadId: string) => void;
}) {
  const [images, setImages] = useState<IngestImage[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getIngestImages()
      .then((list) => {
        if (!cancelled) setImages(list);
      })
      .catch((err: any) => {
        if (!cancelled) setError(err?.message || "Couldn't read Puff's files");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-[120] grid place-items-center p-4"
      style={{ background: "rgba(5,6,20,0.72)" }}
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label="Choose a picture from Puff's files"
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="w-full max-w-2xl overflow-hidden"
        style={{
          borderRadius: 18,
          border: "1px solid var(--line)",
          background: "var(--card)",
          backdropFilter: "blur(18px)",
          WebkitBackdropFilter: "blur(18px)",
          boxShadow: "var(--shadow)",
        }}
      >
        <div
          className="flex items-center justify-between px-4 py-3"
          style={{ borderBottom: "1px solid var(--line)" }}
        >
          <div style={{ font: "800 15px var(--font-nunito), sans-serif", color: "var(--text)" }}>
            Puff&rsquo;s files
          </div>
          <button
            type="button"
            onClick={onClose}
            className="cursor-pointer"
            aria-label="Close"
            style={{ color: "var(--text2)" }}
          >
            <MsIcon name="close" size={20} />
          </button>
        </div>

        <div className="max-h-[60vh] overflow-y-auto p-3">
          {error ? (
            <p style={{ font: "600 13px var(--font-nunito), sans-serif", color: "var(--pink)" }}>
              {error}
            </p>
          ) : !images ? (
            <p style={{ font: "600 13px var(--font-nunito), sans-serif", color: "var(--text2)" }}>
              Loading&hellip;
            </p>
          ) : images.length === 0 ? (
            <p style={{ font: "600 13px var(--font-nunito), sans-serif", color: "var(--text2)" }}>
              No pictures yet &mdash; send one to Puff from your phone first.
            </p>
          ) : (
            <div className="grid grid-cols-3 gap-2 sm:grid-cols-5">
              {images.map((img) => (
                <button
                  key={img.upload_id}
                  type="button"
                  onClick={() => onPick(img.upload_id)}
                  title={imageLabel(img)}
                  className="cursor-pointer text-left"
                  style={{ minWidth: 0 }}
                >
                  <span
                    className="block overflow-hidden"
                    style={{
                      borderRadius: 12,
                      border: "1px solid var(--line)",
                      aspectRatio: "1 / 1",
                      background: "var(--card2)",
                    }}
                  >
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    <img
                      src={thumbSrc(img)}
                      alt={imageLabel(img)}
                      style={{ width: "100%", height: "100%", objectFit: "cover" }}
                    />
                  </span>
                  {/* Puff's label for the picture, so a grid of phone photos
                      isn't a wall of IMG_0421-shaped nothing. */}
                  <span
                    className="mt-1 block truncate"
                    style={{
                      font: "600 11px var(--font-nunito), sans-serif",
                      color: "var(--text2)",
                    }}
                  >
                    {imageLabel(img)}
                  </span>
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
