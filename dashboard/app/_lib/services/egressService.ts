import { apiGet, apiPostRaw } from '../api';
import type { EgressDoor, EgressLedgerRecord } from '../../_interfaces/api';

/**
 * Owner control + audit read for the egress door registry.
 * Backend: src/puffpai/security/egress_admin.py, routed in api_server.py.
 */

/** What the toggle route can answer with. Every non-200 leaves the appliance
 *  config byte-identical and writes no ledger line, so a failure here is a
 *  message to show — never a reason to re-render the switch as changed. */
export type DoorToggleResult =
  | { ok: true; door: EgressDoor }
  /** `status` is part of the contract: 409 means this door is not a control
   *  (or an env var is forcing it) and the switch should stay disabled with
   *  `error` as its reason. 400/404/429/500 are transient or programming
   *  errors and leave the switch usable. `error` is written to be shown
   *  verbatim; never paraphrase it. */
  | { ok: false; status: number; error: string };

export async function setEgressDoor(
  id: string,
  open: boolean
): Promise<DoorToggleResult> {
  // `open` must be a real JSON boolean — the backend refuses "false"/0/"0"
  // with a 400 rather than guessing which one the caller meant.
  const res = await apiPostRaw<{
    success?: boolean;
    door?: EgressDoor;
    error?: string;
  }>('/system/egress/door', { id, open: Boolean(open) });

  if (res.ok && res.data?.door) return { ok: true, door: res.data.door };
  return {
    ok: false,
    status: res.status,
    error: res.data?.error || `Request failed (${res.status}).`,
  };
}

/** Recent decisions, newest last. A missing ledger is count:0, not an error. */
export async function getEgressLedger(
  limit = 10
): Promise<EgressLedgerRecord[]> {
  const res = await apiGet<{
    success: boolean;
    limit: number;
    count: number;
    records: EgressLedgerRecord[];
  }>(`/system/egress/ledger?limit=${limit}`, { cache: 'no-store' });
  return res.records || [];
}
