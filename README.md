# OCI 镜像清理预演

镜像仓库快满了？本工具帮助值班员安全地回答"退役这批旧标签能腾出多少空间"：
导入全仓盘点 JSON → 勾选待退役标签 → 页面给出仅因此次退役而不再可达的对象与合计字节
（跨仓共享对象只计一次）→ 下载清理清单前用**最新盘点**复核，发现冲突整份拒发。

## 快速开始

```bash
docker compose up --build
# 打开 http://localhost:8000
```

本地开发（Python 3.11+）：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/uvicorn app.main:app --reload
```

运行测试：

```bash
.venv/bin/python -m pytest tests/ -q
```

`samples/` 下有两份示例：`inventory.sample.json`（完整盘点，可直接预演）、
`inventory.incomplete.json`（缺少一个被引用对象，用于演示"暂停发单"）。

## 盘点 JSON 格式

```json
{
  "captured_at": "2026-11-21T07:30:00Z",
  "retained": ["sha256:..."],
  "repositories": [
    {
      "name": "web/frontend",
      "tags": {"v1.0": "sha256:...", "latest": "sha256:..."},
      "retained": ["sha256:..."]
    }
  ],
  "objects": {
    "sha256:...": {"type": "index", "size": 1500, "refs": ["sha256:..."]},
    "sha256:...": {"type": "manifest", "size": 2000, "refs": ["sha256:..."]},
    "sha256:...": {"type": "config", "size": 4000},
    "sha256:...": {"type": "layer", "size": 30000000}
  }
}
```

- `tags`：仓库标签 → 清单/索引摘要。
- `retained`（顶层或仓库级，可选）：按摘要保留的版本，与标签一样作为可达性根。
- `objects`：全仓对象表。`type` ∈ `index|manifest|config|layer`（其它字符串也接受），
  `size` 为字节数，`refs` 为引用边（索引→清单、清单→配置/层）。
- 被引用但不在 `objects` 中的摘要**不算导入错误**，而是标记为"盘点不完整"：
  可正常预演，但清理清单暂停发放，直到导入完整盘点。

## 工作流与复核规则

1. **导入盘点**：每次导入生成一个新盘点快照（历史保留），最新一份为当前盘点。
2. **预演**：勾选标签后，系统计算 `可达(全部标签+保留) − 可达(剩余标签+保留)`，
   即"仅因这批退役而不再可达"的对象；对象表按摘要全局唯一，跨仓共享对象只计一次。
3. **下载清理清单**：下载瞬间用**最新盘点**复核预演结果，任一不满足则整份拒发（HTTP 409）：
   - 所选标签指向有变或已不存在（`tag_moved` / `tag_missing`）；
   - 待删对象在最新盘点中已缺失（`object_missing`）；
   - 待删对象重新可达（`object_reachable`，如被新标签/保留指回）；
   - 最新盘点不完整（`inventory_incomplete`，暂停发单）。
   
   无关变动（其它标签换指向、新增仓库/对象）不影响发单。
4. **清单内容**：只包含预演中的标签及其**原指向**、待删摘要列表，外加少量元数据
   （预演号、生成时间、复核所用盘点号、合计字节）。

## 实现说明

- `app/oci.py`：纯逻辑——盘点解析校验、可达性遍历、删除计划、发单复核，不依赖 Web/DB。
- `app/db.py`：SQLite 持久层（盘点快照、标签、对象、引用边、预演结果）。
- `app/main.py`：FastAPI 路由与页面渲染（Jinja2，无前端构建）。
- `tests/`：pytest + TestClient，覆盖导入校验、共享对象去重、四类冲突、暂停/恢复发单等 22 个用例。
- 数据库路径由环境变量 `OCI_GC_DB` 指定（Compose 中落在命名卷 `/data`）。

## 已知限制 / 未完成项

- 无认证鉴权，仅限内网值班使用；如需多人操作请自行加反向代理鉴权。
- 复核基于"标签指向 + 对象存在性 + 可达性"，不校验对象字节数在两次盘点间是否变化。
- 清理清单只描述"删什么"，不直接调用注册中心删除 API；实际删除由值班员按清单执行。
- 大盘点（百万级对象）未做性能优化；可达性为内存 BFS，导入为单事务批量插入。
