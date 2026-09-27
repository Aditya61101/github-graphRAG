# github-graphRAG

GraphRAG over code repositories **and** their documentation, stored in Neo4j.

The `codegraph` pipeline builds the code graph with parsers (exact, no LLM),
uses the LLM only for short summaries (cached), and links docs to code with
exact mentions plus vector similarity. The same command handles a new repo
and every later update; only what changed is reprocessed.

## Quick start

```bash
uv sync                      # install
cp .env.example .env         # fill in Neo4j + Gemini (or Azure) settings

uv run codegraph ingest ../claimIQ --repo claimIQ          # first run and every update
uv run codegraph ask "How does login work?" --repo claimIQ
uv run codegraph impact verify_password --repo claimIQ     # what breaks if it changes
uv run codegraph status --repo claimIQ
```

Other commands: `calibrate` (choose `BRIDGE_MIN_SCORE`), `delete-repo`,
`reset-embeddings` (after switching embedding model). `-v` for debug logs.

## Pipeline

| # | Stage | Module | What it does | LLM? |
|---|---|---|---|---|
| 0-1 | Discover | `discover.py` | `git ls-tree` (blob SHA per file) + extension rules | no |
| 2-3 | Sync | `sync.py`, `parse_code.py`, `parse_docs.py` | parse changed files, upsert nodes, prune removed ones | no |
| 4 | Resolve | `resolve.py` | IMPORTS, CALLS, INHERITS, INSTANTIATES | no |
| 5 | Enrich | `enrich.py` | 2-sentence summary per function | yes, cached |
| 6 | Embed | `embed.py` | vectors for function summaries and doc sections | embeddings |
| 7 | Bridge | `bridge.py` | Section -> code: exact MENTIONS + vector RELATES_TO | optional check |
| 8 | Modules | `modules.py` | one Module per directory, summary, DEPENDS_ON, product docs -> modules | yes, cached |
| - | Ready | `sync.mark_repo_ready` | commit pointer moves last | no |

Orchestrated by `pipeline.ingest()`; questions go through `retrieve.ask()`.

### Parsing

- **Python** uses the standard `ast` module: functions, methods, classes,
  imports, decorators, docstrings, calls *with their receiver*
  (`security.verify_password`, `self.save`), simple variable types
  (`repo = UserRepository(db)`), and FastAPI/Flask routes -> `Endpoint` nodes.
- **JS/TS/Java/Go** use tree-sitter (`treesitter-chunker`): functions, classes,
  called names. Calls are resolved by name only (no import map yet).
- **Docs**: Markdown/text by heading (code fences respected), `.pptx` one
  section per slide incl. speaker notes, `.docx` by Heading 1-3. Sections
  longer than ~750 tokens are split by paragraph.

### Graph

```
(Repo)-[:CONTAINS]->(File)-[:DEFINES]->(Class)-[:HAS_METHOD]->(Function)
(File)-[:DEFINES]->(Function)-[:CALLS {confidence, via}]->(Function)
(Function)-[:INSTANTIATES]->(Class)-[:INHERITS]->(Class)
(Function)-[:HANDLES]->(Endpoint)          (File)-[:IMPORTS]->(File)
(File)-[:IN_MODULE]->(Module)-[:DEPENDS_ON {calls}]->(Module)
(Repo)-[:CONTAINS]->(Doc)-[:HAS_SECTION]->(Section)
(Section)-[:MENTIONS]->(Function|Class|Endpoint)
(Section)-[:RELATES_TO {score}]->(Function|Module)
```

IDs are stable (`<repo>:<path>:<Class.method>`), so re-runs update nodes
instead of duplicating them.

### Updates

Run `codegraph ingest` again after a pull/merge (commit your changes first;
it reads the git `HEAD` tree).

- Files are compared by git blob SHA -> added / modified / deleted.
- Inside a modified file, functions are compared by content hash; unchanged
  functions keep their summary, embedding and doc links.
- Calls are re-resolved only for affected files (changed files, their
  importers, and callers of names they define).
- Summaries are cached by code hash + model + prompt version.
- A doc link to a function whose code changed gets `doc_may_be_outdated = true`
  and is flagged in answers and `impact` output.
- Doc sections are compared by content hash; only edited sections are
  re-embedded and re-linked. New code is also matched against unchanged docs.

### Tuning

`BRIDGE_MIN_SCORE` depends on the embedding model. Run
`codegraph calibrate --repo X`, check which section -> function pairs are
correct, and set the score just below the lowest correct one. Add
`--verify-links` to `ingest` to have the LLM confirm each vector link.

## Tests

The end-to-end test runs both flows (new repo, then code + doc changes) with
no network: FalkorDB Lite as an embedded Cypher database, a fake LLM and an
offline hash embedder.

```bash
uv run pytest -s
```

Neo4j-only statements (constraints, vector indexes, vector search) are
replaced in `tests/falkor_store.py`; everything else runs the real Cypher.

## Limitations (known)

- Python has the most precise call resolution; other languages resolve by name.
- Calls through return values (`get_db().query()`) and dynamic dispatch are not resolved.
- Endpoint routes do not include router prefixes (`APIRouter(prefix=...)`).
- Modules are directories (Aura Free has no Graph Data Science plugin for community detection).
- PDFs and Confluence are not ingested yet.

## Legacy pipeline

The earlier LLM-extraction pipeline (`ingestion/`, `run_poc.py`,
`communities_operations.py`, `retrievers/`, `tests/` inside the package) is
left unchanged for reference. `codegraph` does not use it.
