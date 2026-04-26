from __future__ import annotations

import ctypes
import sys
import threading
from pathlib import Path


if sys.platform == "win32":
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    shell32 = ctypes.windll.shell32
    kernel32 = ctypes.windll.kernel32

    IMAGE_ICON = 1
    LR_LOADFROMFILE = 0x0010
    LR_DEFAULTSIZE = 0x0040
    WM_APP = 0x8000
    WM_CLOSE = 0x0010
    WM_DESTROY = 0x0002
    WM_LBUTTONUP = 0x0202
    WM_LBUTTONDBLCLK = 0x0203
    WM_RBUTTONUP = 0x0205
    WM_RBUTTONDBLCLK = 0x0206
    NIM_ADD = 0x00000000
    NIM_DELETE = 0x00000002
    NIF_MESSAGE = 0x00000001
    NIF_ICON = 0x00000002
    NIF_TIP = 0x00000004
    CW_USEDEFAULT = -2147483648
    TRAY_CALLBACK = WM_APP + 1

    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    HCURSOR = wintypes.HANDLE
    HBRUSH = wintypes.HANDLE

    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.restype = LRESULT


    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]


    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT),
            ("hIcon", wintypes.HICON),
            ("szTip", wintypes.WCHAR * 128),
            ("dwState", wintypes.DWORD),
            ("dwStateMask", wintypes.DWORD),
            ("szInfo", wintypes.WCHAR * 256),
            ("uTimeoutOrVersion", wintypes.UINT),
            ("szInfoTitle", wintypes.WCHAR * 64),
            ("dwInfoFlags", wintypes.DWORD),
            ("guidItem", GUID),
            ("hBalloonIcon", wintypes.HICON),
        ]


    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", HCURSOR),
            ("hbrBackground", HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]


    class WindowsTrayIcon:
        def __init__(self, icon_path: Path, tooltip: str, on_activate) -> None:
            self.icon_path = Path(icon_path)
            self.tooltip = tooltip[:127]
            self.on_activate = on_activate
            self._thread: threading.Thread | None = None
            self._ready = threading.Event()
            self._visible = False
            self._hwnd: int | None = None
            self._hicon = None
            self._window_proc = None
            self._class_name = f"JournalCheckTrayWindow_{id(self)}"

        @property
        def visible(self) -> bool:
            return self._visible

        def show(self) -> bool:
            if self._visible or not self.icon_path.exists():
                return self._visible
            self._ready.clear()
            self._thread = threading.Thread(target=self._run, name="JournalCheckTrayIcon", daemon=True)
            self._thread.start()
            self._ready.wait(timeout=2.0)
            return self._visible

        def hide(self) -> None:
            hwnd = self._hwnd
            if hwnd:
                user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            if self._thread and self._thread.is_alive():
                self._thread.join(timeout=2.0)
            self._thread = None
            self._ready.clear()
            self._hwnd = None
            self._visible = False

        def _run(self) -> None:
            instance = kernel32.GetModuleHandleW(None)
            self._window_proc = WNDPROC(self._wnd_proc)
            window_class = WNDCLASSW()
            window_class.lpfnWndProc = self._window_proc
            window_class.hInstance = instance
            window_class.lpszClassName = self._class_name
            user32.RegisterClassW(ctypes.byref(window_class))

            hwnd = user32.CreateWindowExW(
                0,
                self._class_name,
                self.tooltip,
                0,
                CW_USEDEFAULT,
                CW_USEDEFAULT,
                0,
                0,
                None,
                None,
                instance,
                None,
            )
            self._hwnd = hwnd
            self._hicon = user32.LoadImageW(
                None,
                str(self.icon_path),
                IMAGE_ICON,
                0,
                0,
                LR_LOADFROMFILE | LR_DEFAULTSIZE,
            )
            self._visible = bool(hwnd) and bool(self._hicon) and self._add_icon(hwnd)
            self._ready.set()

            if not self._visible:
                if hwnd:
                    user32.DestroyWindow(hwnd)
                return

            message = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) != 0:
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))

            if hwnd:
                user32.UnregisterClassW(self._class_name, instance)
            self._hwnd = None
            self._visible = False

        def _add_icon(self, hwnd: int) -> bool:
            notify_data = NOTIFYICONDATAW()
            notify_data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            notify_data.hWnd = hwnd
            notify_data.uID = 1
            notify_data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            notify_data.uCallbackMessage = TRAY_CALLBACK
            notify_data.hIcon = self._hicon
            notify_data.szTip = self.tooltip
            return bool(shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(notify_data)))

        def _remove_icon(self, hwnd: int) -> None:
            notify_data = NOTIFYICONDATAW()
            notify_data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            notify_data.hWnd = hwnd
            notify_data.uID = 1
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(notify_data))

        def _wnd_proc(self, hwnd, msg, wparam, lparam):
            if msg == TRAY_CALLBACK and lparam in {
                WM_LBUTTONUP,
                WM_LBUTTONDBLCLK,
                WM_RBUTTONUP,
                WM_RBUTTONDBLCLK,
            }:
                try:
                    self.on_activate()
                except Exception:
                    pass
                return 0
            if msg == WM_CLOSE:
                self._remove_icon(hwnd)
                user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


else:
    class WindowsTrayIcon:
        def __init__(self, icon_path: Path, tooltip: str, on_activate) -> None:
            self.icon_path = Path(icon_path)
            self.tooltip = tooltip
            self.on_activate = on_activate
            self._visible = False

        @property
        def visible(self) -> bool:
            return False

        def show(self) -> bool:
            return False

        def hide(self) -> None:
            return None
