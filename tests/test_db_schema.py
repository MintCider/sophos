import re
import unittest

from sophos.db import (
    _CREATE_CONVERSATIONS_TABLE,
    _CREATE_IDENTITY_AUTH_AUDIT_TABLE,
    _CREATE_INDEXES,
    _CREATE_MESSAGES_TABLE,
    _CREATE_USER_IDENTITIES_TABLE,
    SCHEMA_VERSION,
)


def _declared_columns(create_table_sql: str) -> set[str]:
    """提取简单 CREATE TABLE DDL 中声明的列名。"""
    columns: set[str] = set()
    for raw_line in create_table_sql.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("--", "CREATE ", ");", "CHECK ")):
            continue
        match = re.match(r"([a-z_][a-z0-9_]*)\s+", line)
        if match:
            columns.add(match.group(1))
    return columns


class DatabaseSchemaTests(unittest.TestCase):
    def test_messages_schema_contains_runtime_managed_columns(self) -> None:
        columns = _declared_columns(_CREATE_MESSAGES_TABLE)
        self.assertTrue(
            {
                "adapter_binding_id",
                "conversation_id",
                "sender_identity_id",
                "external_message_id",
                "reply_to_message_id",
                "content",
                "raw_payload",
                "plain_text",
                "enrichment_status",
                "enrichment_error",
                "occurred_at",
                "last_accessed_at",
                "metadata",
            }.issubset(columns)
        )

    def test_schema_has_platform_neutral_identity_and_conversation_keys(self) -> None:
        self.assertEqual(SCHEMA_VERSION, 2)
        self.assertIn("platform, identity_namespace, external_user_id", _CREATE_USER_IDENTITIES_TABLE)
        self.assertIn("parent_conversation_id", _CREATE_CONVERSATIONS_TABLE)
        self.assertIn("'direct', 'group', 'channel', 'thread'", _CREATE_CONVERSATIONS_TABLE)
        self.assertIn("verification_method", _CREATE_IDENTITY_AUTH_AUDIT_TABLE)

    def test_message_query_and_cleanup_indexes_are_declared(self) -> None:
        indexes = "\n".join(_CREATE_INDEXES)
        self.assertIn("uidx_messages_external_locator", indexes)
        self.assertIn("adapter_binding_id, conversation_id, external_message_id", indexes)
        self.assertIn("idx_messages_conversation_id", indexes)
        self.assertIn("conversation_id, id DESC", indexes)
        self.assertIn("idx_messages_conversation_time", indexes)
        self.assertIn("idx_messages_pending", indexes)
        self.assertIn("WHERE enrichment_status IN ('pending', 'streaming')", indexes)
        self.assertIn("idx_messages_lru", indexes)
        self.assertIn("COALESCE(last_accessed_at, occurred_at), id", indexes)
        self.assertIn("idx_memories_lru", indexes)
        self.assertIn("COALESCE(last_hit, created_at), id", indexes)


if __name__ == "__main__":
    unittest.main()
