import json
import os
import shlex
import socket
import subprocess
import time
from datetime import datetime, timezone

import socketio

from idle import get_idle_seconds
from state_machine import ClientState

SERVER_URL = os.getenv("SERVER_URL", "http://baylan-portainer:5080")
SECRET = os.getenv("SHARED_SECRET", "change_me_super_secret")
DEFAULT_IDLE_TIMEOUT_SEC = int(os.getenv("DEFAULT_IDLE_TIMEOUT_SEC", "60"))
HEARTBEAT_INTERVAL_SEC = int(os.getenv("HEARTBEAT_INTERVAL_SEC", "10"))
ACTIVITY_RESUME_SEC = float(os.getenv("ACTIVITY_RESUME_SEC", "1.0"))
STATE_LOG_PATH = os.getenv("STATE_LOG_PATH", "client/state_transitions.jsonl")
ERP_WINDOW_TITLE = os.getenv("ERP_WINDOW_TITLE", "ERP")
PLAYER_COMMAND = os.getenv("PLAYER_COMMAND", "")

sio = socketio.Client()
hostname = socket.gethostname()

idle_timeout_sec = DEFAULT_IDLE_TIMEOUT_SEC
current_state = ClientState.ACTIVE
player_process = None


class _WindowManager:
    def __init__(self):
        import ctypes

        self._ctypes = ctypes
        self._user32 = ctypes.windll.user32

    def bring_to_front(self, window_title: str) -> bool:
        hwnd = self._user32.FindWindowW(None, window_title)
        if not hwnd:
            return False

        SW_RESTORE = 9
        self._user32.ShowWindow(hwnd, SW_RESTORE)
        self._user32.SetForegroundWindow(hwnd)
        return True


window_manager = _WindowManager()


def log_state_transition(from_state: ClientState, to_state: ClientState, reason: str):
    os.makedirs(os.path.dirname(STATE_LOG_PATH), exist_ok=True)
    line = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "hostname": hostname,
        "from": from_state.value,
        "to": to_state.value,
        "reason": reason,
    }
    with open(STATE_LOG_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, ensure_ascii=False) + "\n")


def set_state(next_state: ClientState, reason: str):
    global current_state

    if next_state == current_state:
        return

    prev = current_state
    current_state = next_state
    log_state_transition(prev, next_state, reason)
    print(f"🔁 STATE {prev.value} -> {next_state.value} | {reason}")


def start_player():
    global player_process

    if player_process and player_process.poll() is None:
        return

    if not PLAYER_COMMAND:
        print("ℹ️ PLAYER_COMMAND tanımlı değil, PLAYING durumu sadece state/log seviyesinde yürütülüyor.")
        return

    args = shlex.split(PLAYER_COMMAND)
    player_process = subprocess.Popen(args)
    print(f"▶️ Player started: {PLAYER_COMMAND}")


def stop_player():
    global player_process

    if not player_process:
        return

    if player_process.poll() is None:
        player_process.terminate()
        try:
            player_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            player_process.kill()

    player_process = None
    print("⏹️ Player stopped")


def return_to_erp_window():
    if window_manager.bring_to_front(ERP_WINDOW_TITLE):
        print(f"🪟 ERP window brought to front: {ERP_WINDOW_TITLE}")
    else:
        print(f"⚠️ ERP window not found: {ERP_WINDOW_TITLE}")


@sio.event
def connect():
    print("✅ Connected to server")

    sio.emit(
        "register",
        {
            "secret": SECRET,
            "hostname": hostname,
            "ip": socket.gethostbyname(hostname),
            "username": os.getenv("CLIENT_USERNAME", "test_user"),
            "department": os.getenv("CLIENT_DEPARTMENT", "URETIM"),
        },
    )


@sio.event
def disconnect():
    print("❌ Disconnected")


@sio.on("hello")
def on_hello(data):
    print("Server hello:", data)


@sio.on("config")
def on_config(data):
    global idle_timeout_sec

    print("📥 CONFIG RECEIVED:")
    print(data)

    config_timeout = data.get("idle_timeout_sec") if isinstance(data, dict) else None
    if isinstance(config_timeout, (int, float)) and config_timeout > 0:
        idle_timeout_sec = int(config_timeout)
    else:
        idle_timeout_sec = DEFAULT_IDLE_TIMEOUT_SEC

    print(f"🕒 idle_timeout_sec = {idle_timeout_sec}")


@sio.on("command")
def on_command(data):
    print("⚡ COMMAND RECEIVED:")
    print(data)


def run_state_cycle():
    idle_sec = get_idle_seconds()

    if current_state == ClientState.ACTIVE and idle_sec >= idle_timeout_sec:
        set_state(ClientState.IDLE_PENDING, f"idle={idle_sec:.1f}s threshold={idle_timeout_sec}s")

    if current_state == ClientState.IDLE_PENDING:
        start_player()
        set_state(ClientState.PLAYING, "player_started")

    if current_state == ClientState.PLAYING and idle_sec <= ACTIVITY_RESUME_SEC:
        set_state(ClientState.RETURNING, f"activity_detected idle={idle_sec:.1f}s")

    if current_state == ClientState.RETURNING:
        stop_player()
        return_to_erp_window()
        set_state(ClientState.ACTIVE, "returned_to_erp")

    return idle_sec


print("Connecting to:", SERVER_URL)

while True:
    try:
        sio.connect(SERVER_URL)
        break
    except Exception as e:
        print("Connection failed, retrying...", e)
        time.sleep(3)

while True:
    try:
        idle_sec = run_state_cycle()
        sio.emit(
            "heartbeat",
            {
                "hostname": hostname,
                "current_state": current_state.value,
                "idle_seconds": round(idle_sec, 1),
            },
        )
        print(f"💓 heartbeat sent | state={current_state.value} idle={idle_sec:.1f}s")
        time.sleep(HEARTBEAT_INTERVAL_SEC)
    except Exception as e:
        print("Heartbeat loop stopped:", e)
        break
