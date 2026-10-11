import { apiGet, apiPost, apiDelete, API_URL } from '../api';
import type { VaultItem } from '../../_interfaces/api';

export interface VaultStatus {
  /** Whether the backend vault module is loaded at all. */
  vault_available?: boolean;
  /**
   * Whether a vault has ever been created (a PIN was set and a key salt
   * written). Older backends omit this field; callers must treat
   * ``undefined`` as "unknown", not as "not set up".
   */
  vault_initialized?: boolean;
  mfa_enabled?: boolean;
}

/**
 * Read vault availability / initialization state. This is the only
 * endpoint that reports initialization without requiring a PIN, so the UI
 * uses it to tell "never created" apart from "created and locked".
 */
export async function getVaultStatus(): Promise<VaultStatus> {
  return apiGet<VaultStatus>('/vault/mfa-status');
}

export async function unlockVault(pin: string): Promise<{ success: boolean; error?: string }> {
  return apiPost('/vault/unlock', { pin });
}

export async function lockVault(): Promise<{ success: boolean }> {
  return apiPost('/vault/lock');
}

/**
 * Fetch vault contents. The backend requires the unlock session token in
 * the X-Vault-Token header, and returns the list under ``items`` (not
 * ``contents``). Callers must pass the token returned by /vault/unlock.
 */
export async function getVaultContents(token?: string | null): Promise<VaultItem[]> {
  // X-Vault-Token is the caller's own unlock session, not the backend key.
  // The proxy does NOT relay X-* headers generally; it relays this one header
  // by name, and only on /vault/* paths, and attaches the backend key itself.
  const headers: HeadersInit = { 'Content-Type': 'application/json' };
  if (token) headers['X-Vault-Token'] = token;

  const res = await fetch(`${API_URL}/vault/contents`, { method: 'GET', headers });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ message: res.statusText }));
    throw new Error(err.message || res.statusText);
  }
  const data: { success: boolean; items?: VaultItem[]; contents?: VaultItem[] } = await res.json();
  // Prefer ``items`` (current API); fall back to ``contents`` for older builds.
  return data.items || data.contents || [];
}

export async function storeVaultItem(key: string, value: string, type: string): Promise<{ success: boolean }> {
  return apiPost('/vault/store', { key, value, type });
}

export async function deleteVaultItem(key: string): Promise<{ success: boolean }> {
  return apiDelete(`/vault/item/${encodeURIComponent(key)}`);
}
