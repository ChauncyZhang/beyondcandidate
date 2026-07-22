# 参与贡献

欢迎提交 Issue 和 Pull Request。变更应保持租户隔离、审计可追踪和候选人数据最小化原则。

提交前请至少运行：

```bash
cd frontend
npm ci
npm test
npm run build

cd ..
python -m pytest server/tests -q
```

新增依赖时，请在 Pull Request 中说明用途、版本和许可证，并同步更新 `THIRD_PARTY_NOTICES.md`。不得提交真实企业域名、IP、证书、密钥、生产账号、真实简历或面试评价。
