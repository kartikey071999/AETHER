"""OS-owned child lifetimes, including descendants after the direct child exits."""

import ctypes
import os
import signal
from ctypes import wintypes


class _BasicLimits(ctypes.Structure):
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


class _IoCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_uint64)
        for name in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        )
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class ProcessContainment:
    """Windows children start suspended and join a kill-on-close job before user code runs.

    POSIX children lead a new session. Job setup failures fail closed; callers retain a
    psutil cleanup fallback for a child that could not be attached.
    """

    def __init__(self) -> None:
        self._job = None
        if os.name != "nt":
            return
        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._native = ctypes.WinDLL("ntdll")
        for name, args, result in (
            ("CreateJobObjectW", [ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            (
                "SetInformationJobObject",
                [
                    wintypes.HANDLE,
                    ctypes.c_int,
                    ctypes.c_void_p,
                    wintypes.DWORD,
                ],
                wintypes.BOOL,
            ),
            ("OpenProcess", [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            ("AssignProcessToJobObject", [wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            ("TerminateJobObject", [wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            ("CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
        ):
            function = getattr(self._kernel, name)
            function.argtypes, function.restype = args, result
        self._native.NtResumeProcess.argtypes = [wintypes.HANDLE]
        self._native.NtResumeProcess.restype = wintypes.LONG
        job = self._kernel.CreateJobObjectW(None, None)
        if not job:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        if not self._kernel.SetInformationJobObject(
            job, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            error = ctypes.WinError(ctypes.get_last_error())
            self._kernel.CloseHandle(job)
            raise error
        self._job = job

    @property
    def spawn_options(self) -> dict[str, bool | int]:
        if os.name == "nt":
            return {"creationflags": 0x00000004}  # CREATE_SUSPENDED
        return {"start_new_session": True}

    def attach(self, pid: int) -> None:
        if os.name != "nt":
            return
        # SET_QUOTA | TERMINATE | SUSPEND_RESUME
        process = self._kernel.OpenProcess(0x0100 | 0x0001 | 0x0800, False, pid)
        if not process:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not self._kernel.AssignProcessToJobObject(self._job, process):
                raise ctypes.WinError(ctypes.get_last_error())
            # asyncio/Popen closes the primary thread handle. Resume through the process handle.
            status = self._native.NtResumeProcess(process)
            if status < 0:
                raise OSError(f"NtResumeProcess failed with NTSTATUS {status & 0xFFFFFFFF:#x}")
        finally:
            self._kernel.CloseHandle(process)

    def terminate(self, pid: int | None) -> None:
        if os.name == "nt":
            if self._job is None:
                return
            job, self._job = self._job, None
            try:
                if not self._kernel.TerminateJobObject(job, 1):
                    raise ctypes.WinError(ctypes.get_last_error())
            finally:
                if not self._kernel.CloseHandle(job):
                    raise ctypes.WinError(ctypes.get_last_error())
        elif pid is not None:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
