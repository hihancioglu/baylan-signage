# app/config.py
import os
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///data/signage.db")
SHARED_SECRET = os.getenv("SHARED_SECRET", "change_me")
PANEL_SESSION_SECRET = os.getenv("PANEL_SESSION_SECRET", "panel_session_change_me")
AD_SERVER_URI = os.getenv("AD_SERVER_URI", "")
AD_DOMAIN = os.getenv("AD_DOMAIN", "")
AD_USER_DN_TEMPLATE = os.getenv("AD_USER_DN_TEMPLATE", "")
AD_USE_SSL = os.getenv("AD_USE_SSL", "false").lower() in {"1", "true", "yes", "on"}
