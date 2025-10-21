# studip-sync

A simple script that uses the studip REST api to sync files.  
It can be installed via pipx:

```bash
pipx install git+https://github.com/tionis/studip_sync
# or if you want to use uv to install the tool:
uv tool install git+https://github.com/tionis/studip_sync

```

## Usage
This script needs cookies either via a temporary browser window you login to or it steals your browsers cookies. You can select a browser with the `--browser` flag.  
Before the first sync you need to select the current semester to set up the symlinks for the current semester folder and for the tool to select which courses to sync.
```
studip_sync --data-path ~/Documents/studip --browser brave select-semester
```

Syncing can then be done with the sync subcommand:
```
studip_sync --data-path ~/Documents/studip --browser brave sync
```

> NOTE: The studip REST API will be deprecated soon as mentionend in #1

> NOTE: studip-sync does not handle file changes, as it only syncs based on the filename
