import os
from contextlib import AsyncExitStack

from azure.ai.projects.aio import AIProjectClient
from azure.identity.aio import DefaultAzureCredential, ManagedIdentityCredential

from .config import Settings
from .contracts import MODEL_TIMEOUT


async def open_clients(stack: AsyncExitStack, settings: Settings, *, hosted=False):
    credential = (
        DefaultAzureCredential()
        if os.getenv("COMPARISON_LOCAL_DEVELOPMENT") == "true"
        else ManagedIdentityCredential(client_id=os.getenv("AZURE_CLIENT_ID"))
    )
    await stack.enter_async_context(credential)
    project = await stack.enter_async_context(AIProjectClient(
        endpoint=settings.project_endpoint,
        credential=credential,
        logging_enable=False,
    ))
    if hosted:
        client = project.get_openai_client(timeout=MODEL_TIMEOUT, max_retries=0)
        return await stack.enter_async_context(client)
    result = {}
    for side, name in (("prompt", settings.prompt_agent), ("hosted", settings.hosted_agent)):
        client = project.get_openai_client(
            agent_name=name, timeout=MODEL_TIMEOUT + 10, max_retries=0,
        )
        result[side] = await stack.enter_async_context(client)
    return result
