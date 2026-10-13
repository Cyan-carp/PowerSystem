import asyncio
import json
from pathlib import Path

import httpx
from review_contract import Opinion, RULES, POLICY, RUNTIME_POLICY


class ReviewFailure(Exception):
    def __init__(self, code, state='unavailable', validation_code=None):
        self.code, self.state, self.validation_code = code, state, validation_code
        super().__init__(code)


class Provider:
    def __init__(self, cfg, client):
        self.cfg, self.client = cfg, client
        self.policy = (Path(__file__).resolve().parents[1] / 'policy/sec-m1-policy.txt').read_text(encoding='utf-8')
        self.runtime_policies = {version: (Path(__file__).resolve().parents[1] / filename).read_text(encoding='utf-8')
            for version,filename in [('sec-m2-policy/v1','policy/sec-m2-policy.txt'),('sec-m2-policy/v2','policy/sec-m2-policy-v2.txt'),('sec-m2-policy/v3','policy/sec-m2-policy-v3.txt'),('sec-m2-policy/v4','policy/sec-m2-policy-v4.txt')]}
        self.runtime_current=json.loads((Path(__file__).resolve().parents[1]/'policy/sec-m2-policy-v5.json').read_text(encoding='utf-8'))

    async def review(self, projected, timeout):
        if projected['policy_version'] != POLICY and (self.cfg.model != 'deepseek-v4-flash-0731' or self.cfg.model_timeout != 30 or self.cfg.acceptance_timeout_exception):
            raise ReviewFailure('runtime_model_configuration_blocked')
        schema=Opinion.model_json_schema()
        schema['properties']['rule_hits']['items']={'type':'string','enum':list(RULES)}
        schema['properties']['evidence_refs']['items']={'type':'string','enum':[e['ref'] for e in projected['evidence']]}
        policy = (self.runtime_current['common']+'\n本次审查类别严格为 '+projected['kind']+'，只应用以下类别规则：\n'+self.runtime_current['kinds'][projected['kind']]) if projected['policy_version']==RUNTIME_POLICY else {POLICY: self.policy, **self.runtime_policies}[projected['policy_version']]
        instruction=policy+'\n严格匹配以下输出JSON Schema，模型只返回四个意见字段，不生成任何关联标识。Schema不预先指定decision。\n'+json.dumps(schema,ensure_ascii=False)
        body = {'model': self.cfg.model, 'stream': False, 'max_tokens': self.cfg.max_tokens,
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
            raw = response.json()
            message = raw['choices'][0]['message']
            if message.get('tool_calls') or not isinstance(message.get('content'), str):
                raise ReviewFailure('model_invalid_output', 'invalid')
            decision = json.loads(message['content'])
            usage = raw.get('usage', {})
            # Billing counts only; never save model reasoning or gateway response bodies.
            usage = {k: v for k, v in usage.items() if k in ('prompt_tokens', 'completion_tokens', 'total_tokens') and type(v) is int and v >= 0}
            return decision, usage
        except (TimeoutError, httpx.TimeoutException):
            raise ReviewFailure('model_timeout') from None
        except httpx.HTTPError:
            raise ReviewFailure('model_transport_unavailable') from None
        except (KeyError, TypeError, IndexError, ValueError):
            raise ReviewFailure('model_invalid_output', 'invalid') from None
