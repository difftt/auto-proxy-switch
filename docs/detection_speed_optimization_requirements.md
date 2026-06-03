# 检测速度优化需求说明

文件：`check_us_proxy_status.py`

## 背景

当前 `check_us_proxy_status.py` 对地区节点进行基础测速与目标 API 可达性检测时，节点之间完全串行。实测基线（macOS / mihomo Unix Socket / 32 个美国节点 / 1 个默认 target）：

```text
总耗时：~53s
模型：32 节点 × (1 base + 1 target) ≈ 64 次串行 HTTP-over-Unix-Socket
```

需求侧已经将自动切换策略改为"懒检测"（先只检当前节点，bad 确认后才扫候选），但：

1. 默认模式（无 auto-switch）的全量节点扫描仍是主要性能瓶颈。
2. auto-switch 模式第二阶段（候选扫描）触发时，候选节点之间同样串行。
3. `restore_changed_groups` 在每个检测动作后都重读 `/proxies`，放大 API 调用次数。
4. `MihomoUnixClient` 每次请求都新建 socket，未复用连接。

本文档定义在不改变现有检测语义、不改变 JSON 输出字段结构、不引入新依赖的前提下，对检测速度进行优化。

## 目标

1. 默认模式下，地区节点之间支持并发检测。
2. auto-switch 模式仅第二阶段（候选节点扫描）支持并发；第一阶段懒检测（先只检当前节点）保持串行，语义不变。
3. 减少非必要的 `/proxies` 全量重读次数。
4. 默认模式与 auto-switch 模式的总耗时显著下降。
5. 不改变延迟等级阈值、状态文件结构、JSON 字段名。
6. 不引入第三方依赖，仅使用 Python 标准库（`concurrent.futures.ThreadPoolExecutor` 等）。

## 非目标

1. 不改变基础测速、目标 API 检测的接口来源（仍为 mihomo `GET /proxies/{节点}/delay`）。
2. 不改变地区节点过滤规则、当前节点识别规则、防回跳 / 冷却策略。
3. 不改变自动切换策略本身的决策流程（仅优化其执行速度）。
4. 不引入 asyncio 改造（I/O 密集场景下线程池足够，避免改造面过大）。`concurrent.futures.ThreadPoolExecutor` 属于 Python 标准库，不在此限制范围内。
5. 不动 `tests/test_auto_switch_policy.py` 现有测试用例（新增并发集成测试需用户确认后由用户执行）。

## 性能基线与目标值

### 实测基线（参考）

```text
场景 1 — 默认模式全量检测：
  节点数：32
  检测项：1 base + 1 default target
  墙钟：~53s
  `/proxies` 重读次数：~64 次（base 后 + 每 target 后各 1 次）
  HTTP-over-Unix-Socket 总请求数：~96 次（64 delay + ~32 restore 重读 + 收尾）

场景 2 — auto-switch 模式当前节点为 good：
  检测项：1 base + 1 target（仅当前节点）
  墙钟：~6-8s
  无需扫描候选
```

### 节点耗时分布参考值

```text
单节点 1 base + 1 target 串行耗时（来自 ~53s / 32 节点的均值拆分）：
  均值：1.6s/节点
  注：受 mihomo 单节点 timeout（base 5000ms + target 8000ms）和网络波动影响，
      实测中部分 dead 节点可能接近 timeout 边界（11-14s），alive 节点通常 < 2s。
  实施前建议先用 time -p 跑 5 次取中位数 / 95 百分位，作为并发数选择的实测依据：
    5 次中位数 = 1.6s/节点 → 16 并发理论下界 = 5-6s（受最慢节点约束）
    5 次 95 百分位 = 14s/节点 → 16 并发最坏下界 ≈ max(1.6s, 14s/16) ≈ 1.6s（墙钟上限受单节点 timeout 约束）
  推荐并发数公式：concurrent = ceil(总墙钟目标 / 单节点 P95 耗时)，默认目标 12s → 至少 2 并发；推荐 8-16 留余量。
```

### 目标值

```text
场景 1 — 默认模式全量检测：
  墙钟：≤ 12s
  `/proxies` 重读次数：减少 ≥ 30%
  节点顺序：与优化前 `region_nodes` 列表顺序一致

场景 2 — auto-switch 模式候选扫描（10 候选）：
  墙钟：≤ 20s
  第一阶段当前节点检测耗时：保持原样不变
  候选节点顺序：与 `region_nodes` 列表顺序一致
```

## 优化项

### 优化项 1 — 默认模式节点级并发（主优化）

**目标**：将 32 节点串行 ~53s 降到 ≤ 12s。

**改动范围**：
  - 新增命令行参数：`--concurrent N`，默认 `16`。
  - 默认模式扫描循环（line 1154-1158）改用 `concurrent.futures.ThreadPoolExecutor`。
  - 每个 worker 持有**独立**的 `MihomoUnixClient` 实例（避免 Unix Socket 上的 HTTP 帧交错）。
  - `allowed_changes` 在主线程持有，worker 内**只读不可改**（详见"实施顺序"约束）。
  - 并发完成后按原 `region_nodes` 顺序排序 `node_results`，再写入 JSON。

**约束**：
  - 默认并发数 16 为建议值，实施前需先做 mihomo 端并发压测（见"前置验证"）。
  - 并发数可通过 `--concurrent` 调整；设置 `1` 时退化为原串行行为。
  - JSON 输出 `nodes` 数组顺序必须与 `--region` 过滤出的 `region_nodes` 顺序保持一致。

**验收标准**：
  1. 32 节点场景下总墙钟 ≤ 12s。
  2. JSON 输出 `nodes` 数组顺序与优化前一致。
  3. `--concurrent 1` 时行为与原实现一致（功能等价、字段一致、退出码一致）。
  4. 新增 JSON 字段 `auto_switch.concurrent`（仅在 auto-switch 模式下记录实际并发数；默认模式不写）。

**风险**：
  - mihomo 端并发承载能力：需压测确认 16 并发下 `/proxies/{节点}/delay` 不出现 >5% 失败或响应时间退化 >2x。
  - 缓解：默认并发从 8 起步，压测后再决定是否上调。

### 优化项 2 — 精简 restore_changed_groups 触发频率（与优化项 1 协同）

**目标**：减少 `check_node` 内部的 `/proxies` 拉取次数，从每检测项 1 次降到每节点 1 次，与优化项 1 的并发改造协同生效。

**改动范围**：
  - `check_node` 函数（line 292-318）重组调用顺序：
    1. 串行完成 base 测速。
    2. 串行完成所有 target 测速。
    3. 最后统一调用 1 次 `restore_changed_groups`。
  - 适用于默认模式与 auto-switch 模式（含候选扫描与候选复测）。
  - 协同要求：本优化项必须**先于**优化项 1 实施或在同一次改动中合并完成。否则优化项 1 引入并发后，worker 内部仍是检测项级 restore，并发场景下 restore 调用次数不会减少。

**约束**：
  - 检测过程中如果发生外部切换，脚本仍会尝试恢复为脚本启动时的策略组 `now`。
  - 与原需求 line 438 关系：本优化项**修订**原 `check_us_proxy_status_requirements.md` 中"防意外切换保护机制"第 5 步（line 438）：
    - 原条款：每个节点的每个检测动作完成后，重新读取 `/proxies` 并对比恢复。
    - 修订后：每个节点的所有检测动作完成后统一读取 `/proxies` 并对比恢复。
  - 修订原因：原条款在并发场景下会导致 restore 频率放大，并成为整体性能的次要瓶颈。

**验收标准**：
  1. 单次检测总 `/proxies` 重读次数减少 ≥ 30%。
  2. `still_changed` 字段在策略组最终状态与初始不一致时仍为 `True`。
  3. 现有 `tests/test_auto_switch_policy.py` 全部通过。

**风险**：
  - 检测项级别的 restore 触发被合并到节点级别后，一个节点检测过程中的"中途外部切换"将延后到节点检测完成后才被恢复。
  - 缓解：节点之间的外部切换仍按原语义在节点检测后被恢复；单节点检测耗时较短（base+target ≤ 16s），最坏情况外部切换恢复延后一个节点检测周期。

### 优化项 3 — auto-switch 模式第二阶段候选并发

**目标**：当前节点 bad 确认后扫描候选时，候选节点之间并发检测。

**改动范围**：
  - auto-switch 模式 `for node in candidate_nodes` 循环（line 1027-1039）改用 `ThreadPoolExecutor`。
  - 默认并发数：min(8, len(candidate_nodes))，可由 `--concurrent` 统一控制。
  - 第一阶段"懒检测"（仅检测当前节点）保持串行，语义不变。
  - 候选复测（`check_node(best.name, ...)`）保持单节点串行。

**约束**：
  - 候选扫描期间策略组 `now` 仍为当前节点，不允许并发触发 mihomo 自动选优。
  - 前提：mihomo `GET /proxies/{节点}/delay` 不会改变调用方传入的代理组 `now`。需先实测确认。

**验收标准**：
  1. auto-switch 模式 `should_scan_candidates=True` 时，10 候选场景总墙钟 ≤ 20s。
  2. `current_node_info` 中当前节点结果与并发前一致。
  3. 候选结果在 `choose_switch_target` 中按原顺序处理（不因并发影响排序）。

**风险**：
  - 并发候选扫描时，mihomo 端压力叠加在默认模式并发之上，需要 `--concurrent` 调小以避免双重并发峰值。
  - 缓解：auto-switch 模式默认并发数取 min(8, 候选数)，低于默认模式 16。

### 优化项 4 — HTTP 客户端连接复用（已纳入本轮，建议实施）

**目标**：避免每次 `request` 都新建 Unix Socket 连接。

**改动范围**：
  - `MihomoUnixClient.request`（line 100-139）改用 `http.client.HTTPConnection` 配合 Unix Socket 文件描述符。
  - 或在当前 `MihomoUnixClient` 内部实现 HTTP/1.1 keep-alive 连接复用。

**约束**：
  - 不改变对外 API：`get_json` / `put_json` / `request` 签名与返回值不变。
  - 不改变超时行为。

**验收标准**：
  1. 所有现有 `tests/test_auto_switch_policy.py` 通过。
  2. 默认模式 + auto-switch 模式各跑一次真实环境，无功能差异。
  3. `still_changed` / `guarantee` / `switch_result.status` 字段值与优化前一致。

**风险**：
  - 连接复用可能引入 HTTP 帧解析细节差异（如 Connection: keep-alive 处理、错误响应解析）。
  - 缓解：完整回归测试 + 真实环境对比。

## 新增命令行参数

```text
--concurrent N   节点级并发 worker 数，默认 16；硬约束 [1, 32]，超出范围 argparse 报错退出（退出码 2）；设 1 时退化为串行
                 同时控制默认模式节点扫描与 auto-switch 模式候选扫描的并发度
```

## JSON 输出变更

### 新增字段

在 `auto_switch` 顶层对象下新增 `concurrent` 字段，记录实际使用的并发 worker 数：

```json
{
  "auto_switch": {
    "enabled": true,
    "mode": "current-first",
    "concurrent": 8,
    ...
  }
}
```

### 字段不变

- `nodes` 数组顺序保持与 `--region` 过滤出的 `region_nodes` 顺序一致。
- `region_nodes_count` / `auto_switch.candidate_filter` / `auto_switch.candidate_confirmation` / `restore_events` / `allowed_changes` / `still_changed` / `guarantee` 等字段名与结构不变。
- `switch_policy` / `switch_decision` 结构不变。

## 前置验证

在实施优化项 1 之前，建议先做 1 次 mihomo 端并发压测（由用户执行）。

### 压测内容

```text
工具：临时 Python 脚本或 xargs -P N
方法：对 mihomo Unix Socket 并发打 N 个 /proxies/{节点}/delay 请求
参数：N 取 8 / 16 / 32
观察：响应成功率、平均响应时间、是否有 5xx 或连接拒绝
判定标准：失败率 < 5% 且平均响应时间退化 < 2x
```

压测结果决定：
  - 16 并发承载足够 → 默认 `--concurrent 16`。
  - 16 并发不稳定 → 默认 `--concurrent 8`，并考虑加入 mihomo 端限流。

### 附加观察 — URLTest 策略组 `now` 是否被并发检测触发自动重选

并发候选扫描（优化项 3）的前置条件是：`/proxies/{节点}/delay` 不会改变调用方传入的代理组 `now`。

需要在压测过程中**同时**观察一个 URLTest 类型策略组的 `now`：
  1. 压测开始前记录 URLTest 组的 `now`。
  2. 压测期间并发 16/32 个 `/proxies/{节点}/delay` 请求。
  3. 压测结束后再次读取 URLTest 组的 `now`。
  4. 对比：若 `now` 未变化 → 前置条件成立。
  5. 对比：若 `now` 被自动重选 → 不能在 auto-switch 模式启用候选并发，需回退到优化项 1 默认模式并发 + auto-switch 候选串行的混合方案。

### 降级阶梯

如果 mihomo 端并发承载不达标，按以下阶梯降级：

```text
Level 0（默认）：--concurrent 16
  触发：压测 16 并发失败率 < 5% 且响应退化 < 2x
  适用：mihomo 端 Unix Socket 性能充足

Level 1：--concurrent 8
  触发：压测 16 并发不达标但 8 并发达标
  适用：mihomo 端中度承载
  调整：更新 `main()` 中 argparse 默认值为 8

Level 2：--concurrent 4
  触发：压测 8 并发不达标但 4 并发达标
  适用：mihomo 端较弱或网络环境受限
  调整：更新 argparse 默认值为 4，并在 README「常用参数」标注

Level 3：--concurrent 1（退化为原串行）
  触发：压测 4 并发仍不达标
  适用：mihomo 端极弱
  行为：优化项 1/3 整体降级为原实现，仅保留优化项 2（restore 频率精简）和优化项 4（HTTP 连接复用）
  文档：在 README 增加说明「在弱 mihomo 环境下，建议设 --concurrent 1 退回串行」
```

每档降级都需要在 README 的「常用参数」章节同步更新默认值，并在 cron 推荐命令中给出对应建议。

## 实施顺序与依赖

### 并发安全约束（适用于所有含并发的优化项）

```text
1. MihomoUnixClient 实例化：每个 worker 必须持有独立实例
   原因：Unix Socket 上的 HTTP/1.1 帧若共享一个 socket 会交错
   实施：worker 入口创建 client，worker 出口关闭 client（或放入 thread-local 池）

2. allowed_changes 字典：
   持有者：主线程
   worker 行为：只读（line 307 / 316 当前仅读取 allowed_changes 中的 key，不修改）
   主线程合并：worker 返回 NodeResult 后由主线程在唯一收尾点（run_check line 1107 起）写入
   注：当前代码 line 307/316 不修改 allowed_changes，但需在重构时严格保持这一不变量

3. 状态文件读写：
   持有者：主线程（load_switch_state / save_switch_state 仅在 run_check 入口/收尾点调用）
   worker 行为：不读写
   并发安全：auto-switch 流程的状态文件写入只发生在 decision / switch / confirmation 收尾点，
              与候选扫描 worker 无关；cron 跨进程无并发。
   结论：不需要加锁。

4. NodeResult 数据结构：
   每个 worker 独立构造，append 到主线程 list 时由 GIL 保护
   节点结果按 region_nodes 顺序在主线程排序后再写入 JSON
```

### 实施步骤

```text
Step 1 — 优化项 2（精简 restore 频率）
  改动小、风险低，先实施为后续并发让路
  协同：与优化项 1 联动生效
  依赖：无

Step 2 — 优化项 1（默认模式节点并发）
  引入 --concurrent 参数
  实施 ThreadPoolExecutor + per-worker 客户端
  前置：mihomo 并发压测（前置验证章节）
  前置：Step 1 已先降低 /proxies 频率

Step 3 — 优化项 3（auto-switch 候选并发）
  auto-switch 模式第二阶段并行
  第一阶段"懒检测"语义保持不变
  前置：URLTest 组 now 观察（前置验证章节）
  依赖：Step 2 已建立的并发基础设施

Step 4 — 优化项 4（HTTP 客户端重构）
  MihomoUnixClient 改用 http.client 或 keep-alive
  决策：已纳入本轮（D4）
  收益：单次请求节省 < 100ms
  依赖：无，但与 Step 2/3 独立可放最后
  备注：若实施时改造复杂度超预期，可推迟到下个迭代，不阻塞 P0/P1/P2 验收
```

## 验证要求

### 单元测试

- 现有 `tests/test_auto_switch_policy.py` 全部通过（仅测策略决策，与并发无关）。

### 集成测试（建议新增，由用户执行）

```text
1. 默认模式 — 32 节点并发场景：
   ./check_us_proxy_status.py --no-default-targets --json
   断言：region_nodes_count = 32；nodes 数组顺序与 region 过滤结果一致；总墙钟 < 12s

2. 默认模式 — 串行回退：
   ./check_us_proxy_status.py --concurrent 1 --json
   断言：行为与无 --concurrent 参数时的旧版一致

3. auto-switch 模式 — 当前节点为 good：
   ./check_us_proxy_status.py --auto-switch-if-current-not-good --switch-check-target discord
   断言：candidate_scan_started = false；墙钟与单节点检测耗时相当

4. auto-switch 模式 — 候选扫描触发：
   （需要当前节点确认 bad 的环境）
   断言：candidate_scan_started = true；候选节点检测总墙钟 < 20s
```

### 真实环境回归

```text
1. 优化前后各跑一次默认模式，对比 JSON 字段值（除 nodes 顺序外）。
   测量方法：
     time -p ./check_us_proxy_status.py --no-default-targets --json
   多次测量取中位数（建议 5 次）以减少 mihomo 抖动影响。
   记录：单次运行 total wall time / `/proxies` 调用次数（可临时打 log 计数）。

2. 优化前后各跑一次 auto-switch 模式，对比 switch_policy / switch_decision。
   当前节点 good：确认第一阶段耗时与原版一致（懒检测语义未变）。

3. cron 节奏回归：
   1 分钟节奏：*/1 * * * * 跑 5 次，确认每次 total wall < 60s，无重叠。
     优化后：默认模式 32 节点 < 12s、auto-switch 模式 < 15s，节奏未变但单次耗时显著下降。
   5 分钟节奏：*/5 * * * * 跑 3 次，行为同上。
   注：若用户历史依赖旧版 ~53s 的隐式节流，优化后会变成"每 60s 跑 12s + 48s 空闲"，
       这是预期行为，不影响功能；建议在 README「cron 定时运行」章节补一句"节奏未变，
       单次耗时从约 53s 降至约 12s"。

4. 现有测试回归：
   python3 -m pytest tests/test_auto_switch_policy.py
   全部通过（run_check 签名新增参数后 FakeClient 路径应仍兼容）。
```

## 风险登记

### R1 — mihomo 并发承载不足
  - 影响：优化项 1、3
  - 触发：压测失败率 > 5% 或响应退化 > 2x
  - 缓解：先压测；默认并发从 8 起步；提供 `--concurrent` 调优

### R2 — 节点顺序变化破坏 JSON 消费者
  - 影响：优化项 1、3
  - 缓解：所有并发完成后按 `region_nodes` 原顺序排序输出
  - 触发：外部脚本/工具对 `nodes` 数组顺序有依赖

### R3 — restore 频率精简导致检测项间外部切换被延迟恢复
  - 影响：优化项 2
  - 缓解：节点级 restore 仍能恢复节点检测完成后的外部切换
  - 触发：用户在脚本运行期间手动切节点

### R4 — 线程池引入新依赖或 import
  - 影响：优化项 1、3
  - 缓解：仅使用 stdlib `concurrent.futures.ThreadPoolExecutor`
  - 触发：无

### R5 — HTTP 客户端重构引入行为差异
  - 影响：优化项 4
  - 缓解：完整回归测试 + 真实环境对比
  - 触发：JSON 字段值变化或退出码变化

## 待确认事项（已决策）

1. **默认并发数**：
   - 默认值：`16`。
   - 决策依据：mihomo 端 Unix Socket 不存在 TCP 拥塞/握手开销，理论上能支撑更高并发；16 是经验上安全的起步值。
   - 软警告：脚本启动时若检测到 mihomo `/version` 较旧（< 1.18）或响应中包含 `x-mihomo-build` 标记为社区版时，输出 warning 日志（不阻断），建议用户使用 `--concurrent 8` 重试。
   - 回退路径：见「前置验证 / 降级阶梯」，压测不达标时由用户自行改 argparse 默认值。

2. **`--concurrent` 参数范围**：
   - 硬约束：`[1, 32]`，超出范围 argparse 报错退出（退出码 2）。
   - 决策依据：32 已远超典型代理池规模（实测 32 节点），再大无意义；硬约束避免用户误传巨大值导致 mihomo 过载。

3. **优化项 2 修订 line 438**：
   - 状态：已写入文档。
   - 实施时需同步在 `check_us_proxy_status_requirements.md` line 438 处加注：
     ```
     <!-- 修订：见 docs/detection_speed_optimization_requirements.md 优化项 2 章节 -->
     ```
   - 由用户在实施时手工加注（不修改原文档正文）。

4. **优化项 4（HTTP 客户端重构）**：
   - 状态：纳入本轮。
   - 决策依据：收益虽小（< 100ms），但改造面集中在 `MihomoUnixClient` 一个类，不影响业务逻辑；保持实施完整便于统一回归测试。
   - 备注：若用户实施时发现改造复杂度超预期，可推迟到下个迭代（不阻塞 P0/P1/P2 验收）。

5. **mihomo 并发压测执行方**：
   - 状态：由用户执行。
   - 决策依据：遵循项目铁律"只写文档，不改代码"；压测涉及构造 HTTP 请求脚本，属于代码范畴。
   - 文档已给出压测方法（前置验证章节）；用户可在 1-2 分钟内完成。
   - 兜底：若用户希望 AI 代为压测，可由用户显式发起新一轮任务。

## 与原文档的协作关系

- 本文档补充 `check_us_proxy_status_requirements.md` 的检测速度优化能力。
- 原文档的基础功能（地区过滤、目标 API 检测、auto-switch 策略、防意外切换保护等）完全沿用。
- 文档结构与 `docs/region_select_requirements.md` / `docs/auto_switch_optimization_requirements.md` 一致，作为独立补充文档存在。
- 实施时由用户根据本计划自行修改 `check_us_proxy_status.py`。
- 本优化不修改任何 JSON 字段名，仅在 `auto_switch` 顶层新增 `concurrent` 字段。
- 本优化修订原需求 line 438（防意外切换保护机制第 5 步）：
  - 原条款：每个节点的每个检测动作完成后，重新读取 `/proxies` 并对比恢复。
  - 修订后：每个节点的所有检测动作完成后统一读取 `/proxies` 并对比恢复。
  - 修订原因：原条款在并发场景下会导致 restore 频率放大。
