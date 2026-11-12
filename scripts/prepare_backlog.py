#!/usr/bin/env python3
"""Reviewable, resumable preparation of unlaesh/project issues through the user's gh CLI."""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "scripts" / "backlog_catalog.json"
OUTPUT = ROOT / ".backlog-preparation"
REPO = "unlaesh/project"
SCHEMA = 1
CODE = re.compile(r"(?<![\w.])\d+\.\d+\.\d+(?![\w.])")
MARKER = "<!-- pary-backlog-preparation:v1 -->"
REPLACED_SECTIONS = {
    "описание",
    "критерии готовности",
    "критерии приёмки",
    "критерии приемки",
    "границы задачи",
    "объём работы",
    "объем работы",
}


class BacklogError(Exception):
    """An actionable error which does not require a traceback."""


def now() -> str:
    return datetime.now(UTC).isoformat()


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def unescape(text: str) -> str:
    # Some copies from editors contain literal Markdown escaping. Parse it, never execute it.
    return re.sub(r"\\([*_\[\]()#:])", r"\1", text).replace("**", "")


def identify(issue: dict) -> tuple[set[str], str | None]:
    body = unescape(issue.get("body") or "")
    metadata = []
    for line in body.splitlines():
        if re.match(r"^\s*#{1,3}\s+", line):
            break
        if re.match(r"^\s*Элемент(?:ы)?\s+WBS\s*:", line, re.IGNORECASE):
            fragment = line.split(":", 1)[1].split("—", 1)[0].split("см.", 1)[0]
            metadata.append(set(CODE.findall(fragment)))
    title = unescape(issue.get("title") or "")
    prefix = re.match(
        r"^\s*\[?(?:WBS\s+)?(\d+\.\d+\.\d+(?:\s*[,;+–-]\s*\d+\.\d+\.\d+)*)"
        r"(?=$|[\s\]:—–]|\.(?:\s|$))",
        title,
        re.IGNORECASE,
    )
    title_codes = set(CODE.findall(prefix[1])) if prefix else set()
    if len(metadata) > 1:
        return set().union(*metadata, title_codes), "Несколько строк «Элемент WBS»."
    if metadata:
        if not metadata[0]:
            return title_codes, "Строка WBS есть, но коды в ней не распознаны."
        if title_codes and not title_codes.issubset(metadata[0]):
            return metadata[0] | title_codes, "Коды WBS в заголовке и описании расходятся."
        return metadata[0], None
    return title_codes, None


def editable(issue: dict) -> dict:
    return {"title": issue.get("title") or "", "body": issue.get("body") or ""}


def normalized(value: dict) -> dict:
    return {key: value[key].replace("\r\n", "\n").strip() for key in ("title", "body")}


def same_content(issue: dict, payload: dict) -> bool:
    return normalized(editable(issue)) == normalized(payload)


def state_of(issue: dict) -> str:
    return (issue.get("state") or "").lower()


def load_catalog() -> dict:
    catalog = read_json(CATALOG)
    if catalog.get("repo") != REPO or catalog.get("schema_version") != SCHEMA:
        raise BacklogError("Неверный репозиторий или версия каталога.")
    seen: set[str] = set()
    for entry in catalog["entries"]:
        if not entry["wbs"] or not entry["description"] or not entry["acceptance"]:
            raise BacklogError("В каталоге есть неполная карточка.")
        for code in entry["wbs"]:
            if not CODE.fullmatch(code) or code in seen:
                raise BacklogError(f"Повторяющийся или некорректный WBS: {code}")
            seen.add(code)
    return catalog


def markdown_headings(body: str) -> list[tuple[int, int, str]]:
    """Find section boundaries, excluding headings inside fenced examples."""
    headings = []
    offset = 0
    fence_char = None
    fence_size = 0
    for line in body.splitlines(keepends=True):
        fence = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line.rstrip("\r\n"))
        if fence:
            if fence_char is None:
                fence_char, fence_size = fence[1][0], len(fence[1])
            elif fence[1][0] == fence_char and len(fence[1]) >= fence_size and not fence[2].strip():
                fence_char = None
        elif fence_char is None:
            heading = re.match(r"^(#{1,3})\s+(.+?)\s*#*\s*$", line.rstrip("\r\n"))
            if heading:
                headings.append((offset, len(heading[1]), heading[2]))
        offset += len(line)
    return headings


def preserved_material(body: str) -> str:
    """Keep custom metadata/sections verbatim. Replaced sections remain in the backup and diff."""
    matches = markdown_headings(body)
    preamble = body[: matches[0][0]] if matches else body
    kept = []
    for line in preamble.splitlines():
        plain = unescape(line).strip().rstrip("\\")
        known = re.match(
            r"^(Элемент(?:ы)? WBS|Описание функции|Требования(?: и сценарии)?)\s*:",
            plain,
            re.IGNORECASE,
        )
        if plain and not known and line.strip() != MARKER:
            kept.append(line)
    index = 0
    while index < len(matches):
        start, depth, title = matches[index]
        following = index + 1
        while following < len(matches) and matches[following][1] > depth:
            following += 1
        end = matches[following][0] if following < len(matches) else len(body)
        name = unescape(title).strip().lower()
        section = body[start:end].strip()
        if name not in REPLACED_SECTIONS:
            kept.append(section)
        index = following
    return "\n\n".join(kept).strip()


def render(entry: dict, previous: str = "") -> dict:
    codes = ", ".join(entry["wbs"])
    lines = [MARKER, f"**Элементы WBS:** {codes} — см. [WBS](https://github.com/{REPO}/wiki/WBS)"]
    if entry.get("feature"):
        feature = entry["feature"]
        lines.append(f"**Описание функции:** [{feature}](https://github.com/{REPO}/wiki/{feature})")
    if entry.get("references"):
        lines.append("**Требования и сценарии:** " + ", ".join(entry["references"]))
    lines.extend(["", "## Описание", "", entry["description"], "", "## Критерии готовности", ""])
    lines.extend("- [ ] " + criterion for criterion in entry["acceptance"])
    if entry.get("scope"):
        lines.extend(["", "## Границы задачи", ""])
        lines.extend("- " + item for item in entry["scope"])
    extra = preserved_material(previous)
    if extra:
        lines.extend(["", extra])
    return {
        "title": f"[{' + '.join(entry['wbs'])}] {entry['title']}",
        "body": "\n".join(lines).strip() + "\n",
    }


def structural_findings(issue: dict) -> list[str]:
    body = unescape(issue.get("body") or "")
    findings = []
    if not re.search(r"^#{1,3}\s+Описание\s*$", body, re.MULTILINE | re.IGNORECASE):
        findings.append("Нет отдельного описания результата.")
    if not re.search(r"^\s*- \[[ xX]\]\s+\S", body, re.MULTILINE):
        findings.append("Нет критериев в виде проверяемого списка.")
    if re.search(
        r"^\s*- \[[ xX]\]\s*(?:готово|работает|сделано|реализовано)[.!]?\s*$",
        body,
        re.MULTILINE | re.IGNORECASE,
    ):
        findings.append("Есть критерий без конкретного проверяемого результата.")
    return findings


def build_plan(
    issues: list[dict], catalog: dict, *, live: bool, create_missing: bool = False
) -> dict:
    issues = [issue for issue in issues if "pull_request" not in issue]
    entries = catalog["entries"]
    groups = {entry["wbs"][0]: entry for entry in entries}
    by_code = {code: entry["wbs"][0] for entry in entries for code in entry["wbs"]}
    matched: dict[str, list[dict]] = defaultdict(list)
    blocked: set[str] = set()
    rows = []
    operations = []
    missing = []
    for issue in issues:
        codes, problem = identify(issue)
        keys = {by_code[code] for code in codes if code in by_code}
        if problem or not codes or len(keys) != 1 or any(code not in by_code for code in codes):
            blocked.update(keys)
            rows.append(
                {
                    "number": issue["number"],
                    "status": "review",
                    "codes": sorted(codes),
                    "reason": problem or "Нет однозначного соответствия каталогу WBS.",
                }
            )
        else:
            matched[next(iter(keys))].append(issue)
    for key, entry in groups.items():
        candidates = matched.get(key, [])
        if key in blocked or len(candidates) > 1:
            rows.append(
                {
                    "wbs": entry["wbs"],
                    "status": "conflict",
                    "numbers": [item["number"] for item in candidates],
                    "reason": (
                        "Пересекающиеся или противоречивые карточки; "
                        "автоматическое объединение отключено."
                    ),
                }
            )
            continue
        if not candidates:
            missing.append(entry["wbs"])
            rows.append(
                {
                    "wbs": entry["wbs"],
                    "status": "missing",
                    "reason": (
                        "Карточка с этими явными кодами WBS не найдена; "
                        "проверьте несопоставленные задачи."
                    ),
                }
            )
            # Never create when an unknown issue could represent the same work.
            unknown = any(row["status"] == "review" for row in rows)
            if create_missing and not unknown:
                operations.append({"kind": "create", "wbs": entry["wbs"], "after": render(entry)})
                if entry.get("estimation_inputs"):
                    rows.append(
                        {
                            "wbs": entry["wbs"],
                            "status": "parameters",
                            "reason": " ".join(entry["estimation_inputs"]),
                        }
                    )
            continue
        issue = candidates[0]
        number = issue["number"]
        codes, _ = identify(issue)
        if state_of(issue) == "closed":
            rows.append(
                {
                    "number": number,
                    "wbs": sorted(codes),
                    "status": "closed",
                    "reason": "Закрытая карточка учтена, её содержание и состояние сохранены.",
                }
            )
            uncovered = sorted(set(entry["wbs"]) - codes)
            if uncovered:
                missing.append(uncovered)
                rows.append(
                    {
                        "wbs": uncovered,
                        "status": "review",
                        "reason": f"Закрытая #{number} покрывает только часть объединённой работы.",
                    }
                )
            continue
        if entry.get("estimation_inputs"):
            rows.append(
                {
                    "number": number,
                    "wbs": entry["wbs"],
                    "status": "parameters",
                    "reason": " ".join(entry["estimation_inputs"]),
                }
            )
        proposed = render(entry, issue.get("body") or "")
        if same_content(issue, proposed):
            rows.append(
                {
                    "number": number,
                    "wbs": entry["wbs"],
                    "status": "unchanged",
                    "reason": "Содержание уже соответствует каталогу.",
                }
            )
            continue
        if re.search(r"^\s*-\s+\[[xX]\]", unescape(issue.get("body") or ""), re.MULTILINE):
            rows.append(
                {
                    "number": number,
                    "wbs": sorted(codes),
                    "status": "review",
                    "reason": (
                        "Есть выполненные пункты: автоматическая замена могла бы сбросить прогресс."
                    ),
                }
            )
            continue
        reason = structural_findings(issue)
        reason.append("Уточнить границы и критерии по проверенному каталогу wiki.")
        rows.append(
            {"number": number, "wbs": entry["wbs"], "status": "update", "reason": " ".join(reason)}
        )
        operations.append(
            {
                "kind": "update",
                "number": number,
                "wbs": entry["wbs"],
                "before": editable(issue),
                "before_state": state_of(issue),
                "before_updated_at": issue.get("updated_at"),
                "after": proposed,
            }
        )
    # An unresolved unmatched issue might be any missing WBS: require resolution before creation.
    if any(row["status"] in {"review", "conflict"} for row in rows):
        operations = [op for op in operations if op["kind"] != "create"]
    return {
        "schema_version": SCHEMA,
        "repo": REPO,
        "created_at": now(),
        "live": live,
        "catalog_sha256": digest(catalog),
        "create_missing": create_missing,
        "issue_count": len(issues),
        "rows": rows,
        "missing": missing,
        "operations": operations,
    }


class GitHub:
    def __init__(self) -> None:
        if not shutil.which("gh"):
            raise BacklogError(
                "Не найден GitHub CLI (gh). Установите его и выполните gh auth login."
            )

    def request(self, endpoint: str, method: str = "GET", payload: dict | None = None) -> Any:
        command = ["gh", "api", "--hostname", "github.com", "--method", method, endpoint]
        data = None
        if payload is not None:
            command.extend(["--input", "-"])
            data = json.dumps(payload, ensure_ascii=False)
        try:
            result = subprocess.run(
                command,
                input=data,
                text=True,
                encoding="utf-8",
                capture_output=True,
                timeout=90,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise BacklogError(
                "GitHub не ответил за 90 секунд; результат записи может быть неопределённым. "
                "Повторный apply сверит состояние."
            ) from exc
        if result.returncode:
            raise BacklogError(result.stderr.strip() or "Ошибка GitHub CLI.")
        return json.loads(result.stdout)

    def list_issues(self) -> list[dict]:
        issues = []
        page = 1
        while True:
            batch = self.request(
                f"repos/{REPO}/issues?state=all&sort=created&direction=asc&per_page=100&page={page}"
            )
            if not isinstance(batch, list):
                raise BacklogError("GitHub вернул неожиданный формат списка Issues.")
            issues.extend(item for item in batch if "pull_request" not in item)
            if len(batch) < 100:
                return issues
            page += 1

    def get(self, number: int) -> dict:
        return self.request(f"repos/{REPO}/issues/{number}")

    def patch(self, number: int, payload: dict) -> dict:
        if set(payload) != {"title", "body"}:
            raise BacklogError("Разрешены только изменения заголовка и описания.")
        return self.request(f"repos/{REPO}/issues/{number}", "PATCH", payload)

    def create(self, payload: dict) -> dict:
        return self.request(f"repos/{REPO}/issues", "POST", payload)


def report(plan: dict) -> str:
    counts = Counter(row["status"] for row in plan["rows"])
    lines = [
        "# Подготовка бэклога",
        "",
        f"Репозиторий: {REPO}",
        f"Карточек в снимке: {plan['issue_count']}. Изменений в плане: {len(plan['operations'])}.",
        "",
        "Это план изменений; он ещё не применён к GitHub.",
        "",
        "## Итог проверки",
        "",
    ]
    lines.extend(f"- {key}: {value}" for key, value in sorted(counts.items()))
    lines.extend(["", "## Проверка карточек", ""])
    for row in plan["rows"]:
        identity = f"#{row['number']}" if "number" in row else ", ".join(row.get("wbs", []))
        if row.get("numbers"):
            identity += " (" + ", ".join(f"#{n}" for n in row["numbers"]) + ")"
        lines.append(f"- **{identity} — {row['status']}:** {row['reason']}")
    lines.extend(
        [
            "",
            "Сопоставление выполняется по строке «Элемент(ы) WBS» или коду в начале заголовка.",
            "Закрытые карточки, карточки с выполненными пунктами "
            "и неоднозначные соответствия не переписываются.",
            "Описание, критерии и границы заменяются версиями из каталога; "
            "нестандартные разделы сохраняются.",
            "Исходные тексты и весь полученный список сохранены рядом с планом.",
            "Обновляются только заголовок и описание. Метки, исполнители, комментарии, "
            "состояния, поля Project и сроки сохраняются.",
            "",
            "## Изменения",
            "",
        ]
    )
    for op in plan["operations"]:
        identity = f"#{op['number']}" if "number" in op else "Создать " + ", ".join(op["wbs"])
        before = op.get("before", {"title": "", "body": ""})
        old = (before["title"] + "\n\n" + before["body"]).splitlines(keepends=True)
        new = (op["after"]["title"] + "\n\n" + op["after"]["body"]).splitlines(keepends=True)
        change = "".join(difflib.unified_diff(old, new, fromfile="было", tofile="станет"))
        fence = chr(96) * (max([3] + [len(m[0]) + 1 for m in re.finditer(r"`+", change)]))
        lines.extend([f"### {identity}", "", fence + "diff", change, fence, ""])
    return "\n".join(lines) + "\n"


def save_plan(plan: dict, snapshot: list[dict], output: Path) -> Path:
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    run = output / (datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6])
    run.mkdir(mode=0o700)
    write_json(run / "issues-before.json", snapshot)
    write_json(run / "plan.json", plan)
    (run / "report.md").write_text(report(plan), encoding="utf-8")
    proposed = run / "proposed"
    proposed.mkdir()
    for op in plan["operations"]:
        name = str(op.get("number", "new-" + op["wbs"][0])) + ".md"
        (proposed / name).write_text(
            "# " + op["after"]["title"] + "\n\n" + op["after"]["body"], encoding="utf-8"
        )
    write_json(output / "latest.json", {"plan": str(run / "plan.json")})
    return run / "plan.json"


def resolve_plan(argument: str | None, output: Path) -> Path:
    if argument:
        return Path(argument).expanduser().resolve()
    latest = output / "latest.json"
    if not latest.exists():
        raise BacklogError("Сначала выполните plan: сохранённого плана пока нет.")
    return Path(read_json(latest)["plan"])


def validate_plan(plan: dict) -> None:
    if plan.get("repo") != REPO or plan.get("schema_version") != SCHEMA:
        raise BacklogError("План предназначен для другого репозитория или другой версии скрипта.")
    if not plan.get("live"):
        raise BacklogError(
            "План создан по локальному JSON для проверки. Для записи нужен новый plan с GitHub."
        )
    for op in plan["operations"]:
        if op["kind"] not in {"update", "create"} or set(op["after"]) != {"title", "body"}:
            raise BacklogError("Некорректная операция в плане.")
        if len(op["after"]["body"]) > 65000 or len(op["after"]["title"]) > 256:
            raise BacklogError("Заголовок или описание превышает допустимый размер.")
        if op["kind"] == "update" and (not isinstance(op["number"], int) or op["number"] <= 0):
            raise BacklogError("Неверный номер issue.")


def apply_plan(path: Path, github: Any) -> int:
    plan = read_json(path)
    validate_plan(plan)
    result_path = path.with_name("apply-results.json")
    result = (
        read_json(result_path)
        if result_path.exists()
        else {"plan_sha256": digest(plan), "operations": {}}
    )
    if result.get("plan_sha256") != digest(plan):
        raise BacklogError("План изменён после начала применения. Создайте новый plan.")
    failures = 0
    for index, op in enumerate(plan["operations"]):
        key = str(index)
        previous = result["operations"].get(key, {})
        if previous.get("status") == "restored":
            print(
                f"Пропущено: операция {index + 1} уже отменена. "
                "Для повторной правки создайте новый plan."
            )
            continue
        record = dict(previous)
        try:
            if op["kind"] == "update":
                current = github.get(op["number"])
                if same_content(current, op["after"]):
                    record.update(status="already_applied", number=op["number"])
                else:
                    if (
                        state_of(current) != op["before_state"]
                        or not same_content(current, op["before"])
                        or (
                            op.get("before_updated_at")
                            and current.get("updated_at") != op["before_updated_at"]
                        )
                    ):
                        raise BacklogError("Карточка изменилась после plan. Составьте новый план.")
                    record.update(status="pending", number=op["number"])
                    result["operations"][key] = record
                    write_json(result_path, result)
                    github.patch(op["number"], op["after"])
                    verified = github.get(op["number"])
                    if not same_content(verified, op["after"]):
                        raise BacklogError(
                            "После записи содержание отличается от плана; проверьте карточку."
                        )
                    record.update(status="applied", number=op["number"])
            else:
                matches = [
                    item for item in github.list_issues() if identify(item)[0] & set(op["wbs"])
                ]
                if matches:
                    if (
                        len(matches) == 1
                        and same_content(matches[0], op["after"])
                        and previous.get("status")
                        in {"pending", "error", "created", "already_applied"}
                    ):
                        record.update(
                            status="already_applied", number=matches[0]["number"], created=True
                        )
                    else:
                        raise BacklogError(
                            "Карточка с этим WBS уже существует; повторное создание отменено."
                        )
                else:
                    record.update(status="pending")
                    result["operations"][key] = record
                    write_json(result_path, result)
                    created = github.create(op["after"])
                    record.update(number=created["number"], created=True)
                    result["operations"][key] = record
                    write_json(result_path, result)
                    verified = github.get(created["number"])
                    if not same_content(verified, op["after"]):
                        raise BacklogError("Не удалось подтвердить содержание созданной карточки.")
                    record.update(status="created")
            record.pop("error", None)
            print(f"{record['status']}: #{record.get('number', '?')} WBS {', '.join(op['wbs'])}")
        except BacklogError as exc:
            failures += 1
            record.update(status="error", error=str(exc))
            print(f"Пропущено WBS {', '.join(op['wbs'])}: {exc}", file=sys.stderr)
        record["checked_at"] = now()
        result["operations"][key] = record
        write_json(result_path, result)
    print(f"Результат: {result_path}\nОшибок или конфликтов применения: {failures}")
    created = {
        tuple(op["wbs"])
        for index, op in enumerate(plan["operations"])
        if op["kind"] == "create"
        and result["operations"].get(str(index), {}).get("status") in {"created", "already_applied"}
    }
    unresolved = [
        row
        for row in plan["rows"]
        if row["status"] in {"review", "conflict", "parameters"}
        or (row["status"] == "missing" and tuple(row["wbs"]) not in created)
    ]
    result["unresolved"] = unresolved
    write_json(result_path, result)
    if unresolved:
        print(
            "В report.md есть карточки/пропуски, требующие разбора. "
            "Весь бэклог ещё нельзя считать проверенным."
        )
    return 1 if failures else 0


def restore_plan(path: Path, github: Any) -> int:
    plan = read_json(path)
    validate_plan(plan)
    result_path = path.with_name("apply-results.json")
    if not result_path.exists():
        raise BacklogError("Нет журнала применения этого плана.")
    result = read_json(result_path)
    if result.get("plan_sha256") != digest(plan):
        raise BacklogError("План и журнал применения не совпадают.")
    failures = 0
    for index, op in enumerate(plan["operations"]):
        record = result["operations"].get(str(index))
        if not record or record["status"] == "restored":
            continue
        if op["kind"] == "create":
            print(
                f"Созданная карточка #{record.get('number', '?')} сохранена; "
                "restore не удаляет Issues."
            )
            continue
        try:
            current = github.get(op["number"])
            if same_content(current, op["before"]):
                record["status"] = "restored"
            elif same_content(current, op["after"]) and state_of(current) == op["before_state"]:
                github.patch(op["number"], op["before"])
                if not same_content(github.get(op["number"]), op["before"]):
                    raise BacklogError("Не удалось подтвердить восстановление.")
                record["status"] = "restored"
            else:
                raise BacklogError("Есть более поздние изменения; восстановление пропущено.")
            record.pop("restore_error", None)
            print(f"Восстановлено: #{op['number']}")
        except BacklogError as exc:
            failures += 1
            record["restore_error"] = str(exc)
            print(f"#{op['number']}: {exc}", file=sys.stderr)
        write_json(result_path, result)
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Подготовка Issues unlaesh/project к оценке.")
    sub = parser.add_subparsers(dest="command", required=True)
    plan_args = sub.add_parser(
        "plan", help="Загрузить карточки, сохранить резервную копию и план без записи на GitHub."
    )
    plan_args.add_argument(
        "--from-json",
        type=Path,
        help="Офлайн-проверка выгрузки REST API; такой план нельзя применить.",
    )
    plan_args.add_argument(
        "--create-missing",
        action="store_true",
        help="Добавить в план создание отсутствующих WBS-карточек, если нет неоднозначностей.",
    )
    plan_args.add_argument("--output-dir", type=Path, default=OUTPUT)
    for command in ("apply", "restore"):
        action = sub.add_parser(
            command,
            help="Применить сохранённый план."
            if command == "apply"
            else "Восстановить предыдущие описания.",
        )
        action.add_argument(
            "plan_file", nargs="?", help="Путь к plan.json; по умолчанию последний план."
        )
        action.add_argument("--output-dir", type=Path, default=OUTPUT)
    sub.add_parser("validate", help="Проверить каталог и покрытие локальной WBS без сети.")
    args = parser.parse_args(argv if argv is not None else (sys.argv[1:] or ["plan"]))
    try:
        catalog = load_catalog()
        if args.command == "validate":
            wbs = (ROOT / "project.wiki" / "WBS.md").read_text(encoding="utf-8")
            expected = set(re.findall(r"^\s{4}- (\d+\.\d+\.\d+)\.", wbs, re.MULTILINE))
            actual = {code for entry in catalog["entries"] for code in entry["wbs"]}
            if expected != actual:
                raise BacklogError(
                    f"Покрытие WBS расходится: отсутствуют {sorted(expected - actual)}, "
                    f"лишние {sorted(actual - expected)}"
                )
            print(
                f"Каталог: {len(catalog['entries'])} карточек, "
                f"{len(actual)} элементов WBS. Покрытие полное."
            )
            return 0
        if args.command == "plan":
            snapshot = read_json(args.from_json) if args.from_json else GitHub().list_issues()
            if not isinstance(snapshot, list):
                raise BacklogError("Ожидается JSON-массив Issues из REST API GitHub.")
            plan = build_plan(
                snapshot, catalog, live=args.from_json is None, create_missing=args.create_missing
            )
            path = save_plan(plan, snapshot, args.output_dir.resolve())
            counts = Counter(row["status"] for row in plan["rows"])
            print(
                f"План: {path}\nОтчёт: {path.with_name('report.md')}\n"
                f"Операций: {len(plan['operations'])}\nИтог: {dict(counts)}"
            )
            print("GitHub пока не изменён. После просмотра report.md выполните apply.")
            return 0
        path = resolve_plan(args.plan_file, args.output_dir.resolve())
        if args.command == "apply":
            return apply_plan(path, GitHub())
        return restore_plan(path, GitHub())
    except (BacklogError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
