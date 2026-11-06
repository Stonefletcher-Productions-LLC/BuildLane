# Token use and routing: the 2026-11-06 review

What Fletchery's own ledger said about Nov 3-6 on LifeShare (672 ledger rows, 117 tasks, 1,162 sessions), what
changed because of it, and the agent policy that came out of it.

## What the ledger showed

- **Most sessions did not end in a PR.** 84 rows reached a PR; 139 were blocked, 101 retried, 76 requeued. By the
  dollar estimate, 41% of the spend went to rows that were blocked or requeued.
- **Escalation repeated the same wall.** A failed task restarted from a clean branch on the next stronger agent, up
  to seven agents in a row. Most of those failures were not about code: "Hearth is outside the writable worktree",
  "a person must decide", "needs the Build PC". #991 and #992 went through six agents each and stopped at the same
  sentence every time.
- **Broken agents kept getting work.** `claude-opus-5-5` and `claude-sonnet-5-5` routed through `opencode/`,
  `gpt-6.1-terra` and `claude-haiku-5-5` passed 0 of 57 worker sessions between them: the provider refused the model
  ("Model unavailable", `provider.no-route`). Only reviewers were paused for that; workers were not.
- **The router learned from the wrong failures.** Every one of those setup failures counted against the agent, so
  Codex's measured pass rate (16 of 151 worker attempts) mostly reflects tasks nobody could finish in a LifeShare
  worktree, and the router drifted toward Claude, which spends the Claude plan.
- **One blocker loop ran 29 times.** #944's prerequisite planner kept answering "wait for #42", a PR merged long ago.
  The wait was over at once, the task ran, failed the same way, and was planned the same way: 84 sessions.
- **Nobody could see tokens.** Codex reports no cost, so about 290 Codex rows came to $2.90 between them, and the Claude figure is an
  estimate. The plans' limits count tokens, and nothing recorded them.
- **Fable was the dearest writer.** 9 rows, $62.90 estimated, 2 of 5 worker attempts passed, about $6.18 per PR
  (Opus $1.67, Sonnet $0.96, DeepSeek pro $0.48). DeepSeek pro and flash share one OpenCode Go 5-hour limit, and pro
  hit it 23 times, which also stopped flash.
- **Right now the clog is the merge lane, not the writers.** 31 PRs wait to merge, so backpressure (#105) holds new
  feature tasks. That is working as designed; faster merge rounds are the lever there, not more writers.

## What changed

1. **Token accounting.** Every session (worker, reviewer, fixer, planner, architect, merge round) writes one line to
   `<state dir>/usage.jsonl`: agent, account, role, tokens (input, output, cache reads and writes), seconds, cost.
   Claude, Codex and OpenCode all report tokens. See it in the dashboard's week dialog ("Tokens by agent"),
   `python -m fletchery usage`, and at the end of `python -m fletchery doctor`.
2. **Plan, then write.** A hard task first goes to an **architect**: a strong model, read-only, at most about 60
   lines (approach, files, order, traps, proof). The plan is saved in the issue body, so retries, escalations and
   restarts reuse it. The writing then routes as a medium task, usually to a cheaper agent. The review still runs at
   the hard level. If no architect is free, or it fails, the task is written without a plan as before.
3. **Kinds of work.** A new kind, **Blender & 3D (MCP)** (`art`), covers Blender, meshes, rigs, textures, exports and
   the Silhouette / TailorKit / Hearth tools. Codex is preferred for it whenever a Codex agent is free
   (`routing.preferredKindsBySkill`, default `{"art": ["codex"]}`); when none is free the usual score rule applies.
4. **No escalation into a wall.** A worker that stops at something outside the code (another repo, a person's
   decision, the Build PC, a denied tool) goes straight to the blocker planner. Those attempts and refused-model
   attempts no longer count against the agent's score. Escalation stops after two stronger agents
   (`routing.maxEscalations`).
5. **Refused models are paused.** A worker whose model the provider refuses is paused for at least two hours, as
   reviewers already were, and the task moves on.
6. **Cheaper reviews where they are safe.** Easy and medium work gets the cheapest reviewer that clears `reviewMin`
   (still from another vendor than the writer); hard work keeps the best one (`routing.cheapReviewLevels`).
7. **Daily caps.** `maxRunsPerDay` on an agent caps its sessions per UTC day, across every role; the count survives
   a restart.
8. **The #944 loop.** If the prerequisite planner names only finished work that the task already depends on, the task
   stays blocked and asks a person, once.

## The agent policy

| Agent | Account | Role | Why |
|---|---|---|---|
| Sonnet, Opus | Claude Max | Architect first (Sonnet, then Opus); writer for unplanned hard work; reviewer | Best Terminal-Bench of what we run (70.6, 66.4); short read-only plans spend the plan least |
| Codex (astra, sol, luna) | ChatGPT | Preferred writer for Blender & 3D; ordinary writer and reviewer otherwise | Alex: Codex is better at modelling. Its limit is separate from Claude's |
| DeepSeek flash | OpenCode Go | Cheap writer for easy and planned work | Cheapest; shares the Go limit with pro |
| DeepSeek pro | OpenCode Go | Architect only, 6 a day | Strong on SWE-bench, but heavy, and it uses up flash's shared Go limit |
| Fable | Claude Max | Off. If turned on: architect only, 3 a day, never merges | Dearest per PR, scores under Sonnet and Opus, and shares their plan, so it adds no headroom |

In the profile:

```json
"agents": [
  {"name": "fable", "kind": "claude", "model": "claude-fable-5-1", "enabled": false, "roles": ["architect"], "maxRunsPerDay": 3},
  {"name": "deepseek-pro", "kind": "opencode", "model": "opencode-go/deepseek-v4-pro", "roles": ["architect"], "maxRunsPerDay": 6}
],
"routing": {
  "mode": "scores",
  "plan": {"levels": ["hard"], "lowerTo": "medium", "architects": ["sonnet", "opus", "deepseek-pro", "fable"], "minScore": 70},
  "preferredKindsBySkill": {"art": ["codex"]},
  "maxEscalations": 2,
  "cheapReviewLevels": ["easy", "medium"]
}
```

`"plan": false` turns planning off. Everything here is in the profile, so the Agents panel's switches keep working.

## Next, once there is data

After a week of `usage.jsonl`, compare tokens per merged PR by agent and by kind of work, and move the bars or the
architect order with numbers rather than benchmarks. Plans that writers had to ignore (the worker says so in its
summary) are the signal to raise `minScore` or change the architect list.
