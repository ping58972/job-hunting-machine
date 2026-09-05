"""The explicit local initialization command is repeatable and stays in scope."""

import json
from pathlib import Path

from sqlalchemy import text
from typer.testing import CliRunner

from job_hunting_machine.cli import app
from job_hunting_machine.database.engine import Database
from job_hunting_machine.security.paths import PROJECT_ROOT


def test_database_init_migrates_seeds_and_preserves_policy_on_repeat(tmp_path: Path) -> None:
    runner = CliRunner()
    target = tmp_path / "job-hunting.db"
    result = runner.invoke(app, ["db", "init", "--database", str(target)])
    assert result.exit_code == 0, result.output
    output = json.loads(result.stdout)
    assert output["database"] == str(target)
    assert output["seed"]["rule_sets_created"] == 1
    assert output["seed"]["salary_rules_created"] == 3
    repeated = runner.invoke(app, ["db", "init", "--database", str(target)])
    assert repeated.exit_code == 0, repeated.output
    assert all(count == 0 for count in json.loads(repeated.stdout)["seed"].values())
    database = Database(target)
    try:
        with database.transaction() as session:
            for table in (
                "check_job_position_quality",
                "application_pipeline",
                "agent_queue",
                "my_information_for_filling_form",
                "candidate_facts",
                "external_actions",
            ):
                assert session.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one() == 0
    finally:
        database.dispose()


def test_database_init_rejects_outside_path_without_creating_file() -> None:
    result = CliRunner().invoke(
        app, ["db", "init", "--database", str(PROJECT_ROOT.parent / "forbidden-jhm.db")]
    )
    assert result.exit_code == 2
    assert "initialization failed" in result.stderr
