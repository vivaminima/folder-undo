# folder-undo

![folder-undo: a copy before the shell deletes it](docs/folder-undo_banner.png)

**Not a developer?**
- A file Claude Code deletes with a command skips the Recycle Bin, and Claude Code's rewind (`/rewind`) can't bring it back.
- To set it up, open Claude Code in your folder and say: "Install folder-undo from https://github.com/vivaminima/folder-undo. Save folder_undo.py in this folder and show me the dry run first." Then start a new session there. You need Python 3.10 or newer.
- If files go missing, say: "Use folder-undo to show me what it can bring back, then restore it."

Keeps a copy of a file right before a shell command deletes, moves or overwrites it — the part Claude Code's rewind doesn't cover.

Claude Code's checkpoints track the edits made by its file editing tools. Its documentation lists
"Bash command changes not tracked" as a limitation: "A file that only Bash commands changed isn't
tracked and stays as it is." https://code.claude.com/docs/en/checkpointing

folder-undo is one Python file and two hooks:

- **Before a Bash or PowerShell command runs**, the hook reads the command. If it would delete, move or
  overwrite paths inside the project folder, those paths are copied to `.folder-undo/<time>-<n>/` first,
  with a `manifest.json` (command, time, paths, bytes, sha256). It does not block: the command always runs.
- **When a session starts**, the hook keeps one restore point of the whole project folder in
  `.folder-undo/store/`. Files are stored by their sha256, so only changed files are written again.
  When the next session starts, the earlier restore point is replaced.

Reading a command does not catch every form (for example, a file deleted from inside a script).
That is why a restore point of the whole folder is kept when a session starts. Restore points for
several sessions and a report are in Pro.

## Quick start

Put `folder_undo.py` anywhere on your machine, then turn the hooks on for one project:

```
python folder_undo.py --project path/to/project install --dry-run
python folder_undo.py --project path/to/project install
```

`install` shows the file it would change (`<project>/.claude/settings.json`), where the copy of the
current file goes, and the exact lines it adds. It writes only after you type `y`. Hooks and settings
that are already there are kept. If the project uses git, it then asks whether to add `.folder-undo/`
to `.gitignore`. The hooks load when the next session starts in that project.

Later, from inside the project folder:

```
python folder_undo.py list
python folder_undo.py show <id>
python folder_undo.py restore <id>
python folder_undo.py restore <id> notes/plan.md
python folder_undo.py restore --session
python folder_undo.py prune --keep 20
```

`restore` first lists what it would write and where (`bring back`, `overwrite`, `same`) and waits for
`y`. Before it overwrites a file, the version on disk now is copied to
`.folder-undo/before-restore-<time>/`, and that copy can be restored like any other. If a file cannot
be written (it is open in another program, or the disk is full), that file is left as it was and the
command says so.

`restore --session` puts back the files as they were when the session started. Files made after that
point stay as they are.

To turn the hooks off:

```
python folder_undo.py --project path/to/project uninstall
```

`uninstall` puts `settings.json` back byte for byte from the copy made at install. If you changed
the file in the meantime, it takes out only its own two hooks and keeps the rest. Your copies in
`.folder-undo/` stay where they are.

## What it covers

Each form below has a test in `tests/` that copies real files and puts them back.

| Tool | Forms |
|---|---|
| PowerShell | `Remove-Item` and its aliases `rm` `del` `erase` `rd` `ri` (with `-Recurse`, wildcards, comma lists, `-LiteralPath`), `rmdir /s`, `Move-Item` (the source and the file it overwrites), `Rename-Item`, `Copy-Item` onto an existing file, `Set-Content`, `Out-File`, `Clear-Content`, `>` onto an existing file, `cmd /c del` |
| Bash | `rm`, `rm -rf`, `mv` (the source and the file it overwrites), `cp` onto an existing file, `>` onto an existing file, `sed -i`, `truncate`, `tee`, `find ... -delete`, `find ... -exec rm`, `bash -c "..."`, `cd dir && rm ...` |
| Both | Paths with spaces, non-ASCII file names, several commands on one line |
| Restore point | A file deleted by a Python script (the command reader does not see it) comes back with `restore --session` |

What is left alone, by design:

- Paths outside the project folder are not copied.
- Names in `.folder-undo/ignore` are neither copied nor part of a restore point. It starts with
  `.git`, `node_modules`, `.venv`, `__pycache__` and `.folder-undo`; edit it as you like.
- One copy is at most 200 MB and a restore point is made of a folder of at most 500 MB. Change
  `copy_limit_mb` and `snapshot_limit_mb` in `.folder-undo/config.json`
  (for example `{"copy_limit_mb": 500}`).
- Whatever is skipped (too large, unreadable, a path with a variable in it) gets one line in
  `.folder-undo/skipped.log`. The hooks always exit 0.

## What it needs

- Python 3.10 or newer. No pip install: one file, standard library only.
- No network calls.
- Claude Code with hooks (`PreToolUse` for the Bash and PowerShell tools, and `SessionStart`).
- Tested on Windows 11 with Python 3.10 and Windows PowerShell 5.1: 56 tests, also run in a console
  that cannot print every character (cp949). Run them yourself with
  `python -m unittest discover -s tests`.

## Pro

Folder Undo Pro adds restore points for the last 10 sessions, the version history of one file,
an HTML report of what changed in each session, and an optional stop for commands that reach
outside the project. Folder Undo Pro ($19): https://vivastory4.gumroad.com/l/folder-undo-pro

## Licence

MIT, see `LICENSE`.
