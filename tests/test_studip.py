from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

import requests

from studip_sync.errors import ApiError, SyncIncompleteError
from studip_sync.studip import StudipSync


class FakeResponse:
    def __init__(
        self,
        url: str,
        *,
        status_code: int = 200,
        json_data: Any = None,
        chunks: tuple[bytes, ...] = (),
        stream_error: requests.RequestException | None = None,
    ) -> None:
        self.url = url
        self.status_code = status_code
        self.text = "server error" if status_code >= 400 else ""
        self.content = b"".join(chunks)
        self._json_data = json_data
        self._chunks = chunks
        self._stream_error = stream_error
        self.closed = False

    def json(self) -> Any:
        return self._json_data

    def iter_content(self, chunk_size: int) -> Any:
        del chunk_size
        yield from self._chunks
        if self._stream_error:
            raise self._stream_error

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class FakeSession:
    def __init__(self, responses: dict[str, FakeResponse]) -> None:
        self.responses = responses
        self.requests: list[str] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        del kwargs
        self.requests.append(url)
        return self.responses[url]


class StudipSyncTests(unittest.TestCase):
    def make_client(
        self, data_path: Path, responses: dict[str, FakeResponse]
    ) -> StudipSync:
        client = StudipSync(
            {"data_path": str(data_path)},
            session=FakeSession(responses),  # type: ignore[arg-type]
        )
        client.cookie = "test-cookie"
        return client

    def test_http_error_includes_status_and_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            url = "https://studip.uni-passau.de/studip/api.php/file/bad/download"
            client = self.make_client(
                Path(directory),
                {url: FakeResponse(url, status_code=500)},
            )

            with self.assertRaises(ApiError) as raised:
                client.get_req("/file/bad/download")

            self.assertEqual(raised.exception.status_code, 500)
            self.assertIn(url, str(raised.exception))

    def test_failed_stream_does_not_leave_partial_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            url = "https://studip.uni-passau.de/studip/api.php/file/bad/download"
            client = self.make_client(
                root,
                {
                    url: FakeResponse(
                        url,
                        chunks=(b"partial",),
                        stream_error=requests.ConnectionError("connection lost"),
                    )
                },
            )
            destination = root / "archive" / "course" / "document.pdf"

            with self.assertRaisesRegex(Exception, "Download stream failed"):
                client._write_atomic_download("bad", destination)

            self.assertFalse(destination.exists())
            self.assertEqual(list(destination.parent.glob("*.part")), [])

    def test_sync_continues_after_one_download_returns_500(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = "https://studip.uni-passau.de/studip/api.php"
            responses = {
                f"{base}/file/bad": FakeResponse(
                    f"{base}/file/bad",
                    json_data={"is_downloadable": True, "size": 10},
                ),
                f"{base}/file/bad/download": FakeResponse(
                    f"{base}/file/bad/download", status_code=500
                ),
                f"{base}/file/good": FakeResponse(
                    f"{base}/file/good",
                    json_data={"is_downloadable": True, "size": 4},
                ),
                f"{base}/file/good/download": FakeResponse(
                    f"{base}/file/good/download", chunks=(b"good",)
                ),
            }
            client = self.make_client(root, responses)
            client.get_courses = lambda semester_id=None: [  # type: ignore[method-assign]
                {
                    "title": "Course",
                    "top_folder": {
                        "files": [
                            {"name": "bad.pdf", "id": "bad"},
                            {"name": "good.pdf", "id": "good"},
                        ],
                        "subfolders": [],
                    },
                }
            ]

            with self.assertRaises(SyncIncompleteError) as raised:
                client.sync()

            self.assertEqual(len(raised.exception.failures), 1)
            self.assertFalse((root / "archive" / "Course" / "bad.pdf").exists())
            self.assertEqual(
                (root / "archive" / "Course" / "good.pdf").read_bytes(), b"good"
            )

    def test_old_empty_partial_file_is_repaired(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = "https://studip.uni-passau.de/studip/api.php/file/file-id"
            client = self.make_client(
                root,
                {
                    base: FakeResponse(
                        base,
                        json_data={"is_downloadable": True, "size": 8},
                    ),
                    f"{base}/download": FakeResponse(
                        f"{base}/download", chunks=(b"repaired",)
                    ),
                },
            )
            destination = root / "archive" / "Course" / "document.pdf"
            destination.parent.mkdir(parents=True)
            destination.touch()

            changed = client._sync_file(("Course", "document.pdf"), "file-id")

            self.assertTrue(changed)
            self.assertEqual(destination.read_bytes(), b"repaired")

    def test_remote_names_cannot_escape_archive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = self.make_client(Path(directory), {})
            folder = {
                "files": [{"name": "../../secret", "id": "file-id"}],
                "subfolders": [],
            }

            files = client.get_files(folder, "../Course")

            self.assertEqual(files, {"_/Course/.._.._secret": "file-id"})

    def test_subfolder_path_uses_subfolder_name_not_root_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = self.make_client(Path(directory), {})
            folder = {
                "name": "Root folder",
                "files": [],
                "subfolders": [
                    {
                        "name": "Week 1",
                        "files": [{"name": "slides.pdf", "id": "slides"}],
                        "subfolders": [],
                    }
                ],
            }

            files = client.get_files(folder, "Course")

            self.assertEqual(files, {"Course/Week 1/slides.pdf": "slides"})


if __name__ == "__main__":
    unittest.main()
