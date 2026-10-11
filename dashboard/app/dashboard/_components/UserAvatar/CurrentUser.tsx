import React, { ReactNode } from "react";
import { useAppSelector } from "../../../_stores/hooks";
import UserAvatar from ".";

type Props = {
  className?: string;
  children?: ReactNode;
};

export default function UserAvatarCurrentUser({ className = "", children }: Props) {
  // Same identity source as the topbar chip: HouseholdSync fills the store
  // from the household owner. The email is only an initials seed, so the name
  // stands in when the backend has none.
  const userEmail = useAppSelector((state) => state.main.userEmail);
  const userName = useAppSelector((state) => state.main.userName);
  const userAvatar = useAppSelector((state) => state.main.userAvatar);

  return (
    <UserAvatar
      username={userEmail || userName || ""}
      imageUrl={userAvatar ?? undefined}
      className={className}
    >
      {children}
    </UserAvatar>
  );
}
