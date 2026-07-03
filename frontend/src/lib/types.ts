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

export interface Message {
  id: number
  tg_message_id: number
  sender_name: string
  text: string
  sent_at: string
  source: string
}

export interface SearchHit {
  chunk_id: number
  distance: number
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

export interface Run {
  id: number
  trigger: string
  status: string
  request_text: string
  response_text: string | null
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

export interface Provider {
  role: string
  base_url: string
  model_name: string
  has_api_key: boolean
  updated_at: string
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
  cooldown_seconds: number
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
  created_at: string
}

export interface TriggerStateInfo {
  thread_key: number
  slots: Record<string, { value: string; confidence: number }>
  last_evaluated_at: string | null
  last_stage: string | null
  last_score: number | null
  last_confidence: number | null
  last_match_at: string | null
  cooldown_until: string | null
}

export interface ChatWorkflowRun {
  id: number
  status: string
  error: string | null
  response_text: string | null
  created_at: string
}

export interface ChatWorkflow {
  id: number
  name: string
  type: string
  enabled: boolean
  confirm: boolean
  threshold: number | null
  examples_status: string
  cron: string | null
  next_fire_at: string | null
  required_slots: SlotSpec[]
  assigned: boolean
  states: TriggerStateInfo[]
  recent_fires: Fire[]
  recent_runs: ChatWorkflowRun[]
}
