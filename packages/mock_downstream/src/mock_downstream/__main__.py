"""Run the mock MCP server: python -m mock_downstream --port 8101."""

import argparse
import os

import uvicorn

from gateway_common.logs import configure
from mock_downstream.app import CallLog, build_app


def main() -> None:
    parser = argparse.ArgumentParser(prog="mock-downstream")
    parser.add_argument("--port", type=int, default=8101)
    args = parser.parse_args()
    configure(os.environ.get("LOG_LEVEL", "INFO"))
    app = build_app(CallLog(), os.environ.get("GATEWAY_DOWNSTREAM_TOKEN"))
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_config=None, access_log=False)


if __name__ == "__main__":
    main()
