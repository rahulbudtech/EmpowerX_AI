// Reference PM2 programs for production EmpowerX AI.
// Installer does not replace the running process list. Restart keeps the
// existing PM2 definitions, including concurrency=2.

const path = require("path");

const live = "/var/www/empowerx/EmpowerX_AI";

module.exports = {
  apps: [
    {
      name: "empowerx-ai-api",
      cwd: live,
      script: path.join(live, ".venv/bin/uvicorn"),
      args: "app.main:app --host 127.0.0.1 --port 8002",
      interpreter: "none",
      exec_mode: "fork",
      instances: 1,
      autorestart: true,
      max_restarts: 10,
      min_uptime: "5s",
      kill_timeout: 10000,
      time: true,
      out_file: "/var/log/empowerx-ai/api.out.log",
      error_file: "/var/log/empowerx-ai/api.err.log",
      merge_logs: true,
    },
    {
      name: "empowerx-ai-worker",
      cwd: live,
      script: path.join(live, ".venv/bin/celery"),
      args: "-A app.workers.celery_app.celery_app worker --loglevel=INFO --concurrency=2 -Q radiology,celery",
      interpreter: "none",
      exec_mode: "fork",
      instances: 1,
      autorestart: true,
      max_restarts: 10,
      min_uptime: "5s",
      kill_timeout: 20000,
      time: true,
      out_file: "/var/log/empowerx-ai/worker.out.log",
      error_file: "/var/log/empowerx-ai/worker.err.log",
      merge_logs: true,
    },
  ],
};
