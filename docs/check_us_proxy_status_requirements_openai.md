# OpenAI API 目标检测需求说明

文件：`check_us_proxy_status_requirements_openai.md`

## 背景

在代理节点状态检测脚本中新增 OpenAI API 延迟检测目标，用于评估代理节点访问 OpenAI 的可达性和响应速度。

OpenAI API 与 Discord API 在检测机制上完全一致：均通过 mihomo 单节点 delay API 发起 HTTP GET 请求，以返回的 `delay` 字段判断可达性和延迟。

## 验证结果（2026-06-02）

对以下端点进行了可访问性验证，测试环境为直接网络连接（未经过代理）：

| 端点 | HTTP 状态码 | 响应时间 | 适用性 |
|------|-------------|----------|--------|
| `https://api.openai.com/v1/models` | 401 | ~500ms | 推荐 |
| `https://api.openai.com/` | 421 | ~450ms | 可用 |
| `https://api.openai.com/health` | 404 | ~460ms | 可用 |

mihomo `/proxies/{node}/delay` 接口以 `delay` 字段是否存在判断节点可达性，不依赖 HTTP 状态码，因此以上三个端点均可正常工作。选择 `https://api.openai.com/v1/models` 作为默认检测端点，原因如下：

- 401 响应表示服务器正常处理请求，只是缺少身份凭证，符合"不提供 API Key 也能检测网络可达性"的设计目标
- 端点路径明确（`/v1/models`），不同于根路径可能被重定向
- 相比 421/404 响应，401 是最标准的无认证可达性响应

## 目标 API 定义

**默认检测端点**：

```text
openai=https://api.openai.com/v1/models
```

**检测方式**：

```http
GET /proxies/{节点名}/delay?timeout=8000&url=https://api.openai.com/v1/models
```

**状态定义**：

- 成功返回 `delay`：节点可访问 OpenAI API，记录延迟
- 返回错误或超时：节点无法在指定超时内访问 OpenAI API

**延迟等级**：复用现有等级定义

```text
good    delay <= 300ms
slow    301ms <= delay <= 800ms
poor    delay > 800ms
dead    检测失败
```

## 命令行参数

OpenAI 目标通过 `--target` 显式传入，不作为默认目标加载（与 Discord 不同）：

```bash
./check_us_proxy_status.py \
  --target openai
```

**设计理由**：OpenAI API 调用可能产生不必要的网络开销，且不同用户的代理池对 OpenAI 的访问需求不同。按需显式传入是更安全的默认行为。

如需在默认 Discord 之外增加 OpenAI，可传入：

```bash
./check_us_proxy_status.py \
  --target openai \
  --target discord=https://discord.com/api/v10/gateway
```

`--target openai` 是内置别名，等价于 `--target openai=https://api.openai.com/v1/models`。

OpenAI 目标支持所有现有功能：

- 作为多目标检测中的任意一个目标
- 作为 `--switch-check-target` 的判断目标
- 作为自动切换的候选质量排序目标
- 作为 `--confirm-target` 的候选复测目标

## OpenAI 作为自动切换判断目标

当 `--switch-check-target openai` 时，行为与 `discord` 完全一致：

```bash
./check_us_proxy_status.py \
  --auto-switch-if-current-not-good \
  --switch-check-target openai \
  --target openai \
  --json
```

效果：

- 当前节点质量由 OpenAI 延迟等级判断（`openai.level` 而非 `base.level`）
- 候选节点按 OpenAI 延迟排序
- OpenAI 不可用（`ok=false`）的候选节点被过滤
- slow/bad 连续确认、切换冷却、候选过滤逻辑完全复用现有实现

## JSON 输出字段

`targets` 字段示例：

```json
{
  "targets": {
    "discord": "https://discord.com/api/v10/gateway",
    "openai": "https://api.openai.com/v1/models"
  }
}
```

`nodes[].targets` 扩展：

```json
{
  "targets": {
    "discord": {"ok": true, "delay_ms": 210, "level": "good", "error": ""},
    "openai": {"ok": true, "delay_ms": 502, "level": "slow", "error": ""}
  }
}
```

`target_alive` / `target_dead` 扩展：

```json
{
  "target_alive": {
    "discord": [...],
    "openai": [...]
  },
  "target_dead": {
    "discord": [...],
    "openai": [...]
  }
}
```

`current_node.targets` 扩展同 `nodes[].targets` 结构。当 `switch_by` 为 `target:openai` 时，`current_node.switch_by` 和 `current_node.switch_level` 反映 OpenAI 目标的状态。

`auto_switch.candidate_quality_target` 为 `openai` 时，`candidate_filter` 和候选排序均基于 OpenAI 延迟。

## 注意事项

1. **不需要 Authorization**：检测端点 `/v1/models` 在无 API Key 时返回 401，mihomo 的 delay 接口仍会返回 `{"delay": N}`，因此检测不受影响
2. **不反映 API Key 有效性**：检测的是 OpenAI API HTTP/TLS 入口可达性，不代表 API Key 有效或配额充足
3. **不反映特定模型可用性**：不检测 `/v1/chat/completions` 等需要 API Key 的业务接口
4. **Azure OpenAI**：不在本需求范围内，如有需要可通过 `--target azure_openai=https://<endpoint>.openai.azure.com/` 形式单独添加

## 验收标准

**基础检测**：

```bash
./check_us_proxy_status.py \
  --target openai \
  --json
```

验证项：

- `targets.openai` 字段存在且值为 `https://api.openai.com/v1/models`
- `nodes` 中每个节点的 `targets.openai` 字段存在
- `target_alive.openai` 和 `target_dead.openai` 字段存在
- `target_alive.openai` 数量 + `target_dead.openai` 数量 = `region_nodes_count`
- `openai` 延迟等级字段 `level` 正确（good/slow/poor/dead）
- 当能识别当前节点时，`current_node.targets.openai` 字段存在且结构为 `{"ok": true/false, "delay_ms": N, "level": "good/slow/poor/dead", "error": ""}`
- 当所有节点都可达 OpenAI 时，`target_dead.openai` 应为空数组 `[]`；当所有节点都不可达时，`target_alive.openai` 应为空数组 `[]`；两字段在任意情况下均应存在

**自动切换（OpenAI 判断目标）**：

```bash
./check_us_proxy_status.py \
  --auto-switch-if-current-not-good \
  --switch-check-target openai \
  --target openai \
  --json
```

验证项：

- `check_target` = `openai`
- `candidate_quality_target` = `openai`
- `current_node.switch_by` = `target:openai`
- 切换逻辑与 Discord 目标行为一致（slow/bad 确认、冷却、候选过滤）
- `auto_switch` 结构中所有子字段与 OpenAI 目标正确关联
- `auto_switch.candidate_confirmation.target` = `openai`

**候选确认（OpenAI 目标）**：

```bash
./check_us_proxy_status.py \
  --auto-switch-if-current-not-good \
  --switch-check-target openai \
  --confirm-candidate \
  --confirm-target openai \
  --target openai \
  --json
```

验证项：

- `switch_policy.confirm_target` = `openai`
- `auto_switch.candidate_confirmation.enabled` = `true`
- `auto_switch.candidate_confirmation.target` = `openai`
- 候选复测使用 OpenAI 目标而非 discord 或 base，复测结果与 OpenAI 延迟正确关联
