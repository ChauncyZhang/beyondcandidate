# 第三方软件声明

BeyondCandidate 源码采用 MIT License。构建和运行过程中使用的第三方软件仍分别受其自身许可证约束。本文件是便于审计的依赖摘要，不替代依赖包内附带的完整许可证与 NOTICE 文件。

## Python 直接依赖

| 依赖 | 版本 | 许可证 |
| --- | --- | --- |
| Alembic | 1.16.4 | MIT |
| aiosmtplib | 5.1.2 | MIT |
| argon2-cffi | 25.1.0 | MIT |
| asyncpg | 0.30.0 | Apache-2.0 |
| cryptography | 45.0.5 | Apache-2.0 OR BSD-3-Clause |
| email-validator | 2.2.0 | Unlicense |
| FastAPI | 0.116.1 | MIT |
| HTTPX | 0.28.1 | BSD-3-Clause |
| MinIO Python SDK | 7.2.15 | Apache-2.0 |
| Pydantic | 2.11.7 | MIT |
| pdfplumber | 0.11.10 | MIT |
| Pillow | 12.3.0 | MIT-CMU |
| pypdf | 5.8.0 | BSD-3-Clause |
| pypdfium2 | 5.12.1 | BSD-3-Clause / Apache-2.0，另含其发行包列明的 PDFium 第三方许可 |
| python-docx | 1.2.0 | MIT |
| python-multipart | 0.0.20 | Apache-2.0 |
| psycopg | 3.2.9 | LGPL-3.0-only |
| prometheus-client | 0.22.1 | Apache-2.0 AND BSD-2-Clause |
| SQLAlchemy | 2.0.41 | MIT |
| Uvicorn | 0.35.0 | BSD-3-Clause |
| WeasyPrint | 69.0 | BSD-3-Clause |

## Python 开发与许可审计直接依赖

| 依赖 | 版本 | 许可证 |
| --- | --- | --- |
| aiosqlite | 0.21.0 | MIT |
| pip-licenses | 5.5.5 | MIT |
| pytest | 8.4.1 | MIT |

`requirements-dev.txt` 复用运行时已声明的 HTTPX，不形成新的许可边界。

## Offer PDF 容器运行时组件

| Debian 包/组件 | 用途 | 许可证 |
| --- | --- | --- |
| `fonts-noto-cjk` | 固定 `Noto Sans CJK SC` 中文字体族；字体嵌入 PDF | SIL Open Font License 1.1 |
| Pango (`libpango-1.0-0`, `libpangoft2-1.0-0`) | 文本布局和 Fontconfig 字体发现 | LGPL-2.1-or-later |
| HarfBuzz subset (`libharfbuzz-subset0`) | 字形塑形与确定性字体子集 | MIT |

这些系统组件来自基础镜像配置的 Debian 软件源，容器发行时必须保留镜像内对应的 copyright/license 文件。Offer 模板只使用上述本地字体族，不从网络或模板路径读取字体。

Windows 本地测试通过 `server/scripts/install_weasyprint_windows.ps1` 获取 WeasyPrint 69.0 官方 Windows 发行包及 Noto CJK 官方字体文件；脚本锁定发布 URL、源码提交和 SHA-256，不提交二进制文件。官方 Windows 发行包仍适用 WeasyPrint 的 BSD-3-Clause，字体仍适用 SIL Open Font License 1.1。

## 前端直接依赖

| 依赖 | 许可证 |
| --- | --- |
| React / React DOM | MIT |
| React Router | MIT |
| react-pdf | MIT |
| Lucide React | ISC |
| Vite / @vitejs/plugin-react | MIT |
| Playwright | Apache-2.0 |
| vite-plugin-static-copy | MIT |

完整 Node.js 传递依赖及其版本记录在 `frontend/package-lock.json` 中。容器镜像和系统组件也保留其各自许可证；发布二进制或容器镜像时，发行方应同时保留其中附带的许可证和 NOTICE 内容。

## 许可边界

- 本项目不再依赖 PyMuPDF 或 PyMuPDF4LLM，因此不会把其 AGPL-3.0 许可引入默认构建。
- LGPL 依赖以独立 Python 包的形式动态加载，未复制或修改其源码。重新打包或修改该依赖时，应重新检查 LGPL 合规要求。
- 增加依赖前必须核对许可证，禁止未经评估引入 AGPL、SSPL 或其他会改变项目分发义务的组件。
