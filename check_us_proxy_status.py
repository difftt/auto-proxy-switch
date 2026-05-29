#!/usr/bin/env python3
"""
Realtime US proxy status and target API reachability checker for Clash Verge Rev / mihomo.

Properties:
- Reads the full /proxies pool, not a country/group membership list.
- Filters US nodes locally by name.
- Tests nodes via /proxies/{node}/delay.
- Tests target APIs through the same single-node delay API.
- Does NOT call /group/{group}/delay.
- Default mode scans all matched US nodes and restores unexpected strategy-group changes.
- Auto-switch mode is current-first: test current US node only; scan candidates only when current is not good.
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Protocol

GROUP_TYPES = {"Selector", "URLTest", "Fallback", "LoadBalance", "Relay"}
SPECIAL_NAMES = {"DIRECT", "REJECT", "REJECT-DROP", "PASS", "COMPATIBLE"}
DEFAULT_SOCKET = "/tmp/verge/verge-mihomo.sock"
DEFAULT_BASE_URL = "http://www.gstatic.com/generate_204"
DEFAULT_TARGETS = {"discord": "https://discord.com/api/v10/gateway"}
DEFAULT_PREFER_GROUPS = ["🤖 OpenAi", "🤖AI网站", "🔰 代理", "🚀节点选择", "GLOBAL"]
GOOD_DELAY_MS = 300
SLOW_DELAY_MS = 800


def delay_level(ok: bool, delay: int | None) -> str:
    if not ok:
        return "dead"
    if delay is None:
        return "unknown"
    if delay <= GOOD_DELAY_MS:
        return "good"
    if delay <= SLOW_DELAY_MS:
        return "slow"
    return "poor"


@dataclass
class CheckResult:
    ok: bool
    delay: int | None = None
    error: str = ""

    @property
    def level(self) -> str:
        return delay_level(self.ok, self.delay)

    def as_json(self) -> dict[str, Any]:
        return {"ok": self.ok, "delay_ms": self.delay, "level": self.level, "error": self.error}


@dataclass
class NodeResult:
    name: str
    base: CheckResult
    targets: dict[str, CheckResult] = field(default_factory=dict)


class MihomoClient(Protocol):
    def request(self, method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 10) -> tuple[str, str]: ...
    def get_json(self, path: str, timeout: float = 10) -> Any: ...
    def put_json(self, path: str, body: dict[str, Any], timeout: float = 10) -> tuple[str, str]: ...


class MihomoUnixClient:
    def __init__(self, sock_path: str):
        self.sock_path = sock_path

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        timeout: float = 10,
    ) -> tuple[str, str]:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(self.sock_path)

        data = b"" if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = f"{method} {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n"
        if data:
            req += "Content-Type: application/json\r\n"
            req += f"Content-Length: {len(data)}\r\n"
        req += "\r\n"

        sock.sendall(req.encode("utf-8") + data)
        out = b""
        while True:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            out += chunk
        sock.close()

        head, _, body_bytes = out.partition(b"\r\n\r\n")
        header_text = head.decode("utf-8", errors="replace")
        body_bytes = self._decode_chunked_if_needed(header_text, body_bytes)
        status = header_text.split("\r\n", 1)[0]
        text = body_bytes.decode("utf-8", errors="replace")
        return status, text

    @staticmethod
    def _decode_chunked_if_needed(header_text: str, body_bytes: bytes) -> bytes:
        if "Transfer-Encoding: chunked" not in header_text:
            return body_bytes
        raw = body_bytes
        chunks: list[bytes] = []
        while raw:
            line, _, rest = raw.partition(b"\r\n")
            try:
                size = int(line.split(b";", 1)[0], 16)
            except ValueError:
                break
            if size == 0:
                break
            chunks.append(rest[:size])
            raw = rest[size + 2 :]
        return b"".join(chunks)

    def get_json(self, path: str, timeout: float = 10) -> Any:
        status, text = self.request("GET", path, timeout=timeout)
        if not status.endswith("200 OK"):
            raise RuntimeError(f"{status}: {text[:300]}")
        return json.loads(text)

    def put_json(self, path: str, body: dict[str, Any], timeout: float = 10) -> tuple[str, str]:
        return self.request("PUT", path, body=body, timeout=timeout)


def enc(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def snapshot_strategy_groups(proxies: dict[str, dict[str, Any]]) -> dict[str, str]:
    return {
        name: proxy["now"]
        for name, proxy in proxies.items()
        if proxy.get("type") in GROUP_TYPES and isinstance(proxy.get("now"), str) and proxy.get("now")
    }


def is_us_real_node(name: str, proxy: dict[str, Any]) -> bool:
    if name in SPECIAL_NAMES:
        return False
    if proxy.get("type") in GROUP_TYPES:
        return False
    return bool(
        "🇺🇸" in name
        or "美国" in name
        or re.search(r"\bUS\b|\bUSA\b|United States", name, re.IGNORECASE)
    )


def validate_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"URL must include http:// or https:// scheme: {url}")


def parse_target(raw: str) -> tuple[str, str]:
    if "=" not in raw:
        raise ValueError(f"--target must use name=URL format: {raw}")
    name, url = raw.split("=", 1)
    name = name.strip()
    url = url.strip()
    if not name:
        raise ValueError(f"target name cannot be empty: {raw}")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise ValueError(f"target name should contain only letters, numbers, underscore, hyphen: {name}")
    validate_url(url)
    return name, url


def build_targets(args: argparse.Namespace) -> dict[str, str]:
    targets: dict[str, str] = {}
    if not args.no_default_targets:
        targets.update(DEFAULT_TARGETS)
    for raw in args.target or []:
        name, url = parse_target(raw)
        targets[name] = url
    return targets


def restore_changed_groups(
    client: MihomoClient,
    original_groups: dict[str, str],
    allowed_changes: dict[str, str] | None = None,
) -> list[tuple[str, str, str]]:
    allowed_changes = allowed_changes or {}
    proxies = client.get_json("/proxies")["proxies"]
    restored: list[tuple[str, str, str]] = []
    for group_name, original_now in original_groups.items():
        current_now = proxies.get(group_name, {}).get("now")
        if group_name in allowed_changes and current_now == allowed_changes[group_name]:
            continue
        if current_now and current_now != original_now:
            client.put_json(f"/proxies/{enc(group_name)}", {"name": original_now})
            after = client.get_json(f"/proxies/{enc(group_name)}").get("now")
            restored.append((group_name, str(current_now), str(after)))
    return restored


def delay_check(client: MihomoClient, node_name: str, url: str, timeout_ms: int) -> CheckResult:
    path = f"/proxies/{enc(node_name)}/delay?timeout={timeout_ms}&url={enc(url)}"
    status, text = client.request("GET", path, timeout=timeout_ms / 1000 + 3)
    try:
        data = json.loads(text)
        if "delay" in data:
            return CheckResult(ok=True, delay=int(data["delay"]))
        return CheckResult(ok=False, error=text.strip().replace("\n", " ")[:180])
    except Exception:
        return CheckResult(ok=False, error=f"{status} {text.strip()}".replace("\n", " ")[:180])


def append_restore_events(events: list[dict[str, str]], trigger: str, restored: list[tuple[str, str, str]]) -> None:
    for group_name, changed_from, restored_to in restored:
        events.append(
            {
                "after_check": trigger,
                "group": group_name,
                "changed_from": changed_from,
                "restored_to": restored_to,
            }
        )


def node_json(result: NodeResult) -> dict[str, Any]:
    return {
        "name": result.name,
        "base": result.base.as_json(),
        "targets": {name: check.as_json() for name, check in result.targets.items()},
    }


def check_node(
    client: MihomoClient,
    node: str,
    base_url: str,
    timeout_ms: int,
    targets: dict[str, str],
    target_timeout_ms: int,
    original_groups: dict[str, str],
    restore_events: list[dict[str, str]],
    allowed_changes: dict[str, str] | None = None,
) -> NodeResult:
    base = delay_check(client, node, base_url, timeout_ms)
    append_restore_events(
        restore_events,
        f"base:{node}",
        restore_changed_groups(client, original_groups, allowed_changes),
    )

    target_results: dict[str, CheckResult] = {}
    for target_name, target_url in targets.items():
        target_results[target_name] = delay_check(client, node, target_url, target_timeout_ms)
        append_restore_events(
            restore_events,
            f"target:{target_name}:{node}",
            restore_changed_groups(client, original_groups, allowed_changes),
        )
    return NodeResult(name=node, base=base, targets=target_results)


def resolve_group_to_us_node(
    group_name: str,
    proxies: dict[str, dict[str, Any]],
    us_nodes: set[str],
    max_depth: int = 5,
) -> str | None:
    seen: set[str] = set()
    current = group_name
    for _ in range(max_depth):
        if current in seen:
            return None
        seen.add(current)
        now = proxies.get(current, {}).get("now")
        if not isinstance(now, str) or not now:
            return None
        if now in us_nodes:
            return now
        if proxies.get(now, {}).get("type") in GROUP_TYPES:
            current = now
            continue
        return None
    return None


def detect_current_node(
    proxies: dict[str, dict[str, Any]],
    original_groups: dict[str, str],
    us_nodes: list[str],
    prefer_groups: list[str] | None,
) -> dict[str, Any]:
    us_set = set(us_nodes)
    direct: list[tuple[str, str]] = []
    resolved: list[tuple[str, str]] = []
    for group, now in original_groups.items():
        if now in us_set:
            direct.append((group, now))
        elif proxies.get(now, {}).get("type") in GROUP_TYPES:
            node = resolve_group_to_us_node(group, proxies, us_set)
            if node:
                resolved.append((group, node))

    candidates = direct + resolved
    prefer_groups = prefer_groups or DEFAULT_PREFER_GROUPS
    for preferred in prefer_groups:
        for group, node in candidates:
            if group == preferred:
                return {"detected": True, "group": group, "name": node, "via": "direct" if (group, node) in direct else "resolved"}
    if candidates:
        group, node = candidates[0]
        return {"detected": True, "group": group, "name": node, "via": "direct" if (group, node) in direct else "resolved"}
    return {"detected": False, "group": None, "name": None, "via": None, "reason": "no_strategy_group_points_to_us_node"}


def current_switch_check(result: NodeResult, switch_check_target: str | None) -> tuple[CheckResult | None, str]:
    if switch_check_target:
        check = result.targets.get(switch_check_target)
        if check is None:
            return None, f"target:{switch_check_target}:missing"
        return check, f"target:{switch_check_target}"
    return result.base, "base"


def current_needs_switch(result: NodeResult, switch_check_target: str | None) -> tuple[bool, str, str]:
    check, source = current_switch_check(result, switch_check_target)
    if check is None:
        return True, source, "missing"
    return check.level != "good", source, check.level


def choose_switch_target(
    node_results: list[NodeResult],
    current_node: str,
    current_result: NodeResult,
    switch_group: str,
    proxies: dict[str, dict[str, Any]],
    switch_check_target: str | None,
    targets: dict[str, str],
) -> NodeResult | None:
    allowed = set(proxies.get(switch_group, {}).get("all") or [])
    sort_target = switch_check_target or ("discord" if "discord" in targets else None)
    current_sort_delay = current_result.targets[sort_target].delay if sort_target and sort_target in current_result.targets else current_result.base.delay
    candidates: list[NodeResult] = []
    for result in node_results:
        if result.name == current_node:
            continue
        if result.name not in allowed:
            continue
        if not result.base.ok:
            continue
        if sort_target:
            target_check = result.targets.get(sort_target)
            if target_check is None or not target_check.ok:
                continue
            if current_sort_delay is not None and (target_check.delay is None or target_check.delay >= current_sort_delay):
                continue
        elif current_sort_delay is not None and (result.base.delay is None or result.base.delay >= current_sort_delay):
            continue
        candidates.append(result)
    if not candidates:
        return None

    def key(result: NodeResult) -> int:
        if sort_target:
            delay = result.targets[sort_target].delay
        else:
            delay = result.base.delay
        return delay if delay is not None else 10**9

    return sorted(candidates, key=key)[0]


def summarize_results(node_results: list[NodeResult], targets: dict[str, str]) -> tuple[list[NodeResult], list[NodeResult], dict[str, list[NodeResult]], dict[str, list[NodeResult]]]:
    base_alive = sorted([r for r in node_results if r.base.ok], key=lambda r: r.base.delay if r.base.delay is not None else 10**9)
    base_dead = [r for r in node_results if not r.base.ok]
    target_alive: dict[str, list[NodeResult]] = {}
    target_dead: dict[str, list[NodeResult]] = {}
    for target_name in targets:
        def target_sort_key(result: NodeResult, target: str = target_name) -> int:
            delay = result.targets[target].delay
            return delay if delay is not None else 10**9

        target_alive[target_name] = sorted(
            [r for r in node_results if r.targets.get(target_name) and r.targets[target_name].ok],
            key=target_sort_key,
        )
        target_dead[target_name] = [r for r in node_results if not (r.targets.get(target_name) and r.targets[target_name].ok)]
    return base_alive, base_dead, target_alive, target_dead


def final_changed(
    client: MihomoClient,
    original_groups: dict[str, str],
    allowed_changes: dict[str, str] | None = None,
) -> dict[str, dict[str, str | None]]:
    allowed_changes = allowed_changes or {}
    final_proxies = client.get_json("/proxies")["proxies"]
    final_groups = snapshot_strategy_groups(final_proxies)
    return {
        group: {"original": original_now, "final": final_groups.get(group)}
        for group, original_now in original_groups.items()
        if final_groups.get(group) != original_now and not (group in allowed_changes and final_groups.get(group) == allowed_changes[group])
    }


def run_check(
    client: MihomoClient,
    base_url: str,
    timeout_ms: int,
    targets: dict[str, str],
    target_timeout_ms: int,
    auto_switch_if_current_not_good: bool = False,
    switch_check_target: str | None = None,
    prefer_groups: list[str] | None = None,
) -> dict[str, Any]:
    proxies = client.get_json("/proxies")["proxies"]
    original_groups = snapshot_strategy_groups(proxies)
    us_nodes = [name for name, proxy in proxies.items() if is_us_real_node(name, proxy)]
    restore_events: list[dict[str, str]] = []
    allowed_changes: dict[str, str] = {}
    node_results: list[NodeResult] = []

    if switch_check_target and switch_check_target not in targets:
        raise ValueError(f"--switch-check-target not present in targets: {switch_check_target}")

    current_node_info: dict[str, Any] = {"detected": False, "group": None, "name": None, "via": None}
    auto_switch: dict[str, Any] = {
        "enabled": auto_switch_if_current_not_good,
        "mode": "current-first" if auto_switch_if_current_not_good else "full-scan",
        "candidate_scan_started": False,
        "candidate_scan_reason": None,
        "triggered": False,
        "reason": "disabled" if not auto_switch_if_current_not_good else None,
        "check_target": switch_check_target,
        "from_group": None,
        "from_node": None,
        "to_node": None,
        "status": "disabled" if not auto_switch_if_current_not_good else "pending",
    }

    if auto_switch_if_current_not_good:
        current_node_info = detect_current_node(proxies, original_groups, us_nodes, prefer_groups)
        if not current_node_info.get("detected"):
            auto_switch.update({"reason": current_node_info.get("reason", "current_node_not_detected"), "status": "skipped"})
            append_restore_events(restore_events, "FINAL", restore_changed_groups(client, original_groups))
        else:
            current_name = str(current_node_info["name"])
            current_group = str(current_node_info["group"])
            current_result = check_node(
                client,
                current_name,
                base_url,
                timeout_ms,
                targets,
                target_timeout_ms,
                original_groups,
                restore_events,
            )
            node_results.append(current_result)
            needs_switch, switch_by, switch_level = current_needs_switch(current_result, switch_check_target)
            current_node_info.update({
                "base": current_result.base.as_json(),
                "targets": {name: check.as_json() for name, check in current_result.targets.items()},
                "dead": not current_result.base.ok,
                "dead_by": "base" if not current_result.base.ok else None,
                "needs_switch": needs_switch,
                "switch_by": switch_by,
                "switch_level": switch_level,
            })
            auto_switch.update({"from_group": current_group, "from_node": current_name})
            if not needs_switch:
                auto_switch.update({
                    "candidate_scan_started": False,
                    "candidate_scan_reason": "current_node_good",
                    "triggered": False,
                    "reason": "current_node_good",
                    "status": "not_needed",
                })
                append_restore_events(restore_events, "FINAL", restore_changed_groups(client, original_groups))
            else:
                auto_switch.update({"candidate_scan_started": True, "candidate_scan_reason": f"current_node_{switch_level}"})
                allowed_for_switch_group = set(proxies.get(current_group, {}).get("all") or [])
                candidate_nodes = [
                    node
                    for node in us_nodes
                    if node != current_name and node in allowed_for_switch_group
                ]
                for node in candidate_nodes:
                    node_results.append(
                        check_node(
                            client,
                            node,
                            base_url,
                            timeout_ms,
                            targets,
                            target_timeout_ms,
                            original_groups,
                            restore_events,
                        )
                    )
                best = choose_switch_target(node_results, current_name, current_result, current_group, proxies, switch_check_target, targets)
                if best is None:
                    auto_switch.update({"triggered": False, "reason": "no_available_candidate", "status": "skipped"})
                    append_restore_events(restore_events, "FINAL", restore_changed_groups(client, original_groups))
                else:
                    client.put_json(f"/proxies/{enc(current_group)}", {"name": best.name})
                    after = client.get_json(f"/proxies/{enc(current_group)}").get("now")
                    if after == best.name:
                        allowed_changes[current_group] = best.name
                        auto_switch.update({
                            "triggered": True,
                            "reason": f"current_node_{switch_level}_by_{switch_by}",
                            "to_node": best.name,
                            "status": "success",
                        })
                    else:
                        auto_switch.update({
                            "triggered": True,
                            "reason": f"current_node_{switch_level}_by_{switch_by}",
                            "to_node": best.name,
                            "status": "failed",
                            "error": f"switch verification failed: now={after}",
                        })
                    append_restore_events(restore_events, "FINAL", restore_changed_groups(client, original_groups, allowed_changes))
    else:
        for node in us_nodes:
            node_results.append(
                check_node(client, node, base_url, timeout_ms, targets, target_timeout_ms, original_groups, restore_events)
            )
        append_restore_events(restore_events, "FINAL", restore_changed_groups(client, original_groups))

    base_alive, base_dead, target_alive, target_dead = summarize_results(node_results, targets)
    still_changed = final_changed(client, original_groups, allowed_changes)
    allowed_changes_list = [
        {"group": group, "original": original_groups.get(group), "final": final}
        for group, final in allowed_changes.items()
    ]

    if still_changed:
        guarantee = "restore_failed"
    elif auto_switch.get("status") == "success":
        guarantee = "changed_as_requested"
    else:
        guarantee = "unchanged"

    return {
        "source": "/proxies full pool",
        "filter": "name contains 🇺🇸 / 美国 / US / USA / United States; excludes strategy groups and special nodes",
        "base_url": base_url,
        "test_url": base_url,
        "targets": targets,
        "timeout_ms": timeout_ms,
        "target_timeout_ms": target_timeout_ms,
        "strategy_groups_protected": len(original_groups),
        "us_nodes_count": len(us_nodes),
        "nodes": [node_json(r) for r in node_results],
        "base_alive": [{"name": r.name, "delay_ms": r.base.delay, "level": r.base.level} for r in base_alive],
        "base_dead": [{"name": r.name, "level": r.base.level, "error": r.base.error} for r in base_dead],
        "alive": [{"name": r.name, "delay_ms": r.base.delay, "level": r.base.level} for r in base_alive],
        "dead": [{"name": r.name, "level": r.base.level, "error": r.base.error} for r in base_dead],
        "target_alive": {
            name: [{"name": r.name, "delay_ms": r.targets[name].delay, "level": r.targets[name].level} for r in rows]
            for name, rows in target_alive.items()
        },
        "target_dead": {
            name: [{"name": r.name, "level": r.targets[name].level, "error": r.targets[name].error} for r in rows]
            for name, rows in target_dead.items()
        },
        "current_node": current_node_info,
        "auto_switch": auto_switch,
        "restore_events": restore_events,
        "allowed_changes": allowed_changes_list,
        "still_changed": still_changed,
        "guarantee": guarantee,
    }


def parse_prefer_groups(raw: str | None) -> list[str]:
    if not raw:
        return DEFAULT_PREFER_GROUPS
    return [item.strip() for item in raw.split(",") if item.strip()]


def print_human(result: dict[str, Any]) -> None:
    targets: dict[str, str] = result["targets"]
    print("过滤来源: /proxies 全量代理池")
    print("过滤规则: 名称包含 🇺🇸 / 美国 / US / USA / United States；排除策略组与 DIRECT/REJECT")
    print(f"基础测试 URL: {result['base_url']}")
    print(f"基础超时: {result['timeout_ms']}ms")
    print(f"目标 API 超时: {result['target_timeout_ms']}ms")
    print("目标 API:")
    if targets:
        for name, url in targets.items():
            print(f"- {name}: {url}")
    else:
        print("- 无")
    print(f"捕获需保护的策略组数量: {result['strategy_groups_protected']}")
    print(f"美国节点数量: {result['us_nodes_count']}")
    print(f"自动切换模式: {'开启' if result['auto_switch']['enabled'] else '关闭'}")
    if result["auto_switch"]["enabled"]:
        cn = result["current_node"]
        print(f"当前节点识别: {'成功' if cn.get('detected') else '失败'}")
        if cn.get("detected"):
            print(f"当前策略组: {cn.get('group')}")
            print(f"当前节点: {cn.get('name')}")
            print(f"当前节点基础状态: {'dead' if cn.get('dead') else 'alive'} ({cn.get('dead_by')})")
            print(f"当前节点需要切换: {cn.get('needs_switch')} ({cn.get('switch_by')}={cn.get('switch_level')})")
        print(f"是否扫描候选节点: {result['auto_switch'].get('candidate_scan_started')}")
        print(f"自动切换结果: {result['auto_switch'].get('status')} / {result['auto_switch'].get('reason')}")
        if result["auto_switch"].get("to_node"):
            print(f"切换目标: {result['auto_switch']['to_node']}")
    print()

    print("美国节点实时状态，按基础延迟排序:")
    ordered_nodes = sorted(
        result["nodes"],
        key=lambda r: (not r["base"]["ok"], r["base"]["delay_ms"] if r["base"]["delay_ms"] is not None else 10**9),
    )
    for i, row in enumerate(ordered_nodes, 1):
        base = row["base"]
        base_text = f"base={base['delay_ms']}ms/{base['level']}" if base["ok"] else f"base={base['level']}({base['error']})"
        parts = [base_text]
        for target_name in targets:
            check = row["targets"].get(target_name, {"ok": False, "error": "not tested", "delay_ms": None, "level": "dead"})
            parts.append(f"{target_name}={check['delay_ms']}ms/{check['level']}" if check["ok"] else f"{target_name}={check['level']}({check['error']})")
        mark = " <- 当前" if row["name"] == result.get("current_node", {}).get("name") else ""
        print(f"{i:02d}. {row['name']} [{'; '.join(parts)}]{mark}")

    print()
    print("目标 API 可达摘要:")
    if targets:
        for target_name in targets:
            rows = result["target_alive"].get(target_name, [])
            print(f"- {target_name}: 可达 {len(rows)}/{len(result['nodes'])}")
            for i, row in enumerate(rows[:10], 1):
                print(f"  {i:02d}. {row['name']} [{row['delay_ms']}ms/{row['level']}]")
            if len(rows) > 10:
                print(f"  ... 还有 {len(rows) - 10} 个可达节点未显示")
    else:
        print("- 未配置目标 API")

    print()
    print("切换保护:")
    print(f"测速期间恢复事件数: {len(result['restore_events'])}")
    for event in result["restore_events"][:20]:
        print("-", event["after_check"], "| group:", event["group"], "| changed:", event["changed_from"], "=> restored:", event["restored_to"])
    if len(result["restore_events"]) > 20:
        print(f"... 还有 {len(result['restore_events']) - 20} 条恢复事件未显示")

    if result["still_changed"]:
        print("最终策略组状态: 仍有非预期变化")
        print(json.dumps(result["still_changed"], ensure_ascii=False, indent=2))
    elif result["guarantee"] == "changed_as_requested":
        print("最终策略组状态: 已按请求自动切换，其它策略组保持原样")
    else:
        print("最终策略组状态: 全部保持原样")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Realtime US proxy status and target API checker without using group membership or accidental switching."
    )
    parser.add_argument("--socket", default=DEFAULT_SOCKET, help=f"mihomo unix socket path; default: {DEFAULT_SOCKET}")
    parser.add_argument("--url", default=DEFAULT_BASE_URL, help=f"base delay-test URL; default: {DEFAULT_BASE_URL}")
    parser.add_argument("--timeout", type=int, default=5000, help="base delay-test timeout in ms; default: 5000")
    parser.add_argument("--target", action="append", help="target API to test, format name=URL; repeatable")
    parser.add_argument("--target-timeout", type=int, default=8000, help="target API timeout in ms; default: 8000")
    parser.add_argument("--no-default-targets", action="store_true", help="do not include built-in default target APIs")
    parser.add_argument("--auto-switch-if-current-not-good", action="store_true", help="current-first mode: switch to best US node when current check level is not good")
    parser.add_argument("--switch-check-target", help="target name used to decide current quality and choose best node, e.g. discord")
    parser.add_argument("--prefer-groups", help="comma-separated strategy group priority for current-node detection")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of human-readable text")
    args = parser.parse_args()

    try:
        validate_url(args.url)
        targets = build_targets(args)
        if args.switch_check_target and args.switch_check_target not in targets:
            raise ValueError(f"--switch-check-target not present in targets: {args.switch_check_target}")
        client = MihomoUnixClient(args.socket)
        result = run_check(
            client=client,
            base_url=args.url,
            timeout_ms=args.timeout,
            targets=targets,
            target_timeout_ms=args.target_timeout,
            auto_switch_if_current_not_good=args.auto_switch_if_current_not_good,
            switch_check_target=args.switch_check_target,
            prefer_groups=parse_prefer_groups(args.prefer_groups),
        )
    except Exception as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2))
        else:
            print(f"执行失败: {exc}")
        return 1

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print_human(result)

    if result["auto_switch"].get("status") == "failed":
        return 3
    if result["still_changed"]:
        return 2
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        raise SystemExit(1)
