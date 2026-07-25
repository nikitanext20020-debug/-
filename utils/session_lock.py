"""
Cross-platform exclusive lock for Telethon .session files.

Prevents two threads/processes from opening the same session SQLite file.
Uses fcntl.flock on Unix and msvcrt locking on Windows. The lock is held on a
sidecar "*.session.lock" file so Telethon can still open the real session DB.
"""
from __future__ import annotations

import os
import sys
import time
from typing import Optional


class SessionLockError(RuntimeError):
    """Raised when an exclusive session lock cannot be acquired."""


class SessionFileLock:
    """
    OS-backed exclusive lock for a Telethon session path.

    Accepts either ``.../name`` or ``.../name.session``; the lock file is always
    ``.../name.session.lock`` next to the real session database.
    """

    def __init__(self, session_path: str):
        if not session_path:
            raise ValueError("session_path is required")
        path = os.path.abspath(session_path)
        if path.endswith(".session"):
            self.session_file = path
        else:
            self.session_file = path + ".session"
        self.lock_file = self.session_file + ".lock"
        self._fh = None
        self._acquired = False

    @property
    def locked(self) -> bool:
        return self._acquired

    def acquire(self, timeout: Optional[float] = 0.0, poll_interval: float = 0.1) -> "SessionFileLock":
        """
        Acquire an exclusive lock.

        :param timeout: seconds to wait; 0 = try once; None = block forever
        :raises SessionLockError: if the lock cannot be taken
        """
        if self._acquired:
            return self

        os.makedirs(os.path.dirname(self.lock_file) or ".", exist_ok=True)
        
        deadline = None
        if timeout is not None:
            deadline = time.monotonic() + max(0.0, float(timeout))

        attempt = 0
        while True:
            attempt += 1
            try:
                # ✅ Закрываем старый файловый дескриптор перед новой попыткой
                if self._fh:
                    try:
                        self._fh.close()
                    except:
                        pass
                    self._fh = None
                
                # Открываем заново
                self._fh = open(self.lock_file, "a+b")
                
                self._lock_fd(self._fh.fileno(), blocking=False)
                self._acquired = True
                try:
                    self._fh.seek(0)
                    self._fh.truncate(0)
                    payload = f"pid={os.getpid()} time={time.time():.3f}\n".encode("ascii", "replace")
                    self._fh.write(payload)
                    self._fh.flush()
                except Exception:
                    pass
                return self
            except (BlockingIOError, OSError, SessionLockError) as e:
                self._close_fh()
                
                if timeout == 0 or (deadline is not None and time.monotonic() >= deadline):
                    raise SessionLockError(
                        f"Session file already in use: {self.session_file}"
                    )
                # blocking forever or still waiting
                if timeout is None:
                    try:
                        self._lock_fd(self._fh.fileno(), blocking=True)
                        self._acquired = True
                        return self
                    except Exception as e:
                        self._close_fh()
                        raise SessionLockError(
                            f"Failed to lock session file {self.session_file}: {e}"
                        ) from e
                
                # На Windows иногда нужна большая задержка
                wait_time = min(poll_interval * (attempt ** 0.5), 1.0)
                time.sleep(wait_time)

    def release(self) -> None:
        """Release the lock if held. Safe to call multiple times."""
        if not self._fh:
            self._acquired = False
            return
        try:
            if self._acquired:
                self._unlock_fd(self._fh.fileno())
        except Exception:
            pass
        finally:
            self._acquired = False
            self._close_fh()

    def __enter__(self) -> "SessionFileLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()

    def __del__(self):
        try:
            self.release()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # platform bits
    # ------------------------------------------------------------------

    def _close_fh(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None

    def _lock_fd(self, fd: int, blocking: bool = False) -> None:
        if sys.platform == "win32":
            self._lock_win(fd, blocking=blocking)
        else:
            self._lock_unix(fd, blocking=blocking)

    def _unlock_fd(self, fd: int) -> None:
        if sys.platform == "win32":
            self._unlock_win(fd)
        else:
            self._unlock_unix(fd)

    @staticmethod
    def _lock_unix(fd: int, blocking: bool = False) -> None:
        import fcntl

        flags = fcntl.LOCK_EX
        if not blocking:
            flags |= fcntl.LOCK_NB
        try:
            fcntl.flock(fd, flags)
        except BlockingIOError:
            raise
        except OSError as e:
            # EAGAIN / EACCES / EWOULDBLOCK depending on platform
            if getattr(e, "errno", None) in (
                getattr(__import__("errno"), "EAGAIN", 11),
                getattr(__import__("errno"), "EACCES", 13),
                getattr(__import__("errno"), "EWOULDBLOCK", 11),
            ):
                raise BlockingIOError from e
            raise

    @staticmethod
    def _unlock_unix(fd: int) -> None:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)

    @staticmethod
    def _lock_win(fd: int, blocking: bool = False) -> None:
        import msvcrt

        # ✅ Lock one byte at the start of the lock file
        # msvcrt.locking works on the current file position
        try:
            os.lseek(fd, 0, os.SEEK_SET)
        except (OSError, ValueError):
            # Если уже закрыт, ошибка
            raise BlockingIOError("File descriptor is invalid")
        
        mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
        try:
            # Ensure the file has at least 1 byte to lock
            try:
                size = os.fstat(fd).st_size
            except Exception:
                size = 0
            if size < 1:
                try:
                    os.write(fd, b"\0")
                    os.lseek(fd, 0, os.SEEK_SET)
                except (OSError, ValueError):
                    pass
            
            msvcrt.locking(fd, mode, 1)
        except OSError as e:
            raise BlockingIOError from e

    @staticmethod
    def _unlock_win(fd: int) -> None:
        import msvcrt

        try:
            # ✅ Проверяем что дескриптор еще живой
            try:
                os.lseek(fd, 0, os.SEEK_SET)
            except (OSError, ValueError):
                # Дескриптор уже закрыт, ничего не делаем
                return
            
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            # Молча игнорируем ошибки разблокировки
            pass
