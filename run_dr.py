"""
run_dr.py — standalone DR conditions runner (shim).

Usage:
    python run_dr.py "2025-02-18"
    python run_dr.py "2025-02-18" --hours 24
    python run_dr.py "2025-02-18 14:00" "2025-02-18 22:00"

Implementation lives in lab_agent/cli/run_dr.py.
"""
from lab_agent.cli.run_dr import main

if __name__ == "__main__":
    main()
