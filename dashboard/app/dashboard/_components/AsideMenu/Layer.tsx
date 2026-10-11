import React from "react";
import { mdiLogout, mdiClose } from "@mdi/js";
import Icon from "../../../_components/Icon";
import AsideMenuItem from "./Item";
import AsideMenuList from "./List";
import { MenuAsideItem } from "../../../_interfaces";

type Props = {
  menu: MenuAsideItem[];
  className?: string;
  onAsideLgCloseClick: () => void;
  onRouteChange: () => void;
};

export default function AsideMenuLayer({ menu, className = "", ...props }: Props) {
  const logoutItem: MenuAsideItem = {
    label: "Logout",
    icon: mdiLogout,
    color: "info",
    isLogout: true,
  };

  const handleAsideLgCloseClick = (e: React.MouseEvent) => {
    e.preventDefault();
    props.onAsideLgCloseClick();
  };

  return (
    <aside
      className={`${className} zzz fixed top-0 z-40 flex h-screen w-60 overflow-hidden transition-(--transition-position) lg:py-2 lg:pl-2`}
    >
      <div
        className={`aside glass-aside flex flex-1 flex-col overflow-hidden lg:rounded-2xl dark:bg-slate-900`}
      >
        <div
          className={`aside-brand flex h-14 flex-row items-center justify-between dark:bg-slate-900`}
        >
          <div className="flex flex-1 items-center justify-center gap-2 lg:justify-start lg:pl-4 xl:justify-center xl:pl-0">
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src="/puffpai-logo.png"
              alt="PuffPAI"
              className="h-10 w-auto"
              style={{ filter: "drop-shadow(0 0 16px var(--puff-glow))" }}
            />
            <b
              className="text-lg font-semibold tracking-tight"
              style={{ color: "var(--puff-text-strong)", fontFamily: "var(--font-fredoka), sans-serif" }}
            >
              Puff<span style={{ color: "var(--puff-pink)" }}>PAI</span>
            </b>
          </div>
          <button
            className="hidden p-3 lg:inline-block xl:hidden"
            onClick={handleAsideLgCloseClick}
          >
            <Icon path={mdiClose} />
          </button>
        </div>
        <div
          className={`aside-scrollbar flex-1 overflow-x-hidden overflow-y-auto dark:scrollbar-styled-dark`}
        >
          <AsideMenuList menu={menu} onRouteChange={props.onRouteChange} />
        </div>
        <ul>
          <AsideMenuItem item={logoutItem} onRouteChange={props.onRouteChange} />
        </ul>
      </div>
    </aside>
  );
}
