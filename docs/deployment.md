# 部署教程

## 本机运行

1. 安装 Docker 与 Docker Compose。
2. 复制 `.env.example` 为 `.env`，为数据库和初始管理员填写**不同**的强随机密码。密码仅用字母数字，避免在数据库 URL 中遇到未编码的特殊字符。
3. 运行 `docker compose up -d --build`。`init` 服务会等待数据库就绪，执行 Alembic 迁移并写入四院、管理员、演示账号与待审核样例题；API 就绪后再启动网页。
4. 打开 `http://localhost:8080`，以 `admin` 登录，立即修改初始密码。进入后台审核样例题，之后创建比赛。

可用 `docker compose ps` 查看容器，`docker compose logs -f init api web` 查启动日志。修改代码后再次执行 `docker compose up -d --build`；迁移与种子脚本可重复运行。`docker compose down` 只停止容器，保留数据库卷；删除卷会删除全部比赛数据。

## 对外提供访问

默认 Web 端口绑定 `127.0.0.1:8080`，不会直接暴露到公网。**对外开放之前**，先在后台账号管理中封禁全部 `demo_*` 演示账号；它们的示例口令随源码公开。然后让同机 HTTPS 反向代理把你的域名转发到该端口，并在 `.env` 中设置：

```dotenv
MGUS_ALLOWED_ORIGINS=https://quiz.example.edu
MGUS_COOKIE_SECURE=true
```

域名只是示例，应改成实际域名。HTTPS 代理须支持 WebSocket 升级请求；站点应通过同一域名访问 `/api` 与 `/ws`。如果代理不在同一台机器，需要自行调整 Compose 的端口绑定和防火墙规则。不要把 PostgreSQL 端口、`.env`、备份或管理密钥对外开放。

上线前应完成 HTTPS、实际 Wi-Fi/局域网压测、数据库备份与恢复演练。当前实时控制器限定单 API 实例，不要通过增加 worker 或 API 副本直接扩容。

## 数据备份

数据库位于 Docker 命名卷 `db_data`。在有权限的主机上通过 `pg_dump` 定期导出，并把加密备份存放到容器主机之外。恢复前先在独立环境验证备份文件与迁移版本；不要把导出文件提交到 Git。
