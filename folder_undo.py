#!/usr/bin/env python3
"""folder-undo: keeps a copy of a file right before a shell command deletes, moves or overwrites it.

Two hooks do the work, and neither one ever blocks a command:

  hook pre            reads the command a Bash or PowerShell tool call is about to run. If it deletes,
                      moves or overwrites paths inside the project, those paths are copied first to
                      .folder-undo/<time>-<n>/ together with a manifest.json.
  hook session-start  keeps one restore point of the whole project folder in .folder-undo/store/, made
                      when a session starts. It also covers deletions the command reader cannot see,
                      such as a file removed from inside a script.

Commands you run yourself:

  list                         what is kept
  show <id>                    one copy or restore point in detail
  restore <id> [path ...]      put files back from a copy (shows the plan, then asks). Free: once per PC
  restore --session [path ...] put the folder back to how it was when the session started
  prune --keep N               drop all but the newest N copies
  install / uninstall          turn the hooks on or off for one project (--dry-run to look first)

Python 3.10 or newer, standard library only, no network.
"""
import argparse
import copy
import datetime
import difflib
import fnmatch
import glob
import hashlib
import json
import locale
import os
import re
import shutil
import subprocess
import sys

VERSION = "1.1.0"
PROG = "folder_undo.py"
PRO_URL = "https://vivastory4.gumroad.com/l/folder-undo-pro?utm_source=folder-undo-free&utm_medium=cli&utm_campaign=folder-undo-pro"
FREE_RESTORES = 1
DIR = ".folder-undo"
DEFAULT_IGNORE = [".git", "node_modules", ".venv", "__pycache__", DIR]
DEFAULTS = {"copy_limit_mb": 200, "snapshot_limit_mb": 500}
MB = 1024 * 1024
TIME = "%Y-%m-%d %H:%M:%S"
NOTE = "folder_undo.install.json"
GITIGNORE_LINE = DIR + "/"
REDIR, APPEND = "\x00>", "\x00>>"

DELETE = {"rm", "rmdir", "unlink", "del", "erase", "rd", "ri", "remove-item", "shred"}
MOVE = {"mv", "move", "mi", "move-item"}
RENAME = {"ren", "rni", "rename-item"}
COPY = {"cp", "copy", "cpi", "copy-item"}
WRITE_PS = {"set-content", "sc", "out-file", "clear-content", "clc", "tee-object"}
WRITE_SH = {"truncate", "tee"}
CD = {"cd", "chdir", "pushd", "set-location", "sl", "push-location"}
SHELLS = {"bash", "sh", "zsh", "dash"}
PSHELLS = {"powershell", "pwsh"}
SKIP = {"sudo", "command", "builtin", "time", "nohup", "exec", "env", "call"}
PS_VALUE = ("path", "literalpath", "lp", "pspath", "destination", "filepath", "value", "newname", "filter",
            "include", "exclude", "encoding", "erroraction", "ea", "stream", "width", "inputobject",
            "credential", "totalcount")
PS_SWITCH = ("recurse", "force", "whatif", "confirm", "nonewline", "passthru", "verbose", "append",
             "noclobber", "debug", "asbytestream")


class Stop(Exception):
    pass


def now():
    fixed = os.environ.get("FOLDER_UNDO_NOW")
    if fixed:
        try:
            return datetime.datetime.strptime(fixed, TIME)
        except ValueError:
            pass
    return datetime.datetime.now().replace(microsecond=0)


def human(size):
    for unit in ("bytes", "KB", "MB"):
        if size < 1024 or unit == "MB":
            return "%d %s" % (size, unit) if unit == "bytes" else "%.1f %s" % (size, unit)
        size /= 1024.0


def short(text, width=70):
    text = " ".join(text.split())
    return text if len(text) <= width else text[:width - 3] + "..."


def soft_stdio():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except Exception:
            pass


def copy_file(src, dst):
    """Copy src to dst. Returns (bytes, sha256). Restore and the store both go through here."""
    digest, size = hashlib.sha256(), 0
    with open(src, "rb") as reader, open(dst, "wb") as writer:
        while True:
            chunk = reader.read(1 << 20)
            if not chunk:
                break
            digest.update(chunk)
            writer.write(chunk)
            size += len(chunk)
    try:
        shutil.copystat(src, dst)
    except OSError:
        pass
    return size, digest.hexdigest()


def sha_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as reader:
        for chunk in iter(lambda: reader.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path, default=None):
    try:
        with open(path, "rb") as handle:
            return json.loads(handle.read().decode("utf-8-sig"))
    except (OSError, ValueError):
        return default


def write_json(path, data):
    with open(path, "wb") as handle:
        handle.write((json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


# --------------------------------------------------------------------------- project folder

class Project:
    def __init__(self, root):
        self.root = os.path.realpath(root)
        self.dir = os.path.join(self.root, DIR)
        self.store = os.path.join(self.dir, "store")

    def ensure(self):
        os.makedirs(self.dir, exist_ok=True)
        path = os.path.join(self.dir, "ignore")
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write("# One name or pattern per line. These are never copied and never in a restore point.\n")
                handle.write("\n".join(DEFAULT_IGNORE) + "\n")

    def config(self):
        data = dict(DEFAULTS)
        found = read_json(os.path.join(self.dir, "config.json"), {})
        if isinstance(found, dict):
            data.update(found)
        return data

    def limit(self, key):
        try:
            return float(self.config().get(key, DEFAULTS[key])) * MB
        except (TypeError, ValueError):
            return DEFAULTS[key] * MB

    def patterns(self):
        lines = list(DEFAULT_IGNORE)
        try:
            with open(os.path.join(self.dir, "ignore"), encoding="utf-8-sig") as handle:
                lines = [line.strip() for line in handle if line.strip() and not line.strip().startswith("#")]
        except OSError:
            pass
        return lines + [DIR]

    def ignored(self, rel, patterns=None):
        parts = rel.split("/")
        for pattern in patterns or self.patterns():
            pattern = pattern.replace("\\", "/").strip("/")
            if fnmatch.fnmatch(rel, pattern) or any(fnmatch.fnmatch(part, pattern) for part in parts):
                return True
        return False

    def log_skip(self, text):
        try:
            self.ensure()
            with open(os.path.join(self.dir, "skipped.log"), "a", encoding="utf-8", newline="\n") as handle:
                handle.write("%s  %s\n" % (now().strftime(TIME), " ".join(str(text).split())))
        except OSError:
            pass

    def rel(self, path):
        """Project-relative path with forward slashes; "" for the project folder; None when outside."""
        path = os.path.abspath(path)
        real = os.path.join(os.path.realpath(os.path.dirname(path)), os.path.basename(path))
        root = os.path.normcase(self.root)
        if os.path.normcase(real).rstrip("\\/") == root.rstrip("\\/"):
            return ""
        try:
            if os.path.normcase(os.path.commonpath([self.root, real])) != root:
                return None
        except ValueError:
            return None
        return os.path.relpath(real, self.root).replace("\\", "/")

    def own(self, rel):
        return rel == DIR or rel.startswith(DIR + "/")

    def sort(self, paths):
        inside, outside = [], []
        for path in paths:
            if not os.path.lexists(path):
                continue
            rel = self.rel(path)
            if rel is None:
                outside.append(path)
            elif not self.own(rel):
                inside.append(path)
        return sorted(set(inside)), sorted(set(outside))

    def uid(self, folder, base, first=True):
        n = 1
        while True:
            name = "%s-%d" % (base, n) if (first or n > 1) else base
            if not os.path.exists(os.path.join(folder, name)) and not os.path.exists(os.path.join(folder, name + ".json")):
                return name
            n += 1

    def copies(self):
        found = []
        try:
            names = sorted(os.listdir(self.dir))
        except OSError:
            return found
        for name in names:
            data = read_json(os.path.join(self.dir, name, "manifest.json"))
            if isinstance(data, dict) and isinstance(data.get("files"), list):
                data["id"] = name
                found.append(data)
        found.sort(key=lambda item: (str(item.get("time", "")), item["id"]))
        return found

    def sessions(self):
        found, folder = [], os.path.join(self.store, "sessions")
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            return found
        for name in names:
            data = read_json(os.path.join(folder, name)) if name.endswith(".json") else None
            if isinstance(data, dict) and isinstance(data.get("files"), dict):
                data["id"] = name[:-5]
                found.append(data)
        found.sort(key=lambda item: (str(item.get("time", "")), item["id"]))
        return found

    def object(self, sha):
        return os.path.join(self.store, "objects", sha[:2], sha)

    def walk(self):
        """Yield (full path, relative path) for every project file that is not ignored."""
        patterns = self.patterns()
        for folder, dirs, files in os.walk(self.root):
            rel_folder = self.rel(folder) or ""
            keep = []
            for name in dirs:
                rel = (rel_folder + "/" + name).lstrip("/")
                if not self.ignored(rel, patterns) and not os.path.islink(os.path.join(folder, name)):
                    keep.append(name)
            dirs[:] = keep
            for name in files:
                rel = (rel_folder + "/" + name).lstrip("/")
                full = os.path.join(folder, name)
                if not self.ignored(rel, patterns) and not os.path.islink(full):
                    yield full, rel


# --------------------------------------------------------------------------- reading a command

def split_command(text, ps):
    """Split a command line into segments of plain tokens. Quotes are removed; > and >> become markers."""
    segments, segment, cur = [], [], []
    state = {"has": False}
    n, i = len(text), 0

    def end_token():
        if state["has"]:
            segment.append("".join(cur))
        del cur[:]
        state["has"] = False

    def end_segment():
        end_token()
        if segment:
            segments.append(list(segment))
        del segment[:]

    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if c == "'":
            state["has"] = True
            i += 1
            while i < n:
                if text[i] == "'":
                    if ps and text[i + 1:i + 2] == "'":
                        cur.append("'")
                        i += 2
                        continue
                    break
                cur.append(text[i])
                i += 1
            i += 1
        elif c == '"':
            state["has"] = True
            i += 1
            while i < n and text[i] != '"':
                if ps and text[i] == "`" and i + 1 < n:
                    cur.append(text[i + 1])
                    i += 2
                elif not ps and text[i] == "\\" and text[i + 1:i + 2] in ('"', "\\", "$", "`"):
                    cur.append(text[i + 1])
                    i += 2
                else:
                    cur.append(text[i])
                    i += 1
            i += 1
        elif nxt and ((ps and c == "`") or (not ps and c == "\\")):
            if nxt != "\n":
                cur.append(nxt)
                state["has"] = True
            i += 2
        elif c in " \t\r":
            end_token()
            i += 1
        elif c in ";\n":
            end_segment()
            i += 1
        elif c == "|":
            end_segment()
            i += 2 if nxt == "|" else 1
        elif c == "&":
            if nxt == ">":
                end_token()
                i += 1
            else:
                end_segment()
                i += 2 if nxt == "&" else 1
        elif c == ">":
            if cur and all(ch.isdigit() or ch == "*" for ch in cur):
                del cur[:]
                state["has"] = False
            end_token()
            if nxt == ">":
                segment.append(APPEND)
                i += 2
            elif nxt == "&":
                i += 2
                while i < n and (text[i].isdigit() or text[i] == "-"):
                    i += 1
            else:
                segment.append(REDIR)
                i += 2 if nxt == "|" else 1
        elif c == "<":
            end_token()
            i += 1
        elif c in "()" or (ps and c in "{}") or (not ps and c == "`"):
            if c == "{" and cur and cur[-1] == "$":
                cur.append(c)
            else:
                if c == "(" and cur and cur[-1] in "$@":
                    cur.pop()
                    state["has"] = bool(cur)
                end_segment()
            i += 1
        else:
            cur.append(c)
            state["has"] = True
            i += 1
    end_segment()
    return segments


def expand(token, ps):
    text = token
    if ps:
        text = re.sub(r"\$env:(\w+)", lambda m: os.environ.get(m.group(1), m.group(0)), text, flags=re.I)
    text = os.path.expandvars(text)
    if text.startswith("~"):
        text = os.path.expanduser(text)
    if "$" in text or "`" in text:
        return None
    return text


def to_abs(token, cwd, ps):
    text = expand(token, ps)
    if text is None or not text:
        return None
    if os.name == "nt" and not ps:
        found = re.match(r"^/([a-zA-Z])(/|$)", text)
        if found:
            text = found.group(1) + ":/" + text[3:]
    return os.path.normpath(os.path.join(cwd, text))


def sh_args(args):
    options, positional, ended = [], [], False
    for arg in args:
        if ended or not arg.startswith("-") or arg == "-":
            positional.append(arg)
        elif arg == "--":
            ended = True
        else:
            options.append(arg)
    return options, positional


def ps_args(args):
    """PowerShell and cmd style arguments -> (named values, positional, switches)."""
    merged = []
    for arg in args:
        if merged and (merged[-1].endswith(",") or arg.startswith(",")):
            merged[-1] += arg
        else:
            merged.append(arg)
    named, positional, switches = {}, [], set()
    i = 0
    while i < len(merged):
        arg = merged[i]
        if re.match(r"^-[A-Za-z]", arg):
            name, _, value = arg[1:].partition(":")
            name = name.lower()
            full = next((item for item in PS_VALUE if item.startswith(name)), None)
            if full and not any(item.startswith(name) for item in PS_SWITCH):
                if not value and i + 1 < len(merged):
                    i += 1
                    value = merged[i]
                named.setdefault(full, []).append(value)
            else:
                switches.add(next((item for item in PS_SWITCH if item.startswith(name)), name))
        elif re.match(r"^/[A-Za-z?](:.*)?$", arg) and not os.path.lexists(arg):
            switches.add(arg.lower())
        else:
            positional.append(arg)
        i += 1
    return named, positional, switches


class Reader:
    """Collects the paths a command line may delete, move or overwrite. It reads; it runs nothing but git."""

    def __init__(self, proj):
        self.proj = proj
        self.found = []
        self.notes = []

    def read(self, text, ps, cwd, depth=0):
        for segment in split_command(text, ps):
            cwd = self.segment(segment, ps, cwd, depth)
        return cwd

    def paths(self, tokens, cwd, ps, literal=False):
        result = []
        for token in tokens:
            first = to_abs(token, cwd, ps)
            parts = [token]
            if ps and "," in token and not (first and os.path.lexists(first)):
                parts = [part for part in token.split(",") if part]
            for part in parts:
                path = to_abs(part, cwd, ps)
                if path is None:
                    self.notes.append("could not read a path with a variable in it: %s" % part)
                elif os.path.lexists(path) or literal or not re.search(r"[*?\[]", path):
                    result.append(path)
                else:
                    result.extend(glob.glob(path, recursive=True))
        return result

    def add(self, tokens, cwd, ps, files_only=False, literal=False):
        for path in self.paths(tokens, cwd, ps, literal):
            if os.path.lexists(path) and not (files_only and not os.path.isfile(path)):
                self.found.append(path)

    def into(self, sources, dest, cwd, ps):
        """What a move or copy would overwrite at the destination."""
        target = to_abs(dest, cwd, ps) if dest else None
        if not target:
            return
        if os.path.isfile(target):
            self.found.append(target)
        elif os.path.isdir(target):
            for source in self.paths(sources, cwd, ps):
                inside = os.path.join(target, os.path.basename(source.rstrip("\\/")))
                if os.path.lexists(inside):
                    self.found.append(inside)

    def segment(self, seg, ps, cwd, depth):
        rest, i = [], 0
        while i < len(seg):
            if seg[i] in (REDIR, APPEND):
                if seg[i] == REDIR and i + 1 < len(seg) and seg[i + 1].lower() not in ("$null", "nul", "/dev/null"):
                    self.add([seg[i + 1]], cwd, ps, files_only=True)
                i += 2
            else:
                rest.append(seg[i])
                i += 1
        while rest and (rest[0].lower() in SKIP or re.match(r"^\w+=", rest[0])):
            rest.pop(0)
        if not rest:
            return cwd
        verb = os.path.basename(rest[0].replace("\\", "/")).lower()
        if verb.endswith(".exe"):
            verb = verb[:-4]
        args = rest[1:]
        if ps:
            named, pos, switches = ps_args(args)
            opts = []
        else:
            opts, pos = sh_args(args)
            named, switches = {}, set()
        direct = named.get("path", []) + named.get("pspath", [])
        literal = named.get("literalpath", []) + named.get("lp", [])
        if "whatif" in switches:
            return cwd
        if verb in CD:
            where = (direct + literal + pos)[:1]
            target = to_abs(where[0], cwd, ps) if where else None
            return target if target and os.path.isdir(target) else cwd
        if depth < 3 and verb in SHELLS:
            for index, arg in enumerate(args[:-1]):
                if re.match(r"^-[a-z]*c$", arg):
                    self.read(args[index + 1], False, cwd, depth + 1)
                    break
        elif depth < 3 and verb in PSHELLS:
            for index, arg in enumerate(args[:-1]):
                if arg.lower() in ("-c", "-command"):
                    self.read(" ".join(args[index + 1:]), True, cwd, depth + 1)
                    break
        elif depth < 3 and verb == "cmd":
            for index, arg in enumerate(args[:-1]):
                if arg.lower() in ("/c", "/k"):
                    self.read(" ".join(args[index + 1:]), True, cwd, depth + 1)
                    break
        elif verb == "git":
            self.git(args, cwd)
        elif verb == "find" and not ps:
            self.find(args, cwd)
        elif verb == "sed" and not ps:
            if any(o.startswith("--in-place") or (not o.startswith("--") and "i" in o[1:]) for o in opts):
                self.add(pos, cwd, ps, files_only=True)
        elif verb in DELETE:
            self.add(direct + pos, cwd, ps)
            self.add(literal, cwd, ps, literal=True)
        elif verb in MOVE or verb in COPY:
            if ps:
                sources = direct + literal or pos[:1]
                dest = (named.get("destination") or pos[0 if direct + literal else 1:][:1] or [None])[0]
            else:
                sources, dest = pos[:-1], (pos[-1] if len(pos) > 1 else None)
            if verb in MOVE:
                self.add(sources, cwd, ps)
            self.into(sources, dest, cwd, ps)
        elif verb in RENAME and ps:
            self.add((direct + literal or pos)[:1], cwd, ps)
        elif verb in WRITE_PS and ps:
            if "append" not in switches:
                self.add(direct + literal + named.get("filepath", []) or pos[:1], cwd, ps, files_only=True)
        elif verb in WRITE_SH and not ps:
            if not (verb == "tee" and any(o == "--append" or (not o.startswith("--") and "a" in o) for o in opts)):
                self.add(pos, cwd, ps, files_only=True)
        return cwd

    def run_git(self, cwd, *args):
        try:
            done = subprocess.run(["git", "-c", "core.quotepath=off"] + list(args), cwd=cwd, timeout=15,
                                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout.decode("utf-8", "replace") if done.returncode == 0 else None

    def git(self, args, cwd):
        args = list(args)
        while args and args[0].startswith("-"):
            flag = args.pop(0)
            if flag in ("-C", "-c", "--git-dir", "--work-tree") and args:
                value = args.pop(0)
                if flag == "-C":
                    cwd = os.path.normpath(os.path.join(cwd, value))
        if not args or not os.path.isdir(cwd):
            return
        sub, rest = args[0], args[1:]
        if sub == "clean":
            flags = []
            for arg in rest:
                if re.match(r"^-[a-zA-Z]+$", arg):
                    if "n" in arg:
                        return
                    arg = "-" + arg[1:].replace("f", "")
                if arg in ("--dry-run",):
                    return
                if arg not in ("-", "--force"):
                    flags.append(arg)
            out = self.run_git(cwd, "clean", "-n", *flags)
            if out is None:
                self.notes.append("could not ask git what `git clean` would remove")
                return
            for line in out.splitlines():
                if line.startswith("Would remove "):
                    self.found.append(os.path.join(cwd, line[13:].strip().strip('"')))
        elif (sub == "reset" and "--hard" in rest) or sub in ("checkout", "restore"):
            paths = []
            if sub != "reset":
                if "--" in rest:
                    paths = rest[rest.index("--") + 1:]
                else:
                    paths = [a for a in rest if not a.startswith("-") and os.path.lexists(os.path.join(cwd, a))]
                if not paths:
                    return
            top = self.run_git(cwd, "rev-parse", "--show-toplevel")
            out = self.run_git(cwd, "diff", "--name-only", "-z", "HEAD", "--", *paths) if top else None
            if out is None:
                self.notes.append("could not ask git which files `git %s` would change" % sub)
                return
            for name in out.split("\0"):
                if name:
                    self.found.append(os.path.normpath(os.path.join(top.strip(), name)))

    def find(self, args, cwd):
        removes = "-delete" in args
        for index, arg in enumerate(args[:-1]):
            if arg in ("-exec", "-execdir", "-ok"):
                tool = os.path.basename(args[index + 1]).lower()
                removes = removes or tool in DELETE or tool in MOVE or tool == "truncate"
        if not removes:
            return
        starts = []
        for arg in args:
            if arg in ("-H", "-L", "-P"):
                continue
            if arg.startswith("-") or arg in ("(", ")", "!"):
                break
            starts.append(arg)
        names = [(args[i + 1], a == "-iname") for i, a in enumerate(args[:-1]) if a in ("-name", "-iname")]
        kind = next((args[i + 1] for i, a in enumerate(args[:-1]) if a == "-type"), None)
        if "!" in args or "-not" in args:
            names = []
        for start in starts or ["."]:
            base = to_abs(start, cwd, False)
            if not base or not os.path.lexists(base):
                continue
            if not names or not os.path.isdir(base):
                self.found.append(base)
                continue
            for folder, dirs, files in os.walk(base):
                dirs[:] = [d for d in dirs if d != DIR]
                pool = ([] if kind == "d" else files) + ([] if kind == "f" else dirs)
                for name in pool:
                    for pattern, loose in names:
                        if fnmatch.fnmatchcase(name.lower() if loose else name, pattern.lower() if loose else pattern):
                            self.found.append(os.path.join(folder, name))
                            break


# --------------------------------------------------------------------------- copies

def make_copy(proj, paths, command="", tool="", kind="copy", limited=True, use_ignore=True, base=None):
    """Copy the given project paths into a new folder under .folder-undo. Returns its manifest or None."""
    patterns = proj.patterns()
    files, dirs, seen = [], [], set()
    for path in sorted(set(paths)):
        rel = proj.rel(path)
        if rel is None or proj.own(rel) or not os.path.lexists(path):
            continue
        if os.path.islink(path):
            proj.log_skip("a link, not copied: %s" % rel)
        elif use_ignore and rel and proj.ignored(rel, patterns):
            proj.log_skip("on the ignore list, not copied: %s" % rel)
        elif os.path.isdir(path):
            for folder, names, leaves in os.walk(path):
                rel_folder = proj.rel(folder) or ""
                names[:] = [n for n in names if not os.path.islink(os.path.join(folder, n))
                            and not proj.own((rel_folder + "/" + n).lstrip("/"))
                            and not (use_ignore and proj.ignored((rel_folder + "/" + n).lstrip("/"), patterns))]
                if rel_folder:
                    dirs.append(rel_folder)
                for leaf in leaves:
                    leaf_rel = (rel_folder + "/" + leaf).lstrip("/")
                    full = os.path.join(folder, leaf)
                    if leaf_rel in seen or os.path.islink(full) or (use_ignore and proj.ignored(leaf_rel, patterns)):
                        continue
                    seen.add(leaf_rel)
                    files.append((full, leaf_rel))
        elif rel not in seen:
            seen.add(rel)
            files.append((path, rel))
    if not files and not dirs:
        return None
    total = 0
    for full, _ in files:
        try:
            total += os.path.getsize(full)
        except OSError:
            pass
    cap = proj.limit("copy_limit_mb")
    if limited and total > cap:
        proj.log_skip("over the copy limit (%s, limit %d MB), nothing copied | %s" % (human(total), cap // MB, short(command)))
        return None
    proj.ensure()
    stamp = now()
    ident = proj.uid(proj.dir, base or stamp.strftime("%Y%m%d-%H%M%S"), first=base is None)
    target = os.path.join(proj.dir, ident)
    done = []
    for full, rel in files:
        dst = os.path.join(target, "files", *rel.split("/"))
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            size, sha = copy_file(full, dst)
            done.append({"path": rel, "bytes": size, "sha256": sha})
        except OSError as error:
            proj.log_skip("could not copy %s: %s" % (rel, error))
    if not done and not dirs:
        shutil.rmtree(target, ignore_errors=True)
        return None
    os.makedirs(target, exist_ok=True)
    manifest = {"id": ident, "kind": kind, "time": stamp.strftime(TIME), "tool": tool, "command": command,
                "bytes": sum(item["bytes"] for item in done), "files": done, "dirs": sorted(set(dirs))}
    write_json(os.path.join(target, "manifest.json"), manifest)
    return manifest


def hook_pre(proj, data, guard=None):
    command = (data.get("tool_input") or {}).get("command")
    if not isinstance(command, str) or not command.strip():
        return 0
    tool = str(data.get("tool_name") or "")
    cwd = data.get("cwd") or proj.root
    if not os.path.isdir(cwd):
        cwd = proj.root
    reader = Reader(proj)
    reader.read(command, tool.lower() == "powershell", cwd)
    inside, outside = proj.sort(reader.found)
    if guard:
        code = guard(proj, command, outside)
        if code:
            return code
    for note in reader.notes:
        proj.log_skip("%s | %s" % (note, short(command)))
    if inside:
        make_copy(proj, inside, command=command, tool=tool)
    return 0


# --------------------------------------------------------------------------- restore point of the whole folder

def collect(proj):
    referenced = set()
    for session in proj.sessions():
        referenced.update(item["sha256"] for item in session["files"].values())
    objects = os.path.join(proj.store, "objects")
    for folder, _, files in os.walk(objects):
        for name in files:
            if name not in referenced:
                try:
                    os.remove(os.path.join(folder, name))
                except OSError:
                    pass


def drop_session(proj, ident):
    try:
        os.remove(os.path.join(proj.store, "sessions", ident + ".json"))
    except OSError:
        pass


def snapshot(proj, session_id="", source="", keep=1):
    """Keep a restore point of the whole folder. Returns (manifest or None, message or None)."""
    sessions = proj.sessions()
    if session_id and any(item.get("session_id") == session_id for item in sessions):
        return None, None
    proj.ensure()
    entries, total = [], 0
    for full, rel in proj.walk():
        try:
            info = os.stat(full)
        except OSError:
            continue
        entries.append((full, rel, info.st_size, info.st_mtime_ns))
        total += info.st_size
    cap = proj.limit("snapshot_limit_mb")
    if total > cap:
        text = ("folder-undo: no restore point was made for this session. The folder is %s and the limit is "
                "%d MB (snapshot_limit_mb in %s/config.json)." % (human(total), cap // MB, DIR))
        proj.log_skip(text)
        return None, text
    previous = sessions[-1]["files"] if sessions else {}
    objects = os.path.join(proj.store, "objects")
    os.makedirs(os.path.join(proj.store, "sessions"), exist_ok=True)
    os.makedirs(objects, exist_ok=True)
    files = {}
    for full, rel, size, mtime in entries:
        old = previous.get(rel)
        if old and old.get("bytes") == size and old.get("mtime_ns") == mtime and os.path.exists(proj.object(old["sha256"])):
            files[rel] = dict(old)
            continue
        temp = os.path.join(objects, "incoming-%d" % os.getpid())
        try:
            size, sha = copy_file(full, temp)
            target = proj.object(sha)
            if os.path.exists(target):
                os.remove(temp)
            else:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                os.replace(temp, target)
            files[rel] = {"sha256": sha, "bytes": size, "mtime_ns": mtime}
        except OSError as error:
            proj.log_skip("not in the restore point, could not read %s: %s" % (rel, error))
            try:
                os.remove(temp)
            except OSError:
                pass
    stamp = now()
    ident = proj.uid(os.path.join(proj.store, "sessions"), stamp.strftime("%Y%m%d-%H%M%S"), first=False)
    manifest = {"id": ident, "session_id": session_id, "source": source, "time": stamp.strftime(TIME),
                "bytes": sum(item["bytes"] for item in files.values()), "files": files}
    write_json(os.path.join(proj.store, "sessions", ident + ".json"), manifest)
    for old in proj.sessions()[:-max(1, int(keep))]:
        drop_session(proj, old["id"])
    collect(proj)
    return manifest, None


def hook_session(proj, data, keep=1):
    if data.get("source") == "compact":
        return None
    return snapshot(proj, str(data.get("session_id") or ""), str(data.get("source") or ""), keep)[1]


# --------------------------------------------------------------------------- free edition: one restore per PC

RUN = {"pro": False, "edition": None}


def pro_here():
    """True when this run is Folder Undo Pro: started from folder_undo_pro.py, or that file sits next to this one."""
    forced = os.environ.get("FOLDER_UNDO_EDITION", "").lower()
    if forced in ("free", "pro"):
        return forced == "pro"
    return RUN["pro"] or os.path.isfile(os.path.join(os.path.dirname(os.path.abspath(__file__)), "folder_undo_pro.py"))


def usage_file():
    folder = os.environ.get("FOLDER_UNDO_STATE_DIR")
    if not folder:
        if os.name == "nt":
            base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
            folder = os.path.join(base, "folder-undo")
        else:
            folder = os.path.join(os.path.expanduser("~"), ".local", "state", "folder-undo")
    return os.path.join(folder, "usage.json")


def restores_used():
    """How many restores this PC has done. A file that is missing or cannot be read counts as 0."""
    data = read_json(usage_file(), {})
    try:
        return max(0, int(data.get("restores", 0))) if isinstance(data, dict) else 0
    except (TypeError, ValueError):
        return 0


def count_restore():
    path = usage_file()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        write_json(path, {"restores": restores_used() + 1})
    except OSError:
        pass


def pro_hint(proj):
    return "Your files are still saved in %s. Folder Undo Pro restores them in one step: %s\n" % (proj.dir, PRO_URL)


# --------------------------------------------------------------------------- restore

def agreed(args, io, question):
    if getattr(args, "dry_run", False):
        io.out.write("\nDry run: nothing was written.\n")
        return False
    try:
        answer = io.ask("\n%s Type y to go ahead, anything else to cancel: " % question)
    except EOFError:
        answer = ""
    if answer.strip().lstrip("\ufeff").lower() not in ("y", "yes"):
        io.out.write("Cancelled. Nothing was written.\n")
        return False
    return True


def wanted(proj, rel, filters):
    if not filters:
        return True
    fold = (lambda text: text.lower()) if os.name == "nt" else (lambda text: text)
    low = fold(rel)
    for group in filters:
        for item in group:
            item = fold(item)
            if low == item or low.startswith(item.rstrip("/") + "/") or item == "":
                return True
    return False


def plan_rows(proj, items):
    rows = []
    for rel, source, size, sha in items:
        target = os.path.join(proj.root, *rel.split("/"))
        if not os.path.lexists(target):
            state = "bring back"
        elif os.path.isfile(target) and os.path.getsize(target) == size and sha_of(target) == sha:
            state = "same"
        else:
            state = "overwrite"
        rows.append({"path": rel, "source": source, "bytes": size, "state": state, "target": target})
    return rows


def write_rows(proj, rows, io, label):
    """Write the planned files. The version on disk now is kept first; a failed file leaves its original alone."""
    todo = [row for row in rows if row["state"] != "same"]
    over = [row["target"] for row in todo if row["state"] == "overwrite"]
    kept = None
    if over:
        kept = make_copy(proj, over, command="restore from " + label, tool=PROG, kind="before-restore", limited=False,
                         use_ignore=False, base="before-restore-" + now().strftime("%Y%m%d-%H%M%S"))
        if not kept or len(kept["files"]) < len([p for p in over if os.path.isfile(p)]):
            raise Stop("could not keep the current version of the files first, so nothing was changed.")
    done, failed = 0, []
    for row in todo:
        temp = row["target"] + ".folder-undo-tmp"
        try:
            os.makedirs(os.path.dirname(row["target"]), exist_ok=True)
            copy_file(row["source"], temp)
            os.replace(temp, row["target"])
            done += 1
        except OSError as error:
            failed.append((row["path"], error))
            try:
                os.remove(temp)
            except OSError:
                pass
    if done and not pro_here():
        count_restore()
    io.out.write("\nWritten: %d file%s.\n" % (done, "" if done == 1 else "s"))
    if kept:
        io.out.write("The versions that were on disk are kept in %s/%s (restore %s puts them back).\n"
                     % (DIR, kept["id"], kept["id"]))
    for path, error in failed:
        io.out.write("NOT written, left as it was: %s (%s)\n" % (path, error))
    return 1 if failed else 0


def cmd_restore(proj, args, io):
    if not args.dry_run and not pro_here() and restores_used() >= FREE_RESTORES:
        io.out.write(pro_hint(proj))
        return 3
    words = list(args.what)
    filters_from = words
    extra = 0
    if args.session is not None:
        sessions = proj.sessions()
        chosen = args.session
        if chosen and not any(item["id"] == chosen for item in sessions):
            words.insert(0, chosen)
            chosen = ""
        if not sessions:
            raise Stop("there is no restore point yet. One is made when a session starts with the hooks on.")
        snap = sessions[-1] if not chosen else next(item for item in sessions if item["id"] == chosen)
        items = [(rel, proj.object(v["sha256"]), v["bytes"], v["sha256"]) for rel, v in sorted(snap["files"].items())]
        label = "restore point %s" % snap["id"]
        title = "Restore point %s, the folder as it was at %s" % (snap["id"], snap["time"])
        if not words:
            extra = len([1 for _, rel in proj.walk() if rel not in snap["files"]])
    else:
        if not words:
            raise Stop("give the id of a copy (see `%s list`), or use --session." % PROG)
        ident, filters_from = words[0], words[1:]
        base = os.path.join(proj.dir, ident)
        manifest = read_json(os.path.join(base, "manifest.json"))
        if not isinstance(manifest, dict):
            raise Stop("there is no copy with the id %s. See `%s list`." % (ident, PROG))
        items = [(f["path"], os.path.join(base, "files", *f["path"].split("/")), f["bytes"], f["sha256"])
                 for f in manifest.get("files", [])]
        label = "copy %s" % ident
        title = "Copy %s, made %s" % (ident, manifest.get("time", "?"))
    filters = []
    for word in filters_from:
        group = {word.replace("\\", "/").strip("/")}
        rel = proj.rel(os.path.join(os.getcwd(), word))
        if rel is not None:
            group.add(rel)
        filters.append(group)
    items = [item for item in items if wanted(proj, item[0], filters)]
    if not items:
        raise Stop("nothing in %s matches that." % label)
    rows = plan_rows(proj, items)
    io.out.write(title + "\n")
    for row in rows:
        io.out.write("  %-10s  %s  (%s)\n" % (row["state"], row["path"], human(row["bytes"])))
    todo = [row for row in rows if row["state"] != "same"]
    over = len([row for row in todo if row["state"] == "overwrite"])
    io.out.write("%d to write, %d already the same.\n" % (len(todo), len(rows) - len(todo)))
    if over:
        io.out.write("%d file%s on disk would be overwritten. The current version is kept first in %s/before-restore-<time>.\n"
                     % (over, "" if over == 1 else "s", DIR))
    if extra:
        io.out.write("%d file%s made after this restore point stay%s as they are.\n"
                     % (extra, "" if extra == 1 else "s", "s" if extra == 1 else ""))
    if not todo:
        io.out.write("Nothing to do.\n")
        return 0
    if not agreed(args, io, "Write these files?"):
        return 0
    return write_rows(proj, rows, io, label)


# --------------------------------------------------------------------------- list, show, prune

def cmd_list(proj, args, io):
    sessions, copies = proj.sessions(), proj.copies()
    io.out.write("Restore points (the whole folder, made when a session starts)\n")
    for item in sessions:
        io.out.write("  %-22s %s  %5d files  %s\n" % (item["id"], item["time"], len(item["files"]), human(item.get("bytes", 0))))
    if not sessions:
        io.out.write("  none yet\n")
    io.out.write("\nCopies (made right before a command)\n")
    for item in copies:
        io.out.write("  %-34s %s  %3d files  %-9s  %s\n" % (item["id"], item.get("time", "?"), len(item["files"]),
                                                          human(item.get("bytes", 0)), short(item.get("command", ""), 60)))
    if not copies:
        io.out.write("  none yet\n")
    return 0


def cmd_show(proj, args, io):
    manifest = read_json(os.path.join(proj.dir, args.id, "manifest.json"))
    if isinstance(manifest, dict):
        io.out.write("Copy %s (%s)\nMade:    %s\nTool:    %s\nCommand: %s\n" % (
            args.id, manifest.get("kind", "copy"), manifest.get("time", "?"), manifest.get("tool", ""), manifest.get("command", "")))
        for item in manifest.get("files", []):
            io.out.write("  %s  %s  sha256 %s\n" % (item["path"], human(item["bytes"]), item["sha256"][:12]))
        for item in manifest.get("dirs", []):
            io.out.write("  %s/  (folder)\n" % item)
        return 0
    snap = next((item for item in proj.sessions() if item["id"] == args.id), None)
    if not snap:
        raise Stop("there is no copy or restore point with the id %s. See `%s list`." % (args.id, PROG))
    io.out.write("Restore point %s\nMade:  %s\nFiles: %d, %s\n" % (snap["id"], snap["time"], len(snap["files"]), human(snap.get("bytes", 0))))
    for rel, item in sorted(snap["files"].items()):
        io.out.write("  %s  %s  sha256 %s\n" % (rel, human(item["bytes"]), item["sha256"][:12]))
    return 0


def remove_copies(proj, victims, args, io):
    if not victims:
        io.out.write("Nothing to remove.\n")
        return 0
    io.out.write("These copies would be removed:\n")
    for item in victims:
        io.out.write("  %-34s %s  %s\n" % (item["id"], item.get("time", "?"), human(item.get("bytes", 0))))
    if not agreed(args, io, "Remove %d cop%s?" % (len(victims), "y" if len(victims) == 1 else "ies")):
        return 0
    for item in victims:
        shutil.rmtree(os.path.join(proj.dir, item["id"]), ignore_errors=True)
    io.out.write("Removed %d.\n" % len(victims))
    return 0


def cmd_prune(proj, args, io):
    if args.keep is None or args.keep < 0:
        raise Stop("say how many copies to keep: prune --keep N")
    copies = proj.copies()
    return remove_copies(proj, copies[:max(0, len(copies) - args.keep)], args, io)


# --------------------------------------------------------------------------- install, uninstall

def quoted(path):
    return '"%s"' % os.path.abspath(path).replace("\\", "/")


def program_path(python):
    """The interpreter as one program path, when --python names a file (quoted or not); otherwise None."""
    text = (python or "").strip()
    if len(text) > 1 and text[0] == text[-1] == '"':
        text = text[1:-1]
    return os.path.abspath(text).replace("\\", "/") if text and os.path.isfile(text) else None


def hook_groups(python, script, project, program=None):
    """The two hook handlers. With a program path they use exec form: Claude Code starts that program with these
    arguments and no shell, so the hooks run the same with Git Bash, with PowerShell (Windows without Git for
    Windows) and on macOS and Linux. A command name such as `python` keeps the shell form."""
    groups = []
    for event, extra, name, timeout in (("PreToolUse", {"matcher": "Bash|PowerShell"}, "pre", 60),
                                        ("SessionStart", {"matcher": "startup|resume|clear"}, "session-start", 120)):
        if program:
            hook = {"type": "command", "command": program,
                    "args": [os.path.abspath(script).replace("\\", "/"), "--project",
                             os.path.abspath(project).replace("\\", "/"), "hook", name],
                    "timeout": timeout}
        else:
            hook = {"type": "command", "command": "%s %s --project %s hook %s" % (python, quoted(script), quoted(project), name),
                    "timeout": timeout}
        groups.append((event, extra, hook))
    return tuple(groups)


def hook_text(hook):
    """A hook handler as one line: the command and, in exec form, its arguments."""
    if not isinstance(hook, dict):
        return ""
    args = hook.get("args")
    return " ".join([str(hook.get("command", ""))] + ([str(a) for a in args] if isinstance(args, list) else []))


def parse_settings(text):
    data = json.loads(text) if text.strip() else {}
    if not isinstance(data, dict) or not isinstance(data.get("hooks", {}), dict):
        raise ValueError("not a settings object")
    return data


def with_hooks(data, groups):
    data = copy.deepcopy(data)
    hooks = data.setdefault("hooks", {})
    for event, extra, hook in groups:
        current = hooks.setdefault(event, [])
        if not isinstance(current, list):
            raise ValueError("hooks.%s is not a list" % event)
        present = any(h.get("command") == hook["command"] and h.get("args") == hook.get("args")
                      for g in current if isinstance(g, dict) for h in g.get("hooks", []) if isinstance(h, dict))
        if not present:
            current.append(dict(extra, hooks=[dict(hook)]))
    return data


def without_hooks(data, mark):
    data = copy.deepcopy(data)
    hooks = data.get("hooks", {})
    for event in list(hooks):
        if not isinstance(hooks[event], list):
            continue
        kept = []
        for group in hooks[event]:
            if isinstance(group, dict) and isinstance(group.get("hooks"), list):
                inner = [h for h in group["hooks"] if not (isinstance(h, dict) and mark in hook_text(h))]
                if not inner:
                    continue
                group = dict(group, hooks=inner)
            kept.append(group)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if "hooks" in data and not data["hooks"]:
        del data["hooks"]
    return data


MARKS = ("folder_undo.py", "folder_undo_pro.py")
PRO_MARK = MARKS[1]


def our_commands(data):
    """The folder-undo hook commands already in a settings object, from any copy of the free or the Pro script."""
    found = []
    for groups in data.get("hooks", {}).values():
        for group in groups if isinstance(groups, list) else []:
            inner = group.get("hooks") if isinstance(group, dict) else None
            for hook in inner if isinstance(inner, list) else []:
                command = hook_text(hook)
                if any(mark in command for mark in MARKS):
                    found.append(command)
    return found


def read_bytes(path):
    with open(path, "rb") as handle:
        return handle.read()


def write_bytes(path, data):
    with open(path, "wb") as handle:
        handle.write(data)


def show_change(io, old, new):
    io.out.write("Change:\n")
    lines = difflib.unified_diff(old.decode("utf-8-sig").splitlines(), new.decode("utf-8-sig").splitlines(),
                                 "current", "new", lineterm="", n=2)
    io.out.write("\n".join(lines) + "\n")


def settings_paths(proj, home):
    target = os.path.join(proj.root, ".claude", "settings.json")
    user_wide = os.path.join(os.path.abspath(home or os.path.expanduser("~")), ".claude", "settings.json")
    if os.path.normcase(os.path.realpath(target)) == os.path.normcase(os.path.realpath(user_wide)):
        raise Stop("that is the user-wide settings file (%s). This tool only changes a project's own file. "
                   "Give the project folder with --project." % user_wide)
    return target, os.path.join(proj.root, ".claude", NOTE)


def cmd_install(proj, args, io, script, home=None):
    target, note_path = settings_paths(proj, home)
    old = read_bytes(target) if os.path.exists(target) else b""
    python = args.python or ("python" if os.name == "nt" else "python3")
    program = program_path(args.python) if args.python else None
    portable = RUN["edition"].portable() if RUN.get("edition") and not args.python else None
    if portable:
        program, script = os.path.abspath(portable["python"]).replace("\\", "/"), portable["script"]
    try:
        data = parse_settings(old.decode("utf-8-sig"))
        groups = hook_groups(python, script, proj.root, program)
        new = with_hooks(data, groups)
        earlier = our_commands(data) if new != data else []
        if earlier:
            if not RUN.get("edition") and any(PRO_MARK in command for command in earlier):
                raise Stop("Folder Undo Pro hooks are already on in this project. Pro does everything the free "
                           "version does, so nothing was changed.")
            base = data
            for mark in MARKS:
                base = without_hooks(base, mark)
            new = with_hooks(base, groups)
    except ValueError:
        raise Stop("%s is not valid JSON settings. Nothing was written. Fix or move that file first." % target)
    io.out.write("File: %s\n" % target)
    if new == data:
        io.out.write("Nothing to change: these hooks are already on.\n")
        return 0
    folder = os.path.dirname(target)
    first = read_json(note_path) if earlier and os.path.exists(note_path) else None
    keep_first = (isinstance(first, dict) and first.get("written_sha256") == hashlib.sha256(old).hexdigest()
                  and (not first.get("copy") or os.path.exists(os.path.join(folder, str(first["copy"])))))
    backup = target + ".bak-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    if earlier:
        kind = "Folder Undo Pro" if any(PRO_MARK in command for command in earlier) else "free folder-undo"
        io.out.write("This project already has %s hooks. They are taken out and the ones below are put in. The "
                     "copies and restore points in %s stay and are used as before.\n" % (kind, DIR))
    if keep_first:
        io.out.write("Copy of the file from before folder-undo was first turned on: %s\n"
                     % (os.path.join(folder, first["copy"]) if first.get("copy") else "(there was no file)"))
    else:
        io.out.write("Copy of the current file: %s\n" % (backup if old else "(there is no current file)"))
    new_bytes = (json.dumps(new, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    show_change(io, old, new_bytes)
    ignore_file = os.path.join(proj.root, ".gitignore")
    lines = read_bytes(ignore_file).decode("utf-8-sig", "replace").splitlines() if os.path.exists(ignore_file) else []
    offer = GITIGNORE_LINE not in [line.strip() for line in lines] and (lines or os.path.isdir(os.path.join(proj.root, ".git")))
    if portable:
        io.out.write("Folder Undo Pro and its own Python are copied to %s, and the hooks run from there. "
                     "The Python on this PC is not used or changed.\n" % portable["folder"])
    if offer and args.dry_run:
        io.out.write("You would also be asked whether to add %s to .gitignore.\n" % GITIGNORE_LINE)
    if not agreed(args, io, "Write this change?"):
        return 0
    if portable:
        try:
            portable["copy"]()
        except OSError as error:
            raise Stop("could not copy Folder Undo Pro to %s (%s). Nothing was changed." % (portable["folder"], error))
    created = not os.path.isdir(folder)
    os.makedirs(folder, exist_ok=True)
    if old and not keep_first:
        shutil.copy2(target, backup)
    write_bytes(target, new_bytes)
    json.loads(read_bytes(target).decode("utf-8-sig"))
    written = hashlib.sha256(new_bytes).hexdigest()
    if keep_first:          # uninstall still goes back to the file from before the first install
        note = {"copy": first.get("copy"), "created_folder": bool(first.get("created_folder")), "written_sha256": written}
    elif earlier:           # the file was changed by hand: uninstall takes out only the folder-undo hooks
        note = {"copy": None, "created_folder": False, "written_sha256": None}
    else:
        note = {"copy": os.path.basename(backup) if old else None, "created_folder": created, "written_sha256": written}
    if portable:
        note["portable_folder"] = portable["folder"]
    write_json(note_path, note)
    proj.ensure()
    io.out.write("\nWritten and read back. The hooks load when the next session starts in this project.\n")
    if offer:
        try:
            answer = io.ask("Add %s to .gitignore so the copies are not committed? Type y to add, anything else to skip: " % GITIGNORE_LINE)
        except EOFError:
            answer = ""
        if answer.strip().lstrip("\ufeff").lower() in ("y", "yes"):
            current = read_bytes(ignore_file) if os.path.exists(ignore_file) else b""
            newline = b"\r\n" if b"\r\n" in current else b"\n"
            gap = b"" if not current or current.endswith(b"\n") else newline
            write_bytes(ignore_file, current + gap + GITIGNORE_LINE.encode("ascii") + newline)
            io.out.write("Added to .gitignore.\n")
        else:
            io.out.write(".gitignore was left as it is.\n")
    return 0


def cmd_uninstall(proj, args, io, script, home=None):
    target, note_path = settings_paths(proj, home)
    folder = os.path.dirname(target)
    io.out.write("File: %s\n" % target)
    if not os.path.exists(target):
        io.out.write("Nothing to change: there is no settings file.\n")
        return 0
    old = read_bytes(target)
    note = read_json(note_path) if os.path.exists(note_path) else None
    exact = isinstance(note, dict) and hashlib.sha256(old).hexdigest() == note.get("written_sha256")
    copy_path = os.path.join(folder, note["copy"]) if exact and note.get("copy") else None
    if copy_path and not os.path.exists(copy_path):
        exact = False
    if exact:
        new_bytes = read_bytes(copy_path) if copy_path else b""
        io.out.write("The file goes back to exactly what it was before the hooks were turned on%s.\n"
                     % (" (from %s)" % os.path.basename(copy_path) if copy_path else ": it did not exist, so it is removed"))
    else:
        try:
            data = parse_settings(old.decode("utf-8-sig"))
        except ValueError:
            raise Stop("%s is not valid JSON settings. Nothing was written." % target)
        new = without_hooks(data, os.path.basename(script))
        if new == data:
            io.out.write("Nothing to change: no folder-undo hooks in it.\n")
            return 0
        new_bytes = (json.dumps(new, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        io.out.write("The file was changed after the hooks were turned on (or they were added by hand), so only the "
                     "folder-undo hooks are taken out and everything else stays.\n")
    show_change(io, old, new_bytes)
    if not agreed(args, io, "Write this change?"):
        return 0
    if exact:
        if copy_path:
            write_bytes(target, new_bytes)
            os.remove(copy_path)
        else:
            os.remove(target)
        os.remove(note_path)
        if note.get("created_folder") and not os.listdir(folder):
            os.rmdir(folder)
    else:
        write_bytes(target, new_bytes)
        json.loads(read_bytes(target).decode("utf-8-sig"))
        if os.path.exists(note_path):
            os.remove(note_path)
    io.out.write("\nDone. The hooks are off for this project. The %s folder with your copies, and any %s line in "
                 ".gitignore, are left as they are.\n" % (DIR, GITIGNORE_LINE))
    if isinstance(note, dict) and note.get("portable_folder"):
        io.out.write("The copy of Folder Undo Pro and its Python stays in %s. Your saved copies can still be put back "
                     "from there, and you can delete that folder whenever you like.\n" % note["portable_folder"])
    return 0


# --------------------------------------------------------------------------- command line

class IO:
    def __init__(self, out, ask):
        self.out, self.ask = out, ask


def ask_line(prompt):
    """input(), but the answer is read as bytes. Windows PowerShell 5.1 can put a UTF-8 byte order mark in
    front of a piped "y", and a console whose code page is not UTF-8 would then misread the whole line."""
    sys.stdout.write(prompt)
    sys.stdout.flush()
    stream = getattr(sys.stdin, "buffer", None)
    if stream is None:
        return input()
    raw = stream.readline()
    if not raw:
        raise EOFError
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode(locale.getpreferredencoding(False) or "utf-8", "replace")
    return text.rstrip("\r\n")


def build_parser(edition=None):
    prog = edition.prog if edition else PROG
    parser = argparse.ArgumentParser(prog=prog, allow_abbrev=False, description=__doc__.split("\n")[0])
    parser.add_argument("--version", action="version", version="%s %s" % (prog, edition.version if edition else VERSION))
    parser.add_argument("--project", help="the project folder (default: the nearest folder above here that has %s, else here)" % DIR)
    sub = parser.add_subparsers(dest="command", required=True)
    cmds = {}
    cmds["hook"] = sub.add_parser("hook", help="what the installed hooks run; reads the hook JSON on stdin")
    cmds["hook"].add_argument("event", choices=("pre", "session-start"))
    cmds["list"] = sub.add_parser("list", help="what is kept")
    cmds["show"] = sub.add_parser("show", help="one copy or restore point in detail")
    cmds["show"].add_argument("id")
    cmds["restore"] = sub.add_parser("restore", help="put files back: shows the plan, then asks")
    cmds["restore"].add_argument("what", nargs="*", help="a copy id, then optional paths; with --session only paths")
    cmds["restore"].add_argument("--session", nargs="?", const="", default=None, metavar="ID",
                                 help="use the restore point made when the session started")
    cmds["restore"].add_argument("--dry-run", action="store_true", help="show the plan and stop")
    cmds["prune"] = sub.add_parser("prune", help="remove old copies")
    cmds["prune"].add_argument("--keep", type=int, help="keep the newest N copies")
    cmds["prune"].add_argument("--dry-run", action="store_true", help="show what would be removed and stop")
    for name in ("install", "uninstall"):
        cmds[name] = sub.add_parser(name, help="turn the hooks %s for one project" % ("on" if name == "install" else "off"))
        cmds[name].add_argument("--dry-run", action="store_true", help="show the change and stop. Nothing is asked or written")
        cmds[name].add_argument("--python", default=None, help="the interpreter the hooks are run with "
                                "(default: python, or python3 outside Windows)")
    if edition:
        edition.extend(sub, cmds)
    return parser


def find_root(given):
    if given:
        return given
    here = os.getcwd()
    probe = here
    while True:
        if os.path.isdir(os.path.join(probe, DIR)):
            return probe
        parent = os.path.dirname(probe)
        if parent == probe:
            return here
        probe = parent


def run_hook(args, edition, stdin):
    raw = (stdin or sys.stdin.buffer).read()
    data = json.loads(raw.decode("utf-8-sig", "replace") or "{}")
    if not isinstance(data, dict):
        return 0
    root = args.project or os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd()
    if not os.path.isdir(root):
        return 0
    proj = Project(root)
    if args.event == "pre":
        return hook_pre(proj, data, guard=edition.guard if edition else None)
    message = hook_session(proj, data, keep=edition.keep(proj) if edition else 1)
    if edition:
        edition.after_snapshot(proj)
    if message:
        sys.stdout.write(json.dumps({"systemMessage": message}) + "\n")
    return 0


def main(argv=None, edition=None, home=None, out=None, ask=None, stdin=None):
    soft_stdio()
    RUN["edition"], RUN["pro"] = edition, edition is not None
    argv = list(sys.argv[1:] if argv is None else argv)
    if "hook" in argv:
        try:
            return run_hook(build_parser(edition).parse_args(argv), edition, stdin)
        except BaseException:
            return 0
    args = build_parser(edition).parse_args(argv)
    io = IO(out or sys.stdout, ask or ask_line)
    script = edition.script if edition else os.path.abspath(__file__)
    try:
        root = find_root(args.project)
        if not os.path.isdir(root):
            raise Stop("the project folder does not exist: %s" % root)
        proj = Project(root)
        if args.command in ("install", "uninstall"):
            return (cmd_install if args.command == "install" else cmd_uninstall)(proj, args, io, script, home)
        handlers = {"list": cmd_list, "show": cmd_show, "restore": cmd_restore, "prune": cmd_prune}
        if edition:
            handlers.update(edition.handlers())
        return handlers[args.command](proj, args, io)
    except Stop as error:
        sys.stderr.write("stopped: %s\n" % error)
        return 3


if __name__ == "__main__":
    sys.exit(main())
