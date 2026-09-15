from __future__ import annotations

from alembic.config import Config

from alembic import command


def main() -> None:
    command.upgrade(Config("alembic.ini"), "head")


if __name__ == "__main__":
    main()
