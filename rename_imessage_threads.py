#!/usr/bin/env python3
"""
rename_imessage_threads.py

Renames imessage-exporter HTML conversation files to reflect the names of
participants other than yourself — but ONLY when the filename still has the
default phone-number / email-address format produced by imessage-exporter.
Files that already have a human-readable name are left untouched.

Usage:
    python rename_imessage_threads.py <directory> [--me <your_name>] [--dry-run]

Arguments:
    directory       Path to folder containing imessage-exporter HTML files
    --me            Your name as it appears in the HTML (default: tries to
                    auto-detect the most frequent sender across all files)
    --dry-run       Print what would be renamed without actually doing it
    --separator     Separator between names for group chats (default: ", ")
    --no-clobber    Skip rename if destination file already exists
"""

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

from bs4 import BeautifulSoup


# ---------------------------------------------------------------------------
# Filename classification
# ---------------------------------------------------------------------------

# E.164 international phone number: +14155551212, +1 (650) 200-0667, etc.
_PHONE_E164_RE = re.compile(r"^\+[\d\s\-().]+$")

# Plain 10-digit domestic number without +
_PHONE_10D_RE = re.compile(r"^\d{10}$")

# SMS short code: 4-6 digits (e.g. 32592, 877877)
_SHORT_CODE_RE = re.compile(r"^\d{4,6}$")

# Longer all-digit strings that are not standard phone numbers (internal IDs, etc.)
_DIGIT_BLOB_RE = re.compile(r"^\d{7,}$")

# Email address
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# imessage-exporter internal chat ID: hex/digits + " - " + number
# e.g. "68afba - 1012", "608166 - 828", "938799 - 3429"
_INTERNAL_ID_RE = re.compile(r"^[0-9a-f]+ - \d+$", re.IGNORECASE)

# "and N others" trailer on truncated group chat stems
_AND_OTHERS_RE = re.compile(r",\s*and \d+ others$", re.IGNORECASE)

# Trailing comma left by truncation in very long group filenames
_TRAILING_COMMA_RE = re.compile(r",\s*$")


def _token_is_default(token: str) -> bool:
    """Return True if a single comma-split token looks like a default identifier."""
    token = token.strip()
    if not token:
        return True  # empty token from split artefact
    return bool(
        _PHONE_E164_RE.match(token)
        or _PHONE_10D_RE.match(token)
        or _SHORT_CODE_RE.match(token)
        or _DIGIT_BLOB_RE.match(token)
        or _EMAIL_RE.match(token)
    )


def filename_is_default(stem: str) -> bool:
    """
    Return True when the filename stem is still in the default format produced
    by imessage-exporter (phone numbers, email addresses, short codes, or
    internal chat IDs), and has not been given a human-readable name.

    Patterns treated as DEFAULT (will rename):
        "+14155551212"                         E.164 phone
        "+1 (650) 200-0667"                   formatted E.164
        "+12037670046, +12319399011"           group of phones
        "jennifermurphy66@me.com"              email
        "jennifermurphy66@me.com, +12067994827"  mixed
        "32592"                                SMS short code
        "68afba - 1012"                        internal chat ID
        "+12088603784, ..., and 3 others"      truncated group

    Patterns treated as NAMED (will NOT rename):
        "Belize! - 769"
        "Da lit fam  - 2327"
        "HSBG 2025 - 779"
        "Alice, Bob"
        "Famalot - 16"
    """
    # Internal chat ID format (hex/digits + " - " + number)
    if _INTERNAL_ID_RE.match(stem):
        return True

    # Strip "and N others" trailer from truncated group chats
    stem_clean = _AND_OTHERS_RE.sub("", stem)
    # Strip trailing comma left by truncation
    stem_clean = _TRAILING_COMMA_RE.sub("", stem_clean)

    tokens = [t.strip() for t in stem_clean.split(",")]
    if not tokens:
        return False

    return all(_token_is_default(t) for t in tokens)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def extract_participants(soup: BeautifulSoup) -> list[str]:
    """
    Pull unique sender names from the HTML produced by imessage-exporter.

    imessage-exporter renders each message inside a <div class="message">
    block. The sender name appears in a <span class="sender"> element.
    For sent messages, the sender is typically "Me" or your actual name.
    """
    names: list[str] = []
    seen: set[str] = set()

    # Primary strategy: <span class="sender">Name</span>
    for span in soup.select("span.sender"):
        name = span.get_text(strip=True)
        if name and name not in seen:
            seen.add(name)
            names.append(name)

    # Fallback: look for a <title> or <h1> that lists participants,
    # e.g. "Conversation with Alice, Bob"
    if not names:
        title_tag = soup.find("title") or soup.find("h1")
        if title_tag:
            text = title_tag.get_text(strip=True)
            # "Conversation with Alice, Bob" or just "Alice, Bob"
            match = re.search(r"(?:with\s+)?(.+)", text, re.IGNORECASE)
            if match:
                for part in re.split(r",\s*|\s+and\s+", match.group(1)):
                    part = part.strip()
                    if part and part not in seen:
                        seen.add(part)
                        names.append(part)

    return names


def load_participants_from_file(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        soup = BeautifulSoup(fh, "html.parser")
    return extract_participants(soup)


# ---------------------------------------------------------------------------
# Auto-detect "me"
# ---------------------------------------------------------------------------

def detect_self(html_files: list[Path]) -> str | None:
    """
    Heuristic: across all files, the sender who appears most often is
    likely the phone's owner. Works well when you text many different people.
    Also checks for common placeholder names used by imessage-exporter.
    """
    # imessage-exporter commonly uses "Me" for the local user
    COMMON_SELF_LABELS = {"Me", "me", "You", "you"}

    counter: Counter = Counter()
    for p in html_files:
        try:
            participants = load_participants_from_file(p)
            for name in participants:
                counter[name] += 1
        except Exception:
            continue

    if not counter:
        return None

    # Prefer explicit self-labels if present
    for label in COMMON_SELF_LABELS:
        if label in counter:
            return label

    # Otherwise return the most common name (likely you)
    return counter.most_common(1)[0][0]


# ---------------------------------------------------------------------------
# Safe filename construction
# ---------------------------------------------------------------------------

UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_filename(name: str) -> str:
    """Strip characters that are illegal in filenames on common OSes."""
    return UNSAFE_CHARS.sub("_", name).strip(". ")


def build_new_stem(others: list[str], separator: str) -> str:
    parts = [safe_filename(n) for n in others if n]
    return separator.join(parts) if parts else "Unknown"


# ---------------------------------------------------------------------------
# Core rename logic
# ---------------------------------------------------------------------------

def rename_files(
    directory: Path,
    me: str,
    separator: str = ", ",
    dry_run: bool = False,
    no_clobber: bool = False,
) -> None:
    html_files = sorted(directory.glob("*.html"))
    if not html_files:
        print(f"No HTML files found in {directory}")
        return

    print(f"Found {len(html_files)} HTML file(s). Self identifier: '{me}'\n")

    rename_map: dict[Path, Path] = {}
    stem_counter: Counter = Counter()

    for path in html_files:
        # Only rename files still using the default phone/email filename format
        if not filename_is_default(path.stem):
            print(f"  [SKIP]  {path.name} — already named, leaving untouched")
            continue

        try:
            participants = load_participants_from_file(path)
        except Exception as exc:
            print(f"  [ERROR] Could not parse {path.name}: {exc}")
            continue

        others = [p for p in participants if p != me]

        if not others:
            print(f"  [SKIP]  {path.name} — no other participants found "
                  f"(participants: {participants})")
            continue

        stem = build_new_stem(others, separator)
        stem_counter[stem] += 1
        rename_map[path] = stem

    # Resolve collisions by appending a counter suffix
    stem_usage: Counter = Counter()
    final_map: dict[Path, Path] = {}
    for src, stem in rename_map.items():
        if stem_counter[stem] > 1:
            stem_usage[stem] += 1
            unique_stem = f"{stem} ({stem_usage[stem]})"
        else:
            unique_stem = stem
        final_map[src] = src.with_name(unique_stem + ".html")

    # Execute (or preview) renames
    for src, dst in final_map.items():
        if src == dst:
            print(f"  [SAME]  {src.name}")
            continue
        if no_clobber and dst.exists():
            print(f"  [SKIP]  {src.name} → {dst.name}  (destination exists)")
            continue
        if dry_run:
            print(f"  [DRY]   {src.name}\n          → {dst.name}")
        else:
            src.rename(dst)
            print(f"  [OK]    {src.name}\n          → {dst.name}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rename imessage-exporter HTML files to participant names."
    )
    parser.add_argument("directory", type=Path, help="Folder with HTML files")
    parser.add_argument(
        "--me",
        default=None,
        help="Your name as it appears in the HTML (auto-detected if omitted)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview renames without touching the filesystem",
    )
    parser.add_argument(
        "--separator",
        default=", ",
        help='Separator for group chat names (default: ", ")',
    )
    parser.add_argument(
        "--no-clobber",
        action="store_true",
        help="Skip rename if the destination filename already exists",
    )
    args = parser.parse_args()

    if not args.directory.is_dir():
        sys.exit(f"Error: '{args.directory}' is not a directory.")

    html_files = sorted(args.directory.glob("*.html"))
    if not html_files:
        sys.exit(f"No HTML files found in '{args.directory}'.")

    me = args.me
    if me is None:
        print("Auto-detecting your name from message history…")
        me = detect_self(html_files)
        if me is None:
            sys.exit(
                "Could not auto-detect your name. "
                "Please supply it with --me 'Your Name'."
            )
        print(f"Detected self as: '{me}'\n")

    rename_files(
        directory=args.directory,
        me=me,
        separator=args.separator,
        dry_run=args.dry_run,
        no_clobber=args.no_clobber,
    )


if __name__ == "__main__":
    main()
