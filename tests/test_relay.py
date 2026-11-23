#!/usr/bin/env python3
"""Unit tests for the relay's shape translation. No network, no deps."""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import relay  # noqa: E402


THINKING_BLOCKS = [
    {"type": "thinking", "thinking": [{"type": "text", "text": "два плюс два"}], "closed": True},
    {"type": "text", "text": "4"},
]


class BlocksTest(unittest.TestCase):
    def test_split_blocks(self):
        text, reasoning = relay._blocks_to_parts(THINKING_BLOCKS)
        self.assertEqual(text, "4")
        self.assertEqual(reasoning, "два плюс два")

    def test_string_content_untouched(self):
        self.assertEqual(relay._blocks_to_parts("ok"), ("ok", ""))
        self.assertEqual(relay._blocks_to_parts(None), ("", ""))

    def test_multiple_text_blocks_join(self):
        blocks = [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
        self.assertEqual(relay._blocks_to_parts(blocks), ("ab", ""))


class HistoryTest(unittest.TestCase):
    def test_reasoning_content_stripped(self):
        messages = [
            {"role": "user", "content": "2+2?"},
            {"role": "assistant", "content": "4", "reasoning_content": "два плюс два"},
        ]
        cleaned, touched = relay.sanitize_history(messages)
        self.assertNotIn("reasoning_content", cleaned[1])
        self.assertEqual(cleaned[1]["content"], "4")
        self.assertEqual(touched, 1)

    def test_block_content_collapsed(self):
        messages = [{"role": "assistant", "content": THINKING_BLOCKS}]
        cleaned, touched = relay.sanitize_history(messages)
        self.assertEqual(cleaned[0]["content"], "4")
        self.assertEqual(touched, 1)

    def test_clean_history_untouched(self):
        messages = [{"role": "user", "content": "hi"}]
        cleaned, touched = relay.sanitize_history(messages)
        self.assertEqual(cleaned, messages)
        self.assertEqual(touched, 0)

    def test_input_list_not_mutated(self):
        messages = [{"role": "assistant", "content": "4", "reasoning_content": "x"}]
        relay.sanitize_history(messages)
        self.assertIn("reasoning_content", messages[0])


class ResponseTest(unittest.TestCase):
    def test_nonstream_message_normalized(self):
        payload = {"choices": [{"message": {"role": "assistant", "content": THINKING_BLOCKS}}]}
        out = relay.normalize_response(payload)
        message = out["choices"][0]["message"]
        self.assertEqual(message["content"], "4")
        self.assertEqual(message["reasoning_content"], "два плюс два")

    def test_plain_message_untouched(self):
        payload = {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        out = relay.normalize_response(payload)
        self.assertNotIn("reasoning_content", out["choices"][0]["message"])

    def test_stream_delta_normalized(self):
        chunk = {"choices": [{"delta": {"content": THINKING_BLOCKS}}]}
        out = relay.normalize_chunk(chunk)
        delta = out["choices"][0]["delta"]
        self.assertEqual(delta["content"], "4")
        self.assertEqual(delta["reasoning_content"], "два плюс два")

    def test_stream_delta_accumulates_reasoning(self):
        chunk = {"choices": [{"delta": {"content": THINKING_BLOCKS, "reasoning_content": "!"}}]}
        out = relay.normalize_chunk(chunk)
        self.assertEqual(out["choices"][0]["delta"]["reasoning_content"], "!два плюс два")

    def test_tool_call_chunks_untouched(self):
        chunk = {"choices": [{"delta": {"tool_calls": [{"index": 0}]}}]}
        self.assertEqual(relay.normalize_chunk(chunk), chunk)


class RequestShapeTest(unittest.TestCase):
    def test_metadata_signed(self):
        payload = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
        out, changes = relay.shape_request(payload)
        self.assertEqual(out["metadata"]["call_source"], "vibe_code")
        self.assertEqual(out["metadata"]["call_type"], "main_call")
        self.assertEqual(changes.get("metadata"), "vibe_code")

    def test_callers_own_metadata_fields_kept(self):
        payload = {"model": "m", "messages": [], "metadata": {"call_source": "vibe_code", "trace_id": "abc"}}
        out, _ = relay.shape_request(payload)
        self.assertEqual(out["metadata"]["trace_id"], "abc")
        self.assertEqual(out["metadata"]["call_source"], "vibe_code")

    def test_history_cleaned_in_one_pass(self):
        payload = {"model": "m", "messages": [
            {"role": "user", "content": "2+2?"},
            {"role": "assistant", "content": THINKING_BLOCKS, "reasoning_content": "x"},
        ]}
        out, changes = relay.shape_request(payload)
        self.assertEqual(out["messages"][1]["content"], "4")
        self.assertNotIn("reasoning_content", out["messages"][1])
        self.assertGreaterEqual(changes["history_fields_stripped"], 2)


class SseParseTest(unittest.TestCase):
    def test_data_line(self):
        chunk = relay.parse_sse_line(b'data: {"choices": []}\n')
        self.assertEqual(chunk, {"choices": []})

    def test_done_and_comment_lines(self):
        self.assertIsNone(relay.parse_sse_line(b"data: [DONE]\n"))
        self.assertIsNone(relay.parse_sse_line(b": keep-alive\n"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
