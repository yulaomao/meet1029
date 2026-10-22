from hypothesis import given, settings, strategies as st

from app.services.meeting_state import merge_patch


scalars = st.one_of(st.text(max_size=20), st.integers(-10, 10), st.none())
patch_strat = st.fixed_dictionaries(
    {
        "current_topic": st.text(max_size=30),
        "open_questions": st.lists(st.text(max_size=20), max_size=5),
    }
)


@given(base=patch_strat, patch=patch_strat)
@settings(max_examples=40)
def test_state_patch_merge(base, patch):
    merged = merge_patch(base, patch)
    assert merged["current_topic"] == patch["current_topic"]
    for q in patch["open_questions"]:
        assert q in merged["open_questions"]
    # merging twice is stable for same patch
    again = merge_patch(merged, patch)
    assert again["current_topic"] == merged["current_topic"]
