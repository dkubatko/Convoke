import { SlotSpec, TriggerStateInfo } from './types'

// The intent pipeline a window passes through, and how to explain where the
// last check ended — shared by the chat view and the workflow detail page.

export type Tone = 'ok' | 'warn' | 'err' | 'accent' | 'idle'

export function coolingDown(s: TriggerStateInfo | undefined): boolean {
  return !!s?.cooldown_until && new Date(s.cooldown_until) > new Date()
}

/** Top-level status of an intent workflow in a chat: what is it doing now. */
export function statusChip(
  s: TriggerStateInfo | undefined,
  examplesStatus?: string,
): { label: string; tone: Tone } {
  if (coolingDown(s)) return { label: 'cooling down', tone: 'warn' }
  switch (s?.last_stage) {
    case 'fired':
      return { label: 'just fired', tone: 'ok' }
    case 'accumulating':
      return { label: 'gathering details', tone: 'accent' }
    case 'no_match':
    case 'prefilter_skip':
      return { label: 'listening', tone: 'idle' }
    case 'throttled':
      return { label: 'checking', tone: 'idle' }
    case 'classifier_error':
      return { label: 'model unreachable', tone: 'err' }
    default:
      return examplesStatus === 'pending'
        ? { label: 'calibrating', tone: 'idle' }
        : { label: 'no activity yet', tone: 'idle' }
  }
}

export interface FunnelStep {
  name: string
  status: 'pass' | 'fail' | 'wait' | 'skip'
  detail?: string
}

/** The three gates of the last evaluation and how far it got. */
export function funnel(
  s: TriggerStateInfo | undefined,
  threshold: number | null,
  requiredSlots: SlotSpec[],
): FunnelStep[] {
  const stage = s?.last_stage ?? null
  const score = s?.last_score ?? null
  const conf = s?.last_confidence ?? null
  const th = threshold ?? 0.8
  const reachedClassifier = stage === 'no_match' || stage === 'accumulating' || stage === 'fired'

  const prefilter: FunnelStep =
    score == null
      ? { name: 'Prefilter', status: 'skip', detail: 'gathering details' }
      : {
          name: 'Prefilter',
          status: score >= th ? 'pass' : 'fail',
          detail: `${score.toFixed(2)} / ${th.toFixed(2)}`,
        }

  const classifier: FunnelStep = reachedClassifier
    ? {
        name: 'Classifier',
        status: stage === 'no_match' ? 'fail' : 'pass',
        detail: conf != null ? `conf ${conf.toFixed(2)}` : undefined,
      }
    : { name: 'Classifier', status: 'skip' }

  const gathered = Object.keys(s?.slots ?? {}).length
  const need = requiredSlots.length
  const convergence: FunnelStep =
    stage === 'fired'
      ? { name: 'Fire', status: 'pass' }
      : stage === 'accumulating'
        ? { name: 'Fire', status: 'wait', detail: need ? `${gathered}/${need} details` : 'any match' }
        : { name: 'Fire', status: 'skip' }

  return [prefilter, classifier, convergence]
}

/** One plain-English sentence: where the last check ended and why. */
export function stageStory(
  s: TriggerStateInfo | undefined,
  threshold: number | null,
  examplesStatus?: string,
): string {
  const stage = s?.last_stage ?? null
  const score = s?.last_score
  const conf = s?.last_confidence
  const th = threshold ?? 0.8
  const scoreStr = score != null ? score.toFixed(2) : '—'
  switch (stage) {
    case 'cooldown':
      return score != null && score >= th
        ? `Recently fired — a new match (${scoreStr}) is queued and will fire when the cooldown ends.`
        : `Recently fired — cooling down before it can trigger again.`
    case 'prefilter_skip':
      return `The conversation didn't resemble the trigger (match ${scoreStr}, needs ${th.toFixed(2)}).`
    case 'throttled':
      return `Rate-limited between model checks — it will re-check within a couple of minutes.`
    case 'classifier_error':
      return `The intent model was unreachable — it will retry automatically.`
    case 'no_match':
      return `Looked similar (match ${scoreStr}) but the classifier ruled it out${
        conf != null ? ` (confidence ${conf.toFixed(2)})` : ''
      }.`
    case 'accumulating':
      return `Match confirmed — gathering the remaining details before it fires.`
    case 'fired':
      return `Converged and fired.`
    default:
      return examplesStatus === 'pending'
        ? `Generating the detector's example phrases…`
        : `Watching — no conversation window has been evaluated yet.`
  }
}
