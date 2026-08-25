"""minimax-remaining-mcp 鈥?MCP server for the MiniMax Token Plan.

Provides tools for AI agents to:

- Query remaining Token Plan credits via the persistent Camoufox
  session (anti-Cloudflare Firefox fingerprint).
- Detect low-credit thresholds in the 5h fixed window and recommend
  a pause.
- Self-report per-call consumption into an agent-local observation
  window for pacing.
- Trigger a fresh interactive login when cookies expire.

Design notes
------------

- Camoufox keeps a persistent Firefox profile at ``data/profile/`` so
  the session cookies survive MCP server restarts. The browser is only
  launched for a fresh login 鈥?never on the hot path.
- The Coding Plan API has no Bearer-key auth path. The web console's
  ``api_key`` field is not a Coding Plan subscription key (it returns
  ``base_resp = {2062, "no active token plan"}``). Only the web
  session cookies work.
- The 5h window is a **fixed** window aligned to Beijing time 鈥?typical
  boundaries are 10:00 / 15:00 / 20:00 CST etc. (per the official
  Token Plan docs: "濂楅鍐呴搴﹀彈 5 灏忔椂鍥哄畾绐楀彛鍜屽懆绐楀彛鎺у埗"). Unused quota
  does **not** roll over to the next cycle.
- All persistent state lives as plain JSON in ``data/``. No database.
"""

__version__ = "0.1.1"