from fnmatch import fnmatchcase
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ArtifactIgnoreTests(unittest.TestCase):
    def test_generated_rules_use_shared_git_docker_glob_subset(self):
        # Limit new rules to the common **/ glob subset. Actual git check-ignore
        # validation can additionally be run outside the network-blocked suite.
        patterns = ["**/outputs/", "**/*.jsonl", "**/*_resume.sqlite3",
                    "**/*_resume.sqlite3-*", "**/*_resume.sqlite3.lock", "**/.*.tmp"]
        names = ["run.jsonl", "run_resume.sqlite3", "run_resume.sqlite3-journal",
                 "run_resume.sqlite3-wal", "run_resume.sqlite3-shm",
                 "run_resume.sqlite3.lock", ".run_part001.md.random.tmp",
                 ".run_part001.xlsx.random.tmp", ".run_manifest.json.random.tmp"]
        for config in (".gitignore", ".dockerignore"):
            rules = (ROOT / config).read_text().splitlines()
            for pattern in patterns:
                self.assertIn(pattern, rules)
            for prefix in ("", "nested/deeper/"):
                for name in names:
                    self.assertTrue(any(
                        fnmatchcase(prefix + name, pattern) or
                        fnmatchcase(prefix + name, pattern.removeprefix("**/"))
                        for pattern in patterns), prefix + name)
            for name in ("src/resume_state.py", "tests/test_web_security.py",
                         "requirements.txt", "poetry.lock", "tests/fixture.sqlite3"):
                self.assertFalse(any(
                    fnmatchcase(name, pattern) or
                    fnmatchcase(name, pattern.removeprefix("**/"))
                    for pattern in patterns), name)


if __name__ == "__main__":
    unittest.main()
