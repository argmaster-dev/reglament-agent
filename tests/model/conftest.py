import asyncio
import inspect

import pytest


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem):
    """Run `async def` tests with asyncio.run (no pytest-asyncio dependency)."""
    fn = pyfuncitem.obj
    if inspect.iscoroutinefunction(fn):
        args = {a: pyfuncitem.funcargs[a] for a in pyfuncitem._fixtureinfo.argnames}
        asyncio.run(asyncio.wait_for(fn(**args), 5))
        return True
