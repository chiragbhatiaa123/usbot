"""Initialize the SQLite workspace index."""

from __future__ import annotations

from . import storage


def main() -> None:
    storage.ensure_db().close()


if __name__ == "__main__":
    main()
