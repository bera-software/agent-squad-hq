#!/usr/bin/env python3
"""
Tests for squadhq.agent_runner — covers the empty-reply incident (#46) and
the robustness changes made in response to it: an empty API reply, a raised
API exception, a non-empty-but-unparseable reply, and the "no inputs" skip
path. No network access: the Anthropic HTTP call is mocked at
urllib.request.urlopen, and every `gh` call is mocked at agent_runner.sh /
agent_runner._run.

    python -m pytest tests/test_agent_runner.py
"""
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO_ROOT, "src"))

from squadhq import agent_runner as ar  # noqa: E402
from squadhq import github  # noqa: E402

HQ = "test-org/test-hq"


def _no_network_runner(cmd, **kw):
    raise AssertionError(f"unexpected real gh call in tests: {cmd}")


def setUpModule():
    # The module has no import-time configuration any more; pin what
    # configure() would have resolved for a `brief` run against a test hub.
    global _saved
    _saved = {k: getattr(ar, k) for k in ("HQ", "MODE", "ALLOWED_REPOS", "PROJECTS")}
    ar.HQ = HQ
    ar.MODE = "brief"
    ar.ALLOWED_REPOS = {HQ}
    ar.PROJECTS = []
    github.set_client(github.GitHubClient(runner=_no_network_runner, retries=0))


def tearDownModule():
    for k, v in _saved.items():
        setattr(ar, k, v)
    github.set_client(None)


def _fake_http_response(body_dict):
    """Stand-in for what urllib.request.urlopen(...) returns — json.load()
    only needs .read(), so a BytesIO is enough."""
    return io.BytesIO(json.dumps(body_dict).encode())


def _fresh_usage():
    return {"input_tokens": 0, "output_tokens": 0, "est_cost_usd": 0.0}


class CallClaudeEmptyReplyTests(unittest.TestCase):
    """Incident #46: a valid, billable 200 response with zero text blocks."""

    def test_empty_content_list_returns_empty_text_not_exception(self):
        resp_body = {
            "content": [],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 500, "output_tokens": 0},
        }
        with mock.patch("squadhq.agent_runner.urllib.request.urlopen",
                         return_value=_fake_http_response(resp_body)):
            usage = _fresh_usage()
            reply = ar.call_claude("system prompt", "user content", usage)
        self.assertIsInstance(reply, ar.LLMReply)
        self.assertEqual(reply.text, "")
        self.assertEqual(reply.stop_reason, "end_turn")
        self.assertEqual(reply.input_tokens, 500)
        self.assertEqual(reply.output_tokens, 0)
        # usage dict (budget tracking) must still be updated even though the
        # reply carried no text — tokens were billed either way.
        self.assertEqual(usage["input_tokens"], 500)

    def test_missing_content_key_entirely_is_also_empty_text_not_keyerror(self):
        resp_body = {"stop_reason": "end_turn", "usage": {"input_tokens": 10, "output_tokens": 0}}
        with mock.patch("squadhq.agent_runner.urllib.request.urlopen",
                         return_value=_fake_http_response(resp_body)):
            reply = ar.call_claude("sys", "user", _fresh_usage())
        self.assertEqual(reply.text, "")

    def test_non_text_blocks_only_are_ignored_not_concatenated(self):
        resp_body = {
            "content": [{"type": "tool_use", "name": "x", "input": {}}],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 20, "output_tokens": 5},
        }
        with mock.patch("squadhq.agent_runner.urllib.request.urlopen",
                         return_value=_fake_http_response(resp_body)):
            reply = ar.call_claude("sys", "user", _fresh_usage())
        self.assertEqual(reply.text, "")
        self.assertEqual(reply.stop_reason, "tool_use")

    def test_normal_reply_joins_text_blocks(self):
        resp_body = {
            "content": [{"type": "text", "text": '[{"type":"create_issue"}]'}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 100, "output_tokens": 20},
        }
        with mock.patch("squadhq.agent_runner.urllib.request.urlopen",
                         return_value=_fake_http_response(resp_body)):
            reply = ar.call_claude("sys", "user", _fresh_usage())
        self.assertEqual(reply.text, '[{"type":"create_issue"}]')


class AdaptiveThinkingGuardTests(unittest.TestCase):
    """Confirmed root cause of #46: claude-sonnet-5's adaptive thinking is on
    by default and eats max_tokens before any visible text. call_claude
    must send thinking:{type:disabled} for that model specifically, and
    leave other models' requests untouched."""

    def _capture_request_body(self, resp_body):
        captured = {}

        def fake_urlopen(req):
            captured["body"] = json.loads(req.data.decode())
            return _fake_http_response(resp_body)

        return captured, fake_urlopen

    def test_sonnet_5_gets_thinking_disabled(self):
        resp_body = {"content": [{"type": "text", "text": "[]"}], "stop_reason": "end_turn",
                      "usage": {"input_tokens": 1, "output_tokens": 1}}
        captured, fake_urlopen = self._capture_request_body(resp_body)
        with mock.patch("squadhq.agent_runner.MODEL", "claude-sonnet-5"), \
             mock.patch("squadhq.agent_runner.urllib.request.urlopen", side_effect=fake_urlopen):
            ar.call_claude("sys", "user", _fresh_usage())
        self.assertEqual(captured["body"].get("thinking"), {"type": "disabled"})

    def _thinking_sent(self, model):
        resp_body = {"content": [{"type": "text", "text": "[]"}], "stop_reason": "end_turn",
                      "usage": {"input_tokens": 1, "output_tokens": 1}}
        captured, fake_urlopen = self._capture_request_body(resp_body)
        with mock.patch("squadhq.agent_runner.MODEL", model), \
             mock.patch("squadhq.agent_runner.urllib.request.urlopen", side_effect=fake_urlopen):
            ar.call_claude("sys", "user", _fresh_usage())
        return captured["body"]

    def test_haiku_5_5_gets_thinking_disabled(self):
        self.assertEqual(self._thinking_sent("claude-haiku-5-5").get("thinking"), {"type": "disabled"})

    def test_sonnet_5_5_gets_between_tools_not_disabled(self):
        # "disabled" is a 400 on Sonnet 5.5; between_tools is its lowest setting.
        self.assertEqual(self._thinking_sent("claude-sonnet-5-5").get("thinking"), {"type": "between_tools"})

    def test_max_tokens_raised_to_12000(self):
        self.assertEqual(self._thinking_sent("claude-haiku-5-5")["max_tokens"], 12000)

    def test_no_effort_sent(self):
        # Haiku 5.5 only accepts thinking:disabled at effort high or below;
        # omitting effort keeps the model default (medium / high) in range.
        self.assertNotIn("output_config", self._thinking_sent("claude-haiku-5-5"))

    def test_other_models_are_not_sent_a_thinking_field(self):
        self.assertNotIn("thinking", self._thinking_sent("claude-haiku-4-5-20251001"))


class RateTests(unittest.TestCase):
    def test_haiku_5_5_short_and_long_prompt_rates(self):
        self.assertEqual(ar.rate_for("claude-haiku-5-5", 100_000), (0.10, 0.50))
        self.assertEqual(ar.rate_for("claude-haiku-5-5", 100_001), (0.50, 2.50))

    def test_every_configured_model_is_priced(self):
        cfg = json.load(open(os.path.join(os.path.dirname(__file__), "..", "config", "models.json")))
        for tier in cfg["hub_loops"].values():
            for m in (tier["primary"], tier["fallback"]):
                self.assertIn(m, ar.RATES, m)


class CallClaudeApiErrorTests(unittest.TestCase):
    """API failures must never become an empty string — always a typed
    exception, and only transient statuses retry."""

    def test_non_transient_error_raises_immediately_no_retry(self):
        err = ar.urllib.error.HTTPError(
            url="https://api.anthropic.com/v1/messages", code=401,
            msg="Unauthorized", hdrs=None,
            fp=io.BytesIO(b'{"error":{"message":"invalid x-api-key"}}'))
        with mock.patch("squadhq.agent_runner.urllib.request.urlopen", side_effect=err) as urlopen_mock, \
             mock.patch("squadhq.agent_runner.time.sleep") as sleep_mock:
            with self.assertRaises(ar.AnthropicAPIError) as ctx:
                ar.call_claude("sys", "user", _fresh_usage())
        self.assertIn("401", str(ctx.exception))
        self.assertEqual(urlopen_mock.call_count, 1)  # no retry on a 4xx
        sleep_mock.assert_not_called()

    def test_transient_error_retries_with_backoff_then_raises_when_exhausted(self):
        err = ar.urllib.error.HTTPError(
            url="https://api.anthropic.com/v1/messages", code=503,
            msg="Service Unavailable", hdrs=None, fp=io.BytesIO(b"{}"))
        with mock.patch("squadhq.agent_runner.urllib.request.urlopen", side_effect=err) as urlopen_mock, \
             mock.patch("squadhq.agent_runner.time.sleep") as sleep_mock:
            with self.assertRaises(ar.AnthropicAPIError):
                ar.call_claude("sys", "user", _fresh_usage())
        # _MAX_RETRIES=2 -> 3 attempts total, 2 backoff sleeps before the 2nd/3rd
        self.assertEqual(urlopen_mock.call_count, ar._MAX_RETRIES + 1)
        self.assertEqual(sleep_mock.call_count, ar._MAX_RETRIES)

    def test_network_error_is_treated_as_transient_and_retried(self):
        with mock.patch("squadhq.agent_runner.urllib.request.urlopen",
                         side_effect=ConnectionError("connection reset")) as urlopen_mock, \
             mock.patch("squadhq.agent_runner.time.sleep") as sleep_mock:
            with self.assertRaises(ar.AnthropicAPIError):
                ar.call_claude("sys", "user", _fresh_usage())
        self.assertEqual(urlopen_mock.call_count, ar._MAX_RETRIES + 1)
        self.assertGreater(sleep_mock.call_count, 0)


class ParseActionsTests(unittest.TestCase):
    def test_reply_with_no_parseable_actions(self):
        # Non-empty reply, but no JSON list anywhere in it.
        actions, parsed_ok = ar.parse_actions("Sorry, nothing to report today.")
        self.assertEqual(actions, [])
        self.assertFalse(parsed_ok)

    def test_reply_wrapped_in_json_fence_parses(self):
        text = '```json\n[{"type":"create_issue","repo":"a/b","title":"x","body":"y"}]\n```'
        actions, parsed_ok = ar.parse_actions(text)
        self.assertTrue(parsed_ok)
        self.assertEqual(len(actions), 1)

    def test_deliberate_empty_list_is_parsed_ok(self):
        actions, parsed_ok = ar.parse_actions("[]")
        self.assertTrue(parsed_ok)
        self.assertEqual(actions, [])

    def test_unescaped_control_character_in_body_still_parses(self):
        # Confirmed real recurrence (2026-08-27, brief run): a long "body"
        # field containing embedded markdown had a literal, unescaped
        # newline instead of the model correctly writing "\n" — strict JSON
        # rejects that ("Invalid control character"), even though the
        # intent is unambiguous. parse_actions must tolerate it.
        text = ('```json\n[\n  {\n    "type": "create_issue",\n    "repo": "a/b",\n'
                '    "title": "Daily Brief",\n    "body": "Run this:\n```bash\nls -la\n```\n"\n'
                '  }\n]\n```')
        actions, parsed_ok = ar.parse_actions(text)
        self.assertTrue(parsed_ok)
        self.assertEqual(len(actions), 1)
        self.assertIn("ls -la", actions[0]["body"])

    def test_genuinely_malformed_json_still_fails(self):
        # strict=False must not become "accept anything" — a real syntax
        # error (trailing comma) still fails to parse.
        actions, parsed_ok = ar.parse_actions('[{"type":"create_issue",}]')
        self.assertFalse(parsed_ok)
        self.assertEqual(actions, [])


class DiagnoseParseFailureTests(unittest.TestCase):
    def test_reports_json_decode_error_with_position(self):
        diag = ar._diagnose_parse_failure('[{"type":"create_issue",}]')
        self.assertIn("bracket-slice", diag)
        self.assertRegex(diag, r"char \d+")

    def test_reports_non_list_top_level_type(self):
        diag = ar._diagnose_parse_failure('{"type":"create_issue"}')
        self.assertIn("wasn't a list", diag)
        self.assertIn("dict", diag)

    def test_no_json_span_at_all(self):
        # _extraction_attempts always tries the raw text itself (as a
        # fallback "fenced-block-split" candidate even with no fence), so
        # there's always at least one attempt/error line, not a bail-out.
        diag = ar._diagnose_parse_failure("Sorry, nothing to report today.")
        self.assertIn("fenced-block-split", diag)
        self.assertIn("Expecting value", diag)


class LogDiagnosticsTests(unittest.TestCase):
    def test_logs_full_reply_stop_reason_usage_and_model(self):
        reply_obj = ar.LLMReply(text="", stop_reason="end_turn", input_tokens=10,
                                 output_tokens=0, model="claude-sonnet-5", raw={})
        buf = io.StringIO()
        with redirect_stdout(buf):
            ar._log_diagnostics("brief", reply_obj, parsed_ok=False, num_actions=0)
        out = buf.getvalue()
        self.assertIn("end_turn", out)
        self.assertIn("claude-sonnet-5", out)
        self.assertIn("10 in", out)
        self.assertIn("empty reply", out)

    def test_long_reply_is_not_truncated_in_job_log(self):
        long_text = "x" * 5000
        reply_obj = ar.LLMReply(text=f"prose, no json: {long_text}", stop_reason="end_turn",
                                 input_tokens=1, output_tokens=1, model="m", raw={})
        buf = io.StringIO()
        with redirect_stdout(buf):
            ar._log_diagnostics("brief", reply_obj, parsed_ok=False, num_actions=0)
        self.assertIn(long_text, buf.getvalue())


class NoInputsTests(unittest.TestCase):
    def test_true_when_everything_empty_and_warnings_present(self):
        ctx = {"warnings": ["hq_open_issues: gh: command failed"],
               "projects": [{"open_issues": [], "open_prs": [], "recent_merged": []}],
               "hq_open_issues": []}
        self.assertTrue(ar._is_no_inputs(ctx, "brief"))

    def test_false_on_a_clean_quiet_day_no_warnings(self):
        # Zero activity but NO warnings — a legitimate quiet day. The brief
        # prompt explicitly wants a "<name>: quiet." report even then, so
        # this must NOT be treated as "no inputs" (would silently skip the
        # daily habit the agent exists for).
        ctx = {"warnings": [], "projects": [{"open_issues": [], "open_prs": [], "recent_merged": []}],
               "hq_open_issues": []}
        self.assertFalse(ar._is_no_inputs(ctx, "brief"))

    def test_false_when_something_present_despite_warnings(self):
        ctx = {"warnings": ["po_verdicts(x/y): timeout"],
               "projects": [{"open_issues": [{"number": 1}], "open_prs": [], "recent_merged": []}],
               "hq_open_issues": []}
        self.assertFalse(ar._is_no_inputs(ctx, "brief"))

    def test_never_true_for_other_modes(self):
        ctx = {"warnings": ["x"], "projects": [], "hq_open_issues": []}
        self.assertFalse(ar._is_no_inputs(ctx, "cos"))


class EmptyReplyEscalationTests(unittest.TestCase):
    """The #46 incident, reproduced from its real logged diagnostics:
    model=claude-sonnet-5, stop_reason=max_tokens, output_tokens==max_tokens,
    zero text — must get the distinct 'hit max_tokens' title, not the
    generic 'replied with empty output' one."""

    def test_max_tokens_stop_reason_gets_budget_exhausted_title(self):
        reply_obj = ar.LLMReply(text="", stop_reason="max_tokens", input_tokens=22119,
                                 output_tokens=4000, model="claude-sonnet-5", raw={})
        title, body = ar._empty_reply_escalation("brief", reply_obj)
        self.assertIn("hit max_tokens", title)
        self.assertNotIn("replied with empty output", title)
        self.assertIn("adaptive", body.lower())
        self.assertIn("4000", body)

    def test_body_opens_with_mode_role_badge(self):
        # org/CLAUDE-org.md IDENTITY rule: every comment starts with a role
        # badge. This is a script-authored body (not model text), so
        # agent_runner.py must prepend it itself.
        reply_obj = ar.LLMReply(text="", stop_reason="end_turn", input_tokens=1,
                                 output_tokens=0, model="m", raw={})
        _, body = ar._empty_reply_escalation("brief", reply_obj)
        self.assertTrue(body.startswith("[Daily Brief]"))

    def test_other_stop_reason_gets_generic_empty_output_title(self):
        reply_obj = ar.LLMReply(text="", stop_reason="end_turn", input_tokens=100,
                                 output_tokens=0, model="claude-haiku-4-5-20251001", raw={})
        title, body = ar._empty_reply_escalation("brief", reply_obj)
        self.assertIn("replied with empty output", title)
        self.assertNotIn("hit max_tokens", title)


class EscalationDedupTests(unittest.TestCase):
    def test_comments_on_existing_recent_issue_instead_of_creating_duplicate(self):
        title = "ESCALATION: brief run — model replied with empty output"
        list_output = json.dumps([{"number": 46, "title": title, "author": {"login": "app/squadhq-ops"},
                                    "createdAt": "2099-01-01T00:00:00Z"}])

        def fake_sh(args):
            if args[:2] == ["issue", "list"]:
                return mock.Mock(returncode=0, stdout=list_output, stderr="")
            raise AssertionError(f"unexpected gh call: {args}")

        def fake_run(cmd):
            if cmd[:3] == ["gh", "issue", "comment"]:
                return 0, "https://github.com/test-org/test-hq/issues/46#issuecomment-1", ""
            raise AssertionError(f"unexpected _run() call: {cmd}")

        with mock.patch.object(github.client(), "run", side_effect=lambda a, **k: fake_sh(a)), \
             mock.patch("squadhq.agent_runner._run", side_effect=fake_run):
            results, ok, fail, records = ar.create_or_comment(ar.HQ, title, "body text", ["escalation"])
        self.assertEqual(ok, 1)
        self.assertEqual(fail, 0)
        self.assertEqual(records[0]["kind"], "comment")
        self.assertEqual(records[0]["number"], 46)

    def test_creates_new_issue_when_no_recent_duplicate(self):
        title = "ESCALATION: brief run — model replied with empty output"

        def fake_sh(args):
            if args[:2] == ["issue", "list"]:
                return mock.Mock(returncode=0, stdout="[]", stderr="")
            raise AssertionError(f"unexpected gh call before create: {args}")

        def fake_run(cmd):
            if cmd[0] == "gh" and cmd[1] == "issue" and cmd[2] == "create":
                return 0, "https://github.com/test-org/test-hq/issues/47", ""
            raise AssertionError(f"unexpected _run() call: {cmd}")

        with mock.patch.object(github.client(), "run", side_effect=lambda a, **k: fake_sh(a)), \
             mock.patch("squadhq.agent_runner._run", side_effect=fake_run):
            results, ok, fail, records = ar.create_or_comment(ar.HQ, title, "body", ["escalation"])
        self.assertEqual(ok, 1)
        self.assertEqual(records[0]["kind"], "create_issue")
        self.assertEqual(records[0]["number"], 47)

    def test_lookup_failure_falls_back_to_create(self):
        # find_recent_open_issue must fail open (return None, i.e. "assume no
        # duplicate") rather than block a real escalation on a search hiccup.
        def fake_sh(args):
            raise RuntimeError("gh: transient failure")

        def fake_run(cmd):
            if cmd[0] == "gh" and cmd[1] == "issue" and cmd[2] == "create":
                return 0, "https://github.com/test-org/test-hq/issues/48", ""
            raise AssertionError(f"unexpected _run() call: {cmd}")

        with mock.patch.object(github.client(), "run", side_effect=lambda a, **k: fake_sh(a)), \
             mock.patch("squadhq.agent_runner._run", side_effect=fake_run):
            results, ok, fail, records = ar.create_or_comment(ar.HQ, "some title", "body", [])
        self.assertEqual(ok, 1)
        self.assertEqual(records[0]["kind"], "create_issue")


class ExecuteDryRunTests(unittest.TestCase):
    def test_dry_run_never_shells_out(self):
        actions = [{"type": "create_issue", "repo": ar.HQ, "title": "t", "body": "b"}]
        with mock.patch("squadhq.agent_runner._run") as run_mock, mock.patch("squadhq.agent_runner.sh") as sh_mock:
            done, ok, fail, records = ar.execute(actions, dry_run=True)
        run_mock.assert_not_called()
        sh_mock.assert_not_called()
        self.assertEqual(ok, 1)
        self.assertEqual(fail, 0)
        self.assertEqual(records[0]["classification"], "dry-run")
        self.assertTrue(records[0]["success"])



class CosLabelGuardTests(unittest.TestCase):
    """CoS must never apply Product Owner outcome labels (flows.yml
    labels.*.applied_by) — it once labeled two
    never-shaped issues proposal:pending and then waited on its own label."""

    def run_exec(self, labels):
        calls = []
        def fake_run(cmd):
            calls.append(cmd)
            return 0, "", ""
        with mock.patch("squadhq.agent_runner.MODE", "cos"), \
             mock.patch("squadhq.agent_runner._run", side_effect=fake_run), \
             mock.patch("squadhq.agent_runner._current_labels", return_value=set()), \
             redirect_stdout(io.StringIO()):
            ar.execute([{"type": "add_labels", "repo": ar.HQ,
                                   "number": 5, "labels": labels}])
        return [c for c in calls if "--add-label" in c]

    def test_po_label_dropped_others_kept(self):
        calls = self.run_exec(["dept:growth", "P2", "proposal:pending"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][calls[0].index("--add-label") + 1], "dept:growth,P2")

    def test_only_po_labels_is_skipped(self):
        self.assertEqual(self.run_exec(["proposal:pending", "verdict:decline"]), [])


class CosQueueGuardTests(unittest.TestCase):
    """CoS may never remove triaged:ready — it is the sweeper's dispatch queue
    ."""

    def run_exec(self, labels):
        calls = []
        def fake_run(cmd):
            calls.append(cmd)
            return 0, "", ""
        with mock.patch("squadhq.agent_runner.MODE", "cos"), \
             mock.patch("squadhq.agent_runner._run", side_effect=fake_run), \
             mock.patch("squadhq.agent_runner._current_labels", return_value=set(labels)), \
             redirect_stdout(io.StringIO()):
            ar.execute([{"type": "remove_labels", "repo": ar.HQ, "number": 5, "labels": labels}])
        return [c for c in calls if "--remove-label" in c]

    def test_ready_kept_others_removed(self):
        calls = self.run_exec(["triaged:ready", "needs-human"])
        self.assertEqual(len(calls), 1)
        self.assertNotIn("triaged:ready", " ".join(calls[0]))
        self.assertIn("needs-human", " ".join(calls[0]))

    def test_only_ready_is_skipped(self):
        self.assertEqual(self.run_exec(["triaged:ready"]), [])


class HqRepoAliasTests(unittest.TestCase):
    """Models shorten the HQ repo ("hub", "hq") — those resolve to HQ instead
    of being skipped as not allowed."""

    def test_aliases_and_missing_map_to_hq(self):
        for a in ({"repo": "hub"}, {"repo": "HQ"}, {"repo": " agent-squad-hq "}, {"repo": "test-hq"},
                  {"repo": ""}, {"repo": None}, {}):
            self.assertEqual(ar._repo_of(a), ar.HQ, a)

    def test_full_names_and_unknowns_untouched(self):
        self.assertEqual(ar._repo_of({"repo": "o/proj"}), "o/proj")
        self.assertEqual(ar._repo_of({"repo": "evil/repo"}), "evil/repo")

    def test_execute_creates_issue_for_hub_alias(self):
        calls = []
        def fake_run(cmd):
            calls.append(cmd)
            return 0, "https://github.com/test-org/test-hq/issues/1", ""
        with mock.patch("squadhq.agent_runner.MODE", "growth"), \
             mock.patch("squadhq.agent_runner._run", side_effect=fake_run), \
             mock.patch("squadhq.agent_runner.ALLOWED_REPOS", {"o/proj", ar.HQ}), \
             redirect_stdout(io.StringIO()):
            ar.execute([{"type": "create_issue", "repo": "hub",
                         "title": "Draft: X — Y", "body": "[Growth] draft",
                         "labels": ["awaiting-approval", "dept:growth"]}])
        creates = [c for c in calls if c[:3] == ["gh", "issue", "create"]]
        self.assertEqual(len(creates), 1)
        self.assertEqual(creates[0][creates[0].index("-R") + 1], ar.HQ)


class CosNarrationGuardTests(unittest.TestCase):
    """A CoS comment with no label change/close on the same item is dropped
    when CoS already commented there within the cooldown."""

    def run_exec(self, actions, commented_recently):
        calls = []
        def fake_run(cmd):
            calls.append(cmd)
            return 0, "", ""
        with mock.patch("squadhq.agent_runner.MODE", "cos"), \
             mock.patch("squadhq.agent_runner._run", side_effect=fake_run), \
             mock.patch("squadhq.agent_runner._current_labels", return_value=set()), \
             mock.patch("squadhq.agent_runner._own_comment_within", return_value=commented_recently), \
             mock.patch("squadhq.agent_runner.ALLOWED_REPOS", {"o/proj", ar.HQ}), \
             redirect_stdout(io.StringIO()):
            ar.execute(actions)
        return [c for c in calls if c[:3] == ["gh", "issue", "comment"]]

    def test_repeat_narration_dropped(self):
        a = [{"type": "comment", "repo": "o/proj", "number": 5, "body": "[Chief of Staff] holding"}]
        self.assertEqual(self.run_exec(a, True), [])

    def test_first_narration_allowed(self):
        a = [{"type": "comment", "repo": "o/proj", "number": 5, "body": "[Chief of Staff] note"}]
        self.assertEqual(len(self.run_exec(a, False)), 1)

    def test_comment_with_action_always_allowed(self):
        a = [{"type": "add_labels", "repo": "o/proj", "number": 5, "labels": ["P1"]},
             {"type": "comment", "repo": "o/proj", "number": 5, "body": "[Chief of Staff] raised to P1 because"}]
        self.assertEqual(len(self.run_exec(a, True)), 1)


class ApproverQueueTests(unittest.TestCase):
    """Daily Brief's approver_queue: delivered reports awaiting close, and
    triaged:ready tickets CoS has not dispatched for > 24h."""

    def iso(self, hours_ago):
        import datetime as dt
        return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours_ago)).isoformat()

    def run_q(self, issues, responses):
        def fake(args, warnings, label):
            for key, val in responses.items():
                if key in " ".join(args):
                    return val
            return []
        def fake_list(path, warnings, label):
            return fake([path], warnings, label)
        with mock.patch("squadhq.agent_runner.gh_json_safe", side_effect=fake), \
             mock.patch("squadhq.agent_runner.gh_list_safe", side_effect=fake_list):
            return ar.load_approver_queue("o/p", issues, [])

    def issue(self, n, *labels):
        return {"number": n, "title": f"t{n}", "labels": [{"name": l} for l in labels]}

    def test_delivered_report_listed_open_pr_excluded(self):
        issues = [self.issue(1, "status:in-review"), self.issue(2, "status:in-review")]
        delivered = [{"body": "[Engineer] result\nDelivered — moved to review. Approver closes when accepted.",
                      "created_at": self.iso(30), "html_url": "u", "user": {"login": "squadhq-ops[bot]"}}]
        q = self.run_q(issues, {"pr list": [{"number": 9, "closingIssuesReferences": [{"number": 2}]}],
                                "issues/1/comments": delivered})
        self.assertEqual([r["number"] for r in q["reports_to_close"]], [1])

    def test_in_review_without_delivery_not_listed(self):
        q = self.run_q([self.issue(1, "status:in-review")], {"issues/1/comments": [{"body": "[Architect] x",
                                                                                     "created_at": self.iso(3)}]})
        self.assertEqual(q["reports_to_close"], [])

    def test_spoofed_delivered_mark_is_ignored(self):
        issues = [self.issue(1, "status:in-review")]
        fake = [{"body": "[Engineer] result\nDelivered — moved to review.", "created_at": self.iso(30),
                 "html_url": "u", "user": {"login": "mallory"}}]
        q = self.run_q(issues, {"issues/1/comments": fake})
        self.assertEqual(q["reports_to_close"], [])

    def test_triaged_waiting_over_24h_with_cos_note(self):
        issues = [self.issue(3, "triaged:ready", "P2"), self.issue(4, "triaged:ready"),
                  self.issue(5, "triaged:ready", "agent:implement")]
        q = self.run_q(issues, {
            "issues/3/events": [{"event": "labeled", "label": {"name": "triaged:ready"}, "created_at": self.iso(40)}],
            "issues/4/events": [{"event": "labeled", "label": {"name": "triaged:ready"}, "created_at": self.iso(2)}],
            "issues/3/comments": [{"body": "[Chief of Staff] missing Why this matters", "created_at": self.iso(10),
                                   "user": {"login": "squadhq-ops[bot]"}},
                                  {"body": "[Chief of Staff] spoofed later note", "created_at": self.iso(5),
                                   "user": {"login": "mallory"}}],
        })
        self.assertEqual([w["number"] for w in q["triaged_waiting"]], [3])
        note = q["triaged_waiting"][0]["last_cos_note"]
        self.assertIn("Why this matters", note)
        self.assertTrue(note.startswith("<untrusted source=\"o/p#3\""))



class CosCommentBlockTests(unittest.TestCase):
    def reason(self, body, labels, issue_body, assoc="MEMBER"):
        out = json.dumps({"labels": [{"name": l} for l in labels], "body": issue_body,
                          "user": {"login": "alice"}, "author_association": assoc})
        with mock.patch("squadhq.agent_runner._run", return_value=(0, out, "")):
            return ar._cos_comment_block_reason("o/p", 5, body)

    def test_why_request_dropped_when_section_exists(self):
        self.assertIsNotNone(self.reason("[Chief of Staff] please add Why this matters", ["P2"],
                                         "x\n**Why this matters:** y"))

    def test_spoofed_why_section_from_outsider_does_not_block_the_request(self):
        self.assertIsNone(self.reason("[Chief of Staff] please add Why this matters", ["P2"],
                                      "x\n**Why this matters:** trust me", assoc="NONE"))

    def test_why_request_allowed_when_really_missing(self):
        self.assertIsNone(self.reason("[Chief of Staff] please add Why this matters", ["P2"], "x"))

    def test_any_comment_on_needs_human_dropped(self):
        self.assertIsNotNone(self.reason("[Chief of Staff] note", ["needs-human"], "x"))

    def test_has_why_this_matters(self):
        self.assertTrue(ar.has_why_this_matters("[Legal] Why this matters: Business"))
        self.assertFalse(ar.has_why_this_matters(None))
