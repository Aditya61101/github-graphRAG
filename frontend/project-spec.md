# Decision Guard — Frontend Product Specification

## 1. Product Overview

Decision Guard is a project-centric knowledge and decision intelligence platform built around a GitHub repository.

The system ingests a repository, extracts meaningful code and architectural relationships, builds a knowledge graph in Neo4j, and exposes that knowledge through Graph-RAG powered exploration.

The frontend should make the lifecycle of that knowledge clear:

```text
GitHub Repository
      ↓
   Ingestion
      ↓
Knowledge Extraction
      ↓
 Graph Construction
      ↓
     Neo4j
      ↓
   Graph-RAG
      ↓
 Ask / Graph Exploration
```

The frontend must be designed around **projects**, not individual repositories or source providers.

---

## 2. Core Product Concept

A **Project** is the primary workspace in Decision Guard.

Each project contains:

- Exactly one GitHub repository.
- One or more knowledge sources.
- An ingestion lifecycle.
- A knowledge graph.
- Graph-RAG capabilities.
- Exploration interfaces such as Ask and Graph.

GitHub is mandatory because it is the primary source of technical knowledge.

Additional sources such as Google Drive and Confluence may later enrich the same project knowledge graph.

The frontend should therefore treat GitHub, Google Drive, Confluence, and future providers as different **source types**, rather than separate product areas.

---

## 3. GitHub Requirements

GitHub is mandatory for the MVP.

Authentication is performed using GitHub OAuth.

A user may have multiple Decision Guard projects.

Each project has exactly one GitHub repository.

A GitHub repository cannot belong to multiple Decision Guard projects.

Conceptually:

```text
User
 ├── Project A
 │    └── GitHub Repository A
 │
 ├── Project B
 │    └── GitHub Repository B
 │
 └── Project C
      └── GitHub Repository C
```

The repository-selection UI should prevent or clearly communicate repository uniqueness constraints.

---

## 4. Project Lifecycle

The primary lifecycle is:

```text
GitHub OAuth
     ↓
Projects Dashboard
     ↓
Create Project Drawer
     ↓
Select GitHub Repository
     ↓
Create Project
     ↓
Initial Ingestion
     ↓
Knowledge Extraction
     ↓
Graph Construction
     ↓
Neo4j
     ↓
Graph-RAG Ready
     ↓
Project Overview
     ↓
Explore
 ├── Ask
 └── Graph
```

The UI should make these state transitions visible without requiring the user to understand the backend implementation.

---

## 5. Information Architecture

The product should use a project-centric information architecture.

Recommended primary areas:

```text
Projects

Current Project
├── Overview
├── Sources
├── Ingestion
└── Explore
    ├── Ask
    └── Graph

Settings
```

Projects are global navigation.

Everything below **Current Project** is scoped to the selected project.

---

## 6. Main Pages

The recommended primary full-page surfaces are:

1. Projects Dashboard
2. Project Overview
3. Sources
4. Ingestion
5. Explore — Ask
6. Explore — Graph

Not every interaction should become a page. Details and focused operations should generally use drawers or modals.

---

## 7. Projects Dashboard

Route:

```text
/projects
```

Purpose:

- Show all Decision Guard projects owned by the user.
- Provide project discovery.
- Provide project creation.
- Show high-level project health/status.

Possible project-card information:

- Project name.
- GitHub repository.
- Last ingestion.
- Source count.
- Graph status.
- Ingestion status.
- Last activity.

Primary action:

```text
+ Create Project
```

Project creation should open a drawer/offcanvas rather than navigating to a separate page.

---

## 8. Create Project

Create Project should be implemented as a drawer/offcanvas from the Projects Dashboard.

The MVP flow should be approximately:

```text
Project Name

GitHub Repository
[ Select Repository ]

[ Create Project ]
```

After creation:

```text
Project Created
      ↓
Initial Ingestion
      ↓
Processing
      ↓
Graph Construction
      ↓
Graph-RAG Ready
```

The drawer should not become an unnecessarily complex wizard.

Future source connections should not be forced into the initial project creation flow.

---

## 9. Project Overview

Route:

```text
/projects/:projectId
```

The Project Overview is an operational dashboard for the current project.

It should provide a quick understanding of:

- Project identity.
- Connected sources.
- GitHub repository.
- Current source status.
- Current ingestion status.
- Current Graph-RAG status.
- Graph statistics.
- Recent activity.
- Quick access to Ask.

Example conceptual layout:

```text
Project Name
GitHub: organization/repository

Sources
┌────────────────────────────┐
│ GitHub             ✓ Synced│
└────────────────────────────┘

Knowledge
Nodes: 12,430
Relationships: 28,920

Graph-RAG
✓ Ready

Recent Activity
...

[ Ask a Question ]
```

---

## 10. Sources

Route:

```text
/projects/:projectId/sources
```

Sources are project-scoped.

GitHub must always be present for an MVP project.

The page should eventually support additional source providers without changing the overall information architecture.

Conceptual structure:

```text
Sources

Connected Sources
├── GitHub
│   └── repository
│
└── Future connected sources

Additional Sources
├── Google Drive
├── Confluence
└── Future providers
```

Providers should not become top-level navigation items.

---

## 11. Google Drive — Future Requirement

Google Drive is future scope and should **not** be implemented as part of the current MVP.

The frontend architecture should nevertheless allow it to be added cleanly later.

Desired future behavior:

```text
Connect Google Drive
       ↓
Google OAuth
       ↓
Choose Folder
       ↓
Confirm Source
       ↓
Initial Sync
       ↓
Source Ingestion
       ↓
Graph Update
```

The conceptual user requirement is:

> Connect a Drive folder and everything inside it becomes part of the project's knowledge.

The UI should not require users to individually select files inside the connected folder.

The connected folder becomes a project-scoped source.

Future periodic synchronization should detect changes inside the folder and update the project knowledge accordingly.

---

## 12. Confluence — Future Requirement

Confluence is also future scope and should not be implemented in the MVP.

The conceptual future flow is:

```text
Connect Confluence
       ↓
Authenticate
       ↓
Choose Space / Page
       ↓
Confirm Source
       ↓
Initial Sync
       ↓
Source Ingestion
       ↓
Graph Update
```

The same source architecture used for GitHub and Google Drive should support Confluence.

---

## 13. Why External Sources Exist

External sources exist to provide context that may not exist in the repository itself.

Examples include:

- Architecture documentation.
- Architectural decision records.
- Technical specifications.
- Design documents.
- Product or system requirements.
- Operational documentation.
- Other technical knowledge.

The goal is not to create independent knowledge bases.

All source types should enrich the same project knowledge graph.

For example:

```text
PaymentService
     ├── implemented_by → GitHub source
     ├── documented_in  → Architecture document
     └── specified_by   → Confluence page
```

This enables Graph-RAG to answer questions using information across different sources.

---

## 14. Ingestion

Route:

```text
/projects/:projectId/ingestion
```

Ingestion represents the process of fetching, processing, extracting, and incorporating source material into the project's knowledge graph.

For GitHub:

```text
Repository
    ↓
Fetch
    ↓
Process Files
    ↓
Extract Knowledge
    ↓
Build Graph
    ↓
Update Graph-RAG
```

The frontend should distinguish source synchronization from knowledge-graph readiness.

---

## 15. Ingestion Page

The Ingestion page should show both the current ingestion state and Graph-RAG state.

It should include:

- Current ingestion state.
- Current Graph-RAG state.
- Source synchronization statuses.
- Last successful ingestion.
- Current/active ingestion.
- Ingestion history.
- Errors or warnings.

Example:

```text
Current Ingestion

GitHub       ● Synced
Extraction   ✓ Complete
Graph        ● Building
Graph-RAG    ◐ Updating
```

Once complete:

```text
GitHub       ✓ Synced
Extraction   ✓ Complete
Graph        ✓ Complete
Graph-RAG    ✓ Ready
```

---

## 16. Ingestion Details

Ingestion details should generally be displayed in a drawer rather than a dedicated page.

Example:

```text
Ingestion #18

Sources
✓ GitHub

Files
24 processed
2 skipped

Extraction
✓ Complete

Graph Construction
✓ Complete

Graph-RAG
✓ Ready

Errors
None
```

This preserves the user's current page context.

---

## 17. Ingestion State Model

The frontend should support clear processing states.

Recommended states:

```text
Not Connected
Connecting
Connected
Syncing
Queued
Processing
Extracting
Building Graph
Updating Graph-RAG
Ready
Partial
Failed
```

Not every state must be visible in every UI component, but the design system should support them consistently.

---

## 18. External Source Updates

When future sources are added, source synchronization and Graph-RAG readiness should remain separate concepts.

Example:

```text
GitHub       ● Synced
Google Drive ● Syncing
Graph-RAG    ◐ Updating
```

After processing:

```text
GitHub       ✓ Synced
Google Drive ✓ Synced
Graph-RAG    ✓ Ready
```

This distinction is important because a source can be synchronized while the knowledge graph is still being updated.

---

## 19. Explore

Explore is the user-facing knowledge-consumption area.

It contains two primary experiences:

```text
Explore
├── Ask
└── Graph
```

Ask and Graph should operate on the same underlying project knowledge.

They should also be interconnected so that users can move from conversational answers to graph exploration and vice versa.

---

## 20. Explore — Ask

Route:

```text
/projects/:projectId/explore/ask
```

Ask is the conversational interface for Graph-RAG.

Users should be able to ask questions about:

- Code.
- Architecture.
- Services.
- Dependencies.
- Relationships.
- Project structure.
- Design decisions.
- Documentation.
- Technical specifications.
- Cross-source relationships.

The answer should expose supporting provenance wherever practical.

Example:

```text
User:
Why does PaymentService depend on TransactionRepository?

Answer:
...

Sources
├── payment/service.py
├── transaction/repository.py
└── architecture/payments.md
```

---

## 21. Graph-RAG Answer Provenance

Answers should not appear as unsupported AI-generated text.

The UI should provide a way to inspect the evidence behind an answer.

Possible provenance information:

- GitHub file.
- Code location.
- Documentation file.
- Google Drive document.
- Confluence page.
- Graph entity.
- Relationship.

Selecting a source reference can open a Source Preview drawer without leaving the Ask page.

---

## 22. Source Preview

Source previews should generally use a drawer.

The user should be able to inspect the supporting source while retaining the current Ask conversation.

Example:

```text
Ask Page
   │
   └── Source Reference clicked
           ↓
      Source Preview Drawer
```

The drawer may show:

- Source name.
- Provider.
- Path/page/folder.
- Relevant content.
- Relevant code section.
- Metadata.
- Relationship to the graph entity.

---

## 23. Explore — Graph

Route:

```text
/projects/:projectId/explore/graph
```

The Graph page provides an interactive visualization of the Neo4j knowledge graph.

The graph should be dynamically generated from the project's graph data.

The page should allow users to:

- Explore nodes.
- Explore relationships.
- Zoom and pan.
- Filter or focus the graph where appropriate.
- Select nodes.
- Inspect node details.
- Move from graph entities into Ask.

---

## 24. Graph Node Interaction

Clicking a graph node should generally open a Node Details drawer.

Example:

```text
Node Details

PaymentService

Type
Service

Properties
...

Relationships
├── depends_on → TransactionRepository
├── calls → PaymentGateway
└── documented_in → payments.md

[ Ask about this node ]
```

The user should be able to transition directly from a node to a contextual Ask query.

---

## 25. Relationship Between Ask and Graph

Ask and Graph are two views over the same knowledge system.

They should be connected conceptually and through UI actions.

Example:

```text
Ask
 │
 ├── Source reference → Source Preview
 │
 └── Graph entity → Graph

Graph
 │
 ├── Node → Node Details
 │
 └── Ask about node → Ask
```

This prevents the product from feeling like two unrelated tools.

---

## 26. Sidebar Design

Recommended sidebar:

```text
Decision Guard

Projects
├── Browse
└── Recent

────────────────────

Current Project
├── Overview
├── Sources
├── Ingestion
└── Explore
    ├── Ask
    └── Graph

────────────────────

Settings
```

`Projects` is global.

`Current Project` contains navigation within the selected project.

The sidebar should remain compact and avoid unnecessary nesting.

---

## 27. Sidebar Principles

Do not create top-level navigation areas for:

- GitHub.
- Google Drive.
- Confluence.
- Neo4j.
- Graph-RAG.

These are implementation details or source providers, not primary product destinations.

Instead:

```text
Sources
├── GitHub
├── Google Drive
└── Confluence
```

And:

```text
Explore
├── Ask
└── Graph
```

This keeps the product centered around the user's project rather than the underlying technologies.

---

## 28. Page vs Drawer vs Modal

Use a **full page** for persistent workspaces and primary product capabilities.

Use a **drawer** for focused inspection or operations that should preserve the current context.

Use a **modal** for short, consequential confirmations.

Use a **popover/tooltip** for lightweight contextual information.

Recommended mapping:

| Interaction | Surface |
|---|---|
| Projects Dashboard | Page |
| Project Overview | Page |
| Sources | Page |
| Ingestion | Page |
| Ask | Page |
| Graph | Page |
| Create Project | Drawer |
| Ingestion Details | Drawer |
| Source Details | Drawer |
| Source Preview | Drawer |
| Node Details | Drawer |
| Project Settings | Drawer |
| Delete Project confirmation | Modal |
| Lightweight status explanation | Popover / Tooltip |

Avoid creating standalone pages for every small interaction.

---

## 29. Recommended Route Structure

```text
/projects
/projects/:projectId
/projects/:projectId/sources
/projects/:projectId/ingestion
/projects/:projectId/explore/ask
/projects/:projectId/explore/graph
```

The route structure should remain shallow and project-centric.

Future integrations should not require top-level routes such as:

```text
/google-drive
/confluence
/github
```

Instead, their workflows should be launched from the project Sources area.

---

## 30. Suggested Angular Feature Architecture

This is a conceptual structure and should be adapted to the existing Angular application rather than forcing a complete rewrite.

```text
app/
├── core/
│   ├── auth/
│   ├── api/
│   └── layout/
│
├── projects/
│   ├── project-list/
│   ├── project-create/
│   ├── project-overview/
│   └── project-shell/
│
├── sources/
│   ├── source-list/
│   ├── github/
│   ├── integrations/
│   └── source-status/
│
├── ingestion/
│   ├── ingestion-status/
│   ├── ingestion-history/
│   └── ingestion-details/
│
└── exploration/
    ├── chat/
    ├── graph/
    └── source-reference/
```

The exact component/module organization should follow the application's existing Angular architecture and conventions.

---

## 31. Generic Source Architecture

Sources should be modeled generically so new providers can be introduced without redesigning the entire product.

Conceptual model:

```text
Source
├── id
├── projectId
├── type
├── name
├── status
├── syncStatus
├── lastSyncedAt
└── metadata
```

Possible source types:

```text
github
google_drive
confluence
...
```

GitHub is the only required implementation for the MVP.

---

## 32. Project Data Model — Conceptual

```text
Project
├── id
├── name
├── githubRepository
├── sources
├── ingestionStatus
├── graphStatus
└── metadata
```

The frontend should not overfit to the current implementation if the backend model evolves.

The important conceptual relationships are:

```text
Project
  ├── has one mandatory GitHub source
  ├── has zero or more additional sources
  ├── has ingestion history
  └── has one evolving knowledge graph
```

---

## 33. Knowledge Graph Model — Conceptual

Graph nodes:

```text
Node
├── id
├── type
├── label
├── properties
└── sourceReferences
```

Relationships:

```text
Relationship
├── id
├── type
├── sourceNode
└── targetNode
```

The actual graph schema belongs to the backend/domain model, but the frontend should be prepared to display heterogeneous nodes and relationships.

---

## 34. Status Design

Statuses should be consistent across the product.

Recommended status vocabulary:

```text
Not Connected
Connecting
Connected
Syncing
Queued
Processing
Extracting
Building Graph
Updating Graph-RAG
Ready
Partial
Failed
```

Use clear visual distinctions for:

- In progress.
- Successful.
- Warning/partial.
- Failed.
- Not started.

Do not expose raw backend implementation states unless they provide useful user-facing information.

---

## 35. Error Handling

Errors should be contextual and actionable.

Examples:

```text
GitHub connection failed

Unable to access this repository.
Check that the GitHub account has permission to access it.

[ Retry ]
```

For ingestion:

```text
Ingestion failed

22 files were processed successfully.
2 files could not be processed.

[ View Details ]
[ Retry Ingestion ]
```

Detailed technical errors should be available through a details drawer rather than overwhelming the primary UI.

---

## 36. Important UX Principle: Preserve Context

Whenever possible, opening secondary information should not destroy the user's current context.

Examples:

```text
Ask
 ↓
Source Preview Drawer
```

```text
Graph
 ↓
Node Details Drawer
```

```text
Ingestion History
 ↓
Ingestion Details Drawer
```

```text
Projects Dashboard
 ↓
Create Project Drawer
```

This should be a core interaction principle throughout the application.

---

## 37. MVP Scope

The MVP should include:

- GitHub OAuth.
- Multiple projects per user.
- Exactly one GitHub repository per project.
- Repository uniqueness enforcement.
- Projects Dashboard.
- Project creation.
- GitHub repository ingestion.
- Knowledge extraction.
- Graph construction.
- Neo4j-backed knowledge graph.
- Graph-RAG.
- Project Overview.
- GitHub source management.
- Ingestion status.
- Ingestion history.
- Ask exploration.
- Graph exploration.
- Source/node provenance where applicable.

---

## 38. Explicitly Future Scope

Do not implement these as part of the current MVP unless separately requested:

- Google Drive OAuth.
- Google Drive folder selection.
- Google Drive periodic synchronization.
- Confluence authentication.
- Confluence Space/page selection.
- Confluence synchronization.
- Additional external source providers.

The frontend architecture should be extensible enough to accommodate them later.

---

## 39. Future Multi-Source Vision

The long-term model is:

```text
                    ┌── GitHub
                    │
Project Knowledge ──┼── Google Drive
                    │
                    ├── Confluence
                    │
                    └── Future Sources
                           ↓
                    Shared Knowledge Graph
                           ↓
                       Graph-RAG
                           ↓
                    ┌─────────────┐
                    │             │
                   Ask          Graph
```

All connected sources contribute to the same project's knowledge system.

The user should not have to understand which provider contains a piece of knowledge before asking a question.

---

## 40. Core Terminology

Use these terms consistently in the frontend.

### Project

A top-level knowledge workspace containing one mandatory GitHub repository and optional additional sources.

### Source

A system from which knowledge is obtained, such as GitHub, Google Drive, or Confluence.

### Ingestion

The process of fetching, processing, extracting, and incorporating source material into the project knowledge graph.

### Graph

The Neo4j-backed knowledge graph representing entities and relationships discovered from project sources.

### Graph-RAG

The retrieval and reasoning layer operating over the project's knowledge graph.

### Explore

The user-facing area for consuming project knowledge through Ask and Graph.

---

## 41. Design Principles

### 1. Project-centric

The project is the primary unit of organization.

### 2. GitHub-first

GitHub is mandatory and is the primary source in the MVP.

### 3. Source-agnostic architecture

Future providers should fit into the same source model.

### 4. Graph-RAG is core value

Ingestion and graph construction exist to power knowledge exploration.

### 5. Avoid over-navigation

Do not create pages for every detail or provider.

### 6. Preserve context

Prefer drawers for secondary information and focused operations.

### 7. Do not prematurely implement future integrations

Design for Google Drive and Confluence without pretending they are currently operational.

### 8. Clear processing states

Users should understand what is happening during ingestion and graph updates.

### 9. Provenance matters

AI answers should be connected to supporting project knowledge wherever possible.

### 10. Extensibility

New sources should be additive rather than architectural rewrites.

---

## 42. Final Product Flow

```text
┌───────────────────┐
│    GitHub OAuth   │
└─────────┬─────────┘
          ↓
┌───────────────────┐
│ Projects Dashboard│
└─────────┬─────────┘
          ↓
┌───────────────────┐
│ Create Project    │
│ Drawer            │
└─────────┬─────────┘
          ↓
┌───────────────────┐
│ Select GitHub Repo│
└─────────┬─────────┘
          ↓
┌───────────────────┐
│ Initial Ingestion │
└─────────┬─────────┘
          ↓
┌───────────────────┐
│ Knowledge         │
│ Extraction        │
└─────────┬─────────┘
          ↓
┌───────────────────┐
│ Graph Construction│
└─────────┬─────────┘
          ↓
┌───────────────────┐
│      Neo4j        │
└─────────┬─────────┘
          ↓
┌───────────────────┐
│   Graph-RAG Ready │
└─────────┬─────────┘
          ↓
┌───────────────────┐
│ Project Overview  │
└─────────┬─────────┘
          ↓
      ┌───┴───┐
      ↓       ↓
    Ask     Graph
```

---

## 43. Future Enrichment Flow

Once a project already has a GitHub-derived knowledge graph:

```text
Existing Graph-RAG
       ↓
Connect External Source
       ↓
Initial Source Sync
       ↓
Source Ingestion
       ↓
Graph Update
       ↓
Graph-RAG Updating
       ↓
Graph-RAG Ready
```

Example:

```text
GitHub Graph
     +
Architecture Documentation
     +
Confluence Specifications
     ↓
Unified Project Knowledge Graph
     ↓
Graph-RAG
```

---

## 44. Non-Goals for Current MVP

The MVP should explicitly avoid:

- Separate product areas for Google Drive or Confluence.
- Repository-only top-level navigation.
- Individual file-selection workflows for future folder integrations.
- Separate Graph-RAG systems per source.
- Separate exploration experiences per source.
- Standalone pages for every detail interaction.
- Excessive nested navigation.
- Fake or placeholder integrations presented as operational.

---

## 45. Acceptance Criteria

The frontend architecture should satisfy the following:

### Authentication

- User can authenticate through GitHub OAuth.

### Projects

- User can view multiple projects.
- User can create a project through a drawer.
- Each project is associated with exactly one GitHub repository.
- A repository cannot be assigned to multiple projects.

### Project Navigation

- Selecting a project exposes its project-scoped navigation.
- Overview, Sources, Ingestion, Ask, and Graph are accessible without excessive navigation depth.

### Sources

- GitHub is represented as a source.
- The source model is generic enough to support future providers.
- Future Google Drive and Confluence concepts can be added without changing the overall navigation model.

### Ingestion

- The user can understand the current ingestion state.
- The user can distinguish source synchronization from Graph-RAG readiness.
- Ingestion history can be inspected.
- Detailed ingestion information can be viewed without leaving the current context.

### Explore

- Users can ask questions against the project knowledge.
- Answers can expose supporting provenance.
- Users can explore the graph.
- Selecting a graph node provides details without leaving the graph page.
- Ask and Graph can link to one another contextually.

### UX

- Secondary interactions preserve page context.
- Drawers are preferred over unnecessary pages.
- Modals are reserved for short consequential confirmations.
- Statuses and errors are understandable to users.

---

## 46. Summary

Decision Guard should be presented as a **project-centric knowledge workspace**.

The primary mental model is:

```text
Project
  ↓
Sources
  ↓
Ingestion
  ↓
Knowledge Graph
  ↓
Graph-RAG
  ↓
Explore
 ├── Ask
 └── Graph
```

GitHub is mandatory and forms the foundation of the MVP.

Google Drive and Confluence are future project-scoped sources that should enrich the same knowledge graph rather than becoming separate product areas.

The frontend should remain intentionally simple:

- Projects Dashboard for project management.
- Project Overview for operational visibility.
- Sources for knowledge inputs.
- Ingestion for processing visibility.
- Ask for conversational Graph-RAG.
- Graph for visual knowledge exploration.
- Drawers for focused details and operations.
- Modals only for consequential confirmations.

The resulting architecture should feel like one coherent product rather than a collection of provider-specific tools.
