"""Межпроцессная блокировка каталога данных на Windows и POSIX."""

from pathlib import Path


class ProcessLock:
    """Держит системную блокировку открытого файла до выхода или завершения процесса."""

    def __init__(self, path: Path):
        """Запоминает путь; файл создаётся только при входе в контекст."""
        self.path = Path(path)
        self.handle = None

    def __enter__(self):
        """Захватывает неблокирующий lock; второй процесс получает понятную ошибку."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            import os
            if os.name == "nt":
                import msvcrt
                if handle.seek(0, 2) == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise RuntimeError("Этот каталог данных открыт другим процессом ag-tasks. Сначала остановите его.") from None
        self.handle = handle
        return self

    def __exit__(self, exc_type, exc, traceback):
        """Освобождает lock, не удаляя общий lock-файл конкурирующих процессов."""
        if self.handle is not None:
            import os
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            self.handle.close()
            self.handle = None
