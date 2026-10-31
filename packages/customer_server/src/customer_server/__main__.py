"""Entry point for the customer server. The protocol descriptors are claimed first."""

import argparse
import sys

from customer_server.guard import claim_stdio


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
        from customer_server.selftest import run_self_test

        sys.exit(run_self_test())

    wire = claim_stdio()
    # Imported only now, so nothing the SDK or its dependencies print while
    # loading can reach the protocol stream.
    from customer_server.server import serve

    serve(wire)


if __name__ == "__main__":
    main()
