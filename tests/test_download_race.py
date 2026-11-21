"""复核期间盘点并发导入的关键场景。

通过 app.create_app 的测试注入点 review_hook，在每轮复核完成后、确认最新盘点号
之前插入一份新盘点，确定性地复现"值班员下载清单时另一份盘点恰好导入成功"。
"""

import json

from conftest import base_inventory, dg, import_inv, manifest, rehearse

from app import db
from app.main import MAX_REVIEW_ATTEMPTS
from app.oci import parse_inventory


def _rehearsal_id(client) -> int:
    r = rehearse(client, "app/one=t1")
    assert r.status_code == 303, r.text
    return int(r.headers["location"].rsplit("/", 1)[1])


def _direct_import(app, inv: dict) -> int:
    """用独立连接直接写库，模拟另一个请求成功导入盘点。"""
    parsed = parse_inventory(json.dumps(inv))
    con = db.connect(app.state.db_path)
    try:
        with con:
            return db.insert_inventory(con, parsed)
    finally:
        con.close()


def _install_hook(app, fn):
    # download_manifest 通过闭包读取该模块级变量
    import app.main as main_mod

    main_mod.review_hook = fn
    return fn


def test_new_inventory_during_review_revalidates_against_latest(client, app):
    import_inv(client, base_inventory())
    rid = _rehearsal_id(client)

    seen = []

    def hook(_rid, inv_id, attempt):
        seen.append((inv_id, attempt))
        # 第 1 轮复核期间，另一份内容相同的盘点恰好导入成功（无关变动）
        if attempt == 1:
            _direct_import(app, base_inventory())

    _install_hook(app, hook)

    r = manifest(client, rid)
    assert r.status_code == 200, r.text
    body = r.json()
    # 发单依据的是作出决定时最新且已成功导入的盘点 #2，而不是请求开始时的 #1
    assert body["validated_against_inventory"] == 2
    assert body["rehearsal_id"] == rid
    # 预演清单内容不变（无关变动照常发放）
    assert body["delete"] == [dg(1), dg(2), dg(3), dg(4)]
    assert seen == [(1, 1), (2, 2)]


def test_conflict_appearing_during_review_rejects_whole_manifest(client, app):
    import_inv(client, base_inventory())
    rid = _rehearsal_id(client)

    def hook(_rid, inv_id, attempt):
        if attempt == 1:
            inv2 = base_inventory()
            inv2["repositories"][0]["tags"]["t1"] = dg(5)  # 标签指向有变
            _direct_import(app, inv2)

    _install_hook(app, hook)

    r = manifest(client, rid)
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "conflict"
    # 冲突来自最新盘点 #2，而非请求开始时的 #1
    assert "#2" in body["message"]
    assert any(c["type"] == "tag_moved" for c in body["conflicts"])


def test_incomplete_inventory_during_review_blocks(client, app):
    import_inv(client, base_inventory())
    rid = _rehearsal_id(client)

    def hook(_rid, inv_id, attempt):
        if attempt == 1:
            inv2 = base_inventory()
            del inv2["objects"][dg(4)]  # 被引用对象缺失 -> 盘点不完整
            _direct_import(app, inv2)

    _install_hook(app, hook)

    r = manifest(client, rid)
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "inventory_incomplete"
    assert dg(4) in body["missing"]


def test_continuously_updated_inventory_returns_503_retry_later(client, app):
    import_inv(client, base_inventory())
    rid = _rehearsal_id(client)

    seen = []

    def hook(_rid, inv_id, attempt):
        seen.append(attempt)
        # 每轮复核都有新盘点导入，复核始终无法完成
        _direct_import(app, base_inventory())

    _install_hook(app, hook)

    r = manifest(client, rid)
    assert r.status_code == 503
    body = r.json()
    assert body["error"] == "inventory_changing"
    assert body["attempts"] == MAX_REVIEW_ATTEMPTS
    assert body["latest_inventory"] == 1 + MAX_REVIEW_ATTEMPTS
    assert "稍后重试" in body["message"]
    assert r.headers["retry-after"] == "2"
    assert seen == [1, 2, 3]


def test_retry_loop_converges_after_two_interruptions(client, app):
    import_inv(client, base_inventory())
    rid = _rehearsal_id(client)

    seen = []

    def hook(_rid, inv_id, attempt):
        seen.append(attempt)
        if attempt < 3:
            _direct_import(app, base_inventory())

    _install_hook(app, hook)

    r = manifest(client, rid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["validated_against_inventory"] == 3
    assert body["delete"] == [dg(1), dg(2), dg(3), dg(4)]


def test_imports_are_not_blocked_while_review_runs(client, app):
    # 复核循环中的并发导入必须能立即提交成功（WAL 下读写互不阻塞）
    import_inv(client, base_inventory())
    rid = _rehearsal_id(client)

    def hook(_rid, inv_id, attempt):
        if attempt == 1:
            new_id = _direct_import(app, base_inventory())
            assert new_id == 2

    _install_hook(app, hook)

    assert manifest(client, rid).status_code == 200


def test_busy_page_renders_html_for_browser(client, app):
    import_inv(client, base_inventory())
    rid = _rehearsal_id(client)

    def hook(_rid, inv_id, attempt):
        _direct_import(app, base_inventory())

    import app.main as main_mod
    main_mod.review_hook = hook

    r = client.get(f"/rehearsal/{rid}/manifest.json", headers={"Accept": "text/html"})
    assert r.status_code == 503
    assert "稍后重试" in r.text
    assert f"#{1 + MAX_REVIEW_ATTEMPTS}" in r.text
