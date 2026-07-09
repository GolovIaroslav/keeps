import argparse
import sys

from keeps import __version__


def main() -> int:
    parser = argparse.ArgumentParser(prog="keeps")
    parser.add_argument("--version", action="store_true", help="print version and exit")
    args = parser.parse_args()

    if args.version:
        print(__version__)
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
