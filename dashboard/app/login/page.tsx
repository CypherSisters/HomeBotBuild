import React from "react";
import CardBox from "../_components/CardBox";
import SectionFullScreen from "../_components/Section/FullScreen";
import { getPageTitle } from "../_lib/config";
import { Metadata } from "next";

export const metadata: Metadata = {
  title: getPageTitle("Sign in"),
};

/**
 * There is no login on this appliance, and this page says so.
 *
 * It has no sign-in form on purpose: a convincing-looking login box is
 * worse than none: it invites a household to type a real password into a
 * field that never checks one.
 *
 * The real access control is the household PAIRING GATE — see
 * `dashboard/middleware.ts` and `app/_lib/server/pairing.ts`. A device proves
 * itself once with a one-hour code and carries a signed device cookie
 * afterwards; there are no accounts and nothing to sign out of. This page is
 * kept only because the operator menu's "Log Out" item routes here, and it is
 * deliberately STATIC: no form, no input, no client component, nothing typed
 * and nothing logged.
 */
const LoginPage = () => {
  return (
    <SectionFullScreen bg="purplePink">
      <CardBox className="w-11/12 shadow-2xl md:w-7/12 lg:w-6/12 xl:w-4/12">
        <h1 className="text-xl font-semibold">No account, no password</h1>
        <p className="mt-3">
          This appliance does not have logins. Access is granted per device:
          each phone, tablet or laptop is paired once with a code from this
          household&apos;s own screen, and stays paired until you remove it.
          Nothing you could type here would sign you in, so there is no form.
        </p>
        <p className="mt-3">
          To add a device, open{" "}
          <a href="/dashboard/settings" className="underline">
            Settings &rarr; Paired devices
          </a>{" "}
          on an already-paired screen and scan the code, or type it into{" "}
          <a href="/send" className="underline">
            the send page
          </a>{" "}
          on the new device. To revoke one, remove it in the same place — that
          is what &quot;logging out&quot; means here.
        </p>
        <p className="mt-3">
          <a href="/dashboard" className="underline">
            Back to the dashboard
          </a>
        </p>
      </CardBox>
    </SectionFullScreen>
  );
};

export default LoginPage;
