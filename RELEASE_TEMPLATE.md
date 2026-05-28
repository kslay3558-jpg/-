## ⚠️ Important Notice (Antivirus False Positives)

This application modifies Windows registry IRQ affinity settings (HKLM) and may be flagged by antivirus products as a false positive due to:

- PyInstaller single-file executable packaging
- Elevated administrator privileges (UAC)
- PowerShell-based hardware/device queries
- Registry writes for IRQ policy values

This project is open source and all behavior can be reviewed in this repository before running.

## What this release contains

- `IRQOptimizer.exe` (built via `build.spec`)

## Safety and usage reminders

- Windows 10/11 only
- Run as Administrator
- Reboot required after Apply / Undo / Reset actions
- Use at your own risk (see README and LICENSE disclaimer)
