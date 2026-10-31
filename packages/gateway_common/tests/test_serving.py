import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from gateway_common.serving import background_server


async def hello(request: Request) -> PlainTextResponse:
    return PlainTextResponse("hello")


def test_serves_on_a_free_local_port_and_stops_on_exit() -> None:
    app = Starlette(routes=[Route("/", hello)])
    with background_server(app) as base_url:
        assert base_url.startswith("http://127.0.0.1:")
        assert httpx.get(base_url).text == "hello"
    try:
        httpx.get(base_url, timeout=1)
    except httpx.ConnectError:
        return
    raise AssertionError("the server was still answering after the block exited")
