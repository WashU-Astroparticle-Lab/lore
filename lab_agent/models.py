from __future__ import annotations

from typing import List, Optional
from pydantic import BaseModel, Field


class Artifact(BaseModel):
    path: str
    type: str
    description: Optional[str] = None


class ArtifactGroup(BaseModel):
    primary_execution: List[Artifact] = Field(default_factory=list)
    supporting_context: List[Artifact] = Field(default_factory=list)
    outputs: List[Artifact] = Field(default_factory=list)
    instrument_logs: List[Artifact] = Field(default_factory=list)
    imaging: List[Artifact] = Field(default_factory=list)
    design_files: List[Artifact] = Field(default_factory=list)


class ExperimentMeta(BaseModel):
    id: str
    title: str


class Objective(BaseModel):
    research_question: str


class ExperimentConfig(BaseModel):
    experiment: ExperimentMeta
    objective: Objective
    artifacts: ArtifactGroup


class CollectedArtifact(BaseModel):
    path: str
    kind: str
    description: Optional[str] = None
    content: Optional[str] = None
    markdown_cells: List[str] = Field(default_factory=list)
    csv_rows: List[List[str]] = Field(default_factory=list)
    exists: bool = True
    source: str = "local"
    raw_bytes: Optional[bytes] = None  # image/binary content; saved to disk by run.py
    # Provenance (WS2): where/when this artifact came from, for citations + freshness.
    source_ref: Optional[str] = None   # commit SHA (GitHub) or entry id (LabArchives)
    created_at: Optional[str] = None   # ISO timestamp, when available
    updated_at: Optional[str] = None   # ISO timestamp, when available


class ExperimentBundle(BaseModel):
    root_dir: str
    config: ExperimentConfig
    collected_artifacts: List[CollectedArtifact] = Field(default_factory=list)


class StructuredSummary(BaseModel):
    experiment_id: str
    title: str
    research_question: str
    missing_information: List[str] = Field(default_factory=list)
    evidence_map: List[str] = Field(default_factory=list)
    csv_tables: List[str] = Field(default_factory=list)
    csv_summaries: List[str] = Field(default_factory=list)
    notebook_markdown: List[str] = Field(default_factory=list)
    labarchives_context: List[str] = Field(default_factory=list)

    @property
    def output_dirname(self) -> str:
        return self.experiment_id
