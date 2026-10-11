"use client";

import React from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import MsIcon from "../../../_components/MsIcon";
import OverlayLayer from "../../../_components/OverlayLayer";
import { MenuAsideItem } from "../../../_interfaces";

type Props = {
  menu: MenuAsideItem[];
  isAsideMobileExpanded: boolean;
  onRouteChange: () => void;
  onOverlayClick: () => void;
};

/** Active-route check: exact for the dashboard root, prefix for subpages. */
function isActiveHref(href: string | undefined, pathname: string): boolean {
  if (!href) return false;
  if (href === "/dashboard") return pathname === "/dashboard";
  return pathname === href || pathname.startsWith(`${href}/`);
}

/** Primary nav button: 46-56px, radius 16, Nunito 600. */
function RailPrimaryItem({
  item,
  pathname,
  onRouteChange,
}: {
  item: MenuAsideItem;
  pathname: string;
  onRouteChange: () => void;
}) {
  const active = isActiveHref(item.href, pathname);
  return (
    <Link
      href={item.href ?? "#"}
      onClick={onRouteChange}
      className="flex flex-none items-center overflow-hidden whitespace-nowrap transition-colors"
      style={{
        gap: 14,
        minHeight: 46,
        height: "clamp(46px,4.6vh,56px)",
        padding: "0 16px",
        borderRadius: 16,
        font: "600 clamp(14px,1.05vw,17px) var(--font-nunito), sans-serif",
        background: active
          ? "linear-gradient(140deg, var(--pink), var(--magenta))"
          : "transparent",
        color: active ? "#fff" : "var(--text2)",
        boxShadow: active ? "0 10px 26px var(--glow1)" : "none",
      }}
    >
      {item.msIcon && <MsIcon name={item.msIcon} size={25} />}
      {item.label}
    </Link>
  );
}

/** Secondary nav row: dimmed link; active row lights up. */
function RailSecondaryItem({
  item,
  pathname,
  onRouteChange,
}: {
  item: MenuAsideItem;
  pathname: string;
  onRouteChange: () => void;
}) {
  const active = isActiveHref(item.href, pathname);
  return (
    <Link
      href={item.href ?? "#"}
      onClick={onRouteChange}
      className="flex flex-none items-center overflow-hidden whitespace-nowrap transition-opacity"
      style={{
        gap: 14,
        minHeight: 36,
        height: "clamp(36px,4vh,48px)",
        padding: "0 16px",
        borderRadius: 16,
        font: "600 clamp(13px,1vw,16px) var(--font-nunito), sans-serif",
        opacity: active ? 1 : 0.62,
        background: active
          ? "linear-gradient(140deg, var(--pink), var(--magenta))"
          : "transparent",
        color: active ? "#fff" : "var(--text2)",
        boxShadow: active ? "0 10px 26px var(--glow1)" : "none",
      }}
    >
      {item.msIcon && <MsIcon name={item.msIcon} size={23} />}
      {item.label}
    </Link>
  );
}

/** SECURITY ZONE card — links to /dashboard/firewall.
 *  Highlighted segment is static Zone 1 (there is no backend zone state). */
function SecurityZoneCard({ onRouteChange }: { onRouteChange: () => void }) {
  return (
    <Link
      href="/dashboard/firewall"
      onClick={onRouteChange}
      className="block flex-none transition-transform hover:scale-[1.015]"
      style={{
        marginTop: 16,
        padding: 16,
        borderRadius: 20,
        background: "var(--card)",
        border: "1px solid var(--line)",
        color: "var(--text)",
      }}
    >
      <div
        style={{
          font: "700 11px/1 var(--font-jbmono), monospace",
          letterSpacing: ".14em",
          color: "var(--text3)",
          marginBottom: 12,
        }}
      >
        SECURITY ZONE · AUTO
      </div>
      <div className="flex" style={{ gap: 6, marginBottom: 12 }}>
        <div
          className="flex-1"
          style={{
            height: 8,
            borderRadius: 99,
            background: "var(--magenta)",
            boxShadow: "0 0 14px var(--magenta)",
          }}
        />
        <div
          className="flex-1"
          style={{ height: 8, borderRadius: 99, background: "var(--sky)", opacity: 0.28 }}
        />
        <div
          className="flex-1"
          style={{ height: 8, borderRadius: 99, background: "var(--lemon)", opacity: 0.16 }}
        />
      </div>
      <div className="flex items-center" style={{ gap: 9 }}>
        <MsIcon name="shield_lock" size={20} style={{ color: "var(--magenta)" }} />
        <div>
          <div style={{ font: "700 15px var(--font-nunito), sans-serif", lineHeight: 1.1 }}>
            Zone 1 · Vault
          </div>
          <div style={{ font: "500 12px var(--font-nunito), sans-serif", color: "var(--text3)" }}>
            Nothing leaves without your consent
          </div>
        </div>
      </div>
    </Link>
  );
}

/**
 * The navigation rail. Static glass column on
 * lg+; slide-in overlay drawer below lg (small screens must still navigate).
 */
export default function AsideMenu({
  menu,
  isAsideMobileExpanded = false,
  onRouteChange,
  onOverlayClick,
}: Props) {
  const pathname = usePathname();

  const primary = menu.filter((m) => m.railGroup === "primary");
  const secondary = menu.filter((m) => m.railGroup === "secondary");
  const settings = menu.filter((m) => m.railGroup === "settings");

  return (
    <>
      <aside
        className={`fixed inset-y-0 z-50 flex w-[264px] flex-col transition-all duration-300 lg:static lg:z-auto lg:h-full lg:w-[clamp(212px,15vw,264px)] lg:flex-none ${
          isAsideMobileExpanded ? "left-0" : "-left-[264px] lg:left-0"
        }`}
        style={{
          background: "var(--railBg)",
          backdropFilter: "blur(22px)",
          WebkitBackdropFilter: "blur(22px)",
          borderRight: "1px solid var(--line)",
          padding: "26px 18px 20px",
        }}
      >
        {/* Brand row */}
        <div className="flex flex-wrap items-center" style={{ gap: 12, padding: "0 8px 22px" }}>
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src="/puffpai-logo.png"
            alt="PuffPAI"
            style={{
              width: "clamp(40px,3vw,54px)",
              height: "clamp(40px,3vw,54px)",
              objectFit: "contain",
              filter: "drop-shadow(0 0 16px var(--glow1))",
              animation: "puffFloat 6s ease-in-out infinite",
            }}
          />
          <div
            className="whitespace-nowrap"
            style={{
              font: "600 clamp(19px,1.5vw,27px) var(--font-fredoka), sans-serif",
              letterSpacing: "-.3px",
              color: "var(--text)",
            }}
          >
            Puff<span style={{ color: "var(--pink)" }}>PAI</span>
          </div>
        </div>

        {/* Nav column */}
        <nav className="flex min-h-0 flex-1 flex-col overflow-y-auto" style={{ gap: 2 }}>
          {primary.map((item) => (
            <RailPrimaryItem
              key={item.href}
              item={item}
              pathname={pathname}
              onRouteChange={onRouteChange}
            />
          ))}

          <div style={{ height: 1, background: "var(--line)", margin: "10px 12px", flex: "none" }} />

          <div className="flex flex-none flex-col" style={{ gap: 2 }}>
            {secondary.map((item) => (
              <RailSecondaryItem
                key={item.href}
                item={item}
                pathname={pathname}
                onRouteChange={onRouteChange}
              />
            ))}
          </div>

          <div className="min-h-2 flex-1" />

          {settings.map((item) => (
            <RailPrimaryItem
              key={item.href}
              item={item}
              pathname={pathname}
              onRouteChange={onRouteChange}
            />
          ))}
        </nav>

        <SecurityZoneCard onRouteChange={onRouteChange} />
      </aside>

      {/* Mobile drawer backdrop */}
      {isAsideMobileExpanded && (
        <div className="lg:hidden">
          <OverlayLayer zIndex="z-40" onClick={onOverlayClick} />
        </div>
      )}
    </>
  );
}
