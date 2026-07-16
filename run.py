"""
run.py — experiment pipeline runner (shim).

Usage:
    python run.py "<github_url>" "<la_page_1>" "<la_page_2>" ...   # full pipeline
    python run.py "<la_page_1>" "<la_page_2>" ...                   # LabArchives-only

Implementation lives in lab_agent/cli/run_pipeline.py.
"""
from lab_agent.cli.run_pipeline import main

if __name__ == "__main__":
    main()
