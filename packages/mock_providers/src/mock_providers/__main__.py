"""Run one mock provider: python -m mock_providers --port 8201 --first-byte-delay-ms 3500."""

import argparse

import uvicorn

from mock_providers.app import Behavior, MockProvider


def main() -> None:
    parser = argparse.ArgumentParser(prog="mock-provider")
    parser.add_argument("--port", type=int, default=8201)
    parser.add_argument("--name", default="mock")
    parser.add_argument("--first-byte-delay-ms", type=float, default=0)
    parser.add_argument("--token-interval-ms", type=float, default=0)
    parser.add_argument("--status", type=int, default=200)
    parser.add_argument("--repeat", type=int, default=1, help="stream the text this many times")
    parser.add_argument("--text", help="text to stream instead of the default, which has PII in it")
    args = parser.parse_args()
    behavior = Behavior(
        status=args.status,
        first_byte_delay=args.first_byte_delay_ms / 1000,
        token_interval=args.token_interval_ms / 1000,
        repeat=args.repeat,
    )
    if args.text is not None:
        behavior.text = args.text
    app = MockProvider(args.name, behavior).app()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_config=None, access_log=False)


if __name__ == "__main__":
    main()
