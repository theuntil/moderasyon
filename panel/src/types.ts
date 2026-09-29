export type Role = "owner" | "admin" | "moderator" | "viewer";
export type Decision = "allow" | "review" | "block";
export type RangeKey = "24h" | "7d" | "30d" | "90d";

export interface Me {
  id: string;
  username: string;
  email: string | null;
  name: string | null;
  role: Role;
  must_change_password: boolean;
  totp_enabled?: boolean;
  recovery_codes_left?: number;
}

export interface Evidence {
  id: string;
  kind: "image" | "frame";
  timestamp_ms: number | null;
  width: number | null;
  height: number | null;
  categories: Category[];
}

export interface BlocklistItem {
  id: string;
  project_id: string | null;
  project_name: string | null;
  has_sha: boolean;
  hash_count: number;
  reason: string | null;
  source_public_id: string | null;
  has_preview: boolean;
  created_by: string | null;
  created_at: string;
}

export interface WebhookInfo {
  webhook_url: string | null;
  webhook_enabled: boolean;
  has_secret: boolean;
  stats_7d: { delivered: number; failed: number; pending: number };
  deliveries: {
    id: string; event: string; request_public_id: string | null; status: "pending" | "delivered" | "failed";
    attempts: number; last_status_code: number | null; last_error: string | null; next_attempt_at: string;
    delivered_at: string | null; created_at: string;
  }[];
}

export interface Project {
  id: string;
  name: string;
  slug: string;
  description: string | null;
  status: "active" | "disabled";
  created_at: string;
  updated_at: string;
  rate_limit_per_second: number | null;
  rate_limit_per_minute: number | null;
  active_keys: number;
  pending_reviews: number;
  requests_30d: number;
  block_30d: number;
  review_30d: number;
  latency_30d: number | null;
  last_request_at: string | null;
  webhook_url: string | null;
  webhook_enabled: boolean;
}

export interface ApiKey {
  id: string;
  project_id: string;
  name: string;
  key_prefix: string;
  environment: "live" | "test";
  status: "active" | "disabled" | "revoked";
  created_at: string;
  last_used_at: string | null;
  expires_at: string | null;
  revoked_at: string | null;
  created_by_email: string | null;
  recent_ips: { ip: string; requests: number; last_seen: string }[];
}

export interface Overview {
  range: RangeKey;
  bucket: "hour" | "day";
  timezone: string;
  totals: {
    requests: number; allow: number; review: number; block: number; failed: number;
    in_progress: number; text: number; image: number; video: number;
  };
  latency: { avg: number | null; p50: number | null; p95: number | null; p99: number | null };
  series: { t: string; total: number; allow: number; review: number; block: number; failed: number; latency: number | null }[];
  categories: { name: string; count: number }[];
  projects: { id: string; name: string; slug: string; requests: number; block: number; review: number }[];
  live: { last_minute: number; today: number };
  pending_reviews: number;
  reviews_resolved: number;
}

export interface Category { name: string; score: number }

export interface ReviewItem {
  id: string;
  status: "pending" | "resolved";
  ai_decision: Decision;
  human_decision: "allow" | "block" | null;
  created_at: string;
  reviewed_at: string | null;
  reviewed_by_email: string | null;
  public_id: string;
  content_type: string;
  content_text: string | null;
  external_user_id: string | null;
  external_content_id: string | null;
  project_id: string;
  project_name: string;
  categories: Category[] | null;
  max_score: number | null;
  reason: string | null;
  policy_version: string | null;
  media_duration_ms: number | null;
  media_width: number | null;
  media_height: number | null;
  media_mime: string | null;
  evidence: Evidence[];
}

export interface DecisionRow {
  public_id: string;
  status: string;
  content_type: string;
  created_at: string;
  completed_at: string | null;
  external_user_id: string | null;
  external_content_id: string | null;
  client_ip: string | null;
  preview: string | null;
  project_id: string;
  project_name: string;
  ai_decision: Decision | null;
  human_decision: "allow" | "block" | null;
  final_decision: Decision | null;
  max_score: number | null;
  categories: Category[] | null;
  latency_ms: number | null;
  media_duration_ms: number | null;
  labels: string[] | null;
  severity: "normal" | "critical" | null;
  legal_hold: boolean;
}

export interface DecisionDetail extends DecisionRow {
  content_text: string | null;
  content_url: string | null;
  metadata: Record<string, unknown>;
  idempotency_key: string | null;
  attempts: number;
  error: string | null;
  started_at: string | null;
  api_key_name: string;
  key_prefix: string;
  reason: string | null;
  providers: { provider: string; model: string; version: string }[] | null;
  policy_version: string | null;
  processing_time_ms: number | null;
  review_id: string | null;
  review_status: string | null;
  reviewed_at: string | null;
  reviewed_by_email: string | null;
  source: "json" | "upload" | "url" | null;
  media_mime: string | null;
  media_bytes: number | null;
  media_width: number | null;
  media_height: number | null;
  media_frames: number | null;
  content_purged_at: string | null;
  evidence: Evidence[];
  layer1_decision: Decision | null;
  ai_used: boolean | null;
  ai_latency_ms: number | null;
  labels: string[] | null;
  severity: "normal" | "critical" | null;
  legal_hold: boolean;
  held_at: string | null;
  has_original: boolean;
  user_info: { id?: string; name?: string; surname?: string; username?: string; email?: string; phone?: string; extra?: Record<string, unknown> } | null;
  evidence_restricted: boolean;
}

export interface IpRule {
  id: string;
  project_id: string | null;
  project_name: string | null;
  cidr: string;
  reason: string | null;
  created_at: string;
  expires_at: string | null;
  created_by_email: string | null;
  expired: boolean;
  requests_7d: number;
  source: "manual" | "auto";
}

export interface PlatformSettings {
  service_enabled: boolean;
  maintenance_message: string | null;
  threshold_review: number;
  threshold_block: number;
  policy_version: number;
  default_rate_limit_per_second: number;
  default_rate_limit_per_minute: number;
  retention_days: number;
  autoban_mode: "off" | "monitor" | "enforce";
  autoban_auth_fail_limit: number;
  autoban_scan_limit: number;
  autoban_flood_limit: number;
  autoban_panel_login_limit: number;
  autoban_allowlist: string[];
  evidence_retention_hours: number;
  legal_hold_enabled: boolean;
  ai_sensitive_media: boolean;
  visual_rules_enabled: boolean;
  updated_at: string;
  updated_by_email: string | null;
  policy_versions: { version: number; threshold_review: number; threshold_block: number; created_at: string; created_by_email: string | null }[];
}

export interface AdminRow {
  id: string;
  username: string;
  email: string | null;
  name: string | null;
  role: Role;
  status: "active" | "disabled";
  must_change_password: boolean;
  last_login_at: string | null;
  last_login_ip: string | null;
  created_at: string;
  locked: boolean | null;
  active_sessions: number;
  totp_enabled: boolean;
}

export interface AuditRow {
  id: number;
  actor_email: string | null;
  action: string;
  project_id: string | null;
  project_name: string | null;
  target_type: string | null;
  target_id: string | null;
  details: Record<string, unknown>;
  ip: string | null;
  created_at: string;
}

export interface SystemHealth {
  checked_at: string;
  database: { ok: boolean; latency_ms?: number; version?: string; size?: string; error?: string };
  redis: { ok: boolean; latency_ms?: number; memory?: string; error?: string };
  queue?: { depth?: number; media_depth?: number; stuck?: number; in_progress?: number; failed_24h?: number };
  worker?: { ok: boolean; heartbeat: string | null };
  media_worker?: { ok: boolean; heartbeat: string | null };
  service?: { service_enabled: boolean; policy_version: number; retention_days: number };
  webhooks?: { pending: number; failed_24h: number };
  storage?: {
    ok: boolean; configured: boolean; bucket?: string; latency_ms?: number; error?: string;
    evidence_files: number; evidence_pending_review: number; incoming_files: number;
  } | null;
}
