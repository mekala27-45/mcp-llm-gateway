"""Run the LLM gateway: python -m llm_gateway --port 8200, configured from the environment."""

import argparse
import os
from pathlib import Path

import uvicorn

from gateway_common.logs import configure
from llm_gateway.app import create_app
from llm_gateway.settings import Settings, read_env_file


def main() -> None:
    parser = argparse.ArgumentParser(prog="llm-gateway")
    parser.add_argument("--port", type=int, default=8200)
    args = parser.parse_args()
    env = {**read_env_file(Path(".env")), **os.environ}
    configure(env.get("LOG_LEVEL", "INFO"))
    app = create_app(Settings.from_env(env))
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_config=None, access_log=False)


if __name__ == "__main__":
    main()
