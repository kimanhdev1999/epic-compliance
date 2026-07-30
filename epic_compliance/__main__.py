"""CLI entrypoint: python -m epic_compliance run"""
from __future__ import annotations

import sys

from rich.console import Console
from rich.table import Table
from rich import box

from .config import get_config
from .pipeline import run_full_pipeline

console = Console()

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
VERDICT_STYLE = {
    "pass": "green",
    "fail": "bold red",
    "needs_human": "yellow",
}


def cmd_run() -> None:
    config = get_config()
    console.print(f"\n[bold]Epic Compliance Tool[/bold] — mode=[cyan]{config.run_mode}[/cyan]")
    console.print(f"FHIR base: {config.fhir_base_url}\n")

    report = run_full_pipeline(config)
    summary = report.summary()
    counts = summary["counts"]

    console.print(
        f"[bold]Run ID:[/bold] {report.run_id}  "
        f"pass=[green]{counts['pass']}[/green]  "
        f"fail=[red]{counts['fail']}[/red]  "
        f"needs_human=[yellow]{counts['needs_human']}[/yellow]\n"
    )

    by_category = summary["by_category"]
    for category in sorted(by_category.keys()):
        findings = by_category[category]
        findings.sort(key=lambda f: SEVERITY_ORDER.get(f["severity"], 99))

        table = Table(
            title=f"Category: {category.upper()}",
            box=box.SIMPLE_HEAVY,
            show_lines=True,
        )
        table.add_column("Rule ID", style="dim", width=12)
        table.add_column("Severity", width=10)
        table.add_column("Verdict", width=14)
        table.add_column("Evidence", width=50)
        table.add_column("Hint", width=40)

        for f in findings:
            verdict_str = f["verdict"]
            style = VERDICT_STYLE.get(verdict_str, "white")
            evidence = f["evidence"]
            if len(evidence) > 80:
                evidence = evidence[:77] + "..."
            hint = f["remediation_hint"]
            if len(hint) > 60:
                hint = hint[:57] + "..."
            table.add_row(
                f["rule_id"],
                f["severity"],
                f"[{style}]{verdict_str}[/{style}]",
                evidence,
                hint or "-",
            )

        console.print(table)

    # Exit non-zero if any failures
    if counts["fail"] > 0:
        sys.exit(1)


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] == "run":
        cmd_run()
    elif args[0] == "serve":
        import uvicorn
        port = 8000
        console.print(
            f"\n[bold]Epic Compliance[/bold] web app → "
            f"[cyan]http://localhost:{port}[/cyan]\n"
        )
        uvicorn.run("epic_compliance.api:app", host="0.0.0.0", port=port, reload=False)
    else:
        console.print(f"[red]Unknown command: {args[0]}[/red]")
        console.print("Usage: python -m epic_compliance [run|serve]")
        sys.exit(1)


if __name__ == "__main__":
    main()
