# app/config.py
import os
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///data/signage.db")
SHARED_SECRET = os.getenv("SHARED_SECRET", "change_me")
