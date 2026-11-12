@echo off
rem MCP server for STEP Studio (stdio). Register it in Claude Code with:
rem   claude mcp add steprecon -- cmd /c "%~dp0mcp.cmd"
call "%~dp0fc.cmd" mcp_server.py %*
