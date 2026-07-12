import { CursorInfo, EpisodeInfo, SlotSpec } from './types'

// The episode pipeline and how to explain it — shared by the chat view and
// the workflow detail page. A workflow's live state in a chat is now a set of
// EPISODES (tracked occurrences of the intent, each with a lifecycle) plus a
// per-thread CURSOR recording where the detector's last check ended.

export type Tone = 'ok' | 'warn' | 'err' | 'accent' | 'idle'

const OPEN = ['candidate', 'converged', 'fired', 'satisfied']

/** Backend default for intent_min_fire_confidence_pct — only used when the
    API response predates the field (deploy skew). */
export const DEFAULT_FIRE_BAR = 0.7

export function openEpisodes(episodes: EpisodeInfo[]): EpisodeInfo[] {
  return episodes.filter((e) => OPEN.includes(e.status))
}

/** Badge for one episode's lifecycle state. A candidate's presentation, like
    its behavior, derives from gathered substance. */
export function episodeBadge(e: EpisodeInfo): { label: string; tone: Tone; live?: boolean } {
  switch (e.status) {
    case 'candidate':
      return Object.keys(e.slots ?? {}).length > 0
        ? { label: 'following', tone: 'accent', live: true }
        : { label: 'possible topic', tone: 'idle' }
    case 'converged':
      return { label: 'ready — rate-limited', tone: 'warn', live: true }
    case 'fired':
      return { label: 'acting', tone: 'accent', live: true }
    case 'satisfied':
      return { label: 'handled', tone: 'ok' }
    default:
      switch (e.close_reason) {
        case 'duplicate':
          return { label: 'duplicate — skipped', tone: 'idle' }
        case 'abandoned':
          return { label: 'dropped by the group', tone: 'idle' }
        case 'stale':
          return { label: 'went stale — not acted on', tone: 'idle' }
        case 'done':
          return { label: 'done', tone: 'idle' }
        case 'superseded':
          return { label: 'superseded', tone: 'idle' }
        default:
          return { label: 'expired', tone: 'idle' }
      }
  }
}

const LIVE_STAGES = ['evaluating', 'evaluating_prefilter', 'rechecking']

export function cursorBusy(c: CursorInfo | undefined): boolean {
  return !!c && LIVE_STAGES.includes(c.last_stage ?? '')
}

/** Top-level status of an intent workflow in a chat: what is it doing now. */
export function statusChip(
  cursors: CursorInfo[],
  episodes: EpisodeInfo[],
  examplesStatus?: string,
): { label: string; tone: Tone; live?: boolean } {
  if (cursors.some(cursorBusy)) return { label: 'checking now', tone: 'accent', live: true }
  const open = openEpisodes(episodes)
  if (open.some((e) => e.status === 'fired')) return { label: 'acting now', tone: 'accent', live: true }
  if (open.some((e) => e.status === 'converged'))
    return { label: 'ready — waiting out rate limit', tone: 'warn', live: true }
  if (open.some((e) => e.status === 'candidate' && Object.keys(e.slots ?? {}).length > 0))
    return { label: 'following a topic', tone: 'accent', live: true }
  if (open.some((e) => e.status === 'candidate'))
    return { label: 'watching a possible topic', tone: 'accent' }
  if (open.some((e) => e.status === 'satisfied'))
    return { label: 'handled — watching for follow-ups', tone: 'ok' }
  // A forum chat has one cursor per thread — surface an error in ANY thread,
  // otherwise fall back to wherever the detector last actually looked.
  const cursor =
    cursors.find((c) => c.last_stage === 'classifier_error') ??
    cursors.reduce<CursorInfo | undefined>(
      (latest, c) =>
        c.last_evaluated_at &&
        (!latest?.last_evaluated_at || c.last_evaluated_at > latest.last_evaluated_at)
          ? c
          : latest,
      undefined,
    ) ??
    cursors[0]
  const stage = cursor?.last_stage
  switch (stage) {
    case 'classifier_error':
      return { label: 'model error', tone: 'err' }
    case 'throttled':
      return { label: 'checking', tone: 'idle' }
    case 'no_match':
    case 'prefilter_skip':
    case 'suppressed':
    case 'concluded':
    case 'duplicate':
    case 'stale':
    case 'fired':
      return { label: 'listening', tone: 'idle' }
    default:
      return examplesStatus === 'pending'
        ? { label: 'calibrating', tone: 'idle' }
        : { label: 'no activity yet', tone: 'idle' }
  }
}

export interface FunnelStep {
  name: string
  status: 'pass' | 'fail' | 'wait' | 'skip' | 'stop' | 'held'
  detail?: string
}

/** The live pipeline graph: Waiting (if messages are queued) → Prefilter →
    Classifier → Fire. Once a topic is open the prefilter is bypassed for later
    bursts (sticky → straight to the classifier); but the window that first
    scored and opened the topic still shows its real prefilter score, not
    "bypassed". */
export function funnel(
  cursor: CursorInfo | undefined,
  episodes: EpisodeInfo[],
  threshold: number | null,
  requiredSlots: SlotSpec[],
  opts?: { pending?: boolean; awaitingConfirm?: boolean; minFireConfidence?: number },
): FunnelStep[] {
  const stage = cursor?.last_stage ?? null
  const score = cursor?.last_score ?? null
  const conf = cursor?.last_confidence ?? null
  const th = threshold ?? 0.8
  const inFlight = LIVE_STAGES.includes(stage ?? '')
  const open = openEpisodes(episodes).filter((e) => e.thread_key === (cursor?.thread_key ?? 0))
  // Only ACTIVE episodes bypass the prefilter; a handled topic is dedup
  // memory behind the embedding gate.
  const sticky = open.some((e) => e.status !== 'satisfied')
  const reachedClassifier = [
    'no_match', 'candidate', 'accumulating', 'suppressed', 'concluded',
    'duplicate', 'parked', 'fired', 'cap_full', 'stale',
  ].includes(stage ?? '')

  if (opts?.pending && !inFlight) {
    return [
      { name: 'Waiting', status: 'wait', detail: 'new messages' },
      { name: 'Prefilter', status: 'skip' },
      { name: 'Classifier', status: 'skip' },
      { name: 'Fire', status: 'skip' },
    ]
  }

  // A non-null score means the prefilter actually scored THIS window, so show
  // it even when an episode is open — the window that OPENS a topic ran the gate
  // and then created the episode, and would otherwise be mislabelled "bypassed".
  // The backend clears last_score to null whenever the gate is truly skipped
  // (sticky bypass, or no positive vectors), so a non-null score is unambiguous.
  const prefilter: FunnelStep =
    stage === 'evaluating_prefilter'
      ? { name: 'Prefilter', status: 'wait', detail: 'running…' }
      : score != null
        ? {
            name: 'Prefilter',
            status: score >= th ? 'pass' : 'fail',
            detail: `${score.toFixed(2)} / ${th.toFixed(2)}`,
          }
        : sticky
          ? { name: 'Prefilter', status: 'pass', detail: 'topic open — bypassed' }
          : { name: 'Prefilter', status: 'skip' }

  // Suppression happens AT the classifier — color it there (amber), so a
  // blocked run reads "the model held this", not "the fire step failed".
  const classifier: FunnelStep =
    stage === 'evaluating' || stage === 'rechecking'
      ? { name: 'Classifier', status: 'wait', detail: 'running…' }
      : stage === 'classifier_error'
        ? { name: 'Classifier', status: 'fail', detail: 'model error' }
        : stage === 'suppressed'
          ? { name: 'Classifier', status: 'stop', detail: 'continues a handled topic' }
          : stage === 'concluded'
            ? { name: 'Classifier', status: 'stop', detail: 'group dropped it' }
            : reachedClassifier
              ? {
                  name: 'Classifier',
                  status: stage === 'no_match' ? 'fail' : 'pass',
                  detail: conf != null ? `conf ${conf.toFixed(2)}` : undefined,
                }
              : { name: 'Classifier', status: 'skip' }

  const active = open.find((e) => e.status === 'candidate')
  // Count REQUIRED names present — never raw keys, so a stray slot the model
  // invented can't display as "1/1" while convergence still waits. Only
  // slots clearing the fire bar count: a probable (sub-bar) detail showing
  // as gathered here is exactly the "3/3 but not firing" confusion.
  const fireBar = opts?.minFireConfidence ?? DEFAULT_FIRE_BAR
  const gathered = requiredSlots.filter(
    (r) => (active?.slots?.[r.name]?.confidence ?? 0) >= fireBar,
  ).length
  const probable = requiredSlots.filter(
    (r) =>
      r.name in (active?.slots ?? {}) &&
      (active?.slots?.[r.name]?.confidence ?? 0) < fireBar,
  ).length
  const need = requiredSlots.length
  // Fire-stage blocks (fingerprint duplicate, stale recheck) are amber at
  // Fire; classifier-level blocks leave Fire dashed 'held' — nothing
  // happened here, the stop was upstream.
  const fire: FunnelStep = opts?.awaitingConfirm
    ? { name: 'Fire', status: 'wait', detail: 'awaiting confirm' }
    : stage === 'fired'
      ? { name: 'Fire', status: 'pass' }
      : stage === 'parked'
        ? { name: 'Fire', status: 'wait', detail: 'rate limit' }
        : stage === 'duplicate'
          ? { name: 'Fire', status: 'stop', detail: 'duplicate — skipped' }
          : stage === 'stale'
            ? { name: 'Fire', status: 'stop', detail: 'went stale' }
            : stage === 'cap_full'
              ? { name: 'Fire', status: 'stop', detail: 'topic cap reached' }
              : stage === 'suppressed' || stage === 'concluded'
                ? { name: 'Fire', status: 'held', detail: 'not acted' }
                : stage === 'candidate' || stage === 'accumulating'
                  ? {
                      name: 'Fire',
                      status: 'wait',
                      detail: need
                        ? `${gathered}/${need} details${probable ? ` · ${probable} probable` : ''}`
                        : 'any match',
                    }
                  : { name: 'Fire', status: 'skip' }

  return [prefilter, classifier, fire]
}

/** One plain-English sentence: where the last check ended and why. */
export function stageStory(
  cursor: CursorInfo | undefined,
  episodes: EpisodeInfo[],
  threshold: number | null,
  examplesStatus?: string,
): string {
  const stage = cursor?.last_stage ?? null
  const score = cursor?.last_score
  const conf = cursor?.last_confidence
  const th = threshold ?? 0.8
  const scoreStr = score != null ? score.toFixed(2) : '—'
  const open = openEpisodes(episodes)
  switch (stage) {
    case 'evaluating_prefilter':
      return `Scoring the latest messages against the trigger…`
    case 'evaluating':
      return `Running the classifier model on the latest messages…`
    case 'rechecking':
      return `A topic waited out the rate limit — checking it's still wanted before acting…`
    case 'prefilter_skip':
      return `The conversation didn't resemble the trigger (match ${scoreStr}, needs ${th.toFixed(2)}).`
    case 'throttled':
      return `Rate-limited between model checks — it will re-check shortly.`
    case 'classifier_error':
      return `The intent model didn't return a usable answer (unreachable, or its reply didn't fit the expected format) — it will retry automatically.`
    case 'no_match':
      return open.length
        ? `The latest messages weren't about the tracked topic${conf != null ? ` (confidence ${conf.toFixed(2)})` : ''}.`
        : `Looked similar (match ${scoreStr}) but the classifier ruled it out${conf != null ? ` (confidence ${conf.toFixed(2)})` : ''}.`
    case 'candidate':
      return `Possibly on-topic but not clear yet — following tentatively; every new burst is now checked.`
    case 'accumulating':
      return `Following the topic — gathering the remaining details before it acts.`
    case 'suppressed':
      return `The group is still talking about a topic that was already handled — not acting twice.`
    case 'concluded':
      return `The group dropped the topic themselves — closed without acting.`
    case 'duplicate':
      return `Converged on the exact same details as a recent occurrence — skipped as a duplicate.`
    case 'parked':
      return `Ready to act, but the rate limit is active — it will recheck and act when the limit lifts.`
    case 'stale':
      return `The rate limit lifted, but the conversation had moved on — closed without acting.`
    case 'cap_full':
      return `A second topic appeared, but the tracker follows one at a time — it was not tracked.`
    case 'fired':
      return open.some((e) => e.status === 'fired')
        ? `Converged — the agent is acting on it now.`
        : `Acted earlier — now watching for the next occurrence.`
    default:
      return examplesStatus === 'pending'
        ? `Generating the detector's example phrases…`
        : `Watching — no conversation window has been evaluated yet.`
  }
}

export function cooldownLabel(seconds: number): string {
  if (!seconds) return 'none'
  if (seconds % 3600 === 0) return `${seconds / 3600} h`
  if (seconds % 60 === 0) return `${seconds / 60} min`
  return `${seconds} s`
}

/** How the workflow avoids acting twice: episode dedup, in plain words. */
export function dedupLabel(wf: {
  cooldown_seconds: number
  dedup_window_hours: number
}): string {
  const base = `each occurrence is tracked as a topic and acted on once; follow-ups are recognized for ${wf.dedup_window_hours} h`
  return wf.cooldown_seconds > 0
    ? `${base}; acts at most once per ${cooldownLabel(wf.cooldown_seconds)} (extra topics wait, then recheck)`
    : base
}
