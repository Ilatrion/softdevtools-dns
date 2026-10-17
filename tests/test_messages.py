from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.messages import (
    ValidationError,
    record_filename_advice,
    render_log,
    render_text,
    suggest_field,
    suggest_folder,
    summary_markdown,
    write_step_summary,
)


def error(index: int = 0) -> ValidationError:
    return ValidationError(
        code=f"test.{index}",
        title="Имя файла",
        message=f"Проблема {index}",
        path=f"records/ivan-{index}.yaml",
        line=1,
        column=1,
        hint="Переименуйте файл.",
    )


class ValidationErrorTests(unittest.TestCase):
    def test_log_lines_show_location_title_hint(self):
        lines = ValidationError(
            code="file.name",
            title="Имя файла",
            message="После расширения .yaml стоит лишняя точка.",
            path="records/ivan.yaml.",
            line=1,
            hint="Переименуйте файл в records/ivan.yaml.",
            details="Ожидается: records/ivan.yaml",
        ).log_lines()
        self.assertEqual(lines[0], "❌ records/ivan.yaml.:1 — Имя файла")
        self.assertIn("лишняя точка", lines[1])
        self.assertIn("Ожидается: records/ivan.yaml", "\n".join(lines))
        self.assertTrue(lines[-1].startswith("💡 "))

    def test_error_without_path_has_no_location(self):
        lines = ValidationError(
            code="pr.no_record", title="Нет новой заявки", message="Нет файла."
        ).log_lines()
        self.assertEqual(lines[0], "❌ — Нет новой заявки")

    def test_annotation_escapes_properties_and_message(self):
        annotation = ValidationError(
            code="file.name",
            title="Имя: файла",
            message="Перенос\nстроки и 100%",
            path="records/a:b,c.yaml.",
            line=2,
            column=3,
        ).annotation()
        self.assertTrue(
            annotation.startswith("::error file=records/a%3Ab%2Cc.yaml.,line=2,col=3,title=Имя%3A файла::"),
            annotation,
        )
        self.assertIn("Перенос%0Aстроки и 100%25", annotation)

    def test_annotation_skips_line_without_position(self):
        annotation = ValidationError(code="x", title="Заявка", message="Нет файла.").annotation()
        self.assertEqual(annotation, "::error title=Заявка::Нет файла.")


class RenderLogTests(unittest.TestCase):
    def test_without_annotations_only_human_text(self):
        lines = render_log([error(0)], annotations=False)
        self.assertTrue(all(not line.startswith("::error") for line in lines))
        self.assertTrue(lines[0].startswith("❌ "))

    def test_annotations_are_limited_to_ten(self):
        errors = [error(index) for index in range(12)]
        lines = render_log(errors, annotations=True)
        annotations = [line for line in lines if line.startswith("::error ")]
        self.assertEqual(len(annotations), 10)
        self.assertTrue(any("остальные 2" in line for line in lines))

    def test_render_text_is_plain(self):
        text = render_text([error(0)])
        self.assertIn("Проблема 0", text)
        self.assertNotIn("::error", text)


class SummaryTests(unittest.TestCase):
    def test_summary_lists_every_error(self):
        summary = summary_markdown([error(index) for index in range(12)])
        self.assertIn("Замечаний: 12", summary)
        self.assertIn("Проблема 11", summary)
        self.assertIn("💡 Переименуйте файл.", summary)

    def test_summary_neutralises_student_file_names(self):
        hostile = ValidationError(
            code="file.name",
            title="Имя файла",
            message="Файл не похож на заявку.",
            path="records/<img src=x onerror=y>`boom`.yaml",
            line=1,
        )
        summary = summary_markdown([hostile], repository="denrad/softdevtools-dns")
        self.assertNotIn("<img", summary)
        self.assertIn("&lt;img", summary)
        self.assertNotIn("`boom`", summary)
        self.assertIn("https://github.com/denrad/softdevtools-dns/blob/main/docs/student.md", summary)

    def test_write_step_summary_uses_environment(self):
        with TemporaryDirectory() as temp:
            target = Path(temp) / "summary.md"
            env = {"GITHUB_STEP_SUMMARY": str(target), "GITHUB_REPOSITORY": "denrad/softdevtools-dns"}
            self.assertTrue(write_step_summary([error(0)], env=env))
            written = target.read_text(encoding="utf-8")
        self.assertIn("Заявка не прошла проверку", written)
        self.assertIn("Проблема 0", written)

    def test_write_step_summary_without_environment(self):
        self.assertFalse(write_step_summary([error(0)], env={}))
        self.assertFalse(write_step_summary([], env={"GITHUB_STEP_SUMMARY": "unused"}))


class SuggestionTests(unittest.TestCase):
    def test_close_field_names(self):
        fields = ["subdomain", "type", "target", "repository"]
        self.assertEqual(suggest_field("ubdomain", fields), "subdomain")
        self.assertEqual(suggest_field("targe", fields), "target")
        self.assertIsNone(suggest_field("extra", fields))
        self.assertIsNone(suggest_field("", ["subdomain"]))

    def test_close_folder_name(self):
        self.assertEqual(suggest_folder("record"), "records")
        self.assertIsNone(suggest_folder("src"))

    def test_filename_advice_for_typos(self):
        cases = {
            "ivan.yaml.": "ivan.yaml",
            "ivan.yaml ": "ivan.yaml",
            "ivan.yaml.txt": "ivan.yaml",
            "ivan.yaml.yaml": "ivan.yaml",
            "ivan.yml": "ivan.yaml",
            "ivan.YAML": "ivan.yaml",
            "Ivan.yaml": "ivan.yaml",
            "ivan ivanov.yaml": "ivan-ivanov.yaml",
            "ivan_ivanov.yaml": "ivan-ivanov.yaml",
            "ivan": "ivan.yaml",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                suggestion, explanation = record_filename_advice(name)
                self.assertEqual(suggestion, expected)
                self.assertTrue(explanation, name)

    def test_filename_advice_gives_up_on_foreign_files(self):
        suggestion, explanation = record_filename_advice("notes.txt")
        self.assertIsNone(suggestion)
        self.assertTrue(explanation)

    def test_filename_advice_keeps_valid_name(self):
        self.assertEqual(record_filename_advice("ivan-ivanov.yaml"), (None, None))


if __name__ == "__main__":
    unittest.main()
