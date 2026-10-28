# Phase 8a: Postgres ledger

Branch `phase-8a`. Adds a second ledger backend, `PostgresLedger`, so worker processes on
several hosts can share one database. It also adds a backend-agnostic contract suite that both
ledgers must pass, and a `ledger_from_url` factory used by the CLI, the demo workers and the
chaos harness. The engine (`scheduler.py`, `worker.py`, `context.py`, `app.py`) is unchanged.

## Layout

| File | What |
|---|---|
| `src/agentline/ledger/postgres.py` | `PostgresLedger(dsn, *, schema=None)`, `PostgresTx`, DDL, `redact_dsn` |
| `src/agentline/ledger/__init__.py` | `ledger_from_url`, `sqlite_path`, `split_schema`, `is_postgres_url` |
| `src/agentline/ledger/base.py` | `LedgerTx.claim_delivery` (new op); `Ledger.tx()` contract documented |
| `src/agentline/ledger/sqlite.py` | `webhook_deliveries` table + `claim_delivery` |
| `src/agentline/deliveries.py` | goes through `tx.claim_delivery`; no more raw SQLite connection |
| `src/agentline/cli/env.py` | `--db` / `$AGENTLINE_DB` go through `ledger_from_url` |
| `demo/{durable,pipeline}/worker.py` | `LEDGER_DB` goes through `ledger_from_url` |
| `demo/chaos.py` | `--ledger-url` for the `durable` and `pipeline` targets |
| `tests/ledger/test_contract.py` | the contract suite, parametrized over `sqlite` / `postgres` |
| `tests/ledger/test_factory.py` | factory, CLI `--db` and chaos `--ledger-url` (no server needed) |

## Design

### Ledger URLs

`ledger_from_url(url)` accepts:

- a plain path (`~` expanded), e.g. `./agentline.db`. This gives a SQLite ledger.
- `sqlite:///relative/path.db` or `sqlite:////absolute/path.db` (the SQLAlchemy convention).
- `postgresql://user:pass@host:5432/db[?schema=name&sslmode=...]` (or `postgres://`). The
  `schema` query parameter is agentline's, not libpq's. It is removed from the URL before
  connecting, and it puts every table in that Postgres schema, which is created if missing.
  All other parameters go to libpq unchanged.

The CLI (`--db` / `AGENTLINE_DB`), the demo workers (`LEDGER_DB`) and `demo.chaos
--ledger-url` all go through it. `CliEnv.db_path` (used only for display) shows a Postgres URL
with its password replaced by `***`.

### Isolation and locking

The scheduler runs its logic in Python inside one `ledger.tx()`: it reads rows, decides, and
writes. Join tokens (`tokens_live` / `tokens_skip`), loop counters, `refresh_run` and
`retry_run` are read-modify-write sequences. They are correct on SQLite because every
transaction is `BEGIN IMMEDIATE`, so write transactions run one at a time. The Postgres ledger
keeps that guarantee and makes claims concurrent:

1. **Claims run concurrently.** `claim_next` is a single statement:

   ```sql
   WITH next AS (
     SELECT id FROM executions
     WHERE status IN ('ready', 'failed') AND next_attempt_at <= $now
     ORDER BY next_attempt_at, created_at, id LIMIT 1
     FOR UPDATE SKIP LOCKED)
   UPDATE executions e SET status = 'running', lease_owner = $owner,
          lease_expires = $now + $ttl, lease_epoch = e.lease_epoch + 1, started_at = $now
   FROM next WHERE e.id = next.id RETURNING e.*
   ```

   Concurrent claimers skip rows that another claimer has locked, so no execution is claimed
   twice. Under READ COMMITTED, `FOR UPDATE` re-checks the `WHERE` clause against the newest
   committed row version. A row that another transaction moved out of `ready` / `failed` is
   therefore never claimed. A claim-only transaction (which is what `Worker._claim` runs) does
   not take the transaction lock below.
2. **Every other transaction is serialized.** The first operation in a transaction, other than
   `claim_next`, takes `pg_advisory_xact_lock(k)`. `k` is a 64-bit key derived from the
   schema name. The lock is released at commit or rollback, including when the client
   process is SIGKILLed (the server aborts the transaction when the socket closes). This
   matches SQLite's `BEGIN IMMEDIATE` across processes and hosts, so the scheduler needs no
   retry loop.
3. **Isolation level is READ COMMITTED.** Once the lock is held, every statement sees every
   transaction committed before it. The only concurrent writers are claims, and they change
   only a `ready`/`failed` row to `running`. The scheduler's reads of such a row are either
   before the claim (it is still `ready`) or after it (it is `running`). Either way is a state
   SQLite could also produce.
4. **Fencing is unchanged.** `update_execution(expect_status=, expect_epoch=)` is a conditional
   `UPDATE ... WHERE id = ? AND status = ? AND lease_epoch = ?`. It returns whether one row
   changed. READ COMMITTED evaluates that condition on the newest row version (after waiting
   for any concurrent claim of the same row). So a stale worker, whose epoch was bumped by a
   reclaim plus a new claim, cannot commit. This is the same as ARCHITECTURE.md §7.

Why not SERIALIZABLE? A serialization failure has to be handled by re-running the whole
transaction body. `Ledger.tx()` is a context manager, so the ledger cannot replay the caller's
block, and the scheduler is out of scope. Under SERIALIZABLE, a conflict would surface as an
exception in `Worker._commit`. The step's result would be lost until lease expiry and
re-execution. That is correct (the effect guard holds) but wasteful.

### Multi-host claims

Each host runs `agentline worker --db postgresql://...` (or a demo worker with
`LEDGER_DB=postgresql://...`). Leases are absolute timestamps (`lease_expires = now + ttl`)
from each worker's own clock. As on SQLite, any worker's reaper reclaims an expired lease.
**Hosts need synchronized clocks (NTP).** A skew close to the lease TTL can lead to early
reclaims. Fencing still prevents a double commit, but the step runs again.

### Storage

- JSON columns (`payload_json`, `detail_json`, `result_json`, `advice_json`) are `JSONB`. They
  keep the SQLite column names so both backends map rows the same way.
- `blobs.data` is `BYTEA`. Timestamps and money are `DOUBLE PRECISION`. Auto-increment ids
  (`costs`, `events`, `artifact_links`, `audit_log`) are `BIGINT GENERATED BY DEFAULT AS
  IDENTITY`, and inserts use `RETURNING id`.
- `INSERT OR IGNORE` becomes `ON CONFLICT ... DO NOTHING`. Upserts (`effects`, `loop_counts`)
  use `ON CONFLICT ... DO UPDATE`.
- **Schema creation.** The schema is created with `CREATE TABLE IF NOT EXISTS` on every open,
  under a database-wide advisory lock, because concurrent `CREATE ... IF NOT EXISTS` can still
  collide in the catalog. A `schema_version(version, applied_at)` table records version `1`.
  If the database's version is newer than the code's, opening it fails with a clear error. Future
  migrations go in as numbered steps keyed on this table.

### Connections

The ledger keeps one connection per thread, in a `threading.local`, opened on first use. The
engine calls the ledger from `asyncio.to_thread`, so a worker process uses at most the default
executor size, `min(32, cpu + 4)` connections. Connections are in autocommit mode, and
`tx()` uses `with conn.transaction()`. That gives `BEGIN` / `COMMIT`, or `ROLLBACK` and
re-raise on any exception. This matches `SqliteLedger.tx()`. If a connection breaks
(`OperationalError` with `conn.broken`), it is dropped, and the thread's next `tx()`
reconnects. `close()` closes every connection the ledger opened.

### Deliveries table (Phase 4)

Before this phase, `deliveries.py` created `webhook_deliveries` through the SQLite connection
(`tx.c`). It also raised an error on any other backend. The table is now part of both
schemas, and the insert-if-absent is a protocol operation:
`LedgerTx.claim_delivery(source, delivery_id, now) -> bool`. `deliveries.ensure_table(tx)` is
kept for callers (`api/webhooks.py`). It now only checks that the backend supports the
operation. Existing SQLite files already have an identical table, so `IF NOT EXISTS` is a no-op
for them.

## Running against a real Postgres

```bash
docker run -d --name agentline-pg -e POSTGRES_PASSWORD=pw -p 5432:5432 postgres:16
export AGENTLINE_PG_DSN=postgresql://postgres:pw@localhost:5432/postgres

uv sync --extra postgres          # psycopg is also in the dev group, so plain `uv sync` works
uv run pytest tests/ledger -rs    # both backends; each postgres test gets its own schema
uv run pytest                     # whole suite; without the env var the postgres params skip

# chaos: every run gets a fresh schema chaos_<target>_seed<N>_<ts>
uv run python -m demo.chaos --target durable  --jobs 50 --expect-clean \
    --ledger-url "$AGENTLINE_PG_DSN"
uv run python -m demo.chaos --target pipeline --jobs 50 --expect-clean \
    --ledger-url "$AGENTLINE_PG_DSN"

# the CLI against Postgres
AGENTLINE_DB="$AGENTLINE_PG_DSN?schema=prod" uv run agentline worker
```

Chaos schemas are left behind for inspection. To clean them up, run
`DROP SCHEMA chaos_... CASCADE`. A `--ledger-url` that is a SQLite path or one that already has
`?schema=` is used as given. Reusing it across runs makes the dedupe keys of a previous run
absorb the new submissions.

**Not verified here.** No Postgres server was available while building this phase. The
contract suite's `postgres` parameters (32 tests) and the chaos commands above have not been
run against a real server yet. The SQLite side of the suite passes, and `--ledger-url` with
a SQLite path was smoke-tested with chaos (10 jobs, clean).

## Tests

`tests/ledger/test_contract.py` uses only `Ledger.tx()` and the `LedgerTx` protocol:

- A meta-test fails if any `LedgerTx` operation is never called in the file.
- Runs: round trip, any JSON payload (`null`, scalars, lists, unicode, 2^53), `update_run`,
  `list_runs` order/status/limit, `dedupe_key` uniqueness (and rollback of the failed tx),
  duplicate ids, costs.
- Executions: full-field round trip, `(run, node, iteration)` uniqueness, fenced
  `update_execution` (status, epoch, both, missing row), `open_execution` / `max_iteration`,
  `claim_next` order / due time / lease fields / epoch bump, `expired_leases`,
  `waiting_by_key`, `count_active`.
- Concurrency: 8 threads claiming 80 executions, with separate ledger instances (separate
  connections or processes' worth of state) and with one shared instance. No double claims,
  and every claim has epoch 1 and the right owner. 4 instances doing read-modify-write on one
  row lose no updates. 6 threads on one instance. Commits are visible across instances.
- Loop counters, effect upsert (every field replaced, `result` NULL vs value, `advice` `{}`),
  blobs (binary, empty, idempotent), artifacts and links (insert-if-absent, a size > 2^32),
  events (`since` inclusive, consume, non-dict payloads), `claim_delivery` (including
  rollback), audit (per run, latest `limit` oldest-first).
- Rollback on exception across every table.
- The engine end to end on each backend: fan-out/join, retry, effect, wait + event, dedupe
  across 6 runs with 4 worker slots.

Counts on this machine: 405 passed, 34 skipped (32 contract + 2 pre-existing smoke). Every
`postgres` param skips with "set AGENTLINE_PG_DSN=postgresql://... to run it".

## Known limits

- **Write throughput is one transaction at a time** per schema, as on SQLite. Claims are the
  exception. Transactions are a few milliseconds and steps take seconds (LLM calls), so this
  is not the bottleneck at v0.1 scale. Proposal 3 below is the path beyond that.
- A transaction that starts with `claim_next` and then does more work takes the row lock
  before the transaction lock. It can deadlock with a serialized transaction that touches the
  same row. Postgres detects this and aborts one of them. The engine never does this: its claim
  transaction is claim-only.
- `claim_next` may return `None` while a claimable row exists. This happens when its single
  candidate was claimed concurrently: `LIMIT 1` plus the re-check yields no row. The worker
  simply polls again.
- Two hosts submitting the same new `dedupe_key` at once are serialized by the lock, so the
  second sees the first run (as on SQLite). A writer outside agentline that bypasses the lock
  would get a unique violation instead.
- JSONB cannot store `\u0000` in strings, and it does not keep object key order or duplicate
  keys. Results are compared by value everywhere, and artifact hashes come from canonical
  JSON in Python, so neither matters today.
- A nested `ledger.tx()` on one thread becomes a savepoint on Postgres. SQLite rejects it. The
  engine never nests.
- `poller._waiting` has a SQLite fast path (`tx.c`). On Postgres it falls back to the
  protocol path (list runs by status, then their executions). That path is correct but does
  more queries. A `LedgerTx.list_waiting()` op would fix it (`poller.py` was out of scope).
- `api/dev.py` still builds `SqliteLedger(args.db)` directly (out of scope). It should use
  `ledger_from_url`.
- `tests/artifacts/test_store.py` counts rows through `tx.c`, so it stays SQLite-only.

## Decisions (proposals, numbered for the log)

1. **Lock-free claims plus one serializing advisory lock per schema** for everything else,
   at READ COMMITTED. This keeps the scheduler's read-modify-write logic correct without
   retries, and it lets many hosts claim at once.
2. **No SERIALIZABLE.** The ledger cannot replay a `tx()` body, so a serialization failure
   would surface as a lost commit.
3. **Future: per-run locks.** Almost all read-modify-write is within one run. Replacing the
   global lock with `pg_advisory_xact_lock(hash(run_id))` would let transitions of different
   runs commit in parallel. It needs either a declared run id at `tx()` start (a scheduler
   change) or lock-order discipline for multi-run transactions (`deliver_event`,
   `reclaim_expired`).
4. **`LedgerTx.claim_delivery` joins the protocol,** and `webhook_deliveries` moves into both
   schemas. This replaces raw SQL on the SQLite connection.
5. **`?schema=` in Postgres URLs** gives each tenant, test or chaos run its own namespace in one
   database.
6. **psycopg 3 as the `postgres` extra** (`psycopg[binary]>=3.2`), imported lazily with an
   error naming the extra. It is also in the dev group so the suite can import it.
7. **One connection per thread, no pool.** The thread count is bounded by the executor. A pool
   (`psycopg_pool`) can replace this later behind the same `tx()`.
8. **Same column names as SQLite** (`*_json` even when the type is JSONB), so row mapping is
   the same in both backends.
9. **`schema_version` table, version 1**, and opening a newer schema refuses to run. Migrations
   are numbered steps, added when the first schema change lands.
