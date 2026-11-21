import json

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


def dg(n: int) -> str:
    """生成确定性的测试摘要。"""
    return "sha256:" + f"{n:064x}"


# 基础盘点图：
#   app/one:t1 -> idx1(1) -> m1(2) -> cfg1(3), lay_a(4), lay_shared(7)
#   app/two:t2 -> idx2(5) -> m2(6) -> cfg2(8), lay_shared(7), lay_b(9)
#   retained: m_keep(10) -> cfg_keep(11), lay_keep(12)
def base_inventory() -> dict:
    return {
        "captured_at": "2026-11-21T08:00:00Z",
        "repositories": [
            {"name": "app/one", "tags": {"t1": dg(1)}},
            {"name": "app/two", "tags": {"t2": dg(5)}},
        ],
        "retained": [dg(10)],
        "objects": {
            dg(1): {"type": "index", "size": 100, "refs": [dg(2)]},
            dg(2): {"type": "manifest", "size": 200, "refs": [dg(3), dg(4), dg(7)]},
            dg(3): {"type": "config", "size": 10},
            dg(4): {"type": "layer", "size": 1000},
            dg(5): {"type": "index", "size": 100, "refs": [dg(6)]},
            dg(6): {"type": "manifest", "size": 200, "refs": [dg(8), dg(7), dg(9)]},
            dg(7): {"type": "layer", "size": 5000},
            dg(8): {"type": "config", "size": 10},
            dg(9): {"type": "layer", "size": 2000},
            dg(10): {"type": "manifest", "size": 200, "refs": [dg(11), dg(12)]},
            dg(11): {"type": "config", "size": 10},
            dg(12): {"type": "layer", "size": 300},
        },
    }


@pytest.fixture()
def app(tmp_path):
    application = create_app(str(tmp_path / "test.db"))
    # 每个用例默认不安装复核注入点
    import app.main as main_mod

    main_mod.review_hook = None
    yield application
    main_mod.review_hook = None


@pytest.fixture()
def client(app):
    with TestClient(app) as c:
        yield c


def import_inv(client: TestClient, inv: dict, **kwargs):
    return client.post("/inventory", data={"payload": json.dumps(inv)}, **kwargs)


def rehearse(client: TestClient, *selected: str):
    return client.post(
        "/rehearsal", data={"selected": list(selected)}, follow_redirects=False
    )


def manifest(client: TestClient, rid: int = 1):
    return client.get(
        f"/rehearsal/{rid}/manifest.json", headers={"Accept": "application/json"}
    )
