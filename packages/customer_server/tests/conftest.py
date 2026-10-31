from collections.abc import Iterator

import pytest
from stdio_session import StdioSession, server_env


@pytest.fixture(scope="module")
def protocol_session() -> Iterator[StdioSession]:
    session = StdioSession(server_env())
    session.start()
    yield session
    session.close()


@pytest.fixture(scope="module")
def tool_session() -> Iterator[StdioSession]:
    session = StdioSession(server_env(VALIDATION_ERRORS="tool"))
    session.start()
    yield session
    session.close()
