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


class TestMediaManagerDownloadJitter(unittest.TestCase):
    def test_sync_playlist_entries_staggers_download_start_when_needed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = MediaManager(cache_root=tmpdir)
            manager._download_jitter_max_sec = 5
            source = "http://example.com/video.mp4"

            class DummyResponse(BytesIO):
                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    self.close()

            with patch("client.media_manager.random.uniform", return_value=1.25) as uniform_mock:
                with patch("client.media_manager.time.sleep") as sleep_mock:
                    with patch("client.media_manager.urlopen", return_value=DummyResponse(b"abc")):
                        entries = manager.sync_playlist_entries(
                            [{"path": source, "media_type": "video", "duration_sec": None}],
                            "v-jitter",
                            {source: "sig-jitter"},
                        )

            self.assertEqual(len(entries), 1)
            uniform_mock.assert_called_once_with(0, 5)
            sleep_mock.assert_called_once_with(1.25)

    def test_sync_playlist_entries_skips_stagger_when_no_download_needed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = MediaManager(cache_root=tmpdir)
            source = "http://example.com/video.mp4"
            signature = "sig-existing"
            target = manager._url_cache_target(source, signature)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"existing")

            with patch("client.media_manager.time.sleep") as sleep_mock:
                entries = manager.sync_playlist_entries(
                    [{"path": source, "media_type": "video", "duration_sec": None}],
                    "v-jitter-skip",
                    {source: signature},
                )

            self.assertEqual(len(entries), 1)
            sleep_mock.assert_not_called()



    def test_sync_playlist_entries_keeps_html_widget_without_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = MediaManager(cache_root=tmpdir)
            widget_payload = {"name": "Duyuru", "type": "html", "content": "<h1>Merhaba</h1>"}

            entries = manager.sync_playlist_entries(
                [
                    {
                        "path": None,
                        "item_type": "widget",
                        "media_type": "widget",
                        "duration_sec": 10,
                        "widget_payload": widget_payload,
                        "widget_url": None,
                    }
                ],
                "v-html-widget",
                {},
            )

            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["item_type"], "widget")
            self.assertEqual(entries[0]["local_path"], "")
            self.assertEqual(entries[0]["widget_payload"], widget_payload)
    def test_load_last_successful_playlist_entries_restores_widget_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = MediaManager(cache_root=tmpdir)
            local_media = Path(tmpdir) / "media_store" / "local.jpg"
            local_media.parent.mkdir(parents=True, exist_ok=True)
            local_media.write_bytes(b"x")

            manager._save_state(
                {
                    "last_successful_playlist_entries": [
                        {
                            "local_path": str(local_media),
                            "duration_sec": 10,
                            "media_type": "image",
                            "item_type": "media",
                            "display_name": "Local",
                            "widget_payload": [{"type": "iframe", "url": "https://example.com"}],
                            "widget_url": "https://example.com",
                            "columns": [{"width": 12}],
                        },
                        {
                            "local_path": "https://widget.example.com",
                            "duration_sec": 20,
                            "media_type": "widget",
                            "item_type": "widget",
                            "display_name": "Widget",
                            "widget_payload": [{"type": "iframe", "url": "https://widget.example.com"}],
                            "widget_url": "https://widget.example.com",
                            "columns": [{"width": 12}],
                        },
                    ]
                }
            )

            entries = manager.load_last_successful_playlist_entries()

            self.assertEqual(len(entries), 2)
            self.assertEqual(entries[0]["widget_url"], "https://example.com")
            self.assertEqual(entries[1]["item_type"], "widget")
            self.assertEqual(entries[1]["columns"], [{"width": 12}])


class TestMediaManagerSourceResolution(unittest.TestCase):
    def test_sync_playlist_entries_resolves_relative_media_url_with_server_base(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"SERVER_URL": "http://panel.local:5080"}, clear=False):
                manager = MediaManager(cache_root=tmpdir)

            opened_urls = []

            class DummyResponse(BytesIO):
                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    self.close()

            def fake_urlopen(url, timeout=30):
                opened_urls.append(url)
                return DummyResponse(b"video-bytes")

            with patch("client.media_manager.urlopen", side_effect=fake_urlopen):
                entries = manager.sync_playlist_entries(
                    [{"path": "/media/sample.mp4", "media_type": "video", "duration_sec": None}],
                    "v-rel-path",
                    {},
                )

            self.assertEqual(len(entries), 1)
            self.assertEqual(opened_urls, ["http://panel.local:5080/media/sample.mp4"])
            self.assertTrue(Path(entries[0]["local_path"]).exists())

    def test_sync_playlist_entries_uses_source_url_when_path_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = MediaManager(cache_root=tmpdir)
            opened_urls = []

            class DummyResponse(BytesIO):
                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    self.close()

            def fake_urlopen(url, timeout=30):
                opened_urls.append(url)
                return DummyResponse(b"video-bytes")

            with patch("client.media_manager.urlopen", side_effect=fake_urlopen):
                entries = manager.sync_playlist_entries(
                    [{
                        "path": "",
                        "source_url": "https://cdn.example.com/playlist-item.mp4",
                        "media_type": "video",
                        "duration_sec": None,
                    }],
                    "v-source-url",
                    {},
                )

            self.assertEqual(len(entries), 1)
            self.assertEqual(opened_urls, ["https://cdn.example.com/playlist-item.mp4"])
            self.assertTrue(Path(entries[0]["local_path"]).exists())

    def test_sync_playlist_entries_localizes_dashboard_iframe_media_urls(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"SERVER_URL": "http://panel.local:5080"}, clear=False):
                manager = MediaManager(cache_root=tmpdir)
            opened_urls = []

            class DummyResponse(BytesIO):
                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    self.close()

            def fake_urlopen(url, timeout=30):
                opened_urls.append(url)
                return DummyResponse(b"video-bytes")

            with patch("client.media_manager.urlopen", side_effect=fake_urlopen):
                entries = manager.sync_playlist_entries(
                    [{
                        "item_type": "widget",
                        "media_type": "widget",
                        "duration_sec": 15,
                        "widget_payload": {
                            "widgets": [
                                {"type": "iframe", "url": "/media/dashboard-loop.mp4"},
                                {"type": "iframe", "url": "https://example.com"},
                            ]
                        },
                    }],
                    "v-dashboard-iframe-media",
                    {},
                )

            self.assertEqual(len(entries), 1)
            self.assertEqual(opened_urls, ["http://panel.local:5080/media/dashboard-loop.mp4"])
            payload = entries[0]["widget_payload"]
            self.assertIsInstance(payload, dict)
            first_widget_url = payload["widgets"][0]["url"]
            self.assertTrue(Path(first_widget_url).exists())
            self.assertEqual(payload["widgets"][1]["url"], "https://example.com")

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


    def test_write_json_atomic_handles_circular_references(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "state.json"
            payload = {"items": []}
            payload["self"] = payload

            MediaManager._write_json_atomic(target, payload)

            written = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(written["self"], "<circular-reference>")

    def test_safe_print_swallows_os_error(self):
        with patch("builtins.print", side_effect=OSError(6, "Invalid handle")):
            MediaManager._safe_print("hello")

class TestMediaManagerWidgetSources(unittest.TestCase):
    def test_extract_widget_media_sources_supports_path_field(self):
        payload = {
            "widgets": [
                {"type": "video", "path": "https://cdn.example.com/from-path.mp4"},
                {"type": "image", "source_url": "https://cdn.example.com/from-source-url.jpg"},
            ]
        }

        sources = MediaManager._extract_widget_media_sources(payload)

        self.assertEqual(
            sources,
            [
                "https://cdn.example.com/from-path.mp4",
                "https://cdn.example.com/from-source-url.jpg",
            ],
        )


if __name__ == "__main__":
    unittest.main()
