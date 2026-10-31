import type { EngineInterface, Register, Timer } from 'claude-code'

import type { IdleGuardView } from '../types'

const PANE = 'idle-guard'
const MIN = 60_000
// The built-in idle compaction fires at exactly 54 min in interactive sessions;
// waiting 30 s more lets it go first, after which the context is small and we skip.
const OFFSET_MS = 30_000
// Past the 1 h prompt-cache TTL the cache is gone anyway: compacting buys nothing.
const LAPSE_MS = 60 * MIN
const PREFIX = 'idle-guard  compact'
const view = { plugin: 'idle-guard', key: 'view' } as const
const IDLE_VIEW: IdleGuardView = { enabled: true, detail: '', at: 0, armedAt: null, last: '' }
const BAR_CELLS = 19

// UI strings by language. zh-TW is the original text; any other language value is English.
const TEXT = {
  en: {
    command: 'Toggle idle compaction (Idle Guard)',
    countdown: (n: number) => `compacts in ${n} min`,
    lapsed: 'Not compacted: idle over 60 min',
    belowMin: (min: string) => `Not compacted: context under ${min}`,
    skipped: 'Not compacted: skipped',
    done: (at: string) => `Compacted at ${at}`,
    doneSized: (at: string, before: string, after: string) => `Compacted at ${at} (${before}→${after})`,
    lastDone: (at: string) => `${at} compacted`,
    lastDoneSized: (at: string, before: string, after: string) => `${at} compacted ${before} → ${after}`,
    ctxOff: 'off, will not compact',
    ctxWill: 'will compact when idle',
    ctxWont: 'will not compact',
    on: '● ON',
    off: '○ OFF',
    hintToggle: 'c toggle',
    hintClose: 'Esc close',
    idleLabel: 'Idle  ',
    running: 'Turn running, timer off',
    elapsed: (n: number, total: number) => `${n} / ${total} min`,
    lastLabel: 'Last  ',
  },
  'zh-TW': {
    command: '閒置 compact 開關（Idle Guard）',
    countdown: (n: number) => `${n} 分後壓縮`,
    lapsed: '未壓縮：閒置已超過 60 分',
    belowMin: (min: string) => `未壓縮：context 不到 ${min}`,
    skipped: '未壓縮：已略過',
    done: (at: string) => `已於 ${at} 壓縮`,
    doneSized: (at: string, before: string, after: string) => `已於 ${at} 壓縮（${before}→${after}）`,
    lastDone: (at: string) => `${at} 壓縮`,
    lastDoneSized: (at: string, before: string, after: string) => `${at} 壓縮 ${before} → ${after}`,
    ctxOff: '已關閉，不會壓縮',
    ctxWill: '時間到會壓縮',
    ctxWont: '不會壓縮',
    on: '● 開啟',
    off: '○ 關閉',
    hintToggle: 'c 切換',
    hintClose: 'Esc 關閉',
    idleLabel: '閒置  ',
    running: '對話進行中，未計時',
    elapsed: (n: number, total: number) => `${n} / ${total} 分`,
    lastLabel: '上次  ',
  },
}

// Settings from userConfig, fixed at register().
let minutes = 54
let minTokens = 200_000
let t: (typeof TEXT)['en'] = TEXT.en

// Module state: a hot reload drops it together with the timers, so nothing is
// re-armed after a reload; the next main-loop turn.complete arms again.
let lastActivityAt: number | undefined
let deadline: number | undefined
let armedAt: number | undefined
let result = ''
// The last compaction or skip, kept across turns for the pane.
let last = ''
let timer: Timer | undefined
let ticker: Timer | undefined

const kilo = (n: number) => `${Math.round(n / 1000)}K`
const pad = (n: number) => String(n).padStart(2, '0')
const clockTime = (ms: number) => {
  const d = new Date(ms)
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`
}
// Filled cells of the idle bar: elapsed / minutes of the width, rounded to the nearest cell.
const barOf = (elapsed: number, total: number) => {
  const filled = Math.min(BAR_CELLS, Math.max(0, Math.round((elapsed / total) * BAR_CELLS)))
  return '█'.repeat(filled) + '░'.repeat(BAR_CELLS - filled)
}
const statusLine = ({ enabled, detail }: IdleGuardView) =>
  !enabled ? `${PREFIX} ○ OFF` : detail ? `${PREFIX} ● ON · ${detail}` : `${PREFIX} ● ON`

function disarm() {
  timer?.cancel()
  ticker?.cancel()
  timer = undefined
  ticker = undefined
  deadline = undefined
}

async function loadEnabled($: EngineInterface) {
  return (await $.store.get('enabled')) !== false
}

async function show($: EngineInterface) {
  const now = await $.clock.now()
  const detail =
    deadline !== undefined ? t.countdown(Math.max(1, Math.floor((deadline - now) / MIN))) : result
  const next: IdleGuardView = {
    enabled: await loadEnabled($),
    detail,
    at: now,
    armedAt: armedAt ?? null,
    last,
  }
  $.ui.status(statusLine(next))
  await $.state.set(view, next)
}

async function fire($: EngineInterface) {
  disarm()
  const now = await $.clock.now()
  if (!(await loadEnabled($))) {
    result = ''
  } else if (lastActivityAt === undefined || now - lastActivityAt > LAPSE_MS) {
    result = t.lapsed
    last = `${clockTime(now)} ${result}`
  } else {
    const tokens = (await $.session.usage()).context.tokens ?? 0
    if (tokens < minTokens) {
      result = t.belowMin(kilo(minTokens))
      last = `${clockTime(now)} ${result}`
    } else {
      // Rejects while a turn runs, resolves { skip } when vetoed: both are a skip.
      const done = await $.session.compact().catch(() => undefined)
      if (done === undefined || done.skip !== undefined) {
        result = t.skipped
        last = `${clockTime(now)} ${result}`
      } else {
        const at = clockTime(await $.clock.now())
        const before = done.tokensBefore ?? tokens
        result =
          done.tokensAfter === undefined
            ? t.done(at)
            : t.doneSized(at, kilo(before), kilo(done.tokensAfter))
        last =
          done.tokensAfter === undefined ? t.lastDone(at) : t.lastDoneSized(at, kilo(before), kilo(done.tokensAfter))
      }
    }
  }
  await show($)
}

async function arm($: EngineInterface) {
  disarm()
  const now = await $.clock.now()
  const delayMs = minutes * MIN + OFFSET_MS
  lastActivityAt = now
  armedAt = now
  deadline = now + delayMs
  result = ''
  timer = $.clock.after(delayMs, () => void fire($).catch(() => undefined))
  ticker = $.clock.every(MIN, () => void show($).catch(() => undefined))
  await show($)
}

async function idle($: EngineInterface) {
  disarm()
  armedAt = undefined
  result = ''
  await show($)
}

async function toggle($: EngineInterface) {
  await $.store.set('enabled', !(await loadEnabled($)))
  await show($)
}

export const register: Register = (on, options) => {
  if (typeof options.minutes === 'number') minutes = options.minutes
  if (typeof options.minTokens === 'number') minTokens = options.minTokens
  t = options.language === 'zh-TW' ? TEXT['zh-TW'] : TEXT.en

  on('session.start', async ($, e, next) => {
    await $.command.register({ name: 'idle-guard', description: t.command })
    await show($)
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    if (e.agentId === undefined) await arm($)
    return next(e)
  })

  on('turn.start', async ($, e, next) => {
    if (e.agentId === undefined) await idle($)
    return next(e)
  })

  on('command.run', { command: 'idle-guard' }, async $ => {
    await $.ui.open({ id: PANE, title: 'Idle Guard', focus: true, closeOnEscape: true, holdToasts: true, rows: 4 })
    return {}
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Button, Text } = $.ui.resolve(e)
    // Reading the view subscribes this drawing: each per-minute write redraws it, and
    // the usage below is read afresh with it.
    const { value = IDLE_VIEW } = await $.state.get(view)
    const tokens = await $.session
      .usage()
      .then(u => u.context.tokens)
      .catch(() => undefined)
    const enabled = value.enabled
    const elapsed =
      value.armedAt === null ? 0 : Math.min(minutes, Math.max(0, Math.floor((value.at - value.armedAt) / MIN)))
    const ctx =
      tokens === undefined
        ? '—'
        : `${kilo(tokens)} ${tokens >= minTokens ? '≥' : '<'} ${kilo(minTokens)} → ${
            !enabled ? t.ctxOff : tokens >= minTokens ? t.ctxWill : t.ctxWont
          }`

    // The pane frame draws `title` only while several panes are open (it is their tab),
    // so the heading is drawn here; the frame title stays for the tab.
    return (
      <Box flexDirection="column">
        <Box flexDirection="row" justifyContent="space-between">
          <Text bold>Idle Guard</Text>
          <Box flexDirection="row" gap={1}>
            <Button
              key="toggle"
              hotkey="c"
              label={enabled ? t.on : t.off}
              onPress={() => void toggle($).catch(() => undefined)}
            >
              {enabled ? <Text color="success">{t.on}</Text> : <Text dimColor>{t.off}</Text>}
            </Button>
            <Text dimColor>{t.hintToggle}</Text>
          </Box>
        </Box>
        {value.armedAt === null ? (
          <Box flexDirection="row">
            <Text>{t.idleLabel}</Text>
            <Text dimColor>{t.running}</Text>
          </Box>
        ) : (
          <Box flexDirection="row">
            <Text>{t.idleLabel}</Text>
            <Text dimColor={!enabled}>{barOf(elapsed, minutes)}</Text>
            <Text>{`  ${t.elapsed(elapsed, minutes)}`}</Text>
          </Box>
        )}
        <Text>{`ctx   ${ctx}`}</Text>
        <Box flexDirection="row" justifyContent="space-between">
          <Text>{`${t.lastLabel}${value.last || '—'}`}</Text>
          <Text dimColor>{t.hintClose}</Text>
        </Box>
      </Box>
    )
  })
}
