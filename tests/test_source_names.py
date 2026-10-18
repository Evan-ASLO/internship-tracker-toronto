from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import scrape
from catalog_guard import main as catalog_guard_main


FIXTURE = Path(__file__).parent / "fixtures" / "source_name_import.json"


class SourceNameImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sections = json.loads(FIXTURE.read_text(encoding="utf-8"))
        cls.extracted = [
            name for names in cls.sections.values() for name in names
        ]

    def test_all_numbered_sections_and_recommendations_are_complete(self):
        counts = {
            "01": 18, "02": 18, "03": 22, "04": 94, "05": 26,
            "06": 19, "07": 18, "08": 24, "09": 9, "10": 9,
            "11": 13, "12": 15, "13": 30, "14": 19, "15": 17,
            "16": 17, "17": 15, "18": 16, "19": 21, "20": 11,
            "21": 15, "22": 26, "23": 12, "24": 16, "25": 37,
            "26": 18, "27": 17, "28": 11, "29": 10,
            "recommendations": 23,
        }
        self.assertEqual(
            {section: len(names) for section, names in self.sections.items()},
            counts,
        )
        self.assertEqual(len(self.extracted), 616)
        self.assertEqual(len(set(self.extracted)), 565)
        expected = {
            scrape.canonical_source_name(name) for name in self.extracted
        }
        self.assertEqual(len(expected), 556)
        self.assertEqual(set(scrape.SOURCE_NAMES), expected)
        self.assertEqual(len(scrape.SOURCE_NAMES), 556)
        self.assertEqual(len({name.casefold() for name in scrape.SOURCE_NAMES}), 556)

    def test_recommendations_are_repeated_sources_not_new_names(self):
        numbered = {
            scrape.canonical_source_name(name)
            for section, names in self.sections.items()
            if section != "recommendations"
            for name in names
        }
        recommended = {
            scrape.canonical_source_name(name)
            for name in self.sections["recommendations"]
        }
        self.assertLessEqual(recommended, numbered)
        self.assertEqual(scrape.SOURCE_NAMES.count("League"), 1)
        self.assertEqual(scrape.SOURCE_NAMES.count("Unilever"), 1)

    def test_header_names_accents_and_short_names_are_retained(self):
        for name in (
            "Greenhouse", "Strategy&", "NRC", "Government of Canada FSWEP",
            "Hydro-Québec", "AtkinsRéalis", "Nestlé", "L'Oréal",
            "Queen's", "Queen's MyCareer",
        ):
            with self.subTest(name=name):
                self.assertIn(name, scrape.SOURCE_NAMES)

    def test_aliases_are_explicit_and_do_not_collapse_distinct_entities(self):
        aliases = {
            "FSWEP": "Government of Canada FSWEP",
            "CBC": "CBC/Radio-Canada",
            "MaRS": "MaRS Discovery District",
            "Intact": "Intact Financial",
            "Ontario Lottery and Gaming": "OLG",
            "Canadian Natural": "Canadian Natural Resources",
            "Government Job Bank": "Job Bank Canada",
            "SuccessFactors": "SAP SuccessFactors",
            "cpp": "CPP Investments",
            "otpp": "Ontario Teachers' Pension Plan",
            "otppb": "Ontario Teachers' Pension Plan",
            "sunlife": "Sun Life",
            "deloittecanada": "Deloitte",
        }
        for alias, canonical in aliases.items():
            with self.subTest(alias=alias):
                self.assertEqual(scrape.canonical_source_name(alias), canonical)
        for group in (
            ("RBC", "RBC Capital Markets", "RBC Global Asset Management",
             "RBC Future Launch opportunities"),
            ("HP", "HPE"),
            ("Amazon", "AWS"),
            ("Equitable Bank", "EQ Bank"),
            ("Square", "Block"),
            ("Economical", "Definity"),
            ("Sobeys", "Empire"),
            ("Lowe's", "RONA"),
            ("University of Toronto", "University of Toronto CLNx"),
            ("York", "York University career boards", "York Region"),
            ("Government of Canada Jobs", "Job Bank Canada",
             "Government of Canada student employment"),
            ("Oracle", "Oracle Recruiting Cloud"),
            ("SAP", "SAP SuccessFactors"),
            ("Mars", "MaRS Discovery District"),
        ):
            with self.subTest(group=group):
                self.assertTrue(set(group).issubset(scrape.SOURCE_NAMES))
                self.assertEqual(
                    len({scrape.canonical_source_name(name) for name in group}),
                    len(group),
                )
        self.assertEqual(scrape.canonical_source_name("Mars"), "Mars")
        self.assertEqual(
            scrape.canonical_source_name("New RBC programme"), "New RBC programme"
        )

    def test_inventory_contains_names_only_not_commentary_or_examples(self):
        excluded = {
            "ATS", "General", "Strategy", "Big Tech", "Big Five",
            "Federal", "Ontario", "Municipal", "Tier 1", "Tier 2", "Tier 3",
            "Canadian Internship Engine", "NORMALIZATION", "DEDUPLICATION",
            "FILTERING", "DATABASE", "COMPANIES", "Company A", "Company B",
            "Company C", "Example Company", "Business Analyst Intern",
            "Toronto, ON", "Canada", "Internship", "Summer 2027",
            "company", "title", "location", "country", "job_type", "term",
            "category", "posted_date", "deadline", "url", "source", "ats",
            "remote", "GenericStructuredDataAdapter",
        }
        self.assertTrue(excluded.isdisjoint(scrape.SOURCE_NAMES))
        for name in scrape.SOURCE_NAMES:
            with self.subTest(name=name):
                self.assertIsInstance(name, str)
                self.assertEqual(name, scrape.clean_text(name))
                self.assertNotRegex(
                    name, r"https?://|Adapter\b|_scraper\.py|[★│┌┐└┘]"
                )

    def test_loader_uses_file_relative_path_and_rejects_invalid_data(self):
        self.assertEqual(
            scrape.SOURCE_NAMES_PATH,
            Path(scrape.__file__).with_name("source_names.json"),
        )
        self.assertEqual(scrape.load_source_names(), scrape.SOURCE_NAMES)
        invalid = ({}, [], ["Name", 1], ["Name", "name"], [" Name "], [""])
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "source_names.json"
            for payload in invalid:
                with self.subTest(payload=payload):
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    with patch.object(scrape, "SOURCE_NAMES_PATH", path):
                        with self.assertRaises(ValueError):
                            scrape.load_source_names()


class SourceNameConsumptionTests(unittest.TestCase):
    def setUp(self):
        scrape.reset_coverage()

    def test_inventory_is_linked_to_every_registered_task(self):
        inventory = scrape.source_name_inventory()
        self.assertEqual(
            {key for entry in inventory for key in entry["source_keys"]},
            {key for _, _, key in scrape.TASKS},
        )
        self.assertEqual(
            len(inventory), len({entry["name"].casefold() for entry in inventory})
        )
        by_name = {entry["name"]: entry for entry in inventory}
        for name in scrape.SOURCE_NAMES:
            self.assertIn(name, by_name)
        requested = [by_name[name] for name in scrape.SOURCE_NAMES]
        self.assertEqual(
            sum(entry["status"] == "configured" for entry in requested), 80
        )
        self.assertEqual(
            sum(entry["status"] == "pending_configuration" for entry in requested),
            476,
        )
        for entry in inventory:
            with self.subTest(name=entry["name"]):
                self.assertEqual(
                    entry["status"],
                    "configured" if entry["source_keys"] else "pending_configuration",
                )
        self.assertIn(
            "workday:rbc.wd3/RBCEarlyTalent1", by_name["RBC"]["source_keys"]
        )
        self.assertEqual(
            by_name["Government of Canada Jobs"]["source_keys"], ["gcjobs:public"]
        )
        self.assertEqual(
            by_name["Canadian Natural Resources"]["source_keys"],
            ["oracle:CNRL-Campus"],
        )
        self.assertEqual(
            by_name["SaskPower"]["source_keys"], ["successfactors:saskpower"]
        )
        self.assertEqual(
            by_name["Ontario Teachers' Pension Plan"]["source_keys"],
            ["workday:otppb.wd3/OntarioTeachers_Careers"],
        )
        for name, key in {
            "Hatch": "successfactors:hatch",
            "Aecon": "successfactors:aecon",
            "Kiewit": "successfactors:kiewit",
            "City of Toronto": "successfactors:cityoftoronto",
            "City of Mississauga": "successfactors:cityofmississauga",
            "City of Ottawa": "successfactors:cityofottawa",
            "BlackBerry": "workday:bb.wd3/Student",
            "PointClickCare": "lever:pointclickcare",
            "Caseware": "lever:caseware",
            "BenchSci": "lever:benchsci",
        }.items():
            with self.subTest(configured=name):
                self.assertEqual(by_name[name]["source_keys"], [key])
        for name in (
            "Microsoft", "AWS", "Job Bank Canada", "RBC Capital Markets",
            "RBC Future Launch opportunities", "University of Toronto CLNx",
            "Greenhouse", "Lever", "Workday", "SAP SuccessFactors",
            "Oracle Recruiting Cloud",
        ):
            with self.subTest(pending=name):
                self.assertEqual(by_name[name]["status"], "pending_configuration")
                self.assertEqual(by_name[name]["source_keys"], [])

    def test_pending_names_do_not_change_tasks_coverage_or_completeness_guards(self):
        tasks = deepcopy(scrape.TASKS)
        retained = scrape.RETAINED_SOURCE_KEYS.copy()
        daily = scrape.DAILY_SOURCE_KEYS.copy()
        key = "workday:rbc.wd3/RBCEarlyTalent1"
        scrape.set_coverage(key, complete=False, errors=["fixture failure"])
        coverage = deepcopy(scrape.COVERAGE)
        state = scrape.source_state_payload()
        with patch.object(
            scrape, "SOURCE_NAMES",
            (*scrape.SOURCE_NAMES, "Pending source fixture"),
        ):
            added = next(
                entry for entry in scrape.source_name_inventory()
                if entry["name"] == "Pending source fixture"
            )
        self.assertEqual(
            added,
            {"name": "Pending source fixture",
             "status": "pending_configuration", "source_keys": []},
        )
        self.assertEqual(scrape.TASKS, tasks)
        self.assertEqual(scrape.RETAINED_SOURCE_KEYS, retained)
        self.assertEqual(scrape.DAILY_SOURCE_KEYS, daily)
        self.assertEqual(scrape.COVERAGE, coverage)
        self.assertEqual(scrape.source_state_payload(), state)
        self.assertFalse(scrape.COVERAGE[key]["complete"])

    def test_pending_inventory_neither_fails_nor_weakens_publication_guards(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "snapshot.json"
            for key in ("lever:waabi", "gcjobs:public"):
                for complete in (True, False):
                    with self.subTest(key=key, complete=complete):
                        scrape.reset_coverage()
                        scrape.set_coverage(
                            key, complete=complete,
                            status="fresh" if complete else "blocked",
                        )
                        before = scrape.source_state_payload()
                        scrape.source_name_inventory()
                        self.assertEqual(scrape.source_state_payload(), before)
                        path.write_text(json.dumps({
                            "fetched_at": "2026-10-17T20:00:00Z",
                            "jobs": [],
                            "source_state": before,
                        }), encoding="utf-8")
                        for extra in ([], ["--allow-drop"]):
                            with (
                                redirect_stdout(io.StringIO()),
                                redirect_stderr(io.StringIO()),
                            ):
                                result = catalog_guard_main([
                                    "--current", str(path), *extra,
                                ])
                            self.assertEqual(result, 0 if complete else 1)

    def test_list_command_consumes_inventory_without_scraping_or_writing(self):
        output = io.StringIO()
        with (
            redirect_stdout(output),
            patch.object(scrape, "main") as main,
            patch.object(
                scrape.requests.sessions.Session, "request",
                side_effect=AssertionError("network not allowed"),
            ),
            patch("builtins.open", side_effect=AssertionError("writes not allowed")),
        ):
            scrape.cli(["--list-source-names"])
        main.assert_not_called()
        self.assertEqual(json.loads(output.getvalue()), scrape.source_name_inventory())

    def test_normal_scraper_reads_inventory_before_starting_collectors(self):
        with patch.object(
            scrape, "source_name_inventory",
            side_effect=RuntimeError("inventory read fixture"),
        ):
            with self.assertRaisesRegex(RuntimeError, "inventory read fixture"):
                scrape.main()

    def test_default_cli_preserves_normal_entry_point(self):
        with patch.object(scrape, "main") as main:
            scrape.cli([])
        main.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
