"""python -m window_agent — interactive agent entry point."""

from window_agent.cli import main
from window_agent.client import _c

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(_c("\n\n  Interrupted.\n", "dim"))
