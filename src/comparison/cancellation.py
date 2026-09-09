import asyncio


async def while_connected(operation, disconnected):
    """Cancel downstream work when an anonymous caller abandons the HTTP request."""
    finished = asyncio.Event()

    async def monitor():
        while not finished.is_set() and not await disconnected():
            await asyncio.sleep(0.1)

    work = asyncio.create_task(operation)
    watcher = asyncio.create_task(monitor())
    try:
        done, _ = await asyncio.wait((work, watcher), return_when=asyncio.FIRST_COMPLETED)
        if watcher in done and not work.done():
            raise asyncio.CancelledError()
        return await work
    finally:
        # Request.is_disconnected() has its own cancellation scope. The explicit
        # flag ensures its watcher exits even if that scope consumes cancellation.
        finished.set()
        for task in (work, watcher):
            if not task.done():
                task.cancel()
        await asyncio.gather(work, watcher, return_exceptions=True)
