"use client";

/**
 * /dashboard/directions/<route id>/print: one saved route, on screen and on
 * paper. The kiosk's show-me overlay frames this path for a "directions" item;
 * "latest" is accepted as the id and means the newest route. Anything that is
 * not a route id shows the "isn't saved anymore" message (routeApiPath).
 * See ../../_components/DirectionsPage.tsx.
 */

import { useParams } from "next/navigation";
import DirectionsPage from "../../_components/DirectionsPage";

export default function RouteDirectionsPage() {
  const params = useParams<{ id: string }>();
  const id = typeof params?.id === "string" ? params.id : "";
  return <DirectionsPage id={id} />;
}
