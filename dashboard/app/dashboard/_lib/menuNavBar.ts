import { mdiAccount, mdiAccountGroup, mdiLogout } from "@mdi/js";
import { MenuNavBarItem } from "../../_interfaces";

/**
 * Operator-chip dropdown in the topbar.
 *
 * The dashboard has no real session to destroy — the template's old Logout
 * item never had a handler. "Log Out" now routes to /login so the action
 * stays reachable and meaningful once auth exists.
 */
const menuNavBar: MenuNavBarItem[] = [
  {
    icon: mdiAccount,
    msIcon: "person",
    label: "My Profile",
    href: "/dashboard/profile",
  },
  {
    icon: mdiAccountGroup,
    msIcon: "groups",
    label: "Household",
    href: "/dashboard/household",
  },
  {
    icon: mdiLogout,
    msIcon: "logout",
    label: "Log Out",
    isLogout: true,
  },
];

export default menuNavBar;
