# 地址责任账（M8）设计说明

## 1 要解决的问题

一个公开地址先由市府门户（如 `site-a` 河州市人民政府）登记，下季度转由部门站
（如 `site-b` 政务服务局）维护。两种朴素做法都会坏账：

- **按当前归属重算历史**：切换前发布的缺陷整笔换站，原整改记录失去承接；
- **锁死首次归属**：切换后产生的新观测记不到维护部门，新缺陷无人认领。

本模块在不拆分同址的前提下，给每个地址保存一条**带生效时点的责任变更链**，
让「旧账只读、新账归新站」同时成立。

## 2 核心模型

### 2.1 责任变更表 `responsibility_changes`

一个地址一条变更链，按 `effective_at` 排序；同一地址同一生效时点只允许一条
（UNIQUE 约束兜底，重放登记不产生第二行）。

| 字段 | 含义 |
|---|---|
| `change_id` | `rc-` + 内容哈希前 12 位，同内容同号（幂等登记的直接依据） |
| `url_key` | 规范化后的比较键，与巡检同一把键，不拆同址 |
| `from_site` / `to_site` | 来源站 / 接管站，必须在站点清单内 |
| `effective_at` | 生效时点，UTC 秒级字符串（ISO 8601 可直接字符串比较） |
| `basis` | 变更依据（公文号/移交单），随责任快照保留，旧账可追溯 |
| `status` | `pending` 待生效 / `active` 已生效 / `failed` 切换失败 |
| `applied_at` | 迁移器实际生效时间；`pending` 时为空 |
| `failure_reason` | 切换失败原因；成功时为空 |
| `created_at` / `updated_at` | 登记与最近变更时间 |

### 2.2 时点归属解析（纯函数，不发请求）

`resolve(url_key, as_of)` 取该地址所有 `active` 且 `effective_at <= as_of` 的变更，
选 `effective_at` 最大的一条（再同点取 `change_id` 最大，保证全序确定）：

- 命中 → 责任站 = `to_site`，依据 = `basis`，依据类型 `transfer`；
- 未命中 → 责任站 = 主机归属站（`scope.site_of`），依据 = 主机清单，依据类型 `host`。

`pending` 与 `failed` 的变更**不参与**解析：切换没成功就保留旧账。

### 2.3 旧账只读、新账归新站

责任站在**巡检批次生成时按批次开始时间解析一次，快照进 run payload**，之后不再变：

- 历史批次的缺陷行冻结在它发布时的责任站，事后再登记/撤销变更都不会改写它；
- 切换生效后跑的新批次，同址观测解析到新责任站，新缺陷归新站；
- 地址始终只有一条 key、一条探测行、一条缺陷行，**绝不拆成两笔**。

每条缺陷同时保留：

| 字段 | 含义 |
|---|---|
| `site` | 来源站（哪个站的目录/sitemap 采到的），维持原语义不变 |
| `responsible_site` | 责任站（时点解析结果，快照） |
| `responsibility_basis` | 责任依据：`host` 主机清单 / `transfer` 移交变更 |
| `responsibility_ref` | 移交变更号 `rc-xxxx` 或主机名；`basis` 原文保留在责任账可查 |

## 3 切换流程与失败保留

1. `POST /api/responsibility/changes` 登记一条 `pending` 变更（校验站点、地址、时点格式）。
2. 迁移器 `apply_pending(as_of)` 在一个 SQLite 事务内处理所有到期
   （`effective_at <= as_of`）的 `pending` 变更：
   - 目标站仍在清单、地址仍可规范化且在范围内 → 置 `active`、写 `applied_at`；
   - 校验不过 → 置 `failed`、写 `failure_reason`，**归属解析不命中它，旧账原样保留**。
3. 迁移在每次巡检 `full` 启动时先跑一次；也可 `POST /api/responsibility/apply` 手动触发。

### 3.1 幂等：重跑或续跑不重复迁移

- 迁移器只挑 `status = 'pending'` 的行，已 `active`/`failed` 的永不再处理；
- 同一变更内容生成同一 `change_id`，登记接口遇同号直接返回既有行，不新增；
- 整批处理放在单事务里，任一条抛错整体回滚，不产生半生效状态；
- 续跑（resume）不重新迁移，只复用批次开始时冻结的责任快照。

## 4 责任版本与批次键

责任版本 `responsibility_version` = 对当前所有 `active` 变更（按 `url_key`、
`effective_at`、`change_id` 排序后）取 sha256，前 12 位；无变更时为 `none`。

批次键纳入责任版本：

```
batch_key = sha256(站点集合 | input_digest | rule_version | responsibility_version)
```

因此同一份样例站字节，在责任账变化后重跑会得到**新批次、新 run_id**，新批次按新责任
出账；而责任账不动时重跑仍命中旧批次键直接复用，天然幂等。历史批次行不受影响。

## 5 页面与导出同一责任版本

页面（缺陷单、可达性、对账、变更监测）、CSV、运行清单 JSON、快照 JSON 全部从同一份
run payload 的责任快照取 `responsible_site`，不各自重算：

- 缺陷 CSV 增列「责任站」「责任依据」，列顺序追加在末尾，不破坏既有表头前缀契约；
- 缺陷单页面同时显示来源站标签与责任站标签；
- 运行清单写入 `responsibility_version` 与本次应用的变更清单，可复核；
- 监测快照的 `site` 改取责任站，跨季度趋势按责任站归集。

## 6 新增入口

| 动作 | 入口 |
|---|---|
| 登记责任变更 | `POST /api/responsibility/changes` |
| 到期迁移（手动） | `POST /api/responsibility/apply` |
| 变更链查询 | `GET /api/responsibility/changes`、`GET /api/responsibility/changes/{change_id}` |
| 时点归属试算 | `GET /api/responsibility/resolve?url=...&as_of=...` |
| 责任账页面 | `/responsibility` |

## 7 与既有规则的边界

- 责任账只改「账记在哪个站」，不改探测状态码、缺陷分类、四账等式；四账仍按 key 守恒。
- 来源站 `site` 字段语义保持不变，所有既有按来源站的筛选/统计继续可用。
- 责任变更不制造新 key：同址的来源证据（catalog/sitemap 多行）仍折叠在同一 slot。
