from collections.abc import Iterator

import pytest
from gateway_stack import Stack, running_stack


@pytest.fixture(scope="module")
def stack() -> Iterator[Stack]:
    with running_stack() as running:
        yield running
