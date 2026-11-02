# Garbled completions and "double free of block" under load with a shared system prompt

We serve many requests that start with the same system prompt. Under load,
when the KV pool is full and the server has started preempting requests
(`preemptions` > 0 in the stats), some completions come back garbled: the
same request sent alone, or with a large `--num-blocks`, gives a different
and sensible greedy output. A while later the engine loop sometimes dies with
`AssertionError: double free of block N` in `BlockPool`.

It never happens without preemptions, and it never happens when the prompts
share nothing. The scheduler unit tests pass.

Expected: with greedy sampling, every request's output is the same as when it
runs alone, whatever else is running, and the block pool's accounting stays
exact: when the server is idle, every block is free.


## Your environment

The repository is checked out in `/workspace`. There is no network and no GPU
in your sandbox: the model and the engine library cannot run there, and
nothing can be downloaded. The scheduler tests (`python3 -m unittest
tests.test_scheduler`) need neither. Change the code so the problem is fixed.
Your change is verified outside the sandbox by running the real engine on the
real model under a load that fills the KV pool and checking the block pool's
invariants and every output against the same request run alone; the engine,
the tests and the scripts are restored to their original version before that
run, so changing them has no effect.
