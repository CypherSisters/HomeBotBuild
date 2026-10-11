# PuffPAI Avatar System

Pluggable, user-switchable avatars for the PuffPAI dashboard.

_Copyright (c) 2026 Cypher Sisters. Apache-2.0 License._

---

## 1. What this is

The dashboard shows a talking avatar in three places (home tile, chat overlay,
full avatar page). Users can **choose** their avatar: the cloud mascot, a 3D
digital human, or their **own** 3D model. Every choice degrades gracefully when
an engine can't load.

This document describes the files involved, the postinstall patch the 3D engine
needs, and the pluggable **engine** architecture.

---

## 2. Files

All avatar code lives in `app/_components/Avatar/`.

| File | Role |
| --- | --- |
| `PuffCloudAvatar.tsx` | **2D SVG engine.** Inline SVG of the mascot; the mouth opens off the audio waveform (Web Audio `AnalyserNode`). No WebGL, no downloads. The reliable default. |
| `TalkingHeadAvatar.tsx` | **3D engine.** Wraps the `talkinghead` library (Three.js): full-body human, viseme lip-sync, moods, idle motion. Loaded via `dynamic(ssr:false)`. |
| `AvaturnAvatar.tsx` | **3D GLB engine** built on React Three Fiber (`@react-three/fiber` + `drei`). Loads any GLB, portrait framing, personality-reactive lighting. |
| `AvatarStage.tsx` | Renders the chosen engine with a fallback to Puff (see 4.3). |
| `engines/types.ts`, `engines/registry.tsx` | The engine contract and the registry (see 4.1, 4.2). |
| `AvatarPanel.tsx` | Full avatar page: viewport + personality sliders + test-speech + info card. |
| `AvatarTile.tsx` | Dashboard home tile: voice-first (mic → Vosk → Cortex → Piper → lip-sync). |
| `AvatarOverlay.tsx` | Draggable floating avatar on the chat page. |
| `useAvatarCharacter.ts` | Resolves the chosen avatar. localStorage answers instantly; `/profile/ai` reconciles in the background. |
| `poses.ts`, `usePose.ts`, `usePersonalityEffects.ts`, `useSpeechAnimation.ts` | R3F helpers used by `AvaturnAvatar`. |

Supporting state / persistence:

- `app/_stores/avatarSlice.ts`: Redux slice holding `character`
  (an `AvatarEngineId`), plus `avatarUrl`, `mood`, `speaking`, `personality`, etc.
- `app/_lib/services/profileService.ts`: `getAISettings()` / `updateAISettings()`
  against the backend `/profile/ai` endpoint.
- `app/_interfaces/api.ts`: `AISettings` (carries `avatar_character` and
  `avatar_3d_url`).
- `app/dashboard/profile/_components/AICustomizationForm.tsx`: the picker UI.

---

## 3. The talkinghead worklet patch

`talkinghead.mjs` references its AudioWorklet processor with the standard
bundler idiom:

```js
const workletUrl = new URL('./playback-worklet.js', import.meta.url);
```

and loads it at runtime via
`this.audioCtx.audioWorklet.addModule(workletUrl.href)`. Bundlers special-case
`new URL('./x', import.meta.url)` and try to resolve `./playback-worklet.js` as
a graph asset. **Turbopack fails that resolution** and aborts the whole
dashboard build with:

```
./node_modules/talkinghead/modules/talkinghead.mjs:36:20
Module not found: Can't resolve './playback-worklet.js'
```

`scripts/postinstall.js` therefore does three things:

1. **Copies** `playback-worklet.js` → `public/playback-worklet.js`.
2. **Patches** talkinghead's dynamic lipsync `import()` (a separate non-literal
   import) into a static import map.
3. **Rewrites** the worklet reference to load the public copy instead of the
   module graph:

```js
const workletUrl = new URL('/playback-worklet.js',
  (typeof self !== 'undefined' && self.location) ? self.location.origin : 'http://localhost');
```

Because the first argument is an **absolute** path (not `./…` +
`import.meta.url`), Turbopack does not treat it as a bundled asset; at runtime
`workletUrl.href` resolves to `<origin>/playback-worklet.js`, served from
`public/`. Both patches are **idempotent** and guarded by independent markers,
so re-running `npm install` never double-applies or skips one.

### Why talkinghead stays

- `talkinghead` is a git dependency (`github:met4citizen/TalkingHead`), not
  versioned on npm, and needs its internals patched at postinstall. That is
  fragile: a library update can silently break the patch (the script warns
  when its target text isn't found).
- It is the only engine here with real **viseme** lip-sync, and it is wired to
  the offline voice loop (`speakPcm` drives the "en" lipsync module from Piper
  PCM).
- The R3F engine (`@react-three/fiber`, `@react-three/drei`, `three`) has no
  viseme model, but audio-plus-motion speech is acceptable and it accepts
  **any user GLB**.

The registry makes the choice a **configuration** matter: `talkinghead`
(`human`) and R3F (`glb`) are two registry entries. Retiring talkinghead means
deleting its descriptor. A possible path: add viseme→morph-target lip-sync to
the R3F engine (Ready Player Me models expose ARKit blendshapes), then retire the
`human` engine.

---

## 4. Pluggable engine architecture

### 4.1 The engine contract: `engines/types.ts`

Every avatar renderer implements one **handle** and accepts one **prop shape**:

```ts
interface AvatarEngineHandle {
  speakText(text): Promise<void>;
  speakPcm(pcmBase64, sampleRate, text): Promise<void>;   // server-TTS + lipsync
  stopSpeaking(): void;
  setMood(mood): void;
  setView(view): void;
  isReady(): boolean;
}
```

Engines that can't honour a capability implement it as a **safe no-op** (the SVG
engine's `setView` does nothing) so callers never branch on engine type.

Each engine also declares **capabilities** (`lipsync`, `mood`, `view`,
`personality`, `customModel: 'glb' | 'image' | 'none'`), which drive the picker
UI and let callers feature-detect instead of hard-coding ids.

### 4.2 The registry: `engines/registry.tsx`

The single source of truth. Each entry is a **descriptor**: `id`, `label`,
`description`, `capabilities`, a lazily-loaded (`next/dynamic`, `ssr:false`)
`Component`, and an optional `defaultModelUrl`.

| id | label | engine | lipsync | custom model |
| --- | --- | --- | --- | --- |
| `puff` | Puff | `PuffCloudAvatar` (SVG) | audio-driven | none |
| `human` | Digital Human | `TalkingHeadAvatar` (talkinghead) | viseme | GLB / RPM URL |
| `glb` | Custom 3D Model | `AvaturnAvatar` (R3F) | motion only | GLB URL |

Helpers: `resolveEngine(id)` (**always** returns a usable engine; unknown ids
fall back to Puff), `isAvatarEngineId(x)` (type guard for persistence),
`listEngines()` (for building pickers). **Adding an engine = one descriptor + a
component that implements the handle.** No placement code changes.

The heavy Three.js engines are `dynamic(ssr:false)`, so they never enter the SSR
bundle and only download when actually selected; Puff (cheap, safe) is the
default and the universal fallback.

### 4.3 The stage: `AvatarStage.tsx`

Every placement renders the avatar through one component. It:

1. Resolves the engine by id via the registry.
2. Renders it, forwarding the caller's `ref` (React 19 passes `ref` through
   `dynamic()`) and props, defaulting `avatarUrl` to the engine's model.
3. Wraps heavy engines in an **error boundary**. If a WebGL/GLB/chunk failure
   throws during load, the stage **degrades to Puff**. The caller's ref stays
   valid because Puff implements the same handle, so speech/sink wiring keeps
   working. Puff itself is never wrapped (nowhere left to degrade to).

Placements render a single:

```tsx
<AvatarStage engineId={character} ref={avatarRef} avatarUrl={avatarUrl} mood={mood} … />
```

### 4.4 Per-user persistence

Two layers:

- **localStorage**: `puffpai_avatar_character` (engine id) and
  `puffpai_avatar_model_url` (custom GLB). Answers on first paint, no flicker.
- **Profile / backend**: `AISettings.avatar_character` and
  `AISettings.avatar_3d_url`, via `/profile/ai`. Reconciled in the background by
  `useAvatarCharacter`; the server value wins and is mirrored back to
  localStorage. Everything is best-effort: offline → localStorage decides →
  store default (`puff`).

`useAvatarCharacter` validates the stored/remote id against the registry
(`isAvatarEngineId`), so a stale or mistyped id can never blank the avatar.

### 4.5 The picker: `AICustomizationForm.tsx`

The Profile → AI Customization card is **registry-driven**: it maps
`listEngines()` into selectable cards (so new engines appear automatically). When
the selected engine's `capabilities.customModel === 'glb'`, the **"Your own 3D
model (.glb)"** block appears: an upload button, a Remove button once a model is
stored, and — folded away — a LAN-only model URL field. Saving persists
`avatar_character` + `avatar_3d_url` to the backend and mirrors both into the
live store and localStorage.

---

## 5. How a user supplies their own avatar

**No GLB ships with this repository.** With no model URL set, the `human` and
`glb` engines do not fetch anything: `AvatarStage` renders the always-safe Puff
engine and one line of text pointing at the upload.

- **Upload.** `POST /profile/avatar-model` (multipart `file`) through the
  pairing-gated proxy. The backend (`src/puffpai/avatar_model.py`) verifies the
  glTF binary header, caps at 48 MB and stores ONE file at
  `$PUFFPAI_DATA_DIR/avatar_models/custom.glb`, then sets `avatar_3d_url` to
  `/api/pai/profile/avatar-model`. `DELETE` removes it and clears the setting.
  **Nothing is written into `dashboard/public/avatars/`**: that path is inside
  the checkout, is wiped by the next build, and is served with no household
  gate. Do not store models there.
- **A model URL on the LAN.** Accepted, restricted to loopback / RFC 1918
  / unique-local hosts by `api_server._valid_avatar_url` →
  `puffpai.security.lan_http.validate_url`. Public URLs are refused: the browser
  does the fetching, so storing one would make every screen in the house pull a
  model off the internet with no Bridge switch and no consent.
- **2D image/SVG:** the SVG engine is the fixed mascot. The contract reserves
  `customModel: 'image'` for a "your own 2D portrait" engine; add it as a
  registry entry when needed, no other code changes.

User-facing instructions, licences and the per-engine bone/morph requirements
live in [`docs/AVATARS.md`](../../docs/AVATARS.md).

---

## 6. Dependencies

The R3F engine uses `@react-three/fiber`, `@react-three/drei`, and `three`, all
in `package.json`. `talkinghead` is a git dependency, patched by postinstall
(section 3).

---

## 7. Known limitations

- **Backend `avatar_3d_url`**: the dashboard sends it with `/profile/ai`; check
  that your backend version persists the field.
- **File upload**: URL entry works; dragging a `.glb` off disk needs a backend
  upload endpoint to host the file and return a URL. The picker's URL field is
  where that would plug in.
- **talkinghead patch**: a talkinghead update can move the patched lines; the
  postinstall script warns when its target text isn't found (section 3).
