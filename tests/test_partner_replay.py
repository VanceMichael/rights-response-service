"""与协作方协议回放器对接：504 受理不明后只能查询原流水，逐帧观测。"""
import json
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _http(base, path, value=None):
    raw = json.dumps(value).encode() if value is not None else None
    req = urllib.request.Request(
        base + path, data=raw,
        headers={"Content-Type": "application/json"},
        method="POST" if value is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


class PartnerReplayContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = str(Path(self.tmp.name) / "partner.sqlite3")
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "partner" / "server.py"),
             "--port", "0", "--state", self.state],
            stdout=subprocess.PIPE, text=True)
        self.base = "http://127.0.0.1:%d" % json.loads(
            self.proc.stdout.readline())["port"]

    def tearDown(self):
        self.proc.terminate()
        self.proc.wait(timeout=5)
        self.proc.stdout.close()
        self.tmp.cleanup()

    def test_504_then_poll_original_flow(self):
        commands = json.loads(
            (ROOT / "partner" / "commands.json").read_text())["commands"]
        cmd = next(c for c in commands if c["id"] == "restrict-A")
        payload = {"command_id": cmd["id"], "operation": cmd["operation"],
                   "request": cmd["request"]}
        # 首次提交得到 504（响应不明，但协作方可能已受理）
        status, _ = _http(self.base, "/commands", payload)
        self.assertEqual(status, 504)
        # 不能当作失败而另发；逐帧查询同一流水
        first_status, first = _http(
            self.base, "/commands/" + cmd["id"])
        self.assertEqual(first_status, 200)
        self.assertEqual(first["state"], "pending")
        _, second = _http(self.base, "/commands/" + cmd["id"])
        self.assertEqual(second["state"], "restricted")
        self.assertEqual(second["platform_revision"], 12)
        # 末帧保持稳定，不重新开始观测片段
        _, stable = _http(self.base, "/commands/" + cmd["id"])
        self.assertEqual(stable["state"], "restricted")

    def test_undefined_flow_rejected(self):
        status, body = _http(self.base, "/commands", {
            "command_id": "not-defined", "operation": "restrict",
            "request": {}})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
