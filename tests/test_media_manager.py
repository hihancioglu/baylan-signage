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


if __name__ == "__main__":
    unittest.main()
