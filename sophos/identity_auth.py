"""Audited authentication of platform identities as the same unified user."""

from __future__ import annotations

from dataclasses import dataclass

import asyncpg


@dataclass(frozen=True, slots=True)
class IdentityConflictError(RuntimeError):
    identity_id: int
    source_user_id: int
    target_user_id: int
    conflicts: tuple[str, ...]

    def __str__(self) -> str:
        return (
            f"identity {self.identity_id} cannot be authenticated as user {self.target_user_id}; "
            f"source user {self.source_user_id} has state: {', '.join(self.conflicts)}"
        )


class IdentityAuthenticationService:
    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    async def authenticate_as_same_user(
        self,
        *,
        identity_id: int,
        target_user_id: int,
        verification_method: str,
        verified_by_user_id: int,
    ) -> None:
        """Attach an otherwise-empty identity user to a verified target user.

        Full profile/permission/policy conflict resolution is deliberately outside
        this contract. A source user with authored state fails atomically.
        """
        method = verification_method.strip()
        if not method:
            raise ValueError("verification_method cannot be empty")
        async with self._pool.acquire() as conn, conn.transaction():
            identity = await conn.fetchrow(
                "SELECT id, user_id FROM user_identities WHERE id = $1 FOR UPDATE",
                identity_id,
            )
            if identity is None:
                raise ValueError(f"unknown identity_id: {identity_id}")
            if not await conn.fetchval("SELECT EXISTS(SELECT 1 FROM users WHERE id = $1)", target_user_id):
                raise ValueError(f"unknown target_user_id: {target_user_id}")
            if not await conn.fetchval("SELECT EXISTS(SELECT 1 FROM users WHERE id = $1)", verified_by_user_id):
                raise ValueError(f"unknown verified_by_user_id: {verified_by_user_id}")

            source_user_id = int(identity["user_id"])
            if source_user_id != target_user_id:
                conflicts = await self._find_conflicts(conn, source_user_id, identity_id)
                if conflicts:
                    raise IdentityConflictError(
                        identity_id=identity_id,
                        source_user_id=source_user_id,
                        target_user_id=target_user_id,
                        conflicts=tuple(conflicts),
                    )
                await conn.execute(
                    """
                    UPDATE user_identities
                    SET user_id = $2, verification_method = $3, verified_at = now(),
                        verified_by_user_id = $4, updated_at = now()
                    WHERE id = $1
                    """,
                    identity_id,
                    target_user_id,
                    method,
                    verified_by_user_id,
                )
                await conn.execute(
                    "DELETE FROM users WHERE id = $1 AND NOT EXISTS "
                    "(SELECT 1 FROM user_identities WHERE user_id = $1)",
                    source_user_id,
                )
            else:
                await conn.execute(
                    """
                    UPDATE user_identities
                    SET verification_method = $2, verified_at = now(),
                        verified_by_user_id = $3, updated_at = now()
                    WHERE id = $1
                    """,
                    identity_id,
                    method,
                    verified_by_user_id,
                )
            await conn.execute(
                """
                INSERT INTO identity_auth_audit
                    (identity_id, source_user_id, target_user_id,
                     verified_by_user_id, verification_method)
                VALUES ($1, $2, $3, $4, $5)
                """,
                identity_id,
                source_user_id,
                target_user_id,
                verified_by_user_id,
                method,
            )

    @staticmethod
    async def _find_conflicts(
        conn: asyncpg.Connection,
        source_user_id: int,
        identity_id: int,
    ) -> list[str]:
        checks = {
            "other_identities": "SELECT EXISTS(SELECT 1 FROM user_identities WHERE user_id = $1 AND id != $2)",
            "roles": "SELECT EXISTS(SELECT 1 FROM user_roles WHERE user_id = $1)",
            "permissions": "SELECT EXISTS(SELECT 1 FROM perm_grant WHERE user_id = $1 OR granted_by = $1)",
            "profile": "SELECT EXISTS(SELECT 1 FROM memory_profile_user WHERE user_id = $1)",
            "trigger_policy": "SELECT EXISTS(SELECT 1 FROM user_trigger_policy WHERE user_id = $1)",
        }
        conflicts: list[str] = []
        for name, query in checks.items():
            args = (source_user_id, identity_id) if name == "other_identities" else (source_user_id,)
            if await conn.fetchval(query, *args):
                conflicts.append(name)
        return conflicts
