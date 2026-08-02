"""记忆工具 — 档案设置 + 记忆读写删。"""

from contextlib import suppress
from typing import Any

from sophos.tools.base import Tool


class SetProfileSelfTool(Tool):
    """设置自我档案（整体替换）。"""

    @property
    def category(self) -> str:
        return "output"

    @property
    def group(self) -> str:
        return "memory"

    @property
    def name(self) -> str:
        return "set_profile_self"

    @property
    def description(self) -> str:
        return (
            "设置你的自我档案（整体替换）。"
            "这是你对自身的动态认知，会始终出现在你的提示中。"
            "用于记录你在互动中形成的自我认知变化。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "自我档案内容"},
            },
            "required": ["content"],
        }

    async def execute(self, params: dict[str, Any], context: dict[str, Any]) -> Any:
        store = context["memory_store"]
        return await store.set_profile_self(params["content"])


class SetProfileContextTool(Tool):
    """设置当前会话的档案（整体替换）。"""

    @property
    def category(self) -> str:
        return "output"

    @property
    def group(self) -> str:
        return "memory"

    @property
    def name(self) -> str:
        return "set_profile_context"

    @property
    def description(self) -> str:
        return "设置当前会话的档案（整体替换）。档案是你对这个群/私聊的核心认知，会始终出现在你的提示中。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "档案内容"},
            },
            "required": ["content"],
        }

    async def execute(self, params: dict[str, Any], context: dict[str, Any]) -> Any:
        store = context["memory_store"]
        return await store.set_profile_context("conversation", context["conversation_id"], params["content"])


class SetProfileUserTool(Tool):
    """设置指定用户的档案（整体替换）。"""

    @property
    def category(self) -> str:
        return "output"

    @property
    def group(self) -> str:
        return "memory"

    @property
    def name(self) -> str:
        return "set_profile_user"

    @property
    def description(self) -> str:
        return (
            "设置指定用户的档案（整体替换）。"
            "档案是你对这个人的核心认知，当此人出现在对话中或被提及时会自动注入。"
            "可设置额外关键词，当关键词出现时也会触发注入。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "user_id": {"type": "integer", "description": "Sophos 内部用户 ID"},
                "content": {"type": "string", "description": "档案内容"},
                "keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "额外触发关键词（如昵称、别名）",
                },
            },
            "required": ["user_id", "content"],
        }

    async def execute(self, params: dict[str, Any], context: dict[str, Any]) -> Any:
        store = context["memory_store"]
        return await store.set_profile_user(
            params["user_id"],
            params["content"],
            params.get("keywords"),
        )


class WriteMemoryTool(Tool):
    """写入一条长期记忆。"""

    @property
    def category(self) -> str:
        return "output"

    @property
    def group(self) -> str:
        return "memory"

    @property
    def name(self) -> str:
        return "write_memory"

    @property
    def description(self) -> str:
        return "将一条信息写入长期记忆。自动去重，与已有记忆相似度>0.9时合并而非新增。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "要记住的内容"},
            },
            "required": ["content"],
        }

    async def execute(self, params: dict[str, Any], context: dict[str, Any]) -> Any:
        store = context["memory_store"]
        return await store.write_memory(params["content"], "conversation", context["conversation_id"])


class SearchMemoryTool(Tool):
    """搜索长期记忆。"""

    @property
    def group(self) -> str:
        return "memory"

    @property
    def name(self) -> str:
        return "search_memory"

    @property
    def description(self) -> str:
        return "搜索记忆库（关键词 + 语义混合搜索），返回最相关的记忆及其 ID。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词或描述"},
            },
            "required": ["query"],
        }

    async def execute(self, params: dict[str, Any], context: dict[str, Any]) -> Any:
        store = context["memory_store"]
        embed = context.get("embedding_provider")
        query_vec = None
        if embed is not None:
            with suppress(Exception):
                query_vec = await embed.embed_single(params["query"])
        results = await store.search_hybrid(params["query"], query_vec, limit=10)
        # 格式化返回
        formatted = []
        for m in results:
            ts = m["created_at"].strftime("%Y-%m-%d %H:%M") if m.get("created_at") else "?"
            formatted.append({"id": m["id"], "time": ts, "content": m["content"]})
        return formatted


class DeleteMemoryTool(Tool):
    """删除一条记忆。"""

    @property
    def category(self) -> str:
        return "output"

    @property
    def group(self) -> str:
        return "memory"

    @property
    def name(self) -> str:
        return "delete_memory"

    @property
    def description(self) -> str:
        return "按 ID 删除一条记忆（从 search_memory 结果中获取 ID）。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "memory_id": {"type": "integer", "description": "要删除的记忆 ID"},
            },
            "required": ["memory_id"],
        }

    async def execute(self, params: dict[str, Any], context: dict[str, Any]) -> Any:
        store = context["memory_store"]
        return await store.delete_memory(int(params["memory_id"]))


MEMORY_TOOLS: list[Tool] = [
    SetProfileSelfTool(),
    SetProfileContextTool(),
    SetProfileUserTool(),
    WriteMemoryTool(),
    SearchMemoryTool(),
    DeleteMemoryTool(),
]
