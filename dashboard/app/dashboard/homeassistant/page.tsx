"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import {
  mdiHome,
  mdiHomeAssistant,
  mdiLightbulb,
  mdiLightbulbOn,
  mdiLightbulbOff,
  mdiThermometer,
  mdiVolumeHigh,
  mdiVolumeOff,
  mdiCctv,
  mdiCogOutline,
  mdiPower,
  mdiMotionSensor,
  mdiPlay,
  mdiPause,
  mdiStop,
  mdiSkipNext,
  mdiSkipPrevious,
  mdiPlus,
  mdiMinus,
  mdiRefresh,
  mdiOpenInNew,
  mdiCheckCircle,
  mdiAlertCircle,
  mdiClose,
  mdiFlash,
  mdiFlashOff,
  mdiSnowflake,
  mdiFire,
  mdiFan,
  mdiToggleSwitch,
  mdiToggleSwitchOff,
  mdiCamera,
  // mdiChevronUp,
  // mdiChevronDown,
  mdiVolumePlus,
  mdiVolumeMinus,
  mdiWalk,
  mdiAccountGroup,
  mdiDevices,
  mdiShieldLock,
  mdiShieldCheck,
  mdiHelpCircleOutline,
  mdiNewBox,
} from "@mdi/js";
import SectionMain from "../../_components/Section/Main";
import SectionTitleLineWithButton from "../../_components/Section/TitleLineWithButton";
import CardBox from "../../_components/CardBox";
import CardBoxWidget from "../../_components/CardBox/Widget";
import Icon from "../../_components/Icon";
import Button from "../../_components/Button";
import {
  getHAStatus,
  getHAEntities,
  callHAService,
  getHAAutomations,
  getHAScenes,
  getHADeviceOverview,
  approvePendingDevice,
  rejectPendingDevice,
  type HAEntity,
  type HAOverviewDevice,
  type HADeviceOverview,
} from "../../_lib/services/haService";
import Blueprint from "./_blueprint/Blueprint";

/* ------------------------------------------------------------------ */
/*  Constants                                                          */
/* ------------------------------------------------------------------ */

const REFRESH_INTERVAL_MS = 10_000;

type TabKey = "overview" | "lights" | "climate" | "media" | "security" | "automations" | "devices";

const TABS: { key: TabKey; label: string; icon: string }[] = [
  { key: "overview", label: "Overview", icon: mdiHome },
  { key: "devices", label: "Devices", icon: mdiDevices },
  { key: "lights", label: "Lights", icon: mdiLightbulb },
  { key: "climate", label: "Climate", icon: mdiThermometer },
  { key: "media", label: "Media", icon: mdiVolumeHigh },
  { key: "security", label: "Security", icon: mdiCctv },
  { key: "automations", label: "Automations", icon: mdiCogOutline },
];

/* Room definitions — group entities by room */
const ROOM_DEFINITIONS: {
  name: string;
  id: string;
  patterns: string[];
  icon: string;
  color: string;
}[] = [
  {
    name: "Bedroom",
    id: "bedroom",
    patterns: ["bedroom"],
    icon: mdiThermometer,
    color: "from-blue-400 to-blue-600",
  },
  {
    name: "Living Room",
    id: "living_room",
    patterns: ["living_room", "livingroom"],
    icon: mdiHome,
    color: "from-cyan-400 to-blue-500",
  },
  {
    name: "Outdoor",
    id: "outdoor",
    patterns: ["outdoor", "garden", "yard", "porch", "patio", "driveway"],
    icon: mdiCctv,
    color: "from-green-400 to-emerald-600",
  },
  {
    name: "Voice Assistant",
    id: "voice_assistant",
    patterns: ["home_assistant_voice"],
    icon: mdiVolumeHigh,
    color: "from-purple-400 to-violet-600",
  },
  {
    name: "Network",
    id: "network",
    patterns: ["router", "gateway", "mesh", "access_point"],
    icon: mdiFlash,
    color: "from-orange-400 to-red-500",
  },
];

/* ------------------------------------------------------------------ */
/*  Main Page Component                                                */
/* ------------------------------------------------------------------ */

export default function HomeAssistantPage() {
  // Connection state
  const [connected, setConnected] = useState(false);
  const [haUrl, setHaUrl] = useState("http://homeassistant.local:8123");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  // Entity state
  const [entities, setEntities] = useState<HAEntity[]>([]);
  const [automations, setAutomations] = useState<HAEntity[]>([]);
  const [scenes, setScenes] = useState<HAEntity[]>([]);
  const [deviceOverview, setDeviceOverview] = useState<HADeviceOverview | null>(null);

  // UI state
  const [activeTab, setActiveTab] = useState<TabKey>("overview");

  // The page opens on the Blueprint; the tabs below live behind "Lists".
  // Remembered per browser so the kiosk keeps whichever face it was left on.
  const [view, setView] = useState<"blueprint" | "lists">("blueprint");
  const [blueprintNote, setBlueprintNote] = useState("");
  const viewRef = useRef<"blueprint" | "lists">("blueprint");

  // Stable: Blueprint holds this in a fetch callback's dependency list.
  const handleBlueprintUnavailable = useCallback((message: string) => {
    setBlueprintNote(message);
    setView("lists");
  }, []);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [toastMessage, setToastMessage] = useState("");
  const [toastColor, setToastColor] = useState<"success" | "danger">("success");

  // Camera refresh counter
  const [cameraRefreshKey, setCameraRefreshKey] = useState(0);

  const refreshTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  /* ---------------------------------------------------------------- */
  /*  Data fetching                                                    */
  /* ---------------------------------------------------------------- */

  const showToast = useCallback((msg: string, color: "success" | "danger") => {
    setToastMessage(msg);
    setToastColor(color);
    setTimeout(() => setToastMessage(""), 4000);
  }, []);

  const fetchAll = useCallback(async () => {
    try {
      const [statusRes, entitiesRes, autoRes, sceneRes, deviceRes] = await Promise.allSettled([
        getHAStatus(),
        getHAEntities(),
        getHAAutomations(),
        getHAScenes(),
        getHADeviceOverview(),
      ]);

      if (statusRes.status === "fulfilled") {
        setConnected(statusRes.value.connected);
        if (statusRes.value.ha_url) setHaUrl(statusRes.value.ha_url);
      } else {
        setConnected(false);
      }

      if (entitiesRes.status === "fulfilled") {
        setEntities(entitiesRes.value.entities || []);
      }

      if (autoRes.status === "fulfilled") {
        // getHAAutomations returns HAAutomation[]; the panel reads entity_id/state/
        // attributes defensively (see `(auto as any).last_triggered`), so bridge the type.
        setAutomations(autoRes.value as unknown as HAEntity[]);
      }

      if (sceneRes.status === "fulfilled") {
        setScenes(sceneRes.value);
      }

      if (deviceRes.status === "fulfilled") {
        setDeviceOverview(deviceRes.value);
      }

      setLastUpdated(new Date());
      setError("");
    } catch (err: any) {
      setError(err.message || "Failed to connect to Home Assistant");
      setConnected(false);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    try {
      const saved = localStorage.getItem("puffpai_ha_view");
      if (saved === "lists" || saved === "blueprint") setView(saved);
    } catch {
      /* storage disabled: Blueprint stays the default */
    }
  }, []);

  useEffect(() => {
    viewRef.current = view;
    try {
      localStorage.setItem("puffpai_ha_view", view);
    } catch {
      /* storage disabled: the choice lasts this session only */
    }
  }, [view]);

  useEffect(() => {
    fetchAll();
    refreshTimerRef.current = setInterval(() => {
      // The Blueprint runs its own 5s poll; do not double up on the bridge.
      if (viewRef.current !== "lists") return;
      fetchAll();
      setCameraRefreshKey((k) => k + 1);
    }, REFRESH_INTERVAL_MS);
    return () => {
      if (refreshTimerRef.current) clearInterval(refreshTimerRef.current);
    };
  }, [fetchAll]);

  /* ---------------------------------------------------------------- */
  /*  Entity helpers                                                   */
  /* ---------------------------------------------------------------- */

  const byDomain = (domain: string) =>
    entities.filter((e) => e.entity_id.startsWith(domain + "."));

  const lights = byDomain("light");
  const climates = byDomain("climate");
  const mediaPlayers = byDomain("media_player");
  const cameras = byDomain("camera");
  const binarySensors = byDomain("binary_sensor");
  const switches = byDomain("switch");

  const lightsOn = lights.filter((l) => l.state === "on").length;
  const mediaPlaying = mediaPlayers.filter((m) =>
    ["playing", "paused"].includes(m.state)
  ).length;
  const motionDetected = binarySensors.filter(
    (b) =>
      b.state === "on" &&
      (b.attributes.device_class === "motion" ||
        b.entity_id.includes("motion"))
  ).length;

  // Average temperature from climate entities
  const climateTemps = climates
    .map((c) => c.attributes.current_temperature)
    .filter((t): t is number => typeof t === "number");
  const avgTemp =
    climateTemps.length > 0
      ? Math.round((climateTemps.reduce((a, b) => a + b, 0) / climateTemps.length) * 10) / 10
      : null;

  // Room grouping
  function getEntitiesForRoom(patterns: string[]): HAEntity[] {
    return entities.filter((e) =>
      patterns.some((p) => e.entity_id.toLowerCase().includes(p.toLowerCase()))
    );
  }

  /* ---------------------------------------------------------------- */
  /*  Service call helpers                                             */
  /* ---------------------------------------------------------------- */

  const toggleLight = async (entityId: string, currentState: string) => {
    try {
      const service = currentState === "on" ? "turn_off" : "turn_on";
      await callHAService("light", service, { entity_id: entityId });
      showToast(`Light ${service === "turn_on" ? "turned on" : "turned off"}`, "success");
      setTimeout(fetchAll, 500);
    } catch {
      showToast("Failed to toggle light", "danger");
    }
  };

  const setBrightness = async (entityId: string, brightness: number) => {
    try {
      await callHAService("light", "turn_on", {
        entity_id: entityId,
        brightness: Math.round(brightness * 2.55),
      });
      setTimeout(fetchAll, 500);
    } catch {
      showToast("Failed to set brightness", "danger");
    }
  };

  const setClimateTemp = async (entityId: string, temperature: number) => {
    try {
      await callHAService("climate", "set_temperature", {
        entity_id: entityId,
        temperature,
      });
      showToast(`Temperature set to ${temperature}`, "success");
      setTimeout(fetchAll, 500);
    } catch {
      showToast("Failed to set temperature", "danger");
    }
  };

  const setHvacMode = async (entityId: string, mode: string) => {
    try {
      await callHAService("climate", "set_hvac_mode", {
        entity_id: entityId,
        hvac_mode: mode,
      });
      showToast(`HVAC mode set to ${mode}`, "success");
      setTimeout(fetchAll, 500);
    } catch {
      showToast("Failed to set HVAC mode", "danger");
    }
  };

  const mediaControl = async (entityId: string, service: string) => {
    try {
      await callHAService("media_player", service, { entity_id: entityId });
      setTimeout(fetchAll, 500);
    } catch {
      showToast(`Failed: ${service}`, "danger");
    }
  };

  const setVolume = async (entityId: string, volume: number) => {
    try {
      await callHAService("media_player", "volume_set", {
        entity_id: entityId,
        volume_level: volume,
      });
      setTimeout(fetchAll, 500);
    } catch {
      showToast("Failed to set volume", "danger");
    }
  };

  const toggleAutomation = async (entityId: string, currentState: string) => {
    try {
      const service = currentState === "on" ? "turn_off" : "turn_on";
      await callHAService("automation", service, { entity_id: entityId });
      showToast(`Automation ${service === "turn_on" ? "enabled" : "disabled"}`, "success");
      setTimeout(fetchAll, 500);
    } catch {
      showToast("Failed to toggle automation", "danger");
    }
  };

  const triggerAutomation = async (entityId: string) => {
    try {
      await callHAService("automation", "trigger", { entity_id: entityId });
      showToast("Automation triggered", "success");
    } catch {
      showToast("Failed to trigger automation", "danger");
    }
  };

  const activateScene = async (entityId: string) => {
    try {
      await callHAService("scene", "turn_on", { entity_id: entityId });
      showToast("Scene activated", "success");
    } catch {
      showToast("Failed to activate scene", "danger");
    }
  };

  const approveDevice = async (
    entityId: string,
    securityMode: "balanced" | "lockdown",
  ) => {
    try {
      await approvePendingDevice(entityId, securityMode);
      showToast(
        `Device approved (${securityMode === "lockdown" ? "locked down" : "balanced"})`,
        "success",
      );
      setTimeout(fetchAll, 500);
    } catch {
      showToast("Failed to approve device", "danger");
    }
  };

  const rejectDevice = async (entityId: string) => {
    try {
      await rejectPendingDevice(entityId);
      showToast("Device rejected", "success");
      setTimeout(fetchAll, 500);
    } catch {
      showToast("Failed to reject device", "danger");
    }
  };

  /* ---------------------------------------------------------------- */
  /*  Render                                                           */
  /* ---------------------------------------------------------------- */

  return (
    <SectionMain>
      {/* ---- Header ---- */}
      <SectionTitleLineWithButton icon={mdiHomeAssistant} title="Smart Home" main>
        <div className="flex items-center gap-3">
          {/* Connection indicator */}
          <span className="flex items-center gap-2 text-sm">
            <span
              className={`inline-block h-2.5 w-2.5 rounded-full ${
                connected
                  ? "bg-green-400 shadow-[0_0_6px_rgba(74,222,128,.6)]"
                  : "bg-red-400 shadow-[0_0_6px_rgba(248,113,113,.6)]"
              }`}
            />
            <span className="text-gray-600 dark:text-slate-400">
              {connected ? "Connected" : "Disconnected"}
            </span>
          </span>
          <Button
            label="Open HA"
            icon={mdiOpenInNew}
            href={haUrl}
            target="_blank"
            color="info"
            small
            roundedFull
          />
          <Button
            icon={mdiRefresh}
            label="Refresh"
            color="whiteDark"
            small
            roundedFull
            onClick={fetchAll}
          />
        </div>
      </SectionTitleLineWithButton>

      {/* ---- Blueprint | Lists ---- */}
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <div
          className="inline-flex overflow-hidden rounded-sm border"
          style={{ borderColor: "var(--line)" }}
        >
          {(
            [
              ["blueprint", "Blueprint"],
              ["lists", "Lists"],
            ] as const
          ).map(([key, label]) => (
            <button
              key={key}
              type="button"
              onClick={() => setView(key)}
              className="px-3 py-1.5 text-[11px] font-semibold tracking-[0.14em] uppercase"
              style={{
                background: view === key ? "var(--chip)" : "transparent",
                color: view === key ? "var(--text)" : "var(--text2)",
              }}
            >
              {label}
            </button>
          ))}
        </div>
        {blueprintNote && (
          <span className="text-xs" style={{ color: "var(--text3)" }}>
            {blueprintNote}
          </span>
        )}
      </div>

      {view === "blueprint" && <Blueprint onUnavailable={handleBlueprintUnavailable} />}

      {view === "lists" && (
        <>
      {/* ---- Toast ---- */}
      {toastMessage && (
        <div
          className={`mb-4 flex items-center justify-between rounded-lg border px-4 py-3 text-sm transition-all ${
            toastColor === "success"
              ? "border-green-200 bg-green-50 text-green-700 dark:border-green-800 dark:bg-green-900/20 dark:text-green-400"
              : "border-red-200 bg-red-50 text-red-700 dark:border-red-800 dark:bg-red-900/20 dark:text-red-400"
          }`}
        >
          <div className="flex items-center gap-2">
            <Icon
              path={toastColor === "success" ? mdiCheckCircle : mdiAlertCircle}
              size="18"
            />
            {toastMessage}
          </div>
          <button onClick={() => setToastMessage("")} className="opacity-60 hover:opacity-100">
            <Icon path={mdiClose} size="16" />
          </button>
        </div>
      )}

      {/* ---- Error banner ---- */}
      {error && !loading && (
        <div className="mb-6 flex items-center gap-3 rounded-xl border border-red-200 bg-red-50 px-5 py-4 dark:border-red-800/40 dark:bg-red-900/20">
          <Icon path={mdiAlertCircle} size="24" className="text-red-500" />
          <div className="flex-1">
            <p className="font-medium text-red-700 dark:text-red-400">
              Home Assistant Unreachable
            </p>
            <p className="mt-0.5 text-sm text-red-600/80 dark:text-red-300/70">
              {error}. Make sure the PuffPAI backend is running and HA is configured.
            </p>
          </div>
          <Button
            icon={mdiRefresh}
            label="Retry"
            color="danger"
            small
            roundedFull
            onClick={fetchAll}
          />
        </div>
      )}

      {/* ---- Tab Navigation ---- */}
      <div className="mb-6 flex gap-1 overflow-x-auto rounded-xl bg-white/60 p-1 shadow-sm backdrop-blur-sm dark:bg-slate-900/50">
        {TABS.map((tab) => {
          const badgeCount =
            tab.key === "devices" ? deviceOverview?.counts.new ?? 0 : 0;
          return (
            <button
              key={tab.key}
              onClick={() => setActiveTab(tab.key)}
              className={`flex items-center gap-2 whitespace-nowrap rounded-lg px-4 py-2.5 text-sm font-medium transition-all ${
                activeTab === tab.key
                  ? "bg-gradient-to-r from-[#005F8A] to-[#00B4D8] text-white shadow-md"
                  : "text-gray-600 hover:bg-gray-100 dark:text-slate-400 dark:hover:bg-slate-800"
              }`}
            >
              <Icon path={tab.icon} size="18" />
              {tab.label}
              {badgeCount > 0 && (
                <span
                  className={`ml-0.5 inline-flex min-w-[1.25rem] items-center justify-center rounded-full px-1.5 py-0.5 text-xs font-semibold ${
                    activeTab === tab.key
                      ? "bg-white/25 text-white"
                      : "bg-[#EF4444] text-white"
                  }`}
                >
                  {badgeCount}
                </span>
              )}
            </button>
          );
        })}
      </div>

      {/* ---- Loading skeleton ---- */}
      {loading && (
        <div className="grid grid-cols-1 gap-6 sm:grid-cols-2 lg:grid-cols-4">
          {[0, 1, 2, 3].map((i) => (
            <CardBox key={i}>
              <div className="animate-pulse">
                <div className="mb-3 h-5 w-20 rounded-full bg-gray-200 dark:bg-slate-700" />
                <div className="h-8 w-16 rounded bg-gray-200 dark:bg-slate-700" />
              </div>
            </CardBox>
          ))}
        </div>
      )}

      {/* ---- Tab content ---- */}
      {!loading && activeTab === "overview" && (
        <OverviewTab
          lightsOn={lightsOn}
          lightsTotal={lights.length}
          avgTemp={avgTemp}
          mediaPlaying={mediaPlaying}
          motionDetected={motionDetected}
          entities={entities}
          rooms={ROOM_DEFINITIONS}
          getEntitiesForRoom={getEntitiesForRoom}
        />
      )}

      {!loading && activeTab === "devices" && (
        <DevicesTab
          overview={deviceOverview}
          onApprove={approveDevice}
          onReject={rejectDevice}
        />
      )}

      {!loading && activeTab === "lights" && (
        <LightsTab
          lights={lights}
          switches={switches}
          onToggle={toggleLight}
          onBrightness={setBrightness}
        />
      )}

      {!loading && activeTab === "climate" && (
        <ClimateTab
          climates={climates}
          binarySensors={binarySensors}
          onSetTemp={setClimateTemp}
          onSetMode={setHvacMode}
        />
      )}

      {!loading && activeTab === "media" && (
        <MediaTab
          players={mediaPlayers}
          onControl={mediaControl}
          onVolume={setVolume}
        />
      )}

      {!loading && activeTab === "security" && (
        <SecurityTab
          cameras={cameras}
          binarySensors={binarySensors}
          cameraRefreshKey={cameraRefreshKey}
          haUrl={haUrl}
        />
      )}

      {!loading && activeTab === "automations" && (
        <AutomationsTab
          automations={automations}
          scenes={scenes}
          onToggle={toggleAutomation}
          onTrigger={triggerAutomation}
          onActivateScene={activateScene}
        />
      )}

      {/* ---- Last updated footer ---- */}
      {lastUpdated && (
        <CardBox className="mt-6">
          <div className="flex items-center gap-3 text-sm text-gray-500 dark:text-slate-400">
            <Icon path={mdiRefresh} size="18" />
            <span>
              Last updated: {lastUpdated.toLocaleTimeString()} — Auto-refresh every 10 seconds
            </span>
          </div>
        </CardBox>
      )}
        </>
      )}
    </SectionMain>
  );
}

/* ================================================================== */
/*  OVERVIEW TAB                                                       */
/* ================================================================== */

function OverviewTab({
  lightsOn,
  lightsTotal,
  avgTemp,
  mediaPlaying,
  motionDetected,
  entities: _entities,
  rooms,
  getEntitiesForRoom,
}: {
  lightsOn: number;
  lightsTotal: number;
  avgTemp: number | null;
  mediaPlaying: number;
  motionDetected: number;
  entities: HAEntity[];
  rooms: typeof ROOM_DEFINITIONS;
  getEntitiesForRoom: (patterns: string[]) => HAEntity[];
}) {
  return (
    <>
      {/* Status widgets */}
      <div className="mb-6 grid grid-cols-1 gap-6 sm:grid-cols-2 lg:grid-cols-4">
        <CardBoxWidget
          trendLabel={lightsOn > 0 ? `${lightsOn} on` : "All off"}
          trendType={lightsOn > 0 ? "success" : "info"}
          trendColor={lightsOn > 0 ? "success" : "info"}
          icon={mdiLightbulb}
          iconColor="warning"
          number={lightsTotal}
          label="Lights"
          numberSuffix=" total"
        />
        <CardBoxWidget
          trendLabel={avgTemp !== null ? `${avgTemp}°F avg` : "No data"}
          trendType="info"
          trendColor="info"
          icon={mdiThermometer}
          iconColor="info"
          number={avgTemp ?? 0}
          label="Climate"
          numberSuffix="°"
        />
        <CardBoxWidget
          trendLabel={mediaPlaying > 0 ? `${mediaPlaying} active` : "Idle"}
          trendType={mediaPlaying > 0 ? "success" : "info"}
          trendColor={mediaPlaying > 0 ? "success" : "info"}
          icon={mdiVolumeHigh}
          iconColor="success"
          number={mediaPlaying}
          label="Media"
          numberSuffix=" playing"
        />
        <CardBoxWidget
          trendLabel={motionDetected > 0 ? "Motion!" : "Clear"}
          trendType={motionDetected > 0 ? "danger" : "success"}
          trendColor={motionDetected > 0 ? "danger" : "success"}
          icon={mdiMotionSensor}
          iconColor={motionDetected > 0 ? "danger" : "success"}
          number={motionDetected}
          label="Motion"
          numberSuffix=" detected"
        />
      </div>

      {/* Rooms */}
      <SectionTitleLineWithButton icon={mdiHome} title="Rooms" />
      <div className="mb-6 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {rooms.map((room) => {
          const roomEntities = getEntitiesForRoom(room.patterns);
          if (roomEntities.length === 0) return null;

          // Key state summaries
          const roomLights = roomEntities.filter((e) => e.entity_id.startsWith("light."));
          const roomClimate = roomEntities.find((e) => e.entity_id.startsWith("climate."));
          const roomMotion = roomEntities.filter(
            (e) =>
              e.entity_id.startsWith("binary_sensor.") &&
              (e.attributes.device_class === "motion" || e.entity_id.includes("motion"))
          );
          const roomOccupancy = roomEntities.filter(
            (e) =>
              e.entity_id.startsWith("binary_sensor.") &&
              (e.attributes.device_class === "occupancy" || e.entity_id.includes("occupancy"))
          );
          const lightsOnCount = roomLights.filter((l) => l.state === "on").length;
          const hasMotion = roomMotion.some((m) => m.state === "on");
          const isOccupied = roomOccupancy.some((o) => o.state === "on");

          return (
            <CardBox key={room.id} isHoverable>
              <div className="flex items-start gap-4">
                <div
                  className={`flex h-12 w-12 flex-shrink-0 items-center justify-center rounded-xl bg-gradient-to-br ${room.color} text-white shadow-md`}
                >
                  <Icon path={room.icon} size="24" />
                </div>
                <div className="flex-1 min-w-0">
                  <h3 className="font-semibold text-[#0C2F44] dark:text-white">
                    {room.name}
                  </h3>
                  <p className="text-xs text-gray-500 dark:text-slate-400">
                    {roomEntities.length} entities
                  </p>
                  <div className="mt-2 flex flex-wrap gap-2">
                    {roomClimate && (
                      <span className="inline-flex items-center gap-1 rounded-full bg-[#00B4D8]/10 px-2 py-0.5 text-xs text-[#00B4D8]">
                        <Icon path={mdiThermometer} size="12" />
                        {roomClimate.attributes.current_temperature ?? "---"}&deg;
                      </span>
                    )}
                    {roomLights.length > 0 && (
                      <span
                        className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs ${
                          lightsOnCount > 0
                            ? "bg-[#FBBF24]/15 text-[#D97706]"
                            : "bg-gray-100 text-gray-500 dark:bg-slate-700 dark:text-slate-400"
                        }`}
                      >
                        <Icon path={mdiLightbulb} size="12" />
                        {lightsOnCount}/{roomLights.length} on
                      </span>
                    )}
                    {hasMotion && (
                      <span className="inline-flex items-center gap-1 rounded-full bg-[#EF4444]/10 px-2 py-0.5 text-xs text-[#EF4444]">
                        <Icon path={mdiMotionSensor} size="12" />
                        Motion
                      </span>
                    )}
                    {isOccupied && (
                      <span className="inline-flex items-center gap-1 rounded-full bg-[#4ADE80]/10 px-2 py-0.5 text-xs text-[#16A34A]">
                        <Icon path={mdiAccountGroup} size="12" />
                        Occupied
                      </span>
                    )}
                  </div>
                </div>
              </div>
            </CardBox>
          );
        })}
      </div>
    </>
  );
}

/* ================================================================== */
/*  DEVICES TAB                                                        */
/* ================================================================== */

const DOMAIN_ICONS: Record<string, string> = {
  light: mdiLightbulb,
  switch: mdiToggleSwitch,
  climate: mdiThermometer,
  media_player: mdiVolumeHigh,
  camera: mdiCctv,
  binary_sensor: mdiMotionSensor,
  sensor: mdiMotionSensor,
  fan: mdiFan,
  lock: mdiShieldLock,
  cover: mdiHome,
};

function SecurityModeChip({ mode }: { mode: string | null }) {
  if (!mode) return null;
  const styles: Record<string, string> = {
    lockdown: "bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400",
    balanced: "bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400",
    open: "bg-gray-100 text-gray-600 dark:bg-slate-700 dark:text-slate-400",
    allow: "bg-gray-100 text-gray-600 dark:bg-slate-700 dark:text-slate-400",
    auto: "bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400",
  };
  const icon =
    mode === "lockdown" ? mdiShieldLock : mode === "balanced" ? mdiShieldCheck : mdiHelpCircleOutline;
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ${
        styles[mode] ?? styles.open
      }`}
    >
      <Icon path={icon} size="12" />
      {capitalize(mode)}
    </span>
  );
}

function DeviceRow({ device }: { device: HAOverviewDevice }) {
  return (
    <div className="flex items-center gap-3 px-4 py-3">
      <Icon
        path={DOMAIN_ICONS[device.domain] ?? mdiDevices}
        size="20"
        className="flex-shrink-0 text-gray-400 dark:text-slate-500"
      />
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm font-medium text-[#0C2F44] dark:text-white">
          {device.name}
        </p>
        <p className="text-xs text-gray-500 dark:text-slate-400">
          {device.domain} · {device.state}
        </p>
      </div>
      {device.security_mode && <SecurityModeChip mode={device.security_mode} />}
    </div>
  );
}

function DevicesTab({
  overview,
  onApprove,
  onReject,
}: {
  overview: HADeviceOverview | null;
  onApprove: (entityId: string, mode: "balanced" | "lockdown") => void;
  onReject: (entityId: string) => void;
}) {
  if (!overview) {
    return (
      <CardBox>
        <div className="py-8 text-center text-gray-400 dark:text-slate-500">
          <Icon path={mdiDevices} size="32" className="mx-auto mb-2 opacity-40" />
          <p>Device data unavailable.</p>
        </div>
      </CardBox>
    );
  }

  const newDevices = overview.devices.filter((d) => d.is_new);
  const managed = overview.devices.filter((d) => d.managed);
  const unmanaged = overview.devices.filter((d) => !d.managed && !d.is_new);

  return (
    <>
      {/* Count widgets */}
      <div className="mb-6 grid grid-cols-2 gap-4 sm:grid-cols-4">
        <CardBoxWidget
          trendLabel="on network"
          trendType="info"
          trendColor="info"
          icon={mdiDevices}
          iconColor="info"
          number={overview.counts.total}
          label="Total"
        />
        <CardBoxWidget
          trendLabel={overview.counts.new > 0 ? "action needed" : "all reviewed"}
          trendType={overview.counts.new > 0 ? "danger" : "success"}
          trendColor={overview.counts.new > 0 ? "danger" : "success"}
          icon={mdiNewBox}
          iconColor={overview.counts.new > 0 ? "danger" : "success"}
          number={overview.counts.new}
          label="New"
        />
        <CardBoxWidget
          trendLabel="approved"
          trendType="success"
          trendColor="success"
          icon={mdiShieldCheck}
          iconColor="success"
          number={overview.counts.managed}
          label="Managed"
        />
        <CardBoxWidget
          trendLabel="not managed"
          trendType="info"
          trendColor="info"
          icon={mdiHelpCircleOutline}
          iconColor="warning"
          number={overview.counts.unmanaged}
          label="Unmanaged"
        />
      </div>

      {/* New devices — prominent */}
      <SectionTitleLineWithButton icon={mdiNewBox} title="New Devices" />
      <div className="mb-6 grid grid-cols-1 gap-4 lg:grid-cols-2">
        {newDevices.map((device) => (
          <CardBox
            key={device.entity_id}
            className="border-l-4 border-l-[#EF4444]"
          >
            <div className="flex items-start gap-3">
              <div className="flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-full bg-[#EF4444]/15">
                <Icon
                  path={DOMAIN_ICONS[device.domain] ?? mdiDevices}
                  size="22"
                  className="text-[#EF4444]"
                />
              </div>
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <h3 className="truncate font-semibold text-[#0C2F44] dark:text-white">
                    {device.name}
                  </h3>
                  <span className="rounded-full bg-[#EF4444] px-2 py-0.5 text-xs font-semibold text-white">
                    NEW
                  </span>
                </div>
                <p className="text-xs text-gray-500 dark:text-slate-400">
                  {device.entity_id} · {device.state}
                </p>
              </div>
            </div>

            <div className="mt-4 flex flex-wrap gap-2">
              <button
                onClick={() => onApprove(device.entity_id, "balanced")}
                className="flex items-center gap-1.5 rounded-full bg-[#00B4D8] px-3 py-1.5 text-xs font-medium text-white shadow-sm transition hover:bg-[#0096C7]"
              >
                <Icon path={mdiShieldCheck} size="14" />
                Approve (balanced)
              </button>
              <button
                onClick={() => onApprove(device.entity_id, "lockdown")}
                className="flex items-center gap-1.5 rounded-full bg-[#005F8A] px-3 py-1.5 text-xs font-medium text-white shadow-sm transition hover:bg-[#0C2F44]"
              >
                <Icon path={mdiShieldLock} size="14" />
                Approve (locked down)
              </button>
              <button
                onClick={() => onReject(device.entity_id)}
                className="flex items-center gap-1.5 rounded-full bg-gray-100 px-3 py-1.5 text-xs font-medium text-gray-600 transition hover:bg-red-100 hover:text-red-600 dark:bg-slate-700 dark:text-slate-300 dark:hover:bg-red-900/30 dark:hover:text-red-400"
              >
                <Icon path={mdiClose} size="14" />
                Reject
              </button>
            </div>
          </CardBox>
        ))}

        {newDevices.length === 0 && (
          <CardBox className="lg:col-span-2">
            <div className="py-6 text-center text-gray-400 dark:text-slate-500">
              <Icon path={mdiCheckCircle} size="28" className="mx-auto mb-2 text-green-400 opacity-70" />
              <p>No new devices — everything on the network has been reviewed.</p>
            </div>
          </CardBox>
        )}
      </div>

      {/* Managed devices */}
      <SectionTitleLineWithButton icon={mdiShieldCheck} title={`Managed (${managed.length})`} />
      <CardBox className="mb-6 !p-0">
        <div className="divide-y divide-gray-100 dark:divide-slate-700">
          {managed.map((device) => (
            <DeviceRow key={device.entity_id} device={device} />
          ))}
          {managed.length === 0 && (
            <div className="py-6 text-center text-sm text-gray-400 dark:text-slate-500">
              No managed devices yet.
            </div>
          )}
        </div>
      </CardBox>

      {/* Unmanaged devices */}
      <SectionTitleLineWithButton icon={mdiHelpCircleOutline} title={`Unmanaged (${unmanaged.length})`} />
      <CardBox className="mb-6 !p-0">
        <div className="divide-y divide-gray-100 dark:divide-slate-700">
          {unmanaged.map((device) => (
            <DeviceRow key={device.entity_id} device={device} />
          ))}
          {unmanaged.length === 0 && (
            <div className="py-6 text-center text-sm text-gray-400 dark:text-slate-500">
              No unmanaged devices.
            </div>
          )}
        </div>
      </CardBox>
    </>
  );
}

/* ================================================================== */
/*  LIGHTS TAB                                                         */
/* ================================================================== */

function LightsTab({
  lights,
  switches,
  onToggle,
  onBrightness,
}: {
  lights: HAEntity[];
  switches: HAEntity[];
  onToggle: (entityId: string, state: string) => void;
  onBrightness: (entityId: string, brightness: number) => void;
}) {
  return (
    <>
      <SectionTitleLineWithButton icon={mdiLightbulb} title="Lights" />
      <div className="mb-6 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {lights.map((light) => {
          const isOn = light.state === "on";
          const isUnavailable = light.state === "unavailable";
          const brightness = light.attributes.brightness
            ? Math.round((light.attributes.brightness / 255) * 100)
            : null;
          const supportsBrightness =
            light.attributes.supported_features !== undefined &&
            (light.attributes.supported_features & 1) !== 0;

          return (
            <CardBox
              key={light.entity_id}
              className={`border-l-4 ${
                isUnavailable
                  ? "border-l-red-400"
                  : isOn
                    ? "border-l-[#FBBF24]"
                    : "border-l-gray-300 dark:border-l-slate-600"
              }`}
            >
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-3">
                  <div
                    className={`flex h-10 w-10 items-center justify-center rounded-full ${
                      isUnavailable
                        ? "bg-red-100 dark:bg-red-900/30"
                        : isOn
                          ? "bg-[#FBBF24]/20"
                          : "bg-gray-100 dark:bg-slate-700"
                    }`}
                  >
                    <Icon
                      path={isOn ? mdiLightbulbOn : mdiLightbulbOff}
                      size="22"
                      className={
                        isUnavailable
                          ? "text-red-400"
                          : isOn
                            ? "text-[#FBBF24]"
                            : "text-gray-400 dark:text-slate-500"
                      }
                    />
                  </div>
                  <div>
                    <p className="text-sm font-medium text-[#0C2F44] dark:text-white">
                      {friendlyName(light)}
                    </p>
                    <p className="text-xs text-gray-500 dark:text-slate-400">
                      {isUnavailable
                        ? "Unavailable"
                        : isOn
                          ? brightness !== null
                            ? `On - ${brightness}%`
                            : "On"
                          : "Off"}
                    </p>
                  </div>
                </div>
                <button
                  onClick={() => onToggle(light.entity_id, light.state)}
                  disabled={isUnavailable}
                  className={`flex h-10 w-10 items-center justify-center rounded-full transition ${
                    isUnavailable
                      ? "cursor-not-allowed opacity-40"
                      : isOn
                        ? "bg-[#FBBF24] text-white shadow-md hover:bg-[#D97706]"
                        : "bg-gray-200 text-gray-500 hover:bg-gray-300 dark:bg-slate-600 dark:text-slate-300 dark:hover:bg-slate-500"
                  }`}
                >
                  <Icon path={mdiPower} size="20" />
                </button>
              </div>

              {/* Brightness slider */}
              {(supportsBrightness || brightness !== null) && !isUnavailable && (
                <div className="mt-3 flex items-center gap-3">
                  <Icon path={mdiLightbulbOff} size="14" className="text-gray-400" />
                  <input
                    type="range"
                    min={0}
                    max={100}
                    value={brightness ?? 0}
                    onChange={(e) =>
                      onBrightness(light.entity_id, parseInt(e.target.value, 10))
                    }
                    className="flex-1 accent-[#FBBF24]"
                  />
                  <Icon path={mdiLightbulbOn} size="14" className="text-[#FBBF24]" />
                </div>
              )}
            </CardBox>
          );
        })}

        {lights.length === 0 && (
          <CardBox className="col-span-full">
            <div className="py-8 text-center text-gray-400 dark:text-slate-500">
              <Icon path={mdiLightbulb} size="32" className="mx-auto mb-2 opacity-40" />
              <p>No lights found.</p>
            </div>
          </CardBox>
        )}
      </div>

      {/* Switches section */}
      {switches.length > 0 && (
        <>
          <SectionTitleLineWithButton icon={mdiToggleSwitch} title="Switches" />
          <div className="mb-6 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {switches.map((sw) => {
              const isOn = sw.state === "on";
              const isUnavailable = sw.state === "unavailable";
              return (
                <CardBox key={sw.entity_id}>
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-3">
                      <Icon
                        path={isOn ? mdiToggleSwitch : mdiToggleSwitchOff}
                        size="24"
                        className={
                          isUnavailable
                            ? "text-red-400"
                            : isOn
                              ? "text-[#4ADE80]"
                              : "text-gray-400 dark:text-slate-500"
                        }
                      />
                      <div>
                        <p className="text-sm font-medium text-[#0C2F44] dark:text-white">
                          {friendlyName(sw)}
                        </p>
                        <p className="text-xs text-gray-500 dark:text-slate-400">
                          {isUnavailable ? "Unavailable" : isOn ? "On" : "Off"}
                        </p>
                      </div>
                    </div>
                    <button
                      onClick={() => onToggle(sw.entity_id, sw.state)}
                      disabled={isUnavailable}
                      className={`flex h-8 w-8 items-center justify-center rounded-full transition ${
                        isUnavailable
                          ? "cursor-not-allowed opacity-40"
                          : isOn
                            ? "bg-[#4ADE80] text-white hover:bg-green-600"
                            : "bg-gray-200 text-gray-500 hover:bg-gray-300 dark:bg-slate-600 dark:hover:bg-slate-500"
                      }`}
                    >
                      <Icon path={mdiPower} size="16" />
                    </button>
                  </div>
                </CardBox>
              );
            })}
          </div>
        </>
      )}
    </>
  );
}

/* ================================================================== */
/*  CLIMATE TAB                                                        */
/* ================================================================== */

function ClimateTab({
  climates,
  binarySensors,
  onSetTemp,
  onSetMode,
}: {
  climates: HAEntity[];
  binarySensors: HAEntity[];
  onSetTemp: (entityId: string, temp: number) => void;
  onSetMode: (entityId: string, mode: string) => void;
}) {
  const hvacModes = ["off", "heat", "cool", "heat_cool"];
  const hvacIcons: Record<string, string> = {
    off: mdiPower,
    heat: mdiFire,
    cool: mdiSnowflake,
    heat_cool: mdiFan,
  };
  const hvacColors: Record<string, string> = {
    off: "bg-gray-200 text-gray-600 dark:bg-slate-700 dark:text-slate-400",
    heat: "bg-red-100 text-red-600 dark:bg-red-900/30 dark:text-red-400",
    cool: "bg-blue-100 text-blue-600 dark:bg-blue-900/30 dark:text-blue-400",
    heat_cool: "bg-purple-100 text-purple-600 dark:bg-purple-900/30 dark:text-purple-400",
  };

  return (
    <>
      <SectionTitleLineWithButton icon={mdiThermometer} title="Climate Control" />
      <div className="mb-6 grid grid-cols-1 gap-6 lg:grid-cols-2">
        {climates.map((climate) => {
          const currentTemp = climate.attributes.current_temperature;
          const targetTemp = climate.attributes.temperature;
          const hvacMode = climate.state;
          const name = friendlyName(climate);

          // Find associated motion/occupancy sensors
          const roomKey = climate.entity_id.split(".")[1]; // e.g. "bedroom"
          const motionSensor = binarySensors.find(
            (b) =>
              b.entity_id.includes(roomKey) &&
              (b.attributes.device_class === "motion" || b.entity_id.includes("motion"))
          );
          const occupancySensor = binarySensors.find(
            (b) =>
              b.entity_id.includes(roomKey) &&
              (b.attributes.device_class === "occupancy" || b.entity_id.includes("occupancy"))
          );

          return (
            <CardBox key={climate.entity_id}>
              <div className="flex items-start justify-between">
                <div>
                  <h3 className="text-lg font-semibold text-[#0C2F44] dark:text-white">
                    {name}
                  </h3>
                  <p className="text-xs text-gray-500 dark:text-slate-400">
                    Mode: {capitalize(hvacMode)}
                  </p>
                </div>
                {/* Motion + Occupancy indicators */}
                <div className="flex gap-2">
                  {motionSensor && (
                    <span
                      className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs ${
                        motionSensor.state === "on"
                          ? "bg-[#EF4444]/10 text-[#EF4444]"
                          : "bg-gray-100 text-gray-400 dark:bg-slate-700 dark:text-slate-500"
                      }`}
                    >
                      <Icon path={mdiWalk} size="12" />
                      {motionSensor.state === "on" ? "Motion" : "Clear"}
                    </span>
                  )}
                  {occupancySensor && (
                    <span
                      className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs ${
                        occupancySensor.state === "on"
                          ? "bg-[#4ADE80]/10 text-[#16A34A]"
                          : "bg-gray-100 text-gray-400 dark:bg-slate-700 dark:text-slate-500"
                      }`}
                    >
                      <Icon path={mdiAccountGroup} size="12" />
                      {occupancySensor.state === "on" ? "Occupied" : "Empty"}
                    </span>
                  )}
                </div>
              </div>

              {/* Temperature display */}
              <div className="mt-4 flex items-center justify-center gap-8">
                <div className="text-center">
                  <p className="text-xs uppercase tracking-wide text-gray-500 dark:text-slate-400">
                    Current
                  </p>
                  <p className="text-5xl font-bold text-[#00B4D8]">
                    {currentTemp !== undefined && currentTemp !== null
                      ? currentTemp
                      : "---"}
                    <span className="text-2xl">&deg;</span>
                  </p>
                </div>
                <div className="h-16 w-px bg-gray-200 dark:bg-slate-700" />
                <div className="text-center">
                  <p className="text-xs uppercase tracking-wide text-gray-500 dark:text-slate-400">
                    Target
                  </p>
                  <div className="flex items-center gap-2">
                    <button
                      onClick={() =>
                        targetTemp !== undefined && onSetTemp(climate.entity_id, targetTemp - 1)
                      }
                      className="flex h-8 w-8 items-center justify-center rounded-full bg-gray-100 text-gray-600 transition hover:bg-gray-200 dark:bg-slate-700 dark:text-slate-300 dark:hover:bg-slate-600"
                    >
                      <Icon path={mdiMinus} size="16" />
                    </button>
                    <p className="text-3xl font-bold text-[#0C2F44] dark:text-white">
                      {targetTemp ?? "---"}
                      <span className="text-lg">&deg;</span>
                    </p>
                    <button
                      onClick={() =>
                        targetTemp !== undefined && onSetTemp(climate.entity_id, targetTemp + 1)
                      }
                      className="flex h-8 w-8 items-center justify-center rounded-full bg-gray-100 text-gray-600 transition hover:bg-gray-200 dark:bg-slate-700 dark:text-slate-300 dark:hover:bg-slate-600"
                    >
                      <Icon path={mdiPlus} size="16" />
                    </button>
                  </div>
                </div>
              </div>

              {/* HVAC mode selector */}
              <div className="mt-4 flex justify-center gap-2">
                {hvacModes.map((mode) => (
                  <button
                    key={mode}
                    onClick={() => onSetMode(climate.entity_id, mode)}
                    className={`flex items-center gap-1.5 rounded-full px-3 py-1.5 text-xs font-medium transition ${
                      hvacMode === mode
                        ? hvacColors[mode] + " ring-2 ring-offset-1 ring-[#00B4D8]"
                        : "bg-gray-50 text-gray-500 hover:bg-gray-100 dark:bg-slate-800 dark:text-slate-400 dark:hover:bg-slate-700"
                    }`}
                  >
                    <Icon path={hvacIcons[mode] || mdiPower} size="14" />
                    {capitalize(mode === "heat_cool" ? "Auto" : mode)}
                  </button>
                ))}
              </div>
            </CardBox>
          );
        })}

        {climates.length === 0 && (
          <CardBox className="col-span-full">
            <div className="py-8 text-center text-gray-400 dark:text-slate-500">
              <Icon path={mdiThermometer} size="32" className="mx-auto mb-2 opacity-40" />
              <p>No climate entities found.</p>
            </div>
          </CardBox>
        )}
      </div>
    </>
  );
}

/* ================================================================== */
/*  MEDIA TAB                                                          */
/* ================================================================== */

function MediaTab({
  players,
  onControl,
  onVolume,
}: {
  players: HAEntity[];
  onControl: (entityId: string, service: string) => void;
  onVolume: (entityId: string, volume: number) => void;
}) {
  return (
    <>
      <SectionTitleLineWithButton icon={mdiVolumeHigh} title="Media Players" />
      <div className="mb-6 grid grid-cols-1 gap-6 lg:grid-cols-2">
        {players.map((player) => {
          const isPlaying = player.state === "playing";
          const isPaused = player.state === "paused";
          const isIdle = player.state === "idle" || player.state === "off";
          const isUnavailable = player.state === "unavailable";
          const volume = player.attributes.volume_level;
          const volumePercent =
            volume !== undefined ? Math.round(volume * 100) : null;
          const mediaTitle = player.attributes.media_title || null;
          const mediaArtist = player.attributes.media_artist || null;

          return (
            <CardBox
              key={player.entity_id}
              className={`border-l-4 ${
                isPlaying
                  ? "border-l-[#4ADE80]"
                  : isPaused
                    ? "border-l-[#FBBF24]"
                    : "border-l-gray-300 dark:border-l-slate-600"
              }`}
            >
              <div className="flex items-start justify-between">
                <div>
                  <h3 className="font-semibold text-[#0C2F44] dark:text-white">
                    {friendlyName(player)}
                  </h3>
                  <p className="text-xs text-gray-500 dark:text-slate-400">
                    {capitalize(player.state)}
                  </p>
                </div>
                <span
                  className={`rounded-full px-2 py-0.5 text-xs font-medium ${
                    isPlaying
                      ? "bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400"
                      : isPaused
                        ? "bg-yellow-100 text-yellow-700 dark:bg-yellow-900/30 dark:text-yellow-400"
                        : "bg-gray-100 text-gray-500 dark:bg-slate-700 dark:text-slate-400"
                  }`}
                >
                  {capitalize(player.state)}
                </span>
              </div>

              {/* Now playing info */}
              {(mediaTitle || mediaArtist) && (
                <div className="mt-3 rounded-lg bg-gray-50 p-3 dark:bg-slate-800">
                  {mediaTitle && (
                    <p className="text-sm font-medium text-[#0C2F44] dark:text-white">
                      {mediaTitle}
                    </p>
                  )}
                  {mediaArtist && (
                    <p className="text-xs text-gray-500 dark:text-slate-400">
                      {mediaArtist}
                    </p>
                  )}
                </div>
              )}

              {/* Transport controls */}
              <div className="mt-4 flex items-center justify-center gap-3">
                <button
                  onClick={() => onControl(player.entity_id, "media_previous_track")}
                  disabled={isUnavailable}
                  className="flex h-9 w-9 items-center justify-center rounded-full bg-gray-100 text-gray-600 transition hover:bg-gray-200 disabled:cursor-not-allowed disabled:opacity-40 dark:bg-slate-700 dark:text-slate-300 dark:hover:bg-slate-600"
                >
                  <Icon path={mdiSkipPrevious} size="20" />
                </button>
                {isPlaying ? (
                  <button
                    onClick={() => onControl(player.entity_id, "media_pause")}
                    className="flex h-12 w-12 items-center justify-center rounded-full bg-[#00B4D8] text-white shadow-md transition hover:bg-[#0096C7]"
                  >
                    <Icon path={mdiPause} size="24" />
                  </button>
                ) : (
                  <button
                    onClick={() => onControl(player.entity_id, "media_play")}
                    disabled={isUnavailable}
                    className="flex h-12 w-12 items-center justify-center rounded-full bg-[#4ADE80] text-white shadow-md transition hover:bg-green-600 disabled:cursor-not-allowed disabled:opacity-40"
                  >
                    <Icon path={mdiPlay} size="24" />
                  </button>
                )}
                <button
                  onClick={() => onControl(player.entity_id, "media_stop")}
                  disabled={isUnavailable || isIdle}
                  className="flex h-9 w-9 items-center justify-center rounded-full bg-gray-100 text-gray-600 transition hover:bg-gray-200 disabled:cursor-not-allowed disabled:opacity-40 dark:bg-slate-700 dark:text-slate-300 dark:hover:bg-slate-600"
                >
                  <Icon path={mdiStop} size="20" />
                </button>
                <button
                  onClick={() => onControl(player.entity_id, "media_next_track")}
                  disabled={isUnavailable}
                  className="flex h-9 w-9 items-center justify-center rounded-full bg-gray-100 text-gray-600 transition hover:bg-gray-200 disabled:cursor-not-allowed disabled:opacity-40 dark:bg-slate-700 dark:text-slate-300 dark:hover:bg-slate-600"
                >
                  <Icon path={mdiSkipNext} size="20" />
                </button>
              </div>

              {/* Volume */}
              {volumePercent !== null && (
                <div className="mt-4 flex items-center gap-3">
                  <button
                    onClick={() =>
                      onControl(player.entity_id, "volume_mute")
                    }
                    className="text-gray-400 hover:text-gray-600 dark:hover:text-slate-300"
                  >
                    <Icon
                      path={volumePercent === 0 ? mdiVolumeOff : mdiVolumeMinus}
                      size="18"
                    />
                  </button>
                  <input
                    type="range"
                    min={0}
                    max={100}
                    value={volumePercent}
                    onChange={(e) =>
                      onVolume(
                        player.entity_id,
                        parseInt(e.target.value, 10) / 100
                      )
                    }
                    className="flex-1 accent-[#00B4D8]"
                  />
                  <Icon path={mdiVolumePlus} size="18" className="text-gray-400" />
                  <span className="min-w-[3ch] text-right text-xs text-gray-500 dark:text-slate-400">
                    {volumePercent}%
                  </span>
                </div>
              )}
            </CardBox>
          );
        })}

        {players.length === 0 && (
          <CardBox className="col-span-full">
            <div className="py-8 text-center text-gray-400 dark:text-slate-500">
              <Icon path={mdiVolumeHigh} size="32" className="mx-auto mb-2 opacity-40" />
              <p>No media players found.</p>
            </div>
          </CardBox>
        )}
      </div>
    </>
  );
}

/* ================================================================== */
/*  SECURITY TAB                                                       */
/* ================================================================== */

function SecurityTab({
  cameras,
  binarySensors,
  cameraRefreshKey,
  haUrl: _haUrl,
}: {
  cameras: HAEntity[];
  binarySensors: HAEntity[];
  cameraRefreshKey: number;
  haUrl: string;
}) {
  const motionSensors = binarySensors.filter(
    (b) =>
      b.attributes.device_class === "motion" || b.entity_id.includes("motion")
  );
  const occupancySensors = binarySensors.filter(
    (b) =>
      b.attributes.device_class === "occupancy" ||
      b.entity_id.includes("occupancy")
  );
  const personSensors = binarySensors.filter(
    (b) =>
      b.attributes.device_class === "presence" ||
      b.entity_id.includes("person") ||
      b.entity_id.includes("vehicle") ||
      b.entity_id.includes("animal")
  );

  return (
    <>
      {/* Camera section */}
      <SectionTitleLineWithButton icon={mdiCamera} title="Cameras" />
      <div className="mb-6 grid grid-cols-1 gap-6 lg:grid-cols-2">
        {cameras.map((cam) => (
          <CardBox key={cam.entity_id}>
            <div className="flex items-center justify-between mb-3">
              <div className="flex items-center gap-3">
                <div className="flex h-10 w-10 items-center justify-center rounded-full bg-gradient-to-br from-red-400 to-red-600 text-white shadow-md">
                  <Icon path={mdiCctv} size="22" />
                </div>
                <div>
                  <h3 className="font-semibold text-[#0C2F44] dark:text-white">
                    {friendlyName(cam)}
                  </h3>
                  <p className="text-xs text-gray-500 dark:text-slate-400">
                    {capitalize(cam.state)}
                  </p>
                </div>
              </div>
              <span
                className={`rounded-full px-2 py-0.5 text-xs font-medium ${
                  cam.state === "idle" || cam.state === "recording"
                    ? "bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400"
                    : "bg-gray-100 text-gray-500 dark:bg-slate-700 dark:text-slate-400"
                }`}
              >
                {cam.state === "idle" ? "Live" : capitalize(cam.state)}
              </span>
            </div>
            {/* Camera preview placeholder */}
            <div className="relative overflow-hidden rounded-xl bg-gray-900">
              <div className="flex h-48 items-center justify-center text-gray-500">
                <div className="text-center">
                  <Icon path={mdiCamera} size="48" className="mx-auto mb-2 text-gray-600" />
                  <p className="text-sm text-gray-400">
                    Camera feed via HA proxy
                  </p>
                  <p className="mt-1 text-xs text-gray-500">
                    Refresh #{cameraRefreshKey}
                  </p>
                </div>
              </div>
              {/* Live indicator */}
              <div className="absolute left-3 top-3 flex items-center gap-1.5 rounded-full bg-black/60 px-2.5 py-1 text-xs text-white backdrop-blur-sm">
                <span className="h-2 w-2 animate-pulse rounded-full bg-red-500" />
                LIVE
              </div>
            </div>
          </CardBox>
        ))}

        {cameras.length === 0 && (
          <CardBox className="col-span-full">
            <div className="py-8 text-center text-gray-400 dark:text-slate-500">
              <Icon path={mdiCctv} size="32" className="mx-auto mb-2 opacity-40" />
              <p>No cameras found.</p>
            </div>
          </CardBox>
        )}
      </div>

      {/* Motion sensors */}
      <SectionTitleLineWithButton icon={mdiMotionSensor} title="Motion Sensors" />
      <div className="mb-6 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {motionSensors.map((sensor) => {
          const active = sensor.state === "on";
          return (
            <CardBox
              key={sensor.entity_id}
              className={`border-l-4 ${
                active
                  ? "border-l-[#EF4444]"
                  : "border-l-gray-200 dark:border-l-slate-700"
              }`}
            >
              <div className="flex items-center gap-3">
                <div
                  className={`flex h-10 w-10 items-center justify-center rounded-full ${
                    active
                      ? "bg-[#EF4444]/15 animate-pulse"
                      : "bg-gray-100 dark:bg-slate-700"
                  }`}
                >
                  <Icon
                    path={mdiMotionSensor}
                    size="22"
                    className={active ? "text-[#EF4444]" : "text-gray-400 dark:text-slate-500"}
                  />
                </div>
                <div className="flex-1">
                  <p className="text-sm font-medium text-[#0C2F44] dark:text-white">
                    {friendlyName(sensor)}
                  </p>
                  <p className="text-xs text-gray-500 dark:text-slate-400">
                    {active ? "Motion detected" : "Clear"}
                  </p>
                </div>
                <span
                  className={`rounded-full px-2 py-0.5 text-xs font-medium ${
                    active
                      ? "bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400"
                      : "bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400"
                  }`}
                >
                  {active ? "Active" : "Clear"}
                </span>
              </div>
            </CardBox>
          );
        })}

        {motionSensors.length === 0 && (
          <CardBox className="col-span-full">
            <div className="py-4 text-center text-gray-400 dark:text-slate-500">
              <p>No motion sensors found.</p>
            </div>
          </CardBox>
        )}
      </div>

      {/* Occupancy */}
      {occupancySensors.length > 0 && (
        <>
          <SectionTitleLineWithButton icon={mdiAccountGroup} title="Occupancy" />
          <div className="mb-6 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {occupancySensors.map((sensor) => {
              const active = sensor.state === "on";
              return (
                <CardBox key={sensor.entity_id}>
                  <div className="flex items-center gap-3">
                    <Icon
                      path={mdiAccountGroup}
                      size="22"
                      className={
                        active
                          ? "text-[#4ADE80]"
                          : "text-gray-400 dark:text-slate-500"
                      }
                    />
                    <div className="flex-1">
                      <p className="text-sm font-medium text-[#0C2F44] dark:text-white">
                        {friendlyName(sensor)}
                      </p>
                    </div>
                    <span
                      className={`rounded-full px-2 py-0.5 text-xs font-medium ${
                        active
                          ? "bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400"
                          : "bg-gray-100 text-gray-500 dark:bg-slate-700 dark:text-slate-400"
                      }`}
                    >
                      {active ? "Occupied" : "Empty"}
                    </span>
                  </div>
                </CardBox>
              );
            })}
          </div>
        </>
      )}

      {/* Person / vehicle / animal detection */}
      {personSensors.length > 0 && (
        <>
          <SectionTitleLineWithButton icon={mdiWalk} title="Detection" />
          <div className="mb-6 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {personSensors.map((sensor) => {
              const active = sensor.state === "on";
              return (
                <CardBox
                  key={sensor.entity_id}
                  className={active ? "border border-[#EF4444]/30" : ""}
                >
                  <div className="flex items-center gap-3">
                    <Icon
                      path={mdiWalk}
                      size="22"
                      className={active ? "text-[#EF4444]" : "text-gray-400 dark:text-slate-500"}
                    />
                    <div className="flex-1">
                      <p className="text-sm font-medium text-[#0C2F44] dark:text-white">
                        {friendlyName(sensor)}
                      </p>
                    </div>
                    <span
                      className={`rounded-full px-2 py-0.5 text-xs font-medium ${
                        active
                          ? "bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400"
                          : "bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400"
                      }`}
                    >
                      {active ? "Detected" : "Clear"}
                    </span>
                  </div>
                </CardBox>
              );
            })}
          </div>
        </>
      )}
    </>
  );
}

/* ================================================================== */
/*  AUTOMATIONS TAB                                                    */
/* ================================================================== */

function AutomationsTab({
  automations,
  scenes,
  onToggle,
  onTrigger,
  onActivateScene,
}: {
  automations: HAEntity[];
  scenes: HAEntity[];
  onToggle: (entityId: string, state: string) => void;
  onTrigger: (entityId: string) => void;
  onActivateScene: (entityId: string) => void;
}) {
  return (
    <>
      {/* Automations */}
      <SectionTitleLineWithButton icon={mdiCogOutline} title="Automations" />
      <div className="mb-6 space-y-3">
        {automations.map((auto) => {
          const isOn = auto.state === "on";
          const lastTriggered = auto.attributes?.last_triggered || (auto as any).last_triggered;

          return (
            <CardBox key={auto.entity_id}>
              <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                <div className="flex items-center gap-3 flex-1 min-w-0">
                  <div
                    className={`flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-full ${
                      isOn
                        ? "bg-[#A78BCC]/15"
                        : "bg-gray-100 dark:bg-slate-700"
                    }`}
                  >
                    <Icon
                      path={mdiCogOutline}
                      size="22"
                      className={
                        isOn
                          ? "text-[#A78BCC]"
                          : "text-gray-400 dark:text-slate-500"
                      }
                    />
                  </div>
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-medium text-[#0C2F44] dark:text-white">
                      {friendlyName(auto)}
                    </p>
                    <p className="text-xs text-gray-500 dark:text-slate-400">
                      {lastTriggered
                        ? `Last triggered: ${formatRelativeTime(lastTriggered)}`
                        : "Never triggered"}
                    </p>
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  {/* Enable/disable toggle */}
                  <button
                    onClick={() => onToggle(auto.entity_id, auto.state)}
                    className={`flex items-center gap-1.5 rounded-full px-3 py-1.5 text-xs font-medium transition ${
                      isOn
                        ? "bg-[#A78BCC]/15 text-[#A78BCC] hover:bg-[#A78BCC]/25"
                        : "bg-gray-100 text-gray-500 hover:bg-gray-200 dark:bg-slate-700 dark:text-slate-400 dark:hover:bg-slate-600"
                    }`}
                  >
                    <Icon
                      path={isOn ? mdiCheckCircle : mdiFlashOff}
                      size="14"
                    />
                    {isOn ? "Enabled" : "Disabled"}
                  </button>
                  {/* Trigger button */}
                  <Button
                    label="Trigger"
                    icon={mdiFlash}
                    color="info"
                    small
                    roundedFull
                    onClick={() => onTrigger(auto.entity_id)}
                  />
                </div>
              </div>
            </CardBox>
          );
        })}

        {automations.length === 0 && (
          <CardBox>
            <div className="py-8 text-center text-gray-400 dark:text-slate-500">
              <Icon path={mdiCogOutline} size="32" className="mx-auto mb-2 opacity-40" />
              <p>No automations found.</p>
            </div>
          </CardBox>
        )}
      </div>

      {/* Scenes */}
      {scenes.length > 0 && (
        <>
          <SectionTitleLineWithButton icon={mdiFlash} title="Scenes" />
          <div className="mb-6 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {scenes.map((scene) => (
              <CardBox key={scene.entity_id} isHoverable>
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-3">
                    <div className="flex h-10 w-10 items-center justify-center rounded-full bg-gradient-to-br from-[#A78BCC] to-purple-600 text-white shadow-md">
                      <Icon path={mdiFlash} size="22" />
                    </div>
                    <p className="text-sm font-medium text-[#0C2F44] dark:text-white">
                      {friendlyName(scene)}
                    </p>
                  </div>
                  <Button
                    label="Activate"
                    icon={mdiFlash}
                    color="info"
                    small
                    roundedFull
                    onClick={() => onActivateScene(scene.entity_id)}
                  />
                </div>
              </CardBox>
            ))}
          </div>
        </>
      )}
    </>
  );
}

/* ================================================================== */
/*  Helpers                                                            */
/* ================================================================== */

function friendlyName(entity: any): string {
  return (
    entity.attributes?.friendly_name ||
    entity.friendly_name ||
    entity.entity_id?.split(".").pop()?.replace(/_/g, " ") ||
    entity.entity_id ||
    "Unknown"
  );
}

function capitalize(s: string): string {
  if (!s) return s;
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function formatRelativeTime(isoString: string): string {
  try {
    const then = new Date(isoString).getTime();
    const now = Date.now();
    const diffSec = Math.floor((now - then) / 1000);
    if (diffSec < 0) return "Just now";
    if (diffSec < 60) return "Just now";
    if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m ago`;
    if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}h ago`;
    return `${Math.floor(diffSec / 86400)}d ago`;
  } catch {
    return "Unknown";
  }
}
