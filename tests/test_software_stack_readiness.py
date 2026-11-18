import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import httpx
import pytest
from tools.run_software_stack import wait_for_web


def test_readiness_retries_only_read_only_get_on_transient_transport():
    http = SimpleNamespace(
        get=AsyncMock(
            side_effect=[
                httpx.ConnectError("not listening"),
                httpx.ReadError("reset"),
                httpx.ReadTimeout("starting"),
                SimpleNamespace(status_code=503),
                SimpleNamespace(status_code=200),
            ]
        )
    )
    asyncio.run(wait_for_web(http, Mock(poll=Mock(return_value=None)), interval=0))
    assert http.get.await_count == 5
    assert all(c.args == ("/",) for c in http.get.await_args_list)


def test_readiness_has_deadline_for_persistent_failure():
    http = SimpleNamespace(get=AsyncMock(side_effect=httpx.ReadError("reset")))
    with pytest.raises(TimeoutError):
        asyncio.run(
            wait_for_web(
                http, Mock(poll=Mock(return_value=None)), timeout=0.02, interval=0.005
            )
        )


def test_readiness_exited_child_not_retried():
    http = SimpleNamespace(get=AsyncMock())
    with pytest.raises(RuntimeError, match="process exited"):
        asyncio.run(wait_for_web(http, Mock(poll=Mock(return_value=1))))
    http.get.assert_not_called()


def test_readiness_does_not_mask_programming_error():
    http = SimpleNamespace(get=AsyncMock(side_effect=ValueError("wrong request")))
    with pytest.raises(ValueError):
        asyncio.run(wait_for_web(http, Mock(poll=Mock(return_value=None))))
