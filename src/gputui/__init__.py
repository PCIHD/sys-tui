"""gputui — live PCB-level view of NVIDIA RTX 50-series cards."""

__version__ = "0.1.0"


def main() -> None:
    from .cli import main as _main

    _main()
