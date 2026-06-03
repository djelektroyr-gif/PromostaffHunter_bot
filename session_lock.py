"""Эксклюзивная блокировка user_session — один процесс Telethon на файл сессии."""
import os
import sys

LOCK_PATH = "user_session.lock"
_lock_handle = None


class SessionLockError(Exception):
    pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_lock_pid() -> int | None:
    if not os.path.exists(LOCK_PATH):
        return None
    try:
        with open(LOCK_PATH, "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except (ValueError, OSError):
        return None


def acquire_session_lock() -> None:
    """Блокирует user_session. Вызывать один раз при старте процесса."""
    global _lock_handle

    stale_pid = _read_lock_pid()
    if stale_pid and not _pid_alive(stale_pid):
        try:
            os.remove(LOCK_PATH)
        except OSError:
            pass

    if sys.platform == "win32":
        import msvcrt

        fp = open(LOCK_PATH, "a+", encoding="utf-8")
        try:
            msvcrt.locking(fp.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as e:
            fp.close()
            other = _read_lock_pid()
            raise SessionLockError(
                f"user_session уже используется другим процессом (PID {other or '?'}). "
                f"Остановите второй инстанс бота."
            ) from e
    else:
        import fcntl

        fp = open(LOCK_PATH, "w", encoding="utf-8")
        try:
            fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            fp.close()
            other = _read_lock_pid()
            raise SessionLockError(
                f"user_session уже используется другим процессом (PID {other or '?'}). "
                f"Остановите второй инстанс бота."
            ) from e

    fp.seek(0)
    fp.truncate()
    fp.write(str(os.getpid()))
    fp.flush()
    _lock_handle = fp


def release_session_lock() -> None:
    global _lock_handle
    if _lock_handle:
        try:
            _lock_handle.close()
        except OSError:
            pass
        _lock_handle = None
    try:
        if os.path.exists(LOCK_PATH):
            os.remove(LOCK_PATH)
    except OSError:
        pass
