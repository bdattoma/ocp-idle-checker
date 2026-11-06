"""`python -m ocp_idle_check` entry point."""

import sys
import traceback


def main() -> int:
    # Everything that can fail, imports included, runs inside the try: an
    # uncaught exception or Ctrl-C exits 1 by default, which the exit-code
    # contract reads as IDLE.  Errors must exit 2.
    try:
        from .main import run

        return run(sys.argv[1:])
    except KeyboardInterrupt:
        return 2
    except Exception as exc:
        # Plain print, not the package logger: the failure may be the import.
        print(f"[ERROR] unexpected failure: {type(exc).__name__}: {exc}", file=sys.stderr)
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    sys.exit(main())
