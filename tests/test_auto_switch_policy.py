import json
import io
import inspect
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import check_us_proxy_status as compat_wrapper
from check_proxy_status import (
    CheckResult,
    DEFAULT_CONCURRENT,
    DEFAULT_REGION,
    MihomoUnixClient,
    OPENAI_TARGET_NAME,
    OPENAI_TARGET_URL,
    REGION_KEYWORDS,
    build_targets,
    decide_switch_policy,
    is_region_real_node,
    is_us_real_node,
    main,
    normalize_region,
    print_human,
    run_check,
)


class UnixHTTPTestServer:
    def __init__(self, responses):
        self._tmp = tempfile.TemporaryDirectory()
        self.sock_path = str(Path(self._tmp.name) / "mihomo.sock")
        self._responses = list(responses)
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._stop = threading.Event()
        self.accepted_connections = 0
        self.request_lines = []
        self._thread = threading.Thread(target=self._serve)

    def __enter__(self):
        self._thread.start()
        self._ready.wait(timeout=2)
        return self

    def __exit__(self, exc_type, exc, tb):
        self._stop.set()
        wakeup = None
        try:
            wakeup = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            wakeup.settimeout(0.2)
            wakeup.connect(self.sock_path)
        except OSError:
            pass
        finally:
            if wakeup is not None:
                wakeup.close()
        self._thread.join(timeout=2)
        self._tmp.cleanup()

    def _serve(self):
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.settimeout(0.2)
        server.bind(self.sock_path)
        server.listen()
        self._ready.set()
        try:
            while not self._stop.is_set():
                with self._lock:
                    if not self._responses:
                        break
                try:
                    conn, _ = server.accept()
                except socket.timeout:
                    continue
                self.accepted_connections += 1
                with conn:
                    conn.settimeout(2)
                    while not self._stop.is_set():
                        response = self._next_response()
                        if response is None:
                            return
                        if response == b"":
                            break
                        request = self._read_request(conn)
                        if not request:
                            break
                        first_line = request.split(b"\r\n", 1)[0].decode("utf-8", errors="replace")
                        self.request_lines.append(first_line)
                        conn.sendall(response)
                        if b"Connection: close" in response:
                            break
        finally:
            server.close()

    def _next_response(self):
        with self._lock:
            if not self._responses:
                return None
            return self._responses.pop(0)

    def _read_request(self, conn):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(65536)
            if not chunk:
                return data
            data += chunk
        head, _, body = data.partition(b"\r\n\r\n")
        content_length = 0
        for line in head.split(b"\r\n")[1:]:
            name, _, value = line.partition(b":")
            if name.lower() == b"content-length":
                content_length = int(value.strip())
                break
        while len(body) < content_length:
            chunk = conn.recv(65536)
            if not chunk:
                break
            body += chunk
        return head + b"\r\n\r\n" + body


def http_response(body, connection="keep-alive"):
    body_bytes = body.encode("utf-8")
    return (
        b"HTTP/1.1 200 OK\r\n"
        + f"Connection: {connection}\r\n".encode("ascii")
        + f"Content-Length: {len(body_bytes)}\r\n".encode("ascii")
        + b"\r\n"
        + body_bytes
    )


class FakeClient:
    def __init__(self, delays, nodes=None, now="🇺🇸 current"):
        self.delays = delays
        self.now = now
        self.requests = []
        self.puts = []
        self.nodes = nodes or ["🇺🇸 current", "🇺🇸 better"]
        self.proxy_snapshots = 0

    def get_json(self, path, timeout=10):
        if path == "/proxies":
            self.proxy_snapshots += 1
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


class ConcurrentRequestProbe:
    def __init__(self, parties):
        self.barrier = threading.Barrier(parties)
        self.lock = threading.Lock()
        self.in_flight = 0
        self.peak_in_flight = 0

    def wait_for_overlap(self):
        with self.lock:
            self.in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        try:
            self.barrier.wait(timeout=2)
        except threading.BrokenBarrierError as exc:
            raise AssertionError("worker requests did not overlap") from exc
        finally:
            with self.lock:
                self.in_flight -= 1


class CloseableOverlapClient(FakeClient):
    def __init__(self, delays, nodes=None, now="🇺🇸 current", probe=None):
        super().__init__(delays, nodes=nodes, now=now)
        self.probe = probe
        self.closed = False
        self.close_count = 0

    def request(self, method, path, body=None, timeout=10):
        if self.closed:
            raise AssertionError("request called after close")
        if self.probe is not None:
            self.probe.wait_for_overlap()
        return super().request(method, path, body, timeout)

    def close(self):
        self.close_count += 1
        self.closed = True


def run_fake(client, state_file, **kwargs):
    targets = kwargs.pop("targets", {})
    switch_check_target = kwargs.pop("switch_check_target", None)
    return run_check(
        client=client,
        base_url="http://example.test/204",
        timeout_ms=1000,
        targets=targets,
        target_timeout_ms=1000,
        auto_switch_if_current_not_good=True,
        switch_check_target=switch_check_target,
        prefer_groups=["🔰 代理"],
        state_file=str(state_file),
        now=1000.0,
        **kwargs,
    )


class AutoSwitchPolicyTest(unittest.TestCase):
    def test_mihomo_unix_client_reuses_connection_for_consecutive_requests(self):
        with UnixHTTPTestServer([
            http_response('{"ok": 1}'),
            http_response('{"ok": 2}'),
        ]) as server:
            client = MihomoUnixClient(server.sock_path)

            first = client.request("GET", "/first", timeout=1)
            second = client.request("GET", "/second", timeout=1)
            client.close()

        self.assertEqual(("HTTP/1.1 200 OK", '{"ok": 1}'), first)
        self.assertEqual(("HTTP/1.1 200 OK", '{"ok": 2}'), second)
        self.assertEqual(1, server.accepted_connections)
        self.assertEqual(["GET /first HTTP/1.1", "GET /second HTTP/1.1"], server.request_lines)

    def test_mihomo_unix_client_reconnects_after_remote_close(self):
        with UnixHTTPTestServer([
            b"",
            http_response('{"ok": true}'),
        ]) as server:
            client = MihomoUnixClient(server.sock_path)

            with self.assertRaises(Exception):
                client.request("GET", "/closed", timeout=1)
            status, text = client.request("GET", "/reconnected", timeout=1)
            client.close()

        self.assertEqual("HTTP/1.1 200 OK", status)
        self.assertEqual('{"ok": true}', text)
        self.assertEqual(2, server.accepted_connections)
        self.assertEqual(["GET /reconnected HTTP/1.1"], server.request_lines)

    def test_openai_target_is_explicit_and_not_loaded_by_default(self):
        class Args:
            no_default_targets = False
            target = None

        self.assertEqual({"discord": "https://discord.com/api/v10/gateway"}, build_targets(Args))

        Args.target = [OPENAI_TARGET_NAME]
        self.assertEqual(
            {
                "discord": "https://discord.com/api/v10/gateway",
                OPENAI_TARGET_NAME: OPENAI_TARGET_URL,
            },
            build_targets(Args),
        )

        Args.target = [f"{OPENAI_TARGET_NAME}={OPENAI_TARGET_URL}"]
        self.assertEqual(
            {
                "discord": "https://discord.com/api/v10/gateway",
                OPENAI_TARGET_NAME: OPENAI_TARGET_URL,
            },
            build_targets(Args),
        )

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

    def test_run_check_reports_openai_target_status_when_explicitly_configured(self):
        base_url = "http://example.test/204"
        client = FakeClient({
            ("🇺🇸 current", base_url): 120,
            ("🇺🇸 current", OPENAI_TARGET_URL): 502,
            ("🇺🇸 better", base_url): 80,
            ("🇺🇸 better", OPENAI_TARGET_URL): None,
        })

        result = run_check(
            client=client,
            base_url=base_url,
            timeout_ms=1000,
            targets={OPENAI_TARGET_NAME: OPENAI_TARGET_URL},
            target_timeout_ms=1000,
            state_file=None,
        )

        self.assertEqual(OPENAI_TARGET_URL, result["targets"][OPENAI_TARGET_NAME])
        self.assertEqual(2, result["region_nodes_count"])
        self.assertEqual("slow", result["nodes"][0]["targets"][OPENAI_TARGET_NAME]["level"])
        self.assertEqual("dead", result["nodes"][1]["targets"][OPENAI_TARGET_NAME]["level"])
        self.assertEqual(1, len(result["target_alive"][OPENAI_TARGET_NAME]))
        self.assertEqual(1, len(result["target_dead"][OPENAI_TARGET_NAME]))

    def test_run_check_restores_once_per_node_after_base_and_targets(self):
        base_url = "http://example.test/204"
        targets = {
            "discord": "https://discord.test/api",
            OPENAI_TARGET_NAME: OPENAI_TARGET_URL,
        }

        class ChangingClient(FakeClient):
            def request(self, method, path, body=None, timeout=10):
                status, text = super().request(method, path, body, timeout)
                self.now = "🇺🇸 temporary"
                return status, text

        client = ChangingClient({
            ("🇺🇸 current", base_url): 120,
            ("🇺🇸 current", targets["discord"]): 130,
            ("🇺🇸 current", OPENAI_TARGET_URL): 140,
            ("🇺🇸 better", base_url): 80,
            ("🇺🇸 better", targets["discord"]): 90,
            ("🇺🇸 better", OPENAI_TARGET_URL): 100,
        })

        result = run_check(
            client=client,
            base_url=base_url,
            timeout_ms=1000,
            targets=targets,
            target_timeout_ms=1000,
            state_file=None,
            concurrent=1,
        )

        self.assertEqual(
            ["node:🇺🇸 current", "node:🇺🇸 better"],
            [event["after_check"] for event in result["restore_events"]],
        )
        self.assertEqual(5, client.proxy_snapshots)

    def test_default_mode_concurrent_preserves_region_node_order_and_uses_factory_clients(self):
        base_url = "http://example.test/204"
        nodes = ["🇺🇸 slow", "🇺🇸 fast", "🇺🇸 middle"]
        delays = {
            "🇺🇸 slow": 300,
            "🇺🇸 fast": 80,
            "🇺🇸 middle": 160,
        }
        probe = ConcurrentRequestProbe(parties=len(nodes))
        main_client = FakeClient(delays, nodes=nodes)
        worker_clients = []
        worker_clients_lock = threading.Lock()

        def client_factory():
            worker = CloseableOverlapClient(delays, nodes=nodes, probe=probe)
            with worker_clients_lock:
                worker_clients.append(worker)
            return worker

        result = run_check(
            client=main_client,
            base_url=base_url,
            timeout_ms=1000,
            targets={},
            target_timeout_ms=1000,
            state_file=None,
            concurrent=3,
            client_factory=client_factory,
        )

        self.assertEqual(nodes, [node["name"] for node in result["nodes"]])
        self.assertEqual([], main_client.requested_nodes())
        self.assertEqual(3, len(worker_clients))
        self.assertTrue(all(len(client.requested_nodes()) == 1 for client in worker_clients))
        self.assertCountEqual(nodes, [client.requested_nodes()[0] for client in worker_clients])
        self.assertGreater(probe.peak_in_flight, 1)
        self.assertTrue(all(client.closed for client in worker_clients))
        self.assertTrue(all(client.close_count == 1 for client in worker_clients))
        self.assertNotIn("concurrent", result["auto_switch"])

    def test_default_mode_concurrent_one_uses_passed_client_without_factory(self):
        base_url = "http://example.test/204"
        nodes = ["🇺🇸 current", "🇺🇸 better"]
        client = FakeClient({"🇺🇸 current": 120, "🇺🇸 better": 80}, nodes=nodes)

        def client_factory():
            raise AssertionError("client_factory should not be called when concurrent=1")

        result = run_check(
            client=client,
            base_url=base_url,
            timeout_ms=1000,
            targets={},
            target_timeout_ms=1000,
            state_file=None,
            concurrent=1,
            client_factory=client_factory,
        )

        self.assertEqual(nodes, [node["name"] for node in result["nodes"]])
        self.assertEqual(nodes, client.requested_nodes())

    def test_auto_switch_candidate_scan_uses_factory_clients_and_preserves_candidate_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            base_url = "http://example.test/204"
            nodes = ["🇺🇸 current", "🇺🇸 slow", "🇺🇸 fast", "🇺🇸 middle"]
            delays = {
                "🇺🇸 current": 900,
                "🇺🇸 slow": 300,
                "🇺🇸 fast": 80,
                "🇺🇸 middle": 160,
            }

            probe = ConcurrentRequestProbe(parties=len(nodes) - 1)
            main_client = FakeClient(delays, nodes=nodes)
            worker_clients = []
            worker_clients_lock = threading.Lock()

            def client_factory():
                worker = CloseableOverlapClient(delays, nodes=nodes, probe=probe)
                with worker_clients_lock:
                    worker_clients.append(worker)
                return worker

            result = run_fake(
                main_client,
                state_file,
                bad_confirm_count=1,
                concurrent=16,
                client_factory=client_factory,
            )

            self.assertEqual(nodes, [node["name"] for node in result["nodes"]])
            self.assertEqual(["🇺🇸 current"], main_client.requested_nodes())
            self.assertEqual(3, len(worker_clients))
            self.assertTrue(all(len(client.requested_nodes()) == 1 for client in worker_clients))
            self.assertCountEqual(nodes[1:], [client.requested_nodes()[0] for client in worker_clients])
            self.assertGreater(probe.peak_in_flight, 1)
            self.assertTrue(all(client.closed for client in worker_clients))
            self.assertTrue(all(client.close_count == 1 for client in worker_clients))
            self.assertEqual(3, result["auto_switch"]["concurrent"])
            self.assertEqual("success", result["auto_switch"]["status"])
            self.assertEqual("🇺🇸 fast", result["auto_switch"]["to_node"])

    def test_auto_switch_candidate_confirmation_uses_passed_client_serially(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            base_url = "http://example.test/204"
            nodes = ["🇺🇸 current", "🇺🇸 better", "🇺🇸 worse"]
            delays = {
                "🇺🇸 current": 900,
                "🇺🇸 better": 100,
                "🇺🇸 worse": 500,
            }
            main_client = FakeClient(delays, nodes=nodes)
            worker_clients = []

            def client_factory():
                worker = FakeClient(delays, nodes=nodes)
                worker_clients.append(worker)
                return worker

            result = run_fake(
                main_client,
                state_file,
                bad_confirm_count=1,
                confirm_candidate=True,
                concurrent=2,
                client_factory=client_factory,
            )

            self.assertEqual(["🇺🇸 current", "🇺🇸 better"], main_client.requested_nodes())
            self.assertEqual(2, len(worker_clients))
            self.assertCountEqual(nodes[1:], [client.requested_nodes()[0] for client in worker_clients])
            self.assertEqual(2, result["auto_switch"]["concurrent"])
            self.assertTrue(result["auto_switch"]["candidate_confirmation"]["passed"])
            self.assertEqual("success", result["auto_switch"]["status"])

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
        with patch.object(sys, "argv", ["check_proxy_status.py", "--help"]):
            with redirect_stdout(buffer):
                with self.assertRaises(SystemExit) as raised:
                    main()

        self.assertEqual(0, raised.exception.code)
        self.assertIn("--region", buffer.getvalue())

    def test_cli_help_includes_concurrent_option(self):
        buffer = io.StringIO()
        with patch.object(sys, "argv", ["check_proxy_status.py", "--help"]):
            with redirect_stdout(buffer):
                with self.assertRaises(SystemExit) as raised:
                    main()

        self.assertEqual(0, raised.exception.code)
        self.assertIn("--concurrent", buffer.getvalue())
        self.assertIn("default: 16", buffer.getvalue())

    def test_run_check_default_concurrent_is_16(self):
        concurrent_parameter = inspect.signature(run_check).parameters["concurrent"]

        self.assertEqual(16, DEFAULT_CONCURRENT)
        self.assertEqual(16, concurrent_parameter.default)

    def test_cli_passes_default_concurrent_16_to_run_check(self):
        buffer = io.StringIO()
        result = {"auto_switch": {"status": "disabled"}, "still_changed": {}}
        with patch.object(sys, "argv", ["check_proxy_status.py", "--no-default-targets", "--json"]):
            with patch("check_proxy_status.MihomoUnixClient") as client_class:
                with patch("check_proxy_status.run_check", return_value=result) as run_check_mock:
                    with redirect_stdout(buffer):
                        code = main()

        self.assertEqual(0, code)
        client_class.assert_called_once()
        self.assertEqual(16, run_check_mock.call_args.kwargs["concurrent"])

    def test_cli_rejects_concurrent_below_min_with_argparse_exit_2(self):
        buffer = io.StringIO()
        with patch.object(sys, "argv", ["check_proxy_status.py", "--concurrent", "0"]):
            with redirect_stderr(buffer):
                with self.assertRaises(SystemExit) as raised:
                    main()

        self.assertEqual(2, raised.exception.code)

    def test_cli_rejects_concurrent_above_max_with_argparse_exit_2(self):
        buffer = io.StringIO()
        with patch.object(sys, "argv", ["check_proxy_status.py", "--concurrent", "33"]):
            with redirect_stderr(buffer):
                with self.assertRaises(SystemExit) as raised:
                    main()

        self.assertEqual(2, raised.exception.code)

    def test_cli_rejects_non_integer_concurrent_with_argparse_exit_2(self):
        buffer = io.StringIO()
        with patch.object(sys, "argv", ["check_proxy_status.py", "--concurrent", "abc"]):
            with redirect_stderr(buffer):
                with self.assertRaises(SystemExit) as raised:
                    main()

        self.assertEqual(2, raised.exception.code)
        self.assertIn("--concurrent must be an integer in [1, 32]", buffer.getvalue())

    def test_cli_invalid_region_exits_1_without_requesting_proxies(self):
        buffer = io.StringIO()
        with patch.object(sys, "argv", ["check_proxy_status.py", "--region", "ca"]):
            with patch("check_proxy_status.MihomoUnixClient") as client_class:
                with redirect_stdout(buffer):
                    code = main()

        self.assertEqual(1, code)
        self.assertEqual("--region: invalid value 'ca'\n", buffer.getvalue())
        client_class.assert_not_called()

    def test_cli_invalid_region_json_keeps_structured_error_without_requesting_proxies(self):
        buffer = io.StringIO()
        with patch.object(sys, "argv", ["check_proxy_status.py", "--region", "ca", "--json"]):
            with patch("check_proxy_status.MihomoUnixClient") as client_class:
                with redirect_stdout(buffer):
                    code = main()

        self.assertEqual(1, code)
        self.assertEqual({"error": "--region: invalid value 'ca'"}, json.loads(buffer.getvalue()))
        client_class.assert_not_called()

    def test_cli_accepts_openai_target_alias(self):
        base_url = "http://example.test/204"
        client = FakeClient(
            {
                ("日本 current", base_url): 120,
                ("日本 current", OPENAI_TARGET_URL): 502,
            },
            nodes=["日本 current"],
            now="日本 current",
        )
        buffer = io.StringIO()
        with patch.object(
            sys,
            "argv",
            [
                "check_proxy_status.py",
                "--region",
                "jp",
                "--url",
                base_url,
                "--no-default-targets",
                "--target",
                "openai",
                "--json",
            ],
        ):
            with patch("check_proxy_status.MihomoUnixClient", return_value=client):
                with redirect_stdout(buffer):
                    code = main()

        result = json.loads(buffer.getvalue())
        self.assertEqual(0, code)
        self.assertEqual("jp", result["region"])
        self.assertEqual({OPENAI_TARGET_NAME: OPENAI_TARGET_URL}, result["targets"])
        self.assertEqual("slow", result["nodes"][0]["targets"][OPENAI_TARGET_NAME]["level"])

    def test_compat_wrapper_injects_default_region_us_when_absent(self):
        injected = compat_wrapper._inject_default_region(["check_us_proxy_status.py"])
        self.assertEqual(
            ["check_us_proxy_status.py", "--region", "us"], injected
        )

    def test_compat_wrapper_keeps_caller_region_unchanged(self):
        argv = ["check_us_proxy_status.py", "--region", "sg", "--json"]
        self.assertEqual(argv, compat_wrapper._inject_default_region(argv))

    def test_compat_wrapper_keeps_long_form_region_unchanged(self):
        argv = ["check_us_proxy_status.py", "--region=sg", "--json"]
        self.assertEqual(argv, compat_wrapper._inject_default_region(argv))

    def test_compat_wrapper_does_not_treat_substring_region_as_region_flag(self):
        """Tokens that merely contain the substring ``--region`` (e.g.
        ``--my-region-flag`` or ``--region-extra``) must not be treated as the
        region argument, so the wrapper still injects ``--region us``."""
        argv = ["check_us_proxy_status.py", "--my-region-flag", "value"]
        self.assertEqual(
            ["check_us_proxy_status.py", "--region", "us", "--my-region-flag", "value"],
            compat_wrapper._inject_default_region(argv),
        )

    def test_compat_wrapper_injects_with_unrelated_flag(self):
        """A token like ``--json`` that shares no substring with ``--region``
        must not block default-region injection."""
        argv = ["check_us_proxy_status.py", "--json"]
        self.assertEqual(
            ["check_us_proxy_status.py", "--region", "us", "--json"],
            compat_wrapper._inject_default_region(argv),
        )

    def test_compat_wrapper_main_defaults_to_us_region(self):
        buffer = io.StringIO()
        with patch.object(
            sys, "argv", ["check_us_proxy_status.py", "--no-default-targets", "--json"]
        ):
            with patch("check_proxy_status.MihomoUnixClient"):
                with patch(
                    "check_proxy_status.run_check", return_value={"auto_switch": {"status": "disabled"}, "still_changed": {}}
                ) as run_check_mock:
                    with redirect_stdout(buffer):
                        code = compat_wrapper.main()

        self.assertEqual(0, code)
        run_check_mock.assert_called_once()
        self.assertEqual("us", run_check_mock.call_args.kwargs["region"])

    def test_compat_wrapper_main_respects_caller_region(self):
        buffer = io.StringIO()
        with patch.object(
            sys,
            "argv",
            ["check_us_proxy_status.py", "--region", "sg", "--no-default-targets", "--json"],
        ):
            with patch("check_proxy_status.MihomoUnixClient"):
                with patch(
                    "check_proxy_status.run_check", return_value={"auto_switch": {"status": "disabled"}, "still_changed": {}}
                ) as run_check_mock:
                    with redirect_stdout(buffer):
                        code = compat_wrapper.main()

        self.assertEqual(0, code)
        self.assertEqual("sg", run_check_mock.call_args.kwargs["region"])

    def test_compat_wrapper_main_respects_long_form_region(self):
        buffer = io.StringIO()
        with patch.object(
            sys,
            "argv",
            ["check_us_proxy_status.py", "--region=sg", "--no-default-targets", "--json"],
        ):
            with patch("check_proxy_status.MihomoUnixClient"):
                with patch(
                    "check_proxy_status.run_check", return_value={"auto_switch": {"status": "disabled"}, "still_changed": {}}
                ) as run_check_mock:
                    with redirect_stdout(buffer):
                        code = compat_wrapper.main()

        self.assertEqual(0, code)
        self.assertEqual("sg", run_check_mock.call_args.kwargs["region"])

    def test_compat_wrapper_help_forwards_to_canonical_help(self):
        """The wrapper must delegate ``--help`` to ``check_proxy_status.main``
        so users see the same help text from either entry point."""
        buffer = io.StringIO()
        with patch.object(sys, "argv", ["check_us_proxy_status.py", "--help"]):
            with redirect_stdout(buffer):
                with self.assertRaises(SystemExit) as raised:
                    compat_wrapper.main()

        self.assertEqual(0, raised.exception.code)
        self.assertIn("--region", buffer.getvalue())
        self.assertIn("--concurrent", buffer.getvalue())

    def test_compat_wrapper_default_region_tracks_canonical_default(self):
        """The region the wrapper injects must always equal
        ``check_proxy_status.DEFAULT_REGION`` so the two cannot drift apart
        if a future change touches the canonical default."""
        self.assertEqual("us", DEFAULT_REGION)
        self.assertEqual(
            ["x.py", "--region", DEFAULT_REGION],
            compat_wrapper._inject_default_region(["x.py"]),
        )
        self.assertEqual(
            ["x.py", "--region", DEFAULT_REGION, "--json"],
            compat_wrapper._inject_default_region(["x.py", "--json"]),
        )

    def test_compat_wrapper_main_handles_empty_argv(self):
        """When invoked as a library (``main()`` directly with no argv setup),
        the wrapper must not raise ``IndexError`` on the empty list."""
        buffer = io.StringIO()
        # Stub the human-readable output to avoid needing a fully shaped result
        # dict; the test only cares that the wrapper reaches ``run_check`` with
        # the default region injected.
        with patch.object(sys, "argv", []):
            with patch("check_proxy_status.MihomoUnixClient"):
                with patch("check_proxy_status.print_human"):
                    with patch(
                        "check_proxy_status.run_check",
                        return_value={
                            "auto_switch": {"status": "disabled"},
                            "still_changed": {},
                            "targets": {},
                            "nodes": [],
                        },
                    ) as run_check_mock:
                        with redirect_stdout(buffer):
                            code = compat_wrapper.main()
                        # All sys.argv assertions must live inside the patch
                        # block because ``patch.object`` restores the original
                        # list on exit.
                        self.assertEqual(0, code)
                        self.assertEqual(
                            DEFAULT_REGION, run_check_mock.call_args.kwargs["region"]
                        )
                        self.assertGreaterEqual(len(sys.argv), 3)
                        self.assertEqual("--region", sys.argv[1])
                        self.assertEqual(DEFAULT_REGION, sys.argv[2])

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
            self.assertEqual(0, result["auto_switch"]["concurrent"])
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

    def test_auto_switch_can_use_openai_as_switch_check_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            targets = {OPENAI_TARGET_NAME: OPENAI_TARGET_URL}
            base_url = "http://example.test/204"
            client = FakeClient({
                ("🇺🇸 current", base_url): 100,
                ("🇺🇸 current", OPENAI_TARGET_URL): 900,
                ("🇺🇸 better", base_url): 120,
                ("🇺🇸 better", OPENAI_TARGET_URL): 220,
            })

            result = run_fake(
                client,
                state_file,
                targets=targets,
                switch_check_target=OPENAI_TARGET_NAME,
                bad_confirm_count=1,
            )

            self.assertEqual(OPENAI_TARGET_NAME, result["auto_switch"]["check_target"])
            self.assertEqual(OPENAI_TARGET_NAME, result["auto_switch"]["candidate_quality_target"])
            self.assertEqual(f"target:{OPENAI_TARGET_NAME}", result["current_node"]["switch_by"])
            self.assertEqual("poor", result["current_node"]["switch_level"])
            self.assertEqual(1, result["auto_switch"]["candidate_filter"]["eligible"])
            self.assertEqual("success", result["auto_switch"]["status"])
            self.assertEqual("🇺🇸 better", result["auto_switch"]["to_node"])

    def test_candidate_confirmation_can_use_openai_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            targets = {OPENAI_TARGET_NAME: OPENAI_TARGET_URL}
            base_url = "http://example.test/204"
            client = FakeClient({
                ("🇺🇸 current", base_url): 100,
                ("🇺🇸 current", OPENAI_TARGET_URL): 900,
                ("🇺🇸 better", base_url): 120,
                ("🇺🇸 better", OPENAI_TARGET_URL): [220, 230],
            })

            result = run_fake(
                client,
                state_file,
                targets=targets,
                switch_check_target=OPENAI_TARGET_NAME,
                bad_confirm_count=1,
                confirm_candidate=True,
                confirm_target=OPENAI_TARGET_NAME,
            )

            self.assertEqual(OPENAI_TARGET_NAME, result["switch_policy"]["confirm_target"])
            confirmation = result["auto_switch"]["candidate_confirmation"]
            self.assertTrue(confirmation["enabled"])
            self.assertEqual(OPENAI_TARGET_NAME, confirmation["target"])
            self.assertEqual(230, confirmation["check"]["delay_ms"])
            self.assertTrue(confirmation["passed"])
            self.assertEqual("success", result["auto_switch"]["status"])

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
