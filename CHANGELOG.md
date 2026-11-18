# Changelog

## 1.0.1

- A `y` piped into `install`, `uninstall` or `restore` now counts as yes. Windows PowerShell 5.1 can
  put a UTF-8 byte order mark in front of it, and a console with a non-UTF-8 code page then read the
  line wrongly, so the first try ended in "Cancelled". Nothing else changed.
- 56 tests.

## 1.0.0

- `hook pre`: copies the paths a Bash or PowerShell command is about to delete, move or overwrite
  to `.folder-undo/<time>-<n>/`, with a manifest. Always exits 0.
- `hook session-start`: one restore point of the whole folder, stored by sha256; replaced when the
  next session starts.
- `list`, `show`, `restore` (plan first, then `y`; the version on disk is kept before it is
  overwritten), `restore --session`, `prune --keep`.
- `install` and `uninstall` for one project, both with `--dry-run`.
- 52 tests.
