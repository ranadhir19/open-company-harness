"""Channel layer: mention routing, breakout threads, transcripts."""

import pytest

from open_company_harness import build_harness


@pytest.fixture()
def workspace():
    return build_harness().workspace


def test_mention_parsing(workspace):
    msg = workspace.post("eng", "ana", "@reviewer please check this, @ben")
    assert msg.mentions == ("reviewer", "ben")
    assert msg.author == "ana"
    assert msg.channel_id == "eng"
    plain = workspace.post("eng", "ben", "no mentions here, just an @ sign")
    assert plain.mentions == ()


def test_breakout_creates_agent_owned_thread_and_note(workspace):
    parent = workspace.post("eng", "ana", "@reviewer this needs a deep look")
    thread = workspace.breakout(parent, "reviewer", title="deep look")
    assert thread.owner == "agent:reviewer"
    assert thread.channel_id == "eng"
    assert thread.parent_message_id == parent.id
    assert thread.title == "deep look"
    notes = [m for m in workspace.transcript("eng")
             if m.author == "agent:reviewer"]
    assert len(notes) == 1
    assert thread.id in notes[0].text
    assert notes[0].thread_id is None


def test_transcript_separation_by_thread(workspace):
    parent = workspace.post("eng", "ana", "main channel question")
    thread = workspace.breakout(parent, "reviewer")
    workspace.post("eng", "ben", "main channel follow-up")
    workspace.post("eng", "agent:reviewer", "working note one",
                   thread_id=thread.id)
    workspace.post("eng", "agent:reviewer", "working note two",
                   thread_id=thread.id)
    workspace.post("gtm", "cy", "other channel message")

    main = workspace.transcript("eng")
    assert [m.text for m in main] == [
        "main channel question",
        f"(moved my work on this into a side thread: {thread.id})",
        "main channel follow-up",
    ]
    side = workspace.transcript("eng", thread_id=thread.id)
    assert [m.text for m in side] == ["working note one", "working note two"]
    assert all(m.thread_id == thread.id for m in side)


def test_transcripts_do_not_mix_channels(workspace):
    workspace.post("eng", "ana", "engineering only")
    workspace.post("gtm", "cy", "go-to-market only")
    assert [m.text for m in workspace.transcript("eng")] == ["engineering only"]
    assert [m.text for m in workspace.transcript("gtm")] == ["go-to-market only"]
    assert workspace.transcript("studio") == []
