"""Local native process metrics and cached journal-only disk measurements, without an observer dependency."""

import ctypes
import os
from pathlib import Path
import struct
import sys
import threading
import time

from fastapi import Request

from .events import utc_now
from .http_api import permitted_request, http_response
from .http_query import query_pairs

STARTED = time.monotonic()
UPTIME_ANCHOR = None


def process_uptime(started):
    """Anchors native process age once; subsequent wall-clock corrections cannot change elapsed uptime."""
    global UPTIME_ANCHOR
    now = time.monotonic()
    if UPTIME_ANCHOR is None:
        age = max(0, time.time() - started) if started is not None else max(0, now - STARTED)
        UPTIME_ANCHOR = (now, age)
    return max(0, UPTIME_ANCHOR[1] + now - UPTIME_ANCHOR[0])


def native_process():
    """Reads current RSS and native process start time; peak RSS is a separate platform counter."""
    resident, peak, started = None, None, None
    if sys.platform == "darwin":
        library = ctypes.CDLL("/usr/lib/libproc.dylib")
        function = library.proc_pidinfo
        function.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
        function.restype = ctypes.c_int
        task, bsd = ctypes.create_string_buffer(96), ctypes.create_string_buffer(136)
        # Darwin proc_taskinfo.resident_size and proc_bsdinfo.start_tvsec/start_tvusec, fixed native ABI.
        if function(os.getpid(), 4, 0, task, len(task)) == len(task):
            resident = struct.unpack_from("=Q", task.raw, 8)[0]
        if function(os.getpid(), 3, 0, bsd, len(bsd)) == len(bsd):
            seconds, micros = struct.unpack_from("=QQ", bsd.raw, 120)
            started = seconds + micros / 1_000_000
    elif sys.platform.startswith("linux"):
        with Path("/proc/self/statm").open() as handle:
            resident = int(handle.read(1024).split()[1]) * os.sysconf("SC_PAGE_SIZE")
        with Path("/proc/self/stat").open() as handle:
            fields = handle.read(16384).rsplit(")", 1)[1].split()
        with Path("/proc/uptime").open() as handle:
            elapsed = float(handle.read(128).split()[0]) - int(fields[19]) / os.sysconf("SC_CLK_TCK")
        started = time.time() - elapsed
    elif os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        process = kernel.GetCurrentProcess()
        times = [ctypes.c_uint64() for _ in range(4)]
        kernel.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(ctypes.c_uint64)] * 4
        if kernel.GetProcessTimes(process, *(ctypes.byref(value) for value in times)):
            started = times[0].value / 10_000_000 - 11644473600
        class MemoryCounters(ctypes.Structure):
            """Windows PROCESS_MEMORY_COUNTERS keeps current and peak working set distinct."""
            _fields_ = [("cb", ctypes.c_ulong), ("faults", ctypes.c_ulong)] + [
                (name, ctypes.c_size_t) for name in ("peak", "current", "peakPaged", "paged", "peakNonpaged", "nonpaged", "pagefile", "peakPagefile")]
        counters = MemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
        if psapi.GetProcessMemoryInfo(process, ctypes.byref(counters), counters.cb):
            resident, peak = counters.current, counters.peak
    if os.name != "nt":
        import resource
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if sys.platform != "darwin":
            peak *= 1024
    uptime = process_uptime(started)
    return {"processId": os.getpid(), "residentBytes": resident, "peakResidentBytes": peak, "uptimeSeconds": uptime}


def owned_directory_bytes(root):
    """Measures only the configured service-owned directory, bounded to 10,000 entries/depth eight."""
    pending, count, total = [(Path(root), 0)], 0, 0
    if Path(root).is_symlink():
        raise OSError("Owned data symlink")
    while pending:
        directory, depth = pending.pop()
        if depth > 8:
            raise OSError("Owned data depth limit")
        with os.scandir(directory) as entries:
            for entry in entries:
                count += 1
                if count > 10000 or entry.is_symlink():
                    raise OSError("Owned data measurement limit")
                if entry.is_dir(follow_symlinks=False):
                    pending.append((Path(entry.path), depth + 1))
                elif entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
    return total


class Diagnostics:
    """Caches a bounded flat walk of only the two owned journals for five seconds."""

    def __init__(self, journals, owned_data_dir=None):
        """Retains service references, never traverses user project or source directories."""
        self.journals = journals
        self.lock = threading.Lock()
        self.expires = 0
        self.storage = None
        self.storage_error = None
        self.owned_data_dir = owned_data_dir
        self.data_expires = 0
        self.data_bytes = None
        self.data_error = None

    def owned_usage(self):
        """Caches the explicit service data root for 60s; an incomplete measurement stays null."""
        if self.owned_data_dir is None:
            return None, None
        with self.lock:
            if time.monotonic() >= self.data_expires:
                try:
                    self.data_bytes = owned_directory_bytes(self.owned_data_dir)
                    self.data_error = None
                except (OSError, ValueError):
                    self.data_bytes, self.data_error = None, "owned_data_measurement_unavailable"
                self.data_expires = time.monotonic() + 60
            return self.data_bytes, self.data_error

    def disk_usage(self):
        """Counts owned journal files without recursion/symlink following; failures remain unknown."""
        with self.lock:
            if time.monotonic() < self.expires:
                return self.storage, self.storage_error
            total, error = 0, None
            try:
                for journal in self.journals:
                    if journal.disk is None:
                        raise OSError("Journal unavailable")
                    with journal.disk.lock:
                        with os.scandir(journal.disk.directory) as entries:
                            for index, entry in enumerate(entries):
                                if index >= 512 or entry.is_symlink():
                                    raise OSError("Unbounded journal directory")
                                if entry.is_file(follow_symlinks=False):
                                    total += entry.stat(follow_symlinks=False).st_size
            except (OSError, ValueError):
                total, error = None, "journal_measurement_unavailable"
            self.storage = {"journalBytes": total, "dataBytes": None, "measurement": "journals-only"}
            self.storage_error = error
            self.expires = time.monotonic() + 5
            return self.storage, error

    def snapshot(self):
        """Returns truthful nullable measurements; collection failures never expose paths or exceptions."""
        error = None
        try:
            process = native_process()
            if process["residentBytes"] is None:
                error = "process_measurement_unavailable"
        except (OSError, ValueError, IndexError, AttributeError, struct.error):
            process = {"processId": os.getpid(), "residentBytes": None, "peakResidentBytes": None,
                       "uptimeSeconds": process_uptime(None)}
            error = "process_measurement_unavailable"
        storage, storage_error = self.disk_usage()
        data_bytes, data_error = self.owned_usage()
        storage = {**storage, "dataBytes": data_bytes,
                   "measurement": "owned-data" if self.owned_data_dir is not None else "journals-only"}
        error = error or storage_error or data_error
        return {"schemaVersion": "service-diagnostics/v1", "application": 'ag-tasks', "collectedAt": utc_now(),
                "process": process, "storage": storage,
                "health": {"status": "degraded" if error else "ok", "errorCode": error}}


def register_diagnostics_routes(app, journals, owned_data_dir=None):
    """Registers only local GET metrics; no push, projects, settings or observer address is accepted."""
    diagnostics = Diagnostics(journals, owned_data_dir)

    @app.get("/api/diagnostics/v1/metrics")
    def metrics(request: Request):
        """Returns a native snapshot using the same local peer/Host/Origin guard as log retrieval."""
        if not permitted_request(request):
            return diagnostics_error(403)
        try:
            if query_pairs(request):
                return diagnostics_error(400)
        except (ValueError, UnicodeError):
            return diagnostics_error(400)
        return http_response(diagnostics.snapshot())


def diagnostics_error(status):
    """Uses the portable diagnostics envelope without echoing query, host or filesystem details."""
    code, message = ("forbidden", "Access forbidden.") if status == 403 else ("invalid_query", "Invalid query parameters.")
    return http_response({"schemaVersion": "service-diagnostics/v1", "application": 'ag-tasks',
                          "error": {"code": code, "message": message, "retryable": False}, "resetCursor": None}, status)
