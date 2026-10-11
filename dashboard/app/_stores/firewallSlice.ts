import { createSlice, createAsyncThunk } from '@reduxjs/toolkit';
import { getFirewallStatus, getFirewallRules, getNetworkDevices, blockDevice, unblockDevice } from '../_lib/services/firewallService';
import type { FirewallRule, NetworkDevice } from '../_interfaces/api';

interface FirewallState {
  enabled: boolean;
  rules: FirewallRule[];
  devices: NetworkDevice[];
  blockedCount: number;
  loading: boolean;
  error: string | null;
}

const initialState: FirewallState = {
  enabled: true,
  rules: [],
  devices: [],
  blockedCount: 0,
  loading: false,
  error: null,
};

export const fetchFirewallStatus = createAsyncThunk('firewall/fetchStatus', async () => {
  return getFirewallStatus();
});

export const fetchFirewallRules = createAsyncThunk('firewall/fetchRules', async () => {
  return getFirewallRules();
});

export const fetchNetworkDevices = createAsyncThunk('firewall/fetchDevices', async () => {
  return getNetworkDevices();
});

export const blockDeviceThunk = createAsyncThunk('firewall/blockDevice', async (ip: string) => {
  await blockDevice(ip);
  return ip;
});

export const unblockDeviceThunk = createAsyncThunk('firewall/unblockDevice', async (ip: string) => {
  await unblockDevice(ip);
  return ip;
});

const firewallSlice = createSlice({
  name: 'firewall',
  initialState,
  reducers: {},
  extraReducers: (builder) => {
    builder
      .addCase(fetchFirewallStatus.fulfilled, (state, action) => {
        state.enabled = action.payload.status === 'active';
        state.blockedCount = action.payload.blocked_count;
      })
      .addCase(fetchFirewallRules.fulfilled, (state, action) => {
        state.rules = action.payload;
      })
      .addCase(fetchNetworkDevices.fulfilled, (state, action) => {
        state.devices = action.payload;
      });
  },
});

export default firewallSlice.reducer;
