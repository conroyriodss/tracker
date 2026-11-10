"""Versioned vocabulary loading and bounded term normalization for v19."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from catalog_schema import canonical_json


RESOURCE_TYPE_ALIASES = {
    "data repository": "repository",
    "repository": "repository",
    "disciplinary repository": "repository",
    "institutional repository": "repository",
    "data archive": "data_archive",
    "archive": "data_archive",
    "knowledgebase": "knowledge_base",
    "knowledge base": "knowledge_base",
    "knowledge graph": "knowledge_base",
    "data portal": "data_portal",
    "portal": "data_portal",
    "data catalog": "data_catalog",
    "catalog": "data_catalog",
    "database": "database",
    "registry": "registry_results_database",
    "registry/results database": "registry_results_database",
    "clinical trial registry": "registry_results_database",
    "cloud analysis environment": "cloud_analysis_environment",
    "analysis platform": "cloud_analysis_environment",
    "biobank": "biobank_biospecimen_resource",
    "biospecimen repository": "biobank_biospecimen_resource",
    "model resource": "model_resource",
    "access system": "access_system",
}


HUMAN_SCOPE_PATTERNS = (
    "human", "homo sapiens", "patient", "participant", "clinical", "cohort", "electronic health record",
    "ehr", "individual-level", "population health", "person-level", "subject data",
)
NON_HUMAN_SCOPE_PATTERNS = (
    "non-human", "nonhuman", "mouse", "mice", "murine", "rat", "rodent", "animal",
    "zebrafish", "danio rerio", "drosophila", "drosophila melanogaster", "yeast",
    "saccharomyces", "plant", "arabidopsis", "mus musculus", "rattus norvegicus",
    "macaque", "non-human primate", "nhp", "bacterial", "bacteria", "microbial",
    "virus", "viral", "pathogen", "model organism",
)
MODEL_SYSTEM_PATTERNS = (
    "model system", "animal model", "mouse model", "rat model", "disease model",
    "preclinical", "cell line", "organoid", "in vitro", "in vivo", "experimental model",
)

MODALITY_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("single_cell_spatial", ("single cell", "single-cell", "spatial transcript", "spatial omics")),
    ("genomics", ("genom", "dna sequence", "whole genome", "variant", "gwas")),
    ("epigenomics", ("epigen", "methylation", "chip-seq", "chromatin")),
    ("transcriptomics", ("transcript", "rna-seq", "gene expression")),
    ("proteomics", ("proteom", "protein expression", "mass spectrom")),
    ("metabolomics", ("metabolom", "metabolite")),
    ("medical_imaging", ("medical imaging", "radiology", "mri", "ct scan", "pet scan")),
    ("digital_pathology", ("digital pathology", "whole slide", "histopath")),
    ("microscopy", ("microscopy", "cell image", "bioimage")),
    ("biospecimen_biobank", ("biospecimen", "biobank", "specimen")),
    ("behavioral_social_population", ("behavior", "social science", "population health", "survey", "demograph")),
    ("environmental_geospatial", ("environment", "exposure", "geospatial", "geographic")),
    ("physiologic_wearable_mobile", ("wearable", "mobile health", "physiologic", "sensor", "actigraphy")),
    ("neuroimaging", ("neuroimaging", "brain imaging")),
    ("electrophysiology_neural", ("electrophysiology", "eeg", "neural recording", "neurophysiology")),
    ("chemical_pharmacologic_screening", ("chemical", "compound", "pharmac", "screening", "drug")),
    ("clinical_trials_protocols", ("clinical trial", "study protocol", "trial result")),
    ("software_workflows_models", ("software", "workflow", "notebook", "container", "algorithm", "computational model")),
    ("semantic_artifacts", ("ontology", "vocabulary", "terminology", "common data element", "cde")),
    ("synthetic_benchmark_challenge", ("synthetic data", "benchmark", "challenge data")),
    ("clinical_real_world", ("clinical", "electronic health record", "ehr", "real-world", "real world")),
]


def normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", normalize_text(value).casefold()).strip()


class VocabularyRegistry:
    def __init__(self, payload: dict[str, Any], *, source_path: Path | None = None) -> None:
        if payload.get("schema") != "nih-tracker-vocabularies/v1":
            raise ValueError(f"Unsupported vocabulary schema: {payload.get('schema')!r}")
        vocabularies = payload.get("vocabularies")
        if not isinstance(vocabularies, dict):
            raise ValueError("Vocabulary payload lacks vocabularies")
        self.payload = payload
        self.source_path = source_path
        self._terms: dict[str, dict[str, dict[str, Any]]] = {}
        for vocabulary, records in vocabularies.items():
            if not isinstance(records, list):
                raise ValueError(f"Vocabulary {vocabulary!r} must be an array")
            by_id: dict[str, dict[str, Any]] = {}
            for record in records:
                if not isinstance(record, dict):
                    raise ValueError(f"Vocabulary {vocabulary!r} has a non-object term")
                term_id = normalize_text(record.get("id"))
                label = normalize_text(record.get("label"))
                if not term_id or not label:
                    raise ValueError(f"Vocabulary {vocabulary!r} has an incomplete term")
                if term_id in by_id:
                    raise ValueError(f"Duplicate term {vocabulary}:{term_id}")
                by_id[term_id] = dict(record)
            self._terms[str(vocabulary)] = by_id

    @classmethod
    def load(cls, path: Path) -> "VocabularyRegistry":
        return cls(json.loads(path.read_text(encoding="utf-8")), source_path=path)

    def ids(self, vocabulary: str) -> set[str]:
        return set(self._terms.get(vocabulary, {}))

    def term(self, vocabulary: str, term_id: str, *, fallback: str = "not_established") -> dict[str, Any]:
        records = self._terms.get(vocabulary, {})
        selected = records.get(term_id) or records.get(fallback)
        if selected is None:
            raise KeyError(f"Unknown controlled term {vocabulary}:{term_id}")
        return dict(selected)

    def label(self, vocabulary: str, term_id: str) -> str:
        return str(self.term(vocabulary, term_id).get("label") or term_id)

    def validate(self, vocabulary: str, term_id: str) -> str:
        if term_id not in self.ids(vocabulary):
            raise ValueError(f"Unknown controlled term {vocabulary}:{term_id}")
        return term_id

    def canonical_payload(self) -> str:
        return canonical_json(self.payload)


def normalize_resource_type(values: list[Any], registry: VocabularyRegistry) -> list[str]:
    result: list[str] = []
    for value in values:
        normalized = key(value)
        term = RESOURCE_TYPE_ALIASES.get(normalized)
        if not term:
            if "repository" in normalized:
                term = "repository"
            elif "archive" in normalized:
                term = "data_archive"
            elif "knowledge" in normalized:
                term = "knowledge_base"
            elif "portal" in normalized:
                term = "data_portal"
            elif "catalog" in normalized:
                term = "data_catalog"
            elif "registry" in normalized:
                term = "registry_results_database"
            elif "database" in normalized:
                term = "database"
            elif normalized:
                term = "other"
        if term and term in registry.ids("resource_type") and term not in result:
            result.append(term)
    return result or ["not_established"]


def infer_modalities(values: list[Any], registry: VocabularyRegistry) -> list[str]:
    text = " ".join(normalize_text(value).casefold() for value in values if normalize_text(value))
    result: list[str] = []
    for term_id, patterns in MODALITY_PATTERNS:
        if any(pattern in text for pattern in patterns) and term_id not in result:
            result.append(term_id)
    allowed = registry.ids("data_modality")
    result = [item for item in result if item in allowed]
    return result or ["not_established"]



def infer_organism_scopes(values: list[Any], registry: VocabularyRegistry) -> list[str]:
    """Infer broad organism/model-system facets from source-bound descriptive terms.

    The inference is deliberately coarse and is emitted with an explicit limitation
    by the graph compiler; it is not a replacement for asset-level taxonomy.
    """
    text = " ".join(normalize_text(value).casefold() for value in values if normalize_text(value))
    human = any(pattern in text for pattern in HUMAN_SCOPE_PATTERNS)
    non_human = any(pattern in text for pattern in NON_HUMAN_SCOPE_PATTERNS)
    model = any(pattern in text for pattern in MODEL_SYSTEM_PATTERNS)
    result: list[str] = []
    if human:
        result.append("human")
    if non_human:
        result.append("non_human")
    if human and non_human:
        result.append("multi_species")
    if model:
        result.append("model_system")
    allowed = registry.ids("organism_scope")
    result = [item for item in result if item in allowed]
    return result or ["not_established"]


def infer_human_data_sensitivity(
    organism_scopes: list[str],
    access_pathway: str,
    registry: VocabularyRegistry,
) -> str:
    """Derive a cautious human-data access/sensitivity context.

    This never asserts legal, consent, or privacy status. It only combines a
    source-bound human-data signal with the documented resource-level access path.
    """
    human_context = "human" in organism_scopes or "multi_species" in organism_scopes
    if human_context:
        if access_pathway in {"controlled", "restricted"}:
            term = "controlled_or_sensitive"
        elif access_pathway in {"open", "registered"}:
            term = "open_or_registered"
        elif access_pathway == "mixed":
            term = "mixed"
        else:
            term = "not_established"
    elif any(item in organism_scopes for item in {"non_human", "model_system"}):
        term = "not_applicable"
    else:
        term = "not_established"
    return registry.validate("human_data_sensitivity", term)

def map_operational_status(value: Any, registry: VocabularyRegistry) -> str:
    normalized = key(value)
    mapping = {
        "active": "active",
        "temporarily paused": "temporarily_paused",
        "under review": "under_review",
        "migrating": "transition",
        "migrated": "transition",
        "retired": "retired",
        "inactive": "retired",
        "archived": "archived",
        "sunset": "sunset",
    }
    term = mapping.get(normalized, "not_established")
    return registry.validate("operational_status", term)


def map_submission_status(value: Any, registry: VocabularyRegistry) -> str:
    normalized = key(value)
    mapping = {
        "accepting broad submissions": "broad",
        "accepting limited or programmatic submissions": "limited_programmatic",
        "not accepting new submissions": "not_accepting",
        "temporarily paused": "temporarily_paused",
        "not applicable": "not_applicable",
        "not documented": "not_established",
    }
    return registry.validate("submission_model", mapping.get(normalized, "not_established"))


def map_access_status(value: Any, registry: VocabularyRegistry) -> str:
    normalized = key(value)
    mapping = {
        "open": "open",
        "registered": "registered",
        "controlled": "controlled",
        "mixed": "mixed",
        "restricted": "restricted",
        "temporarily paused": "temporarily_paused",
        "migrated": "migrated",
        "unavailable": "unavailable",
        "not documented": "not_established",
    }
    return registry.validate("access_pathway", mapping.get(normalized, "not_established"))
