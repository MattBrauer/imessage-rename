# rename_imessage_threads

Renames HTML files exported by [imessage-exporter](https://github.com/ReagentX/imessage-exporter) from their default phone-number/email filenames to the names of the people in each conversation.

Files that already have a human-readable name (e.g. `Belize! - 769.html`, `Da lit fam - 2327.html`) are left untouched.

---

## Prerequisites: imessage-exporter

This script operates on HTML output from [imessage-exporter](https://github.com/ReagentX/imessage-exporter), a command-line tool that exports iMessage and SMS conversations from macOS or iOS backups.

### Installing imessage-exporter

**Via Homebrew (easiest):**
```bash
brew install imessage-exporter
```
Note: the Homebrew formula may lag slightly behind the latest release.

**Via Cargo (most up-to-date):**
```bash
cargo install imessage-exporter
```
Requires Rust. If you don't have it: `curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh`

### Granting Full Disk Access

imessage-exporter needs to read `~/Library/Messages/chat.db`, which is protected by macOS. Before running it, grant Full Disk Access to Terminal (or whichever app you run it from):

**System Settings → Privacy & Security → Full Disk Access → add Terminal**

### Exporting your messages as HTML

Basic export to `~/imessage_export/`:
```bash
imessage-exporter -f html -c basic -o ~/imessage_export
```

The `-c basic` flag converts attachments (e.g. HEIC → JPEG) so they render correctly in browsers. Other copy method options are `clone` (no conversion), `full` (converts images, audio, and video), or `disabled` (no attachments).

### How imessage-exporter names its output files

By design, imessage-exporter names each HTML file after the raw identifier(s) from its database — phone numbers, email addresses, or internal chat IDs — rather than contact names. This guarantees uniqueness across exports but makes the files hard to navigate. For example:

```
+14155551212.html
+14155551212, alice@example.com.html
68afba - 1012.html
32592.html
```

Group chat files with a custom name set in the Messages app are exported with that name, followed by a numeric suffix:

```
Belize! - 769.html
Da lit fam - 2328.html
```

`rename_imessage_threads.py` picks up where imessage-exporter leaves off: it renames the raw-identifier files to the names of the participants found inside the HTML, while leaving the already-named files untouched.

---

## Requirements

- Python 3.10+
- [beautifulsoup4](https://pypi.org/project/beautifulsoup4/)

```bash
pip install beautifulsoup4
```

---

## Usage

```bash
python rename_imessage_threads.py <directory> [options]
```

### Arguments

| Argument | Description |
|---|---|
| `directory` | Path to the folder containing imessage-exporter HTML files |
| `--me <name>` | Your name as it appears in the HTML. Auto-detected if omitted. |
| `--dry-run` | Preview what would be renamed without touching the filesystem |
| `--separator <str>` | Separator between names in group chat filenames (default: `", "`) |
| `--no-clobber` | Skip a rename if the destination filename already exists |

### Examples

```bash
# Preview renames without making any changes (recommended first step)
python rename_imessage_threads.py ~/imessage_export/ --dry-run

# Run for real, with your name auto-detected
python rename_imessage_threads.py ~/imessage_export/

# Specify your name explicitly (use this if auto-detection picks the wrong name)
python rename_imessage_threads.py ~/imessage_export/ --me "Me"

# Use " & " as the separator for group chats
python rename_imessage_threads.py ~/imessage_export/ --me "Me" --separator " & "

# Don't overwrite if a destination file already exists
python rename_imessage_threads.py ~/imessage_export/ --no-clobber
```

---

## How it works

### Filename detection

The script only renames files whose stems look like default imessage-exporter output. It recognises the following patterns as "default" (eligible for renaming):

| Pattern | Example |
|---|---|
| E.164 phone number | `+14155551212.html` |
| Formatted phone number | `+1 (650) 200-0667.html` |
| Group of phone numbers | `+12037670046, +12319399011.html` |
| Email address | `acjoslin@gmail.com.html` |
| Mixed phone + email | `jennifermurphy66@me.com, +12067994827.html` |
| SMS short code (4–6 digits) | `32592.html`, `877877.html` |
| Internal chat ID | `68afba - 1012.html`, `608166 - 828.html` |
| Truncated group chat | `+12088603784, ..., and 3 others.html` |

Files with human-readable names are skipped:

| Example | Reason |
|---|---|
| `Belize! - 769.html` | Named group chat |
| `Da lit fam - 2327.html` | Named group chat |
| `HSBG 2025 - 779.html` | Named group chat |
| `Sunset Dads - 165.html` | Named group chat |
| `Portugal 2022 - 1763.html` | Named trip |

### Name extraction

Participant names are read from `<span class="sender">` elements inside each HTML file. The script collects all unique senders, removes your own name, and uses the remainder to build the new filename.

### Self-detection

If `--me` is not supplied, the script scans all files and looks for the sender labelled `"Me"` or `"You"` (imessage-exporter's default for the local user). If neither is found, it falls back to the most frequently appearing sender name across all files. You can override this at any time with `--me`.

### Collision handling

If two default-named files resolve to the same participant name (e.g. two separate threads with the same contact), the script appends a counter: `Alice.html`, `Alice (2).html`.

---

## Output

The script prints one line per file:

| Tag | Meaning |
|---|---|
| `[OK]` | File successfully renamed |
| `[DRY]` | Would rename (dry-run mode) |
| `[SKIP]` | Already named, or destination exists (`--no-clobber`) |
| `[SAME]` | New name matches old name, no action needed |
| `[ERROR]` | File could not be parsed |
