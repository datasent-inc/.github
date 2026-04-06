#!/usr/bin/env python3
"""Sync Asana project tasks to the GitHub Projects board.

Architecture:
    Asana (product)  -->  GitHub Projects board  -->  GitHub Issues (engineering)
                          (project-planning)

This script syncs Asana tasks to the GitHub Projects board as either:
- Linked items (for tasks that already have GitHub issues)
- Draft items (for tasks without issues -- visible to engineering for triage)

Engineering owns issue creation. Asana never creates repo issues directly.
The engineering manager reviews draft items on the board and converts them
to real issues when ready.

Metadata synced:
- Priority (Asana notes -> board Priority field + issue labels)
- Size (Asana notes -> board Size field + issue labels)
- Status (Asana section/notes -> board Status field)

Usage:
    Normally runs via GitHub Actions (see workflows/asana-board-sync.yml).
    The ASANA_PAT org secret is injected automatically.

    # Manual local run (if needed)
    export ASANA_PAT="<your-token>"
    python3 sync_asana_github.py --dry-run

Requires: gh CLI authenticated with project scope
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PAT = os.environ.get("ASANA_PAT")
if not PAT:
    sys.exit("ERROR: set ASANA_PAT environment variable")

ASANA_PROJECT = "1213923225906735"
ASANA_API = "https://app.asana.com/api/1.0"
ASANA_HEADERS = {
    "Authorization": f"Bearer {PAT}",
    "Content-Type": "application/json",
}

GH_ORG = "datasent-inc"
GH_REPO = "datasent-inc/polygen-sdk"
GH_PROJECT_NUM = 3
GH_PROJECT_ID = "PVT_kwDOBeAGa84A6JkY"

# GitHub Projects v2 field IDs (polygen-sdk project-planning, project #3)
STATUS_FIELD_ID = "PVTSSF_lADOBeAGa84A6JkYzgu03Fo"
PRIORITY_FIELD_ID = "PVTSSF_lADOBeAGa84A6JkYzgu04D0"
SIZE_FIELD_ID = "PVTSSF_lADOBeAGa84A6JkYzgu04D4"

STATUS_IDS = {
    "Needs Review": "63dc9bbd",
    "Backlog": "f75ad846",
    "Ready": "08afe404",
    "In progress": "47fc9ee4",
    "In review": "4cc61d42",
    "Done": "98236657",
}
PRIORITY_IDS = {
    "Stopper": "0d1d304e",
    "High": "33850bb5",
    "Medium": "fd166021",
    "Low": "8b0e7b8a",
}
SIZE_IDS = {
    "XS": "eff732af",
    "S": "9592a5a3",
    "M": "9728cbdc",
    "L": "c53df028",
    "XL": "7b141a16",
}

# Asana tasks that already have GitHub issues.
# Maps Asana task GID -> list of GitHub issue numbers (polygen-sdk repo).
EXISTING_LINKS: dict[str, list[int]] = {
    "1213923225906744": [2],           # 2.1-2.2 MDL -> #2
    "1213923225906746": [96, 18],      # 2.3-2.4 Adaptive seg -> #96, #18
    "1213923225906753": [30],          # 3.1 ML Feature Token -> #30
    "1213923225906757": [4],           # 3.3 Three operating modes -> #4
    "1213923157386223": [86],          # 4.1-4.2 Token algebra -> #86
    "1213923157386217": [17],          # 5.1 Attestation -> #17
    "1213923157386219": [16, 17],      # 5.2 Residual-only -> #16, #17
    "1213923157386228": [22],          # Compression metrics -> #22
    "1213923157386230": [75],          # Quantization rules -> #75
    "1213923157386232": [92],          # Research quantization -> #92
    "1213923157386234": [77],          # BinaryEncoder quantizes -> #77
    "1213923157386236": [3, 16],       # Lossless tokenization -> #3, #16
    "1213923157386252": [51, 53, 43, 55, 56, 57, 80],  # Docs suite
    "1213923157386254": [63, 87, 88, 83],  # Performance testing
    "1213923157386256": [64],          # TikTok data -> #64
    "1213923157386258": [60],          # Process non-numeric -> #60
    "1213923157386250": [14],          # Metadata Structure -> #14
    "1213959278922765": [9],           # ML datasets -> #9
    "1213959278924092": [29],          # SdkClient -> #29
    "1213960362702160": [58],          # CUDA testing -> #58
    "1213962216589643": [65],          # License logic -> #65
    "1213959278982437": [93],          # Pin GH Actions -> #93
}

# ---------------------------------------------------------------------------
# Mapping helpers
# ---------------------------------------------------------------------------

# Priority: parse from Asana notes ("Priority: P0 Critical", "Priority: P1 High", etc.)
_PRIORITY_RE = re.compile(
    r"Priority:\s*P([0-4])\s*(?:Critical|High|Medium|Low)?",
    re.IGNORECASE,
)
_PRIORITY_MAP = {
    "0": ("Stopper", "high"),      # P0 -> Stopper on board, "high" label
    "1": ("High", "high"),         # P1 -> High on board
    "2": ("Medium", "medium"),     # P2 -> Medium on board
    "3": ("Low", "low"),           # P3 -> Low on board
    "4": ("Low", "low"),           # P4 -> Low on board
}

# Size: parse from notes or use section-based defaults
_SIZE_RE = re.compile(r"\bSize:\s*(XS|S|M|L|XL)\b", re.IGNORECASE)
_SIZE_LABEL_MAP = {
    "XS": "extra-small",
    "S": "small",
    "M": None,         # no "medium" size label -- skip
    "L": "large",
    "XL": "extra-large",
}

# Status: infer from notes and completion state
_STATUS_RE = re.compile(r"Status:\s*(\w[\w\s]*\w|\w)", re.IGNORECASE)
_STATUS_KEYWORD_MAP = {
    "implemented": "In review",
    "ready": "Ready",
    "in progress": "In progress",
    "done": "Done",
    "complete": "Done",
}

# Section -> default board status (for tasks without explicit status)
_SECTION_DEFAULT_STATUS = {
    "Core Framework (P0)": "Backlog",
    "Feature Tokens (P1)": "Backlog",
    "Trusted Setup & Governance (P1)": "Backlog",
    "Compression & Encoder": "Backlog",
    "Multi-Modal & LLM (P2+)": "Needs Review",
    "SDK Infrastructure": "Backlog",
    "Untitled section": "Needs Review",
}


def parse_priority(notes: str) -> tuple[str | None, str | None]:
    """Return (board_priority, label) from Asana notes."""
    m = _PRIORITY_RE.search(notes)
    if m:
        return _PRIORITY_MAP.get(m.group(1), (None, None))
    return None, None


def parse_size(notes: str) -> tuple[str | None, str | None]:
    """Return (board_size, label) from Asana notes."""
    m = _SIZE_RE.search(notes)
    if m:
        key = m.group(1).upper()
        return key, _SIZE_LABEL_MAP.get(key)
    return None, None


def parse_status(notes: str, completed: bool, section_name: str) -> str:
    """Return board status string."""
    if completed:
        return "Done"
    m = _STATUS_RE.search(notes)
    if m:
        val = m.group(1).strip().lower()
        for keyword, status in _STATUS_KEYWORD_MAP.items():
            if keyword in val:
                return status
    return _SECTION_DEFAULT_STATUS.get(section_name, "Needs Review")


# ---------------------------------------------------------------------------
# Shell / API helpers
# ---------------------------------------------------------------------------

DRY_RUN = False


def run(cmd: list[str], *, allow_fail: bool = False) -> str:
    """Run a shell command and return stdout."""
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 and not allow_fail:
        print(f"  CMD FAILED: {' '.join(cmd[:6])}")
        print(f"    {result.stderr.strip()}")
    return result.stdout.strip()


def gh_set_field(
    item_id: str,
    field_id: str,
    option_id: str,
) -> None:
    """Set a single-select field on a GitHub Projects item."""
    if DRY_RUN:
        print(f"    [dry-run] set field {field_id} = {option_id} on {item_id}")
        return
    run([
        "gh", "project", "item-edit",
        "--project-id", GH_PROJECT_ID,
        "--id", item_id,
        "--field-id", field_id,
        "--single-select-option-id", option_id,
    ])


def gh_add_label(issue_num: int, label: str) -> None:
    """Add a label to a GitHub issue (idempotent)."""
    if DRY_RUN:
        print(f"    [dry-run] add label '{label}' to issue #{issue_num}")
        return
    run([
        "gh", "issue", "edit", str(issue_num),
        "--repo", GH_REPO,
        "--add-label", label,
    ], allow_fail=True)


def gh_add_issue_to_board(issue_url: str) -> str | None:
    """Add an existing issue to the Projects board. Returns item node ID."""
    if DRY_RUN:
        print(f"    [dry-run] add {issue_url} to board")
        return None
    raw = run([
        "gh", "project", "item-add", str(GH_PROJECT_NUM),
        "--owner", GH_ORG, "--url", issue_url, "--format", "json",
    ])
    try:
        return json.loads(raw).get("id")
    except (json.JSONDecodeError, TypeError):
        return None


def gh_create_draft(title: str, body: str) -> str | None:
    """Create a draft issue on the Projects board. Returns item node ID."""
    if DRY_RUN:
        print(f"    [dry-run] create draft: {title}")
        return None
    raw = run([
        "gh", "project", "item-create", str(GH_PROJECT_NUM),
        "--owner", GH_ORG,
        "--title", title,
        "--body", body,
        "--format", "json",
    ])
    try:
        return json.loads(raw).get("id")
    except (json.JSONDecodeError, TypeError):
        return None


def asana_get_tasks() -> list[dict]:
    """Fetch all tasks from the Asana project."""
    url = (
        f"{ASANA_API}/tasks?project={ASANA_PROJECT}"
        f"&opt_fields=name,notes,completed,memberships.section.name,assignee.name"
        f"&limit=100"
    )
    req = urllib.request.Request(url, headers=ASANA_HEADERS)
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())
    return data.get("data", [])


# ---------------------------------------------------------------------------
# Main sync logic
# ---------------------------------------------------------------------------

def sync_linked_issues(tasks_by_gid: dict[str, dict]) -> None:
    """For Asana tasks with linked GitHub issues: add to board, set fields, sync labels."""
    print("=" * 60)
    print("STEP 1: Sync linked issues to Projects board")
    print("=" * 60)

    for task_gid, issue_nums in EXISTING_LINKS.items():
        task = tasks_by_gid.get(task_gid)
        if not task:
            print(f"  WARN: Asana task {task_gid} not found in project")
            continue

        notes = task.get("notes", "")
        completed = task.get("completed", False)
        section_name = ""
        memberships = task.get("memberships", [])
        if memberships:
            section_name = memberships[0].get("section", {}).get("name", "")

        board_priority, priority_label = parse_priority(notes)
        board_size, size_label = parse_size(notes)
        board_status = parse_status(notes, completed, section_name)

        for issue_num in issue_nums:
            issue_url = f"https://github.com/{GH_REPO}/issues/{issue_num}"
            print(f"  #{issue_num} ({task['name'][:50]})")

            # Add to board
            item_id = gh_add_issue_to_board(issue_url)

            # Set board fields
            if item_id:
                if board_status and board_status in STATUS_IDS:
                    gh_set_field(item_id, STATUS_FIELD_ID, STATUS_IDS[board_status])
                if board_priority and board_priority in PRIORITY_IDS:
                    gh_set_field(item_id, PRIORITY_FIELD_ID, PRIORITY_IDS[board_priority])
                if board_size and board_size in SIZE_IDS:
                    gh_set_field(item_id, SIZE_FIELD_ID, SIZE_IDS[board_size])

            # Sync labels
            if priority_label:
                gh_add_label(issue_num, priority_label)
            if size_label:
                gh_add_label(issue_num, size_label)

    print()


def sync_draft_items(tasks_by_gid: dict[str, dict]) -> None:
    """For Asana tasks without linked issues: create draft items on the board."""
    print("=" * 60)
    print("STEP 2: Create draft items for unlinked Asana tasks")
    print("=" * 60)

    unlinked = {
        gid: task for gid, task in tasks_by_gid.items()
        if gid not in EXISTING_LINKS
    }

    if not unlinked:
        print("  No unlinked tasks found.")
        print()
        return

    for task_gid, task in unlinked.items():
        name = task.get("name", "")
        notes = task.get("notes", "")
        completed = task.get("completed", False)
        section_name = ""
        memberships = task.get("memberships", [])
        if memberships:
            section_name = memberships[0].get("section", {}).get("name", "")

        assignee_name = ""
        if task.get("assignee"):
            assignee_name = task["assignee"].get("name", "")

        board_priority, _ = parse_priority(notes)
        board_size, _ = parse_size(notes)
        board_status = parse_status(notes, completed, section_name)

        # Build draft body
        asana_url = f"https://app.asana.com/0/{ASANA_PROJECT}/{task_gid}"
        body_lines = [
            f"Asana: {asana_url}",
            f"Section: {section_name}" if section_name else "",
            f"Assignee: {assignee_name}" if assignee_name else "",
            "",
            notes[:500] if notes else "(no description)",
        ]
        body = "\n".join(line for line in body_lines if line or line == "")

        print(f"  {name[:60]:<60s}  [{board_status}]")

        item_id = gh_create_draft(name, body)
        if item_id:
            if board_status and board_status in STATUS_IDS:
                gh_set_field(item_id, STATUS_FIELD_ID, STATUS_IDS[board_status])
            if board_priority and board_priority in PRIORITY_IDS:
                gh_set_field(item_id, PRIORITY_FIELD_ID, PRIORITY_IDS[board_priority])
            if board_size and board_size in SIZE_IDS:
                gh_set_field(item_id, SIZE_FIELD_ID, SIZE_IDS[board_size])

    print()


def main() -> None:
    global DRY_RUN

    parser = argparse.ArgumentParser(
        description="Sync Asana project to GitHub Projects board",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would happen without making changes",
    )
    args = parser.parse_args()
    DRY_RUN = args.dry_run

    if DRY_RUN:
        print("[DRY RUN MODE - no mutations will be made]\n")

    # Fetch Asana tasks
    print("Fetching Asana tasks...")
    tasks = asana_get_tasks()
    tasks_by_gid = {t["gid"]: t for t in tasks}
    print(f"  Found {len(tasks)} tasks in Asana project\n")

    # Step 1: Sync linked issues
    sync_linked_issues(tasks_by_gid)

    # Step 2: Create drafts for unlinked tasks
    sync_draft_items(tasks_by_gid)

    # Summary
    linked_count = sum(len(nums) for nums in EXISTING_LINKS.values())
    unlinked_count = len(tasks_by_gid) - len(
        [g for g in EXISTING_LINKS if g in tasks_by_gid]
    )
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"  Linked issues synced to board: {linked_count}")
    print(f"  Draft items created on board:  {unlinked_count}")
    print(f"  Total Asana tasks:             {len(tasks)}")
    if DRY_RUN:
        print("\n  [DRY RUN - no changes were made]")
    print()


if __name__ == "__main__":
    main()
