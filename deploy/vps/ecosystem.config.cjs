// PM2 programs for EmpowerX AI only.
// The dedicated host ai.devempowerx.io proxies / to 127.0.0.1:8002.
// Uvicorn must not advertise an /ai ASGI prefix. Other EmpowerX PM2 apps are absent.

const path = require("path");

const live = "/var/www/empowerx/EmpowerX-AI";

module.exports = {
  apps: [
    {
      name: "empowerx-ai-api",
      cwd: live,
      script: path.join(live, ".venv/bin/uvicorn"),
      args: "app.main:app --host 127.0.0.1 --port 8002 --proxy-headers --forwarded-allow-ips=127.0.0.1",
      interpreter: "none",
      exec_mode: "fork",
      instances: 1,
      autorestart: true,
      max_restarts: 10,
      min_uptime: "5s",
      kill_timeout: 10000,
      time: true,
      user: "empowerx-ai",
      uid: "empowerx-ai",
      gid: "empowerx-ai",
      out_file: "/var/log/empowerx-ai/api.out.log",
      error_file: "/var/log/empowerx-ai/api.err.log",
      merge_logs: true,
    },
    {
      name: "empowerx-ai-worker",
      cwd: live,
      script: path.join(live, ".venv/bin/celery"),
      args: "-A app.workers.celery_app.celery_app worker --loglevel=INFO -Q radiology,celery --concurrency=1",
      interpreter: "none",
      exec_mode: "fork",
      instances: 1,
      autorestart: true,
      max_restarts: 10,
      min_uptime: "5s",
      kill_timeout: 20000,
      time: true,
      user: "empowerx-ai",
      uid: "empowerx-ai",
      gid: "empowerx-ai",
      out_file: "/var/log/empowerx-ai/worker.out.log",
      error_file: "/var/log/empowerx-ai/worker.err.log",
      merge_logs: true,
    },
  ],
};
