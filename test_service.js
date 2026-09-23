"use strict";

const { spawnSync } = require("node:child_process");

// 先跑领域与 HTTP 集成测试，再跑健康入口契约测试
const suites = [
  ["python3", ["-m", "unittest", "discover", "-s", "tests", "-v"]],
  ["python3", ["-m", "unittest", "-v", "service_contract"]],
];

for (const [cmd, args] of suites) {
  const result = spawnSync(cmd, args, { stdio: "inherit" });
  if (result.error) {
    console.error(result.error.message);
    process.exit(1);
  }
  if (result.status !== 0) {
    process.exit(result.status ?? 1);
  }
}
