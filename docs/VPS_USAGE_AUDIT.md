# VPS Usage Audit

Findings from a full disk/resource assessment of the production host
(`alert.d-t.pw` / `fairview-host`) on 2026-07-20, plus a repeatable checklist
for doing this again later. See also [LOG_ROTATION.md](LOG_ROTATION.md) for
the log-rotation/archiving mechanics referenced below.

## Checklist for a full usage assessment

```bash
# System-level
df -h                                          # disk overview
free -h                                        # memory
uptime; nproc                                  # load/CPU

# Alert Manager data directory breakdown
du -sh /opt/docker/alert-manager/*
du -sh /opt/docker/alert-manager/data/*
du -sh /opt/docker/alert-manager/data/logs/*
du -sh /opt/docker/alert-manager/data/logs/ops/*   | sort -rh
du -sh /opt/docker/alert-manager/data/logs/file/*  | sort -rh

# Docker resource usage (read-only inspection only — see CLAUDE.md:
# never run mutating docker/docker-compose commands without the user)
docker system df -v      # images/containers/volumes + reclaimable space
docker ps --format 'table {{.Names}}\t{{.Status}}'
```

If a client's `file/` directory looks unexpectedly large, don't stop at
total size — break it down by week (`ls -la`, `wc -l`) and sample content
(`grep`/`awk` on level, category, and source/title fields) before assuming
it's a rotation problem. See the `acme-co` finding below for why.

## Findings — 2026-07-20 snapshot

### System health: fine
- Disk: 44G/120G used (39%), 71G free.
- Memory: 3.8G total, 2.5G available, load average ~0.01-0.07 (idle).
- Host uptime: 188 days.

### Docker image cache: 3.2GB reclaimable (biggest single win on the box)

`docker system df -v` showed 79 images total, only **8 actively used** by
running containers. **70 are dangling** `<none>` images — leftover build
layers from repeated `docker-compose build` runs over the past ~8 months,
never pruned.

```
TYPE      TOTAL   ACTIVE   SIZE      RECLAIMABLE
Images    79      8        4.653GB   3.231GB (69%)
```

This dwarfs anything recovered from log rotation/archiving (~35MB). Not
acted on automatically per CLAUDE.md (no docker commands run without the
user). To reclaim, run on the server:

```bash
docker image prune -f     # removes only dangling/untagged layers — safe,
                          # doesn't touch images backing running containers
```

### `acme-co` file-log volume: 401MB, and it's not errors

Per-week line counts jumped **19x** starting the week of 2026-05-04 and have
stayed there since:

| Week | Lines |
|---|---|
| 2026-04-20 | 4,219 |
| 2026-04-27 | 13,169 |
| **2026-05-04** | **247,176** |
| 2026-05-11 – 2026-07-13 | steady 247k–324k/week |

Level breakdown for a full week (2026-07-13, 320,296 lines): **94% `info`**,
6% `warning`, 0.04% `critical`. Average line length: only 130 bytes. So this
isn't oversized messages or stack traces — it's sheer **line count** from
routine operations logged one-alert-per-item instead of batched:

| Source | Count (2026-07-13 week) | % of week |
|---|---|---|
| `product.py:680/659 deleteProduct` | 219,640 | 68.6% |
| `products.py:273/246 bulk_product_update` | 96,338 | 30.1% |
| everything else (shelf_tag, sync summaries, price checks, etc.) | ~4,300 | 1.3% |

The client already knows how to batch — it also sends a tidy "6-Hour Sync
Summary" alert (`alert_logger.py:400`) aggregating the same kind of activity
into one line. Somewhere around 2026-05-04, the per-item `deleteProduct` /
`bulk_product_update` code paths started firing an individual alert per row
instead of relying on that summary path.

**Status**: client-side fix, owned by the `acme-co` maintainer (not an
alert-manager bug). At the current rate this is ~1.8GB/year for one client
if left unaddressed, growing — the 90-day archive sweep (see
[LOG_ROTATION.md](LOG_ROTATION.md)) only delays disk pressure, it doesn't
fix the generation rate.

Options considered, if a manager-side mitigation is ever wanted instead of
(or in addition to) the client fix:
1. Routing rule to drop/downgrade `info`-level `application` alerts from
   `acme-co` matching `product.py`/`products.py` sources.
2. Shorter retention window specifically for `acme-co` (e.g. 30 days
   instead of the general 90-day sweep).
