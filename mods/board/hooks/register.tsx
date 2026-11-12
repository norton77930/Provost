import { atom, read, update } from 'claude-code'
import type { AgentStatus, EngineInterface, Register, Timer, ToolCallResult } from 'claude-code'

import type { BoardGate, BoardGateFile, BoardRow, BoardStatus, BoardTodo, BoardView } from '../types'

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
// A path (spelled with forward slashes) in here, ending in .json, is an acceptance file.
const ACCEPTANCE_MARK = '/.claude/acceptance/'

// Every word the board shows, in the language chosen at register time (English unless zh-TW).
// Strings that carry numbers are functions. Labels and glyphs keep the same cells in both.
const EN = {
  command: 'Task board: open or close the board pane',
  none: 'board  no dispatches yet',
  done: (n: number, total: number) => `${n}/${total} done`,
  running: (n: number) => `${n} running`,
  failed: (n: number) => `${n} failed`,
  minutes: (n: number) => `${n} min`,
  underMinute: '<1 min',
  gates: 'gates',
  allPassed: (passed: number, total: number) => `all passed ${passed}/${total}`,
  pending: (n: number) => `${n} pending`,
  agentsRunning: (n: number) => `${n} agent${n === 1 ? '' : 's'} running`,
  waiting: (id: string, n: number) => `needs you: ${id}${n > 1 ? ` (${n} waiting)` : ''}`,
  status: { done: 'done', running: 'running', waiting: 'waiting', failed: 'failed', stopped: 'stopped' } as Record<BoardStatus, string>,
  stopped: 'stopped',
  runFailed: 'run failed',
  denied: (why: string) => `denied: ${why}`,
  dispatchFailed: 'dispatch failed',
  cloud: 'launched in the cloud (not tracked)',
  turnError: 'turn ended with an error',
  expand: 'Expand',
  collapse: 'Collapse',
  // What the collapsed headline leaves for the button beside it.
  buttonW: 12,
  back: 'Back',
  role: 'Role',
  background: ' (background)',
  state: 'Status',
  colon: ': ',
  started: 'Start',
  ended: 'End',
  elapsed: 'Elapsed',
  prompt: 'Dispatch prompt',
  malformed: 'malformed',
  malformedCount: (n: number) => `${n} malformed`,
  steps: 'Steps',
  dispatches: 'Dispatches',
  gatesRule: 'Gates',
  approval: 'needs your approval',
}
const ZH: typeof EN = {
  command: '任務看板：開啟或關閉 board 面板',
  none: 'board  尚無派工',
  done: (n, total) => `${n}/${total} 完成`,
  running: n => `${n} 執行中`,
  failed: n => `${n} 失敗`,
  minutes: n => `${n} 分`,
  underMinute: '<1 分',
  gates: '驗收',
  allPassed: (passed, total) => `全部通過 ${passed}/${total}`,
  pending: n => `待驗 ${n}`,
  agentsRunning: n => `${n} agent 執行中`,
  waiting: (id, n) => `等你 ${id}${n > 1 ? ` 等 ${n} 項` : ''}`,
  status: { done: '完成', running: '執行中', waiting: '等待中', failed: '失敗', stopped: '已停止' },
  stopped: '已停止',
  runFailed: '執行失敗',
  denied: why => `已拒絕：${why}`,
  dispatchFailed: '派工失敗',
  cloud: '已在雲端啟動（不再追蹤）',
  turnError: '回合以錯誤結束',
  expand: '展開',
  collapse: '收合',
  buttonW: 8,
  back: '返回',
  role: '角色',
  background: '（背景）',
  state: '狀態',
  colon: '：',
  started: '開始',
  ended: '結束',
  elapsed: '經過',
  prompt: '派工指令',
  malformed: '格式錯誤',
  malformedCount: n => `${n} 個格式錯誤`,
  steps: '步驟',
  dispatches: '派工',
  gatesRule: '驗收',
  approval: '等你核可',
}
// Chosen once, by register(): any value but 'zh-TW' means English.
let t: typeof EN = EN

// Everything the board shows lives in $.state: it survives a hot reload of this module.
const rows = atom({ plugin: 'board', key: 'rows' } as const, [])
const todos = atom({ plugin: 'board', key: 'todos' } as const, [])
const gates = atom({ plugin: 'board', key: 'gates' } as const, [])
// The acceptance files the main loop wrote, by absolute path: the only ones the board reads.
const owned = atom({ plugin: 'board', key: 'owned' } as const, [])
const tick = atom({ plugin: 'board', key: 'tick' } as const, 0)
const INITIAL_VIEW: BoardView = { isExpanded: false, selected: null, opened: [], isAutoOpened: false }
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
// Gates saved before they carried their check text read it as empty.
const loadGates = async ($: EngineInterface) =>
  (await read($, gates)).map(f => ({ ...f, gates: f.gates.map(g => ({ ...g, check: g.check ?? '' })) }))
const loadOwned = async ($: EngineInterface) => (await read($, owned)).filter(f => typeof f === 'string')
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

const isHumanPending = (g: BoardGate) => g.kind === 'human' && g.status === 'pending'

// The human gates still pending, the first one named: what the person is being waited on for.
// It leads the headline, so a headline cut to the pane's width keeps the name.
function waitingTextOf(files: BoardGateFile[]) {
  const waiting = files.flatMap(f => f.gates).filter(isHumanPending)
  return waiting.length === 0 ? '' : t.waiting(waiting[0]?.id ?? '', waiting.length)
}

// Dispatches that were stopped on purpose are neither done nor failed: they leave the total.
function summaryOf(list: BoardRow[], now: number) {
  const counted = list.filter(r => r.status !== 'stopped')
  const done = counted.filter(r => r.status === 'done').length
  const active = counted.filter(isActive).length
  const failed = counted.filter(r => r.status === 'failed').length
  const eta = longestRemainingMs(list, now)
  return (
    `${barOf(ratio(done, counted.length), STEP_BAR_W)} ${t.done(done, counted.length)} · ${t.running(active)}` +
    (failed > 0 ? ` · ${t.failed(failed)}` : '') +
    (eta === undefined ? '' : ` · ~${t.minutes(minutes(eta))}`)
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

// The plan as a bar of its steps (one in progress counts half) and the steps done, the
// gates passed beside it, no minute ETA: step durations are too few and too unlike to promise one.
// The step in progress is named by the pane's own » line, not here.
function planSummaryOf(plan: BoardTodo[], list: BoardRow[], files: BoardGateFile[]) {
  const running = plan.filter(t => t.status === 'in_progress')
  const completed = plan.filter(t => t.status === 'completed').length
  const active = list.filter(isActive).length
  const all = files.flatMap(f => f.gates)
  const passed = all.filter(g => g.status === 'pass').length
  return (
    `${barOf(ratio(completed + running.length / 2, plan.length), STEP_BAR_W)} ${completed}/${plan.length}` +
    (all.length === 0 ? '' : ` · ${t.gates} ${passed}/${all.length}`) +
    (active > 0 ? ` · ${t.agentsRunning(active)}` : '')
  )
}

// Only acceptance files and nothing else: the gates bar leads. With no gate to count,
// a bar over 0/0 says nothing, so name the malformed files or show a dash.
function gatesSummaryOf(files: BoardGateFile[]) {
  const all = files.flatMap(f => f.gates)
  if (all.length === 0) {
    const broken = files.filter(f => f.task === null).length
    return t.gates + (broken > 0 ? ` · ${t.malformedCount(broken)}` : ' —')
  }
  const passed = all.filter(g => g.status === 'pass').length
  return `${t.gates} ${barOf(ratio(passed, all.length), GATE_BAR_W)} ${passed}/${all.length}`
}

// The status line and both pane summaries: the plan leads when there is one, else the
// dispatches; gates alone lead only when there is neither. The host puts the mod's name before the
// status line and the pane has its title, so the text does not. A human gate waiting leads it.
function headlineOf(list: BoardRow[], plan: BoardTodo[], files: BoardGateFile[], now: number) {
  const summary =
    plan.length > 0
      ? planSummaryOf(plan, list, files)
      : list.length === 0 && files.length > 0
        ? gatesSummaryOf(files)
        : summaryOf(list, now)
  const waiting = waitingTextOf(files)
  return waiting === '' ? summary : `${waiting} · ${summary}`
}

const pad2 = (n: number) => String(n).padStart(2, '0')
const clockTime = (ms: number) => {
  const d = new Date(ms)
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`
}
const elapsedText = (ms: number) => (ms < MIN ? t.underMinute : t.minutes(Math.floor(ms / MIN)))
const elapsedOf = (row: BoardRow, now: number) => (row.endedAt ?? now) - row.startedAt

const ICON: Record<BoardStatus, string> = { done: '✓', running: '»', waiting: '·', failed: '✗', stopped: '✕' }
// Marks are Latin-1 or Dingbats glyphs, one cell wide in every terminal: a Geometric Shapes glyph
// (▶ ○ ● ◆ ■) draws two cells in some, which shifts the line it leads. Stopped is the Dingbats ✕, not ■.
const TODO_ICON: Record<BoardTodo['status'], string> = { completed: '✓', in_progress: '»', pending: '·' }
// A gate's mark: a pending human gate is the one the person is waited on for.
const gateMark = (g: BoardGate) => (isHumanPending(g) ? '!' : { pass: '✓', fail: '✗', pending: '·' }[g.status])

// Terminal cells of a string: CJK and other wide glyphs take two.
const cellsOf = (s: string) => [...s].reduce((n, c) => n + ((c.codePointAt(0) ?? 0) >= 0x2e80 ? 2 : 1), 0)
// The string cut to at most `width` cells with an ellipsis (callers pass one line).
function clip(s: string, width: number) {
  if (cellsOf(s) <= width) return s
  let out = ''
  for (const c of s) {
    if (cellsOf(out + c) > width - 1) break
    out += c
  }
  return out + '…'
}
// The string padded with spaces to `width` cells.
const padCells = (s: string, width: number) => s + ' '.repeat(Math.max(0, width - cellsOf(s)))

// Fields of a row are told apart by this, never by bare spaces.
const SEP = ' · '
// A description is cut before anything else, but never below this.
const MIN_DESC_W = 8
// A failure reason longer than this stays in the detail view.
const REASON_W = 24
// A dispatch row under a plan step.
const STEP_LEAD = '      ↳ '

// A section rule: `── label ──────` out to `width` cells.
function ruleOf(label: string, width: number) {
  const head = clip(`── ${label} `, width)
  return head + '─'.repeat(Math.max(0, width - cellsOf(head)))
}

// The time-left part of an active row: elapsed over the estimate as a bar, nothing once past
// it (the elapsed minutes beside it say how long), '—' with no estimate.
function leftTextOf(row: BoardRow, list: BoardRow[], now: number) {
  if (!isActive(row)) return ''
  const estimate = estimateMs(row, list)
  if (estimate === undefined) return '—'
  const ran = now - row.startedAt
  return ran > estimate ? '' : barOf(ran / estimate, ROW_BAR_W)
}

// `<role> · <description> · <time>`: led by the step arrow under a plan step, else by the
// status icon. The time is the elapsed minutes, then the bar while it runs; a short
// failure or stop reason ends a failed or stopped row. The description gives way first.
function rowText(row: BoardRow, list: BoardRow[], now: number, columns: number, isUnderStep: boolean) {
  const left = leftTextOf(row, list, now)
  const reason = (row.status === 'failed' || row.status === 'stopped') && row.reason !== null ? oneLine(row.reason) : ''
  const tail =
    SEP + elapsedText(elapsedOf(row, now)) + (left === '' ? '' : ` ${left}`) + (reason !== '' && cellsOf(reason) <= REASON_W ? SEP + reason : '')
  const head = (isUnderStep ? STEP_LEAD : ` ${ICON[row.status]} `) + row.subagentType + SEP
  const descW = Math.max(MIN_DESC_W, columns - cellsOf(head) - cellsOf(tail))
  return clip(head + clip(oneLine(row.description), descW) + tail, columns)
}

// AgentStatus as a row's status.
const statusOfAgent = (status: AgentStatus): BoardStatus =>
  status === 'completed'
    ? 'done'
    : status === 'killed'
      ? 'stopped'
      : status === 'failed'
        ? 'failed'
        : status === 'running'
          ? 'running'
          : 'waiting'

const reasonOfAgent = (status: AgentStatus) =>
  status === 'killed' ? t.stopped : status === 'failed' ? t.runFailed : null

// The row as the Agent call's answer leaves it.
function settle(row: BoardRow, ran: ToolCallResult<'Agent'>, now: number): BoardRow {
  if (ran.deny !== undefined) return { ...row, status: 'failed', reason: t.denied(ran.deny), endedAt: now }
  if (ran.isError === true) return { ...row, status: 'failed', reason: ran.text || t.dispatchFailed, endedAt: now }
  const result = ran.result as Record<string, unknown> | undefined
  const agentId = typeof result?.agentId === 'string' ? result.agentId : null
  const status = typeof result?.status === 'string' ? result.status : undefined
  if (status === 'remote_launched') {
    return { ...row, status: 'done', reason: t.cloud, endedAt: now, isRemote: true, background: true }
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
const GATE_KINDS = ['machine', 'live', 'visual', 'review', 'human'] as const
const GATE_STATUSES = ['pass', 'fail', 'pending'] as const
const GATE_OPTIONAL = ['ranAt', 'evidence', 'source'] as const
// A pass of these goes stale after an edit, so they may carry the `tree` stamp.
const GATE_STALE_KINDS: readonly string[] = ['machine', 'live', 'visual']
const TODO_STATUSES = ['pending', 'in_progress', 'completed'] as const

// One acceptance file read the way the Stop gate reads it (scripts/provost_runtime/acceptance.py load(),
// the reference; mods/board/tests/fixtures/acceptance-cases.ts pins both on the same cases): `task: null`
// when it does not fit the schema, `null` for a finished file (a valid top-level `closed` reason), which
// the board leaves out. Two things the board does not do: judge staleness, and parse `ranAt`, which is
// only checked to be a string (the Stop gate holds the ISO 8601 check). A leading BOM is stripped.
function parseGates(file: string, text: string): BoardGateFile | null {
  const malformed: BoardGateFile = { file, task: null, gates: [] }
  let data: unknown
  try {
    data = JSON.parse(text.charCodeAt(0) === 0xfeff ? text.slice(1) : text)
  } catch {
    return malformed
  }
  if (!isRecord(data) || typeof data.task !== 'string') return malformed
  if ('closed' in data && !(typeof data.closed === 'string' && data.closed.trim() !== '')) return malformed
  if (!Array.isArray(data.gates)) return malformed
  const list: BoardGateFile['gates'] = []
  for (const g of data.gates as unknown[]) {
    if (
      !isRecord(g) ||
      typeof g.id !== 'string' ||
      g.id.trim() === '' ||
      !isOneOf(g.kind, GATE_KINDS) ||
      typeof g.check !== 'string' ||
      !isOneOf(g.status, GATE_STATUSES) ||
      !GATE_OPTIONAL.every(key => !(key in g) || typeof g[key] === 'string') ||
      (GATE_STALE_KINDS.includes(g.kind) && 'tree' in g && typeof g.tree !== 'string') ||
      (g.kind !== 'human' && ('files' in g || 'digest' in g) && !hasFilesAndDigest(g))
    ) {
      return malformed
    }
    list.push({
      id: g.id,
      kind: g.kind,
      status: g.status,
      check: g.check,
      ...(typeof g.evidence === 'string' ? { evidence: g.evidence } : {}),
    })
  }
  return 'closed' in data ? null : { file, task: data.task, gates: list }
}

// `files` (a non-empty list of non-empty paths) and `digest` (a string) go together, as `runtime digest` prints them.
const hasFilesAndDigest = (g: Record<string, unknown>) =>
  Array.isArray(g.files) &&
  g.files.length > 0 &&
  g.files.every(name => typeof name === 'string' && name !== '') &&
  typeof g.digest === 'string'

const isAnswered = (ran: ToolCallResult) => ran.deny === undefined && ran.isError !== true

const slashed = (path: string) => path.replace(/\\/g, '/')
const baseName = (path: string) => path.slice(path.lastIndexOf('/') + 1)
const isAcceptancePath = (path: string) => path.includes(ACCEPTANCE_MARK) && path.endsWith('.json')

// The board reads an acceptance file only once the main loop has written it.
async function own($: EngineInterface, path: string | undefined) {
  const at = path === undefined ? '' : slashed(path)
  if (!isAcceptancePath(at)) return
  await update($, owned, list => (list.includes(at) ? list : [...list, at]))
}

// A run of a line in one style: the second line of the collapsed pane is drawn from these.
type Piece = { text: string; color?: 'warning' | 'error' | 'success'; isDim?: boolean }
const SEPARATOR: Piece = { text: '  ' }

// The longest start of `s` that fits `width` cells.
function takeCells(s: string, width: number) {
  let out = ''
  for (const c of s) {
    if (cellsOf(out + c) > width) break
    out += c
  }
  return out
}

// The pieces cut to at most `width` cells, an ellipsis ending the last when something is lost.
function clipPieces(pieces: Piece[], width: number) {
  if (pieces.reduce((n, p) => n + cellsOf(p.text), 0) <= width) return pieces
  const out: Piece[] = []
  let used = 0
  for (const p of pieces) {
    const room = width - 1 - used
    if (room <= 0) break
    if (cellsOf(p.text) <= room) {
      out.push(p)
      used += cellsOf(p.text)
    } else {
      out.push({ ...p, text: takeCells(p.text, room) + '…' })
      return out
    }
  }
  const last = out[out.length - 1]
  if (last !== undefined) out[out.length - 1] = { ...last, text: last.text.trimEnd() + '…' }
  return out
}

// The second line of the collapsed pane: only what asks for attention, in this order: the human
// gates waiting, the failed gates, the agents running, the malformed files. With none of them:
// every gate passed, or how many are still to run. Nothing at all leaves it empty.
function collapsedPieces(list: BoardRow[], files: BoardGateFile[]): Piece[] {
  const all = files.flatMap(f => f.gates)
  const exceptions: Piece[] = [
    ...all.filter(isHumanPending).map(g => ({ text: `!${g.id}`, color: 'warning' })),
    ...all.filter(g => g.status === 'fail').map(g => ({ text: `✗${g.id}`, color: 'error' })),
    ...list.filter(isActive).map(r => ({ text: `${ICON.running} ${r.subagentType}` })),
    ...files.filter(f => f.task === null).map(f => ({ text: `${f.file}  ${t.malformed}`, isDim: true })),
  ]
  if (exceptions.length > 0) return exceptions.flatMap((p, i) => (i === 0 ? [p] : [SEPARATOR, p]))
  const passed = all.filter(g => g.status === 'pass').length
  if (all.length === 0) return []
  if (passed === all.length) return [{ text: '✓', color: 'success' }, { text: ` ${t.allPassed(passed, all.length)}` }]
  return [{ text: t.pending(all.length - passed) }]
}

// ---------------------------------------------------------------- state and effects

async function show($: EngineInterface) {
  const list = await loadRows($)
  const plan = await loadTodos($)
  const files = await loadGates($)
  if (list.length === 0 && plan.length === 0 && files.length === 0) return
  $.ui.status(headlineOf(list, plan, files, await $.clock.now()))
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
        const ended = status === 'done' || status === 'failed' || status === 'stopped'
        return { ...row, status, reason: reasonOfAgent(info.status), endedAt: ended ? now : null }
      }),
    )
  }
  await update($, tick, () => now)
  await readGates($).catch(() => undefined)
  await sync($)
}

// The acceptance files the main loop wrote, by name. One that can no longer be read, or that is
// finished (it carries a `closed` reason), is left out; none written shows nothing.
async function readGates($: EngineInterface) {
  const found: BoardGateFile[] = []
  for (const path of await loadOwned($)) {
    const text = await $.fs.read(path).catch(() => undefined)
    const parsed = typeof text === 'string' ? parseGates(baseName(path), text) : null
    if (parsed !== null) found.push(parsed)
  }
  found.sort((a, b) => a.file.localeCompare(b.file))
  const before = await read($, gates)
  if (JSON.stringify(before) !== JSON.stringify(found)) await update($, gates, () => found)
}

// A /clear starts a fresh conversation in the same process, and $.state outlives it:
// the board starts over, status line included, and the pane may auto-open again.
async function reset($: EngineInterface) {
  poll?.cancel()
  poll = undefined
  await update($, rows, () => [])
  await update($, todos, () => [])
  await update($, gates, () => [])
  await update($, owned, () => [])
  await update($, tick, () => 0)
  await update($, view, () => INITIAL_VIEW)
  $.ui.status(undefined)
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
          : reason === 'aborted'
            ? { ...row, status: 'stopped', endedAt: now, reason: t.stopped }
            : { ...row, status: 'failed', endedAt: now, reason: t.turnError }
        : row,
    ),
  )
  await sync($)
}

// An agent's new turn: a waiting row runs; so does a stopped or failed one, as a SendMessage
// resumes an agent without a new Agent call.
async function wakeAgent($: EngineInterface, agentId: string) {
  await changeRows($, list =>
    list.map(row =>
      row.agentId === agentId && (row.status === 'waiting' || row.status === 'stopped' || row.status === 'failed')
        ? { ...row, status: 'running', endedAt: null, reason: null }
        : row,
    ),
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

// An all-pass file's line shows its gates under it, or hides them again.
async function toggleOpened($: EngineInterface, file: string) {
  await update($, view, v => {
    const opened = v.opened ?? []
    return { ...v, opened: opened.includes(file) ? opened.filter(f => f !== file) : [...opened, file] }
  })
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

export const register: Register = (on, options) => {
  t = options.language === 'zh-TW' ? ZH : EN

  on('session.start', async ($, e, next) => {
    await $.command
      .register({ name: 'board', description: t.command, immediate: true })
      .catch(() => undefined)
    await sync($).catch(() => undefined)
    return next(e)
  })

  // No session.start follows a /clear, so this is the only moment to start over.
  on('session.end', async ($, e, next) => {
    if (e.reason === 'clear') await reset($).catch(() => undefined)
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

  // Which acceptance files are the session's: the main loop's own successful writes.
  on('tool.call', { tool: 'Write' }, async ($, e, next) => {
    const ran = await next(e)
    if (e.agentId === undefined && isAnswered(ran)) await own($, e.file_path).catch(() => undefined)
    return ran
  }).catch(($, e, next) => next(e))

  on('tool.call', { tool: 'Edit' }, async ($, e, next) => {
    const ran = await next(e)
    if (e.agentId === undefined && isAnswered(ran)) await own($, e.file_path).catch(() => undefined)
    return ran
  }).catch(($, e, next) => next(e))

  on('tool.call', { tool: 'NotebookEdit' }, async ($, e, next) => {
    const ran = await next(e)
    if (e.agentId === undefined && isAnswered(ran)) await own($, e.notebook_path).catch(() => undefined)
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
    const files = await loadGates($)
    // Subscribes the drawing to the 30 s tick, so elapsed times and the bars move.
    await read($, tick)
    const now = await $.clock.now()
    const columns = e.props.bodyColumns
    const summary =
      list.length === 0 && plan.length === 0 && files.length === 0 ? t.none : headlineOf(list, plan, files, now)
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

    // The headline cut to leave room for the expand / collapse button beside it.
    const headline = (bold: boolean) => (
      <Text bold={bold} wrap="truncate-end">
        {colored(clip(summary, columns - t.buttonW))}
      </Text>
    )

    if (!v.isExpanded) {
      const pieces = collapsedPieces(list, files)
      return (
        <Box flexDirection="column">
          <Box flexDirection="row" justifyContent="space-between">
            {headline(false)}
            <Button key="expand" hotkey="b" onPress={() => void setExpanded($, true).catch(() => undefined)}>
              {t.expand}
            </Button>
          </Box>
          {pieces.length > 0 && (
            <Text wrap="truncate-end">
              {clipPieces(pieces, columns).map(p =>
                p.color === undefined && !p.isDim ? p.text : (
                  <Text color={p.color} dimColor={p.isDim}>
                    {p.text}
                  </Text>
                ),
              )}
            </Text>
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
              {t.back}
            </Button>
            <Text bold>{picked.description}</Text>
          </Box>
          <Text>{`${t.role}  ${picked.subagentType}${picked.background ? t.background : ''}`}</Text>
          <Text>
            {`${t.state}  ${ICON[picked.status]} ${t.status[picked.status]}` +
              (picked.reason === null || picked.reason === t.status[picked.status] ? '' : `${t.colon}${picked.reason}`)}
          </Text>
          <Text>
            {`${t.started}  ${clockTime(picked.startedAt)}` +
              (picked.endedAt === null ? '' : `   ${t.ended}  ${clockTime(picked.endedAt)}`) +
              `   ${t.elapsed}  ${elapsedText(elapsedOf(picked, now))}`}
          </Text>
          <Text dimColor>{t.prompt}</Text>
          {lines.map(line => (
            <Text>{line === '' ? ' ' : line}</Text>
          ))}
        </Box>
      )
    }

    // A dispatch row as a button: under its plan step with an arrow, else led by its status icon.
    const rowButton = (row: BoardRow) => {
      const text = colored(rowText(row, list, now, columns, row.todoId !== null && plan.some(t => t.id === row.todoId)))
      return (
        <Button
          key={`row:${row.key}`}
          plain
          dimColor={row.status === 'done' || row.status === 'stopped'}
          onPress={() => void select($, row.key).catch(() => undefined)}
        >
          {row.status === 'failed' ? (
            <Text color="error">{text}</Text>
          ) : row.status === 'stopped' ? (
            <Text color="inactive">{text}</Text>
          ) : (
            text
          )}
        </Button>
      )
    }
    // Dispatches under no step: none in progress then, or the step has since gone.
    const loose = plan.length === 0 ? list : list.filter(row => !plan.some(t => t.id === row.todoId))

    // One gate per line: `mark id  evidence | check | needs-approval text`, the ids lined up within the file.
    const gateLines = (file: BoardGateFile) => {
      const idW = Math.max(0, ...file.gates.map(g => cellsOf(g.id))) + 2
      return file.gates.map(g => {
        const mark = gateMark(g)
        const evidence = oneLine(g.evidence ?? '')
        const text = isHumanPending(g) ? t.approval : g.status === 'pass' && evidence !== '' ? evidence : oneLine(g.check)
        const rest = ` ${padCells(g.id, idW)}`
        return (
          <Text wrap="truncate-end">
            {' '}
            <Text color={g.status === 'pass' ? 'success' : g.status === 'fail' ? 'error' : isHumanPending(g) ? 'warning' : undefined}>
              {mark}
            </Text>
            {(rest + clip(text, Math.max(0, columns - 2 - cellsOf(rest)))).trimEnd()}
          </Text>
        )
      })
    }

    // Files with a gate still to pass or fixed (and the malformed ones) come first, each with its gates;
    // a file whose gates all passed is one line that opens to show them.
    const isPassed = (file: BoardGateFile) =>
      file.task !== null && file.gates.length > 0 && file.gates.every(g => g.status === 'pass')
    const openFiles = files.filter(file => !isPassed(file))
    const passedFiles = files.filter(isPassed)
    const shown = v.opened ?? []

    return (
      <Box flexDirection="column">
        <Box flexDirection="row" justifyContent="space-between">
          {headline(true)}
          <Button key="collapse" hotkey="b" onPress={() => void setExpanded($, false).catch(() => undefined)}>
            {t.collapse}
          </Button>
        </Box>
        {plan.length > 0 && <Text dimColor>{ruleOf(t.steps, columns)}</Text>}
        {plan.flatMap((t, i) => [
          <Text dimColor={t.status === 'completed'} bold={t.status === 'in_progress'} wrap="truncate-end">
            {clip(` ${TODO_ICON[t.status]} ${i + 1}  ${oneLine(t.subject)}`, columns)}
          </Text>,
          ...list.filter(row => row.todoId === t.id).map(rowButton),
        ])}
        {loose.length > 0 && <Text dimColor>{ruleOf(t.dispatches, columns)}</Text>}
        {loose.map(rowButton)}
        {openFiles.flatMap(file =>
          file.task === null
            ? [<Text dimColor>{`${file.file}  ${t.malformed}`}</Text>]
            : [<Text dimColor>{ruleOf(`${t.gatesRule} · ${oneLine(file.task)}`, columns)}</Text>, ...gateLines(file)],
        )}
        {passedFiles.flatMap(file => [
          <Button
            key={`file:${file.file}`}
            plain
            dimColor
            onPress={() => void toggleOpened($, file.file).catch(() => undefined)}
          >
            {clip(` ${ICON.done} ${oneLine(file.task ?? '')} ${file.gates.length}/${file.gates.length}`, columns)}
          </Button>,
          ...(shown.includes(file.file) ? gateLines(file) : []),
        ])}
      </Box>
    )
  })
}
