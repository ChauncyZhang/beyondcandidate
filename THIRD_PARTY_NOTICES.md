# 第三方软件声明

BeyondCandidate 源码采用 MIT License。构建和运行过程中使用的第三方软件仍分别受其自身许可证约束。本文件是便于审计的依赖摘要，不替代依赖包内附带的完整许可证与 NOTICE 文件。

## Python 直接依赖

| 依赖 | 版本 | 许可证 |
| --- | --- | --- |
| Alembic | 1.16.4 | MIT |
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
