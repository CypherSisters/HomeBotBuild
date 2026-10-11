import type { Metadata } from "next";
import ReviewScreen from "./ReviewScreen";

export const metadata: Metadata = {
  title: "The review",
  description: "The whole build, proposed, before anything is applied.",
};

export default function ReviewPage() {
  return <ReviewScreen />;
}
