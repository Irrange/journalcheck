from __future__ import annotations

import sys
from dataclasses import dataclass


MUTEX_NAME = r"Local\JournalCheck.SingleInstance"


@dataclass(slots=True)
class SingleInstanceLock:
    handle: int | None = None

    def close(self) -> None:
        if self.handle is None or sys.platform != "win32":
            self.handle = None
            return
        import ctypes

        ctypes.windll.kernel32.CloseHandle(self.handle)
        self.handle = None


def acquire_single_instance() -> SingleInstanceLock | None:
    if sys.platform != "win32":
        return SingleInstanceLock()

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.GetLastError.argtypes = []
    kernel32.GetLastError.restype = wintypes.DWORD

    handle = kernel32.CreateMutexW(None, True, MUTEX_NAME)
    if not handle:
        return SingleInstanceLock()

    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return None

    return SingleInstanceLock(handle=int(handle))


def show_already_running_message() -> None:
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.MessageBoxW(
            None,
            "JournalCheck is already running.\nPlease use the existing window.",
            "JournalCheck",
            0x00000040,
        )
        return

    try:
        from tkinter import messagebox

        messagebox.showinfo("JournalCheck", "JournalCheck is already running.\nPlease use the existing window.")
    except Exception:
        print("JournalCheck is already running.")
