import { expect, mock, test } from 'claude-code/testing'
import type { AgentInfo, On, PaneOpenArgs, ToolCallResult } from 'claude-code'
import type { Engine } from 'claude-code/testing'

const MIN = 60_000
const T0 = Date.UTC(2026, 9, 16, 1, 0, 0)
// The session runs in C:/work.
const ACCEPTANCE = 'C:/work/.claude/acceptance/'

type AgentAnswer = ToolCallResult<'Agent'>

// The world beneath the plugin: clock, status line, panes, agents, files.
function world(on: On) {
  const clock = mock.clock(on, { now: T0 })
  const statuses: (string | undefined)[] = []
  const opened: PaneOpenArgs[] = []
  const closed: string[] = []
  const panes: { id: string; isPlaced: boolean }[] = []
  const agents: AgentInfo[] = []
  const files: Record<string, string> = {}
  let listCalls = 0
  const reads: string[] = []

  on('ui.status', ($, e) => {
    statuses.push(e.text)
    return { value: undefined }
  })
  on('command.register', ($, e) => ({ value: { command: e.name } }))
  on('ui.open', ($, e) => {
    opened.push(e)
    const pane = panes.find(p => p.id === e.id)
    if (pane) pane.isPlaced = true
    else panes.push({ id: e.id, isPlaced: true })
    return { value: { isPlaced: true } }
  })
  on('ui.close', ($, e) => {
    closed.push(e.id)
    panes.splice(
      panes.findIndex(p => p.id === e.id),
      1,
    )
    return { value: undefined }
  })
  on('ui.panes', () => ({
    value: panes.map(p => ({ id: p.id, title: p.id, isShown: p.isPlaced, isFocused: false, isPlaced: p.isPlaced })),
  }))
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('session.end', ($, e) => ({ sessionId: e.sessionId }))
  on('session.usage', () => ({ value: { startedAt: T0, context: { tokens: 1000, window: 1_000_000 }, rateLimits: [] } }))
  on('turn.start', ($, e) => ({ turnId: e.turnId }))
  on('turn.complete', ($, e) => ({ text: e.answer }))
  on('agent.list', () => {
    listCalls++
    return { value: agents.map(a => ({ ...a })) }
  })
  // The engine hands the hook the path in the machine's own spelling; the world keeps forward slashes.
  on('fs.read', ($, e) => {
    const path = e.path.replace(/\\/g, '/')
    reads.push(path)
    const text = files[path]
    return text === undefined ? { deny: `ENOENT: ${e.path}` } : { value: text }
  })

  return {
    clock,
    opened,
    closed,
    panes,
    agents,
    reads,
    listCalls: () => listCalls,
    status: () => statuses[statuses.length - 1],
    statusCount: () => statuses.length,
    // An acceptance file on disk, whoever wrote it.
    file(name: string, text: string) {
      files[ACCEPTANCE + name] = text
    },
  }
}

const start = ($: Engine) => $.session.start({ cwd: 'C:/work', surface: 'terminal', isInteractive: true })
const turnStart = ($: Engine, agentId?: string) => $.turn.start({ text: 'go', turnId: 't', agentId })
const turnDone = ($: Engine, agentId?: string, reason: 'answer' | 'aborted' | 'error' = 'answer') =>
  $.turn.complete({ answer: 'ok', durationMs: 1, isAborted: reason === 'aborted', turnId: 't', reason, agentId })

const usage = {
  input_tokens: 1,
  output_tokens: 1,
  cache_creation_input_tokens: null,
  cache_read_input_tokens: null,
  server_tool_use: null,
  service_tier: null,
  cache_creation: null,
}
const completed = (agentId: string, prompt: string): AgentAnswer => ({
  result: {
    status: 'completed',
    agentId,
    content: [{ type: 'text', text: 'report' }],
    totalToolUseCount: 1,
    totalDurationMs: 1,
    totalTokens: 2,
    usage,
    prompt,
  },
  text: 'report',
})
const launched = (agentId: string, prompt: string, description: string): AgentAnswer => ({
  result: { status: 'async_launched', agentId, description, prompt, outputFile: `C:/tmp/${agentId}.out` },
  text: 'launched',
})

type Dispatch = { description: string; prompt: string; subagent_type?: string }

// An Agent call answered by `answer` after `ms` on the mocked clock; resolves once the call returns.
function agentTool(on: On, w: ReturnType<typeof world>) {
  const queue: { ms: number; answer: (prompt: string, description: string) => AgentAnswer }[] = []
  on('tool.call', { tool: 'Agent' }, async ($, e) => {
    const next = queue.shift()
    if (!next) throw new Error('unexpected Agent call')
    if (next.ms > 0) await w.clock.sleep(next.ms)
    return next.answer(e.prompt, e.description)
  })
  return {
    foreground(ms: number, agentId: string) {
      queue.push({ ms, answer: prompt => completed(agentId, prompt) })
    },
    background(agentId: string) {
      queue.push({ ms: 0, answer: (prompt, description) => launched(agentId, prompt, description) })
    },
    answer(answer: AgentAnswer) {
      queue.push({ ms: 0, answer: () => answer })
    },
  }
}

// Write, Edit and NotebookEdit are answered ok, whoever calls.
function writeTools(on: On) {
  for (const tool of ['Write', 'Edit', 'NotebookEdit'] as const) {
    on('tool.call', { tool }, () => ({ result: {}, text: 'ok' }) as never)
  }
}
// The main loop (or, with an agentId, a subagent) writes a file.
const writeFile = ($: Engine, path: string, agentId?: string) =>
  $.tool.call({ tool: 'Write', file_path: path, content: '{}', agentId } as never)
const writeAcceptance = ($: Engine, name: string, agentId?: string) => writeFile($, ACCEPTANCE + name, agentId)

const dispatch = ($: Engine, d: Dispatch) => $.tool.call({ tool: 'Agent', ...d })

const pad = (n: number) => String(n).padStart(2, '0')
const hhmm = (ms: number) => {
  const d = new Date(ms)
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`
}

// ---------------------------------------------------------------- dispatches

test('nothing shows before the first dispatch', async ($, on) => {
  const w = world(on)
  await start($)
  await turnDone($)
  expect(w.status()).toBeUndefined()
  expect(w.opened).toHaveLength(0)
})

test('a foreground dispatch runs until its result, then counts as done', async ($, on) => {
  const w = world(on)
  const agent = agentTool(on, w)
  await start($)
  agent.foreground(5 * MIN, 'a-1')
  const call = dispatch($, { description: 'Read the code', prompt: 'Read every file.', subagent_type: 'Explore' })
  await w.clock.settle()
  expect(w.status()).toBe('board  ░░░░░░░░░░ 0/1 完成 · 1 執行中')
  await w.clock.advance(5 * MIN)
  await call
  expect(w.status()).toBe('board  ██████████ 1/1 完成 · 0 執行中')
})

test('a background dispatch ends at the turn.complete carrying its agentId', async ($, on) => {
  const w = world(on)
  const agent = agentTool(on, w)
  await start($)
  agent.background('bg-1')
  await dispatch($, { description: 'Run tests', prompt: 'Run them.', subagent_type: 'test-analyst' })
  expect(w.status()).toBe('board  ░░░░░░░░░░ 0/1 完成 · 1 執行中')
  await turnDone($, 'someone-else')
  expect(w.status()).toBe('board  ░░░░░░░░░░ 0/1 完成 · 1 執行中')
  await w.clock.advance(3 * MIN)
  await turnDone($, 'bg-1')
  expect(w.status()).toBe('board  ██████████ 1/1 完成 · 0 執行中')
})

test('a background dispatch ends when the polled agent list says completed', async ($, on) => {
  const w = world(on)
  const agent = agentTool(on, w)
  await start($)
  agent.background('bg-2')
  await dispatch($, { description: 'Survey', prompt: 'Look around.', subagent_type: 'Explore' })
  w.agents.push({ id: 'bg-2', description: 'Survey', type: 'Explore', status: 'running' })
  await w.clock.advance(30_000)
  expect(w.listCalls()).toBe(1)
  expect(w.status()).toBe('board  ░░░░░░░░░░ 0/1 完成 · 1 執行中')
  w.agents[0]!.status = 'completed'
  await w.clock.advance(30_000)
  expect(w.listCalls()).toBe(2)
  expect(w.status()).toBe('board  ██████████ 1/1 完成 · 0 執行中')
})

test('polling stops once nothing runs, and starts again with the next background dispatch', async ($, on) => {
  const w = world(on)
  const agent = agentTool(on, w)
  await start($)
  agent.foreground(MIN, 'fg-1')
  const call = dispatch($, { description: 'Quick', prompt: 'q' })
  await w.clock.advance(MIN)
  await call
  await w.clock.advance(5 * MIN)
  expect(w.listCalls()).toBe(0)

  agent.background('bg-3')
  await dispatch($, { description: 'Long', prompt: 'l' })
  await w.clock.advance(60_000)
  expect(w.listCalls()).toBe(2)
  await turnDone($, 'bg-3')
  await w.clock.advance(10 * MIN)
  expect(w.listCalls()).toBe(2)
})

test('a denied dispatch fails with the reason', async ($, on) => {
  const w = world(on)
  const agent = agentTool(on, w)
  await start($)
  agent.answer({ deny: 'subagents are off here' })
  await dispatch($, { description: 'Nope', prompt: 'n' })
  expect(w.status()).toBe('board  ░░░░░░░░░░ 0/1 完成 · 0 執行中 · 1 失敗')
  expect(w.listCalls()).toBe(0)
})

test('an errored dispatch fails; so do an aborted turn and a killed agent', async ($, on) => {
  const w = world(on)
  const agent = agentTool(on, w)
  await start($)
  agent.answer({ result: undefined, text: 'Agent type "nope" not found', isError: true } as unknown as AgentAnswer)
  await dispatch($, { description: 'Bad type', prompt: 'b', subagent_type: 'nope' })
  agent.background('bg-4')
  await dispatch($, { description: 'Interrupted', prompt: 'i' })
  agent.background('bg-5')
  await dispatch($, { description: 'Killed', prompt: 'k' })
  await turnDone($, 'bg-4', 'aborted')
  w.agents.push({ id: 'bg-5', description: 'Killed', type: 'general-purpose', status: 'killed' })
  await w.clock.advance(30_000)
  expect(w.status()).toBe('board  ░░░░░░░░░░ 0/3 完成 · 0 執行中 · 3 失敗')
})

// Three runs of one role, 4, 6 and 8 minutes long: with them the role has an estimate (median 6).
async function finishThree($: Engine, w: ReturnType<typeof world>, agent: ReturnType<typeof agentTool>, type: string) {
  for (const ms of [4 * MIN, 6 * MIN, 8 * MIN]) {
    agent.foreground(ms, `fg-${type}-${ms}`)
    const call = dispatch($, { description: 'd', prompt: 'p', subagent_type: type })
    await w.clock.advance(ms)
    await call
  }
}

test('ETA: needs three finished rows of the same role; concurrent agents combine by max, not sum', async ($, on) => {
  const w = world(on)
  const agent = agentTool(on, w)
  await start($)
  // Two Explore runs and three general-purpose: Explore has no estimate, and no fallback to the others.
  for (const [ms, type] of [
    [4 * MIN, 'Explore'],
    [8 * MIN, 'Explore'],
    [20 * MIN, 'general-purpose'],
    [20 * MIN, 'general-purpose'],
    [20 * MIN, 'general-purpose'],
  ] as const) {
    agent.foreground(ms, `fg-${ms}`)
    const call = dispatch($, { description: 'd', prompt: 'p', subagent_type: type })
    await w.clock.advance(ms)
    await call
  }
  expect(w.status()).toBe('board  ██████████ 5/5 完成 · 0 執行中')
  agent.background('bg-e')
  await dispatch($, { description: 'e', prompt: 'p', subagent_type: 'Explore' })
  expect(w.status()).toBe('board  ████████▓░ 5/6 完成 · 1 執行中')
  await turnDone($, 'bg-e', 'aborted')
  // A third Explore run gives the role its estimate (4, 8, 6 → 6 minutes).
  agent.foreground(6 * MIN, 'fg-6')
  const third = dispatch($, { description: 'd', prompt: 'p', subagent_type: 'Explore' })
  await w.clock.advance(6 * MIN)
  await third
  agent.background('bg-1')
  await dispatch($, { description: 'one', prompt: 'p', subagent_type: 'Explore' })
  expect(w.status()).toBe('board  ███████▓░░ 6/8 完成 · 1 執行中 · 1 失敗 · ~6 分')
  await w.clock.advance(4 * MIN)
  // The second agent starts with 6 minutes left, the first has 2: the longest, not 8.
  agent.background('bg-2')
  await dispatch($, { description: 'two', prompt: 'p', subagent_type: 'Explore' })
  expect(w.status()).toBe('board  ██████▓░░░ 6/9 完成 · 2 執行中 · 1 失敗 · ~6 分')
  // The first is past its estimate and adds nothing; the second has a minute left.
  await w.clock.advance(5 * MIN)
  expect(w.status()).toBe('board  ██████▓░░░ 6/9 完成 · 2 執行中 · 1 失敗 · ~1 分')
  // Every agent past its estimate: no ETA to give.
  await w.clock.advance(5 * MIN)
  expect(w.status()).toBe('board  ██████▓░░░ 6/9 完成 · 2 執行中 · 1 失敗')
})

test('ETA is left out while nothing has finished', async ($, on) => {
  const w = world(on)
  const agent = agentTool(on, w)
  await start($)
  agent.background('bg-x')
  await dispatch($, { description: 'x', prompt: 'x' })
  expect(w.status()).toBe('board  ░░░░░░░░░░ 0/1 完成 · 1 執行中')
})

// ---------------------------------------------------------------- pane

const PANE_PROPS = {
  title: 'board',
  isFocused: false,
  bodyColumns: 100,
  placement: 'inline',
  scroll: { offset: 0, bodyRows: 14 },
  view: {},
} as const

const mountPane = ($: Engine, surface: 'terminal' | 'desktop') =>
  $.ui.mount({ plugin: 'board', surface, component: 'Pane', requestId: 'board', props: PANE_PROPS })

const runBoard = ($: Engine) =>
  $.command.run({
    command: 'board',
    args: '',
    origin: { kind: 'composer' },
    presentation: { isFullscreen: false, columns: 120 },
  })

test('the pane opens by itself, collapsed and without the keyboard, at the first dispatch only', async ($, on) => {
  const w = world(on)
  const agent = agentTool(on, w)
  await start($)
  agent.background('bg-1')
  await dispatch($, { description: 'one', prompt: 'p' })
  agent.background('bg-2')
  await dispatch($, { description: 'two', prompt: 'p' })
  expect(w.opened).toHaveLength(1)
  expect(w.opened[0]).toMatchObject({ id: 'board', rows: 2 })
  expect(w.opened[0]?.focus).toBeUndefined()
})

test('collapsed: the summary and an expand button on hotkey b', async ($, on) => {
  const w = world(on)
  const agent = agentTool(on, w)
  await start($)
  agent.background('bg-1')
  await dispatch($, { description: 'one', prompt: 'p' })
  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await mountPane($, surface)
    expect(await ui.find({ type: 'Text', text: 'board  ░░░░░░░░░░ 0/1 完成 · 1 執行中' })).toBeDefined()
    const expand = await ui.find({ type: 'Button', key: 'expand' })
    expect(expand?.text).toBe('展開')
    expect(expand?.props.hotkey).toBe('b')
    expect(await ui.find({ type: 'Button', text: /one/ })).toBeUndefined()
    await ui.unmount()
  }
})

test('expand and collapse by button, both ways, resizing the pane', async ($, on) => {
  const w = world(on)
  const agent = agentTool(on, w)
  await start($)
  agent.background('bg-1')
  await dispatch($, { description: 'one', prompt: 'p' })
  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await mountPane($, surface)
    await ui.press({ key: 'expand' })
    expect(w.opened[w.opened.length - 1]).toMatchObject({ id: 'board', rows: 20 })
    expect(await ui.find({ type: 'Button', key: 'expand' })).toBeUndefined()
    expect((await ui.find({ type: 'Button', key: 'collapse' }))?.text).toBe('收合')
    expect(await ui.findAll({ type: 'Button', text: /one/ })).toHaveLength(1)
    await ui.press({ key: 'collapse' })
    expect(w.opened[w.opened.length - 1]).toMatchObject({ id: 'board', rows: 2 })
    expect(await ui.find({ type: 'Button', key: 'collapse' })).toBeUndefined()
    expect(await ui.find({ type: 'Button', key: 'expand' })).toBeDefined()
    await ui.unmount()
  }
})

test('expanded rows: icon, task, role, start, elapsed and the estimate left', async ($, on) => {
  const w = world(on)
  const agent = agentTool(on, w)
  await start($)
  agent.foreground(4 * MIN, 'fg-1')
  const call = dispatch($, { description: 'Map the repo', prompt: 'p', subagent_type: 'Explore' })
  await w.clock.advance(4 * MIN)
  await call
  const second = T0 + 4 * MIN
  agent.background('bg-1')
  await dispatch($, { description: 'Map again', prompt: 'p', subagent_type: 'Explore' })
  agent.answer({ deny: 'no' })
  await dispatch($, { description: 'Refused', prompt: 'p', subagent_type: 'Plan' })
  w.agents.push({ id: 'bg-1', description: 'Map again', type: 'Explore', status: 'waiting' })
  await w.clock.advance(MIN)
  const ui = await mountPane($, 'terminal')
  await ui.press({ key: 'expand' })
  const rows = await ui.findAll({ type: 'Button', text: /Map|Refused/ })
  expect(rows.map(r => r.text.replace(/ +/g, ' ').trim())).toEqual([
    `✓ Map the repo Explore ${hhmm(T0)} 4 分`,
    `○ Map again Explore ${hhmm(second)} 1 分 —`,
    `✗ Refused Plan ${hhmm(second)} <1 分`,
  ])
  await ui.unmount()
})

// Terminal cells of a string, wide glyphs counting two: where a column starts.
const cells = (s: string) => [...s].reduce((n, c) => n + ((c.codePointAt(0) ?? 0) >= 0x2e80 ? 2 : 1), 0)

test('active row: an 8-cell bar of elapsed over the role median; 超時 +N 分 past it; — under three samples', async ($, on) => {
  const w = world(on)
  const agent = agentTool(on, w)
  await start($)
  await finishThree($, w, agent, 'Explore')
  const began = T0 + 18 * MIN
  agent.background('bg-1')
  await dispatch($, { description: 'Map more', prompt: 'p', subagent_type: 'Explore' })
  agent.background('bg-2')
  await dispatch($, { description: 'Draft plan', prompt: 'p', subagent_type: 'Plan' })
  await w.clock.advance(3 * MIN)
  const ui = await mountPane($, 'terminal')
  await ui.press({ key: 'expand' })
  const shown = async () =>
    (await ui.findAll({ type: 'Button', text: /Map more|Draft plan/ })).map(r => r.text.replace(/ +/g, ' ').trim())
  // Half of the 6 minutes: 4 of 8 cells. Plan has no finished row at all.
  expect(await shown()).toEqual([`● Map more Explore ${hhmm(began)} 3 分 ████░░░░`, `● Draft plan Plan ${hhmm(began)} 3 分 —`])
  // Two thirds: 5 and a half cells.
  await w.clock.advance(MIN)
  expect((await shown())[0]).toBe(`● Map more Explore ${hhmm(began)} 4 分 █████▓░░`)
  // Past the estimate: the overrun in minutes, rounded up, not a bar and not "~1 分".
  await w.clock.advance(4.5 * MIN)
  expect(await shown()).toEqual([`● Map more Explore ${hhmm(began)} 8 分 超時 +3 分`, `● Draft plan Plan ${hhmm(began)} 8 分 —`])
  await w.clock.advance(5.5 * MIN)
  expect((await shown())[0]).toBe(`● Map more Explore ${hhmm(began)} 14 分 超時 +8 分`)
  await ui.unmount()
})

test('the 剩餘 header and every row of the column start at the same cell, inside the pane width', async ($, on) => {
  const w = world(on)
  const agent = agentTool(on, w)
  await start($)
  await finishThree($, w, agent, 'Explore')
  agent.background('bg-1')
  await dispatch($, { description: 'Map more', prompt: 'p', subagent_type: 'Explore' })
  agent.background('bg-2')
  await dispatch($, { description: '派一個很長很長的中文描述任務，確保這一欄被截斷而不是撐開', prompt: 'p', subagent_type: 'Plan' })
  const ui = await mountPane($, 'terminal')
  await ui.press({ key: 'expand' })
  const header = (await ui.find({ type: 'Text', text: /剩餘/ }))?.text ?? ''
  const column = cells(header.slice(0, header.indexOf('剩餘')))
  for (const elapsed of [2 * MIN, 5 * MIN, 12 * MIN]) {
    await w.clock.advance(elapsed)
    const rows = await ui.findAll({ type: 'Button', text: /Map more|很長/ })
    expect(rows).toHaveLength(2)
    for (const r of rows) {
      const at = r.text.search(/[█▓░—]|超時/)
      expect(at).toBeGreaterThan(0)
      expect(cells(r.text.slice(0, at))).toBe(column)
      expect(cells(r.text)).toBeLessThanOrEqual(PANE_PROPS.bodyColumns)
    }
  }
  await ui.unmount()
})

test('a row opens its detail with the full prompt; back returns to the table', async ($, on) => {
  const w = world(on)
  const agent = agentTool(on, w)
  await start($)
  const prompt = Array.from({ length: 60 }, (_, i) => `line ${i + 1}: ${'x'.repeat(80)}`).join('\n')
  agent.answer({ deny: 'blocked by policy' })
  await dispatch($, { description: 'Long brief', prompt, subagent_type: 'implementer' })
  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await mountPane($, surface)
    await ui.press({ key: 'expand' })
    const row = await ui.find({ type: 'Button', text: /Long brief/ })
    expect(row?.key).toMatch(/^row:/)
    await ui.press({ key: row?.key ?? '' })
    expect(await ui.find({ type: 'Button', text: /Long brief/ })).toBeUndefined()
    expect((await ui.find({ type: 'Button', key: 'back' }))?.text).toBe('返回')
    expect(await ui.find({ type: 'Text', text: /implementer/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /已拒絕：blocked by policy/ })).toBeDefined()
    const texts = (await ui.findAll({ type: 'Text' })).map(t => t.text)
    expect(texts).toContain('line 1: ' + 'x'.repeat(80))
    expect(texts).toContain('line 60: ' + 'x'.repeat(80))
    await ui.press({ key: 'back' })
    expect(await ui.find({ type: 'Button', key: 'back' })).toBeUndefined()
    expect(await ui.find({ type: 'Button', text: /Long brief/ })).toBeDefined()
    await ui.press({ key: 'collapse' })
    await ui.unmount()
  }
})

test('/board closes an open pane and opens it again at the size last chosen', async ($, on) => {
  const w = world(on)
  const agent = agentTool(on, w)
  await start($)
  agent.background('bg-1')
  await dispatch($, { description: 'one', prompt: 'p' })
  await runBoard($)
  expect(w.closed).toEqual(['board'])
  await runBoard($)
  expect(w.opened[w.opened.length - 1]).toMatchObject({ id: 'board', rows: 2 })
  const ui = await mountPane($, 'terminal')
  await ui.press({ key: 'expand' })
  await runBoard($)
  await runBoard($)
  expect(w.opened[w.opened.length - 1]).toMatchObject({ id: 'board', rows: 20 })
  expect(w.opened[w.opened.length - 1]?.focus).toBeUndefined()
  await ui.unmount()
})

test('/board places a pane that waits undrawn instead of closing it', async ($, on) => {
  const w = world(on)
  const agent = agentTool(on, w)
  await start($)
  agent.background('bg-1')
  await dispatch($, { description: 'one', prompt: 'p' })
  const pane = w.panes[0]
  if (pane) pane.isPlaced = false
  await runBoard($)
  expect(w.closed).toEqual([])
  expect(w.opened).toHaveLength(2)
})

// ---------------------------------------------------------------- checklist

function taskTools(on: On) {
  let next = 0
  on('tool.call', { tool: 'TaskCreate' }, ($, e) => ({
    result: { task: { id: String(++next), subject: e.subject } },
    text: `Task #${next} created`,
  }))
  on('tool.call', { tool: 'TaskUpdate' }, ($, e) => ({
    result: { success: true, taskId: e.taskId, updatedFields: [] },
    text: 'updated',
  }))
  on('tool.call', { tool: 'TodoWrite' }, ($, e) => ({ result: { oldTodos: [], newTodos: e.todos }, text: 'ok' }))
}

// The plan steps as the expanded pane draws them: `{icon} {n}  {subject}`.
const checklistOf = async (ui: Awaited<ReturnType<typeof mountPane>>) =>
  (await ui.findAll({ type: 'Text' })).map(t => t.text).filter(t => /^[✓●○] \d+  /.test(t))

test('TaskCreate and TaskUpdate keep the checklist; it shows only when it has items', async ($, on) => {
  const w = world(on)
  const agent = agentTool(on, w)
  taskTools(on)
  await start($)
  agent.background('bg-1')
  await dispatch($, { description: 'one', prompt: 'p' })
  const ui = await mountPane($, 'terminal')
  await ui.press({ key: 'expand' })
  expect(await checklistOf(ui)).toEqual([])
  expect(await ui.find({ type: 'Text', text: '清單' })).toBeUndefined()
  await $.tool.call({ tool: 'TaskCreate', subject: 'Write tests', description: 'd' })
  await $.tool.call({ tool: 'TaskCreate', subject: 'Ship it', description: 'd' })
  await $.tool.call({ tool: 'TaskCreate', subject: 'Drop me', description: 'd' })
  expect(await checklistOf(ui)).toEqual(['○ 1  Write tests', '○ 2  Ship it', '○ 3  Drop me'])
  await $.tool.call({ tool: 'TaskUpdate', taskId: '1', status: 'completed' })
  await $.tool.call({ tool: 'TaskUpdate', taskId: '2', status: 'in_progress', subject: 'Ship v2' })
  await $.tool.call({ tool: 'TaskUpdate', taskId: '3', status: 'deleted' })
  expect(await checklistOf(ui)).toEqual(['✓ 1  Write tests', '● 2  Ship v2'])
  await ui.unmount()
})

test('TodoWrite replaces the checklist with its new todos', async ($, on) => {
  const w = world(on)
  const agent = agentTool(on, w)
  taskTools(on)
  await start($)
  agent.background('bg-1')
  await dispatch($, { description: 'one', prompt: 'p' })
  await $.tool.call({ tool: 'TaskCreate', subject: 'Old', description: 'd' })
  await $.tool.call({
    tool: 'TodoWrite',
    todos: [
      { content: 'Read', status: 'completed', activeForm: 'Reading' },
      { content: 'Write', status: 'in_progress', activeForm: 'Writing' },
      { content: 'Check', status: 'pending', activeForm: 'Checking' },
    ],
  })
  const ui = await mountPane($, 'desktop')
  await ui.press({ key: 'expand' })
  expect(await checklistOf(ui)).toEqual(['✓ 1  Read', '● 2  Write', '○ 3  Check'])
  await ui.unmount()
})

// ---------------------------------------------------------------- acceptance gates

const GATES = JSON.stringify({
  task: 'Ship v2',
  gates: [
    { id: 'A1', kind: 'machine', check: 'tests pass', status: 'pass', ranAt: '2026-10-16T01:02:00Z' },
    { id: 'A2', kind: 'review', check: 'reviewed', status: 'fail', evidence: 'nits' },
    { id: 'A3', kind: 'human', check: 'looks right', status: 'pending' },
  ],
})

test('acceptance files the main loop wrote show as gates; malformed ones are noted; others are left out', async ($, on) => {
  const w = world(on)
  const agent = agentTool(on, w)
  writeTools(on)
  await start($)
  agent.background('bg-1')
  await dispatch($, { description: 'one', prompt: 'p' })
  w.file('ship.json', GATES)
  w.file('broken.json', '{ "task": "Half", "gates": [ { "id": 1 } ] }')
  w.file('old.json', GATES.replace('Ship v2', 'Old task'))
  w.file('notes.txt', 'not a gate file')
  await writeAcceptance($, 'ship.json')
  await writeAcceptance($, 'broken.json')
  await turnDone($)
  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await mountPane($, surface)
    expect((await ui.find({ type: 'Text', text: /^Ship v2/ }))?.text).toBe('Ship v2  A1✓ A2✗ A3·')
    // A human gate still pending is the one that waits for the person: warning color, not dim.
    expect((await ui.find({ type: 'Text', text: /^A3·$/ }))?.props.color).toBe('warning')
    expect((await ui.find({ type: 'Text', text: /^A3·$/ }))?.props.dimColor).toBeFalsy()
    expect((await ui.find({ type: 'Text', text: /^A1✓$/ }))?.props.dimColor).toBeFalsy()
    expect((await ui.find({ type: 'Text', text: 'broken.json  格式錯誤' }))?.props.dimColor).toBe(true)
    expect(await ui.find({ type: 'Text', text: /Old task/ })).toBeUndefined()
    await ui.press({ key: 'expand' })
    expect(await ui.find({ type: 'Text', text: '驗收' })).toBeDefined()
    expect((await ui.find({ type: 'Text', text: /^Ship v2/ }))?.text).toBe('Ship v2  A1✓ A2✗ A3·')
    await ui.press({ key: 'collapse' })
    await ui.unmount()
  }
})

test('live and visual gates count; a gate of an unknown kind is skipped without spoiling the file', async ($, on) => {
  const w = world(on)
  const agent = agentTool(on, w)
  writeTools(on)
  await start($)
  agent.background('bg-1')
  await dispatch($, { description: 'one', prompt: 'p' })
  w.file(
    'kinds.json',
    JSON.stringify({
      task: 'Kinds',
      gates: [
        { id: 'L1', kind: 'live', check: 'c', status: 'pass' },
        { id: 'V1', kind: 'visual', check: 'c', status: 'pending' },
        { id: 'X1', kind: 'telepathy', check: 'c', status: 'pass' },
        { id: 'M1', kind: 'machine', check: 'c', status: 'fail' },
      ],
    }),
  )
  w.file('bad.json', JSON.stringify({ task: 'Bad', gates: [{ id: 'B1', kind: 'live', check: 'c', status: 'maybe' }] }))
  await writeAcceptance($, 'kinds.json')
  await writeAcceptance($, 'bad.json')
  await turnDone($)
  const ui = await mountPane($, 'terminal')
  expect((await ui.find({ type: 'Text', text: /^Kinds/ }))?.text).toBe('Kinds  L1✓ V1· M1✗')
  expect(await ui.find({ type: 'Text', text: 'bad.json  格式錯誤' })).toBeDefined()
  await ui.unmount()
})

test('only the acceptance files the main loop wrote count, read by absolute path, sorted by name', async ($, on) => {
  const w = world(on)
  const agent = agentTool(on, w)
  writeTools(on)
  await start($)
  agent.background('bg-1')
  await dispatch($, { description: 'one', prompt: 'p' })
  w.file('zeta.json', GATES.replace('Ship v2', 'Zeta'))
  w.file('alpha.json', GATES.replace('Ship v2', 'Alpha'))
  w.file('other-session.json', GATES.replace('Ship v2', 'Other'))
  w.file('sub.json', GATES.replace('Ship v2', 'Sub'))
  w.file('notes.json', GATES.replace('Ship v2', 'Notes'))
  await writeAcceptance($, 'zeta.json')
  // Windows spelling, and a second write of the same file.
  await writeFile($, 'C:\\work\\.claude\\acceptance\\alpha.json')
  await writeAcceptance($, 'zeta.json')
  // Edit and NotebookEdit own a file as well (NotebookEdit names it notebook_path).
  w.file('beta.json', GATES.replace('Ship v2', 'Beta'))
  w.file('gamma.json', GATES.replace('Ship v2', 'Gamma'))
  await $.tool.call({ tool: 'Edit', file_path: `${ACCEPTANCE}beta.json`, old_string: 'a', new_string: 'b' })
  await $.tool.call({ tool: 'NotebookEdit', notebook_path: `${ACCEPTANCE}gamma.json`, new_source: 'x' })
  // Written, but gone by the time it is read.
  await writeAcceptance($, 'gone.json')
  // Not owned: a subagent's write, another folder, another extension.
  await writeAcceptance($, 'sub.json', 'agent-9')
  await writeFile($, 'C:/work/notes/notes.json')
  await writeAcceptance($, 'readme.md')
  await turnDone($)
  const ui = await mountPane($, 'terminal')
  const shown = (await ui.findAll({ type: 'Text', text: /^(Alpha|Beta|Gamma|Zeta|Other|Sub|Notes)  / })).map(t => t.text.split(' ')[0])
  expect(shown).toEqual(['Alpha', 'Beta', 'Gamma', 'Zeta'])
  expect(await ui.find({ type: 'Text', text: /格式錯誤/ })).toBeUndefined()
  expect([...new Set(w.reads)].sort()).toEqual(
    ['alpha', 'beta', 'gamma', 'gone', 'zeta'].map(name => `${ACCEPTANCE}${name}.json`),
  )
  await ui.unmount()
})

test('a write the tool refused does not own the file', async ($, on) => {
  const w = world(on)
  const agent = agentTool(on, w)
  on('tool.call', { tool: 'Write' }, () => ({ deny: 'no' }))
  await start($)
  agent.background('bg-1')
  await dispatch($, { description: 'one', prompt: 'p' })
  w.file('ship.json', GATES)
  await writeAcceptance($, 'ship.json')
  await turnDone($)
  const ui = await mountPane($, 'terminal')
  expect(await ui.find({ type: 'Text', text: /^Ship v2/ })).toBeUndefined()
  await ui.unmount()
})

test('acceptance files are read again on the 30 s tick', async ($, on) => {
  const w = world(on)
  const agent = agentTool(on, w)
  writeTools(on)
  await start($)
  agent.background('bg-1')
  await dispatch($, { description: 'one', prompt: 'p' })
  // Written, but not on disk yet: left out until it can be read.
  await writeAcceptance($, 'ship.json')
  await turnDone($)
  const ui = await mountPane($, 'terminal')
  expect(await ui.find({ type: 'Text', text: /^Ship v2/ })).toBeUndefined()
  w.file('ship.json', GATES)
  await w.clock.advance(30_000)
  expect((await ui.find({ type: 'Text', text: /^Ship v2/ }))?.text).toBe('Ship v2  A1✓ A2✗ A3·')
  await ui.unmount()
})

test('no acceptance file written: no gates and no note', async ($, on) => {
  const w = world(on)
  const agent = agentTool(on, w)
  await start($)
  agent.background('bg-1')
  await dispatch($, { description: 'one', prompt: 'p' })
  await turnDone($)
  await w.clock.advance(30_000)
  const ui = await mountPane($, 'terminal')
  expect(await ui.find({ type: 'Text', text: /格式錯誤/ })).toBeUndefined()
  await ui.press({ key: 'expand' })
  expect(await ui.find({ type: 'Text', text: '驗收' })).toBeUndefined()
  await ui.unmount()
})

// ---------------------------------------------------------------- state

// Any plugin reads any $.state value: this one answers /peek with the board's.
const READER = {
  name: 'reader',
  register(on: On) {
    on('command.run', { command: 'peek' }, async $ => ({
      text: JSON.stringify({
        rows: (await $.state.get({ plugin: 'board', key: 'rows' })).value,
        todos: (await $.state.get({ plugin: 'board', key: 'todos' })).value,
        view: (await $.state.get({ plugin: 'board', key: 'view' })).value,
        tick: (await $.state.get({ plugin: 'board', key: 'tick' })).value ?? 0,
      }),
    }))
  },
}

// The kit has no hot reload, so this shows the other half: every row the pane
// and the status line draw is read from $.state, which the host keeps across a reload.
test('what the board shows is kept in $.state', { plugins: [READER] }, async ($, on) => {
  const w = world(on)
  const agent = agentTool(on, w)
  await start($)
  agent.background('bg-1')
  await dispatch($, { description: 'kept', prompt: 'p', subagent_type: 'Explore' })
  const peek = await $.command.run({
    command: 'peek',
    args: '',
    origin: { kind: 'composer' },
    presentation: { isFullscreen: false, columns: 120 },
  })
  expect(JSON.parse(peek.text ?? '{}')).toMatchObject({
    rows: [{ description: 'kept', subagentType: 'Explore', status: 'running', agentId: 'bg-1', background: true }],
    view: { isExpanded: false, isAutoOpened: true },
  })
})

// ---------------------------------------------------------------- plan

// Stands in for state an older version left behind. A plugin's code runs apart from this
// file, so the stand-in carries its own data: a write of the board whose first row or
// step is named "OLD:..." is replaced by values without the fields added since.
const OLD_STATE = {
  name: 'old-state',
  register(on: On) {
    const row = {
      key: 'old-1',
      description: 'Old dispatch',
      subagentType: 'Explore',
      prompt: 'p',
      background: false,
      startedAt: Date.UTC(2026, 9, 16, 1, 0, 0),
      endedAt: Date.UTC(2026, 9, 16, 1, 1, 0),
      status: 'done',
      reason: null,
      agentId: null,
      isRemote: false,
    }
    const todo = { id: '7', subject: 'Old step', status: 'in_progress' }
    on('state.set', { plugin: 'board', key: 'rows' }, ($, e, next) =>
      next(e.value[0]?.description.startsWith('OLD:') ? ({ ...e, value: [row] } as typeof e) : e),
    )
    on('state.set', { plugin: 'board', key: 'todos' }, ($, e, next) =>
      next(e.value[0]?.subject.startsWith('OLD:') ? ({ ...e, value: [todo] } as typeof e) : e),
    )
  },
}
const OLD_TODO = { id: '7', subject: 'Old step', status: 'in_progress' }

type Peeked = {
  rows: { description: string; todoId: string | null }[]
  todos: {
    id: string
    subject: string
    status: string
    startedAt: number | null
    endedAt: number | null
    activeAt: number | null
  }[]
  tick: number
}
const peek = async ($: Engine) =>
  JSON.parse(
    (
      await $.command.run({
        command: 'peek',
        args: '',
        origin: { kind: 'composer' },
        presentation: { isFullscreen: false, columns: 120 },
      })
    ).text ?? '{}',
  ) as Peeked

const create = ($: Engine, subject: string) => $.tool.call({ tool: 'TaskCreate', subject, description: 'd' })
const setStatus = ($: Engine, taskId: string, status: 'pending' | 'in_progress' | 'completed') =>
  $.tool.call({ tool: 'TaskUpdate', taskId, status })
const todoWrite = ($: Engine, items: [string, 'pending' | 'in_progress' | 'completed'][]) =>
  $.tool.call({
    tool: 'TodoWrite',
    todos: items.map(([content, status]) => ({ content, status, activeForm: content })),
  })

test('plan steps are stamped when they start and finish; a reopened step keeps its first start', { plugins: [READER] }, async ($, on) => {
  const w = world(on)
  taskTools(on)
  await start($)
  await create($, 'A')
  await create($, 'B')
  expect((await peek($)).todos[0]).toMatchObject({ startedAt: null, endedAt: null, activeAt: null })
  await setStatus($, '1', 'in_progress')
  await w.clock.advance(2 * MIN)
  await setStatus($, '2', 'in_progress')
  await w.clock.advance(3 * MIN)
  await setStatus($, '1', 'completed')
  let todos = (await peek($)).todos
  expect(todos[0]).toMatchObject({ startedAt: T0, endedAt: T0 + 5 * MIN, activeAt: T0 })
  expect(todos[1]).toMatchObject({ startedAt: T0 + 2 * MIN, endedAt: null, activeAt: T0 + 2 * MIN })
  await w.clock.advance(MIN)
  await setStatus($, '1', 'in_progress')
  todos = (await peek($)).todos
  expect(todos[0]).toMatchObject({ startedAt: T0, endedAt: null, activeAt: T0 + 6 * MIN })
})

test('TodoWrite carries timestamps (and the step) over from the item with the same subject', { plugins: [READER] }, async ($, on) => {
  const w = world(on)
  taskTools(on)
  await start($)
  await todoWrite($, [['Read', 'in_progress'], ['Write', 'pending'], ['Check', 'pending']])
  expect((await peek($)).todos).toMatchObject([
    { id: 'todo-1', startedAt: T0, activeAt: T0 },
    { id: 'todo-2', startedAt: null },
    { id: 'todo-3', startedAt: null },
  ])
  await w.clock.advance(2 * MIN)
  await todoWrite($, [['Read', 'completed'], ['Write', 'in_progress'], ['Check', 'pending']])
  expect((await peek($)).todos).toMatchObject([
    { subject: 'Read', startedAt: T0, endedAt: T0 + 2 * MIN, activeAt: T0 },
    { subject: 'Write', startedAt: T0 + 2 * MIN, endedAt: null, activeAt: T0 + 2 * MIN },
    { subject: 'Check', startedAt: null, endedAt: null, activeAt: null },
  ])
  // A new item in front: the others keep their stamps and their ids, so attached rows stay attached.
  await w.clock.advance(MIN)
  await todoWrite($, [['New', 'pending'], ['Read', 'completed'], ['Write', 'in_progress'], ['Check', 'pending']])
  const todos = (await peek($)).todos
  expect(todos.map(t => t.subject)).toEqual(['New', 'Read', 'Write', 'Check'])
  expect(todos[1]).toMatchObject({ id: 'todo-1', startedAt: T0, endedAt: T0 + 2 * MIN, activeAt: T0 })
  expect(todos[2]).toMatchObject({ id: 'todo-2', startedAt: T0 + 2 * MIN, endedAt: null, activeAt: T0 + 2 * MIN })
  expect(todos[0]).toMatchObject({ startedAt: null, endedAt: null, activeAt: null })
  expect(new Set(todos.map(t => t.id)).size).toBe(4)
})

test('a dispatch attaches to the one step in progress', { plugins: [READER] }, async ($, on) => {
  const w = world(on)
  const agent = agentTool(on, w)
  taskTools(on)
  await start($)
  await create($, 'Design')
  await create($, 'Build')
  agent.background('bg-0')
  await dispatch($, { description: 'nothing running yet', prompt: 'p' })
  await setStatus($, '2', 'in_progress')
  agent.background('bg-1')
  await dispatch($, { description: 'Anything at all', prompt: 'p' })
  expect((await peek($)).rows.map(r => r.todoId)).toEqual([null, '2'])
})

test('with several steps in progress the dispatch picks the best text match, else the latest activeAt', { plugins: [READER] }, async ($, on) => {
  const w = world(on)
  const agent = agentTool(on, w)
  taskTools(on)
  await start($)
  await create($, '修復登入錯誤')
  await create($, 'Write docs')
  await create($, 'Edit docs')
  for (const id of ['1', '2', '3']) {
    await setStatus($, id, 'in_progress')
    await w.clock.advance(1000)
  }
  const send = async (description: string) => {
    agent.background(`bg-${description}`)
    await dispatch($, { description, prompt: 'p' })
  }
  await send('登入錯誤檢查') // CJK bigrams: step 1
  await send('Draft the DOCS') // lowercase bigrams, step 2 and 3 tie: the later activeAt wins
  await send('Write the docs now') // step 2 shares more than step 3
  await send('zzz qq') // nothing shared: the latest activeAt
  expect((await peek($)).rows.map(r => r.todoId)).toEqual(['1', '3', '2', '3'])
})

test('collapsed with a plan: a bar of steps (one in progress counts half), the step in progress, running agents; no minute ETA', async ($, on) => {
  const w = world(on)
  const agent = agentTool(on, w)
  taskTools(on)
  await start($)
  await create($, 'A')
  await create($, 'B')
  await create($, 'C')
  expect(w.status()).toBe('board  ░░░░░░░░░░ 0/3 步驟')
  await setStatus($, '1', 'in_progress')
  expect(w.status()).toBe('board  █▓░░░░░░░░ 0/3 步驟 · 進行中：A')
  await w.clock.advance(4 * MIN)
  await setStatus($, '1', 'completed')
  await setStatus($, '2', 'in_progress')
  expect(w.status()).toBe('board  █████░░░░░ 1/3 步驟 · 進行中：B')
  await setStatus($, '3', 'in_progress')
  expect(w.status()).toBe('board  ██████▓░░░ 1/3 步驟 · 進行中：B 等 2 項')
  agent.background('bg-1')
  await dispatch($, { description: 'B work', prompt: 'p' })
  expect(w.status()).toBe('board  ██████▓░░░ 1/3 步驟 · 進行中：B 等 2 項 · 1 agent 執行中')
  await w.clock.advance(30 * MIN)
  const ui = await mountPane($, 'terminal')
  expect(await ui.find({ type: 'Text', text: 'board  ██████▓░░░ 1/3 步驟 · 進行中：B 等 2 項 · 1 agent 執行中' })).toBeDefined()
  await ui.unmount()
  expect(w.status()).toBe('board  ██████▓░░░ 1/3 步驟 · 進行中：B 等 2 項 · 1 agent 執行中')
})

test('a plan whose steps are all done fills the bar', async ($, on) => {
  const w = world(on)
  taskTools(on)
  await start($)
  await create($, 'A')
  await setStatus($, '1', 'completed') // never started: still done
  expect(w.status()).toBe('board  ██████████ 1/1 步驟')
})

const gateFile = (task: string, statuses: ('pass' | 'fail' | 'pending')[]) =>
  JSON.stringify({
    task,
    gates: statuses.map((status, i) => ({ id: `G${i + 1}`, kind: 'machine', check: 'c', status })),
  })

test('the plan headline adds a bar of passed gates over every acceptance file; none, no bar', async ($, on) => {
  const w = world(on)
  taskTools(on)
  writeTools(on)
  await start($)
  await create($, 'A')
  await create($, 'B')
  await setStatus($, '1', 'completed')
  await turnDone($)
  expect(w.status()).toBe('board  █████░░░░░ 1/2 步驟')
  w.file('a.json', gateFile('One', ['pass', 'pass', 'pass', 'fail', 'pending']))
  w.file('b.json', gateFile('Two', ['pass', 'pass', 'pending', 'pending', 'pending']))
  w.file('c.json', '{ "task": "Half", "gates": [ { "id": 1 } ] }')
  for (const name of ['a.json', 'b.json', 'c.json']) await writeAcceptance($, name)
  await turnDone($)
  expect(w.status()).toBe('board  █████░░░░░ 1/2 步驟 · 驗收 ██▓░░ 5/10')
  const ui = await mountPane($, 'terminal')
  expect(await ui.find({ type: 'Text', text: 'board  █████░░░░░ 1/2 步驟 · 驗收 ██▓░░ 5/10' })).toBeDefined()
  await ui.unmount()
  // Only malformed files: nothing to count, so no bar.
  w.file('a.json', '[]')
  w.file('b.json', '[]')
  await turnDone($)
  expect(w.status()).toBe('board  █████░░░░░ 1/2 步驟')
})

const humanFile = (task: string, gates: [string, 'pass' | 'fail' | 'pending', 'human' | 'machine'][]) =>
  JSON.stringify({ task, gates: gates.map(([id, status, kind]) => ({ id, kind, check: 'c', status })) })

test('the plan headline names the pending human gate, and how many wait when several', async ($, on) => {
  const w = world(on)
  taskTools(on)
  writeTools(on)
  await start($)
  await create($, 'A')
  await create($, 'B')
  w.file(
    'h.json',
    humanFile('H', [
      ['H1', 'pending', 'human'],
      ['H2', 'pending', 'human'],
      ['H3', 'pass', 'human'],
      ['M1', 'pending', 'machine'],
    ]),
  )
  await writeAcceptance($, 'h.json')
  await turnDone($)
  expect(w.status()).toBe('board  ░░░░░░░░░░ 0/2 步驟 · 驗收 █▓░░░ 1/4 · 等你：H1 等 2 項')
  w.file('h.json', humanFile('H', [['H1', 'pass', 'human'], ['H2', 'pending', 'human']]))
  await turnDone($)
  expect(w.status()).toBe('board  ░░░░░░░░░░ 0/2 步驟 · 驗收 ██▓░░ 1/2 · 等你：H2')
  w.file('h.json', humanFile('H', [['H1', 'pass', 'human'], ['H2', 'fail', 'human'], ['M1', 'pending', 'machine']]))
  await turnDone($)
  expect(w.status()).toBe('board  ░░░░░░░░░░ 0/2 步驟 · 驗收 █▓░░░ 1/3')
})

test('the headline without a plan names the pending human gate too', async ($, on) => {
  const w = world(on)
  const agent = agentTool(on, w)
  writeTools(on)
  await start($)
  agent.background('bg-1')
  await dispatch($, { description: 'one', prompt: 'p' })
  w.file('h.json', humanFile('H', [['H1', 'pending', 'human'], ['H2', 'pending', 'human']]))
  await writeAcceptance($, 'h.json')
  await turnDone($)
  expect(w.status()).toBe('board  ░░░░░░░░░░ 0/1 完成 · 1 執行中 · 等你：H1 等 2 項')
})

test('gates alone: the status line and both pane summaries lead with the gates', async ($, on) => {
  const w = world(on)
  writeTools(on)
  await start($)
  expect(w.status()).toBeUndefined()
  const empty = await mountPane($, 'terminal')
  expect(await empty.find({ type: 'Text', text: 'board  尚無派工' })).toBeDefined()
  await empty.unmount()
  w.file('h.json', humanFile('H', [['H1', 'pending', 'human'], ['M1', 'pass', 'machine']]))
  await writeAcceptance($, 'h.json')
  await turnDone($)
  const headline = 'board  驗收 ██▓░░ 1/2 · 等你：H1'
  expect(w.status()).toBe(headline)
  const ui = await mountPane($, 'terminal')
  expect(await ui.find({ type: 'Text', text: headline })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /尚無派工/ })).toBeUndefined()
  await ui.press({ key: 'expand' })
  expect(await ui.find({ type: 'Text', text: headline })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /尚無派工/ })).toBeUndefined()
  await ui.unmount()
})

test('in the pane a pending human gate is warning-colored; a settled human gate stays dim', async ($, on) => {
  const w = world(on)
  const agent = agentTool(on, w)
  writeTools(on)
  await start($)
  agent.background('bg-1')
  await dispatch($, { description: 'one', prompt: 'p' })
  w.file('h.json', humanFile('H', [['H1', 'pending', 'human'], ['H2', 'pass', 'human'], ['M1', 'pending', 'machine']]))
  await writeAcceptance($, 'h.json')
  await turnDone($)
  const ui = await mountPane($, 'terminal')
  const pending = await ui.find({ type: 'Text', text: /^H1·$/ })
  expect(pending?.props.color).toBe('warning')
  expect(pending?.props.dimColor).toBeFalsy()
  const settled = await ui.find({ type: 'Text', text: /^H2✓$/ })
  expect(settled?.props.dimColor).toBe(true)
  expect(settled?.props.color).toBeUndefined()
  const machine = await ui.find({ type: 'Text', text: /^M1·$/ })
  expect(machine?.props.color).toBeUndefined()
  expect(machine?.props.dimColor).toBeFalsy()
  await ui.unmount()
})

test('without a plan the bar counts the dispatches done', async ($, on) => {
  const w = world(on)
  const agent = agentTool(on, w)
  await start($)
  agent.foreground(MIN, 'fg-1')
  const first = dispatch($, { description: 'one', prompt: 'p' })
  await w.clock.advance(MIN)
  await first
  agent.background('bg-1')
  await dispatch($, { description: 'two', prompt: 'p' })
  expect(w.status()).toBe('board  █████░░░░░ 1/2 完成 · 1 執行中')
})

test('the 30 s tick runs for a foreground dispatch too, and does not poll the agent list for it', { plugins: [READER] }, async ($, on) => {
  const w = world(on)
  const agent = agentTool(on, w)
  await start($)
  agent.foreground(100_000, 'fg-1')
  const call = dispatch($, { description: 'Slow', prompt: 'p' })
  await w.clock.settle()
  const ui = await mountPane($, 'terminal')
  await ui.press({ key: 'expand' })
  expect((await peek($)).tick).toBe(0)
  await w.clock.advance(90_000)
  expect((await peek($)).tick).toBe(T0 + 90_000)
  expect((await ui.find({ type: 'Button', text: /Slow/ }))?.text).toMatch(/Slow +\S+ +\S+ +1 分/)
  expect(w.listCalls()).toBe(0)
  await w.clock.advance(10_000)
  await call
  // Nothing runs any more: the timer is gone.
  await w.clock.advance(5 * MIN)
  expect((await peek($)).tick).toBe(T0 + 90_000)
  await ui.unmount()
})

test('the 30 s tick runs while a plan step is in progress, with no dispatch at all', { plugins: [READER] }, async ($, on) => {
  const w = world(on)
  taskTools(on)
  await start($)
  await create($, 'A')
  await w.clock.advance(2 * MIN)
  // A step still pending: nothing runs.
  expect((await peek($)).tick).toBe(0)
  await setStatus($, '1', 'in_progress')
  await w.clock.advance(90_000)
  expect((await peek($)).tick).toBe(T0 + 2 * MIN + 90_000)
  expect(w.listCalls()).toBe(0)
  await setStatus($, '1', 'completed')
  await w.clock.advance(5 * MIN)
  expect((await peek($)).tick).toBe(T0 + 2 * MIN + 90_000)
})

test('the pane opens by itself, collapsed, when the plan first gets items, once', async ($, on) => {
  const w = world(on)
  const agent = agentTool(on, w)
  taskTools(on)
  await start($)
  await create($, 'A')
  expect(w.opened).toHaveLength(1)
  expect(w.opened[0]).toMatchObject({ id: 'board', rows: 2 })
  expect(w.opened[0]?.focus).toBeUndefined()
  await create($, 'B')
  agent.background('bg-1')
  await dispatch($, { description: 'one', prompt: 'p' })
  expect(w.opened).toHaveLength(1)
})

test('the pane opens by itself at the first TodoWrite too', async ($, on) => {
  const w = world(on)
  taskTools(on)
  await start($)
  await todoWrite($, [['A', 'pending']])
  expect(w.opened).toHaveLength(1)
  expect(w.opened[0]).toMatchObject({ id: 'board', rows: 2 })
})

test('expanded with a plan: numbered steps, their dispatches beneath, then the other dispatches', async ($, on) => {
  const w = world(on)
  const agent = agentTool(on, w)
  taskTools(on)
  await start($)
  agent.background('bg-0')
  await dispatch($, { description: 'Early scout', prompt: 'scout prompt', subagent_type: 'Explore' })
  await create($, 'Design')
  await create($, 'Build')
  await setStatus($, '1', 'completed')
  await setStatus($, '2', 'in_progress')
  agent.background('bg-1')
  await dispatch($, { description: 'Build parts', prompt: 'parts prompt', subagent_type: 'implementer' })
  const ui = await mountPane($, 'terminal')
  await ui.press({ key: 'expand' })
  const shown = (await ui.findAll({ text: /Design|Build|Early|其他派工/ })).filter(n => n.type === 'Text' || n.type === 'Button')
  expect(shown.map(n => n.text.replace(/ +/g, ' '))).toEqual([
    'board ███████▓░░ 1/2 步驟 · 進行中：Build · 2 agent 執行中',
    '✓ 1 Design',
    '● 2 Build',
    ` └ ● Build parts implementer ${hhmm(T0)} <1 分 —`,
    '其他派工',
    `● Early scout Explore ${hhmm(T0)} <1 分 —`,
  ])
  expect(shown[1]?.props.dimColor).toBe(true)
  expect(shown[2]?.props.dimColor).toBeFalsy()
  const attached = await ui.find({ type: 'Button', text: /Build parts/ })
  expect(attached?.text.startsWith('   └ ')).toBe(true)
  await ui.press({ key: attached?.key ?? '' })
  expect((await ui.find({ type: 'Button', key: 'back' }))?.text).toBe('返回')
  expect(await ui.find({ type: 'Text', text: 'parts prompt' })).toBeDefined()
  await ui.press({ key: 'back' })
  expect(await ui.find({ type: 'Button', text: /Build parts/ })).toBeDefined()
  await ui.unmount()
})

test('no 其他派工 label while every dispatch is attached', async ($, on) => {
  const w = world(on)
  const agent = agentTool(on, w)
  taskTools(on)
  await start($)
  await create($, 'Build')
  await setStatus($, '1', 'in_progress')
  agent.background('bg-1')
  await dispatch($, { description: 'Build parts', prompt: 'p' })
  const ui = await mountPane($, 'desktop')
  await ui.press({ key: 'expand' })
  expect(await ui.find({ type: 'Text', text: '其他派工' })).toBeUndefined()
  expect(await ui.find({ type: 'Button', text: /Build parts/ })).toBeDefined()
  await ui.unmount()
})

test('state written by an older version (no new fields) is read as null, not a crash', { plugins: [READER, OLD_STATE] }, async ($, on) => {
  const w = world(on)
  const agent = agentTool(on, w)
  taskTools(on)
  await start($)
  await create($, 'OLD: becomes the old step')
  agent.background('bg-0')
  await dispatch($, { description: 'OLD: becomes the old dispatch', prompt: 'p' })
  expect((await peek($)).todos).toEqual([OLD_TODO])
  agent.background('bg-1')
  await dispatch($, { description: 'New dispatch', prompt: 'p' })
  expect((await peek($)).rows.map(r => r.todoId)).toEqual([null, '7'])
  expect(w.status()).toBe('board  █████░░░░░ 0/1 步驟 · 進行中：Old step · 1 agent 執行中')
  await setStatus($, '7', 'completed')
  expect((await peek($)).todos[0]).toMatchObject({ status: 'completed', startedAt: null, endedAt: T0, activeAt: null })
  const ui = await mountPane($, 'terminal')
  await ui.press({ key: 'expand' })
  expect(await checklistOf(ui)).toEqual(['✓ 1  Old step'])
  expect(await ui.find({ type: 'Button', text: /Old dispatch/ })).toBeDefined()
  await ui.unmount()
})

// ---------------------------------------------------------------- /clear

const endSession = ($: Engine, reason: 'clear' | 'resume' | 'logout' | 'prompt_input_exit' | 'other') =>
  $.session.end({ reason, sessionId: 's-1', resume: { id: 's-1' } })

test('/clear empties the board, gates and status line, and lets the pane open again', { plugins: [READER] }, async ($, on) => {
  const w = world(on)
  const agent = agentTool(on, w)
  taskTools(on)
  writeTools(on)
  await start($)
  await create($, 'A')
  await setStatus($, '1', 'in_progress')
  agent.background('bg-1')
  await dispatch($, { description: 'one', prompt: 'p' })
  w.file('ship.json', GATES)
  await writeAcceptance($, 'ship.json')
  await w.clock.advance(30_000)
  expect(w.opened).toHaveLength(1)
  const listed = w.listCalls()
  const before = await mountPane($, 'terminal')
  expect(await before.find({ type: 'Text', text: /^Ship v2/ })).toBeDefined()
  await before.unmount()
  await endSession($, 'clear')
  expect(w.status()).toBeUndefined()
  expect(await peek($)).toMatchObject({ rows: [], todos: [], tick: 0, view: { isAutoOpened: false, isExpanded: false, selected: null } })
  // The poll timer is gone.
  await w.clock.advance(5 * MIN)
  expect(w.listCalls()).toBe(listed)
  // The file the old conversation wrote is no longer the board's, even though it is still on disk.
  const after = await mountPane($, 'terminal')
  expect(await after.find({ type: 'Text', text: /^Ship v2/ })).toBeUndefined()
  await after.unmount()
  // The next plan opens the pane again.
  await create($, 'B')
  expect(w.opened).toHaveLength(2)
})

test('other session end reasons change nothing', { plugins: [READER] }, async ($, on) => {
  const w = world(on)
  const agent = agentTool(on, w)
  await start($)
  agent.background('bg-1')
  await dispatch($, { description: 'one', prompt: 'p' })
  for (const reason of ['resume', 'logout', 'prompt_input_exit', 'other'] as const) await endSession($, reason)
  expect(w.status()).toBe('board  ░░░░░░░░░░ 0/1 完成 · 1 執行中')
  expect((await peek($)).rows).toHaveLength(1)
})
