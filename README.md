# 科研受试样品盲法编码与揭盲管理系统

基于 **Python (Flask) + SQLite** 的盲法编码 Web 应用：负责人录入分层因素、处理组及各层样品数量，系统按固定随机种子生成不含组别含义的样品码；映射（盲底）仅授权可读；支持样品损毁补发（原码作废留痕、新码继承原分层与处理组、请求标识幂等）；支持双人复核揭盲并留存含完整补发链的快照。

## 业务规则

| 规则 | 实现 |
| --- | --- |
| 样品码不含组别含义 | 码形如 `SMP-XXXXXXXX`（去除易混淆字符），分层/组别分配经种子随机打乱 |
| 固定随机种子 | 由 `(研究种子, 研究名, 分层, 处理组, 数量)` 确定性生成，结果可复现 |
| 编码一经发放不可复用 | `codes.code` 全局唯一约束；生成前预检冲突 |
| 复用已发放码 → 整批拒绝 | 任一编码冲突即整批回绝（HTTP 409），返回全部冲突码列表，不落库 |
| 缺少样品 / 数量不平衡 → 拒绝生成 | 每层数量缺失、为 0、或不能被处理组数整除时拒绝（HTTP 400）并说明原因 |
| 映射仅授权可读 | 揭盲前仅 `admin` 可读；揭盲后对所有登录用户公开 |
| 揭盲双人复核 | 需两位**不同**操作者的账号+密码（角色须为 `admin`/`operator`）+ 完整理由（≥ `MIN_REASON_LENGTH` 字符） |
| 揭盲留痕 | 成功后在单事务内写入：时间、两位操作者、理由、完整映射快照（含状态与补发链，JSON），研究状态置为 `unblinded` |
| 条件不满足不改变编码 | 所有校验先于任何写入，失败仅返回原因 |
| 样品损毁补发（管理员） | 凭**当前有效样品码 + 补发原因 + 请求标识**办理：原码立即作废（保留历史），新码继承原分层与处理组 |
| 补发码全局不重复 | 补发码由加密级随机源生成并与全部已发放码（含已作废码）去重，`codes.code` 唯一约束兜底 |
| 补发幂等 | 同一研究内相同 `request_id` 重试返回原补发结果（揭盲后亦然）；改换样品码或原因则拒绝（409） |
| 补发并发安全 | 不同请求并发补发同一有效码仅成功一次（写事务串行 + 条件更新），其余返回“已作废”（400） |
| 补发前置条件 | 码不存在、已作废、原因/请求标识空白、研究已揭盲：新请求一律失败且不改变数据 |
| 补发可见性 | 研究详情与样品码列表标明有效/作废状态与替换关系；普通用户揭盲前仍看不到处理组；盲底与揭盲快照保留完整补发链 |

## 快速启动（Docker Compose，一键）

```bash
docker compose up --build
```

访问地址：**http://localhost:8000**

SQLite 数据持久化在命名卷 `blind-data`（容器内 `/data/blind.db`）。

## 本地开发

```bash
pip install -r requirements.txt
python run.py          # http://localhost:8000
pytest tests/          # 运行测试（21 个用例）
```

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SECRET_KEY` | `change-me-in-production` | Flask 会话密钥，**生产必须覆盖** |
| `DATABASE` | 容器 `/data/blind.db`；本地 `instance/blind.db` | SQLite 文件路径 |
| `DEFAULT_SEED` | `20260101` | 系统默认随机种子（创建研究时可逐研究覆盖，创建后固定） |
| `MIN_REASON_LENGTH` | `10` | 揭盲理由最短字符数 |
| `ADMIN_PASSWORD` | `admin123` | 初始管理员密码 |
| `OPERATOR1_PASSWORD` / `OPERATOR2_PASSWORD` | `op123456` / `op234567` | 初始操作员密码 |
| `VIEWER_PASSWORD` | `view1234` | 初始访客密码 |

## 内置账号（仅开发默认值，生产请用环境变量覆盖）

| 账号 | 角色 | 权限 |
| --- | --- | --- |
| `admin` | 管理员 | 建研究、生成样品码、查看映射、可担任揭盲操作者 |
| `operator1` / `operator2` | 操作员 | 可担任揭盲操作者 |
| `viewer` | 访客 | 查看样品码（看不到组别） |

## 使用流程

1. `admin` 登录 → **新建研究**：填写研究名、种子（可留空用默认）、处理组（逗号分隔）、分层（每行 `分层名=数量`）。
2. 研究详情页 → **生成样品码**：系统校验平衡性与冲突后整批发放；失败仅显示原因。
3. 样品码列表对非授权用户隐藏组别；`admin` 可查看映射。
4. 样品损毁时 `admin` 在研究详情页「样品损毁补发」卡片（或调用 API）凭**当前有效样品码、补发原因、请求标识**办理：原码作废保留历史，新码继承原分层与处理组；相同请求标识可安全重试。
5. **揭盲**：两位不同操作者分别输入账号密码 + 完整理由 → 成功后生成揭盲事件（时间/操作者/理由/映射快照/补发链），研究变为「已揭盲」，映射向所有登录用户公开。

## JSON API 摘要

```
POST /api/login                        {username, password}
GET  /api/studies                      研究列表
POST /api/studies                      {name, seed, groups[], strata[{name,count}]}   (admin)
POST /api/studies/<id>/generate        生成样品码；409=冲突(含 conflicts 列表)，400=不平衡/缺数量   (admin)
GET  /api/studies/<id>/codes           样品码（未授权时 group_name 为 null）；含 status / replaced_by_code / replacement_of_code
GET  /api/studies/<id>/mapping         映射（仅授权；揭盲后全员）
POST /api/studies/<id>/replacements    样品损毁补发 {old_code, reason, request_id}   (admin)
GET  /api/studies/<id>/replacements    补发链；未授权时 group_name 为 null
POST /api/studies/<id>/unblind         {operator1,password1,operator2,password2,reason}
GET  /api/studies/<id>/unblinding-events   揭盲事件与快照（mapping + replacements）
GET  /api/health                       健康检查
```

### 样品损毁补发（`POST /api/studies/<id>/replacements`，管理员）

请求体：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `old_code` | 是 | 当前**有效**的样品码；不存在或已作废 → 400 |
| `reason` | 是 | 补发原因，空白 → 400 |
| `request_id` | 是 | 客户端幂等键（仅在同一研究内唯一）；空白 → 400 |

行为：

- 首次成功返回 **201**，`replayed=false`；原码置 `voided` 并指向新码，新码继承原 `stratum`/`group_name`。
- 同研究相同 `request_id` 且 `old_code`/`reason` 完全一致时重试返回 **200**，`replayed=true`，`replacement` 为原记录（揭盲后重试亦然）。
- 相同 `request_id` 但 `old_code` 或 `reason` 不一致 → **409**，不改变数据。
- 码不存在、已作废、原因空白、请求标识空白、研究已揭盲 → **400**，不改变数据。
- 不同 `request_id` 并发补发同一有效码：仅一个 201，其余 400。
- 非管理员调用 → 403；未登录 → 401。

`GET /api/studies/<id>/codes` 中每个码新增（既有字段全部保留）：

- `status`：`active` / `voided`
- `replaced_by_code`：该码作废后被哪个新码替换（否则 `null`）
- `replacement_of_code`：该补发码替换的原码（原批次码为 `null`）

补发码 `batch_id` 为 `null`（不属于任何生成批次）。

示例：

```bash
curl -c cj -X POST localhost:8000/api/login -H 'Content-Type: application/json' \
     -d '{"username":"admin","password":"admin123"}'
curl -b cj -X POST localhost:8000/api/studies -H 'Content-Type: application/json' \
     -d '{"name":"ABC-001","seed":42,"groups":["试验药","安慰剂"],
          "strata":[{"name":"中心1","count":10},{"name":"中心2","count":10}]}'
curl -b cj -X POST localhost:8000/api/studies/1/generate

# 样品损毁补发（request_id 由调用方生成，重试时保持不变）
curl -b cj -X POST localhost:8000/api/studies/1/replacements \
     -H 'Content-Type: application/json' \
     -d '{"old_code":"SMP-QG7U6WX8","reason":"样品运输途中损毁无法检测",
          "request_id":"req-20261006-001"}'
# 201 -> {"ok":true,"replayed":false,
#         "replacement":{"old_code":"SMP-QG7U6WX8","new_code":"SMP-GFBQDRTT",
#                        "stratum":"中心1","group_name":"试验药","reason":"...",
#                        "request_id":"req-20261006-001", ...}}

# 相同请求标识重试（网络超时后安全重放）：返回同一条记录，200 + "replayed":true
curl -b cj -X POST localhost:8000/api/studies/1/replacements \
     -H 'Content-Type: application/json' \
     -d '{"old_code":"SMP-QG7U6WX8","reason":"样品运输途中损毁无法检测",
          "request_id":"req-20261006-001"}'

# 查看含状态与替换关系的码列表 / 补发链
curl -b cj localhost:8000/api/studies/1/codes
curl -b cj localhost:8000/api/studies/1/replacements

curl -b cj -X POST localhost:8000/api/studies/1/unblind -H 'Content-Type: application/json' \
     -d '{"operator1":"operator1","password1":"op123456",
          "operator2":"operator2","password2":"op234567",
          "reason":"受试者发生严重不良事件，需紧急揭盲以指导救治"}'
```

> 幂等建议：`request_id` 可使用业务工单号或 UUID（如 `replace-<study>-<code>-<uuid>`）。
> 更换样品码或补发原因时必须使用**新的** `request_id`。

## 目录结构

```
app/
  __init__.py     应用工厂与配置
  db.py           SQLite 连接、建表、内置账号（含旧库平滑迁移）
  auth.py         登录会话、角色装饰器、口令校验
  services.py     核心逻辑：编码生成 / 冲突预检 / 平衡校验 / 损毁补发 / 双人揭盲
  routes.py       页面路由 + JSON API
  templates/      页面模板
  static/style.css
run.py            开发入口（gunicorn 亦指向 run:app）
tests/test_app.py 规则测试（21 例）
Dockerfile / docker-compose.yml
```
