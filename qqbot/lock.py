"""单实例锁：同一个机器人只允许跑一个进程。

否则两个进程都连着 NapCat，同一条消息会被回两遍。
用文件锁实现（进程崩溃时操作系统会自动释放，不会留下"假死锁"）。
"""

from __future__ import annotations

import os
from pathlib import Path


class InstanceLock:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._handle = None

    def acquire(self) -> bool:
        """拿到锁返回 True；已经有别的实例在跑返回 False。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False

        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self._handle.close()
            self._handle = None
