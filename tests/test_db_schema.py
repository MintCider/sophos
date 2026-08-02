import re
import unittest

from sophos.db import _CREATE_INDEXES, _CREATE_MESSAGES_TABLE


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
                "message_id",
                "raw_message",
                "plain_text",
                "enrichment_status",
                "enrichment_error",
                "timestamp",
                "last_accessed",
                "extra",
            }.issubset(columns)
        )

    def test_cleanup_lru_indexes_are_declared(self) -> None:
        indexes = "\n".join(_CREATE_INDEXES)
        self.assertIn("idx_messages_lru", indexes)
        self.assertIn("COALESCE(last_accessed, timestamp), id", indexes)
        self.assertIn("idx_memories_lru", indexes)
        self.assertIn("COALESCE(last_hit, created_at), id", indexes)


if __name__ == "__main__":
    unittest.main()
