import { apiGet, apiPost, apiDelete } from '../api';
import type { FirewallRule, NetworkDevice } from '../../_interfaces/api';

export async function getFirewallStatus(): Promise<{
  status: string;
  blocked_count: number;
  rules_count: number;
}> {
  const res = await apiGet('/firewall/status');
  return res;
}

export async function getFirewallRules(): Promise<FirewallRule[]> {
  const res = await apiGet<{ success: boolean; rules: FirewallRule[] }>('/firewall/rules');
  return res.rules || [];
}

export async function addFirewallRule(rule: Partial<FirewallRule>): Promise<{ success: boolean }> {
  return apiPost('/firewall/rules', rule);
}

export async function deleteFirewallRule(id: string): Promise<{ success: boolean }> {
  return apiDelete(`/firewall/rules/${id}`);
}

export async function getNetworkDevices(): Promise<NetworkDevice[]> {
  const res = await apiGet<{ success: boolean; devices: NetworkDevice[] }>('/firewall/devices');
  return res.devices || [];
}

export async function blockDevice(ip: string, reason?: string): Promise<{ success: boolean }> {
  return apiPost('/firewall/block', { ip, reason: reason || 'User requested' });
}

export async function unblockDevice(ip: string): Promise<{ success: boolean }> {
  return apiPost('/firewall/unblock', { ip });
}

export async function setLockdown(enable: boolean): Promise<{ success: boolean }> {
  return apiPost('/firewall/lockdown', { enable });
}
