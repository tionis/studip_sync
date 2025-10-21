#!/bin/env python3
import os
import platform
#import json
import browser_cookie3
from selenium import webdriver
import shutil
import subprocess
import requests
import pzp
import re
import sqlite3
import sys

def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)

class StudipSync:
    # Config
    data_path = "."
    studip_host = "studip.uni-passau.de"
    prefix = "/studip/api.php"
    auth_method = "cookie"
    use_git = False
    git_commit_message_prefix = "studip-sync: "
    browser = "selenium"  # or "firefox", "chrome", "brave"

    # Cookies DB migrations
    cookie_db_migrations = [
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
    ]

    # Cache
    cookie = None
    current_semester = None
    user_id = None

    # constructor defaults all non specified config values to the default values (config is passed through from arguments in some explicit way
    def __init__(self, config={}):
        for key in config:
            if hasattr(self, key):
                setattr(self, key, config[key])
        if self.use_git:
            if not shutil.which("git"):
                raise FileNotFoundError("git not found")
            git_top_level_process = subprocess.run(["git", "-C", self.data_path, "rev-parse", "--show-toplevel"], capture_output=True)
            if git_top_level_process.returncode != 0:
                eprint("No git repository found in data path")
                eprint("Please initialize the repository with 'git init'")
                eprint("The author also recommends using lfs to track large files")
                raise Exception("No git repository found")
            self.top_level = git_top_level_process.stdout.decode("utf-8").strip()
        self.load_current_semester()

    def open_cookie_db(self, cookie_file):
        conn = sqlite3.connect(cookie_file)
        cursor = conn.cursor()
        while True:
            user_version = cursor.execute("PRAGMA user_version").fetchone()[0]
            if user_version >= len(self.cookie_db_migrations):
                break
            migration = self.cookie_db_migrations[user_version]
            cursor.execute(migration)
            cursor.execute(f"PRAGMA user_version = {user_version + 1}")
        conn.commit()
        cursor.close()
        return conn

    def validate_cookie(self, cookie):
        if cookie is None or cookie == "":
            return False
        # Check if the cookie is valid by making a request to the StudIP API
        try:
            resp = requests.get(f"https://{self.studip_host}{self.prefix}/user", headers={"Cookie": f"Seminar_Session={cookie}"})
            if resp.status_code == 200:
                return True
            else:
                eprint(f"Invalid cookie: {resp.status_code} {resp.text}")
                return False
        except requests.RequestException as e:
            eprint(f"Error validating cookie: {e}")
            return False


    def get_browser_cookie(self):
        if self.cookie is not None:
            return self.cookie
        cache_dir = "" # XDG CACHE DIR for studip_sync
        # use XFD_CACHE_HOME if set, otherwise use ~/.cache/studip_sync
        if "XDG_CACHE_HOME" in os.environ:
            cache_dir = os.path.join(os.environ["XDG_CACHE_HOME"], "studip_sync")
        else:
            if platform.system() == "Windows":
                cache_dir = os.path.join(os.path.expanduser("~"), "AppData", "Local", "studip_sync")
            elif platform.system() == "Linux":
                cache_dir = os.path.join(os.path.expanduser("~"), ".cache", "studip_sync")
            elif platform.system() == "Darwin":
                cache_dir = os.path.join(os.path.expanduser("~"), "Library", "Caches", "studip_sync")
        if cache_dir == "":
            raise ValueError("Cache directory not set. Please set XDG_CACHE_HOME or use a different platform.")
        # Ensure the cache directory exists
        if not os.path.exists(cache_dir):
            os.makedirs(cache_dir)
        # Check if the cookie file exists
        cookie_file = os.path.join(cache_dir, "cookies.db")
        db = self.open_cookie_db(cookie_file)
        cursor = db.cursor()
        # Query the cookie database for the Seminars_Session cookie
        cursor.execute("SELECT value FROM cookies WHERE name = 'Seminar_Session' AND host = ? AND name = 'Seminar_Session'", (self.studip_host,))
        result = cursor.fetchone()
        cookie = ""
        if result is not None:
            cookie = result[0]
            eprint(f"Found cookie in cache: {cookie}")
        
        if self.validate_cookie(cookie):
            self.cookie = cookie
            return cookie

        eprint("Cookie not found or invalid, opening browser session for reauthentication...")

        cookies = []
        if self.browser == "selenium":
            # Initialize the browser driver
            driver = webdriver.Chrome()

            # Load old cookies
            cursor.execute("SELECT name, value, host, path, secure, httponly, expires FROM cookies WHERE host = ?", (self.studip_host,))
            cookies = cursor.fetchall()
            for name, value, host, path, secure, httponly, expires in cookies:
                # Add cookies to the browser
                driver.add_cookie({
                    'name': name,
                    'value': value,
                    'domain': host,
                    'path': path,
                    'secure': bool(secure),
                    'httpOnly': bool(httponly),
                    'expiry': expires if expires is not None else None
                })
            # Open the StudIP login page (use prefix but replace api.php with index.php at the end)
            driver.get(f"https://{self.studip_host}{self.prefix.removesuffix('api.php')}index.php")

            # Wait for the user to log in
            eprint("Please log in to StudIP in the opened browser window and don't close it.")
            input("Press Enter after logging in...")

            # Retrieve browser cookies
            cookies = driver.get_cookies()

            # Close the browser
            driver.quit()

        elif self.browser == "firefox":
            cj = browser_cookie3.firefox(domain_name=self.studip_host)
            cookie_dict = requests.utils.dict_from_cookiejar(cj)
            cookies = []
            for name, value in cookie_dict.items():
                cookies.append({
                    'name': name,
                    'value': value,
                    'domain': self.studip_host,
                    'path': '/',
                    'secure': False,
                    'httpOnly': False,
                    'expiry': None
                })
        elif self.browser == "chrome":
            cj = browser_cookie3.chrome(domain_name=self.studip_host)
            cookie_dict = requests.utils.dict_from_cookiejar(cj)
            cookies = []
            for name, value in cookie_dict.items():
                cookies.append({
                    'name': name,
                    'value': value,
                    'domain': self.studip_host,
                    'path': '/',
                    'secure': False,
                    'httpOnly': False,
                    'expiry': None
                })
        elif self.browser == "brave":
            cj = browser_cookie3.brave(domain_name=self.studip_host)
            cookie_dict = requests.utils.dict_from_cookiejar(cj)
            cookies = []
            for name, value in cookie_dict.items():
                cookies.append({
                    'name': name,
                    'value': value,
                    'domain': self.studip_host,
                    'path': '/',
                    'secure': False,
                    'httpOnly': False,
                    'expiry': None
                })
        else:
            raise ValueError(f"Unknown browser: '{self.browser}'")

        # Print the cookies
        for cookie in cookies:
            cursor.execute(
                "INSERT OR REPLACE INTO cookies (name, value, host, path, secure, httponly, expires) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (cookie['name'], cookie['value'], cookie['domain'], cookie['path'], int(cookie.get('secure', False)), int(cookie.get('httpOnly', False)), cookie.get('expiry'))
            )
        
        # Commit the changes to the database
        db.commit()
        cursor.close()

        # Set the cookie attribute
        self.cookie = next((cookie['value'] for cookie in cookies if cookie['name'] == 'Seminar_Session'), None)
        if self.cookie is None:
            raise Exception("Failed to retrieve Seminar_Session cookie from browser")
        eprint(f"Retrieved cookie: {self.cookie}")
        
        # Return the cookie
        return self.cookie

    def get_cookie(self):
        if self.auth_method == "cookie":
            return self.get_browser_cookie()
        else:
            raise NotImplementedError(f"Auth method \"{self.auth_method}\" not supported")

    def get_user_id(self):
        if self.user_id is not None:
            return self.user_id
        self.user_id = self.get("/user")["user_id"]
        return self.user_id

    def get_req(self, path):
        path = path.removeprefix(self.prefix)
        url = f"https://{self.studip_host}{self.prefix}{path}"
        eprint(f"GET {url}")
        resp = requests.get(url, headers={"Cookie": f"Seminar_Session={self.get_cookie()}"})
        if resp.status_code != 200:
            raise Exception(f"Failed to get {url}: {resp.status_code}")
        return resp

    def get_no_parse(self, path):
        return self.get_req(path).content

    def get(self, path):
        return self.get_req(path).json()

    def get_subfolders(self, folder):
        if "is_readable" in folder and folder["is_readable"]:
            subfolders = self.get(f"/folder/{folder['id']}/subfolders")["collection"]
            folder["files"] = self.get(f"/folder/{folder['id']}/files")["collection"]
            folder["subfolders"] = []
            for subfolder in subfolders:
                folder["subfolders"].append(self.get_subfolders(subfolder))
            return folder
        else:
            return {"files": [], "subfolders": []}

    def get_courses(self, semester_id=None):
        courses = []
        if semester_id is None:
            raw_courses = self.get(f"/user/{self.get_user_id()}/courses")["collection"]
        else:
            raw_courses = self.get(f"/user/{self.get_user_id()}/courses?semester={semester_id}")["collection"]

        for course in raw_courses.values():
            path = course["modules"]["documents"] if "documents" in course["modules"] else None
            if path is not None:
                course["top_folder"] = self.get(path)

            if "top_folder" in course and "id" in course["top_folder"] and course["top_folder"]["id"]:
                course["top_folder"]["files"] = self.get(f"/folder/{course['top_folder']['id']}/files")["collection"]

            if "top_folder" in course and "subfolders" in course["top_folder"] and course["top_folder"]["subfolders"]:
                subfolders = []
                for subfolder in course["top_folder"]["subfolders"]:
                    subfolders.append(self.get_subfolders(subfolder))
                course["top_folder"]["subfolders"] = subfolders

            courses.append(course)

        return courses

    
    def escape_filename(self, name):
        return name.replace("/", "_")

    def clean_path(self, path):
        # forbidden symbols: "<" ">" ":" "\"" "\\" "|" "?" "*"
        dirty_symbols_regex = r"[<>:\"\\|?*]"
        return re.sub(dirty_symbols_regex, "_", path)

    def get_current_semester(self):
        if self.current_semester is None:
            return self.load_current_semester()
        return self.current_semester

    def update_links(self):
        # Update symlinks
        current_semester_path = os.path.join(self.data_path, "this-semester")
        if os.path.exists(current_semester_path):
            eprint(f"Removing old this-semester directory at {current_semester_path}")
            shutil.rmtree(current_semester_path)
        os.mkdir(current_semester_path)
        courses = self.get_courses(self.get_current_semester())
        for course in list(set([self.escape_filename(course["title"]) for course in courses])):
            os.symlink(os.path.join("..", "archive" , course), os.path.join(current_semester_path, course), target_is_directory=True)
        if self.use_git:
            # Count changes in this-semester dir
            changesProcess = subprocess.run(["git", "-C", self.data_path, "diff", "--name-only", "--", "current-semester"], capture_output=True)
            changes = changesProcess.stdout.decode("utf-8").split("\n")
            if len(changes) > 0:
                # Commit changes
                subprocess.run(["git", "-C", self.data_path, "add", current_semester_path])
                subprocess.run(["git", "-C", self.data_path, "commit", "-m", self.git_commit_message_prefix + "updated this-semester links"])
                subprocess.run(["git", "-C", self.data_path, "push"])

    def select_semester(self, semester=None):
        semesters = self.get("/semesters")["collection"]
        semesterNameToMeta = {}
        for val in semesters.values():
            semesterNameToMeta[val["title"]] = val
        if semester is None:
            chosen = pzp.pzp(semesterNameToMeta.keys())
            eprint("Chosen semester:", chosen)
            if len(chosen) == 0:
                raise Exception("No semester chosen")
            semester = chosen
        if semester not in semesterNameToMeta:
            raise ValueError(f"Semester {semester} not found in available semesters: {list(semesterNameToMeta.keys())}")
        semester_id = semesterNameToMeta[semester]["id"]        
        self.current_semester = semester_id
        self.save_current_semester(semester_id)
        self.update_links()

    def load_current_semester(self):
        if os.path.exists(os.path.join(self.data_path, ".current-semester")):
            with open(os.path.join(self.data_path, ".current-semester"), "r") as f:
                self.current_semester = f.read().strip()
        return self.current_semester

    def save_current_semester(self, semester_id):
        # Ensure that the directory exists
        os.makedirs(self.data_path, exist_ok=True)
        with open(os.path.join(self.data_path, ".current-semester"), "w") as f:
            f.write(semester_id)
        if self.use_git:
            subprocess.run(["git", "-C", self.data_path, "add", ".current-semester"])
            subprocess.run(["git", "-C", self.data_path, "commit", "-m", self.git_commit_message_prefix + "updated current semester"])
            subprocess.run(["git", "-C", self.data_path, "push"]) # IDEA: push will be done one layer above this if head changed


    def get_files(self, folder, parent_path):
        files = {}
        for file in folder["files"]:
            files[f"{parent_path}/{file['name']}"] = file["id"]
        if "subfolders" in folder:
            for subfolder in folder["subfolders"]:
                for key,value in self.get_files(subfolder, f"{parent_path}/{folder['name']}").items():
                    files[key] = value
        return files

    def sync(self):
        courses = self.get_courses(self.current_semester)
        files = {}
        for course in courses:
            if "top_folder" in course:
                for key, value in self.get_files(course["top_folder"], course["title"]).items():
                    files[key] = value

        for file in files:
            file_path = self.clean_path(os.path.join(self.data_path, "archive", file))
            if not os.path.exists(file_path):
                os.makedirs(os.path.dirname(file_path), exist_ok=True)
                eprint(f"Downloading {file} to {file_path}")
                file_meta = self.get(f"/file/{files[file]}")
                if file_meta["is_downloadable"]:
                    with open(file_path, 'wb') as f:
                        f.write(self.get_no_parse(f"/file/{files[file]}/download"))
                else:
                    eprint(f"File {file} is not downloadable")
                    eprint(f"Creating placeholder file {file_path}")
                    # Write studip-sync:non-downloadable-file into placeholder file
                    with open(file_path, 'w') as f:
                        f.write("studip-sync:non-downloadable-file")

        if self.use_git:
            # Count changes in archive dir
            changesProcess = subprocess.run(["git", "-C", self.data_path, "diff" , "--name-only", "--", "archive"], capture_output=True)
            changes = changesProcess.stdout.decode("utf-8").split("\n")
            if len(changes) > 0:
                # Commit changes
                subprocess.run(["git", "-C", self.data_path, "add", os.path.join(self.data_path, "archive")])
                subprocess.run(["git", "-C", self.data_path, "commit", "-m", self.git_commit_message_prefix + "updated archive"])
                subprocess.run(["git", "-C", self.data_path, "push"])
