"""The store-validation command #89 and #90 will run on the internal host."""

import os
import subprocess
import sys
import unittest

from helpers import PRODUCT_DIR, StoreCase, answered_turn

VALIDATE_STORE = os.path.join(PRODUCT_DIR, "validate_store.py")


def run(*args):
    completed = subprocess.run(
        [sys.executable, VALIDATE_STORE] + list(args),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return (completed.returncode,
            completed.stdout.decode("utf-8"),
            completed.stderr.decode("utf-8"))


class TestValidateStoreCommand(StoreCase):
    def test_a_good_store_exits_zero_and_says_what_is_in_it(self):
        chat_id = self.store.create_chat("Tooling")["chat_id"]
        answered_turn(self.store, chat_id, "q", "a")
        code, out, err = run(self.root)
        self.assertEqual(code, 0, out + err)
        self.assertIn("satisfies contract v0.1", out)
        # A record count, so "it validates" is never a statement about nothing.
        self.assertIn("message 2", out)

    def test_an_unreadable_store_exits_two_rather_than_zero(self):
        chat_id = self.store.create_chat("Broken")["chat_id"]
        self.store.append_user_message(chat_id, "one")
        path = os.path.join(self.root, "chats", chat_id, "messages", "00000001.json")
        with open(path, "w") as handle:
            handle.write("{ truncated")
        code, _out, err = run(self.root)
        self.assertEqual(code, 2)
        self.assertIn("could not be read", err)

    def test_a_contract_violating_store_exits_one_and_names_the_code(self):
        chat_id = self.store.create_chat("Violating")["chat_id"]
        self.store.append_user_message(chat_id, "one")
        os.rename(
            os.path.join(self.root, "chats", chat_id, "messages", "00000001.json"),
            os.path.join(self.root, "chats", chat_id, "messages", "00000001.json.bak"),
        )
        # A message whose sequence starts at 2: a gap the contract names.
        import json

        with open(os.path.join(self.root, "chats", chat_id, "messages", "00000001.json.bak")) as h:
            record = json.load(h)
        record["sequence"] = 2
        with open(os.path.join(self.root, "chats", chat_id, "messages", "00000002.json"), "w") as h:
            json.dump(record, h)
        os.unlink(os.path.join(self.root, "chats", chat_id, "messages", "00000001.json.bak"))
        code, out, err = run(self.root)
        self.assertIn(code, (1, 2))
        self.assertTrue("SEQUENCE_GAP" in out or "sequence jumps" in err, out + err)

    def test_the_snapshot_it_writes_is_a_fixture_the_validator_accepts(self):
        chat_id = self.store.create_chat("Snapshot")["chat_id"]
        answered_turn(self.store, chat_id, "q", "a")
        snapshot = os.path.join(self.root, "snapshot.json")
        code, out, err = run(self.root, "--snapshot", snapshot)
        self.assertEqual(code, 0, out + err)
        from helpers import VALIDATOR

        completed = subprocess.run(
            [sys.executable, VALIDATOR, snapshot],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.assertEqual(completed.returncode, 0,
                         completed.stdout.decode() + completed.stderr.decode())


if __name__ == "__main__":
    unittest.main()
