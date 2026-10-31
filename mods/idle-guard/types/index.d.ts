export type IdleGuardView = {
  enabled: boolean
  detail: string
  // Clock reading of the write. It changes on every per-minute tick, so a pane that
  // read the view is drawn again (and re-reads the context usage) once a minute.
  at: number
  // When the current idle countdown began; null while a turn runs or none has completed.
  armedAt: number | null
  // The last compaction or skip of this session, as the pane's 上次 row reads it; '' if none.
  last: string
}

declare module 'claude-code' {
  interface PluginState {
    'idle-guard': { view: IdleGuardView }
  }
}
