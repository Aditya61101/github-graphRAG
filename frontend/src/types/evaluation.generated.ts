// Generated from Stage 4.4 Pydantic serialization schemas; do not hand-edit.
export type AuthorityReceipt = {
  policy_version: "repository-adr-v1";
  repository_id: string;
  adr_id: string;
  document_hash: string;
  owner_id: string;
  status: "APPROVED" | "UNKNOWN";
  reason: string;
};

export type BaselineSnapshot = {
  repository_id: string;
  github_repository_id: string;
  tracked_branch: string;
  indexed_rkg_sha: string;
  repository_status: "COMPLETED" | "INDEXING" | "FAILED" | "IDLE";
  readiness: "READY" | "UPDATING" | "DIRTY" | "UNKNOWN";
  graph_generation: number;
  ingestion_run_id: string;
  unfinished_ingestion: boolean;
};

export type CallRecord = {
  batch_id: string;
  phase: "ASSESS" | "CHALLENGE";
  status: string;
  input_tokens: number;
  output_tokens: number;
  unreported_reserved_tokens: number;
  denied_reservation_tokens: number;
  elapsed_ms: number;
  request_id: string | null;
};

export type ChallengeDecision = {
  proposal_id: string;
  verdict: "SUPPORTED" | "REFUTED" | "INSUFFICIENT";
  explanation: string;
  tested_witness: string;
  witness_correspondence: "UNCHANGED" | "MATERIAL_CHANGE" | "UNCERTAIN";
  attribution:
    | "INTRODUCED"
    | "WORSENED"
    | "PRE_EXISTING"
    | "RESOLVED"
    | "UNCHANGED"
    | "UNKNOWN";
  citations: QuoteRef[];
  observation_ids: string[];
  revisions: RevisionAssessment[];
  missing_facts: MissingFact[];
};

export type Citation = {
  evidence_id: string;
  quote: string;
  start_char: number;
  end_char: number;
  location: SourceLocation | null;
};

export type CoreAllegation = {
  boundary: string;
  witness: string;
  qualifiers: string[];
  reachability: string;
  citations: QuoteRef[];
  observation_ids: string[];
};

export type EvaluationCoverage = {
  assessed: number;
  uncertain: number;
  omitted: number;
  errors: number;
  scope: "COMPLETE_FOR_RETAINED_SCOPE" | "PARTIAL" | "UNASSESSABLE";
};

export type EvaluationResult = {
  schema_version: "dg-evaluation-v2";
  engine_version: "4";
  evaluation_id: string;
  context_hash: string;
  bundle_hash: string;
  settings_hash: string;
  authority_hash: string;
  revision: RevisionIdentity;
  baseline: BaselineSnapshot;
  deployment_revision: string;
  tokenizer: string;
  execution: "COMPLETED" | "PARTIAL";
  outcome:
    | "CONFLICTS_FOUND"
    | "REVIEW_REQUIRED"
    | "NO_CONFLICTS_FOUND"
    | "NOT_EVALUATED";
  authority: AuthorityReceipt[];
  evidence: EvidenceSnapshot[];
  observations: SourceObservation[];
  policy_observations: PolicyObservation[];
  plan: EvaluationUnit[];
  units: UnitDisposition[];
  findings: Finding[];
  proofs: ProofRecord[];
  rejected_findings: RejectedFinding[];
  validation_records_json: string;
  source_anchors: SourceAnchor[];
  coverage: EvaluationCoverage;
  limitations: Limitation[];
  calls: CallRecord[];
  input_tokens: number;
  output_tokens: number;
  reserved_unreported_tokens: number;
  elapsed_ms: number;
  estimated_cost: number | null;
  pricing_revision: string | null;
  currency: string | null;
};

export type EvaluationSummary = {
  job_id: string;
  revision_key: string;
  base_sha: string;
  head_sha: string;
  status: string;
  stage: string;
  created_at: number;
  evaluated_at: number | null;
  outcome: string | null;
  execution: string | null;
  coverage: EvaluationCoverage | null;
  confirmed_conflicts: number;
  review_required_findings: number;
  rejected_findings: number;
  publication: PublicationResponse;
};

export type EvaluationUnit = {
  unit_id: string;
  channel: "ADR" | "RKG";
  rule_id: string;
  path: string;
  delta_ids: string[];
  entity_ids: string[];
  relationship_ids: string[];
  evidence_ids: string[];
};

export type EvidenceDiscussion = {
  statement: string;
  citations: QuoteRef[];
};

export type EvidenceNeed = {
  anchor_id: string;
  revision: "MERGE_BASE" | "TARGET_BASE" | "PR_HEAD";
  fact_id: string | null;
};

export type EvidenceSnapshot = {
  evidence_id: string;
  repository_id: string;
  kind: "CODE" | "ADR" | "GRAPH_ASSERTION";
  origin:
    "MERGE_BASE" | "TARGET_BASE" | "PR_HEAD" | "INDEXED_CODE" | "ADR_DOCUMENT";
  path: string | null;
  commit_sha: string | null;
  chunk_id: string | null;
  assertion_id: string | null;
  adr_id: string | null;
  source_name: string | null;
  source_type: string | null;
  source_url: string | null;
  source_version: string | null;
  document_hash: string | null;
  content_hash: string | null;
  section: string | null;
  location: SourceLocation | null;
  excerpt: string;
  excerpt_hash: string;
  completeness: "COMPLETE" | "EXCERPT" | "UNAVAILABLE";
  placement: "EXACT" | "AMBIGUOUS" | "UNLOCATED" | "NOT_APPLICABLE";
};

export type Finding = {
  finding_id: string;
  unit_id: string;
  kind: "ADR_CONFLICT" | "RKG_REGRESSION" | "ARCHITECTURAL_IMPACT";
  disposition:
    | "SUPPORTED_CONFLICT"
    | "ADVISORY"
    | "NEEDS_REVIEW"
    | "PRE_EXISTING"
    | "REFUTED";
  attribution:
    | "INTRODUCED"
    | "WORSENED"
    | "PRE_EXISTING"
    | "RESOLVED"
    | "UNCHANGED"
    | "UNKNOWN";
  claim: string;
  consequence: string;
  suggested_action: string;
  core: CoreAllegation | null;
  missing_facts: MissingFact[];
  potential_consequences: PotentialConsequence[];
  citations: Citation[];
  proof_ids: string[];
  head_location: SourceLocation | null;
  candidate: ProposedIssue | null;
  review: ChallengeDecision | null;
  review_error: string | null;
  review_rejection: ResponseRejection | null;
};

export type GoverningRule = {
  finding_id: string;
  rule_id: string;
  decision_id: string | null;
  requirement_kind: string | null;
  adr_ids: string[];
  classification: string;
};

export type Limitation = {
  code: string;
  message: string;
  paths: string[];
  item_ids: string[];
};

export type MissingFact = {
  fact_id: string;
  scope: "CORE" | "CONSEQUENCE";
  fact: string;
  relevance: string;
  anchor_id: string | null;
  revision: "MERGE_BASE" | "TARGET_BASE" | "PR_HEAD";
  unrequestable_reason: string | null;
};

export type PolicyObservation = {
  adr_id: string;
  document_hash: string;
  evidence_id: string;
  coverage: "TEXT_COMPLETE" | "TEXT_PARTIAL";
};

export type PotentialConsequence = {
  claim: string;
  condition: string;
};

export type ProofRecord = {
  proof_id: string;
  unit_id: string;
  obligation: string;
  supported: boolean;
  method: "SOURCE_VALIDATION" | "SEMANTIC_ASSESSMENT" | "SEMANTIC_CHALLENGE";
  explanation: string;
  citations: Citation[];
  observation_ids: string[];
};

export type ProposedIssue = {
  kind: "ADR_CONFLICT" | "RKG_REGRESSION" | "ARCHITECTURAL_IMPACT";
  claim: string;
  rule: EvidenceDiscussion;
  core: CoreAllegation;
  mechanism: string;
  counterevidence: EvidenceDiscussion;
  consequence: string;
  suggested_action: string;
  attribution:
    | "INTRODUCED"
    | "WORSENED"
    | "PRE_EXISTING"
    | "RESOLVED"
    | "UNCHANGED"
    | "UNKNOWN";
  revisions: RevisionAssessment[];
  missing_facts: MissingFact[];
  potential_consequences: PotentialConsequence[];
};

export type PublicationResponse = {
  status: string;
  head_sha: string | null;
  check_run_id: string | null;
  conclusion: string | null;
  attempts: number;
  updated_at: number | null;
  published_at: number | null;
  error_code: string | null;
};

export type PullRequestDetail = {
  pull_request: PullRequestResponse;
  result: EvaluationResult | null;
  deferred: Record<string, unknown>[];
  governing_rules: GoverningRule[];
  history: EvaluationSummary[];
  history_total: number;
  limit: number;
  offset: number;
};

export type PullRequestPage = {
  items: PullRequestResponse[];
  total: number;
  limit: number;
  offset: number;
};

export type PullRequestResponse = {
  repository_id: string;
  repository_full_name: string;
  github_url: string;
  pr_number: number;
  title: string | null;
  author: string | null;
  base_branch: string | null;
  head_branch: string | null;
  base_sha: string | null;
  head_sha: string | null;
  state: "open" | "closed" | "merged" | "unknown";
  observed_at: number | null;
  github_updated_at: number | null;
  metadata_freshness: string;
  freshness: "CURRENT_OBSERVED" | "STALE" | "UNKNOWN";
  evaluation: EvaluationSummary | null;
};

export type QuoteRef = {
  evidence_id: string;
  quote: string;
  start_char: number | null;
  end_char: number | null;
};

export type RejectedFinding = {
  finding_id: string;
  unit_id: string;
  reason: string;
  raw_json: string;
};

export type ResponseRejection = {
  subject_id: string;
  reason: string;
  raw_json: string;
};

export type RevisionAssessment = {
  revision: "MERGE_BASE" | "TARGET_BASE" | "PR_HEAD";
  behavior: "SATISFIES" | "VIOLATES" | "NOT_APPLICABLE" | "UNKNOWN";
  citations: QuoteRef[];
  observation_ids: string[];
};

export type RevisionIdentity = {
  repository_id: string;
  github_repository_id: string;
  pull_request_number: number;
  base_sha: string;
  head_sha: string;
  merge_base_sha: string;
  revision_key: string;
};

export type SourceAnchor = {
  anchor_id: string;
  path: string;
  unit_ids: string[];
};

export type SourceLocation = {
  path: string;
  start_line: number;
  end_line: number;
};

export type SourceObservation = {
  observation_id: string;
  revision: "MERGE_BASE" | "TARGET_BASE" | "PR_HEAD";
  path: string;
  status: "PRESENT" | "ABSENT" | "UNAVAILABLE";
  complete_file: boolean;
  evidence_id: string | null;
};

export type UnitDisposition = {
  unit_id: string;
  status:
    | "ASSESSED"
    | "NOT_APPLICABLE"
    | "EXEMPT"
    | "UNCERTAIN"
    | "BUDGET_OMITTED"
    | "ERROR";
  reason_codes: string[];
  finding_ids: string[];
  proposal: UnitProposal | null;
};

export type UnitProposal = {
  unit_id: string;
  applicability: "APPLIES" | "DOES_NOT_APPLY" | "EXEMPT" | "UNCERTAIN";
  normative: "MANDATORY" | "ADVISORY" | "DESCRIPTIVE" | "UNKNOWN";
  lifecycle:
    | "CURRENT_OR_UNSPECIFIED"
    | "PROPOSED"
    | "REJECTED"
    | "SUPERSEDED"
    | "UNCERTAIN";
  conclusion: "ISSUES" | "NO_CONFLICT" | "NOT_APPLICABLE" | "INSUFFICIENT";
  explanation: string;
  support: QuoteRef[];
  observation_ids: string[];
  issues: ProposedIssue[];
  needs: EvidenceNeed[];
  rejections: ResponseRejection[];
};
