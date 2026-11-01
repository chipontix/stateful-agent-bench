// sabrace is the concurrency half of the stateful-agent-bench verifier for
// ledger-saga. It is copied into the repository under test as cmd/sabrace and
// drives that repository's real ledger.Store against a real Postgres, the way
// the ledger consumer does after a backlog: commands of one saga arrive
// together and are handled at the same time.
//
// Per saga, after a start barrier, one of two pairs runs concurrently:
//
//	race:  hold(attempt 1) || release(attempt 1)    (a hold timing out while it is still queued)
//	retry: hold(1) || hold(2), then capture(1) || capture(2), then credit(1) || credit(2)
//
// A handler error is retried, as the consumer would. Then, per saga, the
// recorded ledger operations must be ones the saga's final state allows
// (saga.CheckEffects, the project's own rule) and the money must have moved
// exactly once; globally the journal must balance and nothing may stay held.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"math/rand/v2"
	"net/url"
	"os"
	"sync"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/chipontix/ledger-saga/internal/db"
	"github.com/chipontix/ledger-saga/internal/ledger"
	"github.com/chipontix/ledger-saga/internal/saga"
	"github.com/chipontix/ledger-saga/internal/wire"
)

const accounts = 40

type result struct {
	Pass     bool           `json:"pass"`
	Problems []string       `json:"problems"`
	Metrics  map[string]any `json:"metrics"`
}

type sagaRun struct {
	id       uuid.UUID
	kind     string // race | retry
	src, dst int64
	amount   int64
}

func main() {
	admin := flag.String("admin", "postgres://postgres:postgres@localhost:23432/postgres?sslmode=disable", "")
	out := flag.String("out", "", "result JSON")
	n := flag.Int("sagas", 600, "")
	inflight := flag.Int("inflight", 24, "sagas being handled at once")
	seed := flag.Uint64("seed", 1, "")
	budget := flag.Float64("budget", 150, "minimum handled commands per second")
	flag.Parse()
	r := &result{Metrics: map[string]any{}}
	if err := run(r, *admin, *n, *inflight, *seed, *budget); err != nil {
		r.Problems = append(r.Problems, "verifier error: "+err.Error())
	}
	r.Pass = len(r.Problems) == 0
	b, _ := json.MarshalIndent(r, "", "  ")
	os.WriteFile(*out, b, 0o644)
	fmt.Println(string(b))
	if !r.Pass {
		os.Exit(1)
	}
}

func run(r *result, admin string, n, inflight int, seed uint64, budget float64) error {
	ctx := context.Background()
	name := fmt.Sprintf("sabrace_%d_%d", time.Now().UnixNano()%1e9, os.Getpid())
	adm, err := db.Open(ctx, admin, 2)
	if err != nil {
		return err
	}
	defer adm.Close()
	if _, err := adm.Exec(ctx, "CREATE DATABASE "+name); err != nil {
		return err
	}
	defer adm.Exec(ctx, "DROP DATABASE IF EXISTS "+name+" WITH (FORCE)")
	u, _ := url.Parse(admin)
	u.Path = "/" + name
	pool, err := db.Open(ctx, u.String(), int32(2*inflight+4))
	if err != nil {
		return err
	}
	defer pool.Close()
	if err := db.Migrate(ctx, pool); err != nil {
		return err
	}
	if err := ledger.Seed(ctx, pool, accounts, 1_000_000, 0); err != nil {
		return err
	}
	store := &ledger.Store{DB: pool, Replies: "sabrace.replies"}

	rng := rand.New(rand.NewPCG(seed, 3))
	runs := make([]sagaRun, n)
	for i := range runs {
		src := int64(1 + rng.IntN(accounts))
		dst := src%accounts + 1 + int64(rng.IntN(accounts-1))
		if dst > accounts {
			dst -= accounts
		}
		if dst == src {
			dst = src%accounts + 1
		}
		kind := "race"
		if i%2 == 1 {
			kind = "retry"
		}
		runs[i] = sagaRun{uuid.Must(uuid.NewV7()), kind, src, dst, int64(1 + rng.IntN(500))}
	}

	var handled, retried, stuck int
	var mu sync.Mutex
	var problems []string
	// do runs the given commands of one saga at the same instant and retries
	// each until the handler accepts it.
	do := func(cmds ...wire.Command) {
		start := make(chan struct{})
		var wg sync.WaitGroup
		for _, c := range cmds {
			wg.Go(func() {
				<-start
				for try := 0; ; try++ {
					_, err := store.Handle(ctx, c)
					mu.Lock()
					if err == nil {
						handled++
						mu.Unlock()
						return
					}
					retried++
					if try == 20 {
						stuck++
						if len(problems) < 5 {
							problems = append(problems, fmt.Sprintf("saga %s %s attempt %d still failing after 20 tries: %v", c.SagaID, c.Step, c.Attempt, err))
						}
						mu.Unlock()
						return
					}
					mu.Unlock()
					time.Sleep(time.Duration(5+try*5) * time.Millisecond)
				}
			})
		}
		close(start)
		wg.Wait()
	}
	cmd := func(s sagaRun, step wire.Step, acct int64, attempt int) wire.Command {
		return wire.Command{MsgID: wire.CommandID(s.id, step, attempt), SagaID: s.id, Step: step, Account: acct,
			Amount: s.amount, Attempt: attempt, SentAt: time.Now()}
	}

	t0 := time.Now()
	jobs := make(chan sagaRun)
	var wg sync.WaitGroup
	for range inflight {
		wg.Go(func() {
			for s := range jobs {
				if s.kind == "race" {
					do(cmd(s, wire.Hold, s.src, 1), cmd(s, wire.Release, s.src, 1))
					continue
				}
				do(cmd(s, wire.Hold, s.src, 1), cmd(s, wire.Hold, s.src, 2))
				do(cmd(s, wire.Capture, s.src, 1), cmd(s, wire.Capture, s.src, 2))
				do(cmd(s, wire.Credit, s.dst, 1), cmd(s, wire.Credit, s.dst, 2))
			}
		})
	}
	for _, s := range runs {
		jobs <- s
	}
	close(jobs)
	wg.Wait()
	secs := time.Since(t0).Seconds()
	rate := float64(handled) / secs

	r.Problems = append(r.Problems, problems...)
	r.Problems = append(r.Problems, audit(ctx, pool, runs)...)
	r.Metrics["sagas"], r.Metrics["handled"], r.Metrics["handler_errors_retried"] = n, handled, retried
	r.Metrics["stuck_commands"], r.Metrics["seconds"] = stuck, secs
	r.Metrics["commands_per_s"], r.Metrics["budget_commands_per_s"] = rate, budget
	if len(r.Problems) == 0 && rate < budget {
		r.Problems = append(r.Problems, fmt.Sprintf("performance budget: %.0f commands/s, budget %.0f", rate, budget))
	}
	return nil
}

func audit(ctx context.Context, pool *pgxpool.Pool, runs []sagaRun) []string {
	var out []string
	add := func(format string, a ...any) {
		if len(out) < 12 {
			out = append(out, fmt.Sprintf(format, a...))
		}
	}
	var total, held int64
	if err := pool.QueryRow(ctx, `SELECT coalesce(sum(amount), 0) FROM ledger.entries`).Scan(&total); err != nil {
		return []string{err.Error()}
	}
	if total != 0 {
		add("journal sums to %d, not 0", total)
	}
	if err := pool.QueryRow(ctx, `SELECT coalesce(sum(held), 0) FROM ledger.accounts`).Scan(&held); err != nil {
		return []string{err.Error()}
	}
	if held != 0 {
		add("at rest but %d held", held)
	}
	bad := map[string]int{}
	for _, s := range runs {
		rows, err := pool.Query(ctx, `SELECT o.step, o.outcome, o.reason, coalesce(sum(e.amount) FILTER (WHERE e.account_id = $2), 0),
				coalesce(sum(e.amount) FILTER (WHERE e.account_id = $3), 0)
			FROM ledger.operations o LEFT JOIN ledger.entries e ON e.op_id = o.id
			WHERE o.saga_id = $1 GROUP BY o.id, o.step, o.outcome, o.reason`, s.id, s.src, s.dst)
		if err != nil {
			return append(out, err.Error())
		}
		ops := map[wire.Step]string{}
		count := map[wire.Step]int{}
		var src, dst int64
		for rows.Next() {
			var step wire.Step
			var outcome, reason string
			var a, b int64
			if err := rows.Scan(&step, &outcome, &reason, &a, &b); err != nil {
				rows.Close()
				return append(out, err.Error())
			}
			if step == wire.Release {
				outcome += "/" + reason
			}
			ops[step] = outcome
			count[step]++
			src += a
			dst += b
		}
		rows.Close()
		state, want := saga.Cancelled, int64(0)
		if s.kind == "retry" {
			state, want = saga.Completed, s.amount
		}
		for step, c := range count {
			if c > 1 {
				bad["applied twice"]++
				add("saga %s (%s): %s recorded %d times", s.id, s.kind, step, c)
			}
		}
		if err := saga.CheckEffects(state, ops); err != nil {
			bad["effects"]++
			add("saga %s (%s): %v", s.id, s.kind, err)
		}
		if src != -want || dst != want {
			bad["money"]++
			add("saga %s (%s) moved src %d dst %d, want %d/%d", s.id, s.kind, src, dst, -want, want)
		}
	}
	if len(bad) > 0 {
		out = append([]string{fmt.Sprintf("%d sagas with disallowed effects, %d applied twice, %d with wrong money (of %d)",
			bad["effects"], bad["applied twice"], bad["money"], len(runs))}, out...)
	}
	return out
}
