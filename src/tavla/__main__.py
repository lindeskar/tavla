"""`tavla` opens the board; `tavla <command> ...` runs a core CLI command instead."""

import sys


def main() -> int:
    if len(sys.argv) > 1:
        from .board import main as board_main

        return board_main(sys.argv[1:])
    from .tui import run

    run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
