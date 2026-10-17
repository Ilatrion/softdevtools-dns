"""Check that a student pull request only adds new DNS request files."""

import argparse
from pathlib import Path
import os
import subprocess

from scripts.messages import (
    ValidationError,
    record_filename_advice,
    render_log,
    suggest_folder,
    write_step_summary,
)


def is_new_record(path: str) -> bool:
    """Ровно новый файл заявки: records/<имя>.yaml."""
    parts = path.split("/")
    return len(parts) == 2 and parts[0] == "records" and parts[1].endswith(".yaml")


def _record_error(code: str, title: str, message: str, path: str, hint: str, details: str | None = None):
    return ValidationError(
        code=code,
        title=title,
        message=message,
        path=path,
        line=1,
        column=1,
        hint=hint,
        details=details,
    )


def _added_path_error(path: str) -> tuple[ValidationError, bool]:
    """Ошибка для добавленного файла и признак «студент имел в виду заявку»."""
    parts = path.split("/")
    name = parts[-1]
    inside_records = len(parts) > 1 and parts[0].lower() == "records"

    if len(parts) > 2 and inside_records:
        return (
            _record_error(
                "file.nested",
                "Вложенная папка",
                f"Заявка {path} лежит во вложенной папке.",
                path,
                f"Переместите файл в records/{name}.",
                "Заявки лежат прямо в папке records/, без вложенных папок.",
            ),
            True,
        )
    if len(parts) == 2 and parts[0] != "records":
        folder = parts[0]
        if folder.lower() == "records":
            return (
                _record_error(
                    "file.folder_case",
                    "Имя папки",
                    f"Папка «{folder}» отличается от records только регистром букв.",
                    path,
                    f"Переименуйте папку в records и оставьте файл records/{name}.",
                    "GitHub различает регистр букв в именах папок.",
                ),
                True,
            )
        suggestion = suggest_folder(folder)
        if suggestion is not None:
            return (
                _record_error(
                    "file.folder",
                    "Не та папка",
                    f"Заявка {path} лежит в папке «{folder}», а не в {suggestion}/.",
                    path,
                    f"Перенесите файл в {suggestion}/{name}.",
                ),
                True,
            )
    if len(parts) == 1:
        looks_like_record = name.endswith((".yaml", ".yml"))
        return (
            _record_error(
                "file.root",
                "Файл не в той папке",
                f"Файл {name} добавлен в корень репозитория.",
                path,
                f"Перенесите его в папку records: records/{name}.",
                "Заявки лежат только в папке records/.",
            ),
            looks_like_record,
        )
    if inside_records:
        suggestion, explanation = record_filename_advice(name)
        if suggestion is not None:
            return (
                _record_error(
                    "file.name",
                    "Имя файла",
                    explanation or "Имя файла не подходит для заявки.",
                    path,
                    f"Переименуйте файл в records/{suggestion}.",
                ),
                True,
            )
        return (
            _record_error(
                "file.unexpected",
                "Лишний файл",
                f"Файл {path} не похож на заявку.",
                path,
                "В PR можно добавлять только заявки вида records/<поддомен>.yaml.",
            ),
            True,
        )
    return (
        _record_error(
            "pr.unexpected_file",
            "Лишний файл",
            f"PR добавляет файл {path}.",
            path,
            "В PR можно добавлять только новые заявки: records/<поддомен>.yaml.",
            "Служебные файлы, документацию и настройки меняет только преподаватель.",
        ),
        False,
    )


def _path_error(status: str, path: str) -> tuple[ValidationError, bool]:
    """Ошибка для одного изменённого пути и признак «это про заявку»."""
    if status == "A":
        return _added_path_error(path)
    action = "изменяет" if status == "M" else "удаляет" if status == "D" else "затрагивает"
    if status in {"M", "D"} and is_new_record(path):
        return (
            _record_error(
                "pr.modified_record" if status == "M" else "pr.deleted_record",
                "Уже принятая заявка",
                f"PR {action} заявку {path}, которая уже есть в основной ветке.",
                path,
                "Заявку из основной ветки через PR менять нельзя. Если в ней ошибка — "
                "напишите преподавателю.",
                "В PR можно только добавлять новые файлы records/<поддомен>.yaml.",
            ),
            True,
        )
    return (
        _record_error(
            "pr.changed_file",
            "Служебный файл",
            f"PR {action} файл {path}, который не является заявкой.",
            path,
            "В PR можно добавлять только новые заявки: records/<поддомен>.yaml.",
            "Служебные файлы, документацию и настройки меняет только преподаватель.",
        ),
        False,
    )


def validate_pr_changes(root: Path, base: str, head: str) -> list[ValidationError]:
    """Compare the PR head with its merge base, without reading file contents."""
    result = subprocess.run(
        ["git", "diff", "--name-status", "--no-renames", "-z", f"{base}...{head}", "--"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        return [
            ValidationError(
                code="pr.diff_failed",
                title="Сравнение изменений",
                message="Не удалось сравнить изменения PR с основной веткой.",
                details=f"Причина: {result.stderr.decode('utf-8', 'replace').strip()}",
                hint="Перезапустите проверку; если ошибка повторится — сообщите преподавателю.",
            )
        ]

    fields = result.stdout.rstrip(b"\0").split(b"\0") if result.stdout else []
    if len(fields) % 2:
        return [
            ValidationError(
                code="pr.diff_failed",
                title="Сравнение изменений",
                message="Не удалось разобрать список изменённых файлов.",
                hint="Перезапустите проверку; если ошибка повторится — сообщите преподавателю.",
            )
        ]

    errors: list[ValidationError] = []
    added_records = 0
    record_like = 0
    for status_bytes, path_bytes in zip(fields[::2], fields[1::2]):
        status = status_bytes.decode("ascii", "replace")
        path = path_bytes.decode("utf-8", "replace")
        if status == "A" and is_new_record(path):
            added_records += 1
            continue
        error, is_record_like = _path_error(status, path)
        errors.append(error)
        if is_record_like:
            record_like += 1
    if not added_records and not record_like:
        errors.append(
            ValidationError(
                code="pr.no_record_added",
                title="Нет новой заявки",
                message="В этом PR нет новой заявки records/*.yaml.",
                hint="Добавьте файл records/<ваш-поддомен>.yaml, например records/ivan-ivanov.yaml.",
                details="Заявка — это четыре строки: subdomain, type, target, repository.",
            )
        )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Check files changed by a student DNS request PR")
    parser.add_argument("base", help="base branch commit SHA")
    parser.add_argument("head", help="PR head commit SHA")
    parser.add_argument("root", nargs="?", type=Path, default=Path("."))
    parser.add_argument(
        "--annotations",
        action="store_true",
        help="печатать аннотации GitHub Actions (в CI включается само)",
    )
    args = parser.parse_args()
    errors = validate_pr_changes(args.root, args.base, args.head)
    annotations = args.annotations or os.environ.get("GITHUB_ACTIONS") == "true"
    for line in render_log(errors, annotations=annotations):
        print(line)
    if errors:
        write_step_summary(errors)
        return 1
    print("Изменения PR корректны.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
