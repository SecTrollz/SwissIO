# SwissIO Right-to-Repair Playbook

SwissIO is designed for **repair, verification, and repurposing** through interfaces that hardware already exposes.

## What SwissIO helps with

- Detecting device identity and connection paths (`scan`, `ports`).
- Auditing available interfaces (serial, DFU, JTAG/OpenOCD, vendor USB endpoints).
- Reading diagnostics, firmware dumps, and protocol traces.
- Performing intentional writes/flashes only when explicitly armed by the user.

## Practical workflow for old/unsupported devices

1. **Identify paths and mode**
   - Run `scan` and `ports`.
   - Note VID:PID, location path, and serial path.
2. **Check recovery channels first**
   - DFU / boot ROM / fastboot / vendor restore modes.
3. **Create backups before changes**
   - Firmware read/dump where supported.
4. **Then apply writes intentionally**
   - Enable ARM and Tier 3 before any write/flash command.
5. **Record everything**
   - Save command logs and dumps to support reproducibility.

## Security boundary statement

SwissIO operates through host OS permissions and available protocol channels. It does not claim to bypass secure enclaves, kernel hardening, or cryptographic trust roots.

That boundary keeps the tool practical, transparent, and useful for lawful repair workflows.
