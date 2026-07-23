---
name: two-axis-review
description: Review a non-empty change against repository standards and its originating specification. Use when the user requests a standards-versus-spec review from a fixed point.
---

# Two-axis review for Grok

Review the change since a user-supplied fixed point. Keep the two axes independent: a change can follow repository standards while missing the requested behavior, or implement the request while violating standards.

## Prepare the evidence

1. Resolve the fixed point and inspect a non-empty three-dot diff plus its commit list. If either is unavailable, stop and report that limitation.
2. Locate the originating plan, specification, or PRD. If none is available, mark the Spec axis as skipped rather than guessing.
3. Locate repository standards and conventions. Apply them before general code-smell heuristics.
4. Make two independent review passes. They may run in parallel when the runtime offers safe read-only workers; otherwise run them sequentially without letting one pass change the other pass's conclusions.

## Standards

Report documented-standard violations with the governing path and rule. Separately label code smells as judgement calls, not hard failures. Cite the relevant changed path and line range. If no material finding exists, say so explicitly.

## Spec

Report missing, partial, incorrect, or unrequested behavior against the plan or specification. Quote the applicable requirement and cite the changed path and line range. If the specification is unavailable, state `skipped: no specification available`.

## Report

Present `## Standards` and `## Spec` as separate sections. Do not merge, rerank, or use one axis to hide the other. End with the finding count and the worst issue within each available axis.
