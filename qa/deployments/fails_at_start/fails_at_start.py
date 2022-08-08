from prefect import flow, get_run_logger, task
from prefect.cli._dev_utilities import assert_state_type_failed
from prefect.client import get_client
from prefect.context import get_run_context

purpose = """
The purpose of this flow is to see how Prefect handles a flow
with multiple tasks if the first task fails.

Expected behavior: TODO
"""


@task
def parent_task():
    raise Exception("Parent task intentionally failed for testing purposes")


@task
def first_child(from_parent):
    return 256


@task
def second_child(from_parent):
    return 42


@task
def grand_child(from_child):
    return 512


@flow
def fails_at_start_body():
    logger = get_run_logger()
    logger.info(purpose)

    parent_res = parent_task.submit()
    c1_res = first_child.submit(parent_res)
    c2_res = second_child.submit(parent_res)
    grand_child.submit(c1_res)

    ctx = get_run_context()
    return ctx


@flow
async def fails_at_start():
    run_id = fails_at_start_body()
    async with get_client() as client:
        flow_run = await client.read_flow_run(flow_run_id=run_id)

    await assert_state_type_failed(flow_run.state_type)


if __name__ == "__main__":
    fails_at_start()
