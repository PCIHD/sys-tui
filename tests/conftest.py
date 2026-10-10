import pytest

from gputui.telemetry import DemoSource


@pytest.fixture
def demo_snap():
    src = DemoSource("5080")
    return src.collect(src.t0 + 30)
