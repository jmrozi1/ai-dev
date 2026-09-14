"""The chat shell's use cases: create, list, open, send, abandon.

Thin by design. Every durable decision belongs to `store`, and every decision
about an agent belongs to `session_manager`; this layer adds only the things the
shell actually does. In particular it holds no cache: `open_chat` reads the chat
and its messages from disk on every call, so the history the shell renders is
the durable history and cannot diverge from it.

A sent turn goes through the chat loop (`SessionManager.send_turn`), which is
the only path that records a user turn in the served application: the turn is
recorded when it is offered to an agent and refused before it is recorded when
the chat is already served (decision 0003). Abandon is the one lifecycle action
the shell exposes, because a restart that finds an agent nobody can reach must
leave the user a way out.
"""

from .wiring import open_harness

DEFAULT_TITLE = "New chat"

# A new chat is named from the user's first turn while it still carries the
# placeholder title. This is a display convenience over text the user typed;
# nothing is parsed out of an identifier to produce it.
TITLE_FROM_FIRST_TURN_CHARS = 60

# The launcher the shell uses when none is configured: the external development
# launcher in the shape of the one internal path that is proven, a one-shot
# script. Configuration, not code, chooses another (`serve.py --launcher`).
DEFAULT_LAUNCHER = {"launcher": "dev-local", "options": {"profile": "one_shot"}}


class ChatService(object):
    def __init__(self, sessions):
        self.sessions = sessions
        self.store = sessions.store

    @classmethod
    def open(cls, root, launcher_config=None, launcher=None):
        return cls(open_harness(launcher_config or DEFAULT_LAUNCHER, root,
                                launcher=launcher))

    # -- the new-chat flow ---------------------------------------------

    def create_chat(self, title=None):
        return self.store.create_chat(title or DEFAULT_TITLE)

    # -- the conversation list -----------------------------------------

    def list_chats(self):
        """Every open chat, most recently updated first, with its last turn.

        The preview is the text of the highest-sequence message, read from the
        durable record. There is no session, lifecycle, or diagnostic field
        here: the conversation list is chat metadata only.
        """
        out = []
        for chat in self.store.list_chats():
            messages = self.store.read_messages(chat["chat_id"])
            out.append(
                {
                    "chat_id": chat["chat_id"],
                    "title": chat["title"],
                    "created_at": chat["created_at"],
                    "updated_at": chat["updated_at"],
                    "message_count": len(messages),
                    "preview": messages[-1]["content"]["text"] if messages else "",
                }
            )
        return out

    # -- the active conversation ---------------------------------------

    def open_chat(self, chat_id):
        """A chat and its complete ordered user-visible history, from disk.

        Contract D1: this read requires no live agent, no live launcher, and no
        provider-side conversation. It touches the chats tree only, so no
        diagnostic record can reach the transcript by accident (contract P4).
        """
        chat = self.store.read_chat(chat_id)
        messages = self.store.read_messages(chat_id)
        return {
            "chat_id": chat["chat_id"],
            "title": chat["title"],
            "created_at": chat["created_at"],
            "updated_at": chat["updated_at"],
            "state": chat["state"],
            "messages": [
                {
                    "message_id": m["message_id"],
                    "sequence": m["sequence"],
                    "author": m["author"],
                    "created_at": m["created_at"],
                    "text": m["content"]["text"],
                }
                for m in messages
            ],
        }

    # -- send ----------------------------------------------------------

    def send_user_message(self, chat_id, text):
        """Send a user turn through the chat loop and return the reloaded chat.

        The turn is offered to an agent and the chat is then re-read from disk
        rather than patched in memory, so what the shell shows after a send is
        the same history it would show after a restart. A refused turn raises
        before anything is recorded; a turn that was recorded names the chat even
        when what followed it failed.
        """
        chat = self.store.read_chat(chat_id)
        try:
            self.sessions.send_turn(chat_id, text)
        finally:
            self._name_from_first_turn(chat)
        return self.open_chat(chat_id)

    def _name_from_first_turn(self, chat):
        if chat["title"] != DEFAULT_TITLE:
            return
        messages = self.store.read_messages(chat["chat_id"])
        if not messages or messages[0]["author"] != "user":
            return
        text = messages[0]["content"]["text"]
        title = " ".join(text.split())[:TITLE_FROM_FIRST_TURN_CHARS].strip()
        if title:
            self.store.set_title(chat["chat_id"], title, only_if_titled=DEFAULT_TITLE)

    # -- the one lifecycle action --------------------------------------

    def abandon(self, chat_id):
        """The user abandons the agent holding this chat, then sees the chat."""
        self.store.read_chat(chat_id)
        self.sessions.abandon(chat_id)
        return self.open_chat(chat_id)
