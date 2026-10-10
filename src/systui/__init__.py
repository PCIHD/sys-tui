"""systui — live motherboard-level view of the whole system."""

__version__ = "0.1.0"


def main() -> None:
    from .cli import main as _main

    _main()
