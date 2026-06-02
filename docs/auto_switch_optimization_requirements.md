# 网络节点自动切换优化需求说明

## 需求审核结论

当前 PDF 的核心诉求明确：将优化前的「即时反应型自动切换」改为「稳定性优先、带迟滞与冷却的自动切换」。该方向合理，且与旧逻辑中「当前节点不是 `good` 即扫描并切换」的行为存在直接优化关系。

现有方案中，部分规则已具备可实现性，例如连续失败确认、切换冷却、候选节点改善阈值、候选复测、状态文件和防回跳机制。但在进入实现前，需要补齐以下细节：

1. 明确 `slow` 是否完全不切，还是仅当延迟超过阈值且连续出现时才切。
2. 明确状态文件的读写失败策略，避免状态损坏导致脚本异常退出或频繁切换。
3. 明确冷却期例外规则只针对 `dead`，还是也包含连续 `poor`。
4. 明确候选节点「明显更好」的比较规则，尤其是当前节点无可比较延迟时的排序方式。
5. 明确新增参数的默认值与关闭方式，保证已有使用方式不被突然改变。
6. 明确日志字段是否需要同时覆盖人类可读输出和 JSON 输出。

建议将本需求拆成 3 个实施阶段：先降低切换频率，再优化候选质量，最后补齐监控与报表。

## 背景

当前脚本支持自动切换模式：

```bash
./check_us_proxy_status.py \
  --auto-switch-if-current-not-good \
  --switch-check-target discord
```

优化前的触发逻辑偏激进：当当前节点用于切换判断的检测结果不是 `good` 时，即进入候选扫描和切换流程。由于 `slow` 的定义包含 `301ms <= delay <= 800ms`，例如 327ms 或 354ms 的 Discord API 延迟虽然不属于 `good`，但通常不代表节点已经需要立即切换。

从 PDF 给出的日志统计看，109 次完整执行中成功切换 43 次，成功切换比例约为 39%。近期还出现了几分钟内连续切换的情况，例如：

```text
118 -> 108 -> 109 -> 108 -> 104 -> 118
```

这说明当前策略对 Discord 延迟波动过于敏感，容易在节点池整体状态不稳定时来回切换。

## 优化目标

1. 降低自动切换频率，减少抖动。
2. 只在当前节点明显不可用或持续不佳时切换。
3. 切换成功后为新节点保留稳定观察时间。
4. 避免整体网络较差时在多个不稳定节点之间来回切换。
5. 保留自动恢复能力，当前节点确实不可用时仍能切换。
6. 优先保护 Hermes / Discord 通道稳定，而不是追求最低延迟。

## 非目标

1. 不做最低延迟抢占。
2. 不做负载均衡。
3. 不因为存在更快节点而切换仍然可用的当前节点。
4. 不改变美国节点过滤方式。
5. 不改变基础检测和目标 API 检测的接口来源。

## 核心策略

自动切换策略应从「只要不是 `good` 就切」调整为：

```text
good       不切换，并重置连续异常计数
slow       默认不切换；仅当延迟超过阈值且连续确认后才允许切换
poor       连续确认后允许切换
dead       连续确认后允许切换；冷却期内可按例外规则提前切换
unknown    按 poor 处理，连续确认后允许切换
missing    按 dead 处理，连续确认后允许切换
```

## 延迟等级处理

沿用当前延迟等级定义：

```text
good    delay <= 300ms
slow    301ms <= delay <= 800ms
poor    delay > 800ms
dead    检测失败
unknown 检测成功但没有可用 delay 值
```

新增切换判断规则：

1. `good`：不切换，清空当前目标的异常计数。
2. `slow` 且延迟不超过 `--slow-switch-threshold-ms`：不切换。
3. `slow` 且延迟超过 `--slow-switch-threshold-ms`：累计 slow 计数，达到 `--slow-confirm-count` 后才允许切换。
4. `poor`：累计 bad 计数，达到 `--bad-confirm-count` 后才允许切换。
5. `dead`：累计 bad 计数和 dead 计数，达到 `--bad-confirm-count` 后才允许切换。

推荐默认值：

```text
--bad-threshold poor
--bad-confirm-count 2
--slow-switch-threshold-ms 600
--slow-confirm-count 5
```

说明：

- `--bad-threshold poor` 表示 `poor`、`dead`、`unknown`、`missing` 进入 bad 确认逻辑。
- `slow` 不直接视为 bad，避免 300ms 到 600ms 区间的轻微波动触发切换。

## 切换冷却

切换成功后进入冷却期。冷却期内默认只记录状态，不执行新的切换。

推荐默认值：

```text
--switch-cooldown-seconds 600
--break-cooldown-dead-count 3
```

规则：

1. 距离上次成功切换不足 `--switch-cooldown-seconds` 时，禁止再次切换。
2. 冷却期内如果当前节点连续 `dead` 达到 `--break-cooldown-dead-count`，允许提前打破冷却。
3. 冷却期内的 `slow` 和 `poor` 只更新计数与日志，不触发切换。

更保守配置：

```text
--switch-cooldown-seconds 900
--bad-confirm-count 3
```

## 候选节点筛选

候选节点必须同时满足：

1. 属于当前策略组允许切换的节点。
2. 不是当前节点。
3. 基础检测可用，即 `base.ok = true`。
4. 切换判断目标可用，例如 `discord.ok = true`。
5. 未命中近期回跳过滤规则。
6. 相比当前节点明显更好。

「明显更好」满足以下任一条件即可：

1. 当前节点为 `dead`、`unknown` 或 `missing`，候选目标 API 可达。
2. 候选节点的目标等级明显优于当前节点。
3. 候选节点目标延迟至少比当前节点低 `--min-improvement-ms`。

等级明显提升示例：

```text
dead -> slow
dead -> good
poor -> slow
poor -> good
slow -> good
```

推荐默认值：

```text
--min-improvement-ms 100
```

## 候选节点复测

选出最佳候选节点后，不应立即切换，应先对候选节点复测一次。

推荐参数：

```text
--confirm-candidate
--confirm-target discord
```

默认复测规则：

1. 对候选节点重新检测 `--confirm-target` 指定的目标。
2. 复测结果必须 `ok = true`。
3. 如果复测失败，放弃本轮切换，并记录 `candidate_confirmation_failed`。

可选更保守规则：

1. 同时复测基础 URL 和目标 API。
2. 两者都可用时才允许切换。

## 状态文件

新增状态文件，用于跨进程保存切换判断所需状态。

推荐路径：

```text
logs/auto_switch_state.json
```

推荐结构：

```json
{
  "current_node": "美国 108 ChatGPT | 1x US",
  "last_switch_at": "2026-05-29T19:22:00+08:00",
  "consecutive_bad": {
    "discord": 1
  },
  "consecutive_slow": {
    "discord": 0
  },
  "consecutive_dead": {
    "discord": 0
  },
  "recent_switches": [
    {
      "at": "2026-05-29T19:22:00+08:00",
      "from": "美国 104 ChatGPT | 1x US",
      "to": "美国 118 ChatGPT | 1x US",
      "reason": "current_node_dead_by_target:discord"
    }
  ]
}
```

状态文件要求：

1. 启动时读取；文件不存在时使用空状态。
2. JSON 解析失败时不得直接触发切换，应记录错误并使用保守策略。
3. 写入时应使用临时文件加原子替换，避免中途写坏。
4. 切换成功后必须更新 `last_switch_at` 和 `recent_switches`。
5. 当前节点恢复为 `good` 后，应重置对应目标的异常计数。

## 防回跳机制

短时间内避免切回最近切过的节点，以减少以下抖动：

```text
108 -> 109 -> 108
```

推荐默认值：

```text
--avoid-recent-switches 3
--avoid-recent-window-seconds 1800
```

规则：

1. 读取 `recent_switches` 中最近 `--avoid-recent-switches` 条记录。
2. 仅保留发生在 `--avoid-recent-window-seconds` 时间窗口内的记录。
3. 候选节点如果出现在近期 `from` 或 `to` 节点中，则默认过滤。
4. 当没有其它候选且当前节点为 `dead` 时，可考虑提供参数允许忽略该过滤规则；默认不启用。

## 推荐运行命令

推荐默认版本：

```bash
python3 check_us_proxy_status.py \
  --auto-switch-if-current-not-good \
  --switch-check-target discord \
  --state-file logs/auto_switch_state.json \
  --bad-threshold poor \
  --bad-confirm-count 2 \
  --slow-switch-threshold-ms 600 \
  --slow-confirm-count 5 \
  --switch-cooldown-seconds 600 \
  --break-cooldown-dead-count 3 \
  --min-improvement-ms 100 \
  --confirm-candidate \
  --avoid-recent-switches 3 \
  --avoid-recent-window-seconds 1800
```

更保守版本：

```bash
python3 check_us_proxy_status.py \
  --auto-switch-if-current-not-good \
  --switch-check-target discord \
  --state-file logs/auto_switch_state.json \
  --bad-threshold poor \
  --bad-confirm-count 3 \
  --slow-switch-threshold-ms 600 \
  --slow-confirm-count 5 \
  --switch-cooldown-seconds 900 \
  --break-cooldown-dead-count 3 \
  --min-improvement-ms 100 \
  --confirm-candidate \
  --avoid-recent-switches 3 \
  --avoid-recent-window-seconds 1800
```

## 决策流程

伪代码：

```python
current = check_current_node()

if current.discord.level == "good":
    reset_bad_counter("discord")
    reset_slow_counter("discord")
    reset_dead_counter("discord")
    return no_switch("current_node_good")

if current.discord.level == "slow":
    if current.discord.delay <= slow_switch_threshold_ms:
        reset_bad_counter("discord")
        return no_switch("current_node_slow_but_acceptable")

    increment_slow_counter("discord")
    if slow_counter < slow_confirm_count:
        return no_switch("waiting_for_slow_confirmation")

if current.discord.level in {"poor", "unknown"}:
    increment_bad_counter("discord")
    if bad_counter < bad_confirm_count:
        return no_switch("waiting_for_bad_confirmation")

if current.discord.level in {"dead", "missing"}:
    increment_bad_counter("discord")
    increment_dead_counter("discord")
    if bad_counter < bad_confirm_count:
        return no_switch("waiting_for_bad_confirmation")

if in_cooldown():
    if dead_counter < break_cooldown_dead_count:
        return no_switch("cooldown_active")
    allow_switch("break_cooldown_current_dead")

candidates = scan_candidates()
candidates = [
    candidate
    for candidate in candidates
    if candidate.base.ok
    and candidate.discord.ok
    and not recently_switched(candidate)
    and clearly_better(candidate, current, min_improvement_ms)
]

best = pick_best(candidates)
if not best:
    return no_switch("no_available_candidate")

if confirm_candidate:
    recheck = check_node(best)
    if not recheck.discord.ok:
        return no_switch("candidate_confirmation_failed")

switch_to(best)
record_switch()
return success("candidate_confirmed")
```

## 日志与 JSON 输出

人类可读输出应包含：

```text
决策原因: switch_cooldown_active
计数器 bad: 2/2
计数器 slow: 0/5
计数器 dead: 0/3
冷却状态: 生效，剩余 420 秒，允许打破冷却: False
候选过滤: 扫描 5，可用 1，近期切换 1，基础不可用 1，目标不可用 1，改善不足 1
候选复测: 开启，目标 discord，结果 True
自动切换结果: success / bad_confirmed
```

JSON 输出应包含：

```json
{
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
    "avoid_recent_switches": 3,
    "avoid_recent_window_seconds": 1800,
    "confirm_candidate": true,
    "confirm_target": "discord",
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
      "min_improvement_ms": 100,
      "scanned": 5,
      "filtered_recent": 1,
      "filtered_base_unavailable": 1,
      "filtered_target_unavailable": 1,
      "filtered_not_improved": 1,
      "eligible": 1
    },
    "candidate_confirmation": {
      "enabled": true,
      "target": "discord",
      "passed": true
    }
  }
}
```

## 分阶段实施建议

### Phase 1：降低切换频率

目标：

1. `slow` 默认不切换。
2. `poor` / `dead` 连续确认后才切换。
3. 切换成功后进入 10 分钟冷却。

新增参数：

```text
--state-file
--bad-threshold
--bad-confirm-count
--slow-switch-threshold-ms
--slow-confirm-count
--switch-cooldown-seconds
--break-cooldown-dead-count
```

验收标准：

1. 当前节点为 `good` 时不扫描候选。
2. 当前节点为可接受 `slow` 时不扫描候选。
3. 当前节点第一次 `poor` 或 `dead` 时不切换。
4. 当前节点连续达到确认次数后才进入候选扫描。
5. 冷却期内不重复切换。

### Phase 2：提升候选质量

目标：

1. 候选节点必须明显更好。
2. 候选节点切换前复测。
3. 近期切换过的节点默认不参与候选。

新增参数：

```text
--min-improvement-ms
--confirm-candidate
--confirm-target
--avoid-recent-switches
--avoid-recent-window-seconds
```

验收标准：

1. 候选节点延迟未达到改善阈值时不切换。
2. 候选节点复测失败时不切换。
3. 近期切换过的节点被过滤。
4. 当前节点为 `dead` 且无可比较延迟时，候选可达即可参与排序。

### Phase 3：监控和报表

目标：

1. 增加结构化 JSONL 日志。
2. 增加每小时汇总。
3. 增加切换次数告警字段。
4. 增加 Discord 可达率趋势字段。

每小时汇总示例：

```text
最近 1 小时：
- 执行 60 次
- no_switch 57 次
- switch 3 次
- Discord 平均可达 6.2/20
- 当前节点保持时间 27 分钟
```

## 待确认事项

1. `unknown` 是否按 `poor` 处理，还是单独设置确认次数。
2. 冷却期例外是否只允许连续 `dead` 打破。
3. 状态文件损坏时是否允许自动重建。
4. 是否需要提供参数完全关闭状态文件，仅使用单次运行内存状态。
5. 防回跳过滤是否需要在当前节点 `dead` 且无候选时自动降级。
6. 结构化日志文件路径是否与状态文件同目录。
