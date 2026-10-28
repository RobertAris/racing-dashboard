"""GitHub links select existing local code without network or checkout changes."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from racing_dashboard.repository import resolve_repository_selection


class RepositorySelectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.repo = self.make_repo("lab8-controller", "git@github.com:RoboRacer-Class/lab8-controller.git")

    def git(self, repo, *args):
        return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
            "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-C", str(repo), *args],
            capture_output=True, check=True, text=True, timeout=5).stdout.strip()

    def make_repo(self, name, origin):
        path = self.root / name
        path.mkdir()
        self.git(path, "init", "-qb", "main")
        self.git(path, "remote", "add", "origin", origin)
        (path / "controller.py").write_text("temperature = 0.4\n")
        self.git(path, "add", ".")
        self.git(path, "commit", "-qm", "baseline")
        return path

    def resolve(self, value, ref=""):
        return resolve_repository_selection(value, ref, search_roots=[self.root])

    def test_https_tree_url_matches_ssh_origin_and_preserves_local_edits(self):
        source = self.repo / "controller.py"
        source.write_text("temperature = 0.8\n")
        result = self.resolve("https://github.com/RoboRacer-Class/lab8-controller/tree/", "liam-trial-1")
        self.assertEqual(result["repo"], str(self.repo))
        self.assertEqual(result["ref"], "liam-trial-1")
        self.assertTrue(result["resolved_from_url"])
        self.assertEqual(self.git(self.repo, "branch", "--show-current"), "main")
        self.assertEqual(source.read_text(), "temperature = 0.8\n")

    def test_embedded_slash_branch_is_used_unless_explicit_ref_supplied(self):
        url = "https://github.com/roboracer-class/lab8-controller/tree/feature/mppi-tuning"
        self.assertEqual(self.resolve(url)["ref"], "feature/mppi-tuning")
        self.assertEqual(self.resolve(url, "another-branch")["ref"], "another-branch")

    def test_root_git_link_matches_renamed_local_checkout(self):
        self.repo.rename(self.root / "my-local-working-copy")
        result = self.resolve("https://github.com/RoboRacer-Class/lab8-controller.git")
        self.assertEqual(result["repo_name"], "my-local-working-copy")
        self.assertEqual(result["ref"], "")

    def test_https_origin_and_duplicate_roots_are_supported(self):
        self.git(self.repo, "remote", "set-url", "origin", "https://github.com/RoboRacer-Class/lab8-controller.git")
        result = resolve_repository_selection("https://github.com/ROBORACER-CLASS/LAB8-CONTROLLER", search_roots=[self.root, self.root])
        self.assertEqual(result["repo"], str(self.repo))

    def test_ambiguous_clones_require_explicit_local_path(self):
        self.make_repo("another-local-copy", "https://github.com/RoboRacer-Class/lab8-controller")
        with self.assertRaisesRegex(ValueError, "More than one"):
            self.resolve("https://github.com/RoboRacer-Class/lab8-controller")

    def test_no_local_match_explains_how_to_choose_source(self):
        with self.assertRaisesRegex(ValueError, "No local checkout found.*local repository folder"):
            self.resolve("https://github.com/RoboRacer-Class/missing-controller")

    def test_same_name_with_wrong_origin_is_not_selected(self):
        self.git(self.repo, "remote", "set-url", "origin", "https://github.com/another-owner/lab8-controller.git")
        with self.assertRaisesRegex(ValueError, "No local checkout found"):
            self.resolve("https://github.com/RoboRacer-Class/lab8-controller")

    def test_local_and_empty_paths_do_not_search_git_or_network(self):
        with patch("racing_dashboard.repository.subprocess.run", side_effect=AssertionError("must not run Git")):
            empty = self.resolve("", "main")
            local = self.resolve(str(self.repo))
        self.assertEqual(empty["repo"], "")
        self.assertEqual(empty["ref"], "main")
        self.assertFalse(local["resolved_from_url"])
        self.assertEqual(local["repo"], str(self.repo))

    def test_short_branch_matches_unique_remote_without_checkout(self):
        self.git(self.repo, "update-ref", "refs/remotes/origin/liam-trial-1", "HEAD")
        for value in (str(self.repo), "https://github.com/RoboRacer-Class/lab8-controller/tree/"):
            with self.subTest(value=value):
                result = self.resolve(value, "liam-trial-1")
                self.assertEqual(result["ref"], "origin/liam-trial-1")
                self.assertEqual(self.git(self.repo, "branch", "--show-current"), "main")

    def test_exact_local_branch_tag_or_commit_wins_over_remote(self):
        commit = self.git(self.repo, "rev-parse", "HEAD")
        self.git(self.repo, "update-ref", "refs/remotes/origin/main", commit)
        self.git(self.repo, "tag", "baseline")
        for ref in ("main", "baseline", commit, "HEAD", "origin/main"):
            with self.subTest(ref=ref):
                self.assertEqual(self.resolve(str(self.repo), ref)["ref"], ref)

    def test_ambiguous_remote_shorthand_requires_full_reference(self):
        for remote in ("origin", "teammate"):
            self.git(self.repo, "update-ref", f"refs/remotes/{remote}/trial", "HEAD")
        with self.assertRaisesRegex(ValueError, "multiple remotes.*origin/trial.*teammate/trial"):
            self.resolve(str(self.repo), "trial")
        self.assertEqual(self.resolve(str(self.repo), "origin/trial")["ref"], "origin/trial")

    def test_remote_branch_with_slashes_resolves_embedded_link(self):
        self.git(self.repo, "update-ref", "refs/remotes/origin/feature/mppi-tuning", "HEAD")
        result = self.resolve("https://github.com/RoboRacer-Class/lab8-controller/tree/feature/mppi-tuning")
        self.assertEqual(result["ref"], "origin/feature/mppi-tuning")

    def test_unsafe_and_unsupported_urls_are_rejected_without_git(self):
        values = ["https://user:password@github.com/owner/repo", "http://github.com/owner/repo",
                  "https://example.com/owner/repo", "https://github.com/owner/repo?token=secret",
                  "https://github.com/owner/repo#readme", "file:///private/path",
                  "https://github.com/owner/repo/blob/main/controller.py",
                  "https://github.com/owner/%2e%2e", "https://github.com/owner/repo/tree/--bad"]
        with patch("racing_dashboard.repository.subprocess.run", side_effect=AssertionError("must not run Git")):
            for value in values:
                with self.subTest(value=value), self.assertRaises(ValueError):
                    self.resolve(value)


if __name__ == "__main__":
    unittest.main()
