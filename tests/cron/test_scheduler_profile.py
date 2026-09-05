"""Real profile and ledger paths, with only the outbound delivery mocked."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("dispatch", ["manual", "scheduled"])
@pytest.mark.parametrize("multiplex", [False, True])
def test_profile_run_isolates_runtime_and_preserves_owner_store(tmp_path, monkeypatch, dispatch, multiplex):
    from agent import secret_scope
    from agent.secret_scope import get_secret
    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", multiplex)
    from cron import scheduler
    from cron.executions import list_executions
    from cron.jobs import create_job, get_job, trigger_job
    from hermes_cli.config import load_config
    from hermes_constants import get_hermes_home
    from tools.cronjob_tools import cronjob

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    owner = tmp_path / ".hermes"
    profile = owner / "profiles" / "worker"
    scripts = profile / "scripts"
    scripts.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(owner))
    monkeypatch.setenv("CRON_PROFILE_VALUE", "owner")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "owner-token")
    (owner / "config.yaml").write_text("model: {default: owner-model}\n")
    (profile / "config.yaml").write_text("model: {default: worker-model}\n")
    (profile / ".env").write_text(
        "CRON_PROFILE_VALUE=worker\nTELEGRAM_BOT_TOKEN=worker-token\n")
    (scripts / "probe.py").write_text(
        "import json, os\nprint(json.dumps({key: os.environ.get(key) for key in "
        "['HERMES_HOME', 'CRON_PROFILE_VALUE']}))\n")
    live_adapters = {"telegram": object()}
    live_loop = object()
    deliveries = []

    def deliver(job, response, *, adapters, loop, **kwargs):
        assert get_hermes_home() == profile
        assert load_config()["model"]["default"] == "worker-model"
        assert get_secret("TELEGRAM_BOT_TOKEN") == "worker-token"
        assert adapters is None and loop is None
        assert {home for _, home in scheduler._running_fire_owners[job["id"]].values()} == {owner}
        assert json.loads(response) == {
            "HERMES_HOME": str(profile), "CRON_PROFILE_VALUE": "worker"}
        # An unrelated gateway thread must retain its original profile while this run is active.
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(get_hermes_home).result(timeout=5) == owner
            assert pool.submit(load_config).result(timeout=5)["model"]["default"] == "owner-model"
        deliveries.append(job["execution_id"])

    monkeypatch.setattr(scheduler, "_deliver_result", deliver)
    job = create_job("", "every 1h", script="probe.py", no_agent=True,
                     profile="worker", deliver="telegram:123")
    if dispatch == "manual":
        import gateway.run
        runner = SimpleNamespace(adapters=live_adapters, _gateway_loop=live_loop)
        monkeypatch.setattr(gateway.run, "_gateway_runner_ref", lambda: runner)
        result = json.loads(cronjob(action="run", job_id=job["id"]))
        assert result["job"]["execution_success"], result
    else:
        trigger_job(job["id"])
        assert scheduler.tick(verbose=False, adapters=live_adapters, loop=live_loop) == 1

    assert get_job(job["id"])["last_status"] == "ok"
    attempts = list_executions(job_id=job["id"])
    assert [attempt["id"] for attempt in attempts] == deliveries
    assert len(deliveries) == 1
    assert attempts[0]["status"] == "completed"
    assert get_hermes_home() == owner
    assert load_config()["model"]["default"] == "owner-model"
    assert os.environ["CRON_PROFILE_VALUE"] == "owner"
    assert os.environ["TELEGRAM_BOT_TOKEN"] == "owner-token"
    assert not (profile / "cron" / "jobs.json").exists()


@pytest.mark.parametrize("id_source", ["record", "argument", "both", "new"])
def test_execution_identity_reaches_handoff_without_duplicate(tmp_path, monkeypatch, id_source):
    from cron import scheduler
    from cron.executions import create_execution, list_executions

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    job = {"id": "identity-job"}
    record = create_execution(job["id"], source="builtin") if id_source in {"record", "both"} else None
    explicit = create_execution(job["id"], source="direct") if id_source in {"argument", "both"} else None
    if record:
        job["execution_id"] = record["id"]
    before = {row["id"] for row in list_executions(job_id=job["id"])}
    handed_off = []
    monkeypatch.setattr(scheduler, "_launch_external_cron_worker",
                        lambda job: handed_off.append(job["execution_id"]) or True)
    assert scheduler.run_one_job(job, execution_id=explicit["id"] if explicit else None)
    after = {row["id"] for row in list_executions(job_id=job["id"])}
    expected = (explicit or record or {"id": job["execution_id"]})["id"]
    assert handed_off == [expected]
    assert after == (before or {expected})
