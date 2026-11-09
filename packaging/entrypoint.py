"""Frozen application entrypoint; Finder starts Tasks's own native window."""

import argparse
import logging
from logging.handlers import RotatingFileHandler
import multiprocessing
import os
from pathlib import Path
import sys


def logging_directory(arguments=None, environment=None) -> Path:
    """Honor explicit CLI, then service environment, then the installer-owned data locator."""
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument('--data-dir', type=Path)
    options, _ = parser.parse_known_args(sys.argv[1:] if arguments is None else arguments)
    if options.data_dir is not None:
        return options.data_dir
    effective = os.environ if environment is None else environment
    if effective.get('AG_TASKS_DATA_DIR'):
        return Path(effective['AG_TASKS_DATA_DIR']).expanduser()
    from ag_tasks.config import default_data_dir
    return default_data_dir()


def configure_logging() -> None:
    """Retain bounded startup failures when a windowed executable has no terminal streams."""
    folder = logging_directory()
    folder.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(folder / "desktop.log", maxBytes=1_048_576, backupCount=1, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.basicConfig(level=logging.WARNING, handlers=[handler])


class _FinalFlushStream:
    """Delegate output normally while tolerating only a late close during frozen finalization."""

    def __init__(self, stream):
        """Retain the actual stream so genuine writes and other failures keep their normal behavior."""
        self.stream = stream

    def __getattr__(self, name):
        """Preserve the standard stream interface for logging and the frozen bootloader."""
        return getattr(self.stream, name)

    def flush(self):
        """MCP can close its binary wrapper after CLI returns; a closed stream has nothing left to flush."""
        try:
            self.stream.flush()
        except ValueError:
            if not self.stream.closed:
                raise


def repair_closed_standard_streams() -> None:
    """Preserve final output but tolerate MCP wrappers that close during delayed cleanup.

    Replacement happens only after CLI completion; it cannot swallow MCP replies.
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if stream is None or stream.closed:
            stream = open(os.devnull, "w", encoding="utf-8")
        wrapped = _FinalFlushStream(stream)
        setattr(sys, name, wrapped)
        # PyInstaller flushes both public and original aliases when they differ.
        setattr(sys, "__" + name + "__", wrapped)


def main() -> None:
    """Delegate to Tasks CLI only; no Atlas code or external Python installation is required."""
    multiprocessing.freeze_support()
    configure_logging()
    if len(sys.argv) == 1:
        sys.argv.append("--desktop")
    from ag_tasks.__main__ import main as tasks_main
    try:
        tasks_main()
    except Exception:
        logging.exception("Tasks startup failed")
        raise
    finally:
        repair_closed_standard_streams()


if __name__ == "__main__":
    main()
