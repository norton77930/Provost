# hooks GIF ("Hooks keep the decisions and hold the turn until it's proven.")

Sources: `stage.html` (all animation; `window.render(t)` is a pure function of time; hook hit times are `HOOKS`, caption
switches `CAPS`, decision-log entries `LOGT`, the Stop sequence `T_BLOCK` / `T_GATE` / `T_ENDS`), `timeline.json`
(1600x900, 12 fps, 13.4 s), `strings.en.json` / `strings.zh-TW.json`. The three decision-log entries are illustrative.

Needs: `uv`, `ffmpeg` on PATH, Microsoft Edge, and the promo-video skill's `render.py`
(`C:\Users\NORTON.DENG\.claude\skills\promo-video\scripts\render.py`). Run from the repo root.

```bash
R=C:/Users/NORTON.DENG/.claude/skills/promo-video/scripts/render.py
T=C:/Users/NORTON.DENG/.claude/jobs/e73ea53e/tmp/gifwork   # any scratch dir

# preview frames (optional)
uv run --with playwright python $R docs/demo/gif-src/hooks --lang en --preview 1.5 5.2 7.0 9.9 12.5 --sheet $T/hooks-sheet.png --out-dir $T/prev

# render + convert, once per language (en, zh-TW)
uv run --with playwright python $R docs/demo/gif-src/hooks --lang en --out $T/hooks-en.mp4
ffmpeg -y -i $T/hooks-en.mp4 -vf "scale=1200:-1:flags=lanczos,split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle" -loop 0 docs/demo/hooks-en.gif
```

Same for `--lang zh-TW` -> `docs/demo/hooks-zh-TW.gif`.
