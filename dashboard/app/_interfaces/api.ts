/** One decision recorded against a door by the egress ledger. Content-free by
 *  construction on the backend: a result, a reason, and when. */
export interface EgressEvent {
  ts: string;
  epoch?: number;
  result?: 'allowed' | 'refused' | string;
  detail?: string;
}

/** One row of `egress_doors.door_status()`, published verbatim by
 *  /system/status. `enforced:false` means nothing in the registry stops this
 *  call — the door is listed anyway, and `enforcement_note` says so in words. */
export interface EgressDoor {
  id: string;
  open: boolean;
  reason?: string;
  /** Which tier decides this door's value right now, in precedence order:
   *  'env' (PUFFPAI_EGRESS_* in the systemd unit — the operator, and the only
   *  tier the running service cannot write), 'overlay' (the owner's own
   *  switches, the tier POST /system/egress/door writes), 'config' (shipped /
   *  provisioned), 'default' (never set; always shut). Absent on appliances
   *  older than the owner overlay. */
  source?: 'env' | 'overlay' | 'config' | 'default' | string;
  /** Whether the toggle route will accept a write for this door. The backend
   *  asserts this agrees with what the route actually does, door by door, so
   *  the UI can trust it instead of keeping its own list. Absent on older
   *  appliances — treat undefined as "try it and let the 409 answer", never
   *  as false. */
  writable?: boolean;
  enforced: boolean;
  enforcement?: string;
  enforcement_note?: string;
  hosts: string[];
  purpose: string;
  data_sent?: string;
  third_party?: string | null;
  consent_kind?: string;
  config_flag?: string;
  env_var?: string;
  default?: boolean;
  last_used?: EgressEvent | null;
  last_refused?: EgressEvent | null;
}

/** One record from GET /system/egress/ledger.
 *
 *  The backend returns an ALLOWLIST projection (egress_ledger.PUBLIC_FIELDS):
 *  there is deliberately no `host`, `path`, `url`, `query` or `caller` on a
 *  record, so the owner-facing list cannot leak a search term, a document name
 *  or a visited address. Do not add those fields here speculatively — if the
 *  card ever needs one it has to be added to PUBLIC_FIELDS on the backend
 *  first, on purpose. */
export interface EgressLedgerRecord {
  ts: string;
  door: string;
  /** 'allowed' | 'refused' for a request; 'opened' | 'closed' for a toggle. */
  result?: string;
  kind?: string;
  /** 'door_toggled' marks an owner action rather than an outbound request. */
  event?: string;
  open?: boolean;
  /** Who acted. 'owner-api' for a toggle — one shared appliance credential,
   *  so the backend does NOT claim to know which person flipped the switch. */
  by?: string;
  purpose?: string;
  third_party?: string | null;
  bytes_out?: number | null;
  bytes_in?: number | null;
  status?: number | null;
}

export interface SystemStatus {
  knowledge: { status: string; library_count: number; chunk_count: number };
  voice: { status: string; wake_word: string };
  firewall: { status: string; blocked_count: number; rules_count: number };
  vault: { status: string; item_count: number };
  inference: { status: string; model: string; backend: string };
  /** Absent on appliances older than the egress-door registry. */
  egress_doors?: EgressDoor[];
  uptime_seconds: number;
}

export interface ChatSource {
  library: string;
  collection: string;
  type: string;       // 'video' | 'encyclopedia' | 'qa' | 'medical' | 'survival' | 'reference' | etc.
  title: string;
  url: string;
  relevance: number;  // 0–1
}

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  timestamp: string;
  sources?: ChatSource[];
  suggestions?: string[];
}

export interface Conversation {
  id: string;
  messages: ChatMessage[];
  created_at: string;
  title?: string;
}

/** A zone identifier. Legacy ChromaDB rows use the numbers 1 (vault) and 2
 *  (general); ZIM-backed rows report the string "knowledge". Treat anything
 *  else as an unlabelled general zone rather than crashing. */
export type KnowledgeZone = 1 | 2 | string | number;

export interface KnowledgeLibrary {
  name: string;
  /** Human label: what the sender typed on /send, or Puff's derived one.
   *  Absent on ZIM packs and on anything stored before labels existed — render sites
   *  fall back to `name`. */
  title?: string;
  collection: string;
  zone: KnowledgeZone;
  zone_label?: string;
  /** Absent on ZIM rows that were never ingested into ChromaDB. */
  chunk_count?: number;
  /** Bytes (number) on ZIM rows, pre-formatted string on legacy rows. */
  size?: string | number;
  added?: string;
  library_id?: string;
  content_type?: 'encyclopedia' | 'video' | 'qa' | 'medical' | 'reference' | 'survival' | 'science' | 'travel' | 'books' | 'uploads';
  // User-upload / source-material metadata (populated by /knowledge/libraries).
  uploaded_at?: string;
  is_upload?: boolean;
  source_available?: boolean;
  source_content_type?: string;
  /** "zim" for packs discovered on disk, "chromadb" for collections/uploads. */
  source?: 'zim' | 'chromadb' | string;
  /** True when a SQLite FTS5 shard exists, i.e. full-text search works. */
  fts_ready?: boolean;
  zim_filename?: string;
}

export interface LibraryArticle {
  index: number;
  title: string;
  url: string;
  mime_type: string;
}

export interface ArticleContent {
  title: string;
  url: string;
  mime_type: string;
  html: string;
}

export interface VaultItem {
  key: string;
  // Vaulted raw documents have no explicit type; auto-detected PII log
  // and manually-stored items will set this.
  type?: string;
  // API returns a byte count (number), not a pre-formatted string.
  size: number;
  // API uses 'modified' (ISO-8601). Older copies may still send
  // 'last_modified' — keep both optional to avoid breakage.
  modified?: string;
  last_modified?: string;
  // Extras returned for auto_detected_pii.json
  record_count?: number;
  pii_types?: string[];
}

export interface FirewallRule {
  id: string;
  name: string;
  direction: 'inbound' | 'outbound' | 'both';
  action: 'allow' | 'block';
  protocol: string;
  port: string;
  status: 'active' | 'inactive';
}

export interface NetworkDevice {
  name: string;
  ip: string;
  mac: string;
  device_class: string;
  trust: 'trusted' | 'classified' | 'unknown' | 'blocked';
  blocked?: boolean;
  zone3_managed?: boolean;
  zone3_mode?: 'lockdown' | 'balanced' | 'open' | 'always_allow' | null;
}

export interface UserProfile {
  name: string;
  email: string;
  avatar?: string;
}

export interface AISettings {
  ai_name: string;
  avatar_style: string;
  /** Which avatar engine to render: 'puff' (SVG), 'human' (TalkingHead 3D), or 'glb' (custom model) */
  avatar_character?: string;
  /** Per-user model URL for the 3D engines (Ready Player Me / GLB). */
  avatar_3d_url?: string;
  personality: string;
  temperature: number;
  context_length: number;
  model: string;
  backend: string;
  embedding_model: string;
}

export interface AppData {
  items: any[];
  last_updated: string;
}
