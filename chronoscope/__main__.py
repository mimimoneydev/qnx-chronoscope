"""Allow ``python3 -m chronoscope ...``."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
