"""Offline controlled assessment preflight. No Azure client or live flag."""
import json
import os
from pathlib import Path
import tiktoken
from dotenv import load_dotenv
from ai_services.decision_guard.budget import ModelTokenCounter, model_context_json
from ai_services.decision_guard.evaluation import EvaluationSettings, UploadAuthority
from ai_services.decision_guard.evaluation.engine import ContextRequestBudget
from ai_services.decision_guard.evaluation.evidence import validate_context, role_shas
from ai_services.decision_guard.evaluation.model_adapter import ASSESS_PROMPT, PROMPT_VERSION, wire_text
from ai_services.decision_guard.evaluation.wire import provider_request_json, expand_source_texts, WireAssessment
from ai_services.decision_guard.models import ArchitectureContext, digest
from ai_services.runs.benchmark_stage_4_1_hybrid_retrieval import file_hash, save
from ai_services.runs.prepare_stage_4_2_live_validation import ROOT, source_for
from ai_services.runs.run_decision_guard_evaluation import ReadOnlyStore

TAG='controlled_preflight_v1'
def load(name):return json.loads((ROOT/name).read_text(encoding='utf-8'))
def main():
    if list(ROOT.glob(f'A_{TAG}_*.json')):raise FileExistsError('Preserve existing checkpoint')
    protected={p.name:file_hash(p) for p in ROOT.glob('*.json')}
    manifest=load('manifest.json')
    assert digest((ROOT/'claimiq_graph_snapshot.json').read_text(encoding='utf-8'))==manifest['snapshot_hash']
    ctx=validate_context(ArchitectureContext.model_validate_json((ROOT/'A_semantic_live_v1_without_semantic_context.json').read_text(encoding='utf-8')))
    plan=load('A_semantic_live_v1_without_semantic_plan.json')
    assert next(c for c in plan['channels'] if c['channel']=='ADR_SEMANTIC')['status']=='DISABLED'
    case=next(c for c in manifest['cases'] if c['id']=='A')
    assert (ctx.revision.base_sha,ctx.revision.head_sha,ctx.revision.merge_base_sha)==(case['base_sha'],case['head_sha'],case['merge_base_sha'])
    source,_=source_for(ctx.revision.repository_id,Path(case['source_root']))
    authority=UploadAuthority(ReadOnlyStore('data/decisionguard.db'),'data/adrs')
    settings=EvaluationSettings.model_validate(manifest['settings'])
    enc=tiktoken.get_encoding('o200k_base')
    counter=ModelTokenCounter('o200k_base:deployment-estimate',lambda t:len(enc.encode(t,disallowed_special=())))
    sizing=ContextRequestBudget(source,authority,settings=settings,token_counter=counter)
    sizing(ctx)
    assert [p.model_dump(mode='json') for p in sizing.last_packets]==load('A_semantic_live_v1_without_semantic_packets.json')
    assert (len(ctx.requirements),len(ctx.entities),len(ctx.relationships),len(sizing.last_packets),sum(len(p.units) for p in sizing.last_packets))==(6,4,3,6,7)
    # Target label only after unchanged production packet construction.
    target=ArchitectureContext.model_validate_json((ROOT/'A_context.json').read_text(encoding='utf-8')).requirements[0].requirement_id
    candidates=[(i,p) for i,p in enumerate(sizing.last_packets) if any(u.rule_id==target for u in p.units)]
    assert len(candidates)==1
    ordinal,request=candidates[0];ev={e.evidence_id:e for e in request.evidence}
    rule=next(r for r in ctx.requirements if r.requirement_id==target)
    assert set(rule.evidence_ids)<=ev.keys() and set(rule.scope_evidence_ids)<=ev.keys()
    assert request.policy_observations and all(o.coverage=='TEXT_COMPLETE' for o in request.policy_observations)
    policy='\n'.join(ev[o.evidence_id].excerpt for o in request.policy_observations)
    assert 'active' in policy.lower() and 'READY_FOR_EVALUATION' in policy
    checks=[]
    for role,sha in role_shas(ctx).items():
        obs=next(o for o in request.observations if o.revision==role and o.path=='backend/app/services/claim_status.py');e=ev[obs.evidence_id]
        assert obs.status=='PRESENT' and obs.complete_file and e.origin==role and e.commit_sha==sha and e.placement=='EXACT'
        assert 'Document.claim_id == claim.id' in e.excerpt and 'statuses = ' in e.excerpt
        assert ('if "VERIFIED" in statuses:' if role=='PR_HEAD' else 'if statuses == {"VERIFIED"}:') in e.excerpt
        checks.append({'revision':role,'sha':sha,'path':e.path,'evidence_id':e.evidence_id,'content_hash':e.content_hash,'complete':True})
    assert any(e.path=='backend/app/models/documents.py' and 'is_active' in e.excerpt for e in request.evidence)
    assert all(e.repository_id==ctx.revision.repository_id for e in request.evidence)
    assert all(r.status=='APPROVED' for r in request.authority)
    wire=provider_request_json(request,'ASSESS');expanded=expand_source_texts(json.loads(wire))
    assert {e['evidence_id']:e['excerpt'] for e in expanded['evidence']}=={e.evidence_id:e.excerpt for e in request.evidence}
    tokens=sizing.count(request,'ASSESS')
    assert sizing.fits(request,'ASSESS') and tokens+settings.max_output_tokens<=settings.model_window
    ledger=load('aggregate_usage.json');charged=sum(c['charged_tokens'] for c in ledger['calls']);remaining=ledger['max_tokens']-charged
    reservation=counter(wire_text(request,'ASSESS'))+512+settings.max_output_tokens
    assert settings.provider_reserve==512 and reservation==tokens+settings.max_output_tokens and reservation<=remaining
    load_dotenv('.env')
    config={'deployment':os.getenv('AZURE_OPENAI_DEPLOYMENT_NAME'),'api_version':os.getenv('AZURE_OPENAI_API_VERSION'),'endpoint_configured':bool(os.getenv('AZURE_OPENAI_ENDPOINT')),'credentials_configured':bool(os.getenv('AZURE_OPENAI_API_KEY'))}
    assert config['deployment']==manifest['deployment'] and config['api_version']==manifest['api_version'] and all(config.values())
    assert ledger['deployment_revision']==f"{config['deployment']}:configured:{config['api_version']}"
    assert PROMPT_VERSION=='dg-semantic-v4' and all(t in ASSESS_PROMPT for t in ['CORE allegation','potential_consequences','CORE uncertainty','EXCEPTION_CLEARANCE','EVIDENCE ROLE CONTRACT'])
    result={'status':'OFFLINE_PREFLIGHT_PASSED_APPROVAL_REQUIRED','provider_calls':0,'embedding_calls':0,'batch_id':request.batch_id,'packet_ordinal':ordinal+1,'units':[u.model_dump(mode='json') for u in request.units],'context_hash':ctx.context_hash,'revision':ctx.revision.model_dump(mode='json'),'context_tokens':counter(model_context_json(ctx)),'all_packet_inputs':[sizing.count(p,'ASSESS') for p in sizing.last_packets],'selected_input_tokens':tokens,'selected_wire_tokens_without_reserve':tokens-512,'output_limit':settings.max_output_tokens,'reservation':reservation,'remaining_after_max_reservation':remaining-reservation,'ledger_charged_tokens':charged,'ledger_remaining_tokens':remaining,'ledger_calls':len(ledger['calls']),'ledger_remaining_attempts':ledger['max_attempts']-len(ledger['calls']),'settings':settings.model_dump(mode='json'),'configuration':config,'deployment_revision':ledger['deployment_revision'],'prompt_version':PROMPT_VERSION,'prompt_sha256':digest(ASSESS_PROMPT),'schema_sha256':digest(json.dumps(WireAssessment.model_json_schema(),sort_keys=True)),'canonical_request_sha256':digest(request.model_dump_json()),'provider_payload_sha256':digest(wire),'estimator_wire_sha256':digest(wire_text(request,'ASSESS')),'source_checks':checks,'policy_observations':[o.model_dump(mode='json') for o in request.policy_observations],'source_anchors':[a.model_dump(mode='json') for a in request.source_anchors],'budget_increase_required':False,'live_execution_not_authorized':True,'fixture_readiness_only':True}
    assert all(file_hash(ROOT/n)==sha for n,sha in protected.items())
    save(ROOT/f'A_{TAG}_request.json',request.model_dump(mode='json'))
    save(ROOT/f'A_{TAG}_provider_payload.json',json.loads(wire))
    save(ROOT/f'A_{TAG}_preflight.json',result)
    save(ROOT/f'A_{TAG}_integrity.json',{'historical_files_unchanged':{n:True for n in protected},'ledger_sha256':file_hash(ROOT/'aggregate_usage.json'),'provider_calls':0})
    print(json.dumps({k:result[k] for k in ['status','batch_id','packet_ordinal','selected_input_tokens','output_limit','reservation','ledger_remaining_tokens','remaining_after_max_reservation']},indent=2))
if __name__=='__main__':main()

