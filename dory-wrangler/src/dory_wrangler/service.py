"""The chat shell's use cases: create, list, open, send.

Thin by design. Every durable decision belongs to `store`, and this layer adds
only the four things the shell actually does. In particular it holds no cache:
`open_chat` reads the chat and its messages from disk on every call, so the
history the shell renders is the durable history and cannot diverge from it.

This layer contains no launch mechanics. #86 starts no agent, and the contract's
section 6 boundary is #87's to implement. `turn_listener` exists so that a later
ticket can be told a user turn is durable without editing the send path; it is a
notification with no autonomy of its own -- it is called only because a user
sent a turn, never on a schedule, and never because time passed.
"""

from .store import ChatStore

DEFAULT_TITLE = "New chat"

# A new chat is named from the user's first turn while it still carries the
# placeholder title. This is a display convenience over text the user typed;
# nothing is parsed out of an identifier to produce it.
TITLE_FROM_FIRST_TURN_CHARS = 60


class ChatService(object):
    def __init__(self, store, turn_listener=None):
        self.store = store
        self.turn_listener = turn_listener

    @classmethod
    def open(cls, root, turn_listener=None):
        return cls(ChatStore(root), turn_listener=turn_listener)

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
        """Append a user turn and return the reloaded chat.

        The message is durable before anything else happens. The chat is then
        re-read from disk rather than patched in memory, so what the shell shows
        after a send is the same history it would show after a restart.
        """
        chat = self.store.read_chat(chat_id)
        message = self.store.append_user_message(chat_id, text)
        if chat["title"] == DEFAULT_TITLE and message["sequence"] == 1:
            title = " ".join(text.split())[:TITLE_FROM_FIRST_TURN_CHARS].strip()
            if title:
                self.store.set_title(chat_id, title, only_if_titled=DEFAULT_TITLE)
        if self.turn_listener is not None:
            self.turn_listener(chat_id, message["message_id"])
        return self.open_chat(chat_id)
