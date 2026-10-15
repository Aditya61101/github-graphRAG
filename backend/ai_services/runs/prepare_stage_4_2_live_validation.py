"""Prepare isolated source-grounded fixtures; never construct a model client.

Run from backend. ClaimIQ input is an archived, read-only graph snapshot.
Readiness below applies only to that frozen fixture, not to the live graph.
Expected results are stored separately and never enter model requests.
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import tiktoken
from dotenv import load_dotenv

from ai_services.decision_guard.budget import ModelTokenCounter
from ai_services.decision_guard.context import ArchitectureContextBuilder
from ai_services.decision_guard.evaluation import EvaluationSettings, UploadAuthority
from ai_services.decision_guard.evaluation.engine import build_units, make_request
from ai_services.decision_guard.evaluation.evidence import (
    SourceSupplementer,
    role_shas,
    validate_context,
)
from ai_services.decision_guard.evaluation.model_adapter import wire_text
from ai_services.decision_guard.models import BaselineSnapshot, digest
from ai_services.decision_guard.ports import Page
from ai_services.decision_guard.settings import ContextSettings
from ai_services.decision_guard.source import GitRevisionSourceReader
from ai_services.ingestion.pr.git_reader import PullRequestGitReader
from ai_services.ingestion.pr.models import PullRequestChangeSet
from ai_services.ingestion.pr.settings import PullRequestSettings
from ai_services.ingestion.sources.github import GitHubRepositorySource
from ai_services.runs.run_decision_guard_evaluation import ReadOnlyStore

ROOT = Path("data/stage_4_2_validation").resolve()
REPO = "repo_1120610241"
BASE = "930a9d2a91447253ef9e2c11835e03c9425a25ce"


def save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def git(root, *args, data=None, env=None):
    return (
        subprocess.run(
            ["git", "-C", str(root), *args],
            input=data,
            capture_output=True,
            check=True,
            timeout=45,
            env=env,
        )
        .stdout.decode("utf-8")
        .strip()
    )


def blob(root, sha, path):
    return subprocess.run(
        ["git", "-C", str(root), "show", sha + ":" + path],
        capture_output=True,
        check=True,
        timeout=30,
    ).stdout.decode("utf-8")


def component_slice(payload, paths, calls):
    """Documented component fixture, preserving exact archived IDs and support."""
    data = dict(payload)
    data["chunks"] = [r for r in payload["chunks"] if r["path"] in paths]
    data["edges"] = [
        r
        for r in payload["edges"]
        if (r["source"]["name"], r["target"]["name"]) in calls
        and r["assertion"]["relationshipType"] == "CALLS"
    ]
    return data


def commit(root, parent, files):
    # Only the isolated clone's object database/index changes; no source refs move.
    env = os.environ.copy()
    env.update(
        GIT_INDEX_FILE=str(root / ".git" / "validation-index"),
        GIT_AUTHOR_NAME="DecisionGuard validation",
        GIT_AUTHOR_EMAIL="validation@example.invalid",
        GIT_COMMITTER_NAME="DecisionGuard validation",
        GIT_COMMITTER_EMAIL="validation@example.invalid",
        GIT_AUTHOR_DATE="2026-10-12T00:00:00Z",
        GIT_COMMITTER_DATE="2026-10-12T00:00:00Z",
    )
    git(root, "read-tree", parent if parent else "--empty", env=env)
    for path, text in files.items():
        oid = git(root, "hash-object", "-w", "--stdin", data=text.encode(), env=env)
        git(
            root,
            "update-index",
            "--add",
            "--cacheinfo",
            f"100644,{oid},{path}",
            env=env,
        )
    tree = git(root, "write-tree", env=env)
    args = ("-p", parent) if parent else ()
    return git(
        root, "commit-tree", tree, *args, data=b"Local validation revision\n", env=env
    )


class FrozenGraph:
    """Replay original graph rows without inventing ClaimIQ associations."""

    def __init__(self, payload, *, rkg_only=False):
        self.payload, self.rkg_only = payload, rkg_only

    @contextmanager
    def read_snapshot(self, repository_id):
        if repository_id != self.payload["repository_id"]:
            raise ValueError("Fixture repository mismatch")
        yield self

    @staticmethod
    def page(rows, limit):
        return Page(tuple(rows[:limit]), len(rows) > limit)

    def file_chunks(self, paths, limit):
        return self.page(
            [r for r in self.payload["chunks"] if r["path"] in paths], limit
        )

    def relationships(self, ids, limit):
        rows = [
            r
            for r in self.payload["edges"]
            if r["source"]["id"] in ids or r["target"]["id"] in ids
        ]
        if self.rkg_only:
            rows = [r for r in rows if r["assertion"].get("source_type") != "ADR"]
        return self.page(rows, limit)

    def adr_catalog(self, limit):
        return self.page([] if self.rkg_only else self.payload["catalog"], limit)

    def requirements(self, ids, limit, **kwargs):
        # Full archived candidate catalog is ranked by the existing builder.
        return self.page([] if self.rkg_only else self.payload["requirements"], limit)

    def communities(self, ids, limit):
        return self.page(
            [r for r in self.payload["communities"] if r["entity"]["id"] in ids], limit
        )

    def applicability_evidence(self, adr_ids, chunk_ids, limit):
        support = self.payload.get("scope_evidence")
        if support is None:
            support = [s for r in self.payload["requirements"] for s in r["support"]]
        rows = [
            s
            for s in support
            if s.get("chunk")
            and s["chunk"]["id"] in chunk_ids
            and s["adr"]["id"] in adr_ids
        ]
        unique = {r["chunk"]["id"]: r for r in rows}
        return self.page(list(unique.values()), limit)


class FixtureReadiness:
    """Frozen fixture identity, explicitly not a production readiness provider."""

    def __init__(self, repo, sha, snapshot_hash, github_id, branch):
        self.state = BaselineSnapshot(
            repository_id=repo,
            github_repository_id=str(github_id),
            tracked_branch=branch,
            indexed_rkg_sha=sha,
            repository_status="COMPLETED",
            readiness="READY",
            graph_generation=int(snapshot_hash[:12], 16),
            ingestion_run_id="isolated-snapshot:" + snapshot_hash,
            unfinished_ingestion=False,
        )

    async def snapshot(self, repository_id):
        if repository_id != self.state.repository_id:
            raise ValueError("Fixture repository mismatch")
        return self.state


def source_for(repo, root):
    source = GitHubRepositorySource(root.parent)
    reader = PullRequestGitReader(source, PullRequestSettings(git_timeout_seconds=30))
    return GitRevisionSourceReader(repo, root, reader), reader


def independent_fixture(root):
    root.mkdir(parents=True, exist_ok=True)
    if not (root / ".git").exists():
        git(root, "init")
    rule = (
        "# Durable payment audit\n\nStatus: Accepted\n\n"
        "Decision: Payment dispatchers must await successful audit persistence before publishing a payment. "
        "The awaited persistence may be composed through Promise.all or an equivalent awaited adapter. "
        "Metrics and telemetry are best-effort and exempt from this payment ordering rule.\n\n"
        "Rejected alternative: Publish payments without waiting for audit persistence.\n"
    )
    paths = {
        "payments/send.mjs": "export async function send(payment, appendAudit, publish) {\n  await appendAudit(payment);\n  return publish(payment);\n}\n",
        "payments/batch.mjs": "export async function send(payment, appendAudit, publish) {\n  await appendAudit(payment);\n  return publish(payment);\n}\n",
        "payments/archive.mjs": "export async function send(payment, appendAudit, publish) {\n  appendAudit(payment);\n  return publish(payment);\n}\n",
        "telemetry/send.mjs": "export async function send(metric, appendAudit, publish) {\n  await appendAudit(metric);\n  return publish(metric);\n}\n",
        "payments/forward.mjs": "export async function send(payment, appendAudit, publish, relay) {\n  await appendAudit(payment);\n  return publish(payment);\n}\n",
    }
    changed = dict(paths)
    changed["payments/send.mjs"] = paths["payments/send.mjs"].replace(
        "await appendAudit", "appendAudit"
    )
    changed["payments/batch.mjs"] = paths["payments/batch.mjs"].replace(
        "await appendAudit(payment)", "await Promise.all([appendAudit(payment)])"
    )
    changed["payments/archive.mjs"] = paths["payments/archive.mjs"].replace(
        "  appendAudit(payment);", "  const event = payment;\n  appendAudit(event);"
    )
    changed["telemetry/send.mjs"] = paths["telemetry/send.mjs"].replace(
        "await appendAudit", "appendAudit"
    )
    changed["payments/forward.mjs"] = paths["payments/forward.mjs"].replace(
        "await appendAudit(payment)", "await relay(payment)"
    )
    names = [
        "dispatchPayment",
        "dispatchBatch",
        "dispatchArchived",
        "reportMetric",
        "dispatchForward",
    ]
    before = "\n".join(
        text.replace("function send(", "function " + name + "(")
        for text, name in zip(paths.values(), names, strict=True)
    )
    after = "\n".join(
        text.replace("function send(", "function " + name + "(")
        for text, name in zip(changed.values(), names, strict=True)
    )
    paths = {"dispatch.mjs": before}
    changed = {"dispatch.mjs": after}
    base = commit(root, None, paths)
    head = commit(root, base, changed)
    repo, adr = "fixture_durable_payments", "fixture_audit_adr"
    doc_path = ROOT / "fixture_adrs" / repo / adr / "original.md"
    doc_path.parent.mkdir(parents=True, exist_ok=True)
    doc_path.write_bytes(rule.encode("utf-8"))
    document = {
        "id": adr,
        "repository": repo,
        "content_hash": digest(rule),
        "source_name": "audit.md",
        "source_type": "FIXTURE",
    }
    policy_chunk = {
        "id": "fixture:original-audit-rule",
        "repository": repo,
        "adr_id": adr,
        "text": rule,
        "contentHash": digest(rule),
        "section": "Decision",
    }
    rows = []
    for path, text in paths.items():
        ent = {
            "id": "module:" + path,
            "repository": repo,
            "name": path,
            "label": "Module",
            "aliases": [],
        }
        ch = {
            "id": "chunk:" + path,
            "repository": repo,
            "commit": base,
            "filePath": path,
            "text": text,
            "contentHash": digest(text),
        }
        rows.append({"path": path, "chunk": ch, "entities": [ent]})
    payload = {
        "repository_id": repo,
        "catalog": [{"adr": document}],
        "chunks": rows,
        "edges": [],
        "communities": [],
        "requirements": [
            {
                "kind": "CONSTRAINT",
                "knowledge": {
                    "id": "fixture-audit-order",
                    "repository": repo,
                    "description": "Payment dispatchers await successful audit persistence before publishing; telemetry exempt.",
                },
                "targets": [],
                "support": [{"adr": document, "chunk": policy_chunk}],
            }
        ],
    }
    record = {
        "id": adr,
        "repository_id": repo,
        "content_hash": digest(rule),
        "status": "COMPLETED",
        "file_path": str(doc_path),
    }
    save(
        ROOT / "independent_authority.json",
        {"repository": {"id": repo, "user_id": "fixture-owner"}, "adrs": [record]},
    )
    save(ROOT / "independent_graph_snapshot.json", payload)
    return payload, base, head


class FixtureStore:
    def __init__(self, path):
        self.data = json.loads(Path(path).read_text(encoding="utf-8"))

    def get_repository(self, identity):
        row = self.data["repository"]
        return SimpleNamespace(**row) if row["id"] == identity else None

    def get_adr(self, identity):
        return next(
            (SimpleNamespace(**r) for r in self.data["adrs"] if r["id"] == identity),
            None,
        )


def verify_behavior(cases):
    """Execute isolated original functions, never import/run ClaimIQ services."""
    result = {}
    for name, _, _, root, base, head, _, _, _ in cases:
        if name in {"A", "B"}:
            outcomes = {}
            for role, sha in (("BASE", base), ("HEAD", head)):
                tree = ast.parse(
                    blob(root, sha, "backend/app/services/claim_status.py")
                )
                fn = next(
                    n
                    for n in tree.body
                    if isinstance(n, ast.FunctionDef)
                    and n.name == "derive_processing_status"
                )
                ns = {
                    "Claim": object,
                    "Document": SimpleNamespace(claim_id=0),
                    "Session": object,
                    "BLOCKING_STATUS": {
                        "EXTRACTION_FAILED",
                        "VALIDATION_FAILED",
                        "VERIFICATION_FAILED",
                    },
                    "build_action_required_payload": lambda docs: {"blocking": True},
                }
                exec(  # noqa: S102 -- isolated local Git function, no service imports
                    compile(
                        ast.Module(body=[fn], type_ignores=[]),
                        "isolated-source",
                        "exec",
                    ),
                    ns,
                )
                outcomes[role] = {}
                for statuses in (
                    [],
                    ["VERIFIED"],
                    ["VERIFIED", "UPLOADED"],
                    ["VERIFIED", "VALIDATION_FAILED"],
                ):
                    docs = [SimpleNamespace(status=s) for s in statuses]
                    query = SimpleNamespace(all=lambda items=docs: items)
                    query.filter = lambda _, q=query: q
                    db = SimpleNamespace(query=lambda _, q=query: q)
                    outcomes[role][str(statuses)] = ns["derive_processing_status"](
                        SimpleNamespace(id=1), db
                    )[0]
            assert outcomes["BASE"]["['VERIFIED', 'UPLOADED']"] == "DOCUMENT_PROCESSING"
            assert outcomes["HEAD"]["['VERIFIED', 'UPLOADED']"] == (
                "READY_FOR_EVALUATION" if name == "A" else "DOCUMENT_PROCESSING"
            )
            result[name] = outcomes
        elif name == "C":
            outcomes = {}
            for role, sha in (("BASE", base), ("HEAD", head)):
                tree = ast.parse(
                    blob(
                        root, sha, "backend/app/extraction/structured/extract_router.py"
                    )
                )
                fn = next(
                    n
                    for n in tree.body
                    if isinstance(n, ast.FunctionDef)
                    and n.name == "extract_structured_data"
                )
                ns = {
                    "DocumentTypes": SimpleNamespace(
                        BILLS="bills", PRESCRIPTIONS="prescriptions", REPORTS="reports"
                    ),
                    "extract_bill_data": lambda *args: {"parsed": True},
                }
                exec(  # noqa: S102 -- isolated local Git function, no service imports
                    compile(
                        ast.Module(body=[fn], type_ignores=[]),
                        "isolated-source",
                        "exec",
                    ),
                    ns,
                )
                try:
                    ns["extract_structured_data"](
                        document_type="bills", claim_type="medical", text="sample"
                    )
                    outcomes[role] = "compatible keyword call"
                except TypeError as exc:
                    outcomes[role] = str(exc)
            assert (
                outcomes["BASE"] == "compatible keyword call"
                and "strict" in outcomes["HEAD"]
            )
            result[name] = outcomes
        else:
            sources = {
                "BASE": blob(root, base, "dispatch.mjs"),
                "HEAD": blob(root, head, "dispatch.mjs"),
            }
            script = """let raw=''; for await (const chunk of process.stdin) raw+=chunk;
const sources=JSON.parse(raw), result={};
for (const [role,source] of Object.entries(sources)) {
 const module=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64')); result[role]={};
 for (const name of ['dispatchPayment','dispatchBatch','dispatchArchived','reportMetric']) {
  const events=[];
  const appendAudit=async value=>{await new Promise(resolve=>setTimeout(resolve,2));events.push('audit');};
  const publish=value=>{events.push('publish');return value;};
  await module[name]({id:1},appendAudit,publish); await new Promise(resolve=>setTimeout(resolve,5));
  result[role][name]=events;
 }
}
console.log(JSON.stringify(result));"""
            output = subprocess.run(
                ["node", "--input-type=module", "-e", script],
                input=json.dumps(sources).encode(),
                capture_output=True,
                check=True,
                timeout=15,
            )
            outcomes = json.loads(output.stdout)
            assert outcomes["BASE"]["dispatchPayment"] == ["audit", "publish"]
            assert outcomes["HEAD"]["dispatchPayment"] == ["publish", "audit"]
            assert outcomes["HEAD"]["dispatchBatch"] == ["audit", "publish"]
            assert (
                outcomes["BASE"]["dispatchArchived"]
                == outcomes["HEAD"]["dispatchArchived"]
                == ["publish", "audit"]
            )
            result[name] = outcomes
    save(ROOT / "behavior_checks.json", result)


async def prepare():
    load_dotenv(".env")
    snapshot_file = ROOT / "claimiq_graph_snapshot.json"
    payload = json.loads(snapshot_file.read_text(encoding="utf-8"))
    clone = ROOT / "clones" / "claimiq"
    clone.parent.mkdir(parents=True, exist_ok=True)
    live = Path("data/repos/Aditya61101/claimIQ").resolve()
    if not clone.exists():
        await asyncio.to_thread(
            subprocess.run,
            ["git", "clone", "--no-hardlinks", "--no-checkout", str(live), str(clone)],
            capture_output=True,
            check=True,
            timeout=60,
        )
    path = "backend/app/services/claim_status.py"
    old = blob(clone, BASE, path)
    needle = 'if statuses == {"VERIFIED"}:'
    assert old.count(needle) == 1
    a = commit(clone, BASE, {path: old.replace(needle, 'if "VERIFIED" in statuses:')})
    b = commit(
        clone,
        BASE,
        {
            path: old.replace(
                needle, 'if all(doc.status == "VERIFIED" for doc in documents):'
            )
        },
    )
    router = "backend/app/extraction/structured/extract_router.py"
    old_router = blob(clone, BASE, router)
    assert old_router.count("    text: str\n") == 1
    c = commit(
        clone,
        BASE,
        {
            router: old_router.replace(
                "    text: str\n", "    text: str,\n    *,\n    strict: bool\n"
            )
        },
    )
    independent_root = ROOT / "clones" / "payments"
    independent, dbase, dhead = independent_fixture(independent_root)
    status_data = component_slice(
        payload,
        {path, "backend/app/orchestration/claim/claim_executor.py"},
        {
            ("update_claim_processing_status", "derive_processing_status"),
            ("update_claim_processing_status", "invoke_claim_graph"),
        },
    )
    # A single-rule fixture, not an assessment of the full repository catalog.
    # Preserve original rows/IDs and scope evidence for the selected requirement.
    status_data["requirements"] = [
        r
        for r in payload["requirements"]
        if r["knowledge"]["id"]
        == "b9652205302af1fa53bddf46fcaa7c7fa0aecb07138504891a105f2ead97a31a"
    ]
    assert len(status_data["requirements"]) == 1
    status_data["chunks"] = [
        r
        for r in payload["chunks"]
        if r["path"] == path
        and any(e["name"] == "derive_processing_status" for e in r["entities"])
    ]
    status_data["chunks"] = [
        {
            **r,
            "entities": [
                e for e in r["entities"] if e["name"] == "derive_processing_status"
            ],
        }
        for r in status_data["chunks"]
    ]
    status_data["edges"] = []
    status_data["scope_evidence"] = [
        s for r in payload["requirements"] for s in r["support"] if s.get("chunk")
    ]
    extraction_data = component_slice(
        payload,
        {router, "backend/app/services/document_extraction.py"},
        {("extract_document", "extract_structured_data")},
    )
    extraction_data["chunks"] = [
        {
            **r,
            "entities": [
                e
                for e in r["entities"]
                if e["name"] in {"extract_document", "extract_structured_data"}
            ],
        }
        for r in extraction_data["chunks"]
    ]
    save(ROOT / "status_component_snapshot.json", status_data)
    save(ROOT / "extraction_component_snapshot.json", extraction_data)
    cases = [
        ("A", REPO, status_data, clone, BASE, a, False, 1120610241, "main"),
        ("B", REPO, status_data, clone, BASE, b, False, 1120610241, "main"),
        ("C", REPO, extraction_data, clone, BASE, c, True, 1120610241, "main"),
        (
            "D",
            "fixture_durable_payments",
            independent,
            independent_root,
            dbase,
            dhead,
            False,
            900001,
            "fixture",
        ),
    ]
    enc = tiktoken.get_encoding("o200k_base")
    counter = ModelTokenCounter(
        "o200k_base:deployment-estimate",
        lambda t: len(enc.encode(t, disallowed_special=())),
    )
    settings = EvaluationSettings(
        concurrency=1,
        retries=0,
        max_input_tokens=20000,
        max_output_tokens=5000,
        call_timeout_seconds=90,
    )
    manifest = {
        "deployment": os.environ["AZURE_OPENAI_DEPLOYMENT_NAME"],
        "api_version": os.environ["AZURE_OPENAI_API_VERSION"],
        "aggregate_max_attempts": 8,
        "aggregate_max_tokens": 60000,
        "concurrency": 1,
        "real_calls": 0,
        "execution_order": ["A", "D", "B", "C"],
        "settings": settings.model_dump(),
        "snapshot_hash": digest(snapshot_file.read_text()),
        "cases": [],
    }
    for name, repo, data, root, base, head, rkg_only, github_id, branch in cases:
        source, reader = source_for(repo, root)
        change_set = PullRequestChangeSet(
            installation_id=169465177 if repo == REPO else 900001,
            github_repository_id=github_id,
            repository_full_name="Aditya61101/claimIQ"
            if repo == REPO
            else "fixture/payments",
            repository_private=False,
            pull_request_number=1,
            title="Local source revision",
            body=None,
            base_branch=branch,
            head_branch="isolated",
            base_sha=base,
            head_sha=head,
            base_repository_id=github_id,
            source_event="opened",
            repository_id=repo,
            merge_base_sha=git(root, "merge-base", base, head),
            files=reader.build_file_changes(root, base, head),
        )
        builder = ArchitectureContextBuilder(
            FrozenGraph(data, rkg_only=rkg_only),
            source,
            FixtureReadiness(
                repo, base, digest(json.dumps(data, sort_keys=True)), github_id, branch
            ),
            lambda _: asyncio.Lock(),
            ContextSettings(
                max_decisions=4, max_constraints=8, max_context_tokens=24000
            ),
            token_counter=counter,
        )
        ctx = validate_context(await builder.build(change_set))
        authority = (
            UploadAuthority(ReadOnlyStore("data/decisionguard.db"), "data/adrs")
            if repo == REPO
            else UploadAuthority(
                FixtureStore(ROOT / "independent_authority.json"), ROOT / "fixture_adrs"
            )
        )
        receipts = authority.receipts(ctx)
        assert all(r.status == "APPROVED" for r in receipts)
        if rkg_only:
            assert not ctx.requirements and not ctx.adr_snapshot
        supplement = SourceSupplementer(source, settings)
        paths = {d.path for d in ctx.deltas} | {
            e.path for e in ctx.evidence if e.kind == "CODE" and e.path
        }
        for p in sorted(paths):
            for role in role_shas(ctx):
                await supplement.read(ctx, p, role)
        units, overflow = build_units(ctx, settings.max_units)
        batches, current = [], []
        for unit in units:
            req = make_request(ctx, (*current, unit), supplement.bundle(), receipts)
            if current and (
                len(current) >= settings.max_batch_units
                or counter(wire_text(req, "ASSESS")) + 512 > settings.max_input_tokens
            ):
                batches.append(
                    make_request(ctx, current, supplement.bundle(), receipts)
                )
                current = []
            single = make_request(ctx, (unit,), supplement.bundle(), receipts)
            if counter(wire_text(single, "ASSESS")) + 512 > settings.max_input_tokens:
                raise ValueError(
                    "Indivisible dry-run packet too large: "
                    + name
                    + " tokens="
                    + str(counter(wire_text(single, "ASSESS")))
                    + " paths="
                    + str(paths)
                )
            current.append(unit)
        if current:
            batches.append(make_request(ctx, current, supplement.bundle(), receipts))
        save(ROOT / (name + "_changes.json"), change_set.model_dump(mode="json"))
        save(ROOT / (name + "_context.json"), ctx.model_dump(mode="json"))
        for i, request in enumerate(batches):
            save(
                ROOT / (name + f"_assessment_{i}.json"), request.model_dump(mode="json")
            )
        info = {
            "id": name,
            "repository_id": repo,
            "source_root": str(root),
            "base_sha": base,
            "merge_base_sha": change_set.merge_base_sha,
            "head_sha": head,
            "context_hash": ctx.context_hash,
            "rkg_only": rkg_only,
            "context_kind": "Stage 4.1 builder over frozen source-grounded fixture",
            "units": len(units),
            "overflow": overflow,
            "assessment_batches": len(batches),
            "estimated_input_tokens": [
                counter(wire_text(r, "ASSESS")) + 512 for r in batches
            ],
            "authority": [r.model_dump() for r in receipts],
            "requirements": [
                {"id": r.requirement_id, "statement": r.statement}
                for r in ctx.requirements
            ],
            "limitations": [l.code for l in ctx.coverage.limitations],
        }
        manifest["cases"].append(info)
        print(json.dumps(info, indent=2))
    save(ROOT / "manifest.json", manifest)
    await asyncio.to_thread(verify_behavior, cases)
    save(
        ROOT / "expected_outcomes.json",
        {
            "A": "Introduced ADR conflict: VERIFIED+UPLOADED must not trigger readiness; changed guard does.",
            "B": "No introduced conflict for equivalent all-documents guard; existing unrelated issues remain pre-existing.",
            "C": "RKG-only contract regression: existing keyword caller omits newly required strict argument.",
            "D": {
                "dispatchPayment": "introduced ordering conflict",
                "dispatchBatch": "compliant awaited composition",
                "dispatchArchived": "pre-existing ordering issue, not introduced",
                "reportMetric": "explicitly exempt",
                "dispatchForward": "unknown relay contract; abstain from confirmed violation",
            },
        },
    )


if __name__ == "__main__":
    asyncio.run(prepare())
