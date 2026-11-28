# 业务办理服务运行文档

本服务在本地办理**合成影视作品**面向**合作平台**的保护通知，覆盖提交、法务核准、
平台送达跟进、补件、上传者异议独立复核与状态查询。所有人员、作品、链接、片段与
权利文件均为演练数据：

- 不抓取真实网站，不识别侵权内容，不代替法务作法律结论；
- 作品、链接、片段范围和权利文件由有权限的人提交；
- 只办理本地合成作品与已登记的合作平台，平台协议差异（地区/片段 vs 整项）原样保留。

## 1. 角色与职责边界

| 角色 | 可执行操作 | 明确禁止 |
| --- | --- | --- |
| 代理 `agent` | 建案、提交保护请求、补件、撤回本人请求、补交继续维持文件 | 不能自批；只能使用委托给本人的授权 |
| 法务 `legal` | 核准/驳回/退件、授权冲突确认、撤销委托、登记主数据 | 不能核准自己提交的请求；核准不得超出请求与有效委托范围 |
| 联络人 `liaison` | 接收异议材料、登记送达与平台回执、待协商登记 | 不能判断材料有效性，不能作中止/维持/解除决定 |
| 独立复核员 `reviewer` | 异议有效性检查、复核决定 | 不能复核自己参与过原核准的请求 |
| 上传者 `uploader` | 提交异议与异议补件 | 范围不得超出被异议请求 |
| 管理员 `admin` | 主数据引导 | 不参与具体业务决定 |

身份通过请求头 `X-User-ID` 传递（演练身份，非认证凭据）。

## 2. 核心业务规则

1. **授权按地区、时段、媒介拆开。** 委托只在其权利记录的地区/媒介集合与适用期内
   有效；片方拥有部分权利不代表能要求所有平台全球下架。超出范围的申请进入
   `conflict_pending`，由法务显式确认，不能自动扩张。
2. **材料不全先补件。** 缺少权利文件、授权文件或声明的请求停留在
   `pending_supplement`，补齐前不得核准。
3. **法务批准的是具体发布项与限定范围。** 不能把一个预告片片段的核准扩展为整部
   作品；核准范围必须被请求范围包含。相同链接换了内容版本（`content_sha256`
   不同）会生成新版本发布项，原批准不能无条件沿用，须另行申请。
4. **未核准/无有效依据不得向平台发送有效处置。**
5. **受理不等于生效。** 平台 202 受理仅证明收到；平台实际状态以查询回执为准。
6. **受理后连接中断（如 504）只能继续查询原发布项与原处置流水**，不能另发一份
   相反请求覆盖未知结果。
7. **撤回/撤销后的恢复要判断独立依据。** 一位权利人撤诉或授权文件撤销，不恢复
   另一位权利人仍有效保护的范围；恢复范围与他人有效依据重叠时被拒绝。
8. **迟到回执不能重新限制。** 平台“已限制”回执到达时若已无有效依据，记为
   `restricted_no_basis`，计入平台实际范围以暴露差异，但不形成新依据，应转恢复。
9. **历史不可改写。** 曾限制、曾受理等事实以只追加事件保留；文件更正只影响此后
   判断，不能把历史改写成“从未处置”。

### 异议与期限（工作日历 `calendars`）

- 工作日：周一至周五扣除 `holidays`，加上 `make_up_days`；截止时刻为当地 18:00。
- 上传者提出异议后，**两个工作日内**完成材料有效性检查。无效异议必须列明缺项，
  不启动期限；补齐后重新按实际有效送达计算。
- 有效异议**送达原申请人**后才开始倒计时；**未送达不计时**。送达后五个工作日内
  未收到继续维持确认，相应通知失去继续限制依据（自动判定为 `lapsed`）。
- 原申请人在期限内补交继续维持文件的，自补交时刻重新顺延五个工作日。
- 单项异议只影响被主张的内容和地区，不撤同案其他发布项。
- 中止、维持、解除由未参与原核准的复核员决定；已结束异议不能被重复通知重新计时。
- 到期已经发生后，事后的“维持”决定不能恢复已失去的依据。

## 3. 持久化与时间

- 业务状态保存在独立 SQLite 文件（默认 `app-state.sqlite3`），与协作方回放状态
  （`partner-state.sqlite3`）分别保管。
- 所有写入在请求内提交并记录只追加事件（`events`、`delivery_events`）。
- 请求头 `X-Current-Time`（带时区 ISO-8601）可注入“当前时刻”，用于期限与迟到
  回执演练；缺省使用服务器墙钟。看板按该时刻**重建历史状态**，因此可复算任意
  历史时点的应限制范围与平台实际范围。

## 4. HTTP 接口

所有接口返回 JSON；错误返回 4xx 与中文错误体（`error`、`code`）。

### 主数据（admin/legal）

| 方法与路径 | 说明 |
| --- | --- |
| `POST /users` `/works` `/rights` `/mandates` `/platforms` `/publications` `/calendars` `/cases` | 登记人员、作品、权利、委托、平台、发布项、日历、投诉案 |
| `POST /mandates/revoke` | 撤销委托（历史留痕，撤销后依据失效） |
| `GET /users` `/works` `/platforms` `/calendars` `/cases` | 主数据查询 |

### 提交与核准

| 方法与路径 | 说明 |
| --- | --- |
| `POST /notices` | 代理提交保护请求（自动判定补件/冲突/待核准） |
| `POST /notices/supplement` | 请求补件 |
| `POST /notices/withdraw` | 撤回请求 |
| `POST /legal/decisions` | 法务 `approved`/`rejected`/`returned`/`conflict_resolved` |

### 异议与复核

| 方法与路径 | 说明 |
| --- | --- |
| `POST /disputes` | 联络人接收上传者异议 |
| `POST /disputes/validity` | 复核员两日内有效性检查（无效须给 `missing_items`） |
| `POST /disputes/supplement` | 异议补件（补齐重新进入待送达） |
| `POST /disputes/serve` | 有效异议送达原申请人（`served=false` 表示未送达，不计时） |
| `POST /disputes/continuation` | 补交继续维持文件（顺延五日） |
| `POST /disputes/review` | 独立复核决定 `maintain`/`suspend`/`lift`/`restore` |
| `POST /timeouts/evaluate` | 到期自动判定 |

### 送达跟进与查询

| 方法与路径 | 说明 |
| --- | --- |
| `POST /deliveries` | 发起 `restrict`/`restore` 送达（无依据/中断期/越界恢复会被拒） |
| `POST /deliveries/negotiation` | 平台能力不支持粒度时登记待协商 |
| `POST /deliveries/accept` | 登记受理（不代表生效） |
| `POST /deliveries/unknown` | 登记受理后结果不明（504） |
| `POST /deliveries/observe` | 登记平台查询回执（迟到限制自动标记 `restricted_no_basis`） |
| `POST /deliveries/event` | 无回放协议时手工登记平台原生状态 |
| `GET /publications/{id}/board` | 一条链接的应限制/实际范围/差异/责任人 |
| `GET /notices/{id}/trace` | 从法务决定追到送达、异议与后续动作 |
| `GET /notices/{id}` `/disputes/{id}` `/deliveries/{id}` | 单实体查询 |
| `GET /events?entity_type=..&entity_id=..` | 只追加事件轨迹 |

## 5. 本地运行

```sh
# 1) 启动协作方协议回放（独立状态文件，可选）
python3 partner/server.py --port 8765 --state partner-state.sqlite3

# 2) 启动业务服务（首次自动从 data/catalog.json 引导主数据）
python3 -m app.main --port 8080 --state app-state.sqlite3 \
    --replay-base-url http://127.0.0.1:8765

# 3) 健康检查
curl -s http://127.0.0.1:8080/health
```

服务默认仅监听环回地址。重启使用同一 `--state` 文件会保留全部业务数据；
更换全新文件即可开始独立演练。

### 请求示例

```sh
# 代理提交（注入演练时刻）
curl -s -X POST http://127.0.0.1:8080/notices \
  -H 'Content-Type: application/json' \
  -H 'X-User-ID: agent-A' \
  -H 'X-Current-Time: 2026-12-02T10:00:00+08:00' \
  -d '{
    "case_id":"case-1","notice_id":"n-A",
    "publication_id":"post-A","mandate_id":"mandate-A",
    "regions":["east","south"],"media":["streaming"],
    "range_ms":[0,60000],
    "materials":{
      "rights_file_sha256":"r1",
      "authorization_file_sha256":"a1",
      "statement_sha256":"s1"}}'

# 联络人查看一条链接的看板
curl -s -H 'X-Current-Time: 2026-12-10T12:30:00+08:00' \
  http://127.0.0.1:8080/publications/post-A/board
```

## 6. 自动检查

```sh
python3 -m unittest discover -s tests -v
```

检查套件包含：

- `test_mandate.py`：部分权利限制、补件、冲突交法务、不能自批、撤销失效；
- `test_dispute.py`：两日有效性、五日继续维持、未送达不计时、无效列明缺项、
  复核独立性、单项异议范围；
- `test_convergence.py`：**授权撤销、异议期限与迟到平台回执在同一发布项相遇**
  的完整结果，以及一位权利人撤销不得恢复他人有效保护；
- `test_platform.py`：平台整项能力差异、链接换版本不沿用、受理不等于生效、
  历史不可改写；
- `test_http_flow.py`：真实环回 HTTP、角色头、注入时钟、重启后持久化；
- `test_partner_replay.py`：与协作方回放器对接（504 后查询原流水）；
- `test_scopes.py`：半开区间与多 (地区,媒介) 范围运算。

## 7. 状态词表

- 请求：`pending_supplement` / `submitted` / `conflict_pending` / `approved` /
  `rejected` / `withdrawn` / `returned`
- 异议：`pending_validity` / `invalid` / `awaiting_service` / `served` /
  `lapsed` / `ended`
- 送达：`draft` / `accepted` / `unknown` / `pending` / `restricted` /
  `restricted_no_basis` / `partially_restricted` / `rejected` / `negotiation` /
  `restored` / `restore_pending` / `failed`

平台原生状态保存在 `delivery_events.native_state`，与本服务规范状态并存，
协议差异不被抹平。
