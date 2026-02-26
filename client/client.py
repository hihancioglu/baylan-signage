import socketio
import socket
import time

SERVER_URL = "http://baylan-portainer:5080"   # kendi portunu yaz
SECRET = "change_me_super_secret"

sio = socketio.Client()
hostname = socket.gethostname()

@sio.event
def connect():
    print("✅ Connected to server")

    sio.emit("register", {
        "secret": SECRET,
        "hostname": hostname,
        "ip": socket.gethostbyname(hostname),
        "username": "test_user",
        "department": "URETIM"
    })

@sio.event
def disconnect():
    print("❌ Disconnected")

@sio.on("hello")
def on_hello(data):
    print("Server hello:", data)

@sio.on("config")
def on_config(data):
    print("📥 CONFIG RECEIVED:")
    print(data)

@sio.on("command")
def on_command(data):
    print("⚡ COMMAND RECEIVED:")
    print(data)

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
        sio.emit("heartbeat", {"hostname": hostname})
        print("💓 heartbeat sent")
        time.sleep(10)
    except:
        break
