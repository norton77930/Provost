import { atom, read, update } from 'claude-code'
import type { AgentStatus, EngineInterface, Register, Timer, ToolCallResult } from 'claude-code'

import type { BoardGateFile, BoardRow, BoardStatus, BoardTodo, BoardView } from '../types'

const PANE = 'board'
const MIN = 60_000
const POLL_MS = 30_000
// Finished rows of a role it takes before a running one gets an estimate.
const MIN_SAMPLES = 3
// Bars, in cells: plan steps (and dispatches done) in the headline, gates beside them, one running row's time.
const STEP_BAR_W = 10
const GATE_BAR_W = 5
const ROW_BAR_W = 8
const BAR_FULL = '█'
const BAR_HALF = '▓'
const BAR_EMPTY = '░'
const COLLAPSED_ROWS = 2
const EXPANDED_ROWS = 20
// Relative to the session's working directory, as $.fs resolves it.
const ACCEPTANCE_DIR = '.claude/acceptance'

// Everything the board shows lives in $.state: it survives a hot reload of this module.
const rows = atom({ plugin: 'board', key: 'rows' } as const, [])
const todos = atom({ plugin: 'board', key: 'todos' } as const, [])
const gates = atom({ plugin: 'board', key: 'gates' } as const, [])
const tick = atom({ plugin: 'board', key: 'tick' } as const, 0)
const INITIAL_VIEW: BoardView = { isExpanded: false, selected: null, isAutoOpened: false }
const view = atom({ plugin: 'board', key: 'view' } as const, INITIAL_VIEW)

// State written by an older version of this module lacks the fields added since:
// every read fills them in as null instead of trusting the stored shape.
const normRow = (r: BoardRow): BoardRow => ({ ...r, todoId: r.todoId ?? null })
const normTodo = (t: BoardTodo): BoardTodo => ({
  ...t,
  startedAt: t.startedAt ?? null,
  endedAt: t.endedAt ?? null,
  activeAt: t.activeAt ?? null,
})
const loadRows = async ($: EngineInterface) => (await read($, rows)).map(normRow)
const loadTodos = async ($: EngineInterface) => (await read($, todos)).map(normTodo)
const changeRows = ($: EngineInterface, change: (list: BoardRow[]) => BoardRow[]) =>
  update($, rows, list => change(list.map(normRow)))
const changeTodos = ($: EngineInterface, change: (list: BoardTodo[]) => BoardTodo[]) =>
  update($, todos, list => change(list.map(normTodo)))

// The poll timer is the one module variable: a reload cancels it with the old
// environment, and the next event this module hears starts it again.
let poll: Timer | undefined

// ---------------------------------------------------------------- pure helpers

const isActive = (row: BoardRow) => row.status === 'running' || row.status === 'waiting'

const median = (values: number[]) => {
  if (values.length === 0) return undefined
  const sorted = [...values].sort((a, b) => a - b)
  const mid = Math.floor(sorted.length / 2)
  return sorted.length % 2 === 1 ? (sorted[mid] ?? 0) : ((sorted[mid - 1] ?? 0) + (sorted[mid] ?? 0)) / 2
}

const durationsOf = (list: BoardRow[]) =>
  list.filter(r => r.status === 'done' && !r.isRemote && r.endedAt !== null).map(r => (r.endedAt ?? 0) - r.startedAt)

// The median duration of the finished rows of an active row's role: how long a run of
// it takes. undefined below MIN_SAMPLES of them; other roles' rows are no stand-in.
function estimateMs(row: BoardRow, list: BoardRow[]) {
  const spans = durationsOf(list.filter(r => r.subagentType === row.subagentType))
  return spans.length < MIN_SAMPLES ? undefined : median(spans)
}

// Milliseconds an active row has left of its estimate, negative once it has overrun.
const remainingMs = (row: BoardRow, list: BoardRow[], now: number) => {
  const estimate = estimateMs(row, list)
  return estimate === undefined ? undefined : estimate - (now - row.startedAt)
}

// The active rows run side by side, so the wait is the longest left, not the sum. Rows
// past their estimate add nothing. undefined with no active row, one without an estimate,
// or all of them past it.
function longestRemainingMs(list: BoardRow[], now: number) {
  const active = list.filter(isActive)
  if (active.length === 0) return undefined
  let longest = 0
  for (const row of active) {
    const left = remainingMs(row, list, now)
    if (left === undefined) return undefined
    longest = Math.max(longest, left)
  }
  return longest > 0 ? longest : undefined
}

const minutes = (ms: number) => Math.max(1, Math.round(ms / MIN))

// `value` (0 to 1) as `width` cells, to the half cell: full, one half, empty.
function barOf(value: number, width: number) {
  const halves = Math.round((value > 0 ? Math.min(1, value) : 0) * width * 2)
  const full = Math.floor(halves / 2)
  const half = halves % 2
  return BAR_FULL.repeat(full) + BAR_HALF.repeat(half) + BAR_EMPTY.repeat(width - full - half)
}

const ratio = (part: number, whole: number) => (whole === 0 ? 0 : part / whole)

function summaryOf(list: BoardRow[], now: number) {
  const done = list.filter(r => r.status === 'done').length
  const active = list.filter(isActive).length
  const failed = list.filter(r => r.status === 'failed').length
  const eta = longestRemainingMs(list, now)
  return (
    `board  ${barOf(ratio(done, list.length), STEP_BAR_W)} ${done}/${list.length} 完成 · ${active} 執行中` +
    (failed > 0 ? ` · ${failed} 失敗` : '') +
    (eta === undefined ? '' : ` · ~${minutes(eta)} 分`)
  )
}

const oneLine = (s: string) => s.replace(/\s+/g, ' ').trim()

// The step as a status change leaves it: first start, last activation, completion.
// The same status again changes nothing.
function withStatus(t: BoardTodo, status: BoardTodo['status'], now: number): BoardTodo {
  if (t.status === status) return t
  return {
    ...t,
    status,
    startedAt: status === 'in_progress' ? (t.startedAt ?? now) : t.startedAt,
    activeAt: status === 'in_progress' ? now : t.activeAt,
    endedAt: status === 'completed' ? now : null,
  }
}

// Distinct pairs of neighbouring characters of each word: works for CJK and English alike.
function bigramsOf(s: string) {
  const out = new Set<string>()
  for (const word of s.toLowerCase().split(/\s+/)) {
    const chars = [...word]
    for (let i = 0; i + 1 < chars.length; i++) out.add(`${chars[i]}${chars[i + 1]}`)
  }
  return out
}

// The plan step a dispatch belongs to: the one in progress; of several, the best text
// match to the description, ties and no match going to the latest activeAt; else null.
function linkedTodo(plan: BoardTodo[], description: string) {
  const open = plan.filter(t => t.status === 'in_progress')
  if (open.length <= 1) return open[0]?.id ?? null
  const wanted = bigramsOf(description)
  const scored = open.map(t => ({ t, score: [...bigramsOf(t.subject)].filter(g => wanted.has(g)).length }))
  const best = Math.max(...scored.map(x => x.score))
  const pool = best > 0 ? scored.filter(x => x.score === best) : scored
  return pool.reduce((a, b) => ((b.t.activeAt ?? -1) > (a.t.activeAt ?? -1) ? b : a)).t.id
}

// The plan as a bar of its steps (one in progress counts half) with the gates passed
// beside it, no minute ETA: step durations are too few and too unlike to promise one.
function planSummaryOf(plan: BoardTodo[], list: BoardRow[], files: BoardGateFile[]) {
  const running = plan.filter(t => t.status === 'in_progress')
  const completed = plan.filter(t => t.status === 'completed').length
  const active = list.filter(isActive).length
  const all = files.flatMap(f => f.gates)
  const passed = all.filter(g => g.status === 'pass').length
  return (
    `board  ${barOf(ratio(completed + running.length / 2, plan.length), STEP_BAR_W)} ${completed}/${plan.length} 步驟` +
    (running.length === 0
      ? ''
      : ` · 進行中：${oneLine(running[0]?.subject ?? '')}${running.length > 1 ? ` 等 ${running.length} 項` : ''}`) +
    (all.length === 0 ? '' : ` · 驗收 ${barOf(ratio(passed, all.length), GATE_BAR_W)} ${passed}/${all.length}`) +
    (active > 0 ? ` · ${active} agent 執行中` : '')
  )
}

// The status line and the collapsed pane: the plan leads when there is one.
const headlineOf = (list: BoardRow[], plan: BoardTodo[], files: BoardGateFile[], now: number) =>
  plan.length > 0 ? planSummaryOf(plan, list, files) : summaryOf(list, now)

const pad2 = (n: number) => String(n).padStart(2, '0')
const clockTime = (ms: number) => {
  const d = new Date(ms)
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`
}
const elapsedText = (ms: number) => (ms < MIN ? '<1 分' : `${Math.floor(ms / MIN)} 分`)
const elapsedOf = (row: BoardRow, now: number) => (row.endedAt ?? now) - row.startedAt

const ICON: Record<BoardStatus, string> = { done: '✓', running: '●', waiting: '○', failed: '✗' }
const LABEL: Record<BoardStatus, string> = { done: '完成', running: '執行中', waiting: '等待中', failed: '失敗' }
const TODO_ICON: Record<BoardTodo['status'], string> = { completed: '✓', in_progress: '●', pending: '○' }
const GATE_MARK = { pass: '✓', fail: '✗', pending: '·' } as const

// Terminal cells of a string: CJK and other wide glyphs take two.
const cellsOf = (s: string) => [...s].reduce((n, c) => n + ((c.codePointAt(0) ?? 0) >= 0x2e80 ? 2 : 1), 0)
// The string cut or padded to exactly `width` cells.
function fit(s: string, width: number) {
  const flat = s.replace(/\s+/g, ' ')
  if (cellsOf(flat) <= width) return flat + ' '.repeat(width - cellsOf(flat))
  let out = ''
  for (const c of flat) {
    if (cellsOf(out + c) > width - 1) break
    out += c
  }
  return out + '…' + ' '.repeat(Math.max(0, width - 1 - cellsOf(out)))
}

// Column widths of the table, in cells, beside the task's own.
const ROLE_W = 18
const TIME_W = 7
const ELAPSED_W = 7
// Room for `超時 +99 分`, the widest the column gets.
const LEFT_W = 12
// Indents a dispatch under the plan step it is attached to.
const STEP_PREFIX = '   └ '

// The 剩餘 column of an active row: elapsed over the estimate as a bar, the overrun in
// minutes (rounded up) once past it, '—' with no estimate.
function leftTextOf(row: BoardRow, list: BoardRow[], now: number) {
  if (!isActive(row)) return ''
  const estimate = estimateMs(row, list)
  if (estimate === undefined) return '—'
  const ran = now - row.startedAt
  return ran > estimate ? `超時 +${Math.ceil((ran - estimate) / MIN)} 分` : barOf(ran / estimate, ROW_BAR_W)
}

function rowText(row: BoardRow, list: BoardRow[], now: number, columns: number, prefix = '') {
  const taskW = Math.max(10, columns - 2 - cellsOf(prefix) - ROLE_W - TIME_W - ELAPSED_W - LEFT_W - 1)
  const leftText = leftTextOf(row, list, now)
  return (
    prefix +
    `${ICON[row.status]} ` +
    fit(row.description, taskW) +
    ' ' +
    fit(row.subagentType, ROLE_W) +
    fit(clockTime(row.startedAt), TIME_W) +
    fit(elapsedText(elapsedOf(row, now)), ELAPSED_W) +
    leftText
  ).trimEnd()
}

// AgentStatus as a row's status.
const statusOfAgent = (status: AgentStatus): BoardStatus =>
  status === 'completed'
    ? 'done'
    : status === 'failed' || status === 'killed'
      ? 'failed'
      : status === 'running'
        ? 'running'
        : 'waiting'

const reasonOfAgent = (status: AgentStatus) =>
  status === 'killed' ? '已停止' : status === 'failed' ? '執行失敗' : null

// The row as the Agent call's answer leaves it.
function settle(row: BoardRow, ran: ToolCallResult<'Agent'>, now: number): BoardRow {
  if (ran.deny !== undefined) return { ...row, status: 'failed', reason: `已拒絕：${ran.deny}`, endedAt: now }
  if (ran.isError === true) return { ...row, status: 'failed', reason: ran.text || '派工失敗', endedAt: now }
  const result = ran.result as Record<string, unknown> | undefined
  const agentId = typeof result?.agentId === 'string' ? result.agentId : null
  const status = typeof result?.status === 'string' ? result.status : undefined
  if (status === 'remote_launched') {
    return { ...row, status: 'done', reason: '已在雲端啟動（不再追蹤）', endedAt: now, isRemote: true, background: true }
  }
  if (status === 'async_launched' || status === 'teammate_spawned' || (status === undefined && agentId !== null)) {
    // Launched in the background: its turn.complete or the poll ends it.
    return { ...row, agentId, background: true, status: row.status === 'waiting' ? 'waiting' : 'running' }
  }
  // A foreground call returns when the agent has finished.
  return { ...row, agentId, status: 'done', endedAt: now }
}

let serial = 0
const keyOf = (id: string | undefined, now: number) => id || `agent-${now}-${++serial}`

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v)
const isOneOf = <T extends string>(v: unknown, values: readonly T[]): v is T =>
  typeof v === 'string' && (values as readonly string[]).includes(v)
const isOptionalString = (v: unknown) => v === undefined || typeof v === 'string'
const GATE_KINDS = ['machine', 'review', 'human'] as const
const GATE_STATUSES = ['pass', 'fail', 'pending'] as const
const TODO_STATUSES = ['pending', 'in_progress', 'completed'] as const

// One acceptance file read against its schema; `task: null` when it does not fit.
function parseGates(file: string, text: string | undefined): BoardGateFile {
  const malformed: BoardGateFile = { file, task: null, gates: [] }
  if (text === undefined) return malformed
  let data: unknown
  try {
    data = JSON.parse(text)
  } catch {
    return malformed
  }
  if (!isRecord(data) || typeof data.task !== 'string' || !Array.isArray(data.gates)) return malformed
  const list: BoardGateFile['gates'] = []
  for (const g of data.gates as unknown[]) {
    if (
      !isRecord(g) ||
      typeof g.id !== 'string' ||
      typeof g.check !== 'string' ||
      !isOneOf(g.kind, GATE_KINDS) ||
      !isOneOf(g.status, GATE_STATUSES) ||
      !isOptionalString(g.ranAt) ||
      !isOptionalString(g.evidence)
    ) {
      return malformed
    }
    list.push({ id: g.id, kind: g.kind, status: g.status })
  }
  return { file, task: data.task, gates: list }
}

const isAnswered = (ran: ToolCallResult) => ran.deny === undefined && ran.isError !== true

// ---------------------------------------------------------------- state and effects

async function show($: EngineInterface) {
  const list = await loadRows($)
  const plan = await loadTodos($)
  if (list.length === 0 && plan.length === 0) return
  $.ui.status(headlineOf(list, plan, await read($, gates), await $.clock.now()))
}

// One 30 s tick: the agent list for the rows that have an agent, then the clock,
// the acceptance files and the status line for every board that is timing something.
async function pollOnce($: EngineInterface) {
  const now = await $.clock.now()
  const hasAgents = (await loadRows($)).some(r => isActive(r) && r.agentId !== null)
  const agents = hasAgents ? await $.agent.list().catch(() => undefined) : undefined
  if (agents !== undefined) {
    await changeRows($, list =>
      list.map(row => {
        if (!isActive(row) || row.agentId === null) return row
        const info = agents.find(a => a.id === row.agentId)
        if (info === undefined) return row
        const status = statusOfAgent(info.status)
        if (status === row.status) return row
        const ended = status === 'done' || status === 'failed'
        return { ...row, status, reason: reasonOfAgent(info.status), endedAt: ended ? now : null }
      }),
    )
  }
  await update($, tick, () => now)
  await readGates($).catch(() => undefined)
  await sync($)
}

// The acceptance files modified since the session began, by name. No directory
// (or one $.fs cannot list) shows nothing.
async function readGates($: EngineInterface) {
  const entries = await $.fs.list(ACCEPTANCE_DIR).catch(() => undefined)
  const found: BoardGateFile[] = []
  if (entries !== undefined) {
    const since = (await $.session.usage().catch(() => undefined))?.startedAt ?? 0
    const files = entries
      .filter(f => f.kind === 'file' && f.name.endsWith('.json') && f.mtimeMs >= since)
      .sort((a, b) => a.name.localeCompare(b.name))
    for (const f of files) {
      const text = await $.fs.read(`${ACCEPTANCE_DIR}/${f.name}`).catch(() => undefined)
      found.push(parseGates(f.name, typeof text === 'string' ? text : undefined))
    }
  }
  const before = await read($, gates)
  if (JSON.stringify(before) !== JSON.stringify(found)) await update($, gates, () => found)
}

async function createTodo($: EngineInterface, id: string, subject: string) {
  const item: BoardTodo = { id, subject, status: 'pending', startedAt: null, endedAt: null, activeAt: null }
  await changeTodos($, list => [...list.filter(t => t.id !== id), item])
}

async function updateTodo($: EngineInterface, id: string, change: { subject?: string; status?: string }) {
  const status = isOneOf(change.status, TODO_STATUSES) ? change.status : undefined
  const now = await $.clock.now()
  await changeTodos($, list =>
    change.status === 'deleted'
      ? list.filter(t => t.id !== id)
      : list.map(t => {
          if (t.id !== id) return t
          const renamed = { ...t, subject: change.subject ?? t.subject }
          return status === undefined ? renamed : withStatus(renamed, status, now)
        }),
  )
}

// TodoWrite hands over the whole list: an item with the subject of an earlier one keeps
// that item's id and stamps, so the dispatches attached to it stay attached.
async function replaceTodos($: EngineInterface, items: { content: string; status: BoardTodo['status'] }[]) {
  const now = await $.clock.now()
  await changeTodos($, before => {
    const taken = new Set<number>()
    const matched = items.map(item => {
      const at = before.findIndex((t, i) => !taken.has(i) && t.subject === item.content)
      if (at >= 0) taken.add(at)
      return before[at]
    })
    // Fresh ids avoid every id of the list being replaced.
    const used = new Set(before.map(t => t.id))
    return items.map((item, i) => {
      let id = matched[i]?.id
      if (id === undefined) {
        let n = i + 1
        while (used.has(`todo-${n}`)) n++
        id = `todo-${n}`
        used.add(id)
      }
      const base = matched[i] ?? { id, subject: item.content, status: 'pending' as const, startedAt: null, endedAt: null, activeAt: null }
      return withStatus(base, item.status, now)
    })
  })
}

// Ticks every 30 s while a row is active or a plan step is in progress, foreground ones
// included, and not otherwise; refreshes the status line.
async function sync($: EngineInterface) {
  const list = await loadRows($)
  const plan = await loadTodos($)
  const needsPoll = list.some(isActive) || plan.some(t => t.status === 'in_progress')
  if (needsPoll && poll === undefined) {
    poll = $.clock.every(POLL_MS, () => void pollOnce($).catch(() => undefined))
  } else if (!needsPoll && poll !== undefined) {
    poll.cancel()
    poll = undefined
  }
  await show($)
}

async function endAgent($: EngineInterface, agentId: string, reason: string) {
  const now = await $.clock.now()
  await changeRows($, list =>
    list.map(row =>
      row.agentId === agentId && isActive(row)
        ? reason === 'answer'
          ? { ...row, status: 'done', endedAt: now, reason: null }
          : { ...row, status: 'failed', endedAt: now, reason: reason === 'aborted' ? '已中斷' : '回合以錯誤結束' }
        : row,
    ),
  )
  await sync($)
}

async function wakeAgent($: EngineInterface, agentId: string) {
  await changeRows($, list =>
    list.map(row => (row.agentId === agentId && row.status === 'waiting' ? { ...row, status: 'running' } : row)),
  )
  await sync($)
}

const openPane = ($: EngineInterface, isExpanded: boolean) =>
  $.ui.open({ id: PANE, title: 'board', rows: isExpanded ? EXPANDED_ROWS : COLLAPSED_ROWS })

// The first dispatch or plan step of the session opens the pane, collapsed, without the keyboard.
async function autoOpen($: EngineInterface) {
  const before = await read($, view)
  if (before.isAutoOpened) return
  await update($, view, v => ({ ...v, isAutoOpened: true, isExpanded: false, selected: null }))
  await openPane($, false)
}

// Re-opening the open id asks for the other height (each open sets `rows` anew).
async function setExpanded($: EngineInterface, isExpanded: boolean) {
  await update($, view, v => ({ ...v, isExpanded, selected: null }))
  await openPane($, isExpanded)
}

async function select($: EngineInterface, key: string | null) {
  await update($, view, v => ({ ...v, selected: key }))
}

// /board: closes a drawn pane; opens (or places a waiting one) at the size last chosen.
async function toggle($: EngineInterface) {
  const pane = (await $.ui.panes()).find(p => p.id === PANE)
  if (pane?.isPlaced) {
    await $.ui.close({ id: PANE })
    return
  }
  await openPane($, (await read($, view)).isExpanded)
}

// ---------------------------------------------------------------- register

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command
      .register({ name: 'board', description: '任務看板：開啟或關閉 board 面板', immediate: true })
      .catch(() => undefined)
    await sync($).catch(() => undefined)
    return next(e)
  })

  on('tool.call', { tool: 'Agent' }, async ($, e, next) => {
    const now = await $.clock.now()
    const loose = e as { run_in_background?: unknown }
    const todoId = linkedTodo(await loadTodos($), e.description)
    const row: BoardRow = {
      key: keyOf(e.tool_use_id, now),
      description: e.description,
      subagentType: e.subagent_type || 'general-purpose',
      prompt: e.prompt,
      background: loose.run_in_background === true,
      startedAt: now,
      endedAt: null,
      status: 'running',
      reason: null,
      agentId: null,
      isRemote: false,
      todoId,
    }
    await changeRows($, list => [...list, row])
    await sync($)
    await autoOpen($).catch(() => undefined)
    const ran = await next(e)
    const at = await $.clock.now()
    await changeRows($, list => list.map(r => (r.key === row.key ? settle(r, ran, at) : r)))
    await sync($)
    return ran
  }).catch(($, e, next) => next(e))

  on('turn.start', async ($, e, next) => {
    if (e.agentId !== undefined) await wakeAgent($, e.agentId).catch(() => undefined)
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    // Gates first: the status line the sync draws counts them.
    await readGates($).catch(() => undefined)
    if (e.agentId !== undefined) await endAgent($, e.agentId, e.reason).catch(() => undefined)
    else await sync($).catch(() => undefined)
    return next(e)
  })

  // The model's plan. The task list is the session's (any loop); TodoWrite is
  // per loop, so only the main loop's replaces the list.
  on('tool.call', { tool: 'TaskCreate' }, async ($, e, next) => {
    const ran = await next(e)
    const task = isAnswered(ran) && isRecord(ran.result) ? ran.result.task : undefined
    if (isRecord(task) && typeof task.id === 'string') {
      await createTodo($, task.id, typeof task.subject === 'string' ? task.subject : e.subject)
      await sync($).catch(() => undefined)
      await autoOpen($).catch(() => undefined)
    }
    return ran
  }).catch(($, e, next) => next(e))

  on('tool.call', { tool: 'TaskUpdate' }, async ($, e, next) => {
    const ran = await next(e)
    const isDone = isAnswered(ran) && !(isRecord(ran.result) && ran.result.success === false)
    if (isDone) {
      await updateTodo($, e.taskId, { subject: e.subject, status: e.status })
      await sync($).catch(() => undefined)
    }
    return ran
  }).catch(($, e, next) => next(e))

  on('tool.call', { tool: 'TodoWrite' }, async ($, e, next) => {
    const ran = await next(e)
    if (e.agentId === undefined && isAnswered(ran)) {
      const after = isRecord(ran.result) && Array.isArray(ran.result.newTodos) ? ran.result.newTodos : e.todos
      await replaceTodos($, after)
      await sync($).catch(() => undefined)
      if (after.length > 0) await autoOpen($).catch(() => undefined)
    }
    return ran
  }).catch(($, e, next) => next(e))

  on('command.run', { command: 'board' }, async $ => {
    await toggle($)
    return {}
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Button, Text } = $.ui.resolve(e)
    const list = await loadRows($)
    const v = await read($, view)
    const plan = await loadTodos($)
    const files = await read($, gates)
    // Subscribes the drawing to the 30 s tick, so elapsed times and the bars move.
    await read($, tick)
    const now = await $.clock.now()
    const columns = e.props.bodyColumns
    const summary = list.length === 0 && plan.length === 0 ? 'board  尚無派工' : headlineOf(list, plan, files, now)
    // `s` with each run of bar cells in its color: done green, half yellow, left gray.
    const colored = (s: string) =>
      s
        .split(/(█+|▓+|░+)/)
        .filter(part => part !== '')
        .map(part =>
          part.startsWith(BAR_FULL) ? (
            <Text color="success">{part}</Text>
          ) : part.startsWith(BAR_HALF) ? (
            <Text color="warning">{part}</Text>
          ) : part.startsWith(BAR_EMPTY) ? (
            <Text color="inactive">{part}</Text>
          ) : (
            part
          ),
        )

    const gatesLine = (file: BoardGateFile) =>
      file.task === null ? (
        <Text dimColor>{`${file.file}  格式錯誤`}</Text>
      ) : (
        <Text>
          {`${file.task}  `}
          {file.gates.flatMap((g, i) => [
            ...(i > 0 ? [' '] : []),
            <Text dimColor={g.kind === 'human'} color={g.status === 'fail' ? 'error' : undefined}>
              {`${g.id}${GATE_MARK[g.status]}`}
            </Text>,
          ])}
        </Text>
      )

    if (!v.isExpanded) {
      return (
        <Box flexDirection="column">
          <Box flexDirection="row" justifyContent="space-between">
            <Text wrap="truncate-end">{colored(summary)}</Text>
            <Button key="expand" hotkey="b" onPress={() => void setExpanded($, true).catch(() => undefined)}>
              展開
            </Button>
          </Box>
          {files.length > 0 && (
            <Box flexDirection="row" gap={3}>
              {files.map(gatesLine)}
            </Box>
          )}
        </Box>
      )
    }

    const picked = v.selected === null ? undefined : list.find(r => r.key === v.selected)
    if (picked !== undefined) {
      const lines = picked.prompt.split(/\r?\n/)
      return (
        <Box flexDirection="column">
          <Box flexDirection="row" gap={1}>
            <Button key="back" hotkey="b" onPress={() => void select($, null).catch(() => undefined)}>
              返回
            </Button>
            <Text bold>{picked.description}</Text>
          </Box>
          <Text>{`角色  ${picked.subagentType}${picked.background ? '（背景）' : ''}`}</Text>
          <Text>
            {`狀態  ${ICON[picked.status]} ${LABEL[picked.status]}` + (picked.reason === null ? '' : `：${picked.reason}`)}
          </Text>
          <Text>
            {`開始  ${clockTime(picked.startedAt)}` +
              (picked.endedAt === null ? '' : `   結束  ${clockTime(picked.endedAt)}`) +
              `   經過  ${elapsedText(elapsedOf(picked, now))}`}
          </Text>
          <Text dimColor>派工指令</Text>
          {lines.map(line => (
            <Text>{line === '' ? ' ' : line}</Text>
          ))}
        </Box>
      )
    }

    // A dispatch row as a button; the ones under a plan step are indented beneath it.
    const rowButton = (row: BoardRow) => (
      <Button
        key={`row:${row.key}`}
        plain
        dimColor={row.status === 'done'}
        onPress={() => void select($, row.key).catch(() => undefined)}
      >
        {colored(rowText(row, list, now, columns, row.todoId !== null && plan.some(t => t.id === row.todoId) ? STEP_PREFIX : ''))}
      </Button>
    )
    // Dispatches under no step: none in progress then, or the step has since gone.
    const loose = list.filter(row => !plan.some(t => t.id === row.todoId))

    return (
      <Box flexDirection="column">
        <Box flexDirection="row" justifyContent="space-between">
          <Text bold wrap="truncate-end">
            {colored(summary)}
          </Text>
          <Button key="collapse" hotkey="b" onPress={() => void setExpanded($, false).catch(() => undefined)}>
            收合
          </Button>
        </Box>
        {plan.length === 0 && list.length > 0 && (
          <Text dimColor>
            {(
              '  ' +
              fit('任務', Math.max(10, columns - 2 - ROLE_W - TIME_W - ELAPSED_W - LEFT_W - 1)) +
              ' ' +
              fit('角色', ROLE_W) +
              fit('開始', TIME_W) +
              fit('經過', ELAPSED_W) +
              '剩餘'
            ).trimEnd()}
          </Text>
        )}
        {plan.flatMap((t, i) => [
          <Text dimColor={t.status === 'completed'} wrap="truncate-end">
            {`${TODO_ICON[t.status]} ${i + 1}  ${oneLine(t.subject)}`}
          </Text>,
          ...list.filter(row => row.todoId === t.id).map(rowButton),
        ])}
        {plan.length > 0 && loose.length > 0 && <Text dimColor>其他派工</Text>}
        {(plan.length === 0 ? list : loose).map(row => rowButton(row))}
        {files.length > 0 && <Text dimColor>驗收</Text>}
        {files.map(gatesLine)}
      </Box>
    )
  })
}
