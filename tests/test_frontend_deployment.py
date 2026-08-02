from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_nginx_splits_api_proxy_from_spa_static_files() -> None:
    template = (ROOT / "deploy" / "nginx" / "bdencode.conf.in").read_text(
        encoding="utf-8"
    )

    assert "location ^~ /encoder/api/" in template
    assert "proxy_pass http://127.0.0.1:@PORT@/api/;" in template
    assert "location /encoder/" in template
    assert "root @FRONTEND_ROOT@;" in template
    assert "try_files $uri $uri/ /encoder/index.html =404;" in template
    assert "Content-Security-Policy" in template


def test_installer_publishes_frontend_with_the_release_transaction() -> None:
    installer = (ROOT / "install" / "install.sh").read_text(encoding="utf-8")

    assert 'frontend_dist="$repo_root/frontend/dist"' in installer
    assert "Missing prebuilt frontend release" in installer
    assert 'frontend_release="$frontend_root/releases/$release_id"' in installer
    assert 'sudo diff -qr "$frontend_dist" "$frontend_release/encoder"' in installer
    assert 'sudo mv -Tf "$frontend_new" "$frontend_root/current"' in installer
    assert 's|@FRONTEND_ROOT@|$frontend_root/current|g' in installer

    publish = installer.index('sudo mv -Tf "$frontend_new" "$frontend_root/current"')
    validate_nginx = installer.index("if ! sudo nginx -t; then")
    mark_healthy = installer.index(
        "sudo /usr/local/libexec/bdencode-install-transaction healthy"
    )
    assert publish < validate_nginx < mark_healthy
