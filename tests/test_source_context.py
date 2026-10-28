"""Read-only chat source retrieval, freshness, provenance and disclosure bounds."""
import hashlib
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from racing_dashboard.source_context import (
    list_source_branches, read_chat_source_context, read_chat_source_file, read_source_context,
)


class ChatSourceContextTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name) / "controller"
        self.repo.mkdir()
        self.git("init", "-q")
        self.write("mpc/scripts/mpc_node.py", "MAX_SPEED = 3.0\n")
        self.git("add", ".")
        self.git("commit", "-qm", "baseline")

    def git(self, *arguments):
        return subprocess.run(
            ["git", "-c", "user.name=Source test", "-c", "user.email=source@example.test",
             "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
             "-C", str(self.repo), *arguments],
            capture_output=True, check=True, timeout=10,
        ).stdout.decode().strip()

    def write(self, relative, content):
        target = self.repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    def context(self, ref="", query="", **kwargs):
        return read_chat_source_context(str(self.repo), ref, query, **kwargs)

    def test_working_tree_refreshes_edits_and_nonignored_untracked_source(self):
        before = self.context()
        self.write("mpc/scripts/mpc_node.py", "MAX_SPEED = 4.0\n")
        self.write("mpc/config/controller.yaml", "profile_accel: 2.5\n")
        self.write(".gitignore", "scratch.py\n")
        self.write("scratch.py", "do_not_include = True\n")
        after = self.context()
        files = {item["path"]: item for item in after["files"]}
        self.assertEqual(after["mode"], "working_tree")
        self.assertEqual(after["commit"], before["commit"])
        self.assertFalse(after["deployment_verified"])
        self.assertNotEqual(after["snapshot_sha256"], before["snapshot_sha256"])
        self.assertEqual(files["mpc/scripts/mpc_node.py"]["content"], "MAX_SPEED = 4.0\n")
        self.assertIn("mpc/config/controller.yaml", files)
        self.assertNotIn("scratch.py", after["inventory"])
        self.assertEqual(files["mpc/scripts/mpc_node.py"]["line_count"], 1)
        self.assertEqual(read_source_context(str(self.repo), "")["mode"], "working_tree")

    def test_explicit_ref_keeps_git_blob_and_branch_while_local_source_changes(self):
        branch = self.git("branch", "--show-current")
        commit = self.git("rev-parse", "HEAD")
        self.write("mpc/scripts/mpc_node.py", "MAX_SPEED = 9.0\n")
        self.write("new.py", "untracked = True\n")
        result = self.context("HEAD")
        self.assertEqual(result["mode"], "git_revision")
        self.assertEqual(result["commit"], commit)
        self.assertEqual(result["files"][0]["content"], "MAX_SPEED = 3.0\n")
        self.assertNotIn("new.py", result["inventory"])
        self.assertEqual(self.git("branch", "--show-current"), branch)
        self.assertEqual((self.repo / "mpc/scripts/mpc_node.py").read_text(), "MAX_SPEED = 9.0\n")

    def test_symlinks_hidden_credentials_and_sensitive_contents_are_not_disclosed(self):
        outside = Path(self.temporary.name) / "outside.py"
        outside.write_text("outside_secret = 'never send this'\n")
        (self.repo / "linked.py").symlink_to(outside)
        self.write(".aws/config", "private\n")
        self.write("credentials.json", '{"value": "private"}')
        self.write("secrets/config.yaml", "private: never send this\n")
        self.write("config/settings.yaml", "password: sensitive-clear-text-value\n")
        self.write("config/other.json", '{"api_key": "sensitive-clear-text-value"}')
        self.write("binary.py", "first\0second")
        self.git("add", ".")
        self.git("commit", "-qm", "unsafe paths")
        for ref in ("", "HEAD"):
            result = self.context(ref)
            self.assertEqual({item["path"] for item in result["files"]}, {"mpc/scripts/mpc_node.py"})
            self.assertNotIn("credentials.json", result["inventory"])
            self.assertNotIn(".aws/config", result["inventory"])
            self.assertNotIn("secrets/config.yaml", result["inventory"])
            self.assertNotIn("sensitive-clear-text-value", str(result))
            self.assertNotIn("never send this", str(result))
        for relative in ("linked.py", "credentials.json", "../outside.py", "/tmp/outside.py"):
            with self.assertRaises(ValueError):
                read_chat_source_file(str(self.repo), "", relative)

    def test_query_prioritizes_exact_file_inside_a_bounded_context(self):
        self.write("mpc/config/default.py", "#" + "a" * 899)
        self.write("tests/experiment.py", "#" + "b" * 899)
        result = self.context(query="tests/experiment.py", max_bytes=1024)
        self.assertLessEqual(result["context_bytes"], 1024)
        self.assertEqual(result["files"][0]["path"], "tests/experiment.py")
        self.assertIn("mpc/config/default.py", result["inventory"])
        self.assertTrue(any(item["path"] == "mpc/config/default.py" and item["reason"] == "context size limit"
                            for item in result["omitted_files"]))

    def test_large_source_can_be_retrieved_in_exact_line_chunks(self):
        content = "".join(f"value_{index} = {index}\n" for index in range(9000))
        self.write("mpc/scripts/large.py", content)
        result = self.context()
        self.assertNotIn("mpc/scripts/large.py", {item["path"] for item in result["files"]})
        self.git("add", ".")
        self.git("commit", "-qm", "large source")
        for ref in ("", "HEAD"):
            chunk = read_chat_source_file(str(self.repo), ref, "mpc/scripts/large.py", start_line=40, max_lines=3)
            self.assertEqual(chunk["start_line"], 40)
            self.assertEqual(chunk["end_line"], 42)
            self.assertEqual(chunk["next_line"], 43)
            self.assertEqual(chunk["line_count"], 9000)
            self.assertEqual(chunk["content"], "value_39 = 39\nvalue_40 = 40\nvalue_41 = 41\n")
            self.assertEqual(chunk["sha256"], hashlib.sha256(content.encode()).hexdigest())
        with self.assertRaisesRegex(ValueError, "end of"):
            read_chat_source_file(str(self.repo), "", "mpc/scripts/large.py", start_line=10000)

    def test_chunk_retrieval_checks_credentials_outside_the_requested_lines(self):
        self.write("config.py", "value = 1\n" * 1000 + "api_key = 'sensitive-clear-text-value'\n")
        with self.assertRaisesRegex(ValueError, "credentials"):
            read_chat_source_file(str(self.repo), "", "config.py", max_lines=1)

    def test_non_git_directory_works_without_claiming_a_commit(self):
        folder = Path(self.temporary.name) / "plain"
        folder.mkdir()
        (folder / "controller.py").write_text("gain = 1.0\n")
        result = read_chat_source_context(str(folder))
        self.assertIsNone(result["commit"])
        self.assertEqual(result["files"][0]["path"], "controller.py")
        self.assertEqual(result["repo_name"], "plain")
        self.assertIsNone(result["branch"])
        branches = list_source_branches(str(folder))
        self.assertFalse(branches["is_git"])
        self.assertEqual(branches["branches"], [])

    def test_arbitrary_lab_branch_reads_generic_source_and_preserves_checkout(self):
        self.git("rm", "-r", "mpc")
        self.git("checkout", "-qb", "lab8/mppi+tuning")
        files = {
            "mppi/scripts/mppi_node.py": "NUM_SAMPLES = 2048\n",
            "mppi/config/controller.yaml": "temperature: 0.25\n",
            "mppi/kernels/rollout.cu": "__global__ void rollout() {}\n",
            "mppi/include/rollout.cuh": "void rollout();\n",
            "mppi/msg/ControllerStatus.msg": "float64 solve_time\n",
            "mppi/srv/Reset.srv": "bool reset\n---\nbool success\n",
            "mppi/action/Drive.action": "float64 speed\n---\nbool success\n---\nfloat64 progress\n",
            "mppi/package.xml": "<package><name>mppi</name></package>\n",
            "mppi/CMakeLists.txt": "project(mppi)\n",
        }
        for name, content in files.items():
            self.write(name, content)
        self.git("add", ".")
        self.git("commit", "-qm", "Lab 8 MPPI controller")
        commit = self.git("rev-parse", "HEAD")
        self.git("checkout", "-qb", "other-work")
        self.write("mppi/scripts/mppi_node.py", "NUM_SAMPLES = 4096\n")
        result = read_source_context(str(self.repo), "lab8/mppi+tuning")
        self.assertEqual(result["mode"], "git_revision")
        self.assertEqual(result["ref"], "lab8/mppi+tuning")
        self.assertEqual(result["repo_name"], "controller")
        self.assertEqual(result["branch"], "other-work")
        self.assertEqual(result["commit"], commit)
        self.assertEqual({item["path"]: item["content"] for item in result["files"]}, files)
        self.assertEqual(self.git("branch", "--show-current"), "other-work")
        self.assertEqual((self.repo / "mppi/scripts/mppi_node.py").read_text(), "NUM_SAMPLES = 4096\n")
        working = read_source_context(str(self.repo), "")
        self.assertEqual(working["branch"], "other-work")
        self.assertEqual(next(item for item in working["files"] if item["path"] == "mppi/scripts/mppi_node.py")["content"],
                         "NUM_SAMPLES = 4096\n")

    def test_branch_listing_includes_fetched_remote_branches_without_switching(self):
        branch = self.git("branch", "--show-current")
        self.git("branch", "lab8/mppi+tuning")
        self.git("update-ref", "refs/remotes/origin/lab8-remote", "HEAD")
        self.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/lab8-remote")
        branches = list_source_branches(str(self.repo))
        self.assertTrue(branches["is_git"])
        self.assertEqual(branches["repo_name"], "controller")
        self.assertEqual(branches["current_branch"], branch)
        self.assertEqual(branches["branches"][0], branch)
        self.assertIn("lab8/mppi+tuning", branches["branches"])
        self.assertIn("origin/lab8-remote", branches["branches"])
        self.assertNotIn("origin/HEAD", branches["branches"])
        self.assertFalse(branches["truncated"])
        self.assertEqual(self.git("branch", "--show-current"), branch)
        self.git("checkout", "--detach", "-q", "HEAD")
        self.assertIsNone(list_source_branches(str(self.repo))["current_branch"])

    def test_branch_listing_validates_paths_and_propagates_timeout(self):
        for repo in ("", " /missing/source/directory ", [], None):
            with self.subTest(repo=repo), self.assertRaises(ValueError):
                list_source_branches(repo)
        with patch("racing_dashboard.source_context.subprocess.run", side_effect=subprocess.TimeoutExpired("git", 5)):
            with self.assertRaisesRegex(ValueError, "timed out"):
                list_source_branches(str(self.repo))

    def test_unsafe_refs_and_invalid_ranges_are_rejected(self):
        for ref in ("--output=/tmp/bad", "HEAD:other.py", "HEAD\n"):
            with self.assertRaises(ValueError):
                self.context(ref)
        for start_line in (0, -1, True):
            with self.assertRaises(ValueError):
                read_chat_source_file(str(self.repo), "", "mpc/scripts/mpc_node.py", start_line=start_line)

    def test_git_timeout_does_not_fall_back_to_ignoring_gitignore(self):
        with patch("racing_dashboard.source_context.subprocess.run", side_effect=subprocess.TimeoutExpired("git", 5)):
            with self.assertRaisesRegex(ValueError, "timed out"):
                self.context()


if __name__ == "__main__":
    unittest.main()
