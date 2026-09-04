"""Prefect deployment and worker entry point for ContextMine sync flows."""

import asyncio
import logging
from pathlib import Path

from contextmine_core import get_settings
from contextmine_core.lsp import shutdown_lsp_manager
from contextmine_core.telemetry import init_telemetry
from prefect.workers.process import ProcessWorker

from contextmine_worker.flows import sync_due_sources, sync_single_source
from contextmine_worker.init_prefect import init_prefect

logger = logging.getLogger(__name__)

# The deployments below use a relative entrypoint (e.g. "contextmine_worker/flows.py:...")
# resolved against this directory. The ProcessWorker's default `working_dir` job variable
# is `null`, which makes it launch each flow run in a fresh temporary directory instead of
# here - causing the relative entrypoint to be unresolvable and the flow run to crash with
# FileNotFoundError. Pin `working_dir` so flow runs execute with the correct CWD.
_WORKER_ROOT = Path(__file__).resolve().parent.parent


def configure_deployments() -> None:
    """Apply the two supported source-sync deployments."""
    settings = get_settings()
    sync_due_sources.to_deployment(
        name="default",
        interval=settings.prefect_due_interval_seconds,
        work_pool_name=settings.prefect_work_pool_name,
        job_variables={"working_dir": str(_WORKER_ROOT)},
    ).apply()
    sync_single_source.to_deployment(
        name="default",
        work_pool_name=settings.prefect_work_pool_name,
        job_variables={"working_dir": str(_WORKER_ROOT)},
    ).apply()


async def run_worker() -> None:
    """Run Prefect's process worker with supported cancellation and cleanup."""
    settings = get_settings()
    worker = ProcessWorker(
        work_pool_name=settings.prefect_work_pool_name,
        create_pool_if_not_found=False,
        limit=settings.prefect_worker_limit,
    )
    try:
        await worker.start(with_healthcheck=True)
    finally:
        await shutdown_lsp_manager()


def main() -> None:
    """Configure deployments and start the official Prefect worker."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    init_telemetry(service_suffix="-worker")
    asyncio.run(init_prefect())
    configure_deployments()
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
