from pathlib import Path

from llm_gateway.db import Database
from llm_gateway.estimate import MESSAGE_OVERHEAD, REPLY_PRIMING, TokenEstimator
from llm_gateway.tenants import TenantStore

SECRET = "tenant-test-secret-0123456789abcdef"


async def test_keys_are_found_by_their_hmac_and_never_stored(tmp_path: Path) -> None:
    db = await Database.open(str(tmp_path / "tenants.db"))
    try:
        store = TenantStore(db, SECRET)
        key = await store.add("acme")
        stored = await db.fetch_all("SELECT tenant_id, key_hmac FROM tenants")
        assert await store.find(key) == "acme"
        assert await store.find(key + "x") is None
        assert await TenantStore(db, SECRET + "-rotated").find(key) is None
    finally:
        await db.close()
    assert key.startswith("gk_")
    assert stored[0][0] == "acme"
    assert key not in stored[0][1]
    assert key.encode() not in (tmp_path / "tenants.db").read_bytes()


def test_the_default_estimate_is_an_upper_bound_from_utf8_length() -> None:
    estimator = TokenEstimator(use_tiktoken=False)
    messages = [
        {"role": "user", "content": "caf\u00e9 au lait"},
        {"role": "assistant", "content": None},
    ]
    expected = REPLY_PRIMING + 2 * MESSAGE_OVERHEAD + len("caf\u00e9 au lait".encode())
    assert estimator.prompt(messages) == expected
    assert estimator.method == "utf-8 length"


def test_content_parts_are_counted() -> None:
    estimator = TokenEstimator(use_tiktoken=False)
    parts = [
        {
            "role": "user",
            "content": [{"type": "text", "text": "hello"}, {"type": "text", "text": "there"}],
        }
    ]
    assert estimator.prompt(parts) == REPLY_PRIMING + MESSAGE_OVERHEAD + len("hello there")
