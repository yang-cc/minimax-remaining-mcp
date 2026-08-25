@echo off
REM Launch the minimax-remaining-mcp MCP server (stdio transport).
REM Used by DSH / Claude Desktop / Cursor as the MCP server command line.
REM
REM The trailing "." on %~dp0 strips the trailing backslash so the quoted
REM interpreter path does not accidentally swallow the next arg.
REM
REM -u makes Python stdio unbuffered so MCP client logs show up live.
setlocal
"%~dp0.venv\Scripts\python.exe" -u -m minimax_remaining_mcp.server
endlocal