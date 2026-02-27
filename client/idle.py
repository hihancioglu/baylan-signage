import ctypes
import platform
from ctypes import wintypes


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("dwTime", wintypes.DWORD),
    ]


def get_idle_seconds() -> float:
    """Return elapsed seconds since the last keyboard/mouse/touch interaction."""
    if not platform.system().lower().startswith("win"):
        return 0.0

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    last_input_info = LASTINPUTINFO()
    last_input_info.cbSize = ctypes.sizeof(LASTINPUTINFO)

    if not user32.GetLastInputInfo(ctypes.byref(last_input_info)):
        raise OSError("GetLastInputInfo failed")

    tick_count = kernel32.GetTickCount()
    elapsed_ms = tick_count - last_input_info.dwTime
    return max(elapsed_ms / 1000.0, 0.0)
