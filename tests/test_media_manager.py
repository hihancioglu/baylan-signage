import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from client.media_manager import MediaManager


class TestMediaManagerDownload(unittest.TestCase):
    def test_download_url_writes_file_and_cleans_temp_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = MediaManager(cache_root=tmpdir)
            target = Path(tmpdir) / "media_store" / "video.mp4"

            class DummyResponse(BytesIO):
                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    self.close()

            with patch("client.media_manager.urlopen", return_value=DummyResponse(b"abc123")):
                manager._download_url("http://example.com/video.mp4", target)

            self.assertTrue(target.exists())
            self.assertEqual(target.read_bytes(), b"abc123")
            leftovers = [p for p in target.parent.iterdir() if p.name != target.name]
            self.assertEqual(leftovers, [])

    def test_sync_playlist_downloads_manifest_and_slide_images(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = MediaManager(cache_root=tmpdir)
            source_manifest = "http://example.com/slides/deck.json"
            manifest_payload = {
                "type": "ppt_slideshow",
                "slides": [
                    {"image": "slide_001.png", "duration_sec": 3},
                    {"image": "slide_002.png", "duration_sec": 4},
                ],
            }

            class DummyResponse(BytesIO):
                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    self.close()

            def fake_urlopen(url, timeout=30):
                if url.endswith("deck.json"):
                    return DummyResponse(json.dumps(manifest_payload).encode("utf-8"))
                if url.endswith("slide_001.png"):
                    return DummyResponse(b"png-1")
                if url.endswith("slide_002.png"):
                    return DummyResponse(b"png-2")
                raise AssertionError(f"unexpected url: {url}")

            with patch("client.media_manager.urlopen", side_effect=fake_urlopen):
                items = manager.sync_playlist([source_manifest], "v1", {source_manifest: "sig-1"})

            self.assertEqual(len(items), 1)
            manifest_path = Path(items[0])
            self.assertTrue(manifest_path.exists())

            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(len(payload["slides"]), 2)
            for slide in payload["slides"]:
                self.assertTrue(Path(slide["image"]).exists())


class TestMediaManagerManifestWrite(unittest.TestCase):
    def test_sync_playlist_entries_handles_manifest_write_permission_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = MediaManager(cache_root=tmpdir)
            source_manifest = "http://example.com/slides/deck.json"

            class DummyResponse(BytesIO):
                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    self.close()

            with patch("client.media_manager.urlopen", return_value=DummyResponse(b"{}")):
                with patch.object(MediaManager, "_write_json_atomic", side_effect=PermissionError(13, "Permission denied")):
                    items = manager.sync_playlist_entries(
                        [{"path": source_manifest, "media_type": "image", "duration_sec": None}],
                        "v-perm",
                        {source_manifest: "sig-1"},
                    )

            self.assertEqual(len(items), 1)

    def test_write_json_atomic_retries_after_permission_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "manifest.json"
            calls = {"count": 0}

            original_replace = Path.replace

            def flaky_replace(path_obj, dst):
                calls["count"] += 1
                if calls["count"] == 1:
                    raise PermissionError(13, "Permission denied")
                return original_replace(path_obj, dst)

            with patch.object(Path, "replace", new=flaky_replace):
                MediaManager._write_json_atomic(target, {"ok": True})

            self.assertGreaterEqual(calls["count"], 2)
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"ok": True})

    def test_safe_print_swallows_os_error(self):
        with patch("builtins.print", side_effect=OSError(6, "Invalid handle")):
            MediaManager._safe_print("hello")


if __name__ == "__main__":
    unittest.main()
