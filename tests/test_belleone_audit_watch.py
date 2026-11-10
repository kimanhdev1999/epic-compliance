"""scripts/belleone_audit_watch.py: checks read origin/main of each repo, never the working copy."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "belleone_audit_watch.py"
spec = importlib.util.spec_from_file_location("belleone_audit_watch", SCRIPT)
watch = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = watch  # dataclasses look the module up by name
spec.loader.exec_module(watch)

# A BelleOne with everything SEC-002 needs, as it would look once SRE-233 and SRE-171 land.
DONE = {
    "terraform": {
        "modules/belleone-security-audit-log/main.tf":
            'resource "aws_cloudwatch_log_group" "a" { name = "/belle/security-audit/prod" }\n'
            'resource "aws_iam_policy" "w" { name = "belleone-prod-security-audit-writer" }\n',
        "env/prod/us/belleone/s3-access-logs.tf":
            'locals { access_logged_buckets = { (a) = "app-backend/", (b) = "ai-image/" } }\n'
            'resource "aws_s3_bucket_logging" "logged" {}\n'
            'resource "aws_cloudtrail" "belleone" { data_resource { type = "AWS::S3::Object" } }\n',
        "env/prod/eu/belleone/s3-access-logs.tf":
            'locals { access_logged_buckets = { (a) = "app-backend/", (b) = "ai_processing_image/" } }\n'
            'resource "aws_s3_bucket_logging" "logged" {}\n',
        "env/prod/us/belleone/variables.tf":
            'variable "s3_access_log_retention_days" {\n  type = number\n  default = 2190\n}\n',
        "env/prod/eu/belleone/terraform.tfvars": "s3_access_log_retention_days = 2190\n",
    },
    "backend": {
        "internal/entity/phi_access_log.go":
            'const (\n\tPHIAccessViewImage PHIAccessAction = "view_image"\n)\ntype PHIAccessLog struct {}\n',
        "internal/infrastructure/securityaudit/writer.go": '// writes to /belle/security-audit\n',
        "internal/usecase/auth_impl.go": 'audit.Info("login", zap.String("outcome", "failure"))\n',
    },
    "backoffice": {"internal/delivery/http/handler/patient_handler.go": "errors.Is(err, usecase.ErrAuditUnavailable)\n"},
    "mobile": {"lib/main.dart": "void main() {}\n"},
}


def _repo(root: Path, rel: str, files: dict[str, str], working_copy: dict[str, str] | None = None) -> None:
    """A repo whose origin/main holds ``files``; the working copy can differ."""
    path = root / rel
    path.mkdir(parents=True)
    git = ["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run([*git[:3], "init", "-q", "-b", "main"], check=True)
    for name, text in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(text)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "main"], check=True)
    subprocess.run([*git, "update-ref", "refs/remotes/origin/main", "HEAD"], check=True)
    for name, text in (working_copy or {}).items():  # uncommitted local work
        (path / name).write_text(text)


def _root(tmp_path: Path, content: dict[str, dict[str, str]], **working) -> Path:
    for name, (rel, _branch) in watch.REPOS.items():
        _repo(tmp_path, rel, content[name], working.get(name))
    return tmp_path


def _status(round_: dict) -> dict[str, str]:
    return {x["id"]: x["status"] for x in round_["results"]}


def test_everything_in_place_has_no_gaps(tmp_path):
    status = _status(watch.run_round(_root(tmp_path, DONE), fetch=False))
    assert {k: v for k, v in status.items() if v == "gap"} == {}
    assert status["B4"] == "info"  # no OnBase delivery code yet


def test_todays_belleone_gaps_are_reported(tmp_path):
    today = {**DONE, "terraform": {
        "env/prod/us/belleone/s3-access-logs.tf": 'locals { access_logged_buckets = { (a) = "app-backend/" } }\n'
                                                  'resource "aws_s3_bucket_logging" "logged" {}\n',
        "env/prod/eu/belleone/s3-access-logs.tf": 'resource "aws_s3_bucket_logging" "logged" {}\napp-backend\n',
        "env/prod/us/belleone/variables.tf":
            'variable "s3_access_log_retention_days" {\n  default = 365\n}\n',
    }, "backend": {k: v for k, v in DONE["backend"].items() if "securityaudit" not in k and "auth" not in k},
        "mobile": {"lib/save.dart": "// PHPhotoLibrary add-only on iOS\n"}}
    status = _status(watch.run_round(_root(tmp_path, today), fetch=False))
    assert {i for i, s in status.items() if s == "gap"} == {"T1", "T2", "T4", "T5", "T6", "B2", "B3", "M1"}
    assert status["T3"] == status["B1"] == status["O1"] == "ok"


def test_reads_main_not_the_working_copy(tmp_path):
    # Uncommitted local work that would pass T1 must not count: only origin/main does.
    no_group = {**DONE, "terraform": {k: v for k, v in DONE["terraform"].items() if "security-audit" not in k}}
    root = _root(tmp_path, no_group, terraform={"local.tf": 'name = "/belle/security-audit/prod"\n'})
    assert _status(watch.run_round(root, fetch=False))["T1"] == "gap"


def test_onbase_word_match_ignores_lookalikes(tmp_path):
    lookalike = {**DONE, "backend": {**DONE["backend"], "x.go": "renditionBaseWidth = 400\n"}}
    assert _status(watch.run_round(_root(tmp_path, lookalike), fetch=False))["B4"] == "info"


def test_delivery_without_export_audit_is_a_gap(tmp_path):
    built = {**DONE, "backend": {**DONE["backend"], "delivery.go": "client := sftp.NewClient(conn)\n"}}
    assert _status(watch.run_round(_root(tmp_path, built), fetch=False))["B4"] == "gap"


def test_report_says_what_changed(tmp_path):
    cur = watch.run_round(_root(tmp_path, DONE), fetch=False)
    prev = {"results": [{**x, "status": "gap"} if x["id"] == "T1" else x for x in cur["results"]]}
    text = watch.report(cur, prev)
    assert "T1 Security-audit log group (6-year, immutable) exists: gap -> ok" in text
    assert "No change since the last round." in watch.report(cur, cur)


def test_missing_repo_is_an_error_not_a_crash(tmp_path):
    out = watch.run_round(tmp_path / "nowhere", fetch=False)
    assert set(out["errors"]) == set(watch.REPOS) and all(x["status"] == "error" for x in out["results"])


@pytest.mark.parametrize("args", [["--once", "--no-fetch"]])
def test_once_exits_nonzero_on_gaps_and_saves_state(tmp_path, args, capsys):
    root = _root(tmp_path / "repos", {**DONE, "mobile": {"lib/s.dart": "PHPhotoLibrary\n"}})
    state = tmp_path / "state.json"
    assert watch.main([*args, "--root", str(root), "--state", str(state)]) == 1
    assert state.exists() and "GAP  M1" in capsys.readouterr().out
