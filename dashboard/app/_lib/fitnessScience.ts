/**
 * FitFam Exercise Science Module
 *
 * Evidence-based exercise database, programming logic, and analytics
 * helpers. Built around principles from Schoenfeld, Helms, Israetel, and
 * the NSCA Essentials of Strength Training & Conditioning.
 *
 * Key concepts:
 *   - Compound vs isolation lifts
 *   - RIR (reps in reserve) / RPE (rate of perceived exertion) for autoregulation
 *   - Volume landmarks (MEV, MAV, MRV) per muscle group
 *   - Estimated 1-rep max via Epley formula
 *   - Progressive overload tracking
 */

export type Equipment =
  | "barbell"
  | "dumbbell"
  | "kettlebell"
  | "bodyweight"
  | "cable"
  | "machine"
  | "band"
  | "trap-bar";

export type MuscleGroup =
  | "chest"
  | "back"
  | "shoulders"
  | "biceps"
  | "triceps"
  | "quads"
  | "hamstrings"
  | "glutes"
  | "calves"
  | "core"
  | "forearms";

export type Pattern =
  | "horizontal-push"
  | "horizontal-pull"
  | "vertical-push"
  | "vertical-pull"
  | "squat"
  | "hinge"
  | "lunge"
  | "carry"
  | "rotation"
  | "isolation";

export interface Exercise {
  id: string;
  name: string;
  equipment: Equipment;
  primary: MuscleGroup[];
  secondary: MuscleGroup[];
  pattern: Pattern;
  isCompound: boolean;
  /** 1 = master/staple, 2 = great, 3 = situational */
  tier: 1 | 2 | 3;
  formCues: string[];
  /** Common substitutes if equipment is unavailable */
  substitutes?: string[];
  /** Brief safety notes */
  safety?: string;
  /** Inline SVG stick figure (viewBox 0 0 150 190, currentColor strokes). Hand-authored. */
  figure?: string;
}

/**
 * Curated list of evidence-based exercises. This is intentionally
 * focused on the highest-ROI movements rather than exhaustive: quality
 * over quantity. Add more over time.
 */
export const EXERCISE_DB: Exercise[] = [
  // ── PUSH ──────────────────────────────────────────────────────────
  {
    id: "bench-press",
    name: "Barbell Bench Press",
    equipment: "barbell",
    primary: ["chest"],
    secondary: ["triceps", "shoulders"],
    pattern: "horizontal-push",
    isCompound: true,
    tier: 1,
    formCues: [
      "Set your shoulder blades down and back, like tucking them into your back pockets, before you lift",
      "Bring the bar to the bottom of your chest, not up by your collarbone",
      "Push your feet into the floor to stay solid",
      "Keep your wrists straight, stacked right over your elbows",
    ],
    substitutes: ["dumbbell-bench", "pushup", "machine-chest-press"],
    safety: "Always have someone spotting you, or use safety bars, when the weight is heavy.",
  },
  {
    id: "dumbbell-bench",
    name: "Dumbbell Bench Press",
    equipment: "dumbbell",
    primary: ["chest"],
    secondary: ["triceps", "shoulders"],
    pattern: "horizontal-push",
    isCompound: true,
    tier: 1,
    formCues: [
      "Lower the dumbbells to chest level, nice and controlled",
      "Keep your elbows about halfway out from your sides, not flared wide",
      "Press up and bring the dumbbells slightly together at the top",
    ],
    substitutes: ["bench-press", "pushup"],
  },
  {
    id: "pushup",
    name: "Push-Up",
    equipment: "bodyweight",
    primary: ["chest"],
    secondary: ["triceps", "shoulders", "core"],
    pattern: "horizontal-push",
    isCompound: true,
    tier: 1,
    formCues: [
      "Keep your body in one straight line from head to heels",
      "Lower until your chest almost touches the floor",
      "Keep your elbows about halfway out from your sides, not flared wide",
    ],
    substitutes: ["dumbbell-bench", "dip"],
    figure: `<svg viewBox="0 0 150 190" role="img"><title>Push-Up</title><line x1="14" y1="154" x2="142" y2="154" stroke="currentColor" stroke-opacity="0.3" stroke-width="4" stroke-linecap="round"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><circle cx="40" cy="92" r="11"/><line x1="50" y1="97" x2="128" y2="138"/><line x1="60" y1="101" x2="56" y2="152"/><line x1="128" y1="138" x2="124" y2="152"/></g></svg>`,
  },
  {
    id: "ohp",
    name: "Overhead Press",
    equipment: "barbell",
    primary: ["shoulders"],
    secondary: ["triceps", "core"],
    pattern: "vertical-push",
    isCompound: true,
    tier: 1,
    formCues: [
      "Tighten your stomach before you lift the bar off",
      "Squeeze your glutes so your lower back doesn't arch",
      "Press the bar straight up until it's over your ears",
    ],
    substitutes: ["dumbbell-shoulder-press", "pike-pushup"],
    figure: `<svg viewBox="0 0 150 190" role="img"><title>Strict Overhead Press</title><line x1="46" y1="24" x2="104" y2="24" stroke="currentColor" stroke-opacity="0.4" stroke-width="4" stroke-linecap="round"/><line x1="22" y1="178" x2="128" y2="178" stroke="currentColor" stroke-opacity="0.3" stroke-width="4" stroke-linecap="round"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><circle cx="75" cy="52" r="11"/><line x1="75" y1="63" x2="75" y2="116"/><path d="M75 72 L60 46 L58 26"/><path d="M75 72 L90 46 L92 26"/><path d="M75 116 L64 148 L64 176"/><path d="M75 116 L86 148 L86 176"/></g></svg>`,
  },
  {
    id: "dumbbell-shoulder-press",
    name: "Dumbbell Shoulder Press",
    equipment: "dumbbell",
    primary: ["shoulders"],
    secondary: ["triceps"],
    pattern: "vertical-push",
    isCompound: true,
    tier: 1,
    formCues: [
      "Start with the dumbbells at shoulder height, palms facing forward",
      "Press up and bring them slightly together at the top",
      "Lower slowly, all the way back down",
    ],
    substitutes: ["ohp", "lateral-raise"],
    figure: `<svg viewBox="0 0 150 190" role="img"><title>Single-Arm KB Press</title><line x1="22" y1="178" x2="128" y2="178" stroke="currentColor" stroke-opacity="0.3" stroke-width="4" stroke-linecap="round"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><circle cx="70" cy="52" r="11"/><line x1="70" y1="63" x2="70" y2="116"/><path d="M70 72 L88 46 L90 28"/><circle cx="90" cy="23" r="6" stroke-opacity="0.55"/><path d="M70 74 L57 98 L59 116"/><path d="M70 116 L60 148 L60 176"/><path d="M70 116 L82 148 L82 176"/></g></svg>`,
  },
  {
    id: "dip",
    name: "Parallel Bar Dip",
    equipment: "bodyweight",
    primary: ["chest", "triceps"],
    secondary: ["shoulders"],
    pattern: "vertical-push",
    isCompound: true,
    tier: 1,
    formCues: [
      "Lean forward a little to work the chest more",
      "Lower until your shoulders dip just below your elbows",
      "Keep your shoulder blades down, away from your ears",
    ],
    substitutes: ["pushup", "dumbbell-shoulder-press"],
    figure: `<svg viewBox="0 0 150 190" role="img"><title>Dip</title><line x1="40" y1="64" x2="40" y2="120" stroke="currentColor" stroke-opacity="0.4" stroke-width="4" stroke-linecap="round"/><line x1="110" y1="64" x2="110" y2="120" stroke="currentColor" stroke-opacity="0.4" stroke-width="4" stroke-linecap="round"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><circle cx="75" cy="46" r="11"/><line x1="75" y1="57" x2="75" y2="120"/><path d="M75 66 L52 66 L40 84"/><path d="M75 66 L98 66 L110 84"/><path d="M75 120 L66 146 L78 156"/><path d="M75 120 L86 146 L96 154"/></g></svg>`,
  },

  // ── PULL ──────────────────────────────────────────────────────────
  {
    id: "deadlift",
    name: "Conventional Deadlift",
    equipment: "barbell",
    primary: ["hamstrings", "glutes", "back"],
    secondary: ["forearms", "core"],
    pattern: "hinge",
    isCompound: true,
    tier: 1,
    formCues: [
      "Tighten your stomach and take the slack out of the bar before you pull",
      "Push the floor away with your feet. Don't jerk the bar up",
      "Keep the bar touching your legs the whole way up",
      "Finish standing tall by squeezing your glutes",
    ],
    substitutes: ["trap-bar-deadlift", "rdl", "kettlebell-swing"],
    safety: "Stop if you feel any sharp pain in your lower back. A lifting belt helps on your heaviest sets.",
  },
  {
    id: "rdl",
    name: "Romanian Deadlift",
    equipment: "barbell",
    primary: ["hamstrings", "glutes"],
    secondary: ["back"],
    pattern: "hinge",
    isCompound: true,
    tier: 1,
    formCues: [
      "Push your hips backward, not straight down",
      "Keep a slight, soft bend in your knees the whole time",
      "Stop when you feel a strong stretch down the back of your thighs",
    ],
    substitutes: ["deadlift", "kettlebell-swing"],
    figure: `<svg viewBox="0 0 150 190" role="img"><title>Romanian Deadlift</title><line x1="66" y1="128" x2="98" y2="128" stroke="currentColor" stroke-opacity="0.4" stroke-width="4" stroke-linecap="round"/><line x1="22" y1="178" x2="128" y2="178" stroke="currentColor" stroke-opacity="0.3" stroke-width="4" stroke-linecap="round"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><circle cx="44" cy="60" r="11"/><line x1="54" y1="66" x2="104" y2="86"/><line x1="82" y1="76" x2="82" y2="128"/><path d="M104 86 L102 130 L96 176"/></g></svg>`,
  },
  {
    id: "pullup",
    name: "Pull-Up",
    equipment: "bodyweight",
    primary: ["back"],
    secondary: ["biceps", "forearms"],
    pattern: "vertical-pull",
    isCompound: true,
    tier: 1,
    formCues: [
      "Start hanging fully, with your shoulders active, not loose",
      "Pull your chest up toward the bar",
      "Squeeze your shoulder blades together at the top",
    ],
    substitutes: ["lat-pulldown", "inverted-row"],
    figure: `<svg viewBox="0 0 150 190" role="img"><title>Pull-Up</title><line x1="32" y1="34" x2="118" y2="34" stroke="currentColor" stroke-opacity="0.4" stroke-width="4" stroke-linecap="round"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M62 34 L56 58 L75 74"/><path d="M88 34 L94 58 L75 74"/><circle cx="75" cy="60" r="11"/><line x1="75" y1="74" x2="75" y2="126"/><path d="M75 126 L64 168"/><path d="M75 126 L86 168"/></g></svg>`,
  },
  {
    id: "lat-pulldown",
    name: "Lat Pulldown",
    equipment: "cable",
    primary: ["back"],
    secondary: ["biceps"],
    pattern: "vertical-pull",
    isCompound: true,
    tier: 2,
    formCues: [
      "Pull the bar down to your upper chest",
      "Lead with your elbows, driving them down and back",
      "Stay mostly upright. Don't lean way back",
    ],
  },
  {
    id: "barbell-row",
    name: "Barbell Row",
    equipment: "barbell",
    primary: ["back"],
    secondary: ["biceps", "forearms"],
    pattern: "horizontal-pull",
    isCompound: true,
    tier: 1,
    formCues: [
      "Bend forward to about 45 degrees, keeping your back flat",
      "Pull the bar to the bottom of your chest",
      "Squeeze your shoulder blades together at the top",
    ],
    substitutes: ["dumbbell-row", "cable-row"],
    figure: `<svg viewBox="0 0 150 190" role="img"><title>Barbell Row</title><line x1="52" y1="120" x2="92" y2="120" stroke="currentColor" stroke-opacity="0.4" stroke-width="4" stroke-linecap="round"/><line x1="22" y1="178" x2="128" y2="178" stroke="currentColor" stroke-opacity="0.3" stroke-width="4" stroke-linecap="round"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><circle cx="44" cy="64" r="11"/><line x1="54" y1="70" x2="110" y2="90"/><path d="M74 78 L80 62 L72 118"/><line x1="60" y1="118" x2="88" y2="118"/><path d="M110 90 L110 130 L102 176"/><path d="M110 130 L120 176"/></g></svg>`,
  },
  {
    id: "dumbbell-row",
    name: "One-Arm Dumbbell Row",
    equipment: "dumbbell",
    primary: ["back"],
    secondary: ["biceps"],
    pattern: "horizontal-pull",
    isCompound: true,
    tier: 1,
    formCues: [
      "Steady yourself with one knee and one hand on a bench",
      "Pull the dumbbell back toward your hip, not up to your chest",
      "Lower it slowly, letting your arm stretch all the way down",
    ],
    substitutes: ["barbell-row", "lat-pulldown"],
    figure: `<svg viewBox="0 0 150 190" role="img"><title>Single-Arm KB Row</title><line x1="28" y1="124" x2="96" y2="124" stroke="currentColor" stroke-opacity="0.4" stroke-width="4" stroke-linecap="round"/><line x1="22" y1="178" x2="128" y2="178" stroke="currentColor" stroke-opacity="0.3" stroke-width="4" stroke-linecap="round"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><circle cx="40" cy="80" r="11"/><line x1="50" y1="86" x2="110" y2="102"/><line x1="58" y1="91" x2="60" y2="124"/><line x1="94" y1="99" x2="96" y2="140"/><circle cx="96" cy="146" r="6" stroke-opacity="0.55"/><line x1="110" y1="102" x2="118" y2="176"/></g></svg>`,
  },

  // ── SQUAT ─────────────────────────────────────────────────────────
  {
    id: "back-squat",
    name: "Barbell Back Squat",
    equipment: "barbell",
    primary: ["quads", "glutes"],
    secondary: ["hamstrings", "core"],
    pattern: "squat",
    isCompound: true,
    tier: 1,
    formCues: [
      "Tighten your stomach before you go down",
      "Sit back and down, letting your knees travel out over your toes",
      "Go down until your hips are at least level with your knees",
      "Push up through the middle of your feet to stand",
    ],
    substitutes: ["front-squat", "goblet-squat", "leg-press"],
    safety: "Set up in a power rack or with safety bars in case you can't finish a rep.",
  },
  {
    id: "front-squat",
    name: "Barbell Front Squat",
    equipment: "barbell",
    primary: ["quads"],
    secondary: ["glutes", "core"],
    pattern: "squat",
    isCompound: true,
    tier: 1,
    formCues: [
      "Keep your elbows up high so the bar stays put on your shoulders",
      "Stay tall with your chest up",
      "Drop straight down between your feet",
    ],
    substitutes: ["back-squat", "goblet-squat"],
    figure: `<svg viewBox="0 0 150 190" role="img"><title>Tempo Front Squat</title><line x1="54" y1="52" x2="96" y2="52" stroke="currentColor" stroke-opacity="0.4" stroke-width="4" stroke-linecap="round"/><line x1="22" y1="172" x2="128" y2="172" stroke="currentColor" stroke-opacity="0.3" stroke-width="4" stroke-linecap="round"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><circle cx="75" cy="42" r="11"/><line x1="75" y1="53" x2="74" y2="108"/><path d="M74 62 L92 54 L78 52"/><path d="M74 62 L58 54 L72 52"/><path d="M74 108 L98 120 L92 168"/><path d="M74 108 L50 120 L56 168"/></g></svg>`,
  },
  {
    id: "goblet-squat",
    name: "Goblet Squat",
    equipment: "dumbbell",
    primary: ["quads", "glutes"],
    secondary: ["core"],
    pattern: "squat",
    isCompound: true,
    tier: 2,
    formCues: [
      "Hold the dumbbell against your upper chest",
      "Drop straight down between your feet",
      "Let your elbows brush the inside of your knees at the bottom",
    ],
    substitutes: ["front-squat", "bulgarian-split-squat"],
    figure: `<svg viewBox="0 0 150 190" role="img"><title>Goblet Squat</title><line x1="22" y1="172" x2="128" y2="172" stroke="currentColor" stroke-opacity="0.3" stroke-width="4" stroke-linecap="round"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><circle cx="75" cy="42" r="11"/><line x1="75" y1="53" x2="74" y2="106"/><path d="M75 64 L66 84 L74 90"/><path d="M75 64 L84 84 L76 90"/><circle cx="75" cy="95" r="6" stroke-opacity="0.55"/><path d="M74 106 L96 118 L90 166"/><path d="M74 106 L52 118 L58 166"/></g></svg>`,
  },
  {
    id: "bulgarian-split-squat",
    name: "Bulgarian Split Squat",
    equipment: "dumbbell",
    primary: ["quads", "glutes"],
    secondary: ["hamstrings"],
    pattern: "lunge",
    isCompound: true,
    tier: 1,
    formCues: [
      "Rest the top of your back foot on a bench",
      "Drop straight down. Don't lean forward into a lunge",
      "Keep your front shin fairly upright",
    ],
    substitutes: ["goblet-squat", "back-squat"],
    figure: `<svg viewBox="0 0 150 190" role="img"><title>KB Bulgarian Split Squat</title><line x1="24" y1="122" x2="46" y2="122" stroke="currentColor" stroke-opacity="0.4" stroke-width="4" stroke-linecap="round"/><line x1="22" y1="172" x2="128" y2="172" stroke="currentColor" stroke-opacity="0.3" stroke-width="4" stroke-linecap="round"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><circle cx="74" cy="46" r="11"/><line x1="74" y1="57" x2="74" y2="110"/><path d="M74 66 L66 92 L74 96"/><path d="M74 66 L82 92 L74 96"/><circle cx="74" cy="102" r="6" stroke-opacity="0.55"/><path d="M74 110 L90 134 L90 168"/><path d="M74 110 L56 130 L40 122"/></g></svg>`,
  },

  // ── ISOLATION ─────────────────────────────────────────────────────
  {
    id: "barbell-curl",
    name: "Barbell Curl",
    equipment: "barbell",
    primary: ["biceps"],
    secondary: ["forearms"],
    pattern: "isolation",
    isCompound: false,
    tier: 2,
    formCues: [
      "Keep your elbows tucked at your sides",
      "Squeeze your biceps at the top",
      "Lower slowly. Don't just drop it",
    ],
    figure: `<svg viewBox="0 0 150 190" role="img"><title>Barbell Curl</title><line x1="22" y1="178" x2="128" y2="178" stroke="currentColor" stroke-opacity="0.3" stroke-width="4" stroke-linecap="round"/><line x1="56" y1="88" x2="94" y2="88" stroke="currentColor" stroke-opacity="0.4" stroke-width="4" stroke-linecap="round"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><circle cx="75" cy="44" r="11"/><line x1="75" y1="55" x2="75" y2="114"/><path d="M75 64 L70 92 L60 88"/><path d="M75 64 L80 92 L90 88"/><path d="M75 114 L66 148 L66 176"/><path d="M75 114 L84 148 L84 176"/></g></svg>`,
  },
  {
    id: "tricep-pushdown",
    name: "Cable Tricep Pushdown",
    equipment: "cable",
    primary: ["triceps"],
    secondary: [],
    pattern: "isolation",
    isCompound: false,
    tier: 2,
    formCues: [
      "Keep your elbows tucked at your sides",
      "Straighten your arms all the way at the bottom",
      "Stand tall. Don't lean over the bar",
    ],
  },
  {
    id: "lateral-raise",
    name: "Dumbbell Lateral Raise",
    equipment: "dumbbell",
    primary: ["shoulders"],
    secondary: [],
    pattern: "isolation",
    isCompound: false,
    tier: 1,
    formCues: [
      "Keep a slight bend in your elbows the whole time",
      "Lead the movement with your elbows, not your hands",
      "Stop when your arms reach shoulder height, no higher",
    ],
    substitutes: ["face-pull", "dumbbell-shoulder-press"],
    figure: `<svg viewBox="0 0 150 190" role="img"><title>KB Lateral Raise</title><line x1="22" y1="178" x2="128" y2="178" stroke="currentColor" stroke-opacity="0.3" stroke-width="4" stroke-linecap="round"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><circle cx="75" cy="46" r="11"/><line x1="75" y1="57" x2="75" y2="112"/><path d="M75 66 L104 60 L114 62"/><circle cx="120" cy="64" r="6" stroke-opacity="0.55"/><path d="M75 66 L62 88 L64 108"/><path d="M75 112 L65 148 L65 176"/><path d="M75 112 L85 148 L85 176"/></g></svg>`,
  },
  {
    id: "face-pull",
    name: "Cable Face Pull",
    equipment: "cable",
    primary: ["shoulders"],
    secondary: ["back"],
    pattern: "isolation",
    isCompound: false,
    tier: 1,
    formCues: [
      "Pull the rope toward your face, hands ending up by your ears",
      "At the end, rotate your hands back like you're showing off your biceps",
      "Go light with higher reps. This one's for healthy shoulders",
    ],
    substitutes: ["lateral-raise", "barbell-row"],
    figure: `<svg viewBox="0 0 150 190" role="img"><title>Face Pull</title><line x1="110" y1="46" x2="128" y2="46" stroke="currentColor" stroke-opacity="0.4" stroke-width="4" stroke-linecap="round"/><line x1="22" y1="178" x2="128" y2="178" stroke="currentColor" stroke-opacity="0.3" stroke-width="4" stroke-linecap="round"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><circle cx="58" cy="46" r="11"/><line x1="58" y1="57" x2="58" y2="114"/><path d="M58 64 L84 52 L72 40"/><path d="M58 66 L84 78 L72 52"/><path d="M84 52 L110 48"/><path d="M84 78 L110 50"/><path d="M58 114 L49 148 L49 176"/><path d="M58 114 L67 148 L67 176"/></g></svg>`,
  },
  {
    id: "calf-raise",
    name: "Standing Calf Raise",
    equipment: "machine",
    primary: ["calves"],
    secondary: [],
    pattern: "isolation",
    isCompound: false,
    tier: 2,
    formCues: [
      "Let your heels sink for a good stretch at the bottom",
      "Rise all the way up and hold for a second at the top",
      "No bouncing. Keep it slow and controlled",
    ],
  },
  {
    id: "plank",
    name: "Plank",
    equipment: "bodyweight",
    primary: ["core"],
    secondary: [],
    pattern: "isolation",
    isCompound: false,
    tier: 2,
    formCues: [
      "Squeeze your glutes and tighten your stomach",
      "Keep your body flat. Don't let your hips sag or stick up",
      "Hold good form, quality matters more than a long time",
    ],
    substitutes: ["hanging-leg-raise"],
    figure: `<svg viewBox="0 0 150 190" role="img"><title>Plank</title><line x1="14" y1="152" x2="142" y2="152" stroke="currentColor" stroke-opacity="0.3" stroke-width="4" stroke-linecap="round"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><circle cx="34" cy="112" r="11"/><line x1="44" y1="116" x2="126" y2="146"/><path d="M52 118 L48 148 L66 148"/><line x1="126" y1="146" x2="122" y2="150"/></g></svg>`,
  },
  {
    id: "hanging-leg-raise",
    name: "Hanging Leg Raise",
    equipment: "bodyweight",
    primary: ["core"],
    secondary: [],
    pattern: "isolation",
    isCompound: false,
    tier: 1,
    formCues: [
      "Hang from a bar with your shoulders active, not loose",
      "Curl your hips up, not just your legs",
      "Lower slowly. Don't swing",
    ],
    substitutes: ["plank"],
    figure: `<svg viewBox="0 0 150 190" role="img"><title>Hanging Leg Raise</title><line x1="40" y1="28" x2="110" y2="28" stroke="currentColor" stroke-opacity="0.4" stroke-width="4" stroke-linecap="round"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><line x1="66" y1="46" x2="66" y2="30"/><line x1="84" y1="46" x2="84" y2="30"/><circle cx="75" cy="56" r="11"/><line x1="75" y1="67" x2="75" y2="106"/><line x1="75" y1="106" x2="120" y2="100"/><line x1="120" y1="100" x2="130" y2="92"/></g></svg>`,
  },

  // ── KETTLEBELL ────────────────────────────────────────────────────
  {
    id: "kettlebell-swing",
    name: "Kettlebell Swing",
    equipment: "kettlebell",
    primary: ["glutes", "hamstrings"],
    secondary: ["back", "core"],
    pattern: "hinge",
    isCompound: true,
    tier: 1,
    formCues: [
      "Push your hips back to swing it. Don't squat down",
      "Snap your hips forward to send the bell up",
      "Let the bell float up to about chest height, no higher",
      "Let it swing back down between your legs naturally",
    ],
    substitutes: ["rdl", "deadlift"],
    figure: `<svg viewBox="0 0 150 190" role="img"><title>Kettlebell Swing</title><line x1="22" y1="178" x2="128" y2="178" stroke="currentColor" stroke-opacity="0.3" stroke-width="4" stroke-linecap="round"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><circle cx="56" cy="52" r="11"/><line x1="58" y1="63" x2="70" y2="110"/><line x1="62" y1="74" x2="110" y2="66"/><circle cx="117" cy="64" r="7" stroke-opacity="0.55"/><path d="M70 110 L64 148 L64 176"/><path d="M70 110 L84 146 L92 176"/></g></svg>`,
  },
  {
    id: "turkish-get-up",
    name: "Turkish Get-Up",
    equipment: "kettlebell",
    primary: ["shoulders", "core"],
    secondary: ["glutes", "quads"],
    pattern: "carry",
    isCompound: true,
    tier: 2,
    formCues: [
      "Keep the bell balanced right over your shoulder the whole time",
      "Move slowly and deliberately through each step",
      "Keep your eyes on the bell as you change positions",
    ],
    safety: "Get comfortable with the movement using no weight first.",
    substitutes: ["farmer-carry", "plank"],
    figure: `<svg viewBox="0 0 150 190" role="img"><title>Turkish Get-Up</title><line x1="18" y1="156" x2="140" y2="156" stroke="currentColor" stroke-opacity="0.3" stroke-width="4" stroke-linecap="round"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><circle cx="52" cy="98" r="11"/><line x1="62" y1="104" x2="96" y2="122"/><line x1="64" y1="106" x2="58" y2="154"/><line x1="66" y1="102" x2="72" y2="56"/><circle cx="73" cy="50" r="7" stroke-opacity="0.55"/><path d="M96 122 L108 130 L110 154"/><path d="M96 122 L120 138 L136 152"/></g></svg>`,
  },

  // ── CARRIES ───────────────────────────────────────────────────────
  {
    id: "farmer-carry",
    name: "Farmer's Carry",
    equipment: "dumbbell",
    primary: ["forearms", "core"],
    secondary: ["shoulders"],
    pattern: "carry",
    isCompound: true,
    tier: 1,
    formCues: [
      "Stand tall with your shoulders back and down",
      "Walk slowly with steady, controlled steps",
      "Keep your stomach tight the whole way",
    ],
    substitutes: ["turkish-get-up", "suitcase-carry"],
    figure: `<svg viewBox="0 0 150 190" role="img"><title>Farmer Carry</title><line x1="22" y1="178" x2="128" y2="178" stroke="currentColor" stroke-opacity="0.3" stroke-width="4" stroke-linecap="round"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><circle cx="75" cy="40" r="11"/><line x1="75" y1="51" x2="75" y2="108"/><line x1="66" y1="60" x2="60" y2="106"/><line x1="84" y1="60" x2="90" y2="106"/><circle cx="59" cy="114" r="6" stroke-opacity="0.55"/><circle cx="91" cy="114" r="6" stroke-opacity="0.55"/><path d="M75 108 L64 148 L60 176"/><path d="M75 108 L86 146 L92 174"/></g></svg>`,
  },
  {
    id: "suitcase-carry",
    name: "Suitcase Carry",
    equipment: "kettlebell",
    primary: ["core", "forearms"],
    secondary: ["shoulders", "glutes"],
    pattern: "carry",
    isCompound: true,
    tier: 2,
    formCues: [
      "One bell in one hand, hanging straight down like a suitcase",
      "Don't lean away from it, stand square and fight the side bend",
      "Ribs down, shoulders level, slow steady steps",
      "Switch hands and walk the same distance back",
    ],
    substitutes: ["farmer-carry"],
    figure: `<svg viewBox="0 0 150 190" role="img"><title>Suitcase Carry</title><line x1="22" y1="178" x2="128" y2="178" stroke="currentColor" stroke-opacity="0.3" stroke-width="4" stroke-linecap="round"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><circle cx="72" cy="40" r="11"/><line x1="72" y1="51" x2="72" y2="108"/><line x1="64" y1="60" x2="58" y2="104"/><line x1="80" y1="60" x2="86" y2="88"/><circle cx="57" cy="112" r="6" stroke-opacity="0.55"/><path d="M72 108 L62 148 L58 176"/><path d="M72 108 L84 146 L90 174"/></g></svg>`,
  },
  {
    id: "ruck",
    name: "Ruck",
    equipment: "bodyweight",
    primary: ["glutes", "core"],
    secondary: ["back", "calves", "shoulders"],
    pattern: "carry",
    isCompound: true,
    tier: 2,
    formCues: [
      "Pack high and tight against your back, straps snug, nothing swinging",
      "Walk tall: chest up, shoulders back, short steady steps",
      "Conversational pace. You should be able to talk the whole way",
      "Build the time before you build the weight",
    ],
    safety:
      "If the pack digs in or your posture starts to break down, drop the weight or cut the walk short, especially in the first week.",
    substitutes: ["farmer-carry"],
    figure: `<svg viewBox="0 0 150 190" role="img"><title>Ruck</title><line x1="18" y1="178" x2="132" y2="178" stroke="currentColor" stroke-opacity="0.3" stroke-width="4" stroke-linecap="round"/><rect x="40" y="58" width="26" height="34" rx="4" stroke="currentColor" stroke-opacity="0.5" fill="none" stroke-width="3"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><circle cx="78" cy="42" r="11"/><line x1="76" y1="53" x2="74" y2="108"/><line x1="76" y1="64" x2="62" y2="92"/><line x1="76" y1="64" x2="90" y2="90"/><path d="M74 108 L60 142 L54 176"/><path d="M74 108 L90 144 L98 172"/></g></svg>`,
  },

  // ── Fixed-implement variations ────────────────────────────────────
  // Kettlebell, dumbbell and bodyweight movements that home-gym plans call
  // for and the base library above does not carry.
  {
    id: "kb-single-leg-rdl",
    name: "KB Single-Leg RDL",
    equipment: "kettlebell",
    primary: ["hamstrings", "glutes"],
    secondary: ["back", "core"],
    pattern: "hinge",
    isCompound: true,
    tier: 1,
    formCues: [
      "Bell in the hand opposite the leg you're standing on",
      "Push your hips straight back and let the bell run down your shin",
      "Keep your hips level. Don't let the free hip roll open to the ceiling",
      "Slow. Balance beats speed here",
    ],
    substitutes: ["rdl", "kettlebell-swing"],
    safety: "Touch a wall or chair with your free hand if balance is the limit, the hinge is the point, not the wobble.",
    figure: `<svg viewBox="0 0 150 190" role="img"><title>KB Single-Leg RDL</title><line x1="22" y1="178" x2="128" y2="178" stroke="currentColor" stroke-opacity="0.3" stroke-width="4" stroke-linecap="round"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><circle cx="40" cy="72" r="11"/><line x1="50" y1="78" x2="86" y2="98"/><line x1="60" y1="84" x2="56" y2="126"/><circle cx="55" cy="133" r="6" stroke-opacity="0.55"/><path d="M86 98 L88 140 L88 176"/><path d="M86 98 L108 84 L126 78"/></g></svg>`,
  },
  {
    id: "kb-high-pull",
    name: "KB High Pull",
    equipment: "kettlebell",
    primary: ["shoulders", "back"],
    secondary: ["glutes", "hamstrings"],
    pattern: "horizontal-pull",
    isCompound: true,
    tier: 2,
    formCues: [
      "Start it like a swing, the hips drive the bell, your arm only steers",
      "As the bell floats up, pull your elbow high and back behind you",
      "Keep the bell close to your body the whole way",
      "Let it drop back into the next hinge, don't muscle it down",
    ],
    substitutes: ["rear-delt-fly", "face-pull", "kettlebell-swing"],
    figure: `<svg viewBox="0 0 150 190" role="img"><title>KB High Pull</title><line x1="22" y1="178" x2="128" y2="178" stroke="currentColor" stroke-opacity="0.3" stroke-width="4" stroke-linecap="round"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><circle cx="72" cy="46" r="11"/><line x1="72" y1="57" x2="72" y2="112"/><path d="M72 66 L46 56 L60 74"/><circle cx="66" cy="80" r="7" stroke-opacity="0.55"/><path d="M72 112 L62 146 L62 176"/><path d="M72 112 L84 146 L86 176"/></g></svg>`,
  },
  {
    id: "kb-clean",
    name: "KB Clean",
    equipment: "kettlebell",
    primary: ["glutes", "hamstrings"],
    secondary: ["shoulders", "back", "forearms"],
    pattern: "hinge",
    isCompound: true,
    tier: 2,
    formCues: [
      "Tame the arc. Keep the bell close so it never bangs your wrist",
      "Snap your hips, then guide your hand around the bell rather than letting it flip over",
      "Finish with the bell resting against your forearm, elbow tight to your ribs",
      "Stand tall at the top before the next rep",
    ],
    substitutes: ["kettlebell-swing"],
    figure: `<svg viewBox="0 0 150 190" role="img"><title>KB Clean</title><line x1="22" y1="178" x2="128" y2="178" stroke="currentColor" stroke-opacity="0.3" stroke-width="4" stroke-linecap="round"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><circle cx="74" cy="44" r="11"/><line x1="74" y1="55" x2="74" y2="112"/><path d="M74 66 L60 76 L66 62"/><circle cx="66" cy="56" r="7" stroke-opacity="0.55"/><path d="M74 68 L88 96 L86 112"/><path d="M74 112 L64 146 L64 176"/><path d="M74 112 L86 146 L88 176"/></g></svg>`,
  },
  {
    id: "rear-delt-fly",
    name: "DB Rear Delt Fly",
    equipment: "dumbbell",
    primary: ["shoulders"],
    secondary: ["back"],
    pattern: "isolation",
    isCompound: false,
    tier: 2,
    formCues: [
      "Hinge forward with a flat back, dumbbells hanging under your chest",
      "Raise them out to the sides with a soft, fixed elbow",
      "Lead with your elbows, not your hands",
      "Squeeze for a second at the top, then lower slowly",
    ],
    substitutes: ["face-pull", "kb-high-pull"],
    figure: `<svg viewBox="0 0 150 190" role="img"><title>DB Rear Delt Fly</title><line x1="22" y1="178" x2="128" y2="178" stroke="currentColor" stroke-opacity="0.3" stroke-width="4" stroke-linecap="round"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><circle cx="40" cy="76" r="11"/><line x1="50" y1="82" x2="96" y2="98"/><path d="M62 86 L54 66"/><path d="M64 88 L74 68"/><circle cx="52" cy="60" r="6" stroke-opacity="0.55"/><circle cx="76" cy="62" r="6" stroke-opacity="0.55"/><path d="M96 98 L98 140 L98 176"/><path d="M96 98 L88 140 L86 176"/></g></svg>`,
  },
  {
    id: "db-floor-press",
    name: "DB Floor Press",
    equipment: "dumbbell",
    primary: ["chest", "triceps"],
    secondary: ["shoulders"],
    pattern: "horizontal-push",
    isCompound: true,
    tier: 1,
    formCues: [
      "Lie on the floor, knees bent, feet flat",
      "Lower until the backs of your upper arms touch the floor, then pause",
      "Press straight up and slightly together",
      "Slow on the way down. That's where the work is",
    ],
    substitutes: ["dumbbell-bench", "pushup", "barbell-floor-press"],
    safety: "The floor is your safety stop, no bench and no spotter needed.",
    figure: `<svg viewBox="0 0 150 190" role="img"><title>DB Floor Press</title><line x1="14" y1="150" x2="142" y2="150" stroke="currentColor" stroke-opacity="0.3" stroke-width="4" stroke-linecap="round"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><circle cx="34" cy="136" r="11"/><line x1="45" y1="140" x2="96" y2="142"/><path d="M60 140 L62 110"/><circle cx="62" cy="102" r="6" stroke-opacity="0.55"/><path d="M96 142 L112 120 L128 146"/></g></svg>`,
  },
  {
    id: "barbell-floor-press",
    name: "Barbell Floor Press",
    equipment: "barbell",
    primary: ["chest", "triceps"],
    secondary: ["shoulders"],
    pattern: "horizontal-push",
    isCompound: true,
    tier: 2,
    formCues: [
      "Lie on the floor with the bar over your chest, knees bent",
      "Lower until your upper arms rest on the floor, pause for a beat",
      "Press straight up, no bounce off the floor",
      "Keep your wrists stacked over your elbows",
    ],
    substitutes: ["db-floor-press", "bench-press", "pushup"],
    safety: "Roll the bar over your hips to set up and finish rather than lifting it past your face.",
    figure: `<svg viewBox="0 0 150 190" role="img"><title>Barbell Floor Press</title><line x1="14" y1="150" x2="142" y2="150" stroke="currentColor" stroke-opacity="0.3" stroke-width="4" stroke-linecap="round"/><line x1="46" y1="104" x2="112" y2="104" stroke="currentColor" stroke-opacity="0.45" stroke-width="4" stroke-linecap="round"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><circle cx="34" cy="136" r="11"/><line x1="45" y1="140" x2="96" y2="142"/><path d="M58 138 L56 106"/><path d="M72 140 L74 106"/><path d="M96 142 L112 120 L128 146"/></g></svg>`,
  },
  {
    id: "hang-power-clean",
    name: "Hang Power Clean",
    equipment: "barbell",
    primary: ["glutes", "hamstrings", "back"],
    secondary: ["shoulders", "quads"],
    pattern: "hinge",
    isCompound: true,
    tier: 2,
    formCues: [
      "Start from the hang, bar just above your knees, back flat",
      "Explosive hip snap, the bar rides straight up your thighs",
      "Drop under it and catch it in the rack with your elbows high",
      "Stand tall, then reset your back before the next rep",
    ],
    substitutes: ["kettlebell-swing", "deadlift"],
    safety: "Technical lift, groove the sequence with the bare 44 lb bar before you load it.",
    figure: `<svg viewBox="0 0 150 190" role="img"><title>Hang Power Clean</title><line x1="22" y1="178" x2="128" y2="178" stroke="currentColor" stroke-opacity="0.3" stroke-width="4" stroke-linecap="round"/><line x1="42" y1="86" x2="108" y2="86" stroke="currentColor" stroke-opacity="0.45" stroke-width="4" stroke-linecap="round"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><circle cx="75" cy="46" r="11"/><line x1="75" y1="57" x2="75" y2="112"/><path d="M75 68 L54 86"/><path d="M75 68 L96 86"/><path d="M75 112 L62 144 L62 176"/><path d="M75 112 L88 144 L88 176"/></g></svg>`,
  },
  {
    id: "push-press",
    name: "Push Press",
    equipment: "barbell",
    primary: ["shoulders"],
    secondary: ["triceps", "quads", "core"],
    pattern: "vertical-push",
    isCompound: true,
    tier: 1,
    formCues: [
      "Dip a few inches with your knees, chest stays upright",
      "Drive hard with your legs and let that start the bar",
      "Your arms only finish it, lock out with the bar over your ears",
      "This is not a strict press; the leg drive is the whole point",
    ],
    substitutes: ["ohp", "dumbbell-shoulder-press"],
    figure: `<svg viewBox="0 0 150 190" role="img"><title>Push Press</title><line x1="44" y1="22" x2="106" y2="22" stroke="currentColor" stroke-opacity="0.45" stroke-width="4" stroke-linecap="round"/><line x1="22" y1="178" x2="128" y2="178" stroke="currentColor" stroke-opacity="0.3" stroke-width="4" stroke-linecap="round"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><circle cx="75" cy="52" r="11"/><line x1="75" y1="63" x2="75" y2="114"/><path d="M75 72 L58 46 L56 24"/><path d="M75 72 L92 46 L94 24"/><path d="M75 114 L60 138 L62 176"/><path d="M75 114 L90 138 L88 176"/></g></svg>`,
  },
  {
    id: "pike-pushup",
    name: "Pike Push-Up",
    equipment: "bodyweight",
    primary: ["shoulders"],
    secondary: ["triceps", "core"],
    pattern: "vertical-push",
    isCompound: true,
    tier: 2,
    formCues: [
      "Hips high, body in an upside-down V",
      "Lower the top of your head toward the floor between your hands",
      "Elbows track back and slightly out, not flared wide",
      "Press back up until your arms are straight",
    ],
    substitutes: ["dumbbell-shoulder-press", "pushup", "ohp"],
    figure: `<svg viewBox="0 0 150 190" role="img"><title>Pike Push-Up</title><line x1="14" y1="158" x2="142" y2="158" stroke="currentColor" stroke-opacity="0.3" stroke-width="4" stroke-linecap="round"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><circle cx="44" cy="120" r="11"/><path d="M52 114 L84 62"/><path d="M84 62 L116 152"/><path d="M50 128 L38 156"/></g></svg>`,
  },
  {
    id: "scapular-pullup",
    name: "Scapular Pull-Up",
    equipment: "bodyweight",
    primary: ["back"],
    secondary: ["forearms", "shoulders"],
    pattern: "vertical-pull",
    isCompound: false,
    tier: 2,
    formCues: [
      "Hang from the bar with straight arms",
      "Without bending your elbows, pull your shoulders down away from your ears",
      "Hold the top for a beat, then let them rise again under control",
      "Small movement, a couple of inches is the whole rep",
    ],
    substitutes: ["dead-hang", "pullup"],
    figure: `<svg viewBox="0 0 150 190" role="img"><title>Scapular Pull-Up</title><line x1="36" y1="26" x2="114" y2="26" stroke="currentColor" stroke-opacity="0.45" stroke-width="4" stroke-linecap="round"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><line x1="64" y1="52" x2="64" y2="28"/><line x1="86" y1="52" x2="86" y2="28"/><circle cx="75" cy="62" r="11"/><line x1="75" y1="73" x2="75" y2="128"/><path d="M75 128 L66 158 L66 178"/><path d="M75 128 L84 158 L84 178"/></g></svg>`,
  },
  {
    id: "dead-hang",
    name: "Dead Hang",
    equipment: "bodyweight",
    primary: ["forearms", "back"],
    secondary: ["shoulders"],
    pattern: "vertical-pull",
    isCompound: false,
    tier: 2,
    formCues: [
      "Hang with straight arms and let your body lengthen",
      "Relax and breathe. This is decompression, not a hold-your-breath test",
      "Keep a firm grip and let go before your hands do",
      "Step off gently rather than dropping",
    ],
    substitutes: ["scapular-pullup"],
    figure: `<svg viewBox="0 0 150 190" role="img"><title>Dead Hang</title><line x1="36" y1="26" x2="114" y2="26" stroke="currentColor" stroke-opacity="0.45" stroke-width="4" stroke-linecap="round"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><line x1="66" y1="58" x2="66" y2="28"/><line x1="84" y1="58" x2="84" y2="28"/><circle cx="75" cy="68" r="11"/><line x1="75" y1="79" x2="75" y2="136"/><line x1="75" y1="136" x2="72" y2="176"/><line x1="75" y1="136" x2="79" y2="176"/></g></svg>`,
  },
  {
    id: "glute-bridge",
    name: "Glute Bridge",
    equipment: "bodyweight",
    primary: ["glutes"],
    secondary: ["hamstrings", "core"],
    pattern: "hinge",
    isCompound: false,
    tier: 2,
    formCues: [
      "Lie on your back, knees bent, heels close to your backside",
      "Push through your heels and squeeze your glutes to lift your hips",
      "Stop where your body makes a straight line. Don't arch past it",
      "Lower slowly and reset",
    ],
    substitutes: ["rdl", "kettlebell-swing"],
    figure: `<svg viewBox="0 0 150 190" role="img"><title>Glute Bridge</title><line x1="14" y1="156" x2="142" y2="156" stroke="currentColor" stroke-opacity="0.3" stroke-width="4" stroke-linecap="round"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><circle cx="30" cy="144" r="11"/><path d="M41 140 L84 108"/><path d="M84 108 L104 140 L104 154"/><path d="M44 146 L40 154"/></g></svg>`,
  },
  {
    id: "hollow-hold",
    name: "Hollow Hold",
    equipment: "bodyweight",
    primary: ["core"],
    secondary: [],
    pattern: "isolation",
    isCompound: false,
    tier: 2,
    formCues: [
      "Lie on your back and press your lower back flat into the floor",
      "Lift your shoulders and legs a few inches, arms overhead",
      "If your back lifts off the floor, bend your knees or lower your arms",
      "Breathe. Don't hold your breath to stay tight",
    ],
    substitutes: ["plank", "hanging-leg-raise"],
    figure: `<svg viewBox="0 0 150 190" role="img"><title>Hollow Hold</title><line x1="14" y1="150" x2="142" y2="150" stroke="currentColor" stroke-opacity="0.3" stroke-width="4" stroke-linecap="round"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><circle cx="52" cy="120" r="11"/><path d="M62 124 Q90 140 118 118"/><path d="M46 112 L20 100"/></g></svg>`,
  },
];

/* ────────────────────────────────────────────────────────────────────
 * Workout templates (split routines)
 * ──────────────────────────────────────────────────────────────────── */

export interface WorkoutTemplate {
  id: string;
  name: string;
  description: string;
  level: "beginner" | "intermediate" | "advanced";
  daysPerWeek: number;
  /** Each day is a list of exercise IDs from EXERCISE_DB */
  days: { name: string; exerciseIds: string[]; targetSets?: number; targetRepsLow?: number; targetRepsHigh?: number }[];
}

export const TEMPLATES: WorkoutTemplate[] = [
  {
    id: "ppl",
    name: "Push / Pull / Legs",
    description:
      "Classic 6-day bodybuilding split. Each muscle group gets 2 sessions per week with great recovery.",
    level: "intermediate",
    daysPerWeek: 6,
    days: [
      {
        name: "Push A",
        exerciseIds: ["bench-press", "ohp", "dip", "lateral-raise", "tricep-pushdown"],
        targetSets: 4,
        targetRepsLow: 6,
        targetRepsHigh: 10,
      },
      {
        name: "Pull A",
        exerciseIds: ["deadlift", "pullup", "barbell-row", "face-pull", "barbell-curl"],
        targetSets: 4,
        targetRepsLow: 6,
        targetRepsHigh: 10,
      },
      {
        name: "Legs A",
        exerciseIds: ["back-squat", "rdl", "bulgarian-split-squat", "calf-raise", "hanging-leg-raise"],
        targetSets: 4,
        targetRepsLow: 6,
        targetRepsHigh: 12,
      },
      {
        name: "Push B",
        exerciseIds: ["dumbbell-bench", "dumbbell-shoulder-press", "pushup", "lateral-raise", "tricep-pushdown"],
        targetSets: 3,
        targetRepsLow: 8,
        targetRepsHigh: 12,
      },
      {
        name: "Pull B",
        exerciseIds: ["rdl", "lat-pulldown", "dumbbell-row", "face-pull", "barbell-curl"],
        targetSets: 3,
        targetRepsLow: 8,
        targetRepsHigh: 12,
      },
      {
        name: "Legs B",
        exerciseIds: ["front-squat", "rdl", "goblet-squat", "calf-raise", "plank"],
        targetSets: 3,
        targetRepsLow: 8,
        targetRepsHigh: 15,
      },
    ],
  },
  {
    id: "upper-lower",
    name: "Upper / Lower",
    description:
      "Balanced 4-day split. Great for intermediate lifters wanting strength and size.",
    level: "intermediate",
    daysPerWeek: 4,
    days: [
      {
        name: "Upper A (Heavy)",
        exerciseIds: ["bench-press", "barbell-row", "ohp", "pullup", "tricep-pushdown", "barbell-curl"],
        targetSets: 4,
        targetRepsLow: 5,
        targetRepsHigh: 8,
      },
      {
        name: "Lower A (Heavy)",
        exerciseIds: ["back-squat", "rdl", "bulgarian-split-squat", "calf-raise", "plank"],
        targetSets: 4,
        targetRepsLow: 5,
        targetRepsHigh: 8,
      },
      {
        name: "Upper B (Volume)",
        exerciseIds: ["dumbbell-bench", "lat-pulldown", "dumbbell-shoulder-press", "dumbbell-row", "lateral-raise", "face-pull"],
        targetSets: 3,
        targetRepsLow: 10,
        targetRepsHigh: 15,
      },
      {
        name: "Lower B (Volume)",
        exerciseIds: ["front-squat", "rdl", "goblet-squat", "calf-raise", "hanging-leg-raise"],
        targetSets: 3,
        targetRepsLow: 10,
        targetRepsHigh: 15,
      },
    ],
  },
  {
    id: "full-body-3day",
    name: "Full Body 3-Day",
    description: "Hit every major muscle group 3 times per week. Ideal for beginners and busy people.",
    level: "beginner",
    daysPerWeek: 3,
    days: [
      {
        name: "Full Body A",
        exerciseIds: ["back-squat", "bench-press", "barbell-row", "plank"],
        targetSets: 3,
        targetRepsLow: 6,
        targetRepsHigh: 10,
      },
      {
        name: "Full Body B",
        exerciseIds: ["deadlift", "ohp", "pullup", "hanging-leg-raise"],
        targetSets: 3,
        targetRepsLow: 6,
        targetRepsHigh: 10,
      },
      {
        name: "Full Body C",
        exerciseIds: ["front-squat", "dumbbell-bench", "dumbbell-row", "face-pull"],
        targetSets: 3,
        targetRepsLow: 8,
        targetRepsHigh: 12,
      },
    ],
  },
  {
    id: "minimalist",
    name: "Minimalist Strength",
    description: "Just the big lifts. 2 days a week, 30 minutes per session. Perfect for busy weeks.",
    level: "beginner",
    daysPerWeek: 2,
    days: [
      {
        name: "Day A",
        exerciseIds: ["back-squat", "bench-press", "barbell-row"],
        targetSets: 5,
        targetRepsLow: 5,
        targetRepsHigh: 5,
      },
      {
        name: "Day B",
        exerciseIds: ["deadlift", "ohp", "pullup"],
        targetSets: 5,
        targetRepsLow: 5,
        targetRepsHigh: 5,
      },
    ],
  },
];

/* ────────────────────────────────────────────────────────────────────
 * Analytics helpers
 * ──────────────────────────────────────────────────────────────────── */

/**
 * Estimate 1-rep max from a working set using the Epley formula.
 * Most accurate for sets in the 1–10 rep range.
 *
 * 1RM = weight × (1 + reps / 30)
 */
export function estimate1RM(weight: number, reps: number): number {
  if (weight <= 0 || reps <= 0) return 0;
  if (reps === 1) return weight;
  return Math.round(weight * (1 + reps / 30) * 10) / 10;
}

/**
 * Calculate the weight you should use to hit a target rep count at a
 * given percentage of your 1RM. Useful for picking working weights.
 */
export function weightForReps(oneRM: number, targetReps: number): number {
  // Inverted Epley: w = 1RM / (1 + reps/30)
  if (oneRM <= 0 || targetReps <= 0) return 0;
  return Math.round((oneRM / (1 + targetReps / 30)) * 2) / 2; // round to nearest 0.5
}

/**
 * RPE-to-RIR mapping. RPE 10 = max effort, RPE 6 = 4 reps in reserve.
 */
export const RPE_RIR: Record<number, number> = {
  10: 0,
  9.5: 0,
  9: 1,
  8.5: 1,
  8: 2,
  7.5: 2,
  7: 3,
  6.5: 3,
  6: 4,
  5: 5,
};

export function rpeToRir(rpe: number): number {
  return RPE_RIR[rpe] ?? Math.max(0, Math.round(10 - rpe));
}

/**
 * Volume landmarks per muscle group, in working sets per week.
 * Based on Israetel's Renaissance Periodization recommendations.
 *
 *   MV  = Maintenance Volume
 *   MEV = Minimum Effective Volume (start of growth)
 *   MAV = Maximum Adaptive Volume (sweet spot)
 *   MRV = Maximum Recoverable Volume (overload ceiling)
 */
export const VOLUME_LANDMARKS: Record<MuscleGroup, { mv: number; mev: number; mav: number; mrv: number }> = {
  chest:      { mv: 8,  mev: 10, mav: 16, mrv: 22 },
  back:       { mv: 8,  mev: 10, mav: 18, mrv: 25 },
  shoulders:  { mv: 8,  mev: 8,  mav: 16, mrv: 26 },
  biceps:     { mv: 5,  mev: 8,  mav: 14, mrv: 20 },
  triceps:    { mv: 4,  mev: 6,  mav: 14, mrv: 18 },
  quads:      { mv: 6,  mev: 8,  mav: 14, mrv: 20 },
  hamstrings: { mv: 4,  mev: 6,  mav: 12, mrv: 18 },
  glutes:     { mv: 4,  mev: 6,  mav: 12, mrv: 16 },
  calves:     { mv: 6,  mev: 8,  mav: 14, mrv: 20 },
  core:       { mv: 0,  mev: 6,  mav: 16, mrv: 25 },
  forearms:   { mv: 0,  mev: 4,  mav: 10, mrv: 16 },
};

export function getExercise(id: string): Exercise | undefined {
  return EXERCISE_DB.find((e) => e.id === id);
}

export function exercisesByMuscle(muscle: MuscleGroup): Exercise[] {
  return EXERCISE_DB.filter(
    (e) => e.primary.includes(muscle) || e.secondary.includes(muscle),
  );
}

export function exercisesByEquipment(equipment: Equipment): Exercise[] {
  return EXERCISE_DB.filter((e) => e.equipment === equipment);
}


/* ────────────────────────────────────────────────────────────────────
 * Stretch & recovery library
 *
 * Cooldown stretches and rest-day mobility/recovery movements. These are not
 * loaded lifts (nothing to log), so they live in a sibling DB rather than
 * EXERCISE_DB, but the shape mirrors an Exercise's coaching fields so the
 * fitness card can resolve a figure + cue for any stretch id the plan
 * references. Hand-authored, vetted content, never generated at runtime.
 * ──────────────────────────────────────────────────────────────────── */

export interface Stretch {
  id: string;
  name: string;
  /** One encouraging, safety-first coaching paragraph in Puff's voice. */
  cue: string;
  /** Three quick checkpoints. */
  checkpoints: string[];
  /** Inline SVG stick figure (viewBox 0 0 150 190, currentColor strokes). */
  figure: string;
}

export const STRETCH_DB: Stretch[] = [
  {
    id: "doorway-pec-stretch",
    name: "Doorway Pec Stretch",
    cue:
      "Stand in a doorway and set your forearm against the frame with your elbow at about shoulder height. Step gently forward with one foot until you feel an easy stretch across the front of your chest and shoulder, nothing sharp. Keep your ribs down and breathe; hold, then ease off, and let those pressing muscles unwind after all the push work.",
    checkpoints: ["forearm on the frame, elbow shoulder-height", "step forward for an easy stretch", "ribs down, breathe, no sharp pull"],
    figure: `<svg viewBox="0 0 150 190" role="img"><title>Doorway Pec Stretch</title><line x1="118" y1="18" x2="118" y2="172" stroke="currentColor" stroke-opacity="0.4" stroke-width="4" stroke-linecap="round"/><line x1="22" y1="178" x2="128" y2="178" stroke="currentColor" stroke-opacity="0.3" stroke-width="4" stroke-linecap="round"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><circle cx="64" cy="46" r="11"/><line x1="64" y1="57" x2="66" y2="112"/><path d="M66 66 L94 60 L114 68"/><path d="M66 68 L54 96 L56 112"/><path d="M66 112 L56 148 L52 176"/><path d="M66 112 L80 146 L90 172"/></g></svg>`,
  },
  {
    id: "cross-body-shoulder-stretch",
    name: "Cross-Body Shoulder Stretch",
    cue:
      "Bring one arm straight across the front of your body at chest height. Use your other hand to gently draw it closer against your chest until you feel a soft stretch in the back of the shoulder. Keep that shoulder down away from your ear and don't shrug; hold easy, breathe, then switch arms.",
    checkpoints: ["arm straight across at chest height", "opposite hand draws it in gently", "shoulder down, no shrug"],
    figure: `<svg viewBox="0 0 150 190" role="img"><title>Cross-Body Shoulder Stretch</title><line x1="22" y1="178" x2="128" y2="178" stroke="currentColor" stroke-opacity="0.3" stroke-width="4" stroke-linecap="round"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><circle cx="78" cy="44" r="11"/><line x1="78" y1="55" x2="78" y2="112"/><line x1="78" y1="66" x2="40" y2="74"/><path d="M78 68 L54 84 L44 76"/><path d="M78 112 L68 148 L68 176"/><path d="M78 112 L88 148 L88 176"/></g></svg>`,
  },
  {
    id: "overhead-triceps-stretch",
    name: "Overhead Triceps Stretch",
    cue:
      "Reach one arm up and let your hand drop down behind your head, elbow pointing at the ceiling. With your other hand, gently press on that elbow until you feel a stretch down the back of your upper arm. Keep your ribs from flaring, stand tall. Hold softly, then switch sides.",
    checkpoints: ["hand drops behind the head", "gently press the elbow back", "stand tall, ribs down"],
    figure: `<svg viewBox="0 0 150 190" role="img"><title>Overhead Triceps Stretch</title><line x1="22" y1="178" x2="128" y2="178" stroke="currentColor" stroke-opacity="0.3" stroke-width="4" stroke-linecap="round"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><circle cx="72" cy="48" r="11"/><line x1="72" y1="59" x2="72" y2="114"/><path d="M72 68 L92 44 L74 54"/><path d="M72 70 L86 50 L92 42"/><path d="M72 114 L63 148 L63 176"/><path d="M72 114 L81 148 L81 176"/></g></svg>`,
  },
  {
    id: "thoracic-extension-stretch",
    name: "Thoracic Extension Stretch",
    cue:
      "Stand tall, lace your fingers loosely or reach your arms up overhead, and gently arch your upper back. Think of lifting your chest toward the ceiling. Let the movement come from the middle of your back, not by cranking your lower back or letting your ribs flare. Breathe into it and hold easy; a few gentle reaches undo a lot of hunching.",
    checkpoints: ["reach up, lift the chest", "arch from the mid-back", "keep the low back quiet"],
    figure: `<svg viewBox="0 0 150 190" role="img"><title>Thoracic Extension Stretch</title><line x1="22" y1="178" x2="128" y2="178" stroke="currentColor" stroke-opacity="0.3" stroke-width="4" stroke-linecap="round"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><circle cx="80" cy="48" r="11"/><path d="M78 59 Q72 86 74 112"/><path d="M76 66 L64 40 L66 22"/><path d="M76 66 L88 40 L88 22"/><path d="M74 112 L64 148 L64 176"/><path d="M74 112 L84 148 L84 176"/></g></svg>`,
  },
  {
    id: "childs-pose-lat-stretch",
    name: "Child's Pose",
    cue:
      "Kneel and sit your hips back toward your heels, then walk your hands out in front and lower your chest toward the floor. Reach your fingertips long and, to bias the lats, walk both hands slightly to one side until you feel the stretch down that side of your back. Relax your neck, breathe slowly, and let your whole back settle; this is a rest, not a strain.",
    checkpoints: ["hips back toward the heels", "reach the fingertips long", "relax the neck and breathe"],
    figure: `<svg viewBox="0 0 150 190" role="img"><title>Child's Pose</title><line x1="16" y1="156" x2="142" y2="156" stroke="currentColor" stroke-opacity="0.3" stroke-width="4" stroke-linecap="round"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><circle cx="110" cy="134" r="11"/><line x1="100" y1="132" x2="52" y2="148"/><line x1="100" y1="134" x2="138" y2="152"/><path d="M52 148 L40 138"/><path d="M52 148 L44 154"/></g></svg>`,
  },
  {
    id: "biceps-wall-stretch",
    name: "Biceps Wall Stretch",
    cue:
      "Stand next to a wall and place your palm flat on it behind you at about shoulder height, arm straight. Slowly turn your body away from that arm until you feel a gentle stretch along the front of your upper arm and shoulder. Keep it mild and keep breathing; hold, then switch sides to unwind both biceps after all the pulling.",
    checkpoints: ["palm flat on the wall, arm straight", "turn away for a gentle stretch", "keep it mild, no sharp pull"],
    figure: `<svg viewBox="0 0 150 190" role="img"><title>Biceps Wall Stretch</title><line x1="30" y1="18" x2="30" y2="172" stroke="currentColor" stroke-opacity="0.4" stroke-width="4" stroke-linecap="round"/><line x1="24" y1="178" x2="128" y2="178" stroke="currentColor" stroke-opacity="0.3" stroke-width="4" stroke-linecap="round"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><circle cx="74" cy="46" r="11"/><line x1="74" y1="57" x2="72" y2="112"/><line x1="73" y1="66" x2="34" y2="72"/><path d="M73 68 L86 96 L84 112"/><path d="M72 112 L62 148 L58 176"/><path d="M72 112 L84 146 L92 174"/></g></svg>`,
  },
  {
    id: "forearm-flexor-stretch",
    name: "Forearm Flexor Stretch",
    cue:
      "Reach one arm straight out in front of you, palm facing up. With your other hand, gently pull the fingers back and down toward the floor until you feel a stretch through your palm and inner forearm. Keep the outstretched elbow straight and the pull soft; hold, breathe, and switch. Your grip will thank you after the rows and carries.",
    checkpoints: ["arm straight out, palm up", "gently draw the fingers back", "keep the elbow straight, pull soft"],
    figure: `<svg viewBox="0 0 150 190" role="img"><title>Forearm Flexor Stretch</title><line x1="22" y1="178" x2="128" y2="178" stroke="currentColor" stroke-opacity="0.3" stroke-width="4" stroke-linecap="round"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><circle cx="62" cy="46" r="11"/><line x1="62" y1="57" x2="62" y2="112"/><line x1="62" y1="66" x2="110" y2="72"/><path d="M62 68 L94 86 L112 80"/><path d="M62 112 L53 148 L53 176"/><path d="M62 112 L71 148 L71 176"/></g></svg>`,
  },
  {
    id: "neck-side-stretch",
    name: "Neck Side Stretch",
    cue:
      "Sit or stand tall and let one ear drift toward that shoulder, keeping the opposite shoulder relaxed and down. For a touch more, rest that same-side hand lightly on your head, no pulling, just the weight of your hand. Breathe slowly and ease out; go gently, the neck likes kindness, not force.",
    checkpoints: ["ear toward the shoulder", "opposite shoulder stays down", "hand weight only, no pulling"],
    figure: `<svg viewBox="0 0 150 190" role="img"><title>Neck Side Stretch</title><line x1="22" y1="178" x2="128" y2="178" stroke="currentColor" stroke-opacity="0.3" stroke-width="4" stroke-linecap="round"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><circle cx="82" cy="46" r="11"/><path d="M78 56 L74 112"/><path d="M76 64 L92 44 L84 34"/><path d="M76 66 L64 96 L66 112"/><path d="M74 112 L65 148 L65 176"/><path d="M74 112 L83 148 L83 176"/></g></svg>`,
  },
  {
    id: "standing-quad-stretch",
    name: "Standing Quad Stretch",
    cue:
      "Standing tall, bend one knee and catch that foot behind you, drawing your heel toward your backside. Keep your knees close together and your hips pushed slightly forward so you feel the stretch down the front of your thigh. Hold onto a wall for balance if you need it, breathe, and switch legs, no yanking on the ankle.",
    checkpoints: ["heel toward the backside", "knees together, hips forward", "steady balance, gentle hold"],
    figure: `<svg viewBox="0 0 150 190" role="img"><title>Standing Quad Stretch</title><line x1="22" y1="178" x2="128" y2="178" stroke="currentColor" stroke-opacity="0.3" stroke-width="4" stroke-linecap="round"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><circle cx="72" cy="42" r="11"/><line x1="72" y1="53" x2="72" y2="110"/><line x1="72" y1="64" x2="52" y2="72"/><path d="M72 66 L80 96 L70 118"/><line x1="72" y1="110" x2="70" y2="176"/><path d="M72 110 L86 130 L70 120"/></g></svg>`,
  },
  {
    id: "seated-hamstring-stretch",
    name: "Seated Hamstring Stretch",
    cue:
      "Sit with one or both legs stretched out in front, toes pointing up. Hinge forward from your hips, leading with your chest, back long, and reach toward your toes until you feel an easy stretch behind your thigh. Don't round your back to grab your feet; go only as far as your hamstrings comfortably allow, and breathe into it.",
    checkpoints: ["legs long, toes up", "hinge from the hips, chest leads", "easy reach. Don't round the back"],
    figure: `<svg viewBox="0 0 150 190" role="img"><title>Seated Hamstring Stretch</title><line x1="16" y1="152" x2="142" y2="152" stroke="currentColor" stroke-opacity="0.3" stroke-width="4" stroke-linecap="round"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><line x1="44" y1="142" x2="124" y2="142"/><line x1="124" y1="142" x2="124" y2="128"/><circle cx="88" cy="104" r="11"/><line x1="46" y1="140" x2="82" y2="110"/><line x1="90" y1="112" x2="124" y2="134"/></g></svg>`,
  },
  {
    id: "figure-four-glute-stretch",
    name: "Figure-Four Glute Stretch",
    cue:
      "Lie on your back, bend both knees, then cross one ankle over the opposite thigh to make a figure four. Reach through and hold behind that thigh, gently drawing your legs toward your chest until you feel a stretch deep in the glute and hip. Keep your head and shoulders relaxed on the floor, breathe, and switch sides.",
    checkpoints: ["ankle crossed over the thigh", "draw the legs toward the chest", "head and shoulders stay relaxed"],
    figure: `<svg viewBox="0 0 150 190" role="img"><title>Figure-Four Glute Stretch</title><line x1="16" y1="152" x2="142" y2="152" stroke="currentColor" stroke-opacity="0.3" stroke-width="4" stroke-linecap="round"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><circle cx="30" cy="128" r="11"/><line x1="41" y1="130" x2="84" y2="134"/><line x1="84" y1="134" x2="102" y2="100"/><line x1="102" y1="100" x2="98" y2="132"/><line x1="70" y1="116" x2="112" y2="124"/><line x1="64" y1="132" x2="96" y2="110"/></g></svg>`,
  },
  {
    id: "calf-wall-stretch",
    name: "Calf Wall Stretch",
    cue:
      "Face a wall, place both hands on it, and step one foot well back with that leg straight and heel pressed to the floor. Lean your hips toward the wall, keeping the back knee straight, until you feel a stretch through your calf. Then, for a second round, bend that back knee slightly to reach the lower calf; hold each, breathe, and switch.",
    checkpoints: ["back leg straight, heel down", "lean the hips toward the wall", "keep the stretch steady, not bouncy"],
    figure: `<svg viewBox="0 0 150 190" role="img"><title>Calf Wall Stretch</title><line x1="120" y1="18" x2="120" y2="172" stroke="currentColor" stroke-opacity="0.4" stroke-width="4" stroke-linecap="round"/><line x1="22" y1="176" x2="120" y2="176" stroke="currentColor" stroke-opacity="0.3" stroke-width="4" stroke-linecap="round"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><circle cx="60" cy="48" r="11"/><line x1="62" y1="59" x2="78" y2="104"/><line x1="66" y1="70" x2="114" y2="62"/><line x1="66" y1="72" x2="114" y2="74"/><path d="M78 104 L92 132 L96 174"/><path d="M78 104 L52 138 L40 174"/></g></svg>`,
  },
  {
    id: "worlds-greatest-stretch",
    name: "World's Greatest Stretch",
    cue:
      "Step into a long lunge and lower your back knee toward the floor so you feel the front of that back hip open up. Plant your inside hand by your front foot, then reach your other arm up toward the ceiling and follow it with your eyes, rotating your chest open. Move slowly through it, breathe, and do both sides. This one wakes up everything at once.",
    checkpoints: ["long lunge, back hip opens", "inside hand plants by the front foot", "top arm reaches up, chest rotates open"],
    figure: `<svg viewBox="0 0 150 190" role="img"><title>World's Greatest Stretch</title><line x1="16" y1="172" x2="142" y2="172" stroke="currentColor" stroke-opacity="0.3" stroke-width="4" stroke-linecap="round"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><circle cx="54" cy="64" r="11"/><line x1="58" y1="74" x2="78" y2="106"/><line x1="62" y1="78" x2="56" y2="150"/><line x1="62" y1="76" x2="74" y2="32"/><path d="M78 106 L68 138 L70 170"/><path d="M78 106 L110 138 L128 166"/></g></svg>`,
  },
  {
    id: "downward-dog-stretch",
    name: "Downward Dog",
    cue:
      "From your hands and knees, tuck your toes and lift your hips up and back into an upside-down V. Press the floor away through your hands, let your head hang between your arms, and gently reach your heels toward the ground. It's fine to keep a soft bend in your knees. Think long spine first, straight legs second. Breathe slow and easy.",
    checkpoints: ["hips lift up and back into a V", "press the floor away, long spine", "soft knees fine, heels reach down"],
    figure: `<svg viewBox="0 0 150 190" role="img"><title>Downward Dog</title><line x1="16" y1="156" x2="142" y2="156" stroke="currentColor" stroke-opacity="0.3" stroke-width="4" stroke-linecap="round"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><line x1="75" y1="58" x2="32" y2="152"/><line x1="75" y1="58" x2="120" y2="152"/><circle cx="50" cy="106" r="10"/></g></svg>`,
  },
  {
    id: "kneeling-hip-flexor-stretch",
    name: "Kneeling Hip Flexor Stretch",
    cue:
      "Kneel on one knee with the other foot planted flat in front, like a proposal position, cushion the down knee if the floor is hard. Squeeze the glute on your kneeling side and gently glide your hips forward until you feel a stretch across the front of that hip. Stay tall and don't arch your lower back to fake more range; hold, breathe, then switch.",
    checkpoints: ["half-kneel, front foot flat", "squeeze the glute, glide hips forward", "stay tall. Don't arch the low back"],
    figure: `<svg viewBox="0 0 150 190" role="img"><title>Kneeling Hip Flexor Stretch</title><line x1="22" y1="172" x2="128" y2="172" stroke="currentColor" stroke-opacity="0.3" stroke-width="4" stroke-linecap="round"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><circle cx="68" cy="46" r="11"/><line x1="68" y1="57" x2="72" y2="108"/><path d="M70 68 L88 96 L94 118"/><path d="M72 108 L94 128 L94 168"/><path d="M72 108 L56 140 L34 156"/></g></svg>`,
  },
  {
    id: "easy-walk",
    name: "Easy Walk",
    cue:
      "Keep it conversational. You should be able to chat the whole way without gasping. Breathe in and out through your nose if you can; that's your built-in speed limit for an easy day. Pick flat ground over hills, let your arms swing, and just enjoy moving; today the walk is the whole point, not a warm-up for something harder.",
    checkpoints: ["conversation pace, no hills", "nasal breathing only", "relaxed arms, easy stride"],
    figure: `<svg viewBox="0 0 150 190" role="img"><title>Easy Walk</title><line x1="22" y1="178" x2="128" y2="178" stroke="currentColor" stroke-opacity="0.3" stroke-width="4" stroke-linecap="round"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><circle cx="70" cy="40" r="11"/><line x1="70" y1="51" x2="72" y2="104"/><line x1="71" y1="62" x2="58" y2="92"/><line x1="71" y1="62" x2="84" y2="86"/><path d="M72 104 L58 140 L54 176"/><path d="M72 104 L88 142 L96 172"/></g></svg>`,
  },
  {
    id: "hip-90-90",
    name: "90/90 Hip Switches",
    cue:
      "Sit on the floor with one shin in front of you and the other out to the side, both knees bent to about ninety degrees. Sitting tall, rotate slowly from one side to the other, letting your knees fall to the new position, no forcing, no bouncing. Only go as far as feels easy; this is gentle mobility to open the hips, not a stretch to grit through.",
    checkpoints: ["both knees near 90 degrees", "sit tall, rotate slowly", "stay in the easy range, no forcing"],
    figure: `<svg viewBox="0 0 150 190" role="img"><title>90/90 Hip Switches</title><line x1="18" y1="156" x2="140" y2="156" stroke="currentColor" stroke-opacity="0.3" stroke-width="4" stroke-linecap="round"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><circle cx="56" cy="70" r="11"/><line x1="56" y1="81" x2="56" y2="128"/><line x1="56" y1="92" x2="78" y2="116"/><path d="M56 128 L92 128 L92 150"/><path d="M56 128 L40 150 L66 152"/></g></svg>`,
  },
  {
    id: "thoracic-opener",
    name: "Thoracic Opener",
    cue:
      "Lie back over a foam roller or a rolled towel placed across your mid-back, knees bent and feet flat. Support your head in your hands and let your upper back drape gently over the roller, breathing into the stretch. Never crank your lower back to get more. Do a few slow reps per spot; you're opening the stiff middle of your spine, one relaxed breath at a time.",
    checkpoints: ["roller across the mid-back", "hands cradle the head", "extend from the upper back, not the low back"],
    figure: `<svg viewBox="0 0 150 190" role="img"><title>Thoracic Opener</title><line x1="18" y1="160" x2="140" y2="160" stroke="currentColor" stroke-opacity="0.3" stroke-width="4" stroke-linecap="round"/><circle cx="78" cy="142" r="9" stroke="currentColor" stroke-opacity="0.55" fill="none" stroke-width="3"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><circle cx="110" cy="116" r="11"/><line x1="100" y1="120" x2="52" y2="140"/><path d="M100 118 L104 100 L92 90"/><path d="M52 140 L44 156"/><path d="M52 140 L70 150 L64 158"/></g></svg>`,
  },
  {
    id: "box-breathing",
    name: "Box Breathing",
    cue:
      "Sit comfortably, close your eyes, and rest your hands on your knees. Breathe in for a slow count of four, hold for four, breathe out for four, and hold empty for four. That's one square. Keep the corners of the square smooth and unhurried; if four feels like a strain, shorten the count. This calm, steady breathing is today's actual training.",
    checkpoints: ["in 4, hold 4, out 4, hold 4", "smooth, unforced corners", "sit tall and relaxed"],
    figure: `<svg viewBox="0 0 150 190" role="img"><title>Box Breathing</title><rect x="98" y="26" width="30" height="30" rx="3" stroke="currentColor" stroke-opacity="0.45" fill="none" stroke-width="3"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><circle cx="70" cy="56" r="11"/><line x1="70" y1="67" x2="70" y2="122"/><path d="M70 78 L52 106 L64 122"/><path d="M70 78 L88 106 L76 122"/><path d="M70 122 L48 138 L92 138"/><path d="M70 122 L92 138 L52 138"/></g></svg>`,
  },

  // ── Sample 10-minute morning mobility routine + rest-day swaps ─────
  {
    id: "cat-cow",
    name: "Cat-Cow",
    cue:
      "Start on hands and knees, hands under shoulders and knees under hips. Breathe out and round your back toward the ceiling, tucking your chin and tailbone; breathe in and let your belly sink as your chest and tailbone lift. Move slowly and let the breath set the pace. This is a spine warm-up, not a stretch to push into.",
    checkpoints: ["hands under shoulders, knees under hips", "round on the out-breath, arch on the in-breath", "slow. Let the breath lead"],
    figure: `<svg viewBox="0 0 150 190" role="img"><title>Cat-Cow</title><line x1="14" y1="158" x2="142" y2="158" stroke="currentColor" stroke-opacity="0.3" stroke-width="4" stroke-linecap="round"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><circle cx="36" cy="104" r="11"/><path d="M46 100 Q76 74 108 100"/><line x1="46" y1="106" x2="44" y2="156"/><line x1="108" y1="100" x2="110" y2="156"/></g></svg>`,
  },
  {
    id: "squat-to-stand",
    name: "Squat to Stand",
    cue:
      "Stand with your feet about shoulder-width apart, bend down and take hold of your toes, then drop your hips into the deepest squat you can while keeping that grip. Lift your chest, then straighten your legs to stand while still holding on, letting the backs of your legs stretch. On the last rep, sit in the bottom for two or three seconds and breathe.",
    checkpoints: ["hold your toes the whole time", "drop the hips, lift the chest", "hold the bottom 2-3 seconds on the last rep"],
    figure: `<svg viewBox="0 0 150 190" role="img"><title>Squat to Stand</title><line x1="22" y1="178" x2="128" y2="178" stroke="currentColor" stroke-opacity="0.3" stroke-width="4" stroke-linecap="round"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><circle cx="66" cy="86" r="11"/><path d="M66 97 L72 126"/><path d="M68 104 L58 140 L58 168"/><path d="M72 126 L94 140 L92 176"/><path d="M72 126 L58 150 L60 176"/></g></svg>`,
  },
  {
    id: "shoulder-dislocate",
    name: "Shoulder Dislocates",
    cue:
      "Hold a towel, band or broomstick with a wide grip in front of your thighs, arms straight. Keeping your elbows locked, sweep it up overhead and as far behind you as it will comfortably go, then back the same way. Go wider on your grip if it pinches, the whole point is to prep your shoulders to press, not to test how narrow you can hold it.",
    checkpoints: ["wide grip, arms straight", "sweep overhead and behind, slowly", "widen the grip if it pinches"],
    figure: `<svg viewBox="0 0 150 190" role="img"><title>Shoulder Dislocates</title><line x1="22" y1="178" x2="128" y2="178" stroke="currentColor" stroke-opacity="0.3" stroke-width="4" stroke-linecap="round"/><line x1="34" y1="30" x2="116" y2="30" stroke="currentColor" stroke-opacity="0.45" stroke-width="4" stroke-linecap="round"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><circle cx="75" cy="58" r="11"/><line x1="75" y1="69" x2="75" y2="122"/><path d="M75 78 L44 32"/><path d="M75 78 L106 32"/><path d="M75 122 L65 150 L65 176"/><path d="M75 122 L85 150 L85 176"/></g></svg>`,
  },
  {
    id: "couch-stretch",
    name: "Couch Stretch",
    cue:
      "Kneel with one shin up against a wall or the front of a couch, back foot pointing up, and plant the other foot flat in front of you. Squeeze the glute on the kneeling side and slowly bring your torso upright until you feel a long stretch down the front of that thigh and hip. Keep your ribs down, breathe, and back off the moment it turns sharp, a minute a side is plenty.",
    checkpoints: ["back shin against the wall or couch", "squeeze the glute, come upright slowly", "ribs down, ease off if it goes sharp"],
    figure: `<svg viewBox="0 0 150 190" role="img"><title>Couch Stretch</title><line x1="118" y1="30" x2="118" y2="168" stroke="currentColor" stroke-opacity="0.4" stroke-width="4" stroke-linecap="round"/><line x1="18" y1="172" x2="128" y2="172" stroke="currentColor" stroke-opacity="0.3" stroke-width="4" stroke-linecap="round"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><circle cx="62" cy="60" r="11"/><line x1="64" y1="71" x2="70" y2="124"/><path d="M70 124 L96 140 L114 118"/><path d="M70 124 L44 140 L44 170"/></g></svg>`,
  },
  {
    id: "pigeon-pose",
    name: "Pigeon",
    cue:
      "From hands and knees, slide one knee forward toward that same wrist and let the shin angle across in front of you, then stretch the other leg straight back. Lower your hips toward the floor, put a cushion under the near hip if it hangs in the air, and let your weight settle. Breathe slowly into the outside of the front hip and come out gently if anything pinches at the knee.",
    checkpoints: ["front shin across, back leg long", "cushion under the hip if it hangs", "settle and breathe. Never force the knee"],
    figure: `<svg viewBox="0 0 150 190" role="img"><title>Pigeon</title><line x1="14" y1="158" x2="142" y2="158" stroke="currentColor" stroke-opacity="0.3" stroke-width="4" stroke-linecap="round"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><circle cx="44" cy="106" r="11"/><line x1="54" y1="112" x2="86" y2="140"/><path d="M86 140 L128 152"/><path d="M60 128 L30 138 L58 146"/></g></svg>`,
  },
  {
    id: "forward-fold",
    name: "Forward Fold + Hang",
    cue:
      "Stand with your feet hip-width apart, soften your knees, and fold forward from the hips letting your head and arms hang heavy. Don't reach for the floor, just let gravity do it, and let your neck go completely loose. Breathe slowly for a minute, then roll up one bone at a time so you don't come up dizzy.",
    checkpoints: ["soft knees, fold from the hips", "let the head and arms hang heavy", "roll up slowly at the end"],
    figure: `<svg viewBox="0 0 150 190" role="img"><title>Forward Fold + Hang</title><line x1="22" y1="178" x2="128" y2="178" stroke="currentColor" stroke-opacity="0.3" stroke-width="4" stroke-linecap="round"/><g fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><circle cx="60" cy="104" r="11"/><path d="M64 93 Q84 74 82 106"/><line x1="60" y1="115" x2="58" y2="146"/><path d="M82 106 L78 140 L78 176"/><path d="M82 106 L88 140 L88 176"/></g></svg>`,
  },
];

export function getStretch(id: string): Stretch | undefined {
  return STRETCH_DB.find((s) => s.id === id);
}
