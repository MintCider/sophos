"""Embedding provider — OpenAI 兼容 embedding 客户端。

支持标准 /embeddings 和火山引擎 /embeddings/multimodal 端点。
multimodal 端点的请求格式自动适配（input 从字符串列表变为嵌套对象列表）。
"""

import json
import logging
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)


class EmbeddingProvider:
    """轻量级 embedding API 客户端。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        endpoint: str = "/embeddings",
        extra_body: dict[str, Any] | None = None,
        request_timeout: int = 30,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._endpoint = endpoint
        self._extra_body = extra_body or {}
        self._timeout = aiohttp.ClientTimeout(total=request_timeout)
        self._session: aiohttp.ClientSession | None = None

    @property
    def model(self) -> str:
        return self._model

    @property
    def endpoint(self) -> str:
        return self._endpoint

    @property
    def multimodal(self) -> bool:
        return "multimodal" in self._endpoint

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
            )
        return self._session

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """批量嵌入文本，返回向量列表。"""
        if not texts:
            return []
        if self.multimodal:
            return await self._embed_multimodal(texts)
        payload: dict[str, Any] = {"model": self._model, "input": texts}
        if self._extra_body:
            payload.update(self._extra_body)
        session = self._get_session()
        url = f"{self._base_url}{self._endpoint}"
        async with session.post(url, json=payload) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"Embedding API error ({resp.status}): {body[:300]}")
            data = await resp.json()
        # 按 index 排序（API 不保证顺序）
        items = sorted(data["data"], key=lambda x: x["index"])
        return [item["embedding"] for item in items]

    async def _embed_multimodal(self, texts: list[str]) -> list[list[float]]:
        """火山引擎 multimodal 端点：逐条请求，响应格式 data.embedding。"""
        session = self._get_session()
        url = f"{self._base_url}{self._endpoint}"
        results: list[list[float]] = []
        for t in texts:
            payload: dict[str, Any] = {
                "model": self._model,
                "input": [{"type": "text", "text": t}],
            }
            if self._extra_body:
                payload.update(self._extra_body)
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(f"Embedding API error ({resp.status}): {body[:300]}")
                data = await resp.json()
            # multimodal 响应：data 是对象 {"embedding": [...]} 而非数组
            raw = data["data"]
            if isinstance(raw, list):
                results.append(raw[0]["embedding"])
            else:
                results.append(raw["embedding"])
        return results

    async def embed_single(self, text: str) -> list[float]:
        """嵌入单条文本。"""
        vecs = await self.embed([text])
        return vecs[0]

    async def detect_dimension(self) -> int:
        """发一次测试请求获取模型输出维度。"""
        vec = await self.embed_single("dimension detection")
        dim = len(vec)
        logger.info("Detected embedding dimension: %d (model=%s)", dim, self._model)
        return dim

    async def close(self) -> None:
        """关闭 HTTP session。"""
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None
