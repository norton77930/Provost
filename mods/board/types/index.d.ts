// running: its loop runs a turn; waiting: pending, held or idle between turns.
export type BoardStatus = 'running' | 'waiting' | 'done' | 'failed'

// One Agent dispatch, from its tool.call to its end.
export type BoardRow = {
  // The call's tool_use_id, or a generated one.
  key: string
  description: string
  subagentType: string
  // The full dispatch prompt.
  prompt: string
  background: boolean
  startedAt: number
  endedAt: number | null
  status: BoardStatus
  // Why it failed, or a note on how it ended; null when there is none.
  reason: string | null
  // The spawned agent's id, once the Agent result named it.
  agentId: string | null
  // A remote launch is not tracked after it starts and is left out of the ETA.
  isRemote: boolean
  // The plan step (BoardTodo.id) it was dispatched under; null when none was in progress.
  todoId: string | null
}

// One step of the model's plan (TaskCreate / TaskUpdate / TodoWrite).
export type BoardTodo = {
  id: string
  subject: string
  status: 'pending' | 'in_progress' | 'completed'
  // First time it became in_progress.
  startedAt: number | null
  // When it was completed; null again if it is reopened.
  endedAt: number | null
  // Last time it was set in_progress: what a dispatch links to when several steps run.
  activeAt: number | null
}

export type BoardGate = {
  id: string
  kind: 'machine' | 'live' | 'visual' | 'review' | 'human'
  status: 'pass' | 'fail' | 'pending'
  // What the gate checks; '' in state saved before the board kept it.
  check: string
  // What showed it passed, when the file records any.
  evidence?: string
}

// One `.claude/acceptance/*.json` file the main loop wrote during this session.
export type BoardGateFile = {
  file: string
  // null when the file did not parse as the schema.
  task: string | null
  gates: BoardGate[]
}

export type BoardView = {
  isExpanded: boolean
  // The row whose detail is shown, by key.
  selected: string | null
  // The pane opens unasked once per session, at the first dispatch or the first plan step.
  isAutoOpened: boolean
}

declare module 'claude-code' {
  interface PluginState {
    board: {
      rows: BoardRow[]
      todos: BoardTodo[]
      gates: BoardGateFile[]
      // Absolute paths (forward slashes) of the acceptance files the main loop wrote.
      owned: string[]
      view: BoardView
      // Clock reading of the last 30 s tick: redraws elapsed times and the bars.
      // It runs while a row is active or a plan step is in progress.
      tick: number
    }
  }
}
