"""Persistence helpers for platform-neutral accounts, identities, and conversations."""

from __future__ import annotations

import json
from typing import Any

import asyncpg

from sophos.platform import ConversationKind, ConversationRef, IdentityRef


class PlatformStore:
    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    async def resolve_identity(
        self,
        *,
        platform: str,
        identity_namespace: str,
        external_user_id: str,
        display_name: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> IdentityRef:
        """Resolve an observed platform identity, creating an independent user if needed."""
        external_user_id = str(external_user_id)
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT i.id AS identity_id, i.user_id, i.platform, i.identity_namespace,
                       i.external_user_id, COALESCE(NULLIF(i.display_name, ''), u.display_name) AS display_name
                FROM user_identities i
                JOIN users u ON u.id = i.user_id
                WHERE i.platform = $1 AND i.identity_namespace = $2 AND i.external_user_id = $3
                """,
                platform,
                identity_namespace,
                external_user_id,
            )
            if row:
                if display_name and display_name != row["display_name"]:
                    await conn.execute(
                        """
                        UPDATE user_identities SET display_name = $2, metadata = metadata || $3::jsonb,
                                                   updated_at = now()
                        WHERE id = $1
                        """,
                        row["identity_id"],
                        display_name,
                        json.dumps(metadata or {}, ensure_ascii=False),
                    )
                return IdentityRef(**dict(row))

            # Serialize first observation of the same external identity without a global lock.
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                f"identity:{platform}:{identity_namespace}:{external_user_id}",
            )
            row = await conn.fetchrow(
                """
                SELECT i.id AS identity_id, i.user_id, i.platform, i.identity_namespace,
                       i.external_user_id, COALESCE(NULLIF(i.display_name, ''), u.display_name) AS display_name
                FROM user_identities i
                JOIN users u ON u.id = i.user_id
                WHERE i.platform = $1 AND i.identity_namespace = $2 AND i.external_user_id = $3
                """,
                platform,
                identity_namespace,
                external_user_id,
            )
            if row:
                return IdentityRef(**dict(row))

            user_id = await conn.fetchval(
                "INSERT INTO users (display_name) VALUES ($1) RETURNING id",
                display_name,
            )
            identity_id = await conn.fetchval(
                """
                INSERT INTO user_identities
                    (user_id, platform, identity_namespace, external_user_id, display_name, metadata)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb)
                RETURNING id
                """,
                user_id,
                platform,
                identity_namespace,
                external_user_id,
                display_name,
                json.dumps(metadata or {}, ensure_ascii=False),
            )
            return IdentityRef(
                identity_id=identity_id,
                user_id=user_id,
                platform=platform,
                identity_namespace=identity_namespace,
                external_user_id=external_user_id,
                display_name=display_name,
            )

    async def ensure_account_binding(
        self,
        *,
        platform: str,
        identity_namespace: str,
        external_account_id: str,
        adapter_kind: str,
        binding_name: str,
        external_id_namespace: str,
        display_name: str = "",
    ) -> tuple[int, int]:
        async with self._pool.acquire() as conn, conn.transaction():
            account_id = await conn.fetchval(
                """
                INSERT INTO platform_accounts
                    (platform, identity_namespace, external_account_id, display_name)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (platform, identity_namespace, external_account_id)
                DO UPDATE SET display_name = CASE
                    WHEN EXCLUDED.display_name = '' THEN platform_accounts.display_name
                    ELSE EXCLUDED.display_name
                END, updated_at = now()
                RETURNING id
                """,
                platform,
                identity_namespace,
                str(external_account_id),
                display_name,
            )
            binding_id = await conn.fetchval(
                """
                INSERT INTO adapter_bindings
                    (account_id, adapter_kind, name, external_id_namespace)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (account_id, name)
                DO UPDATE SET adapter_kind = EXCLUDED.adapter_kind,
                              external_id_namespace = EXCLUDED.external_id_namespace,
                              enabled = TRUE,
                              updated_at = now()
                RETURNING id
                """,
                account_id,
                adapter_kind,
                binding_name,
                external_id_namespace,
            )
            return account_id, binding_id

    async def resolve_conversation(
        self,
        *,
        account_id: int,
        kind: ConversationKind,
        external_conversation_id: str,
        parent_conversation_id: int | None = None,
        display_name: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> ConversationRef:
        row = await self._pool.fetchrow(
            """
            INSERT INTO conversations
                (account_id, kind, external_conversation_id, parent_conversation_id, display_name, metadata)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb)
            ON CONFLICT (account_id, kind, parent_conversation_id, external_conversation_id)
            DO UPDATE SET display_name = CASE
                              WHEN EXCLUDED.display_name = '' THEN conversations.display_name
                              ELSE EXCLUDED.display_name
                          END,
                          metadata = conversations.metadata || EXCLUDED.metadata,
                          updated_at = now()
            RETURNING id AS conversation_id, account_id, kind, external_conversation_id,
                      parent_conversation_id, display_name
            """,
            account_id,
            kind.value,
            str(external_conversation_id),
            parent_conversation_id,
            display_name,
            json.dumps(metadata or {}, ensure_ascii=False),
        )
        return self._conversation_ref(row)

    async def get_conversation(self, conversation_id: int) -> ConversationRef | None:
        row = await self._pool.fetchrow(
            """
            SELECT id AS conversation_id, account_id, kind, external_conversation_id,
                   parent_conversation_id, display_name
            FROM conversations WHERE id = $1
            """,
            conversation_id,
        )
        return self._conversation_ref(row) if row else None

    async def get_identity_for_account(self, *, user_id: int, account_id: int) -> IdentityRef | None:
        row = await self._pool.fetchrow(
            """
            SELECT i.id AS identity_id, i.user_id, i.platform, i.identity_namespace,
                   i.external_user_id, COALESCE(NULLIF(i.display_name, ''), u.display_name) AS display_name
            FROM user_identities i
            JOIN users u ON u.id = i.user_id
            JOIN platform_accounts a
              ON a.id = $2 AND a.platform = i.platform
             AND a.identity_namespace = i.identity_namespace
            WHERE i.user_id = $1
            ORDER BY i.id
            LIMIT 1
            """,
            user_id,
            account_id,
        )
        return IdentityRef(**dict(row)) if row else None

    @staticmethod
    def _conversation_ref(row: asyncpg.Record) -> ConversationRef:
        values = dict(row)
        values["kind"] = ConversationKind(values["kind"])
        return ConversationRef(**values)
