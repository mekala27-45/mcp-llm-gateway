"""Entry point for the customer server."""

import argparse
import sys

from customer_server.selftest import run_self_test
from customer_server.server import serve


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="customer-server", description="Customer lookups and refunds over MCP on stdio."
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="run a scripted session against a fresh server process and report on stderr",
    )
    args = parser.parse_args()
    if args.self_test:
        sys.exit(run_self_test())
    serve()


if __name__ == "__main__":
    main()
