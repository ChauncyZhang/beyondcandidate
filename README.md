# BeyondCandidate

BeyondCandidate 是一个开源的 AI 招聘协作平台，覆盖简历导入与解析、AI 初筛、候选人管理、用人经理评审、面试安排、反馈、人才库和数据治理。

## 主要能力

- 按职位批量导入 PDF、DOCX、TXT、JPG 和 PNG 简历；扫描件与图片型 PDF 可通过已配置的 OCR 服务识别。
- 结构化解析、扫描件 OCR 和 LLM 补全组成的多层简历处理链路。
- 使用 OpenAI-compatible Provider 进行多维度 AI 评估与宽松流转。
- 候选人、职位、招聘流程模板、面试轮次和反馈统一管理。
- 飞书账号绑定、日历忙闲查询和面试日程同步。
- 基于角色和组织的数据隔离、审计日志、删除审批与法律保留。
- PostgreSQL、MinIO、ClamAV、FastAPI、React 和 Docker Compose 部署。

## 快速开始

需要 Docker Desktop，或 Linux 上的 Docker Engine 与 Compose plugin；初始化脚本还需要 Python 3.10 以上版本。

Windows PowerShell：

```powershell
.\scripts\setup.ps1
```

macOS / Linux：

```bash
chmod +x scripts/setup.sh
./scripts/setup.sh
```

脚本会自动生成 `deploy/.env` 中的本地密钥、构建镜像、初始化 PostgreSQL/MinIO、执行迁移、创建管理员并启动服务。首次运行会在终端显示一次管理员初始密码。

默认访问地址：[http://localhost:8080](http://localhost:8080)。可指定组织、管理员和端口：

```bash
python3 scripts/community_setup.py \
  --organization-slug example \
  --organization-name "Example Recruiting" \
  --admin-email admin@example.com \
  --port 8080
```

再次运行不会自动重置管理员密码。需要主动重置时传入 `--admin-password`。

## 开发

前端：

```bash
cd frontend
npm ci
npm run dev
```

后端需要 Python 3.12。完整依赖和测试入口位于 `server/requirements*.txt` 与 `server/README.md`。推荐使用可复现的 Docker 测试镜像：

```bash
docker build --target test -t beyondcandidate-server-test -f server/Dockerfile .
docker run --rm beyondcandidate-server-test
```

## 生产部署

社区初始化脚本用于本地验证，不应直接当作生产安全基线。生产环境至少需要：

- 独立域名、HTTPS 证书和受控反向代理。
- 外部密钥管理、数据库和对象存储备份。
- `APP_ENVIRONMENT=production`、明确的 HTTPS CORS 和独立高强度凭据。
- 可用的 LLM/OCR Provider，以及对简历外发范围的合规评估。
- 监控、告警、容量评估和恢复演练。

通用 Compose、TLS、备份和可观测性模板位于 `deploy/`。企业专属域名、证书和部署编排不应提交到本公开仓库。

## AI 与招聘决策

AI 评分只用于辅助招聘流程，不应作为自动拒绝候选人的唯一依据。部署方应检查提示词、模型偏差、数据保留、跨境传输和当地就业法规，并为候选人提供必要的人工复核及申诉渠道。

## 许可证

项目源码采用 [MIT License](LICENSE)。第三方依赖按各自许可证使用，详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。MIT 授权不包含 BeyondCandidate 名称和标识的商标权，详见 [TRADEMARKS.md](TRADEMARKS.md)。
