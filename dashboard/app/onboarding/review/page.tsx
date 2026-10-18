import { redirect } from "next/navigation";

/** An old link to the review screen lands on setup. */
export default function ReviewPage() {
  redirect("/onboarding");
}
