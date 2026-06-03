# 地区节点选择功能需求说明

文件：`check_proxy_status.py`（兼容入口：`check_us_proxy_status.py` 等价于默认 `--region us`）

## 背景

现有 `check_us_proxy_status_requirements.md` 中，节点地区过滤逻辑硬编码为美国关键词。本文档定义可配置地区选择功能，允许用户通过 `--region` 参数指定要检测的地区。

## 目标

1. 支持通过 `--region` 参数指定地区，可选值：`us`（默认）、`sg`、`uk`、`jp`、`hk`、`de`、`fr`
2. 内置地区关键词配置表，支持扩展
3. `--region us` 行为与现有硬编码美国节点过滤逻辑完全一致
4. 无效 `--region` 值时报错退出，退出码 1

## 内置地区关键词配置表

| 地区代码 | 节点名称关键词 |
|---------|--------------|
| us | 🇺🇸, 美国, US, USA, United States |
| sg | 新加坡, SG, Singapore |
| uk | 英国, UK, United Kingdom, England |
| jp | 日本, JP, Japan |
| hk | 香港, HK, Hong Kong |
| de | 德国, DE, Germany |
| fr | 法国, FR, France |

配置表为内置只读，扩展需修改代码。

## --region 参数

```text
--region   指定地区，如 us/sg/uk/jp/hk/de/fr 等，默认 us；无效值报错退出
```

行为定义：

- 可选值：`us`, `sg`, `uk`, `jp`, `hk`, `de`, `fr`（大小写不敏感，`US`/`Us`/`us` 均合法，统一规范为小写存储）
- 默认值：`us`
- 无效值时：输出错误信息 `--region: invalid value '{value}'`，退出码 1，不执行检测
- `--region us` 的过滤逻辑与原硬编码的美国节点过滤完全一致

## 节点过滤逻辑

从 `/proxies` 返回的完整代理池中，按以下规则过滤指定地区节点：

1. 节点名称包含 `--region` 对应地区关键词列表中任一关键词
2. 排除策略组类型：`Selector`, `URLTest`, `Fallback`, `LoadBalance`, `Relay`
3. 排除特殊节点：`DIRECT`, `REJECT`, `REJECT-DROP`, `PASS`, `COMPATIBLE`

最终只对真实代理节点测速。

## 命令行参数新增

在 `check_us_proxy_status_requirements.md`「命令行参数」章节中新增：

```text
--region                      指定地区，如 us/sg/uk/jp/hk/de/fr 等，默认 us；无效值报错退出
```

## JSON 输出变更

### 新增顶层字段

在 JSON 顶层新增 `region` 字段，说明实际使用的地区代码：

```json
{
  "source": "/proxies full pool",
  "region": "us",
  "filter": "name contains {region_keywords}; excludes strategy groups and special nodes",
  ...
}
```

### 字段名变更

`us_nodes_count` → `region_nodes_count`

### filter 字段动态化

`filter` 字段内容根据 `--region` 值动态生成：

| --region | filter 示例 |
|----------|------------|
| us | `name contains 🇺🇸 / 美国 / US / USA / United States; excludes strategy groups and special nodes` |
| sg | `name contains 新加坡 / SG / Singapore; excludes strategy groups and special nodes` |
| uk | `name contains 英国 / UK / United Kingdom / England; excludes strategy groups and special nodes` |
| jp | `name contains 日本 / JP / Japan; excludes strategy groups and special nodes` |
| hk | `name contains 香港 / HK / Hong Kong; excludes strategy groups and special nodes` |
| de | `name contains 德国 / DE / Germany; excludes strategy groups and special nodes` |
| fr | `name contains 法国 / FR / France; excludes strategy groups and special nodes` |

## 验证要求

在 `check_us_proxy_status_requirements.md`「当前验证要求」章节中新增：

```text
region 字段存在且值与 --region 参数一致（默认 "us"）
region_nodes_count > 0
```

## 与原文档的协作关系

- 本文档补充 `check_us_proxy_status_requirements.md` 的地区选择能力
- 原文档的基础功能（自动切换、目标 API 检测、防意外切换保护等）在本功能中完全沿用
- `--region` 参数与所有现有参数兼容（`--auto-switch-if-current-not-good`、`--json` 等）
- auto-switch 模式下，候选节点扫描范围限定为指定地区

### auto-switch 与 --region 组合行为

- 当前节点识别仍按原文档规则执行，不受 `--region` 影响
- 如果当前节点不属于指定地区，auto-switch 不执行切换，只输出原因
- 候选节点扫描仅在指定地区内进行

当前实现文件：

```text
check_proxy_status.py
check_us_proxy_status.py  # 兼容入口
```