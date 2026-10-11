import React from "react";
import type { Metadata } from "next";
import { getPageTitle } from "../../_lib/config";
import AppsIndex from "./_components/AppsIndex";

export const metadata: Metadata = {
  title: getPageTitle("Apps"),
};

// The index the sidebar's Apps submenu never had: /dashboard/apps was a 404 even
// though every app under it resolved. The body is a client component because it
// asks the box which apps are installed; the title stays here on the server.
export default function AppsIndexPage() {
  return <AppsIndex />;
}
