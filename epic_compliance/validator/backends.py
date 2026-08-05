"""FHIR validator backends.

Day 15 rule: do NOT write our own validator. Both backends drive the official
HL7 validator; they differ only in how it is hosted.

  HL7ValidatorService — HTTP to the Inferno resource-validator container
                        (infernocommunity/inferno-resource-validator), the same
                        image the ONC (g)(10) test kit uses. No Java on the host.
  JavaCliValidator    — subprocess `java -jar validator_cli.jar`. Needs a JRE.

Both return a ValidationResult. Neither ever raises for "server unreachable" —
that becomes available=False, which callers must map to needs_human.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Protocol

import httpx

from .models import ValidationResult
from .outcome import parse_operation_outcome


class FhirValidator(Protocol):
    """What the rest of the tool depends on. Backends are interchangeable."""

    name: str

    def health(self) -> tuple[bool, str]:
        """(available, detail). Cheap; safe to call before every run."""

    def validate(self, resource: dict[str, Any], profile: str = "") -> ValidationResult:
        """Validate one resource, optionally against a profile canonical URL."""


def _unavailable(resource: dict[str, Any], profile: str, backend: str, reason: str) -> ValidationResult:
    return ValidationResult(
        resource_type=resource.get("resourceType", "Unknown"),
        profile=profile,
        available=False,
        unavailable_reason=reason,
        backend=backend,
    )


#: FHIR version the ONC (g)(10) criteria target.
FHIR_VERSION = "4.0.1"

#: IG packages that must be loaded before a US Core profile can be resolved.
#: US Core 3.1.1 is the version (g)(10) requires for USCDI v1.
DEFAULT_IGS = ("hl7.fhir.us.core#3.1.1",)


def build_validation_request(
    resource: dict[str, Any],
    profile: str = "",
    *,
    fhir_version: str = FHIR_VERSION,
    igs: list[str] | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """The request envelope the HL7 validator wrapper expects.

    Verified against infernocommunity/inferno-resource-validator:1.0.78. Two
    things the service is strict about, both learned from 500s:

    1. Posting a bare resource fails with
       ``NullPointerException: ... ValidationRequest.getValidationContext() is null``.
       It wants a ValidationRequest: a validationContext plus filesToValidate,
       each carrying its content as an escaped JSON *string*.
    2. Naming a profile without loading the IG that defines it fails with
       ``Error: Unable to resolve profile <url>``. `igs` must list the package,
       e.g. hl7.fhir.us.core#3.1.1 — the server log shows ``IGs: []`` otherwise.
    """
    context: dict[str, Any] = {"sv": fhir_version, "igs": list(igs or DEFAULT_IGS)}
    if profile:
        context["profiles"] = [profile]
    request: dict[str, Any] = {
        "validationContext": context,
        "filesToValidate": [
            {
                "fileName": f"{resource.get('resourceType', 'resource')}.json",
                "fileContent": json.dumps(resource),
                "fileType": "json",
            }
        ],
    }
    if session_id:
        request["sessionId"] = session_id
    return request


def _first_outcome(payload: Any) -> dict[str, Any]:
    """The wrapper may return one OperationOutcome or a list (one per file)."""
    if isinstance(payload, list):
        return payload[0] if payload and isinstance(payload[0], dict) else {}
    if isinstance(payload, dict):
        # Some versions wrap the outcomes in an "outcomes" envelope.
        outcomes = payload.get("outcomes")
        if isinstance(outcomes, list) and outcomes:
            entry = outcomes[0]
            if isinstance(entry, dict):
                return entry.get("issues") or entry.get("outcome") or entry
        return payload
    return {}


class HL7ValidatorService:
    """Client for the Inferno resource-validator HTTP API.

    The g10 test kit runs this image internally and proxies /hl7validatorapi/ to
    hl7_validator_service:3500 (config/nginx.background.conf). We publish 3500
    directly — see docker/fhir-validator.compose.yml.

        GET  {base}/validator/version -> {"validatorWrapperVersion", "validatorVersion"}
        POST {base}/validate          -> ValidationRequest, returns OperationOutcome

    Note the first validate call is slow (minutes): the service downloads and
    loads the FHIR core + US Core packages before it can answer.
    """

    name = "hl7-validator-service"

    def __init__(self, base_url: str, timeout_s: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    def health(self) -> tuple[bool, str]:
        url = f"{self.base_url}/validator/version"
        try:
            resp = httpx.get(url, timeout=5.0)
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            return False, f"cannot reach validator service at {self.base_url}: {exc}"
        if resp.status_code != 200:
            return False, f"validator service at {url} returned HTTP {resp.status_code}"
        try:
            body = resp.json()
            detail = (
                f"validator {body.get('validatorVersion', '?')} "
                f"(wrapper {body.get('validatorWrapperVersion', '?')})"
            )
        except (json.JSONDecodeError, ValueError):
            detail = resp.text.strip()[:80]
        return True, detail

    def validate(self, resource: dict[str, Any], profile: str = "") -> ValidationResult:
        try:
            resp = httpx.post(
                f"{self.base_url}/validate",
                json=build_validation_request(resource, profile),
                timeout=self.timeout_s,
            )
            resp.raise_for_status()
            payload = resp.json()
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            return _unavailable(resource, profile, self.name, f"validator service call failed: {exc}")
        except (json.JSONDecodeError, ValueError) as exc:
            return _unavailable(resource, profile, self.name, f"validator returned non-JSON: {exc}")

        outcome = _first_outcome(payload)
        return ValidationResult(
            resource_type=resource.get("resourceType", "Unknown"),
            profile=profile,
            messages=parse_operation_outcome(outcome),
            backend=self.name,
            raw=outcome,
        )


class JavaCliValidator:
    """Subprocess wrapper around the official validator_cli.jar.

    Download: https://github.com/hapifhir/org.hl7.fhir.core/releases (validator_cli.jar)
    """

    name = "validator-cli-jar"

    def __init__(self, jar_path: str, timeout_s: float = 300.0, java_bin: str = "java") -> None:
        self.jar_path = jar_path
        self.timeout_s = timeout_s
        self.java_bin = java_bin

    def health(self) -> tuple[bool, str]:
        if not self.jar_path:
            return False, "VALIDATOR_JAR_PATH is not set"
        if not Path(self.jar_path).is_file():
            return False, f"validator jar not found at {self.jar_path}"
        if shutil.which(self.java_bin) is None:
            return False, f"no Java runtime on PATH ({self.java_bin}); install a JRE 11+"
        return True, f"validator_cli.jar at {self.jar_path}"

    def validate(self, resource: dict[str, Any], profile: str = "") -> ValidationResult:
        ok, detail = self.health()
        if not ok:
            return _unavailable(resource, profile, self.name, detail)

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "resource.json"
            out = Path(tmp) / "outcome.json"
            src.write_text(json.dumps(resource))

            cmd = [self.java_bin, "-jar", self.jar_path, str(src), "-version", "4.0.1",
                   "-output", str(out)]
            if profile:
                cmd += ["-profile", profile]

            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout_s)
            except subprocess.TimeoutExpired:
                return _unavailable(resource, profile, self.name,
                                    f"validator_cli.jar timed out after {self.timeout_s}s")
            except OSError as exc:
                return _unavailable(resource, profile, self.name, f"could not run java: {exc}")

            if not out.is_file():
                # Non-zero exit with no outcome file means the run itself failed.
                tail = (proc.stderr or proc.stdout or "").strip()[-400:]
                return _unavailable(resource, profile, self.name,
                                    f"validator produced no OperationOutcome (exit {proc.returncode}): {tail}")
            try:
                outcome = json.loads(out.read_text())
            except json.JSONDecodeError as exc:
                return _unavailable(resource, profile, self.name, f"unreadable OperationOutcome: {exc}")

        return ValidationResult(
            resource_type=resource.get("resourceType", "Unknown"),
            profile=profile,
            messages=parse_operation_outcome(outcome),
            backend=self.name,
            raw=outcome,
        )


class NullValidator:
    """Used when validation is switched off or no backend is usable.

    Returns available=False for everything, so downstream rules report
    needs_human instead of inventing a pass.
    """

    name = "none"

    def __init__(self, reason: str = "validator disabled (VALIDATOR_MODE=off)") -> None:
        self.reason = reason

    def health(self) -> tuple[bool, str]:
        return False, self.reason

    def validate(self, resource: dict[str, Any], profile: str = "") -> ValidationResult:
        return _unavailable(resource, profile, self.name, self.reason)
