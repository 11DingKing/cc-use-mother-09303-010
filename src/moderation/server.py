"""启动 moderation 服务端。

用法：
    python3 -m moderation.server [--db data/moderation.db] [--host 127.0.0.1] [--port 8080]
默认使用内存数据库（进程退出即清空），传入 --db 路径可持久化。
"""
from __future__ import annotations

import argparse

from .api import build_server


def main() -> None:
    parser = argparse.ArgumentParser(description="跨国联合考核 moderation 服务端")
    parser.add_argument("--db", default=":memory:", help="SQLite 数据库路径")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    httpd = build_server(args.db, args.host, args.port)
    print(f"moderation 服务已启动：http://{args.host}:{args.port}  数据库={args.db}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭…")
    finally:
        httpd.server_close()
        httpd.store.close()


if __name__ == "__main__":
    main()
