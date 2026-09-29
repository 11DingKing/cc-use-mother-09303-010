"""启动 moderation 服务端。

用法：python3 tools/run_server.py --db moderation.db --port 8080
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from moderation_service.server import build_server  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="跨国联合考核 moderation 服务端")
    ap.add_argument("--db", default="moderation.db", help="SQLite 路径")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()

    httpd = build_server(args.db, args.host, args.port)
    print(f"moderation 服务已启动：http://{args.host}:{args.port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
