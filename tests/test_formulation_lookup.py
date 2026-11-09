"""Tests for ip_pillar.primary_market_entry_horizon.blocking_analysis.formulation_lookup.

No network, no Google Cloud: the public APIs, Gemini and the GCS cache are replaced with fakes.
Run from the repository root:   python -m unittest discover -s tests -v
"""

import asyncio
import copy
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import requests

from ip_pillar import config
from ip_pillar.primary_market_entry_horizon.blocking_analysis import formulation_lookup as fl
from ip_pillar.primary_market_entry_horizon.blocking_analysis import step2_claim_elements as step2

# ── fixtures shaped like the real API replies ────────────────────────────────

def ct_study(nct="NCT04017832", phases=("PHASE3",), desc="Oral tablet 14 mg", arm="Participants take a tablet daily",
             title="A study", name="Semaglutide"):
    return {"protocolSection": {
        "identificationModule": {"nctId": nct, "briefTitle": title},
        "statusModule": {"overallStatus": "COMPLETED"},
        "designModule": {"phases": list(phases)},
        "armsInterventionsModule": {
            "armGroups": [{"label": "Arm A", "description": arm}],
            "interventions": [{"type": "DRUG", "name": name, "description": desc, "otherNames": ["Rybelsus"]}],
        }}}


def fda_label(brand="OZEMPIC", generic="SEMAGLUTIDE", route="SUBCUTANEOUS", with_forms=True):
    item = {
        "openfda": {"brand_name": [brand], "generic_name": [generic], "route": [route]},
        "description": ["DESCRIPTION  Ozempic contains semaglutide.�"],
        "how_supplied": ["Pen delivers 0.25 mg or 0.5 mg"],
        "spl_product_data_elements": ["OZEMPIC semaglutide SODIUM PHOSPHATE, DIBASIC"],
        "instructions_for_use": ["Use the pen with a new NovoFine needle"],
    }
    if with_forms:
        item["dosage_forms_and_strengths"] = ["Injection: 2 mg/3 mL pen"]
    return item


class Resp:
    def __init__(self, status, body=None, bad_json=False):
        self.status_code, self._body, self._bad = status, body, bad_json

    def json(self):
        if self._bad:
            raise ValueError("not json")
        return self._body


# ── search terms ─────────────────────────────────────────────────────────────

class SearchTerms(unittest.TestCase):
    def test_plain_name(self):
        self.assertEqual(fl._search_terms("Semaglutide"), ["Semaglutide"])

    def test_salt_word_is_stripped_and_original_kept(self):
        self.assertEqual(fl._search_terms("Orforglipron Calcium"), ["Orforglipron", "Orforglipron Calcium"])

    def test_combination_is_split(self):
        self.assertEqual(fl._search_terms("Insulin Icodec+Semaglutide"), ["Insulin Icodec", "Semaglutide"])
        self.assertEqual(fl._search_terms("Cagrilintide + Semaglutide"), ["Cagrilintide", "Semaglutide"])

    def test_multi_word_name_is_not_cut_to_one_word(self):
        self.assertEqual(fl._search_terms("Insulin Degludec"), ["Insulin Degludec"])

    def test_single_salt_like_word_survives_and_quotes_are_removed(self):
        self.assertEqual(fl._search_terms("Acetate"), ["Acetate"])
        self.assertEqual(fl._search_terms('Foo "Bar"'), ["Foo Bar"])

    def test_empty_and_cap(self):
        self.assertEqual(fl._search_terms(""), [])
        self.assertEqual(len(fl._search_terms("A+B+C+D+E+F")), 4)


# ── HTTP helper ──────────────────────────────────────────────────────────────

class GetJson(unittest.TestCase):
    def setUp(self):
        self.sleep = mock.patch.object(fl.time, "sleep").start()
        self.addCleanup(mock.patch.stopall)

    def test_ok(self):
        with mock.patch.object(fl.requests, "get", return_value=Resp(200, {"a": 1})) as get:
            self.assertEqual(fl._get_json("u", {"p": 1}, "x"), {"a": 1})
        self.assertEqual(get.call_args.kwargs["headers"], {"User-Agent": "ip-pillar/1.0"})

    def test_404_means_nothing_matched(self):
        with mock.patch.object(fl.requests, "get", return_value=Resp(404, {"error": "No matches"})):
            self.assertIsNone(fl._get_json("u", {}, "x"))

    def test_429_is_retried_with_backoff(self):
        with mock.patch.object(fl.requests, "get", side_effect=[Resp(429), Resp(503), Resp(200, {"ok": True})]) as get:
            self.assertEqual(fl._get_json("u", {}, "x"), {"ok": True})
        self.assertEqual(get.call_count, 3)
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [2.0, 4.0])

    def test_network_error_is_retried_then_raises(self):
        with mock.patch.object(fl.requests, "get", side_effect=requests.ConnectionError("down")) as get:
            with self.assertRaises(fl.SourceUnavailable):
                fl._get_json("u", {}, "ClinicalTrials.gov")
        self.assertEqual(get.call_count, 3)

    def test_persistent_500_raises(self):
        with mock.patch.object(fl.requests, "get", return_value=Resp(500)):
            with self.assertRaises(fl.SourceUnavailable):
                fl._get_json("u", {}, "x")

    def test_client_error_is_not_retried(self):
        with mock.patch.object(fl.requests, "get", return_value=Resp(400)) as get:
            with self.assertRaises(fl.SourceUnavailable):
                fl._get_json("u", {}, "x")
        self.assertEqual(get.call_count, 1)

    def test_invalid_json_is_retried(self):
        with mock.patch.object(fl.requests, "get", side_effect=[Resp(200, bad_json=True), Resp(200, {"ok": 1})]):
            self.assertEqual(fl._get_json("u", {}, "x"), {"ok": 1})


# ── ClinicalTrials.gov ───────────────────────────────────────────────────────

class CtGov(unittest.TestCase):
    def test_parse_study(self):
        s = fl._parse_study(ct_study("nct04017832", phases=("PHASE1", "PHASE2")))
        self.assertEqual(s["nct_id"], "NCT04017832")
        self.assertEqual(s["phase"], "Phase 1/Phase 2")
        self.assertEqual(s["interventions"][0]["other_names"], ["Rybelsus"])
        self.assertEqual(s["arms"][0]["description"], "Participants take a tablet daily")

    def test_parse_study_tolerates_missing_parts(self):
        self.assertIsNone(fl._parse_study({}))
        self.assertIsNone(fl._parse_study({"protocolSection": {"identificationModule": {}}}))
        s = fl._parse_study({"protocolSection": {"identificationModule": {"nctId": "NCT00000001"}}})
        self.assertEqual((s["interventions"], s["arms"], s["phase"]), ([], [], ""))

    def test_fetch_dedupes_across_terms_ranks_and_drops_empty(self):
        pages = {
            "Orforglipron": {"studies": [ct_study("NCT00000001", desc="short", arm=""),
                                         ct_study("NCT00000002", desc="a much longer description of the tablet", arm="arm text"),
                                         ct_study("NCT00000003", desc="", arm="")]},
            "Orforglipron Calcium": {"studies": [ct_study("NCT00000001", desc="short", arm=""),
                                                 ct_study("NCT00000004", desc="medium length text", arm="")]},
        }
        seen = []

        def fake_get(url, params, what, **kw):
            seen.append(params["query.intr"])
            return pages[params["query.intr"]]

        with mock.patch.object(fl, "_get_json", fake_get):
            studies = fl.fetch_ctgov_studies("Orforglipron Calcium", max_studies=3)
        self.assertEqual(seen, ["Orforglipron", "Orforglipron Calcium"])
        self.assertEqual([s["nct_id"] for s in studies], ["NCT00000002", "NCT00000004", "NCT00000001"])  # no NCT...03

    def test_fetch_respects_max_and_handles_no_reply(self):
        with mock.patch.object(fl, "_get_json", return_value={"studies": [ct_study(f"NCT0000000{i}") for i in range(1, 6)]}):
            self.assertEqual(len(fl.fetch_ctgov_studies("X", max_studies=2)), 2)
        with mock.patch.object(fl, "_get_json", return_value=None):
            self.assertEqual(fl.fetch_ctgov_studies("X"), [])

    def test_request_parameters(self):
        with mock.patch.object(fl, "_get_json", return_value={"studies": []}) as g:
            fl.fetch_ctgov_studies("Semaglutide")
        url, params = g.call_args.args[0], g.call_args.args[1]
        self.assertEqual(url, fl.CTGOV_URL)
        self.assertEqual(params["query.intr"], "Semaglutide")
        self.assertIn("InterventionDescription", params["fields"])

    def test_study_text(self):
        text = fl._study_text(fl._parse_study(ct_study()))
        self.assertIn("NCT04017832 | Phase 3 | COMPLETED", text)
        self.assertIn("Intervention [DRUG] Semaglutide (also called: Rybelsus): Oral tablet 14 mg", text)
        self.assertIn("Arm Arm A: Participants take a tablet daily", text)
        self.assertLessEqual(len(fl._study_text(fl._parse_study(ct_study(desc="x" * 5000)))), 1500)


# ── openFDA ──────────────────────────────────────────────────────────────────

class OpenFda(unittest.TestCase):
    def test_parse_label_keeps_only_formulation_sections_and_cleans_text(self):
        label = fl._parse_label(fda_label())
        self.assertEqual(label["brands"], ["OZEMPIC"])
        self.assertEqual(set(label["sections"]), {"description", "dosage_forms_and_strengths", "how_supplied",
                                                  "spl_product_data_elements", "instructions_for_use"})
        self.assertNotIn("�", label["sections"]["description"])
        self.assertIsNone(fl._parse_label({"openfda": {}}))
        self.assertIsNone(fl._parse_label({}))

    def test_fetch_dedupes_by_brand_and_prefers_dosage_forms(self):
        results = {"results": [fda_label("OZEMPIC", with_forms=False), fda_label("OZEMPIC"),
                               fda_label("WEGOVY"), fda_label("RYBELSUS", route="ORAL", with_forms=False),
                               fda_label("OTHER")]}
        with mock.patch.object(fl, "_get_json", return_value=results):
            labels = fl.fetch_openfda_labels("Semaglutide", max_labels=3)
        self.assertEqual(len(labels), 3)
        self.assertEqual(len({l["brands"][0] for l in labels}), 3)
        self.assertTrue(all("dosage_forms_and_strengths" in l["sections"] for l in labels))
        ozempic = next(l for l in labels if l["brands"][0] == "OZEMPIC")
        self.assertIn("dosage_forms_and_strengths", ozempic["sections"])   # the fuller duplicate won

    def test_no_label(self):
        with mock.patch.object(fl, "_get_json", return_value=None):
            self.assertEqual(fl.fetch_openfda_labels("Da-302168S"), [])

    def test_search_expression_and_api_key(self):
        with mock.patch.object(config, "OPENFDA_API_KEY", "KEY"), \
                mock.patch.object(fl, "_get_json", return_value=None) as g:
            fl.fetch_openfda_labels("Semaglutide")
        params = g.call_args.args[1]
        self.assertEqual(params["search"], 'openfda.generic_name:"Semaglutide" openfda.substance_name:"Semaglutide"')
        self.assertEqual(params["api_key"], "KEY")
        with mock.patch.object(config, "OPENFDA_API_KEY", ""), mock.patch.object(fl, "_get_json", return_value=None) as g:
            fl.fetch_openfda_labels("Semaglutide")
        self.assertNotIn("api_key", g.call_args.args[1])


# ── prompt ───────────────────────────────────────────────────────────────────

class Prompt(unittest.TestCase):
    def test_labels_first_ids_collected_and_no_placeholder_left(self):
        studies = [fl._parse_study(ct_study("NCT00000001")), fl._parse_study(ct_study("NCT00000002"))]
        labels = [fl._parse_label(fda_label())]
        prompt, ids, brands = fl.build_prompt("Semaglutide", studies, labels)
        self.assertEqual(ids, {"NCT00000001", "NCT00000002"})
        self.assertEqual(brands, ["OZEMPIC"])
        self.assertLess(prompt.index("FDA label: OZEMPIC"), prompt.index("NCT00000001"))
        self.assertNotIn("##", prompt)
        self.assertIn("DRUG: Semaglutide", prompt)

    def test_braces_in_sources_do_not_break_it(self):
        s = fl._parse_study(ct_study(desc="dose {mg} per {kg} %s %(x)d"))
        prompt, _, _ = fl.build_prompt("X{y}", [s], [])
        self.assertIn("dose {mg} per {kg}", prompt)

    def test_size_cap_drops_studies_that_do_not_fit_but_keeps_one(self):
        studies = [fl._parse_study(ct_study(f"NCT0000000{i}", desc="d" * 1200)) for i in range(1, 8)]
        with mock.patch.object(config, "FORMULATION_MAX_PROMPT_CHARS", 4000):
            _, ids, _ = fl.build_prompt("X", studies, [])
        self.assertTrue(1 <= len(ids) < 7)
        with mock.patch.object(config, "FORMULATION_MAX_PROMPT_CHARS", 10):
            _, ids, _ = fl.build_prompt("X", studies, [])
        self.assertEqual(len(ids), 1)

    def test_no_sources(self):
        prompt, ids, brands = fl.build_prompt("X", [], [])
        self.assertEqual((ids, brands), (set(), []))


# ── cleaning the model's answer ──────────────────────────────────────────────

def rec(source="NCT00000001", **kw):
    r = {"Trial ID": source, "Phase": "Phase 3", "Active Ingredient & Form": "semaglutide",
         "Formulation Details": "tablet", "Route of Administration": "oral", "Device Description": "",
         "Combination Tech/Process": "", "Source Quote": "quote"}
    r.update(kw)
    return r


class CleanRecords(unittest.TestCase):
    ids = {"NCT00000001", "NCT00000002"}
    brands = ["OZEMPIC"]

    def clean(self, raw):
        return fl.clean_records(raw, self.ids, self.brands)

    def test_valid_study_record_keeps_only_step2_columns_and_drops_blanks(self):
        rows, audit, dropped = self.clean([rec()])
        self.assertEqual(dropped, 0)
        self.assertEqual(rows, [{"Trial ID": "NCT00000001", "Phase": "Phase 3", "Active Ingredient & Form": "semaglutide",
                                 "Formulation Details": "tablet", "Route of Administration": "oral"}])
        self.assertEqual(audit[0]["Source Quote"], "quote")
        self.assertNotIn("Source Quote", rows[0])

    def test_invented_study_is_dropped(self):
        rows, _, dropped = self.clean([rec("NCT99999999"), rec("not a source"), rec("")])
        self.assertEqual((rows, dropped), ([], 3))

    def test_label_record_needs_a_fetched_brand(self):
        rows, _, dropped = self.clean([rec("FDA label: OZEMPIC"),
                                       rec("fda label: ozempic pen", **{"Formulation Details": "pen injector"}),
                                       rec("FDA label: MOUNJARO")])
        self.assertEqual([r["Trial ID"] for r in rows], ["FDA label: OZEMPIC", "fda label: ozempic pen"])
        self.assertEqual(dropped, 1)

    def test_record_with_no_formulation_field_is_dropped(self):
        empty = rec(**{"Active Ingredient & Form": "N/A", "Formulation Details": "none", "Route of Administration": " "})
        rows, _, dropped = self.clean([empty])
        self.assertEqual((rows, dropped), ([], 1))

    def test_blank_words_and_whitespace(self):
        rows, _, _ = self.clean([rec(**{"Formulation Details": "  oral   tablet\n 14 mg ", "Device Description": "Unknown",
                                        "Combination Tech/Process": "not stated"})])
        self.assertEqual(rows[0]["Formulation Details"], "oral tablet 14 mg")
        self.assertNotIn("Device Description", rows[0])
        self.assertNotIn("Combination Tech/Process", rows[0])

    def test_duplicates_and_garbage(self):
        rows, _, dropped = self.clean([rec(), rec("NCT00000002"), "oops", None, 5, {}])
        self.assertEqual(len(rows), 1)
        # dropped: the second record (same content as the first), "oops", None, 5, and {} (cites no source)
        self.assertEqual(dropped, 5)
        self.assertEqual(self.clean(None), ([], [], 0))
        self.assertEqual(self.clean({"a": 1}), ([], [], 0))

    def test_long_values_are_cut_and_count_is_capped(self):
        rows, _, _ = self.clean([rec(**{"Formulation Details": "x" * 900})])
        self.assertEqual(len(rows[0]["Formulation Details"]), 300)
        many = [rec(**{"Formulation Details": f"form {i}"}) for i in range(30)]
        rows, _, _ = self.clean(many)
        self.assertEqual(len(rows), 12)

    def test_label_records_sorted_first(self):
        rows, audit, _ = self.clean([rec("NCT00000001"), rec("FDA label: OZEMPIC", **{"Formulation Details": "pen"})])
        self.assertTrue(rows[0]["Trial ID"].startswith("FDA label"))
        self.assertTrue(audit[0]["Trial ID"].startswith("FDA label"))

    def test_study_id_is_normalised(self):
        rows, _, _ = self.clean([rec("nct00000002 (Phase 3)")])
        self.assertEqual(rows[0]["Trial ID"], "NCT00000002")


# ── cache ────────────────────────────────────────────────────────────────────

def payload(rows=None, age_days=1.0, version=1):
    return {"version": version, "drug_name": "X", "records": rows if rows is not None else [{"Trial ID": "NCT00000001"}],
            "created_at": (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()}


class Cache(unittest.TestCase):
    def read(self, data, reindex=False, days=30):
        with mock.patch.object(config, "REINDEX", reindex), mock.patch.object(config, "FORMULATION_CACHE_DAYS", days), \
                mock.patch.object(fl.gcp_utils, "read_json", return_value=data) as rj:
            result = fl._cache_read("X")
        return result, rj

    def test_fresh_hit(self):
        result, rj = self.read(payload())
        self.assertEqual(result["records"], [{"Trial ID": "NCT00000001"}])
        self.assertEqual(rj.call_args.args[:2], (config.FORMULATION_CACHE_SUBFOLDER, "formulation_records.json"))
        self.assertEqual(rj.call_args.kwargs["drug_name"], "X")

    def test_expired(self):
        self.assertIsNone(self.read(payload(age_days=31))[0])
        self.assertIsNotNone(self.read(payload(age_days=31), days=60)[0])

    def test_reindex_skips_the_read_entirely(self):
        result, rj = self.read(payload(), reindex=True)
        self.assertIsNone(result)
        rj.assert_not_called()

    def test_bad_entries_are_misses(self):
        for bad in (None, {}, [], "x", payload(version=99), {"version": 1, "records": "no", "created_at": "x"},
                    {"version": 1, "records": [], "created_at": "garbage"}, {"version": 1, "records": []}):
            self.assertIsNone(self.read(bad)[0], bad)

    def test_naive_timestamp_is_accepted(self):
        data = payload()
        data["created_at"] = (datetime.utcnow() - timedelta(days=1)).isoformat()
        self.assertIsNotNone(self.read(data)[0])

    def test_read_error_is_a_miss(self):
        with mock.patch.object(fl.gcp_utils, "read_json", side_effect=RuntimeError("gcs down")):
            self.assertIsNone(fl._cache_read("X"))

    def test_write_payload_and_error_tolerance(self):
        with mock.patch.object(fl.gcp_utils, "write_json") as wj:
            fl._cache_write("X", [{"a": "b"}], [{"a": "b", "Source Quote": "q"}], [{"nct_id": "NCT00000001"}],
                            [{"brands": ["OZEMPIC", "WEGOVY"]}])
        data = wj.call_args.args[2]
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["sources"], {"clinicaltrials_gov": ["NCT00000001"], "fda_labels": ["OZEMPIC", "WEGOVY"]})
        self.assertEqual(data["records"], [{"a": "b"}])
        self.assertEqual(wj.call_args.kwargs["drug_name"], "X")
        with mock.patch.object(fl.gcp_utils, "write_json", side_effect=RuntimeError("gcs down")):
            fl._cache_write("X", [], [], [], [])    # must not raise


# ── the whole lookup ─────────────────────────────────────────────────────────

STUDY = fl._parse_study(ct_study("NCT00000001"))
LABEL = fl._parse_label(fda_label())


def gemini_reply(*records):
    return {"records": list(records)}


class Lookup(unittest.TestCase):
    def run_lookup(self, *, cached=None, sources=([STUDY], [LABEL], True), gemini=None, reindex=False):
        """Run lookup_formulation_rows with fakes; return (rows, mocks)."""
        gem = mock.AsyncMock(return_value=gemini)
        with mock.patch.object(fl, "_cache_read", return_value=cached) as cr, \
                mock.patch.object(fl, "fetch_sources", return_value=sources) as fs, \
                mock.patch.object(fl, "_cache_write") as cw, \
                mock.patch.object(fl.gcp_helpers, "call_gemini_json", gem):
            rows = asyncio.run(fl.lookup_formulation_rows("Semaglutide"))
        return rows, {"cache_read": cr, "fetch": fs, "cache_write": cw, "gemini": gem}

    def test_cache_hit_does_nothing_else(self):
        rows, m = self.run_lookup(cached=payload([{"Trial ID": "NCT00000001", "Route of Administration": "oral"}]))
        self.assertEqual(rows, [{"Trial ID": "NCT00000001", "Route of Administration": "oral"}])
        m["fetch"].assert_not_called()
        m["gemini"].assert_not_called()
        m["cache_write"].assert_not_called()

    def test_cached_empty_result_is_a_hit_too(self):
        rows, m = self.run_lookup(cached=payload([]))
        self.assertEqual(rows, [])
        m["fetch"].assert_not_called()

    def test_full_path_builds_rows_drops_invented_sources_and_caches(self):
        rows, m = self.run_lookup(gemini=gemini_reply(
            rec("FDA label: OZEMPIC", Phase="Approved", **{"Formulation Details": "pen"}),
            rec("NCT00000001"),
            rec("NCT12345678", **{"Formulation Details": "invented"}),
        ))
        self.assertEqual([r["Trial ID"] for r in rows], ["FDA label: OZEMPIC", "NCT00000001"])
        prompt = m["gemini"].call_args.args[0]
        self.assertIn("FDA label: OZEMPIC", prompt)
        self.assertIn("NCT00000001", prompt)
        self.assertEqual(m["gemini"].call_args.args[2], "FORMULATION")
        m["cache_write"].assert_called_once()
        written = m["cache_write"].call_args.args
        self.assertEqual(written[0], "Semaglutide")
        self.assertEqual(written[1], rows)
        self.assertEqual(len(written[2]), 2)          # audit records with their quotes

    def test_nothing_found_is_cached_as_empty_and_gemini_is_not_called(self):
        rows, m = self.run_lookup(sources=([], [], True))
        self.assertEqual(rows, [])
        m["gemini"].assert_not_called()
        m["cache_write"].assert_called_once()
        self.assertEqual(m["cache_write"].call_args.args[1], [])

    def test_both_sources_down_returns_nothing_and_caches_nothing(self):
        rows, m = self.run_lookup(sources=([], [], False))
        self.assertEqual(rows, [])
        m["gemini"].assert_not_called()
        m["cache_write"].assert_not_called()

    def test_one_source_down_still_answers_but_is_not_cached(self):
        rows, m = self.run_lookup(sources=([STUDY], [], False), gemini=gemini_reply(rec("NCT00000001")))
        self.assertEqual(len(rows), 1)
        m["cache_write"].assert_not_called()

    def test_gemini_failure_returns_nothing_and_caches_nothing(self):
        for bad in (None, "text", [], 5):
            rows, m = self.run_lookup(gemini=bad)
            self.assertEqual(rows, [])
            m["cache_write"].assert_not_called()

    def test_gemini_answer_without_records(self):
        rows, m = self.run_lookup(gemini={"unexpected": 1})
        self.assertEqual(rows, [])
        m["cache_write"].assert_called_once()     # a valid reply that found nothing is cached

    def test_fetch_sources_marks_incomplete_when_an_api_fails(self):
        with mock.patch.object(fl, "fetch_ctgov_studies", side_effect=fl.SourceUnavailable("x")), \
                mock.patch.object(fl, "fetch_openfda_labels", return_value=[LABEL]):
            self.assertEqual(fl.fetch_sources("X"), ([], [LABEL], False))
        with mock.patch.object(fl, "fetch_ctgov_studies", return_value=[STUDY]), \
                mock.patch.object(fl, "fetch_openfda_labels", side_effect=fl.SourceUnavailable("x")):
            self.assertEqual(fl.fetch_sources("X"), ([STUDY], [], False))
        with mock.patch.object(fl, "fetch_ctgov_studies", return_value=[STUDY]), \
                mock.patch.object(fl, "fetch_openfda_labels", return_value=[]):
            self.assertEqual(fl.fetch_sources("X"), ([STUDY], [], True))


# ── Excel first, lookup second ───────────────────────────────────────────────

class Fallback(unittest.TestCase):
    def run_fallback(self, excel, enabled=True, lookup=None):
        look = mock.AsyncMock(return_value=[{"Trial ID": "NCT00000009"}]) if lookup is None else lookup
        with mock.patch.object(fl, "get_drug_rows", return_value=excel), \
                mock.patch.object(config, "FORMULATION_LOOKUP_ENABLED", enabled), \
                mock.patch.object(fl, "lookup_formulation_rows", look):
            rows = asyncio.run(fl.get_drug_rows_with_fallback("X"))
        return rows, look

    def test_excel_wins(self):
        rows, look = self.run_fallback([{"Molecule": "X", "Trial ID": "T1"}])
        self.assertEqual(rows, [{"Molecule": "X", "Trial ID": "T1"}])
        look.assert_not_called()

    def test_lookup_fills_in_when_excel_has_nothing(self):
        rows, look = self.run_fallback([])
        self.assertEqual(rows, [{"Trial ID": "NCT00000009"}])
        look.assert_awaited_once_with("X")

    def test_can_be_switched_off(self):
        rows, look = self.run_fallback([], enabled=False)
        self.assertEqual(rows, [])
        look.assert_not_called()

    def test_a_crash_in_the_lookup_never_reaches_the_analysis(self):
        rows, _ = self.run_fallback([], lookup=mock.AsyncMock(side_effect=RuntimeError("boom")))
        self.assertEqual(rows, [])


# ── the rows really work as Step 2 input ─────────────────────────────────────

class WithStep2(unittest.TestCase):
    def test_rows_format_into_the_step2_prompt(self):
        rows, _, _ = fl.clean_records(
            [rec("FDA label: OZEMPIC", **{"Formulation Details": "Injection {2 mg/3 mL} %s pen", "Device Description": "Pen injector"})],
            set(), ["OZEMPIC"])
        block = step2._format_rows_for_prompt(rows)
        self.assertIn("Record 1:", block)
        self.assertIn("Device Description: Pen injector", block)
        prompt = step2.STEP2_PROMPT.format(filename="US1.pdf", patent_number="US1", jurisdiction="US",
                                           claim_category="Formulation", row_count=len(rows),
                                           formulation_rows=block, context="patent text")
        self.assertIn("Injection {2 mg/3 mL} %s pen", prompt)

    def test_every_row_key_is_one_step2_already_understands(self):
        allowed = {"Trial ID", "Phase", *step2._STEP2_COLUMNS}
        rows, _, _ = fl.clean_records([rec(**{"Device Description": "pen", "Combination Tech/Process": "SNAC"})],
                                      {"NCT00000001"}, [])
        self.assertTrue(set(rows[0]) <= allowed)


if __name__ == "__main__":
    unittest.main()
