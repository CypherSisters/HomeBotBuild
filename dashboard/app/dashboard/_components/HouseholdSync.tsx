"use client";

import { useEffect } from "react";
import { useAppDispatch } from "../../_stores/hooks";
import { setUser } from "../../_stores/mainSlice";
import { useHousehold } from "../../_lib/hooks/useHousehold";
import { avatarSrc } from "../../_lib/services/householdService";

/**
 * The bridge between the one identity source and the Redux store.
 *
 * `setUser` existed since the template and was never dispatched, so every
 * consumer of `state.main.userName` rendered the initial-state placeholder
 * forever. Mounted once in the dashboard shell: it subscribes to the household
 * roster and pushes the owner into the store, which is what the welcome card
 * and the legacy nav avatar read. Renders nothing.
 */
export default function HouseholdSync() {
  const dispatch = useAppDispatch();
  const { owner, bust } = useHousehold();

  useEffect(() => {
    if (!owner) return;
    dispatch(
      setUser({
        name: owner.display_name || "",
        email: "",
        avatar: avatarSrc(owner, bust),
      }),
    );
  }, [dispatch, owner, bust]);

  return null;
}
