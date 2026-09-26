import ctypes
import unittest
from unittest.mock import patch

from client import idle


class _Kernel32GetTickCount64:
    def __init__(self, value):
        self._value = value
        self.restype = None

    def __call__(self):
        return self._value


class _Kernel32GetTickCountOnly:
    def __init__(self, value):
        self._value = value

    def __getattr__(self, name):
        if name == "GetTickCount64":
            raise AttributeError(name)
        raise AttributeError(name)

    def GetTickCount(self):
        return self._value


class _User32Stub:
    def __init__(self, dw_time, succeeds=True):
        self.dw_time = dw_time
        self.succeeds = succeeds

    def GetLastInputInfo(self, info_ptr):
        info_ptr._obj.dwTime = self.dw_time
        return int(self.succeeds)


class TestIdleSeconds(unittest.TestCase):
    def test_returns_raw_last_input_tick(self):
        windll = type("WinDll", (), {"user32": _User32Stub(12345)})()

        with patch("client.idle.platform.system", return_value="Windows"), patch.object(
            ctypes, "windll", windll, create=True
        ):
            self.assertEqual(idle.get_last_input_tick(), 12345)

    def test_last_input_tick_raises_when_windows_call_fails(self):
        windll = type("WinDll", (), {"user32": _User32Stub(12345, succeeds=False)})()

        with patch("client.idle.platform.system", return_value="Windows"), patch.object(
            ctypes, "windll", windll, create=True
        ):
            with self.assertRaisesRegex(OSError, "GetLastInputInfo failed"):
                idle.get_last_input_tick()

    def test_last_input_tick_is_zero_when_platform_is_unsupported(self):
        with patch("client.idle.platform.system", return_value="Linux"):
            self.assertEqual(idle.get_last_input_tick(), 0)

    def test_uses_gettickcount64_when_available(self):
        user32 = _User32Stub(0)
        kernel32 = type("Kernel32", (), {"GetTickCount64": _Kernel32GetTickCount64(5000)})()
        windll = type("WinDll", (), {"user32": user32, "kernel32": kernel32})()

        with patch("client.idle.platform.system", return_value="Windows"), patch.object(ctypes, "windll", windll, create=True):
            value = idle.get_idle_seconds()

        self.assertEqual(value, 5.0)

    def test_handles_gettickcount_wraparound(self):
        user32 = _User32Stub(0xFFFFFF00)
        kernel32 = _Kernel32GetTickCountOnly(0x00000100)
        windll = type("WinDll", (), {"user32": user32, "kernel32": kernel32})()

        with patch("client.idle.platform.system", return_value="Windows"), patch.object(ctypes, "windll", windll, create=True):
            value = idle.get_idle_seconds()

        self.assertAlmostEqual(value, 0.512, places=3)


if __name__ == "__main__":
    unittest.main()
