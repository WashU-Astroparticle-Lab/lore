"""
upload_to_labarchives.py — upload a finished experiment report to LabArchives (shim).

Usage:
    python upload_to_labarchives.py <out_dir>

Implementation lives in lab_agent/cli/upload.py.
"""
from lab_agent.cli.upload import main

if __name__ == "__main__":
    main()
