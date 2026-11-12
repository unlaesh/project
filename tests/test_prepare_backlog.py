"""Offline tests: no GitHub access and no real subprocess execution."""

import copy
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prepare_backlog.py"
SPEC = importlib.util.spec_from_file_location("prepare_backlog_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)


def catalog(*codes):
    source = mod.load_catalog()
    selected = set(codes)
    source["entries"] = [
        copy.deepcopy(entry) for entry in source["entries"] if selected.intersection(entry["wbs"])
    ]
    return source


def issue(number=23, code="3.1.4", **changes):
    result = {
        "number": number,
        "title": f"[{code}] Исходная задача",
        "body": (
            f"**Элемент WBS:** {code} — см. WBS\n"
            "**Требования и сценарии:** FR-8\n\n"
            "## Критерии готовности\n\n- [ ] Проверяемый результат\n"
        ),
        "state": "open",
        "updated_at": "2026-11-12T00:00:00Z",
        "labels": [{"name": "must"}],
        "assignees": [{"login": "unlaesh"}],
        "milestone": {"number": 2},
    }
    result.update(changes)
    return result


class FakeGitHub:
    def __init__(self, issues=()):
        self.issues = {item["number"]: copy.deepcopy(item) for item in issues}
        self.patches = []
        self.creates = []
        self.lose_patch_response = False
        self.lose_create_response = False

    def list_issues(self):
        return copy.deepcopy(list(self.issues.values()))

    def get(self, number):
        return copy.deepcopy(self.issues[number])

    def patch(self, number, payload):
        self.patches.append((number, copy.deepcopy(payload)))
        self.issues[number].update(copy.deepcopy(payload))
        self.issues[number]["updated_at"] = "2026-11-12T01:00:00Z"
        if self.lose_patch_response:
            self.lose_patch_response = False
            raise mod.BacklogError("Response lost after PATCH succeeded")
        return self.get(number)

    def create(self, payload):
        self.creates.append(copy.deepcopy(payload))
        number = max(self.issues, default=1000) + 1
        self.issues[number] = issue(number=number, **payload)
        if self.lose_create_response:
            self.lose_create_response = False
            raise mod.BacklogError("Response lost after POST succeeded")
        return self.get(number)


def write_plan(tmp_path, items, *, live=True, create_missing=False):
    selected = catalog("3.1.4")
    plan = mod.build_plan(items, selected, live=live, create_missing=create_missing)
    path = tmp_path / "plan.json"
    mod.write_json(path, plan)
    return path, plan


def test_identify_ignores_dependencies_and_criteria():
    item = issue(
        code="4.1.1",
        body=(
            "**Элемент WBS:** 4.1.1 — см. WBS\n"
            "**Зависимость:** 2.2.3\n\n"
            "## Описание\nИспользовать результат 2.1.2.\n\n"
            "## Критерии готовности\n- [ ] Связать с 3.1.3\n"
        ),
    )
    assert mod.identify(item) == ({"4.1.1"}, None)


def test_dependency_alone_is_not_identity():
    item = issue(title="Миграция заметок", body="Зависимость: 2.2.3")
    assert mod.identify(item) == (set(), None)


def test_title_metadata_disagreement_blocks_write():
    item = issue(title="[3.1.3] Другая работа")
    plan = mod.build_plan([item], catalog("3.1.3", "3.1.4"), live=True)
    assert not plan["operations"]
    assert any("расходятся" in row["reason"] for row in plan["rows"])


def test_single_notification_issue_covers_both_wbs_codes():
    plan = mod.build_plan([issue(code="5.1.1")], catalog("5.1.1"), live=True)
    assert len(plan["operations"]) == 1
    op = plan["operations"][0]
    assert op["number"] == 23
    assert op["wbs"] == ["5.1.1", "5.1.2"]
    assert mod.identify({**issue(), **op["after"]})[0] == {"5.1.1", "5.1.2"}


def test_separate_notification_issues_are_not_silently_merged():
    plan = mod.build_plan(
        [issue(23, "5.1.1"), issue(24, "5.1.2")],
        catalog("5.1.1"),
        live=True,
        create_missing=True,
    )
    assert not plan["operations"]
    assert any(row["status"] == "conflict" for row in plan["rows"])


def test_closed_issue_counts_as_coverage_and_is_unchanged():
    plan = mod.build_plan([issue(state="closed")], catalog("3.1.4"), live=True, create_missing=True)
    assert not plan["operations"]
    assert not plan["missing"]
    assert plan["rows"][0]["status"] == "closed"


def test_completed_checkboxes_are_not_reset():
    item = issue()
    item["body"] = item["body"].replace("[ ]", "[x]")
    plan = mod.build_plan([item], catalog("3.1.4"), live=True)
    assert not plan["operations"]
    assert plan["rows"][0]["status"] == "review"


def test_custom_section_is_preserved_and_render_is_idempotent():
    original = issue()
    extra = "## Решения команды\n\nИспользовать сохранённую договорённость."
    original["body"] += "\n" + extra + "\n"
    entry = catalog("3.1.4")["entries"][0]
    first = mod.render(entry, original["body"])
    assert extra in first["body"]
    assert mod.render(entry, first["body"]) == first
    updated = {**original, **first}
    plan = mod.build_plan([updated], catalog("3.1.4"), live=True)
    assert not plan["operations"]


def test_custom_section_code_fence_is_preserved_verbatim():
    extra = "## Пример\n\n```markdown\n## Описание\nНе удалять пример.\n```"
    old = issue()["body"] + "\n" + extra + "\n"
    entry = catalog("3.1.4")["entries"][0]
    result = mod.render(entry, old)
    assert extra in result["body"]
    assert mod.render(entry, result["body"]) == result


def test_unknown_issue_prevents_automatic_creation():
    unknown = issue(title="Наша другая задача", body="Обсуждение")
    plan = mod.build_plan([unknown], catalog("3.1.4"), live=True, create_missing=True)
    assert plan["missing"]
    assert not plan["operations"]


def test_creation_is_opt_in():
    default = mod.build_plan([], catalog("3.1.4"), live=True)
    requested = mod.build_plan([], catalog("3.1.4"), live=True, create_missing=True)
    assert not default["operations"]
    assert requested["operations"][0]["kind"] == "create"


def test_pagination_counts_raw_page_size_and_excludes_pull_requests(monkeypatch):
    github = object.__new__(mod.GitHub)
    first = [issue(number=n) for n in range(1, 101)]
    first[49]["pull_request"] = {"url": "irrelevant"}
    pages = [first, [issue(number=101)]]
    endpoints = []

    def request(endpoint):
        endpoints.append(endpoint)
        return pages.pop(0)

    monkeypatch.setattr(github, "request", request)
    result = github.list_issues()
    assert len(result) == 100
    assert {item["number"] for item in result} == set(range(1, 102)) - {50}
    assert endpoints[-1].endswith("page=2")


def test_gh_receives_literal_json_without_shell_interpretation(monkeypatch):
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/fake/gh")
    captured = {}

    def run(command, **kwargs):
        captured.update(command=command, **kwargs)
        return subprocess.CompletedProcess(command, 0, stdout='{"number": 23}', stderr="")

    monkeypatch.setattr(mod.subprocess, "run", run)
    payload = {
        "title": "Кириллица",
        "body": "Literal $(do_not_run) and `do_not_run`\nsecond line",
    }
    mod.GitHub().patch(23, payload)
    assert captured.get("shell", False) is False
    assert json.loads(captured["input"]) == payload
    assert payload["body"] not in captured["command"]
    assert captured["command"][-2:] == ["--input", "-"]


def test_apply_changes_only_title_and_body(tmp_path):
    original = issue()
    github = FakeGitHub([original])
    path, plan = write_plan(tmp_path, [original])
    assert mod.apply_plan(path, github) == 0
    assert set(github.patches[0][1]) == {"title", "body"}
    for key in ("state", "labels", "assignees", "milestone"):
        assert github.get(23)[key] == original[key]
    assert mod.same_content(github.get(23), plan["operations"][0]["after"])
    assert mod.apply_plan(path, github) == 0
    assert len(github.patches) == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [("body", "Edited by user"), ("state", "closed"), ("updated_at", "newer")],
)
def test_preflight_rejects_changed_issue(tmp_path, field, value):
    original = issue()
    path, _ = write_plan(tmp_path, [original])
    github = FakeGitHub([original])
    github.issues[23][field] = value
    assert mod.apply_plan(path, github) == 1
    assert not github.patches


def test_post_write_verification_detects_mismatch(tmp_path, monkeypatch):
    original = issue()
    path, _ = write_plan(tmp_path, [original])
    github = FakeGitHub([original])
    real_patch = github.patch

    def incorrect_patch(number, payload):
        result = real_patch(number, payload)
        github.issues[number]["body"] = "Concurrent edit"
        return result

    monkeypatch.setattr(github, "patch", incorrect_patch)
    assert mod.apply_plan(path, github) == 1
    result = mod.read_json(path.with_name("apply-results.json"))
    assert result["operations"]["0"]["status"] == "error"


def test_retry_recovers_lost_patch_response_without_repeating_write(tmp_path):
    original = issue()
    path, _ = write_plan(tmp_path, [original])
    github = FakeGitHub([original])
    github.lose_patch_response = True
    assert mod.apply_plan(path, github) == 1
    assert mod.apply_plan(path, github) == 0
    assert len(github.patches) == 1


def test_retry_recovers_lost_create_response_without_duplicate(tmp_path):
    path, _ = write_plan(tmp_path, [], create_missing=True)
    github = FakeGitHub()
    github.lose_create_response = True
    assert mod.apply_plan(path, github) == 1
    assert mod.apply_plan(path, github) == 0
    assert len(github.creates) == 1
    assert len(github.issues) == 1


def test_restore_recovers_original_and_is_repeat_safe(tmp_path):
    original = issue()
    path, _ = write_plan(tmp_path, [original])
    github = FakeGitHub([original])
    assert mod.apply_plan(path, github) == 0
    assert mod.restore_plan(path, github) == 0
    assert mod.editable(github.get(23)) == mod.editable(original)
    assert mod.restore_plan(path, github) == 0
    assert len(github.patches) == 2


def test_restore_does_not_overwrite_newer_user_changes(tmp_path):
    original = issue()
    path, _ = write_plan(tmp_path, [original])
    github = FakeGitHub([original])
    assert mod.apply_plan(path, github) == 0
    github.issues[23]["body"] += "\nNew user requirement"
    later_body = github.issues[23]["body"]
    assert mod.restore_plan(path, github) == 1
    assert github.issues[23]["body"] == later_body
    assert len(github.patches) == 1


def test_offline_plan_cannot_write(tmp_path):
    path, _ = write_plan(tmp_path, [issue()], live=False)
    github = FakeGitHub([issue()])
    with pytest.raises(mod.BacklogError, match="локальному JSON"):
        mod.apply_plan(path, github)
    assert not github.patches


@pytest.mark.parametrize("title", ["3.1.4.1 Подзадача", "3.1.4abc Другая задача"])
def test_title_requires_complete_wbs_token(title):
    assert mod.identify(issue(title=title, body="")) == (set(), None)


def test_dependency_metadata_and_nested_custom_section_survive():
    original = issue()
    original["body"] = original["body"].replace(
        "## Критерии готовности",
        "**Зависимость:** 3.1.2, 3.1.3\n\n## Критерии готовности",
    )
    extra = "## Решения команды\n\nСохранить.\n\n### Описание\nОсобое пояснение."
    original["body"] += "\n" + extra
    result = mod.render(catalog("3.1.4")["entries"][0], original["body"])
    assert "**Зависимость:** 3.1.2, 3.1.3" in result["body"]
    assert extra in result["body"]


def test_successful_creation_resolves_missing_summary(tmp_path):
    path, _ = write_plan(tmp_path, [], create_missing=True)
    github = FakeGitHub()
    assert mod.apply_plan(path, github) == 0
    result = mod.read_json(path.with_name("apply-results.json"))
    assert result["unresolved"] == []


def test_management_parameters_remain_visible_until_supplied():
    incomplete = catalog("1.3.1")
    incomplete["entries"][0]["estimation_inputs"] = ["Указать число оставшихся итераций."]
    plan = mod.build_plan([issue(code="1.3.1")], incomplete, live=True)
    assert len(plan["operations"]) == 1
    assert any(row["status"] == "parameters" for row in plan["rows"])
