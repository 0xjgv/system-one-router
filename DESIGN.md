# Effort router: can a free classifier pick model and effort per task?

Status: Phase 0 complete, 152 free calls. Verdict: NO-GO (gate 2 fails). Phase 1 stays a spec.

## Question

Can Respan's free classifier, `span-01-free` (Span-01 Lite), read a task request and choose
the model and effort that solve it at the lowest cost? Span-01 is not a router. It scores a
turn against plain-language behaviour definitions and returns `p_present`, `p_absent` and
`p_not_observable` per behaviour. The router would be a rule over those scores.

## Why two axes, and a clarify action

Thariq's "Spending your effort" (claude.dev/blog/spending-your-effort, 2026-09-25) reports that
effort buys verification and edge-case hunting, not better judgment. Fable 5.1 from low to max:
"missed a case" 59 → 24, "made the wrong call" 133 → 107, and "picked the wrong reading" rose
25 → 47. Detailed specs shrink the effort gap. Gains are largest in security (64 → 87%) and
hardware (34 → 75%), smallest in ops (12 → 22%). Opus 5.5 on TB3 goes from about 37% at about
40k tokens (low) to about 66% at about 280k (max).

So "complexity" is two independent scores:

1. **Verification load:** hidden edge cases, a brownfield bug, correctness-critical work.
   Higher effort should pay.
2. **Underspecification:** vague goal, no acceptance criteria, several readings. Effort should
   not pay; the right action is to clarify first.

The action space is `{clarify} ∪ (model × effort)`.

## Hypotheses

- **H1:** the verification score predicts the pass-rate gain from low to high effort.
- **H2:** the underspecification score predicts a small effort gain and more wrong-reading
  failures, so the best action is clarify, then low effort.
- **H3:** per task, the best cell is often not the most expensive one. Take the cheapest cell
  within δ = 5 points of the task's best pass rate. That policy beats constant Opus 5.5 high on
  cost at equal pass rate.

Phase 0 tests none of these directly. It tests whether the scores are worth paying to test them.

## Behaviours

One request scores all 15. The exact definitions are in `phase0.py` (`VERIFICATION`,
`UNDERSPEC`, `SCOPE`, `DOMAINS`) and are frozen with the framing: the cache key hashes them,
so any edit re-spends calls.

| Group | Behaviours |
|---|---|
| Verification | `hidden_edge_cases`, `brownfield_bug_fix`, `correctness_critical`, `needs_test_verification` |
| Underspecification | `ambiguous_goal`, `missing_acceptance_criteria`, `multiple_valid_readings` |
| Scope | `trivial_edit`, `multi_component_change`, `exploratory_or_brainstorm` |
| Domain | `domain_security`, `domain_hardware`, `domain_ml`, `domain_science`, `domain_ops` |

Derived scores, fixed before any data:

- `verif` = mean `p_present` of the 4 verification behaviours.
- `under` = mean `p_present` of the 3 underspecification behaviours.
- `span_cost` = `verif` + `p(multi_component_change)` − `p(trivial_edit)`.

## Phase 0: free, runs under this design

### Cost guard

- `score_free()` raises on any model other than `span-01-free`, before a request exists.
- The run loop stops on `DailyCapReached`, HTTP 402 and HTTP 403. It never retries on another
  model. `span-01-pro` is never called.
- Every live call logs `experiment = effort-router/span-01-free/<stage>` to
  `.state/calls.jsonl`, so the log itself shows the model.
- `phase0.py` imports no network library; only `respan_client.py` opens a connection. The one
  other network step was the TB3 `git clone` below, run once to build `data/tb3.jsonl`.
- The key is never stored here. Export it at call time:
  `export RESPAN_API_KEY=$(grep ^RESPAN_API_KEY ~/Code/corgea/lab/respan/.env | cut -d= -f2)`.

### Data

**TB3, 74 tasks** (`data/tb3.jsonl`, committed). Source:
`git clone --depth 1 --branch v3.0.0 https://github.com/harbor-framework/terminal-bench.git`
(commit `2b0442c3`). Each row holds `instruction.md` plus `category`, `subcategory` and
`expert_time_estimate_hours` from `task.toml`. Categories: Software 20, Science 15, ML 13,
Operations 10, Security 7, Hardware 5, Media 4. The instructions keep their harbor canary GUID
comment: it marks benchmark text for training filters, and Respan's terms are unread.

**History, 50 prompts** (`artifacts/history_sample.jsonl`, git-ignored). What exists:

- `~/.claude/history.jsonl` has 6,189 eligible entries after filters. Filters: drop `!` and `/`
  commands, text under 15 characters, entries with pasted content, exact duplicates, and any
  `project` path matching `corgea` (3,911 entries).
- Only 195 of them, in 53 sessions, still have a transcript. Older transcripts were pruned.
- 161 of those match a transcript prompt answered by Fable 5.1 or Opus 5.5. That is the pool.
- The sample takes 50 round-robin across 7 (model, effort) strata. 32 come from `~/.claude`.

**Approval gate.** No history prompt is sent until Juan reviews `artifacts/history_review.md`
and writes the sample's sha256 to `artifacts/history_approved.txt`. `main` checks the hash, so
a re-extracted sample needs a new approval. The path filter is not enough on its own: at least
one sampled prompt from a `~/.claude` session names a customer. Delete such items (or add terms
to `EXCLUDE`), re-extract, then approve.

**Known limits.** Many prompts are follow-ups ("1,4, and 2. lead the execution…") that mean
little without the conversation. A router sees the same text, so this is realistic, but it
caps what any classifier can do on history.

### Proxy labels (history only)

From the main-thread transcript between a prompt and the next user action (prompt or command;
tool results and teammate messages do not end the window):

- **Revealed cost:** `log1p(output tokens)` minus the median of its (model, effort) stratum
  over the whole pool. Stream records repeat usage per message id, so usage is counted once per
  id. Subagent tokens are not counted. Tool-call count is kept for reference.
- **Correction:** the next prompt matches the lexical rule `CORRECTION`. Hand check on 30
  next-prompts from the pool (2 flagged, 28 not; labels are my reading, not Juan's): 2/2
  flagged are corrections, and 2 of 28 unflagged are ("don't mention…", "our PR is already
  opened. just push"). `don't` was added to the rule after this check. The pool has 170
  prompts with a next prompt and the sample has 1 correction, so this label is **too thin to
  gate**; it is reported only.

TB3 has no transcripts. Its free labels are `category` (checks the domain tags) and
`expert_time_estimate_hours` (checks `span_cost`). Both are reported, not gated.

**Strata.** Only Fable 5.1 has turns at every effort (pool: high 120, low 10, medium 4,
xhigh 4). Opus 5.5 has high 12, medium 10, xhigh 1. Older models (`claude-opus-5`,
`claude-fable-5`) are left out, not pooled. Any within-model effort comparison on free data is
Fable 5.1 only.

### Framing pilot (39 live calls at most)

Items: 13 TB3 tasks from the five tagged categories (3 each from Science, ML and Operations,
2 each from Security and Hardware; seed 0). TB3 only, so the pilot needs no approval.

- **F1:** a system line in `span.input` ("The next message is a task request a user sends to an
  AI coding agent. Judge the request itself, before any work starts."), the request as
  `span.output` with role `user`.
- **F2:** the request as the user message in `span.input`, and a neutral assistant
  acknowledgement as `span.output`.
- **F3:** F1 plus a second system line with context ("the agent works in a Linux container…"
  for TB3, the repository name for history).

**Winner rule, fixed now.** Drop a framing with any HTTP error, or with `p_not_observable` std
under 0.02 across its item × behaviour values. Among the rest, pick the highest top-domain
accuracy: the share of items whose highest domain tag matches the TB3 category. Break ties by
the larger `verif` std + `under` std.

Pilot items are excluded from gates 1 and 3.

**Pilot result (2026-09-29), 39 live calls, all `span-01-free`:**

- **F1 and F3 are invalid.** All 26 calls returned
  `HTTP 422: {"detail":"bad span: span output must be an assistant message"}`. The API
  requires the judged turn to be an assistant message.
- **F2 scored 13/13.** Top-domain accuracy 0.85 (11/13). `verif` mean 0.523, std 0.234.
  `under` mean 0.170, std 0.064. Every behaviour has `p_present` std above 0.05.
- **F2 fails the winner rule.** `p_not_observable` mean 0.021, std 0.010, under the 0.02
  floor. It is pinned, as in the lab's V1 runs.

Under the rule as written, no framing is eligible and there is no main run. Gate 1 carries the
same `p_not_observable` test, so F2 would also fail it.

**Amendment (2026-09-29, Juan's decision, after the pilot and before the main run).** The
`p_not_observable` floor is removed from the winner rule and from gate 1. It stays in the
report. Reason: the router reads `p_present` only and never uses the abstention channel, and
the lab already showed that channel is flat on this model. F2 is then the only valid framing.

Frozen framing: F2. It is the only framing the API accepts; top-domain accuracy 11/13.

### Main run (77 live calls at most; 127 with history)

Every item once in the frozen framing: 74 TB3, plus 50 history if approved. The 13 pilot items
come from the cache. The run order round-robins over (source, length quintile), so a run cut
short by the daily cap still spans the data. Three canary items are then rescored outside the
cache; a `p_present` shift above 0.05 means the served model changed during the run.
`span-01-free` changed once already, between 2026-09-26 and 2026-09-28 (lab `drift/RESULTS.md`).
The lab ran 1,850 calls in one day with 0 errors and never hit the cap.

**Main result, TB3 only (2026-09-29).** 61 live calls (13 pilot items from the cache), 0
errors, canary drift 0.0. History is not yet approved.

| Check | Value | Result |
|---|---|---|
| Gate 1: `verif` std / `under` std (61 non-pilot items) | 0.194 / 0.075 | pass |
| Gate 1, reported: `p_not_observable` std | 0.013 | still pinned |
| Gate 1, reported: behaviours with std ≤ 0.05 | `ambiguous_goal` 0.044, `trivial_edit` 0.037, `exploratory_or_brainstorm` 0.026 | flat on TB3 |
| Gate 2 | no history items | **no verdict** |
| Gate 3: Spearman(`verif`, `under`) | 0.41 | pass |
| TB3 domain AUC: Security / Hardware / ML / Science | 1.00 / 0.98 / 0.98 / 0.98 | strong |
| TB3 domain AUC: Operations | 0.58 | weak |
| TB3 Spearman with expert hours: `span_cost` vs `lex_len` | 0.09 vs 0.22 | **span loses to length** |

Reading, marked as inference:

- TB3 "Operations" tasks include business operations (`medical-claims-processing`,
  `intrastat-meldung`), while `domain_ops` defines devops. The low AUC is probably a
  definition mismatch, not a model failure.
- The expert-hours result is the first check against a cost-like label, and length beats
  Span on it. It is not a gate, but it predicts trouble for gate 2.
- The flat underspecification and scope behaviours fit TB3: its tasks are written as complete
  specs. History prompts should vary more.

Verdict: NO VERDICT until the history sample is approved and scored.

**Main result with history (2026-09-29): NO-GO.** Juan approved the sample after `hist/03`
(names a customer) was dropped, so 49 prompts were scored. That took 49 live calls plus 3
canary, with 0 errors and canary drift 0.003. The day's total is 152 calls, all `span-01-free`.

| Check | Value | Result |
|---|---|---|
| Gate 1: `verif` std / `under` std (non-pilot TB3 + history) | 0.248 / 0.210 | pass |
| Gate 2: Spearman with revealed cost, `span_cost` vs best lexical (`lex_zsum`) | 0.200 vs 0.297; 90% CI of difference [−0.38, +0.21] | **fail** |
| Gate 2: correction | 1 positive | underpowered, not gated |
| Gate 3: Spearman(`verif`, `under`) | −0.53 | pass |

Post-hoc, exploratory only (not declared before the data; do not treat as evidence):

| Score | Spearman with revealed cost | Spearman with tool calls |
|---|---|---|
| `verif` alone | 0.308 | 0.395 |
| `span_cost` | 0.200 | −0.028 |
| `lex_len` | 0.261 | 0.060 |

`verif` alone edges the lexical baseline on cost by 0.01, far inside the noise, and
correlates with tool calls where length does not. That is a hypothesis for a fresh sample,
not a rescue of this one: picking `verif` after seeing these numbers is selection on the test
set.

**Verdict: NO-GO for Phase 1 as designed.** The Span scores are varied and form two distinct
axes, but the declared cost score does not beat a length-and-keyword baseline on the only
label available. With 49 items the interval is wide, so this is a failure to show a gain, not
proof that none exists. Any retry needs a fresh history sample, with `verif` declared before
any score is seen, and more transcripts than the 161-prompt pool.

### Baselines

- `lex_zsum`: z-score sum of `log1p(length)`, file-path count and keyword count (bug, fix,
  error, fail, broken, crash, security, vuln, test, why, race, edge). Unfitted.
- `lex_len`: `log1p(length)` alone.
- Span must beat the **better** of the two on each label.
- A constant policy has no ranking, so it enters in Phase 1, not here.

### Kill gate: Phase 1 only if all three pass

1. **Non-degenerate.** On non-pilot main items: `verif` std > 0.05 and `under` std > 0.05.
   Per-behaviour std and `p_not_observable` std are reported. (The original plan also required
   `p_not_observable` std ≥ 0.02; that was dropped by the amendment above.)
2. **Beats lexical.** On scored history items: Spearman(`span_cost`, revealed cost) exceeds
   the better lexical Spearman by at least 0.05, with a bootstrap 90% interval of the
   difference (1,000 resamples). If correction ever has ≥ 5 positives and ≥ 5 negatives,
   AUC(`under`, correction) must also beat lexical by 0.05; otherwise it is reported as
   underpowered. The scores are unfitted and the framing is chosen on TB3, so every history
   item is held out. With about 50 items, a null here is likely power-limited: say so, do not
   read it as proof of no signal.
3. **Distinct axes.** |Spearman(`verif`, `under`)| < 0.7 on non-pilot main items.

`report` prints each gate with its numbers and a verdict: GO, NO-GO, or NO VERDICT when gate 2
has fewer than 10 history items. A no-go is a valid result.

### Runbook

```bash
cd ~/Code/effort-router
python3 phase0.py self-test                       # offline
python3 phase0.py dry-run                         # counts + sample body, no calls
export RESPAN_API_KEY=...                         # see Cost guard
python3 phase0.py pilot                           # ≤ 39 live
python3 phase0.py report                          # pick by the winner rule
python3 phase0.py freeze F? "reason"              # then add "Frozen framing: F?" above
# review artifacts/history_review.md, then write its sha256 to artifacts/history_approved.txt
python3 phase0.py main                            # ≤ 127 live
python3 phase0.py report                          # gates + verdict
```

## Phase 1: paid. Spec only; nothing here runs under this design

**Tasks.** Terminal-Bench 3.0, 74 tasks, via
`harbor run -d terminal-bench/terminal-bench@3.0.0 -a claude-code -m anthropic/<model>`. This
anchors to the article's numbers. Harbor ships a `claude-code` agent (README and the
pre-integrated agents page). Its source on `main` exposes `reasoning_effort`
(`low|medium|high|xhigh|max`), set with `--ak reasoning_effort=<level>` or
`--ae CLAUDE_CODE_EFFORT_LEVEL=<level>`. That flag form is read from source, not run; check it
against the installed harbor version first. A SWE-bench Verified slice is the fallback.

**Grid.** {Fable 5.1, Opus 5.5, Sonnet 5.5} × {low, medium, high, xhigh, max}, k = 5 attempts
(the article's k). Haiku 4.5 does not accept effort (models overview: "Default effort … Not
supported"), so it enters as one no-effort cell or not at all. Pilot first: 20 tasks
stratified by Phase 0 scores, on 8 cells chosen from the Phase 0 result.

**Clarify arm.** For the top underspecification tasks, compare "interview → low
implementation → high verification" (the article's loop) with a single max run.

**Labels.** Pass rate and tokens per cell. Best cell = the cheapest cell within δ = 5 points
of the task's best pass rate.

**Router metric.** Regret against the per-task oracle, in pass rate and token cost. Nulls:
constant Opus 5.5 high, the article's rule-of-thumb card, and the lexical router.

**Cost, before any paid run.** Prices per MTok (claude-api skill, cached 2026-09-25):
Fable 5.1 $10 in / $50 out, Opus 5.5 $4 / $20, Sonnet 5.5 $2 / $10, Haiku 4.5 $1 / $5; cache
reads $0.25 (Fable 5.1) and $0.20 (Opus 5.5, Sonnet 5.5). The article gives tokens per attempt
but not the input/output/cache split, which sets the price. Bounds for one Opus 5.5 attempt:

| Effort | Tokens | All cache reads ($0.20) | All output ($20) |
|---|---|---|---|
| low | ~40k | $0.008 | $0.80 |
| max | ~280k | $0.056 | $5.60 |

The upper bound for the pilot (20 tasks × 8 cells × 5 attempts = 800 attempts), at Opus 5.5
max output pricing, is 800 × $5.60 ≈ $4,480. Fable cells cost 2.5× the Opus rate. The real
figure sits far below the bound, because agent runs are mostly cached input. Tightening it
needs one measured attempt's usage split, which is itself a paid run. That call is Juan's.

## Rules

- Report cap truncation, invalid responses and HTTP errors as they are. A missing run is not a
  zero.
- Item-level history data and scores stay in git-ignored `artifacts/`. Only aggregates and
  TB3 data are committed.
- Respan's terms were not read (lab README). Keep results internal.
