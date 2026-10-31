import { expect, mock, test } from 'claude-code/testing'
import type { On, PaneOpenArgs } from 'claude-code'
import type { Engine } from 'claude-code/testing'

const MIN = 60_000
const T0 = Date.UTC(2026, 9, 16, 1, 0, 0)
const DELAY = 54 * MIN + 30_000
const ON_LINE = 'idle-guard  compact ● ON'
const OFF_LINE = 'idle-guard  compact ○ OFF'

type Compact = 'ok' | 'skip' | 'reject'

const ZH = { language: 'zh-TW' }
const ZH_TEST = { options: ZH }

function world(on: On, opts: { tokens?: number | null; compact?: Compact; store?: Record<string, unknown> } = {}) {
  const clock = mock.clock(on, { now: T0 })
  const store: Record<string, unknown> = { ...opts.store }
  const statuses: (string | undefined)[] = []
  const opened: PaneOpenArgs[] = []
  const described: (string | undefined)[] = []
  let compacts = 0

  on('store.get', ($, e) => ({ value: store[e.key] }))
  on('store.set', ($, e) => {
    store[e.key] = e.value
    return { value: undefined }
  })
  on('ui.status', ($, e) => {
    statuses.push(e.text)
    return { value: undefined }
  })
  on('command.register', ($, e) => {
    described.push(e.description)
    return { value: { command: e.name } }
  })
  on('ui.open', ($, e) => {
    opened.push(e)
    return { value: { isPlaced: true } }
  })
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('turn.start', ($, e) => ({ turnId: e.turnId }))
  on('turn.complete', ($, e) => ({ text: e.answer }))
  on('session.usage', () => ({
    value: {
      startedAt: T0,
      // null: the figure is missing, as before the first response.
      context: { tokens: opts.tokens === null ? undefined : (opts.tokens ?? 300_000), window: 1_000_000 },
      rateLimits: [],
    },
  }))
  // 'reject' leaves session.compact unanswered: the kit's bottom throws, so the call rejects.
  if (opts.compact !== 'reject') {
    on('session.compact', () => {
      compacts++
      return opts.compact === 'skip'
        ? { skip: 'vetoed by a hook' }
        : {
            messages: [{ role: 'user' as const, text: 'summary', toolUses: [] }],
            tokensBefore: 300_000,
            tokensAfter: 40_000,
          }
    })
  }

  return {
    clock,
    store,
    opened,
    described,
    compacts: () => compacts,
    status: () => statuses[statuses.length - 1],
  }
}

const start = ($: Engine) => $.session.start({ cwd: 'C:/work', surface: 'terminal', isInteractive: true })
const turnStart = ($: Engine, agentId?: string) => $.turn.start({ text: 'hi', turnId: 't', agentId })
const turnDone = ($: Engine, agentId?: string) =>
  $.turn.complete({ answer: 'ok', durationMs: 1, isAborted: false, turnId: 't', reason: 'answer', agentId })

test('arms on turn.complete and compacts at the deadline when usage is over the threshold', ZH_TEST, async ($, on) => {
  const w = world(on)
  await start($)
  await turnDone($)
  await w.clock.advance(DELAY - 1)
  expect(w.compacts()).toBe(0)
  await w.clock.advance(1)
  expect(w.compacts()).toBe(1)
  expect(w.status()).toMatch(/^idle-guard {2}compact ● ON · 已於 \d\d:\d\d 壓縮（300K→40K）$/)
})

test('status counts down while armed', ZH_TEST, async ($, on) => {
  const w = world(on)
  await start($)
  expect(w.status()).toBe(ON_LINE)
  await turnDone($)
  expect(w.status()).toBe(`${ON_LINE} · 54 分後壓縮`)
  await w.clock.advance(22 * MIN)
  expect(w.status()).toBe(`${ON_LINE} · 32 分後壓縮`)
})

test('turn.start cancels the timer', async ($, on) => {
  const w = world(on)
  await start($)
  await turnDone($)
  await w.clock.advance(10 * MIN)
  await turnStart($)
  expect(w.status()).toBe(ON_LINE)
  await w.clock.advance(120 * MIN)
  expect(w.compacts()).toBe(0)
  expect(w.status()).toBe(ON_LINE)
})

test('a new turn.complete re-arms one timer, not two', async ($, on) => {
  const w = world(on)
  await start($)
  await turnDone($)
  await w.clock.advance(10 * MIN)
  await turnDone($)
  await w.clock.advance(DELAY)
  expect(w.compacts()).toBe(1)
  await w.clock.advance(60 * MIN)
  expect(w.compacts()).toBe(1)
})

test('subagent turns neither arm nor cancel', async ($, on) => {
  const w = world(on)
  await start($)
  await turnDone($, 'agent-1')
  expect(w.status()).toBe(ON_LINE)
  await w.clock.advance(DELAY)
  expect(w.compacts()).toBe(0)
  await turnDone($)
  await turnStart($, 'agent-1')
  await w.clock.advance(DELAY)
  expect(w.compacts()).toBe(1)
})

test('under the threshold: no compaction', ZH_TEST, async ($, on) => {
  const w = world(on, { tokens: 150_000 })
  await start($)
  await turnDone($)
  await w.clock.advance(DELAY)
  expect(w.compacts()).toBe(0)
  expect(w.status()).toBe(`${ON_LINE} · 未壓縮：context 不到 200K`)
})

test('disabled: no compaction and the OFF line', async ($, on) => {
  const w = world(on, { store: { enabled: false } })
  await start($)
  expect(w.status()).toBe(OFF_LINE)
  await turnDone($)
  expect(w.status()).toBe(OFF_LINE)
  await w.clock.advance(DELAY)
  expect(w.compacts()).toBe(0)
  expect(w.status()).toBe(OFF_LINE)
})

test('lapsed: a timer firing past 60 idle minutes does not compact', { options: { ...ZH, minutes: 61 } }, async ($, on) => {
  const w = world(on)
  await start($)
  await turnDone($)
  await w.clock.advance(61 * MIN + 30_000)
  expect(w.compacts()).toBe(0)
  expect(w.status()).toBe(`${ON_LINE} · 未壓縮：閒置已超過 60 分`)
})

test('options override the delay and the threshold', { options: { minutes: 1, minTokens: 1000 } }, async ($, on) => {
  const w = world(on, { tokens: 2000 })
  await start($)
  await turnDone($)
  await w.clock.advance(MIN + 30_000)
  expect(w.compacts()).toBe(1)
})

test('a vetoed compaction is a skip, not an error', ZH_TEST, async ($, on) => {
  const w = world(on, { compact: 'skip' })
  await start($)
  await turnDone($)
  await w.clock.advance(DELAY)
  expect(w.compacts()).toBe(1)
  expect(w.status()).toBe(`${ON_LINE} · 未壓縮：已略過`)
})

test('a rejected compaction is a skip, not an error', ZH_TEST, async ($, on) => {
  const w = world(on, { compact: 'reject' })
  await start($)
  await turnDone($)
  await w.clock.advance(DELAY)
  expect(w.status()).toBe(`${ON_LINE} · 未壓縮：已略過`)
})

test('/idle-guard opens the pane as a dialog', async ($, on) => {
  const w = world(on)
  await start($)
  await $.command.run({
    command: 'idle-guard',
    args: '',
    origin: { kind: 'composer' },
    presentation: { isFullscreen: false, columns: 100 },
  })
  expect(w.opened[w.opened.length - 1]).toMatchObject({
    id: 'idle-guard',
    title: 'Idle Guard',
    focus: true,
    closeOnEscape: true,
    holdToasts: true,
  })
  expect(w.opened[w.opened.length - 1]?.rows).toBeGreaterThan(0)
})

const PANE_PROPS = {
  title: 'Idle Guard',
  isFocused: true,
  bodyColumns: 60,
  placement: 'inline',
  scroll: { offset: 0, bodyRows: 6 },
  view: {},
} as const

const mountPane = ($: Engine, surface: 'terminal' | 'desktop' = 'terminal') =>
  $.ui.mount({ plugin: 'idle-guard', surface, component: 'Pane', requestId: 'idle-guard', props: PANE_PROPS })

// Plain-text rendering of a drawn pane, for what the terminal prints: a column Box is
// its children's lines, a row Box joins them (space-between pushes the ends apart),
// a Button prints as [ label ]. CJK glyphs count two cells.
type Node = { type: string; props: Record<string, unknown>; children?: unknown[] }
const cells = (s: string) => [...s].reduce((n, c) => n + ((c.codePointAt(0) ?? 0) >= 0x2e80 ? 2 : 1), 0)
function textOf(n: unknown): string {
  if (typeof n === 'string') return n
  const e = n as Node
  const gap = e.type === 'Box' && typeof e.props.gap === 'number' ? ' '.repeat(e.props.gap) : ''
  const inner = (e.children ?? []).map(textOf).join(gap)
  return e.type === 'Button' ? `[ ${inner || String(e.props.label)} ]` : inner
}
function linesOf(n: unknown, width: number): string[] {
  const e = n as Node
  if (typeof n === 'string' || e.type !== 'Box') return [textOf(n)]
  const kids = e.children ?? []
  if (e.props.flexDirection !== 'row') return kids.flatMap(k => linesOf(k, width))
  const parts = kids.map(textOf)
  if (e.props.justifyContent === 'space-between' && parts.length === 2) {
    const [a = '', b = ''] = parts
    return [a + ' '.repeat(Math.max(1, width - cells(a) - cells(b))) + b]
  }
  return [parts.join(' '.repeat(typeof e.props.gap === 'number' ? e.props.gap : 0))]
}
const paneLines = async (ui: Awaited<ReturnType<typeof mountPane>>) =>
  linesOf(await ui.drawn(), PANE_PROPS.bodyColumns).map(l => l.trimEnd())

const BAR = (n: number) => '█'.repeat(n) + '░'.repeat(19 - n)

test('title: the pane draws its own heading once; the frame title is only a tab', ZH_TEST, async ($, on) => {
  world(on)
  await start($)
  const ui = await mountPane($)
  expect(await ui.findAll({ type: 'Text', text: 'Idle Guard' })).toHaveLength(1)
  expect((await ui.find({ type: 'Text', text: 'Idle Guard' }))?.props.bold).toBe(true)
  expect((await paneLines(ui))[0]).toMatch(/^Idle Guard +\[ ● 開啟 \] c 切換$/)
  await ui.unmount()
})

test('idle row: the bar is empty at arm, rounds to 8 of 19 at 22/54, full at 54/54', ZH_TEST, async ($, on) => {
  const w = world(on)
  await start($)
  await turnDone($)
  const ui = await mountPane($)
  expect((await paneLines(ui))[1]).toBe(`閒置  ${BAR(0)}  0 / 54 分`)
  await w.clock.advance(22 * MIN)
  // 22 / 54 * 19 = 7.74 cells: Math.round gives 8.
  expect((await paneLines(ui))[1]).toBe(`閒置  ${BAR(8)}  22 / 54 分`)
  await w.clock.advance(32 * MIN)
  expect((await paneLines(ui))[1]).toBe(`閒置  ${BAR(19)}  54 / 54 分`)
  await ui.unmount()
})

test('idle row: not armed while a turn runs or before the first one', ZH_TEST, async ($, on) => {
  world(on)
  await start($)
  const ui = await mountPane($)
  expect((await paneLines(ui))[1]).toBe('閒置  對話進行中，未計時')
  expect((await ui.find({ type: 'Text', text: '對話進行中，未計時' }))?.props.dimColor).toBe(true)
  await turnDone($)
  expect((await paneLines(ui))[1]).toContain('0 / 54 分')
  await turnStart($)
  expect((await paneLines(ui))[1]).toBe('閒置  對話進行中，未計時')
  await ui.unmount()
})

test('idle row: the bar is dim when disabled', async ($, on) => {
  world(on, { store: { enabled: false } })
  await start($)
  await turnDone($)
  const ui = await mountPane($)
  expect((await ui.find({ type: 'Text', text: BAR(0) }))?.props.dimColor).toBe(true)
  await ui.unmount()
})

test('ctx row: at or over the threshold says it will compact', ZH_TEST, async ($, on) => {
  world(on, { tokens: 312_000 })
  await start($)
  const ui = await mountPane($)
  expect((await paneLines(ui))[2]).toBe('ctx   312K ≥ 200K → 時間到會壓縮')
  await ui.unmount()
})

test('ctx row: under the threshold says it will not', ZH_TEST, async ($, on) => {
  world(on, { tokens: 150_000 })
  await start($)
  const ui = await mountPane($)
  expect((await paneLines(ui))[2]).toBe('ctx   150K < 200K → 不會壓縮')
  await ui.unmount()
})

test('ctx row: a missing figure is unknown', async ($, on) => {
  world(on, { tokens: null })
  await start($)
  const ui = await mountPane($)
  expect((await paneLines(ui))[2]).toBe('ctx   —')
  await ui.unmount()
})

test('ctx row follows the configured threshold', { options: { ...ZH, minTokens: 1000 } }, async ($, on) => {
  world(on, { tokens: 2000 })
  await start($)
  const ui = await mountPane($)
  expect((await paneLines(ui))[2]).toBe('ctx   2K ≥ 1K → 時間到會壓縮')
  await ui.unmount()
})

test('last row: none, then the compaction with before and after', ZH_TEST, async ($, on) => {
  const w = world(on)
  await start($)
  await turnDone($)
  const ui = await mountPane($)
  expect((await paneLines(ui))[3]).toMatch(/^上次 {2}— +Esc 關閉$/)
  await w.clock.advance(DELAY)
  expect((await paneLines(ui))[3]).toMatch(/^上次 {2}\d\d:\d\d 壓縮 300K → 40K +Esc 關閉$/)
  await ui.unmount()
})

test('last row: a skip reason, kept through the next turn', ZH_TEST, async ($, on) => {
  const w = world(on, { tokens: 150_000 })
  await start($)
  await turnDone($)
  const ui = await mountPane($)
  await w.clock.advance(DELAY)
  expect((await paneLines(ui))[3]).toMatch(/^上次 {2}\d\d:\d\d 未壓縮：context 不到 200K +Esc 關閉$/)
  await turnStart($)
  expect((await paneLines(ui))[3]).toMatch(/^上次 {2}\d\d:\d\d 未壓縮：context 不到 200K/)
  await ui.unmount()
})

test('last row: a vetoed compaction reads 已略過', ZH_TEST, async ($, on) => {
  const w = world(on, { compact: 'skip' })
  await start($)
  await turnDone($)
  const ui = await mountPane($)
  await w.clock.advance(DELAY)
  expect((await paneLines(ui))[3]).toMatch(/^上次 {2}\d\d:\d\d 未壓縮：已略過 +Esc 關閉$/)
  await ui.unmount()
})

test('last row: a lapsed timer reads 閒置已超過 60 分', { options: { ...ZH, minutes: 61 } }, async ($, on) => {
  const w = world(on)
  await start($)
  await turnDone($)
  const ui = await mountPane($)
  await w.clock.advance(61 * MIN + 30_000)
  expect((await paneLines(ui))[3]).toMatch(/^上次 {2}\d\d:\d\d 未壓縮：閒置已超過 60 分 +Esc 關閉$/)
  await ui.unmount()
})

test('the pane is four rows and /idle-guard asks for four', async ($, on) => {
  const w = world(on)
  await start($)
  await turnDone($)
  await $.command.run({
    command: 'idle-guard',
    args: '',
    origin: { kind: 'composer' },
    presentation: { isFullscreen: false, columns: 100 },
  })
  const ui = await mountPane($)
  expect(await paneLines(ui)).toHaveLength(4)
  expect(w.opened[w.opened.length - 1]?.rows).toBe(4)
  await ui.unmount()
})

test('the pane redraws every minute while open', ZH_TEST, async ($, on) => {
  const w = world(on)
  await start($)
  await turnDone($)
  const ui = await mountPane($)
  expect((await paneLines(ui))[1]).toContain('0 / 54 分')
  await w.clock.advance(MIN)
  expect((await paneLines(ui))[1]).toContain('1 / 54 分')
  await w.clock.advance(MIN)
  expect((await paneLines(ui))[1]).toContain('2 / 54 分')
  await ui.unmount()
})

test('the pane toggle flips the stored setting, the label, its color and the status line', ZH_TEST, async ($, on) => {
  const w = world(on)
  await start($)
  await turnDone($)
  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await mountPane($, surface)
    expect(await ui.find({ type: 'Text', text: 'Idle Guard' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: 'Esc 關閉' })).toBeDefined()
    expect((await ui.find({ type: 'Text', text: 'c 切換' }))?.props.dimColor).toBe(true)
    const button = await ui.find({ type: 'Button', key: 'toggle' })
    expect(button?.text).toBe('● 開啟')
    expect(button?.props.hotkey).toBe('c')
    expect((await ui.find({ type: 'Text', text: '● 開啟' }))?.props.color).toBe('success')

    await ui.press({ key: 'toggle' })
    expect(w.store.enabled).toBe(false)
    expect(w.status()).toBe(OFF_LINE)
    expect((await ui.find({ type: 'Button', key: 'toggle' }))?.text).toBe('○ 關閉')
    const off = await ui.find({ type: 'Text', text: '○ 關閉' })
    expect(off?.props.dimColor).toBe(true)
    expect(off?.props.color).toBeUndefined()

    await ui.press({ key: 'toggle' })
    expect(w.store.enabled).toBe(true)
    expect(w.status()).toBe(`${ON_LINE} · 54 分後壓縮`)
    expect((await ui.find({ type: 'Button', key: 'toggle' }))?.text).toBe('● 開啟')
    await ui.unmount()
  }
})

// English is the default language; any value other than 'zh-TW' means English.
test('en: the status line counts down, compacts and reports sizes', async ($, on) => {
  const w = world(on)
  await start($)
  await turnDone($)
  expect(w.status()).toBe(`${ON_LINE} · compacts in 54 min`)
  await w.clock.advance(22 * MIN)
  expect(w.status()).toBe(`${ON_LINE} · compacts in 32 min`)
  await w.clock.advance(DELAY)
  expect(w.status()).toMatch(/^idle-guard {2}compact ● ON · Compacted at \d\d:\d\d \(300K→40K\)$/)
})

test('en: status line skip reasons', async ($, on) => {
  const w = world(on, { tokens: 150_000 })
  await start($)
  await turnDone($)
  await w.clock.advance(DELAY)
  expect(w.status()).toBe(`${ON_LINE} · Not compacted: context under 200K`)
})

test('en: a vetoed compaction reads skipped', async ($, on) => {
  const w = world(on, { compact: 'skip' })
  await start($)
  await turnDone($)
  await w.clock.advance(DELAY)
  expect(w.status()).toBe(`${ON_LINE} · Not compacted: skipped`)
})

test('en: a lapsed timer reads idle over 60 min', { options: { minutes: 61 } }, async ($, on) => {
  const w = world(on)
  await start($)
  await turnDone($)
  await w.clock.advance(61 * MIN + 30_000)
  expect(w.status()).toBe(`${ON_LINE} · Not compacted: idle over 60 min`)
})

test('en: the pane header, toggle and hints', async ($, on) => {
  const w = world(on)
  await start($)
  await turnDone($)
  const ui = await mountPane($)
  expect((await paneLines(ui))[0]).toMatch(/^Idle Guard +\[ ● ON \] c toggle$/)
  expect((await ui.find({ type: 'Text', text: '● ON' }))?.props.color).toBe('success')
  expect(await ui.find({ type: 'Text', text: 'Esc close' })).toBeDefined()
  await ui.press({ key: 'toggle' })
  expect(w.status()).toBe(OFF_LINE)
  expect((await ui.find({ type: 'Button', key: 'toggle' }))?.text).toBe('○ OFF')
  expect((await ui.find({ type: 'Text', text: '○ OFF' }))?.props.dimColor).toBe(true)
  await ui.unmount()
})

test('en: the idle row and the not-armed note', async ($, on) => {
  const w = world(on)
  await start($)
  const ui = await mountPane($)
  expect((await paneLines(ui))[1]).toBe('Idle  Turn running, timer off')
  await turnDone($)
  expect((await paneLines(ui))[1]).toBe(`Idle  ${BAR(0)}  0 / 54 min`)
  await w.clock.advance(22 * MIN)
  expect((await paneLines(ui))[1]).toBe(`Idle  ${BAR(8)}  22 / 54 min`)
  await ui.unmount()
})

test('en: the ctx row in each state', async ($, on) => {
  world(on, { tokens: 312_000 })
  await start($)
  const ui = await mountPane($)
  expect((await paneLines(ui))[2]).toBe('ctx   312K ≥ 200K → will compact when idle')
  await ui.press({ key: 'toggle' })
  expect((await paneLines(ui))[2]).toBe('ctx   312K ≥ 200K → off, will not compact')
  await ui.unmount()
})

test('en: the ctx row under the threshold', async ($, on) => {
  world(on, { tokens: 150_000 })
  await start($)
  const ui = await mountPane($)
  expect((await paneLines(ui))[2]).toBe('ctx   150K < 200K → will not compact')
  await ui.unmount()
})

test('en: the last row, aligned with the Esc hint', async ($, on) => {
  const w = world(on)
  await start($)
  await turnDone($)
  const ui = await mountPane($)
  const first = (await paneLines(ui))[3] ?? ''
  expect(first).toMatch(/^Last {2}— +Esc close$/)
  expect(cells(first)).toBe(PANE_PROPS.bodyColumns)
  await w.clock.advance(DELAY)
  expect((await paneLines(ui))[3]).toMatch(/^Last {2}\d\d:\d\d compacted 300K → 40K +Esc close$/)
  await ui.unmount()
})

test('en: last row skip reasons', async ($, on) => {
  const w = world(on, { tokens: 150_000 })
  await start($)
  await turnDone($)
  const ui = await mountPane($)
  await w.clock.advance(DELAY)
  expect((await paneLines(ui))[3]).toMatch(/^Last {2}\d\d:\d\d Not compacted: context under 200K +Esc close$/)
  await ui.unmount()
})

test('en: the command description; zh-TW keeps the original', async ($, on) => {
  const w = world(on)
  await start($)
  expect(w.described).toEqual(['Toggle idle compaction (Idle Guard)'])
})

test('zh-TW: the command description', ZH_TEST, async ($, on) => {
  const w = world(on)
  await start($)
  expect(w.described).toEqual(['閒置 compact 開關（Idle Guard）'])
})

test('an unknown language value falls back to English', { options: { language: 'fr' } }, async ($, on) => {
  const w = world(on)
  await start($)
  await turnDone($)
  expect(w.status()).toBe(`${ON_LINE} · compacts in 54 min`)
})
