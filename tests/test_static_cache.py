"""
Frontendfilerna skickas med "Cache-Control: no-cache" (app.py:NoCacheStaticFiles),
så webbläsaren alltid frågar servern efter nya versioner - ingen Ctrl+F5
behövs efter en uppdatering - men fortfarande får ett billigt 304 när
inget ändrats.
"""
import pytest


@pytest.mark.parametrize("path", ["/", "/index.html", "/app.js", "/style.css"])
def test_frontend_files_are_revalidated(client, path):
    res = client.get(path)
    assert res.status_code == 200
    assert res.headers["cache-control"] == "no-cache"
    assert res.headers.get("etag")


def test_unchanged_file_gives_304(client):
    etag = client.get("/app.js").headers["etag"]
    res = client.get("/app.js", headers={"If-None-Match": etag})
    assert res.status_code == 304
    assert res.headers["cache-control"] == "no-cache"
