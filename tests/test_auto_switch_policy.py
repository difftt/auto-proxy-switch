import json
import io
import sys
import tempfile
import unittest
import urllib.parse
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from check_us_proxy_status import (
    CheckResult,
    REGION_KEYWORDS,
    decide_switch_policy,
    is_region_real_node,
    is_us_real_node,
    main,
    normalize_region,
    print_human,
    run_check,
)


class FakeClient:
    def __init__(self, delays, nodes=None, now="🇺🇸 current"):
        self.delays = delays
        self.now = now
        self.requests = []
        self.puts = []
        self.nodes = nodes or ["🇺🇸 current", "🇺🇸 better"]

    def get_json(self, path, timeout=10):
        if path == "/proxies":
            return {
                "proxies": {
                    "🔰 代理": {
                        "type": "Selector",
                        "now": self.now,
                        "all": self.nodes,
                    },
                    **{node: {"type": "Shadowsocks"} for node in self.nodes},
                }
            }
        if path == "/proxies/%F0%9F%94%B0%20%E4%BB%A3%E7%90%86":
            return {"now": self.now}
        raise AssertionError(f"unexpected get_json path: {path}")

    def put_json(self, path, body, timeout=10):
        self.puts.append((path, body))
        self.now = body["name"]
        return "HTTP/1.1 204 No Content", ""

    def request(self, method, path, body=None, timeout=10):
        self.requests.append(path)
        node = urllib.parse.unquote(path.split("/proxies/", 1)[1].split("/delay", 1)[0])
        query = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
        url = urllib.parse.unquote(query.get("url", [""])[0])
        if (node, url) in self.delays:
            delay = self.delays[(node, url)]
        else:
            delay = self.delays[node]
        if isinstance(delay, list):
            delay = delay.pop(0)
        if delay == "unknown":
            return "HTTP/1.1 200 OK", "{}"
        if delay is None:
            return "HTTP/1.1 504 Gateway Timeout", "{}"
        return "HTTP/1.1 200 OK", json.dumps({"delay": delay})

    def requested_nodes(self):
        nodes = []
        for path in self.requests:
            node = urllib.parse.unquote(path.split("/proxies/", 1)[1].split("/delay", 1)[0])
            nodes.append(node)
        return nodes


def run_fake(client, state_file, **kwargs):
    targets = kwargs.pop("targets", {})
    return run_check(
        client=client,
        base_url="http://example.test/204",
        timeout_ms=1000,
        targets=targets,
        target_timeout_ms=1000,
        auto_switch_if_current_not_good=True,
        switch_check_target=None,
        prefer_groups=["🔰 代理"],
        state_file=str(state_file),
        now=1000.0,
        **kwargs,
    )


class AutoSwitchPolicyTest(unittest.TestCase):
    def test_region_keywords_cover_supported_regions(self):
        self.assertEqual({"us", "sg", "uk", "jp", "hk", "de", "fr"}, set(REGION_KEYWORDS))
        self.assertEqual(("🇺🇸", "美国", "US", "USA", "United States"), REGION_KEYWORDS["us"])
        with self.assertRaises(TypeError):
            REGION_KEYWORDS["ca"] = ("加拿大",)

    def test_normalize_region_is_case_insensitive_and_rejects_invalid_value(self):
        self.assertEqual("us", normalize_region(None))
        self.assertEqual("us", normalize_region("US"))
        self.assertEqual("sg", normalize_region("Sg"))
        with self.assertRaisesRegex(ValueError, "--region: invalid value 'ca'"):
            normalize_region("ca")

    def test_us_real_node_keeps_legacy_keywords_and_boundaries(self):
        proxy = {"type": "Shadowsocks"}

        for name in ["🇺🇸 fast", "美国 fast", "US fast", "usa fast", "United States fast"]:
            self.assertTrue(is_us_real_node(name, proxy), name)
            self.assertTrue(is_region_real_node(name, proxy, "US"), name)

        for name in ["BUS node", "USAble node", "united kingdom"]:
            self.assertFalse(is_us_real_node(name, proxy), name)

    def test_region_real_node_filters_groups_special_names_and_other_regions(self):
        self.assertTrue(is_region_real_node("新加坡 01", {"type": "Shadowsocks"}, "sg"))
        self.assertTrue(is_region_real_node("Singapore 01", {"type": "Shadowsocks"}, "SG"))
        self.assertFalse(is_region_real_node("🇺🇸 01", {"type": "Shadowsocks"}, "sg"))
        self.assertFalse(is_region_real_node("新加坡 group", {"type": "Selector"}, "sg"))
        self.assertFalse(is_region_real_node("DIRECT", {"type": "Shadowsocks"}, "sg"))

    def test_run_check_uses_requested_region_for_real_node_filtering(self):
        client = FakeClient(
            {"🇺🇸 current": 120, "新加坡 better": 80, "Singapore backup": 90},
            nodes=["🇺🇸 current", "新加坡 better", "Singapore backup"],
        )

        result = run_check(
            client=client,
            base_url="http://example.test/204",
            timeout_ms=1000,
            targets={},
            target_timeout_ms=1000,
            state_file=None,
            region="SG",
        )

        self.assertEqual(["新加坡 better", "Singapore backup"], client.requested_nodes())
        self.assertEqual("sg", result["region"])
        self.assertEqual(
            "name contains 新加坡 / SG / Singapore; excludes strategy groups and special nodes",
            result["filter"],
        )
        self.assertEqual(2, result["region_nodes_count"])

    def test_run_check_rejects_invalid_region_before_requesting_proxies(self):
        client = FakeClient({"🇺🇸 current": 120})

        with self.assertRaisesRegex(ValueError, "--region: invalid value 'ca'"):
            run_check(
                client=client,
                base_url="http://example.test/204",
                timeout_ms=1000,
                targets={},
                target_timeout_ms=1000,
                state_file=None,
                region="ca",
            )

        self.assertEqual([], client.requests)

    def test_cli_help_includes_region_option(self):
        buffer = io.StringIO()
        with patch.object(sys, "argv", ["check_us_proxy_status.py", "--help"]):
            with redirect_stdout(buffer):
                with self.assertRaises(SystemExit) as raised:
                    main()

        self.assertEqual(0, raised.exception.code)
        self.assertIn("--region", buffer.getvalue())

    def test_cli_invalid_region_exits_1_without_requesting_proxies(self):
        buffer = io.StringIO()
        with patch.object(sys, "argv", ["check_us_proxy_status.py", "--region", "ca"]):
            with patch("check_us_proxy_status.MihomoUnixClient") as client_class:
                with redirect_stdout(buffer):
                    code = main()

        self.assertEqual(1, code)
        self.assertEqual("--region: invalid value 'ca'\n", buffer.getvalue())
        client_class.assert_not_called()

    def test_cli_invalid_region_json_keeps_structured_error_without_requesting_proxies(self):
        buffer = io.StringIO()
        with patch.object(sys, "argv", ["check_us_proxy_status.py", "--region", "ca", "--json"]):
            with patch("check_us_proxy_status.MihomoUnixClient") as client_class:
                with redirect_stdout(buffer):
                    code = main()

        self.assertEqual(1, code)
        self.assertEqual({"error": "--region: invalid value 'ca'"}, json.loads(buffer.getvalue()))
        client_class.assert_not_called()

    def test_default_policy_fields_match_requirements(self):
        client = FakeClient({"🇺🇸 current": 120, "🇺🇸 better": 80})

        result = run_check(
            client=client,
            base_url="http://example.test/204",
            timeout_ms=1000,
            targets={},
            target_timeout_ms=1000,
            auto_switch_if_current_not_good=True,
            switch_check_target=None,
            prefer_groups=["🔰 代理"],
            state_file=None,
            now=1000.0,
        )

        self.assertEqual("poor", result["switch_policy"]["bad_threshold"])
        self.assertEqual(600, result["switch_policy"]["slow_switch_threshold_ms"])
        self.assertEqual(5, result["switch_policy"]["slow_confirm_count"])
        self.assertEqual(600, result["switch_policy"]["switch_cooldown_seconds"])

    def test_auto_switch_skips_when_current_node_is_not_requested_region(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            client = FakeClient(
                {"🇺🇸 current": 900, "新加坡 better": 100},
                nodes=["🇺🇸 current", "新加坡 better"],
                now="🇺🇸 current",
            )

            result = run_fake(client, state_file, region="sg", bad_confirm_count=1)

            self.assertEqual([], client.requested_nodes())
            self.assertEqual([], client.puts)
            self.assertEqual("current_node_region_mismatch", result["current_node"]["reason"])
            self.assertEqual("🇺🇸 current", result["current_node"]["current_raw_node"])
            self.assertEqual("sg", result["current_node"]["expected_region"])
            self.assertEqual("current_node_region_mismatch", result["switch_decision"]["reason"])
            self.assertFalse(result["switch_decision"]["should_scan_candidates"])
            self.assertFalse(result["auto_switch"]["candidate_scan_started"])
            self.assertEqual("skipped", result["auto_switch"]["status"])
            self.assertEqual("current_node_region_mismatch", result["auto_switch"]["reason"])

    def test_auto_switch_candidates_are_limited_to_requested_region(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            client = FakeClient(
                {"新加坡 current": 900, "新加坡 better": 100, "🇺🇸 faster": 50},
                nodes=["新加坡 current", "新加坡 better", "🇺🇸 faster"],
                now="新加坡 current",
            )

            result = run_fake(client, state_file, region="sg", bad_confirm_count=1)

            self.assertEqual(["新加坡 current", "新加坡 better"], client.requested_nodes())
            self.assertEqual("success", result["auto_switch"]["status"])
            self.assertEqual("新加坡 better", result["auto_switch"]["to_node"])
            self.assertNotIn("🇺🇸 faster", client.requested_nodes())

    def test_good_current_does_not_scan_candidates_and_resets_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            state_file.write_text(
                json.dumps({
                    "current": {
                        "key": "🔰 代理\n🇺🇸 current\nbase",
                        "bad_count": 3,
                        "slow_count": 2,
                        "dead_count": 1,
                    }
                }),
                encoding="utf-8",
            )
            client = FakeClient({"🇺🇸 current": 120, "🇺🇸 better": 80})

            result = run_fake(client, state_file)

            self.assertEqual(["🇺🇸 current"], client.requested_nodes())
            self.assertFalse(result["switch_decision"]["should_scan_candidates"])
            self.assertEqual("current_node_good", result["switch_decision"]["reason"])
            saved = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(0, saved["current"]["bad_count"])
            self.assertEqual(0, saved["current"]["slow_count"])
            self.assertEqual(0, saved["current"]["dead_count"])

    def test_acceptable_slow_current_does_not_scan_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            client = FakeClient({"🇺🇸 current": 500, "🇺🇸 better": 100})

            result = run_fake(client, state_file, slow_switch_threshold_ms=700)

            self.assertEqual(["🇺🇸 current"], client.requested_nodes())
            self.assertFalse(result["auto_switch"]["candidate_scan_started"])
            self.assertEqual("current_node_slow_acceptable", result["switch_decision"]["reason"])

    def test_default_slow_threshold_counts_slow_above_600ms(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            client = FakeClient({"🇺🇸 current": 700, "🇺🇸 better": 100})

            result = run_fake(client, state_file)

            self.assertEqual(["🇺🇸 current"], client.requested_nodes())
            self.assertFalse(result["switch_decision"]["should_scan_candidates"])
            self.assertEqual("slow_wait_confirm", result["switch_decision"]["reason"])
            self.assertEqual(600, result["switch_policy"]["slow_switch_threshold_ms"])
            self.assertEqual(1, result["switch_policy"]["slow_count"])
            self.assertEqual(5, result["switch_policy"]["slow_confirm_count"])

    def test_poor_current_requires_confirmation_before_switching(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            first_client = FakeClient({"🇺🇸 current": 900, "🇺🇸 better": 100})

            first = run_fake(first_client, state_file, bad_confirm_count=2)

            self.assertEqual(["🇺🇸 current"], first_client.requested_nodes())
            self.assertFalse(first["switch_decision"]["should_scan_candidates"])
            self.assertEqual("bad_wait_confirm", first["switch_decision"]["reason"])

            second_client = FakeClient({"🇺🇸 current": 900, "🇺🇸 better": 100})
            second = run_fake(second_client, state_file, bad_confirm_count=2)

            self.assertEqual(["🇺🇸 current", "🇺🇸 better"], second_client.requested_nodes())
            self.assertTrue(second["switch_decision"]["should_scan_candidates"])
            self.assertEqual("success", second["auto_switch"]["status"])
            self.assertEqual("🇺🇸 better", second["auto_switch"]["to_node"])
            saved = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual("🇺🇸 better", saved["last_switch"]["to_node"])

    def test_cooldown_blocks_confirmed_bad_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            state_file.write_text(
                json.dumps({
                    "current": {
                        "key": "🔰 代理\n🇺🇸 current\nbase",
                        "bad_count": 1,
                        "slow_count": 0,
                        "dead_count": 0,
                    },
                    "last_switch": {"at": 950.0, "to_node": "🇺🇸 better"},
                }),
                encoding="utf-8",
            )
            client = FakeClient({"🇺🇸 current": 900, "🇺🇸 better": 100})

            result = run_fake(client, state_file, bad_confirm_count=2, switch_cooldown_seconds=100)

            self.assertEqual(["🇺🇸 current"], client.requested_nodes())
            self.assertFalse(result["switch_decision"]["should_scan_candidates"])
            self.assertTrue(result["switch_policy"]["in_cooldown"])
            self.assertEqual("switch_cooldown_active", result["switch_decision"]["reason"])

    def test_dead_current_can_break_cooldown_after_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            state_file.write_text(
                json.dumps({
                    "current": {
                        "key": "🔰 代理\n🇺🇸 current\nbase",
                        "bad_count": 2,
                        "slow_count": 0,
                        "dead_count": 2,
                    },
                    "last_switch": {"at": 950.0, "to_node": "🇺🇸 better"},
                }),
                encoding="utf-8",
            )
            client = FakeClient({"🇺🇸 current": None, "🇺🇸 better": 100})

            result = run_fake(
                client,
                state_file,
                bad_confirm_count=2,
                switch_cooldown_seconds=100,
                break_cooldown_dead_count=3,
                avoid_recent_switches=0,
            )

            self.assertEqual(["🇺🇸 current", "🇺🇸 better"], client.requested_nodes())
            self.assertTrue(result["switch_policy"]["cooldown_break_allowed"])
            self.assertEqual("success", result["auto_switch"]["status"])

    def test_unknown_current_does_not_break_cooldown(self):
        policy, decision, _ = decide_switch_policy(
            {
                "current": {
                    "key": "🔰 代理\n🇺🇸 current\nbase",
                    "bad_count": 2,
                    "slow_count": 0,
                    "dead_count": 2,
                },
                "last_switch": {"at": 950.0, "to_node": "🇺🇸 better"},
            },
            now=1000.0,
            current_group="🔰 代理",
            current_node="🇺🇸 current",
            switch_by="base",
            switch_level="unknown",
            check=CheckResult(ok=True, delay=None),
            state_load_error=None,
            bad_threshold="poor",
            bad_confirm_count=2,
            slow_switch_threshold_ms=600,
            slow_confirm_count=5,
            switch_cooldown_seconds=100,
            break_cooldown_dead_count=3,
        )

        self.assertFalse(policy["cooldown_break_allowed"])
        self.assertEqual(0, policy["dead_count"])
        self.assertEqual("switch_cooldown_active", decision["reason"])

    def test_corrupt_state_file_blocks_switch_and_reports_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            state_file.write_text("{not json", encoding="utf-8")
            client = FakeClient({"🇺🇸 current": 900, "🇺🇸 better": 100})

            result = run_fake(client, state_file, bad_confirm_count=1)

            self.assertEqual(["🇺🇸 current"], client.requested_nodes())
            self.assertFalse(result["switch_decision"]["should_scan_candidates"])
            self.assertEqual("state_load_error", result["switch_decision"]["reason"])
            self.assertIn("state_load_error", result["switch_policy"])

    def test_same_level_candidate_below_improvement_threshold_does_not_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            client = FakeClient({"🇺🇸 current": 900, "🇺🇸 better": 850})

            result = run_fake(client, state_file, bad_confirm_count=1, min_improvement_ms=100)

            self.assertEqual("skipped", result["auto_switch"]["status"])
            self.assertEqual("no_available_candidate", result["auto_switch"]["reason"])
            self.assertEqual(1, result["auto_switch"]["candidate_filter"]["filtered_not_improved"])
            self.assertEqual([], client.puts)

    def test_level_improvement_can_switch_even_without_delay_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            client = FakeClient({"🇺🇸 current": 850, "🇺🇸 better": 790})

            result = run_fake(client, state_file, bad_confirm_count=1, min_improvement_ms=100)

            self.assertEqual("success", result["auto_switch"]["status"])
            self.assertEqual("🇺🇸 better", result["auto_switch"]["to_node"])
            self.assertEqual(1, result["auto_switch"]["candidate_filter"]["eligible"])

    def test_dead_current_without_comparable_delay_can_switch_to_reachable_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            client = FakeClient({"🇺🇸 current": None, "🇺🇸 better": 900})

            result = run_fake(client, state_file, bad_confirm_count=1, min_improvement_ms=100)

            self.assertEqual("success", result["auto_switch"]["status"])
            self.assertEqual("🇺🇸 better", result["auto_switch"]["to_node"])

    def test_candidate_confirmation_failure_blocks_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            client = FakeClient({"🇺🇸 current": 900, "🇺🇸 better": [700, None]})

            result = run_fake(client, state_file, bad_confirm_count=1, confirm_candidate=True)

            self.assertEqual("skipped", result["auto_switch"]["status"])
            self.assertEqual("candidate_confirmation_failed", result["auto_switch"]["reason"])
            self.assertEqual([], client.puts)
            self.assertFalse(result["auto_switch"]["candidate_confirmation"]["passed"])

    def test_candidate_confirmation_must_still_be_improved(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            client = FakeClient({"🇺🇸 current": 900, "🇺🇸 better": [700, 850]})

            result = run_fake(
                client,
                state_file,
                bad_confirm_count=1,
                min_improvement_ms=100,
                confirm_candidate=True,
            )

            self.assertEqual("skipped", result["auto_switch"]["status"])
            self.assertEqual("candidate_confirmation_not_improved", result["auto_switch"]["reason"])
            self.assertEqual([], client.puts)
            confirmation = result["auto_switch"]["candidate_confirmation"]
            self.assertFalse(confirmation["passed"])
            self.assertEqual("candidate_confirmation_not_improved", confirmation["reason"])

    def test_candidate_confirmation_uses_confirm_target_current_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            targets = {"discord": "https://discord.test/api"}
            base_url = "http://example.test/204"
            discord_url = targets["discord"]
            client = FakeClient({
                ("🇺🇸 current", base_url): 950,
                ("🇺🇸 current", discord_url): 900,
                ("🇺🇸 better", base_url): 100,
                ("🇺🇸 better", discord_url): [700, 850],
            })

            result = run_fake(
                client,
                state_file,
                targets=targets,
                bad_confirm_count=1,
                min_improvement_ms=100,
                confirm_candidate=True,
                confirm_target="discord",
            )

            self.assertEqual("skipped", result["auto_switch"]["status"])
            self.assertEqual("candidate_confirmation_not_improved", result["auto_switch"]["reason"])
            self.assertEqual([], client.puts)
            confirmation = result["auto_switch"]["candidate_confirmation"]
            self.assertFalse(confirmation["passed"])
            self.assertEqual("discord", confirmation["target"])
            self.assertEqual(850, confirmation["check"]["delay_ms"])
            self.assertEqual("candidate_confirmation_not_improved", confirmation["reason"])

    def test_empty_confirm_target_raises_value_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            client = FakeClient({"🇺🇸 current": 900, "🇺🇸 better": 100})

            with self.assertRaisesRegex(ValueError, "--confirm-target cannot be empty"):
                run_fake(client, state_file, confirm_target="")

    def test_missing_confirm_target_still_raises_value_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            client = FakeClient({"🇺🇸 current": 900, "🇺🇸 better": 100})

            with self.assertRaisesRegex(ValueError, "--confirm-target not present in targets: missing"):
                run_fake(client, state_file, targets={}, confirm_target="missing")

    def test_recently_switched_nodes_are_filtered(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            state_file.write_text(
                json.dumps({
                    "last_switch": {
                        "at": 900.0,
                        "from_node": "🇺🇸 current",
                        "to_node": "🇺🇸 better",
                    }
                }),
                encoding="utf-8",
            )
            client = FakeClient({"🇺🇸 current": 900, "🇺🇸 better": 100})

            result = run_fake(
                client,
                state_file,
                bad_confirm_count=1,
                switch_cooldown_seconds=0,
                avoid_recent_switches=3,
                avoid_recent_window_seconds=1800,
            )

            self.assertEqual("skipped", result["auto_switch"]["status"])
            self.assertEqual("no_available_candidate", result["auto_switch"]["reason"])
            self.assertEqual(1, result["auto_switch"]["candidate_filter"]["filtered_recent"])
            self.assertEqual([], client.puts)

    def test_human_output_includes_observability_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            state_file.write_text(
                json.dumps({
                    "current": {
                        "key": "🔰 代理\n🇺🇸 current\nbase",
                        "bad_count": 1,
                        "slow_count": 0,
                        "dead_count": 0,
                    },
                    "last_switch": {"at": 950.0, "to_node": "🇺🇸 other"},
                }),
                encoding="utf-8",
            )
            client = FakeClient({"🇺🇸 current": 900, "🇺🇸 better": 100})
            result = run_fake(client, state_file, bad_confirm_count=2, switch_cooldown_seconds=100)

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                print_human(result)

            output = buffer.getvalue()
            self.assertIn("决策原因: switch_cooldown_active", output)
            self.assertIn("计数器 bad: 2/2", output)
            self.assertIn("计数器 slow: 0/5", output)
            self.assertIn("计数器 dead: 0/3", output)
            self.assertIn("冷却状态: 生效，剩余 50 秒", output)
            self.assertIn("候选过滤: 未扫描", output)


if __name__ == "__main__":
    unittest.main()
