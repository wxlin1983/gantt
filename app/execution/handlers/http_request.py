"""Generic HTTP handler (implement.md §6.1.1).

The point of this handler is that connecting a new system should not require a
code deploy. Without it, every integration means write-review-deploy, which is
enough friction that people quietly go back to ticking boxes by hand and the
automation never gets used.

Configuration lives in the task template's ``api_config``:

    url, method, headers, body, auth_ref, external_ref_path,
    poll_url, success_when, failure_when
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

import httpx

from app.config import get_settings
from app.execution.registry import TaskContext, TaskResult, task_handler

#: `${ ... }` is the execution-time form, kept distinct from the DSL's
#: build-time `{{ ... }}` so it is obvious when a value is resolved.
_PLACEHOLDER = re.compile(r"\$\{\s*([a-zA-Z0-9_.]+)\s*\}")

_SAFE_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}


class HttpConfigError(Exception):
    """The handler cannot run with the configuration it was given."""


def substitute(value: Any, ctx: TaskContext) -> Any:
    """Resolve ``${ params.x }`` style references against the task context."""
    if isinstance(value, dict):
        return {key: substitute(item, ctx) for key, item in value.items()}
    if isinstance(value, list):
        return [substitute(item, ctx) for item in value]
    if not isinstance(value, str):
        return value

    sources = {
        "params": ctx.params,
        "case_params": ctx.case_params,
        "secrets": ctx.secrets,
    }
    scalars = {
        "external_ref": ctx.external_ref or "",
        "task_name": ctx.task_name,
        "case_id": str(ctx.case_id),
        "callback_url": ctx.callback_url or "",
        "idempotency_key": ctx.idempotency_key,
    }

    def replace(match: re.Match[str]) -> str:
        reference = match.group(1)
        if reference in scalars:
            return str(scalars[reference])
        namespace, _, key = reference.partition(".")
        table = sources.get(namespace)
        if table is None or key not in table:
            return ""
        return str(table[key])

    return _PLACEHOLDER.sub(replace, value)


def read_path(payload: Any, path: str) -> Any:
    """Read a dotted path such as ``$.build.id`` out of a JSON body.

    A deliberately tiny subset of JSONPath: enough for "pull the job id out of
    the response", without pulling in a dependency or a query language nobody
    asked for.
    """
    if not path:
        return None
    cursor = payload
    for part in path.removeprefix("$").strip(".").split("."):
        if not part:
            continue
        if isinstance(cursor, dict):
            cursor = cursor.get(part)
        elif isinstance(cursor, list) and part.isdigit():
            index = int(part)
            cursor = cursor[index] if index < len(cursor) else None
        else:
            return None
    return cursor


def check_condition(condition: str, payload: Any) -> bool:
    """Evaluate a ``$.path == 'value'`` or ``$.path in [...]`` condition.

    Intentionally not the DSL expression evaluator: that one resolves template
    parameters at build time, and reusing it here would blur two very
    different moments in a task's life.
    """
    if not condition:
        return False
    match = re.match(
        r"^\s*(\$[\w.\[\]]*)\s*(==|!=|in|not in)\s*(.+?)\s*$", condition
    )
    if match is None:
        raise HttpConfigError(f"unparseable condition: {condition!r}")

    path, operator, literal = match.groups()
    actual = read_path(payload, path)
    try:
        expected = json.loads(literal.replace("'", '"'))
    except json.JSONDecodeError as exc:
        raise HttpConfigError(
            f"condition right-hand side is not valid JSON: {literal!r}"
        ) from exc

    match operator:
        case "==":
            return actual == expected
        case "!=":
            return actual != expected
        case "in":
            return actual in expected
        case "not in":
            return actual not in expected
    return False


def assert_host_allowed(url: str) -> None:
    """Refuse hosts outside the allowlist.

    An unrestricted outbound HTTP handler driven by user-editable templates is
    a server-side request forgery primitive. The allowlist defaults to empty,
    so the failure mode is "refuses to run" rather than "will call anything".
    """
    allowed = get_settings().http_handler_allowed_hosts
    host = urlparse(url).hostname or ""
    if not allowed:
        raise HttpConfigError(
            "HTTP_HANDLER_ALLOWED_HOSTS is empty, so no outbound host is "
            "permitted; add the host you intend to call"
        )
    if host not in allowed:
        raise HttpConfigError(
            f"host {host!r} is not in HTTP_HANDLER_ALLOWED_HOSTS"
        )


@task_handler("http_request", builtin=True)
class HttpRequestHandler:
    """Call an HTTP API, optionally polling a status URL until it settles."""

    timeout_seconds = 30.0

    async def trigger(self, ctx: TaskContext) -> TaskResult:
        config = ctx.config
        url = substitute(config.get("url", ""), ctx)
        if not url:
            return TaskResult.fatal("api_config.url is not set")
        try:
            assert_host_allowed(url)
        except HttpConfigError as exc:
            return TaskResult.fatal(str(exc))

        method = str(config.get("method", "POST")).upper()
        if method not in _SAFE_METHODS:
            return TaskResult.fatal(f"method {method!r} is not allowed")

        response = await self._send(
            method,
            url,
            headers=substitute(config.get("headers") or {}, ctx),
            body=substitute(config.get("body"), ctx),
            ctx=ctx,
        )
        if isinstance(response, TaskResult):
            return response

        payload = self._json(response)
        if response.status_code >= 400:
            return TaskResult.failed(
                f"{method} {url} returned {response.status_code}",
                detail=response.text[:2000],
            )

        external_ref = read_path(
            payload, config.get("external_ref_path", "")
        )
        # No poll target and no success condition means the call itself was
        # the whole job.
        if not config.get("poll_url") and not config.get("success_when"):
            return TaskResult.succeeded(response=payload)
        if config.get("success_when") and check_condition(
            config["success_when"], payload
        ):
            return TaskResult.succeeded(response=payload)

        return TaskResult.running(
            external_ref=str(external_ref) if external_ref else None,
            response=payload,
        )

    async def poll(self, ctx: TaskContext) -> TaskResult:
        config = ctx.config
        url = substitute(
            config.get("poll_url") or config.get("url", ""), ctx
        )
        if not url:
            return TaskResult.fatal("api_config.poll_url is not set")
        try:
            assert_host_allowed(url)
        except HttpConfigError as exc:
            return TaskResult.fatal(str(exc))

        response = await self._send(
            "GET",
            url,
            headers=substitute(config.get("headers") or {}, ctx),
            body=None,
            ctx=ctx,
        )
        if isinstance(response, TaskResult):
            return response

        payload = self._json(response)
        if response.status_code >= 400:
            return TaskResult.failed(
                f"poll returned {response.status_code}",
                detail=response.text[:2000],
            )

        try:
            if config.get("failure_when") and check_condition(
                config["failure_when"], payload
            ):
                return TaskResult.failed(
                    "external system reported failure",
                    detail=json.dumps(payload)[:2000],
                )
            if config.get("success_when") and check_condition(
                config["success_when"], payload
            ):
                return TaskResult.succeeded(response=payload)
        except HttpConfigError as exc:
            return TaskResult.fatal(str(exc))

        return TaskResult.running(
            external_ref=ctx.external_ref, response=payload
        )

    async def _send(
        self,
        method: str,
        url: str,
        *,
        headers: dict,
        body: Any,
        ctx: TaskContext,
    ) -> httpx.Response | TaskResult:
        merged = {
            "Idempotency-Key": ctx.idempotency_key,
            **{str(k): str(v) for k, v in (headers or {}).items()},
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds, follow_redirects=False
            ) as client:
                return await client.request(
                    method,
                    url,
                    headers=merged,
                    json=body if body not in (None, {}) else None,
                )
        except httpx.TimeoutException:
            # Retryable: the request may simply have been slow.
            return TaskResult.failed(f"{method} {url} timed out")
        except httpx.HTTPError as exc:
            return TaskResult.failed(f"{method} {url} failed: {exc}")

    @staticmethod
    def _json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            return {"text": response.text[:2000]}
