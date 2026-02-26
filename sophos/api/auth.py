"""WebUI session 管理。

内存存储，服务器重启后所有 session 失效（用户重新输入密码即可）。
"""

import secrets
import time

SESSION_MAX_AGE = 7 * 24 * 3600  # 7 天

_sessions: dict[str, float] = {}  # token → expiry_timestamp


def create_session() -> str:
    """创建新 session，返回 token。"""
    token = secrets.token_urlsafe(32)
    _sessions[token] = time.time() + SESSION_MAX_AGE
    return token


def validate_session(token: str) -> bool:
    """验证 token 是否有效且未过期。"""
    expiry = _sessions.get(token)
    if not expiry or time.time() > expiry:
        _sessions.pop(token, None)
        return False
    return True


def revoke_session(token: str) -> None:
    """撤销单个 session。"""
    _sessions.pop(token, None)


def revoke_all() -> None:
    """撤销所有 session（密码修改时调用）。"""
    _sessions.clear()
