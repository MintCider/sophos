"""视觉相关工具。"""

from typing import Any

from sophos.message_store import MessageStore
from sophos.tools.base import Tool


class CorrectImageDescriptionTool(Tool):
    """纠正图片描述（懒加载）。

    将修正信息写入 image_cache，下次该图片再出现时强制 VLM 重新识别。
    """

    @property
    def name(self) -> str:
        return "correct_image_description"

    @property
    def description(self) -> str:
        return (
            "纠正图片描述。从聊天记录中的 [图片(hash): ...] 获取 hash，"
            "提供正确描述后，下次该图片出现时会重新识别"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "image_hash": {
                    "type": "string",
                    "description": "图片哈希（从上下文中 [图片(hash): ...] 获取）",
                },
                "correct_description": {
                    "type": "string",
                    "description": "用户指出的正确描述或修正提示",
                },
            },
            "required": ["image_hash", "correct_description"],
        }

    async def execute(self, params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        store: MessageStore = context["store"]
        image_hash = params["image_hash"]
        correction = params["correct_description"]

        result = await store.pool.execute(
            """
            UPDATE image_cache
            SET correction_hint = $2, pending_correction = true
            WHERE hash = $1
            """,
            image_hash, correction,
        )
        if result == "UPDATE 0":
            return {"error": f"未找到哈希为 {image_hash} 的图片缓存"}
        return {"status": "ok", "message": "已记录纠正，下次该图片出现时将重新识别"}


VISION_TOOLS: list[Tool] = [
    CorrectImageDescriptionTool(),
]
