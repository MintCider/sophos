import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from sophos import observability
from sophos.llm.embedding import EmbeddingProvider
from sophos.llm.tool_loop import run_tool_loop
from sophos.tools.image_gen import ImageGenerationTool
from sophos.tools.web import WebFetchTool, WebSearchTool


class ResourceUsageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.responses = []
        self.requests = []

        async def handle(request):
            self.requests.append((request.path, await request.json()))
            status, body = self.responses.pop(0)
            return web.json_response(body, status=status)

        app = web.Application()
        app.router.add_post('/{path:.*}', handle)
        self.server = TestServer(app)
        await self.server.start_server()
        self.base = str(self.server.make_url('')).rstrip('/')
        self.pool = SimpleNamespace(execute=AsyncMock())
        self.pool_patch = patch.object(observability, '_pool', self.pool)
        self.pool_patch.start()
        self.addCleanup(self.pool_patch.stop)

    async def asyncTearDown(self):
        await self.server.close()

    def calls(self, table):
        return [c.args for c in self.pool.execute.await_args_list if f'INSERT INTO {table}' in c.args[0]]

    async def test_batch_embedding_counts_one_request_and_inherits_context(self):
        self.responses = [(200, {
            'data': [{'index': 1, 'embedding': [2.]}, {'index': 0, 'embedding': [1.]}],
            'usage': {'prompt_tokens': 31, 'total_tokens': 31},
        })]
        provider = EmbeddingProvider(self.base, 'secret', 'embedding-model', provider_alias='embedding-provider')
        try:
            with observability.scope(conversation_id=42, run_id='run'):
                self.assertEqual(await provider.embed(['a', 'b']), [[1.], [2.]])
        finally:
            await provider.close()
        calls = self.calls('llm_usage')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1:8], (42, 'run', 'embedding', 'embedding-provider', 'embedding-model', 31, 0))
        self.assertEqual(calls[0][11], 31)
        self.assertTrue(calls[0][-2])

    async def test_multimodal_counts_each_request_including_failure(self):
        self.responses = [
            (200, {'data': {'embedding': [1.]}, 'usage': {'total_tokens': 12}}),
            (429, {'error': 'secret must not be stored'}),
        ]
        provider = EmbeddingProvider(self.base, 'secret', 'embedding-model', endpoint='/embeddings/multimodal')
        try:
            with self.assertRaises(aiohttp.ClientResponseError):
                await provider.embed(['a', 'b'])
        finally:
            await provider.close()
        calls = self.calls('llm_usage')
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][6], 12)
        self.assertFalse(calls[1][-2])
        self.assertIsNone(calls[1][11])
        self.assertNotIn('secret', str(self.calls('runtime_errors')))

    async def test_empty_embedding_has_no_request(self):
        provider = EmbeddingProvider(self.base, 'key', 'model')
        self.assertEqual(await provider.embed([]), [])
        self.assertEqual(self.calls('llm_usage'), [])

    async def test_embedding_dimension_detection_is_counted(self):
        self.responses = [(200, {'data': [{'index': 0, 'embedding': [1., 2.]}]})]
        provider = EmbeddingProvider(self.base, 'key', 'model')
        try:
            self.assertEqual(await provider.detect_dimension(), 2)
        finally:
            await provider.close()
        self.assertEqual(len(self.calls('llm_usage')), 1)
        self.assertIsNone(self.calls('llm_usage')[0][11])

    async def test_openai_image_usage_preserves_reported_total_and_cache(self):
        self.responses = [(200, {
            'data': [{'b64_json': 'image'}],
            'usage': {'input_tokens': 100, 'output_tokens': 500, 'total_tokens': 600,
                      'input_tokens_details': {'cached_tokens': 40}},
        })]
        with observability.scope(conversation_id=42, run_id='run-image'):
            image = await ImageGenerationTool()._call_openai(
                self.base, 'secret', 'image-model', 'draw', provider_alias='image-provider',
            )
        self.assertEqual(image, (None, 'image'))
        call = self.calls('llm_usage')[0]
        self.assertEqual(call[1:9], (42, 'run-image', 'image', 'image-provider', 'image-model', 100, 500, 40))
        self.assertEqual(call[11], 600)

    async def test_gemini_image_counts_thoughts_once(self):
        self.responses = [(200, {
            'generatedImages': [{'image': {'imageBytes': 'image'}}],
            'usageMetadata': {'promptTokenCount': 20, 'candidatesTokenCount': 80,
                              'thoughtsTokenCount': 10, 'totalTokenCount': 110},
        })]
        await ImageGenerationTool()._call_gemini(self.base, 'key', 'model', 'draw')
        call = self.calls('llm_usage')[0]
        self.assertEqual(call[6:8], (20, 90))
        self.assertEqual(call[10:12], (10, 110))

    async def test_image_without_usage_is_unknown_and_http_failure_is_recorded(self):
        self.responses = [(200, {'data': [{'url': 'https://example.com/image.png'}]}), (502, {'error': 'secret'})]
        tool = ImageGenerationTool()
        await tool._call_openai(self.base, 'key', 'model', 'draw')
        with self.assertRaises(aiohttp.ClientResponseError):
            await tool._call_openai(self.base, 'key', 'model', 'draw')
        calls = self.calls('llm_usage')
        self.assertEqual(len(calls), 2)
        self.assertIsNone(calls[0][11])
        self.assertTrue(calls[0][-2])
        self.assertFalse(calls[1][-2])

    async def test_image_empty_response_fails_and_gemini_error_does_not_leak_key(self):
        self.responses = [(200, {'data': []}), (429, {'error': 'upstream detail'})]
        tool = ImageGenerationTool()
        with self.assertRaises(RuntimeError):
            await tool._call_openai(self.base, 'secret', 'model', 'draw')
        with self.assertRaises(RuntimeError) as failure:
            await tool._call_gemini(self.base, 'secret', 'model', 'draw')
        self.assertNotIn('secret', str(failure.exception))
        self.assertTrue(all(not c[-2] for c in self.calls('llm_usage')))

    async def test_cancelled_tool_request_is_a_failure(self):
        with self.assertRaises(asyncio.CancelledError):
            async with observability.tool_call('web_search', provider='tavily'):
                raise asyncio.CancelledError()
        call = self.calls('tool_usage')[0]
        self.assertFalse(call[7])
        self.assertIsNone(call[5])

    def test_defaulted_zero_total_does_not_discard_reported_input(self):
        usage = observability.normalize_usage({'prompt_tokens': 20, 'completion_tokens': 5, 'total_tokens': 0}, '')
        self.assertEqual(usage['total_tokens'], 25)

    async def test_background_image_retains_conversation_and_run(self):
        finished = asyncio.Event()
        captured = []

        async def generate(**kwargs):
            ctx = observability._context.get()
            captured.append((kwargs['provider_alias'], ctx.conversation_id, ctx.run_id, ctx.response))
            finished.set()

        pool = SimpleNamespace(fetchrow=AsyncMock(side_effect=[
            {'provider_alias': 'image-provider', 'model_name': 'm', 'api_type': 'openai', 'config': {}},
            {'base_urls': {'openai': self.base}, 'api_key': 'key'},
        ]))
        with patch('sophos.tools.image_gen._generate_and_send', generate), \
             patch('sophos.tools.image_gen._register_queue_task', return_value='test-task'):
            with observability.scope(run_id='run', response=observability.ResponseTrace()):
                await ImageGenerationTool().execute({'prompt': 'draw'}, {'pool': pool, 'conversation_id': 42})
            await asyncio.wait_for(finished.wait(), timeout=2)
        self.assertEqual(captured, [('image-provider', 42, 'run', None)])

    def tavily_config(self, key):
        return {'tavily_api_key': 'secret', 'tavily_max_results': 5, 'tavily_include_answer': True}.get(key)

    async def test_tavily_search_is_a_request_not_tokens(self):
        self.responses = [(200, {'results': [], 'usage': {'credits': 2}})]
        with patch('sophos.tools.web._TAVILY_SEARCH_URL', self.base + '/search'), \
             patch('sophos.tools.web.runtime_config.get', side_effect=self.tavily_config), \
             observability.scope(run_id='search-run'):
            output = await WebSearchTool().execute({'query': 'q'}, {'conversation_id': 42})
            self.assertEqual(output, {'results': []})
        self.assertTrue(self.requests[0][1]['include_usage'])
        call = self.calls('tool_usage')[0]
        self.assertEqual(call[1:6], (42, 'search-run', 'web_search', 'tavily', 2.))
        self.assertTrue(call[7])
        self.assertEqual(self.calls('llm_usage'), [])

    async def test_extract_multiple_urls_is_one_request_and_partial_failure_is_visible(self):
        self.responses = [(200, {
            'results': [{'url': 'a', 'raw_content': 'text'}], 'failed_results': [{'url': 'b', 'error': 'failed'}],
            'usage': {'credits': 0.2},
        })]
        with patch('sophos.tools.web._TAVILY_EXTRACT_URL', self.base + '/extract'), \
             patch('sophos.tools.web.runtime_config.get', side_effect=self.tavily_config):
            output = await WebFetchTool().execute({'urls': ['a', 'b']}, {'conversation_id': 42})
        self.assertIn('failed', output)
        calls = self.calls('tool_usage')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][3:6], ('web_fetch', 'tavily', .2))
        self.assertFalse(calls[0][7])
        self.assertEqual(len(self.calls('runtime_errors')), 1)

    async def test_tavily_unknown_zero_and_failed_requests(self):
        self.responses = [(200, {'results': []}), (200, {'results': [], 'usage': {'credits': 0}}), (429, {})]
        with patch('sophos.tools.web._TAVILY_SEARCH_URL', self.base + '/search'), \
             patch('sophos.tools.web.runtime_config.get', side_effect=self.tavily_config):
            for _ in range(3):
                await WebSearchTool().execute({'query': 'q'}, {})
        calls = self.calls('tool_usage')
        self.assertIsNone(calls[0][5])
        self.assertEqual(calls[1][5], 0.)
        self.assertFalse(calls[2][7])
        self.assertEqual(calls[2][8], 'HTTP 429')

    async def test_old_tool_loop_retries_and_fallback_are_all_counted(self):
        provider = SimpleNamespace(
            chat=AsyncMock(side_effect=[TimeoutError(), TimeoutError(), {'message': {'content': 'done'}}]),
            usage_alias='provider', _model='model',
        )
        result = await run_tool_loop(provider, [], [], AsyncMock(), slot='collector')
        self.assertEqual(result[-1]['content'], 'done')
        calls = self.calls('llm_usage')
        self.assertEqual(len(calls), 3)
        self.assertEqual([c[3] for c in calls], ['collector'] * 3)
        self.assertEqual([c[-2] for c in calls], [False, False, True])


if __name__ == '__main__':
    unittest.main()
