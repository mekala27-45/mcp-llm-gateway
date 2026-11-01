"""mcp-gateway-token: mint a short lived token for local testing, signed with GATEWAY_JWT_SECRET."""

import argparse

from mcp_gateway.auth import ROLES, mint_token
from mcp_gateway.settings import Settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="mcp-gateway-token", description=__doc__)
    parser.add_argument("--role", choices=ROLES, required=True)
    parser.add_argument("--subject", default="local-user")
    parser.add_argument("--ttl", type=int, default=3600, help="lifetime in seconds")
    args = parser.parse_args()
    print(mint_token(Settings.from_env(), args.role, args.subject, args.ttl))


if __name__ == "__main__":
    main()
