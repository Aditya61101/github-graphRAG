export type KnowledgeSource = {
  id: string;
  title: string | null;
  source_name: string | null;
  source_type: string | null;
  source_version: string | null;
  content_hash: string | null;
};
export type KnowledgeItem = {
  id: string;
  kind: "decision" | "constraint";
  title: string | null;
  description: string;
  type: string | null;
  sources: KnowledgeSource[];
};
export type KnowledgePage = {
  items: KnowledgeItem[];
  total: number;
  offset: number;
  limit: number;
};
export type KnowledgeDetail = KnowledgeItem & {
  entities: {
    id: string;
    name: string | null;
    label: string | null;
    relationship: "AFFECTS" | "CONSTRAINS";
    source: "ADR";
  }[];
  evidence: {
    id: string;
    adr_id: string;
    section: string | null;
    chunk_index: number | null;
    excerpt: string;
    truncated: boolean;
    content_hash: string | null;
  }[];
  entities_truncated: boolean;
  evidence_truncated: boolean;
  relationship_basis: "documented_ADR_knowledge";
};
export type ADRDocument = {
  id: string;
  title: string;
  source_name: string;
  source_type: string;
  created_at: string | null;
  status: string;
  updated_at?: string | null;
  error: string | null;
};
export type ADRUploadResult = {
  repository_id: string;
  total: number;
  completed: number;
  accepted: number;
  duplicates: number;
  failed: number;
  results: {
    index: number;
    filename: string;
    status: string;
    status_code: number;
    error: string | null;
    adr: ADRDocument | null;
  }[];
};
export type ADRStatus = {
  adr_id: string;
  status: "PENDING" | "PROCESSING" | "COMPLETED" | "FAILED";
  updated_at: string | null;
  error: string | null;
};
