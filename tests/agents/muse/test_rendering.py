import re
from datetime import timedelta

import pytest

from aether.agents.muse.schemas import CommunicationPreferences, HandoffOptions
from aether.agents.muse.workflow import (
    feedback_preferences,
    handoff_options,
    personalization,
    render_message,
)
from aether.communication.contracts import CommunicationRequestPayload, validate_message
from aether.storage.repositories import KnowledgeGraphRepository, PersonRepository


def test_required_sections_and_no_invented_numbers():
    request = CommunicationRequestPayload(
        what_happened="Evaluated build B-12.",
        results=[{"passed": 12, "duration": 0.5}],
        verified=["12 tests passed."],
        uncertain=["No performance measurement."],
        blockers=["Hardware unavailable."],
    )
    rendered = render_message(request, CommunicationPreferences(), HandoffOptions())
    for section in (
        "What happened",
        "Reported results",
        "Verified",
        "Uncertain",
        "Blockers",
        "Decisions required",
        "Authorization requests",
    ):
        assert section in rendered
    assert "- duration: 0.5" in rendered
    assert "not independently verified by MUSE" in rendered
    assert set(re.findall(r"\d+(?:\.\d+)?", rendered)) == {"12", "0.5"}
    assert "No performance measurement." in rendered
    with pytest.raises(ValueError, match="unsupported number or identifier"):
        render_message(
            request, CommunicationPreferences(), HandoffOptions(), context_names=["Invented-999"]
        )


def test_shorter_and_formal_preferences_change_presentation_without_losing_facts():
    request = CommunicationRequestPayload(
        what_happened="Evaluation completed.",
        results=[{"passed": 12, "source": "local test runner"}],
        verified=["12 tests passed."],
        blockers=["Hardware unavailable."],
    )
    normal = render_message(request, CommunicationPreferences(), HandoffOptions())
    short = render_message(
        request, CommunicationPreferences(length="short", tone="direct"), HandoffOptions()
    )
    formal = render_message(
        request, CommunicationPreferences(length="long", formality="formal"), HandoffOptions()
    )
    assert len(short) < len(normal) < len(formal)
    assert formal.startswith("Please review the update below.")
    for text in (normal, short, formal):
        assert "12 tests passed." in text
        assert "Hardware unavailable." in text
        assert "local test runner" in text


def test_one_question_per_decision_with_options_and_deadlines(clock):
    deadline = clock.now + timedelta(hours=1)
    request = CommunicationRequestPayload(
        what_happened="A decision is needed.",
        decisions_required=["Choose the implementation", "Proceed with testing?"],
        personalization_context={
            "decision_options": {"Choose the implementation": ["Local", "Remote"]},
            "decision_deadlines": {"Choose the implementation": deadline.isoformat()},
        },
    )
    text = render_message(request, CommunicationPreferences(), handoff_options(request))
    assert text.count("Choose the implementation?") == 1
    assert text.count("Proceed with testing?") == 1
    assert "Options: Local; Remote." in text
    assert f"Deadline: {deadline.isoformat()}." in text
    assert "Deadline: not supplied." in text
    assert text.count("No response = no authorization.") == 2


def test_identifier_guard_accepts_json_numeric_and_unicode_formatting():
    request = CommunicationRequestPayload(
        what_happened="Evaluation completed.",
        results=[{"error": 1e-7, "name": "caf\u00e9"}],
        decisions_required=["Proceed"],
        personalization_context={"decision_deadlines": {"Proceed": "2026-10-31T01:00:00Z"}},
    )
    text = render_message(request, CommunicationPreferences(), handoff_options(request))
    assert "- error: 1e-07" in text
    assert "- name: caf\u00e9" in text
    assert "2026-10-31T01:00:00+00:00" in text


@pytest.mark.parametrize("length", ["short", "normal", "long"])
@pytest.mark.parametrize("count", [0, 1, 9])
def test_task_results_are_readable_and_not_independently_verified(length, count):
    request = CommunicationRequestPayload(
        what_happened="Objective completed.",
        results=[
            {
                "objective": "Add a text_statistics capability",
                "tasks_completed": count,
                "note": "Offline run",
            }
        ],
        verified=["Acceptance criteria passed."],
        uncertain=["Performance not measured."],
    )
    text = render_message(request, CommunicationPreferences(length=length), HandoffOptions())
    label = "task" if count == 1 else "tasks"
    assert f"{count} {label} completed for 'Add a text_statistics capability'" in text
    assert "note: Offline run" in text
    assert "tasks_completed" not in text
    assert '{"objective"' not in text
    reported = text.split("Reported results", 1)[1].split("Verified", 1)[0]
    assert "not independently verified by MUSE" in reported
    assert "Acceptance criteria passed." not in reported
    assert "Performance not measured." in text.split("Uncertain", 1)[1]


def test_unknown_nested_fields_preserve_values_without_raw_json():
    request = CommunicationRequestPayload(
        what_happened="Reported measurements.",
        results=[
            {
                "metrics": {"run2": {"duration": 0.5, "enabled": True, "error": None}},
                "artifacts": ["caf\u00e9-12.txt", {"name": "second.txt", "bytes": 42}],
                "empty_mapping": {},
                "empty_list": [],
            }
        ],
    )
    text = render_message(request, CommunicationPreferences(), HandoffOptions())
    for line in (
        "metrics: run2: duration: 0.5",
        "metrics: run2: enabled: true",
        "metrics: run2: error: null",
        "artifacts: caf\u00e9-12.txt",
        "artifacts: name: second.txt",
        "artifacts: bytes: 42",
        "empty_mapping: (empty mapping)",
        "empty_list: (empty list)",
    ):
        assert f"- {line}" in text
    assert '{"' not in text
    assert '["' not in text
    with pytest.raises(ValueError, match="unsupported number or identifier"):
        render_message(
            request, CommunicationPreferences(), HandoffOptions(), opening="Completed 999 tasks."
        )


@pytest.mark.parametrize("count", [True, -1, "9", 0.5, None])
def test_invalid_task_counts_remain_literal_reported_fields(count):
    request = CommunicationRequestPayload(
        what_happened="Reported fields.",
        results=[{"objective": "Evaluate", "tasks_completed": count}],
    )
    text = render_message(request, CommunicationPreferences(), HandoffOptions())
    assert "- objective: Evaluate" in text
    assert "- tasks_completed:" in text
    assert "tasks completed" not in text


def test_count_without_title_and_empty_results_do_not_invent_an_objective():
    request = CommunicationRequestPayload(
        what_happened="Reported fields.",
        results=[{"tasks_completed": 9}, {}, {"objective": ""}],
    )
    text = render_message(request, CommunicationPreferences(), HandoffOptions())
    assert "- 9 tasks completed" in text
    assert "completed for" not in text
    assert "- No result fields supplied." in text
    assert "- objective: " in text


def test_identifier_guard_handles_decoded_multiline_and_escaped_strings():
    request = CommunicationRequestPayload(
        what_happened="Reported fields.",
        results=[
            {
                "objective": "First line\njob-9",
                "tasks_completed": 9,
                "message": "First line\nbuild-12\tcaf\u00e9-42",
                "path": "C:\\run42",
            }
        ],
    )
    text = render_message(request, CommunicationPreferences(), HandoffOptions())
    assert "job-9" in text
    assert "build-12\tcaf\u00e9-42" in text
    assert "C:\\run42" in text
    with pytest.raises(ValueError, match="unsupported number or identifier"):
        render_message(
            request, CommunicationPreferences(), HandoffOptions(), context_names=["job-999"]
        )


def test_invalid_options_are_rejected_without_echoing_input():
    request = CommunicationRequestPayload(
        what_happened="Updated.",
        personalization_context={"routine": {"private": "sensitive-input"}},
    )
    with pytest.raises(ValueError, match="Invalid MUSE handoff") as error:
        handoff_options(request)
    assert "sensitive-input" not in str(error.value)


async def test_only_explicit_person_preferences_apply(database, ctx, request_message, person_id):
    people = PersonRepository(database.sessions)
    await people.set_preference(person_id, "length", "short")
    await people.set_preference(person_id, "tone", "warm")
    await people.set_preference(person_id, "unrelated_private_fact", "not relevant")
    request = validate_message(request_message(personalization_context={"tone": "direct"}))
    preferences, _ = await personalization(request, handoff_options(request), ctx)
    assert preferences.length == "short"
    assert preferences.tone == "warm"
    assert "unrelated_private_fact" not in preferences.model_dump()


async def test_graph_is_owner_permission_and_objective_scoped(
    database, ctx, request_message, person_id, objective_id
):
    graph = KnowledgeGraphRepository(database.sessions)
    other = await PersonRepository(database.sessions).create(
        display_name="Other", access_level="full"
    )
    relevant = await graph.upsert_entity(
        entity_type="project",
        name="Relevant project",
        actor="MUSE",
        person_id=person_id,
        attributes={"objective_id": str(objective_id), "private": "never render"},
    )
    wrong_owner = await graph.upsert_entity(
        entity_type="project",
        name="Other person's project",
        actor="MUSE",
        person_id=other,
        attributes={"objective_id": str(objective_id)},
    )
    unrelated = await graph.upsert_entity(
        entity_type="project",
        name="Unrelated project",
        actor="MUSE",
        person_id=person_id,
        attributes={"objective_id": "another-objective"},
    )
    request = validate_message(
        request_message(
            personalization_context={
                "context_entity_ids": [str(relevant), str(wrong_owner), str(unrelated)],
                "context_names": [
                    "Relevant project",
                    "Other person's project",
                    "Unrelated project",
                ],
            }
        )
    )
    preferences, names = await personalization(request, handoff_options(request), ctx)
    assert names == ["Relevant project"]
    rendered = render_message(request, preferences, handoff_options(request), context_names=names)
    assert "Relevant project" in rendered
    assert "never render" not in rendered
    restricted = await PersonRepository(database.sessions).create(display_name="Restricted")
    restricted_request = request.model_copy(update={"audience": restricted})
    _, names = await personalization(restricted_request, handoff_options(request), ctx)
    assert names == []


async def test_missing_read_permission_omits_personalization(ctx, request_message, monkeypatch):
    original = ctx.permission_service.is_allowed

    async def allowed(agent, action):
        return action != "preferences.read" and await original(agent, action)

    monkeypatch.setattr(
        type(ctx.permission_service),
        "is_allowed",
        lambda self, agent, action: allowed(agent, action),
    )
    request = validate_message(request_message())
    preferences, names = await personalization(request, handoff_options(request), ctx)
    assert preferences == CommunicationPreferences()
    assert names == []


@pytest.mark.parametrize(
    ("text", "changes"),
    [
        ("Shorter please!", {"length": "short"}),
        ("more detail please", {"detail": "detailed"}),
        ("more formal please", {"formality": "formal"}),
        ("warmer please", {"tone": "warm"}),
    ],
)
def test_explicit_feedback_commands(text, changes):
    assert feedback_preferences(text) == changes


def test_feedback_does_not_infer_preferences_from_unrelated_text():
    with pytest.raises(ValueError, match="supported explicit"):
        feedback_preferences("I suppose you know my personality now")
