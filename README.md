# PM2.5 小时数据每日发布 API

环保监测站向分析人员发布每日 PM2.5 小时数据的服务。技术栈：**FastAPI + SQLite + 本地文件存储**，使用 **Docker Compose** 一键启动。

## 功能与业务规则

### 数据上传（按监测点 + 日期）

`POST /stations/{station_id}/dates/{data_date}/uploads`，multipart 表单：

| 字段 | 说明 |
|---|---|
| `file` | CSV 文件，表头 `hour,pm25`（兼容 `小时,浓度`、`pm2.5` 等别名） |
| `expected_version` | 当前版本号；首次上传传 `0`（默认），补报必须传当前版本号 |

**整批校验规则（任一不满足则整批拒绝，不发布任何记录，HTTP 422 并逐条给出原因）：**

- 必须恰好包含小时 `0～23` 共 24 条记录；
- 小时不重复、不缺失（响应中逐个列出缺失小时及重复行所在行号）；
- 浓度必须是非负有限数（拒绝负数、`NaN`、`Inf`、空值、非数值）；
- 列数必须恰好为 2，小时必须是 0..23 的整数。

### 版本与补报

- 每个 `(监测点, 日期)` 从版本 1 开始单调递增；
- 补报携带当前版本号，校验通过后**整批替换**为新版本，旧版本数据与原始 CSV 均保留；
- 版本号不匹配返回 `409 version_conflict`，并回带当前版本号；
- 并发补报通过 SQLite `BEGIN IMMEDIATE` 写事务串行化，**保证只有一份生效**。

### 查询与文件留存

| 接口 | 用途 |
|---|---|
| `GET /stations/{station_id}/dates/{data_date}/records` | 当前版本的 24 条小时记录 + 文件标识 `file_id` |
| `GET /stations/{station_id}/dates/{data_date}/versions` | 全部历史版本（含每版 `file_id`、文件路径、校验和） |
| `GET /stations/{station_id}/dates/{data_date}/versions/{version}/file` | 下载指定版本留存的原始 CSV（字节一致） |
| `GET /health` | 健康检查 |

### 存档路径安全与历史版本恢复

- **新上传**的 `station_id` 必须是合法且可作为单个路径片段的标识；`.` / `..`
  会被拒绝（HTTP 400），任何情况下都不会把 CSV 写到留存目录 `files/` 之外。
  写入层 `db.persist_csv_file` 还会再次解析并校验最终路径，双重防护。
- **修复前已发布**的越界版本（当初 `station_id` 误填为 `..`）仍然：
  - 可通过 `records` / `versions` 正常查到版本记录与校验和；
  - 可通过原下载接口取回**字节一致**的原始 CSV。
- 下载时会按 `(station, date, version, file_id)` 定位历史原文件并核对登记校验和，
  **不会使用数据库里的 `file_path` 去读取任意文件**。校验通过后把存档复制回
  `files/_recovered/<编码后的站点>/<日期>/`，更新登记后再清理目录外的旧副本。
- 原文件缺失返回 `410 archive_missing`；校验和不吻合返回 `422 archive_corrupt`；
  两种情况都不会改动版本登记，也不会产生新的存档。
- 复制或数据库更新失败返回 `500 archive_recovery_failed` 且 `"retryable": true`；
  原文件与版本登记保持原样，稍后重试即可。同一版本的并发恢复由按版本加的文件锁
  串行化，不会错绑文件。

### 失败安全保证

- 原始 CSV 先写入临时文件，再 `os.replace` 原子落到**不可变路径**
  （`files/<station>/<date>/v<version>_<file_id>.csv`），旧文件永不被覆盖；
- 文件写入成功后才开启数据库事务，版本元数据与 24 条记录在同一事务中提交；
- 任何文件或数据库写入失败都会回滚，**上一版本仍完整可查**（测试覆盖）。

## 目录结构

```
app/
  config.py       # 数据目录配置（环境变量 PM25_DATA_DIR）与路径片段校验
  validation.py   # CSV 解析与整批校验（错误行/缺失小时/原因）
  db.py           # SQLite 模式、乐观版本控制、事务与文件原子落盘
  archive.py      # 安全存档路径、校验和核验、越界历史文件恢复
  main.py         # FastAPI 路由与错误处理
tests/            # 49 个 pytest 用例（校验/API/并发/故障恢复/存档恢复）
Dockerfile
docker-compose.yml
requirements.txt
```

## 启动方式

```bash
docker compose up --build
# 服务监听 http://localhost:8000，交互式文档 http://localhost:8000/docs
```

SQLite 数据库、留存 CSV、临时文件统一存放在命名卷 `pm25-data`（容器内 `/data`）。

## 本地开发与测试

```bash
pip install -r requirements.txt
pip install pytest httpx
PM25_DATA_DIR=./data python -m uvicorn app.main:app --reload
python -m pytest tests/ -q
```

## 使用示例

```bash
# 首次上传
curl -X POST http://localhost:8000/stations/ST-001/dates/2026-11-14/uploads \
  -F "expected_version=0" -F "file=@2026-11-14.csv"

# 查询当前版本
curl http://localhost:8000/stations/ST-001/dates/2026-11-14/records

# 补报（携带当前版本号 1）
curl -X POST http://localhost:8000/stations/ST-001/dates/2026-11-14/uploads \
  -F "expected_version=1" -F "file=@2026-11-14-fixed.csv"

# 下载历史版本原始 CSV
curl -OJ http://localhost:8000/stations/ST-001/dates/2026-11-14/versions/1/file
```

## 校验失败响应示例

```json
{
  "error": "validation_failed",
  "message": "整批数据未通过校验，没有任何记录被发布",
  "reasons": [
    {"reason": "missing hour", "hour": 23},
    {"reason": "duplicate hour", "line": 25, "hour": 0, "first_line": 2},
    {"reason": "pm25 concentration must be non-negative", "line": 8, "value": -1.2}
  ]
}
```
