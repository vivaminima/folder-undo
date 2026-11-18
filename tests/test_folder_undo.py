"""Tests for folder_undo.py. Run: python -m unittest discover -s tests

Files are really deleted, moved and overwritten here (inside temporary folders) and then put back.
Non-ASCII names are written as \\u escapes so that this file stays plain ASCII.
"""
import errno
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.dirname(HERE)
if SRC not in sys.path:
    sys.path.insert(0, SRC)
import folder_undo as fu  # noqa: E402

SCRIPT = os.path.join(SRC, "folder_undo.py")
ODD = "caf\u00e9 \ub178\ud2b8.md"          # e-acute, a space and two Hangul syllables
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")
GIT = shutil.which("git")


def find_bash():
    for path in (shutil.which("bash"), os.path.join(os.environ.get("ProgramFiles", ""), "Git", "bin", "bash.exe")):
        if path and os.path.exists(path) and "system32" not in path.lower():
            return path
    return None


BASH = find_bash()


class Base(unittest.TestCase):
    script = SCRIPT
    module = fu

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="fu-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = os.path.join(self.tmp, "my project")
        self.away = os.path.join(self.tmp, "outside")
        os.makedirs(self.root)
        os.makedirs(self.away)
        self.proj = fu.Project(self.root)
        os.environ.pop("FOLDER_UNDO_NOW", None)

    def put(self, rel, text="one\n", base=None):
        path = os.path.join(base or self.root, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(text.encode("utf-8"))
        return path

    def text(self, rel):
        with open(os.path.join(self.root, *rel.split("/")), "rb") as handle:
            return handle.read().decode("utf-8")

    def there(self, rel):
        return os.path.exists(os.path.join(self.root, *rel.split("/")))

    def payload(self, command, tool, cwd=None):
        return {"hook_event_name": "PreToolUse", "session_id": "s1", "cwd": cwd or self.root,
                "tool_name": tool, "tool_input": {"command": command}}

    def pre(self, command, tool="PowerShell", cwd=None):
        """Run the reader in this process. Returns the set of copied relative paths (all copies so far)."""
        code = fu.hook_pre(self.proj, self.payload(command, tool, cwd))
        self.assertEqual(code, 0)
        return self.copied()

    def copied(self):
        return {item["path"] for manifest in self.proj.copies() for item in manifest["files"]}

    def hook(self, event, data, extra=()):
        raw = data if isinstance(data, bytes) else json.dumps(data).encode("utf-8")
        return subprocess.run([sys.executable, "-I", self.script, "--project", self.root, "hook", event] + list(extra),
                              input=raw, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)

    def cli(self, *argv, answers=(), home=None):
        out, replies = io.StringIO(), list(answers)
        asked = []

        def ask(question):
            asked.append(question)
            return replies.pop(0) if replies else ""

        code = self.module.main(["--project", self.root] + list(argv), out=out, ask=ask,
                                home=home or os.path.join(self.tmp, "home"))
        self.asked = asked
        return code, out.getvalue()

    def really(self, command, tool, by_hand=None):
        """Feed the command to the hook as a separate process, then really run it in that shell.

        Where bash is not installed, `by_hand` does to the files what the Bash command would have done."""
        done = self.hook("pre", self.payload(command, tool))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout, b"")
        if tool == "Bash" and not BASH:
            return by_hand()
        if tool == "PowerShell":
            argv = [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", command]
        else:
            argv = [BASH, "-c", command]
        ran = subprocess.run(argv, cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        self.assertEqual(ran.returncode, 0, ran.stderr)

    def restore_newest(self):
        newest = [item for item in self.proj.copies() if item["kind"] == "copy"][-1]
        code, text = self.cli("restore", newest["id"], answers=["y"])
        self.assertEqual(code, 0, text)
        return text


class PowerShellReadTest(Base):
    def test_remove_item_copies_the_file_with_a_manifest(self):
        self.put("notes/a.md", "alpha\n")
        self.assertEqual(self.pre("Remove-Item notes\\a.md"), {"notes/a.md"})
        manifest = self.proj.copies()[0]
        self.assertEqual(manifest["command"], "Remove-Item notes\\a.md")
        self.assertEqual(manifest["files"][0]["bytes"], 6)
        self.assertEqual(manifest["files"][0]["sha256"], fu.sha_of(os.path.join(self.root, "notes", "a.md")))
        kept = os.path.join(self.proj.dir, manifest["id"], "files", "notes", "a.md")
        with open(kept, "rb") as handle:
            self.assertEqual(handle.read(), b"alpha\n")

    def test_aliases_and_cmd_forms(self):
        for index, form in enumerate(("del %s", "erase %s", "rm %s", "ri %s", "rd %s", "Remove-Item -Path %s -Force")):
            name = "f%d.txt" % index
            self.put(name)
            self.assertIn(name, self.pre(form % name), form)
        self.put("old/deep/x.txt")
        self.assertIn("old/deep/x.txt", self.pre("rmdir /s /q old"))

    def test_recurse_copies_the_whole_folder(self):
        self.put("build/a.txt")
        self.put("build/sub/b.txt")
        self.assertEqual(self.pre("Remove-Item -Recurse -Force .\\build"), {"build/a.txt", "build/sub/b.txt"})

    def test_wildcard_comma_list_and_literal_path(self):
        self.put("a.md")
        self.put("b.md")
        self.put("c.txt")
        self.put("d [1].txt")
        self.assertEqual(self.pre("Remove-Item *.md"), {"a.md", "b.md"})
        self.assertIn("c.txt", self.pre("Remove-Item a.md, c.txt"))
        self.assertIn("d [1].txt", self.pre("Remove-Item -LiteralPath 'd [1].txt'"))

    def test_space_and_non_ascii_names(self):
        self.put("my notes/" + ODD)
        self.assertEqual(self.pre('Remove-Item "my notes\\%s"' % ODD), {"my notes/" + ODD})

    def test_move_rename_and_copy_over(self):
        self.put("a.txt", "a")
        self.put("b.txt", "b")
        self.put("dir/a.txt", "inner")
        self.assertEqual(self.pre("Move-Item a.txt b.txt -Force"), {"a.txt", "b.txt"})
        self.assertIn("dir/a.txt", self.pre("Copy-Item -Path a.txt -Destination dir -Force"))
        self.put("c.txt")
        self.assertIn("c.txt", self.pre("Rename-Item c.txt d.txt"))

    def test_writers_copy_only_a_file_that_exists(self):
        self.put("a.txt")
        self.put("b.txt")
        self.put("c.txt")
        self.put("d.txt")
        self.assertEqual(self.pre("Set-Content -Path a.txt -Value 'new'"), {"a.txt"})
        self.assertIn("b.txt", self.pre("Get-Date | Out-File b.txt"))
        self.assertIn("c.txt", self.pre("Clear-Content c.txt"))
        self.assertIn("d.txt", self.pre("echo hi > d.txt"))
        before = len(self.proj.copies())
        self.pre("Set-Content -Path fresh.txt -Value 'new'")
        self.pre("echo hi >> a.txt")
        self.pre("Add-Content a.txt 'more'")
        self.pre("Get-Date | Out-File -Append a.txt")
        self.assertEqual(len(self.proj.copies()), before)
        self.pre("Get-Item a.txt 2>$null")
        self.assertFalse(os.path.exists(os.path.join(self.proj.dir, "skipped.log")))

    def test_whatif_and_read_only_commands_copy_nothing(self):
        self.put("a.txt")
        self.pre("Remove-Item a.txt -WhatIf")
        self.pre("Get-Content a.txt")
        self.pre("Get-ChildItem . | Select-Object Name")
        self.assertEqual(self.proj.copies(), [])
        self.assertFalse(os.path.exists(self.proj.dir))

    def test_cd_inside_the_command_is_followed(self):
        self.put("sub/a.txt")
        self.assertEqual(self.pre("Set-Location sub; Remove-Item a.txt"), {"sub/a.txt"})

    def test_block_and_nested_shell(self):
        self.put("a.txt")
        self.put("b.txt")
        self.assertEqual(self.pre("if (Test-Path a.txt) { Remove-Item a.txt }"), {"a.txt"})
        self.assertIn("b.txt", self.pre('cmd /c del b.txt'))

    def test_path_outside_the_project_is_not_copied(self):
        other = self.put("far.txt", base=self.away)
        self.put("near.txt")
        self.assertEqual(self.pre('Remove-Item "%s", near.txt' % other), {"near.txt"})
        self.pre('Remove-Item "%s"' % other)
        self.assertEqual(len(self.proj.copies()), 1)

    def test_variable_in_a_path_is_logged_not_guessed(self):
        self.put("a.txt")
        self.pre("Remove-Item $target")
        self.assertEqual(self.proj.copies(), [])
        with open(os.path.join(self.proj.dir, "skipped.log"), encoding="utf-8") as handle:
            self.assertIn("variable", handle.read())


class BashReadTest(Base):
    def pre(self, command, tool="Bash", cwd=None):
        return Base.pre(self, command, tool, cwd)

    def test_rm_forms(self):
        self.put("a.txt")
        self.put("dir/b.txt")
        self.put("my notes/" + ODD)
        self.assertEqual(self.pre("rm a.txt"), {"a.txt"})
        self.assertIn("dir/b.txt", self.pre("rm -rf ./dir"))
        self.assertIn("my notes/" + ODD, self.pre("rm 'my notes/%s'" % ODD))

    def test_mv_copies_source_and_what_it_overwrites(self):
        self.put("a.txt", "a")
        self.put("b.txt", "b")
        self.put("dir/a.txt", "inner")
        self.assertEqual(self.pre("mv a.txt b.txt"), {"a.txt", "b.txt"})
        self.assertIn("dir/a.txt", self.pre("mv a.txt dir/"))

    def test_cp_copies_only_what_it_overwrites(self):
        self.put("a.txt", "a")
        self.put("b.txt", "b")
        self.assertEqual(self.pre("cp a.txt b.txt"), {"b.txt"})
        before = len(self.proj.copies())
        self.pre("cp a.txt new.txt")
        self.assertEqual(len(self.proj.copies()), before)

    def test_redirect_sed_truncate_tee(self):
        for name in ("a.txt", "b.txt", "c.txt", "d.txt", "e.txt"):
            self.put(name)
        self.assertEqual(self.pre("echo hi > a.txt"), {"a.txt"})
        self.assertIn("b.txt", self.pre("sed -i 's/one/two/' b.txt"))
        self.assertIn("c.txt", self.pre("truncate -s 0 c.txt"))
        self.assertIn("d.txt", self.pre("echo hi | tee d.txt"))
        before = len(self.proj.copies())
        self.pre("echo hi >> e.txt")
        self.pre("echo hi | tee -a e.txt")
        self.pre("sed -n '1p' e.txt")
        self.pre("echo hi 2>&1")
        self.assertEqual(len(self.proj.copies()), before)

    def test_find_delete_and_exec_rm(self):
        self.put("x/a.tmp")
        self.put("x/keep.md")
        self.put("y/b.log")
        self.assertEqual(self.pre("find . -name '*.tmp' -delete"), {"x/a.tmp"})
        self.assertIn("y/b.log", self.pre("find y -type f -exec rm {} \\;"))
        self.assertNotIn("x/keep.md", self.copied())
        before = len(self.proj.copies())
        self.pre("find . -name '*.md'")
        self.assertEqual(len(self.proj.copies()), before)

    def test_chained_commands_cd_and_nested_shells(self):
        self.put("sub/a.txt")
        self.put("b.txt")
        self.put("c.txt")
        self.put("d.txt")
        self.assertEqual(self.pre("cd sub && rm a.txt"), {"sub/a.txt"})
        self.assertIn("b.txt", self.pre("ls; rm b.txt || true"))
        self.assertIn("c.txt", self.pre('bash -c "rm c.txt"'))
        self.assertIn("d.txt", self.pre('powershell -Command "Remove-Item d.txt"'))

    def test_outside_paths_are_not_copied(self):
        other = self.put("far.txt", base=self.away)
        self.pre("rm '%s'" % other.replace("\\", "/"))
        self.pre("rm ../outside/far.txt")
        self.assertEqual(self.proj.copies(), [])

    @unittest.skipUnless(GIT, "git is not on this machine")
    def test_git_clean_checkout_and_reset(self):
        def git(*args):
            done = subprocess.run([GIT, "-c", "user.name=t", "-c", "user.email=t@example.invalid"] + list(args),
                                  cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(done.returncode, 0, done.stderr)

        self.put("tracked.txt", "v1\n")
        self.put("other.txt", "v1\n")
        git("init", "-q")
        git("add", ".")
        git("commit", "-q", "-m", "first")
        self.put("tracked.txt", "v2 not committed\n")
        self.put("other.txt", "v2 not committed\n")
        self.put("scratch/new.txt", "never added\n")
        self.assertEqual(self.pre("git checkout -- tracked.txt"), {"tracked.txt"})
        self.assertIn("scratch/new.txt", self.pre("git clean -fd"))
        self.assertIn("other.txt", self.pre("git reset --hard"))
        before = len(self.proj.copies())
        self.pre("git clean -n")
        self.pre("git status")
        self.assertEqual(len(self.proj.copies()), before)


class HookProcessTest(Base):
    def test_bad_input_still_exits_zero(self):
        for raw in (b"", b"not json", b"[1, 2]", b'{"tool_input": 5}', "\ufeff{}".encode("utf-8")):
            done = self.hook("pre", raw)
            self.assertEqual((done.returncode, done.stdout), (0, b""), raw)
        self.assertEqual(self.hook("pre", b"{}", extra=["--no-such-option"]).returncode, 0)
        self.assertEqual(self.hook("session-start", b"not json").returncode, 0)

    def test_missing_project_folder_exits_zero(self):
        done = subprocess.run([sys.executable, "-I", self.script, "--project", os.path.join(self.tmp, "gone"), "hook", "pre"],
                              input=json.dumps(self.payload("rm a", "Bash")).encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(done.returncode, 0)

    def test_over_the_limit_is_skipped_with_a_log_line(self):
        self.put("big.bin", "x" * 4096)
        os.makedirs(self.proj.dir)
        fu.write_json(os.path.join(self.proj.dir, "config.json"), {"copy_limit_mb": 0.001})
        done = self.hook("pre", self.payload("rm big.bin", "Bash"))
        self.assertEqual(done.returncode, 0)
        self.assertEqual(self.proj.copies(), [])
        with open(os.path.join(self.proj.dir, "skipped.log"), encoding="utf-8") as handle:
            self.assertIn("over the copy limit", handle.read())

    def test_ignore_list_applies_to_copies(self):
        self.put("node_modules/pkg/index.js")
        self.put("src/a.js")
        self.assertEqual(self.pre("rm -rf node_modules src", "Bash"), {"src/a.js"})
        self.put("debug.log")
        with open(os.path.join(self.proj.dir, "ignore"), "a", encoding="utf-8") as handle:
            handle.write("*.log\n")
        self.assertEqual(self.pre("rm debug.log", "Bash"), {"src/a.js"})

    def test_the_tool_never_copies_its_own_folder(self):
        self.put("a.txt")
        self.pre("rm a.txt", "Bash")
        self.pre("rm -rf .", "Bash")
        for manifest in self.proj.copies():
            for item in manifest["files"]:
                self.assertFalse(item["path"].startswith(fu.DIR))

    def test_console_that_cannot_print_every_character(self):
        self.put(ODD)
        self.pre('Remove-Item "%s"' % ODD)
        ident = self.proj.copies()[0]["id"]
        env = dict(os.environ, PYTHONIOENCODING="cp949")
        for flags in ([], ["-I"]):
            for argv in (["list"], ["show", ident], ["restore", ident, "--dry-run"]):
                done = subprocess.run([sys.executable] + flags + [self.script, "--project", self.root] + argv,
                                      env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertIn(ident.encode(), done.stdout)


class RealRunTest(Base):
    """The command is really run after the hook saw it, and the files are then put back."""

    @unittest.skipUnless(POWERSHELL, "PowerShell is not on this machine")
    def test_powershell_remove_item(self):
        self.put("notes/a.md", "alpha\n")
        self.put("my notes/" + ODD, "odd name\n")
        self.really('Remove-Item notes\\a.md; Remove-Item -Recurse "my notes"', "PowerShell")
        self.assertFalse(self.there("notes/a.md"))
        self.assertFalse(self.there("my notes"))
        self.restore_newest()
        self.assertEqual(self.text("notes/a.md"), "alpha\n")
        self.assertEqual(self.text("my notes/" + ODD), "odd name\n")

    @unittest.skipUnless(POWERSHELL, "PowerShell is not on this machine")
    def test_powershell_move_item_over_a_file(self):
        self.put("draft.md", "draft\n")
        self.put("final.md", "final\n")
        self.really("Move-Item draft.md final.md -Force", "PowerShell")
        self.assertFalse(self.there("draft.md"))
        self.assertEqual(self.text("final.md"), "draft\n")
        text = self.restore_newest()
        self.assertEqual((self.text("draft.md"), self.text("final.md")), ("draft\n", "final\n"))
        self.assertIn("before-restore-", text)

    @unittest.skipUnless(POWERSHELL, "PowerShell is not on this machine")
    def test_powershell_set_content(self):
        self.put("plan.md", "the long plan\n")
        self.really("Set-Content -Path plan.md -Value 'oops'", "PowerShell")
        self.assertNotEqual(self.text("plan.md"), "the long plan\n")
        self.restore_newest()
        self.assertEqual(self.text("plan.md"), "the long plan\n")

    def test_bash_rm_mv_and_redirect(self):
        path = lambda rel: os.path.join(self.root, *rel.split("/"))
        self.put("a.txt", "a\n")
        self.put("my notes/b c.txt", "b\n")
        self.put("keep.txt", "keep\n")
        self.put("log.txt", "long log\n")
        self.really("rm a.txt 'my notes/b c.txt'", "Bash",
                    lambda: (os.remove(path("a.txt")), os.remove(path("my notes/b c.txt"))))
        self.assertFalse(self.there("a.txt") or self.there("my notes/b c.txt"))
        self.restore_newest()
        self.assertEqual((self.text("a.txt"), self.text("my notes/b c.txt")), ("a\n", "b\n"))
        self.really("mv a.txt keep.txt", "Bash", lambda: os.replace(path("a.txt"), path("keep.txt")))
        self.assertEqual((self.there("a.txt"), self.text("keep.txt")), (False, "a\n"))
        self.restore_newest()
        self.assertEqual((self.text("a.txt"), self.text("keep.txt")), ("a\n", "keep\n"))
        self.really("echo short > log.txt", "Bash", lambda: self.put("log.txt", "short\n"))
        self.assertEqual(self.text("log.txt").strip(), "short")
        self.restore_newest()
        self.assertEqual(self.text("log.txt"), "long log\n")

    def test_file_deleted_by_a_python_script_comes_back_from_the_restore_point(self):
        self.put("notes/a.md", "alpha\n")
        self.put("notes/" + ODD, "odd\n")
        self.put("clean.py", "import shutil\nshutil.rmtree('notes')\n")
        done = self.hook("session-start", {"hook_event_name": "SessionStart", "session_id": "s1", "source": "startup", "cwd": self.root})
        self.assertEqual((done.returncode, done.stdout), (0, b""), done.stderr)
        self.assertEqual(self.hook("pre", self.payload("python clean.py", "PowerShell")).returncode, 0)
        self.assertEqual(self.proj.copies(), [])          # the command reader cannot see inside the script
        ran = subprocess.run([sys.executable, "clean.py"], cwd=self.root)
        self.assertEqual(ran.returncode, 0)
        self.assertFalse(self.there("notes"))
        code, text = self.cli("restore", "--session", answers=["y"])
        self.assertEqual(code, 0, text)
        self.assertEqual((self.text("notes/a.md"), self.text("notes/" + ODD)), ("alpha\n", "odd\n"))


class SnapshotTest(Base):
    def objects(self):
        found = []
        for folder, _, files in os.walk(os.path.join(self.proj.store, "objects")):
            found += files
        return sorted(found)

    def test_content_addressed_and_only_changed_files_are_written(self):
        self.put("a.txt", "same\n")
        self.put("b.txt", "same\n")
        self.put("c.txt", "other\n")
        first, _ = fu.snapshot(self.proj, "s1")
        self.assertEqual(len(first["files"]), 3)
        self.assertEqual(len(self.objects()), 2)
        stamp = os.path.getmtime(self.proj.object(first["files"]["a.txt"]["sha256"]))
        self.put("c.txt", "changed\n")
        second, _ = fu.snapshot(self.proj, "s2")
        self.assertEqual(os.path.getmtime(self.proj.object(second["files"]["a.txt"]["sha256"])), stamp)
        self.assertEqual([item["session_id"] for item in self.proj.sessions()], ["s2"])   # only the newest stays
        self.assertEqual(self.objects(), sorted({fu.sha_of(os.path.join(self.root, n)) for n in ("a.txt", "c.txt")}))

    def test_same_session_does_not_replace_its_own_restore_point(self):
        self.put("a.txt", "start\n")
        fu.snapshot(self.proj, "s1")
        self.put("a.txt", "later\n")
        self.assertEqual(fu.snapshot(self.proj, "s1"), (None, None))
        self.assertIsNone(fu.hook_session(self.proj, {"session_id": "other", "source": "compact"}))
        self.assertEqual(len(self.proj.sessions()), 1)
        code, _ = self.cli("restore", "--session", answers=["y"])
        self.assertEqual((code, self.text("a.txt")), (0, "start\n"))

    def test_ignore_file(self):
        self.put("src/a.py")
        self.put("node_modules/x/y.js")
        self.put(".git/config")
        self.put("out/big.log")
        snap, _ = fu.snapshot(self.proj, "s1")
        self.assertEqual(sorted(snap["files"]), ["out/big.log", "src/a.py"])
        with open(os.path.join(self.proj.dir, "ignore"), "a", encoding="utf-8") as handle:
            handle.write("*.log\n")
        snap, _ = fu.snapshot(self.proj, "s2")
        self.assertEqual(sorted(snap["files"]), ["src/a.py"])

    def test_folder_over_the_limit_is_skipped_and_said_in_one_line(self):
        self.put("a.txt", "x" * 4096)
        os.makedirs(self.proj.dir)
        fu.write_json(os.path.join(self.proj.dir, "config.json"), {"snapshot_limit_mb": 0.001})
        done = self.hook("session-start", {"session_id": "s1", "source": "startup", "cwd": self.root})
        self.assertEqual(done.returncode, 0)
        message = json.loads(done.stdout.decode("utf-8"))["systemMessage"]
        self.assertIn("no restore point", message)
        self.assertEqual(len(done.stdout.strip().splitlines()), 1)
        self.assertEqual(self.proj.sessions(), [])

    def test_restore_session_plan_paths_and_new_files(self):
        self.put("a.txt", "a1\n")
        self.put("dir/b.txt", "b1\n")
        fu.snapshot(self.proj, "s1")
        self.put("a.txt", "a2\n")
        os.remove(os.path.join(self.root, "dir", "b.txt"))
        self.put("made later.txt")
        code, text = self.cli("restore", "--session", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("overwrite", text)
        self.assertIn("bring back", text)
        self.assertIn("1 file made after this restore point stays", text)
        self.assertEqual(self.text("a.txt"), "a2\n")
        code, text = self.cli("restore", "--session", "dir", answers=["y"])
        self.assertEqual((code, self.text("dir/b.txt"), self.text("a.txt")), (0, "b1\n", "a2\n"))
        self.assertTrue(self.there("made later.txt"))

    def test_no_restore_point_yet(self):
        code, _ = self.cli("restore", "--session")
        self.assertEqual(code, 3)


class RestoreTest(Base):
    def setUp(self):
        Base.setUp(self)
        self.put("a.txt", "a1\n")
        self.put("dir/b.txt", "b1\n")
        self.pre("rm a.txt dir/b.txt", "Bash")
        self.ident = self.proj.copies()[0]["id"]

    def test_plan_is_shown_and_anything_but_y_changes_nothing(self):
        os.remove(os.path.join(self.root, "a.txt"))
        self.put("dir/b.txt", "b2\n")
        for answer in ("", "n", "no", "yes please"):
            code, text = self.cli("restore", self.ident, answers=[answer])
            self.assertEqual(code, 0)
            self.assertIn("bring back  a.txt", text)
            self.assertIn("overwrite   dir/b.txt", text)
            self.assertIn("Cancelled", text)
            self.assertEqual(len(self.asked), 1)
            self.assertFalse(self.there("a.txt"))
            self.assertEqual(self.text("dir/b.txt"), "b2\n")
        self.assertEqual(len(self.proj.copies()), 1)

    def test_y_writes_and_keeps_the_overwritten_version(self):
        os.remove(os.path.join(self.root, "a.txt"))
        self.put("dir/b.txt", "b2\n")
        code, text = self.cli("restore", self.ident, answers=["y"])
        self.assertEqual(code, 0, text)
        self.assertEqual((self.text("a.txt"), self.text("dir/b.txt")), ("a1\n", "b1\n"))
        kept = [item for item in self.proj.copies() if item["kind"] == "before-restore"]
        self.assertEqual(len(kept), 1)
        self.assertEqual([item["path"] for item in kept[0]["files"]], ["dir/b.txt"])
        code, _ = self.cli("restore", kept[0]["id"], answers=["y"])       # and that one can be put back too
        self.assertEqual((code, self.text("dir/b.txt")), (0, "b2\n"))

    def test_same_files_need_no_question(self):
        code, text = self.cli("restore", self.ident, answers=["y"])
        self.assertEqual(code, 0)
        self.assertIn("Nothing to do", text)
        self.assertEqual(self.asked, [])

    def test_only_the_named_path(self):
        os.remove(os.path.join(self.root, "a.txt"))
        os.remove(os.path.join(self.root, "dir", "b.txt"))
        code, _ = self.cli("restore", self.ident, "dir/b.txt", answers=["y"])
        self.assertEqual(code, 0)
        self.assertTrue(self.there("dir/b.txt"))
        self.assertFalse(self.there("a.txt"))
        self.assertEqual(self.cli("restore", self.ident, "nothing/here")[0], 3)
        self.assertEqual(self.cli("restore", "no-such-id")[0], 3)

    @unittest.skipUnless(os.name == "nt", "an open file blocks replacing it only on Windows")
    def test_locked_target_stays_as_it_was(self):
        self.put("a.txt", "a2 open in another program\n")
        os.remove(os.path.join(self.root, "dir", "b.txt"))
        with open(os.path.join(self.root, "a.txt"), "rb"):
            code, text = self.cli("restore", self.ident, answers=["y"])
        self.assertEqual(code, 1)
        self.assertIn("NOT written, left as it was: a.txt", text)
        self.assertEqual(self.text("a.txt"), "a2 open in another program\n")
        self.assertEqual(self.text("dir/b.txt"), "b1\n")                    # the other file was still restored
        self.assertEqual([n for n in os.listdir(self.root) if n.endswith("-tmp")], [])

    def test_disk_full_during_restore_leaves_the_original(self):
        self.put("a.txt", "a2\n")
        real = fu.copy_file

        def full(src, dst):
            if dst.endswith(".folder-undo-tmp"):
                with open(dst, "wb") as handle:
                    handle.write(b"par")
                raise OSError(errno.ENOSPC, "No space left on device")
            return real(src, dst)

        fu.copy_file = full
        self.addCleanup(setattr, fu, "copy_file", real)
        code, text = self.cli("restore", self.ident, answers=["y"])
        self.assertEqual(code, 1)
        self.assertIn("NOT written", text)
        self.assertEqual(self.text("a.txt"), "a2\n")
        self.assertEqual([n for n in os.listdir(self.root) if n.endswith("-tmp")], [])

    def test_nothing_changes_when_the_current_version_cannot_be_kept(self):
        self.put("a.txt", "a2\n")
        real = fu.copy_file

        def broken(src, dst):
            if "before-restore" in dst:
                raise OSError(errno.ENOSPC, "No space left on device")
            return real(src, dst)

        fu.copy_file = broken
        self.addCleanup(setattr, fu, "copy_file", real)
        code, _ = self.cli("restore", self.ident, answers=["y"])
        self.assertEqual((code, self.text("a.txt")), (3, "a2\n"))


class ListPruneTest(Base):
    def test_list_show_and_prune(self):
        code, text = self.cli("list")
        self.assertEqual(code, 0)
        self.assertEqual(text.count("none yet"), 2)
        for index in range(4):
            self.put("f%d.txt" % index)
            self.pre("rm f%d.txt" % index, "Bash")
        fu.snapshot(self.proj, "s1")
        ids = [item["id"] for item in self.proj.copies()]
        self.assertEqual(len(set(ids)), 4)
        code, text = self.cli("list")
        for ident in ids:
            self.assertIn(ident, text)
        code, text = self.cli("show", ids[0])
        self.assertIn("rm f0.txt", text)
        self.assertIn("f0.txt", text)
        self.assertEqual(self.cli("show", self.proj.sessions()[0]["id"])[0], 0)
        self.assertEqual(self.cli("show", "nope")[0], 3)
        self.cli("prune", "--keep", "1", "--dry-run")
        self.cli("prune", "--keep", "1", answers=["n"])
        self.assertEqual(len(self.proj.copies()), 4)
        code, _ = self.cli("prune", "--keep", "1", answers=["y"])
        self.assertEqual([item["id"] for item in self.proj.copies()], ids[-1:])
        self.assertEqual(self.cli("prune")[0], 3)


class InstallTest(Base):
    def settings(self):
        return os.path.join(self.root, ".claude", "settings.json")

    def test_dry_run_and_no_write_nothing(self):
        code, text = self.cli("install", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("Dry run", text)
        self.assertIn("PreToolUse", text)
        self.assertEqual(self.asked, [])
        code, text = self.cli("install", answers=["n"])
        self.assertIn("Cancelled", text)
        self.assertFalse(os.path.exists(os.path.join(self.root, ".claude")))
        self.assertFalse(os.path.exists(self.proj.dir))

    def test_y_merges_with_what_is_there_and_uninstall_restores_the_bytes(self):
        original = b'{\r\n  "model": "x",\r\n  "hooks": {"PreToolUse": [{"matcher": "Edit", "hooks": [{"type": "command", "command": "echo mine"}]}]}\r\n}\r\n'
        os.makedirs(os.path.dirname(self.settings()))
        with open(self.settings(), "wb") as handle:
            handle.write(original)
        code, text = self.cli("install", answers=["y"])
        self.assertEqual(code, 0, text)
        data = fu.read_json(self.settings())
        self.assertEqual(data["model"], "x")
        commands = [h["command"] for g in data["hooks"]["PreToolUse"] for h in g["hooks"]]
        self.assertEqual(commands[0], "echo mine")
        self.assertIn("hook pre", commands[1])
        self.assertEqual(data["hooks"]["PreToolUse"][1]["matcher"], "Bash|PowerShell")
        self.assertIn("hook session-start", data["hooks"]["SessionStart"][0]["hooks"][0]["command"])
        self.assertEqual(len([n for n in os.listdir(os.path.dirname(self.settings())) if ".bak-" in n]), 1)
        code, text = self.cli("install", answers=["y"])
        self.assertIn("already on", text)
        code, text = self.cli("uninstall", "--dry-run")
        self.assertEqual(fu.read_json(self.settings()), data)
        code, text = self.cli("uninstall", answers=["y"])
        self.assertEqual(code, 0, text)
        with open(self.settings(), "rb") as handle:
            self.assertEqual(handle.read(), original)
        self.assertEqual(os.listdir(os.path.dirname(self.settings())), ["settings.json"])

    def test_uninstall_removes_what_install_created(self):
        self.cli("install", answers=["y"])
        self.assertTrue(os.path.exists(self.settings()))
        code, _ = self.cli("uninstall", answers=["y"])
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(os.path.join(self.root, ".claude")))

    def test_settings_edited_after_install_keep_the_edit(self):
        self.cli("install", answers=["y"])
        data = fu.read_json(self.settings())
        data["model"] = "added later"
        fu.write_json(self.settings(), data)
        code, text = self.cli("uninstall", answers=["y"])
        self.assertEqual(code, 0, text)
        self.assertEqual(fu.read_json(self.settings()), {"model": "added later"})

    def test_refuses_the_user_wide_file_and_broken_json(self):
        code, _ = self.cli("install", answers=["y"], home=self.root)
        self.assertEqual(code, 3)
        self.assertFalse(os.path.exists(self.settings()))
        os.makedirs(os.path.dirname(self.settings()))
        with open(self.settings(), "wb") as handle:
            handle.write(b"{ not json")
        self.assertEqual(self.cli("install", answers=["y"])[0], 3)
        self.assertEqual(self.cli("uninstall", answers=["y"])[0], 3)
        with open(self.settings(), "rb") as handle:
            self.assertEqual(handle.read(), b"{ not json")

    def test_gitignore_is_asked_separately(self):
        self.put(".gitignore", "dist/")
        self.cli("install", answers=["y", "n"])
        self.assertEqual(len(self.asked), 2)
        self.assertEqual(self.text(".gitignore"), "dist/")
        self.cli("uninstall", answers=["y"])
        code, text = self.cli("install", answers=["y", "y"])
        self.assertEqual(self.text(".gitignore"), "dist/\n.folder-undo/\n")
        self.cli("uninstall", answers=["y"])
        self.cli("install", answers=["y", "y"])
        self.assertEqual(len(self.asked), 1)                              # the line is there, so no second question
        self.assertEqual(self.text(".gitignore"), "dist/\n.folder-undo/\n")

    def test_installed_commands_really_run(self):
        self.put("a.txt", "a\n")
        self.cli("install", "--python", '"%s"' % sys.executable.replace("\\", "/"), answers=["y"])
        data = fu.read_json(self.settings())
        start = data["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        before = data["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        done = subprocess.run(start, shell=True, input=json.dumps({"session_id": "s1", "source": "startup"}).encode(),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(done.returncode, 0, done.stderr)
        done = subprocess.run(before, shell=True, input=json.dumps(self.payload("rm a.txt", "Bash")).encode(),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(len(self.proj.sessions()), 1)
        self.assertEqual(self.copied(), {"a.txt"})


BOM_Y = b"\xef\xbb\xbfy\r\n"          # what Windows PowerShell 5.1 pipes for "y" when its output encoding is UTF-8


def ps_quote(text):
    return "'" + text.replace("'", "''") + "'"


class PipedAnswerCases:
    """An answer that a script or an agent pipes in instead of typing it. Windows PowerShell 5.1 can put a
    UTF-8 byte order mark in front of it, and a Korean Windows console reads stdin as cp949 (forced here)."""

    def piped(self, *argv, data=BOM_Y):
        env = dict(os.environ, PYTHONIOENCODING="cp949", PYTHONUTF8="0")
        done = subprocess.run([sys.executable, self.script, "--project", self.root] + list(argv), input=data, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        return done.returncode, done.stdout.decode("cp949", "replace") + done.stderr.decode("cp949", "replace")

    def settings(self):
        return os.path.join(self.root, ".claude", "settings.json")

    def test_piped_y_with_a_byte_order_mark_installs(self):
        code, text = self.piped("install")
        self.assertEqual(code, 0, text)
        self.assertNotIn("Cancelled", text)
        self.assertIn("Written and read back", text)
        command = fu.read_json(self.settings())["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        self.assertIn(os.path.basename(self.script), command)
        code, text = self.piped("uninstall")
        self.assertEqual(code, 0, text)
        self.assertFalse(os.path.exists(os.path.join(self.root, ".claude")))

    def test_piped_y_with_a_byte_order_mark_restores(self):
        self.put("a.txt", "a1\n")
        self.pre("rm a.txt", "Bash")
        os.remove(os.path.join(self.root, "a.txt"))
        code, text = self.piped("restore", self.proj.copies()[0]["id"])
        self.assertEqual(code, 0, text)
        self.assertNotIn("Cancelled", text)
        self.assertEqual(self.text("a.txt"), "a1\n")

    def test_piped_no_or_nothing_still_cancels(self):
        for data in (b"\xef\xbb\xbfn\r\n", b"\xef\xbb\xbf\r\n", b"", b"\xef\xbb\xbfyes please\r\n",
                     b"\xc7\xd1\r\n"):                                   # one Hangul syllable in cp949, not UTF-8
            code, text = self.piped("install", data=data)
            self.assertEqual(code, 0, text)
            self.assertIn("Cancelled", text)
            self.assertFalse(os.path.exists(os.path.join(self.root, ".claude")))

    @unittest.skipUnless(POWERSHELL, "PowerShell is not on this machine")
    def test_real_powershell_pipe_installs(self):
        command = ("$OutputEncoding = New-Object System.Text.UTF8Encoding $true; 'y' | & %s %s --project %s install"
                   % (ps_quote(sys.executable), ps_quote(self.script), ps_quote(self.root)))
        env = dict(os.environ, PYTHONIOENCODING="cp949", PYTHONUTF8="0")
        done = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", command], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        text = done.stdout.decode("cp949", "replace") + done.stderr.decode("cp949", "replace")
        self.assertEqual(done.returncode, 0, text)
        self.assertNotIn("Cancelled", text)
        self.assertTrue(os.path.exists(self.settings()), text)


class PipedAnswerTest(PipedAnswerCases, Base):
    pass


if __name__ == "__main__":
    unittest.main()
