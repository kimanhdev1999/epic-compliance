#!/usr/bin/env python3
"""Watch BelleOne's audit-logging readiness (SEC-002) on the latest main.

Each round fetches the BelleOne repos and checks their ``origin/main`` (or the
branch given per repo) for the pieces SEC-002 needs: who opened or changed which
patient record, sign-in events, the photo buckets' access logs, and 6-year
retention. It reads the fetched commits with ``git grep`` / ``git show`` only:
your working copies, local branches and uncommitted work are never touched, so
it's safe to run while you work on a feature branch.

    python scripts/belleone_audit_watch.py --once            # one report
    python scripts/belleone_audit_watch.py --interval 30     # every 30 minutes
    python scripts/belleone_audit_watch.py --once --json     # machine-readable

Repos default to ~/BelleGitRepos/...; override with BELLEONE_REPOS_ROOT or
--root. Each round's results are saved (--state) and the report says what
changed since the last round, so a loop shows when a ticket's work lands.

What each check maps to: SRE-233 (app access/auth events to the 6-year
security-audit log), SRE-171 (S3 access logs + CloudTrail data events for the
PHI buckets), SRE-164/SRE-200 (6-year retention). See docs/progress.md.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

HIPAA_RETENTION_DAYS = 2190  # 6 years, HIPAA §164.316(b)(2)

#: name -> (path under the root, branch to check)
REPOS = {
    "terraform": ("InfraOpsEngineering/terraform-bellle-v2", "main"),
    "backend": ("BelleOne/BelleOne-backend", "main"),
    "backoffice": ("BelleOne/BelleOne-backoffice-backend", "main"),
    "mobile": ("BelleOne/BelleOne-mobile", "main"),
}


@dataclass
class Result:
    id: str
    repo: str
    title: str
    status: str  # "ok", "gap", "info" or "error"
    detail: str
    ticket: str = ""


class Repo:
    """Read-only view of one repo's fetched branch."""

    def __init__(self, path: Path, branch: str):
        self.path, self.ref = path, f"origin/{branch}"

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(self.path), *args], capture_output=True, text=True, timeout=300)

    def fetch(self) -> str:
        """Fetch the branch; returns "" or an error. Never touches the working copy."""
        branch = self.ref.split("/", 1)[1]
        out = self._git("fetch", "--quiet", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}")
        return out.stderr.strip() if out.returncode else ""

    def head(self) -> str:
        out = self._git("log", "-1", "--format=%h %cs %s", self.ref)
        return out.stdout.strip()[:90] if out.returncode == 0 else "?"

    def grep(self, pattern: str, *pathspecs: str, ignore_case: bool = True) -> list[str]:
        """Lines on the branch matching ``pattern`` (extended regex), as "path:line:text"."""
        args = ["grep", "-n", "-I", "-E"] + (["-i"] if ignore_case else []) + [pattern, self.ref, "--", *pathspecs]
        out = self._git(*args)
        prefix = f"{self.ref}:"
        return [line.removeprefix(prefix) for line in out.stdout.splitlines()]

    def show(self, path: str) -> str:
        out = self._git("show", f"{self.ref}:{path}")
        return out.stdout if out.returncode == 0 else ""


def _first(lines: list[str], n: int = 2) -> str:
    return "; ".join(line[:140] for line in lines[:n])


# --------------------------------------------------------------------------- #
# Checks. Each takes the repos and returns a Result.
# --------------------------------------------------------------------------- #
def check_audit_log_group(r: dict[str, Repo]) -> Result:
    hits = r["terraform"].grep(r"security-audit|security_audit", "*.tf")
    return Result("T1", "terraform", "Security-audit log group (6-year, immutable) exists",
                  "ok" if hits else "gap",
                  _first(hits) if hits else "No /belle/security-audit log group or module on main: "
                  "SRE-233 says it exists, but it doesn't.", "SRE-233")


def check_writer_policy(r: dict[str, Repo]) -> Result:
    hits = r["terraform"].grep(r"security-audit-writer|security_audit_writer", "*.tf")
    return Result("T2", "terraform", "Backend task role can write to the security-audit log",
                  "ok" if hits else "gap",
                  _first(hits) if hits else "No security-audit-writer policy attached to the ECS task role.",
                  "SRE-233")


def _access_logged_maps(r: dict[str, Repo]) -> str:
    return "\n".join(r["terraform"].show(f"env/prod/{region}/belleone/s3-access-logs.tf") for region in ("us", "eu"))


def check_s3_access_logs_app(r: dict[str, Repo]) -> Result:
    text = _access_logged_maps(r)
    ok = "aws_s3_bucket_logging" in text and re.search(r"app-backend", text)
    return Result("T3", "terraform", "S3 access logs on the app bucket (prod US + EU)",
                  "ok" if ok else "gap",
                  "env/prod/{us,eu}/belleone/s3-access-logs.tf logs the app bucket" if ok
                  else "No aws_s3_bucket_logging for the BelleOne app bucket.", "SRE-171")


def check_s3_access_logs_ai(r: dict[str, Repo]) -> Result:
    text = _access_logged_maps(r)
    ok = re.search(r"ai[_-]?(processing[_-]?)?image", text, re.I)
    return Result("T4", "terraform", "S3 access logs on the AI image bucket (lesion photos)",
                  "ok" if ok else "gap",
                  "AI image bucket is in access_logged_buckets" if ok
                  else "belleone-ai-image-processed.* isn't in access_logged_buckets.", "SRE-171")


def check_s3_access_log_retention(r: dict[str, Repo]) -> Result:
    days = []
    for region in ("us", "eu"):
        var = r["terraform"].show(f"env/prod/{region}/belleone/variables.tf")
        tfvars = r["terraform"].show(f"env/prod/{region}/belleone/terraform.tfvars")
        m = re.search(r"s3_access_log_retention_days\s*=\s*(\d+)", tfvars) or re.search(
            r'variable "s3_access_log_retention_days"[^}]*?default\s*=\s*(\d+)', var, re.S)
        days.append(int(m.group(1)) if m else 0)
    ok = all(d >= HIPAA_RETENTION_DAYS for d in days)
    return Result("T5", "terraform", f"S3 access logs kept {HIPAA_RETENTION_DAYS} days (6 years)",
                  "ok" if ok else "gap", f"prod us/eu retention: {days[0]} / {days[1]} days", "SRE-171")


def check_cloudtrail_data_events(r: dict[str, Repo]) -> Result:
    hits = [h for h in r["terraform"].grep(r"AWS::S3::Object", "*.tf") if "belleone" in h.lower()]
    return Result("T6", "terraform", "CloudTrail data events (Get/Put/Delete) on BelleOne PHI buckets",
                  "ok" if hits else "gap",
                  _first(hits) if hits else "Only the belle-ai-provider module has S3 data events; "
                  "none for BelleOne's buckets.", "SRE-171")


def check_phi_trail(r: dict[str, Repo]) -> Result:
    hits = r["backend"].grep(r"type PHIAccessLog struct", "internal/entity/*.go", ignore_case=False)
    actions = r["backend"].grep(r'PHIAccessAction = "', "internal/entity/phi_access_log.go", ignore_case=False)
    names = [re.search(r'"(\w+)"', a).group(1) for a in actions if re.search(r'"(\w+)"', a)]
    return Result("B1", "backend", "PHI access trail (who opened/changed which patient record)",
                  "ok" if hits else "gap",
                  f"phi_access_logs collection, {len(names)} actions: {', '.join(names)}" if hits
                  else "No PHI access trail entity.", "SRE-233")


def check_phi_trail_retained(r: dict[str, Repo]) -> Result:
    hits = r["backend"].grep(r"security-audit|security_audit|SecurityAudit", "*.go")
    return Result("B2", "backend", "PHI trail also sent to the 6-year immutable security-audit log",
                  "ok" if hits else "gap",
                  _first(hits) if hits else "The trail is only in DocumentDB (phi_access_logs): append-only "
                  "in code, but the app's DB user could still edit it, and it follows backup retention.",
                  "SRE-233")


def check_auth_events(r: dict[str, Repo]) -> Result:
    hits = r["backend"].grep(r'audit.*(login|logout|sign.?in)|(login|logout).*audit|"event".*"auth"',
                             "internal/usecase/auth*.go", "internal/delivery/http/handler/auth*.go")
    return Result("B3", "backend", "Sign-in events (login, logout, failed login) in the audit log",
                  "ok" if hits else "gap",
                  _first(hits) if hits else "Failed logins only reach the app log (\"login failed: wrong "
                  "password\"), without user ID or IP, and the access middleware skips unauthenticated calls.",
                  "SRE-233")


def check_delivery_logged(r: dict[str, Repo]) -> Result:
    # git grep -E has no \b: bound the words by hand ("renditionBaseWidth" isn't OnBase).
    code = r["backend"].grep(r"(^|[^a-z])(sftp|onbase)([^a-z]|$)", "*.go")
    if not code:
        return Result("B4", "backend", "PDF delivery to OnBase (sFTP) logged as an export",
                      "info", "No sFTP/OnBase delivery code on main yet: log it as export_pdf when it's built.",
                      "SRE-286")
    logged = [h for h in code if re.search(r"PHIAccessExport|export_pdf|phiAudit", h)]
    return Result("B4", "backend", "PDF delivery to OnBase (sFTP) logged as an export",
                  "ok" if logged else "gap", _first(logged or code), "SRE-286")


def check_backoffice_audited(r: dict[str, Repo]) -> Result:
    hits = r["backoffice"].grep(r"ErrAuditUnavailable", "internal/delivery/http/handler/patient*.go")
    return Result("O1", "backoffice", "Back-office patient reads are audited (fail closed)",
                  "ok" if hits else "gap",
                  "patient handlers return ErrAuditUnavailable when the audit write fails" if hits
                  else "No audit on back-office patient reads.")


def check_mobile_gallery(r: dict[str, Repo]) -> Result:
    hits = r["mobile"].grep(r"PHPhotoLibrary|MediaStore|saveImageToGallery|gallery_saver|ImageGallerySaver", "lib/*")
    return Result("M1", "mobile", "Photos not saved to the phone's photo library",
                  "gap" if hits else "ok",
                  "The app can save images to the phone's photo library: " + _first(hits) if hits
                  else "No photo-library saving found.", "")


CHECKS: list[Callable[[dict[str, Repo]], Result]] = [
    check_audit_log_group, check_writer_policy, check_s3_access_logs_app, check_s3_access_logs_ai,
    check_s3_access_log_retention, check_cloudtrail_data_events,
    check_phi_trail, check_phi_trail_retained, check_auth_events, check_delivery_logged,
    check_backoffice_audited, check_mobile_gallery,
]


# --------------------------------------------------------------------------- #
# Round, report and loop
# --------------------------------------------------------------------------- #
def run_round(root: Path, fetch: bool = True) -> dict:
    repos, heads, errors = {}, {}, {}
    for name, (rel, branch) in REPOS.items():
        repo = Repo(root / rel, branch)
        if not (root / rel / ".git").exists():
            errors[name] = f"not found at {root / rel}"
            continue
        if fetch and (err := repo.fetch()):
            errors[name] = f"fetch failed: {err[:200]}"
        repos[name], heads[name] = repo, repo.head()
    results = []
    for check in CHECKS:
        try:
            results.append(check(repos))
        except KeyError as exc:  # a repo is missing
            results.append(Result(check.__name__, str(exc).strip("'"), check.__name__, "error", "repo not found"))
    return {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "heads": heads, "errors": errors,
            "results": [asdict(x) for x in results]}


def changes(prev: dict | None, cur: dict) -> list[str]:
    if not prev:
        return []
    before = {x["id"]: x["status"] for x in prev.get("results", [])}
    return [f"{x['id']} {x['title']}: {before[x['id']]} -> {x['status']}"
            for x in cur["results"] if x["id"] in before and before[x["id"]] != x["status"]]


def report(cur: dict, prev: dict | None) -> str:
    mark = {"ok": "OK  ", "gap": "GAP ", "info": "INFO", "error": "ERR "}
    lines = [f"BelleOne audit-logging readiness (SEC-002) — {cur['at']}", ""]
    for name, head in cur["heads"].items():
        lines.append(f"  {name:<10} {REPOS[name][1]:<8} {head}")
    for name, err in cur["errors"].items():
        lines.append(f"  {name:<10} !! {err}")
    lines.append("")
    for x in cur["results"]:
        ticket = f" [{x['ticket']}]" if x["ticket"] else ""
        lines.append(f"{mark[x['status']]} {x['id']:<3} {x['title']}{ticket}")
        lines.append(f"          {x['detail']}")
    gaps = sum(1 for x in cur["results"] if x["status"] == "gap")
    lines += ["", f"{gaps} gap(s) of {len(cur['results'])} checks."]
    if diff := changes(prev, cur):
        lines += ["", "Changed since the last round:"] + [f"  * {d}" for d in diff]
    elif prev:
        lines += ["", "No change since the last round."]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", default=os.environ.get("BELLEONE_REPOS_ROOT", str(Path.home() / "BelleGitRepos")))
    ap.add_argument("--once", action="store_true", help="run one round and exit")
    ap.add_argument("--interval", type=float, default=30, help="minutes between rounds (default 30)")
    ap.add_argument("--no-fetch", action="store_true", help="check what's already fetched")
    ap.add_argument("--json", action="store_true", help="print the round as JSON")
    ap.add_argument("--state", default=str(Path.home() / ".cache" / "belleone-audit-watch" / "last.json"))
    args = ap.parse_args(argv)

    state = Path(args.state)
    while True:
        prev = json.loads(state.read_text()) if state.exists() else None
        cur = run_round(Path(args.root).expanduser(), fetch=not args.no_fetch)
        print(json.dumps(cur, indent=2) if args.json else report(cur, prev), flush=True)
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps(cur, indent=2))
        if args.once:
            return 1 if any(x["status"] == "gap" for x in cur["results"]) else 0
        print(f"\nNext round in {args.interval:g} min (Ctrl-C to stop).\n", flush=True)
        try:
            time.sleep(args.interval * 60)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    sys.exit(main())
