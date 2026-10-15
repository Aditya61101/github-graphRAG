"""One approved canonical assessment; no full evaluation, retry or challenge path."""
import asyncio
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import tiktoken
from dotenv import load_dotenv
from openai import AsyncAzureOpenAI

from ai_services.decision_guard.budget import ModelTokenCounter
from ai_services.decision_guard.evaluation import AzureEvaluationModel, EvaluationSettings, UploadAuthority
from ai_services.decision_guard.evaluation.engine import ContextRequestBudget
from ai_services.decision_guard.evaluation.evidence import validate_context, resolve_quote
from ai_services.decision_guard.evaluation.model_adapter import ASSESS_PROMPT, PROMPT_VERSION, wire_text
from ai_services.decision_guard.evaluation.models import AssessmentRequest, AssessmentBatchOutput, QuoteRef
from ai_services.decision_guard.evaluation.validation import validate_proposal, validate_issue
from ai_services.decision_guard.evaluation.wire import provider_request_json, WireAssessment
from ai_services.decision_guard.models import ArchitectureContext, digest, stable_id
from ai_services.runs.benchmark_stage_4_1_hybrid_retrieval import save, file_hash
from ai_services.runs.prepare_stage_4_2_live_validation import ROOT, source_for
from ai_services.runs.run_decision_guard_evaluation import ReadOnlyStore
from ai_services.runs.run_stage_4_2_live_validation import AggregateModel

TAG='controlled_live_v1'
def out(name):return ROOT/f'A_{TAG}_{name}.json'
def read(name):return json.loads((ROOT/name).read_text(encoding='utf-8'))

async def run():
    if list(ROOT.glob(f'A_{TAG}_*.json')) or list(ROOT.glob('call_03.*')):
        raise FileExistsError('Never repeat an attempted controlled assessment')
    lease=ROOT/'live_validation.lock'
    descriptor=os.open(lease,os.O_CREAT|os.O_EXCL|os.O_WRONLY);os.close(descriptor)
    client=None
    try:
        protected={p.name:file_hash(p) for p in ROOT.glob('*.json')}
        pre=read('A_controlled_preflight_v1_preflight.json')
        previous=read('aggregate_usage.json')
        integrity=read('A_controlled_preflight_v1_integrity.json')
        assert file_hash(ROOT/'aggregate_usage.json')==integrity['ledger_sha256']
        assert all(file_hash(ROOT/n)==h for n,h in protected.items())
        request=AssessmentRequest.model_validate_json((ROOT/'A_controlled_preflight_v1_request.json').read_text(encoding='utf-8'))
        manifest=read('manifest.json')
        settings=EvaluationSettings.model_validate(manifest['settings'])
        assert settings.model_dump(mode='json')==pre['settings']
        assert PROMPT_VERSION==pre['prompt_version'] and digest(ASSESS_PROMPT)==pre['prompt_sha256']
        assert digest(json.dumps(WireAssessment.model_json_schema(),sort_keys=True))==pre['schema_sha256']
        assert digest(request.model_dump_json())==pre['canonical_request_sha256']
        wire=provider_request_json(request,'ASSESS')
        assert digest(wire)==pre['provider_payload_sha256'] and digest(wire_text(request,'ASSESS'))==pre['estimator_wire_sha256']
        assert json.loads(wire)==read('A_controlled_preflight_v1_provider_payload.json')
        ctx=validate_context(ArchitectureContext.model_validate_json((ROOT/'A_semantic_live_v1_without_semantic_context.json').read_text(encoding='utf-8')))
        source,_=source_for(ctx.revision.repository_id,Path(next(c for c in manifest['cases'] if c['id']=='A')['source_root']))
        authority=UploadAuthority(ReadOnlyStore('data/decisionguard.db'),'data/adrs')
        enc=tiktoken.get_encoding('o200k_base')
        counter=ModelTokenCounter('o200k_base:deployment-estimate',lambda t:len(enc.encode(t,disallowed_special=())))
        sizing=ContextRequestBudget(source,authority,settings=settings,token_counter=counter)
        await asyncio.to_thread(sizing,ctx)
        assert [p.model_dump(mode='json') for p in sizing.last_packets]==read('A_semantic_live_v1_without_semantic_packets.json')
        assert sizing.last_packets[5]==request and sizing.fits(request,'ASSESS')
        assert sizing.count(request,'ASSESS')==pre['selected_input_tokens']==18854
        assert settings.max_output_tokens==5000 and sizing.count(request,'ASSESS')+5000==pre['reservation']==23854
        assert previous['max_tokens']==60000 and previous['max_attempts']==8 and len(previous['calls'])==2
        assert sum(c['charged_tokens'] for c in previous['calls'])==32772
        load_dotenv('.env')
        assert os.environ['AZURE_OPENAI_DEPLOYMENT_NAME']==pre['configuration']['deployment']
        assert os.environ['AZURE_OPENAI_API_VERSION']==pre['configuration']['api_version']
        calls=[]
        async def guard(http_request):
            assert not calls and http_request.method=='POST' and http_request.url.path.endswith('/chat/completions')
            payload=json.loads(http_request.content)
            assert payload['model']==pre['configuration']['deployment']
            assert payload['max_completion_tokens']==5000
            assert payload['messages']==[{'role':'system','content':ASSESS_PROMPT},{'role':'user','content':wire}]
            calls.append('ASSESS')
            save(out('http_attempt'),{'request_count':1,'batch_id':request.batch_id,'request_verified':True})
        async def capture(response):
            await response.aread()
            with (ROOT/'call_03.provider.json').open('xb') as f:f.write(response.content)
        client=AsyncAzureOpenAI(azure_endpoint=os.environ['AZURE_OPENAI_ENDPOINT'],api_key=os.environ['AZURE_OPENAI_API_KEY'],api_version=pre['configuration']['api_version'],max_retries=0,timeout=settings.call_timeout_seconds,http_client=httpx.AsyncClient(event_hooks={'request':[guard],'response':[capture]}))
        adapter=AzureEvaluationModel(SimpleNamespace(client=client,deployment=pre['configuration']['deployment']),deployment_revision=pre['deployment_revision'])
        aggregate=AggregateModel(adapter,ROOT,counter)
        assert aggregate.remaining_tokens==27228 and aggregate.remaining_attempts==6
        assert counter(wire_text(request,'ASSESS'))+512+5000==23854
        assert all(file_hash(ROOT/n)==h for n,h in protected.items())
        save(out('authorization'),{'batch_id':request.batch_id,'canonical_request_sha256':pre['canonical_request_sha256'],'provider_payload_sha256':pre['provider_payload_sha256'],'max_requests':1,'reservation':23854,'max_output_tokens':5000,'retries':0,'challenge':False,'supplementation':False,'embedding_calls':0,'protected_files':protected})
        aggregate.case='A';aggregate.run_tag=TAG
        started=time.monotonic()
        try:
            reply=await aggregate.assess(request,max_output_tokens=5000,timeout=settings.call_timeout_seconds)
        except BaseException as error:
            save(out('failure'),{'error_type':type(error).__name__,'http_requests':len(calls),'elapsed_seconds':time.monotonic()-started,'retry':False})
            raise
        elapsed=time.monotonic()-started
        assert len(calls)==1
        save(out('reply'),reply.model_dump(mode='json'))
        diagnostic={'provider_status':reply.status,'elapsed_seconds':elapsed,'input_tokens':reply.input_tokens,'output_tokens':reply.output_tokens,'total_tokens':(reply.input_tokens+reply.output_tokens) if reply.input_tokens is not None and reply.output_tokens is not None else None,'http_requests':len(calls),'challenge_executed':False,'generative_calls':1,'embedding_calls':0,'unit_diagnostics':[],'citation_audit':[],'not_a_complete_evaluation_result':True}
        if reply.status=='OK':
            try:
                output=AssessmentBatchOutput.model_validate_json(reply.output_json)
                assert output.batch_id==request.batch_id and {p.unit_id for p in output.units}=={u.unit_id for u in request.units} and len(output.units)==len(request.units)
                save(out('structured_assessment'),output.model_dump(mode='json'))
                refs={}
                def walk(value):
                    if isinstance(value,dict):
                        if 'evidence_id' in value and 'quote' in value:refs[(value['evidence_id'],value['quote'])]=value
                        for child in value.values():walk(child)
                    elif isinstance(value,list):
                        for child in value:walk(child)
                walk(output.model_dump(mode='json'))
                evidence={e.evidence_id:e for e in request.evidence}
                for eid,quote in refs:
                    try:
                        citation=resolve_quote(QuoteRef(evidence_id=eid,quote=quote),evidence)
                        diagnostic['citation_audit'].append({'valid':True,**citation.model_dump(mode='json'),'kind':evidence[eid].kind,'origin':evidence[eid].origin})
                    except ValueError as error:
                        diagnostic['citation_audit'].append({'valid':False,'evidence_id':eid,'quote':quote,'reason':str(error)})
                for proposal in output.units:
                    unit=next(u for u in request.units if u.unit_id==proposal.unit_id)
                    entry={'unit_id':unit.unit_id,'conclusion':proposal.conclusion,'normative':proposal.normative,'lifecycle':proposal.lifecycle,'applicability':proposal.applicability,'evidence_needs':[n.model_dump(mode='json') for n in proposal.needs],'issues':[]}
                    try:
                        validate_proposal(proposal,request,unit)
                        entry['proposal_valid']=True
                        for index,issue in enumerate(proposal.issues):
                            item={'claim':issue.claim,'kind':issue.kind,'core':issue.core.model_dump(mode='json') if issue.core else None,'missing_facts':[f.model_dump(mode='json') for f in issue.missing_facts],'potential_consequences':[c.model_dump(mode='json') for c in issue.potential_consequences]}
                            try:
                                proofs,citations,attribution,eligible=validate_issue(proposal,issue,request,unit,stable_id(unit.unit_id,str(index),issue.claim))
                                item.update(validation_passed=True,attribution=attribution,preliminary_challenge_eligible=eligible,proofs=[p.model_dump(mode='json') for p in proofs],revision_and_core_citations=[c.model_dump(mode='json') for c in citations],disposition='SUPPORTED_CORE_PENDING_INDEPENDENT_CHALLENGE' if eligible else 'NEEDS_REVIEW')
                            except ValueError as error:item.update(validation_passed=False,reason=str(error),disposition='NEEDS_REVIEW')
                            entry['issues'].append(item)
                    except ValueError as error:entry.update(proposal_valid=False,reason=str(error))
                    diagnostic['unit_diagnostics'].append(entry)
            except Exception as error:diagnostic['parse_or_manifest_error_type']=type(error).__name__
        current=read('aggregate_usage.json')
        assert current['calls'][:2]==previous['calls'] and len(current['calls'])==3
        unchanged={n:file_hash(ROOT/n)==h for n,h in protected.items() if n!='aggregate_usage.json'}
        assert all(unchanged.values())
        diagnostic['remaining_tokens']=aggregate.remaining_tokens
        diagnostic['ledger_entry']=current['calls'][-1]
        save(out('diagnostics'),diagnostic)
        save(out('integrity'),{'historical_files_unchanged':unchanged,'historical_ledger_entries_unchanged':True,'appended_entries':1,'old_ledger_sha256':protected['aggregate_usage.json'],'new_ledger_sha256':file_hash(ROOT/'aggregate_usage.json'),'http_requests':1})
        print(json.dumps({k:diagnostic[k] for k in ['provider_status','input_tokens','output_tokens','total_tokens','elapsed_seconds','remaining_tokens']},indent=2))
    finally:
        if client is not None:await client.close()
        lease.unlink()

if __name__=='__main__':asyncio.run(run())
