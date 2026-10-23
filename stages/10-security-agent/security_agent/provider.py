import asyncio
import json
from pathlib import Path

import httpx
from review_contract import Opinion, RULES, POLICY, RUNTIME_POLICY, INSPECTION_POLICY


class ReviewFailure(Exception):
    def __init__(self, code, state='unavailable', validation_code=None):
        self.code, self.state, self.validation_code = code, state, validation_code
        super().__init__(code)


def merge_tool_calls(target, deltas):
    """Merge OpenAI-style incremental tool_call deltas into a full list."""
    for delta in deltas:
        if not isinstance(delta, dict):
            continue
        index = delta.get('index')
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            index = len(target)
        while len(target) <= index:
            target.append({'id': '', 'type': 'function', 'function': {'name': '', 'arguments': ''}})
        slot = target[index]
        if isinstance(delta.get('id'), str) and delta['id'] and not slot['id']:
            slot['id'] = delta['id']
        function = delta.get('function')
        if not isinstance(function, dict):
            continue
        if isinstance(function.get('name'), str) and function['name'] and not slot['function']['name']:
            slot['function']['name'] = function['name']
        if isinstance(function.get('arguments'), str):
            slot['function']['arguments'] += function['arguments']
    return target


def parse_event_stream(raw):
    """Rebuild (content, tool_calls, usage) from a completed SSE body.

    The gateway answers stream=false with a bare keepalive frame, so requests are
    sent with stream=true and the chunks are aggregated here. Frames carrying
    usage usually have an empty choices list and must not be treated as errors.
    Keep in sync with the ops and RAG providers.
    """
    content = []
    calls = []
    usage = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith(':'):
            continue
        if not line.startswith('data:'):
            continue
        payload = line[5:].strip()
        if payload == '[DONE]':
            break
        try:
            data = json.loads(payload)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        if isinstance(data.get('usage'), dict) and data['usage']:
            usage = data['usage']
        choices = data.get('choices')
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            continue
        delta = choices[0].get('delta')
        if not isinstance(delta, dict):
            delta = choices[0].get('message')
        if not isinstance(delta, dict):
            continue
        piece = delta.get('content')
        if isinstance(piece, str):
            content.append(piece)
        if isinstance(delta.get('tool_calls'), list):
            merge_tool_calls(calls, delta['tool_calls'])
    return (''.join(content) or None), (calls or None), usage


def message_from_json(payload):
    """Read (content, tool_calls, usage) from a non-streaming completion body."""
    choices = payload['choices']
    if not isinstance(choices, list) or not choices:
        raise TypeError()
    message = choices[0]['message']
    if not isinstance(message, dict):
        raise TypeError()
    calls = message.get('tool_calls')
    usage = payload.get('usage')
    return message.get('content'), (calls if isinstance(calls, list) and calls else None), (usage if isinstance(usage, dict) else {})


def read_message(response):
    """Return (content, tool_calls, usage) for either wire format."""
    if 'text/event-stream' in response.headers.get('content-type', '').lower():
        return parse_event_stream(response.text)
    return message_from_json(response.json())


class Provider:
    def __init__(self, cfg, client):
        self.cfg, self.client = cfg, client
        self.policy = (Path(__file__).resolve().parents[1] / 'policy/sec-m1-policy.txt').read_text(encoding='utf-8')
        self.runtime_policies = {version: (Path(__file__).resolve().parents[1] / filename).read_text(encoding='utf-8')
            for version,filename in [('sec-m2-policy/v1','policy/sec-m2-policy.txt'),('sec-m2-policy/v2','policy/sec-m2-policy-v2.txt'),('sec-m2-policy/v3','policy/sec-m2-policy-v3.txt'),('sec-m2-policy/v4','policy/sec-m2-policy-v4.txt')]}
        self.runtime_current=json.loads((Path(__file__).resolve().parents[1]/'policy/sec-m2-policy-v5.json').read_text(encoding='utf-8'))

        self.m4_policies={v:json.loads((Path(__file__).resolve().parents[1]/f'policy/sec-m4-policy-v{v.rsplit("v",1)[1]}.json').read_text(encoding='utf-8')) for v in ('sec-m4-policy/v1','sec-m4-policy/v2','sec-m4-policy/v3','sec-m4-policy/v4','sec-m4-policy/v5','sec-m4-policy/v6')}
        self.inspection_policies={version:json.loads((Path(__file__).resolve().parents[1]/filename).read_text(encoding='utf-8')) for version,filename in [('sec-m3-policy/v1','policy/sec-m3-policy-v1.json'),('sec-m3-policy/v2','policy/sec-m3-policy-v2.json'),(INSPECTION_POLICY,'policy/sec-m3-policy-v3.json')]}

    async def review(self, projected, timeout):
        expected_model = 'deepseek-v4.1-flash' if self.cfg.runtime_profile == 'm4-server-readonly' else 'deepseek-v4-flash-0731'
        if projected['policy_version'] != POLICY and (self.cfg.model != expected_model or self.cfg.model_timeout != 30 or self.cfg.acceptance_timeout_exception):
            raise ReviewFailure('runtime_model_configuration_blocked')
        schema=Opinion.model_json_schema()
        schema['properties']['rule_hits']['items']={'type':'string','enum':list(RULES)}
        schema['properties']['evidence_refs']['items']={'type':'string','enum':[e['ref'] for e in projected['evidence']]}
        policy = (self.runtime_current['common']+'\n本次审查类别严格为 '+projected['kind']+'，只应用以下类别规则：\n'+self.runtime_current['kinds'][projected['kind']]) if projected['policy_version']==RUNTIME_POLICY else {POLICY: self.policy, 'sec-m4-policy/v1':'', 'sec-m4-policy/v2':'', 'sec-m4-policy/v3':'', 'sec-m4-policy/v4':'', 'sec-m4-policy/v5':'', 'sec-m4-policy/v6':'', INSPECTION_POLICY:'', 'sec-m3-policy/v1':'', 'sec-m3-policy/v2':'', **self.runtime_policies}[projected['policy_version']]
        if projected['policy_version'] in self.inspection_policies:
            inspection_policy=self.inspection_policies[projected['policy_version']]
            policy=inspection_policy['common']+'\n本次审查类别：'+projected['kind']+'\n'+inspection_policy['kinds'][projected['kind']]
        if projected['policy_version'] in self.m4_policies:
            m4=self.m4_policies[projected['policy_version']]
            policy=m4['common']+'\n本次审查类别：'+projected['kind']+'\n'+m4['kinds'][projected['kind']]
        instruction=policy+'\n严格匹配以下输出JSON Schema，模型只返回四个意见字段，不生成任何关联标识。Schema不预先指定decision。\n'+json.dumps(schema,ensure_ascii=False)
        body = {'model': self.cfg.model, 'stream': True, 'max_tokens': self.cfg.max_tokens,
                'response_format':{'type':'json_object'},'temperature':0,
                'messages': [{'role': 'system', 'content': instruction},
                             {'role': 'user', 'content': json.dumps(projected, ensure_ascii=False, allow_nan=False)}]}
        # This explicitly selected temporary model must fit the same 30-second budget.
        # Do not send OpenAI-specific parameters when the owner switches back to DeepSeek.
        if self.cfg.model == 'gpt-5.6-sol':
            body['reasoning_effort'] = 'none'
            body['response_format'] = {'type':'json_schema','json_schema':{
                'name':'security_opinion','strict':True,'schema':schema}}
        try:
            async with asyncio.timeout(timeout):
                response = await self.client.post(self.cfg.llm_url + '/chat/completions', json=body,
                    headers={'Authorization': 'Bearer ' + self.cfg.llm_key}, timeout=timeout)
            if response.status_code != 200:
                raise ReviewFailure('model_http_' + str(response.status_code))
            content, calls, usage = read_message(response)
            if calls or not isinstance(content, str):
                raise ReviewFailure('model_invalid_output', 'invalid')
            decision = json.loads(content)
            # Billing counts only; never save model reasoning or gateway response bodies.
            usage = {k: v for k, v in usage.items() if k in ('prompt_tokens', 'completion_tokens', 'total_tokens') and type(v) is int and v >= 0}
            return decision, usage
        except (TimeoutError, httpx.TimeoutException):
            raise ReviewFailure('model_timeout') from None
        except httpx.HTTPError:
            raise ReviewFailure('model_transport_unavailable') from None
        except (KeyError, TypeError, IndexError, ValueError):
            raise ReviewFailure('model_invalid_output', 'invalid') from None
