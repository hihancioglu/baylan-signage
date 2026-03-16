import hashlib
import json
import os
import random
import sys
import threading
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.request import urlopen


class MediaManager:
    def __init__(self, cache_root: str = "client/cache"):
        self.cache_root = Path(cache_root)
        self.versions_root = self.cache_root / "versions"
        self.media_store_root = self.cache_root / "media_store"
        self.state_file = self.cache_root / "state.json"
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.versions_root.mkdir(parents=True, exist_ok=True)
        self.media_store_root.mkdir(parents=True, exist_ok=True)
        self._state_lock = threading.RLock()
        self._download_jitter_max_sec = max(0.0, float(os.getenv("MEDIA_SYNC_JITTER_MAX_SEC", "20")))
        self._source_base_url = str(
            os.getenv("MEDIA_SOURCE_BASE_URL") or os.getenv("SERVER_URL") or ""
        ).strip()

    @staticmethod
    def _sha256_text(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _sha256_file(path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    def _load_state(self) -> dict:
        with self._state_lock:
            if not self.state_file.exists():
                return {}
            try:
                with open(self.state_file, "r", encoding="utf-8") as fh:
                    loaded = json.load(fh)
                    return loaded if isinstance(loaded, dict) else {}
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                return {}

    def _save_state(self, state: dict):
        with self._state_lock:
            safe_state = state if isinstance(state, dict) else {}
            self._write_json_atomic(self.state_file, safe_state)

    @staticmethod
    def _json_safe(value, seen: set[int] | None = None):
        if seen is None:
            seen = set()

        if value is None or isinstance(value, (str, int, float, bool)):
            return value

        obj_id = id(value)
        if obj_id in seen:
            return "<circular-reference>"

        if isinstance(value, dict):
            seen.add(obj_id)
            normalized = {}
            try:
                items = list(value.items())
            except Exception:
                items = []

            for key, item in items:
                safe_key = key if isinstance(key, str) else f"<{type(key).__name__}>"
                normalized[safe_key] = MediaManager._json_safe(item, seen)
            seen.remove(obj_id)
            return normalized

        if isinstance(value, (list, tuple, set)):
            seen.add(obj_id)
            try:
                iterable = list(value)
            except Exception:
                iterable = []
            normalized = [MediaManager._json_safe(item, seen) for item in iterable]
            seen.remove(obj_id)
            return normalized

        # Avoid calling user-defined __str__/__repr__ methods from background
        # threads (e.g. Tkinter objects), which can crash on Windows.
        return f"<{type(value).__name__}>"

    @staticmethod
    def _safe_print(message: str):
        try:
            print(message)
        except UnicodeEncodeError:
            try:
                encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
                fallback_message = message.encode(encoding, errors="replace").decode(
                    encoding, errors="replace"
                )
                print(fallback_message)
            except (OSError, ValueError):
                # Some Windows service/runtime contexts can have a closed/invalid stdout handle.
                pass
        except (OSError, ValueError):
            # Some Windows service/runtime contexts can have a closed/invalid stdout handle.
            pass

    @staticmethod
    def _write_json_atomic(path: Path, payload: dict):
        path.parent.mkdir(parents=True, exist_ok=True)
        safe_payload = MediaManager._json_safe(payload)

        for attempt in range(5):
            tmp_path = None
            try:
                tmp_path = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
                fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(safe_payload, fh, ensure_ascii=False, indent=2)
                    fh.flush()
                    os.fsync(fh.fileno())

                tmp_path.replace(path)
                return
            except (FileExistsError, PermissionError):
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
            finally:
                if tmp_path and tmp_path.exists():
                    tmp_path.unlink(missing_ok=True)

    @staticmethod
    def _is_url(path: str) -> bool:
        parsed = urlparse(path)
        return parsed.scheme in {"http", "https"}

    def _normalize_source(self, source: str) -> str:
        value = (source or "").strip()
        if value.lower().startswith(("http://", "https://")):
            return value.replace("\\", "/")
        if value.startswith("/") and self._source_base_url:
            return urljoin(f"{self._source_base_url.rstrip('/')}/", value.lstrip("/"))
        return value

    @staticmethod
    def _extract_source_from_item(raw_item) -> str:
        if not isinstance(raw_item, dict):
            return raw_item
        return (
            raw_item.get("path")
            or raw_item.get("source")
            or raw_item.get("source_url")
            or raw_item.get("url")
            or raw_item.get("local_path")
            or ""
        )

    @staticmethod
    def _path_exists_safely(path: Path) -> bool:
        # Avoid blocking startup on unreachable Windows network shares (UNC paths).
        # These can raise WinError 53 or hang for a long time when disconnected.
        if str(path).startswith("\\\\"):
            return False
        try:
            return path.exists()
        except OSError:
            return False

    @classmethod
    def _is_file_safely(cls, path: Path) -> bool:
        if not cls._path_exists_safely(path):
            return False
        try:
            return path.is_file()
        except OSError:
            return False

    def _download_url(self, source_url: str, target_path: Path):
        target_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = None
        try:
            # Create temp file in target directory to avoid cross-device rename issues
            # (e.g. WinError 17 when %TEMP% and cache directory are on different drives).
            with urlopen(source_url, timeout=30) as res, tempfile.NamedTemporaryFile(
                dir=target_path.parent, delete=False
            ) as tmp:
                shutil.copyfileobj(res, tmp)
                tmp_path = Path(tmp.name)

            tmp_path.replace(target_path)
        finally:
            if tmp_path and tmp_path.exists():
                tmp_path.unlink(missing_ok=True)

    def _maybe_stagger_download_start(self):
        if self._download_jitter_max_sec <= 0:
            return

        delay = random.uniform(0, self._download_jitter_max_sec)
        self._safe_print(
            f"⏳ medya indirme başlangıcı dengeleniyor | bekleme={delay:.1f}s max={self._download_jitter_max_sec:.1f}s"
        )
        time.sleep(delay)



    def _download_slideshow_manifest(self, source_url: str, target_manifest_path: Path, signature: str | None):
        self._download_url(source_url, target_manifest_path)

        with open(target_manifest_path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)

        slides = payload.get("slides") or []
        resolved_slides = []
        for idx, slide in enumerate(slides, start=1):
            image_source = str((slide or {}).get("image", "")).strip()
            if not image_source:
                continue

            image_url = urljoin(source_url, image_source)
            image_signature = self._sha256_text(f"{signature or source_url}:{idx}:{image_url}")
            local_image = self._url_cache_target(image_url, image_signature)
            if not local_image.exists():
                self._download_url(image_url, local_image)

            resolved_slides.append(
                {
                    "image": str(local_image),
                    "duration_sec": int((slide or {}).get("duration_sec", 8) or 8),
                }
            )

        if resolved_slides:
            payload["slides"] = resolved_slides
            with open(target_manifest_path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)

    def _url_cache_target(self, source_url: str, signature: str | None) -> Path:
        parsed = urlparse(source_url)
        ext = Path(parsed.path).suffix
        cache_key = signature or self._sha256_text(source_url)
        if not ext:
            ext = ".bin"
        safe_key = self._sha256_text(cache_key)[:20]
        return self.media_store_root / f"{safe_key}{ext}"

    @staticmethod
    def _normalize_item_type(raw_item: dict | None) -> str:
        item = raw_item or {}
        item_type = str(item.get("item_type") or item.get("media_type") or "").strip().lower()
        return item_type or "media"

    def sync_playlist(
        self,
        playlist_items: list[str],
        playlist_version: str,
        media_signatures: dict[str, str],
        progress_callback=None,
    ) -> list[str]:
        entries = self.sync_playlist_entries(
            playlist_items,
            playlist_version,
            media_signatures,
            progress_callback=progress_callback,
        )
        return [entry["local_path"] for entry in entries]

    def sync_playlist_entries(
        self,
        playlist_items: list,
        playlist_version: str,
        media_signatures: dict[str, str],
        progress_callback=None,
    ) -> list[dict]:
        version = str(playlist_version or "default")
        version_dir = self.versions_root / version
        manifest_path = version_dir / "manifest.json"
        local_entries: list[dict] = []
        manifest = []

        version_dir.mkdir(parents=True, exist_ok=True)

        downloadable_items = []
        for raw_item in playlist_items:
            source = self._extract_source_from_item(raw_item)
            normalized_source = self._normalize_source(source)
            item_type = self._normalize_item_type(raw_item if isinstance(raw_item, dict) else None)
            widget_requires_download = bool((raw_item or {}).get("widget_requires_download")) if isinstance(raw_item, dict) else False
            signature = media_signatures.get(source) or media_signatures.get(normalized_source)
            should_download = self._is_url(normalized_source) and (
                item_type != "widget" or widget_requires_download
            )
            if should_download:
                target = self._url_cache_target(normalized_source, signature)
                if not target.exists():
                    downloadable_items.append(normalized_source)

        downloaded_count = 0
        total_download_count = len(downloadable_items)

        def report_progress(state: str, source: str = ""):
            if not progress_callback:
                return
            percent = int((downloaded_count / total_download_count) * 100) if total_download_count else 100
            progress_callback(
                {
                    "state": state,
                    "source": source,
                    "downloaded": downloaded_count,
                    "total": total_download_count,
                    "percent": max(0, min(percent, 100)),
                }
            )

        report_progress("start")
        if total_download_count:
            self._maybe_stagger_download_start()

        for raw_item in playlist_items:
            source = self._extract_source_from_item(raw_item)
            duration_sec = raw_item.get("duration_sec") if isinstance(raw_item, dict) else None
            media_type = raw_item.get("media_type") if isinstance(raw_item, dict) else None
            item_type = self._normalize_item_type(raw_item if isinstance(raw_item, dict) else None)
            widget_requires_download = bool((raw_item or {}).get("widget_requires_download")) if isinstance(raw_item, dict) else False
            widget_payload = (raw_item or {}).get("widget_payload") if isinstance(raw_item, dict) else None
            widget_url = (raw_item or {}).get("widget_url") if isinstance(raw_item, dict) else None
            columns = (raw_item or {}).get("columns") if isinstance(raw_item, dict) else None
            display_name = (
                raw_item.get("title") or raw_item.get("name") or raw_item.get("display_name")
            ) if isinstance(raw_item, dict) else None
            normalized_source = self._normalize_source(source)
            signature = media_signatures.get(source) or media_signatures.get(normalized_source)
            should_download = self._is_url(normalized_source) and (
                item_type != "widget" or widget_requires_download
            )

            try:
                if item_type == "widget" and not normalized_source:
                    local_entry = {
                        "source": normalized_source,
                        "local_path": "",
                        "duration_sec": duration_sec,
                        "media_type": media_type,
                        "item_type": item_type,
                        "display_name": display_name,
                        "widget_requires_download": widget_requires_download,
                        "widget_payload": widget_payload,
                        "widget_url": widget_url,
                        "columns": columns,
                    }
                    local_entries.append(local_entry)
                    manifest.append(
                        {
                            "source": normalized_source,
                            "local_path": "",
                            "checksum": None,
                            "signature": signature,
                            "duration_sec": duration_sec,
                            "media_type": media_type,
                            "item_type": item_type,
                            "display_name": display_name,
                            "widget_requires_download": widget_requires_download,
                            "widget_payload": widget_payload,
                            "widget_url": widget_url,
                            "columns": columns,
                        }
                    )
                elif should_download:
                    target = self._url_cache_target(normalized_source, signature)
                    if not target.exists():
                        report_progress("downloading", normalized_source)
                        if Path(target).suffix.lower() == ".json":
                            self._download_slideshow_manifest(normalized_source, target, signature)
                        else:
                            self._download_url(normalized_source, target)
                        downloaded_count += 1
                        report_progress("downloaded", normalized_source)
                    checksum = self._sha256_file(target)
                    local_entry = {
                        "source": normalized_source,
                        "local_path": str(target),
                        "duration_sec": duration_sec,
                        "media_type": media_type,
                        "item_type": item_type,
                        "display_name": display_name,
                        "widget_requires_download": widget_requires_download,
                        "widget_payload": widget_payload,
                        "widget_url": widget_url,
                        "columns": columns,
                    }
                    local_entries.append(local_entry)
                    manifest.append(
                        {
                            "source": normalized_source,
                            "local_path": str(target),
                            "checksum": checksum,
                            "signature_token": signature,
                            "duration_sec": duration_sec,
                            "media_type": media_type,
                            "item_type": item_type,
                            "display_name": display_name,
                            "widget_requires_download": widget_requires_download,
                            "widget_payload": widget_payload,
                            "widget_url": widget_url,
                            "columns": columns,
                        }
                    )
                elif self._is_url(normalized_source):
                    local_entry = {
                        "source": normalized_source,
                        "local_path": normalized_source,
                        "duration_sec": duration_sec,
                        "media_type": media_type,
                        "item_type": item_type,
                        "display_name": display_name,
                        "widget_requires_download": widget_requires_download,
                        "widget_payload": widget_payload,
                        "widget_url": widget_url,
                        "columns": columns,
                    }
                    local_entries.append(local_entry)
                    manifest.append(
                        {
                            "source": normalized_source,
                            "local_path": normalized_source,
                            "checksum": None,
                            "signature": signature,
                            "duration_sec": duration_sec,
                            "media_type": media_type,
                            "item_type": item_type,
                            "display_name": display_name,
                            "widget_requires_download": widget_requires_download,
                            "widget_payload": widget_payload,
                            "widget_url": widget_url,
                            "columns": columns,
                        }
                    )
                else:
                    source_path = Path(normalized_source)
                    if not self._is_file_safely(source_path):
                        continue
                    checksum = self._sha256_file(source_path)
                    if signature and checksum != signature:
                        continue
                    local_entry = {
                        "source": normalized_source,
                        "local_path": str(source_path),
                        "duration_sec": duration_sec,
                        "media_type": media_type,
                        "item_type": item_type,
                        "display_name": display_name,
                        "widget_requires_download": widget_requires_download,
                        "widget_payload": widget_payload,
                        "widget_url": widget_url,
                        "columns": columns,
                    }
                    local_entries.append(local_entry)
                    manifest.append(
                        {
                            "source": normalized_source,
                            "local_path": str(source_path),
                            "checksum": checksum,
                            "signature": signature,
                            "duration_sec": duration_sec,
                            "media_type": media_type,
                            "item_type": item_type,
                            "display_name": display_name,
                            "widget_requires_download": widget_requires_download,
                            "widget_payload": widget_payload,
                            "widget_url": widget_url,
                            "columns": columns,
                        }
                    )
            except Exception as exc:
                self._safe_print(f"⚠️ medya senkronizasyonu başarısız: {normalized_source} | {exc}")
                continue

        report_progress("done")

        if local_entries:
            try:
                self._write_json_atomic(manifest_path, {"items": manifest})

                with self._state_lock:
                    state = self._load_state()
                    state["current_version"] = version
                    state["last_successful_playlist"] = [entry["local_path"] for entry in local_entries]
                    state["last_successful_playlist_entries"] = local_entries
                    self._save_state(state)
            except Exception as exc:
                self._safe_print(f"⚠️ manifest/state yazımı başarısız: {manifest_path} | {exc}")

        return local_entries

    def load_last_successful_playlist(self) -> list[str]:
        state = self._load_state()
        items = state.get("last_successful_playlist") or []
        existing = [item for item in items if self._path_exists_safely(Path(item))]
        return existing

    def load_last_successful_playlist_entries(self) -> list[dict]:
        state = self._load_state()
        entries = state.get("last_successful_playlist_entries") or []
        existing_entries = []
        for entry in entries:
            local_path = str((entry or {}).get("local_path") or "")
            if local_path and self._path_exists_safely(Path(local_path)):
                existing_entries.append(
                    {
                        "local_path": local_path,
                        "duration_sec": (entry or {}).get("duration_sec"),
                        "media_type": (entry or {}).get("media_type"),
                        "item_type": (entry or {}).get("item_type"),
                        "display_name": (entry or {}).get("display_name"),
                        "widget_requires_download": bool((entry or {}).get("widget_requires_download")),
                        "widget_payload": (entry or {}).get("widget_payload"),
                        "widget_url": (entry or {}).get("widget_url"),
                        "columns": (entry or {}).get("columns"),
                    }
                )
            elif str((entry or {}).get("item_type") or "").strip().lower() == "widget":
                existing_entries.append(
                    {
                        "local_path": local_path,
                        "duration_sec": (entry or {}).get("duration_sec"),
                        "media_type": (entry or {}).get("media_type"),
                        "item_type": "widget",
                        "display_name": (entry or {}).get("display_name"),
                        "widget_requires_download": bool((entry or {}).get("widget_requires_download")),
                        "widget_payload": (entry or {}).get("widget_payload"),
                        "widget_url": (entry or {}).get("widget_url"),
                        "columns": (entry or {}).get("columns"),
                    }
                )
        return existing_entries

    def load_playback_state(self) -> dict:
        state = self._load_state()
        return state.get("playback_state") or {}

    def save_playback_state(self, playback_state: dict):
        with self._state_lock:
            state = self._load_state()
            state["playback_state"] = playback_state if isinstance(playback_state, dict) else {}
            self._save_state(state)
