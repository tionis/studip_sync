"""Stud.IP API client and local synchronization logic."""

from __future__ import annotations

import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .errors import ApiError, StudipSyncError, SyncIncompleteError


def eprint(*args: object, **kwargs: Any) -> None:
    print(*args, file=sys.stderr, **kwargs)


class StudipSync:
    """Synchronize documents from a Stud.IP instance to a local archive."""

    cookie_db_migrations = (
        """CREATE TABLE cookies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            value TEXT NOT NULL,
            host TEXT NOT NULL,
            path TEXT NOT NULL,
            secure INTEGER NOT NULL,
            httponly INTEGER NOT NULL,
            expires INTEGER
        );""",
        """DELETE FROM cookies
           WHERE id NOT IN (
               SELECT MAX(id) FROM cookies GROUP BY name, host, path
           );
           CREATE UNIQUE INDEX cookies_identity
           ON cookies(name, host, path);""",
    )

    _config_defaults: dict[str, Any] = {
        "data_path": ".",
        "studip_host": "studip.uni-passau.de",
        "prefix": "/studip/api.php",
        "auth_method": "cookie",
        "use_git": False,
        "git_commit_message_prefix": "studip-sync: ",
        "browser": "selenium",
        "request_timeout": 30.0,
    }

    def __init__(
        self,
        config: Mapping[str, Any] | None = None,
        *,
        session: requests.Session | None = None,
    ) -> None:
        values = dict(self._config_defaults)
        values.update(
            {
                key: value
                for key, value in (config or {}).items()
                if key in self._config_defaults and value is not None
            }
        )
        self.data_path = Path(values["data_path"]).expanduser()
        self.studip_host = str(values["studip_host"])
        self.prefix = "/" + str(values["prefix"]).strip("/")
        self.auth_method = str(values["auth_method"])
        self.use_git = bool(values["use_git"])
        self.git_commit_message_prefix = str(values["git_commit_message_prefix"])
        self.browser = str(values["browser"])
        self.request_timeout = float(values["request_timeout"])

        self.cookie: str | None = None
        self.current_semester: str | None = None
        self.user_id: str | None = None
        self.session = session or self._new_session()

        if self.use_git:
            self._initialize_git()
        self.load_current_semester()

    @staticmethod
    def _new_session() -> requests.Session:
        session = requests.Session()
        retries = Retry(
            total=3,
            connect=3,
            read=3,
            status=3,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            raise_on_status=False,
        )
        session.mount("https://", HTTPAdapter(max_retries=retries))
        return session

    def _initialize_git(self) -> None:
        if not shutil.which("git"):
            raise StudipSyncError("git was requested but is not installed")
        process = subprocess.run(
            ["git", "-C", str(self.data_path), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
        )
        if process.returncode != 0:
            raise StudipSyncError(
                "No git repository found in the data path; run 'git init' there first"
            )
        self.top_level = Path(process.stdout.strip())

    def open_cookie_db(self, cookie_file: str | Path) -> sqlite3.Connection:
        connection = sqlite3.connect(cookie_file)
        try:
            while True:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version >= len(self.cookie_db_migrations):
                    break
                connection.executescript(self.cookie_db_migrations[version])
                connection.execute(f"PRAGMA user_version = {version + 1}")
            connection.commit()
        except Exception:
            connection.close()
            raise
        return connection

    def validate_cookie(self, cookie: str | None) -> bool:
        if not cookie:
            return False
        url = self._url("/user")
        try:
            response = self.session.get(
                url,
                headers=self._auth_headers(cookie),
                timeout=self.request_timeout,
            )
        except requests.RequestException as error:
            eprint(f"Could not validate cached login: {error}")
            return False
        try:
            if response.status_code == 200:
                return True
            eprint(f"Cached login is invalid (HTTP {response.status_code})")
            return False
        finally:
            response.close()

    def _cache_directory(self) -> Path:
        if cache_home := os.environ.get("XDG_CACHE_HOME"):
            return Path(cache_home).expanduser() / "studip_sync"
        system = platform.system()
        home = Path.home()
        if system == "Windows":
            return home / "AppData" / "Local" / "studip_sync"
        if system == "Linux":
            return home / ".cache" / "studip_sync"
        if system == "Darwin":
            return home / "Library" / "Caches" / "studip_sync"
        raise StudipSyncError(
            "Cannot determine a cache directory; set the XDG_CACHE_HOME environment variable"
        )

    def get_browser_cookie(self) -> str:
        if self.cookie:
            return self.cookie

        cache_dir = self._cache_directory()
        cache_dir.mkdir(parents=True, exist_ok=True)
        database = self.open_cookie_db(cache_dir / "cookies.db")
        try:
            row = database.execute(
                "SELECT value FROM cookies "
                "WHERE name = 'Seminar_Session' AND host = ? "
                "ORDER BY id DESC LIMIT 1",
                (self.studip_host,),
            ).fetchone()
            cached_cookie = row[0] if row else None
            if cached_cookie:
                eprint("Found a cached Stud.IP login")
            if self.validate_cookie(cached_cookie):
                self.cookie = cached_cookie
                return cached_cookie

            eprint("Cached login not found or expired; reauthenticating...")
            try:
                cookies = self._read_browser_cookies(database)
            except StudipSyncError:
                raise
            except Exception as error:
                raise StudipSyncError(
                    f"Could not read cookies from {self.browser}: {error}"
                ) from error
            database.executemany(
                "INSERT OR REPLACE INTO cookies "
                "(name, value, host, path, secure, httponly, expires) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        item["name"],
                        item["value"],
                        item.get("domain", self.studip_host).lstrip("."),
                        item.get("path", "/"),
                        int(item.get("secure", False)),
                        int(item.get("httpOnly", False)),
                        item.get("expiry"),
                    )
                    for item in cookies
                ],
            )
            database.commit()
        finally:
            database.close()

        cookie = next(
            (item["value"] for item in cookies if item["name"] == "Seminar_Session"),
            None,
        )
        if not cookie:
            raise StudipSyncError(
                "The browser did not provide a Seminar_Session cookie"
            )
        if not self.validate_cookie(cookie):
            raise StudipSyncError("The browser login is not valid for the Stud.IP API")
        self.cookie = cookie
        eprint("Retrieved a valid Stud.IP login")
        return cookie

    def _read_browser_cookies(
        self, database: sqlite3.Connection
    ) -> list[dict[str, Any]]:
        if self.browser == "selenium":
            return self._read_selenium_cookies(database)
        if self.browser not in {"firefox", "chrome", "brave"}:
            raise StudipSyncError(f"Unknown browser: {self.browser!r}")

        import browser_cookie3

        cookie_jar = getattr(browser_cookie3, self.browser)(
            domain_name=self.studip_host
        )
        return [
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain or self.studip_host,
                "path": cookie.path or "/",
                "secure": cookie.secure,
                "httpOnly": bool(cookie._rest.get("HttpOnly", False)),
                "expiry": cookie.expires,
            }
            for cookie in cookie_jar
        ]

    def _read_selenium_cookies(
        self, database: sqlite3.Connection
    ) -> list[dict[str, Any]]:
        from selenium import webdriver

        driver = webdriver.Chrome()
        web_prefix = self.prefix.strip("/")
        if web_prefix.endswith("api.php"):
            web_prefix = web_prefix[: -len("api.php")]
        login_url = f"https://{self.studip_host}/{web_prefix}index.php"
        try:
            # Selenium only accepts cookies after visiting their domain.
            driver.get(login_url)
            rows = database.execute(
                "SELECT name, value, host, path, secure, httponly, expires "
                "FROM cookies WHERE host = ?",
                (self.studip_host,),
            ).fetchall()
            for name, value, host, path, secure, httponly, expires in rows:
                item: dict[str, Any] = {
                    "name": name,
                    "value": value,
                    "domain": host,
                    "path": path,
                    "secure": bool(secure),
                    "httpOnly": bool(httponly),
                }
                if expires is not None:
                    item["expiry"] = expires
                driver.add_cookie(item)
            if rows:
                driver.get(login_url)
            eprint("Log in to Stud.IP in the browser window, then return here.")
            input("Press Enter after logging in...")
            return driver.get_cookies()
        finally:
            driver.quit()

    def get_cookie(self) -> str:
        if self.auth_method != "cookie":
            raise StudipSyncError(
                f"Authentication method {self.auth_method!r} is not supported"
            )
        return self.get_browser_cookie()

    def get_user_id(self) -> str:
        if self.user_id is None:
            self.user_id = self.get("/user")["user_id"]
        return self.user_id

    def _url(self, path: str) -> str:
        if path.startswith(self.prefix):
            path = path[len(self.prefix) :]
        return f"https://{self.studip_host}{self.prefix}/{path.lstrip('/')}"

    @staticmethod
    def _auth_headers(cookie: str) -> dict[str, str]:
        return {"Cookie": f"Seminar_Session={cookie}"}

    def get_req(self, path: str, *, stream: bool = False) -> requests.Response:
        url = self._url(path)
        eprint(f"GET {url}")
        try:
            response = self.session.get(
                url,
                headers=self._auth_headers(self.get_cookie()),
                timeout=self.request_timeout,
                stream=stream,
            )
        except requests.RequestException as error:
            raise StudipSyncError(f"Request failed for {url}: {error}") from error
        if response.status_code != 200:
            detail = response.text.strip().replace("\n", " ")[:200]
            response.close()
            raise ApiError(url, response.status_code, detail)
        return response

    def get_no_parse(self, path: str) -> bytes:
        with self.get_req(path) as response:
            return response.content

    def get(self, path: str) -> Any:
        with self.get_req(path) as response:
            try:
                return response.json()
            except requests.JSONDecodeError as error:
                raise StudipSyncError(
                    f"Stud.IP returned invalid JSON for {response.url}"
                ) from error

    def get_subfolders(self, folder: dict[str, Any]) -> dict[str, Any]:
        if not folder.get("is_readable"):
            return {**folder, "files": [], "subfolders": []}
        folder_id = folder["id"]
        subfolders = self.get(f"/folder/{folder_id}/subfolders")["collection"]
        return {
            **folder,
            "files": self.get(f"/folder/{folder_id}/files")["collection"],
            "subfolders": [self.get_subfolders(item) for item in subfolders],
        }

    def get_courses(self, semester_id: str | None = None) -> list[dict[str, Any]]:
        path = f"/user/{self.get_user_id()}/courses"
        if semester_id is not None:
            path = f"{path}?semester={semester_id}"
        raw_courses = self.get(path)["collection"]
        courses: list[dict[str, Any]] = []
        for raw_course in raw_courses.values():
            course = dict(raw_course)
            documents_path = course.get("modules", {}).get("documents")
            if not documents_path:
                courses.append(course)
                continue
            top_folder = self.get(documents_path)
            folder_id = top_folder.get("id")
            if folder_id:
                top_folder["files"] = self.get(f"/folder/{folder_id}/files")[
                    "collection"
                ]
            top_folder["subfolders"] = [
                self.get_subfolders(item) for item in top_folder.get("subfolders", [])
            ]
            course["top_folder"] = top_folder
            courses.append(course)
        return courses

    @staticmethod
    def escape_filename(name: str) -> str:
        return name.replace("/", "_")

    @staticmethod
    def clean_path(path: str) -> str:
        return re.sub(r"[<>:\"\\|?*]", "_", path)

    @staticmethod
    def _safe_component(name: str) -> str:
        cleaned = re.sub(r"[\x00-\x1f<>:\"/\\|?*]", "_", str(name))
        cleaned = cleaned.rstrip(". ")
        if cleaned in {"", ".", ".."}:
            return "_"
        return cleaned

    def get_current_semester(self) -> str | None:
        if self.current_semester is None:
            self.load_current_semester()
        return self.current_semester

    def update_links(self) -> None:
        current_path = self.data_path / "this-semester"
        if os.path.lexists(current_path):
            eprint(f"Replacing {current_path}")
            if current_path.is_symlink() or current_path.is_file():
                current_path.unlink()
            else:
                shutil.rmtree(current_path)
        current_path.mkdir(parents=True)

        course_names = {
            self._safe_component(course["title"])
            for course in self.get_courses(self.get_current_semester())
        }
        for course_name in sorted(course_names):
            (current_path / course_name).symlink_to(
                Path("..") / "archive" / course_name,
                target_is_directory=True,
            )
        self._commit_path_if_changed("this-semester", "updated this-semester links")

    def select_semester(self, semester: str | None = None) -> None:
        semesters = self.get("/semesters")["collection"]
        by_name = {item["title"]: item for item in semesters.values()}
        if semester is None:
            import pzp

            chosen = pzp.pzp(by_name.keys())
            if not chosen:
                raise StudipSyncError("No semester chosen")
            semester = chosen
        if semester not in by_name:
            available = ", ".join(sorted(by_name))
            raise StudipSyncError(
                f"Semester {semester!r} not found. Available semesters: {available}"
            )
        semester_id = by_name[semester]["id"]
        self.current_semester = semester_id
        self.save_current_semester(semester_id)
        self.update_links()

    def load_current_semester(self) -> str | None:
        semester_file = self.data_path / ".current-semester"
        if semester_file.exists():
            self.current_semester = semester_file.read_text(encoding="utf-8").strip()
        return self.current_semester

    def save_current_semester(self, semester_id: str) -> None:
        self.data_path.mkdir(parents=True, exist_ok=True)
        (self.data_path / ".current-semester").write_text(semester_id, encoding="utf-8")
        self._commit_path_if_changed(".current-semester", "updated current semester")

    def _iter_files(
        self, folder: Mapping[str, Any], parent: tuple[str, ...]
    ) -> Iterator[tuple[tuple[str, ...], str]]:
        for item in folder.get("files", []):
            yield (*parent, self._safe_component(item["name"])), item["id"]
        for subfolder in folder.get("subfolders", []):
            subfolder_parent = (*parent, self._safe_component(subfolder["name"]))
            yield from self._iter_files(subfolder, subfolder_parent)

    def get_files(self, folder: Mapping[str, Any], parent_path: str) -> dict[str, str]:
        """Return files keyed by relative path (kept for API compatibility)."""
        parent = tuple(
            self._safe_component(part)
            for part in Path(parent_path).parts
            if part not in {"/", ""}
        )
        return {
            str(Path(*parts)): file_id
            for parts, file_id in self._iter_files(folder, parent)
        }

    def _write_atomic_download(self, file_id: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_name: str | None = None
        try:
            try:
                with self.get_req(f"/file/{file_id}/download", stream=True) as response:
                    with tempfile.NamedTemporaryFile(
                        mode="wb",
                        dir=destination.parent,
                        prefix=f".{destination.name}.",
                        suffix=".part",
                        delete=False,
                    ) as output:
                        temporary_name = output.name
                        for chunk in response.iter_content(chunk_size=128 * 1024):
                            if chunk:
                                output.write(chunk)
                        output.flush()
                        os.fsync(output.fileno())
            except requests.RequestException as error:
                raise StudipSyncError(
                    f"Download stream failed for file {file_id}: {error}"
                ) from error
            os.replace(temporary_name, destination)
            temporary_name = None
        finally:
            if temporary_name is not None:
                Path(temporary_name).unlink(missing_ok=True)

    @staticmethod
    def _write_placeholder(destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".part",
                delete=False,
            ) as output:
                temporary_name = output.name
                output.write("studip-sync:non-downloadable-file")
            os.replace(temporary_name, destination)
            temporary_name = None
        finally:
            if temporary_name is not None:
                Path(temporary_name).unlink(missing_ok=True)

    def _sync_file(self, relative_parts: tuple[str, ...], file_id: str) -> bool:
        destination = self.data_path / "archive" / Path(*relative_parts)
        if destination.exists() and destination.stat().st_size > 0:
            return False

        metadata = self.get(f"/file/{file_id}")
        if destination.exists() and destination.stat().st_size == 0:
            remote_size = metadata.get("size", metadata.get("filesize"))
            if remote_size in (None, 0, "0"):
                return False
            eprint(f"Replacing incomplete empty file: {destination}")

        relative_name = str(Path(*relative_parts))
        if metadata.get("is_downloadable"):
            eprint(f"Downloading {relative_name} to {destination}")
            self._write_atomic_download(file_id, destination)
        else:
            eprint(f"Creating placeholder for non-downloadable file: {relative_name}")
            self._write_placeholder(destination)
        return True

    def sync(self) -> None:
        courses = self.get_courses(self.current_semester)
        files: list[tuple[tuple[str, ...], str]] = []
        for course in courses:
            top_folder = course.get("top_folder")
            if top_folder:
                course_path = (self._safe_component(course["title"]),)
                files.extend(self._iter_files(top_folder, course_path))

        seen_paths: set[tuple[str, ...]] = set()
        failures: list[tuple[str, str]] = []
        downloaded = 0
        for relative_parts, file_id in files:
            relative_name = str(Path(*relative_parts))
            if relative_parts in seen_paths:
                failures.append((relative_name, "duplicate remote path"))
                eprint(f"Skipping duplicate remote path: {relative_name}")
                continue
            seen_paths.add(relative_parts)
            try:
                downloaded += int(self._sync_file(relative_parts, file_id))
            except (ApiError, StudipSyncError, OSError) as error:
                failures.append((relative_name, str(error)))
                eprint(f"Failed to synchronize {relative_name}: {error}")

        self._commit_path_if_changed("archive", "updated archive")
        eprint(
            f"Sync finished: {downloaded} updated, "
            f"{len(files) - downloaded - len(failures)} unchanged, "
            f"{len(failures)} failed"
        )
        if failures:
            raise SyncIncompleteError(failures)

    def _commit_path_if_changed(self, path: str, message: str) -> None:
        if not self.use_git:
            return
        status = subprocess.run(
            ["git", "-C", str(self.data_path), "status", "--porcelain", "--", path],
            capture_output=True,
            text=True,
            check=True,
        )
        if not status.stdout.strip():
            return
        subprocess.run(
            ["git", "-C", str(self.data_path), "add", "--", path], check=True
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(self.data_path),
                "commit",
                "-m",
                self.git_commit_message_prefix + message,
            ],
            check=True,
        )
        subprocess.run(["git", "-C", str(self.data_path), "push"], check=True)
