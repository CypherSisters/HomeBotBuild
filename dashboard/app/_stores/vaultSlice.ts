import { createSlice, createAsyncThunk } from '@reduxjs/toolkit';
import { unlockVault, lockVault, getVaultContents, deleteVaultItem } from '../_lib/services/vaultService';
import type { VaultItem } from '../_interfaces/api';

interface VaultState {
  isUnlocked: boolean;
  contents: VaultItem[];
  loading: boolean;
  error: string | null;
}

const initialState: VaultState = {
  isUnlocked: false,
  contents: [],
  loading: false,
  error: null,
};

export const unlock = createAsyncThunk('vault/unlock', async (pin: string) => {
  return unlockVault(pin);
});

export const lock = createAsyncThunk('vault/lock', async () => {
  return lockVault();
});

// Accepts the unlock session token so getVaultContents can send the
// X-Vault-Token header. Without a token the API returns 403 and the
// list stays empty even when the vault has items.
export const fetchContents = createAsyncThunk(
  'vault/fetchContents',
  async (token?: string) => {
    return getVaultContents(token);
  },
);

export const removeItem = createAsyncThunk('vault/removeItem', async (key: string) => {
  await deleteVaultItem(key);
  return key;
});

const vaultSlice = createSlice({
  name: 'vault',
  initialState,
  reducers: {
    clearVaultError: (state) => { state.error = null; },
  },
  extraReducers: (builder) => {
    builder
      .addCase(unlock.pending, (state) => { state.loading = true; state.error = null; })
      .addCase(unlock.fulfilled, (state, action) => {
        state.loading = false;
        if (action.payload.success) {
          state.isUnlocked = true;
        } else {
          state.error = action.payload.error || 'Invalid PIN';
        }
      })
      .addCase(unlock.rejected, (state, action) => {
        state.loading = false;
        state.error = action.error.message || 'Failed to unlock';
      })
      .addCase(lock.fulfilled, (state) => {
        state.isUnlocked = false;
        state.contents = [];
      })
      .addCase(fetchContents.fulfilled, (state, action) => {
        state.contents = action.payload;
      })
      .addCase(removeItem.fulfilled, (state, action) => {
        state.contents = state.contents.filter(i => i.key !== action.payload);
      });
  },
});

export const { clearVaultError } = vaultSlice.actions;
export default vaultSlice.reducer;
