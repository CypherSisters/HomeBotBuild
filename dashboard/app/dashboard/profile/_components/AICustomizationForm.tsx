"use client";

import { useState, useEffect, useRef } from "react";
import {
  mdiRobot,
  mdiPalette,
  mdiThermometer,
  mdiCog,
  mdiContentSave,
  mdiCheckCircle,
  mdiAlertCircle,
  mdiLoading,
  mdiRestore,
  mdiCubeOutline,
  mdiUpload,
  mdiDelete,
} from "@mdi/js";
import CardBox from "../../../_components/CardBox";
import CardBoxComponentBody from "../../../_components/CardBox/Component/Body";
import CardBoxComponentFooter from "../../../_components/CardBox/Component/Footer";
import Icon from "../../../_components/Icon";
import Button from "../../../_components/Button";
import Buttons from "../../../_components/Buttons";
import {
  updateAISettings,
  uploadAvatarModel,
  deleteAvatarModel,
  AVATAR_MODEL_URL,
} from "../../../_lib/services/profileService";
import type { AISettings } from "../../../_interfaces/api";
import { useAppDispatch } from "../../../_stores/hooks";
import { setAvatarCharacter, setAvatarUrl } from "../../../_stores/avatarSlice";
import {
  AVATAR_CHARACTER_KEY,
  AVATAR_MODEL_URL_KEY,
} from "../../../_components/Avatar/useAvatarCharacter";
import {
  listEngines,
  isAvatarEngineId,
} from "../../../_components/Avatar/engines/registry";

/**
 * Who greets you on the dashboard. Sourced from the avatar engine registry so
 * new engines appear here automatically. Puff carries the mascot image; the 3D
 * engines show a "3D" badge.
 */
const AVATAR_CHARACTERS = listEngines().map((engine) => ({
  id: engine.id,
  label: engine.label,
  sublabel: engine.description,
  image: engine.id === "puff" ? "/puffpai-logo.png" : null,
  supportsCustomModel: engine.capabilities.customModel === "glb",
}));

const AVATAR_STYLES = [
  { id: "notionists", label: "Notionists", emoji: "R" },
  { id: "bottts", label: "Bottts", emoji: "B" },
  { id: "pixel-art", label: "Pixel Art", emoji: "P" },
  { id: "adventurer", label: "Adventurer", emoji: "A" },
];

const DEFAULT_SETTINGS: Partial<AISettings> = {
  ai_name: "PuffPAI",
  avatar_style: "notionists",
  avatar_character: "puff",
  // Blank = back to the built-in model; sent so a reset really clears a custom URL.
  avatar_3d_url: "",
  personality: "helpful",
  temperature: 0.7,
  context_length: 2048,
};

type Props = {
  settings: AISettings;
  onSaved: (updated: AISettings) => void;
};

export default function AICustomizationForm({ settings, onSaved }: Props) {
  const dispatch = useAppDispatch();
  const [aiName, setAiName] = useState(settings.ai_name || "PuffPAI");
  const [avatarCharacter, setAvatarCharacterState] = useState(
    settings.avatar_character || "puff"
  );
  const [avatarStyle, setAvatarStyle] = useState(settings.avatar_style || "notionists");
  const [avatarModelUrl, setAvatarModelUrl] = useState(settings.avatar_3d_url || "");
  const [temperature, setTemperature] = useState(settings.temperature ?? 0.7);
  const [personality, setPersonality] = useState(settings.personality || "helpful");
  const [contextLength, setContextLength] = useState(settings.context_length ?? 2048);

  // Own-model upload. `uploading` covers both the POST and the DELETE so the
  // two buttons can never be pressed against each other.
  const [uploading, setUploading] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);

  const [savingIdentity, setSavingIdentity] = useState(false);
  const [savingSettings, setSavingSettings] = useState(false);
  const [toast, setToast] = useState<{ message: string; type: "success" | "error" } | null>(null);

  // Debounce ref for temperature
  const tempTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const showToast = (message: string, type: "success" | "error") => {
    setToast({ message, type });
    setTimeout(() => setToast(null), 4000);
  };

  // Update local state when props change
  useEffect(() => {
    setAiName(settings.ai_name || "PuffPAI");
    setAvatarCharacterState(settings.avatar_character || "puff");
    setAvatarStyle(settings.avatar_style || "notionists");
    setAvatarModelUrl(settings.avatar_3d_url || "");
    setTemperature(settings.temperature ?? 0.7);
    setPersonality(settings.personality || "helpful");
    setContextLength(settings.context_length ?? 2048);
  }, [settings]);

  // Debounced temperature save
  const handleTemperatureChange = (value: number) => {
    setTemperature(value);
    if (tempTimer.current) clearTimeout(tempTimer.current);
    tempTimer.current = setTimeout(async () => {
      try {
        await updateAISettings({ temperature: value });
        onSaved({ ...settings, temperature: value });
      } catch {
        // Silently fail on debounced saves — user will see state in UI
      }
    }, 600);
  };

  /** Mirror the chosen engine + model URL into the live avatar + next page load. */
  const applyCharacter = (value: string, modelUrl?: string) => {
    if (!isAvatarEngineId(value)) return;
    dispatch(setAvatarCharacter(value));
    try {
      localStorage.setItem(AVATAR_CHARACTER_KEY, value);
    } catch {
      // storage disabled — the profile setting still persists server-side
    }
    // The custom model URL is mirrored unconditionally so store, localStorage
    // and the saved profile always agree with what was just sent. Blank means
    // "use the engine's built-in model", so it must actively CLEAR the stored
    // choice rather than be ignored — otherwise the field can never be un-set.
    const url = (modelUrl ?? "").trim();
    dispatch(setAvatarUrl(url));
    try {
      if (url) {
        localStorage.setItem(AVATAR_MODEL_URL_KEY, url);
      } else {
        localStorage.removeItem(AVATAR_MODEL_URL_KEY);
      }
    } catch {
      // ignore
    }
  };

  /**
   * Upload a .glb from this device. The appliance stores it under its data dir
   * and points the avatar at it, so there is nothing else to save afterwards —
   * the local state is mirrored here so the live avatar changes immediately.
   *
   * The .glb check here is a courtesy (a clearer message, sooner); the
   * appliance re-checks the real glTF header and is the one that decides.
   */
  const handleModelUpload = async (file: File | null) => {
    if (!file) return;
    if (!file.name.toLowerCase().endsWith(".glb")) {
      showToast("Pick a .glb file — export your avatar as glTF Binary.", "error");
      return;
    }
    setUploading(true);
    try {
      const res = await uploadAvatarModel(file);
      const url = res.model_url || AVATAR_MODEL_URL;
      setAvatarModelUrl(url);
      applyCharacter(avatarCharacter, url);
      onSaved({ ...settings, avatar_3d_url: url });
      showToast(
        `Model saved (${Math.round((res.bytes || file.size) / 1024)} KB)`,
        "success"
      );
    } catch (err: any) {
      showToast(err.message || "Upload failed", "error");
    } finally {
      setUploading(false);
      if (fileInput.current) fileInput.current.value = "";
    }
  };

  /** Remove the stored model and go back to the drawn Puff avatar. */
  const handleModelRemove = async () => {
    setUploading(true);
    try {
      await deleteAvatarModel();
      setAvatarModelUrl("");
      applyCharacter(avatarCharacter, "");
      onSaved({ ...settings, avatar_3d_url: "" });
      showToast("Model removed", "success");
    } catch (err: any) {
      showToast(err.message || "Remove failed", "error");
    } finally {
      setUploading(false);
    }
  };

  // Save identity (name, avatar, personality)
  const handleSaveIdentity = async () => {
    setSavingIdentity(true);
    try {
      const payload = {
        ai_name: aiName,
        avatar_style: avatarStyle,
        avatar_character: avatarCharacter,
        avatar_3d_url: avatarModelUrl.trim(),
        personality,
      };
      const res = await updateAISettings(payload);
      if (res.success) {
        applyCharacter(avatarCharacter, avatarModelUrl);
        showToast("AI identity saved", "success");
        onSaved({ ...settings, ...payload });
      } else {
        showToast("Failed to save identity", "error");
      }
    } catch (err: any) {
      showToast(err.message || "Failed to save identity", "error");
    } finally {
      setSavingIdentity(false);
    }
  };

  // Save model settings (temperature, context_length)
  const handleSaveSettings = async () => {
    setSavingSettings(true);
    try {
      const payload = {
        temperature,
        context_length: contextLength,
      };
      const res = await updateAISettings(payload);
      if (res.success) {
        showToast("Model settings saved", "success");
        onSaved({ ...settings, ...payload });
      } else {
        showToast("Failed to save settings", "error");
      }
    } catch (err: any) {
      showToast(err.message || "Failed to save settings", "error");
    } finally {
      setSavingSettings(false);
    }
  };

  // Reset to defaults
  const handleResetDefaults = async () => {
    setSavingSettings(true);
    try {
      const res = await updateAISettings(DEFAULT_SETTINGS);
      if (res.success) {
        setAiName(DEFAULT_SETTINGS.ai_name!);
        setAvatarStyle(DEFAULT_SETTINGS.avatar_style!);
        setAvatarCharacterState(DEFAULT_SETTINGS.avatar_character!);
        setAvatarModelUrl("");
        applyCharacter(DEFAULT_SETTINGS.avatar_character!);
        setPersonality(DEFAULT_SETTINGS.personality!);
        setTemperature(DEFAULT_SETTINGS.temperature!);
        setContextLength(DEFAULT_SETTINGS.context_length!);
        showToast("Reset to defaults", "success");
        onSaved({ ...settings, ...DEFAULT_SETTINGS } as AISettings);
      } else {
        showToast("Failed to reset", "error");
      }
    } catch (err: any) {
      showToast(err.message || "Failed to reset", "error");
    } finally {
      setSavingSettings(false);
    }
  };

  return (
    <>
      {/* Toast */}
      {toast && (
        <div
          className={`fixed top-4 right-4 z-50 flex items-center gap-2 rounded-lg px-4 py-3 text-sm font-medium text-white shadow-lg ${
            toast.type === "success" ? "bg-green-600" : "bg-red-600"
          }`}
        >
          <Icon
            path={toast.type === "success" ? mdiCheckCircle : mdiAlertCircle}
            size="18"
          />
          {toast.message}
        </div>
      )}

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        {/* AI Avatar & Identity */}
        <CardBox hasComponentLayout>
          <CardBoxComponentBody>
            {/* AI Avatar Preview */}
            <div className="mb-6 flex flex-col items-center">
              <div className="relative mb-4">
                <div className="flex h-28 w-28 items-center justify-center overflow-hidden rounded-full bg-gradient-to-br from-[#00B4D8] to-[#005F8A] text-5xl font-bold text-white shadow-lg">
                  {avatarCharacter === "puff" ? (
                    /* eslint-disable-next-line @next/next/no-img-element */
                    <img
                      src="/puffpai-logo.png"
                      alt="Puff, the PuffPAI cloud mascot"
                      className="h-20 w-20 object-contain"
                    />
                  ) : (
                    AVATAR_STYLES.find((s) => s.id === avatarStyle)?.emoji || "R"
                  )}
                </div>
                <div className="absolute -bottom-1 -right-1 flex h-8 w-8 items-center justify-center rounded-full bg-[#00B4D8] text-white shadow">
                  <Icon path={mdiPalette} size="16" />
                </div>
              </div>
              <h3 className="text-lg font-bold text-[#0C2F44] dark:text-white">{aiName}</h3>
              <p className="text-sm text-gray-500 dark:text-slate-400">Your AI assistant</p>
            </div>

            {/* AI Name */}
            <div className="mb-4">
              <label className="mb-1 block text-sm font-medium text-gray-700 dark:text-slate-300">
                AI Name
              </label>
              <div className="flex items-center gap-2">
                <Icon path={mdiRobot} size="20" className="text-[#00B4D8]" />
                <input
                  type="text"
                  value={aiName}
                  onChange={(e) => setAiName(e.target.value)}
                  className="w-full rounded-lg border border-gray-200 bg-white/50 px-3 py-2 text-sm backdrop-blur-sm focus:border-[#00B4D8] focus:ring-1 focus:ring-[#00B4D8] focus:outline-none dark:border-slate-600 dark:bg-slate-800/50 dark:text-white"
                />
              </div>
            </div>

            {/* Avatar engine — who actually shows up on the dashboard.
                Sourced from the engine registry, so new engines appear here. */}
            <div className="mb-4">
              <label className="mb-2 block text-sm font-medium text-gray-700 dark:text-slate-300">
                Avatar
              </label>
              <div className="grid grid-cols-3 gap-2">
                {AVATAR_CHARACTERS.map((choice) => (
                  <button
                    key={choice.id}
                    onClick={() => setAvatarCharacterState(choice.id)}
                    title={choice.sublabel}
                    className={`flex flex-col items-center gap-2 rounded-lg border-2 p-3 text-sm transition ${
                      avatarCharacter === choice.id
                        ? "border-[#00B4D8] bg-[#00B4D8]/10 text-[#005F8A] dark:text-[#00B4D8]"
                        : "border-gray-200 hover:border-gray-300 dark:border-slate-600 dark:hover:border-slate-500"
                    }`}
                  >
                    {choice.image ? (
                      /* eslint-disable-next-line @next/next/no-img-element */
                      <img
                        src={choice.image}
                        alt={choice.label}
                        className="h-12 w-12 object-contain"
                      />
                    ) : (
                      <span className="flex h-12 w-12 items-center justify-center rounded-full bg-gradient-to-br from-[#00B4D8]/30 to-[#005F8A]/30 text-base font-bold">
                        3D
                      </span>
                    )}
                    <span className="text-center text-xs font-medium leading-tight dark:text-slate-300">
                      {choice.label}
                    </span>
                  </button>
                ))}
              </div>

              {/* Your own 3D model — only for engines that load a GLB.
                  No model ships with this build, so this is the route to a 3D
                  face. The file goes to the appliance's data dir, not into this
                  checkout, and survives restarts and redeploys. */}
              {AVATAR_CHARACTERS.find((c) => c.id === avatarCharacter)
                ?.supportsCustomModel && (
                <div className="mt-3 rounded-lg border border-gray-200 p-3 dark:border-slate-600">
                  <div className="mb-2 flex items-center gap-2">
                    <Icon path={mdiCubeOutline} size="18" className="text-[#00B4D8]" />
                    <span className="text-xs font-medium text-gray-600 dark:text-slate-400">
                      Your own 3D model (.glb)
                    </span>
                  </div>

                  <p className="mb-2 text-[11px] text-gray-500 dark:text-slate-400">
                    No model ships with PuffPAI. Export one from your own
                    Ready&nbsp;Player&nbsp;Me / Avaturn account, or from Blender
                    as <em>glTF Binary (.glb)</em>, then upload it here. It is
                    stored on your appliance and never sent anywhere.{" "}
                    <a
                      href="https://github.com/CypherSisters/HomeBotBuild/blob/main/docs/AVATARS.md"
                      target="_blank"
                      rel="noreferrer"
                      className="underline"
                    >
                      What a model needs
                    </a>
                  </p>

                  <input
                    ref={fileInput}
                    type="file"
                    accept=".glb,model/gltf-binary"
                    className="hidden"
                    onChange={(e) => handleModelUpload(e.target.files?.[0] ?? null)}
                  />

                  <div className="flex flex-wrap items-center gap-2">
                    <Button
                      color="info"
                      small
                      icon={uploading ? mdiLoading : mdiUpload}
                      label={
                        avatarModelUrl === AVATAR_MODEL_URL
                          ? "Replace model"
                          : "Upload .glb"
                      }
                      onClick={() => fileInput.current?.click()}
                      disabled={uploading}
                    />
                    {avatarModelUrl === AVATAR_MODEL_URL && (
                      <Button
                        color="danger"
                        small
                        outline
                        icon={mdiDelete}
                        label="Remove"
                        onClick={handleModelRemove}
                        disabled={uploading}
                      />
                    )}
                    <span className="text-[11px] text-gray-500 dark:text-slate-400">
                      {avatarModelUrl === AVATAR_MODEL_URL
                        ? "Your uploaded model is in use."
                        : avatarModelUrl
                        ? "Using a model on your own network."
                        : "No model yet — Puff is drawn instead."}
                    </span>
                  </div>

                  {/* The paste-a-URL path, kept but fenced: the appliance
                      refuses anything that is not loopback / RFC 1918 /
                      unique-local (api_server._valid_avatar_url →
                      security.lan_http.validate_url), because a public URL here
                      would have every screen in the house fetch a model off the
                      internet with no door and no consent. */}
                  <details className="mt-3">
                    <summary className="cursor-pointer text-[11px] text-gray-500 dark:text-slate-400">
                      Or load it from a machine on your own network
                    </summary>
                    <input
                      type="url"
                      value={
                        avatarModelUrl === AVATAR_MODEL_URL ? "" : avatarModelUrl
                      }
                      onChange={(e) => setAvatarModelUrl(e.target.value)}
                      placeholder="http://192.168.1.50:8080/model.glb"
                      className="mt-2 w-full rounded-lg border border-gray-200 bg-white/50 px-3 py-2 text-sm backdrop-blur-sm focus:border-[#00B4D8] focus:ring-1 focus:ring-[#00B4D8] focus:outline-none dark:border-slate-600 dark:bg-slate-800/50 dark:text-white"
                    />
                    <p className="mt-1 text-[11px] text-gray-400 dark:text-slate-500">
                      House addresses only — a public URL is refused. Press
                      “Save identity” below to apply. Leave blank for none.
                    </p>
                  </details>
                </div>
              )}
            </div>

            {/* Avatar Style */}
            <div className="mb-4">
              <label className="mb-2 block text-sm font-medium text-gray-700 dark:text-slate-300">
                Avatar Style
              </label>
              <div className="grid grid-cols-2 gap-2">
                {AVATAR_STYLES.map((style) => (
                  <button
                    key={style.id}
                    onClick={() => setAvatarStyle(style.id)}
                    className={`flex items-center gap-2 rounded-lg border-2 p-2 text-sm transition ${
                      avatarStyle === style.id
                        ? "border-[#00B4D8] bg-[#00B4D8]/10 text-[#005F8A] dark:text-[#00B4D8]"
                        : "border-gray-200 hover:border-gray-300 dark:border-slate-600 dark:hover:border-slate-500"
                    }`}
                  >
                    <span className="flex h-8 w-8 items-center justify-center rounded-full bg-gradient-to-br from-[#00B4D8]/30 to-[#005F8A]/30 text-sm font-bold">
                      {style.emoji}
                    </span>
                    <span className="dark:text-slate-300">{style.label}</span>
                  </button>
                ))}
              </div>
            </div>

            {/* Personality */}
            <div>
              <label className="mb-1 block text-sm font-medium text-gray-700 dark:text-slate-300">
                Personality
              </label>
              <select
                value={personality}
                onChange={(e) => setPersonality(e.target.value)}
                className="w-full rounded-lg border border-gray-200 bg-white/50 px-3 py-2 text-sm backdrop-blur-sm focus:border-[#00B4D8] focus:ring-1 focus:ring-[#00B4D8] focus:outline-none dark:border-slate-600 dark:bg-slate-800/50 dark:text-white"
              >
                <option value="helpful">Helpful &amp; Friendly</option>
                <option value="concise">Concise &amp; Direct</option>
                <option value="creative">Creative &amp; Playful</option>
                <option value="technical">Technical &amp; Precise</option>
              </select>
            </div>
          </CardBoxComponentBody>

          <CardBoxComponentFooter>
            <Buttons>
              <Button
                color="info"
                label={savingIdentity ? "Saving..." : "Save Identity"}
                icon={savingIdentity ? mdiLoading : mdiContentSave}
                onClick={handleSaveIdentity}
                disabled={savingIdentity}
                isGrouped
              />
            </Buttons>
          </CardBoxComponentFooter>
        </CardBox>

        {/* Model Settings */}
        <CardBox hasComponentLayout>
          <CardBoxComponentBody>
            <h3 className="mb-4 flex items-center gap-2 text-lg font-semibold text-[#0C2F44] dark:text-white">
              <Icon path={mdiCog} size="20" className="text-[#00B4D8]" />
              Model Settings
            </h3>

            {/* Temperature Slider */}
            <div className="mb-6">
              <div className="mb-2 flex items-center justify-between">
                <label className="flex items-center gap-1 text-sm font-medium text-gray-700 dark:text-slate-300">
                  <Icon path={mdiThermometer} size="16" className="text-[#00B4D8]" />
                  Temperature
                </label>
                <span className="rounded-full bg-[#00B4D8]/10 px-2 py-0.5 text-xs font-medium text-[#005F8A] dark:text-[#00B4D8]">
                  {temperature.toFixed(1)}
                </span>
              </div>
              <input
                type="range"
                min="0"
                max="1"
                step="0.1"
                value={temperature}
                onChange={(e) => handleTemperatureChange(parseFloat(e.target.value))}
                className="w-full accent-[#00B4D8]"
              />
              <div className="mt-1 flex justify-between text-xs text-gray-400 dark:text-slate-500">
                <span>Precise</span>
                <span>Creative</span>
              </div>
            </div>

            {/* Context Length */}
            <div className="mb-6">
              <label className="mb-1 block text-sm font-medium text-gray-700 dark:text-slate-300">
                Context Length
              </label>
              <select
                value={contextLength}
                onChange={(e) => setContextLength(parseInt(e.target.value))}
                className="w-full rounded-lg border border-gray-200 bg-white/50 px-3 py-2 text-sm backdrop-blur-sm focus:border-[#00B4D8] focus:ring-1 focus:ring-[#00B4D8] focus:outline-none dark:border-slate-600 dark:bg-slate-800/50 dark:text-white"
              >
                <option value={1024}>1024 tokens (Fast)</option>
                <option value={2048}>2048 tokens (Balanced)</option>
                <option value={4096}>4096 tokens (Extended)</option>
                <option value={8192}>8192 tokens (Maximum)</option>
              </select>
            </div>

            {/* Model Info — from API */}
            <div className="mb-6">
              <label className="mb-1 block text-sm font-medium text-gray-700 dark:text-slate-300">
                LLM Backend
              </label>
              <div className="rounded-lg border border-gray-200 bg-gray-50/50 p-3 dark:border-slate-600 dark:bg-slate-800/50">
                <div className="flex items-center justify-between">
                  <span className="text-sm dark:text-slate-300">
                    {settings.model || "Unknown model"}
                  </span>
                  <span className="rounded-full bg-green-100 px-2 py-0.5 text-xs text-green-700 dark:bg-green-900/30 dark:text-green-400">
                    {settings.backend || "Unknown"}
                  </span>
                </div>
                <p className="mt-1 text-xs text-gray-400 dark:text-slate-500">
                  Production: llama.cpp on ARM hardware
                </p>
              </div>
            </div>

            {/* Embedding Model — from API */}
            <div>
              <label className="mb-1 block text-sm font-medium text-gray-700 dark:text-slate-300">
                Embedding Model
              </label>
              <div className="rounded-lg border border-gray-200 bg-gray-50/50 p-3 dark:border-slate-600 dark:bg-slate-800/50">
                <span className="text-sm dark:text-slate-300">
                  {settings.embedding_model || "Unknown"}
                </span>
                <p className="mt-1 text-xs text-gray-400 dark:text-slate-500">
                  384-dim sentence embeddings for RAG retrieval
                </p>
              </div>
            </div>
          </CardBoxComponentBody>

          <CardBoxComponentFooter>
            <Buttons>
              <Button
                color="info"
                label={savingSettings ? "Saving..." : "Save Settings"}
                icon={savingSettings ? mdiLoading : mdiContentSave}
                onClick={handleSaveSettings}
                disabled={savingSettings}
                isGrouped
              />
              <Button
                color="info"
                label="Reset to Defaults"
                icon={mdiRestore}
                outline
                onClick={handleResetDefaults}
                disabled={savingSettings}
                isGrouped
              />
            </Buttons>
          </CardBoxComponentFooter>
        </CardBox>
      </div>
    </>
  );
}
