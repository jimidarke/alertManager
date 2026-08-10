"""
Alert Manager - gateway.logs.files

Path-traversal defense and log-file authorization for the `logs`
blueprint (routes.py), plus the binary, newline-aligned read algorithms
`/api/logs/tail` and `/api/logs/chunk` both use. Ports
src/http_gateway.py's `is_log_file_authorized()` and the inline
traversal-validation body of its `/api/logs/tail` route (read both in
full, character by character, before writing this module).

--------------------------------------------------------------------------
Path-traversal defense (`resolve_log_path`) -- ported verbatim
--------------------------------------------------------------------------
`is_log_file_authorized()` in the original does NOT itself contain any
traversal checking -- confirmed by reading it end to end; it is pure
string path-parsing (`filepath.split('/')`) plus a role/clientname check.
The actual traversal defense lives inline in `/api/logs/tail`'s route
body, as exactly two checks, in exactly this order, before anything
touches the filesystem:

    1. reject any filename containing the literal substring '..'
    2. os.path.join(LOG_DIR, filename), then os.path.realpath() BOTH that
       joined path and LOG_DIR itself, and require the former to start
       with the latter (containment check)

`resolve_log_path()` below reproduces both checks byte-for-byte (same
substring test, same join-then-realpath-then-startswith containment
check, same two rejection messages) -- merely consolidated into one
function so `/api/logs/tail` and the new `/api/logs/chunk` (this task)
share a single implementation instead of each re-deriving it (the
original only ever had one caller). The file that is actually opened
afterward is the `os.path.join()` result (`log_path`), NOT its realpath
-- also matching the original exactly; the realpath is computed only to
perform the containment check itself.

--------------------------------------------------------------------------
Authorization tiers -- redesigned per docs/OVERHAUL_PLAN.md 4.4, NOT a
verbatim port
--------------------------------------------------------------------------
The original `is_log_file_authorized()` checked `user['type']` /
`user['clientname']` against a JSON `users.json`-backed dict (admin sees
everything; node/client users matched their own clientname against a
folder segment or, for flat files, a filename prefix). This task's brief
replaces that with Task 18's `has_permission()`/`is_master_admin()`,
organized around CLAUDE.md's "Log Segregation" directory semantics:

    ops/**            -> principal.is_master_admin()   (admin-only, always)
    file/{client}/**  -> principal.has_permission('logs:read', client)
    flat files / else -> principal.is_master_admin() only

The only path-PARSING logic kept from the original is which of these
cases a given path falls into (via `filepath.split('/')` and part-count,
see `_extract_client_folder`). The original's flat-file "does the
filename start with `{clientname}.` or `{clientname}-`" prefix-matching
branch is intentionally NOT reproduced: the brief is explicit that flat
files are master_admin-only now, closing what was a fuzzy, spoofable
prefix match (e.g. a client named "prod" could have read
"production-secrets.log") in favor of the same clean permission model
everything else uses.

--------------------------------------------------------------------------
Binary reads, newline-aligned byte offsets
--------------------------------------------------------------------------
`read_tail_forward()` / `read_chunk_backward()` open files in binary mode
('rb') and guarantee every byte offset they hand back (`new_position` /
`start`) satisfies one invariant: the offset is either `0` or immediately
follows a `\n` byte in the file. See each function's own docstring for
its algorithm, and gateway/logs/routes.py's module docstring for how the
two compose into gap-free, non-duplicating backward/forward paging.
"""

import os
from datetime import datetime
from typing import List, Optional, Tuple

import pytz

LOG_DIR = "/data/logs"

LOGS_READ_PERMISSION = "logs:read"

MAX_TAIL_BYTES = 1024 * 1024   # 1MB -- forward tail-read cap
MAX_CHUNK_BYTES = 1024 * 1024  # 1MB -- backward chunk-read target size

# Hard ceiling on how far read_chunk_backward() will widen its search for a
# usable newline boundary before giving up. Matches the *spirit* of
# http_gateway.py's old initial-load safety cap
# (`MAX_INITIAL_FILE_SIZE = 50 * 1024 * 1024`) -- that code refused to read
# more than 50MB into memory in one request; this is the equivalent ceiling
# for the widen-fallback path (see read_chunk_backward()'s docstring). This
# is deliberately a SEPARATE constant from MAX_CHUNK_BYTES (the normal,
# per-request target size): MAX_CHUNK_BYTES bounds every ordinary request,
# MAX_WIDEN_BYTES bounds the rare pathological one.
MAX_WIDEN_BYTES = 50 * 1024 * 1024  # 50MB


class PathValidationError(Exception):
    """Raised by resolve_log_path() for a rejected filename; `.message` is the exact client-facing reason."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class ChunkUnalignable(Exception):
    """
    Raised by read_chunk_backward() when no newline boundary can be found
    within MAX_WIDEN_BYTES bytes before the requested `end` offset -- see
    that function's docstring. This means the file contains one "line"
    (a run of bytes with no `\\n`) longer than MAX_WIDEN_BYTES immediately
    before `end`: an extreme, pathological input real alert log lines
    should never produce (this system's FilePathway lines are short and
    always `\\n`-terminated), but plausible for a corrupted file or one
    written by a misconfigured pathway (e.g. `sanitize_messages: false`
    with an oversized, newline-free message). gateway/logs/routes.py
    catches this and returns 413, rather than letting the read grow
    without bound.
    """


def substitute_date(filename: str) -> str:
    """Replace the `{date}` placeholder with the current UTC date (YYYY-MM-DD) -- ported verbatim from http_gateway.py's `/api/logs/tail`."""
    current_date = datetime.now(pytz.UTC).strftime("%Y-%m-%d")
    return filename.replace("{date}", current_date)


def resolve_log_path(filename: str) -> str:
    """
    Validate `filename` (relative to LOG_DIR, already {date}-substituted)
    against directory traversal -- see module docstring's "Path-traversal
    defense" section for the exact ported checks. Returns the
    `os.path.join()`-derived path to actually open (NOT its realpath),
    exactly as http_gateway.py's route did. Raises `PathValidationError`
    (with the original's exact message) on rejection.
    """
    if ".." in filename:
        raise PathValidationError("Invalid filename - directory traversal not allowed")

    log_path = os.path.join(LOG_DIR, filename)

    real_log_path = os.path.realpath(log_path)
    real_log_dir = os.path.realpath(LOG_DIR)
    if not real_log_path.startswith(real_log_dir):
        raise PathValidationError("Invalid filename - must be within logs directory")

    return log_path


def _extract_client_folder(filepath: str) -> Optional[str]:
    """
    "file/{client}/{filename}" (folder structure, 3+ parts) -> {client},
    normalized (`.strip().lower()`) to match how `has_permission()`'s
    scopes are stored/compared (verbatim, case-sensitive -- see
    `PrincipalData.has_permission`'s own docstring) -- legitimate client
    folder names are always already-lowercase (CLAUDE.md "String
    Normalization"), so this is defensive, not a behavior-bearing
    normalization for real data.

    "file/{filename}" (flat, 2 parts) -> None: no client segment to scope
    to at all -- the caller treats this the same as any other
    non-folder-structured path (master_admin-only).
    """
    path_parts = filepath.split("/")
    if len(path_parts) >= 3:
        return path_parts[1].strip().lower()
    return None


def is_log_file_authorized(filepath: str, principal) -> bool:
    """
    filepath: POSIX-relative path from LOG_DIR (e.g.
    "ops/alert-router.2026-07-20.log", "file/revysmc/2026-07-20.log",
    "file/2026-07-20.log"). `principal` is anything duck-typing
    `Principal` (`gateway.principal`): `.is_master_admin()` and
    `.has_permission(permission, client)`. See module docstring's
    "Authorization tiers" section for the full rationale.
    """
    if principal.is_master_admin():
        return True

    if filepath.startswith("ops/"):
        return False

    if filepath.startswith("file/"):
        client = _extract_client_folder(filepath)
        if client is None:
            return False
        return principal.has_permission(LOGS_READ_PERMISSION, client)

    return False


def _decode_lines(data: bytes) -> List[str]:
    """Decode a byte chunk (UTF-8, errors='replace') and split into non-blank lines (matches http_gateway.py's own 'remove empty lines' step)."""
    text = data.decode("utf-8", errors="replace")
    return [line for line in text.split("\n") if line.strip()]


def read_tail_forward(log_path: str, position: int, file_size: int) -> Tuple[List[str], int, bool]:
    """
    Forward incremental read starting at `position`, capped at
    MAX_TAIL_BYTES, trimmed to the last complete '\\n' in the window read
    so the returned `new_position` always points exactly at a byte
    immediately following a '\\n' -- never mid-line, even if the file is
    being actively appended to concurrently.

    A negative `position` is clamped to 0; a `position` at or beyond
    `file_size` (e.g. a stale client value after the file was rotated/
    truncated shorter) is treated as already caught up.

    If the capped window contains NO '\\n' at all (a single line still
    being written that's already longer than MAX_TAIL_BYTES, or a file
    with no newlines whatsoever), nothing is returned and `position` is
    left unchanged: returning partial, unterminated content would risk a
    mid-line split/duplication on the next poll once the line is
    eventually completed, which the newline-alignment invariant exists
    specifically to prevent. This is a deliberate correctness-over-
    completeness tradeoff for a genuinely pathological input -- ordinary
    alert log lines are short and always '\\n'-terminated (this system's
    FilePathway format) -- documented explicitly because it means such a
    line simply never becomes visible via tail until something appends a
    trailing '\\n' after it.

    Returns `(lines, new_position, eof)`, where `eof` is True once
    `new_position` has caught up to the file's current size.
    """
    position = max(0, position)
    if position >= file_size:
        return [], file_size, True

    bytes_to_read = min(file_size - position, MAX_TAIL_BYTES)
    with open(log_path, "rb") as f:
        f.seek(position)
        chunk = f.read(bytes_to_read)

    last_nl = chunk.rfind(b"\n")
    if last_nl == -1:
        return [], position, False

    usable = chunk[: last_nl + 1]
    new_position = position + last_nl + 1
    return _decode_lines(usable), new_position, new_position >= file_size


def read_chunk_backward(log_path: str, end: int) -> Tuple[int, List[str], bool]:
    """
    Backward read of the region ending at byte offset `end`, targeting
    MAX_CHUNK_BYTES, with the START of the returned data aligned to just
    after the first '\\n' found scanning forward from the capped window's
    own start (i.e. the newline closest to `end - MAX_CHUNK_BYTES`) -- this
    keeps each chunk as close to the MAX_CHUNK_BYTES target as possible
    while guaranteeing it begins on a line boundary (never mid-line).

    A negative/zero `end` short-circuits to `(0, [], True)` -- nothing to
    read, already at the beginning of the file.

    Pathological edge case (widen fallback, hard-bounded at
    MAX_WIDEN_BYTES): if the capped MAX_CHUNK_BYTES window contains no
    INTERIOR newline usable as a start boundary -- either no '\\n' at all,
    or the only one found is the window's own trailing byte (meaning the
    "line" ending at `end` actually began further back than
    MAX_CHUNK_BYTES, so aligning to it would yield an empty,
    non-progressing chunk) -- the search widens backward, DOUBLING the
    window size each attempt (MAX_CHUNK_BYTES, 2x, 4x, ...), re-scanning
    the whole (larger) window each time for the newline closest to its new
    start, until either a usable boundary is found, byte 0 is reached
    (always a valid boundary), or the window would need to extend more
    than MAX_WIDEN_BYTES bytes before `end` -- at which point this raises
    `ChunkUnalignable` rather than reading further.

    This bounds the WORST-CASE total bytes read across all attempts to
    roughly `2 * MAX_WIDEN_BYTES` (a doubling series sums to about twice
    its final term) regardless of how large the file actually is --
    deliberately, so a single pathological or corrupted log entry (one
    "line" longer than MAX_WIDEN_BYTES) can never make this endpoint read
    an unbounded amount of a possibly much-larger file into memory in one
    Flask request. MAX_WIDEN_BYTES (50MB) matches the *spirit* of
    http_gateway.py's old initial-load 50MB safety cap; see that
    constant's own comment. Real alert log lines are short and always
    `\\n`-terminated (this system's FilePathway format), so this ceiling
    should never actually trigger outside a corrupted/anomalous file.

    Returns `(start, lines, eof)`, where `eof` is True exactly when
    `start == 0`. Raises `ChunkUnalignable` if the MAX_WIDEN_BYTES ceiling
    is reached with no usable boundary found -- see that exception's own
    docstring; gateway/logs/routes.py turns this into a 413 response.
    """
    end = max(0, end)
    if end == 0:
        return 0, [], True

    widen_floor = max(0, end - MAX_WIDEN_BYTES)
    window_size = MAX_CHUNK_BYTES

    with open(log_path, "rb") as f:
        while True:
            window_start = max(widen_floor, end - window_size)
            f.seek(window_start)
            window = f.read(end - window_start)

            if window_start == 0:
                # Byte 0 is always a valid boundary -- no newline needed.
                return 0, _decode_lines(window), True

            first_nl = window.find(b"\n")
            if first_nl != -1:
                start = window_start + first_nl + 1
                if start < end:
                    return start, _decode_lines(window[first_nl + 1 :]), False
                # start == end -- the only '\n' found is this window's own
                # trailing byte, so aligning to it would yield zero new
                # bytes (no progress). Fall through and widen further.

            if window_start <= widen_floor:
                # Hit the hard ceiling (widen_floor) without finding a
                # usable boundary -- give up rather than read further.
                raise ChunkUnalignable(
                    f"no newline boundary found within {MAX_WIDEN_BYTES} bytes before offset {end}"
                )

            window_size *= 2
