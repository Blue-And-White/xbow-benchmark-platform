# xbow CTF MCP Server

把 xbow CTF 平台的做题 API 封装为 MCP (Model Context Protocol) server，让 AI agent 通过标准 MCP 协议做题。

## 快速接入

在你的 agent 客户端（如 Claude Desktop、opencode 等）的 MCP 配置中加入：

```json
{
  "mcpServers": {
    "xbow-ctf": {
      "command": "python",
      "args": ["/path/to/mcp/server.py"],
      "env": {
        "XBOW_PLATFORM_URL": "http://121.5.30.191:6888",
        "XBOW_API_KEY": "xben_你的api_key"
      }
    }
  }
}
```

> api-key 从平台的「我的解题表」页面获取（每张看板一个独立 key）。

## 提供的 Tools

| Tool | 说明 |
|---|---|
| `list_challenges` | 列出 104 题 + 当前状态（不暴露考点/tags/title） |
| `start_challenge` | 启动一题，返回靶机 URL（反代，公网可达） |
| `submit_flag` | 提交 flag；正确则自动关闭容器 |
| `stop_challenge` | 手动放弃/关闭一题 |

## 典型做题流程（agent 视角）

```
1. list_challenges         → 看哪些题没做
2. start_challenge(XBEN-001-24) → 拿到靶机 URL
3. (访问靶机 URL 解题, 拿到 flag)
4. submit_flag(XBEN-001-24, "FLAG{...}") → 对了自动关
```

## 安全

- api-key 是唯一鉴权（绑定到某张解题表）
- MCP server 不暴露平台管理功能（admin/sheets 管理不走 MCP）
- 靶机考点信息（tags/title/description）不通过 MCP 返回
