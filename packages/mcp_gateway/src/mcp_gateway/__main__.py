"""Run the gateway: python -m mcp_gateway --port 8100, configured from the environment."""

import argparse
import os

import uvicorn

from gateway_common.logs import configure
from mcp_gateway.app import create_app
from mcp_gateway.settings import Settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="mcp-gateway")
    parser.add_argument("--port", type=int, default=8100)
    args = parser.parse_args()
    configure(os.environ.get("LOG_LEVEL", "INFO"))
    app = create_app(Settings.from_env())
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_config=None, access_log=False)


if __name__ == "__main__":
    main()
