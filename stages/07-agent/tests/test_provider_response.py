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
        self.assertEqual(result, {'role': 'assistant', 'content': '', 'tool_calls': calls,
                                  'finish_reason': 'tool_calls', 'usage': {}})

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

    async def capture(self, tools=None, status=200, max_tokens=None):
        """Return the JSON request body actually sent and the result or error."""
        sent = {}
        payload = {'choices': [{'message': {'role': 'assistant', 'content': 'OK'}, 'finish_reason': 'stop'}]}

        def handler(request):
            sent.update(json.loads(request.content))
            if status == 200:
                return httpx.Response(200, json=payload)
            return httpx.Response(status, json={'error': {'message': 'vendor detail'}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = Provider(CFG, client)
            try:
                return sent, await provider.complete([{'role': 'user', 'content': 'fixture'}], tools, max_tokens=max_tokens)
            except ProviderError as exc:
                return sent, exc

    async def test_tools_always_send_auto_and_keep_schema(self):
        schema = {
            'type': 'object',
            'properties': {'conclusion': {'$ref': '#/$defs/Claim'}},
            'required': ['conclusion'],
            'additionalProperties': False,
            '$defs': {'Claim': {'type': 'object', 'properties': {'text': {'type': 'string', 'maxLength': 1485}},
                                'required': ['text'], 'additionalProperties': False}},
        }
        submit = {'type': 'function', 'function': {
            'name': 'submit_interpretation', 'description': 'fixture', 'parameters': schema}}
        for name, tools in {'single_submit': [submit],
                            'with_other_tool': [submit, {'type': 'function', 'function': {
                                'name': 'probe', 'parameters': {'type': 'object'}}}]}.items():
            with self.subTest(tools=name):
                sent, result = await self.capture(tools)
                self.assertEqual(sent['tool_choice'], 'auto')
                self.assertEqual(sent['tools'], tools)
                self.assertEqual(sent['tools'][0]['function']['parameters'], schema)
                self.assertEqual(result['content'], 'OK')

    async def test_no_tools_omits_tool_choice(self):
        sent, _ = await self.capture()
        self.assertNotIn('tools', sent)
        self.assertNotIn('tool_choice', sent)

    async def test_default_budget_stays_1500_and_interpretation_overrides(self):
        sent, _ = await self.capture()
        self.assertEqual(sent['max_tokens'], 1500)
        sent, _ = await self.capture(max_tokens=4096)
        self.assertEqual(sent['max_tokens'], 4096)
        for invalid in (0, -1, True, '4096', 8193):
            with self.subTest(max_tokens=invalid):
                sent, error = await self.capture(max_tokens=invalid)
                self.assertIsInstance(error, ProviderError)
                self.assertEqual(str(error), 'model_output_budget_invalid')
                self.assertEqual(sent, {})

    async def test_safe_finish_reason_and_numeric_usage_only(self):
        payload = {
            'choices': [{'message': {'role': 'assistant', 'content': 'OK',
                                     'reasoning_content': 'hidden vendor reasoning'},
                         'finish_reason': 'length'}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 1500, 'total_tokens': 1510,
                      'completion_tokens_details': {'reasoning_tokens': 1400, 'text': 'hidden'},
                      'reasoning_content': 'hidden'},
        }
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda req: httpx.Response(200, json=payload))) as client:
            result = await Provider(CFG, client).complete([{'role': 'user', 'content': 'fixture'}])
        self.assertEqual(result['finish_reason'], 'length')
        self.assertEqual(result['usage'], {'prompt_tokens': 10, 'completion_tokens': 1500,
                                           'total_tokens': 1510, 'reasoning_tokens': 1400})
        self.assertNotIn('reasoning_content', result)
        self.assertNotIn('hidden', json.dumps(result))

    async def test_unknown_finish_reason_and_bad_usage_are_dropped(self):
        payload = {'choices': [{'message': {'role': 'assistant', 'content': 'OK'}, 'finish_reason': 'vendor_note'}],
                   'usage': {'prompt_tokens': -1, 'completion_tokens': True, 'total_tokens': 'many',
                             'completion_tokens_details': {'reasoning_tokens': 1.5}}}
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda req: httpx.Response(200, json=payload))) as client:
            result = await Provider(CFG, client).complete([{'role': 'user', 'content': 'fixture'}])
        self.assertEqual(result['finish_reason'], '')
        self.assertEqual(result['usage'], {})

    async def test_502_is_unavailable_with_http_status(self):
        sent, error = await self.capture([{'type': 'function', 'function': {'name': 'submit_interpretation'}}], 502)
        self.assertEqual(sent['tool_choice'], 'auto')
        self.assertIsInstance(error, ProviderError)
        self.assertEqual(str(error), 'unavailable')
        self.assertEqual(error.retry_after, 0)
        self.assertEqual(error.http_status, 502)
        self.assertNotIn('vendor', str(error))
        self.assertIsNone(ProviderError('unavailable').http_status)

    async def test_missing_choice_still_rejected(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(
                200, json={'choices': []}))) as client:
            with self.assertRaisesRegex(ProviderError, '^model_invalid_response$'):
                await Provider(CFG, client).complete([])


if __name__ == '__main__':
    unittest.main()
