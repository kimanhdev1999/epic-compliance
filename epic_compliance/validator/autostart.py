"""Start the HL7 validator container alongside the app.

The validator is a separate Java service (infernocommunity/inferno-resource-validator).
Rather than ask the user to run `podman compose up` by hand, the app starts it
on launch: `serve` does it on a background thread so the dashboard opens at
once, and `run` waits for it.

The container is left running when the app exits. Booting it is cheap, but the
first validation loads FHIR core + US Core (tens of seconds), and a warm
container makes the next launch's runs fast.

Nothing here raises: if no container engine is installed the validator simply
stays unavailable, which callers already map to needs_human.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time
from urllib.parse import urlparse

from ..config import AppConfig
from .backends import HL7ValidatorService

log = logging.getLogger(__name__)

#: Keep in step with docker/fhir-validator.compose.yml (a test checks this).
#: Not 1.0.78 (validator 6.9.7, what the g10 test kit pins): against today's
#: tx.fhir.org it rejects valid codes such as contentType "image/jpeg" ("The
#: System URI could not be determined"), so every write check fails. 1.0.84
#: (validator 6.10.3) validates them correctly.
VALIDATOR_IMAGE = "docker.io/infernocommunity/inferno-resource-validator:1.0.84"
CONTAINER_NAME = "epic-compliance-fhir-validator"
CONTAINER_PORT = 3500

# An app launched from Finder gets a bare PATH without Homebrew's bin dirs.
_EXTRA_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

_state_lock = threading.Lock()
_thread: threading.Thread | None = None
_done = threading.Event()
_done.set()  # nothing in flight until start_in_background() runs
status: dict[str, object] = {"state": "not_started", "detail": ""}


def _set(state: str, detail: str) -> None:
    status.update(state=state, detail=detail)
    log.info("FHIR validator autostart: %s — %s", state, detail)


def find_engine() -> str | None:
    """podman (the project's engine), else docker; None if neither is installed."""
    for name in ("podman", "docker"):
        found = shutil.which(name)
        if found:
            return found
        for d in _EXTRA_BIN_DIRS:
            candidate = shutil.which(name, path=d)
            if candidate:
                return candidate
    return None


def should_autostart(config: AppConfig) -> tuple[bool, str]:
    if not config.validator_autostart:
        return False, "VALIDATOR_AUTOSTART=false"
    if config.validator_mode not in ("auto", "service"):
        return False, f"VALIDATOR_MODE={config.validator_mode}"
    if config.validator_mode == "auto" and config.validator_jar_path:
        return False, "VALIDATOR_JAR_PATH is set — using the jar"
    host = urlparse(config.validator_service_url).hostname or ""
    if host not in _LOCAL_HOSTS:
        return False, f"VALIDATOR_SERVICE_URL points at {host}, not this machine"
    return True, ""


def _host_port(config: AppConfig) -> int:
    return urlparse(config.validator_service_url).port or CONTAINER_PORT


def _engine(engine: str, *args: str, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run([engine, *args], capture_output=True, text=True, timeout=timeout)


def _same_image(a: str, b: str) -> bool:
    """docker reports "infernocommunity/…", podman "docker.io/infernocommunity/…"."""
    return a.removeprefix("docker.io/") == b.removeprefix("docker.io/")


def _start_container(engine: str, host_port: int) -> tuple[bool, str]:
    """Start the existing container, or create it. (ok, detail).

    A container left by an older release runs a different image; it is
    replaced rather than restarted, so an image bump takes effect."""
    found = _engine(engine, "container", "inspect", "--format", "{{.Config.Image}}", CONTAINER_NAME)
    if found.returncode == 0 and _same_image(found.stdout.strip(), VALIDATOR_IMAGE):
        proc = _engine(engine, "start", CONTAINER_NAME)
    else:
        if found.returncode == 0:
            _engine(engine, "rm", "-f", CONTAINER_NAME)
        proc = _engine(
            engine, "run", "-d", "--name", CONTAINER_NAME,
            "-p", f"{host_port}:{CONTAINER_PORT}",
            # Negative = validator sessions never expire. Keeps repeat runs fast.
            "-e", "SESSION_CACHE_DURATION=-1",
            VALIDATOR_IMAGE,
            timeout=600.0,  # first run pulls the image (~500 MB)
        )
    if proc.returncode == 0:
        return True, ""
    return False, (proc.stderr or proc.stdout).strip()[-300:]


def ensure_validator_service(config: AppConfig, wait_s: float = 90.0) -> tuple[bool, str]:
    """Make sure the validator service answers, starting its container if needed.

    Returns (available, detail). Never raises.
    """
    service = HL7ValidatorService(config.validator_service_url, config.validator_timeout_s)
    ok, detail = service.health()
    if ok:
        return True, f"already running: {detail}"

    wanted, why_not = should_autostart(config)
    if not wanted:
        return False, f"not auto-starting ({why_not})"

    engine = find_engine()
    if engine is None:
        return False, "neither podman nor docker is installed"

    try:
        started, err = _start_container(engine, _host_port(config))
        if not started and engine.endswith("podman") and "connect" in err.lower():
            # macOS: the podman VM is stopped. Start it once and retry.
            _engine(engine, "machine", "start", timeout=180.0)
            started, err = _start_container(engine, _host_port(config))
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{engine} failed: {exc}"
    if not started:
        return False, f"could not start {CONTAINER_NAME}: {err}"

    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        ok, detail = service.health()
        if ok:
            return True, f"started {CONTAINER_NAME}: {detail}"
        time.sleep(1.0)
    return False, f"started {CONTAINER_NAME} but it did not answer within {wait_s:.0f}s"


def warm_up(config: AppConfig) -> tuple[bool, str]:
    """One throwaway validation so the service loads FHIR core + US Core now,
    not during the user's first run (that load outlasts VALIDATOR_TIMEOUT_S)."""
    from ..fhir.client import MOCK_FHIR_RESOURCES
    from .profiles import US_CORE_PROFILES

    service = HL7ValidatorService(config.validator_service_url, timeout_s=600.0)
    result = service.validate(MOCK_FHIR_RESOURCES["Patient"], US_CORE_PROFILES["Patient"])
    if not result.available:
        return False, result.unavailable_reason
    return True, "US Core loaded"


def _run(config: AppConfig) -> None:
    try:
        _set("starting", f"checking {config.validator_service_url}")
        ok, detail = ensure_validator_service(config)
        if not ok:
            _set("unavailable", detail)
            return
        _set("warming", detail)
        warm_ok, warm_detail = warm_up(config)
        _set("ready" if warm_ok else "unavailable", f"{detail}; {warm_detail}")
    except Exception as exc:  # a background thread must not die silently
        _set("unavailable", f"autostart crashed: {exc}")
    finally:
        _done.set()


def start_in_background(config: AppConfig) -> None:
    """Kick off ensure + warm-up without blocking the caller. Idempotent."""
    global _thread
    if not should_autostart(config)[0]:
        return
    with _state_lock:
        if _thread is not None and _thread.is_alive():
            return
        _done.clear()
        _thread = threading.Thread(target=_run, args=(config,), name="validator-autostart", daemon=True)
        _thread.start()


def wait_for_autostart(timeout_s: float = 300.0) -> bool:
    """Block until a background autostart (if any) has finished. True if it finished."""
    return _done.wait(timeout_s)
