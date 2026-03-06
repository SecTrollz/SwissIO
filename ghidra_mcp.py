"""
SwissIO local Ghidra MCP bridge metadata.

This module keeps all Ghidra/MCP assumptions local-first and explicit.
It does not perform remote calls.
"""

from dataclasses import dataclass
import os


@dataclass
class GhidraMCPConfig:
    enabled: bool
    server_name: str
    endpoint: str
    project_dir: str


def load_ghidra_mcp_config() -> GhidraMCPConfig:
    return GhidraMCPConfig(
        enabled=os.getenv("SWISSIO_GHIDRA_MCP_ENABLED", "0") == "1",
        server_name=os.getenv("SWISSIO_GHIDRA_MCP_SERVER", "ghidra-local"),
        endpoint=os.getenv("SWISSIO_GHIDRA_MCP_ENDPOINT", "mcp://ghidra-local"),
        project_dir=os.getenv("SWISSIO_GHIDRA_PROJECT_DIR", "./ghidra_projects"),
    )


def describe_ghidra_mcp(config: GhidraMCPConfig) -> str:
    status = "enabled" if config.enabled else "disabled"
    return (
        f"Ghidra MCP is {status} | server={config.server_name} | "
        f"endpoint={config.endpoint} | project_dir={config.project_dir}"
    )
