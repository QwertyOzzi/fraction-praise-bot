import unittest

from agent_directory_v2 import build_projection, candidate_document_id, MigrationConflict


class AgentProjectionTests(unittest.TestCase):
    def test_preserves_legacy_rows_and_links_profile(self):
        key = "a" * 32
        original_frp = {"id": "r1", "nick": "Gomez_Addams", "qty": 3,
                        "proof": {"text": "case", "links": ["example"]}}
        praise = {"id": "r2", "nick": "Gomez_Addams", "qty": 4}
        rows = build_projection([original_frp], [praise], {
            "42": {"nick": "Gomez_Addams", "profileId": key,
                   "rank": "8", "department": "Academy"}
        })
        self.assertEqual(list(rows), ["profile-" + key])
        self.assertEqual(rows["profile-" + key]["sheetRows"]["fractionrp"], original_frp)
        self.assertEqual(rows["profile-" + key]["sheetRows"]["praise"], praise)
        self.assertEqual(rows["profile-" + key]["telegramUid"], "42")

    def test_detect_duplicate_nicks_and_conflicting_binding(self):
        with self.assertRaises(MigrationConflict):
            build_projection([{"nick": "Gomez_Addams"}, {"nick": "gomez_addams"}], [])
        with self.assertRaises(MigrationConflict):
            build_projection(
                [{"nick": "Gomez_Addams", "profileId": "a" * 32}],
                [{"nick": "Gomez_Addams", "profileId": "b" * 32}])

    def test_preserves_registered_profile_without_sheet_rows(self):
        projection = build_projection([], [], {
            "50": {"nick": "Old_Staff", "rank": "5", "profileId": "f" * 32}
        })
        self.assertIn("profile-" + "f" * 32, projection)
        self.assertEqual(projection["profile-" + "f" * 32]["sheetRows"], {})

    def test_profile_duplicates_cannot_merge(self):
        with self.assertRaises(MigrationConflict):
            build_projection([], [], {
                "51": {"nick": "Agent_One", "profileId": "f" * 32},
                "52": {"nick": "Agent_Two", "profileId": "f" * 32}
            })

    def test_no_mutation(self):
        frp = [{"nick": "Agent_One", "qty": 5}]
        projection = build_projection(frp, [])
        projection[next(iter(projection))]["sheetRows"]["fractionrp"]["qty"] = 0
        self.assertEqual(frp[0]["qty"], 5)

    def test_stable_legacy_candidate(self):
        self.assertEqual(candidate_document_id("Agent_One"),
                         candidate_document_id("agent_one"))


if __name__ == "__main__":
    unittest.main()
