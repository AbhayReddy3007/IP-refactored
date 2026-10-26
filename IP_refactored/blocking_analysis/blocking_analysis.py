"""
orchestrator.py
───────────────
Multi-step blocking analysis pipeline — orchestration layer.

Pipeline:
  Step 1  claim classification                 (step1_claim_classification)
  Step 2  claim element matching               (step2_claim_elements)
  Step 3  scientific barrier analysis          (step3_scientific_barrier)
  Step 4  development feasibility              (step4_development_feasibility)
  Step 5  novelty & technical difficulty       (step5_novelty_difficulty)

Flow:
  Phase 1 — Step 1 on all new patents (parallel).
  Routing — the earliest-filed Composition-of-Matter patent per jurisdiction
            is BLOCKING immediately; everything else goes to Phase 2.
  Phase 2 — Steps 2-5 on remaining patents (parallel), stopping at the first
            step that returns NON-BLOCKING.

Also owns: jurisdiction/result helpers, RAG context building, and the
per-patent JSON analysis cache.
"""

import asyncio
import json
import re
from pathlib import Path
from typing import Dict, List, Optional

from .. import config
from .. import gcp_utils
# Shared chunking utilities — reused instead of reimplemented here.
from ..chunking.indexer import generate_embeddings, get_dates_from_alloydb as get_dates_from_chromadb
from ..chunking.utils import extract_jurisdiction
from .step1_claim_classification import _run_step1
from .step2_claim_elements import _run_step2, get_drug_rows
from .step3_scientific_barrier import _run_step3
from .step4_development_feasibility import _run_step4
from .step5_novelty_difficulty import _run_step5


_ANALYSIS_CONCURRENCY = asyncio.Semaphore(config.ANALYSIS_CONCURRENCY)


# ─────────────────────────────────────────────
# Jurisdiction helpers
# ─────────────────────────────────────────────
# extract_jurisdiction() is shared — imported from ..chunking.utils above
# (identical logic was previously duplicated here and in gcs_lister.py).

def is_non_analysable_patent(filename: str) -> bool:
    """Returns True only for patents that cannot be analysed (e.g. unknown jurisdiction)."""
    jurisdiction = extract_jurisdiction(Path(filename).stem)
    return jurisdiction == "UN"  # only skip truly unknown jurisdictions


# ─────────────────────────────────────────────
# Standardised result helpers
# ─────────────────────────────────────────────

def error_result(filename: str) -> Dict:
    stem = Path(filename).stem
    return {
        "patent_number":                  stem,
        "jurisdiction":                   extract_jurisdiction(stem),
        "filing_date":                    None,
        "grant_date":                     None,
        "claim_category":                 None,
        "tag":                            None,
        "blocking_category":              None,
        "reason":                         None,
        "pte":                            None,
        "pediatric_exclusivity":          False,
        "estimated_approval_year":        None,
        "exclusivity_year":               None,
        "controlling_patent_expiry_year": None,
        "years_to_entry":                 None,
        "avg_years_to_entry":             None,
        "score":                          None,
        "approval_date_us":               None,
        "approval_date_eu":               None,
        "approval_date_us_source":        None,
        "approval_date_eu_source":        None,
        "source_file":                    filename,
    }


def skipped_result(filename: str) -> Dict:
    stem         = Path(filename).stem
    jurisdiction = extract_jurisdiction(stem)
    return {
        "patent_number":                  stem,
        "jurisdiction":                   jurisdiction,
        "filing_date":                    None,
        "grant_date":                     None,
        "claim_category":                 None,
        "tag":                            "SKIPPED",
        "blocking_category":              None,
        "reason":                         f"{jurisdiction} patent — indexed for future use, not analysed.",
        "pte":                            None,
        "pediatric_exclusivity":          False,
        "estimated_approval_year":        None,
        "exclusivity_year":               None,
        "controlling_patent_expiry_year": None,
        "years_to_entry":                 None,
        "avg_years_to_entry":             None,
        "score":                          None,
        "approval_date_us":               None,
        "approval_date_eu":               None,
        "approval_date_us_source":        None,
        "approval_date_eu_source":        None,
        "source_file":                    filename,
    }


# ─────────────────────────────────────────────
# RAG retrieval
# ─────────────────────────────────────────────

async def rag_query(
    query: str, collection, filename: str, top_k: int = 6
) -> List[str]:
    emb = await generate_embeddings([query])
    if not emb:
        return []
    try:
        results = collection.query(
            query_embeddings=[emb[0]],
            n_results=top_k,
            where={
                "$and": [
                    {"filename":    {"$eq": filename}},
                    {"chunk_index": {"$gte": 0}},
                ]
            },
            include=["documents", "metadatas", "distances"],
        )
        return results["documents"][0]
    except Exception as e:
        print(f"[RAG] Query failed: {e}")
        return []


def get_all_chunks(collection, filename: str) -> List[str]:
    """
    Returns ALL chunks for a patent in document order (by chunk_index).
    This ensures the full patent text — abstract, description, examples,
    AND claims — is available for analysis, with no sections omitted.
    """
    try:
        results = collection.get(
            where={
                "$and": [
                    {"filename":    {"$eq": filename}},
                    {"chunk_index": {"$gte": 0}},
                ]
            },
            include=["documents", "metadatas"],
        )
        combined = sorted(
            zip(results["documents"], results["metadatas"]),
            key=lambda x: x[1].get("chunk_index", 0),
        )
        chunks = [doc for doc, _ in combined]
        print(f"[FULL DOC] {filename} -> {len(chunks)} chunk(s) retrieved")
        return chunks
    except Exception as e:
        print(f"[FULL DOC] Failed to retrieve chunks for {filename}: {e}")
        return []


async def build_rag_context(collection, filename: str) -> str:
    """
    Builds the full patent context by concatenating ALL stored chunks in
    document order. The entire patent — cover page, abstract, description,
    examples, and claims — is passed to Gemini for analysis.

    No selective retrieval, no semantic filtering, no sections omitted.
    """
    chunks = get_all_chunks(collection, filename)
    if not chunks:
        print(f"[FULL DOC] No chunks found for {filename}")
        return ""

    context = "\n\n---\n\n".join(chunks)
    print(f"[FULL DOC] Built context: {len(chunks)} chunks | {len(context):,} chars for {filename}")
    return context


# ─────────────────────────────────────────────
# Per-file analysis orchestrator
# ─────────────────────────────────────────────

async def _run_step1_only(
    filename:   str,
    collection,
) -> Optional[Dict]:
    """
    Phase 1 helper — runs Step 1 only and returns a dict with everything
    needed to decide CoM routing and then continue to Steps 2+.

    Returns:
        {
          "filename":          str,
          "step1":             dict,       # raw Step 1 Gemini output
          "context":           str,        # RAG context
          "dates":             dict,       # filing_date, grant_date
          "patent_number":     str,        # resolved
          "jurisdiction":      str,        # resolved
          "is_com":            bool,
          "filing_date":       str | None,
        }
        or None on failure.
    """
    async with _ANALYSIS_CONCURRENCY:
        patent_number_hint = Path(filename).stem
        jurisdiction_hint  = extract_jurisdiction(patent_number_hint)

        context = await build_rag_context(collection, filename)
        dates   = get_dates_from_chromadb(collection, filename)

        if not context.strip():
            print(f"[PHASE 1] No RAG chunks for {filename} — skipping")
            return None

        step1 = await _run_step1(filename, context, patent_number_hint, jurisdiction_hint)
        if step1 is None:
            print(f"[PHASE 1] Step 1 failed for {filename}")
            return None

        patent_number = re.sub(r"[\s,]", "", (step1.get("patent_number") or "").strip())
        if not patent_number:
            patent_number = patent_number_hint
        jurisdiction = (step1.get("jurisdiction") or "").strip().upper() or jurisdiction_hint

        step1["patent_number"] = patent_number
        step1["jurisdiction"]  = jurisdiction

        print(
            f"[PHASE 1] {filename} → {step1['claim_category']} | "
            f"{patent_number} | {jurisdiction}"
        )

        return {
            "filename":      filename,
            "step1":         step1,
            "context":       context,
            "dates":         dates,
            "patent_number": patent_number,
            "jurisdiction":  jurisdiction,
            "is_com":        bool(step1.get("is_composition_of_matter")),
            "filing_date":   dates.get("filing_date"),
        }


def _build_com_blocking_result(phase1: Dict) -> Dict:
    """Build the final BLOCKING result for the primary CoM patent."""
    step1 = phase1["step1"]
    dates = phase1["dates"]
    return {
        "patent_number":                  phase1["patent_number"],
        "jurisdiction":                   phase1["jurisdiction"],
        "filing_date":                    dates.get("filing_date"),
        "grant_date":                     dates.get("grant_date"),
        "claim_category":                 "Composition of Matter",
        "tag":                            "BLOCKING",
        "blocking_category":              "Composition of Matter",
        "reason":                         step1.get("reason"),
        "pte":                            step1.get("pte"),
        "pediatric_exclusivity":          bool(step1.get("pediatric_exclusivity", False)),
        "step2_elements_present":                     None,
        "step3_is_technical_barrier":                 None,
        "step3_confidence":                           None,
        "step3_evidence_type":                        None,
        "step3_evidence_summary":                     None,
        "step4_is_blocking_indicator":                None,
        "step4_confidence":                           None,
        "step4_regulatory_failure_if_removed":        None,
        "step4_bridging_studies_required":            None,
        "step4_formulation_consistent_across_phases": None,
        "step4_reason":                               None,
        "estimated_approval_year":        None,
        "exclusivity_year":               None,
        "controlling_patent_expiry_year": None,
        "years_to_entry":                 None,
        "avg_years_to_entry":             None,
        "score":                          None,
        "approval_date_us":               None,
        "approval_date_eu":               None,
        "approval_date_us_source":        None,
        "approval_date_eu_source":        None,
        "source_file":                    phase1["filename"],
    }


async def _run_steps2_plus(
    phase1:     Dict,
    drug_name:  str,
    drug_rows:  List[Dict],
    drug_phase: Dict[str, Optional[str]],
) -> Dict:
    """
    Phase 2 helper — receives Step 1 output and runs Steps 2+ to completion.
    Used for all patents that are NOT the primary CoM for their jurisdiction.
    """
    async with _ANALYSIS_CONCURRENCY:
        filename     = phase1["filename"]
        step1        = phase1["step1"]
        context      = phase1["context"]
        dates        = phase1["dates"]
        patent_number = phase1["patent_number"]
        jurisdiction  = phase1["jurisdiction"]

        print(f"\n[PHASE 2] ── {filename} → Steps 2+ ──")
        print(f"[STEP 1] {filename} → {step1['claim_category']} — proceeding to Step 2...")

        # ── Step 2 ──────────────────────────────────────────────────────────
        step2 = await _run_step2(filename, context, step1, drug_rows)

        if step2 is None:
            print(f"[ANALYSIS] Step 2 failed for {filename}")
            r = error_result(filename)
            r.update(dates)
            return r

        if not step2["any_element_present"]:
            print(
                f"[STEP 2] {filename} → No claim elements present → NON-BLOCKING\n"
                f"         Reason: {(step2.get('reason') or '')[:120]}"
            )
            return {
                "patent_number":                  patent_number,
                "jurisdiction":                   jurisdiction,
                "filing_date":                    dates.get("filing_date"),
                "grant_date":                     dates.get("grant_date"),
                "claim_category":                 step1["claim_category"],
                "tag":                            "NON-BLOCKING",
                "blocking_category":              None,
                "reason":                         step2.get("reason"),
                "pte":                            step1.get("pte"),
                "pediatric_exclusivity":          bool(step1.get("pediatric_exclusivity", False)),
                "step2_elements_present":                     step2.get("elements_present", {}),
                "step3_is_technical_barrier":                 None,
                "step3_confidence":                           None,
                "step3_evidence_type":                        None,
                "step3_evidence_summary":                     None,
                "step4_is_blocking_indicator":                None,
                "step4_confidence":                           None,
                "step4_regulatory_failure_if_removed":        None,
                "step4_bridging_studies_required":            None,
                "step4_formulation_consistent_across_phases": None,
                "step4_reason":                               None,
            "step5_is_novel_and_difficult":               None,
            "step5_novelty_signal":                           None,
            "step5_first_in_class":                           None,
            "step5_prior_failed_attempts":                None,
            "step5_complex_implementation":               None,
            "step5_confidence":                               None,
            "step5_reason":                                       None,
                "estimated_approval_year":        None,
                "exclusivity_year":               None,
                "controlling_patent_expiry_year": None,
                "years_to_entry":                 None,
                "avg_years_to_entry":             None,
                "score":                          None,
                "approval_date_us":               None,
                "approval_date_eu":               None,
                "approval_date_us_source":        None,
                "approval_date_eu_source":        None,
                "source_file":                    filename,
            }

        # ── Step 3 ──────────────────────────────────────────────────────────
        print(
            f"[STEP 2] {filename} → Elements present: {step2['matched_elements']} "
            f"— continuing to Step 3..."
        )

        step3 = await _run_step3(
            filename     = filename,
            context      = context,
            step1_result = step1,
            step2_result = step2,
            drug_name    = drug_name,
            drug_phase   = drug_phase,
            drug_rows    = drug_rows,
        )

        if step3 is None:
            print(f"[ANALYSIS] Step 3 failed for {filename}")
            r = error_result(filename)
            r.update(dates)
            return r

        if not step3["is_technical_barrier"]:
            print(
                f"[STEP 3] {filename} → Not a technical barrier → NON-BLOCKING\n"
                f"         Reason: {(step3.get('reason') or '')[:120]}"
            )
            return {
                "patent_number":                  patent_number,
                "jurisdiction":                   jurisdiction,
                "filing_date":                    dates.get("filing_date"),
                "grant_date":                     dates.get("grant_date"),
                "claim_category":                 step1["claim_category"],
                "tag":                            "NON-BLOCKING",
                "blocking_category":              None,
                "reason":                         step3.get("reason"),
                "pte":                            step1.get("pte"),
                "pediatric_exclusivity":          bool(step1.get("pediatric_exclusivity", False)),
                "step2_elements_present":                     step2.get("elements_present", {}),
                "step3_is_technical_barrier":                 False,
                "step3_confidence":                           step3.get("confidence"),
                "step3_evidence_type":                        step3.get("evidence_type"),
                "step3_evidence_summary":                     step3.get("evidence_summary"),
                "step4_is_blocking_indicator":                None,
                "step4_confidence":                           None,
                "step4_regulatory_failure_if_removed":        None,
                "step4_bridging_studies_required":            None,
                "step4_formulation_consistent_across_phases": None,
                "step4_reason":                               None,
            "step5_is_novel_and_difficult":               None,
            "step5_novelty_signal":                           None,
            "step5_first_in_class":                           None,
            "step5_prior_failed_attempts":                None,
            "step5_complex_implementation":               None,
            "step5_confidence":                               None,
            "step5_reason":                                       None,
                "estimated_approval_year":        None,
                "exclusivity_year":               None,
                "controlling_patent_expiry_year": None,
                "years_to_entry":                 None,
                "avg_years_to_entry":             None,
                "score":                          None,
                "approval_date_us":               None,
                "approval_date_eu":               None,
                "approval_date_us_source":        None,
                "approval_date_eu_source":        None,
                "source_file":                    filename,
            }

        # ── Step 3 pass → Step 4 ─────────────────────────────────────────────
        print(
            f"[STEP 3] {filename} → Real technical barrier confirmed "
            f"({step3['confidence']} confidence) — continuing to Step 4..."
        )

        step4 = await _run_step4(
            filename     = filename,
            context      = context,
            step1_result = step1,
            step2_result = step2,
            step3_result = step3,
            drug_name    = drug_name,
            drug_phase   = drug_phase,
        )

        if step4 is None:
            print(f"[STEP 4] {filename} → Step 4 failed — treating as NON-BLOCKING")
            step4 = {
                "is_blocking_indicator":              False,
                "regulatory_failure_if_removed":      None,
                "bridging_studies_required":          None,
                "formulation_consistent_across_phases": None,
                "confidence":                         "low",
                "reason":                             "Step 4 analysis failed.",
            }

        if not step4["is_blocking_indicator"]:
            print(
                f"[STEP 4] {filename} → Development can proceed without feature → NON-BLOCKING\n"
                f"         Reason: {(step4.get('reason') or '')[:120]}"
            )
            return {
                "patent_number":                          patent_number,
                "jurisdiction":                           jurisdiction,
                "filing_date":                            dates.get("filing_date"),
                "grant_date":                             dates.get("grant_date"),
                "claim_category":                         step1["claim_category"],
                "tag":                                    "NON-BLOCKING",
                "blocking_category":                      None,
                "reason":                                 step4.get("reason"),
                "pte":                                    step1.get("pte"),
                "pediatric_exclusivity":                  bool(step1.get("pediatric_exclusivity", False)),
                "step2_elements_present":                 step2.get("elements_present", {}),
                "step3_is_technical_barrier":             True,
                "step3_confidence":                       step3.get("confidence"),
                "step3_evidence_type":                    step3.get("evidence_type"),
                "step3_evidence_summary":                 step3.get("evidence_summary"),
                "step4_is_blocking_indicator":            False,
                "step4_confidence":                       step4.get("confidence"),
                "step4_regulatory_failure_if_removed":    step4.get("regulatory_failure_if_removed"),
                "step4_bridging_studies_required":        step4.get("bridging_studies_required"),
                "step4_formulation_consistent_across_phases": step4.get("formulation_consistent_across_phases"),
                "step4_reason":                           step4.get("reason"),
                "estimated_approval_year":                None,
                "exclusivity_year":                       None,
                "controlling_patent_expiry_year":         None,
                "years_to_entry":                         None,
                "avg_years_to_entry":                     None,
                "score":                                  None,
                "approval_date_us":                       None,
                "approval_date_eu":                       None,
                "approval_date_us_source":                None,
                "approval_date_eu_source":                None,
                "source_file":                            filename,
            }

        # ── Step 4 pass → blocking indicator confirmed, continue to Step 5 ────
        print(
            f"[STEP 4] {filename} → Blocking indicator confirmed "
            f"({step4['confidence']} confidence) — continuing to Step 5..."
        )

        step5 = await _run_step5(
            filename     = filename,
            context      = context,
            step1_result = step1,
            step2_result = step2,
            step3_result = step3,
            step4_result = step4,
            drug_name    = drug_name,
        )

        if step5 is None:
            print(f"[STEP 5] {filename} → Step 5 failed — treating as NON-BLOCKING")
            step5 = {
                "is_novel_and_difficult": False,
                "novelty_signal":         "low",
                "first_in_class":         False,
                "prior_failed_attempts":  False,
                "complex_implementation": False,
                "final_tag":              "NON-BLOCKING",
                "blocking_category":      None,
                "confidence":             "low",
                "reason":                 "Step 5 analysis failed.",
            }

        final_tag        = step5.get("final_tag", "NON-BLOCKING")
        blocking_cat     = step5.get("blocking_category") if final_tag == "BLOCKING" else None

        print(
            f"[STEP 5] {filename} → FINAL: {final_tag}"
            + (f" | Category: {blocking_cat}" if blocking_cat else "")
        )

        return {
            "patent_number":                          patent_number,
            "jurisdiction":                           jurisdiction,
            "filing_date":                            dates.get("filing_date"),
            "grant_date":                             dates.get("grant_date"),
            "claim_category":                         step1["claim_category"],
            "tag":                                    final_tag,
            "blocking_category":                      blocking_cat,
            "reason":                                 step5.get("reason"),
            "pte":                                    step1.get("pte"),
            "pediatric_exclusivity":                  bool(step1.get("pediatric_exclusivity", False)),
            "step2_elements_present":                 step2.get("elements_present", {}),
            "step3_is_technical_barrier":             True,
            "step3_confidence":                       step3.get("confidence"),
            "step3_evidence_type":                    step3.get("evidence_type"),
            "step3_evidence_summary":                 step3.get("evidence_summary"),
            "step4_is_blocking_indicator":            True,
            "step4_confidence":                       step4.get("confidence"),
            "step4_regulatory_failure_if_removed":    step4.get("regulatory_failure_if_removed"),
            "step4_bridging_studies_required":        step4.get("bridging_studies_required"),
            "step4_formulation_consistent_across_phases": step4.get("formulation_consistent_across_phases"),
            "step4_reason":                           step4.get("reason"),
            "step5_is_novel_and_difficult":           step5.get("is_novel_and_difficult"),
            "step5_novelty_signal":                   step5.get("novelty_signal"),
            "step5_first_in_class":                   step5.get("first_in_class"),
            "step5_prior_failed_attempts":            step5.get("prior_failed_attempts"),
            "step5_complex_implementation":           step5.get("complex_implementation"),
            "step5_confidence":                       step5.get("confidence"),
            "step5_reason":                           step5.get("reason"),
            "estimated_approval_year":                None,
            "exclusivity_year":                       None,
            "controlling_patent_expiry_year":         None,
            "years_to_entry":                         None,
            "avg_years_to_entry":                     None,
            "score":                                  None,
            "approval_date_us":                       None,
            "approval_date_eu":                       None,
            "approval_date_us_source":                None,
            "approval_date_eu_source":                None,
            "source_file":                            filename,
        }


# ─────────────────────────────────────────────
# Per-patent analysis cache (GCS-backed, via gcp_utils)
# ─────────────────────────────────────────────
# Stores completed blocking analysis results per patent as JSON blobs in GCS
# (not local disk — consistent with chunking/indexer.py's progress tracking,
# and survives container restarts / scales across Cloud Run instances) so
# that re-runs only analyse NEW patents.
#
# Structure:
#   gs://{GCS_BUCKET}/{GCS_CACHE_PREFIX}/{GCS_ANALYSIS_CACHE_SUBFOLDER}/{drug_name}/{filename}.json

def _cache_filename(filename: str) -> str:
    """Converts a patent PDF filename to a safe JSON cache filename."""
    stem = Path(filename).stem
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", stem)
    return f"{safe}.json"


def store_patent_analysis(drug_name: str, filename: str, result: Dict) -> None:
    """Store a single patent's completed analysis result as a JSON blob."""
    try:
        gcp_utils.write_json(
            config.GCS_ANALYSIS_CACHE_SUBFOLDER, _cache_filename(filename), result, drug_name=drug_name,
        )
    except Exception as e:
        print(f"[ANALYSIS CACHE] Store failed for {filename}: {e}")


def load_cached_patent_analysis(drug_name: str, filename: str) -> Optional[Dict]:
    """Load a single patent's cached analysis result. Returns None if not cached."""
    try:
        return gcp_utils.read_json(
            config.GCS_ANALYSIS_CACHE_SUBFOLDER, _cache_filename(filename), drug_name=drug_name,
        )
    except Exception:
        return None


def load_cached_patents_bulk(drug_name: str, filenames: List[str]) -> Dict[str, Dict]:
    """
    Load cached analysis results for multiple filenames at once.
    Returns {filename: patent_dict} for files that have cached results.
    """
    cached: Dict[str, Dict] = {}
    for filename in filenames:
        try:
            patent = gcp_utils.read_json(
                config.GCS_ANALYSIS_CACHE_SUBFOLDER, _cache_filename(filename), drug_name=drug_name,
            )
            if patent is not None:
                cached[filename] = patent
        except Exception as e:
            print(f"[ANALYSIS CACHE] Failed to read cache for {filename}: {e}")
            continue
    return cached


def invalidate_patent_cache(drug_name: str, filename: str) -> None:
    """Remove a single patent's cached analysis."""
    try:
        gcp_utils.delete_blob(
            config.GCS_ANALYSIS_CACHE_SUBFOLDER, _cache_filename(filename), drug_name=drug_name,
        )
    except Exception:
        pass


def invalidate_drug_cache(drug_name: str) -> None:
    """Remove ALL cached analysis results for a drug."""
    try:
        names = gcp_utils.list_blob_names(
            config.GCS_ANALYSIS_CACHE_SUBFOLDER, drug_name=drug_name, suffix=".json",
        )
        for name in names:
            gcp_utils.delete_blob(config.GCS_ANALYSIS_CACHE_SUBFOLDER, name, drug_name=drug_name)
        print(f"[ANALYSIS CACHE] Invalidated {len(names)} cached result(s) for '{drug_name}'")
    except Exception as e:
        print(f"[ANALYSIS CACHE] Invalidation failed for '{drug_name}': {e}")


# ─────────────────────────────────────────────
# Main public function
# ─────────────────────────────────────────────

async def run_blocking_analysis(
    drug_name:  str,
    pdf_refs:   List[dict],
    collection,
    drug_phase: Optional[Dict[str, Optional[str]]] = None,
    force_reanalyse: bool = False,
) -> List[Dict]:
    """
    Two-phase blocking analysis for all US/EP patents — with incremental caching.

    On first run: analyses all patents and caches each result.
    On subsequent runs: loads cached results for already-analysed patents,
    only sends NEW patents through the Gemini Steps 1-5 pipeline, then
    merges cached + fresh results for CoM routing and final output.

    Phase 1 — Run Step 1 on NEW patents in parallel to classify claim categories.
              Load cached Step 1 data for already-analysed patents.
    Between  — Identify the ONE primary CoM per jurisdiction (earliest filing date)
               across ALL patents (cached + new).
               Primary CoM → BLOCKING immediately, skips Steps 2+.
               All other patents (including secondary CoMs) → Phase 2.
    Phase 2  — Run Steps 2+ in parallel on NEW non-primary-CoM patents.
               Cached non-CoM patents use their stored results directly.

    Args:
        drug_name:        Drug name string (from GCS folder name)
        pdf_refs:         List of {"filename": str, ...} from gcs_lister
        collection:       AlloyDB collection
        drug_phase:       {"US": phase, "EP": phase} — from clinical timeline.
        force_reanalyse:  If True, ignore cache and re-analyse everything.
    """
    all_filenames  = [ref["filename"] for ref in pdf_refs]
    analysis_files = [f for f in all_filenames if not is_non_analysable_patent(f)]
    skipped_files  = [f for f in all_filenames if     is_non_analysable_patent(f)]

    for f in skipped_files:
        print(f"[SKIP ANALYSIS] {f} — non-US/EP patent")

    # ── Load cached results for already-analysed patents ──────────────────────
    cached_results: Dict[str, Dict] = {}
    new_files:      List[str]       = []

    if not force_reanalyse:
        cached_results = load_cached_patents_bulk(drug_name, analysis_files)
        new_files      = [f for f in analysis_files if f not in cached_results]

        if cached_results:
            print(
                f"\n[CACHE] {len(cached_results)} patent(s) loaded from cache, "
                f"{len(new_files)} new patent(s) to analyse"
            )
            for f in cached_results:
                tag = cached_results[f].get("tag", "?")
                cat = cached_results[f].get("claim_category", "?")
                print(f"  [CACHED] {f} → {tag} ({cat})")
        else:
            print(f"\n[CACHE] No cached results — analysing all {len(analysis_files)} patent(s)")
            new_files = analysis_files
    else:
        print(f"\n[CACHE] Force re-analyse — ignoring cache for all {len(analysis_files)} patent(s)")
        invalidate_drug_cache(drug_name)
        new_files = analysis_files

    # If everything is cached, skip the expensive Gemini calls entirely
    if not new_files:
        print(f"[CACHE] All patents cached — skipping Gemini analysis entirely")
        patents = list(cached_results.values())
        for filename in skipped_files:
            patents.append(skipped_result(filename))
        _print_summary_table(drug_name, patents)
        return patents

    drug_rows = get_drug_rows(drug_name)
    if drug_rows:
        print(f"[STEP 2] {len(drug_rows)} Excel record(s) ready for '{drug_name}'")
    else:
        print(f"[STEP 2] No Excel data for '{drug_name}' — Step 2 will assess from patent claims alone")

    if drug_phase:
        print(f"[STEP 3] Phase info ready for '{drug_name}': {drug_phase}")
    else:
        print(f"[STEP 3] No phase info for '{drug_name}' — defaulting to clinical path")
        drug_phase = {}

    # ── Phase 1: Step 1 on NEW patents in parallel ────────────────────────────
    print(f"\n[PHASE 1] Running Step 1 on {len(new_files)} NEW patent(s)...")

    new_phase1_results = await asyncio.gather(
        *[_run_step1_only(f, collection) for f in new_files],
        return_exceptions=True,
    )

    # Build a unified phase1 view: cached patents contribute their stored
    # step1 data (claim_category, jurisdiction, filing_date, is_com) so
    # CoM routing considers ALL patents, not just new ones.
    all_phase1: List[Dict] = []

    # Add cached patents as phase1-style dicts for CoM routing
    for filename, cached_patent in cached_results.items():
        all_phase1.append({
            "filename":      filename,
            "patent_number": cached_patent.get("patent_number", Path(filename).stem),
            "jurisdiction":  (cached_patent.get("jurisdiction") or "").upper(),
            "is_com":        cached_patent.get("claim_category") == "Composition of Matter"
                             and cached_patent.get("tag") == "BLOCKING",
            "filing_date":   cached_patent.get("filing_date"),
            "_from_cache":   True,
        })

    # Add new patents' phase1 results
    for filename, result in zip(new_files, new_phase1_results):
        if isinstance(result, Exception) or result is None:
            all_phase1.append({"filename": filename, "_failed": True})
        else:
            result["_from_cache"] = False
            all_phase1.append(result)

    # ── Identify primary CoM per jurisdiction (across ALL patents) ────────────
    primary_com_filenames: set = set()

    all_jurisdictions = sorted(set(
        r.get("jurisdiction") for r in all_phase1
        if isinstance(r, dict) and r.get("jurisdiction") and not r.get("_failed")
    ))
    print(f"[CoM ROUTING] Jurisdictions found: {all_jurisdictions}")

    for jurisdiction in all_jurisdictions:
        com_candidates = [
            r for r in all_phase1
            if isinstance(r, dict)
            and not r.get("_failed")
            and r.get("is_com")
            and r.get("jurisdiction") == jurisdiction
        ]
        if not com_candidates:
            continue

        com_candidates.sort(key=lambda r: r.get("filing_date") or "9999-99-99")
        primary = com_candidates[0]
        primary_com_filenames.add(primary["filename"])

        print(f"\n[CoM ROUTING] Primary CoM for {jurisdiction}: "
              f"{primary.get('patent_number', '?')} (filed: {primary.get('filing_date') or 'unknown'})"
              f" → BLOCKING (skips Steps 2+)")

        for secondary in com_candidates[1:]:
            print(f"[CoM ROUTING] Secondary CoM for {jurisdiction}: "
                  f"{secondary.get('patent_number', '?')} → sent to Steps 2+ as Formulation-class")

    # ── Build results: merge cached results + route new patents ───────────────
    patents: List[Dict] = []
    phase2_inputs: List[Dict] = []

    # Add cached results directly (they already went through full analysis)
    for filename, cached_patent in cached_results.items():
        # If a cached patent is now the primary CoM (e.g. a new patent changed routing),
        # we still use its cached result — CoM routing is stable for existing patents.
        patents.append(cached_patent)
        print(f"[RESULT] {filename} → from cache ({cached_patent.get('tag', '?')})")

    # Route new patents
    for filename, result in zip(new_files, new_phase1_results):
        if isinstance(result, Exception) or result is None:
            print(f"[ERROR] Phase 1 failed for {filename}: {result}")
            patents.append(error_result(filename))
            continue

        if filename in primary_com_filenames:
            com_result = _build_com_blocking_result(result)
            patents.append(com_result)
            store_patent_analysis(drug_name, filename, com_result)
            print(f"[RESULT] {filename} → NEW primary CoM BLOCKING (cached)")
        else:
            if result.get("step1", {}).get("claim_category") == "Composition of Matter":
                result["step1"]["claim_category"] = "Formulation"
                result["step1"]["is_composition_of_matter"] = False
                print(
                    f"[CoM ROUTING] {result.get('patent_number')} reclassified: "
                    f"Composition of Matter → Formulation"
                )
            phase2_inputs.append(result)

    # ── Phase 2: Steps 2+ on NEW non-primary-CoM patents in parallel ──────────
    if phase2_inputs:
        print(f"\n[PHASE 2] Running Steps 2+ on {len(phase2_inputs)} NEW patent(s)...")

        phase2_results = await asyncio.gather(
            *[_run_steps2_plus(p, drug_name, drug_rows, drug_phase)
              for p in phase2_inputs],
            return_exceptions=True,
        )

        for phase1_data, result in zip(phase2_inputs, phase2_results):
            if isinstance(result, Exception):
                print(f"[ERROR] Phase 2 failed for {phase1_data['filename']}: {result}")
                patents.append(error_result(phase1_data["filename"]))
            else:
                patents.append(result)
                store_patent_analysis(drug_name, phase1_data["filename"], result)
                print(f"[RESULT] {phase1_data['filename']} → NEW {result.get('tag', '?')} (cached)")

    # ── Add skipped patents ───────────────────────────────────────────────────
    for filename in skipped_files:
        patents.append(skipped_result(filename))

    _print_summary_table(drug_name, patents)
    return patents


def _print_summary_table(drug_name: str, patents: List[Dict]) -> None:
    """Prints the analysis summary table. Shared by cached and fresh paths."""
    col_pn = max((len(p.get("patent_number") or "") for p in patents), default=14)
    col_pn = max(col_pn, 14)
    col_s1 = 26
    col_s2 = 44
    col_s3 = 36

    header = (
        f"{'Patent Number':<{col_pn}}  "
        f"{'Step 1 Category':<{col_s1}}  "
        f"{'Step 2 Matched Elements':<{col_s2}}  "
        f"{'Step 3 Scientific Barrier':<{col_s3}}"
    )
    divider = "-" * len(header)

    print(f"\n[SUMMARY] ── {drug_name} ──")
    print(divider)
    print(header)
    print(divider)

    for p in patents:
        patent_num = p.get("patent_number") or ""
        tag        = p.get("tag") or ""

        if tag == "SKIPPED":
            s1, s2, s3 = "SKIPPED", "—", "—"
        else:
            s1 = p.get("claim_category") or "—"

            elements = p.get("step2_elements_present")
            if elements is None:
                s2 = "N/A (primary CoM → BLOCKING)"
            else:
                matched = [k for k, v in elements.items() if v]
                s2 = ", ".join(matched) if matched else "None matched"

            barrier = p.get("step3_is_technical_barrier")
            if barrier is None:
                s3 = "N/A (stopped earlier)"
            else:
                s3 = (
                    f"{'YES' if barrier else 'NO'} "
                    f"[{p.get('step3_confidence') or ''}] "
                    f"{p.get('step3_evidence_type') or ''}"
                )

        print(
            f"{patent_num:<{col_pn}}  "
            f"{s1:<{col_s1}}  "
            f"{s2:<{col_s2}}  "
            f"{s3:<{col_s3}}"
        )

    print(divider)
    print()
