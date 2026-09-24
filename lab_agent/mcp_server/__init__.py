"""LORE's read-only knowledge tools, served over MCP to another agent (e.g. a measurement
agent on the lab's DAQ machine). See ``docs/mcp_ssh_setup.md``.

Deliberately imports nothing: ``core`` stays importable without the MCP SDK, and the
server module is only loaded when something actually serves.
"""
