import re
import unittest
from pathlib import Path


NGINX_DIR = Path(__file__).resolve().parent
CONFIGS = ("default.conf", "production.conf.template")


def location_body(config: str, declaration: str) -> str:
    match = re.search(rf"^\s*location\s+{re.escape(declaration)}\s*\{{", config, re.MULTILINE)
    if match is None:
        raise AssertionError(f"missing location {declaration}")

    depth = 1
    cursor = match.end()
    while cursor < len(config) and depth:
        if config[cursor] == "{":
            depth += 1
        elif config[cursor] == "}":
            depth -= 1
        cursor += 1
    if depth:
        raise AssertionError(f"unterminated location {declaration}")
    return config[match.end() : cursor - 1]


class OfferSecurityConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.configs = {
            name: (NGINX_DIR / name).read_text(encoding="utf-8") for name in CONFIGS
        }

    def test_public_offer_api_disables_access_logging_and_keeps_api_proxy_controls(self):
        expected_lines = (
            "access_log off;",
            "include /etc/nginx/snippets/security-headers.conf;",
            'add_header Cache-Control "no-store" always;',
            "limit_req zone=api_per_ip burst=20 nodelay;",
            "proxy_pass http://api:8000;",
            "proxy_http_version 1.1;",
            "proxy_set_header Host $host;",
            "proxy_set_header X-Real-IP $remote_addr;",
            "proxy_set_header X-Forwarded-For $remote_addr;",
            "proxy_set_header X-Forwarded-Proto $scheme;",
            "proxy_set_header X-Trace-ID $http_x_trace_id;",
        )
        for name, config in self.configs.items():
            with self.subTest(config=name):
                body = location_body(config, "^~ /api/public/v1/offers/")
                for line in expected_lines:
                    self.assertIn(line, body)
                self.assertLess(
                    config.index("location ^~ /api/public/v1/offers/"),
                    config.index("location /api/"),
                )

    def test_offer_spa_disables_access_logging_and_sets_private_response_headers(self):
        expected_lines = (
            "access_log off;",
            "include /etc/nginx/snippets/security-headers.conf;",
            'add_header Cache-Control "no-store" always;',
            "expires -1;",
            "etag off;",
            "try_files $uri $uri/ /index.html =404;",
        )
        for name, config in self.configs.items():
            with self.subTest(config=name):
                body = location_body(config, "^~ /offer/")
                for line in expected_lines:
                    self.assertIn(line, body)

    def test_login_rate_limit_and_security_header_policy_remain_present(self):
        headers = (NGINX_DIR / "snippets" / "security-headers.conf").read_text(encoding="utf-8")
        for directive in (
            'add_header Referrer-Policy "no-referrer" always;',
            'add_header Permissions-Policy "camera=(), geolocation=(), microphone=()" always;',
            "add_header Content-Security-Policy",
            "default-src 'self'",
            "connect-src 'self' blob:",
            "object-src 'none'",
            "frame-ancestors 'none'",
        ):
            self.assertIn(directive, headers)

        for name, config in self.configs.items():
            with self.subTest(config=name):
                login = location_body(config, "= /api/v1/auth/login")
                self.assertIn("limit_req zone=login_per_ip burst=20 nodelay;", login)

    def test_local_and_production_offer_locations_remain_identical(self):
        local = self.configs["default.conf"]
        production = self.configs["production.conf.template"]
        for declaration in ("^~ /api/public/v1/offers/", "^~ /offer/"):
            with self.subTest(location=declaration):
                self.assertEqual(
                    location_body(local, declaration),
                    location_body(production, declaration),
                )

    def test_frontend_image_contains_security_header_snippets(self):
        dockerfile = (NGINX_DIR / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("COPY deploy/nginx/snippets /etc/nginx/snippets", dockerfile)


if __name__ == "__main__":
    unittest.main()
