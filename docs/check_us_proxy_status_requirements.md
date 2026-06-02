# 美国代理节点实时状态与目标 API 可达性检查脚本需求说明

文件：`check_us_proxy_status.py`

## 背景

当前代理软件为 Clash Verge Rev / mihomo，控制 API 通过 Unix Socket 暴露：

```text
/tmp/verge/verge-mihomo.sock
```

系统代理端口 `127.0.0.1:7890` 是流量代理端口，不是控制 API 端口。

本需求用于实时获取美国节点的基础可用性、延迟状态，以及这些节点访问目标网络/API 的可达性。例如：Discord API。

默认模式只检测状态，不主动切换当前节点。

新增可选模式：当检测到当前正在使用的美国节点达到策略确认条件且未被冷却阻止时，允许脚本自动切换到更优的可用美国节点。

## 目标

实现一个脚本，用于：

1. 从 mihomo 的完整代理池读取所有代理对象。
2. 在本地过滤出美国节点。
3. 对过滤出的美国真实节点逐个实时测速。
4. 输出每个美国节点的基础实时可用状态和延迟。
5. 输出每个美国节点访问一个或多个目标 API 的实时可达性和延迟。
6. 默认保证测速和目标 API 检测过程不会切换任何策略组当前选中的节点。
7. 在显式启用自动切换模式时，优先只检测当前正在使用的美国节点。
8. 在自动切换模式下，只有当前节点达到策略确认条件且未被冷却阻止时，才开始检测其它美国候选节点并尝试切换到更优可用节点。
9. 自动切换只在策略确认允许时触发，当前节点为 `good` 时不得因为存在更快节点而切换。

## 非目标

脚本不负责：

1. 在默认检测模式下自动切换到最快节点。
2. 修改用户的代理配置文件。
3. 调用代理组级别测速接口。
4. 依赖「🇺🇸美国节点」这个 group 的成员列表来判断美国节点。
5. 刷新全部 provider 的健康检查。
6. 代替完整业务协议测试，例如 Discord Gateway WebSocket 鉴权、Bot 登录、消息发送等。
7. 在当前节点仍可用时做负载均衡、最优延迟抢占或频繁漂移切换。

## 数据来源

脚本必须使用：

```http
GET /proxies
```

从完整代理池读取所有代理对象。

脚本不得使用以下接口作为美国节点来源：

```http
GET /proxies/🇺🇸美国节点
GET /group/🇺🇸美国节点/delay
```

原因：

- `/proxies/🇺🇸美国节点` 依赖 group 成员列表，不满足“从整个代理池过滤”的需求。
- `/group/{group}/delay` 对 URLTest 组可能触发自动重选节点。

## 美国节点过滤规则

从 `/proxies` 返回的完整代理池中，本地过滤节点名。

节点名称包含以下任一特征时，视为美国节点候选：

```text
🇺🇸
美国
US
USA
United States
```

同时必须排除策略组类型：

```text
Selector
URLTest
Fallback
LoadBalance
Relay
```

同时必须排除特殊节点：

```text
DIRECT
REJECT
REJECT-DROP
PASS
COMPATIBLE
```

最终只对真实代理节点测速。

## 地区选择规则

脚本应支持通过 `--region` 选择检测地区，默认值为 `us`，大小写不敏感。默认 `us` 必须保持上述美国节点过滤语义兼容。

当前内置地区值：

```text
us
sg
uk
jp
hk
de
fr
```

传入非内置地区时，脚本必须在执行任何检测前退出，退出码为 `1`。人类可读输出的错误信息必须精确为：

```text
--region: invalid value '{value}'
```

JSON 输出可使用结构化错误字段，但错误值必须为同一字符串。

启用非 `us` 地区后，本文中“美国节点”的检测范围、当前节点识别和自动切换候选范围均按指定地区解释；策略组与特殊节点排除规则保持不变。

## 基础实时状态获取方式

对每个过滤出的美国节点单独调用：

```http
GET /proxies/{节点名}/delay?timeout=5000&url=http://www.gstatic.com/generate_204
```

成功返回示例：

```json
{"delay": 183}
```

失败返回示例：

```json
{"message":"An error occurred in the delay test"}
```

或：

```json
{"message":"Timeout"}
```

状态定义：

- 成功返回 `delay`：节点对该 URL 实时可用。
- 返回错误或超时：节点对该 URL 实时不可用/超时。

延迟等级定义：

```text
good    delay <= 300ms
slow    301ms <= delay <= 800ms
poor    delay > 800ms
dead    检测失败
unknown 检测成功但没有可用 delay 值
```

## 目标 API 可达性检测

脚本必须支持在基础实时状态之外，对目标网络/API 做实时可达性检测。

检测方式仍然使用单节点 delay API：

```http
GET /proxies/{节点名}/delay?timeout=8000&url={目标 API URL}
```

示例：检测 Discord API：

```http
GET /proxies/{节点名}/delay?timeout=8000&url=https://discord.com/api/v10/gateway
```

成功返回示例：

```json
{"delay": 210}
```

目标 API 状态定义：

- 成功返回 `delay`：该节点能实时访问目标 API，记录目标 API 延迟。
- 返回错误或超时：该节点无法在指定超时时间内访问目标 API。

### Discord 推荐检测目标

首选：

```text
https://discord.com/api/v10/gateway
```

原因：

- Discord 官方公开 API。
- 不需要 Authorization token。
- 适合检测 Discord API 的 HTTP/TLS 可达性。

可选辅助目标：

```text
https://discord.com
https://cdn.discordapp.com
https://gateway.discord.gg
```

注意：

- `https://gateway.discord.gg` 只能作为 HTTP/TLS 可达性近似检测。
- 完整 Gateway WebSocket 可用性不属于本脚本目标。
- `https://discord.com/api/v10/users/@me` 需要 token，不建议作为默认检测目标。

## 多目标检测要求

脚本应支持多个目标 API。

命令行形式：

```text
--target 名称=URL
```

可重复传入，例如：

```bash
./check_us_proxy_status.py \
  --target discord=https://discord.com/api/v10/gateway \
  --target discord_cdn=https://cdn.discordapp.com
```

脚本默认应内置一个目标：

```text
discord=https://discord.com/api/v10/gateway
```

如果用户只需要基础状态，可以通过参数关闭默认目标：

```text
--no-default-targets
```

目标名称要求：

- 非空。
- 用于输出字段名。
- 建议只包含字母、数字、下划线、短横线。

目标 URL 要求：

- 必须显式包含 `http://` 或 `https://`。
- 不接受无 scheme URL。

## 禁止使用的测速方式

不得调用：

```http
GET /group/{组名}/delay
```

原因：

当前部分订阅里的美国节点策略组可能是 `URLTest`。对 URLTest 组执行 group delay 可能触发 mihomo 重新评估并自动改变该组的 `now`。

## 当前节点识别规则

为了支持“当前节点达到策略确认条件时自动切换”，脚本需要识别当前正在使用的美国节点。

识别方式：

1. 测试前调用 `/proxies`。
2. 记录所有策略组的当前选择 `now`。
3. 如果某个策略组的 `now` 直接等于一个真实美国节点名称，则该节点视为当前美国节点候选。
4. 如果多个策略组直接选择了不同美国节点，脚本应优先选择业务相关策略组，优先级通过参数指定。
5. 默认优先级建议：

```text
🤖 OpenAi
🤖AI网站
🔰 代理
🚀节点选择
GLOBAL
```

6. 如果优先级列表均未命中，则选择第一个直接指向真实美国节点的策略组。
7. 如果没有任何策略组直接指向真实美国节点，但策略组链路为 `策略组 -> 策略组 -> 美国节点`，脚本可以做有限递归解析。
8. 递归解析必须有循环保护，最大深度建议为 5。
9. 如果仍无法识别当前美国节点，则自动切换模式不得执行切换，只输出原因。

当前节点切换判断：

- 默认使用基础状态的延迟等级判断当前节点质量。
- 如果启用了 `--switch-check-target 目标名`，则以指定目标 API 的延迟等级判断当前节点质量，例如 `discord.level=slow` 进入 slow 判断逻辑，但仍需满足阈值、连续确认和冷却条件。
- 只有判断目标的等级为 `good` 时，当前节点才明确不需要扫描候选，并应重置连续异常计数。
- 判断目标为可接受的 `slow` 时，当前节点不得进入候选扫描。
- 判断目标为超阈值 `slow` 时，需要先达到 `--slow-confirm-count`，再按冷却策略决定是否进入候选扫描。
- 判断目标为 `poor`、`dead`、`unknown` 或 `missing` 时，需要先达到 `--bad-confirm-count`，再按冷却策略决定是否进入候选扫描。
- 冷却期内默认不得扫描候选；只有连续真实 `dead` 达到 `--break-cooldown-dead-count` 时，才允许提前打破冷却。
- 如果同时要求基础状态和目标 API 状态都可用，可以支持 `--switch-require base,target:discord` 形式；默认只使用基础状态判断。

## 自动切换最优节点需求

自动切换必须是显式启用功能，不得默认开启。

建议参数：

```text
--auto-switch-if-current-not-good
```

触发流程：

1. 用户显式传入 `--auto-switch-if-current-not-good`。
2. 脚本先识别当前正在使用的美国节点及其所属可切换策略组。
3. 脚本只对当前节点执行基础状态和必要目标 API 检测。
4. 如果当前节点等级为 `good`，脚本立即结束自动切换流程，不检测其它候选节点，不切换。
5. 当前节点未达到 `good` 时，脚本仍必须先经过 slow 阈值、连续确认和冷却策略判断；只有策略允许时才开始检测其它美国候选节点。
6. 如果存在满足条件且相对当前节点明显更好的候选节点，则切换到最优可用节点。

触发条件：

1. 用户显式传入 `--auto-switch-if-current-not-good`。
2. 成功识别当前美国节点及其所属可切换策略组。
3. 当前节点检测等级达到 slow 或 bad 的连续确认条件，并且未被冷却策略阻止。
4. 存在至少一个候选美国节点满足最优节点筛选条件。

不得触发切换的情况：

1. 未传入 `--auto-switch-if-current-not-good`。
2. 当前节点等级为 `good`，即使有更快节点也不切换，且不得检测其它候选节点。
3. 无法识别当前美国节点或无法识别可切换策略组。
4. 所有候选美国节点均不可用。
5. 最优候选节点不是该策略组可选项之一。
6. PUT 切换后校验失败。

## 最优节点选择规则

默认最优节点选择规则：

1. 候选范围：从 `/proxies` 全量代理池过滤出的真实美国节点。
2. 候选节点必须在目标可切换策略组的 `all` 列表中，避免切换到该组不可选节点。
3. 候选节点必须 `base.ok=true`。
4. 如果配置了目标 API，默认要求默认目标 `discord` 也可用；如果用户关闭默认目标或未配置目标，则只看基础状态。
5. 排序优先级：

```text
目标 API 延迟优先，如果存在目标 API 且要求目标 API 可用；
否则基础延迟优先。
```

6. 如果多个目标 API 被要求，优先使用 `--switch-check-target` 指定的目标。
7. 如果没有指定 `--switch-check-target`，且存在 `discord` 目标，则默认使用 `discord` 作为切换排序目标。
8. 如果没有目标 API 可用于排序，则使用基础延迟。
9. 当前节点不应作为切换目标。
10. 当前节点仍有可比较延迟时，候选节点必须比当前节点用于排序的延迟更低。

建议支持参数：

```text
--switch-check-target discord
```

含义：

- 判断当前节点质量时使用该目标状态。
- 选择最优节点时优先按该目标延迟排序。
- 该目标不可用的候选节点不得作为切换目标。

## 自动切换执行方式

切换必须通过 mihomo API 修改策略组当前选择：

```http
PUT /proxies/{策略组名}
Content-Type: application/json

{"name":"最优节点名"}
```

执行后必须立即校验：

```http
GET /proxies/{策略组名}
```

校验条件：

```text
now == 最优节点名
```

如果校验失败：

- 输出错误。
- JSON 中 `switch_result.status` 应为 `failed`。
- 退出码应为非零。

## 防意外切换保护机制

默认检测模式下，脚本必须保证不会改变用户当前代理选择。

保护流程：

1. 测试前调用 `/proxies`。
2. 记录所有策略组的当前选择：

```text
策略组名 -> now
```

策略组类型包括：

```text
Selector
URLTest
Fallback
LoadBalance
Relay
```

3. 对每个美国真实节点执行基础状态测速。
4. 对每个美国真实节点执行所有目标 API 可达性检测。
5. 每个节点的每个检测动作完成后，重新读取 `/proxies`。
6. 对比所有策略组的 `now` 是否发生变化。
7. 如果发现非预期变化，立即调用：

```http
PUT /proxies/{策略组名}
Content-Type: application/json

{"name":"原来的 now"}
```

8. 所有检测完成后，再做一次最终校验。
9. 如果仍有策略组状态与初始值不一致，脚本应返回非零退出码。

启用 `--auto-switch-if-current-not-good` 时的差异：

- 自动切换模式采用懒检测：先只检测当前节点。
- 当前节点等级为 `good` 时，不检测其它美国节点，不做任何切换。
- 当前节点未达到 `good` 时，仍必须先经过 slow 阈值、连续确认和冷却判断；只有策略允许时才检测其它美国候选节点。
- 候选节点必须通过基础可用性、目标可用性、近期切换过滤和明显改善规则；启用候选复测时，复测结果也必须仍然满足明显改善规则，才允许切换。
- 自动切换目标策略组的 `now` 允许从原当前节点变为最优节点。
- 其他策略组仍必须保持原样，除非它们原本就是同一个待切换策略组。
- `still_changed` 不应把被允许的自动切换视为异常。
- JSON 中必须明确记录允许变化的策略组、原节点、新节点、触发原因。

## auto-switch 检测范围要求

默认检测模式，即未传入 `--auto-switch-if-current-not-good` 时：

- 检测所有从 `/proxies` 过滤出的美国真实节点。
- 输出完整美国节点状态列表。

自动切换模式，即传入 `--auto-switch-if-current-not-good` 时：

- 第一阶段只检测当前正在使用的美国节点。
- 如果当前节点等级为 `good`，脚本不得继续检测其它美国节点，并重置连续异常计数。
- 如果当前节点为可接受的 `slow`，脚本不得继续检测其它美国节点。
- 如果当前节点为超阈值 `slow`，需要达到 `--slow-confirm-count` 后才进入第二阶段。
- 如果当前节点为 `poor`、`dead`、`unknown` 或 `missing`，需要达到 `--bad-confirm-count` 后才进入第二阶段。
- 如果仍处于切换冷却期，默认不得进入第二阶段；只有连续 `dead` 达到 `--break-cooldown-dead-count` 时，才允许提前打破冷却。
- 第二阶段候选节点应排除当前节点。
- 第二阶段候选节点仍必须来自 `/proxies` 全量池过滤结果，且必须在目标可切换策略组 `all` 列表中。
- 第二阶段候选节点必须通过基础可用性、目标可用性、近期切换过滤和改善阈值过滤。

JSON 输出中应体现检测范围：

```json
{
  "auto_switch": {
    "mode": "current-first",
    "candidate_scan_started": false,
    "candidate_scan_reason": "current_node_good"
  }
}
```

当当前节点达到连续确认条件并通过冷却判断时：

```json
{
  "auto_switch": {
    "mode": "current-first",
    "candidate_scan_started": true,
    "candidate_scan_reason": "bad_confirmed"
  }
}
```

## 输出要求

默认输出人类可读文本，包括：

1. 过滤来源。
2. 过滤规则。
3. 基础测试 URL。
4. 目标 API 列表。
5. 超时时间。
6. 受保护策略组数量。
7. 美国节点数量。
8. 按基础延迟升序排列的美国节点状态。
9. 每个节点的基础状态和目标 API 状态。
10. 目标 API 可达节点统计和按目标延迟排序的摘要。
11. 当前节点识别结果。
12. 自动切换模式是否启用。
13. 如果启用自动切换：第一阶段当前节点检测结果、是否开始候选扫描、是否触发切换、切换原因、原节点、目标节点、执行结果。
14. 如果启用自动切换：连续异常计数器、冷却剩余时间、候选过滤数量和候选复测结果。
15. 切换保护结果。
16. 最终策略组状态是否符合预期。

## JSON 输出要求

支持 `--json` 参数输出结构化 JSON，字段包括：

```json
{
  "source": "/proxies full pool",
  "region": "us",
  "filter": "name contains 🇺🇸 / 美国 / US / USA / United States; excludes strategy groups and special nodes",
  "base_url": "http://www.gstatic.com/generate_204",
  "targets": {
    "discord": "https://discord.com/api/v10/gateway"
  },
  "timeout_ms": 5000,
  "target_timeout_ms": 8000,
  "strategy_groups_protected": 10,
  "region_nodes_count": 32,
  "nodes": [
    {
      "name": "节点名",
      "base": {"ok": true, "delay_ms": 183, "level": "good", "error": ""},
      "targets": {
        "discord": {"ok": true, "delay_ms": 210, "level": "good", "error": ""}
      }
    }
  ],
  "base_alive": [
    {"name": "节点名", "delay_ms": 183, "level": "good"}
  ],
  "base_dead": [
    {"name": "节点名", "level": "dead", "error": "错误信息"}
  ],
  "target_alive": {
    "discord": [
      {"name": "节点名", "delay_ms": 210, "level": "good"}
    ]
  },
  "target_dead": {
    "discord": [
      {"name": "节点名", "level": "dead", "error": "错误信息"}
    ]
  },
  "current_node": {
    "detected": true,
    "group": "🔰 代理",
    "name": "🇺🇸 Lil 美国03",
    "base": {"ok": true, "delay_ms": 299, "level": "good", "error": ""},
    "targets": {
      "discord": {"ok": true, "delay_ms": 498, "level": "slow", "error": ""}
    },
    "dead": false,
    "dead_by": null,
    "needs_switch": true,
    "switch_by": "target:discord",
    "switch_level": "slow"
  },
  "auto_switch": {
    "enabled": true,
    "mode": "current-first",
    "candidate_scan_started": true,
    "candidate_scan_reason": "bad_confirmed",
    "triggered": true,
    "reason": "bad_confirmed",
    "check_target": "discord",
    "candidate_quality_target": "discord",
    "from_group": "🔰 代理",
    "from_node": "🇺🇸 Lil 美国03",
    "to_node": "🇺🇸 Lil 美国01",
    "candidate_filter": {
      "quality_target": "discord",
      "min_improvement_ms": 100,
      "scanned": 12,
      "filtered_current": 1,
      "filtered_not_allowed": 0,
      "filtered_recent": 1,
      "filtered_base_unavailable": 2,
      "filtered_target_unavailable": 3,
      "filtered_not_improved": 4,
      "eligible": 2,
      "recent_nodes": ["🇺🇸 Lil 美国02"]
    },
    "candidate_confirmation": {
      "enabled": true,
      "target": "discord",
      "node": "🇺🇸 Lil 美国01",
      "base": {"ok": true, "delay_ms": 180, "level": "good", "error": ""},
      "check": {"ok": true, "delay_ms": 210, "level": "good", "error": ""},
      "passed": true
    },
    "status": "success"
  },
  "switch_policy": {
    "enabled": true,
    "state_file": "logs/auto_switch_state.json",
    "state_load_error": null,
    "bad_threshold": "poor",
    "bad_confirm_count": 2,
    "slow_switch_threshold_ms": 600,
    "slow_confirm_count": 5,
    "switch_cooldown_seconds": 600,
    "break_cooldown_dead_count": 3,
    "min_improvement_ms": 100,
    "confirm_candidate": true,
    "confirm_target": "discord",
    "avoid_recent_switches": 3,
    "avoid_recent_window_seconds": 1800,
    "bad_count": 2,
    "slow_count": 0,
    "dead_count": 0,
    "required_count": 2,
    "observed_count": 2,
    "in_cooldown": false,
    "cooldown_remaining_seconds": 0,
    "cooldown_break_allowed": false
  },
  "switch_decision": {
    "should_scan_candidates": true,
    "reason": "bad_confirmed",
    "level": "poor",
    "switch_by": "target:discord",
    "delay_ms": 910,
    "current_needs_switch": true,
    "candidate_filter": {
      "quality_target": "discord",
      "eligible": 2
    },
    "candidate_confirmation": {
      "enabled": true,
      "target": "discord",
      "passed": true
    }
  },
  "restore_events": [],
  "allowed_changes": [],
  "still_changed": {},
  "guarantee": "changed_as_requested"
}
```

兼容要求：

- `base_alive` / `base_dead` 替代旧版 `alive` / `dead`。
- 为便于迁移，可以保留旧版 `alive` / `dead` 字段作为 `base_alive` / `base_dead` 的别名。
- `region` 表示实际规范化后的地区值，例如 `us`、`sg`。
- `region_nodes_count` 表示当前 `--region` 过滤出的真实节点数量，替代旧版 `us_nodes_count` 口径。
- `filter` 应按地区关键词动态生成。
- 当自动切换成功时，`guarantee` 可以为 `changed_as_requested`。
- 当默认检测模式或未触发自动切换时，`guarantee` 应为 `unchanged`。

## 命令行参数

脚本应支持：

```text
--socket                       mihomo Unix Socket 路径，默认 /tmp/verge/verge-mihomo.sock
--region                       检测地区，支持 us/sg/uk/jp/hk/de/fr，大小写不敏感，默认 us
--url                          基础测速 URL，默认 http://www.gstatic.com/generate_204
--timeout                      基础单节点测速超时时间，单位 ms，默认 5000
--target                       目标 API，格式 名称=URL，可重复传入
--target-timeout               目标 API 单节点检测超时时间，单位 ms，默认 8000
--no-default-targets           不加载默认目标 API
--auto-switch-if-current-not-good 当前节点达到策略确认条件且未被冷却阻止时切换到更优可用节点，默认关闭
--switch-check-target             用于判断当前质量和选择最优节点的目标名，例如 discord
--state-file                   自动切换状态文件，默认 logs/auto_switch_state.json
--bad-threshold                进入 bad 确认逻辑的等级，目前固定支持 poor
--bad-confirm-count            poor/dead/unknown/missing 连续确认次数，默认 2
--slow-switch-threshold-ms     slow 延迟超过该值后才累计 slow 计数，默认 600
--slow-confirm-count           slow 超阈值后的连续确认次数，默认 5
--switch-cooldown-seconds      成功切换后的冷却时间，默认 600
--break-cooldown-dead-count    冷却期内允许提前切换的连续 dead 次数，默认 3
--min-improvement-ms           同等级候选需要达到的最小延迟改善，默认 100
--confirm-candidate            切换前复测选中的候选节点
--confirm-target               候选质量与复测目标，默认 switch 目标、discord、base
--avoid-recent-switches        近期切换过滤检查的事件数量，默认 3
--avoid-recent-window-seconds  近期切换过滤时间窗口，默认 1800
--prefer-groups                当前节点所属策略组优先级，逗号分隔
--json                         输出 JSON
```

## 退出码

```text
0  执行成功；未启用切换时策略组保持原样，或启用切换时只发生被允许的切换
2  执行完成，但最终仍有非预期策略组状态未能恢复
3  自动切换被触发但执行失败或校验失败
1  其他运行错误，例如参数错误、Socket 不可用、API 返回异常
```

## 当前实现文件

```text
check_us_proxy_status.py
```

## 当前验证要求

修改后至少验证：

```bash
./check_us_proxy_status.py --json
```

指定地区验证：

```bash
./check_us_proxy_status.py --region sg --json
```

无效地区验证：

```bash
./check_us_proxy_status.py --region ca
```

应在不请求 `/proxies` 的情况下返回退出码 `1`，并输出：

```text
--region: invalid value 'ca'
```

以及：

```bash
./check_us_proxy_status.py \
  --no-default-targets \
  --target discord=https://discord.com/api/v10/gateway \
  --json
```

自动切换功能修改后还需验证：

```bash
./check_us_proxy_status.py \
  --auto-switch-if-current-not-good \
  --switch-check-target discord \
  --json
```

验证通过标准：

```text
region 为规范化后的地区值
region_nodes_count > 0
nodes 中包含 base 与 targets 字段
base_alive + base_dead 数量等于 region_nodes_count
target_alive.discord + target_dead.discord 数量等于 region_nodes_count
current_node 字段存在且能说明是否识别成功
未启用自动切换时 guarantee = unchanged
启用自动切换但当前节点等级为 good 时 auto_switch.triggered=false、auto_switch.candidate_scan_started=false 且 guarantee=unchanged
启用自动切换且当前节点达到连续确认条件、未被冷却阻止时，auto_switch.candidate_scan_started=true；若存在通过过滤和复测的可用候选，则 auto_switch.triggered=true 且 guarantee=changed_as_requested
启用自动切换时，switch_policy 和 switch_decision 字段存在，并能说明计数器、冷却状态、候选过滤数量和候选复测结果
restore_events 可为空或包含已恢复事件
still_changed 为空，或只包含允许的自动切换变化
退出码符合执行结果
```

## 关键约束总结

最重要的约束是：

```text
只用 /proxies 获取完整代理池并过滤指定地区真实节点，默认地区 us 保持美国节点兼容语义；
只对真实节点逐个调用 /proxies/{node}/delay；
目标 API 检测也必须使用 /proxies/{node}/delay?url={目标 API}；
绝不调用 /group/{group}/delay；
默认模式下每个检测动作后保护并校验所有策略组 now；
自动切换必须显式启用；
自动切换模式默认只检测当前正在使用的节点；
只有当前节点达到策略确认条件且未被冷却阻止时才检测其它候选节点并允许切换；
当前节点等级为 good 时不得因为更快节点存在而检测候选或切换；
自动切换只能切到目标策略组 all 列表中的最优可用指定地区节点；
最终确认策略组状态符合预期。
```
