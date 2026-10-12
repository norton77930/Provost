# Contributing to Provost

Provost is one person's working Claude Code setup, published as it is used.
Bug reports, runtime fixes, tests, cross-platform work and documentation
improvements are welcome. Policy changes are welcome as proposals; the policy
reflects how its maintainer works, so expect discussion before a change lands.

## Report a bug

Use the bug-report template and include the operating system, Claude Code and
Python versions, the smallest reproduction, and the expected and actual result.
Redact credentials, private paths, and private repository content.

Do not report a suspected vulnerability in a public issue. Use the private
channel described in [`SECURITY.md`](SECURITY.md).

## Changing things

- **Runtime and hooks** (`scripts/provost_runtime/`, `hooks/`): start with a
  focused failing test in `tests/adaptive/`, make the smallest coherent fix, and
  keep hooks fail-soft — a hook must never block or break a session. Hooks must
  run on bash 3.2 (macOS); `test_packaging` scans for newer constructs.
- **Installer** (`install.py`): never write outside the config directory, never
  touch `settings.json`, back up before writing, and keep `check` read-only.
- **Policy and roles** (`native/`, `agents/`): update the role table in both
  `README.md` and `README.zh-TW.md` when a model, effort or tool list changes.
- **Skills**: vendored skills stay pinned to the snapshot recorded in
  [`skills/NOTICE.md`](skills/NOTICE.md). Do not remove third-party attribution.
- Keep `README.md` and `README.zh-TW.md` consistent.

## Run the checks

```bash
python -m unittest discover -s tests/adaptive -p "test_*.py"
```

On Windows, also run the link check:

```powershell
powershell.exe -NoProfile -File .\tests\docs\Test-MarkdownLinks.ps1
```

To see how your working copy differs from your own live setup:

```bash
python install.py check
```

## Pull requests

Describe the change, the evidence (tests run, live check), and any behaviour
that changes for existing installs. CI must pass on all three operating
systems.
