"""
Alert Manager - gateway.logs.routes

`logs` blueprint: `GET /logs` (renders `templates/logs.html` -- Task 26's
LogStore/Renderer/Search/Poller frontend, see that template and
`static/js/log-viewer.js` for the client half), `GET /api/logs/list`
(ported unchanged from http_gateway.py), `GET /api/logs/tail` (existing,
reworked to binary/newline-aligned reads -- see below) and
`GET /api/logs/chunk` (new). Auth for every route here is
`gateway.principal`'s default policy
(`DEFAULT_AUTH_METHODS` == `SESSION_OR_API` == session, basic, bearer)
specifically so existing `tools/tests/*.sh` curl workflows keep working
alongside the new session-cookie browser UI (Tasks 22+) -- see
gateway/principal.py's own module docstring, "Per-blueprint auth policy".

--------------------------------------------------------------------------
Binary reads, newline-aligned byte offsets
--------------------------------------------------------------------------
http_gateway.py's `/api/logs/tail` opened files in text mode ('r',
errors='replace') and tracked position via `f.tell()` -- fragile, and (the
more serious problem) it split whatever text landed inside a fixed byte
window on '\n', so a poll landing mid-line could return a partial line if
the file was being actively appended to concurrently, and the NEXT poll
would then either duplicate or corrupt that same partial line.

This module (via gateway/logs/files.py's `read_tail_forward`/
`read_chunk_backward`) opens files in binary mode ('rb') throughout and
guarantees every byte offset it ever hands back to a client -- `position`
from `/api/logs/tail`, `start` from `/api/logs/chunk` -- is either `0` or
immediately follows a `\n` byte in the file. Decoding to text happens only
after a byte-aligned window has already been sliced out.

--------------------------------------------------------------------------
`?stat=1` on GET /api/logs/tail
--------------------------------------------------------------------------
Returns `{"file_size": N}` only (no read at all) -- lets a client learn
the current file size cheaply so it can plan an initial `/api/logs/chunk`
backward-paging sequence (Task 26's job).

--------------------------------------------------------------------------
GET /api/logs/chunk?file=F&end=E (new)
--------------------------------------------------------------------------
Backward read ending at byte offset `E` (defaults to the file's current
size if omitted), capped at ~1MB per response, START-aligned to a line
boundary. See gateway/logs/files.py's `read_chunk_backward` docstring for
the full algorithm, including the widen-backward fallback for the
pathological no-interior-newline case. `eof: true` when the computed
`start` is `0`.

**413 response (Task 26: treat as a hard, non-retryable refusal, not a
transient error):** the widen fallback is itself hard-bounded at
`files.MAX_WIDEN_BYTES` (50MB, matching the *spirit* of the old
http_gateway.py 50MB initial-load cap) -- if a single "line" (a run of
bytes with no `\n`) longer than that sits immediately before `E`, this
route returns `413` with
`{"status": "error", "message": "Log entry too large to read in one
chunk", "error_detail": "..."}` instead of reading further. This should
never happen for a normal alert log (lines are short and always
`\n`-terminated), but is a real possibility for a corrupted file, or one
written with `sanitize_messages: false` and an oversized, newline-free
message (see CLAUDE.md's File Pathway "Message Sanitization" section).
There is no way to page past this via `/api/logs/chunk` for the affected
region -- a client seeing 413 should stop paging further back on this
file and surface that to the user, not retry.

--------------------------------------------------------------------------
Composing chunk + tail: contiguous, non-overlapping paging
--------------------------------------------------------------------------
Both endpoints share the exact same "valid boundary" definition (offset 0,
or immediately after a '\n'), so a client can page backward via
`/api/logs/chunk` (each response's `start` feeds the next request's `end`)
until `eof`, then switch to forward-only polling via `/api/logs/tail`
using the last chunk's `start` (or `/api/logs/tail?stat=1`'s `file_size`)
as its first `position` -- the two can never overlap or leave a gap,
because both always land on the same kind of boundary.

--------------------------------------------------------------------------
Deleted: the old 10k-line (`lines * 100`) truncation
--------------------------------------------------------------------------
http_gateway.py's initial-load path special-cased "read the whole file up
to 50MB, then truncate in-memory to the last `lines * 100` (10,000) lines
if there were more". Per the task brief, this rework has no equivalent
server-side line-count cap at all: `/api/logs/tail` is now ALWAYS a
bytes-bounded (1MB), forward-only incremental read from an explicit
`position` -- never "read the whole file". Building an initial view is
now the CLIENT's job (Task 26), via repeated `/api/logs/chunk` calls. The
50MB total-file safety guard becomes purely client-side messaging (per
the brief: "Older entries beyond 50MB not loaded") -- not this task's
concern.

--------------------------------------------------------------------------
Auth/path/403 semantics -- unchanged outcomes, new mechanism
--------------------------------------------------------------------------
`_resolve_and_authorize()` reproduces http_gateway.py's exact validation
ORDER for a `file` query param (substitute `{date}` -> traversal check ->
authorization check -> existence check), so a given input produces the
same 400/403/404 outcome as before; only the authorization DECISION
itself now goes through `gateway.logs.files.is_log_file_authorized()`
(has_permission()/is_master_admin()) instead of the old ad hoc role
checks (see that module's own docstring).
"""

import logging
import os
from datetime import datetime

import pytz
from flask import Blueprint, current_app, g, jsonify, render_template, request

from gateway.principal import require_auth

from . import files

logger = logging.getLogger("gateway.logs")

logs_bp = Blueprint("logs", __name__)

_DEFAULT_TAIL_FILE = "ops/alert-http-gateway.{date}.log"


def _iso_now() -> str:
    return datetime.now(pytz.UTC).isoformat() + "Z"


# ---------------------------------------------------------------------------
# GET /logs -- renders templates/logs.html (Task 26, atop Task 24's layout.html)
# ---------------------------------------------------------------------------

@logs_bp.route("/logs", methods=["GET"])
@require_auth
def view_logs():
    """
    Renders `templates/logs.html` (Task 26), extending the shared
    `layout.html` (Task 24). The only server-supplied piece of dynamic
    state is `server_poll_interval_ms`, injected as a JS constant exactly
    as http_gateway.py's old inline template did (see that route's own
    `self.poll_interval_ms`) -- everything else (which file is selected,
    filter/search state, the chunk/tail polling loop) is client-side,
    driven by `/api/logs/list` + `/api/logs/tail` + `/api/logs/chunk`
    above.
    """
    config = current_app.extensions["gateway_config"]
    return render_template("logs.html", server_poll_interval_ms=config.LOG_POLL_INTERVAL_MS)


# ---------------------------------------------------------------------------
# GET /api/logs/list -- ported unchanged from http_gateway.py
# ---------------------------------------------------------------------------

@logs_bp.route("/api/logs/list", methods=["GET"])
@require_auth
def list_log_files():
    """List available log files recursively, filtered by the caller's authorization."""
    try:
        principal = g.principal
        log_dir = files.LOG_DIR

        include_ops = request.args.get("include_ops", "false").lower() == "true"

        if not os.path.exists(log_dir):
            return jsonify({"status": "error", "message": "Log directory not found", "files": []}), 404

        all_files = []
        for root, _dirs, filenames in os.walk(log_dir):
            for filename in filenames:
                if filename.endswith(".log"):
                    filepath = os.path.join(root, filename)
                    relpath = os.path.relpath(filepath, log_dir)
                    all_files.append(relpath)

        authorized_files = [f for f in all_files if files.is_log_file_authorized(f, principal)]

        # Filter out ops/ logs unless explicitly requested by a master_admin.
        if not include_ops or not principal.is_master_admin():
            authorized_files = [f for f in authorized_files if not f.startswith("ops/")]

        authorized_files.sort(reverse=True)

        return jsonify(
            {
                "status": "success",
                "files": authorized_files,
                "timestamp": _iso_now(),
                "user": {
                    "username": principal.username,
                    "is_master_admin": principal.is_master_admin(),
                    "default_client": principal.default_client,
                },
            }
        )

    except Exception as exc:
        logger.error("Error listing log files: %s", exc, exc_info=True)
        return jsonify({"status": "error", "message": "Failed to list log files", "error_detail": str(exc)}), 500


# ---------------------------------------------------------------------------
# Shared plumbing for /api/logs/tail and /api/logs/chunk
# ---------------------------------------------------------------------------

def _resolve_and_authorize(raw_filename: str):
    """
    Shared {date}-substitution + traversal-validation + authorization
    sequence for `/api/logs/tail` and `/api/logs/chunk`, in the same
    order http_gateway.py's `/api/logs/tail` route used (traversal check,
    THEN authorization, THEN existence) so a given input's 400/403/404
    outcome is unchanged.

    Returns `(log_path, None)` on success, or `(None, (body, status))` on
    a validation/authorization/existence failure, ready to `return`
    directly from the calling view.
    """
    filename = files.substitute_date(raw_filename)

    try:
        log_path = files.resolve_log_path(filename)
    except files.PathValidationError as exc:
        return None, (jsonify({"status": "error", "message": exc.message}), 400)

    if not files.is_log_file_authorized(filename, g.principal):
        logger.warning(
            "User %s attempted to access unauthorized log file: %s", g.principal.username, filename
        )
        return None, (
            jsonify(
                {
                    "status": "error",
                    "message": "Access denied - you do not have permission to view this log file",
                }
            ),
            403,
        )

    if not os.path.exists(log_path):
        return None, (
            jsonify({"status": "error", "message": "Log file not found", "lines": [], "position": 0}),
            404,
        )

    return log_path, None


@logs_bp.route("/api/logs/tail", methods=["GET"])
@require_auth
def tail_log():
    """
    Forward, byte-aligned incremental read. `?stat=1` short-circuits to
    `{"file_size": N}` before any read is attempted. See module docstring.
    """
    try:
        raw_filename = request.args.get("file", _DEFAULT_TAIL_FILE)

        log_path, error = _resolve_and_authorize(raw_filename)
        if error is not None:
            return error

        file_size = os.path.getsize(log_path)

        if request.args.get("stat") == "1":
            return jsonify({"file_size": file_size})

        position = int(request.args.get("position", 0))

        lines, new_position, eof = files.read_tail_forward(log_path, position, file_size)

        return jsonify(
            {
                "status": "success",
                "lines": lines,
                "position": new_position,
                "file_size": file_size,
                "eof": eof,
            }
        )

    except Exception as exc:
        logger.error("Error tailing log file: %s", exc, exc_info=True)
        return jsonify({"status": "error", "message": "Failed to read log file", "error_detail": str(exc)}), 500


@logs_bp.route("/api/logs/chunk", methods=["GET"])
@require_auth
def chunk_log():
    """Backward, byte-aligned page read ending at `end` (defaults to current file size). See module docstring."""
    try:
        raw_filename = request.args.get("file", "")
        if not raw_filename:
            return jsonify({"status": "error", "message": "file parameter is required"}), 400

        log_path, error = _resolve_and_authorize(raw_filename)
        if error is not None:
            return error

        file_size = os.path.getsize(log_path)

        end = int(request.args.get("end", file_size))
        # Clamp a stale/overshooting `end` (e.g. the file was rotated/
        # truncated shorter since the client learned file_size) to the
        # current size rather than reading past it.
        end = min(end, file_size)

        try:
            start, lines, eof = files.read_chunk_backward(log_path, end)
        except files.ChunkUnalignable as exc:
            # A single "line" longer than files.MAX_WIDEN_BYTES sits
            # immediately before `end` -- see files.ChunkUnalignable's own
            # docstring. Reported as 413 (not 500): this is a well-defined,
            # bounded refusal to read further, not an unexpected server
            # error, and Task 26 (the frontend consumer) should treat it as
            # "this file has an anomalously large entry that cannot be
            # paged here" rather than a transient failure worth retrying.
            logger.warning("Chunk read for %s exceeded the widen ceiling: %s", raw_filename, exc)
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "Log entry too large to read in one chunk",
                        "error_detail": str(exc),
                    }
                ),
                413,
            )

        return jsonify(
            {
                "status": "success",
                "lines": lines,
                "start": start,
                "end": end,
                "file_size": file_size,
                "eof": eof,
            }
        )

    except Exception as exc:
        logger.error("Error reading log chunk: %s", exc, exc_info=True)
        return jsonify({"status": "error", "message": "Failed to read log file", "error_detail": str(exc)}), 500
