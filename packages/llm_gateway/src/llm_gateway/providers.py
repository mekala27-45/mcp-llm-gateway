"""A client for one OpenAI compatible provider."""

from __future__ import annotations

from typing import Any

import httpx

from llm_gateway.settings import ProviderConfig


class Provider:
    def __init__(self, config: ProviderConfig, client: httpx.AsyncClient) -> None:
        self.config = config
        self._client = client

    @property
    def name(self) -> str:
        return self.config.name

    async def open(self, body: dict[str, Any]) -> httpx.Response:
        """Send the request. Returns when the status line and headers arrive, body unread."""
        payload = {**body, "model": self.config.model} if self.config.model else body
        headers = {"content-type": "application/json", "accept-encoding": "identity"}
        if self.config.api_key:
            headers["authorization"] = f"Bearer {self.config.api_key}"
        url = self.config.base_url.rstrip("/") + "/chat/completions"
        request = self._client.build_request("POST", url, json=payload, headers=headers)
        return await self._client.send(request, stream=True)
