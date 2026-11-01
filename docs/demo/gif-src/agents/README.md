# agents GIF ("Every request goes to the cheapest agent that can do it.")

Sources: `stage.html` (all animation; `window.render(t)` is a pure function of time, the schedule is the `REQ` array and the
timings near the top of the script), `timeline.json` (1600x900, 12 fps, 13.4 s), `strings.en.json` / `strings.zh-TW.json`.
The `分類:...` declaration lines live in `stage.html` (`DECL`) and are identical in both languages by design.

Needs: `uv`, `ffmpeg` on PATH, Microsoft Edge, and the promo-video skill's `render.py`
(`C:\Users\NORTON.DENG\.claude\skills\promo-video\scripts\render.py`). Run from the repo root.

```bash
R=C:/Users/NORTON.DENG/.claude/skills/promo-video/scripts/render.py
T=C:/Users/NORTON.DENG/.claude/jobs/e73ea53e/tmp/gifwork   # any scratch dir

# preview frames (optional)
uv run --with playwright python $R docs/demo/gif-src/agents --lang en --preview 1.5 4.8 8.7 12.5 --sheet $T/agents-sheet.png --out-dir $T/prev

# render + convert, once per language (en, zh-TW)
uv run --with playwright python $R docs/demo/gif-src/agents --lang en --out $T/agents-en.mp4
ffmpeg -y -i $T/agents-en.mp4 -vf "scale=1200:-1:flags=lanczos,split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle" -loop 0 docs/demo/agents-en.gif
```

Same for `--lang zh-TW` -> `docs/demo/agents-zh-TW.gif`.
