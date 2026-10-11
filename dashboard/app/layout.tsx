import type { Metadata, Viewport } from "next";
import { Fredoka, Nunito, JetBrains_Mono } from "next/font/google";
import "../css/main.css";
import StoreProvider from "./_stores/StoreProvider";
import IdleMotionGuard from "./_components/IdleMotionGuard";
import RootStateGuard from "./_components/RootStateGuard";

// Design-system fonts: Fredoka for display,
// Nunito for body, JetBrains Mono for stats. next/font self-hosts the files at
// build time, so the shipped app never fetches fonts from the network.
const fredoka = Fredoka({ subsets: ["latin"], weight: ["400", "500", "600"], variable: "--font-fredoka" });
const nunito = Nunito({ subsets: ["latin"], variable: "--font-nunito" });
const jetbrainsMono = JetBrains_Mono({ subsets: ["latin"], variable: "--font-jbmono" });

const title = "PuffPAI — Privacy-First AI Dashboard";

const description = "PuffPAI: Privacy-first, offline-capable AI assistant for edge hardware by Cypher Sisters";

export const metadata: Metadata = {
  title,
  description,
  applicationName: "Puff",
  // Web App Manifest lives at app/manifest.ts (auto-linked by Next). These
  // fields make iOS "Add to Home Screen" open /send fullscreen like an app
  // with the Puff cloud icon — iOS ignores the manifest for install and reads
  // these apple-specific tags instead.
  appleWebApp: {
    capable: true,
    statusBarStyle: "black-translucent",
    title: "Puff",
  },
  icons: {
    icon: "/favicon.png",
    apple: "/puffpai-icon-180.png",
  },
};

// Without this the phone renders /send at a 980px desktop viewport and scales
// it down — tap targets end up unusable. App Router serves this as the
// <meta name="viewport"> tag. themeColor tints the iOS/Android status bar and
// standalone chrome to the dark galaxy background.
export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  themeColor: "#160B28",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <StoreProvider>
      {/* suppressHydrationWarning: data-puff-theme depends on the client's
          local clock, so the server can never render it to match. */}
      <html
        lang="en"
        className={`dark style-basic ${fredoka.variable} ${nunito.variable} ${jetbrainsMono.variable}`}
        suppressHydrationWarning
      >
        <body className={`aero-bg antialiased dark:text-slate-100`}>
          {/* Material Symbols Rounded ligature font (icons). React 19 hoists
              this <link> into <head>; preloading avoids a flash of raw
              ligature text ("mic", "settings") before the font arrives. */}
          <link
            rel="preload"
            href="/fonts/material-symbols-rounded.woff2"
            as="font"
            type="font/woff2"
            crossOrigin="anonymous"
          />
          {/* Theme first paint. The palette the owner (or the active
              household profile) picked is cached in localStorage by
              app/_lib/theme.ts, so this runs before hydration and never
              flashes the wrong ground; ThemeSync corrects it if the active
              profile disagrees. Absent a cache: Cloud Pink 07–19h, Deep
              Plum otherwise. QA override: ?puffTheme=day|plum|light pins it
              (‘night’ is the retired palette and normalises to plum);
              ?puffTheme=auto returns to the clock.
              Display mode: ?puffMode=kiosk pins html.puff-kiosk (persists via
              localStorage, same as the Settings "Kiosk display" toggle) — the
              motion governor in css/_theme.css then keeps the scene static so
              an always-on appliance screen never pins the GPU;
              ?puffMode=normal clears it. Applied here, pre-hydration, so a
              kiosk boot never plays a single animated frame. */}
          <script
            dangerouslySetInnerHTML={{
              __html:
                "(function(){function n(v){if(v==='day'||v==='plum'||v==='light'||v==='auto')return v;if(v==='night')return'plum';return null;}function s(){try{var q=n(new URLSearchParams(location.search).get('puffTheme'));if(q==='auto'){localStorage.removeItem('puff-theme-override');}else if(q){localStorage.setItem('puff-theme-override',q);}var o=n(localStorage.getItem('puff-theme-override'));if(o&&o!=='auto'){document.documentElement.setAttribute('data-puff-theme',o);return;}}catch(e){}var h=new Date().getHours();document.documentElement.setAttribute('data-puff-theme',(h>=7&&h<19)?'day':'plum');}s();setInterval(s,600000);try{var r=document.documentElement;var m=new URLSearchParams(location.search).get('puffMode');if(m==='kiosk'){localStorage.setItem('puffpai_display_mode','kiosk');}else if(m==='normal'||m==='auto'){localStorage.removeItem('puffpai_display_mode');}r.classList.toggle('puff-kiosk',localStorage.getItem('puffpai_display_mode')==='kiosk');r.classList.toggle('puff-big-touch',localStorage.getItem('puffpai_big_touch')==='1');r.classList.toggle('puff-no-motion',localStorage.getItem('puffpai_ambient_motion')==='0');}catch(e){}})();",
            }}
          />
          {/* Hidden-tab rAF shim. Chrome never fires requestAnimationFrame
              in hidden/background tabs, but React schedules its streamed-
              content reveal ($RC/$RV) and Suspense retries through rAF —
              so any page loaded while the tab is not visible sits on its
              loading skeleton forever (kiosk displays, background tabs,
              minimized windows). While hidden, route rAF through a short
              setTimeout; visible tabs keep native behavior. Must run
              before Next's runtime scripts, hence inline at body start. */}
          <script
            dangerouslySetInnerHTML={{
              __html:
                "(function(){var raf=window.requestAnimationFrame.bind(window);var caf=window.cancelAnimationFrame.bind(window);var shim=new Set();window.requestAnimationFrame=function(cb){if(document.visibilityState!=='hidden')return raf(cb);var id=window.setTimeout(function(){shim.delete(id);cb(performance.now());},50);shim.add(id);return id;};window.cancelAnimationFrame=function(id){if(shim.has(id)){shim.delete(id);window.clearTimeout(id);return;}caf(id);};})();",
            }}
          />
          {/* Top cloud banner — fixed, fades to transparent,
              behind all content; styled as .puff-cloud-banner in _theme.css.
              A real element (not body::before) so night keeps its starfield. */}
          <div className="puff-cloud-banner" aria-hidden />
          {/* Pauses the ambient animation loops after a few minutes without
              input (voice counts as presence) — see the motion governor in
              css/_theme.css and the GPU story in IdleMotionGuard itself. */}
          <IdleMotionGuard />
          {/* Re-applies data-puff-theme + the display-mode classes after
              hydration strips them off <html>, and moves the auto theme across
              the 07:00 / 19:00 boundary without a reload. */}
          <RootStateGuard />
          {children}
        </body>
      </html>
    </StoreProvider>
  );
}
