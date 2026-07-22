from pathlib import Path


def test_nginx_replaces_forwarded_chain_with_public_client() -> None:
    config = Path("deploy/nginx/default.conf").read_text(encoding="utf-8")
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in config
    assert "$proxy_add_x_forwarded_for" not in config


def test_nginx_rate_limits_general_api_and_login_more_strictly() -> None:
    configs = [
        Path("deploy/nginx/default.conf").read_text(encoding="utf-8"),
        Path("deploy/nginx/production.conf.template").read_text(encoding="utf-8"),
    ]
    for config in configs:
        assert "zone=api_per_ip" in config
        assert "location /api/" in config
        assert "limit_req zone=api_per_ip" in config
        assert "limit_req zone=login_per_ip burst=20 nodelay;" in config
        assert "limit_req_status 429;" in config


def test_nginx_allows_multipart_overhead_above_the_ten_mib_file_limit() -> None:
    config = Path("deploy/nginx/default.conf").read_text(encoding="utf-8")
    assert "client_max_body_size 11m;" in config
