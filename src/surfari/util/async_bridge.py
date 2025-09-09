import asyncio
import atexit
from concurrent.futures import ThreadPoolExecutor

_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="surfari-sync-bridge")

def run_sync(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No loop in this thread
        return asyncio.run(coro)
    else:
        # Loop is already running -> run in the dedicated bridge thread
        return _EXECUTOR.submit(lambda: asyncio.run(coro)).result()

@atexit.register
def _shutdown_bridge():
    _EXECUTOR.shutdown(wait=False, cancel_futures=True)
