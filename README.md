# 影视保护通知的授权范围与异议复核

作品权利、委托、发布项和异议送达资料均为演练。发布项由平台、链接标识和内容摘要共同识别。

当前发布内容是合成资料及协作方协议回放工具。业务办理服务尚未提供。

使用 Python 3.11，无需安装第三方包。领域字段及协作方约定见 [资料约定](docs/contracts.md)。`data/catalog.json` 保存原始记录和演练角色，`partner/commands.json` 保存外部已定义流水的观测片段。它们描述输入事实，不代表项目已对任何申请作出决定。

运行资料检查：

```sh
python3 -m unittest discover -s tests -v
```

启动本地协作方：

```sh
python3 partner/server.py --port 8765 --state partner-state.sqlite3
```

启动后输出实际端口，`GET /health` 可查询存活。按 `docs/contracts.md` 发起协议请求及查询；重启使用同一状态文件会保留已受理流水。状态文件可在演练结束后另行留存，换全新文件可重新开始独立演练。业务数据和协议回放数据应分别保管。
