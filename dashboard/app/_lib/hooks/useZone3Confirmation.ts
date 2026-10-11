"use client";

/**
 * The owner-consent round trip for Zone 3 changes.
 *
 * Every state-changing Zone 3 route answers an unticketed call with **409 and a
 * proposal** rather than doing the thing. This hook drives the two steps that
 * follow: mint a single-use ticket at `POST /zone3/confirm`, then present it
 * back on the original call. The ticket is bound server-side to exactly one
 * (kind, target, action) triple and is consumed on first presentation, so the
 * approval the owner read is the approval that is spent.
 *
 * Why a round trip at all, when the dashboard already holds the API key: it
 * raises the cost of an unwanted change from "emit one poisoned tool call" to
 * "execute a deliberate two-step protocol", and it guarantees whatever did the
 * confirming was shown the server's own description of the change. It does not
 * prove a person was there — see the note rendered in the dialog. That is why
 * the LLM tools propose and never actuate: they cannot complete this flow, and
 * they are not meant to be able to.
 *
 * The first attempt is fired by `run()`. A 2xx means the route did not need
 * consent (a no-op, or a change the backend does not gate) and the dialog never
 * appears.
 *
 * puff puff pass
 */

import { useCallback, useState } from "react";
import { apiPostRaw } from "../api";

/** What the server says a ticket would authorise. Rendered verbatim. */
export type Zone3Proposal = {
  kind?: string;
  target?: string;
  action?: string;
  /** One sentence naming the change, in the server's words. */
  headline?: string;
  /** What will start (or stop) happening. */
  effects?: string[];
  /** What keeps working regardless — safety devices, and so on. */
  always_allowed?: string[];
};

export type Zone3Request = {
  /** Short label for the dialog's corner, e.g. "Enforcement". */
  label: string;
  /** The route that performs the change. */
  endpoint: string;
  /** Body for that route, without the ticket. */
  body: Record<string, unknown>;
  /** The consent triple the ticket is bound to. */
  kind: string;
  target: string;
  action: string;
};

type Phase = "idle" | "confirming" | "minting" | "redeeming";

type State = {
  phase: Phase;
  request: Zone3Request | null;
  proposal: Zone3Proposal | null;
  refusal: string;
  error: string;
};

const IDLE: State = {
  phase: "idle",
  request: null,
  proposal: null,
  refusal: "",
  error: "",
};

export type Zone3Controller = {
  state: State;
  /** Fire the change. Resolves true when it actually happened. */
  run: (request: Zone3Request) => Promise<boolean>;
  confirm: () => Promise<void>;
  cancel: () => void;
};

export function useZone3Confirmation(onDone?: () => void): Zone3Controller {
  const [state, setState] = useState<State>(IDLE);

  const run = useCallback(
    async (request: Zone3Request): Promise<boolean> => {
      setState({ ...IDLE, request });
      try {
        const res = await apiPostRaw<any>(request.endpoint, request.body);
        if (res.status === 409) {
          setState({
            phase: "confirming",
            request,
            proposal: (res.data?.proposal as Zone3Proposal) ?? null,
            refusal: res.data?.message || res.data?.error || "",
            error: "",
          });
          return false;
        }
        if (!res.ok || res.data?.success === false) {
          setState({
            ...IDLE,
            request,
            phase: "confirming",
            error:
              res.data?.message ||
              res.data?.error ||
              `That was refused (${res.status}).`,
          });
          return false;
        }
        setState(IDLE);
        onDone?.();
        return true;
      } catch (e) {
        setState({
          ...IDLE,
          request,
          phase: "confirming",
          error: e instanceof Error ? e.message : "The request failed.",
        });
        return false;
      }
    },
    [onDone],
  );

  const confirm = useCallback(async () => {
    const request = state.request;
    if (!request) return;
    setState((s) => ({ ...s, phase: "minting", error: "" }));
    try {
      const mint = await apiPostRaw<any>("/zone3/confirm", {
        kind: request.kind,
        target: request.target,
        action: request.action,
      });
      const ticket = mint.data?.ticket;
      if (!mint.ok || !ticket) {
        setState((s) => ({
          ...s,
          phase: "confirming",
          error:
            mint.data?.error ||
            mint.data?.message ||
            "The appliance would not issue a confirmation ticket.",
        }));
        return;
      }
      setState((s) => ({ ...s, phase: "redeeming" }));
      const res = await apiPostRaw<any>(request.endpoint, {
        ...request.body,
        ticket,
      });
      if (!res.ok || res.data?.success === false) {
        setState((s) => ({
          ...s,
          phase: "confirming",
          error:
            res.data?.message ||
            res.data?.error ||
            `The change was refused (${res.status}).`,
        }));
        return;
      }
      setState(IDLE);
      onDone?.();
    } catch (e) {
      setState((s) => ({
        ...s,
        phase: "confirming",
        error: e instanceof Error ? e.message : "The change failed.",
      }));
    }
  }, [state.request, onDone]);

  const cancel = useCallback(() => setState(IDLE), []);

  return { state, run, confirm, cancel };
}
