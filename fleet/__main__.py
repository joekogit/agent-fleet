import argparse
from .server import serve


def main():
    parser = argparse.ArgumentParser(prog="fleet", description="Agent Fleet dashboard")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="Loopback only. A non-loopback address is refused, not warned "
             "about — the page shows working directories and prompt "
             "fragments and has no authentication.",
    )
    args = parser.parse_args()
    serve(port=args.port, host=args.host)


if __name__ == "__main__":
    main()
