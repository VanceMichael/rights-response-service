"""服务入口：python -m app.main --host 127.0.0.1 --port 8080 --state app-state.sqlite3"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .http_app import build_server
from .seed import seed
from .storage import Store

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description="本地合成影视保护通知办理服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--state", default="app-state.sqlite3",
                        help="业务状态 SQLite 文件（与协作方回放状态分别保管）")
    parser.add_argument("--catalog", default=str(ROOT / "data" / "catalog.json"))
    parser.add_argument("--replay-base-url", default=None,
                        help="协作方回放地址，如 http://127.0.0.1:8765")
    parser.add_argument("--no-seed", action="store_true",
                        help="不从 catalog 引导主数据")
    args = parser.parse_args()

    store = Store(args.state)
    if not args.no_seed:
        created = seed(store, args.catalog, args.replay_base_url)
        print(json.dumps({"seeded": created}, ensure_ascii=False), flush=True)
    httpd = build_server(args.host, args.port, store)
    print(json.dumps(
        {"host": args.host, "port": httpd.server_address[1],
         "state": args.state}, ensure_ascii=False), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        store.close()


if __name__ == "__main__":
    main()
