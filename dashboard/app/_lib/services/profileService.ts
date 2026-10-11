import { apiGet, apiPut } from '../api';
import type { UserProfile, AISettings } from '../../_interfaces/api';

export async function getProfile(): Promise<UserProfile> {
  const res = await apiGet<{ success: boolean; profile: UserProfile }>('/profile');
  return res.profile;
}

export async function updateProfile(profile: UserProfile): Promise<{ success: boolean }> {
  return apiPut('/profile', profile);
}

export async function getAISettings(): Promise<AISettings> {
  const res = await apiGet<{ success: boolean; settings: AISettings }>('/profile/ai');
  return res.settings;
}

export async function updateAISettings(settings: Partial<AISettings>): Promise<{ success: boolean }> {
  return apiPut('/profile/ai', settings);
}

/**
 * The household's own 3D avatar model.
 *
 * No model ships with this build, so this is how anyone gets a 3D face at all.
 * These three go through the same-origin, pairing-gated proxy (/api/pai/...)
 * rather than api.ts's direct backend client, for one load-bearing reason: the
 * browser's GLTFLoader fetches AVATAR_MODEL_URL itself and cannot attach an API
 * key, so the model has to be reachable with a cookie. Upload and delete use
 * the same door so all three agree about who is allowed.
 *
 * The file is stored under the appliance's data dir (see
 * src/puffpai/avatar_model.py) — never in the dashboard checkout, which is
 * rebuilt on every deploy.
 */
export const AVATAR_MODEL_URL = '/api/pai/profile/avatar-model';

export async function uploadAvatarModel(
  file: File
): Promise<{ success: boolean; model_url?: string; bytes?: number; message?: string }> {
  const form = new FormData();
  form.append('file', file);
  const res = await fetch(AVATAR_MODEL_URL, { method: 'POST', body: form });
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    throw new Error(data?.message || data?.error || `Upload failed (${res.status})`);
  }
  return data;
}

export async function deleteAvatarModel(): Promise<{ success: boolean; removed: boolean }> {
  const res = await fetch(AVATAR_MODEL_URL, { method: 'DELETE' });
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    throw new Error(data?.message || data?.error || `Remove failed (${res.status})`);
  }
  return data;
}
