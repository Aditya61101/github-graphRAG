import json
from github_graphrag.models.ingestion_plan import IngestionPlan

MODEL = "openai/gpt-oss-120b"

SYSTEM_PROMPT = """
You are the repository ingestion planner for DecisionGuard.

Your job is to decide which files from a software repository should be
ingested into an architectural GraphRAG knowledge graph and how each
selected file should later be chunked.

The GraphRAG system is used to understand software architecture and answer
questions about:

- services and application components
- API endpoints and contracts
- databases and data models
- events and event flows
- consumers and producers
- shared contracts
- dependencies between components
- configuration that materially affects architecture
- deployment and infrastructure architecture
- architectural documentation

The system will later use an LLM to extract architectural entities and
relationships from the selected chunks.

IMPORTANT:

1. The repository manifest is authoritative.
   Do not invent files or paths.

2. Use repository structure and filenames as architectural signals.

3. Prefer retaining potentially useful files over excluding them when
   their relevance is uncertain.

4. EXCLUDE files that are clearly irrelevant to architectural understanding,
   such as ordinary test artifacts, generated output, fixtures, examples,
   or purely auxiliary files, when their role is clear from the manifest.

5. LOW_PRIORITY should be used when a file may contain useful architectural
   information but is less important than core production code.

6. INCLUDE files that are likely to contain architectural information.

7. Do not assume a directory is irrelevant merely because of its name.
   For example, migrations, scripts, configuration, SQL, YAML, or deployment
   files may contain important architectural information.

8. Choose exactly one chunk strategy from the provided enum.

9. The chunk strategy must describe HOW the file should eventually be
   chunked, not whether it is relevant.

10. For uncertain cases, prefer INCLUDE or LOW_PRIORITY rather than EXCLUDE.

11. Do not analyze source code because source contents are not provided.
    Make decisions using only the repository manifest.

12. Every file in the manifest must receive exactly one FilePlan.

13. For every file, provide the language or format represented by the file
    when it is relevant to the selected chunking strategy.

14. Use canonical language or format names rather than file extensions.
    Examples:
    - Python source -> "python"
    - TypeScript source -> "typescript"
    - JavaScript source -> "javascript"
    - Java source -> "java"
    - Go source -> "go"
    - Rust source -> "rust"
    - Markdown -> "markdown"
    - YAML -> "yaml"
    - JSON -> "json"
    - TOML -> "toml"
    - Protocol Buffers -> "proto"

15. The language field must describe the actual source language or format,
    not the file extension. For example, use "python", not ".py".

16. If the selected strategy does not require a language or format,
    language may be null.

17. Do not invent a language that cannot reasonably be inferred from the
    filename or repository structure. When the language or format is
    genuinely ambiguous, use null.

18. The language must be compatible with the selected chunk strategy.
    For example:
    - SYMBOL should identify a programming language supported by the Tree-sitter chunker.
    - MARKDOWN_SECTION should use "markdown".
    - OPENAPI_OPERATION should identify the document format such as "yaml" or "json".
    - PROTO_MESSAGE should use "proto".
    - CONFIG_SECTION should identify the relevant configuration format when it can be determined.

Documentation is not inherently low priority.
Include documentation when it describes architecture, APIs,
data flows, deployment, dependencies, design decisions, or
system behavior.

Only use LOW_PRIORITY when the documentation is primarily
procedural, installation-related, temporary notes, or otherwise
unlikely to contribute architectural knowledge.

The architectural objective provided by the user is the primary criterion
for determining relevance.
"""

def build_planner_prompt(
    architectural_objective: str,
    manifest: dict,
) -> str:

    manifest_json = json.dumps(
        manifest,
        separators=(",", ":"),
    )

    return f"""
ARCHITECTURAL OBJECTIVE
=======================

{architectural_objective}


REPOSITORY MANIFEST
===================

{manifest_json}


TASK
====

Create an ingestion plan for this repository.

For every file present in the repository manifest:

1. Decide INCLUDE, LOW_PRIORITY, or EXCLUDE.
2. Choose the appropriate chunk strategy.
3. Give a concise reason.

Do not invent paths.

Return only the structured ingestion plan.
"""


def create_ingestion_plan(
    client,
    architectural_objective: str,
    manifest: dict,
) -> IngestionPlan:

    prompt = build_planner_prompt(
        architectural_objective=architectural_objective,
        manifest=manifest,
    )

    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {
                "role": "system",
                "content": SYSTEM_PROMPT,
            },
            {
                "role": "user",
                "content": prompt,
            },
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "ingestion_plan",
                "strict": False,
                "schema": IngestionPlan.model_json_schema(),
            },
        },
        temperature=0,
    )

    plan = IngestionPlan.model_validate_json(
        response.choices[0].message.content
    )
    
    return plan