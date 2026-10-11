# Avatars, bring your own face

## What ships

**Puff**: an inline-SVG cloud mascot whose mouth tracks the speech audio. No
WebGL, no downloads, always works, and the fallback whenever anything else
fails. The two 3D engines ("Digital Human", "Custom 3D Model") ship as
*renderers with no model*; until you give them one they draw Puff and say so. No
model is bundled because that would mean redistributing someone's character art
under a licence we cannot promise on your behalf.

## Three honest routes to your own model

1. **Ready Player Me / Avaturn, your own account.** Make an avatar, export
   **.glb**. Their terms govern what you may do with it; read them. Building it
   from a photo of your face is your call, the file never leaves your appliance.
2. **Blender → glTF Binary.** File → Export → glTF 2.0, format **glTF Binary
   (.glb)**, Materials on, Images: Automatic. Free, no account, you own it all.
3. **CC0 / CC-BY downloads** (Sketchfab, Quaternius…). Check the licence on the
   model's page. **CC-BY means keep the attribution**, author, licence, link.

## Upload it

Dashboard → **Profile → AI Avatar** → pick a 3D engine → **Upload .glb**. The
appliance checks the real glTF header (not the filename), caps it at 48 MB, and
stores it at:

```
$PUFFPAI_DATA_DIR/avatar_models/custom.glb      # data/avatar_models/ if unset
```

Outside the dashboard build, so it survives restarts, upgrades and redeploys.
Served back only through the pairing-gated proxy
(`/api/pai/profile/avatar-model`), an unpaired device on your wifi gets 401.
Uploading also points the avatar at it; nothing else to save.

**Remove it** with the Remove button on the same card (or delete the file); the
avatar goes back to drawn Puff at once.

**From another machine in your house:** the "on your own network" field takes
`http://192.168.x.x:port/model.glb`. Public URLs are **refused**, only
loopback / RFC 1918 / unique-local are stored, because a public URL here would
make every screen in the house fetch a model off the internet with no switch and
no consent.

## What a .glb actually needs

One file, textures embedded (`.gltf` + separate `.bin`/`.png` is refused). Past
that, the two engines want different things.

### "Custom 3D Model" (R3F), forgiving

Renders **any** valid GLB, including a static mesh with no skeleton. Names only
buy polish:

| Looked up | Where | Missing → |
| --- | --- | --- |
| `Spine1`, `Left/RightArm`, `Left/RightForeArm`, `Left/RightHand`, `Head`, `Neck` | `poses.ts:53-134` | no posing; own rest pose |
| `Head`, `Neck`, `Spine1` | `useSpeechAnimation.ts:69-71` | no head-nod while speaking |
| meshes named `*body*`/`*head*`/`*face*` | `AvaturnAvatar.tsx:152` | no skin smoothing |

This engine has **no lip-sync at all** (`registry.tsx:94`), it plays the speech
and nods. Morph targets are never read.

### "Digital Human" (TalkingHead), strict

Throws (and degrades to Puff) unless the model is a full RPM-shaped humanoid:

- **Every pose bone must exist**: `Hips`, `Spine`, `Spine1`, `Spine2`, `Neck`,
  `Head`, both `Shoulder`/`Arm`/`ForeArm`/`Hand`, **all 30 finger bones**,
  `UpLeg`/`Leg`/`Foot`/`ToeBase`, `Left/RightEye`. One missing →
  `Avatar object <name> not found` (`talkinghead.mjs:1257-1263`; names at
  `:207-232`). A `mixamorig` prefix is stripped for you (`:1250-1255`).
- **Root node named `Armature`** (`:147`, `:1290`); other names warn only.
- **A mesh with morph targets**, else `Blend shapes not found` (`:1318`).
- **Morph names: ARKit + Oculus visemes.** Blink/expression are ARKit
  (`eyeBlinkLeft/Right`, `jawOpen`, `mouthSmileLeft/Right`, `browInnerUp`,
  `eyeLookUpLeft`…, `:414`, `:634-686`). Lip-sync reads **Oculus**
  `viseme_aa|E|I|O|U|PP|FF|TH|DD|kk|CH|SS|nn|RR|sil` (`:2801`, `:2945`). RPM's
  composites (`mouthOpen`, `mouthSmile`, `eyesClosed`, `eyesLookUp/Down`) are
  synthesised from ARKit if absent (`:743-749`).
- An RPM **full-body** export with ARKit + Oculus Visemes enabled satisfies all
  of this. A **half-body** export does not (no legs) and is refused.

### Degradation

No viseme morphs → the mouth does not move; speech and head motion still work
(use the R3F engine, which never promised visemes). Wrong skeleton, no WebGL, or
a 404 → `AvatarStage` catches it and swaps to Puff with the ref intact
(`AvatarStage.tsx:113-122`): you lose the avatar, not the page. Strict-engine
reference: <https://github.com/met4citizen/TalkingHead>.
