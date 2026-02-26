from enum import Enum


class ClientState(str, Enum):
    ACTIVE = "ACTIVE"
    IDLE_PENDING = "IDLE_PENDING"
    PLAYING = "PLAYING"
    RETURNING = "RETURNING"
    EMERGENCY = "EMERGENCY"

