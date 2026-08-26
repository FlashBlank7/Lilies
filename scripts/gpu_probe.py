"""GPU 探针：把 nvidia-smi 以 JSON 暴露在回环端口，供工作流 http_request 取数。

与金蝶探针同一定位——工作流依赖的外部数据源，以受控 HTTP 端点形式提供。
仅绑定 127.0.0.1，无鉴权（回环内网假设），零依赖。

用法：
    setsid nohup python3 scripts/gpu_probe.py > ~/gpu-probe.log 2>&1 &
    curl http://127.0.0.1:9101/gpu
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = 9101
FIELDS = [
    "index", "name", "memory.total", "memory.used",
    "utilization.gpu", "temperature.gpu", "power.draw", "power.limit",
]


def snapshot() -> dict:
    try:
        out = subprocess.run(
            ["nvidia-smi", f"--query-gpu={','.join(FIELDS)}",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode != 0:
            return {"ok": False, "error": out.stderr.strip()[:300]}
        gpus = []
        for line in out.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            gpus.append({
                "index": int(parts[0]),
                "name": parts[1],
                "memory_total_mib": float(parts[2]),
                "memory_used_mib": float(parts[3]),
                "utilization_pct": float(parts[4]),
                "temperature_c": float(parts[5]),
                "power_draw_w": float(parts[6]),
                "power_limit_w": float(parts[7]),
            })
        return {
            "ok": True,
            "generated_at": datetime.now(timezone.utc).astimezone().isoformat(),
            "gpu_count": len(gpus),
            "gpus": gpus,
        }
    except Exception as error:  # noqa: BLE001 - 探针必须永远给出可读回答
        return {"ok": False, "error": str(error)[:300]}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def do_GET(self) -> None:
        if self.path != "/gpu":
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps(snapshot(), ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    print(f"GPU probe on http://127.0.0.1:{PORT}/gpu")
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
