import asyncio
import os
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import asyncpg
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from sophos import observability
from sophos.adapter_router import AdapterRouter
from sophos.api.middleware import auth_middleware
from sophos.api.routes.stats import parse_window, routes
from sophos.config import settings
from sophos.db import _init_schema
from sophos.migrate_resource_usage import migrate as migrate_resources
from sophos.migrate_stats import migrate
from sophos.trigger_engine import TriggerEngine


class StatsWindowTests(unittest.TestCase):
    def test_local_day_and_inclusive_calendar_ranges(self):
        with patch.object(settings, "timezone", "Asia/Shanghai"):
            now = datetime(2026, 10, 16, 17, 30, tzinfo=UTC)
            start, end, cid, _ = parse_window({"range": "week", "conversation_id": "42"}, now=now)
            self.assertEqual(start.isoformat(), "2026-10-11T00:00:00+08:00")
            self.assertEqual(end.day, 17)
            self.assertEqual(cid, 42)
            today, *_ = parse_window({}, now=now)
            self.assertEqual(today.day, 17)

    def test_invalid_parameters(self):
        for query in ({"range": "forever"}, {"conversation_id": "0"}, {"conversation_id": "abc"},
                      {"conversation_id": "9223372036854775808"}):
            with self.subTest(query=query), self.assertRaises(web.HTTPBadRequest):
                parse_window(query)

    def test_daylight_saving_uses_local_midnight(self):
        with patch.object(settings, "timezone", "America/New_York"):
            start, end, *_ = parse_window({"range": "week"}, now=datetime(2026, 11, 2, 12, tzinfo=UTC))
            self.assertEqual(start.isoformat(), "2026-10-27T00:00:00-04:00")
            self.assertEqual(end.utcoffset().total_seconds(), -18000)


class UsageTests(unittest.IsolatedAsyncioTestCase):
    def test_cache_and_reasoning_are_not_double_counted(self):
        normalize = observability.normalize_usage
        openai = normalize({"prompt_tokens": 100, "completion_tokens": 20, "cached_input_tokens": 70,
                            "raw": {"completion_tokens_details": {"reasoning_tokens": 8}}}, "openai_compat")
        self.assertEqual(openai["total_tokens"], 120)
        self.assertEqual(openai["reasoning_tokens"], 8)
        anthropic = normalize({"prompt_tokens": 10, "completion_tokens": 20,
                               "cached_input_tokens": 70, "cache_write_tokens": 30}, "anthropic")
        self.assertEqual(anthropic["input_tokens"], 110)
        self.assertEqual(anthropic["total_tokens"], 130)
        gemini = normalize({"prompt_tokens": 100, "completion_tokens": 20,
                            "raw": {"thoughtsTokenCount": 8}}, "gemini")
        self.assertEqual(gemini["total_tokens"], 128)
        self.assertTrue(all(v is None for v in normalize(None, "openai_compat").values()))

    async def test_retries_are_separate_calls_and_context_is_preserved(self):
        pool = SimpleNamespace(execute=AsyncMock())
        provider = SimpleNamespace(chat=AsyncMock(side_effect=[TimeoutError(), {"usage": None}]),
                                   usage_alias="test", _model="model")
        with patch.object(observability, "_pool", pool), observability.scope(conversation_id=42, run_id="run"):
            with self.assertRaises(TimeoutError):
                await observability.chat(provider, [], slot="actor")
            await observability.chat(provider, [], slot="actor")
        calls = [c.args for c in pool.execute.await_args_list if "INSERT INTO llm_usage" in c.args[0]]
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1:6], (42, "run", "actor", "test", "model"))
        self.assertFalse(calls[0][-2])
        self.assertTrue(calls[1][-2])
        self.assertIsNone(calls[1][6])

    async def test_telemetry_failure_does_not_fail_a_successful_call(self):
        pool = SimpleNamespace(execute=AsyncMock(side_effect=RuntimeError("db unavailable")))
        provider = SimpleNamespace(chat=AsyncMock(return_value={"usage": None}), _model="m", usage_alias="p")
        with patch.object(observability, "_pool", pool):
            self.assertEqual(await observability.chat(provider, [], slot="vision"), {"usage": None})

    async def test_cancellation_is_persisted(self):
        pool = SimpleNamespace(execute=AsyncMock())
        provider = SimpleNamespace(chat=AsyncMock(side_effect=asyncio.CancelledError()), _model="m", usage_alias="p")
        with patch.object(observability, "_pool", pool), self.assertRaises(asyncio.CancelledError):
            await observability.chat(provider, [], slot="vision")
        self.assertEqual(pool.execute.await_count, 2)

    async def test_response_trace_only_counts_current_conversation(self):
        trace = observability.ResponseTrace()
        with observability.scope(conversation_id=42, response=trace):
            observability.message_sent(99)
            observability.message_sent(42)
            observability.workflow_started("run-1")
        self.assertEqual(trace.sent_count, 1)
        self.assertEqual(trace.run_id, "run-1")


class TriggerTelemetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_result_is_visible_instead_of_counted_as_a_valid_silence(self):
        engine = TriggerEngine()
        provider = SimpleNamespace(chat=AsyncMock(return_value={"message": {"content": "not a decision"}}))
        ctx = SimpleNamespace(
            conversation_id=42, self_user_id=7, state={}, store=object(),
            provider_mgr=SimpleNamespace(get_trigger_provider=lambda: provider, get_embedding_provider=lambda: None),
        )
        tc = SimpleNamespace(latest_id=12, query="test", text="user: test")
        with patch("sophos.trigger_context.load_rows", AsyncMock(return_value=[])), \
             patch("sophos.trigger_context.build_trigger_context", return_value=tc), \
             patch("sophos.trigger_context.persona_values", AsyncMock(return_value={})), \
             patch("sophos.prompts.render", return_value=SimpleNamespace(text="")), \
             patch("sophos.prompts.get", return_value="rules"), \
             patch.object(observability, "trigger_event", AsyncMock(return_value=6)), \
             patch.object(observability, "update_trigger", AsyncMock()) as update:
            self.assertEqual(await engine._evaluate(ctx, ("conversation", 42), since_id=0), "irrelevant")
        self.assertEqual(update.await_args.kwargs["status"], "invalid")
        self.assertEqual(update.await_args.kwargs["raw_result"], "not a decision")

    async def test_rate_limit_and_lock_are_distinct(self):
        engine = TriggerEngine()
        ctx = SimpleNamespace(conversation_id=42)
        key = ("conversation", 42)
        with patch.object(engine, "_get_bucket", return_value=SimpleNamespace(try_consume=lambda: False)), \
             patch.object(observability, "update_trigger", new_callable=AsyncMock) as update:
            await engine._respond(ctx, key, 12)
            update.assert_awaited_with(12, status="rate_limited")
        await engine._get_lock(key).acquire()
        with patch.object(engine, "_get_bucket", return_value=SimpleNamespace(try_consume=lambda: True)), \
             patch.object(observability, "update_trigger", new_callable=AsyncMock) as update:
            await engine._respond(ctx, key, 13)
            update.assert_awaited_with(13, status="locked")

    async def test_actual_reply_is_not_inferred_from_a_positive_evaluation(self):
        engine = TriggerEngine()
        ctx = SimpleNamespace(conversation_id=42)

        async def send(_ctx):
            observability.message_sent(42)

        for handler, expected, sent in ((AsyncMock(), "no_reply", 0), (send, "replied", 1)):
            with patch("sophos.pipeline._handle_llm_trigger", handler), \
                 patch.object(observability, "update_trigger", new_callable=AsyncMock) as update:
                await engine._respond(ctx, ("conversation", 42), 14)
                update.assert_awaited_with(14, status=expected, sent_count=sent, run_id=None)

    async def test_stats_requires_authentication(self):
        request = make_mocked_request("GET", "/api/stats")
        with patch.object(settings, "webui_password", "test-password"), self.assertRaises(web.HTTPUnauthorized):
            await auth_middleware(request, AsyncMock())


@unittest.skipUnless(os.getenv("STATS_TEST_DATABASE_URL"), "Set STATS_TEST_DATABASE_URL to an empty test database")
class StatsDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pool = await asyncpg.create_pool(os.environ["STATS_TEST_DATABASE_URL"], min_size=1, max_size=10)
        await _init_schema(self.pool)
        await self.pool.execute(
            "TRUNCATE platform_accounts, llm_usage, trigger_events, runtime_errors, tool_usage CASCADE"
        )
        account = await self.pool.fetchval(
            "INSERT INTO platform_accounts (platform, external_account_id) VALUES ('qq','bot') RETURNING id"
        )
        self.binding = await self.pool.fetchval(
            """INSERT INTO adapter_bindings (account_id,adapter_kind,name,external_id_namespace)
               VALUES ($1,'onebot','test','test') RETURNING id""", account,
        )
        self.cid = await self.pool.fetchval(
            """INSERT INTO conversations (account_id,kind,external_conversation_id,display_name)
               VALUES ($1,'group','1','测试群') RETURNING id""", account,
        )
        self.other = await self.pool.fetchval(
            """INSERT INTO conversations (account_id,kind,external_conversation_id)
               VALUES ($1,'direct','2') RETURNING id""", account,
        )
        app = web.Application()
        app["pool"] = self.pool
        app["adapter_router"] = AdapterRouter()
        app.router.add_routes(routes)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.pool.close()

    async def test_empty_ranges_and_validation(self):
        for window, length in (("today", 24), ("week", 7), ("month", 30)):
            response = await self.client.get(f"/api/stats?range={window}")
            self.assertEqual(response.status, 200, await response.text())
            data = await response.json()
            self.assertEqual(data["totals"]["messages"], 0)
            self.assertEqual(len(data["trend"]), length)
            self.assertIsNone(data["trigger"]["average_ms"])
        self.assertEqual((await self.client.get("/api/stats?conversation_id=abc")).status, 400)
        self.assertEqual((await self.client.get("/api/stats/usage?before=-1")).status, 400)

    async def test_live_collection_and_restart_recovery(self):
        provider = SimpleNamespace(chat=AsyncMock(return_value={
            "provider": "openai_compat", "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        }), _model="test-model", usage_alias="test-provider")
        with patch.object(observability, "_pool", self.pool):
            with observability.scope(conversation_id=self.cid, run_id="run-test"):
                await observability.chat(provider, [], slot="actor")
                event_id = await observability.trigger_event(self.cid, "simple")
            await observability.init(self.pool)
        usage = await self.pool.fetchrow("SELECT * FROM llm_usage")
        self.assertEqual(usage["total_tokens"], 120)
        self.assertEqual(usage["conversation_id"], self.cid)
        self.assertEqual(usage["provider"], "test-provider")
        self.assertEqual(usage["run_id"], "run-test")
        self.assertEqual(
            await self.pool.fetchval("SELECT status FROM trigger_events WHERE id=$1", event_id), "interrupted",
        )

    async def test_v3_migration_preserves_messages(self):
        mid = await self.pool.fetchval(
            """INSERT INTO messages(adapter_binding_id,conversation_id,source,occurred_at)
               VALUES($1,$2,'user',now()) RETURNING id""", self.binding, self.cid,
        )
        await self.pool.execute("DROP TABLE llm_usage, trigger_events, runtime_errors")
        await self.pool.execute("DROP TABLE tool_usage")
        await self.pool.execute("DROP INDEX IF EXISTS idx_messages_stats_time")
        await self.pool.execute("UPDATE schema_meta SET version=3 WHERE singleton")
        with patch.object(settings, "database_url", os.environ["STATS_TEST_DATABASE_URL"]):
            await migrate()
            await migrate_resources()
        self.assertEqual(await self.pool.fetchval("SELECT version FROM schema_meta"), 5)
        self.assertEqual(await self.pool.fetchval("SELECT id FROM messages WHERE id=$1", mid), mid)

    async def test_v4_migration_preserves_model_usage(self):
        await self.pool.execute(
            """INSERT INTO llm_usage(slot,provider,model,duration_ms,success,total_tokens)
               VALUES('actor','test','model',10,true,100)"""
        )
        await self.pool.execute("DROP TABLE tool_usage")
        await self.pool.execute("UPDATE schema_meta SET version=4 WHERE singleton")
        with patch.object(settings, "database_url", os.environ["STATS_TEST_DATABASE_URL"]):
            await migrate_resources()
            with self.assertRaises(RuntimeError):
                await migrate_resources()
        self.assertEqual(await self.pool.fetchval("SELECT sum(total_tokens) FROM llm_usage"), 100)
        self.assertEqual(await self.pool.fetchval("SELECT version FROM schema_meta"), 5)

    async def test_tavily_units_filters_and_unknown_credits(self):
        await self.pool.execute(
            """INSERT INTO tool_usage(conversation_id,run_id,tool,provider,credits,duration_ms,success,error)
               VALUES($1,'run','web_search','tavily',2,80,true,NULL),
                     ($1,'run','web_search','tavily',NULL,80,false,'HTTP 429'),
                     ($1,'run','web_fetch','tavily',0,80,true,NULL),
                     ($1,'run','web_fetch','tavily',NULL,80,true,NULL),
                     ($2,'other','web_search','tavily',10,80,true,NULL)""", self.cid, self.other,
        )
        await self.pool.execute(
            """INSERT INTO tool_usage(conversation_id,tool,provider,credits,duration_ms,success,created_at)
               VALUES($1,'web_search','tavily',100,80,true,now()-interval '40 days')""", self.cid,
        )
        data = await (await self.client.get(f"/api/stats?conversation_id={self.cid}")).json()
        self.assertEqual(data["totals"]["tokens"], 0)
        self.assertEqual(data["totals"]["calls"], 0)
        tools = {r["tool"]: r for r in data["tools"]}
        self.assertEqual(tools["web_search"]["calls"], 2)
        self.assertEqual(tools["web_search"]["failures"], 1)
        self.assertEqual(tools["web_search"]["credits"], 2)
        self.assertEqual(tools["web_fetch"]["calls"], 2)
        self.assertEqual(tools["web_fetch"]["credits"], 0)
        self.assertEqual(tools["web_fetch"]["missing_credits"], 1)
        page = await (await self.client.get(f"/api/stats/tools?conversation_id={self.cid}")).json()
        self.assertEqual(len(page["items"]), 4)
        self.assertEqual(sum(r["credits"] is None for r in page["items"]), 2)
        self.assertTrue(all(r["conversation_name"] == "测试群" for r in page["items"]))

    async def test_aggregation_filtering_and_cursor_details(self):
        for source, cid in (("user", self.cid), ("sophos", self.cid), ("co_account", self.cid), ("user", self.other)):
            await self.pool.execute(
                """INSERT INTO messages(adapter_binding_id,conversation_id,source,occurred_at)
                   VALUES ($1,$2,$3,now())""", self.binding, cid, source,
            )
        for _ in range(53):
            await self.pool.execute("""INSERT INTO llm_usage
                                   (conversation_id,slot,provider,model,input_tokens,output_tokens,
                                    total_tokens,duration_ms,success)
                                   VALUES($1,'actor','test','model',100,20,120,50,true)""", self.cid)
        await self.pool.execute(
            """INSERT INTO llm_usage(conversation_id,slot,provider,model,duration_ms,success)
               VALUES($1,'vision','test','unknown',50,true)""", self.cid,
        )
        await self.pool.execute(
            """INSERT INTO trigger_events(conversation_id,source,result,status,duration_ms)
               VALUES($1,'llm','relevant_done','locked',400)""", self.cid,
        )
        await self.pool.execute(
            """INSERT INTO runtime_errors(conversation_id,stage,error)
               VALUES($1,'vision','TimeoutError'),($1,'vision','TimeoutError')""", self.cid,
        )
        response = await self.client.get(f"/api/stats?conversation_id={self.cid}")
        self.assertEqual(response.status, 200, await response.text())
        data = await response.json()
        self.assertEqual(data["totals"]["messages"], 1)
        self.assertEqual(data["totals"]["replies"], 1)
        self.assertEqual(data["totals"]["tokens"], 53 * 120)
        self.assertEqual(data["totals"]["missing_usage"], 1)
        self.assertEqual(data["totals"]["tokens"], sum(r["tokens"] for r in data["trend"]))
        self.assertEqual(data["totals"]["messages"], sum(r["messages"] for r in data["trend"]))
        self.assertEqual(data["trigger"]["locked"], 1)
        self.assertEqual(data["trigger"]["replied"], 0)
        self.assertEqual(data["errors"][0]["count"], 2)
        page = await (await self.client.get(f"/api/stats/usage?conversation_id={self.cid}")).json()
        self.assertEqual(len(page["items"]), 50)
        page2 = await (
            await self.client.get(f"/api/stats/usage?conversation_id={self.cid}&before={page['next']}")
        ).json()
        self.assertEqual(len(page2["items"]), 4)
        self.assertIsNone(page2["next"])
        self.assertFalse({r["id"] for r in page["items"]} & {r["id"] for r in page2["items"]})
        trigger = await (await self.client.get("/api/stats/trigger")).json()
        self.assertEqual(trigger["items"][0]["conversation_name"], "测试群")


if __name__ == "__main__":
    unittest.main()
