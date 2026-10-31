# Board pane mockup

Chosen by the user on 2026-10-31 ("分區清楚版"). It is the source for the board pane's
visual gate. It is a layout reference, not pixel-exact: compare structure, order,
icons, labels and alignment, not exact widths.

## Glyph amendment (2026-10-31)

A live Windows PowerShell (conhost) window drew Geometric Shapes glyphs (◆ ▶ ○ ●) two cells
wide, so their lines sat one cell right of the ✓ and · lines. The pane therefore draws
`»` for an in-progress step or a running dispatch, `·` for a pending step or a waiting dispatch,
and `!` for a pending human gate. The drawing below keeps the glyphs the user chose; compare
the layout with these substitutions.

## Expanded, with a plan, one dispatch and an acceptance file

Fixture: five plan steps (1-3 completed, 4 in progress, 5 pending); one finished
`implementer-sonnet` dispatch "Board round-1 fixes" (19 min) under step 3; one acceptance
file with task "board round 1" and gates `tests` (machine, pass, evidence
"plugin test 48 pass"), `validate` (machine, pass, check "plugin validate"),
`live-new-reset` (live, pending, check "real session /new"), `merge` (human, pending).

```
board  ████████░░ 4/5 · 驗收 2/5 · 等你 merge   [收合]
── 步驟 ──────────────────────────────────────
 ✓ 1  Move board mod into Provost repo
 ✓ 2  Write acceptance file for board round 1
 ✓ 3  Implement board round-1 fixes
      ↳ implementer-sonnet · Board round-1 fixes · 19 分
 ▶ 4  Point plugin dir at repo board and verify live
 ○ 5  Commit, push and open draft PR
── 驗收 · board round 1 ──────────────────────
 ✓ tests           plugin test 48 pass
 ✓ validate        plugin validate
 · live-new-reset  real session /new
 ◆ merge           等你核可
```
