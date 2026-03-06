"""
SwissIO local assistant orchestration.

Purpose:
- Interpret terminal output for users.
- Suggest next safe command choices.
- Run logic-check gates before recommending write/flash paths.

This is local-first scaffolding for an on-device model runner
(e.g., quantized DeepSeek Coder behind a local runtime).
"""

from dataclasses import dataclass
from typing import List


@dataclass
class SessionContext:
    profile: str
    armed: bool
    tier: int


@dataclass
class LogicGateResult:
    ok: bool
    reason: str


class LocalAssistantOrchestrator:
    def __init__(self, model_name: str = "deepseek-coder-local-quantized"):
        self.model_name = model_name

    def status(self) -> str:
        return (
            "Local AI status: configured for offline interpretation mode | "
            f"model={self.model_name}"
        )

    def logic_check(self, command: str, ctx: SessionContext) -> LogicGateResult:
        destructive_prefixes = ["dfu flash", "jtag flash", "mem write"]
        is_destructive = any(command.startswith(prefix) for prefix in destructive_prefixes)

        if not is_destructive:
            return LogicGateResult(True, "non-destructive command")

        if not ctx.armed:
            return LogicGateResult(False, "blocked: ARM is off")
        if ctx.tier != 3:
            return LogicGateResult(False, "blocked: tier 3 required for write/flash")
        return LogicGateResult(True, "destructive command passes session safety gates")

    def explain_terminal_output(self, terminal_excerpt: str, ctx: SessionContext) -> str:
        excerpt = (terminal_excerpt or "").strip()
        if not excerpt:
            return "No terminal output provided. Run scan or probe commands first."

        hints: List[str] = []
        lower = excerpt.lower()

        if "dfu" in lower:
            hints.append("DFU path detected. Next: dfu list, then dfu read for backup before flash.")
        if "/dev/tty" in lower or "serial" in lower:
            hints.append("Serial path detected. Next: uart baud <port>, then uart open <port> <baud>.")
        if "stlink" in lower or "jtag" in lower:
            hints.append("Debug path detected. Next: jtag probe <iface> <target>.")
        if "no" in lower and "found" in lower:
            hints.append("No device found in current output. Re-run scan and verify cable/data mode.")

        if not hints:
            hints.append("Output parsed. Next safest step: run scan, then mem probe for endpoint visibility.")

        hints.append(
            f"Session profile={ctx.profile}, armed={'yes' if ctx.armed else 'no'}, tier={ctx.tier}."
        )
        return " ".join(hints)
