"""Register FastMCP middleware based on server configuration."""

from fastmcp import FastMCP

from src.config.server_config import ServerConfig
from src.server_components.account_tool_prefix_middleware import (
    AccountPrefixedToolsMiddleware,
)
from src.server_components.audit_middleware import AuditMiddleware


def register_mcp_middleware(mcp: FastMCP, config: ServerConfig) -> None:
    """Attach optional FastMCP middleware when enabled in config."""
    if config.prefix_mcp_tools_with_account:
        mcp.add_middleware(AccountPrefixedToolsMiddleware())
    # Added last so it sits inside the prefixing middleware and records the
    # internal tool name — the journal must not depend on how a client spells it.
    if config.audit_dsn:
        mcp.add_middleware(AuditMiddleware(config.audit_dsn, config.audit_service))
