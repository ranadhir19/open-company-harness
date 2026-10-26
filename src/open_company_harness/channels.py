"""The channel layer: agents live where people already work.

The workspace is a set of channels, DMs and threads. People post
messages; when a message mentions an agent, the harness can run the
agent *in place* — but long agent work in a busy human thread is
noise, so the layer supports the **breakout** pattern: the agent's
work moves into its own thread, and only a summary returns to the
human conversation.

Chat platforms are an adapter seam. ``ChatAdapter`` is the interface
(Slack-shaped: channels, threads, reactions of identity); the shipped
implementation is ``InMemoryChatAdapter``, a deterministic stub with
the same payload shapes. A real Slack adapter would implement the
same interface against the Slack API without the harness core
changing at all.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field


@dataclass
class Message:
    id: str
    channel_id: str
    author: str                  # user id or "agent:<name>"
    text: str
    tick: int
    thread_id: str | None = None
    mentions: tuple[str, ...] = ()


@dataclass
class Thread:
    id: str
    channel_id: str
    parent_message_id: str
    owner: str                   # "human" or "agent:<name>" for breakouts
    title: str = ""


class ChatAdapter:
    """Interface a real chat platform adapter must implement."""

    def post(self, channel_id: str, author: str, text: str,
             thread_id: str | None = None) -> Message:
        raise NotImplementedError

    def open_thread(self, channel_id: str, parent_message_id: str,
                    owner: str, title: str = "") -> Thread:
        raise NotImplementedError


@dataclass
class InMemoryChatAdapter(ChatAdapter):
    """Deterministic in-memory implementation of the seam. Message
    ids and thread ids are sequential, so transcripts reproduce."""

    _msg_ids: itertools.count = field(default_factory=lambda: itertools.count(1))
    _thread_ids: itertools.count = field(default_factory=lambda: itertools.count(1))
    messages: list[Message] = field(default_factory=list)
    threads: list[Thread] = field(default_factory=list)

    def post(self, channel_id: str, author: str, text: str,
             thread_id: str | None = None, tick: int = 0,
             mentions: tuple[str, ...] = ()) -> Message:
        msg = Message(id=f"m{next(self._msg_ids)}", channel_id=channel_id,
                      author=author, text=text, tick=tick,
                      thread_id=thread_id, mentions=mentions)
        self.messages.append(msg)
        return msg

    def open_thread(self, channel_id: str, parent_message_id: str,
                    owner: str, title: str = "") -> Thread:
        thread = Thread(id=f"t{next(self._thread_ids)}",
                        channel_id=channel_id,
                        parent_message_id=parent_message_id,
                        owner=owner, title=title)
        self.threads.append(thread)
        return thread


@dataclass
class Channel:
    id: str
    name: str
    members: tuple[str, ...]
    kind: str = "channel"        # channel | dm


@dataclass
class Workspace:
    """The channel layer's state and its two distinctive behaviours:
    mention routing (a message can invoke an agent) and breakout
    (agent work moves to an agent-owned thread)."""

    adapter: InMemoryChatAdapter = field(default_factory=InMemoryChatAdapter)
    _channels: dict = field(default_factory=dict)

    def add_channel(self, channel: Channel) -> Channel:
        self._channels[channel.id] = channel
        return channel

    def channel(self, channel_id: str) -> Channel:
        if channel_id not in self._channels:
            raise KeyError(f"unknown channel: {channel_id}")
        return self._channels[channel_id]

    def channels(self) -> list[Channel]:
        return list(self._channels.values())

    def post(self, channel_id: str, author: str, text: str, tick: int = 0,
             thread_id: str | None = None) -> Message:
        mentions = tuple(word[1:] for word in text.split()
                         if word.startswith("@") and len(word) > 1)
        return self.adapter.post(channel_id, author, text,
                                 thread_id=thread_id, tick=tick,
                                 mentions=mentions)

    def breakout(self, message: Message, agent_name: str,
                 title: str = "") -> Thread:
        """Move agent work off the human thread: open an agent-owned
        thread on the message and note the move in the channel, so the
        human conversation stays readable."""
        thread = self.adapter.open_thread(
            message.channel_id, message.id, owner=f"agent:{agent_name}",
            title=title or f"{agent_name} working")
        self.adapter.post(
            message.channel_id, f"agent:{agent_name}",
            f"(moved my work on this into a side thread: {thread.id})",
            tick=message.tick)
        return thread

    def transcript(self, channel_id: str,
                   thread_id: str | None = None) -> list[Message]:
        return [m for m in self.adapter.messages
                if m.channel_id == channel_id and m.thread_id == thread_id]
