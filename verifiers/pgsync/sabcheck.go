// sabcheck is the stateful-agent-bench verifier for pg-search-sync. It is
// copied into the repository under test as cmd/sabcheck and built with that
// repository's go.mod, but it imports nothing from it: it decodes the topic
// with its own types, so the code under test cannot grade itself.
//
// It runs the real cdc binary against a fresh database and topic, writes a
// mixed insert/update/delete/re-insert load, SIGKILLs and restarts the
// connector while the writes continue, then compares the topic with the
// table the way Elasticsearch external versioning would resolve it: per id,
// the event with the highest version must be the current row (same version,
// not deleted), or a tombstone if the row is gone.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"math/rand/v2"
	"net"
	"net/url"
	"os"
	"os/exec"
	"strconv"
	"sync"
	"syscall"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/segmentio/kafka-go"
)

type event struct {
	Op  string `json:"op"`
	Doc struct {
		ID      int64 `json:"id"`
		Version int64 `json:"version"`
		Deleted bool  `json:"deleted"`
	} `json:"doc"`
}

type result struct {
	Pass     bool           `json:"pass"`
	Problems []string       `json:"problems"`
	Metrics  map[string]any `json:"metrics"`
}

func main() {
	cdcBin := flag.String("cdc", "", "cdc binary under test")
	initSQL := flag.String("init", "", "deploy/postgres/init.sql (pristine)")
	out := flag.String("out", "", "result JSON")
	admin := flag.String("admin", "postgres://postgres:postgres@localhost:25432/postgres?sslmode=disable", "")
	brokers := flag.String("brokers", "localhost:29192", "")
	seconds := flag.Int("seconds", 16, "write load duration")
	rate := flag.Int("rate", 400, "writes per second")
	seed := flag.Uint64("seed", 1, "")
	budget := flag.Duration("budget", 30*time.Second, "max time from the end of the writes to a converged topic")
	logPath := flag.String("log", "/dev/null", "cdc output")
	flag.Parse()

	r := &result{Metrics: map[string]any{}}
	if err := run(r, *cdcBin, *initSQL, *admin, *brokers, *seconds, *rate, *seed, *budget, *logPath); err != nil {
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

func run(r *result, cdcBin, initSQL, admin, brokers string, seconds, rate int, seed uint64, budget time.Duration, logPath string) error {
	ctx := context.Background()
	name := fmt.Sprintf("pgs_%d_%d", time.Now().UnixNano()%1e9, os.Getpid())
	adm, err := pgx.Connect(ctx, admin)
	if err != nil {
		return err
	}
	defer adm.Close(ctx)
	if _, err := adm.Exec(ctx, "CREATE DATABASE "+name); err != nil {
		return err
	}
	u, _ := url.Parse(admin)
	u.Path = "/" + name
	defer func() {
		adm.Exec(ctx, `SELECT pg_terminate_backend(active_pid) FROM pg_replication_slots WHERE slot_name = $1 AND active_pid IS NOT NULL`, name)
		time.Sleep(500 * time.Millisecond)
		adm.Exec(ctx, `SELECT pg_drop_replication_slot(slot_name) FROM pg_replication_slots WHERE slot_name = $1`, name)
		adm.Exec(ctx, "DROP DATABASE IF EXISTS "+name+" WITH (FORCE)")
		deleteTopic(brokers, name)
	}()
	db, err := pgxpool.New(ctx, u.String())
	if err != nil {
		return err
	}
	defer db.Close()
	sql, err := os.ReadFile(initSQL)
	if err != nil {
		return err
	}
	if _, err := db.Exec(ctx, string(sql)); err != nil {
		return err
	}
	if err := createTopic(brokers, name); err != nil {
		return err
	}

	logf, err := os.OpenFile(logPath, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return err
	}
	defer logf.Close()
	port, _ := strconv.Atoi(u.Port())
	env := append(os.Environ(), "PG_HOST="+u.Hostname(), "PG_PORT="+strconv.Itoa(port), "PG_USER=postgres",
		"PG_PASSWORD=postgres", "PG_DB="+name, "SLOT="+name, "TOPIC="+name, "KAFKA_BROKERS="+brokers,
		"METRIC_PORT="+strconv.Itoa(freePort()))
	c := &connector{bin: cdcBin, env: env, log: logf}
	if err := c.start(); err != nil {
		return err
	}
	defer c.stop(syscall.SIGTERM)
	if err := waitSlot(ctx, db, name, 30*time.Second); err != nil {
		return err
	}

	// Load runs in the background while the connector is killed and restarted.
	w := &writer{db: db, rng: rand.New(rand.NewPCG(seed, 11))}
	if err := w.seed(ctx, 200); err != nil {
		return err
	}
	stopLoad := make(chan struct{})
	var loadWG sync.WaitGroup
	loadWG.Go(func() { w.load(ctx, rate, stopLoad) })
	step := time.Duration(seconds) * time.Second / 8
	time.Sleep(2 * step)
	kills := 0
	for _, down := range []time.Duration{step, step / 2} { // kill -9 twice, writes continue while it is down
		c.stop(syscall.SIGKILL)
		kills++
		time.Sleep(down)
		if err := c.start(); err != nil {
			return err
		}
		time.Sleep(2 * step)
	}
	c.stop(syscall.SIGTERM) // and one graceful restart
	if err := c.start(); err != nil {
		return err
	}
	time.Sleep(step)
	close(stopLoad)
	loadWG.Wait()
	writesDone := time.Now()
	r.Metrics["writes"] = w.ops
	r.Metrics["kills"] = kills

	// Converged = the topic resolves to exactly the table. Poll until it does
	// or the budget runs out, then report what is still wrong.
	var problems []string
	for {
		problems, err = compare(ctx, db, brokers, name, r.Metrics)
		if err != nil {
			return err
		}
		if len(problems) == 0 || time.Since(writesDone) > budget+15*time.Second {
			break
		}
		time.Sleep(time.Second)
	}
	converged := time.Since(writesDone)
	r.Metrics["converged_s"] = converged.Seconds()
	r.Metrics["budget_s"] = budget.Seconds()
	r.Metrics["unexpected_exits"] = c.unexpected
	r.Problems = append(r.Problems, problems...)
	if len(problems) == 0 && converged > budget {
		r.Problems = append(r.Problems, fmt.Sprintf("performance budget: converged %.1fs after the writes stopped, budget %.0fs", converged.Seconds(), budget.Seconds()))
	}
	for _, e := range c.unexpected {
		r.Problems = append(r.Problems, "cdc exited on its own: "+e)
	}
	return nil
}

// compare resolves the topic per id (highest version wins, as with
// version_type=external) and checks it against the table.
func compare(ctx context.Context, db *pgxpool.Pool, brokers, topic string, m map[string]any) ([]string, error) {
	latest, n, err := readTopic(brokers, topic)
	if err != nil {
		return nil, err
	}
	rows, err := db.Query(ctx, `SELECT id, version FROM products`)
	if err != nil {
		return nil, err
	}
	table := map[int64]int64{}
	for rows.Next() {
		var id, v int64
		if err := rows.Scan(&id, &v); err != nil {
			return nil, err
		}
		table[id] = v
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	var missing, stale, extra int
	var examples []string
	note := func(s string) {
		if len(examples) < 5 {
			examples = append(examples, s)
		}
	}
	for id, v := range table {
		e, ok := latest[id]
		switch {
		case !ok || e.Doc.Deleted:
			missing++
			note(fmt.Sprintf("id %d (version %d) not searchable", id, v))
		case e.Doc.Version != v:
			stale++
			note(fmt.Sprintf("id %d: index has version %d, table %d", id, e.Doc.Version, v))
		}
	}
	for id, e := range latest {
		if _, ok := table[id]; !ok && !e.Doc.Deleted {
			extra++
			note(fmt.Sprintf("id %d deleted in the table, still searchable at version %d", id, e.Doc.Version))
		}
	}
	m["rows"], m["topic_events"], m["missing"], m["stale"], m["extra"] = len(table), n, missing, stale, extra
	if missing+stale+extra == 0 {
		return nil, nil
	}
	return append([]string{fmt.Sprintf("%d missing, %d stale, %d extra documents", missing, stale, extra)}, examples...), nil
}

func readTopic(brokers, topic string) (map[int64]event, int, error) {
	conn, err := kafka.Dial("tcp", brokers)
	if err != nil {
		return nil, 0, err
	}
	parts, err := conn.ReadPartitions(topic)
	conn.Close()
	if err != nil {
		return nil, 0, err
	}
	latest := map[int64]event{}
	n := 0
	for _, p := range parts {
		lc, err := kafka.DialLeader(context.Background(), "tcp", brokers, topic, p.ID)
		if err != nil {
			return nil, 0, err
		}
		first, last, err := lc.ReadOffsets()
		lc.Close()
		if err != nil {
			return nil, 0, err
		}
		if last <= first {
			continue
		}
		rd := kafka.NewReader(kafka.ReaderConfig{Brokers: []string{brokers}, Topic: topic, Partition: p.ID, MaxWait: 100 * time.Millisecond})
		rd.SetOffset(first)
		for {
			ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			msg, err := rd.ReadMessage(ctx)
			cancel()
			if err != nil {
				rd.Close()
				return nil, 0, fmt.Errorf("partition %d at offset %d of %d: %w", p.ID, rd.Offset(), last, err)
			}
			n++
			var e event
			if json.Unmarshal(msg.Value, &e) == nil && e.Doc.ID != 0 {
				if cur, ok := latest[e.Doc.ID]; !ok || e.Doc.Version > cur.Doc.Version {
					latest[e.Doc.ID] = e
				}
			}
			if msg.Offset >= last-1 {
				break
			}
		}
		rd.Close()
	}
	return latest, n, nil
}

type writer struct {
	db  *pgxpool.Pool
	rng *rand.Rand

	mu      sync.Mutex
	live    []int64
	deleted []int64
	ops     map[string]int
	sku     int
}

func (w *writer) seed(ctx context.Context, n int) error {
	w.ops = map[string]int{}
	for range n {
		if err := w.insert(ctx, 0); err != nil {
			return err
		}
	}
	return nil
}

func (w *writer) insert(ctx context.Context, id int64) error {
	w.mu.Lock()
	w.sku++
	sku := w.sku
	w.mu.Unlock()
	q := `INSERT INTO products (sku, title, brand, category, price_cents, stock, version, updated_at)
	      VALUES ($1, 'slim jeans', 'acme', 'denim', $2, 5, 0, now()) RETURNING id`
	args := []any{fmt.Sprintf("sku-%d", sku), 1000 + sku}
	if id != 0 {
		q = `INSERT INTO products (id, sku, title, brand, category, price_cents, stock, version, updated_at)
		     VALUES ($3, $1, 'slim jeans', 'acme', 'denim', $2, 5, 0, now()) RETURNING id`
		args = append(args, id)
	}
	var got int64
	if err := w.db.QueryRow(ctx, q, args...).Scan(&got); err != nil {
		return err
	}
	w.mu.Lock()
	w.live = append(w.live, got)
	if id != 0 {
		w.ops["reinsert"]++
	} else {
		w.ops["insert"]++
	}
	w.mu.Unlock()
	return nil
}

// load issues rate writes/s from 8 connections: 60% update, 20% insert,
// 12% delete, 8% re-insert of a deleted id (a re-insert must beat its
// tombstone, which only holds if versions are carried end to end).
func (w *writer) load(ctx context.Context, rate int, stop chan struct{}) {
	jobs := make(chan float64)
	var wg sync.WaitGroup
	for range 8 {
		wg.Go(func() {
			for x := range jobs {
				w.one(ctx, x)
			}
		})
	}
	t := time.NewTicker(time.Second / time.Duration(rate))
	defer t.Stop()
	for {
		select {
		case <-stop:
			close(jobs)
			wg.Wait()
			return
		case <-t.C:
			w.mu.Lock()
			x := w.rng.Float64()
			w.mu.Unlock()
			jobs <- x
		}
	}
}

func (w *writer) one(ctx context.Context, x float64) {
	w.mu.Lock()
	var id int64
	pick := func(s *[]int64) int64 {
		if len(*s) == 0 {
			return 0
		}
		i := w.rng.IntN(len(*s))
		v := (*s)[i]
		(*s)[i] = (*s)[len(*s)-1]
		*s = (*s)[:len(*s)-1]
		return v
	}
	switch {
	case x < 0.60:
		if len(w.live) > 0 {
			id = w.live[w.rng.IntN(len(w.live))]
		}
		w.mu.Unlock()
		if id != 0 {
			if _, err := w.db.Exec(ctx, `UPDATE products SET price_cents = price_cents + 1, stock = stock + 1 WHERE id = $1`, id); err == nil {
				w.count("update")
			}
		}
	case x < 0.80:
		w.mu.Unlock()
		w.insert(ctx, 0)
	case x < 0.92:
		id = pick(&w.live)
		w.mu.Unlock()
		if id != 0 {
			if _, err := w.db.Exec(ctx, `DELETE FROM products WHERE id = $1`, id); err == nil {
				w.mu.Lock()
				w.deleted = append(w.deleted, id)
				w.ops["delete"]++
				w.mu.Unlock()
			}
		}
	default:
		id = pick(&w.deleted)
		w.mu.Unlock()
		if id != 0 {
			w.insert(ctx, id)
		}
	}
}

func (w *writer) count(op string) {
	w.mu.Lock()
	w.ops[op]++
	w.mu.Unlock()
}

// connector supervises the cdc process. Exits it did not cause are recorded.
type connector struct {
	bin string
	env []string
	log *os.File

	mu         sync.Mutex
	cmd        *exec.Cmd
	done       chan struct{}
	stopping   bool
	unexpected []string
}

func (c *connector) start() error {
	cmd := exec.Command(c.bin)
	cmd.Env, cmd.Stdout, cmd.Stderr = c.env, c.log, c.log
	fmt.Fprintf(c.log, "--- %s start\n", time.Now().Format(time.RFC3339Nano))
	if err := cmd.Start(); err != nil {
		return err
	}
	done := make(chan struct{})
	c.mu.Lock()
	c.cmd, c.done, c.stopping = cmd, done, false
	c.mu.Unlock()
	go func() {
		err := cmd.Wait()
		c.mu.Lock()
		if !c.stopping {
			c.unexpected = append(c.unexpected, fmt.Sprint(err))
		}
		c.mu.Unlock()
		close(done)
	}()
	return nil
}

func (c *connector) stop(sig syscall.Signal) {
	c.mu.Lock()
	cmd, done := c.cmd, c.done
	c.stopping = true
	c.mu.Unlock()
	if cmd == nil {
		return
	}
	cmd.Process.Signal(sig)
	select {
	case <-done:
	case <-time.After(10 * time.Second):
		cmd.Process.Kill()
		<-done
	}
}

func waitSlot(ctx context.Context, db *pgxpool.Pool, slot string, d time.Duration) error {
	deadline := time.Now().Add(d)
	for time.Now().Before(deadline) {
		var active bool
		err := db.QueryRow(ctx, `SELECT active FROM pg_replication_slots WHERE slot_name = $1`, slot).Scan(&active)
		if err == nil && active {
			time.Sleep(time.Second) // streaming starts right after the slot is taken
			return nil
		}
		time.Sleep(200 * time.Millisecond)
	}
	return fmt.Errorf("replication slot %s not active after %v", slot, d)
}

func createTopic(brokers, topic string) error {
	conn, err := kafka.Dial("tcp", brokers)
	if err != nil {
		return err
	}
	defer conn.Close()
	ctrl, err := conn.Controller()
	if err != nil {
		return err
	}
	cc, err := kafka.Dial("tcp", net.JoinHostPort(ctrl.Host, strconv.Itoa(ctrl.Port)))
	if err != nil {
		return err
	}
	defer cc.Close()
	return cc.CreateTopics(kafka.TopicConfig{Topic: topic, NumPartitions: 6, ReplicationFactor: 1})
}

func deleteTopic(brokers, topic string) {
	if conn, err := kafka.Dial("tcp", brokers); err == nil {
		conn.DeleteTopics(topic)
		conn.Close()
	}
}

func freePort() int {
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return 2112
	}
	defer l.Close()
	return l.Addr().(*net.TCPAddr).Port
}
