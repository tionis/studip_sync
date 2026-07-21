# studip-sync

A command-line tool that uses the Stud.IP REST API to synchronize course files
into a local archive.

## Installation

```bash
pipx install git+https://github.com/tionis/studip_sync
# or with uv:
uv tool install git+https://github.com/tionis/studip_sync
```

## Usage

The tool authenticates with a `Seminar_Session` cookie. It can extract the
cookie from Firefox, Chrome, or Brave, or open a Selenium-controlled browser for
an interactive login. Select the browser with `--browser`.

Before the first sync, select a semester. This also creates the
`this-semester` directory containing links to the selected courses:

```bash
studip_sync --data-path ~/Documents/studip --browser brave select-semester
```

Then synchronize new files:

```bash
studip_sync --data-path ~/Documents/studip --browser brave sync
```

Downloads are written to temporary files and atomically moved into place only
after completion. Temporary server errors are retried. If a file still fails,
the remaining files are processed and the command exits with status 1 after a
summary; rerunning the command retries the failed file.

Existing non-empty files are left untouched. This means the tool does not yet
detect a changed remote file when its name stays the same.

## Development

The regression suite uses only the Python standard library:

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

The legacy Stud.IP REST API used by this project is deprecated; see the
discussion in issue #1.
