"""Cross-platform child-process-tree lifetime management."""

import ctypes
import os
import signal
import subprocess
import time


def hidden_process_creationflags():
    """Windows flags for a short helper that needs no console object."""
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def hidden_process_startupinfo(startupinfo=None):
    """Hide a Windows console window while retaining its console semantics."""
    if os.name != "nt":
        return startupinfo
    startupinfo = startupinfo or subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return startupinfo


class _WindowsJob:
    """A Job Object whose members are killed when the handle is closed."""

    def __init__(self, process):
        from ctypes import wintypes

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        )
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        info = EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            handle, 9, ctypes.byref(info), ctypes.sizeof(info)
        ):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(handle)
            raise ctypes.WinError(error)
        if not kernel32.AssignProcessToJobObject(handle, process._handle):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(handle)
            raise ctypes.WinError(error)
        self.kernel32 = kernel32
        self.handle = handle

    def terminate(self):
        if self.handle:
            self.kernel32.TerminateJobObject(self.handle, 1)

    def close(self):
        if self.handle:
            self.kernel32.CloseHandle(self.handle)
            self.handle = None


class ProcessTree:
    """A Popen process and every descendant it creates."""

    def __init__(self, command, **kwargs):
        self.job = None
        if os.name == "nt":
            kwargs["creationflags"] = (
                kwargs.get("creationflags", 0) | subprocess.CREATE_NEW_PROCESS_GROUP
            )
            kwargs["startupinfo"] = hidden_process_startupinfo(
                kwargs.get("startupinfo")
            )
        else:
            kwargs["start_new_session"] = True
        self.process = subprocess.Popen(command, **kwargs)
        if os.name == "nt":
            try:
                self.job = _WindowsJob(self.process)
            except BaseException:
                self.process.kill()
                self.process.wait()
                raise

    def stop(self, graceful_timeout=5, sweep_timeout=5):
        process = self.process
        if process is None:
            return
        if os.name == "nt":
            if process.poll() is None:
                # CoreCLR applications can escape a containing Job Object.
                # taskkill snapshots the descendant tree while the direct
                # Python launcher still exists, so those breakaway children
                # cannot be orphaned when that launcher is terminated.
                subprocess.run(
                    ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=hidden_process_creationflags(),
                )
                try:
                    process.wait(timeout=graceful_timeout)
                except subprocess.TimeoutExpired:
                    if self.job is not None:
                        self.job.terminate()
                    else:
                        process.kill()
                    process.wait()
            # Closing a KILL_ON_JOB_CLOSE job also removes descendants left
            # behind after the direct Python launcher exited.
            if self.job is not None:
                self.job.close()
                self.job = None
            self.process = None
            return

        pgid = process.pid
        if process.poll() is None:
            try:
                os.killpg(pgid, signal.SIGTERM)
                try:
                    process.wait(timeout=graceful_timeout)
                except subprocess.TimeoutExpired:
                    pass
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + sweep_timeout
        while time.monotonic() < deadline:
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.2)
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if process.poll() is None:
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        self.process = None

    def running(self):
        return self.process is not None and self.process.poll() is None
