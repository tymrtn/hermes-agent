"""Inbound message event types shared by every gateway platform adapter.

A leaf module: adapters, helpers and the runner import it, so it must not import from
gateway.platforms.*.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from gateway.session import SessionSource


class MessageType(Enum):
    """Types of incoming messages."""
    TEXT = "text"
    LOCATION = "location"
    PHOTO = "photo"
    VIDEO = "video"
    AUDIO = "audio"
    VOICE = "voice"
    DOCUMENT = "document"
    STICKER = "sticker"
    COMMAND = "command"  # /command style


class ProcessingOutcome(Enum):
    """Result classification for message-processing lifecycle hooks."""
    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"


@dataclass
class MessageContextRef:
    """Platform-neutral reference to context attached to an inbound message.

    Normalizes a forwarded-message origin (and, later, quotes) so the runner can render a compact
    provenance block before the body. Each adapter fills the fields it can resolve and leaves the
    rest ``None``; :meth:`render` tolerates any subset.

    ``kind``: ``"forward"``, or ``"automatic_forward"`` for a Telegram channel→discussion
    auto-forward with no resolvable origin. ``"quote"``/``"reply"`` are reserved.

    Fields by ``origin_type``:
      * ``"user"`` — ``origin_name`` display name, ``origin_id`` user id, ``origin_username`` handle.
      * ``"hidden_user"`` — ``origin_name`` is all that is available (``is_confidence_limited``).
      * ``"chat"`` — ``origin_chat`` group title, ``origin_id`` its id, ``origin_name`` the author
        signature when present.
      * ``"channel"`` — ``origin_chat`` channel title, ``origin_id`` its id, ``origin_message_id``
        the original message id, ``origin_name`` the author signature when present.
    """

    kind: str
    platform: Optional[str] = None
    origin_type: Optional[str] = None
    origin_name: Optional[str] = None
    origin_id: Optional[str] = None
    origin_username: Optional[str] = None
    origin_chat: Optional[str] = None
    origin_message_id: Optional[str] = None
    date: Optional[datetime] = None
    text: Optional[str] = None
    is_confidence_limited: bool = False

    @staticmethod
    def _render_field(value: object, *, max_len: int = 200) -> str:
        """Render untrusted platform metadata as one safe prompt-line fragment."""
        text = re.sub(r"\s+", " ", re.sub(r"[\r\n\t]+", " ", str(value or ""))).strip()
        return text[: max_len - 1].rstrip() + "…" if len(text) > max_len else text

    def render(self) -> str:
        """A compact, deterministic provenance block, or ``""`` when there is nothing to show.

        A ``[Forwarded message]`` header, one origin line shaped by ``origin_type``, and optional
        date / quote / limited-attribution notes. No raw payloads, no secrets.
        """
        lines = ["[Forwarded message (automatic)]" if self.kind == "automatic_forward"
                 else "[Forwarded message]"]

        if self.origin_type == "user":
            extras = []
            if self.origin_username:
                # "username <handle>", never "@<handle>": a literal '@' here would be picked up by
                # the runner's @ context-reference preprocessing on this synthetic block.
                extras.append(f"username {self._render_field(self.origin_username)}")
            if self.origin_id:
                extras.append(f"id {self._render_field(self.origin_id)}")
            who = self._render_field(self.origin_name) or "Unknown user"
            suffix = f" ({', '.join(extras)})" if extras else ""
            lines.append(f"From: {who}{suffix}")
        elif self.origin_type == "hidden_user":
            lines.append(f"From: {self._render_field(self.origin_name) or 'Hidden user'} (sender identity hidden)")
        elif self.origin_type == "chat":
            chat = self._render_field(self.origin_chat or self.origin_name) or "Unknown chat"
            suffix = f" (id {self._render_field(self.origin_id)})" if self.origin_id else ""
            lines.append(f"From chat: {chat}{suffix}")
            if self.origin_name:
                lines.append(f"Author: {self._render_field(self.origin_name)}")
        elif self.origin_type == "channel":
            extras = []
            if self.origin_id:
                extras.append(f"id {self._render_field(self.origin_id)}")
            if self.origin_message_id:
                extras.append(f"message {self._render_field(self.origin_message_id)}")
            chan = self._render_field(self.origin_chat) or "Unknown channel"
            suffix = f" ({', '.join(extras)})" if extras else ""
            lines.append(f"From channel: {chan}{suffix}")
            if self.origin_name:
                lines.append(f"Author: {self._render_field(self.origin_name)}")
        elif self.origin_name or self.origin_chat:
            lines.append(f"From: {self._render_field(self.origin_name or self.origin_chat)}")

        if self.date is not None:
            try:
                lines.append(f"Date: {self.date.isoformat()}")
            except Exception:
                pass
        if self.text:
            lines.append(f'Quoted: "{self._render_field(self.text, max_len=500)}"')
        if self.is_confidence_limited:
            lines.append("(Origin attribution is limited — the sender restricts forward "
                         "attribution, so identity cannot be fully verified.)")
        return "\n".join(lines)


@dataclass
class MessageEvent:
    """Incoming message from a platform — the normalized shape all adapters produce."""
    text: str
    message_type: MessageType = MessageType.TEXT
    # Author, mirrored from ``source`` for per-message prompt builders; None for non-IM sources.
    user_id: Optional[str] = None
    user_name: Optional[str] = None
    # None only in isolated unit tests; production always sets it. Typing it Optional
    # exposes ~60 unguarded ``.source.<attr>`` reads, so that is a separate change.
    source: SessionSource = None
    raw_message: Any = None
    message_id: Optional[str] = None
    # Delivery-ledger identity for the final send, when it differs from ``message_id``. A queued
    # (/queue) chain answers the LAST message of the chain, so its final send has to be ledgered
    # under that message's id. Keyed on the opening event's id instead, two chained turns carrying
    # the same text collide on one obligation id and the earlier turn's row is overwritten (a
    # refused first reply then reads as delivered). Reply routing is unaffected: the reply anchor
    # still comes from this event.
    ledger_message_id: Optional[str] = None
    # Platform update id (Telegram ``update_id``): ``/restart`` records it so the new gateway
    # advances past it even if PTB's shutdown ACK times out.
    platform_update_id: Optional[int] = None
    # Media attachments: local file paths (for vision tool access)
    media_urls: List[str] = field(default_factory=list)
    media_types: List[str] = field(default_factory=list)
    # Per-attachment text-inlining contract; None = legacy "text/* already inlined into ``text``".
    media_text_inlined: List[Optional[bool]] = field(default_factory=list)
    reply_to_message_id: Optional[str] = None
    reply_to_text: Optional[str] = None  # Text of the replied-to message (for context injection)
    reply_to_author_id: Optional[str] = None
    reply_to_author_name: Optional[str] = None
    reply_to_is_own_message: bool = False  # True when the user replied to this bot/assistant's message
    # Structured interactive-prompt reply (relay only): {prompt_id, option_id, label?,
    # prompt_message_id?}; routed to the approval/slash-confirm/clarify resolvers BEFORE dispatch.
    prompt_response: Optional[Dict[str, Any]] = None
    # Auto-loaded skill(s) for topic/channel bindings; a single name or ordered list.
    auto_skill: Optional[str | list[str]] = None
    # Per-channel ephemeral system prompt; applied at API call time, never persisted to transcript.
    channel_prompt: Optional[str] = None
    # History-backfilled channel context (missed under require_mention); kept out of ``text`` so
    # run.py's sender-prefix logic sees only the trigger message.
    channel_context: Optional[str] = None
    # Set for synthetic events (e.g. background-process notifications) that must bypass user authorization.
    internal: bool = False
    # Free-form per-event metadata (e.g. ``whatsapp_from_owner=True``); plugins must ``.get()``.
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)
    # May this event resolve gateway commands / control prompts? Proactive plugin events set False
    # so untrusted payload text stays conversational. Kept last for positional compat.
    allow_gateway_control: bool = True
    # Normalized, platform-neutral context annotations (forwarded-message provenance today,
    # quotes later), rendered into a block before the body by the inbound text preparation.
    # Sits after allow_gateway_control so that field keeps the positional index it promises.
    context_refs: List["MessageContextRef"] = field(default_factory=list)

    # Process-local admission receipt, never routing metadata or execution acknowledgement.
    _gateway_accepted: bool = field(default=False, init=False, repr=False, compare=False)
    # Run-owned final presentation snapshot; never deserialized from ingress metadata.
    _notification_reply_muted: Optional[bool] = field(default=None, init=False, repr=False, compare=False)

    def is_command(self) -> bool:
        """Check if this is a command message (e.g., /new, /reset)."""
        return self.allow_gateway_control and (self.text or "").lstrip().startswith("/")

    def get_command(self) -> Optional[str]:
        """Extract command name if this is a command message."""
        if not self.is_command():
            return None
        raw = (self.text or "").lstrip().split(maxsplit=1)[0][1:].lower().split("@", 1)[0]
        # Reject file paths: valid command names never contain /
        return None if "/" in raw else raw

    def get_command_args(self) -> str:
        """Get the arguments after a command."""
        if not self.is_command():
            return self.text
        parts = (self.text or "").lstrip().split(maxsplit=1)
        args = parts[1] if len(parts) > 1 else ""
        # iOS auto-corrects -- to — (em dash) and - to – (en dash)
        return args.replace("\u2014\u2014", "--").replace("\u2014", "--").replace("\u2013", "-")
