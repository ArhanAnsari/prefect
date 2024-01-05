import asyncio
import inspect
import uuid
from typing import Any, Dict, List, Optional, Union

import anyio
import httpx
from requests.exceptions import HTTPError
from typing_extensions import Literal

from prefect.client.orchestration import get_client
from prefect.client.schemas.filters import FlowRunFilter, TaskRunFilter
from prefect.context import FlowRunContext
from prefect.flows import Flow
from prefect.logging import get_logger
from prefect.settings import (
    PREFECT_EXPERIMENTAL_ENABLE_EXTRA_RUNNER_ENDPOINTS,
    PREFECT_RUNNER_PROCESS_LIMIT,
    PREFECT_RUNNER_SERVER_HOST,
    PREFECT_RUNNER_SERVER_PORT,
)
from prefect.states import Pending
from prefect.tasks import Task
from prefect.utilities.asyncutils import sync_compatible

logger = get_logger("webserver")


async def get_current_run_count() -> int:
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"http://{PREFECT_RUNNER_SERVER_HOST.value()}"
            f":{PREFECT_RUNNER_SERVER_PORT.value()}/run_count"
        )
        response.raise_for_status()
        return response.json()


async def _submit_flow_to_runner(
    flow: Flow,
    parameters: Dict[str, Any],
    # capture_errors: bool = SETTING.value()?
) -> uuid.UUID:
    """
    Run a callable in the background via the runner webserver.

    Args:
        prefect_callable: the callable to run, e.g. a flow or task
        parameters: the keyword arguments to pass to the callable
        timeout: the maximum time to wait for the callable to finish
        poll_interval: the interval (in seconds) to wait between polling the callable
    """
    if not PREFECT_EXPERIMENTAL_ENABLE_EXTRA_RUNNER_ENDPOINTS.value():
        raise ValueError(
            "The `submit_to_runner` utility requires the `Runner` webserver to be"
            " built with extra endpoints enabled. To enable this, set the"
            " `PREFECT_EXPERIMENTAL_ENABLE_EXTRA_RUNNER_ENDPOINTS` setting to `True`."
        )

    from prefect.engine import (
        _dynamic_key_for_task_run,
        collect_task_run_inputs,
        resolve_inputs,
    )

    async with get_client() as client:
        parent_flow_run_context = FlowRunContext.get()

        task_inputs = {
            k: await collect_task_run_inputs(v) for k, v in parameters.items()
        }
        parameters = await resolve_inputs(parameters)
        dummy_task = Task(name=flow.name, fn=flow.fn, version=flow.version)
        parent_task_run = await client.create_task_run(
            task=dummy_task,
            flow_run_id=parent_flow_run_context.flow_run.id,
            dynamic_key=_dynamic_key_for_task_run(parent_flow_run_context, dummy_task),
            task_inputs=task_inputs,
            state=Pending(),
        )

        response = await client._client.post(
            (
                f"http://{PREFECT_RUNNER_SERVER_HOST.value()}"
                f":{PREFECT_RUNNER_SERVER_PORT.value()}"
                "/flow/run"
            ),
            json={
                "entrypoint": flow._entrypoint,
                "parameters": flow.serialize_parameters(parameters),
                "parent_task_run_id": str(parent_task_run.id),
            },
        )
        response.raise_for_status()

        flow_run_id = response.json()["flow_run_id"]

        return uuid.UUID(flow_run_id)


@sync_compatible
async def submit_to_runner(
    prefect_callable: Union[Flow, Task],
    parameters: Union[Dict[str, Any], List[Dict[str, Any]]],
    retry_failed_submissions: bool = True,
) -> Union[uuid.UUID, List[uuid.UUID]]:
    """
    Run a callable in the background via the runner webserver.

    Args:
        prefect_callable: the callable to run (only flows are supported for now, but eventually tasks)
        parameters: keyword arguments to pass to the callable. May be a list of dictionaries where
            each dictionary represents a discrete invocation of the callable
    """
    if isinstance(parameters, dict):
        parameters = [parameters]

    submitted_run_ids = []
    for p in parameters:
        try:
            flow_run_id = await _submit_flow_to_runner(prefect_callable, p)
        except HTTPError:
            # When case client-side retries still fail, continue submitting the
            # next run
            break

        if inspect.isawaitable(flow_run_id):
            flow_run_id = await flow_run_id
        submitted_run_ids.append(flow_run_id)

    if (diff := len(parameters) - len(submitted_run_ids)) > 0:
        logger.warning(
            f"Failed to submit {diff} to the runner, as all of the available "
            f"{PREFECT_RUNNER_PROCESS_LIMIT.value()}slots were occupied. To "
            "increase the number of available slots, configure the"
            "`PREFECT_RUNNER_PROCESS_LIMIT` setting."
        )

    # If one run was submitted, return the run_id directly
    if len(parameters) == 1:
        return submitted_run_ids[0]
    return submitted_run_ids


@sync_compatible
async def wait_for_submitted_runs(
    flow_run_filter: Optional[FlowRunFilter] = None,
    task_run_filter: Optional[TaskRunFilter] = None,
    timeout: Optional[float] = None,
    poll_interval: float = 3.0,
):
    """
    Wait for completion of any provided flow runs (eventually task runs), as well as subflow runs
    of the current flow run (if called from within a flow run and subflow runs exist).

    Args:
        flow_run_filter: A filter to apply to the flow runs to wait for.
        task_run_filter: A filter to apply to the task runs to wait for. # TODO: /task/run
        timeout: How long to wait for completion of all runs (seconds).
        poll_interval: How long to wait between polling each run's state (seconds).
    """

    parent_flow_run_id = ctx.flow_run.id if (ctx := FlowRunContext.get()) else None

    if task_run_filter:
        raise NotImplementedError("Waiting for task runs is not yet supported.")

    async def wait_for_final_state(
        run_type: Literal["flow", "task"], run_id: uuid.UUID
    ):
        read_run_method = getattr(client, f"read_{run_type}_run")
        while True:
            run = await read_run_method(run_id)
            if run.state and run.state.is_final():
                return run_id
            await anyio.sleep(poll_interval)

    async with anyio.move_on_after(timeout), get_client() as client:
        flow_runs_to_wait_for = (
            await client.read_flow_runs(flow_run_filter=flow_run_filter)
            if flow_run_filter
            else []
        )

        if parent_flow_run_id is not None:
            subflow_runs = await client.read_flow_runs(
                flow_run_filter=FlowRunFilter(
                    parent_flow_run_id=dict(any_=[parent_flow_run_id])
                )
            )

            flow_runs_to_wait_for.extend(subflow_runs)

        await asyncio.gather(
            *(wait_for_final_state("flow", run.id) for run in flow_runs_to_wait_for)
        )
