"use client";

/**
 * /dashboard/media — the media hub.
 *
 * Was the Jellyfin embed itself; Jellyfin moved down to
 * /dashboard/media/jellyfin so this route can be a launcher for everything the
 * household actually listens to. The persistent JellyfinFrame overlay now
 * matches only the deeper route, which is what keeps this page visible.
 *
 * Three launchers, then the native World Radio player inline below them.
 * Spotify is an account-bound web player with framing-hostile headers and no
 * embeddable surface. On a phone/laptop its tile opens the real site in a new
 * tab (data-external="allow", the documented opt-out from the link-containment
 * overlay — which only extracts plain ARTICLE text and so fails on a JS web app
 * with "no readable article text"). On the wall kiosk there is no escapable
 * browser to open into, so there those tiles instead surface how to use the
 * service here: voice, and — for Spotify — casting from a phone.
 *
 * puff puff pass
 */
import { useEffect, useState } from "react";
import Link from "next/link";
import SectionMain from "@/app/_components/Section/Main";
import MsIcon from "@/app/_components/MsIcon";
import { FREDOKA, NUNITO } from "@/app/_components/Widgets/DesignTile";
import WorldRadio from "@/app/dashboard/_components/WorldRadio";
import TvCastPanel from "@/app/dashboard/_components/TvCastPanel";

const TILE_CLASS =
  "group relative flex h-full cursor-pointer flex-col justify-between overflow-hidden rounded-[28px] border p-5 text-left backdrop-blur-[18px] transition-transform hover:-translate-y-0.5";

const tileStyle = {
  borderColor: "var(--line)",
  background: "var(--card)",
} as const;

function TileBody({
  icon,
  color,
  title,
  subtitle,
  external,
}: {
  icon: string;
  color: string;
  title: string;
  subtitle: string;
  external?: boolean;
}) {
  return (
    <>
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src="/design-cloud-3.jpg"
        alt=""
        aria-hidden
        className="pointer-events-none absolute inset-0 h-full w-full object-cover"
        style={{ opacity: "var(--imgTile)" }}
      />
      <div className="relative flex h-full flex-col gap-3">
        <div className="flex items-start">
          <span
            className="grid h-11 w-11 place-items-center rounded-2xl"
            style={{ background: "var(--chip)", color }}
          >
            <MsIcon name={icon} size={24} fill />
          </span>
          <div className="flex-1" />
          {external && (
            <MsIcon
              name="open_in_new"
              size={16}
              style={{ color: "var(--text3)" }}
            />
          )}
        </div>
        <div className="mt-auto">
          <div
            style={{
              fontFamily: FREDOKA,
              fontWeight: 600,
              fontSize: 19,
              color: "var(--text)",
            }}
          >
            {title}
          </div>
          <div
            style={{
              fontFamily: NUNITO,
              fontWeight: 500,
              fontSize: 13,
              color: "var(--text2)",
              lineHeight: 1.35,
            }}
          >
            {subtitle}
          </div>
        </div>
      </div>
    </>
  );
}

/** The "opens a real browser tab" tile. Shared by the web-player tiles and by
 *  Books on a phone/laptop, so the containment opt-out and the open_in_new
 *  marker are written down once.
 *
 *  `href` may be undefined for one render: hrefs built from
 *  window.location.hostname are only known after mount, and SSR and the first
 *  client render have to agree. Until then the tile renders as an inert div —
 *  same pixels, no dead link. */
function ExternalTileAnchor({
  href,
  icon,
  color,
  title,
  subtitle,
}: {
  href?: string;
  icon: string;
  color: string;
  title: string;
  subtitle: string;
}) {
  const body = (
    <TileBody icon={icon} color={color} title={title} subtitle={subtitle} external />
  );
  if (!href) {
    return (
      <div className={TILE_CLASS} style={tileStyle}>
        {body}
      </div>
    );
  }
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      // Opt out of the link-containment reader: these are JS web apps, not
      // articles, so open the real site in a real browser tab.
      data-external="allow"
      className={TILE_CLASS}
      style={tileStyle}
    >
      {body}
    </a>
  );
}

/** A launcher for an external web player. On a real browser it opens the site
 *  in a new tab via the containment opt-out; on the chrome-less appliance kiosk
 *  a new tab is an inescapable dead end, so there it surfaces a how-to hint
 *  instead of navigating. */
function ExternalServiceTile({
  isAppliance,
  icon,
  color,
  title,
  subtitle,
  href,
  onApplianceTap,
}: {
  isAppliance: boolean;
  icon: string;
  color: string;
  title: string;
  subtitle: string;
  href: string;
  onApplianceTap: () => void;
}) {
  if (isAppliance) {
    return (
      <button
        type="button"
        onClick={onApplianceTap}
        className={TILE_CLASS}
        style={tileStyle}
      >
        <TileBody icon={icon} color={color} title={title} subtitle={subtitle} />
      </button>
    );
  }
  return (
    <ExternalTileAnchor
      href={href}
      icon={icon}
      color={color}
      title={title}
      subtitle={subtitle}
    />
  );
}

export default function MediaPage() {
  // The wall kiosk (Chromium --kiosk, no browser chrome) is the only place a
  // new tab can't be escaped; a phone in kiosk DISPLAY mode still has chrome, so
  // gate on the appliance's own loopback origin, not the class alone.
  const [isAppliance, setIsAppliance] = useState(false);
  const [hint, setHint] = useState<string | null>(null);
  /** "Cast to a TV": the same sheet as the TV bar's Cast to..., full screen here. */
  const [castOpen, setCastOpen] = useState(false);
  /** Calibre-web as this browser can reach it. Null until mount (window). */
  const [booksHref, setBooksHref] = useState<string | undefined>(undefined);
  useEffect(() => {
    const host = window.location.hostname;
    setIsAppliance(
      document.documentElement.classList.contains("puff-kiosk") &&
        (host === "localhost" || host === "127.0.0.1" || host === "::1"),
    );
    setBooksHref(`http://${host}:8083`);
  }, []);

  /** World Radio isn't a route — bring the inline player into view and arm the
   *  search box so a tap lands somewhere useful. */
  const focusRadio = () => {
    const section = document.getElementById("world-radio");
    section?.scrollIntoView({ behavior: "smooth", block: "start" });
    document.getElementById("world-radio-search")?.focus({ preventScroll: true });
  };

  return (
    <SectionMain>
      {hint && (
        <div
          role="status"
          className="mb-4 flex items-start gap-2 rounded-2xl px-4 py-3"
          style={{
            background: "var(--card2)",
            border: "1px solid var(--line)",
            fontFamily: NUNITO,
            fontWeight: 500,
            fontSize: 13.5,
            lineHeight: 1.45,
            color: "var(--text2)",
          }}
        >
          <MsIcon name="info" size={18} style={{ color: "var(--sky)", flex: "none" }} />
          <span className="flex-1">{hint}</span>
          <button
            type="button"
            onClick={() => setHint(null)}
            aria-label="Dismiss"
            style={{ color: "var(--text3)", cursor: "pointer" }}
          >
            <MsIcon name="close" size={18} />
          </button>
        </div>
      )}
      <div className="mb-4 grid grid-cols-1 gap-3.5 sm:grid-cols-2 xl:grid-cols-4 2xl:grid-cols-7">
        {/* ?view=home: the tile always lands on Jellyfin's home page, even if
            the frame was last left on Live TV (see JellyfinFrame). */}
        <Link
          href="/dashboard/media/jellyfin?view=home"
          className={TILE_CLASS}
          style={tileStyle}
        >
          <TileBody
            icon="movie"
            color="var(--pink)"
            title="Jellyfin"
            subtitle="Movies · TV · Music library"
          />
        </Link>

        {/* Live TV is Jellyfin's own Live TV page in the same persistent
            frame (see JellyfinFrame's ?view=livetv). */}
        <Link
          href="/dashboard/media/jellyfin?view=livetv"
          className={TILE_CLASS}
          style={tileStyle}
        >
          <TileBody
            icon="live_tv"
            color="var(--magenta)"
            title="Live TV"
            subtitle="Your own channels · free news, sports, kids and more"
          />
        </Link>

        {/* Cast to a TV: movies, shows, music and channels from Jellyfin on the
            box, sent to an Apple TV or Roku linked in Home Assistant. Jellyfin's
            own Cast button is Google Cast only, and the house has none. */}
        <button type="button" onClick={() => setCastOpen(true)} className={TILE_CLASS} style={tileStyle}>
          <TileBody
            icon="cast"
            color="var(--pink)"
            title="Cast to a TV"
            subtitle="Movies, shows, music and channels on your TVs"
          />
        </button>

        <ExternalServiceTile
          isAppliance={isAppliance}
          icon="graphic_eq"
          color="var(--mint)"
          title="Spotify"
          subtitle="Cast to “PuffPAI” in the app, or say “play…”. Premium required."
          href="https://open.spotify.com"
          onApplianceTap={() =>
            setHint(
              "On this screen, use Spotify by casting from your phone (pick “PuffPAI” in the app) or by saying “hey PuffPAI, play…”. The Spotify web player only opens on a phone or laptop.",
            )
          }
        />

        <button type="button" onClick={focusRadio} className={TILE_CLASS} style={tileStyle}>
          <TileBody
            icon="radio"
            color="var(--sky)"
            title="World Radio"
            subtitle="Live stations, no account"
          />
        </button>

        {/* Books is the one tile whose destination differs by device rather
            than by dead-end risk: the kiosk gets the framed page (the loopback
            books-proxy strips Calibre-web's X-Frame-Options), while a
            phone/laptop cannot frame it at all and goes straight to the app. */}
        {isAppliance ? (
          <Link href="/dashboard/media/books" className={TILE_CLASS} style={tileStyle}>
            <TileBody
              icon="menu_book"
              color="var(--lemon)"
              title="Books"
              subtitle="Your shelf · Project Gutenberg"
            />
          </Link>
        ) : (
          <ExternalTileAnchor
            href={booksHref}
            icon="menu_book"
            color="var(--lemon)"
            title="Books"
            subtitle="Your shelf · Project Gutenberg"
          />
        )}
      </div>

      <WorldRadio />

      {castOpen && (
        <div className="fixed inset-0 z-50 p-2 sm:p-6" style={{ background: "rgba(0,0,0,0.55)" }}>
          <div className="relative h-full w-full">
            <TvCastPanel onClose={() => setCastOpen(false)} />
          </div>
        </div>
      )}
    </SectionMain>
  );
}
