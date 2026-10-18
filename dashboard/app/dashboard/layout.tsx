"use client";

import { ReactNode, useEffect, useState } from "react";
import menuAside from "./_lib/menuAside";
import { withPersonaNav } from "../_lib/personaNav";
import { getOwnerApps, type OwnerApp } from "../_lib/services/personaService";
import { onBuildChanged } from "../_lib/services/buildEvents";
import NavBar from "./_components/NavBar";
import AsideMenu from "./_components/AsideMenu";
import FooterBar from "./_components/FooterBar";
import AnnouncementToast from "../_components/AnnouncementToast";
import PairPrompt from "./_components/PairPrompt";
import VoiceLiveSync from "../_lib/hooks/useVoiceLive";
import DeviceDiscoveryToast from "../_components/DeviceDiscoveryToast";
import ExternalLinkGuard from "../_components/ExternalLinkGuard";
import OnboardingGate from "../_components/OnboardingGate";
import JellyfinFrame from "./_components/JellyfinFrame";
import KioskMediaBridge from "./_components/KioskMediaBridge";
import FloatingPuff from "./_components/FloatingPuff";
import HouseholdSync from "./_components/HouseholdSync";
import ThemeSync from "./_components/ThemeSync";
import ShowViewer from "./_components/ShowViewer";
import VaultPinPrompt from "../_components/VaultPinPrompt";
import { VoiceStopBar } from "./_components/VoiceStop";
import {
  installAudioUnlockListeners,
  requestNotificationPermission,
} from "../_lib/audioUnlock";

type Props = {
  children: ReactNode;
};

/**
 * App shell: full-height flex row —
 * glass rail (AsideMenu) + main column (topbar NavBar, scrollable content).
 *
 * The content wrapper is the scroll container (not the body): legacy pages
 * scroll under a always-visible topbar, and the dashboard's kiosk grid can
 * fill the remaining height with its own internal scrolling.
 * Below lg the rail becomes an off-canvas drawer behind the hamburger.
 */
export default function LayoutAuthenticated({ children }: Props) {
  const [isAsideMobileExpanded, setIsAsideMobileExpanded] = useState(false);

  // The personas the owner built get a rail entry each, named after the bot,
  // straight after Dashboard. Without this a scaffolded bot's only home is the
  // Apps submenu, which this rail does not draw — the owner could not find the
  // thing this build exists to be. Null until the box answers, and null again
  // if it refuses: the built-in menu is what shows either way, never an
  // invented entry. Refetched when something installs or removes an app,
  // because the rail is mounted above every page and never remounts on its own.
  const [ownerApps, setOwnerApps] = useState<OwnerApp[] | null>(null);
  useEffect(() => {
    let cancelled = false;
    const load = () => {
      getOwnerApps().then((apps) => {
        if (!cancelled) setOwnerApps(apps);
      });
    };
    load();
    const off = onBuildChanged(load);
    return () => {
      cancelled = true;
      off();
    };
  }, []);
  const menu = withPersonaNav(menuAside, ownerApps);

  // EMBED MODE: the "show me X" overlay (ShowViewer) frames a /dashboard/* route
  // in a full-screen iframe. That route runs THIS same layout, so without a
  // guard it would re-draw the rail, the topbar, JellyfinFrame — and another
  // ShowViewer, which frames the route again: infinite nesting (visible as
  // stacked overlays once frame-ancestors allowed the frame at all). When we are
  // framed we render ONLY the page content: clean full-bleed, no chrome, no
  // recursion. The only time a dashboard page is framed is this overlay, so
  // "am I in an iframe" is a precise signal. Resolved after mount so SSR and the
  // first client render agree (avoids a hydration mismatch); the brief chrome
  // flash inside the frame is sub-100ms on the kiosk.
  const [embedded, setEmbedded] = useState(false);
  useEffect(() => {
    try {
      setEmbedded(window.self !== window.top);
    } catch {
      // Cross-origin parent throws on access → we are definitely framed.
      setEmbedded(true);
    }
  }, []);

  // Unlock the audio context on first user interaction (browser autoplay
  // policy) and request notification permission for alarm reminders.
  useEffect(() => {
    installAudioUnlockListeners();
    requestNotificationPermission();
  }, []);

  // QA hook: ?puffAside=1 opens the mobile drawer on load so headless
  // screenshot runs can verify it (mirrors the ?puffTheme override).
  useEffect(() => {
    try {
      if (new URLSearchParams(window.location.search).has("puffAside")) {
        setIsAsideMobileExpanded(true);
      }
    } catch {
      /* no-op */
    }
  }, []);

  // Framed by the show-me overlay: content only. No rail, no topbar, no
  // JellyfinFrame/KioskMediaBridge/FloatingPuff, and no nested ShowViewer.
  if (embedded) {
    return <div className="h-dvh overflow-y-auto">{children}</div>;
  }

  // First-run: if the backend says setup was never completed, the shell is
  // replaced by a redirect to /onboarding. Wrapped around the chrome rather
  // than the content so a first-run box never paints a rail and a topbar over
  // a bot that does not exist yet. The embedded (show-me overlay) branch above
  // returns before this on purpose — a framed route must never redirect the
  // frame into the wizard.
  return (
    <OnboardingGate>
    <div className="flex h-dvh overflow-hidden">
      <AsideMenu
        menu={menu}
        isAsideMobileExpanded={isAsideMobileExpanded}
        onRouteChange={() => setIsAsideMobileExpanded(false)}
        onOverlayClick={() => setIsAsideMobileExpanded(false)}
      />

      <div className="flex min-w-0 flex-1 flex-col">
        <NavBar
          isMobileAsideOpen={isAsideMobileExpanded}
          onToggleMobileAside={() => setIsAsideMobileExpanded((v) => !v)}
        />
        {/* data-puff-content: JellyfinFrame measures this box and pins
            itself to it, so the frame can never cover the topbar or rail. */}
        <div className="min-h-0 flex-1 overflow-y-auto" data-puff-content>
          {children}
          <FooterBar />
        </div>
      </div>

      {/* Renders nothing: pushes the household owner into the Redux store, so
          the welcome card and the legacy nav avatar stop showing the
          template's "PuffPAI User" placeholder. */}
      <HouseholdSync />
      {/* Makes this screen wear the active profile’s palette. */}
      <ThemeSync />
      {/* Global toast that fires when a new device lands in the pending
          queue. Mounted at the layout level so it shows on every page. */}
      <DeviceDiscoveryToast />
      {/* Proactive announcements (backend announcer queue): toast on every
          page, and entries the voice satellite did not already speak are
          said out loud here through the shared TTS path. */}
      <AnnouncementToast />
      <PairPrompt />
      {/* Renders nothing: polls /voice/live and puts the turn happening AT THE
          BOX (wake word, no browser involved) into the store, so the
          transcript tile and the Talk page can show it as it is spoken.
          Mounted once, here — it is one poller for the whole app. */}
      <VoiceLiveSync />
      {/* Renders nothing until a link tries to leave the app. On the KIOSK
          (chromium --kiosk: no tabs, no back button) a target="_blank" is a
          dead end with no way out, so every external link is intercepted at
          the document in the capture phase and shown in an escapable overlay
          instead. Mounted here so it covers every dashboard page, including
          links that only exist at runtime inside chat answers and RSS items. */}
      <ExternalLinkGuard />
      {/* Persistent Jellyfin embed. Lives in the layout (not the media route)
          so it survives navigation within /dashboard — keeping music playing
          and the player registered as an active cast target on every page. */}
      <JellyfinFrame />
      {/* Renders nothing; on the KIOSK browser only, it executes radio
          commands the backend/LLM issues and reports what the appliance's
          speakers are actually doing. Mounted beside JellyfinFrame because it
          needs the same never-unmounted lifetime. */}
      <KioskMediaBridge />
      {/* Puff herself — floating, draggable, on every page (except the full
          avatar page, which owns the stage). */}
      <FloatingPuff />
      {/* "Pull it up on screen": voice ("show me the TED talk about
          surveillance") queues an item at /dashboard/display; this overlay
          polls that and fills the screen with the deep-linked knowledge
          viewer. Layout-level so it works on every kiosk page. */}
      <ShowViewer />
      {/* Tap to stop. On the wall display only, and only while she
          is speaking: a full-width bar across the bottom whose whole area is
          the button. Layout-level so it is there on every kiosk page — the
          owner should never have to be on the right page to shut her up. */}
      <VoiceStopBar />
      {/* The Vault PIN, asked where the owner is, when a change to the build
          needs the Vault open (app/_lib/vaultSession.ts). */}
      <VaultPinPrompt />
    </div>
    </OnboardingGate>
  );
}
