"""mini-serve verifier: the repository's own BlockPool, Scheduler and LLM step loop on the real
model (SmolLM2-135M, attention on the GPU), under a load that fills the KV pool while requests
share prefix blocks, so sequences holding shared blocks get preempted.

Invariants, checked after every step:
  * each block's reference count equals the number of running sequences whose block table holds
    it; a block is on the free list exactly when its count is 0; waiting sequences hold nothing;
  * the prefix cache maps hashes and blocks one to one;
  * KV slots: a shadow of every (block, offset) the engine writes; each row the engine computes
    must write only into a block no one else holds, and its attention must read exactly its own
    sequence's tokens at positions 0..pos through its block table;
  * at rest, every block is free;
  * every request's greedy output equals the same request run alone, on an empty pool with the
    prefix cache off.
Budget: generated tokens per second of the batched run, and the prefix cache must still serve
cached blocks (an implementation that never shares would make the bug unreachable).

Usage: verify.py REPO OUT_JSON   (env: SAB_SEED, SAB_MINISERVE_LIB, SAB_BUDGET_TOK_S, SAB_MIN_HITS)
"""

import json
import os
import random
import sys
import time
import traceback
from pathlib import Path

REPO, OUT = Path(sys.argv[1]).resolve(), Path(sys.argv[2])
sys.path.insert(0, str(REPO))
ROOT = Path(__file__).resolve().parent.parent.parent
LIB = Path(os.environ.get("SAB_MINISERVE_LIB", ROOT / ".cache" / "miniserve-build" / "libminiserve.dylib"))
SEED = int(os.environ.get("SAB_SEED", 1))
BUDGET = float(os.environ.get("SAB_BUDGET_TOK_S", 100))   # measured reference: see README
MIN_HITS = int(os.environ.get("SAB_MIN_HITS", 40))         # prefix-cache blocks served, measured reference: see README
NUM_BLOCKS, PAGE = 48, 16
MAX_PROBLEMS = 12


class Checked:
    """Wraps the engine; checks each forward pass against a shadow of the KV pool."""

    def __init__(self, runner, pool, problems):
        self.runner, self.pool, self.problems = runner, pool, problems
        self.kv = {}           # (block, offset) -> (sequence id, position, token)
        self.check_s = 0.0

    def profile(self):
        return self.runner.profile()

    def run(self, tokens, positions, tables, max_blocks, out_rows, seqs):
        t0 = time.perf_counter()
        P = self.pool.page_size
        start = 0
        for seq, last in zip(seqs, out_rows):
            for i in range(start, last + 1):
                blk = tables[i * max_blocks + positions[i] // P]
                if self.pool.ref[blk] != 1:
                    self.problem(f"seq {seq.id} writes position {positions[i]} into block {blk} with refcount "
                                 f"{self.pool.ref[blk]} (shared or free)")
                self.kv[(blk, positions[i] % P)] = (seq.id, positions[i], tokens[i])
            pos, history = positions[last], seq.tokens
            for p in range(pos + 1):
                blk = tables[last * max_blocks + p // P]
                got = self.kv.get((blk, p % P))
                if got is None or got[1] != p or got[2] != history[p]:
                    self.problem(f"seq {seq.id} at position {pos} would attend to slot {p} of block {blk} holding "
                                 f"{'nothing' if got is None else f'token {got[2]} of position {got[1]} (seq {got[0]})'}"
                                 f", not its token {history[p]}")
                    break
            start = last + 1
        self.check_s += time.perf_counter() - t0
        return self.runner.run(tokens, positions, tables, max_blocks, out_rows, seqs)

    def problem(self, msg):
        if len(self.problems) < MAX_PROBLEMS:
            self.problems.append(msg)


def pool_invariants(pool, sched):
    out = []
    holders = [0] * pool.num_blocks
    for s in sched.running:
        if len(set(s.blocks)) != len(s.blocks):
            out.append(f"seq {s.id} holds a block twice: {s.blocks}")
        for b in s.blocks:
            holders[b] += 1
    for s in sched.waiting:
        if s.blocks:
            out.append(f"waiting seq {s.id} still holds blocks {s.blocks}")
    for b in range(pool.num_blocks):
        if pool.ref[b] != holders[b]:
            out.append(f"block {b}: refcount {pool.ref[b]}, held by {holders[b]} running sequences")
        if (b in pool.free) != (pool.ref[b] == 0):
            out.append(f"block {b}: refcount {pool.ref[b]} but {'on' if b in pool.free else 'off'} the free list")
    if any(pool.hash_of.get(b) != h for h, b in pool.by_hash.items()) or len(pool.hash_of) != len(pool.by_hash):
        out.append("prefix cache: by_hash and hash_of disagree")
    return out


def workload(rng):
    """Three system prompts of 3-4 full blocks; 36 requests, each one of them plus its own text."""
    systems = [[rng.randrange(200, 40000) for _ in range(rng.choice((48, 56, 64)))] for _ in range(3)]
    reqs = []
    for i in range(36):
        sysp = systems[i % 3]
        reqs.append((sysp + [rng.randrange(200, 40000) for _ in range(rng.randrange(4, 30))], rng.randrange(12, 40)))
    return reqs


def main():
    from mini_serve.blocks import BlockPool
    from mini_serve.engine import Runner
    from mini_serve.llm import LLM
    from mini_serve.scheduler import Scheduler, Sequence

    res = {"pass": False, "problems": [], "metrics": {}}
    problems = res["problems"]
    m = res["metrics"]
    runner = Runner(NUM_BLOCKS, page_size=PAGE, kv_f16=True, lib=LIB)
    reqs = workload(random.Random(SEED))

    # Batched: 8 requests at once, then 4 more every 6 steps, so admissions find cached prefixes
    # while the pool is full and decode steps preempt.
    pool = BlockPool(NUM_BLOCKS, PAGE)
    sched = Scheduler(pool)
    checked = Checked(runner, pool, problems)
    llm = LLM(checked, sched)
    seqs = [Sequence(list(p), max_tokens=n) for p, n in reqs]
    pending = list(seqs)
    steps, inv_s, shared_victims, first_broken = 0, 0.0, 0, 0
    t0 = time.perf_counter()
    error = None
    while pending or sched.has_work():
        if pending and (steps == 0 or steps % 6 == 0):
            for s in pending[:8 if steps == 0 else 4]:
                sched.add(s)
            pending = pending[8 if steps == 0 else 4:]
        before = {s: (s.preemptions, any(pool.ref[b] > 1 for b in s.blocks)) for s in sched.running}
        try:
            out = llm.step()
        except Exception as e:  # the code under test broke (an assertion in the pool, exhaustion)
            error = f"step {steps}: {type(e).__name__}: {e}"
            break
        steps += 1
        if out is None and not pending:
            break
        t1 = time.perf_counter()
        shared_victims += sum(1 for s, (n, shared) in before.items() if shared and s.preemptions > n)
        broken = pool_invariants(pool, sched)
        if broken and not first_broken:  # the first step that breaks them; later ones only repeat it
            first_broken = steps
            problems.extend(f"after step {steps}: {p}" for p in broken[:3])
        inv_s += time.perf_counter() - t1
        if steps > 5000:
            error = "more than 5000 steps: the scheduler is not making progress"
            break
    wall = time.perf_counter() - t0 - inv_s - checked.check_s
    if error:
        problems.insert(0, error)

    if not error:
        if pool.num_free() != pool.num_blocks or any(pool.ref):
            problems.append(f"at rest: {pool.num_free()} of {pool.num_blocks} blocks free, "
                            f"{sum(1 for r in pool.ref if r)} with a nonzero refcount")
        unfinished = [s.id for s in seqs if not s.finished]
        if unfinished:
            problems.append(f"{len(unfinished)} requests never finished: {unfinished[:8]}")

        # Each request alone: empty pool, no prefix cache, nothing to share or preempt.
        wrong = []
        for (p, n), s in zip(reqs, seqs):
            solo_pool = BlockPool(NUM_BLOCKS, PAGE, prefix_cache=False)
            solo = Sequence(list(p), max_tokens=n)
            LLM(runner, Scheduler(solo_pool)).generate([solo])
            if solo.output != s.output:
                k = next((i for i, (a, b) in enumerate(zip(solo.output, s.output)) if a != b), len(s.output))
                wrong.append(f"request {s.id}: output differs from the request run alone from token {k} "
                             f"({len(s.output)} of {n} tokens generated)")
        if wrong:
            problems.insert(0, f"{len(wrong)} of {len(seqs)} outputs differ from the same request run alone")
            problems.extend(wrong[:3])

    gen = sum(len(s.output) for s in seqs)
    m.update(requests=len(seqs), steps=steps, outputs_differing=None if error else len(wrong), generated_tokens=gen, preemptions=sched.preemptions,
             preempted_holding_shared_blocks=shared_victims, prefix_cache_hit_blocks=pool.hits,
             seconds=round(wall, 2), tokens_per_s=round(gen / wall, 1) if wall > 0 else 0,
             budget_tokens_per_s=BUDGET, min_prefix_cache_hit_blocks=MIN_HITS,
             checker_seconds=round(inv_s + checked.check_s, 2))
    if not error and sched.preemptions == 0:
        problems.append("verifier error: the scenario caused no preemption, so it tests nothing")
    if not problems:
        if pool.hits < MIN_HITS:
            problems.append(f"prefix cache served {pool.hits} blocks, at least {MIN_HITS} required "
                            "(the shared system prompts must still be shared)")
        elif m["tokens_per_s"] < BUDGET:
            problems.append(f"performance budget: {m['tokens_per_s']} generated tokens/s, budget {BUDGET}")
    res["pass"] = not problems
    return res


if __name__ == "__main__":
    try:
        res = main()
    except Exception as e:
        traceback.print_exc()
        res = {"pass": False, "problems": [f"verifier error: {type(e).__name__}: {e}"], "metrics": {}}
    OUT.write_text(json.dumps(res, indent=2) + "\n")
    print(json.dumps(res, indent=2))
    sys.exit(0 if res["pass"] else 1)
