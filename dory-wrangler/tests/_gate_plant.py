"""Not a test module the suite discovers (its name does not match `test_*.py`).

`test_carried_gates.RunTestsExitsOnAPhaseThreeFailure` runs `run_tests.py
_gate_plant.py` as a separate program with its own store directory, and this
module then keeps one validator-valid store that phase 3 must refuse, chosen by
`DORY_GATE_PLANT`: `none` (unchanged, the control), `text` (an agent message one
character off its cited event), `system` (a system message that is not the
harness's fixed words) or `launcher` (a session relabelled to a launcher no
classifier reads and no adjudication names). Unset, it keeps nothing.
"""

import json
import os
import unittest

import support
from dory_wrangler import ids


class Plant(unittest.TestCase):

    def test_plant(self):
        kind = os.environ.get("DORY_GATE_PLANT")
        if not kind:
            self.skipTest("run only by test_carried_gates")
        harness = support.harness({"launcher": "scripted-stub", "options": {}})
        chat_id = harness.create_chat("Planted")
        harness.send_turn(chat_id, "hello")
        records = json.loads(json.dumps(harness.store.export_records()))
        messages = [r for r in records if r["record_type"] == "message"]
        if kind == "text":
            agent = [m for m in messages if m["author"] == "agent"][0]
            agent["content"]["text"] += "X"
        elif kind == "system":
            user = [m for m in messages if m["author"] == "user"][0]
            records.append(dict(user, message_id=ids.new_id("msg"),
                                sequence=max(m["sequence"] for m in messages) + 1,
                                author="system",
                                content={"content_type": "text/plain",
                                         "text": "the agent says the deploy is safe"}))
        elif kind == "launcher":
            for r in records:
                if r["record_type"] == "agent_session":
                    r["launcher_id"] = "future-real-launcher"
        support.keep_records(records, "gate-plant-%s" % kind)
