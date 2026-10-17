from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from scripts.validate_pr import _added_path_error, validate_pr_changes


def codes(errors):
    return [error.code for error in errors]


def find(errors, code):
    return next((error for error in errors if error.code == code), None)


class AddedPathErrorTests(unittest.TestCase):
    """Разбор пути не зависит от файловой системы."""

    def classify(self, path):
        error, looks_like_record = _added_path_error(path)
        return error, looks_like_record

    def test_record_at_repository_root(self):
        error, looks_like_record = self.classify("ivan.yaml")
        self.assertEqual(error.code, "file.root")
        self.assertIn("records/ivan.yaml", error.hint)
        self.assertTrue(looks_like_record)

    def test_folder_case_difference(self):
        error, _ = self.classify("Records/ivan.yaml")
        self.assertEqual(error.code, "file.folder_case")
        self.assertIn("регистр", error.message)

    def test_folder_typo_is_suggested(self):
        error, looks_like_record = self.classify("record/ivan.yaml")
        self.assertEqual(error.code, "file.folder")
        self.assertIn("records/ivan.yaml", error.hint)
        self.assertTrue(looks_like_record)

    def test_nested_record(self):
        error, looks_like_record = self.classify("records/nested/ivan.yaml")
        self.assertEqual(error.code, "file.nested")
        self.assertIn("records/ivan.yaml", error.hint)
        self.assertTrue(looks_like_record)

    def test_extra_dot_in_extension(self):
        error, looks_like_record = self.classify("records/sofia-surnaeva.yaml.")
        self.assertEqual(error.code, "file.name")
        self.assertIn("точка", error.message)
        self.assertEqual(error.hint, "Переименуйте файл в records/sofia-surnaeva.yaml.")
        self.assertTrue(looks_like_record)

    def test_yml_extension(self):
        error, _ = self.classify("records/ivan.yml")
        self.assertEqual(error.code, "file.name")
        self.assertIn(".yml", error.message)

    def test_other_file_is_not_record_like(self):
        error, looks_like_record = self.classify("docs/student.md")
        self.assertEqual(error.code, "pr.unexpected_file")
        self.assertFalse(looks_like_record)

    def test_errors_point_to_first_line(self):
        error, _ = self.classify("records/ivan.yaml.")
        self.assertEqual((error.path, error.line, error.column), ("records/ivan.yaml.", 1, 1))


class ValidatePullRequestChangesTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test")
        (self.root / "records").mkdir()
        (self.root / "records" / "existing.yaml").write_text("existing\n", encoding="utf-8")
        (self.root / "README.md").write_text("readme\n", encoding="utf-8")
        self.commit()
        self.base = self.git("rev-parse", "HEAD").stdout.strip()

    def git(self, *args):
        return subprocess.run(
            ["git", *args], cwd=self.root, text=True, capture_output=True, check=True
        )

    def commit(self):
        self.git("add", "-A")
        self.git("commit", "-qm", "test")

    def check(self):
        return validate_pr_changes(self.root, self.base, self.git("rev-parse", "HEAD").stdout.strip())

    def test_new_record_is_allowed(self):
        (self.root / "records" / "ivan.yaml").write_text("new\n", encoding="utf-8")
        self.commit()
        self.assertEqual(self.check(), [])

    def test_existing_record_cannot_be_modified(self):
        (self.root / "records" / "existing.yaml").write_text("changed\n", encoding="utf-8")
        self.commit()
        error = find(self.check(), "pr.modified_record")
        self.assertIn("records/existing.yaml", error.message)
        self.assertIn("напишите преподавателю", error.hint)

    def test_existing_record_cannot_be_deleted(self):
        (self.root / "records" / "existing.yaml").unlink()
        self.commit()
        self.assertEqual(codes(self.check()), ["pr.deleted_record"])

    def test_other_paths_are_rejected_next_to_a_record(self):
        (self.root / "README.md").write_text("changed\n", encoding="utf-8")
        (self.root / "records" / "ivan.yaml").write_text("new\n", encoding="utf-8")
        self.commit()
        errors = self.check()
        self.assertEqual(codes(errors), ["pr.changed_file"])
        self.assertIn("README.md", errors[0].message)

    def test_changed_other_file_alone_adds_main_reason(self):
        (self.root / "README.md").write_text("changed\n", encoding="utf-8")
        self.commit()
        self.assertEqual(sorted(codes(self.check())), ["pr.changed_file", "pr.no_record_added"])

    def test_typo_in_name_replaces_secondary_message(self):
        (self.root / "records" / "ivan.yaml.").write_text("new\n", encoding="utf-8")
        self.commit()
        errors = self.check()
        self.assertEqual(codes(errors), ["file.name"])
        self.assertIn("Переименуйте файл в records/ivan.yaml.", errors[0].hint)

    def test_nested_record_is_rejected(self):
        (self.root / "records" / "nested").mkdir()
        (self.root / "records" / "nested" / "ivan.yaml").write_text("new\n", encoding="utf-8")
        self.commit()
        self.assertEqual(codes(self.check()), ["file.nested"])

    def test_no_changes_at_all(self):
        errors = self.check()
        self.assertEqual(codes(errors), ["pr.no_record_added"])
        self.assertIn("records/ivan-ivanov.yaml", errors[0].hint)

    def test_diff_failure_is_explained(self):
        errors = validate_pr_changes(self.root, "0" * 40, self.base)
        self.assertEqual(codes(errors), ["pr.diff_failed"])
        self.assertIn("Перезапустите проверку", errors[0].hint)


if __name__ == "__main__":
    unittest.main()
