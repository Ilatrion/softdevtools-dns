"""Русские сообщения и форматирование результата проверки заявок.

Модуль отвечает только за то, *как* показать найденные проблемы: текст в журнале
прогона, аннотации GitHub Actions и сводку шага. Правила проверки живут в
`scripts.validate` и `scripts.validate_pr` и не дублируются здесь.

Тексты сообщений не раскрывают устройство валидатора: студент видит, что не так
с его заявкой, ожидаемое значение и конкретный следующий шаг.
"""

from __future__ import annotations

from dataclasses import dataclass
import difflib
import os
from pathlib import Path
import re
from typing import Iterable, Mapping, Sequence

ANNOTATION_LIMIT = 10
SUMMARY_ENV = "GITHUB_STEP_SUMMARY"
REPOSITORY_ENV = "GITHUB_REPOSITORY"
EXAMPLE_REQUEST = "examples/ivan-ivanov.yaml"
STUDENT_GUIDE = "docs/student.md"

# Имя заявки: латинские строчные буквы, цифры и дефис внутри, расширение .yaml.
RECORD_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.yaml\Z")


@dataclass(frozen=True)
class ValidationError:
    """Одна проблема заявки: код, место, объяснение и подсказка."""

    code: str
    title: str
    message: str
    path: str | None = None
    line: int | None = None
    column: int | None = None
    hint: str | None = None
    details: str | None = None

    def location(self) -> str:
        if not self.path:
            return ""
        if self.line:
            return f"{self.path}:{self.line}"
        return self.path

    def log_lines(self) -> list[str]:
        """Человекочитаемый блок: студент читает его в журнале шага."""
        header = f"❌ {self.location()}" if self.location() else "❌"
        lines = [f"{header} — {self.title}", f"   {self.message}"]
        if self.details:
            lines.extend(f"   {part}" for part in self.details.splitlines() if part.strip())
        if self.hint:
            lines.append(f"💡 {self.hint}")
        return lines

    def annotation(self) -> str:
        """Команда workflow: аннотация к файлу и строке в интерфейсе проверок."""
        properties: list[str] = []
        if self.path:
            properties.append(f"file={escape_property(self.path)}")
        if self.line:
            properties.append(f"line={self.line}")
            if self.column:
                properties.append(f"col={self.column}")
        properties.append(f"title={escape_property(self.title)}")
        body = [self.message]
        if self.details:
            body.append(self.details)
        if self.hint:
            body.append(f"💡 {self.hint}")
        return f"::error {','.join(properties)}::{escape_data(chr(10).join(body))}"


def escape_property(value: str) -> str:
    """Экранирование значений свойств команды workflow."""
    for source, target in (("%", "%25"), ("\r", "%0D"), ("\n", "%0A"), (":", "%3A"), (",", "%2C")):
        value = value.replace(source, target)
    return value


def escape_data(value: str) -> str:
    """Экранирование текста команды workflow."""
    for source, target in (("%", "%25"), ("\r", "%0D"), ("\n", "%0A")):
        value = value.replace(source, target)
    return value


def render_log(errors: Sequence[ValidationError], *, annotations: bool) -> list[str]:
    """Строки для журнала прогона: полный текст и аннотации для первых ошибок."""
    lines: list[str] = []
    for error in errors:
        lines.extend(error.log_lines())
    if not annotations:
        return lines
    for error in errors[:ANNOTATION_LIMIT]:
        lines.append(error.annotation())
    hidden = len(errors) - ANNOTATION_LIMIT
    if hidden > 0:
        lines.append(
            f"Показаны первые {ANNOTATION_LIMIT} аннотаций; остальные {hidden} — "
            "в сводке шага (Summary)."
        )
    return lines


def render_text(errors: Sequence[ValidationError]) -> str:
    """Полный текст без аннотаций: для журналов и сообщений об отказе."""
    return "\n".join(render_log(errors, annotations=False))


def summary_markdown(errors: Sequence[ValidationError], *, repository: str | None = None) -> str:
    """Сводка шага со всеми ошибками: лимит аннотаций на неё не действует."""
    lines = ["## Заявка не прошла проверку", "", f"Замечаний: {len(errors)}", ""]
    for error in errors:
        location = _code_span(error.location()) if error.location() else "заявка"
        lines.append(f"- {location} — **{_plain(error.title)}**")
        lines.append(f"  - {_plain(error.message)}")
        if error.details:
            lines.extend(f"  - {_plain(part)}" for part in error.details.splitlines() if part.strip())
        if error.hint:
            lines.append(f"  - 💡 {_plain(error.hint)}")
    lines.append("")
    if repository:
        guide = f"https://github.com/{repository}/blob/main/{STUDENT_GUIDE}"
        lines.append(f"Как исправить: [{STUDENT_GUIDE}]({guide})")
    else:
        lines.append(f"Как исправить: {STUDENT_GUIDE}")
    return "\n".join(lines) + "\n"


def write_step_summary(
    errors: Sequence[ValidationError], *, env: Mapping[str, str] | None = None
) -> bool:
    """Дописать сводку в файл, который GitHub показывает на странице прогона."""
    environment = os.environ if env is None else env
    target = environment.get(SUMMARY_ENV)
    if not target or not errors:
        return False
    try:
        with Path(target).open("a", encoding="utf-8") as summary:
            summary.write(summary_markdown(errors, repository=environment.get(REPOSITORY_ENV)))
    except OSError:
        return False
    return True


def suggest_field(name: str, allowed: Iterable[str]) -> str | None:
    """Похожее допустимое имя поля — для подсказки об опечатке."""
    if not isinstance(name, str) or not name:
        return None
    matches = difflib.get_close_matches(name.casefold(), sorted(allowed), n=1, cutoff=0.7)
    return matches[0] if matches else None


def suggest_folder(name: str) -> str | None:
    """Похожее имя папки: студент мог написать record вместо records."""
    if not isinstance(name, str) or not name:
        return None
    matches = difflib.get_close_matches(name.casefold(), ["records"], n=1, cutoff=0.7)
    return matches[0] if matches else None


def record_filename_advice(name: str) -> tuple[str | None, str | None]:
    """Разобрать имя файла заявки: (исправленное имя, объяснение).

    Подсказка для типичных опечаток: лишняя точка, чужое расширение, повтор
    `.yaml`, пробелы, подчёркивания, заглавные буквы.
    """
    explanation: str | None = None
    candidate = name.strip().rstrip(". ")

    if candidate != name:
        explanation = "После расширения .yaml стоит лишняя точка или пробел."

    lowered = candidate.lower()
    for suffix, reason in (
        (".yaml.txt", "Расширение .txt лишнее: нужно только .yaml."),
        (".yaml.yaml", "Расширение .yaml указано дважды."),
        (".yaml.yml", "Расширение .yaml указано дважды."),
        (".yml", "Расширение .yml не поддерживается: нужно .yaml."),
    ):
        if lowered.endswith(suffix):
            core = candidate[: len(candidate) - len(suffix)]
            explanation = explanation or reason
            break
    else:
        if lowered.endswith(".yaml"):
            core = candidate[: -len(".yaml")]
            if not candidate.endswith(".yaml"):
                explanation = explanation or "Расширение .yaml пишется строчными буквами."
        else:
            core = candidate
            explanation = explanation or "У файла нет расширения .yaml."

    if " " in core:
        explanation = explanation or "В имени файла есть пробелы."
        core = core.replace(" ", "-")
    if "_" in core:
        explanation = explanation or "В DNS-имени используется дефис, а не подчёркивание."
        core = core.replace("_", "-")
    if core != core.lower():
        explanation = explanation or "В имени файла нужны только строчные буквы."
        core = core.lower()

    suggestion = f"{core}.yaml"
    if suggestion == name or not RECORD_NAME.fullmatch(suggestion):
        return None, explanation
    return suggestion, explanation


def _plain(text: str) -> str:
    """Безопасный для markdown текст: без html и управляющих символов."""
    cleaned = "".join(" " if character < " " and character != "\n" else character for character in text)
    return cleaned.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _code_span(text: str) -> str:
    """Код-спан в сводке: имя файла приходит от студента, кавычки обезвреживаем."""
    cleaned = _plain(text).replace("`", "'")
    return f"`{cleaned}`"
