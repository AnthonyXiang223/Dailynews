"""冒烟测试:健康检查不依赖数据库。"""


def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_index_page_served(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "每日 AI 新闻简报" in resp.text
