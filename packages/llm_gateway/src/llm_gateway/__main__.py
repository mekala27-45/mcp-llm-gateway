"""Run the LLM gateway, or add a tenant to it. Settings come from the environment and .env.

llm-gateway serve --port 8200
llm-gateway add-tenant acme        prints the new API key, once
"""

import argparse
import asyncio
import os
import re
import sqlite3
from pathlib import Path

import uvicorn

from gateway_common.logs import configure
from llm_gateway.app import create_app
from llm_gateway.settings import Settings, read_env_file
from llm_gateway.tenants import add_tenant


def tenant_name(value: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", value):
        raise argparse.ArgumentTypeError("use lower case letters, digits, - and _ (at most 64)")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(prog="llm-gateway")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="run the gateway")
    serve.add_argument("--port", type=int, default=8200)
    add = commands.add_parser("add-tenant", help="create a tenant and print its API key once")
    add.add_argument("tenant_id", type=tenant_name)
    args = parser.parse_args()

    env = {**read_env_file(Path(".env")), **os.environ}
    configure(env.get("LOG_LEVEL", "INFO"))
    settings = Settings.from_env(env)
    if args.command == "add-tenant":
        try:
            key = asyncio.run(add_tenant(settings.db_path, settings.secret, args.tenant_id))
        except sqlite3.IntegrityError:
            parser.exit(1, f"tenant {args.tenant_id} already exists\n")
        print(key)
        return
    app = create_app(settings)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_config=None, access_log=False)


if __name__ == "__main__":
    main()
