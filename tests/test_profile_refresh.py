import yaml

from conftest import docs
from curator.profile_refresh import JevQuestion, LearnedSource, Option, render_questions, render_sources, split_sources


def test_sources_keep_fixed_part_byte_for_byte():
    text = docs()[1]
    head, learned = split_sources(text)
    new = render_sources(head, [LearnedSource(kind="mastodon_hashtag", value="esp32", reason="r", added="2026-10-01")])
    assert new.startswith(head)
    assert yaml.safe_load(new)["fixed"] == yaml.safe_load(text)["fixed"]
    assert yaml.safe_load(new)["learned"][0]["value"] == "esp32"


def test_choice_questions_always_get_other():
    q = JevQuestion(id="topic", type="choice", instructions="Which?", options=[Option(key="ai", description="AI")])
    out = yaml.safe_load(render_questions([q]))
    assert set(out["topic"]["criteria"]) == {"ai", "other"}


def test_noul_criteria_survive_a_rewrite():
    q = JevQuestion(id="english", type="noul", instructions="English?",
                    options=[Option(key="false", description="No"), Option(key="true", description="Yes")])
    out = yaml.safe_load(render_questions([q]))
    assert out["english"]["criteria"] == {"true": "Yes", "false": "No"}


def test_sample_questions_survive_a_rewrite():
    """What the daily job writes back means the same as what it read."""
    seed = yaml.safe_load(docs()[2])
    qs = []
    for qid, q in seed.items():
        c = q.get("criteria") or []
        opts = ([Option(key=str(k), description=v or "") for k, v in c.items()] if isinstance(c, dict)
                else [Option(key=str(i), description=v) for i, v in enumerate(c)])
        qs.append(JevQuestion(id=qid, type=q["type"], instructions=q["instructions"], options=opts))
    assert yaml.safe_load(render_questions(qs)) == seed
