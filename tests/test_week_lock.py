"""week_lock — OS 배타 잠금(Windows: msvcrt.locking, POSIX: fcntl.flock) 테스트.

- 락 파일(`logs/.locks/{week}.lock`)은 영구 파일이다: 삭제·내용 읽기·쓰기·stale 판정을 하지 않는다.
- 프로세스 간 배제는 실제 하위 프로세스로, 스레드 간 배제는 실제 스레드로 검증한다.
- 시간 초과는 가짜 시계로 검증하므로 실제로 오래 기다리지 않는다. 하위 프로세스를 기다리는 곳은 고정 sleep 이 아니라
  이벤트(출력 한 줄, 프로세스 종료)를 기다린다.
- 이 파일은 실행한 플랫폼에서만 검증된다(Windows 에서 실행해도 Linux 동작은 검증되지 않는다).
"""

import contextlib
import errno
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import ops_state as ops
from ops_state import StatusWriteError

ROOT = Path(__file__).resolve().parent.parent
WEEK = "2026-09-W3"

PRELUDE = f"import sys\nsys.path.insert(0, {str(ROOT)!r})\nfrom pathlib import Path\nimport ops_state as ops\n"

HOLDER = PRELUDE + textwrap.dedent("""
    import time
    with ops.week_lock(Path(sys.argv[1]), sys.argv[2], timeout=30):
        print("LOCKED", flush=True)
        time.sleep(300)
""")

COUNTER_WORKER = PRELUDE + textwrap.dedent("""
    import os, time
    d, week, counter, marker, n = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4]), int(sys.argv[5])
    violations = 0
    for _ in range(n):
        with ops.week_lock(d, week, timeout=120):
            try:
                fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)   # 임계 구역 표지: 이미 있으면 중복 진입
            except FileExistsError:
                violations += 1
                fd = None
            value = int(counter.read_text() or 0)
            time.sleep(0.0005)   # 배제가 깨져 있으면 갱신 유실이 드러나도록 구간을 넓힌다
            counter.write_text(str(value + 1))
            if fd is not None:
                os.close(fd)
                os.remove(marker)
    print("VIOLATIONS", violations, flush=True)
""")


class FakeClock:
    """sleep 이 시계를 앞으로 보내므로 실제로 기다리지 않는다."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def kwargs(self):
        return {"monotonic": self.monotonic, "sleep": self.sleep}


def _env():
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _spawn(code, *args):
    return subprocess.Popen([sys.executable, "-B", "-c", code, *map(str, args)], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                            env=_env(), cwd=str(ROOT))


def _wait_line(proc, expected, limit=60.0):
    """하위 프로세스가 expected 한 줄을 출력할 때까지 기다린다(이벤트 기반, 최대 limit 초)."""
    box = []
    reader = threading.Thread(target=lambda: box.append(proc.stdout.readline()), daemon=True)
    reader.start()
    reader.join(limit)
    return bool(box) and box[0].strip() == expected


def _file_handles_released(path):
    """Windows: 열린 핸들이 있으면 이름 바꾸기가 거부되므로 왕복 이름 바꾸기로 확인한다. 그 외: 열린 fd 개수는 별도로 비교한다."""
    if os.name == "nt":
        tmp = str(path) + ".chk"
        os.rename(path, tmp)
        os.rename(tmp, path)
    return True


def _open_fd_count():
    proc_fd = Path("/proc/self/fd")
    return len(list(proc_fd.iterdir())) if proc_fd.exists() else None


class WeekLockBase(unittest.TestCase):
    def tmp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        return Path(d.name)

    def holder(self, locks_dir):
        proc = _spawn(HOLDER, locks_dir, WEEK)

        def cleanup():
            with contextlib.suppress(Exception):
                proc.kill()
            with contextlib.suppress(Exception):
                proc.wait(timeout=30)
            for stream in (proc.stdout, proc.stderr):
                with contextlib.suppress(Exception):
                    stream.close()

        self.addCleanup(cleanup)
        ready = _wait_line(proc, "LOCKED")
        if not ready:
            proc.kill()
            self.fail("holder process did not acquire the lock: " + proc.stderr.read()[-300:])
        return proc


class BasicBehaviourTests(WeekLockBase):
    def test_acquire_release_keeps_persistent_empty_lock_file(self):
        d = self.tmp() / "locks"
        lock = d / f"{WEEK}.lock"
        with ops.week_lock(d, WEEK) as p:
            self.assertEqual(p, lock)
            self.assertTrue(lock.exists())
        self.assertTrue(lock.exists())  # 해제해도 파일은 남는다
        self.assertEqual(lock.stat().st_size, 0)  # 내용을 쓰지 않는다
        with ops.week_lock(d, WEEK):  # 다시 잡을 수 있다
            pass
        self.assertTrue(lock.exists())
        self.assertEqual([p.name for p in d.iterdir()], [f"{WEEK}.lock"])

    def test_never_deletes_reads_or_writes_the_lock_file(self):
        d = self.tmp()
        boom = AssertionError("lock implementation must not delete/read/write the lock file")
        forbidden = mock.Mock(side_effect=boom)
        with contextlib.ExitStack() as st:
            for target, name in ((os, "unlink"), (os, "remove"), (os, "write"), (os, "rename"), (os, "replace"),
                                 (Path, "unlink"), (Path, "read_bytes"), (Path, "read_text"),
                                 (Path, "write_bytes"), (Path, "write_text")):
                st.enter_context(mock.patch.object(target, name, forbidden))
            for _ in range(3):
                with ops.week_lock(d, WEEK):
                    pass
            with self.assertRaises(RuntimeError):
                with ops.week_lock(d, WEEK):
                    raise RuntimeError("inside")
        forbidden.assert_not_called()

    def test_released_after_exception_inside_block(self):
        d = self.tmp()
        with self.assertRaises(RuntimeError):
            with ops.week_lock(d, WEEK):
                raise RuntimeError("boom")
        with ops.week_lock(d, WEEK, timeout=1.0, **FakeClock().kwargs()):
            pass

    def test_different_weeks_do_not_block_each_other(self):
        d = self.tmp()
        clock = FakeClock()
        with ops.week_lock(d, WEEK):
            with ops.week_lock(d, "2026-09-W4", timeout=1.0, **clock.kwargs()):
                pass
        self.assertEqual(clock.sleeps, [])

    def test_invalid_week_label_is_rejected_before_touching_the_filesystem(self):
        d = self.tmp() / "locks"
        for bad in ("../x", "2026-09", "", None, "2026-09-W3/../../x"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    with ops.week_lock(d, bad):
                        pass
                with self.assertRaises(ValueError):
                    ops.status_path(d, bad)
        self.assertFalse(d.exists())

    def test_thread_lock_is_shared_across_path_spellings(self):
        d = self.tmp()
        a = ops._thread_lock_for(d / "x" / ".." / "w.lock")
        b = ops._thread_lock_for(d / "w.lock")
        self.assertIs(a, b)
        self.assertIsNot(a, ops._thread_lock_for(d / "other.lock"))


class OsLockPrimitiveTests(WeekLockBase):
    def test_second_handle_is_excluded_until_the_first_unlocks(self):
        p = self.tmp() / "x.lock"
        a = os.open(p, os.O_RDWR | os.O_CREAT)
        b = os.open(p, os.O_RDWR | os.O_CREAT)
        try:
            self.assertTrue(ops._try_os_lock(a))
            self.assertFalse(ops._try_os_lock(b))  # 같은 프로세스의 다른 핸들도 배제된다
            ops._os_unlock(a)
            self.assertTrue(ops._try_os_lock(b))
            ops._os_unlock(b)
        finally:
            os.close(a)
            os.close(b)
        self.assertTrue(p.exists())

    def _patch_lock_call(self, side_effect):
        if os.name == "nt":
            import msvcrt
            return mock.patch.object(msvcrt, "locking", side_effect=side_effect)
        import fcntl
        return mock.patch.object(fcntl, "flock", side_effect=side_effect)

    def test_only_contention_errors_are_reported_as_busy(self):
        p = self.tmp() / "x.lock"
        fd = os.open(p, os.O_RDWR | os.O_CREAT)
        try:
            for code in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                with self.subTest(code=code), self._patch_lock_call(OSError(code, "busy")):
                    self.assertFalse(ops._try_os_lock(fd))
            for code in (errno.EIO, errno.EBADF, errno.ENOSPC):
                with self.subTest(code=code), self._patch_lock_call(OSError(code, "other")):
                    with self.assertRaises(OSError):
                        ops._try_os_lock(fd)
        finally:
            os.close(fd)


class ErrorHandlingTests(WeekLockBase):
    def test_non_contention_lock_error_fails_immediately_without_retry(self):
        d = self.tmp()
        clock = FakeClock()
        with mock.patch.object(ops, "_try_os_lock", side_effect=OSError(errno.EIO, "io")):
            with self.assertRaises(StatusWriteError) as cm:
                with ops.week_lock(d, WEEK, timeout=1.0, **clock.kwargs()):
                    pass
        self.assertEqual(str(cm.exception), "lock_failed:OSError")
        self.assertEqual(clock.sleeps, [])
        with ops.week_lock(d, WEEK, timeout=1.0, **clock.kwargs()):  # 스레드 락·핸들이 정리되어 다시 잡을 수 있다
            pass

    def test_open_failure_including_permission_error_is_lock_failed_not_retried(self):
        d = self.tmp()
        for exc, name in ((PermissionError(errno.EACCES, "denied"), "PermissionError"), (OSError(errno.EMFILE, "many"), "OSError")):
            with self.subTest(exc=name):
                clock = FakeClock()
                with mock.patch.object(ops.os, "open", side_effect=exc):
                    with self.assertRaises(StatusWriteError) as cm:
                        with ops.week_lock(d, WEEK, timeout=1.0, **clock.kwargs()):
                            pass
                self.assertEqual(str(cm.exception), f"lock_failed:{name}")
                self.assertEqual(clock.sleeps, [])

    def test_lock_dir_creation_failure(self):
        d = self.tmp()
        blocker = d / "file"
        blocker.write_text("x", encoding="utf-8")
        with self.assertRaises(StatusWriteError) as cm:
            with ops.week_lock(blocker / "sub", WEEK):
                pass
        self.assertTrue(str(cm.exception).startswith("lock_dir_failed:"))


class TimeoutTests(WeekLockBase):
    def test_thread_level_contention_times_out_with_fake_clock(self):
        d = self.tmp()
        clock = FakeClock()
        with ops.week_lock(d, WEEK):
            with self.assertRaises(StatusWriteError) as cm:
                with ops.week_lock(d, WEEK, timeout=1.0, **clock.kwargs()):
                    pass
        self.assertEqual(str(cm.exception), "lock_timeout")
        self.assertTrue(0 < len(clock.sleeps) <= 200)
        self.assertTrue(all(s > 0 for s in clock.sleeps))

    def test_os_level_contention_times_out_and_cleans_up_handle(self):
        d = self.tmp()
        d.mkdir(exist_ok=True)
        lock = d / f"{WEEK}.lock"
        raw = os.open(lock, os.O_RDWR | os.O_CREAT)  # 스레드 락을 거치지 않고 OS 잠금만 보유
        try:
            self.assertTrue(ops._try_os_lock(raw))
            before = _open_fd_count()
            clock = FakeClock()
            with self.assertRaises(StatusWriteError) as cm:
                with ops.week_lock(d, WEEK, timeout=1.0, **clock.kwargs()):
                    pass
            self.assertEqual(str(cm.exception), "lock_timeout")
            self.assertTrue(0 < len(clock.sleeps) <= 200)
            if before is not None:
                self.assertEqual(_open_fd_count(), before)  # 시간 초과 후 열린 핸들이 늘지 않았다
            ops._os_unlock(raw)
        finally:
            os.close(raw)
        self.assertTrue(_file_handles_released(lock))  # Windows: 핸들 잔여가 없으면 이름 바꾸기가 된다
        with ops.week_lock(d, WEEK, timeout=1.0, **FakeClock().kwargs()):
            pass

    def test_lock_held_by_another_process_times_out_and_is_not_stolen(self):
        d = self.tmp()
        proc = self.holder(d)
        lock = d / f"{WEEK}.lock"
        old = time.time() - 10_000
        os.utime(lock, (old, old))  # 아주 오래된 수정 시각이어도 stale 로 회수하지 않는다
        clock = FakeClock()
        with self.assertRaises(StatusWriteError) as cm:
            with ops.week_lock(d, WEEK, timeout=1.0, **clock.kwargs()):
                pass
        self.assertEqual(str(cm.exception), "lock_timeout")
        self.assertTrue(0 < len(clock.sleeps) <= 200)
        self.assertIsNone(proc.poll())  # 보유 프로세스는 계속 살아 있고 락도 그대로
        self.assertTrue(lock.exists())

    def test_record_run_surfaces_lock_timeout_and_writes_nothing(self):
        d = self.tmp()
        proc = self.holder(d / "logs" / ".locks")
        from datetime import date
        from dates import WeekPeriod
        w3 = WeekPeriod(year=2026, week_label=WEEK, start=date(2026, 9, 21), end=date(2026, 9, 27))
        with mock.patch.object(ops, "LOCK_TIMEOUT_SECONDS", 0.2):
            with self.assertRaises(StatusWriteError):
                ops.record_run(d, w3, "insight", "r1", "2026-11-02T09:00:00+09:00",
                               {"trend": ops.SectionOutcome("fake", saved_new=1, saved_total=1)}, [], lock_timeout=0.2)
        self.assertFalse((d / "ops").exists())
        self.assertIsNone(proc.poll())


class ConcurrencyTests(WeekLockBase):
    def test_threads_never_overlap_and_never_time_out(self):
        d = self.tmp()
        state = {"inside": 0, "max": 0, "count": 0}
        errors = []

        def worker():
            try:
                for _ in range(100):
                    with ops.week_lock(d, WEEK, timeout=120):
                        state["inside"] += 1
                        state["max"] = max(state["max"], state["inside"])
                        time.sleep(0)
                        state["count"] += 1
                        state["inside"] -= 1
            except Exception as e:  # pragma: no cover
                errors.append(repr(e))

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual(errors, [])
        self.assertEqual((state["max"], state["count"]), (1, 400))

    def test_repeated_thread_contention_never_leaves_the_lock_stuck(self):
        # 이전 구현(파일 삭제 기반)은 Windows 에서 경쟁 중 삭제 실패로 락이 고아가 되어 간헐적으로 timeout 이 났다.
        for round_no in range(5):
            d = self.tmp()
            errors = []

            def worker():
                try:
                    for _ in range(40):
                        with ops.week_lock(d, WEEK, timeout=120):
                            time.sleep(0)
                except Exception as e:  # pragma: no cover
                    errors.append(repr(e))

            threads = [threading.Thread(target=worker) for _ in range(2)]
            for th in threads:
                th.start()
            for th in threads:
                th.join()
            self.assertEqual(errors, [], f"round {round_no}")

    def test_processes_never_enter_the_critical_section_together(self):
        d = self.tmp() / "locks"
        counter = self.tmp() / "counter.txt"
        marker = self.tmp() / "inside.marker"
        counter.write_text("0", encoding="utf-8")
        procs, per_proc, n_procs = [], 15, 4
        for _ in range(n_procs):
            procs.append(_spawn(COUNTER_WORKER, d, WEEK, counter, marker, per_proc))
        outputs = []
        for proc in procs:
            out, err = proc.communicate(timeout=240)
            outputs.append((proc.returncode, out.strip(), err.strip()[-200:]))
        for code, out, err in outputs:
            self.assertEqual(code, 0, err)
            self.assertEqual(out, "VIOLATIONS 0")
        self.assertEqual(int(counter.read_text()), per_proc * n_procs)  # 갱신 유실 없음
        self.assertFalse(marker.exists())
        self.assertEqual([p.name for p in d.iterdir()], [f"{WEEK}.lock"])

    def test_lock_is_available_immediately_after_the_holder_process_is_killed(self):
        d = self.tmp()
        proc = self.holder(d)
        lock = d / f"{WEEK}.lock"
        proc.kill()  # 정상 종료 없이 강제 종료: OS 가 잠금을 해제해야 한다
        proc.wait(timeout=30)
        with ops.week_lock(d, WEEK, timeout=30):  # 해제되는 즉시 반환된다(고정 대기 없음)
            pass
        self.assertTrue(lock.exists())
        self.assertEqual(lock.stat().st_size, 0)


class ResidueTests(WeekLockBase):
    def test_no_handles_or_lock_state_remain_after_normal_error_and_timeout_paths(self):
        d = self.tmp()
        lock = d / f"{WEEK}.lock"
        before = _open_fd_count()
        for _ in range(10):
            with ops.week_lock(d, WEEK):
                pass
        with self.assertRaises(RuntimeError):
            with ops.week_lock(d, WEEK):
                raise RuntimeError("x")
        with mock.patch.object(ops, "_try_os_lock", side_effect=OSError(errno.EIO, "io")):
            with self.assertRaises(StatusWriteError):
                with ops.week_lock(d, WEEK):
                    pass
        if before is not None:
            self.assertEqual(_open_fd_count(), before)
        self.assertTrue(_file_handles_released(lock))
        raw = os.open(lock, os.O_RDWR | os.O_CREAT)
        try:
            self.assertTrue(ops._try_os_lock(raw))  # 잠금이 남아 있지 않다
            ops._os_unlock(raw)
        finally:
            os.close(raw)
        self.assertFalse(ops._thread_lock_for(lock).locked())  # 스레드 락도 풀려 있다


if __name__ == "__main__":
    unittest.main()
