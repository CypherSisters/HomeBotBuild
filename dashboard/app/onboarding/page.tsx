import { Suspense } from "react";
import OnboardingWizard from "./OnboardingWizard";

export default function OnboardingPage() {
  return (
    // useSearchParams (the ?rerun=1 flag) needs a Suspense boundary in the app
    // router, or the whole route opts out of static rendering.
    <Suspense
      fallback={
        <div
          style={{
            fontFamily: "var(--font-jbmono), monospace",
            fontSize: 11,
            letterSpacing: ".14em",
            textTransform: "uppercase",
            color: "var(--text3)",
            padding: 32,
          }}
        >
          Loading…
        </div>
      }
    >
      <OnboardingWizard />
    </Suspense>
  );
}
