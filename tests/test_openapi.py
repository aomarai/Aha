"""``docs/openapi.yaml`` must describe exactly what the health server does.

The spec is documentation people rely on, so it is checked against the real application: every
route and status code is exercised and the body is validated against the schema the spec declares
for it, and every metric the bot exposes has to be listed. A change to the server that is not
reflected in the spec fails here.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any, cast

import pytest
from aiohttp.test_utils import TestClient, TestServer
from jsonschema import Draft202012Validator
from openapi_spec_validator import validate
from openapi_spec_validator.readers import read_from_filename

from aha.bot import AhaBot
from aha.config import DEFAULT_HEALTH_BIND, DEFAULT_HEALTH_PORT, Settings
from aha.health import HealthServer
from aha.metrics import ShardState

SPEC_FILE = Path(__file__).parent.parent / "docs" / "openapi.yaml"


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    document, _ = read_from_filename(str(SPEC_FILE))
    return dict(document)


def bot_with(monkeypatch: pytest.MonkeyPatch, shards: list[ShardState]) -> AhaBot:
    bot = AhaBot(Settings(token="test-token"))
    monkeypatch.setattr(bot, "shard_states", lambda: shards)
    return bot


async def fetch(bot: AhaBot, path: str) -> tuple[int, str, str]:
    server = HealthServer(bot, "127.0.0.1", 0)
    async with TestClient(TestServer(server.app())) as client:
        response = await client.get(path)
        return response.status, response.headers["Content-Type"], await response.text()


def validate_body(spec: dict[str, Any], body: Any, schema: dict[str, Any]) -> None:
    """Validate ``body`` against a schema of the spec, resolving its ``$ref``s."""
    root = {"components": spec["components"], **schema}
    Draft202012Validator.check_schema(root)
    Draft202012Validator(root).validate(body)


def declared(spec: dict[str, Any], path: str, status: int) -> dict[str, Any]:
    return dict(spec["paths"][path]["get"]["responses"][str(status)])


def schema_of(response: dict[str, Any], media_type: str) -> dict[str, Any]:
    return dict(response["content"][media_type]["schema"])


# ----------------------------------------------------------------------------- the document


def test_the_spec_is_a_valid_openapi_document(spec: dict[str, Any]) -> None:
    validate(spec)

    assert spec["openapi"].startswith("3.1")


def test_the_spec_documents_exactly_the_routes_the_server_serves(spec: dict[str, Any]) -> None:
    app = HealthServer(AhaBot(Settings(token="test-token")), "127.0.0.1", 0).app()
    served = {
        (route.method, cast("Any", route.resource).canonical)
        for route in app.router.routes()
        if route.method != "HEAD"
    }

    documented = {(method.upper(), path) for path, item in spec["paths"].items() for method in item}

    assert documented == served == {("GET", "/livez"), ("GET", "/readyz"), ("GET", "/metrics")}


def test_the_server_defaults_in_the_spec_match_the_configuration(spec: dict[str, Any]) -> None:
    variables = spec["servers"][0]["variables"]

    assert variables["host"]["default"] == DEFAULT_HEALTH_BIND
    assert variables["port"]["default"] == str(DEFAULT_HEALTH_PORT)


def test_every_operation_has_a_unique_id_a_summary_and_only_declared_tags(
    spec: dict[str, Any],
) -> None:
    tags = {tag["name"] for tag in spec["tags"]}
    ids = []
    for item in spec["paths"].values():
        operation = item["get"]
        ids.append(operation["operationId"])
        assert operation["summary"]
        assert set(operation["tags"]) <= tags

    assert len(ids) == len(set(ids)) == 3


# ----------------------------------------------------------------------------- /livez


def test_livez_matches_the_spec_when_alive(
    spec: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    status, content_type, body = asyncio.run(fetch(bot_with(monkeypatch, []), "/livez"))
    response = declared(spec, "/livez", status)

    assert status == 200
    assert content_type.split(";")[0] in response["content"]
    validate_body(spec, json.loads(body), schema_of(response, "application/json"))
    assert json.loads(body) == response["content"]["application/json"]["example"]


def test_livez_matches_the_spec_when_a_critical_task_died(
    spec: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    bot = bot_with(monkeypatch, [])

    async def scenario() -> tuple[int, str, str]:
        async def dies() -> None:
            raise RuntimeError("boom")

        await bot.tasks.spawn(dies, name="gateway-watchdog", critical=True)
        return await fetch(bot, "/livez")

    status, _, body = asyncio.run(scenario())
    response = declared(spec, "/livez", status)

    assert status == 503
    validate_body(spec, json.loads(body), schema_of(response, "application/json"))
    assert json.loads(body) == response["content"]["application/json"]["example"]


# ----------------------------------------------------------------------------- /readyz


def test_readyz_matches_the_spec_when_ready(
    spec: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    bot = bot_with(monkeypatch, [ShardState(0, True, 0.05)])

    status, content_type, body = asyncio.run(fetch(bot, "/readyz"))
    response = declared(spec, "/readyz", status)

    assert status == 200
    assert content_type.split(";")[0] in response["content"]
    validate_body(spec, json.loads(body), schema_of(response, "application/json"))
    assert json.loads(body) == response["content"]["application/json"]["example"]


def test_readyz_matches_the_spec_when_not_ready_with_a_degraded_task(
    spec: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    bot = bot_with(monkeypatch, [ShardState(0, True, 0.05)])
    calls: list[int] = []

    async def flaky() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        await asyncio.sleep(3600)

    async def scenario() -> tuple[int, str, str]:
        bot.tasks.spawn(flaky, name="scheduler", restart=True, critical=True)
        for _ in range(10):
            await asyncio.sleep(0)  # crash once and enter the restart back-off
        result = await fetch(bot, "/readyz")
        await bot.tasks.close()
        return result

    status, _, body = asyncio.run(scenario())
    response = declared(spec, "/readyz", status)

    assert status == 503
    payload = json.loads(body)
    validate_body(spec, payload, schema_of(response, "application/json"))
    assert payload["status"] == "not ready"
    assert payload["degraded_tasks"] == ["scheduler"]
    example = response["content"]["application/json"]["example"]
    assert set(example) == set(payload)  # the example shows every field


def test_readyz_before_the_gateway_starts_is_not_ready_with_no_shards(
    spec: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    status, _, body = asyncio.run(fetch(bot_with(monkeypatch, []), "/readyz"))

    assert status == 503
    validate_body(
        spec, json.loads(body), schema_of(declared(spec, "/readyz", 503), "application/json")
    )
    assert json.loads(body)["shards"] == []


def test_the_readiness_status_is_ready_exactly_when_the_http_status_is_200(
    spec: dict[str, Any],
) -> None:
    schema = spec["components"]["schemas"]["Readiness"]

    assert schema["properties"]["status"]["enum"] == ["ready", "not ready"]
    assert set(spec["paths"]["/readyz"]["get"]["responses"]) == {"200", "503"}
    assert set(spec["paths"]["/livez"]["get"]["responses"]) == {"200", "503"}
    assert set(spec["paths"]["/metrics"]["get"]["responses"]) == {"200"}


# ----------------------------------------------------------------------------- /metrics


def test_metrics_use_the_documented_media_type(
    spec: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    status, content_type, _ = asyncio.run(fetch(bot_with(monkeypatch, []), "/metrics"))
    response = declared(spec, "/metrics", status)

    assert status == 200
    assert content_type in response["content"]


def test_every_exposed_metric_is_listed_in_the_spec(
    spec: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    bot = bot_with(monkeypatch, [ShardState(0, True, 0.05)])
    _, _, text = asyncio.run(fetch(bot, "/metrics"))

    exposed = set(re.findall(r"^# HELP (\S+) ", text, flags=re.MULTILINE))
    description = spec["paths"]["/metrics"]["get"]["description"]
    listed = set(re.findall(r"^\| `(aha_\w+)` \|", description, flags=re.MULTILINE))

    assert exposed  # the registry is not empty
    assert exposed == listed


def test_the_metrics_example_is_valid_exposition_text(spec: dict[str, Any]) -> None:
    media = spec["paths"]["/metrics"]["get"]["responses"]["200"]["content"]
    (example,) = (value["example"] for value in media.values())

    lines = example.strip().splitlines()
    assert lines[0].startswith("# HELP aha_shard_ready ")
    assert lines[1] == "# TYPE aha_shard_ready gauge"
    assert re.fullmatch(r'aha_shard_ready\{shard="0"\} 1\.0', lines[2])
