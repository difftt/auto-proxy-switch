#!/usr/bin/env python3
"""
Realtime region proxy status and target API reachability checker for Clash Verge Rev / mihomo.

Properties:
- Reads the full /proxies pool, not a country/group membership list.
- Filters region nodes locally by name (default region: US).
- Tests nodes via /proxies/{node}/delay.
- Tests target APIs through the same single-node delay API.
- Does NOT call /group/{group}/delay.
- Default mode scans all matched region nodes and restores unexpected strategy-group changes.
- Auto-switch mode is current-first: test current region node only; scan candidates only when policy allows it.
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import http.client
import json
import os
import re
import socket
import time
import urllib.parse
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Protocol

GROUP_TYPES = {"Selector", "URLTest", "Fallback", "LoadBalance", "Relay"}
SPECIAL_NAMES = {"DIRECT", "REJECT", "REJECT-DROP", "PASS", "COMPATIBLE"}
REGION_KEYWORDS = MappingProxyType({
    "us": ("🇺🇸", "美国", "US", "USA", "United States"),
    "sg": ("新加坡", "SG", "Singapore"),
    "uk": ("英国", "UK", "United Kingdom", "England"),
    "jp": ("日本", "JP", "Japan"),
    "hk": ("香港", "HK", "Hong Kong"),
    "de": ("德国", "DE", "Germany"),
    "fr": ("法国", "FR", "France"),
})
DEFAULT_SOCKET = "/tmp/verge/verge-mihomo.sock"
DEFAULT_BASE_URL = "http://www.gstatic.com/generate_204"
DEFAULT_DISCORD_TARGET_URL = "https://discord.com/api/v10/gateway"
OPENAI_TARGET_NAME = "openai"
OPENAI_TARGET_URL = "https://api.openai.com/v1/models"
DEFAULT_TARGETS = {"discord": DEFAULT_DISCORD_TARGET_URL}
TARGET_ALIASES = MappingProxyType({OPENAI_TARGET_NAME: OPENAI_TARGET_URL})
DEFAULT_PREFER_GROUPS = ["🤖 OpenAi", "🤖AI网站", "🔰 代理", "🚀节点选择", "GLOBAL"]
GOOD_DELAY_MS = 300
SLOW_DELAY_MS = 800
DEFAULT_STATE_FILE = "logs/auto_switch_state.json"
DEFAULT_BAD_THRESHOLD = "poor"
DEFAULT_BAD_CONFIRM_COUNT = 2
DEFAULT_SLOW_SWITCH_THRESHOLD_MS = 600
DEFAULT_SLOW_CONFIRM_COUNT = 5
DEFAULT_SWITCH_COOLDOWN_SECONDS = 600
DEFAULT_BREAK_COOLDOWN_DEAD_COUNT = 3
DEFAULT_MIN_IMPROVEMENT_MS = 100
DEFAULT_AVOID_RECENT_SWITCHES = 3
DEFAULT_AVOID_RECENT_WINDOW_SECONDS = 1800
DEFAULT_CONCURRENT = 16
MIN_CONCURRENT = 1
MAX_CONCURRENT = 32


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


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, sock_path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self.sock_path = sock_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self.sock_path)
        except Exception:
            sock.close()
            raise
        self.sock = sock


class MihomoUnixClient:
    def __init__(self, sock_path: str):
        self.sock_path = sock_path
        self._conn: _UnixHTTPConnection | None = None

    def _connection(self, timeout: float) -> _UnixHTTPConnection:
        if self._conn is None:
            self._conn = _UnixHTTPConnection(self.sock_path, timeout)
        self._conn.timeout = timeout
        if self._conn.sock is not None:
            self._conn.sock.settimeout(timeout)
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        timeout: float = 10,
    ) -> tuple[str, str]:
        data = b"" if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Host": "localhost", "Connection": "keep-alive"}
        if data:
            headers["Content-Type"] = "application/json"

        conn = self._connection(timeout)
        try:
            conn.request(method, path, body=data if data else None, headers=headers)
            response = conn.getresponse()
            body_bytes = response.read()
        except Exception:
            self.close()
            raise

        if response.will_close:
            self.close()

        status = f"HTTP/{response.version // 10}.{response.version % 10} {response.status} {response.reason}"
        text = body_bytes.decode("utf-8", errors="replace")
        return status, text

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


def normalize_region(value: str | None) -> str:
    region = "us" if value is None else value.lower()
    if region not in REGION_KEYWORDS:
        raise ValueError(f"--region: invalid value '{value}'")
    return region


def _keyword_matches(name: str, keyword: str) -> bool:
    if keyword.isascii() and keyword.isalpha() and len(keyword) <= 3:
        return bool(re.search(rf"\b{re.escape(keyword)}\b", name, re.IGNORECASE))
    return bool(re.search(re.escape(keyword), name, re.IGNORECASE))


def is_region_real_node(name: str, proxy: dict[str, Any], region: str = "us") -> bool:
    region = normalize_region(region)
    if name in SPECIAL_NAMES:
        return False
    if proxy.get("type") in GROUP_TYPES:
        return False
    return any(_keyword_matches(name, keyword) for keyword in REGION_KEYWORDS[region])


def is_us_real_node(name: str, proxy: dict[str, Any]) -> bool:
    return is_region_real_node(name, proxy, "us")


def validate_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"URL must include http:// or https:// scheme: {url}")


def parse_target(raw: str) -> tuple[str, str]:
    if "=" not in raw:
        name = raw.strip().lower()
        if name in TARGET_ALIASES:
            return name, TARGET_ALIASES[name]
        aliases = ", ".join(TARGET_ALIASES)
        raise ValueError(f"--target must use name=URL format or known alias ({aliases}): {raw}")
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


def parse_concurrent(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"--concurrent must be an integer in [{MIN_CONCURRENT}, {MAX_CONCURRENT}]") from exc
    if value < MIN_CONCURRENT or value > MAX_CONCURRENT:
        raise argparse.ArgumentTypeError(f"--concurrent must be in [{MIN_CONCURRENT}, {MAX_CONCURRENT}]")
    return value


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

    target_results: dict[str, CheckResult] = {}
    for target_name, target_url in targets.items():
        target_results[target_name] = delay_check(client, node, target_url, target_timeout_ms)
    append_restore_events(
        restore_events,
        f"node:{node}",
        restore_changed_groups(client, original_groups, allowed_changes),
    )
    return NodeResult(name=node, base=base, targets=target_results)


def check_node_with_local_restore_events(
    client: MihomoClient,
    node: str,
    base_url: str,
    timeout_ms: int,
    targets: dict[str, str],
    target_timeout_ms: int,
    original_groups: dict[str, str],
    allowed_changes: dict[str, str] | None = None,
) -> tuple[NodeResult, list[dict[str, str]]]:
    local_restore_events: list[dict[str, str]] = []
    result = check_node(
        client,
        node,
        base_url,
        timeout_ms,
        targets,
        target_timeout_ms,
        original_groups,
        local_restore_events,
        allowed_changes,
    )
    return result, local_restore_events


def close_client_if_supported(client: MihomoClient) -> None:
    close = getattr(client, "close", None)
    if callable(close):
        close()


def region_filter_description(region: str) -> str:
    region = normalize_region(region)
    return (
        f"name contains {' / '.join(REGION_KEYWORDS[region])}; "
        "excludes strategy groups and special nodes"
    )


def resolve_group_to_region_node(
    group_name: str,
    proxies: dict[str, dict[str, Any]],
    region_nodes: set[str],
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
        if now in region_nodes:
            return now
        if proxies.get(now, {}).get("type") in GROUP_TYPES:
            current = now
            continue
        return None
    return None


def resolve_group_current_node(
    group_name: str,
    proxies: dict[str, dict[str, Any]],
    max_depth: int = 5,
) -> tuple[str | None, str | None]:
    seen: set[str] = set()
    current = group_name
    via = "direct"
    for _ in range(max_depth):
        if current in seen:
            return None, None
        seen.add(current)
        now = proxies.get(current, {}).get("now")
        if not isinstance(now, str) or not now:
            return None, None
        if proxies.get(now, {}).get("type") in GROUP_TYPES:
            current = now
            via = "resolved"
            continue
        return now, via
    return None, None


def detect_current_node(
    proxies: dict[str, dict[str, Any]],
    original_groups: dict[str, str],
    region_nodes: list[str],
    prefer_groups: list[str] | None,
    region: str,
) -> dict[str, Any]:
    region = normalize_region(region)
    region_set = set(region_nodes)
    direct: list[tuple[str, str]] = []
    resolved: list[tuple[str, str]] = []
    mismatched: list[tuple[str, str, str]] = []
    for group, now in original_groups.items():
        if now in region_set:
            direct.append((group, now))
        elif proxies.get(now, {}).get("type") in GROUP_TYPES:
            node = resolve_group_to_region_node(group, proxies, region_set)
            if node:
                resolved.append((group, node))
            else:
                raw_node, via = resolve_group_current_node(group, proxies)
                if raw_node:
                    mismatched.append((group, raw_node, via or "resolved"))
        else:
            raw_node, via = resolve_group_current_node(group, proxies)
            if raw_node:
                mismatched.append((group, raw_node, via or "direct"))

    candidates = direct + resolved
    prefer_groups = prefer_groups or DEFAULT_PREFER_GROUPS
    for preferred in prefer_groups:
        for group, node in candidates:
            if group == preferred:
                return {"detected": True, "group": group, "name": node, "via": "direct" if (group, node) in direct else "resolved"}
    if candidates:
        group, node = candidates[0]
        return {"detected": True, "group": group, "name": node, "via": "direct" if (group, node) in direct else "resolved"}
    for preferred in prefer_groups:
        for group, node, via in mismatched:
            if group == preferred:
                return {
                    "detected": False,
                    "group": group,
                    "name": node,
                    "via": via,
                    "reason": "current_node_region_mismatch",
                    "current_raw_node": node,
                    "expected_region": region,
                }
    if mismatched:
        group, node, via = mismatched[0]
        return {
            "detected": False,
            "group": group,
            "name": node,
            "via": via,
            "reason": "current_node_region_mismatch",
            "current_raw_node": node,
            "expected_region": region,
        }
    return {
        "detected": False,
        "group": None,
        "name": None,
        "via": None,
        "reason": "no_strategy_group_points_to_region_node",
        "expected_region": region,
    }


def resolve_group_to_us_node(
    group_name: str,
    proxies: dict[str, dict[str, Any]],
    us_nodes: set[str],
    max_depth: int = 5,
) -> str | None:
    return resolve_group_to_region_node(group_name, proxies, us_nodes, max_depth)


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


def load_switch_state(path: str | None) -> tuple[dict[str, Any], str | None]:
    if not path:
        return {}, None
    if not os.path.exists(path):
        return {}, None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        return {}, str(exc)
    if not isinstance(data, dict):
        return {}, "state root is not a JSON object"
    return data, None


def save_switch_state(path: str | None, state: dict[str, Any]) -> str | None:
    if not path:
        return None
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    tmp_path = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp_path, path)
        return None
    except Exception as exc:
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass
        return str(exc)


def _positive(value: int) -> int:
    return max(1, int(value))


def normalize_bad_threshold(value: str) -> str:
    value = value.strip().lower()
    if value != "poor":
        raise ValueError(f"--bad-threshold currently supports only 'poor': {value}")
    return value


def decide_switch_policy(
    state: dict[str, Any],
    *,
    now: float,
    current_group: str | None,
    current_node: str | None,
    switch_by: str,
    switch_level: str,
    check: CheckResult | None,
    state_load_error: str | None,
    bad_threshold: str,
    bad_confirm_count: int,
    slow_switch_threshold_ms: int,
    slow_confirm_count: int,
    switch_cooldown_seconds: int,
    break_cooldown_dead_count: int,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    bad_confirm_count = _positive(bad_confirm_count)
    slow_confirm_count = _positive(slow_confirm_count)
    break_cooldown_dead_count = _positive(break_cooldown_dead_count)
    switch_cooldown_seconds = max(0, int(switch_cooldown_seconds))
    current_key = f"{current_group or ''}\n{current_node or ''}\n{switch_by}"
    previous = state.get("current") if isinstance(state.get("current"), dict) else {}
    same_current = previous.get("key") == current_key
    bad_count = int(previous.get("bad_count", 0)) if same_current else 0
    slow_count = int(previous.get("slow_count", 0)) if same_current else 0
    dead_count = int(previous.get("dead_count", 0)) if same_current else 0

    delay = check.delay if check is not None else None
    is_dead = switch_level == "dead"
    is_good = switch_level == "good"
    is_slow = switch_level == "slow"
    is_bad = switch_level in {"poor", "dead", "missing", "unknown"}
    slow_over_threshold = is_slow and delay is not None and delay > slow_switch_threshold_ms

    reason = "current_node_good"
    required_count = 1
    observed_count = 0
    eligible_after_confirm = False
    state_blocked = bool(state_load_error)

    if is_good:
        bad_count = 0
        slow_count = 0
        dead_count = 0
        reason = "current_node_good"
    elif is_slow and not slow_over_threshold:
        bad_count = 0
        slow_count = 0
        dead_count = 0
        reason = "current_node_slow_acceptable"
    elif slow_over_threshold:
        slow_count += 1
        bad_count = 0
        dead_count = 0
        required_count = slow_confirm_count
        observed_count = slow_count
        eligible_after_confirm = slow_count >= slow_confirm_count
        reason = "slow_confirmed" if eligible_after_confirm else "slow_wait_confirm"
    elif is_bad:
        bad_count += 1
        dead_count = dead_count + 1 if is_dead else 0
        slow_count = 0
        required_count = bad_confirm_count
        observed_count = bad_count
        eligible_after_confirm = bad_count >= bad_confirm_count
        reason = "bad_confirmed" if eligible_after_confirm else "bad_wait_confirm"
    else:
        bad_count = 0
        slow_count = 0
        dead_count = 0
        reason = "current_node_acceptable"

    last_switch = state.get("last_switch") if isinstance(state.get("last_switch"), dict) else {}
    last_switch_at = last_switch.get("at")
    try:
        last_switch_at = float(last_switch_at)
    except (TypeError, ValueError):
        last_switch_at = None
    cooldown_remaining = 0
    in_cooldown = False
    if last_switch_at is not None and switch_cooldown_seconds > 0:
        elapsed = max(0, now - last_switch_at)
        cooldown_remaining = max(0, int(switch_cooldown_seconds - elapsed))
        in_cooldown = cooldown_remaining > 0
    cooldown_break_allowed = in_cooldown and dead_count >= break_cooldown_dead_count

    should_scan = eligible_after_confirm and not state_blocked
    if should_scan and in_cooldown and not cooldown_break_allowed:
        should_scan = False
        reason = "switch_cooldown_active"
    elif state_blocked:
        should_scan = False
        reason = "state_load_error"

    new_state = dict(state)
    new_state["current"] = {
        "key": current_key,
        "group": current_group,
        "node": current_node,
        "switch_by": switch_by,
        "level": switch_level,
        "delay_ms": delay,
        "bad_count": bad_count,
        "slow_count": slow_count,
        "dead_count": dead_count,
        "updated_at": now,
    }
    policy = {
        "state_load_error": state_load_error,
        "bad_threshold": bad_threshold,
        "bad_confirm_count": bad_confirm_count,
        "slow_switch_threshold_ms": slow_switch_threshold_ms,
        "slow_confirm_count": slow_confirm_count,
        "switch_cooldown_seconds": switch_cooldown_seconds,
        "break_cooldown_dead_count": break_cooldown_dead_count,
        "bad_count": bad_count,
        "slow_count": slow_count,
        "dead_count": dead_count,
        "required_count": required_count,
        "observed_count": observed_count,
        "in_cooldown": in_cooldown,
        "cooldown_remaining_seconds": cooldown_remaining,
        "cooldown_break_allowed": cooldown_break_allowed,
    }
    decision = {
        "should_scan_candidates": should_scan,
        "reason": reason,
        "level": switch_level,
        "switch_by": switch_by,
        "delay_ms": delay,
    }
    return policy, decision, new_state


def choose_quality_target(switch_check_target: str | None, targets: dict[str, str], confirm_target: str | None = None) -> str:
    if confirm_target is not None:
        if not confirm_target.strip():
            raise ValueError("--confirm-target cannot be empty")
        return confirm_target
    if switch_check_target:
        return switch_check_target
    if "discord" in targets:
        return "discord"
    return "base"


def result_quality_check(result: NodeResult, quality_target: str) -> CheckResult | None:
    if quality_target == "base":
        return result.base
    return result.targets.get(quality_target)


def level_rank(level: str) -> int | None:
    ranks = {"poor": 1, "slow": 2, "good": 3}
    return ranks.get(level)


def comparable_delay(check: CheckResult | None) -> int | None:
    if check is None or check.level in {"dead", "unknown", "missing"}:
        return None
    return check.delay


def candidate_is_improved(
    current_check: CheckResult | None,
    candidate_check: CheckResult | None,
    min_improvement_ms: int,
) -> bool:
    current_delay = comparable_delay(current_check)
    current_rank = level_rank(current_check.level) if current_check is not None else None
    candidate_delay = candidate_check.delay if candidate_check is not None else None
    candidate_rank = level_rank(candidate_check.level) if candidate_check is not None else None
    min_improvement_ms = max(0, int(min_improvement_ms))
    improved_by_level = current_rank is not None and candidate_rank is not None and candidate_rank > current_rank
    improved_by_delay = (
        current_delay is not None
        and candidate_delay is not None
        and current_delay - candidate_delay >= min_improvement_ms
    )
    current_not_comparable = current_rank is None or current_delay is None
    return current_not_comparable or improved_by_level or improved_by_delay


def recent_switch_nodes(state: dict[str, Any], now: float, max_switches: int, window_seconds: int) -> set[str]:
    max_switches = max(0, int(max_switches))
    window_seconds = max(0, int(window_seconds))
    if max_switches == 0 or window_seconds == 0:
        return set()

    events: list[dict[str, Any]] = []
    last_switch = state.get("last_switch") if isinstance(state.get("last_switch"), dict) else None
    if last_switch:
        events.append(last_switch)
    recent_switches = state.get("recent_switches")
    if isinstance(recent_switches, list):
        events.extend(event for event in recent_switches if isinstance(event, dict))

    unique_events: dict[tuple[Any, Any, Any], dict[str, Any]] = {}
    for event in events:
        key = (event.get("at"), event.get("from_node"), event.get("to_node"))
        unique_events[key] = event

    filtered_events: list[tuple[float, dict[str, Any]]] = []
    for event in unique_events.values():
        try:
            switched_at = float(event.get("at"))
        except (TypeError, ValueError):
            continue
        if now - switched_at <= window_seconds:
            filtered_events.append((switched_at, event))
    filtered_events.sort(key=lambda item: item[0], reverse=True)

    nodes: set[str] = set()
    for _, event in filtered_events[:max_switches]:
        for key in ("from_node", "to_node"):
            node = event.get(key)
            if isinstance(node, str) and node:
                nodes.add(node)
    return nodes


def remember_recent_switch(state: dict[str, Any], switch_event: dict[str, Any]) -> None:
    recent_switches = state.get("recent_switches")
    if not isinstance(recent_switches, list):
        recent_switches = []
    recent_switches.append(dict(switch_event))
    state["recent_switches"] = recent_switches[-50:]


def choose_switch_target(
    node_results: list[NodeResult],
    current_node: str,
    current_result: NodeResult,
    switch_group: str,
    proxies: dict[str, dict[str, Any]],
    switch_check_target: str | None,
    targets: dict[str, str],
    min_improvement_ms: int,
    quality_target: str,
    recent_nodes: set[str] | None = None,
) -> tuple[NodeResult | None, dict[str, Any]]:
    allowed = set(proxies.get(switch_group, {}).get("all") or [])
    recent_nodes = recent_nodes or set()
    current_check = result_quality_check(current_result, quality_target)
    min_improvement_ms = max(0, int(min_improvement_ms))
    candidates: list[NodeResult] = []
    stats: dict[str, Any] = {
        "quality_target": quality_target,
        "min_improvement_ms": min_improvement_ms,
        "scanned": 0,
        "filtered_current": 0,
        "filtered_not_allowed": 0,
        "filtered_recent": 0,
        "filtered_base_unavailable": 0,
        "filtered_target_unavailable": 0,
        "filtered_not_improved": 0,
        "eligible": 0,
    }
    for result in node_results:
        if result.name == current_node:
            stats["filtered_current"] += 1
            continue
        if result.name not in allowed:
            stats["filtered_not_allowed"] += 1
            continue
        stats["scanned"] += 1
        if result.name in recent_nodes:
            stats["filtered_recent"] += 1
            continue
        if not result.base.ok:
            stats["filtered_base_unavailable"] += 1
            continue
        target_check = result_quality_check(result, quality_target)
        if target_check is None or not target_check.ok:
            stats["filtered_target_unavailable"] += 1
            continue
        if not candidate_is_improved(current_check, target_check, min_improvement_ms):
            stats["filtered_not_improved"] += 1
            continue
        candidates.append(result)
    if not candidates:
        return None, stats

    stats["eligible"] = len(candidates)

    def key(result: NodeResult) -> int:
        check = result_quality_check(result, quality_target)
        delay = check.delay if check is not None else None
        return delay if delay is not None else 10**9

    return sorted(candidates, key=key)[0], stats


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
    state_file: str | None = DEFAULT_STATE_FILE,
    bad_threshold: str = DEFAULT_BAD_THRESHOLD,
    bad_confirm_count: int = DEFAULT_BAD_CONFIRM_COUNT,
    slow_switch_threshold_ms: int = DEFAULT_SLOW_SWITCH_THRESHOLD_MS,
    slow_confirm_count: int = DEFAULT_SLOW_CONFIRM_COUNT,
    switch_cooldown_seconds: int = DEFAULT_SWITCH_COOLDOWN_SECONDS,
    break_cooldown_dead_count: int = DEFAULT_BREAK_COOLDOWN_DEAD_COUNT,
    min_improvement_ms: int = DEFAULT_MIN_IMPROVEMENT_MS,
    confirm_candidate: bool = False,
    confirm_target: str | None = None,
    avoid_recent_switches: int = DEFAULT_AVOID_RECENT_SWITCHES,
    avoid_recent_window_seconds: int = DEFAULT_AVOID_RECENT_WINDOW_SECONDS,
    concurrent: int = DEFAULT_CONCURRENT,
    client_factory: Callable[[], MihomoClient] | None = None,
    now: float | None = None,
    region: str = "us",
) -> dict[str, Any]:
    now = time.time() if now is None else now
    region = normalize_region(region)
    bad_threshold = normalize_bad_threshold(bad_threshold)
    if concurrent < MIN_CONCURRENT or concurrent > MAX_CONCURRENT:
        raise ValueError(f"--concurrent must be in [{MIN_CONCURRENT}, {MAX_CONCURRENT}]")
    proxies = client.get_json("/proxies")["proxies"]
    original_groups = snapshot_strategy_groups(proxies)
    region_nodes = [name for name, proxy in proxies.items() if is_region_real_node(name, proxy, region)]
    restore_events: list[dict[str, str]] = []
    allowed_changes: dict[str, str] = {}
    node_results: list[NodeResult] = []

    quality_target = choose_quality_target(switch_check_target, targets, confirm_target)
    if switch_check_target and switch_check_target not in targets:
        raise ValueError(f"--switch-check-target not present in targets: {switch_check_target}")
    if quality_target != "base" and quality_target not in targets:
        raise ValueError(f"--confirm-target not present in targets: {quality_target}")

    current_node_info: dict[str, Any] = {"detected": False, "group": None, "name": None, "via": None}
    auto_switch: dict[str, Any] = {
        "enabled": auto_switch_if_current_not_good,
        "mode": "current-first" if auto_switch_if_current_not_good else "full-scan",
        "candidate_scan_started": False,
        "candidate_scan_reason": None,
        "triggered": False,
        "reason": "disabled" if not auto_switch_if_current_not_good else None,
        "check_target": switch_check_target,
        "candidate_quality_target": quality_target,
        "from_group": None,
        "from_node": None,
        "to_node": None,
        "candidate_filter": None,
        "candidate_confirmation": None,
        "status": "disabled" if not auto_switch_if_current_not_good else "pending",
    }
    switch_policy: dict[str, Any] = {
        "enabled": auto_switch_if_current_not_good,
        "state_file": state_file,
        "bad_threshold": bad_threshold,
        "bad_confirm_count": _positive(bad_confirm_count),
        "slow_switch_threshold_ms": slow_switch_threshold_ms,
        "slow_confirm_count": _positive(slow_confirm_count),
        "switch_cooldown_seconds": max(0, switch_cooldown_seconds),
        "break_cooldown_dead_count": _positive(break_cooldown_dead_count),
        "min_improvement_ms": max(0, int(min_improvement_ms)),
        "confirm_candidate": bool(confirm_candidate),
        "confirm_target": quality_target,
        "avoid_recent_switches": max(0, int(avoid_recent_switches)),
        "avoid_recent_window_seconds": max(0, int(avoid_recent_window_seconds)),
    }
    switch_decision: dict[str, Any] = {
        "should_scan_candidates": False,
        "reason": "disabled" if not auto_switch_if_current_not_good else "pending",
    }

    if auto_switch_if_current_not_good:
        auto_switch["concurrent"] = 0
        switch_state, state_load_error = load_switch_state(state_file)
        switch_policy["state_load_error"] = state_load_error
        current_node_info = detect_current_node(proxies, original_groups, region_nodes, prefer_groups, region)
        if not current_node_info.get("detected"):
            policy, decision, next_state = decide_switch_policy(
                switch_state,
                now=now,
                current_group=None,
                current_node=None,
                switch_by="current_node:missing",
                switch_level="missing",
                check=None,
                state_load_error=state_load_error,
                bad_threshold=bad_threshold,
                bad_confirm_count=bad_confirm_count,
                slow_switch_threshold_ms=slow_switch_threshold_ms,
                slow_confirm_count=slow_confirm_count,
                switch_cooldown_seconds=switch_cooldown_seconds,
                break_cooldown_dead_count=break_cooldown_dead_count,
            )
            switch_policy.update(policy)
            switch_decision.update(decision)
            if current_node_info.get("reason") == "current_node_region_mismatch":
                switch_decision.update({
                    "should_scan_candidates": False,
                    "reason": "current_node_region_mismatch",
                })
            state_save_error = save_switch_state(state_file, next_state)
            if state_save_error:
                switch_policy["state_save_error"] = state_save_error
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
            switch_check, _ = current_switch_check(current_result, switch_check_target)
            current_quality_check = result_quality_check(current_result, quality_target)
            policy, decision, next_state = decide_switch_policy(
                switch_state,
                now=now,
                current_group=current_group,
                current_node=current_name,
                switch_by=switch_by,
                switch_level=switch_level,
                check=switch_check,
                state_load_error=state_load_error,
                bad_threshold=bad_threshold,
                bad_confirm_count=bad_confirm_count,
                slow_switch_threshold_ms=slow_switch_threshold_ms,
                slow_confirm_count=slow_confirm_count,
                switch_cooldown_seconds=switch_cooldown_seconds,
                break_cooldown_dead_count=break_cooldown_dead_count,
            )
            switch_policy.update(policy)
            switch_decision.update(decision)
            switch_decision["current_needs_switch"] = needs_switch
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
            if not decision["should_scan_candidates"]:
                state_save_error = save_switch_state(state_file, next_state)
                if state_save_error:
                    switch_policy["state_save_error"] = state_save_error
                auto_switch.update({
                    "candidate_scan_started": False,
                    "candidate_scan_reason": decision["reason"],
                    "triggered": False,
                    "reason": decision["reason"],
                    "status": "not_needed",
                })
                append_restore_events(restore_events, "FINAL", restore_changed_groups(client, original_groups))
            else:
                auto_switch.update({"candidate_scan_started": True, "candidate_scan_reason": decision["reason"]})
                recent_nodes = recent_switch_nodes(
                    switch_state,
                    now,
                    avoid_recent_switches,
                    avoid_recent_window_seconds,
                )
                allowed_for_switch_group = set(proxies.get(current_group, {}).get("all") or [])
                candidate_nodes = [
                    node
                    for node in region_nodes
                    if node != current_name and node in allowed_for_switch_group
                ]
                if client_factory is None and isinstance(client, MihomoUnixClient):
                    client_factory = lambda: MihomoUnixClient(client.sock_path)
                can_run_parallel_candidates = client_factory is not None
                candidate_workers = min(concurrent, 8, len(candidate_nodes))
                if not candidate_nodes:
                    auto_switch["concurrent"] = 0
                elif candidate_workers <= 1 or not can_run_parallel_candidates:
                    auto_switch["concurrent"] = 1
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
                else:
                    auto_switch["concurrent"] = candidate_workers

                    def check_candidate_node(node: str) -> tuple[NodeResult, list[dict[str, str]]]:
                        assert client_factory is not None
                        worker_client = client_factory()
                        try:
                            return check_node_with_local_restore_events(
                                worker_client,
                                node,
                                base_url,
                                timeout_ms,
                                targets,
                                target_timeout_ms,
                                original_groups,
                                allowed_changes,
                            )
                        finally:
                            close_client_if_supported(worker_client)

                    with futures.ThreadPoolExecutor(max_workers=candidate_workers) as executor:
                        for result, local_restore_events in executor.map(check_candidate_node, candidate_nodes):
                            node_results.append(result)
                            restore_events.extend(local_restore_events)
                best, candidate_filter = choose_switch_target(
                    node_results,
                    current_name,
                    current_result,
                    current_group,
                    proxies,
                    switch_check_target,
                    targets,
                    min_improvement_ms,
                    quality_target,
                    recent_nodes,
                )
                candidate_filter["recent_nodes"] = sorted(recent_nodes)
                auto_switch["candidate_filter"] = candidate_filter
                switch_decision["candidate_filter"] = candidate_filter
                if best is None:
                    state_save_error = save_switch_state(state_file, next_state)
                    if state_save_error:
                        switch_policy["state_save_error"] = state_save_error
                    auto_switch.update({"triggered": False, "reason": "no_available_candidate", "status": "skipped"})
                    append_restore_events(restore_events, "FINAL", restore_changed_groups(client, original_groups))
                else:
                    if confirm_candidate:
                        confirmation = check_node(
                            client,
                            best.name,
                            base_url,
                            timeout_ms,
                            {quality_target: targets[quality_target]} if quality_target != "base" else {},
                            target_timeout_ms,
                            original_groups,
                            restore_events,
                        )
                        confirmed_check = result_quality_check(confirmation, quality_target)
                        confirmation_json = {
                            "enabled": True,
                            "target": quality_target,
                            "node": best.name,
                            "base": confirmation.base.as_json(),
                            "check": confirmed_check.as_json() if confirmed_check is not None else None,
                            "passed": bool(confirmation.base.ok and confirmed_check is not None and confirmed_check.ok),
                        }
                        if confirmation_json["passed"] and not candidate_is_improved(
                            current_quality_check,
                            confirmed_check,
                            min_improvement_ms,
                        ):
                            confirmation_json["passed"] = False
                            confirmation_json["reason"] = "candidate_confirmation_not_improved"
                        auto_switch["candidate_confirmation"] = confirmation_json
                        switch_decision["candidate_confirmation"] = confirmation_json
                        if not confirmation_json["passed"]:
                            state_save_error = save_switch_state(state_file, next_state)
                            if state_save_error:
                                switch_policy["state_save_error"] = state_save_error
                            reason = confirmation_json.get("reason", "candidate_confirmation_failed")
                            auto_switch.update({
                                "triggered": False,
                                "reason": reason,
                                "status": "skipped",
                            })
                            append_restore_events(restore_events, "FINAL", restore_changed_groups(client, original_groups))
                            best = None
                    else:
                        confirmation_json = {"enabled": False, "target": quality_target, "node": best.name, "passed": None}
                        auto_switch["candidate_confirmation"] = confirmation_json
                        switch_decision["candidate_confirmation"] = confirmation_json
                    if best is not None:
                        client.put_json(f"/proxies/{enc(current_group)}", {"name": best.name})
                        after = client.get_json(f"/proxies/{enc(current_group)}").get("now")
                        if after == best.name:
                            allowed_changes[current_group] = best.name
                            switch_event = {
                                "at": now,
                                "from_group": current_group,
                                "from_node": current_name,
                                "to_node": best.name,
                                "reason": decision["reason"],
                            }
                            next_state["last_switch"] = switch_event
                            remember_recent_switch(next_state, switch_event)
                            next_state["current"] = {
                                "key": f"{current_group}\n{best.name}\n{switch_by}",
                                "group": current_group,
                                "node": best.name,
                                "switch_by": switch_by,
                                "level": "switched",
                                "delay_ms": None,
                                "bad_count": 0,
                                "slow_count": 0,
                                "dead_count": 0,
                                "updated_at": now,
                            }
                            state_save_error = save_switch_state(state_file, next_state)
                            if state_save_error:
                                switch_policy["state_save_error"] = state_save_error
                            auto_switch.update({
                                "triggered": True,
                                "reason": decision["reason"],
                                "to_node": best.name,
                                "status": "success",
                            })
                        else:
                            state_save_error = save_switch_state(state_file, next_state)
                            if state_save_error:
                                switch_policy["state_save_error"] = state_save_error
                            auto_switch.update({
                                "triggered": True,
                                "reason": decision["reason"],
                                "to_node": best.name,
                                "status": "failed",
                                "error": f"switch verification failed: now={after}",
                            })
                        append_restore_events(restore_events, "FINAL", restore_changed_groups(client, original_groups, allowed_changes))
    else:
        if client_factory is None and isinstance(client, MihomoUnixClient):
            client_factory = lambda: MihomoUnixClient(client.sock_path)
        can_run_parallel = client_factory is not None

        if concurrent == 1 or len(region_nodes) <= 1 or not can_run_parallel:
            for node in region_nodes:
                node_results.append(
                    check_node(client, node, base_url, timeout_ms, targets, target_timeout_ms, original_groups, restore_events)
                )
        else:
            def check_default_node(node: str) -> tuple[NodeResult, list[dict[str, str]]]:
                assert client_factory is not None
                worker_client = client_factory()
                try:
                    return check_node_with_local_restore_events(
                        worker_client,
                        node,
                        base_url,
                        timeout_ms,
                        targets,
                        target_timeout_ms,
                        original_groups,
                        allowed_changes,
                    )
                finally:
                    close_client_if_supported(worker_client)

            with futures.ThreadPoolExecutor(max_workers=min(concurrent, len(region_nodes))) as executor:
                for result, local_restore_events in executor.map(check_default_node, region_nodes):
                    node_results.append(result)
                    restore_events.extend(local_restore_events)
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
        "region": region,
        "filter": region_filter_description(region),
        "base_url": base_url,
        "test_url": base_url,
        "targets": targets,
        "timeout_ms": timeout_ms,
        "target_timeout_ms": target_timeout_ms,
        "strategy_groups_protected": len(original_groups),
        "region_nodes_count": len(region_nodes),
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
        "switch_policy": switch_policy,
        "switch_decision": switch_decision,
        "restore_events": restore_events,
        "allowed_changes": allowed_changes_list,
        "still_changed": still_changed,
        "guarantee": guarantee,
    }


def parse_prefer_groups(raw: str | None) -> list[str]:
    if not raw:
        return DEFAULT_PREFER_GROUPS
    return [item.strip() for item in raw.split(",") if item.strip()]


def _format_counter(value: Any, total: Any) -> str:
    return f"{value}/{total}"


def _print_candidate_filter(stats: dict[str, Any] | None) -> None:
    if not stats:
        print("候选过滤: 未扫描")
        return
    print(
        "候选过滤: "
        f"扫描 {stats.get('scanned', 0)}，"
        f"可用 {stats.get('eligible', 0)}，"
        f"当前节点 {stats.get('filtered_current', 0)}，"
        f"不在策略组 {stats.get('filtered_not_allowed', 0)}，"
        f"近期切换 {stats.get('filtered_recent', 0)}，"
        f"基础不可用 {stats.get('filtered_base_unavailable', 0)}，"
        f"目标不可用 {stats.get('filtered_target_unavailable', 0)}，"
        f"改善不足 {stats.get('filtered_not_improved', 0)}"
    )


def print_human(result: dict[str, Any]) -> None:
    targets: dict[str, str] = result["targets"]
    print("过滤来源: /proxies 全量代理池")
    print(f"地区: {result.get('region', 'us')}")
    print(f"过滤规则: {result['filter']}")
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
    print(f"地区节点数量: {result['region_nodes_count']}")
    print(f"自动切换模式: {'开启' if result['auto_switch']['enabled'] else '关闭'}")
    if result["auto_switch"]["enabled"]:
        cn = result["current_node"]
        policy = result.get("switch_policy", {})
        decision = result.get("switch_decision", {})
        print(f"当前节点识别: {'成功' if cn.get('detected') else '失败'}")
        if cn.get("detected"):
            print(f"当前策略组: {cn.get('group')}")
            print(f"当前节点: {cn.get('name')}")
            print(f"当前节点基础状态: {'dead' if cn.get('dead') else 'alive'} ({cn.get('dead_by')})")
            print(f"当前节点需要切换: {cn.get('needs_switch')} ({cn.get('switch_by')}={cn.get('switch_level')})")
        elif cn.get("reason") == "current_node_region_mismatch":
            print(f"当前策略组: {cn.get('group')}")
            print(f"当前原始节点: {cn.get('current_raw_node') or cn.get('name')}")
            print(f"期望地区: {cn.get('expected_region')}")
        print(f"切换判断目标: {result['auto_switch'].get('candidate_quality_target')}")
        print(f"决策原因: {decision.get('reason')}")
        print(f"计数器 bad: {_format_counter(policy.get('bad_count', 0), policy.get('bad_confirm_count', 0))}")
        print(f"计数器 slow: {_format_counter(policy.get('slow_count', 0), policy.get('slow_confirm_count', 0))}")
        print(f"计数器 dead: {_format_counter(policy.get('dead_count', 0), policy.get('break_cooldown_dead_count', 0))}")
        print(
            "冷却状态: "
            f"{'生效' if policy.get('in_cooldown') else '未生效'}，"
            f"剩余 {policy.get('cooldown_remaining_seconds', 0)} 秒，"
            f"允许打破冷却: {policy.get('cooldown_break_allowed')}"
        )
        print(f"是否扫描候选节点: {result['auto_switch'].get('candidate_scan_started')}")
        _print_candidate_filter(result["auto_switch"].get("candidate_filter"))
        confirmation = result["auto_switch"].get("candidate_confirmation")
        if confirmation:
            print(
                "候选复测: "
                f"{'开启' if confirmation.get('enabled') else '关闭'}，"
                f"目标 {confirmation.get('target')}，"
                f"结果 {confirmation.get('passed')}"
            )
        print(f"自动切换结果: {result['auto_switch'].get('status')} / {result['auto_switch'].get('reason')}")
        if result["auto_switch"].get("to_node"):
            print(f"切换目标: {result['auto_switch']['to_node']}")
    print()

    print("地区节点实时状态，按基础延迟排序:")
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
    # Contract: ``check_us_proxy_status.py`` is a thin wrapper that injects
    # ``--region us`` when the caller omits ``--region`` and then calls this
    # ``main()``. Do not change the default-region contract here without
    # updating the wrapper in lockstep.
    parser = argparse.ArgumentParser(
        description="Realtime regional proxy status and target API checker without using group membership or accidental switching."
    )
    parser.add_argument("--socket", default=DEFAULT_SOCKET, help=f"mihomo unix socket path; default: {DEFAULT_SOCKET}")
    parser.add_argument(
        "--region",
        default="us",
        help="region to filter by: us, sg, uk, jp, hk, de, fr; case-insensitive; default: us",
    )
    parser.add_argument("--url", default=DEFAULT_BASE_URL, help=f"base delay-test URL; default: {DEFAULT_BASE_URL}")
    parser.add_argument("--timeout", type=int, default=5000, help="base delay-test timeout in ms; default: 5000")
    parser.add_argument(
        "--target",
        action="append",
        help=f"target API to test, format name=URL or alias such as {OPENAI_TARGET_NAME}; repeatable",
    )
    parser.add_argument("--target-timeout", type=int, default=8000, help="target API timeout in ms; default: 8000")
    parser.add_argument("--no-default-targets", action="store_true", help="do not include built-in default target APIs")
    parser.add_argument(
        "--auto-switch-if-current-not-good",
        action="store_true",
        help="current-first mode: scan and switch only after policy confirmation and cooldown checks allow it",
    )
    parser.add_argument("--switch-check-target", help="target name used to decide current quality and choose best node, e.g. discord or openai")
    parser.add_argument("--state-file", default=DEFAULT_STATE_FILE, help=f"auto-switch state file; default: {DEFAULT_STATE_FILE}")
    parser.add_argument("--bad-threshold", default=DEFAULT_BAD_THRESHOLD, choices=["poor"], help=f"level treated as bad for confirmation; default: {DEFAULT_BAD_THRESHOLD}")
    parser.add_argument("--bad-confirm-count", type=int, default=DEFAULT_BAD_CONFIRM_COUNT, help=f"consecutive poor/dead/unknown checks before scanning candidates; default: {DEFAULT_BAD_CONFIRM_COUNT}")
    parser.add_argument("--slow-switch-threshold-ms", type=int, default=DEFAULT_SLOW_SWITCH_THRESHOLD_MS, help=f"slow delay in ms that may trigger switching after confirmation; default: {DEFAULT_SLOW_SWITCH_THRESHOLD_MS}")
    parser.add_argument("--slow-confirm-count", type=int, default=DEFAULT_SLOW_CONFIRM_COUNT, help=f"consecutive slow checks above threshold before scanning candidates; default: {DEFAULT_SLOW_CONFIRM_COUNT}")
    parser.add_argument("--switch-cooldown-seconds", type=int, default=DEFAULT_SWITCH_COOLDOWN_SECONDS, help=f"minimum seconds between successful switches; default: {DEFAULT_SWITCH_COOLDOWN_SECONDS}")
    parser.add_argument("--break-cooldown-dead-count", type=int, default=DEFAULT_BREAK_COOLDOWN_DEAD_COUNT, help=f"consecutive dead checks allowed to break switch cooldown; default: {DEFAULT_BREAK_COOLDOWN_DEAD_COUNT}")
    parser.add_argument("--min-improvement-ms", type=int, default=DEFAULT_MIN_IMPROVEMENT_MS, help=f"minimum delay improvement required for same-level candidates; default: {DEFAULT_MIN_IMPROVEMENT_MS}")
    parser.add_argument("--confirm-candidate", action="store_true", help="re-test the selected candidate before switching")
    parser.add_argument("--confirm-target", help="target name used for candidate quality and confirmation; default: switch target, discord, then base")
    parser.add_argument("--avoid-recent-switches", type=int, default=DEFAULT_AVOID_RECENT_SWITCHES, help=f"number of recent switch events whose nodes are filtered; default: {DEFAULT_AVOID_RECENT_SWITCHES}")
    parser.add_argument("--avoid-recent-window-seconds", type=int, default=DEFAULT_AVOID_RECENT_WINDOW_SECONDS, help=f"recent switch filter window in seconds; default: {DEFAULT_AVOID_RECENT_WINDOW_SECONDS}")
    parser.add_argument("--concurrent", type=parse_concurrent, default=DEFAULT_CONCURRENT, help=f"node scan concurrency in [1, 32]; default: {DEFAULT_CONCURRENT}")
    parser.add_argument("--prefer-groups", help="comma-separated strategy group priority for current-node detection")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of human-readable text")
    args = parser.parse_args()

    try:
        region = normalize_region(args.region)
        validate_url(args.url)
        targets = build_targets(args)
        if args.switch_check_target and args.switch_check_target not in targets:
            raise ValueError(f"--switch-check-target not present in targets: {args.switch_check_target}")
        confirm_target = choose_quality_target(args.switch_check_target, targets, args.confirm_target)
        if confirm_target != "base" and confirm_target not in targets:
            raise ValueError(f"--confirm-target not present in targets: {confirm_target}")
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
            state_file=args.state_file,
            bad_threshold=args.bad_threshold,
            bad_confirm_count=args.bad_confirm_count,
            slow_switch_threshold_ms=args.slow_switch_threshold_ms,
            slow_confirm_count=args.slow_confirm_count,
            switch_cooldown_seconds=args.switch_cooldown_seconds,
            break_cooldown_dead_count=args.break_cooldown_dead_count,
            min_improvement_ms=args.min_improvement_ms,
            confirm_candidate=args.confirm_candidate,
            confirm_target=args.confirm_target,
            avoid_recent_switches=args.avoid_recent_switches,
            avoid_recent_window_seconds=args.avoid_recent_window_seconds,
            concurrent=args.concurrent,
            client_factory=lambda: MihomoUnixClient(args.socket),
            region=region,
        )
    except Exception as exc:
        message = str(exc)
        if args.json:
            print(json.dumps({"error": message}, ensure_ascii=False, indent=2))
        elif message.startswith("--region: invalid value "):
            print(message)
        else:
            print(f"执行失败: {message}")
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
