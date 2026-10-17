"""Read-only validation of student DNS request files."""

import argparse
from pathlib import Path
import os
import re

import yaml

from scripts.messages import (
    EXAMPLE_REQUEST,
    ValidationError,
    record_filename_advice,
    render_log,
    suggest_field,
    write_step_summary,
)


FIELDS = frozenset({"subdomain", "type", "target", "repository"})
MAX_BYTES = 8192
LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
GITHUB_USER = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?\Z")
MAX_PAGES_SUBDOMAIN_LENGTH = 63 - len(".softdevtools.ru")
REPOSITORY = re.compile(
    r"https://github\.com/([A-Za-z0-9][A-Za-z0-9-]{0,62})/([A-Za-z0-9._-]+)\Z"
)
FIELD_ORDER = ("subdomain", "type", "target", "repository")
FIELD_EXAMPLES = {
    "subdomain": "subdomain: imya-familiya",
    "type": "type: CNAME",
    "target": "target: <ваш-логин>.github.io",
    "repository": "repository: https://github.com/<ваш-логин>/<репозиторий>",
}


class RequestLoader(yaml.SafeLoader):
    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise yaml.YAMLError("YAML aliases are not allowed")
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in result:
                raise yaml.YAMLError(f"duplicate YAML key: {key}")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def key_positions(text: str) -> dict[str, list[tuple[int, int]]]:
    """Позиции (строка, колонка) ключей YAML, начиная с 1; для аннотаций."""
    try:
        node = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        return {}
    if not isinstance(node, yaml.MappingNode):
        return {}
    positions: dict[str, list[tuple[int, int]]] = {}
    for key_node, _value_node in node.value:
        if isinstance(key_node, yaml.ScalarNode) and isinstance(key_node.value, str):
            positions.setdefault(key_node.value, []).append(
                (key_node.start_mark.line + 1, key_node.start_mark.column + 1)
            )
    return positions


def _position(
    positions: dict[str, list[tuple[int, int]]], key: str
) -> tuple[int | None, int | None]:
    found = positions.get(key)
    if not found:
        return None, None
    return found[0]


def _problem_line(exc: yaml.YAMLError) -> int | None:
    mark = getattr(exc, "problem_mark", None)
    if mark is None:
        return None
    return mark.line + 1


def parse_record(
    path: Path, *, display: str | None = None
) -> tuple[dict[str, str] | None, list[ValidationError], dict[str, list[tuple[int, int]]]]:
    """Return one four-field YAML record or errors; never write to disk.

    Третий элемент — позиции полей, чтобы сообщение указывало на строку.
    """
    shown = display or str(path)

    def broken(code: str, title: str, message: str, **kwargs) -> ValidationError:
        return ValidationError(code=code, title=title, message=message, path=shown, **kwargs)

    try:
        with path.open("rb") as source:
            raw = source.read(MAX_BYTES + 1)
    except OSError as exc:
        return None, [
            broken(
                "file.unreadable",
                "Файл заявки",
                "Файл не удалось прочитать.",
                details=f"Причина: {exc}",
                hint="Проверьте имя файла. Если ошибка повторяется — сообщите преподавателю.",
            )
        ], {}
    if len(raw) > MAX_BYTES:
        return None, [
            broken(
                "file.too_large",
                "Размер файла",
                f"Файл больше {MAX_BYTES} байт.",
                hint="В заявке всего четыре строки: проверьте, не добавлен ли лишний текст.",
            )
        ], {}

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return None, [
            broken(
                "file.encoding",
                "Кодировка файла",
                "Файл не удалось прочитать как UTF-8.",
                details=f"Причина: {exc}",
                hint="Сохраните файл в кодировке UTF-8 и загрузите его заново.",
            )
        ], {}

    positions = key_positions(text)
    try:
        data = yaml.load(text, Loader=RequestLoader)
    except yaml.YAMLError as exc:
        return None, [_yaml_error(broken, exc, positions)], positions

    if data is None:
        return None, [
            broken(
                "record.empty",
                "Пустая заявка",
                "Файл заявки пустой.",
                hint=f"Скопируйте четыре строки из {EXAMPLE_REQUEST}.",
            )
        ], positions
    if not isinstance(data, dict):
        return None, [
            broken(
                "record.not_mapping",
                "Формат заявки",
                "Заявка должна состоять из четырёх строк «поле: значение».",
                details="Похоже, в файле список или другое значение, а не набор полей.",
                hint=f"Возьмите за основу {EXAMPLE_REQUEST}.",
            )
        ], positions

    errors = []
    for field in sorted(FIELDS - data.keys()):
        line, column = _position(positions, field)
        typo = suggest_field(field, [key for key in data if isinstance(key, str)])
        if typo is None:
            hint = (
                "Добавьте строку с этим полем: "
                f"`{FIELD_EXAMPLES[field]}`. Пример заявки: {EXAMPLE_REQUEST}."
            )
        else:
            hint = f"Похоже на опечатку в поле «{typo}»: переименуйте его в «{field}»."
        errors.append(
            broken(
                "field.missing",
                f"Поле {field}",
                f"В заявке нет обязательного поля «{field}».",
                line=line,
                column=column,
                hint=hint,
            )
        )
    for field in sorted(data.keys() - FIELDS, key=str):
        line, column = _position(positions, field) if isinstance(field, str) else (None, None)
        typo = suggest_field(field, FIELDS) if isinstance(field, str) else None
        if typo is None:
            hint = "Удалите это поле: в заявке нужны только четыре поля."
        else:
            hint = f"Похоже на опечатку: замените «{field}» на «{typo}»."
        errors.append(
            broken(
                "field.unknown",
                "Лишнее поле",
                f"Поле «{field}» в заявке не поддерживается.",
                line=line,
                column=column,
                hint=hint,
                details=f"Допустимые поля: {', '.join(FIELD_ORDER)}.",
            )
        )
    for field in sorted(FIELDS & data.keys()):
        if type(data[field]) is str:
            continue
        line, column = _position(positions, field)
        errors.append(
            broken(
                "field.not_string",
                f"Поле {field}",
                f"Значение поля «{field}» должно быть текстом, а не {_type_name(data[field])}.",
                line=line,
                column=column,
                hint=_type_hint(field, data[field]),
                details=f"Ожидается, например: {FIELD_EXAMPLES[field]}",
            )
        )
    if errors:
        return None, errors, positions
    return data, [], positions


def _yaml_error(broken, exc: yaml.YAMLError, positions) -> ValidationError:
    """Понятное объяснение вместо технического текста PyYAML."""
    context = getattr(exc, "context", None) or ""
    problem = (getattr(exc, "problem", None) or str(exc)).strip()
    described = f"{context} {problem}".strip()
    line = _problem_line(exc)
    if problem.startswith("duplicate YAML key: "):
        field = problem.removeprefix("duplicate YAML key: ")
        found = positions.get(field, [])
        lines = ", ".join(str(item[0]) for item in found)
        return broken(
            "field.duplicate",
            "Повтор поля",
            f"Поле «{field}» указано в файле дважды"
            + (f" (строки {lines})." if lines else "."),
            line=found[1][0] if len(found) > 1 else None,
            column=found[1][1] if len(found) > 1 else None,
            hint=f"Оставьте только одну строку «{field}: …».",
        )
    if "aliases are not allowed" in problem:
        return broken(
            "yaml.alias",
            "YAML-ссылки",
            "В файле используются YAML-ссылки («&имя» или «*имя»).",
            hint="Напишите значения обычным текстом, без ссылок.",
        )
    if "single document" in described:
        return broken(
            "yaml.multiple_documents",
            "Несколько заявок",
            "В файле несколько документов YAML (разделитель «---»).",
            hint="Оставьте одну заявку: четыре строки «поле: значение».",
        )
    return broken(
        "yaml.invalid",
        "Синтаксис YAML",
        "Текст заявки не удалось разобрать как YAML.",
        line=line,
        hint="Проверьте отступы (пробелы, не табуляция) и двоеточия после имён полей. "
        f"Пример заявки: {EXAMPLE_REQUEST}.",
        details=f"Техническое пояснение: {problem}",
    )


def _type_name(value: object) -> str:
    if value is None:
        return "пустым значением"
    if isinstance(value, bool):
        return "логическим значением"
    if isinstance(value, (int, float)):
        return "числом"
    if isinstance(value, list):
        return "списком"
    if isinstance(value, dict):
        return "набором полей"
    return "значением другого типа"


def _type_hint(field: str, value: object) -> str:
    if value is None:
        return f"После двоеточия укажите значение: {FIELD_EXAMPLES[field]}"
    if isinstance(value, bool):
        return f"Нужен текст. Пример: {FIELD_EXAMPLES[field]}"
    if isinstance(value, (int, float)):
        return f"Заключите значение в кавычки, если оно состоит из цифр: {field}: \"{value}\""
    if isinstance(value, (list, dict)):
        return f"Оставьте одно значение, без списков и вложенных полей: {FIELD_EXAMPLES[field]}"
    return f"Укажите текст: {FIELD_EXAMPLES[field]}"


def _subdomain_hint(name: str) -> str:
    if any(character.isupper() for character in name):
        return f"Замените заглавные буквы на строчные: {name.lower()}."
    if "_" in name:
        return "В DNS-имени используется дефис, а не подчёркивание: ivan-ivanov."
    if " " in name:
        return "Уберите пробелы: ivan-ivanov."
    if not name.isascii():
        return "Используйте латинские буквы: ivan-ivanov."
    if "." in name or "/" in name:
        return "Поддомен — одно слово без точек и косых черт: ivan-ivanov."
    if name.startswith("-") or name.endswith("-"):
        return "Уберите дефис в начале или в конце имени."
    return "Используйте строчные латинские буквы, цифры и дефис: ivan-ivanov."


def broken_of(display: str):
    """Сборщик ошибок с общим путём: используется проверками полей заявки."""

    def make(code: str, title: str, message: str, **kwargs) -> ValidationError:
        return ValidationError(code=code, title=title, message=message, path=display, **kwargs)

    return make


def _target_error(broken, raw_target: str, line: int | None, column: int | None):
    target = raw_target.lower()
    target_user = target.removesuffix(".github.io")
    expected = "Ожидается адрес вашего сайта на GitHub Pages: <ваш-логин>.github.io."
    if not raw_target.isascii():
        problem = "В target есть символы не из латиницы."
        hint = "Логин GitHub пишется латинскими буквами: ivan123.github.io."
    elif raw_target.startswith(("http://", "https://")):
        problem = "В target указана ссылка со схемой http:// или https://."
        hint = "Уберите схему: нужно `ivan123.github.io`, без `https://`."
    elif "/" in raw_target:
        problem = "В target указан путь или название репозитория."
        hint = "Оставьте только `<ваш-логин>.github.io`, без названия репозитория."
    elif raw_target != raw_target.rstrip("."):
        problem = "В target есть точка в конце."
        hint = "Уберите точку в конце: `ivan123.github.io`."
    elif not target.endswith(".github.io"):
        problem = f"«{raw_target}» не похоже на адрес GitHub Pages."
        hint = (
            "Укажите `<ваш-логин>.github.io` — адрес, который показывает GitHub Pages "
            "(например, ivan123.github.io)."
        )
    else:
        problem = f"Логин «{target_user}» из target не похож на логин GitHub."
        hint = "Проверьте логин: до 39 символов, без пробелов; регистр букв не важен."
    return broken(
        "target.invalid",
        "Поле target",
        problem,
        line=line,
        column=column,
        hint=hint,
        details=expected,
    )


def _repository_error(broken, raw_repository: str, line: int | None, column: int | None):
    if raw_repository.startswith("git@"):
        hint = "Нужна обычная ссылка: https://github.com/<логин>/<репозиторий>."
    elif raw_repository.startswith("http://"):
        hint = "Замените http:// на https://."
    elif not raw_repository.startswith("https://github.com/"):
        hint = "Ссылка должна начинаться с https://github.com/."
    elif raw_repository.endswith("/"):
        hint = "Уберите косую черту в конце ссылки."
    else:
        hint = "Скопируйте адрес репозитория из адресной строки браузера."
    return broken(
        "repository.invalid",
        "Поле repository",
        f"Ссылка «{raw_repository}» не похожа на адрес репозитория на GitHub.",
        line=line,
        column=column,
        hint=hint,
        details="Ожидается https://github.com/<ваш-логин>/<репозиторий>.",
    )


def validate_records(root: Path) -> list[ValidationError]:
    """Validate the local record tree without contacting external services."""
    errors: list[ValidationError] = []
    reserved_path = root / "config" / "reserved-names.txt"
    try:
        reserved = {
            line.strip().lower()
            for line in reserved_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
    except OSError as exc:
        errors.append(
            ValidationError(
                code="config.reserved_names",
                title="Служебный файл",
                message="Не удалось прочитать список зарезервированных имён.",
                path="config/reserved-names.txt",
                details=f"Причина: {exc}",
                hint="Это проблема репозитория, а не заявки: сообщите преподавателю.",
            )
        )
        reserved = set()

    records_dir = root / "records"
    if records_dir.is_symlink():
        return errors + [
            ValidationError(
                code="records.symlink",
                title="Папка records",
                message="Папка records — символическая ссылка.",
                path="records",
                hint="Нужна обычная папка с файлами заявок.",
            )
        ]
    if not records_dir.is_dir():
        return errors + [
            ValidationError(
                code="records.missing",
                title="Папка records",
                message="В репозитории нет папки records.",
                path="records",
                hint="Это проблема репозитория, а не заявки: сообщите преподавателю.",
            )
        ]

    seen: dict[str, str] = {}
    for path in sorted(records_dir.iterdir()):
        label = f"records/{path.name}"
        if path.name == ".gitkeep":
            continue
        if path.is_symlink():
            errors.append(
                ValidationError(
                    code="file.symlink",
                    title="Символическая ссылка",
                    message="Файл заявки — символическая ссылка.",
                    path=label,
                    hint="Загрузите обычный файл с заявкой.",
                )
            )
            continue
        if path.is_dir():
            errors.append(
                ValidationError(
                    code="file.directory",
                    title="Лишняя папка",
                    message=f"В папке records оказалась папка «{path.name}».",
                    path=label,
                    hint="Заявка — это файл records/<поддомен>.yaml, без вложенных папок.",
                )
            )
            continue
        if not path.is_file() or path.suffix != ".yaml":
            suggestion, explanation = record_filename_advice(path.name)
            if suggestion is None:
                errors.append(
                    ValidationError(
                        code="file.unexpected",
                        title="Лишний файл",
                        message="Файл в папке records не похож на заявку.",
                        path=label,
                        hint="Удалите файл или переименуйте его в records/<поддомен>.yaml.",
                        details=f"Пример имени заявки: {Path(EXAMPLE_REQUEST).name}",
                    )
                )
            else:
                errors.append(
                    ValidationError(
                        code="file.name",
                        title="Имя файла",
                        message=explanation or "Имя файла не подходит для заявки.",
                        path=label,
                        hint=f"Переименуйте файл в records/{suggestion}.",
                    )
                )
            continue

        record, file_errors, positions = parse_record(path, display=label)
        errors.extend(file_errors)
        if record is None:
            continue

        name = record["subdomain"]
        line, column = _position(positions, "subdomain")
        if not LABEL.fullmatch(name):
            errors.append(
                ValidationError(
                    code="subdomain.invalid",
                    title="Поле subdomain",
                    message=f"Имя «{name}» не подходит для поддомена.",
                    path=label,
                    line=line,
                    column=column,
                    hint=_subdomain_hint(name),
                    details="Допустимы строчные латинские буквы, цифры и дефис: ivan-ivanov.",
                )
            )
        elif len(name) > MAX_PAGES_SUBDOMAIN_LENGTH:
            errors.append(
                ValidationError(
                    code="subdomain.too_long",
                    title="Поле subdomain",
                    message=f"Имя «{name}» длиннее {MAX_PAGES_SUBDOMAIN_LENGTH} символов.",
                    path=label,
                    line=line,
                    column=column,
                    hint="Выберите имя короче: например, только фамилию.",
                    details="Ограничение GitHub Pages: иначе HTTPS-сертификат для "
                    "<имя>.softdevtools.ru не выпустится.",
                )
            )
        if name in reserved:
            errors.append(
                ValidationError(
                    code="subdomain.reserved",
                    title="Поле subdomain",
                    message=f"Имя «{name}» зарезервировано преподавателем.",
                    path=label,
                    line=line,
                    column=column,
                    hint="Выберите другое имя: например, imya-familiya.",
                )
            )
        if path.stem != name:
            if LABEL.fullmatch(name):
                hint = (
                    f"Переименуйте файл в records/{name}.yaml — или исправьте subdomain "
                    f"на {path.stem}, если поддомен должен быть таким."
                )
            else:
                hint = (
                    "Файл всегда называется так же, как subdomain: сначала исправьте "
                    "subdomain, затем переименуйте файл (например, в records/"
                    f"{path.stem}.yaml)."
                )
            errors.append(
                ValidationError(
                    code="filename.mismatch",
                    title="Имя файла и subdomain",
                    message=f"Имя файла «{path.stem}» не совпадает с subdomain «{name}».",
                    path=label,
                    line=line,
                    column=column,
                    hint=hint,
                    details="Файл и поддомен всегда называются одинаково.",
                )
            )
        key = name.casefold()
        if key in seen:
            errors.append(
                ValidationError(
                    code="subdomain.duplicate",
                    title="Поле subdomain",
                    message=f"Имя «{name}» уже занято заявкой {seen[key]}.",
                    path=label,
                    line=line,
                    column=column,
                    hint="Выберите другое имя: например, добавьте суффикс -2.",
                    details="Имя должно быть уникальным среди всех заявок.",
                )
            )
        else:
            seen[key] = label

        line, column = _position(positions, "type")
        if record["type"] != "CNAME":
            errors.append(
                ValidationError(
                    code="type.invalid",
                    title="Поле type",
                    message=f"Тип записи «{record['type']}» не подходит.",
                    path=label,
                    line=line,
                    column=column,
                    hint="Поставьте `type: CNAME`.",
                    details="Для сайта на GitHub Pages нужна только запись CNAME.",
                )
            )

        raw_target = record["target"]
        target = raw_target.lower()
        target_user = target.removesuffix(".github.io")
        line, column = _position(positions, "target")
        if (
            not raw_target.isascii()
            or not target.endswith(".github.io")
            or not GITHUB_USER.fullmatch(target_user)
        ):
            errors.append(_target_error(broken_of(label), raw_target, line, column))

        line, column = _position(positions, "repository")
        raw_repository = record["repository"]
        repository = REPOSITORY.fullmatch(raw_repository)
        if repository is None:
            errors.append(_repository_error(broken_of(label), raw_repository, line, column))
        elif target.endswith(".github.io") and repository.group(1).lower() != target_user:
            errors.append(
                ValidationError(
                    code="repository.owner_mismatch",
                    title="Поля target и repository",
                    message=f"Владелец репозитория «{repository.group(1)}» не совпадает "
                    f"с логином в target «{target_user}».",
                    path=label,
                    line=line,
                    column=column,
                    hint=f"В target укажите владельца репозитория: {repository.group(1)}.github.io — "
                    "или возьмите репозиторий того же владельца.",
                    details="target — адрес сайта, repository — репозиторий с его файлами; "
                    "логин в них должен совпадать.",
                )
            )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Check local student CNAME requests")
    parser.add_argument("root", nargs="?", type=Path, default=Path("."))
    parser.add_argument(
        "--annotations",
        action="store_true",
        help="печатать аннотации GitHub Actions (в CI включается само)",
    )
    args = parser.parse_args()
    errors = validate_records(args.root)
    annotations = args.annotations or os.environ.get("GITHUB_ACTIONS") == "true"
    for line in render_log(errors, annotations=annotations):
        print(line)
    if errors:
        write_step_summary(errors)
        return 1
    print("Заявки корректны.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
