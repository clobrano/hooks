# -*- coding: utf-8 -*-
"""Shared constants, helpers, and feature functions for Taskwarrior hooks."""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, time, timezone

import pytz
import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

JIRA_BASE_URL = "https://issues.redhat.com/browse/"
GITHUB_BASE_URL = "https://github.com/"
GITLAB_BASE_URL = "https://gitlab.cee.redhat.com/"

JIRA_ID_REGEX = r"\[([A-Z]+-\d+)\]"
PR_ID_REGEX = r"\[([a-zA-Z0-9_-]+)/([a-zA-Z0-9_-]+) PR(\d+)\]"
MR_ID_REGEX = r"\[([a-zA-Z0-9_-]+)/([a-zA-Z0-9_-]+) MR(\d+)\]"
TICKET_ID_REGEX = r"\[([a-zA-Z0-9_-]+)/([a-zA-Z0-9_-]+) I(\d+)\]"

TAG_SIGN_MAP = {
    "closed": "#CLOSED",
    "codereview": "#CODEREVIEW",
    "duplicate": "#DUPLICATE",
    "epic": "#EPIC",
    "handedoff": "#HANDED-OFF",
    "hold": "#HOLD",
    "meeting": "#MEETING",
    "merged": "#MERGED",
    "notbug": "#NOT-BUG",
    "review": "#IN-REVIEW",
    "wait": "#WAIT-FEEDBACK",
    "wfa": "#WAIT-AUTHOR",
    "wontdo": "#WONTDO",
}

DEFAULT_DUE_HOUR = 17
DEFAULT_DUE_MINUTE = 0
DEFAULT_DUE_SECOND = 0

TAGS_TO_REMOVE_ON_DONE = [
    "doing",
    "hold",
    "review",
    "wait",
    "wfa",
]

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def find_annotation(annotations, prefix):
    """Find an existing annotation by prefix (e.g. "JIRA: ", "PR: ").

    Returns (index, description_value) or (-1, None) if not found.
    """
    for i, item in enumerate(annotations):
        if isinstance(item, dict) and item.get("description", "").startswith(prefix):
            return i, item["description"]
    return -1, None


def add_annotation(annotations, description):
    """Append an annotation with a UTC timestamp.

    Returns a status message string.
    """
    annotations.append({
        "description": description,
        "entry": datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'),
    })
    return f"Added annotation: {description}"


def is_local_midnight(timestamp):
    """Check whether *timestamp* falls on local midnight."""
    local_zone = datetime.now().astimezone().tzinfo
    return timestamp.astimezone(local_zone).time() == time(0, 0, 0)


def set_default_time(timestamp):
    """Replace the time component of *timestamp* with the configured default."""
    local_zone = datetime.now().astimezone().tzinfo
    return timestamp.astimezone(local_zone).replace(
        hour=DEFAULT_DUE_HOUR,
        minute=DEFAULT_DUE_MINUTE,
        second=DEFAULT_DUE_SECOND,
    )


# ---------------------------------------------------------------------------
# URL builders
# ---------------------------------------------------------------------------

def build_jira_url(description):
    """Extract a JIRA ID from *description* and return the full URL, or None."""
    match = re.search(JIRA_ID_REGEX, description)
    if match:
        return f"{JIRA_BASE_URL}{match.group(1)}"
    return None


def build_github_pr_url(description):
    """Extract a PR reference from *description* and return the full URL, or None."""
    match = re.search(PR_ID_REGEX, description)
    if match:
        org, proj, num = match.group(1), match.group(2), match.group(3)
        return f"{GITHUB_BASE_URL}{org}/{proj}/pull/{num}"
    return None


def build_issue_url(description):
    """Extract an issue reference from *description* and return the full URL, or None."""
    match = re.search(TICKET_ID_REGEX, description)
    if match:
        org, proj, num = match.group(1), match.group(2), match.group(3)
        return f"{GITHUB_BASE_URL}{org}/{proj}/issues/{num}"
    return None


def build_gitlab_pr_url(description):
    """Extract a MR reference from *description* and return the full URL, or None."""
    match = re.search(MR_ID_REGEX, description)
    if match:
        org, proj, num = match.group(1), match.group(2), match.group(3)
        return f"{GITLAB_BASE_URL}{org}/{proj}/-/merge_requests/{num}"
    return None


# ---------------------------------------------------------------------------
# Shared feature functions (used by both on-add and on-modify)
# ---------------------------------------------------------------------------

def gmail_link(task):
    """Add a GMAIL annotation when the 'email' tag is present."""
    messages = []
    tags = task.get("tags", [])
    description = task.get("description", "")
    annotations = task.get("annotations", [])

    if "email" not in tags:
        return messages

    # strip project prefix from description
    project = task.get("project", "")
    prefix = _project_prefix(project)
    if prefix and description.startswith(prefix):
        description = description.lstrip(prefix)

    try:
        subprocess.run(["gmail-search-link.sh", description], check=True)
    except subprocess.CalledProcessError as e:
        print(f"Error running gmail-search-link.sh: {e}", file=sys.stderr)
    except FileNotFoundError:
        print("gmail-search-link.sh not found in PATH", file=sys.stderr)

    try:
        result = subprocess.run(["wl-paste"], capture_output=True, text=True, check=True)
        url = result.stdout.strip()

        if url:
            gmail_annotation = f"GMAIL: {url}"
            _, existing = find_annotation(annotations, "GMAIL: ")

            if existing == gmail_annotation:
                messages.append(f"Gmail annotation already exists: {gmail_annotation}")
            elif existing is None:
                messages.append(add_annotation(annotations, gmail_annotation))
    except subprocess.CalledProcessError as e:
        print(f"Error running wl-paste: {e}", file=sys.stderr)
    except FileNotFoundError:
        print("wl-paste not found in PATH", file=sys.stderr)

    task["annotations"] = annotations
    return messages


def fix_due_dates(task):
    """Set a default time on due dates that fall on local midnight."""
    messages = []
    due = task.get("due")
    if not due:
        return messages

    timestamp = datetime.strptime(due, '%Y%m%dT%H%M%SZ').replace(tzinfo=pytz.UTC)
    if is_local_midnight(timestamp):
        timestamp = set_default_time(timestamp)
        task["due"] = timestamp.strftime('%Y%m%dT%H%M%SZ')
        messages.append("Default due time has been set.")

    return messages


def add_signs(task):
    """Add sign strings to description based on tags (add-only, never removes)."""
    messages = []
    tags = task.get("tags", [])
    description = task.get("description", "")

    for tag, sign in TAG_SIGN_MAP.items():
        if tag in tags and sign not in description:
            description += f" {sign}"
            messages.append(f"Added '{sign}' sign for tag '{tag}'.")

    task["description"] = description
    return messages


def add_cve_tag(task):
    """Add +cve tag if description contains 'CVE'."""
    messages = []
    description = task.get("description", "")
    tags = task.get("tags", [])

    if "CVE" in description and "cve" not in tags:
        tags.append("cve")
        task["tags"] = tags
        messages.append("Added 'cve' tag (CVE detected in description).")

    return messages


# ---------------------------------------------------------------------------
# Project prefix helpers
# ---------------------------------------------------------------------------

def _project_prefix(project):
    """Derive the description prefix from a project name.

    Strips the top-level component for hierarchical projects:
      'A.B.C' -> 'B.C'
      'A.B'   -> 'B'
      'A'     -> 'A'
    Returns the prefix string (without the trailing ': ') or None if *project*
    is empty.
    """
    if not project:
        return None
    parts = project.split(".")
    return ".".join(parts[1:]) if len(parts) > 1 else parts[0]


# ---------------------------------------------------------------------------
# on-add-only feature functions
# ---------------------------------------------------------------------------

def jira_link_add(task):
    """Add JIRA annotation and UDA on task creation."""
    messages = []
    description = task.get("description", "")
    annotations = task.get("annotations", [])

    url = build_jira_url(description)
    if not url:
        return messages

    jira_annotation = f"JIRA: {url}"
    _, existing = find_annotation(annotations, "JIRA: ")

    if existing == jira_annotation:
        messages.append(f"Jira annotation already exists and is correct: {jira_annotation}")
    elif existing is None:
        messages.append(add_annotation(annotations, jira_annotation))
        messages[-1] = f"Added Jira annotation: {jira_annotation}"

    task["jira"] = url
    task["annotations"] = annotations
    return messages


def issue_link_add(task):
    """Add TICKET annotation on task creation."""
    messages = []
    description = task.get("description", "")
    annotations = task.get("annotations", [])

    url = build_issue_url(description)
    if not url:
        return messages

    ticket_annotation = f"TICKET: {url}"
    _, existing = find_annotation(annotations, "TICKET: ")

    if existing == ticket_annotation:
        messages.append(f"Ticket annotation already exists and is correct: {ticket_annotation}")
    elif existing is None:
        messages.append(add_annotation(annotations, ticket_annotation))
        messages[-1] = f"Added Ticket annotation: {ticket_annotation}"

    task["annotations"] = annotations
    return messages


def pr_link_add(task):
    """Add PR annotation and UDA on task creation."""
    messages = []
    description = task.get("description", "")
    annotations = task.get("annotations", [])

    url = build_github_pr_url(description)
    if not url:
        return messages

    pr_annotation = f"PR: {url}"
    _, existing = find_annotation(annotations, "PR: ")

    if existing == pr_annotation:
        messages.append(f"PR annotation already exists and is correct: {pr_annotation}")
    elif existing is None:
        messages.append(add_annotation(annotations, pr_annotation))
        messages[-1] = f"Added PR annotation: {pr_annotation}"

    task["pr"] = url
    task["annotations"] = annotations
    return messages


def mr_link_add(task):
    """Add MR annotation and UDA on task creation."""
    messages = []
    description = task.get("description", "")
    annotations = task.get("annotations", [])

    url = build_gitlab_pr_url(description)
    if not url:
        return messages

    mr_annotation = f"MR: {url}"
    _, existing = find_annotation(annotations, "MR: ")

    if existing == mr_annotation:
        messages.append(f"MR annotation already exists and is correct: {mr_annotation}")
    elif existing is None:
        messages.append(add_annotation(annotations, mr_annotation))
        messages[-1] = f"Added MR annotation: {mr_annotation}"

    task["mr"] = url
    task["annotations"] = annotations
    return messages


def project_prefix_add(task):
    """Prefix description with the project name on task creation."""
    messages = []
    project = task.get("project", "")
    description = task.get("description", "")

    prefix = _project_prefix(project)
    if not prefix:
        return messages

    tag = f"{prefix}: "
    if not description.startswith(tag):
        task["description"] = f"{tag}{description}"
        messages.append(f"Prefixed description with '{prefix}'.")

    return messages


def block_tasks_add(task):
    """Make other tasks depend on this one if +blockN tags are present."""
    messages = []
    tags = task.get("tags", [])
    this_uuid = task.get("uuid")

    if not this_uuid:
        return messages

    targets = [t[5:] for t in tags if t.startswith("block") and len(t) > 5]

    for target in targets:
        try:
            subprocess.run(
                ["task", target, "mod", f"depends:{this_uuid}", "rc.confirmation=no"],
                check=True,
                capture_output=True
            )
            messages.append(f"Task {target} now depends on this task.")
        except subprocess.CalledProcessError as e:
            err = e.stderr.decode().strip() if e.stderr else "unknown error"
            print(f"Error blocking task {target}: {err}", file=sys.stderr)

    return messages


# ---------------------------------------------------------------------------
# on-modify-only feature functions
# ---------------------------------------------------------------------------

def jira_link_modify(before, after):
    """Aggressive JIRA annotation management: add, update, or remove."""
    messages = []
    description = after.get("description", "")
    annotations = after.get("annotations", [])

    url = build_jira_url(description)
    idx, existing = find_annotation(annotations, "JIRA: ")

    if url:
        new_annotation = f"JIRA: {url}"
        if existing == new_annotation:
            messages.append(f"Jira annotation already exists and is correct: {new_annotation}")
        elif existing and existing != new_annotation:
            annotations[idx]["description"] = new_annotation
            annotations[idx]["entry"] = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            messages.append(f"Updated Jira annotation to: {new_annotation}")
        else:
            messages.append(add_annotation(annotations, new_annotation))
            messages[-1] = f"Added Jira annotation: {new_annotation}"
        after["jira"] = url
    elif existing:
        del annotations[idx]
        messages.append(f"Removed Jira annotation: {existing}")
        after.pop("jira", None)

    after["annotations"] = annotations
    return messages


def issue_link_modify(before, after):
    """Conservative issue annotation management: add only, never remove."""
    messages = []
    description = after.get("description", "")
    annotations = after.get("annotations", [])

    url = build_issue_url(description)
    idx, existing = find_annotation(annotations, "TICKET: ")

    if url:
        new_annotation = f"TICKET: {url}"
        if not existing:
            messages.append(add_annotation(annotations, new_annotation))
            messages[-1] = f"Added Ticket annotation: {new_annotation}"
        else:
            messages.append(f"Ticket annotation for {existing} already exists. Not modifying or removing.")
    elif existing:
        messages.append(f"Existing Ticket annotation {existing} found, but no new Ticket ID in description. Not modifying or removing.")

    after["annotations"] = annotations
    return messages


def gitlab_mr_link_modify(before, after):
    """Conservative PR annotation management: add only, never remove. Syncs UDA."""
    messages = []
    description = after.get("description", "")
    annotations = after.get("annotations", [])

    url = build_gitlab_pr_url(description)
    idx, existing = find_annotation(annotations, "PR: ")

    if url:
        new_annotation = f"MR: {url}"
        if not existing:
            messages.append(add_annotation(annotations, new_annotation))
            messages[-1] = f"Added MR annotation: {new_annotation}"
        else:
            messages.append(f"MR annotation for {existing} already exists. Not modifying or removing.")
    elif existing:
        messages.append(f"Existing MR annotation {existing} found, but no new PR ID in description. Not modifying or removing.")

    # Sync UDA from whichever MR URL is active
    active_url = (f"MR: {url}" if url else None) or existing
    if active_url:
        after["mr"] = active_url.removeprefix("MR: ")
    else:
        after.pop("mr", None)

    after["annotations"] = annotations
    return messages


def github_pr_link_modify(before, after):
    """Conservative PR annotation management: add only, never remove. Syncs UDA."""
    messages = []
    description = after.get("description", "")
    annotations = after.get("annotations", [])

    url = build_github_pr_url(description)
    idx, existing = find_annotation(annotations, "PR: ")

    if url:
        new_annotation = f"PR: {url}"
        if not existing:
            messages.append(add_annotation(annotations, new_annotation))
            messages[-1] = f"Added PR annotation: {new_annotation}"
        else:
            messages.append(f"PR annotation for {existing} already exists. Not modifying or removing.")
    elif existing:
        messages.append(f"Existing PR annotation {existing} found, but no new PR ID in description. Not modifying or removing.")

    # Sync UDA from whichever PR URL is active
    active_url = (f"PR: {url}" if url else None) or existing
    if active_url:
        after["pr"] = active_url.removeprefix("PR: ")
    else:
        after.pop("pr", None)

    after["annotations"] = annotations
    return messages


def signs_from_tags_modify(before, after):
    """Bidirectional sign management: adds signs for present tags, removes for absent."""
    messages = []
    tags = after.get("tags", [])
    description = after.get("description", "")

    for tag, sign in TAG_SIGN_MAP.items():
        if tag in tags:
            if sign not in description:
                description += f" {sign}"
                messages.append(f"Added '{sign}' sign for tag '{tag}'.")
        else:
            if sign in description:
                parts = description.split(sign)
                description = " ".join(p.strip() for p in parts if p.strip())
                messages.append(f"Removed '{sign}' sign as tag '{tag}' is missing.")

    after["description"] = description
    return messages


def cleanup_on_done(before, after):
    """Remove specific tags when a task transitions to completed."""
    messages = []
    if after.get("status") != "completed" or before.get("status") == "completed":
        return messages

    tags = after.get("tags", [])
    removed = [t for t in TAGS_TO_REMOVE_ON_DONE if t in tags]
    if removed:
        after["tags"] = [t for t in tags if t not in TAGS_TO_REMOVE_ON_DONE]
        messages.append(f"Removed tags on completion: {', '.join(removed)}")

    return messages


def project_prefix_modify(before, after):
    """Update description prefix when the project changes."""
    messages = []
    old_project = before.get("project", "")
    new_project = after.get("project", "")
    description = after.get("description", "")

    old_prefix = _project_prefix(old_project)
    new_prefix = _project_prefix(new_project)

    # Strip existing prefix if present
    if old_prefix:
        old_tag = f"{old_prefix}: "
        if description.startswith(old_tag):
            description = description[len(old_tag):]

    # Apply new prefix
    if new_prefix:
        new_tag = f"{new_prefix}: "
        if not description.startswith(new_tag):
            description = f"{new_tag}{description}"
            messages.append(f"Prefixed description with '{new_prefix}'.")
    elif old_prefix:
        messages.append(f"Removed project prefix '{old_prefix}'.")

    after["description"] = description
    return messages


def timetracker(before, after):
    """Start/stop time tracking via the 'lets' CLI when task start changes."""
    messages = []
    taskrc = os.path.expanduser("~/.taskrc")

    if before.get("start") is None and after.get("start") is not None:
        subprocess.run(["task", f"rc:{taskrc}", "start.not:", "stop", "rc.confirmation=no"])
        description = f"{before.get('description', '')}"
        subprocess.run(["lets", "goto", description])
    elif before.get("start") is not None and after.get("start") is None:
        subprocess.run(["lets", "stop"])

    return messages


def update_slack_status(token, status_text):
    """Update Slack status via Web API."""
    try:
        payload = {
            "profile": {
                "status_text": status_text,
                "status_emoji": ""
            }
        }
        subprocess.run(
            ["curl", "-s", "-X", "POST",
             "-H", f"Authorization: Bearer {token}",
             "-H", "Content-Type: application/json",
             "--data", json.dumps(payload),
             "https://slack.com/api/users.profile.set"],
            check=False,
            capture_output=True
        )
    except Exception as e:
        print(f"Failed to update Slack status: {e}", file=sys.stderr)


def clear_slack_status(token):
    """Clear Slack status via Web API."""
    try:
        payload = {
            "profile": {
                "status_text": "",
                "status_emoji": ""
            }
        }
        subprocess.run(
            ["curl", "-s", "-X", "POST",
             "-H", f"Authorization: Bearer {token}",
             "-H", "Content-Type: application/json",
             "--data", json.dumps(payload),
             "https://slack.com/api/users.profile.set"],
            check=False,
            capture_output=True
        )
    except Exception as e:
        print(f"Failed to clear Slack status: {e}", file=sys.stderr)


def slack_status_update(before, after):
    """Update Slack status when task start/stop state changes."""
    messages = []
    token = os.environ.get("SLACK_TOKEN")

    if not token:
        return messages

    before_start = before.get("start")
    after_start = after.get("start")

    if before_start is None and after_start is not None:
        description = after.get("description", "")[:100]
        update_slack_status(token, description)
    elif before_start is not None and after_start is None:
        clear_slack_status(token)

    return messages


def block_tasks_modify(before, after):
    """Manage dependencies based on adding/removing +blockN tags."""
    messages = []
    before_tags = set(before.get("tags", []))
    after_tags = set(after.get("tags", []))
    this_uuid = after.get("uuid")

    if not this_uuid:
        return messages

    added_tags = after_tags - before_tags
    removed_tags = before_tags - after_tags

    added_targets = [t[5:] for t in added_tags if t.startswith("block") and len(t) > 5]
    removed_targets = [t[5:] for t in removed_tags if t.startswith("block") and len(t) > 5]

    for target in added_targets:
        try:
            subprocess.run(
                ["task", target, "mod", f"depends:{this_uuid}", "rc.confirmation=no"],
                check=True,
                capture_output=True
            )
            messages.append(f"Task {target} now depends on this task.")
        except subprocess.CalledProcessError as e:
            err = e.stderr.decode().strip() if e.stderr else "unknown error"
            print(f"Error blocking task {target}: {err}", file=sys.stderr)

    for target in removed_targets:
        try:
            subprocess.run(
                ["task", target, "mod", f"depends:-{this_uuid}", "rc.confirmation=no"],
                check=True,
                capture_output=True
            )
            messages.append(f"Task {target} no longer depends on this task.")
        except subprocess.CalledProcessError as e:
            err = e.stderr.decode().strip() if e.stderr else "unknown error"
            print(f"Error unblocking task {target}: {err}", file=sys.stderr)

    return messages


# ---------------------------------------------------------------------------
# URL transformation feature functions
# ---------------------------------------------------------------------------

def is_url_only(description):
    """Check if description is a URL and ONLY a URL (possibly with whitespace)."""
    stripped = description.strip()
    return bool(re.match(r'^https?://', stripped)) and ' ' not in stripped


def get_github_pr_metadata(url):
    """Fetch GitHub PR title from a GitHub PR URL.

    Returns (org, repo, pr_num, title) or None if not a valid PR URL.
    """
    match = re.match(r'https://github\.com/([^/]+)/([^/]+)/pull/(\d+)', url)
    if not match:
        return None

    org, repo, pr_num = match.groups()
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36'
        }
        response = requests.get(url, timeout=5, headers=headers)
        response.raise_for_status()
        # Scrape title from HTML - extract text before "by" (author line)
        title_match = re.search(r'<title>([^|]+?)\s+by\s+', response.text)
        if not title_match:
            # Fallback: just get first part of title before "·" or "|"
            title_match = re.search(r'<title>([^·|]+)', response.text)
        if title_match:
            title = title_match.group(1).strip()
            return org, repo, pr_num, title
    except Exception as e:
        print(f"Failed to fetch GitHub PR metadata from {url}: {e}", file=sys.stderr)

    return None


def get_gitlab_mr_metadata(url):
    """Fetch GitLab MR title from a GitLab MR URL.

    Returns (org, repo, mr_num, title) or None if not a valid MR URL.
    """
    match = re.match(r'https://gitlab\.com/([^/]+)/([^/]+)/-/merge_requests/(\d+)', url)
    if not match:
        return None

    org, repo, mr_num = match.groups()
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36'
        }
        response = requests.get(url, timeout=5, headers=headers)
        response.raise_for_status()
        title_match = re.search(r'<title>([^|]+)', response.text)
        if title_match:
            title = title_match.group(1).strip()
            return org, repo, mr_num, title
    except Exception as e:
        print(f"Failed to fetch GitLab MR metadata from {url}: {e}", file=sys.stderr)

    return None


def get_youtube_metadata(url):
    """Extract YouTube video ID and fetch title and duration.

    Returns (title, duration_minutes) or None on failure.
    """
    # Extract video ID from various YouTube URL formats
    video_id = None
    if 'youtube.com/watch' in url:
        match = re.search(r'[?&]v=([^&]+)', url)
        if match:
            video_id = match.group(1)
    elif 'youtu.be' in url:
        match = re.search(r'youtu\.be/([^/?]+)', url)
        if match:
            video_id = match.group(1)

    if not video_id:
        return None

    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36'
        }
        response = requests.get(url, timeout=5, headers=headers)
        response.raise_for_status()

        # Extract title from HTML
        title_match = re.search(r'"title":"([^"]+)"', response.text)
        if not title_match:
            title_match = re.search(r'<title>([^|]+)', response.text)
        title = title_match.group(1).strip() if title_match else "Video"

        # Extract duration from HTML (in seconds, typically in ytInitialData)
        duration_match = re.search(r'"lengthSeconds":"(\d+)"', response.text)
        if duration_match:
            seconds = int(duration_match.group(1))
            minutes = (seconds + 59) // 60  # Round up
            return title, minutes

        return title, None
    except Exception as e:
        print(f"Failed to fetch YouTube metadata from {url}: {e}", file=sys.stderr)

    return None


def estimate_reading_time(text):
    """Estimate reading time in minutes from text.

    Assumes average reading speed of 200 words per minute.
    """
    words = len(text.split())
    minutes = max(1, (words + 100) // 200)  # Round up, minimum 1
    return minutes


def get_webpage_metadata(url):
    """Fetch webpage title and estimate reading time.

    Returns (title, reading_minutes) or None on failure.
    """
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36'
        }
        response = requests.get(url, timeout=5, headers=headers)
        response.raise_for_status()

        # Extract title
        title_match = re.search(r'<title>([^<]+)', response.text)
        title = title_match.group(1).strip() if title_match else "Web page"

        # Extract main content for reading time estimation
        # Remove script/style tags and extract text
        text = re.sub(r'<script[^>]*>.*?</script>', '', response.text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<[^>]+>', ' ', text)  # Remove HTML tags
        text = re.sub(r'\s+', ' ', text).strip()  # Normalize whitespace

        reading_minutes = estimate_reading_time(text)
        return title, reading_minutes
    except Exception as e:
        print(f"Failed to fetch webpage metadata from {url}: {e}", file=sys.stderr)

    return None


def transform_url_description(task):
    """Transform task description if it's a URL into a human-readable format."""
    messages = []
    description = task.get("description", "")

    if not is_url_only(description):
        return messages

    url = description.strip()
    new_description = None
    new_annotation = None

    # Try GitHub PR
    if 'github.com' in url and '/pull/' in url:
        metadata = get_github_pr_metadata(url)
        if metadata:
            org, repo, pr_num, title = metadata
            new_description = f"{title} [{org}/{repo} PR{pr_num}]"
            messages.append(f"Transformed GitHub PR URL to: {new_description}")

    # Try GitLab MR
    elif 'gitlab' in url and ('/-/merge_requests/' in url or '/merge_requests/' in url):
        metadata = get_gitlab_mr_metadata(url)
        if metadata:
            org, repo, mr_num, title = metadata
            new_description = f"{title} [{org}/{repo} MR{mr_num}]"
            messages.append(f"Transformed GitLab MR URL to: {new_description}")

    # Try YouTube
    elif 'youtube.com' in url or 'youtu.be' in url:
        metadata = get_youtube_metadata(url)
        if metadata:
            title, minutes = metadata
            if minutes:
                new_description = f"[{minutes}m] {title}"
            else:
                new_description = title
            new_annotation = f"LINK: {url}"
            messages.append(f"Transformed YouTube URL to: {new_description}")

    # Fallback to generic webpage
    else:
        metadata = get_webpage_metadata(url)
        if metadata:
            title, reading_minutes = metadata
            new_description = f"[{reading_minutes}m] {title}"
            new_annotation = f"LINK: {url}"
            messages.append(f"Transformed webpage URL to: {new_description}")

    # Apply transformations
    if new_description:
        task["description"] = new_description

        if new_annotation:
            annotations = task.get("annotations", [])
            idx, existing = find_annotation(annotations, "LINK: ")
            if not existing:
                messages.append(add_annotation(annotations, new_annotation))
                task["annotations"] = annotations

    return messages


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------
def run_pipeline(pipeline, features_config, *args):
    """Run each enabled feature in *pipeline*, collecting messages.

    Each feature that raises is caught and reported, so other features
    continue to run.
    """
    messages = []
    for name, func in pipeline:
        if not features_config.get(name, False):
            continue
        try:
            msgs = func(*args)
            if msgs:
                messages.extend(msgs)
        except Exception as e:
            print(f"Hook feature '{name}' failed: {e}", file=sys.stderr)
    return messages
