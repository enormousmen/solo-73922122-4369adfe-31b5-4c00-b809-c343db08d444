"""关键场景自测：导入校验、预演计算、共享对象去重、发单复核、暂停发单。"""

import json

from conftest import base_inventory, dg, import_inv, manifest, rehearse


# ---------- 导入 ----------

def test_import_valid_inventory(client):
    r = import_inv(client, base_inventory(), follow_redirects=False)
    assert r.status_code == 303
    page = client.get("/")
    assert page.status_code == 200
    assert "盘点完整" in page.text


def test_import_rejects_bad_json(client):
    r = client.post("/inventory", data={"payload": "{not json"})
    assert r.status_code == 400


def test_import_rejects_bad_digest_and_negative_size(client):
    inv = base_inventory()
    inv["repositories"][0]["tags"]["t1"] = "not-a-digest"
    r = import_inv(client, inv)
    assert r.status_code == 400

    inv = base_inventory()
    inv["objects"][dg(3)]["size"] = -1
    r = import_inv(client, inv)
    assert r.status_code == 400


def test_import_empty_payload_rejected(client):
    r = client.post("/inventory", data={"payload": "  "})
    assert r.status_code == 400


# ---------- 预演计算 ----------

def _rehearse_ok(client, *selected):
    r = rehearse(client, *selected)
    assert r.status_code == 303, r.text
    rid = int(r.headers["location"].rsplit("/", 1)[1])
    return rid


def test_rehearsal_only_counts_what_becomes_unreachable(client):
    import_inv(client, base_inventory())
    rid = _rehearse_ok(client, "app/one=t1")
    m = manifest(client, rid)
    assert m.status_code == 200
    body = m.json()
    # lay_shared(7) 仍被 app/two 引用，不在删除集；m_keep 链路由保留版本兜底
    assert body["delete"] == [dg(1), dg(2), dg(3), dg(4)]
    assert body["total_bytes"] == 100 + 200 + 10 + 1000


def test_shared_object_counted_once(client):
    import_inv(client, base_inventory())
    rid = _rehearse_ok(client, "app/one=t1", "app/two=t2")
    body = manifest(client, rid).json()
    # 跨仓共享的 lay_shared(7) 只出现一次
    assert body["delete"].count(dg(7)) == 1
    assert body["delete"] == [dg(1), dg(2), dg(3), dg(4), dg(5), dg(6), dg(7), dg(8), dg(9)]
    assert body["total_bytes"] == 100 + 200 + 10 + 1000 + 100 + 200 + 5000 + 10 + 2000


def test_retained_digest_keeps_objects(client):
    inv = base_inventory()
    inv["retained"].append(dg(4))  # lay_a 被按摘要保留
    import_inv(client, inv)
    rid = _rehearse_ok(client, "app/one=t1")
    body = manifest(client, rid).json()
    assert body["delete"] == [dg(1), dg(2), dg(3)]
    assert body["total_bytes"] == 100 + 200 + 10


def test_rehearsal_unknown_tag_rejected(client):
    import_inv(client, base_inventory())
    r = rehearse(client, "app/one=nope")
    assert r.status_code == 400


def test_rehearsal_empty_selection_rejected(client):
    import_inv(client, base_inventory())
    r = client.post("/rehearsal", data={})
    assert r.status_code == 400


def test_rehearsal_without_inventory_rejected(client):
    r = rehearse(client, "app/one=t1")
    assert r.status_code == 400


# ---------- 清单内容与下载 ----------

def test_manifest_content_exact(client):
    import_inv(client, base_inventory())
    rid = _rehearse_ok(client, "app/one=t1")
    r = manifest(client, rid)
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    body = r.json()
    # 清单只包含预演中的标签、原指向和待删摘要（加少量元数据）
    assert set(body) == {
        "rehearsal_id", "generated_at", "validated_against_inventory",
        "tags", "delete", "total_bytes",
    }
    assert body["tags"] == [{"repo": "app/one", "tag": "t1", "digest": dg(1)}]
    assert body["delete"] == [dg(1), dg(2), dg(3), dg(4)]
    assert body["total_bytes"] == 1310


def test_unrelated_changes_still_download(client):
    import_inv(client, base_inventory())
    rid = _rehearse_ok(client, "app/one=t1")

    # 无关变动：另一个标签换指向 + 新增对象
    inv2 = base_inventory()
    inv2["repositories"][1]["tags"]["t2"] = dg(20)
    inv2["objects"].update({
        dg(20): {"type": "index", "size": 100, "refs": [dg(21)]},
        dg(21): {"type": "manifest", "size": 200, "refs": [dg(22), dg(23)]},
        dg(22): {"type": "config", "size": 10},
        dg(23): {"type": "layer", "size": 777},
    })
    import_inv(client, inv2)

    r = manifest(client, rid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tags"] == [{"repo": "app/one", "tag": "t1", "digest": dg(1)}]
    assert body["delete"] == [dg(1), dg(2), dg(3), dg(4)]
    assert body["total_bytes"] == 1310
    assert body["validated_against_inventory"] == 2


# ---------- 复核：冲突整份拒发 ----------

def test_tag_moved_conflict(client):
    import_inv(client, base_inventory())
    rid = _rehearse_ok(client, "app/one=t1")

    inv2 = base_inventory()
    inv2["repositories"][0]["tags"]["t1"] = dg(5)  # t1 指向变了
    import_inv(client, inv2)

    r = manifest(client, rid)
    assert r.status_code == 409
    conflicts = r.json()["conflicts"]
    assert any(c["type"] == "tag_moved" and c["was"] == dg(1) and c["now"] == dg(5)
               for c in conflicts)


def test_tag_deleted_conflict(client):
    import_inv(client, base_inventory())
    rid = _rehearse_ok(client, "app/one=t1")

    inv2 = base_inventory()
    inv2["repositories"][0]["tags"] = {}
    import_inv(client, inv2)

    r = manifest(client, rid)
    assert r.status_code == 409
    assert any(c["type"] == "tag_missing" for c in r.json()["conflicts"])


def test_object_missing_conflict(client):
    import_inv(client, base_inventory())
    rid = _rehearse_ok(client, "app/one=t1")

    # 待删对象 lay_a(4) 已被别处清掉：objects 与引用中都不再有它（盘点仍完整）
    inv2 = base_inventory()
    del inv2["objects"][dg(4)]
    inv2["objects"][dg(2)]["refs"] = [dg(3), dg(7)]
    import_inv(client, inv2)

    r = manifest(client, rid)
    assert r.status_code == 409
    conflicts = r.json()["conflicts"]
    assert [c["type"] for c in conflicts] == ["object_missing"]
    assert conflicts[0]["digest"] == dg(4)


def test_object_reachable_again_conflict(client):
    import_inv(client, base_inventory())
    rid = _rehearse_ok(client, "app/one=t1")

    # 新标签重新指向待删子树
    inv2 = base_inventory()
    inv2["repositories"][1]["tags"]["rescue"] = dg(1)
    import_inv(client, inv2)

    r = manifest(client, rid)
    assert r.status_code == 409
    reachable = {c["digest"] for c in r.json()["conflicts"] if c["type"] == "object_reachable"}
    assert reachable == {dg(1), dg(2), dg(3), dg(4)}


def test_all_conflicts_reported_together(client):
    import_inv(client, base_inventory())
    rid = _rehearse_ok(client, "app/one=t1", "app/two=t2")

    inv2 = base_inventory()
    inv2["repositories"][0]["tags"]["t1"] = dg(5)  # tag_moved
    inv2["repositories"][1]["tags"] = {}           # tag_missing
    import_inv(client, inv2)

    r = manifest(client, rid)
    assert r.status_code == 409
    types = {c["type"] for c in r.json()["conflicts"]}
    assert {"tag_moved", "tag_missing"} <= types


# ---------- 盘点不完整：暂停发单 ----------

def test_incomplete_inventory_pauses_download(client):
    import_inv(client, base_inventory())
    rid = _rehearse_ok(client, "app/one=t1")

    # 新盘点缺少被引用对象 lay_a(4)
    inv2 = base_inventory()
    del inv2["objects"][dg(4)]
    import_inv(client, inv2)

    r = manifest(client, rid)
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "inventory_incomplete"
    assert dg(4) in body["missing"]

    # 导入完整盘点后恢复发单
    import_inv(client, base_inventory())
    r = manifest(client, rid)
    assert r.status_code == 200
    assert r.json()["delete"] == [dg(1), dg(2), dg(3), dg(4)]


def test_incomplete_inventory_still_allows_rehearsal(client):
    inv = base_inventory()
    del inv["objects"][dg(4)]
    import_inv(client, inv)
    rid = _rehearse_ok(client, "app/one=t1")
    # 预演可看，但发单被暂停
    assert client.get(f"/rehearsal/{rid}").status_code == 200
    assert manifest(client, rid).status_code == 409


# ---------- 页面 ----------

def test_pages_render(client):
    import_inv(client, base_inventory())
    rid = _rehearse_ok(client, "app/one=t1")
    assert client.get("/").status_code == 200
    page = client.get(f"/rehearsal/{rid}")
    assert page.status_code == 200
    assert f"预演 #{rid}" in page.text
    assert "app/one" in page.text


def test_missing_rehearsal_404(client):
    assert client.get("/rehearsal/999").status_code == 404
    assert manifest(client, 999).status_code == 404


def test_conflict_page_html_for_browser(client):
    import_inv(client, base_inventory())
    rid = _rehearse_ok(client, "app/one=t1")
    inv2 = base_inventory()
    inv2["repositories"][0]["tags"]["t1"] = dg(5)
    import_inv(client, inv2)
    r = client.get(
        f"/rehearsal/{rid}/manifest.json", headers={"Accept": "text/html"}
    )
    assert r.status_code == 409
    assert "整份拒发" in r.text
    assert "标签指向有变" in r.text
