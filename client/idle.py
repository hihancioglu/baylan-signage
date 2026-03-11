import ctypes
import platform
from ctypes import wintypes


UINT32_MODULO = 2**32


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

    try:
        get_tick_count64 = kernel32.GetTickCount64
        if hasattr(get_tick_count64, "restype"):
            get_tick_count64.restype = ctypes.c_ulonglong
        tick_count = int(get_tick_count64())
        elapsed_ms = tick_count - int(last_input_info.dwTime)
    except AttributeError:
        # GetTickCount 32-bit olarak yaklaşık 49.7 günde bir taşar.
        # Taşma durumunda modüler farkı alarak doğru süreyi koruyoruz.
        get_tick_count = kernel32.GetTickCount
        if hasattr(get_tick_count, "restype"):
            get_tick_count.restype = wintypes.DWORD
        tick_count = int(get_tick_count())
        elapsed_ms = (tick_count - int(last_input_info.dwTime)) % UINT32_MODULO

    return max(elapsed_ms / 1000.0, 0.0)
