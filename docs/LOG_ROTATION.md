# Ops Log Rotation

## Known Issue: `data/logs/ops/*.log` does not rotate by date

The `{date}` placeholder in ops log paths (e.g. `alert-router.{date}.log`,
`mosquitto.log`) is only evaluated once, when the process/handler is
initialized. It does **not** re-evaluate at midnight. In practice this means:

- A log file keeps the filename it had when the container last started.
- It grows unbounded for the entire lifetime of the container, regardless of
  how many days pass.
- Restarting the container is the only thing that produces a new dated
  filename.

Observed in production (`alert.d-t.pw`) on 2026-07-20: `alert-router` and
`mosquitto` had both been running for weeks/months without a restart, leaving:

- `alert-router.2026-02-14.log` at 864MB
- `mosquitto.log` at 2.7GB

This only affects `data/logs/ops/` (operational/service logs). Alert content
logs under `data/logs/file/` are unaffected — those rotate correctly because
each file pathway re-derives its path (including `{date}`) per write.

## Safe manual rotation (no restart required)

Both `alert-router`'s Python `logging.FileHandler` and mosquitto's file
logging open their log file in **append mode** (`O_APPEND`). With
`O_APPEND`, the kernel always writes at the current end-of-file — it ignores
the file descriptor's cached offset. That means truncating the file to zero
length is safe: the next write from the still-running process lands at
offset 0, and logging continues with no gap, no restart, no lost writes.

This is the standard `copytruncate` technique used by `logrotate`.

```bash
# 1. Archive the existing (oversized) log, compressed
mkdir -p /opt/docker/alert-manager/data/logs/ops/archive
gzip -c /opt/docker/alert-manager/data/logs/ops/<name>.log \
  > /opt/docker/alert-manager/data/logs/ops/archive/<name>.log.gz

# 2. Truncate the live file in place — do NOT delete/rename it while the
#    container is running, or the process's held file descriptor keeps
#    writing to the (now unlinked or differently-named) inode instead of
#    a fresh file at the original path.
truncate -s 0 /opt/docker/alert-manager/data/logs/ops/<name>.log

# 3. Verify the process resumed writing
tail -f /opt/docker/alert-manager/data/logs/ops/<name>.log
```

Notes:
- If the log is actively being written while `gzip` runs, you may see
  `gzip: file size changed while zipping`. This is harmless — it just means
  the last few lines written during compression aren't in the archive
  (they're still safe in the live file until the truncate step runs
  immediately after).
- `mv`/rename does **not** achieve rotation on its own: the process keeps its
  open file descriptor pointing at the same inode regardless of what path it
  is linked under, so it will keep appending to the renamed file forever
  (harmless, but doesn't stop growth and doesn't restore the original
  filename for new writes). `truncate -s 0` on the *original path* is the
  part that actually matters.
- This does not fix the underlying rotation logic — it's a manual
  workaround. A proper fix would use a `TimedRotatingFileHandler` (or
  equivalent) for ops logging, or rely on `logrotate` with a `copytruncate`
  directive and a cron/systemd timer.

## Verified

Both `alert-router.2026-02-14.log` and `mosquitto.log` were rotated this way
in production on 2026-07-20 with no service interruption — confirmed by
tailing each file immediately after truncation and seeing new entries
(health-check line for the router, MQTT client connect/disconnect lines for
mosquitto).

## File pathway logs: age-based archiving (retention, not rotation)

Unlike `ops/`, `data/logs/file/` (per-client alert content, weekly-rotated)
already rotates correctly — each week gets its own file, and past weeks are
never appended to again. There's no live-writer hazard here, so old files
can simply be compressed and moved, no truncate/copytruncate needed.

There's no automated retention policy yet, so past-dated files just
accumulate. Manual sweep used in production on 2026-07-20 (cutoff: mtime
older than 90 days):

```bash
BASE=/opt/docker/alert-manager/data/logs/file
ARCHIVE=$BASE/archive

find "$BASE" -type f -name '*.log' -mtime +90 | while IFS= read -r f; do
  rel="${f#$BASE/}"                     # e.g. acme-co/2025-12-29.log
  dest="$ARCHIVE/${rel}.gz"
  mkdir -p "$(dirname "$dest")"
  gzip -c "$f" > "$dest"
  gzip -t "$dest" && rm "$f"             # only delete original after a verified-good archive
done
```

Result on 2026-07-20: 83 files (~35MB) across all clients compressed to
~2MB in `data/logs/file/archive/{client}/{date}.log.gz`, mirroring the
original per-client directory structure. Zero integrity failures.

Note: this only trims *old* weeks. If a client's directory is large, it's
usually because of high *recent* volume (e.g. `acme-co` was 401MB of
logs all within the 90-day window after this sweep) — that's a retention
policy or message-volume problem, not something age-based archiving
addresses. Consider `sanitize_messages`/`max_message_length` (see main
CLAUDE.md) or a shorter retention window for chatty clients.
