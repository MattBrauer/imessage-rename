#!/usr/bin/env python3
"""
rename_imessage_threads.py

Renames imessage-exporter HTML conversation files to reflect the names of
participants other than yourself — but ONLY when the filename still has the
default phone-number / email-address format produced by imessage-exporter.
Files that already have a human-readable name are left untouched.

Group chats sometimes show a participant as a raw phone number/email instead
of a resolved name, even though that same identity resolves fine in a 1:1
thread elsewhere. By default, names learned from single-participant threads
are used to disambiguate those raw identities in group chats (disable with
--no-disambiguate).

Also supports archiving stale threads: files whose last message is not more
recent than a given date are moved into an archive folder, leaving all other
threads in place.

Usage:
    python rename_imessage_threads.py <directory> [--me <your_name>] [--dry-run]
    python rename_imessage_threads.py <directory> --archive-before YYYY-MM-DD

Arguments:
    directory           Path to folder containing imessage-exporter HTML files
    --me                Your name as it appears in the HTML (default: tries to
                        auto-detect the most frequent sender across all files)
    --dry-run           Print what would be renamed/archived without actually
                        doing it
    --separator         Separator between names for group chats (default: ", ")
    --no-clobber        Skip rename/archive if destination file already exists
    --archive-before    Move threads with no message after this date
                        (YYYY-MM-DD) into an archive folder
    --archive-dir       Folder to move archived threads into
                        (default: <directory>/Archive)
    --archive-only      Skip renaming; only perform archiving (requires
                        --archive-before)
    --no-disambiguate   Don't disambiguate raw group-chat identities using
                        names resolved in single-participant threads
"""

import argparse
import re
import shutil
import sys
from collections import Counter
from datetime import date, datetime
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


def _looks_like_raw_identifier(text: str) -> bool:
    """Return True if `text` is a raw phone number or email, not a resolved name."""
    text = text.strip()
    return bool(
        _PHONE_E164_RE.match(text)
        or _PHONE_10D_RE.match(text)
        or _EMAIL_RE.match(text)
    )


def normalize_identifier(ident: str) -> str:
    """
    Normalize a phone number or email so the same identity compares equal
    regardless of formatting (e.g. "+1 (650) 200-0667" == "+16502000667").
    """
    ident = ident.strip()
    if _EMAIL_RE.match(ident):
        return ident.lower()
    digits = re.sub(r"\D", "", ident)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]  # strip US/Canada country code for comparison
    return digits


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


def _split_stem_tokens(stem: str) -> list[str]:
    """Split a filename stem into its comma-separated identity tokens."""
    # Strip "and N others" trailer from truncated group chats
    stem_clean = _AND_OTHERS_RE.sub("", stem)
    # Strip trailing comma left by truncation
    stem_clean = _TRAILING_COMMA_RE.sub("", stem_clean)
    return [t.strip() for t in stem_clean.split(",")]


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

    tokens = _split_stem_tokens(stem)
    if not tokens:
        return False

    return all(_token_is_default(t) for t in tokens)


def filename_has_raw_identifier(stem: str) -> bool:
    """
    Return True when at least one comma-separated token in the filename is
    still a raw phone number or email address — e.g. a partially-resolved
    group filename like "Jen Murphy, +15305597313" left over from a previous
    rename that couldn't identify every participant at the time.

    Such filenames are already "named" as far as filename_is_default is
    concerned, but are worth reconsidering once identity disambiguation can
    fill in the missing name.
    """
    if _INTERNAL_ID_RE.match(stem):
        return False
    return any(_looks_like_raw_identifier(t) for t in _split_stem_tokens(stem))


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def extract_participants(
    soup: BeautifulSoup,
    identity_map: dict[str, str] | None = None,
) -> list[str]:
    """
    Pull unique sender names from the HTML produced by imessage-exporter.

    imessage-exporter renders each message inside a <div class="message">
    block. The sender name appears in a <span class="sender"> element.
    For sent messages, the sender is typically "Me" or your actual name.

    In group chats, imessage-exporter sometimes can't resolve a participant
    to a contact name and shows their raw phone number/email instead, even
    though that same identity resolves fine in a 1:1 thread. If `identity_map`
    is supplied (see build_identity_map), raw identifiers are swapped for
    their disambiguated name.
    """
    names: list[str] = []
    seen: set[str] = set()

    # Primary strategy: <span class="sender">Name</span>
    for span in soup.select("span.sender"):
        name = span.get_text(strip=True)
        if name and identity_map and _looks_like_raw_identifier(name):
            name = identity_map.get(normalize_identifier(name), name)
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


def load_participants_from_file(
    path: Path,
    identity_map: dict[str, str] | None = None,
) -> list[str]:
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        soup = BeautifulSoup(fh, "html.parser")
    return extract_participants(soup, identity_map)


# ---------------------------------------------------------------------------
# Identity disambiguation
# ---------------------------------------------------------------------------

def build_identity_map(html_files: list[Path], me: str) -> dict[str, str]:
    """
    Build a {normalized identifier: resolved name} map from single-participant
    threads whose filename is still a raw phone number or email address.

    When imessage-exporter can resolve that thread's other participant to a
    real contact name, we now know what that phone number/email belongs to.
    This lets group-chat threads — where the same identity is sometimes shown
    as a raw phone/email instead of a name — be disambiguated using names
    already known from 1:1 threads.
    """
    identity_map: dict[str, str] = {}
    for path in html_files:
        stem = path.stem
        if "," in stem:
            continue  # group chat filename, not a clean 1:1 id -> name signal

        if not (
            _PHONE_E164_RE.match(stem)
            or _PHONE_10D_RE.match(stem)
            or _EMAIL_RE.match(stem)
        ):
            continue  # filename isn't itself a raw phone/email identifier

        try:
            participants = load_participants_from_file(path)
        except Exception:
            continue

        others = [p for p in participants if p != me]
        if len(others) != 1:
            continue

        name = others[0]
        if not name or _looks_like_raw_identifier(name):
            continue  # unresolved here too — nothing to disambiguate with

        identity_map[normalize_identifier(stem)] = name

    return identity_map


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
    disambiguate: bool = True,
) -> None:
    html_files = sorted(directory.glob("*.html"))
    if not html_files:
        print(f"No HTML files found in {directory}")
        return

    print(f"Found {len(html_files)} HTML file(s). Self identifier: '{me}'\n")

    identity_map: dict[str, str] = {}
    if disambiguate:
        identity_map = build_identity_map(html_files, me)
        if identity_map:
            print(
                f"Disambiguated {len(identity_map)} identifier(s) from "
                f"single-participant threads\n"
            )

    rename_map: dict[Path, Path] = {}
    stem_counter: Counter = Counter()

    for path in html_files:
        # Rename files still using the default phone/email filename format, and
        # also reconsider partially-resolved filenames (e.g. "Jen Murphy,
        # +15305597313") when disambiguation might be able to fill in the rest.
        eligible = filename_is_default(path.stem)
        if not eligible and identity_map and filename_has_raw_identifier(path.stem):
            eligible = True
        if not eligible:
            print(f"  [SKIP]  {path.name} — already named, leaving untouched")
            continue

        try:
            participants = load_participants_from_file(path, identity_map)
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
# Last-message-date parsing
# ---------------------------------------------------------------------------

# imessage-exporter timestamp format, e.g. "Mar 03, 2018  1:00:53 AM"
# (note: a single-digit hour is padded with an extra space, which
# datetime.strptime handles fine since whitespace in the format string
# matches any run of whitespace in the input).
_TIMESTAMP_FORMAT = "%b %d, %Y %I:%M:%S %p"


def parse_timestamp(text: str) -> datetime | None:
    """Parse a single imessage-exporter timestamp string, or None if invalid."""
    text = text.strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, _TIMESTAMP_FORMAT)
    except ValueError:
        return None


def last_message_datetime(soup: BeautifulSoup) -> datetime | None:
    """
    Return the timestamp of the most recent message in the thread, or None
    if no parseable timestamp is found.

    Each real message's timestamp lives in a <span class="timestamp"><a>...
    </a></span> element that links back into the Messages app. Edit-history
    and tapback timestamps either have no <a> child or aren't valid dates, so
    selecting "span.timestamp > a" naturally skips them.
    """
    timestamps = []
    for a_tag in soup.select("span.timestamp > a"):
        dt = parse_timestamp(a_tag.get_text())
        if dt:
            timestamps.append(dt)
    return max(timestamps) if timestamps else None


def load_last_message_datetime(path: Path) -> datetime | None:
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        soup = BeautifulSoup(fh, "html.parser")
    return last_message_datetime(soup)


# ---------------------------------------------------------------------------
# Core archive logic
# ---------------------------------------------------------------------------

def archive_stale_threads(
    directory: Path,
    cutoff: date,
    archive_dir: Path,
    dry_run: bool = False,
    no_clobber: bool = False,
) -> None:
    """
    Move every thread whose last message is not after `cutoff` into
    `archive_dir`. Threads with at least one message after `cutoff`, and
    threads with no parseable timestamp, are left in place.
    """
    html_files = sorted(directory.glob("*.html"))
    if not html_files:
        print(f"No HTML files found in {directory}")
        return

    print(
        f"Archiving threads with no messages after {cutoff.isoformat()} "
        f"→ {archive_dir}\n"
    )

    if not dry_run:
        archive_dir.mkdir(parents=True, exist_ok=True)

    for path in html_files:
        try:
            last_dt = load_last_message_datetime(path)
        except Exception as exc:
            print(f"  [ERROR] Could not parse {path.name}: {exc}")
            continue

        if last_dt is None:
            print(f"  [SKIP]  {path.name} — no parseable message timestamps found")
            continue

        if last_dt.date() > cutoff:
            print(f"  [KEEP]  {path.name} — last message {last_dt.date().isoformat()}")
            continue

        dst = archive_dir / path.name
        if no_clobber and dst.exists():
            print(f"  [SKIP]  {path.name} → {archive_dir.name}/  (destination exists)")
            continue

        if dry_run:
            print(
                f"  [DRY]   {path.name} — last message {last_dt.date().isoformat()} "
                f"→ {archive_dir.name}/"
            )
        else:
            shutil.move(str(path), str(dst))
            print(
                f"  [OK]    {path.name} — last message {last_dt.date().isoformat()} "
                f"→ {archive_dir.name}/"
            )


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
        help="Skip rename/archive if the destination filename already exists",
    )
    parser.add_argument(
        "--archive-before",
        metavar="YYYY-MM-DD",
        type=date.fromisoformat,
        default=None,
        help="Move threads with no message after this date into an archive folder",
    )
    parser.add_argument(
        "--archive-dir",
        type=Path,
        default=None,
        help="Folder to move archived threads into (default: <directory>/Archive)",
    )
    parser.add_argument(
        "--archive-only",
        action="store_true",
        help="Skip renaming; only perform archiving (requires --archive-before)",
    )
    parser.add_argument(
        "--no-disambiguate",
        action="store_true",
        help=(
            "Don't use names resolved in single-participant threads to "
            "disambiguate raw phone/email identities shown in group chats"
        ),
    )
    args = parser.parse_args()

    if not args.directory.is_dir():
        sys.exit(f"Error: '{args.directory}' is not a directory.")

    if args.archive_only and args.archive_before is None:
        sys.exit("Error: --archive-only requires --archive-before to be set.")

    html_files = sorted(args.directory.glob("*.html"))
    if not html_files:
        sys.exit(f"No HTML files found in '{args.directory}'.")

    if not args.archive_only:
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
            disambiguate=not args.no_disambiguate,
        )

    if args.archive_before is not None:
        if not args.archive_only:
            print()  # blank line between rename and archive output
        archive_dir = args.archive_dir or (args.directory / "Archive")
        archive_stale_threads(
            directory=args.directory,
            cutoff=args.archive_before,
            archive_dir=archive_dir,
            dry_run=args.dry_run,
            no_clobber=args.no_clobber,
        )


if __name__ == "__main__":
    main()
