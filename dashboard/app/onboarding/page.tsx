import { Suspense } from "react";
import OnboardingWizard from "./OnboardingWizard";

/**
 * /onboarding: first-run setup. Hardware, the brain's size, the Vault and,
 * if the owner wants, their Home Assistant; then the build. The same answers
 * `puffpai setup` asks for on the console.
 */
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
          Loading...
        </div>
      }
    >
      <OnboardingWizard />
    </Suspense>
  );
}
