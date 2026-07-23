---
name: verification-before-completion
description: Use before claiming any work is complete, fixed, done, or passing — run the most relevant verification first and report evidence rather than assumptions.
---

# Verification Before Completion

A completion claim is a statement about evidence, not intent. Before saying "done", "fixed", or "passing":

1. Name the check that would most quickly prove the claim wrong — a targeted test, build, lint, type check, or the original repro command.
2. Run it now, in this session, and read the output.
3. Report the claim together with the evidence: what changed, what you ran, and what it printed. Quote failures verbatim.

Treat this as one planned final verification for the declared Completion Contract. Do not use this skill to open optional review, testing, analysis, refactoring, or documentation work.

Calibrate the claim to the evidence:

- Code written but never run is unverified — say so instead of implying it works.
- Partial checks make partial claims: "the targeted tests pass; the full suite was not run."
- If the check fails, the failure is the result to report — do not soften it into progress.
- If verification is impossible here (no environment, missing credentials), state what is missing and exactly what you would run.
