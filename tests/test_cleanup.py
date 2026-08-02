import unittest
from unittest.mock import AsyncMock

from sophos.cleanup import _cleanup_table, _logical_table_stats, _require_positive_int


class CleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_logical_size_counts_live_rows_instead_of_relation_size(self) -> None:
        pool = AsyncMock()
        pool.fetchrow.return_value = {"logical_bytes": 1234, "row_count": 5}

        stats = await _logical_table_stats(pool, "messages")

        self.assertEqual(stats.logical_bytes, 1234)
        self.assertEqual(stats.row_count, 5)
        query = pool.fetchrow.await_args.args[0]
        self.assertIn("SUM(pg_column_size(row_data)::bigint)", query)
        self.assertIn("COUNT(*) AS row_count", query)
        self.assertNotIn("pg_total_relation_size", query)

    async def test_cleanup_deletes_lru_rows_until_low_watermark(self) -> None:
        pool = AsyncMock()
        pool.fetchrow.return_value = {"logical_bytes": 1100, "row_count": 20}
        pool.fetch.side_effect = [
            [{"logical_bytes": 100}],
            [{"logical_bytes": 100}],
        ]

        await _cleanup_table(pool, "messages", 1000, keep_per_conversation=100)

        self.assertEqual(pool.fetch.await_count, 2)
        query = pool.fetch.await_args_list[0].args[0]
        self.assertIn("conversation_cutoffs AS MATERIALIZED", query)
        self.assertIn("ORDER BY m.id DESC", query)
        self.assertIn("OFFSET $2", query)
        self.assertIn("m.id <= cutoff.max_deletable_id", query)
        self.assertIn("enrichment_status NOT IN ('pending', 'streaming')", query)
        self.assertIn("ORDER BY COALESCE(last_accessed_at, occurred_at) ASC, m.id ASC", query)
        self.assertIn("RETURNING pg_column_size(m)", query)
        self.assertEqual(pool.fetch.await_args_list[0].args[1], 1)
        self.assertEqual(pool.fetch.await_args_list[0].args[2], 100)

    async def test_cleanup_does_nothing_below_high_watermark(self) -> None:
        pool = AsyncMock()
        pool.fetchrow.return_value = {"logical_bytes": 1000, "row_count": 20}

        await _cleanup_table(pool, "memories", 1000)

        pool.fetch.assert_not_awaited()

    async def test_invalid_limit_is_rejected_before_querying(self) -> None:
        pool = AsyncMock()

        for value in (0, -1, 1.5, True, "100"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                await _cleanup_table(
                    pool,
                    "messages",
                    value,  # type: ignore[arg-type]
                    keep_per_conversation=100,
                )

        pool.fetchrow.assert_not_awaited()

    async def test_message_cleanup_requires_positive_context_floor(self) -> None:
        pool = AsyncMock()

        for value in (None, 0, -1, True, 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                await _cleanup_table(
                    pool,
                    "messages",
                    1000,
                    keep_per_conversation=value,  # type: ignore[arg-type]
                )

        pool.fetchrow.assert_not_awaited()

    async def test_protected_messages_win_over_capacity_limit(self) -> None:
        pool = AsyncMock()
        pool.fetchrow.side_effect = [
            {"logical_bytes": 1100, "row_count": 20},
            {"logical_bytes": 1050, "row_count": 10},
        ]
        pool.fetch.return_value = []

        await _cleanup_table(pool, "messages", 1000, keep_per_conversation=10)

        self.assertEqual(pool.fetch.await_count, 1)
        self.assertEqual(pool.fetchrow.await_count, 2)
        protected_query = pool.fetchrow.await_args_list[1].args[0]
        self.assertIn("CROSS JOIN LATERAL", protected_query)
        self.assertIn("LIMIT $1", protected_query)


class CleanupConfigTests(unittest.TestCase):
    def test_positive_integer_validation(self) -> None:
        self.assertEqual(_require_positive_int(1, "value"), 1)
        for value in (0, -1, 1.5, True, "1", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _require_positive_int(value, "value")


if __name__ == "__main__":
    unittest.main()
