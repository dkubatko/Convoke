export interface Bot {
  id: number
  tg_bot_id: number
  username: string
  name: string
  can_read_all_group_messages: boolean
  status: string
  last_error: string | null
  last_polled_at: string | null
}

export interface Chat {
  id: number
  bot_id: number
  tg_chat_id: number
  type: string
  title: string
  is_forum: boolean
  status: string
  authorized_by_name: string | null
  authorized_at: string | null
}

export interface MediaStatus {
  pending: number
  described: number
  failed: number
  skipped: number
}

export interface MessageAttachment {
  kind: string
  status: string // pending | described | failed | skipped
  description: string | null
  transcript: string | null
  error: string | null
  duration_s: number | null
}

export interface Message {
  id: number
  tg_message_id: number
  sender_name: string
  text: string
  sent_at: string
  source: string
  reply_to: { sender_name: string; text: string } | null
  attachment: MessageAttachment | null
}

export interface SearchHit {
  chunk_id: number
  // Fused hybrid-retrieval (RRF) score — orders hits within one response.
  score: number
  rendered: string
}

export interface ImportJob {
  id: number
  chat_id: number
  filename: string
  status: string
  detail: string | null
  messages_total: number
  messages_ingested: number
  created_at: string
  finished_at: string | null
}

export interface ToolCall {
  tool: string
  // Display name of the tool's provider: an MCP server's name, or "built-in"
  // for Convoke's own tools. May be absent on runs captured before grouping.
  provider?: string
  args: string
  ok: boolean
}

export interface Run {
  id: number
  trigger: string
  status: string
  request_text: string
  response_text: string | null
  tool_calls: ToolCall[] | null
  error: string | null
  created_at: string
  finished_at: string | null
}

export interface GlobalRun extends Run {
  chat_id: number
  chat_title: string
}

export interface Gap {
  id: number
  gap_start: string
  gap_end: string
}

export interface ConnectedModel {
  id: number
  name: string
  base_url: string
  model_name: string
  has_api_key: boolean
  capabilities: Record<string, boolean>
  last_tested_at: string | null
  last_test_detail: string | null
  assigned_roles: string[]
  updated_at: string
}

export interface RoleAssignment {
  role: string
  model_id: number | null
  model_name: string | null
  required_capability: string
  capability_ok: boolean
}

export interface CapabilityProbe {
  ok: boolean
  detail: string
}

export interface ModelTestResult {
  chat: CapabilityProbe
  vision: CapabilityProbe
  transcription: CapabilityProbe
}

export type EmbedderRole = 'intent' | 'memory'

export interface EmbeddingsInfo {
  role: EmbedderRole
  current: {
    model_id: string
    dim: number
    max_tokens: number
    status: string // ready | reembedding
    phase: string | null
    total: number
    done: number
    error: string | null
    target_model_id: string | null
    started_at: string | null
    finished_at: string | null
  }
  registry: { id: string; label: string; dim: number | null }[]
}

export interface McpServer {
  id: number
  name: string
  transport: string
  url: string | null
  command: string | null
  args: string[]
  has_headers: boolean
  enabled: boolean
  auth_type: string
  oauth_status: string | null
  oauth_error: string | null
}

export interface SlotSpec {
  name: string
  description: string
}

export interface Workflow {
  id: number
  name: string
  type: string
  enabled: boolean
  action_prompt: string
  cron: string | null
  next_fire_at: string | null
  trigger_prompt: string | null
  required_slots: SlotSpec[]
  confirm: boolean
  cooldown_seconds: number // optional rate limit; 0 = off. Parks + rechecks, never drops.
  dedup_window_hours: number // how long a handled topic suppresses continuations
  threshold: number | null
  examples_status: string
  chat_ids: number[]
}

export interface Fire {
  id: number
  workflow_id: number
  chat_id: number
  chat_title: string
  slots: Record<string, { value: string }>
  status: string
  error: string | null
  agent_run_id: number | null
  created_at: string
}

export interface CursorInfo {
  thread_key: number
  last_tg_message_id: number
  last_evaluated_at: string | null
  last_stage: string | null
  last_score: number | null
  last_confidence: number | null
}

export interface EpisodeInfo {
  id: number
  thread_key: number
  status: string // candidate | converged | fired | satisfied | closed
  summary: string | null
  slots: Record<string, { value: string; confidence: number }>
  confidence: number | null
  execution_summary: string | null
  close_reason: string | null
  opened_at: string
  last_activity_at: string
  fired_at: string | null
  closed_at: string | null
}

export interface ChatWorkflowRun {
  id: number
  status: string
  error: string | null
  response_text: string | null
  tool_calls: ToolCall[] | null
  created_at: string
}

export interface ChatWorkflow {
  id: number
  name: string
  type: string
  enabled: boolean
  confirm: boolean
  cooldown_seconds: number
  dedup_window_hours: number
  threshold: number | null
  examples_status: string
  cron: string | null
  next_fire_at: string | null
  trigger_prompt: string | null
  action_prompt: string
  required_slots: SlotSpec[]
  assigned: boolean
  cursors: CursorInfo[]
  episodes: EpisodeInfo[]
  recent_fires: Fire[]
  recent_runs: ChatWorkflowRun[]
  pending_messages: number
}

export interface WorkflowChat {
  chat_id: number
  chat_title: string
  chat_status: string
  cursors: CursorInfo[]
  episodes: EpisodeInfo[]
  pending_messages: number
  recent_fires: Fire[]
  recent_runs: ChatWorkflowRun[]
}

export interface WorkflowDetail extends Workflow {
  chats: WorkflowChat[]
}

export interface ThreadPreviewMsg {
  sender_name: string
  text: string
  sent_at: string
}

export interface ChatThread {
  thread_key: number
  name: string
  title: string | null
  default_name: string
  monitored: boolean
  message_count: number
  last_activity: string | null
  preview: ThreadPreviewMsg[]
}

export interface Member {
  sender_id: number
  handle: string | null
  auto_name: string
  override_name: string | null
  display_name: string
}

export interface AppSetting {
  key: string
  label: string
  description: string
  unit: string
  minimum: number
  maximum: number
  value: number
  default: number
  overridden: boolean
  group: string
  // When present, one label per integer minimum..maximum: render a named
  // N-stop control instead of a numeric input (the value stays hidden).
  step_labels?: string[] | null
}
