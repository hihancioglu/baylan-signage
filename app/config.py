# app/config.py
import os
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///data/signage.db")
SHARED_SECRET = os.getenv("SHARED_SECRET", "change_me")
PANEL_SESSION_SECRET = os.getenv("PANEL_SESSION_SECRET", "panel_session_change_me")
AD_SERVER_URI = os.getenv("AD_SERVER_URI", "")
AD_DOMAIN = os.getenv("AD_DOMAIN", "")
AD_USER_DN_TEMPLATE = os.getenv("AD_USER_DN_TEMPLATE", "")
AD_USE_SSL = os.getenv("AD_USE_SSL", "false").lower() in {"1", "true", "yes", "on"}
AD_BASE_DN = os.getenv("AD_BASE_DN", "")
AD_BIND_DN = os.getenv("AD_BIND_DN", "")
AD_BIND_PASSWORD = os.getenv("AD_BIND_PASSWORD", "")
AD_USER_SEARCH_FILTER = os.getenv("AD_USER_SEARCH_FILTER", "(&(objectClass=user)(sAMAccountName={username}))")
AD_CONNECT_TIMEOUT = float(os.getenv("AD_CONNECT_TIMEOUT", "5"))
