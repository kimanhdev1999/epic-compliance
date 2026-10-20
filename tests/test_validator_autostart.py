"""Validator autostart: decides when to start the container, and how."""
from __future__ import annotations

import subprocess
from pathlib import Path

import httpx
import pytest

from epic_compliance.config import AppConfig
from epic_compliance.validator import HL7ValidatorService
from epic_compliance.validator import autostart

COMPOSE_FILE = Path(__file__).parent.parent / "docker" / "fhir-validator.compose.yml"


def _config(**env: str) -> AppConfig:
    return AppConfig.model_validate({"VALIDATOR_AUTOSTART": "true", **env})


def test_image_matches_compose_file():
    image = autostart.VALIDATOR_IMAGE.removeprefix("docker.io/")
    assert f"image: {image}" in COMPOSE_FILE.read_text()


@pytest.mark.parametrize(
    "env, wanted",
    [
        ({}, True),
        ({"VALIDATOR_AUTOSTART": "false"}, False),
        ({"VALIDATOR_MODE": "off"}, False),
        ({"VALIDATOR_MODE": "java"}, False),
        ({"VALIDATOR_JAR_PATH": "/opt/validator_cli.jar"}, False),
        ({"VALIDATOR_MODE": "service", "VALIDATOR_JAR_PATH": "/opt/v.jar"}, True),
        ({"VALIDATOR_SERVICE_URL": "http://127.0.0.1:3600"}, True),
        ({"VALIDATOR_SERVICE_URL": "https://validator.example.org"}, False),
    ],
)
def test_should_autostart(env, wanted):
    assert autostart.should_autostart(_config(**env))[0] is wanted


def test_already_running_starts_nothing(monkeypatch):
    monkeypatch.setattr(HL7ValidatorService, "health", lambda self: (True, "validator 6.9.7"))
    monkeypatch.setattr(autostart, "_engine", lambda *a, **k: pytest.fail("must not call the engine"))
    ok, detail = autostart.ensure_validator_service(_config())
    assert ok and detail.startswith("already running")


def test_no_engine_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(HL7ValidatorService, "health", lambda self: (False, "refused"))
    monkeypatch.setattr(autostart, "find_engine", lambda: None)
    ok, detail = autostart.ensure_validator_service(_config())
    assert not ok and "podman" in detail


def _fake_engine(calls, exists: bool):
    """exists: False, or the image the existing container runs."""
    def run(engine, *args, timeout=120.0):
        calls.append(args)
        if args[:2] == ("container", "inspect"):
            return subprocess.CompletedProcess([engine, *args], 0 if exists else 1, f"{exists}\n" if exists else "", "")
        return subprocess.CompletedProcess([engine, *args], 0, "", "")
    return run


def test_creates_container_on_the_configured_port(monkeypatch):
    health = iter([(False, "refused"), (True, "validator 6.9.7")])
    monkeypatch.setattr(HL7ValidatorService, "health", lambda self: next(health))
    monkeypatch.setattr(autostart, "find_engine", lambda: "/opt/homebrew/bin/podman")
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(autostart, "_engine", _fake_engine(calls, exists=False))

    ok, _ = autostart.ensure_validator_service(_config(VALIDATOR_SERVICE_URL="http://localhost:3600"))

    assert ok
    run = calls[-1]
    assert run[0] == "run" and autostart.VALIDATOR_IMAGE in run
    assert "3600:3500" in run and autostart.CONTAINER_NAME in run


def test_restarts_an_existing_container(monkeypatch):
    health = iter([(False, "refused"), (True, "validator 6.9.7")])
    monkeypatch.setattr(HL7ValidatorService, "health", lambda self: next(health))
    monkeypatch.setattr(autostart, "find_engine", lambda: "/usr/local/bin/docker")
    calls: list[tuple[str, ...]] = []
    # docker reports the image without the docker.io/ prefix podman uses.
    current = autostart.VALIDATOR_IMAGE.removeprefix("docker.io/")
    monkeypatch.setattr(autostart, "_engine", _fake_engine(calls, exists=current))

    ok, _ = autostart.ensure_validator_service(_config())

    assert ok and calls[-1] == ("start", autostart.CONTAINER_NAME)


def test_replaces_a_container_on_an_old_image(monkeypatch):
    """1.0.78 rejects valid codes; a container from before the bump must not be reused."""
    health = iter([(False, "refused"), (True, "validator 6.10.3")])
    monkeypatch.setattr(HL7ValidatorService, "health", lambda self: next(health))
    monkeypatch.setattr(autostart, "find_engine", lambda: "/opt/homebrew/bin/podman")
    calls: list[tuple[str, ...]] = []
    old = "docker.io/infernocommunity/inferno-resource-validator:1.0.78"
    monkeypatch.setattr(autostart, "_engine", _fake_engine(calls, exists=old))

    ok, _ = autostart.ensure_validator_service(_config())

    assert ok
    assert ("rm", "-f", autostart.CONTAINER_NAME) in calls
    assert calls[-1][0] == "run" and autostart.VALIDATOR_IMAGE in calls[-1]


def test_native_issue_shape_parses(monkeypatch):
    """inferno-resource-validator 1.0.78's real response: validator-native issues."""
    payload = {"outcomes": [{"fileInfo": {"fileName": "Media.json"}, "issues": [
        {"level": "ERROR", "type": "REQUIRED", "location": "Media.content",
         "message": "Media.content: minimum required = 1, but only found 0"},
        {"level": "WARNING", "type": "INVARIANT", "location": "Media",
         "message": "Constraint failed: dom-6"},
    ]}], "sessionId": "abc"}
    monkeypatch.setattr(
        httpx, "post",
        lambda url, **kw: httpx.Response(200, json=payload, request=httpx.Request("POST", url)),
    )
    result = HL7ValidatorService("http://localhost:3500").validate({"resourceType": "Media"})
    assert result.is_valid() is False
    assert result.errors[0].location == "Media.content"
    assert "minimum required" in result.errors[0].message
    assert [m.severity for m in result.messages] == ["error", "warning"]


def test_service_reuses_its_session(monkeypatch):
    """A session per request reloads FHIR core each time and OOMs the JVM."""
    monkeypatch.setattr(HL7ValidatorService, "_sessions", {})
    sent = []

    def fake_post(url, json=None, timeout=None):
        sent.append(json.get("sessionId"))
        body = {"outcomes": [{"issues": []}], "sessionId": "s-1"}
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    HL7ValidatorService("http://localhost:3500").validate({"resourceType": "Media"})
    HL7ValidatorService("http://localhost:3500").validate({"resourceType": "Media"})
    assert sent == [None, "s-1"]
