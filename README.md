# 科研受试样品盲法编码与揭盲管理系统

基于 **Python (Flask) + SQLite** 的盲法编码 Web 应用：负责人录入分层因素、处理组及各层样品数量，系统按固定随机种子生成不含组别含义的样品码；映射（盲底）仅授权可读；支持双人复核揭盲并留存快照。

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
| 揭盲留痕 | 成功后在单事务内写入：时间、两位操作者、理由、完整映射快照（JSON），研究状态置为 `unblinded` |
| 条件不满足不改变编码 | 所有校验先于任何写入，失败仅返回原因 |

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
pytest tests/          # 运行测试（11 个用例）
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
4. **揭盲**：两位不同操作者分别输入账号密码 + 完整理由 → 成功后生成揭盲事件（时间/操作者/理由/映射快照），研究变为「已揭盲」，映射向所有登录用户公开。

## JSON API 摘要

```
POST /api/login                        {username, password}
GET  /api/studies                      研究列表
POST /api/studies                      {name, seed, groups[], strata[{name,count}]}   (admin)
POST /api/studies/<id>/generate        生成样品码；409=冲突(含 conflicts 列表)，400=不平衡/缺数量   (admin)
GET  /api/studies/<id>/codes           样品码（未授权时 group_name 为 null）
GET  /api/studies/<id>/mapping         映射（仅授权；揭盲后全员）
POST /api/studies/<id>/unblind         {operator1,password1,operator2,password2,reason}
GET  /api/studies/<id>/unblinding-events   揭盲事件与快照
GET  /api/health                       健康检查
```

示例：

```bash
curl -c cj -X POST localhost:8000/api/login -H 'Content-Type: application/json' \
     -d '{"username":"admin","password":"admin123"}'
curl -b cj -X POST localhost:8000/api/studies -H 'Content-Type: application/json' \
     -d '{"name":"ABC-001","seed":42,"groups":["试验药","安慰剂"],
          "strata":[{"name":"中心1","count":10},{"name":"中心2","count":10}]}'
curl -b cj -X POST localhost:8000/api/studies/1/generate
curl -b cj -X POST localhost:8000/api/studies/1/unblind -H 'Content-Type: application/json' \
     -d '{"operator1":"operator1","password1":"op123456",
          "operator2":"operator2","password2":"op234567",
          "reason":"受试者发生严重不良事件，需紧急揭盲以指导救治"}'
```

## 目录结构

```
app/
  __init__.py     应用工厂与配置
  db.py           SQLite 连接、建表、内置账号
  auth.py         登录会话、角色装饰器、口令校验
  services.py     核心逻辑：编码生成 / 冲突预检 / 平衡校验 / 双人揭盲
  routes.py       页面路由 + JSON API
  templates/      页面模板
  static/style.css
run.py            开发入口（gunicorn 亦指向 run:app）
tests/test_app.py 规则测试（11 例）
Dockerfile / docker-compose.yml
```
