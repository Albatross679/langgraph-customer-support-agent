from arq.worker import get_kwargs
from scripts.evaluation_worker import WorkerSettings

from support_copilot import worker


def test_real_arq_consumer_registers_evaluation_functions_and_recovery():
    settings = get_kwargs(WorkerSettings)
    assert settings["functions"] == [worker.run_agent, worker.resume_agent, worker.recover_agent]
    assert settings["cron_jobs"] == worker.WorkerSettings.cron_jobs
    assert settings["on_shutdown"] is worker.shutdown
    assert settings["max_tries"] == 1
