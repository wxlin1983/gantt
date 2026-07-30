"""Built-in handlers (implement.md §6.1.1).

The security properties get the most attention here: an outbound HTTP handler
driven by user-editable templates is an SSRF primitive, and a shell handler is
remote code execution, so both need to fail closed.
"""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.execution.handlers.builtins import (
    ShellCommandHandler,
    WaitForSignalHandler,
)
from app.execution.handlers.http_request import (
    HttpConfigError,
    HttpRequestHandler,
    assert_host_allowed,
    check_condition,
    read_path,
    substitute,
)
from app.execution.registry import Outcome, TaskContext


@pytest.fixture(autouse=True)
def reset_settings():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def context(**overrides) -> TaskContext:
    base = {
        "case_id": 7,
        "task_id": 42,
        "task_name": "build",
        "attempt": 2,
        "params": {"branch": "main", "project": "X1"},
        "case_params": {"release": "2026.09"},
        "config": {},
        "secrets": {"value": "s3cret", "ci_token": "s3cret"},
        "callback_url": "/api/v1/callbacks/abc",
    }
    base.update(overrides)
    return TaskContext(**base)


class TestSubstitution:
    def test_task_and_case_params(self):
        ctx = context()
        assert substitute("${ params.branch }", ctx) == "main"
        assert substitute("${ case_params.release }", ctx) == "2026.09"

    def test_scalars(self):
        ctx = context()
        assert substitute("${ task_name }", ctx) == "build"
        assert substitute("${ case_id }", ctx) == "7"
        assert substitute("${ callback_url }", ctx) == "/api/v1/callbacks/abc"
        assert substitute("${ idempotency_key }", ctx) == ctx.idempotency_key

    def test_secrets(self):
        assert substitute("Bearer ${ secrets.ci_token }", context()) == (
            "Bearer s3cret"
        )

    def test_nested_structures(self):
        ctx = context()
        result = substitute(
            {"body": {"refs": ["${ params.branch }"], "n": 3}}, ctx
        )
        assert result == {"body": {"refs": ["main"], "n": 3}}

    def test_unknown_reference_becomes_empty(self):
        # Better an empty value than a literal `${ params.nope }` reaching an
        # external system as if it were data.
        assert substitute("${ params.nope }", context()) == ""


class TestReadPath:
    @pytest.mark.parametrize(
        "path,expected",
        [
            ("$.build_id", "b-1"),
            ("$.nested.status", "SUCCESS"),
            ("$.items.0", "first"),
            ("build_id", "b-1"),
            ("$.missing", None),
            ("", None),
        ],
    )
    def test_paths(self, path, expected):
        payload = {
            "build_id": "b-1",
            "nested": {"status": "SUCCESS"},
            "items": ["first", "second"],
        }
        assert read_path(payload, path) == expected


class TestConditions:
    @pytest.mark.parametrize(
        "condition,expected",
        [
            ("$.status == 'SUCCESS'", True),
            ("$.status == 'FAILED'", False),
            ("$.status != 'FAILED'", True),
            ("$.status in ['SUCCESS', 'PASSED']", True),
            ("$.status not in ['FAILED']", True),
        ],
    )
    def test_supported_forms(self, condition, expected):
        assert check_condition(condition, {"status": "SUCCESS"}) is expected

    def test_unparseable_condition_raises(self):
        with pytest.raises(HttpConfigError):
            check_condition("this is not a condition", {})

    def test_empty_condition_is_false(self):
        assert check_condition("", {"status": "SUCCESS"}) is False


class TestHostAllowlist:
    def test_empty_allowlist_refuses_everything(self, monkeypatch):
        # Failing closed matters: an open default would make every template a
        # potential SSRF vector.
        monkeypatch.setenv("HTTP_HANDLER_ALLOWED_HOSTS", "[]")
        get_settings.cache_clear()
        with pytest.raises(HttpConfigError, match="empty"):
            assert_host_allowed("https://ci.internal/api")

    def test_listed_host_is_allowed(self, monkeypatch):
        monkeypatch.setenv("HTTP_HANDLER_ALLOWED_HOSTS", '["ci.internal"]')
        get_settings.cache_clear()
        assert_host_allowed("https://ci.internal/api/builds")

    def test_unlisted_host_is_refused(self, monkeypatch):
        monkeypatch.setenv("HTTP_HANDLER_ALLOWED_HOSTS", '["ci.internal"]')
        get_settings.cache_clear()
        with pytest.raises(HttpConfigError, match="not in"):
            assert_host_allowed("http://169.254.169.254/latest/meta-data")


class TestHttpRequestHandler:
    async def test_missing_url_is_fatal(self):
        result = await HttpRequestHandler().trigger(context(config={}))
        assert result.outcome is Outcome.FATAL

    async def test_disallowed_host_is_fatal_not_retried(self, monkeypatch):
        monkeypatch.setenv("HTTP_HANDLER_ALLOWED_HOSTS", "[]")
        get_settings.cache_clear()
        result = await HttpRequestHandler().trigger(
            context(config={"url": "https://evil.example/x"})
        )
        # Fatal, not failed: retrying a misconfiguration is pointless
        assert result.outcome is Outcome.FATAL

    async def test_unsupported_method_is_fatal(self, monkeypatch):
        monkeypatch.setenv("HTTP_HANDLER_ALLOWED_HOSTS", '["ci.internal"]')
        get_settings.cache_clear()
        result = await HttpRequestHandler().trigger(
            context(config={"url": "https://ci.internal/x", "method": "TRACE"})
        )
        assert result.outcome is Outcome.FATAL


class TestWaitForSignal:
    async def test_trigger_reports_in_flight(self):
        result = await WaitForSignalHandler().trigger(context())
        assert result.outcome is Outcome.RUNNING
        assert result.external_ref == "/api/v1/callbacks/abc"

    async def test_poll_never_resolves_it(self):
        # Only the callback can finish this handler; the timeout is what
        # eventually gives up.
        result = await WaitForSignalHandler().poll(context())
        assert result.outcome is Outcome.RUNNING


class TestShellCommand:
    async def test_disabled_by_default(self):
        get_settings.cache_clear()
        result = await ShellCommandHandler().trigger(
            context(config={"command": "echo hi"})
        )
        assert result.outcome is Outcome.FATAL
        assert "disabled" in result.message

    async def test_command_must_be_allowlisted(self, monkeypatch):
        monkeypatch.setenv("SHELL_HANDLER_ENABLED", "true")
        monkeypatch.setenv("SHELL_HANDLER_ALLOWED_COMMANDS", '["echo"]')
        get_settings.cache_clear()
        result = await ShellCommandHandler().trigger(
            context(config={"command": "rm -rf /"})
        )
        assert result.outcome is Outcome.FATAL
        assert "ALLOWED_COMMANDS" in result.message

    async def test_allowlisted_command_runs(self, monkeypatch):
        monkeypatch.setenv("SHELL_HANDLER_ENABLED", "true")
        monkeypatch.setenv("SHELL_HANDLER_ALLOWED_COMMANDS", '["echo"]')
        get_settings.cache_clear()
        result = await ShellCommandHandler().trigger(
            context(config={"command": "echo hello"})
        )
        assert result.outcome is Outcome.SUCCEEDED
        assert "hello" in result.payload["stdout"]

    async def test_nonzero_exit_is_a_failure(self, monkeypatch):
        monkeypatch.setenv("SHELL_HANDLER_ENABLED", "true")
        monkeypatch.setenv("SHELL_HANDLER_ALLOWED_COMMANDS", '["false"]')
        get_settings.cache_clear()
        result = await ShellCommandHandler().trigger(
            context(config={"command": "false"})
        )
        assert result.outcome is Outcome.FAILED
