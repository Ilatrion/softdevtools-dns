import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from scripts.messages import render_text
from scripts.validate import parse_record, validate_records


VALID = """subdomain: ivan
type: CNAME
target: ivan123.github.io
repository: https://github.com/ivan123/my-lab-site
"""


def codes(errors):
    return [error.code for error in errors]


def find(errors, code):
    return next((error for error in errors if error.code == code), None)


class ParseRecordTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "ivan.yaml"

    def parse(self, content: str):
        self.path.write_text(content, encoding="utf-8")
        return parse_record(self.path)

    def test_valid_record(self):
        record, errors, positions = self.parse(VALID)
        self.assertEqual(errors, [])
        self.assertEqual(record["subdomain"], "ivan")
        self.assertEqual(positions["subdomain"], [(1, 1)])

    def test_malformed_yaml_explains_syntax(self):
        record, errors, _ = self.parse("subdomain: [unterminated")
        self.assertIsNone(record)
        self.assertEqual(codes(errors), ["yaml.invalid"])
        self.assertIn("отступы", errors[0].hint)

    def test_duplicate_key_reports_both_lines(self):
        record, errors, _ = self.parse(VALID + "subdomain: stolen\n")
        self.assertIsNone(record)
        self.assertEqual(codes(errors), ["field.duplicate"])
        self.assertIn("дважды", errors[0].message)
        self.assertIn("1, 5", errors[0].message)
        self.assertEqual(errors[0].line, 5)

    def test_multiple_documents(self):
        record, errors, _ = self.parse(VALID + "---\nsubdomain: other\n")
        self.assertIsNone(record)
        self.assertEqual(codes(errors), ["yaml.multiple_documents"])

    def test_empty_file(self):
        record, errors, _ = self.parse("")
        self.assertIsNone(record)
        self.assertEqual(codes(errors), ["record.empty"])
        self.assertIn("examples/ivan-ivanov.yaml", errors[0].hint)

    def test_not_a_mapping(self):
        record, errors, _ = self.parse("- subdomain: ivan\n")
        self.assertIsNone(record)
        self.assertEqual(codes(errors), ["record.not_mapping"])

    def test_unknown_and_missing_fields(self):
        content = VALID.replace("repository: https://github.com/ivan123/my-lab-site\n", "extra: value\n")
        record, errors, _ = self.parse(content)
        self.assertIsNone(record)
        self.assertEqual(sorted(codes(errors)), ["field.missing", "field.unknown"])
        missing = find(errors, "field.missing")
        unknown = find(errors, "field.unknown")
        self.assertEqual(missing.message, "В заявке нет обязательного поля «repository».")
        self.assertIn("repository: https://github.com", missing.hint)
        self.assertIn("extra", unknown.message)
        self.assertIn("subdomain, type, target, repository", unknown.details)

    def test_typo_in_field_name_is_suggested(self):
        content = VALID.replace("subdomain: ivan", "ubdomain: ivan")
        record, errors, _ = self.parse(content)
        self.assertIsNone(record)
        self.assertIn("замените «ubdomain» на «subdomain»", find(errors, "field.unknown").hint)
        self.assertIn("переименуйте его в «subdomain»", find(errors, "field.missing").hint)

    def test_list_value_is_explained(self):
        record, errors, _ = self.parse(VALID.replace("type: CNAME", "type: [CNAME]"))
        self.assertIsNone(record)
        self.assertEqual(codes(errors), ["field.not_string"])
        self.assertIn("списком", errors[0].message)
        self.assertIn("Оставьте одно значение", errors[0].hint)

    def test_numeric_value_suggests_quotes(self):
        record, errors, _ = self.parse(VALID.replace("subdomain: ivan", "subdomain: 123"))
        self.assertIsNone(record)
        self.assertEqual(codes(errors), ["field.not_string"])
        self.assertIn("числом", errors[0].message)
        self.assertIn('subdomain: "123"', errors[0].hint)

    def test_empty_value_is_explained(self):
        record, errors, _ = self.parse(VALID.replace("target: ivan123.github.io", "target:"))
        self.assertIsNone(record)
        self.assertEqual(codes(errors), ["field.not_string"])
        self.assertIn("пустым значением", errors[0].message)

    def test_alias_is_rejected(self):
        content = VALID.replace("subdomain: ivan", "subdomain: &name ivan").replace(
            "type: CNAME", "type: *name"
        )
        record, errors, _ = self.parse(content)
        self.assertIsNone(record)
        self.assertEqual(codes(errors), ["yaml.alias"])

    def test_oversized_file(self):
        record, errors, _ = self.parse(VALID + " " * 8192)
        self.assertIsNone(record)
        self.assertEqual(codes(errors), ["file.too_large"])

    def test_not_utf8_file(self):
        self.path.write_bytes("subdomain: Иван\n".encode("cp1251"))
        record, errors, _ = parse_record(self.path)
        self.assertIsNone(record)
        self.assertEqual(codes(errors), ["file.encoding"])
        self.assertIn("UTF-8", errors[0].hint)

    def test_errors_point_to_record_name(self):
        record, errors, _ = parse_record(self.path.parent / "missing.yaml", display="records/ivan.yaml")
        self.assertIsNone(record)
        self.assertEqual(errors[0].path, "records/ivan.yaml")


class ValidateRecordsTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "records").mkdir()
        (self.root / "config").mkdir()
        (self.root / "config" / "reserved-names.txt").write_text(
            "# Teacher-owned names\nwww\nadmin\n", encoding="utf-8"
        )

    def write(self, filename: str, content: str = VALID):
        (self.root / "records" / filename).write_text(content, encoding="utf-8")

    def test_valid_tree_and_empty_tree(self):
        self.assertEqual(validate_records(self.root), [])
        self.write("ivan.yaml")
        self.assertEqual(validate_records(self.root), [])

    def test_reserved_name(self):
        self.write("www.yaml", VALID.replace("subdomain: ivan", "subdomain: www"))
        error = find(validate_records(self.root), "subdomain.reserved")
        self.assertIn("зарезервировано", error.message)
        self.assertIn("Выберите другое имя", error.hint)

    def test_invalid_subdomain(self):
        self.write("bad.yaml", VALID.replace("subdomain: ivan", "subdomain: a.b"))
        error = find(validate_records(self.root), "subdomain.invalid")
        self.assertIn("без точек", error.hint)

    def test_subdomain_fits_github_pages_https_name_limit(self):
        allowed = "a" * 47  # 47 + len(".softdevtools.ru") == 63
        rejected = "a" * 48
        self.write(f"{allowed}.yaml", VALID.replace("subdomain: ivan", f"subdomain: {allowed}"))
        self.assertEqual(validate_records(self.root), [])
        (self.root / "records" / f"{allowed}.yaml").unlink()
        self.write(f"{rejected}.yaml", VALID.replace("subdomain: ivan", f"subdomain: {rejected}"))
        error = find(validate_records(self.root), "subdomain.too_long")
        self.assertIn("47", error.message)
        self.assertIn("HTTPS-сертификат", error.details)

    def test_uppercase_subdomain_suggests_lowercase(self):
        self.write("ivan.yaml", VALID.replace("subdomain: ivan", "subdomain: Ivan"))
        error = find(validate_records(self.root), "subdomain.invalid")
        self.assertIn("строчные: ivan", error.hint)

    def test_underscore_subdomain_suggests_hyphen(self):
        self.write("ivan_ivanov.yaml", VALID.replace("subdomain: ivan", "subdomain: ivan_ivanov"))
        error = find(validate_records(self.root), "subdomain.invalid")
        self.assertIn("дефис", error.hint)

    def test_invalid_target(self):
        self.write("ivan.yaml", VALID.replace("ivan123.github.io", "example.com"))
        error = find(validate_records(self.root), "target.invalid")
        self.assertIn("example.com", error.message)
        self.assertIn("ivan123.github.io", error.hint)

    def test_target_with_scheme_is_explained(self):
        self.write(
            "ivan.yaml",
            VALID.replace("target: ivan123.github.io", "target: https://ivan123.github.io"),
        )
        error = find(validate_records(self.root), "target.invalid")
        self.assertIn("Уберите схему", error.hint)

    def test_target_accepts_github_username_case(self):
        content = VALID.replace("ivan123.github.io", "Ivan123.GitHub.IO").replace(
            "github.com/ivan123/", "github.com/IVAN123/"
        )
        self.write("ivan.yaml", content)
        self.assertEqual(validate_records(self.root), [])

    def test_target_still_rejects_non_ascii_username(self):
        content = VALID.replace("ivan123.github.io", "Kvan123.github.io").replace(
            "github.com/ivan123/", "github.com/kvan123/"
        )
        self.write("ivan.yaml", content)
        self.assertEqual(codes(validate_records(self.root)), ["target.invalid"])

    def test_github_username_length_limit(self):
        for length in (39, 40):
            with self.subTest(length=length):
                username = "a" * length
                content = VALID.replace("ivan123.github.io", f"{username}.github.io").replace(
                    "github.com/ivan123/", f"github.com/{username}/"
                )
                self.write("ivan.yaml", content)
                errors = validate_records(self.root)
                if length == 39:
                    self.assertEqual(errors, [])
                else:
                    self.assertEqual(codes(errors), ["target.invalid"])

    def test_invalid_repository(self):
        self.write("ivan.yaml", VALID.replace("https://github.com/", "git@github.com:"))
        error = find(validate_records(self.root), "repository.invalid")
        self.assertIn("https://github.com/", error.hint)

    def test_repository_owner_must_match_target(self):
        self.write("ivan.yaml", VALID.replace("github.com/ivan123/", "github.com/other/"))
        error = find(validate_records(self.root), "repository.owner_mismatch")
        self.assertIn("«other»", error.message)
        self.assertIn("«ivan123»", error.message)
        self.assertIn("other.github.io", error.hint)

    def test_duplicate_subdomain(self):
        self.write("ivan.yaml")
        self.write("other.yaml")
        error = find(validate_records(self.root), "subdomain.duplicate")
        self.assertIn("records/ivan.yaml", error.message)

    def test_filename_must_match_subdomain(self):
        self.write("other.yaml")
        error = find(validate_records(self.root), "filename.mismatch")
        self.assertIn("records/ivan.yaml", error.hint)

    def test_type_must_be_cname(self):
        self.write("ivan.yaml", VALID.replace("type: CNAME", "type: TXT"))
        error = find(validate_records(self.root), "type.invalid")
        self.assertIn("TXT", error.message)
        self.assertIn("type: CNAME", error.hint)

    def test_errors_point_to_yaml_lines(self):
        self.write("ivan.yaml", VALID.replace("type: CNAME", "type: TXT"))
        error = find(validate_records(self.root), "type.invalid")
        self.assertEqual((error.path, error.line, error.column), ("records/ivan.yaml", 2, 1))

    def test_missing_reserved_names_configuration(self):
        (self.root / "config" / "reserved-names.txt").unlink()
        self.write("ivan.yaml")
        error = find(validate_records(self.root), "config.reserved_names")
        self.assertIn("config/reserved-names.txt", error.path)

    def test_unexpected_file_in_records(self):
        self.write("notes.txt", "hello")
        error = find(validate_records(self.root), "file.unexpected")
        self.assertIn("notes.txt", error.path)

    def test_typo_in_extension_is_explained(self):
        self.write("ivan.yaml.", VALID)
        error = find(validate_records(self.root), "file.name")
        self.assertIn("точка", error.message)
        self.assertIn("Переименуйте файл в records/ivan.yaml", error.hint)

    def test_wrong_extension_is_explained(self):
        self.write("ivan.yml", VALID)
        error = find(validate_records(self.root), "file.name")
        self.assertIn(".yml", error.message)

    def test_directory_inside_records(self):
        (self.root / "records" / "nested").mkdir()
        error = find(validate_records(self.root), "file.directory")
        self.assertIn("nested", error.message)

    def test_symlink_is_rejected(self):
        source = self.root / "outside.yaml"
        source.write_text(VALID, encoding="utf-8")
        (self.root / "records" / "ivan.yaml").symlink_to(source)
        self.assertEqual(codes(validate_records(self.root)), ["file.symlink"])

    def test_records_directory_symlink_is_rejected(self):
        (self.root / "records").rmdir()
        outside = self.root / "outside"
        outside.mkdir()
        (self.root / "records").symlink_to(outside, target_is_directory=True)
        self.assertEqual(codes(validate_records(self.root)), ["records.symlink"])

    def test_missing_records_directory(self):
        (self.root / "records").rmdir()
        self.assertEqual(codes(validate_records(self.root)), ["records.missing"])

    def test_messages_are_readable_without_source_code(self):
        self.write("ivan.yaml", VALID.replace("type: CNAME", "type: TXT"))
        text = render_text(validate_records(self.root))
        self.assertIn("❌ records/ivan.yaml:2 — Поле type", text)
        self.assertIn("Тип записи «TXT» не подходит.", text)
        self.assertIn("💡 Поставьте `type: CNAME`.", text)


class ValidateCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "records").mkdir()
        (self.root / "config").mkdir()
        (self.root / "config" / "reserved-names.txt").write_text("www\n", encoding="utf-8")
        (self.root / "records" / "ivan.yaml").write_text(VALID, encoding="utf-8")
        self.project = Path(__file__).resolve().parents[1]
        self.command = [sys.executable, "-m", "scripts.validate", str(self.root)]

    # Переменные GitHub Actions из окружения не должны влиять на проверки ниже.
    ACTIONS_ENV = ("GITHUB_ACTIONS", "GITHUB_STEP_SUMMARY", "GITHUB_REPOSITORY")

    def run_cli(self, env=None):
        base = {key: value for key, value in os.environ.items() if key not in self.ACTIONS_ENV}
        return subprocess.run(
            self.command,
            cwd=self.project,
            capture_output=True,
            text=True,
            env={**base, **(env or {})},
        )

    def test_success_message(self):
        result = self.run_cli()
        self.assertEqual(result.returncode, 0)
        self.assertIn("Заявки корректны.", result.stdout)

    def test_failure_lists_file_and_reason(self):
        (self.root / "records" / "ivan.yaml").write_text(
            VALID.replace("type: CNAME", "type: TXT"), encoding="utf-8"
        )
        result = self.run_cli()
        self.assertEqual(result.returncode, 1)
        self.assertIn("records/ivan.yaml", result.stdout)
        self.assertIn("CNAME", result.stdout)

    def test_annotations_only_inside_actions(self):
        (self.root / "records" / "ivan.yaml").write_text(
            VALID.replace("type: CNAME", "type: TXT"), encoding="utf-8"
        )
        plain = self.run_cli()
        self.assertNotIn("::error", plain.stdout)
        annotated = self.run_cli({"GITHUB_ACTIONS": "true"})
        self.assertIn("::error file=records/ivan.yaml,line=2", annotated.stdout)

    def test_step_summary_is_written_when_requested(self):
        (self.root / "records" / "ivan.yaml").write_text(
            VALID.replace("type: CNAME", "type: TXT"), encoding="utf-8"
        )
        summary = self.root / "summary.md"
        result = self.run_cli({"GITHUB_STEP_SUMMARY": str(summary)})
        self.assertEqual(result.returncode, 1)
        self.assertIn("Заявка не прошла проверку", summary.read_text(encoding="utf-8"))

    def test_annotations_flag_works_outside_actions(self):
        (self.root / "records" / "ivan.yaml").write_text(
            VALID.replace("type: CNAME", "type: TXT"), encoding="utf-8"
        )
        base = {key: value for key, value in os.environ.items() if key not in self.ACTIONS_ENV}
        result = subprocess.run(
            [*self.command, "--annotations"],
            cwd=self.project,
            capture_output=True,
            text=True,
            env=base,
        )
        self.assertIn("::error file=records/ivan.yaml", result.stdout)


if __name__ == "__main__":
    unittest.main()
