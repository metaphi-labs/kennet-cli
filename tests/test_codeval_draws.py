"""The edits and revealed files drawn here must be the service's own: a fixed profile and seeds give fixed draws, the
same vector the service tests its own draws against."""

import json
from pathlib import Path

from metaphi import codeval_draws

VECTOR = Path(__file__).parent / "data" / "codeval_draws.json"


def test_the_seeded_draws_match_the_shared_vector():
    v = json.loads(VECTOR.read_text())
    assert codeval_draws.drawn({"repos": v["repos"]}, v["seeds"]) == (v["edits"], v["blobs"])


def test_a_repository_draws_from_its_own_seed_alone():
    v = json.loads(VECTOR.read_text())
    alone = codeval_draws.drawn({"repos": v["repos"][1:]}, v["seeds"][1:])
    assert alone == (v["edits"][1:], v["blobs"][1:])


def test_a_history_with_enough_candidates_plants_nothing():
    v = json.loads(VECTOR.read_text())
    assert v["edits"][0] and not v["edits"][1]


def test_the_facts_sent_to_picks_decide_candidates_as_the_profile_does():
    for repo in json.loads(VECTOR.read_text())["repos"]:
        of, wide, narrow = codeval_draws.facts(repo), [], []
        for i, f in of.items():
            if f["has_parent"] and f["test_code"] and 1 <= f["source_files"] <= codeval_draws.MAX_SOURCE_FILES:
                big = f["source_files"] >= codeval_draws.WIDE[0] and f["source_lines"] > codeval_draws.WIDE[1]
                (wide if big else narrow).append(i)
        assert (wide, narrow) == codeval_draws.candidates(repo)
        assert {i: f["era"] for i, f in of.items()} == codeval_draws.eras(repo)
