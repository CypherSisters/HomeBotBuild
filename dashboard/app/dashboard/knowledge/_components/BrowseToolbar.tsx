"use client";

/**
 * The library viewer's address bar.
 *
 * A ZIM pack is a website, so the viewer needs the controls a browser has:
 * Home (the pack's own main page), Back, Forward, what you are looking at,
 * and a way into the title index. The kiosk is a touchscreen with no mouse
 * and no keyboard, so every control is at least 44px tall.
 */

import { mdiArrowLeft, mdiArrowRight, mdiHome, mdiFormatListBulleted } from "@mdi/js";
import Icon from "../../../_components/Icon";

const btn: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: 6,
  minHeight: 44,
  minWidth: 44,
  justifyContent: "center",
  padding: "0 12px",
  borderRadius: 10,
  border: "1px solid var(--line)",
  background: "var(--card2)",
  color: "var(--text2)",
  fontSize: "0.8rem",
  fontWeight: 500,
  cursor: "pointer",
};

export default function BrowseToolbar({
  title,
  hasHome,
  canBack,
  canForward,
  browseOpen,
  onHome,
  onBack,
  onForward,
  onToggleBrowse,
}: {
  title: string;
  /** False for packs with no main page: the button would go nowhere. */
  hasHome: boolean;
  canBack: boolean;
  canForward: boolean;
  browseOpen: boolean;
  onHome: () => void;
  onBack: () => void;
  onForward: () => void;
  onToggleBrowse: () => void;
}) {
  const dim = (on: boolean): React.CSSProperties =>
    on ? {} : { opacity: 0.35, cursor: "default" };

  return (
    <div
      className="kb-browse-toolbar"
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        flexWrap: "wrap",
        padding: "8px 10px",
        marginBottom: 12,
        borderRadius: 12,
        border: "1px solid var(--line)",
        background: "var(--card)",
      }}
    >
      {hasHome && (
        <button type="button" style={btn} onClick={onHome} title="Main page">
          <Icon path={mdiHome} size="18" />
        </button>
      )}
      <button
        type="button"
        style={{ ...btn, ...dim(canBack) }}
        onClick={canBack ? onBack : undefined}
        disabled={!canBack}
        title="Back"
      >
        <Icon path={mdiArrowLeft} size="18" />
      </button>
      <button
        type="button"
        style={{ ...btn, ...dim(canForward) }}
        onClick={canForward ? onForward : undefined}
        disabled={!canForward}
        title="Forward"
      >
        <Icon path={mdiArrowRight} size="18" />
      </button>

      <div
        style={{
          flex: "1 1 160px",
          minWidth: 0,
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
          color: "var(--text)",
          fontSize: "0.9rem",
          fontWeight: 600,
          padding: "0 4px",
        }}
        title={title}
      >
        {title}
      </div>

      <button
        type="button"
        style={{
          ...btn,
          ...(browseOpen
            ? { borderColor: "rgba(0,240,255,0.3)", color: "#e2e8f0", background: "rgba(0,240,255,0.08)" }
            : {}),
        }}
        onClick={onToggleBrowse}
        title="Browse the title index"
      >
        <Icon path={mdiFormatListBulleted} size="18" />
        Browse A-Z
      </button>
    </div>
  );
}
