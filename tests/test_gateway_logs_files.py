"""
Tests for src/gateway/logs/files.py (Task 21): `is_log_file_authorized()`'s
full authorization matrix (ops/master_admin, file/{client}/scoped-user,
file/{client}/wrong-client-403, flat-file/master_admin-only), the ported
path-traversal defense (`resolve_log_path`), and the binary,
newline-aligned read algorithms (`read_tail_forward`/`read_chunk_backward`)
against REAL temporary files -- no live Postgres/MQTT is needed for any of
this (it is pure filesystem + in-memory logic), so this suite writes real
bytes to real temp files rather than mocking `open()`/`os.path.*`.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from userstore import PrincipalData  # noqa: E402
from gateway.principal import Principal  # noqa: E402
from gateway.logs import files  # noqa: E402


def _principal(
    username="alice",
    default_client=None,
    roles=frozenset(),
    scopes=None,
) -> Principal:
    data = PrincipalData(
        user_id=1,
        username=username,
        status="active",
        default_client=default_client,
        must_change_password=False,
        roles=roles,
        scopes=scopes or {},
    )
    return Principal(data, auth_method="basic")


MASTER_ADMIN = _principal(username="root", roles=frozenset({"master_admin"}), scopes={"*": frozenset({"*"})})


# ---------------------------------------------------------------------------
# is_log_file_authorized() -- full authorization matrix
# ---------------------------------------------------------------------------

class TestOpsTier:
    def test_master_admin_can_read_ops_logs(self):
        assert files.is_log_file_authorized("ops/alert-router.2026-07-20.log", MASTER_ADMIN) is True

    def test_non_admin_scoped_to_logs_read_still_denied_ops(self):
        # Even holding logs:read for a client (any client, or wildcard
        # client) must NOT grant access to ops/ -- ops/ is master_admin only.
        principal = _principal(scopes={"revysmc": frozenset({"logs:read"})})
        assert files.is_log_file_authorized("ops/alert-router.2026-07-20.log", principal) is False

    def test_non_admin_with_wildcard_client_scope_still_denied_ops(self):
        principal = _principal(scopes={"*": frozenset({"logs:read"})})
        assert files.is_log_file_authorized("ops/alert-router.2026-07-20.log", principal) is False


class TestFileClientTier:
    def test_scoped_user_can_read_own_client_folder(self):
        principal = _principal(scopes={"revysmc": frozenset({"logs:read"})})
        assert files.is_log_file_authorized("file/revysmc/2026-07-20.log", principal) is True

    def test_scoped_user_denied_a_different_clients_folder(self):
        principal = _principal(scopes={"revysmc": frozenset({"logs:read"})})
        assert files.is_log_file_authorized("file/some-other-client/2026-07-20.log", principal) is False

    def test_user_without_logs_read_permission_denied_own_client_folder(self):
        # Holds a scope for the client, but not the logs:read permission.
        principal = _principal(scopes={"revysmc": frozenset({"alerts:write"})})
        assert files.is_log_file_authorized("file/revysmc/2026-07-20.log", principal) is False

    def test_master_admin_can_read_any_client_folder(self):
        assert files.is_log_file_authorized("file/revysmc/2026-07-20.log", MASTER_ADMIN) is True

    def test_client_folder_name_is_normalized_before_permission_check(self):
        # Legitimate client folder names are always already-lowercase, but
        # the extraction defensively normalizes anyway (mirrors
        # PrincipalData.has_permission's own case-sensitive-verbatim
        # comparison contract, and require_permission's established
        # normalize-before-compare convention).
        principal = _principal(scopes={"revysmc": frozenset({"logs:read"})})
        assert files.is_log_file_authorized("file/RevySMC/2026-07-20.log", principal) is True

    def test_deeper_nesting_still_extracts_the_first_path_segment_as_client(self):
        principal = _principal(scopes={"revysmc": frozenset({"logs:read"})})
        assert files.is_log_file_authorized("file/revysmc/subdir/2026-07-20.log", principal) is True


class TestFlatFileTier:
    def test_flat_file_denied_for_non_admin_even_with_matching_scope(self):
        # Old behavior (ported-from code) allowed a "client" user to read a
        # flat file whose name was PREFIXED by their clientname. The new
        # tier model does NOT reproduce that -- flat files are
        # master_admin-only now, full stop.
        principal = _principal(scopes={"revysmc": frozenset({"logs:read"})})
        assert files.is_log_file_authorized("file/revysmc.2026-07-20.log", principal) is False
        assert files.is_log_file_authorized("file/revysmc-2026-07-20.log", principal) is False

    def test_flat_default_file_denied_for_non_admin(self):
        principal = _principal(scopes={"revysmc": frozenset({"logs:read"})})
        assert files.is_log_file_authorized("file/2026-07-20.log", principal) is False

    def test_flat_file_allowed_for_master_admin(self):
        assert files.is_log_file_authorized("file/2026-07-20.log", MASTER_ADMIN) is True


class TestOtherPathsTier:
    def test_unknown_top_level_path_denied_for_non_admin(self):
        principal = _principal(scopes={"revysmc": frozenset({"logs:read", "*"})})
        assert files.is_log_file_authorized("snmp-discovery/snmpFULL.2026-07-20.jsonl", principal) is False

    def test_unknown_top_level_path_allowed_for_master_admin(self):
        assert files.is_log_file_authorized("snmp-discovery/snmpFULL.2026-07-20.jsonl", MASTER_ADMIN) is True


# ---------------------------------------------------------------------------
# resolve_log_path() -- ported traversal defense
# ---------------------------------------------------------------------------

class TestResolveLogPath:
    def test_accepts_a_well_formed_relative_path(self, tmp_path, monkeypatch):
        monkeypatch.setattr(files, "LOG_DIR", str(tmp_path))
        result = files.resolve_log_path("file/revysmc/2026-07-20.log")
        assert result == os.path.join(str(tmp_path), "file/revysmc/2026-07-20.log")

    def test_rejects_dotdot_substring_anywhere_in_filename(self, tmp_path, monkeypatch):
        monkeypatch.setattr(files, "LOG_DIR", str(tmp_path))
        with pytest.raises(files.PathValidationError) as exc_info:
            files.resolve_log_path("../etc/passwd")
        assert "directory traversal" in exc_info.value.message

    def test_rejects_dotdot_embedded_mid_path(self, tmp_path, monkeypatch):
        monkeypatch.setattr(files, "LOG_DIR", str(tmp_path))
        with pytest.raises(files.PathValidationError):
            files.resolve_log_path("file/../../etc/passwd")

    def test_rejects_absolute_path_that_escapes_via_realpath_containment(self, tmp_path, monkeypatch):
        # An absolute path contains no '..' substring, so the first check
        # doesn't catch it -- the realpath-containment check must.
        # os.path.join(LOG_DIR, "/etc/passwd") == "/etc/passwd" (os.path.join
        # discards everything before an absolute second argument), which
        # realpaths outside LOG_DIR.
        monkeypatch.setattr(files, "LOG_DIR", str(tmp_path))
        with pytest.raises(files.PathValidationError) as exc_info:
            files.resolve_log_path("/etc/passwd")
        assert "within logs directory" in exc_info.value.message

    def test_rejects_symlink_that_resolves_outside_log_dir(self, tmp_path, monkeypatch):
        outside = tmp_path.parent / "outside_secret.log"
        outside.write_text("secret\n")
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        symlink_path = log_dir / "sneaky.log"
        symlink_path.symlink_to(outside)
        monkeypatch.setattr(files, "LOG_DIR", str(log_dir))

        with pytest.raises(files.PathValidationError) as exc_info:
            files.resolve_log_path("sneaky.log")
        assert "within logs directory" in exc_info.value.message

    def test_sibling_directory_with_shared_prefix_is_rejected(self, tmp_path, monkeypatch):
        # Ported verbatim from the original startswith() containment
        # check -- documenting (not fixing) its known weakness: a sibling
        # dir sharing LOG_DIR's name as a prefix isn't distinguishable from
        # a real subdirectory by plain string startswith(). This test
        # exists to prove the check still behaves EXACTLY as the original,
        # not to assert it is airtight (it isn't, and porting it verbatim
        # was an explicit brief requirement, not a redesign opportunity).
        log_dir = tmp_path / "data" / "logs"
        log_dir.mkdir(parents=True)
        sibling = tmp_path / "data" / "logs-secret"
        sibling.mkdir(parents=True)
        (sibling / "file.log").write_text("leaked\n")
        monkeypatch.setattr(files, "LOG_DIR", str(log_dir))

        # Reaching the sibling requires a '..' segment, which the first
        # check already rejects -- confirms check #1 guards check #2's
        # known weakness in the one way an attacker could reach a sibling
        # via a relative path at all.
        with pytest.raises(files.PathValidationError):
            files.resolve_log_path("../logs-secret/file.log")


# ---------------------------------------------------------------------------
# substitute_date()
# ---------------------------------------------------------------------------

def test_substitute_date_replaces_placeholder_with_current_utc_date():
    result = files.substitute_date("ops/alert-router.{date}.log")
    assert "{date}" not in result
    assert result.startswith("ops/alert-router.")
    assert result.endswith(".log")


def test_substitute_date_leaves_filename_without_placeholder_unchanged():
    assert files.substitute_date("file/revysmc/fixed-name.log") == "file/revysmc/fixed-name.log"


# ---------------------------------------------------------------------------
# read_tail_forward() -- binary, newline-aligned forward reads (real files)
# ---------------------------------------------------------------------------

class TestReadTailForward:
    def _write(self, tmp_path, name, content: bytes) -> str:
        path = tmp_path / name
        path.write_bytes(content)
        return str(path)

    def test_reads_from_position_zero_and_stops_at_last_newline(self, tmp_path):
        content = b"line1\nline2\nline3\n"
        path = self._write(tmp_path, "a.log", content)
        lines, new_position, eof = files.read_tail_forward(path, 0, len(content))
        assert lines == ["line1", "line2", "line3"]
        assert new_position == len(content)
        assert eof is True

    def test_trailing_partial_line_without_newline_is_withheld(self, tmp_path):
        # The last line has NOT been newline-terminated yet (still being
        # written) -- must not be returned, and position must not advance
        # past the last complete line, so a subsequent poll can pick up the
        # completed line without ever having seen a partial version of it.
        content = b"line1\nline2\npartial-still-being-writ"
        path = self._write(tmp_path, "a.log", content)
        lines, new_position, eof = files.read_tail_forward(path, 0, len(content))
        assert lines == ["line1", "line2"]
        assert new_position == len(b"line1\nline2\n")
        assert eof is False  # new_position < file_size -- more (incomplete) data pending

    def test_file_with_zero_newlines_returns_nothing_and_holds_position(self, tmp_path):
        # Explicit edge case called out in the task brief: a file (or
        # region) containing no '\n' at all must never be returned, since
        # doing so risks a mid-line split/duplication once it's eventually
        # terminated. Position must stay exactly where it was.
        content = b"one giant line with absolutely no newline in it at all"
        path = self._write(tmp_path, "a.log", content)
        lines, new_position, eof = files.read_tail_forward(path, 0, len(content))
        assert lines == []
        assert new_position == 0
        assert eof is False

        # Subsequent poll from the same position, file unchanged: still nothing.
        lines2, new_position2, eof2 = files.read_tail_forward(path, new_position, len(content))
        assert lines2 == []
        assert new_position2 == 0
        assert eof2 is False

    def test_empty_file_is_immediately_eof(self, tmp_path):
        path = self._write(tmp_path, "a.log", b"")
        lines, new_position, eof = files.read_tail_forward(path, 0, 0)
        assert lines == []
        assert new_position == 0
        assert eof is True

    def test_position_already_at_file_size_returns_nothing_new(self, tmp_path):
        content = b"line1\nline2\n"
        path = self._write(tmp_path, "a.log", content)
        lines, new_position, eof = files.read_tail_forward(path, len(content), len(content))
        assert lines == []
        assert new_position == len(content)
        assert eof is True

    def test_position_beyond_file_size_is_clamped_to_caught_up(self, tmp_path):
        # e.g. the file was rotated/truncated shorter since the client's
        # last poll -- must not attempt a negative-length read.
        content = b"line1\n"
        path = self._write(tmp_path, "a.log", content)
        lines, new_position, eof = files.read_tail_forward(path, 999, len(content))
        assert lines == []
        assert new_position == len(content)
        assert eof is True

    def test_negative_position_is_clamped_to_zero(self, tmp_path):
        content = b"line1\nline2\n"
        path = self._write(tmp_path, "a.log", content)
        lines, new_position, eof = files.read_tail_forward(path, -5, len(content))
        assert lines == ["line1", "line2"]
        assert new_position == len(content)

    def test_incremental_polling_never_splits_or_duplicates_a_line(self, tmp_path):
        # Simulates a writer appending lines one at a time while a reader
        # polls repeatedly with whatever `position` the previous poll
        # returned -- the concatenation of every poll's lines must equal
        # the full set of complete lines, with none split or duplicated.
        path = tmp_path / "a.log"
        path.write_bytes(b"")
        position = 0
        seen = []

        all_lines = [f"entry-{i}" for i in range(50)]
        for i, line in enumerate(all_lines):
            with open(path, "ab") as f:
                f.write(f"{line}\n".encode())
                if i % 3 == 0:
                    # Occasionally leave a partial line dangling before the
                    # next poll, to prove it's correctly withheld.
                    f.write(b"partial-fragment-no-newline")

            file_size = os.path.getsize(path)
            lines, position, eof = files.read_tail_forward(str(path), position, file_size)
            seen.extend(lines)
            # Strip the dangling partial fragment before the next append.
            if i % 3 == 0:
                with open(path, "r+b") as f:
                    f.seek(position)
                    f.truncate()

        assert seen == all_lines

    def test_read_is_capped_at_1mb_per_call(self, tmp_path):
        # A single ~2MB line-filled file: one call must not return past the
        # 1MB cap, and must land on a complete line boundary within it.
        line = b"x" * 100 + b"\n"  # 101 bytes/line
        content = line * 20000  # ~2.02MB
        path = tmp_path / "big.log"
        path.write_bytes(content)
        file_size = len(content)

        lines, new_position, eof = files.read_tail_forward(str(path), 0, file_size)
        assert new_position <= files.MAX_TAIL_BYTES + len(line)  # aligned to a line boundary near the cap
        assert eof is False
        assert all(l == "x" * 100 for l in lines)

        # Draining the rest via repeated polls reconstructs every line, no gaps/dupes.
        total_lines = list(lines)
        while not eof:
            more_lines, new_position, eof = files.read_tail_forward(str(path), new_position, file_size)
            total_lines.extend(more_lines)
        assert len(total_lines) == 20000
        assert new_position == file_size


# ---------------------------------------------------------------------------
# read_chunk_backward() -- binary, newline-aligned backward reads (real files)
# ---------------------------------------------------------------------------

def _install_read_byte_counter(monkeypatch):
    """
    Monkeypatch gateway.logs.files.open -- shadowing the builtin within
    that module's namespace (LEGB module-global lookup, a standard
    monkeypatching technique) -- with a wrapper that counts the total
    bytes actually returned by every f.read(...) call made through it.
    Returns a dict; counters["total"] is updated in place as reads happen,
    so a test can assert on REAL I/O volume rather than inferring it
    indirectly (e.g. from elapsed time).
    """
    counters = {"total": 0}
    real_open = open

    class _CountingFile:
        def __init__(self, real_file):
            self._real = real_file

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            self._real.close()
            return False

        def seek(self, *args, **kwargs):
            return self._real.seek(*args, **kwargs)

        def read(self, *args, **kwargs):
            data = self._real.read(*args, **kwargs)
            counters["total"] += len(data)
            return data

    def _counting_open(path, mode="r", *args, **kwargs):
        return _CountingFile(real_open(path, mode, *args, **kwargs))

    monkeypatch.setattr(files, "open", _counting_open, raising=False)
    return counters


class TestReadChunkBackward:
    def _write(self, tmp_path, name, content: bytes) -> str:
        path = tmp_path / name
        path.write_bytes(content)
        return str(path)

    def test_end_zero_is_immediately_eof(self, tmp_path):
        content = b"line1\nline2\n"
        path = self._write(tmp_path, "a.log", content)
        start, lines, eof = files.read_chunk_backward(path, 0)
        assert start == 0
        assert lines == []
        assert eof is True

    def test_small_file_reads_from_byte_zero_in_one_shot(self, tmp_path):
        content = b"line1\nline2\nline3\n"
        path = self._write(tmp_path, "a.log", content)
        start, lines, eof = files.read_chunk_backward(path, len(content))
        assert start == 0
        assert lines == ["line1", "line2", "line3"]
        assert eof is True

    def test_small_file_with_unterminated_last_line_is_still_included(self, tmp_path):
        # `end` is trusted as a valid upper boundary regardless of whether
        # the byte at end-1 is itself a newline -- only the START needs
        # alignment.
        content = b"line1\nline2\nline3-no-trailing-newline"
        path = self._write(tmp_path, "a.log", content)
        start, lines, eof = files.read_chunk_backward(path, len(content))
        assert start == 0
        assert lines == ["line1", "line2", "line3-no-trailing-newline"]
        assert eof is True

    def test_large_file_first_chunk_is_aligned_and_not_eof(self, tmp_path):
        line = b"x" * 100 + b"\n"  # 101 bytes/line
        content = line * 20000  # ~2.02MB
        path = self._write(tmp_path, "big.log", content)
        file_size = len(content)

        start, lines, eof = files.read_chunk_backward(path, file_size)
        assert eof is False
        assert start > 0
        assert (start == 0) or (content[start - 1] == ord("\n"))  # start is a real line boundary
        assert all(l == "x" * 100 for l in lines)

    def test_paging_backward_to_completion_reaches_byte_zero(self, tmp_path):
        line = b"x" * 100 + b"\n"
        content = line * 20000
        path = self._write(tmp_path, "big.log", content)
        file_size = len(content)

        end = file_size
        all_lines = []
        seen_starts = []
        for _ in range(1000):  # generous upper bound on iterations
            start, lines, eof = files.read_chunk_backward(path, end)
            all_lines = lines + all_lines
            seen_starts.append(start)
            if eof:
                assert start == 0
                break
            end = start
        else:
            pytest.fail("backward paging never reached eof")

        assert len(all_lines) == 20000
        assert all(l == "x" * 100 for l in all_lines)
        # Strictly decreasing -- guaranteed forward progress every call.
        assert seen_starts == sorted(seen_starts, reverse=True)
        assert len(set(seen_starts)) == len(seen_starts)

    def test_no_newline_anywhere_in_file_widens_to_byte_zero(self, tmp_path):
        # Explicit edge case from the task brief: a file with no newlines
        # at all must still produce an aligned (start==0), eof chunk,
        # rather than an empty or non-progressing one.
        content = b"one absolutely enormous line with no newline whatsoever" * 20000  # > 1MB, zero '\n'
        path = self._write(tmp_path, "a.log", content)
        file_size = len(content)
        assert file_size > files.MAX_CHUNK_BYTES

        start, lines, eof = files.read_chunk_backward(path, file_size)
        assert start == 0
        assert eof is True
        # No '\n' at all means _decode_lines() (which filters on '\n'
        # boundaries) yields no complete "lines", but the read itself must
        # not have raised or truncated silently -- content length is fully
        # covered by [0:file_size).

    def test_single_line_spanning_past_the_cap_widens_to_byte_zero(self, tmp_path):
        # A file with exactly ONE newline, at the very end -- the naive
        # "first newline in the capped window" would land on that trailing
        # newline itself (start == end, zero new bytes, no progress). Must
        # widen to byte 0 instead.
        content = (b"y" * (files.MAX_CHUNK_BYTES + 5000)) + b"\n"
        path = self._write(tmp_path, "a.log", content)
        file_size = len(content)

        start, lines, eof = files.read_chunk_backward(path, file_size)
        assert start == 0
        assert eof is True
        assert lines == [content[:-1].decode()]

    def test_max_widen_bytes_matches_documented_50mb_ceiling(self):
        # Pins the actual production default -- a regression test for the
        # documented "spirit of http_gateway.py's old 50MB initial-load
        # cap" ceiling, independent of the bounded-I/O test below (which
        # deliberately patches this constant smaller for speed).
        assert files.MAX_WIDEN_BYTES == 50 * 1024 * 1024

    def test_widen_fallback_stays_bounded_and_raises_past_the_ceiling(self, tmp_path, monkeypatch):
        # The Important review finding this test exists to close: earlier,
        # the widen fallback jumped straight from the capped MAX_CHUNK_BYTES
        # window to a single `f.read(end)` covering the ENTIRE file from
        # byte 0 -- unbounded for a large file. It must now (a) never read
        # past a hard MAX_WIDEN_BYTES ceiling, and (b) raise
        # ChunkUnalignable rather than succeed once that ceiling is
        # exceeded with no usable newline found.
        #
        # MAX_CHUNK_BYTES/MAX_WIDEN_BYTES are patched down (same doubling
        # algorithm, same ceiling mechanism, just parameterized smaller)
        # purely so this test doesn't need a real 50MB+ file to prove the
        # mechanism -- the file used below is REAL (written to tmp_path,
        # not mocked) and, in effect for this test, unambiguously larger
        # than the ceiling in play.
        monkeypatch.setattr(files, "MAX_CHUNK_BYTES", 10_000)
        monkeypatch.setattr(files, "MAX_WIDEN_BYTES", 100_000)

        content = b"z" * 5_000_000  # 5MB, one giant "line" -- zero '\n' anywhere
        path = self._write(tmp_path, "huge.log", content)
        file_size = len(content)
        assert file_size > files.MAX_WIDEN_BYTES * 10  # comfortably bigger than the ceiling

        read_counters = _install_read_byte_counter(monkeypatch)

        with pytest.raises(files.ChunkUnalignable):
            files.read_chunk_backward(path, file_size)

        # Bounded: a doubling series from MAX_CHUNK_BYTES up to
        # MAX_WIDEN_BYTES sums to a small constant multiple of
        # MAX_WIDEN_BYTES -- nowhere near having read the whole 5MB file.
        # This is the crux assertion: an unbounded implementation would
        # have read ~file_size (5,000,000) bytes; a bounded one reads only
        # a small multiple of the (patched) 100,000-byte ceiling.
        assert read_counters["total"] < files.MAX_WIDEN_BYTES * 3
        assert read_counters["total"] < file_size

    def test_widen_fallback_finds_a_real_boundary_across_multiple_doublings(self, tmp_path):
        # Distinct from the byte-0-fallback tests above: proves the
        # doubling loop can also terminate SUCCESSFULLY partway through --
        # by finding a genuine interior newline -- rather than only ever
        # falling all the way back to byte 0, at real (unpatched, default)
        # MAX_CHUNK_BYTES/MAX_WIDEN_BYTES scale.
        #
        # Layout: a 2,000,000-byte prefix (no newlines) terminated by a
        # single '\n', then a 1,500,000-byte "huge line" (no newlines) of
        # its own, then a trailing '\n'. `end` points just after that
        # trailing '\n'. The first widen attempt (MAX_CHUNK_BYTES == 1MB)
        # only reaches partway into the huge line (no '\n' found -> must
        # widen). The second attempt (2MB) reaches back far enough to
        # include the prefix's OWN trailing '\n' -- the one and only
        # newline in that whole window -- so the loop must return THAT
        # boundary, not fall through to byte 0.
        prefix = (b"e" * 2_000_000) + b"\n"
        huge_line = b"h" * 1_500_000
        content = prefix + huge_line + b"\ntail\n"
        path = self._write(tmp_path, "wide.log", content)
        end = len(prefix) + len(huge_line) + 1  # right after the '\n' following huge_line

        start, lines, eof = files.read_chunk_backward(path, end)
        assert start == len(prefix)  # aligned right after prefix's own trailing '\n'
        assert eof is False  # start != 0 -- prefix's own content was never read
        assert lines == [huge_line.decode()]


# ---------------------------------------------------------------------------
# chunk (backward) + tail (forward) compose: contiguous, non-overlapping,
# non-duplicated reconstruction of the full file.
# ---------------------------------------------------------------------------

def test_chunk_backward_then_tail_forward_reconstructs_file_with_no_gap_or_overlap(tmp_path):
    num_lines = 300000  # 10 bytes/line -> ~2.86MB, comfortably > 2x the 1MB chunk cap
    lines_content = [f"row-{i:05d}".encode() + b"\n" for i in range(num_lines)]
    content = b"".join(lines_content)
    path = tmp_path / "big.log"
    path.write_bytes(content)
    file_size = len(content)
    assert file_size > files.MAX_CHUNK_BYTES * 2  # ensure multiple chunk pages are needed

    # Page all the way back from file_size to byte 0 via read_chunk_backward.
    end = file_size
    backward_lines = []
    backward_starts = []
    while True:
        start, lines, eof = files.read_chunk_backward(str(path), end)
        backward_lines = lines + backward_lines
        backward_starts.append((start, end))
        if eof:
            break
        end = start

    assert backward_lines == [f"row-{i:05d}" for i in range(num_lines)]

    # Confirm contiguity/no-overlap directly on the raw byte ranges: each
    # consecutive (start, end) pair must chain start_i == end_{i+1} exactly
    # (sorted oldest-to-newest), and the first start must be 0.
    ranges = sorted(backward_starts, key=lambda pair: pair[0])
    assert ranges[0][0] == 0
    for (start, end_), (next_start, _next_end) in zip(ranges, ranges[1:]):
        assert end_ == next_start  # no gap, no overlap, no duplication

    # Now append new content and confirm tail-forward continues seamlessly
    # from exactly where the backward paging left off (file_size).
    new_lines = [f"new-{i:03d}".encode() + b"\n" for i in range(5)]
    with open(path, "ab") as f:
        f.write(b"".join(new_lines))
    new_file_size = os.path.getsize(path)

    forward_lines, new_position, eof = files.read_tail_forward(str(path), file_size, new_file_size)
    assert forward_lines == [f"new-{i:03d}" for i in range(5)]
    assert new_position == new_file_size
    assert eof is True

    # Full reconstruction: backward-paged content + forward-tailed content
    # equals the entire file, in order, with nothing missing/duplicated.
    assert backward_lines + forward_lines == [f"row-{i:05d}" for i in range(num_lines)] + [
        f"new-{i:03d}" for i in range(5)
    ]
