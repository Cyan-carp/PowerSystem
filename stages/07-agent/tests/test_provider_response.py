"""Regression coverage for response-role compatibility without relaxing output guards."""
import json
import unittest
from types import SimpleNamespace

import httpx
from agent.llm import Provider, ProviderError


CFG = SimpleNamespace(model_configured=True, model='fixture', llm_url='https://model.invalid/v1',
                      api_key='fixture-key', service_token='fixture-service', monitor_token='',
                      search_key='', model_timeout=1)


class ProviderResponseTest(unittest.IsolatedAsyncioTestCase):
    async def complete(self, message):
        # The observed gateway advertises event-stream but returns one JSON object.
        response = {'choices': [{'message': message, 'finish_reason': 'tool_calls'}]}
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(
                200, content=json.dumps(response), headers={'content-type': 'text/event-stream'}))) as client:
            return await Provider(CFG, client).complete([{'role': 'user', 'content': 'fixture'}])

    async def test_empty_role_normalized_with_tool_call_preserved(self):
        calls = [{'id': 'fixture-call', 'type': 'function', 'function': {
            'name': 'submit_interpretation', 'arguments': '{"conclusion":{}}'}}]
        result = await self.complete({'role': '', 'content': '', 'tool_calls': calls})
        self.assertEqual(result, {'role': 'assistant', 'content': '', 'tool_calls': calls})

    async def test_empty_role_plain_text(self):
        self.assertEqual((await self.complete({'role': '', 'content': 'OK'}))['content'], 'OK')

    async def test_standard_and_missing_roles_still_supported(self):
        for message in ({'role': 'assistant', 'content': 'OK'}, {'content': 'OK'}):
            with self.subTest(message=message):
                self.assertEqual((await self.complete(message))['role'], 'assistant')

    async def test_explicit_non_assistant_roles_rejected(self):
        for role in ('user', 'system', 'tool', 'developer', None, 42, [], {}):
            with self.subTest(role=role), self.assertRaisesRegex(ProviderError, '^model_invalid_response$'):
                await self.complete({'role': role, 'content': 'unsafe'})

    async def test_invalid_message_and_calls_still_rejected(self):
        for message in (None, [], 'text', {'role': '', 'tool_calls': None},
                        {'role': '', 'tool_calls': {}}, {'role': '', 'tool_calls': [{}] * 11}):
            with self.subTest(message=message), self.assertRaisesRegex(ProviderError, '^model_invalid_response$'):
                await self.complete(message)

    async def test_missing_choice_still_rejected(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(
                200, json={'choices': []}))) as client:
            with self.assertRaisesRegex(ProviderError, '^model_invalid_response$'):
                await Provider(CFG, client).complete([])


if __name__ == '__main__':
    unittest.main()
