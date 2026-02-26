import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlparse
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
        if not self.state_file.exists():
            return {}
        with open(self.state_file, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def _save_state(self, state: dict):
        with open(self.state_file, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)

    @staticmethod
    def _is_url(path: str) -> bool:
        parsed = urlparse(path)
        return parsed.scheme in {"http", "https"}

    def _download_url(self, source_url: str, target_path: Path):
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with urlopen(source_url, timeout=30) as res, tempfile.NamedTemporaryFile(delete=False) as tmp:
            shutil.copyfileobj(res, tmp)
            tmp_path = Path(tmp.name)

        tmp_path.replace(target_path)

    def _url_cache_target(self, source_url: str, signature: str | None) -> Path:
        parsed = urlparse(source_url)
        ext = Path(parsed.path).suffix
        cache_key = signature or self._sha256_text(source_url)
        if not ext:
            ext = ".bin"
        safe_key = self._sha256_text(cache_key)[:20]
        return self.media_store_root / f"{safe_key}{ext}"

    def sync_playlist(
        self,
        playlist_items: list[str],
        playlist_version: str,
        media_signatures: dict[str, str],
        progress_callback=None,
    ) -> list[str]:
        version = str(playlist_version or "default")
        version_dir = self.versions_root / version
        manifest_path = version_dir / "manifest.json"
        local_items: list[str] = []
        manifest = []

        version_dir.mkdir(parents=True, exist_ok=True)

        downloadable_items = []
        for source in playlist_items:
            signature = media_signatures.get(source)
            if self._is_url(source):
                target = self._url_cache_target(source, signature)
                if not target.exists():
                    downloadable_items.append(source)

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

        for source in playlist_items:
            signature = media_signatures.get(source)

            try:
                if self._is_url(source):
                    target = self._url_cache_target(source, signature)
                    if not target.exists():
                        report_progress("downloading", source)
                        self._download_url(source, target)
                        downloaded_count += 1
                        report_progress("downloaded", source)
                    checksum = self._sha256_file(target)
                    local_items.append(str(target))
                    manifest.append(
                        {
                            "source": source,
                            "local_path": str(target),
                            "checksum": checksum,
                            "signature_token": signature,
                        }
                    )
                else:
                    source_path = Path(source)
                    if not source_path.exists() or not source_path.is_file():
                        continue
                    checksum = self._sha256_file(source_path)
                    if signature and checksum != signature:
                        continue
                    local_items.append(str(source_path))
                    manifest.append(
                        {
                            "source": source,
                            "local_path": str(source_path),
                            "checksum": checksum,
                            "signature": signature,
                        }
                    )
            except Exception:
                continue

        report_progress("done")

        if local_items:
            with open(manifest_path, "w", encoding="utf-8") as fh:
                json.dump({"items": manifest}, fh, ensure_ascii=False, indent=2)

            state = self._load_state()
            state["current_version"] = version
            state["last_successful_playlist"] = local_items
            self._save_state(state)

        return local_items

    def load_last_successful_playlist(self) -> list[str]:
        state = self._load_state()
        items = state.get("last_successful_playlist") or []
        existing = [item for item in items if Path(item).exists()]
        return existing
