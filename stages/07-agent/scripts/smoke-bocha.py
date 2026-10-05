"""Real search + unchanged answer validator. Writes successes and failures separately."""
import argparse
import asyncio
import json
import time
from pathlib import Path
import httpx
from agent.config import Config
from agent.context import Context
from agent.knowledge import Knowledge
from agent.llm import Provider, ProviderError
from agent.misses import Misses
from agent.orchestrator import Orchestrator, validate_answer
from agent.schemas import ChatResponse, ModelAnswer
from agent.search import Search

QUESTIONS = ["风机覆冰应该怎么排查？", "逆变器电弧故障如何排查？", "逆变器接地与绝缘异常如何排查？", "光伏电弧问题怎么排查？", "风机过热故障怎么排查？"]


async def main(output):
    output.mkdir(parents=True, exist_ok=True)
    config = Config.load()
    knowledge = Knowledge(config.knowledge_path)
    assert config.search_provider == "bocha" and knowledge.available
    misses = Misses(output/"knowledge-misses", (config.api_key,config.search_key,config.service_token,config.monitor_token))
    records = []
    async with httpx.AsyncClient() as client:
        class RecordedProvider(Provider):
            last = None
            async def complete(self, *args, **kwargs):
                self.last = await super().complete(*args, **kwargs)
                return self.last
        provider, search = RecordedProvider(config, client), Search(config, client)
        class NoBusiness:
            async def execute(self, *args):
                raise AssertionError("Public knowledge must not invoke business tools")
        for index, question in enumerate(QUESTIONS):
            assert knowledge.search(question)[0] == "miss", question
            response = ChatResponse(request_id=f"bocha-real-{index}-{time.time_ns()}",session_id=f"bocha-{index}",status="degraded",model=config.model)
            started = time.monotonic()
            error = None
            provider.last = None
            rejection = None
            try:
                async with asyncio.timeout(config.total_timeout):
                    await Orchestrator(config,provider,NoBusiness()).run(question,[],"",response,[],context=Context(knowledge,search,misses))
            except (ProviderError, TimeoutError) as exc:
                error = str(exc) if isinstance(exc,ProviderError) else "timeout"
                response.conclusion = None; response.suggestions = []
                if error == "answer_validation_failed" and provider.last:
                    try:
                        draft = ModelAnswer.model_validate(json.loads(provider.last['content']))
                        validate_answer(draft,response.evidence)
                    except (ValueError, TypeError, KeyError) as rejected:
                        # Only reason; rejected draft is never shown as an answer.
                        rejection = str(rejected).splitlines()[0][:120]
            if response.conclusion:
                validate_answer(ModelAnswer(conclusion=response.conclusion,suggestions=response.suggestions,limitations=response.limitations),response.evidence)
            passed = response.status == "answered" and response.web_status == "hit" and response.miss_record_status == "recorded" and error is None
            record = {"question":question,"elapsed_seconds":round(time.monotonic()-started,3),"passed":passed,"error":error,"rejection":rejection,"response":response.model_dump()}
            (output/("success" if passed else "failure")).mkdir(exist_ok=True)
            (output/("success" if passed else "failure")/f"answer-{index}.json").write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding="utf-8")
            records.append({k:v for k,v in record.items() if k != "response"})
            (output/"summary.json").write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding="utf-8")
            print(json.dumps(records[-1],ensure_ascii=False),flush=True)
    return all(row["passed"] for row in records)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("--output",type=Path,required=True)
    raise SystemExit(0 if asyncio.run(main(parser.parse_args().output)) else 1)
