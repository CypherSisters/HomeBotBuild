import { createSlice, PayloadAction } from "@reduxjs/toolkit";
import { UserPayloadObject } from "../_interfaces";

interface MainState {
  userName: string;
  userEmail: null | string;
  userAvatar: null | string;
  isFieldFocusRegistered: boolean;
}

const initialState: MainState = {
  /* User.
     Empty, not "PuffPAI User": this is the pre-load state, and a placeholder
     here is indistinguishable from a real name once it is rendered. The owner
     saved a name and the welcome card still said "PuffPAI User" because
     nothing ever dispatched setUser. HouseholdSync now does, from the one
     identity source (GET /household); each surface supplies its own fallback
     for the brief window where this is still empty. */
  userName: "",
  userEmail: null,
  userAvatar: null,

  /* Field focus with ctrl+k (to register only once) */
  isFieldFocusRegistered: false,
};

export const mainSlice = createSlice({
  name: "main",
  initialState,
  reducers: {
    setUser: (state, action: PayloadAction<UserPayloadObject>) => {
      state.userName = action.payload.name;
      state.userEmail = action.payload.email;
      state.userAvatar = action.payload.avatar ?? null;
    },
  },
});

// Action creators are generated for each case reducer function
export const { setUser } = mainSlice.actions;

export default mainSlice.reducer;
