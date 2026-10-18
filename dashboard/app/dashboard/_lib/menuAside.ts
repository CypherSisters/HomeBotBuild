import {
  mdiMonitor,
  mdiMicrophone,
  mdiMessageText,
  mdiBookOpenPageVariant,
  mdiShieldLock,
  mdiShieldOutline,
  mdiHome,
  mdiAccountCircle,
  mdiCog,
  mdiAlarmLight,
  mdiCalendar,
  mdiFood,
  mdiFire,
  mdiHelpCircle,
  mdiRobotOutline,
  mdiAccountVoice,
  mdiAccountGroup,
  mdiPuzzleOutline,
  mdiFileDocumentMultipleOutline,
  mdiMusic,
  mdiChip,
  mdiHeartPulse,
  mdiLan,
  mdiDirections,
  mdiViewGridOutline,
  mdiKeyVariant,
  mdiPlusCircleOutline,
  mdiCodeBraces,
} from "@mdi/js";
import { MenuAsideItem } from "../../_interfaces";

/**
 * Sidebar + search destinations.
 *
 * railGroup decides where an entry renders in the rail:
 *   "primary"   → Dashboard / Talk / Chat / Avatar / Device
 *   "secondary" → dimmed group (Knowledge / Vault / Smart Home / Media / Agent Team)
 *   "settings"  → the Settings button above the security-zone card
 *   omitted     → NOT in the rail, but kept here so QuickSearch still indexes
 *                 the absorbed routes (documents, firewall, workflows, profile,
 *                 apps). The mdi `icon` is what QuickSearch's dropdown renders;
 *                 `msIcon` is the Material Symbols ligature the rail renders.
 */
const menuAside: MenuAsideItem[] = [
  /* ── Primary nav ── */
  {
    href: "/dashboard",
    label: "Dashboard",
    icon: mdiMonitor,
    msIcon: "space_dashboard",
    railGroup: "primary",
  },
  {
    href: "/dashboard/talk",
    label: "Talk",
    icon: mdiMicrophone,
    msIcon: "graphic_eq",
    railGroup: "primary",
  },
  {
    href: "/dashboard/chat",
    label: "Chat",
    icon: mdiMessageText,
    msIcon: "chat_bubble",
    railGroup: "primary",
  },
  {
    href: "/dashboard/avatar",
    label: "Avatar",
    icon: mdiAccountVoice,
    msIcon: "face_retouching_natural",
    railGroup: "primary",
  },
  {
    href: "/dashboard/device",
    label: "Device",
    icon: mdiChip,
    msIcon: "memory",
    railGroup: "primary",
  },

  /* ── Secondary (dimmed) group ── */
  {
    href: "/dashboard/knowledge",
    label: "Knowledge",
    icon: mdiBookOpenPageVariant,
    msIcon: "menu_book",
    railGroup: "secondary",
  },
  {
    // Find more after the build: skills, tools and apps others built, books,
    // live cards. The page's own title says "Add to <bot>". Every build has
    // it (no surface mapping).
    href: "/dashboard/add",
    label: "Find more",
    icon: mdiPlusCircleOutline,
    msIcon: "add_circle",
    railGroup: "secondary",
  },
  {
    href: "/dashboard/vault",
    label: "Vault",
    icon: mdiShieldLock,
    msIcon: "lock",
    railGroup: "secondary",
  },
  {
    href: "/dashboard/homeassistant",
    label: "Smart Home",
    icon: mdiHome,
    msIcon: "home",
    railGroup: "secondary",
  },
  {
    href: "/dashboard/media",
    label: "Media",
    icon: mdiMusic,
    msIcon: "music_note",
    railGroup: "secondary",
  },
  {
    href: "/dashboard/agents",
    label: "Agent Team",
    icon: mdiAccountGroup,
    msIcon: "groups",
    railGroup: "secondary",
  },
  {
    // Zone 3, not the agent. The security agent's display name is also
    // Drawbridge (see the Agent Team page), so this entry names the zone it
    // actually opens. The route is unchanged.
    href: "/dashboard/bridge",
    label: "Network edge",
    icon: mdiLan,
    msIcon: "lan",
    railGroup: "secondary",
  },
  // Side by side with the network edge, deliberately. They are two readings
  // of one instrument: the edge says what the house IS saying, Firewall says
  // what it is ALLOWED to say. Someone who has just watched a television phone home
  // should be one entry away from the only screen that can stop it.
  {
    href: "/dashboard/firewall",
    label: "Firewall",
    icon: mdiShieldOutline,
    msIcon: "shield",
    railGroup: "secondary",
  },
  {
    // Free sign-up keys: the steps to get one, a hidden box to paste it into
    // (it goes straight into the vault), and each key's state. No surface
    // mapping on purpose: every build can add a key.
    href: "/dashboard/keys",
    label: "Sign-up keys",
    icon: mdiKeyVariant,
    msIcon: "key",
    railGroup: "secondary",
  },

  /* ── Settings (bottom, above the security-zone card) ── */
  {
    // The Developer area: the vault, the brain and the Bridge, each with the
    // terminal commands for it. Always shown;
    // changes still need a paired device, and vault changes the PIN. No
    // surface mapping on purpose: every build has it.
    href: "/dashboard/developer",
    label: "Developer",
    icon: mdiCodeBraces,
    msIcon: "code",
    railGroup: "settings",
  },
  {
    href: "/dashboard/settings",
    label: "Settings",
    icon: mdiCog,
    msIcon: "settings",
    railGroup: "settings",
  },

  /* ── Absorbed routes: search-only (no railGroup → hidden from the rail).
        Knowledge absorbs My Documents, Agent Team absorbs Workflows, the
        security-zone card absorbs Firewall, the topbar chip absorbs Profile,
        the dashboard Apps tile absorbs the Apps submenu. Routes stay alive. ── */
  {
    href: "/dashboard/documents",
    label: "My Documents",
    icon: mdiFileDocumentMultipleOutline,
  },
  {
    href: "/dashboard/firewall",
    label: "Firewall",
    icon: mdiShieldOutline,
  },
  {
    href: "/dashboard/workflows",
    label: "Workflows",
    icon: mdiRobotOutline,
  },
  {
    href: "/dashboard/profile",
    label: "Profile",
    icon: mdiAccountCircle,
  },
  {
    href: "/dashboard/household",
    label: "Household",
    icon: mdiAccountGroup,
  },
  {
    href: "/dashboard/skills",
    label: "Skills",
    icon: mdiPuzzleOutline,
  },
  {
    // Search-only: the newest directions Puff gave, for printing from a phone
    // or laptop. The kiosk gets them via show-me.
    href: "/dashboard/directions",
    label: "Directions",
    icon: mdiDirections,
  },
  {
    // The submenu keeps its own href so /dashboard/apps, the index, is a real
    // destination, findable by typing "apps". It lists the built-in apps and
    // every app the build installed.
    label: "Apps",
    href: "/dashboard/apps",
    icon: mdiViewGridOutline,
    menu: [
      {
        label: "AlarmFlow",
        icon: mdiAlarmLight,
        href: "/dashboard/apps/alarmflow",
      },
      {
        label: "Calendar",
        icon: mdiCalendar,
        href: "/dashboard/apps/calendar",
      },
      {
        label: "FreshTable",
        icon: mdiFood,
        href: "/dashboard/apps/freshtable",
      },
      {
        label: "FitFam",
        icon: mdiFire,
        href: "/dashboard/apps/fitfam",
      },
      {
        label: "Trivia",
        icon: mdiHelpCircle,
        href: "/dashboard/apps/trivia",
      },
    ],
  },
];

export default menuAside;
