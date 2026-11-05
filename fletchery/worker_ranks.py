"""Score-based writing capacity. Eligibility and review policy stay with AgentPool."""
from collections import Counter

RANKS = ("low", "medium", "high")


def rank(score):
    return "high" if score >= 75 else "medium" if score >= 60 else "low"


def configure(engine, tasks):
    pool = engine.ctx.agents
    if pool is None:
        return None
    scores = pool.scored(engine.stats())
    pool.worker_rank_names = {name: rank(value["seed"]["coding"]) for name, value in scores.items()}
    active = Counter(r for _, r in pool.worker_slots.values())
    manual = engine.cfg.worker_rank_limits
    capacities = dict(manual) if manual is not None else {r: 0 for r in RANKS}
    if manual is None:
        remaining = engine.cfg.max_parallel
        for r in RANKS:
            capacities[r] = min(10, active[r], max(0, remaining))
            remaining -= capacities[r]
    pending = []
    alternatives = set()
    if manual is None:
        # Use the actual router for task preferences, score bars and explicit
        # overrides. Forecast rank slots without changing live agent usage.
        old_limits = pool.worker_limits
        pool.worker_limits = None
        try:
            for task in tasks:
                choice = pool.choose(task.title, task.body, task.labels, len(task.depends_on), engine.stats())
                if choice:
                    pending.append(pool.worker_rank_names[choice.agent])
                for candidate_rank in RANKS:
                    pool.worker_limits = {r: 10 if r == candidate_rank else 0 for r in RANKS}
                    if pool.choose(task.title, task.body, task.labels, len(task.depends_on), engine.stats()):
                        alternatives.add(candidate_rank)
                pool.worker_limits = None
        finally:
            pool.worker_limits = old_limits
        demand = Counter(pending)
        available = {pool.worker_rank_names[n] for n in pool.specs if pool.available(n, "worker", ignore_capacity=True)}
        for _ in range(max(0, engine.cfg.max_parallel - sum(capacities.values()))):
            choices = [r for r in RANKS if r in available and capacities[r] < 10 and (r in alternatives or not tasks)]
            if not choices:
                break
            chosen = max(choices, key=lambda r: (demand[r] / (capacities[r] + 1), -RANKS.index(r)))
            capacities[chosen] += 1
    pool.worker_limits = capacities
    models = {r: [n for n in pool.specs if pool.worker_rank_names[n] == r and pool.specs[n].enabled and pool.specs[n].can("worker")] for r in RANKS}
    from agents import classify
    levels = Counter(classify(t.title, t.body, t.labels, pool.routing.get("hardPatterns"),
                              pool.routing.get("easyPatterns"), len(t.depends_on), pool.routing.get("hardScore"))[0] for t in tasks)
    return {"mode": "manual" if manual is not None else "automatic", "limits": capacities,
            "active": {r: active[r] for r in RANKS}, "models": models,
            "assignments": {str(t): r for t, (_, r) in pool.worker_slots.items()},
            "agent_ranks": dict(pool.worker_rank_names),
            "ready_levels": {level: levels[level] for level in ("easy", "medium", "hard")},
            "restrictions": {n: pool.specs[n].levels for n in pool.specs},
            "scores": {n: s["seed"]["coding"] for n, s in scores.items()},
            "thresholds": {"low": "below 60", "medium": "60–74", "high": "75–100"},
            "unassigned": max(0, engine.cfg.max_parallel - sum(capacities.values()))}
