import os
import re
import sys
import time
import requests
import json # Import the json library for config file handling
import threading # Import threading for the lock
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from playwright_stealth import Stealth # Import the stealth library

# --- Configuration File Path ---
CONFIG_FILE = 'libby_config.json'
RUN_LOG_FILE = 'libby_run.log'


class _Tee:
    """Mirror writes to several streams. Used to copy all console output into a
    log file, because diagnostic lines scroll out of the terminal buffer long
    before a run finishes."""
    def __init__(self, *streams):
        self._streams = streams

    def write(self, data):
        for s in self._streams:
            s.write(data)
            s.flush()

    def flush(self):
        for s in self._streams:
            s.flush()


_log_file = None  # Set in __main__; target for log_only()


def log_only(msg):
    """Write a line to the run log file only, skipping the console. For noisy
    diagnostics (e.g. unmatched-CDN-request spam) that clog the printout but
    are still useful when digging through a run afterwards."""
    if _log_file:
        try:
            _log_file.write(msg + "\n")
            _log_file.flush()
        except Exception:
            pass

# --- Testing Configuration ---
AUTO_SELECT_FIRST_AUDIOBOOK = True  # Set to False for manual selection

# Playwright action/navigation timeout. Raise this on slow connections; short
# intentional probe timeouts (chapter buttons, optional Next) stay hardcoded.
PLAYWRIGHT_TIMEOUT_MS = 15000

# --- Global Variables for Tracking Download Progress ---
downloaded_parts = set()
found_parts = set()  # Parts detected as soon as Libby triggers (updates before download completes)
max_part_number_found = 0
active_downloads_lock = threading.Lock()
active_downloads_count = 0
_latest_libby_part_number_trigger = None # Global variable to store the last part number seen in a Libby URL
_libby_url_template = None # Captured from first part trigger, used to construct URLs for missing parts
_signed_spine_urls = {} # spine_index -> full signed Libby URL, captured from openbook data
_last_seen_part = 0 # Most recent part number triggered by Libby, in or out of order (used for gap detection)
max_part_number_seen = 0 # Highest part number seen in ANY trigger, even out-of-order/unaccepted ones
_trigger_seq = 0 # Bumped on every part trigger; lets seek probes wait for a FRESH trigger instead of reading stale state
# The request handler ignores everything until this is True. It's flipped on only once
# we've reached the rewind step, so the player's initial auto-load (its resume position
# plus manifest requests, whose part numbers can mismatch the audio actually served)
# can't mis-pair a wrong-part CDN download onto Part 1.
downloads_enabled = False


def next_expected_part():
    """The next part we should accept: the smallest part number (from 1) not yet recorded.

    Parts are only recorded in strict ascending order, so this is always one past the
    contiguous run we've collected so far."""
    expected = 1
    while expected in found_parts:
        expected += 1
    return expected


SNAPSHOT_DIR = 'snapshots'


def save_snapshot(page, name):
    """Save a full-page screenshot plus the page's and player iframe's HTML under
    snapshots/, so element structure at each screen can be inspected offline."""
    try:
        os.makedirs(SNAPSHOT_DIR, exist_ok=True)
        base = os.path.join(SNAPSHOT_DIR, f"{time.strftime('%Y%m%d-%H%M%S')}_{name}")
        page.screenshot(path=f"{base}.png", full_page=True)
        with open(f"{base}.html", 'w') as f:
            f.write(page.content())
        for frame in page.frames:
            if 'listen.libbyapp.com' in frame.url:
                with open(f"{base}_player_iframe.html", 'w') as f:
                    f.write(frame.content())
                break
        print(f"  [snapshot] saved {base}.(png|html)")
    except Exception as e:
        print(f"  [snapshot] failed for {name}: {e}")


# The Libby player's scrubber is the "seekometer": a horizontal tape that spans
# the ENTIRE book, time-linear at 2px per second of audio. Its CSS transform
# (translate3d(-<px>, 0, 0)) is the playhead position, and dragging the tape
# left/right seeks. See snapshots/*_player_iframe.html for the captured DOM.
TAPE_PX_PER_SEC = 2.0


def tape_position_px(player_frame):
    """Current playhead position on the seekometer tape in px, or None."""
    try:
        tape = player_frame.locator('.seekometer-tape').first
        transform = tape.evaluate("el => window.getComputedStyle(el).transform")
        m = re.search(r'matrix\(([^)]+)\)', transform or '')
        if not m:
            return None
        tx = float(m.group(1).split(',')[4])
        return -tx
    except Exception as e:
        print(f"  [tape-seek] could not read tape position: {e}")
        return None


def seek_tape_to_px(page, player_frame, target_px):
    """Drag the seekometer tape until the playhead sits at target_px.

    Dragging the tape left advances the playhead. One drag gesture can cover at
    most ~one viewport width, so this runs closed-loop: read the tape transform,
    drag up to 80% of the visible tape width, wait for the ease-out transition
    to settle, re-read, repeat. Returns the final position, or None on failure."""
    try:
        clip = player_frame.locator('.seekometer').first
        box = clip.bounding_box()
    except Exception:
        box = None
    if not box or box['width'] < 50:
        return None
    cy = box['y'] + box['height'] / 2
    cur = None
    no_move_count = 0
    for _ in range(60):
        prev = cur
        cur = tape_position_px(player_frame)
        if cur is None:
            return None
        delta = target_px - cur
        # Loose tolerance (~30s of audio): probes are classified by where they
        # actually landed, so pixel precision buys nothing and chasing it makes
        # the loop fight the tape's snap/ease animations.
        if abs(delta) <= 60:
            return cur
        if prev is not None and abs(cur - prev) < 1.0:
            no_move_count += 1
            if no_move_count >= 3:
                print(f"  [tape-seek] tape not responding to drags (stuck at {cur:.0f}px, want {target_px:.0f}px).")
                return None
        else:
            no_move_count = 0
        max_step = box['width'] * 0.8
        step = max(-max_step, min(max_step, delta))
        # Start the gesture offset from center so both ends stay inside the tape.
        sx_start = box['x'] + box['width'] / 2 + step / 2
        sx_end = sx_start - step
        try:
            # Drag slowly and hold still before releasing: a fast flick gives the
            # tape momentum, so it coasts past the target (firing part triggers
            # for positions merely passed over) and never settles where asked.
            page.mouse.move(sx_start, cy)
            page.mouse.down()
            page.mouse.move(sx_end, cy, steps=25)
            time.sleep(0.4)
            page.mouse.up()
        except Exception as e:
            print(f"  [tape-seek] drag gesture failed: {e}")
            return None
        time.sleep(0.8)  # tape has a 500ms ease-out transition
    return cur

# --- Configuration Management Functions ---
def load_config():
    """Loads configuration from a JSON file, or prompts user if not found/incomplete."""
    config = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                config = json.load(f)
            print(f"Loaded configuration from {CONFIG_FILE}.")
        except json.JSONDecodeError:
            print(f"Error reading {CONFIG_FILE}. It might be corrupted. Re-prompting for details.")
            config = {} # Reset config if corrupted
    else:
        print(f"Configuration file {CONFIG_FILE} not found. Will prompt for details.")

    # Check for required fields and prompt if missing
    # LIBRARY_CARD_USAGE_OPTION_INDEX and LIBRARY_SEARCH_RESULT_INDEX are handled dynamically in run()
    required_fields = ['LIBRARY_CARD_NUMBER', 'LIBBY_PASSWORD', 'LIBRARY', 'DOWNLOAD_DIRECTORY']
    for field in required_fields:
        if field not in config or not config[field]:
            if field == 'LIBRARY_CARD_NUMBER':
                config[field] = input(f"Please enter your {field.replace('_', ' ')}: ")
            elif field == 'LIBBY_PASSWORD':
                config[field] = input(f"Please enter your {field.replace('_', ' ')} (PIN): ")
            elif field == 'LIBRARY':
                config[field] = input(f"Please enter your {field.replace('_', ' ')} (e.g., [REDACTED]): ")
            elif field == 'DOWNLOAD_DIRECTORY':
                default_dir = os.path.join(os.getcwd(), "Libby_Audiobook_Downloads")
                config[field] = input(f"Enter download directory (default: {default_dir}): ") or default_dir
            save_config(config) # Save after each new input

    # Ensure DOWNLOAD_DIRECTORY exists
    if not os.path.exists(config['DOWNLOAD_DIRECTORY']):
        os.makedirs(config['DOWNLOAD_DIRECTORY'])
        print(f"Created download directory: {config['DOWNLOAD_DIRECTORY']}")

    return config

def save_config(config_data):
    """Saves configuration to a JSON file."""
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config_data, f, indent=4)
    print(f"Saved configuration to {CONFIG_FILE}.")

def collect_shelf_titles(page, format_class):
    """Return (titles, authors) for shelf tiles of the given Libby format class."""
    tiles = page.locator(f'.title-list-tiles .title-tile.{format_class}').all()
    titles = []
    authors = []
    for tile in tiles:
        title_element = tile.locator('.title-tile-title').first
        if title_element:
            titles.append(normalize_text(title_element.text_content()))

        author_text = ""
        for author_selector in ['.title-tile-author', '.title-tile-creator', '.title-tile-subtitle']:
            try:
                author_element = tile.locator(author_selector).first
                if author_element and author_element.is_visible():
                    candidate = normalize_text(author_element.text_content())
                    if candidate:
                        author_text = candidate
                        break
            except Exception:
                continue
        authors.append(author_text)
    return titles, authors


def collect_shelf_audiobook_loans(page):
    """Return (titles, authors) for borrowed audiobook loans (not holds)."""
    return collect_shelf_titles(
        page, 'data-tile-class_loan.data-title-tile-format_audiobook'
    )


def collect_shelf_audiobook_holds(page):
    """Return (titles, authors) for audiobook holds, including ready-to-borrow."""
    return collect_shelf_titles(
        page, 'data-tile-class_hold.data-title-tile-format_audiobook'
    )


def tile_is_hold(tile):
    """True when the shelf tile is a hold rather than an active loan."""
    try:
        class_attr = tile.get_attribute('class') or ''
        return 'data-tile-class_hold' in class_attr.split()
    except Exception:
        return False


def hold_status_from_tile(tile):
    """Best-effort hold status line from a shelf tile (e.g. 'Ready to borrow!')."""
    for selector in ['.shelf-whisperer span[role="text"]', '.wait-list-summary', '.title-status']:
        try:
            element = tile.locator(selector).first
            if element.is_visible():
                text = normalize_text(element.text_content())
                if text:
                    return text
        except Exception:
            continue
    return ''


def explain_hold_not_borrowed(title, hold_status=''):
    """Tell the user their audiobook hold must be borrowed before download."""
    print(f"\nCannot download '{title}': it is on your holds shelf, not borrowed yet.")
    if hold_status:
        print(f"Libby status: {hold_status}")
    print("Borrow it in Libby first (tap 'Borrow' on the shelf or in title details),")
    print("then re-run this script once the tile shows 'Open Audiobook'.")
    return True


def title_matches(requested, candidate):
    """Case-insensitive substring match between a requested title and a shelf title."""
    if not requested or not candidate:
        return False
    req = requested.lower()
    cand = candidate.lower()
    return req in cand or cand in req


def explain_missing_audiobook(page, requested_title):
    """If the user asked for a title that is on loan as an ebook, say so plainly."""
    hold_titles, _ = collect_shelf_audiobook_holds(page)
    for hold_title in hold_titles:
        if title_matches(requested_title, hold_title):
            hold_tiles = page.locator(
                '.title-list-tiles .title-tile.data-tile-class_hold.data-title-tile-format_audiobook'
            ).all()
            hold_status = ''
            for tile in hold_tiles:
                title_element = tile.locator('.title-tile-title').first
                if title_element and title_matches(requested_title, normalize_text(title_element.text_content())):
                    hold_status = hold_status_from_tile(tile)
                    break
            return explain_hold_not_borrowed(requested_title, hold_status)

    ebook_titles, _ = collect_shelf_titles(page, 'data-title-tile-format_book')
    for ebook_title in ebook_titles:
        if title_matches(requested_title, ebook_title):
            print(f"\nCannot download '{requested_title}': that title is on your shelf as an ebook, not an audiobook.")
            print("This script only downloads audiobooks (MP3 parts from the Libby player).")
            print("In Libby, search for the title again and borrow the audiobook edition (headphones icon),")
            print("then re-run this script once it appears on your shelf with 'Open Audiobook'.")
            return True
    print(f"\n'{requested_title}' was not found as an audiobook on your shelf.")
    print("Make sure you've borrowed the audiobook edition and it shows 'Open Audiobook' in Libby.")
    return False


def normalize_text(text):
    """Collapse Libby's non-breaking spaces and HTML entities into normal spaces."""
    if not text:
        return ""
    return text.replace('\xa0', ' ').replace('&nbsp;', ' ').strip()


def sanitize_filename(name):
    """Remove characters unsafe for paths (colons break Android MTP transfers)."""
    if not name:
        return ""
    sanitized = name.replace(':', '')
    sanitized = re.sub(r'[<>"/\\|?*]', '_', sanitized)
    sanitized = sanitized.strip().strip('.')
    sanitized = re.sub(r'[_\s]+', ' ', sanitized)
    return sanitized.strip()


def extract_title_id_from_tile(tile):
    """Parse Libby title ID from a shelf tile's data-title_* class."""
    try:
        class_attr = tile.get_attribute('class') or ''
        match = re.search(r'data-title_(\d+)', class_attr)
        if match:
            return match.group(1)
    except Exception:
        pass
    return None


def _creator_is_author(role):
    return (role or '').lower() in ('author', 'aut')


def display_name_to_file_as(name):
    """Best-effort 'First Last' -> 'Last, First' when OverDrive fileAs is missing."""
    name = normalize_text(name)
    if not name:
        return ''
    if ',' in name:
        return name
    parts = name.split()
    if len(parts) < 2:
        return name
    return f"{parts[-1]}, {' '.join(parts[:-1])}"


def _best_cover_url_from_item(item):
    """Pick the largest available cover image URL from an OverDrive media object."""
    if not isinstance(item, dict):
        return ''
    covers = item.get('covers') or {}
    if not isinstance(covers, dict):
        return ''
    for key in ('cover510Wide', 'cover300Wide', 'cover150Wide', 'cover'):
        entry = covers.get(key)
        if isinstance(entry, dict):
            href = normalize_text(entry.get('href') or '')
            if href:
                return href
    return ''


def parse_file_as(file_as):
    """Split OverDrive fileAs into Last, Title, First, Middle name parts."""
    file_as = normalize_text(file_as)
    if not file_as:
        return {'last': '', 'title': '', 'first': '', 'middle': ''}
    if ',' not in file_as:
        parts = file_as.split()
        return {'last': parts[-1] if parts else '', 'title': '', 'first': ' '.join(parts[:-1]), 'middle': ''}
    last, _, rest = file_as.partition(',')
    rest = rest.strip()
    rest_parts = rest.split() if rest else []
    return {
        'last': last.strip(),
        'title': '',
        'first': rest_parts[0] if rest_parts else '',
        'middle': ' '.join(rest_parts[1:]) if len(rest_parts) > 1 else '',
    }


def _first_author_from_creators(creators):
    """Return (display_name, file_as) for the first author in a creators list."""
    if not creators:
        return '', ''
    for creator in creators:
        if not isinstance(creator, dict):
            continue
        if not _creator_is_author(creator.get('role')):
            continue
        display = normalize_text(creator.get('name') or '')
        file_as = normalize_text(
            creator.get('fileAs') or creator.get('file_as') or creator.get('sortName') or ''
        )
        if not file_as and display:
            file_as = display_name_to_file_as(display)
        return display, file_as
    return '', ''


def _media_records_from_payload(payload):
    """Yield media/title dicts from assorted OverDrive API response shapes."""
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                yield item
        return
    if not isinstance(payload, dict):
        return
    for key in ('titles', 'items', 'media', 'products'):
        value = payload.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    yield item
            return
    if any(k in payload for k in ('title', 'series', 'creators', 'creator', 'readingOrder', 'detailedSeries')):
        yield payload


def parse_overdrive_media_item(item, title_id=None):
    """Extract Libation-relevant metadata from one OverDrive media object."""
    if not isinstance(item, dict):
        return None

    if title_id is not None:
        item_ids = [
            str(item.get('id', '')),
            str(item.get('crossRefId', '')),
            str(item.get('titleId', '')),
        ]
        if str(title_id) not in item_ids and title_id not in item_ids:
            pass  # still try — bulk responses may use different id fields

    title = normalize_text(item.get('title') or '')
    if not title and isinstance(item.get('title'), dict):
        title_obj = item['title']
        title = normalize_text(title_obj.get('main') or title_obj.get('title') or '')

    series_name = normalize_text(item.get('series') or '')
    if not series_name and isinstance(item.get('series'), dict):
        series_name = normalize_text(item['series'].get('name') or item['series'].get('title') or '')

    series_index = item.get('readingOrder') or item.get('seriesIndex') or item.get('sequence')
    detailed_series = item.get('detailedSeries')
    if isinstance(detailed_series, dict):
        if not series_name:
            series_name = normalize_text(detailed_series.get('seriesName') or detailed_series.get('name') or '')
        if not series_index:
            series_index = detailed_series.get('readingOrder') or detailed_series.get('rank')
    if series_index is not None:
        series_index = normalize_text(str(series_index))

    creators = item.get('creators') or item.get('creator') or []
    if isinstance(creators, dict):
        creators = [creators]
    author_name, author_file_as = _first_author_from_creators(creators)

    if not author_name and isinstance(item.get('primaryCreator'), dict):
        pc = item['primaryCreator']
        if _creator_is_author(pc.get('role')):
            author_name = normalize_text(pc.get('name') or '')
            author_file_as = normalize_text(
                pc.get('fileAs') or pc.get('sortName') or ''
            ) or display_name_to_file_as(author_name)

    if not author_file_as:
        author_file_as = normalize_text(
            item.get('firstCreatorSortName') or ''
        ) or display_name_to_file_as(author_name)

    if not title and not author_name and not series_name:
        return None

    return {
        'title': title,
        'author_name': author_name,
        'author_file_as': author_file_as,
        'series_name': series_name,
        'series_index': series_index or '',
        'cover_url': _best_cover_url_from_item(item),
    }


def parse_overdrive_api_payload(payload, title_id=None):
    """Parse the best matching media record from an OverDrive JSON response."""
    best = None
    for item in _media_records_from_payload(payload):
        parsed = parse_overdrive_media_item(item, title_id=title_id)
        if not parsed:
            continue
        if title_id is not None:
            item_ids = {str(item.get('id', '')), str(item.get('crossRefId', '')), str(item.get('titleId', ''))}
            if str(title_id) in item_ids:
                return parsed
        best = best or parsed
    return best


def _parse_captured_overdrive_responses(captured, title_id=None):
    """Pick the richest metadata record from captured API responses."""
    bulk_parsed = None
    fallback_parsed = None
    for url, payload in captured:
        parsed = parse_overdrive_api_payload(payload, title_id=title_id)
        if not parsed:
            continue
        if '/media/bulk' in url:
            if title_id is None or parsed.get('title'):
                bulk_parsed = parsed
        elif not fallback_parsed:
            fallback_parsed = parsed
    return bulk_parsed or fallback_parsed


def scrape_cover_from_dom(page):
    """DOM fallback: find cover image URL on Libby title details."""
    selectors = (
        'img[src*="od-cdn.com"]',
        '.title-details-cover img',
        '.cover img',
        '[class*="cover"] img',
    )
    for selector in selectors:
        try:
            imgs = page.locator(selector)
            if imgs.count() > 0:
                src = normalize_text(imgs.first.get_attribute('src') or '')
                if src.startswith('http'):
                    return src
        except Exception:
            continue
    return ''


def download_cover_image(cover_url, dest_dir):
    """Download cover art into dest_dir as cover.jpg (or .png/.webp from content-type)."""
    if not cover_url:
        return None
    try:
        response = requests.get(cover_url, timeout=30)
        response.raise_for_status()
        content_type = (response.headers.get('content-type') or '').lower()
        if 'png' in content_type:
            ext = '.png'
        elif 'webp' in content_type:
            ext = '.webp'
        else:
            ext = '.jpg'
        cover_path = os.path.join(dest_dir, f'cover{ext}')
        with open(cover_path, 'wb') as f:
            f.write(response.content)
        print(f"Downloaded cover art to {cover_path}")
        return cover_path
    except Exception as e:
        print(f"Cover art download failed: {e}")
        return None


def scrape_series_from_dom(page):
    """DOM fallback: read series block on Libby title details if API data is missing."""
    series_name = ''
    series_index = ''
    try:
        series_heading = page.locator('h2, h3').filter(has_text=re.compile(r'^Series$', re.I))
        if series_heading.count() > 0:
            block = series_heading.first.locator('xpath=ancestor::div[contains(@class,"block") or contains(@class,"strap")][1]')
            if block.count() == 0:
                block = series_heading.first.locator('xpath=..')
            text = normalize_text(block.first.inner_text())
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            for line in lines:
                if line.lower() == 'series':
                    continue
                num_match = re.match(r'^#?(\d+(?:\.\d+)?)\s+(.+)$', line)
                if num_match and ('★' in line or '☆' in line or '*' in line):
                    series_index = num_match.group(1)
                    series_name = normalize_text(num_match.group(2).replace('★', '').replace('☆', '').replace('*', ''))
                    break
                if not series_name and line.lower() != 'series':
                    series_name = line
    except Exception as e:
        print(f"  [metadata] DOM series scrape failed: {e}")
    return series_name, series_index


def scrape_authors_from_dom(page):
    """DOM fallback: first linked author name on title details."""
    for selector in (
        '.title-details-author a',
        '.biblio-author a',
        '.title-tile-author a',
        '[class*="author"] a',
    ):
        try:
            links = page.locator(selector)
            if links.count() > 0:
                name = normalize_text(links.first.text_content())
                if name:
                    return name, display_name_to_file_as(name)
        except Exception:
            continue
    return '', ''


def build_book_download_dir(base_dir, metadata, fallback_title, fallback_author=''):
    """Libation-style path: {fileAs}/{series}/{n}_{title}/ or {fileAs}/{title}/."""
    title = sanitize_filename(metadata.get('title') or fallback_title)
    author_folder = sanitize_filename(
        metadata.get('author_file_as') or display_name_to_file_as(metadata.get('author_name') or '')
        or fallback_author
    )

    series_name = sanitize_filename(metadata.get('series_name') or '')
    series_index = sanitize_filename(metadata.get('series_index') or '')

    if series_name:
        if series_index:
            book_folder = (
                f"{sanitize_filename(series_index)}_"
                f"{sanitize_filename(metadata.get('title') or fallback_title)}"
            )
        else:
            book_folder = title
        parts = [base_dir]
        if author_folder:
            parts.append(author_folder)
        parts.extend([series_name, book_folder])
    else:
        parts = [base_dir]
        if author_folder:
            parts.append(author_folder)
        parts.append(title)

    return os.path.join(*parts)


def fetch_title_metadata(page, tile, fallback_title, fallback_author='', title_id=None):
    """Open title details, capture OverDrive API metadata, return to shelf."""
    if title_id is None:
        title_id = extract_title_id_from_tile(tile)

    captured = []

    def on_response(response):
        url = response.url
        if response.status != 200:
            return
        if 'overdrive.com' not in url:
            return
        if not any(token in url for token in ('media', 'titles', 'products', 'bulk')):
            return
        try:
            payload = response.json()
        except Exception:
            return
        captured.append((url, payload))
        log_only(f"  [metadata] captured API response: {url}")

    page.on('response', on_response)
    metadata = {
        'title': fallback_title,
        'author_name': fallback_author,
        'author_file_as': display_name_to_file_as(fallback_author),
        'series_name': '',
        'series_index': '',
        'cover_url': '',
    }

    try:
        title_link = tile.locator('a.title-tile-action').first
        print(f"Opening title details for metadata (title id={title_id})...")
        title_link.click(timeout=PLAYWRIGHT_TIMEOUT_MS)
        try:
            page.wait_for_load_state('networkidle', timeout=PLAYWRIGHT_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            pass
        time.sleep(2)
        save_snapshot(page, 'title_details')

        parsed = _parse_captured_overdrive_responses(captured, title_id=title_id)
        if parsed:
            print("  [metadata] parsed from OverDrive API")
            metadata.update({k: v for k, v in parsed.items() if v})
        else:
            print("  [metadata] no OverDrive API metadata captured; trying DOM fallback.")
            dom_author, dom_file_as = scrape_authors_from_dom(page)
            dom_series, dom_index = scrape_series_from_dom(page)
            dom_cover = scrape_cover_from_dom(page)
            if dom_author:
                metadata['author_name'] = dom_author
                metadata['author_file_as'] = dom_file_as
            if dom_series:
                metadata['series_name'] = dom_series
            if dom_index:
                metadata['series_index'] = dom_index
            if dom_cover:
                metadata['cover_url'] = dom_cover

        print(
            f"  [metadata] author={metadata.get('author_file_as')!r} "
            f"series={metadata.get('series_name')!r} index={metadata.get('series_index')!r}"
        )
    except Exception as e:
        print(f"  [metadata] title details fetch failed ({e}); using shelf fallbacks.")
    finally:
        try:
            page.remove_listener('response', on_response)
        except Exception:
            pass
        try:
            page.go_back()
            try:
                page.wait_for_load_state('networkidle', timeout=PLAYWRIGHT_TIMEOUT_MS)
            except PlaywrightTimeoutError:
                pass
            time.sleep(2)
            page.wait_for_selector('.title-list-tiles .title-tile', timeout=PLAYWRIGHT_TIMEOUT_MS)
        except Exception as e:
            print(f"  [metadata] warning: could not return to shelf cleanly: {e}")

    return metadata

# --- Network Request Handler ---
def handle_request(request):
    """
    Callback function to process intercepted network requests.
    Identifies and downloads audiobook MP3 parts directly using requests library.
    """
    global downloaded_parts, found_parts, max_part_number_found, active_downloads_count, _latest_libby_part_number_trigger, _libby_url_template, _last_seen_part, max_part_number_seen, _trigger_seq

    # Ignore all traffic until we've reached the rewind step. During the player's initial
    # auto-load, Libby fires manifest/resume requests whose part numbers don't reliably
    # match the audio it actually streams, which would mis-pair a CDN download onto the
    # wrong part (e.g. saving Part 5's audio as Part_01.mp3).
    if not downloads_enabled:
        return

    # Case 1: Intercept initial Libby part request (contains PartXX.mp3)
    # This request triggers the playback and sets up the session for the CDN download.
    if "listen.libbyapp.com" in request.url:
        part_match = re.search(r"Part(\d+).mp3", request.url, re.IGNORECASE)
        if part_match:
            part_number = int(part_match.group(1))
            with active_downloads_lock:
                # Always note the most recent triggered part (for gap detection) and
                # cache its signed URL / URL template regardless of order.
                _last_seen_part = part_number
                max_part_number_seen = max(max_part_number_seen, part_number)
                _trigger_seq += 1
                if _libby_url_template is None:
                    _libby_url_template = re.sub(r'Part\d+\.mp3\?cmpt=.*', '', request.url)
                    print(f"Captured Libby URL template: {_libby_url_template}")
                _signed_spine_urls[part_number] = request.url

                # Only accept parts in strict ascending order. This means the player's
                # resume-position jump (e.g. starting mid-book at Part 6) and chapter
                # overshoots are NOT recorded, so they'll be captured "fresh" once we
                # actually reach them in sequence.
                expected = next_expected_part()
                accepted = (part_number == expected)
                if accepted:
                    found_parts.add(part_number)
                    _latest_libby_part_number_trigger = part_number
            if accepted:
                print(f"Detected in-order part trigger: Part {part_number} (accepted for download).")
            else:
                print(f"Ignoring out-of-order Part {part_number} trigger (expected Part {expected}); will capture it when reached in order.")
            # Do NOT attempt to get response body here, as it's typically empty or a redirect trigger.
            # We are just capturing the part number for the subsequent CDN request.
            return # Exit early, this request is just a trigger

    # Case 2: Intercept actual CDN audio request (does NOT contain PartXX.mp3, relies on previous trigger)
    elif "audioclips.cdn.overdrive.com" in request.url:
        if _latest_libby_part_number_trigger is None:
            # Log-only: this fires constantly (every re-buffer of an already-handled
            # part) and would drown out the console output.
            log_only(f"Skipping CDN request {request.url}: No preceding Libby part trigger found. This might be an unrelated CDN asset.")
            return

        part_number = _latest_libby_part_number_trigger
        
        # Check if this part has already been downloaded (based on the latest trigger)
        if part_number in downloaded_parts:
            # print(f"Part {part_number} already downloaded, skipping CDN request: {request.url}")
            return
        
        # Get the final URL from the Playwright response object.
        # This ensures we have the correct, potentially redirected, CDN URL.
        response_pw = request.response() 
        if not response_pw:
            print(f"No Playwright response object for CDN audio request (derived Part {part_number}): {request.url}")
            return

        cdn_audio_url = response_pw.url # Use the final URL from Playwright's response

        # Basic check for media content type from Playwright's response headers
        content_type = response_pw.headers.get("content-type", "")
        if not content_type.startswith("audio/") and not content_type.startswith("video/"):
            print(f"Skipping non-audio CDN request (content-type: {content_type}): {cdn_audio_url}")
            return

        # Proceed with download for the derived part_number using requests library
        print(f"Processing CDN audio for derived Part {part_number}: {cdn_audio_url}")
        file_name = f"Part_{part_number:02d}.mp3"
        file_path = os.path.join(config['DOWNLOAD_DIRECTORY'], file_name)

        # Fast skip: if we already have this part on disk and it's the same size as
        # the remote part, don't re-download it (saves lots of time on reruns/debugging).
        # Probe the remote size cheaply with a 1-byte range request and read the total
        # from the Content-Range header instead of pulling the whole ~35 MB body.
        if os.path.exists(file_path):
            local_size = os.path.getsize(file_path)
            remote_size = None
            try:
                probe_headers = {k: v for k, v in request.headers.items() if k.lower() != "range"}
                probe_headers["Range"] = "bytes=0-0"
                probe = requests.get(cdn_audio_url, headers=probe_headers, timeout=30)
                content_range = probe.headers.get("content-range", "")
                if "/" in content_range:
                    remote_size = int(content_range.rsplit("/", 1)[-1])
                elif probe.headers.get("content-length"):
                    remote_size = int(probe.headers["content-length"])
            except Exception as e:
                print(f"  Size probe for Part {part_number} failed ({e}); will download normally.")
            if remote_size is not None and local_size == remote_size:
                print(f"Skipping Part {part_number}: already on disk with matching size ({local_size} bytes).")
                with active_downloads_lock:
                    downloaded_parts.add(part_number)
                    max_part_number_found = max(max_part_number_found, part_number)
                    _latest_libby_part_number_trigger = None
                return

        with active_downloads_lock:
            active_downloads_count += 1
        print(f"  Incremented active_downloads_count to: {active_downloads_count}")

        try:
            print(f"  Making direct requests.get() call for Part {part_number} to {cdn_audio_url}...")
            # Use requests.get() to download the content directly.
            # Copy the original request headers (to carry over session/auth) but force a
            # full download: the browser's audio player often sends a Range header when it
            # seeks/re-buffers, which would make the CDN return a partial (206) fragment.
            # Requesting "bytes=0-" guarantees we always get the complete part from byte 0.
            download_headers = {k: v for k, v in request.headers.items() if k.lower() != "range"}
            download_headers["Range"] = "bytes=0-"
            # Set a timeout for the requests call to prevent indefinite hangs
            download_response = requests.get(cdn_audio_url, headers=download_headers, timeout=60) # 60 seconds timeout

            content_length = len(download_response.content)
            print(f"  Requests.get() Response Body Size: {content_length} bytes for Part {part_number}")
            print(f"  Requests.get() Response Status: {download_response.status_code}")
            print(f"  Requests.get() Response Headers: {download_response.headers}")

            # Accept 200 OK or 206 Partial Content (range request) when we have body content
            if (download_response.status_code in (200, 206)) and content_length > 0:
                # Don't overwrite an existing full file with a smaller 206 partial (concurrent requests)
                if os.path.exists(file_path) and content_length < os.path.getsize(file_path):
                    print(f"Skipping {file_name}: already have larger file ({os.path.getsize(file_path)} bytes), this response is {content_length} bytes")
                else:
                    with open(file_path, "wb") as f:
                        f.write(download_response.content)

                    print(f"Successfully downloaded {file_name} ({content_length} bytes)")
                    downloaded_parts.add(part_number)
                    max_part_number_found = max(max_part_number_found, part_number)
                    
                    # Reset the trigger AFTER successfully downloading the corresponding CDN part
                    with active_downloads_lock:
                        _latest_libby_part_number_trigger = None 
                        print(f"  Reset _latest_libby_part_number_trigger to None after Part {part_number} download.")
            else:
                print(f"Failed to download {file_name}. Status: {download_response.status_code}, Body Size: {content_length} bytes.")
                if download_response.status_code == 403:
                    print("  (403 Forbidden: Access denied. This might indicate an issue with session or token.)")
                elif content_length == 0:
                    print("  (Empty response body received. This is unexpected for an actual audio part.)")
        except requests.exceptions.Timeout:
            print(f"  TimeoutError: requests.get() for Part {part_number} did not finish within 60 seconds.")
        except requests.exceptions.RequestException as e:
            print(f"  Requests Error: An error occurred during requests.get() for Part {part_number}: {e}")
        except Exception as e:
            print(f"An unexpected error occurred during download of {file_name} from {cdn_audio_url}: {e}")
        finally:
            with active_downloads_lock:
                active_downloads_count -= 1
            print(f"  Decremented active_downloads_count to: {active_downloads_count}")
    # else:
    #     # Uncomment the line below for verbose debugging of all requests
    #     # print(f"Skipping unrelated request: {request.url}")
            

# Global variable for configuration (will be loaded in run())
config = {}

def run():
    """Main function to execute the automation script."""
    global config # Declare that we are using the global config variable

    # Load configuration at the start
    config = load_config()

    # Playwright Browser Settings - now uses HEADLESS_MODE from config
    HEADLESS_MODE = False # Keep this as False for debugging, can be moved to config later if desired

    # Initialize Playwright with the stealth plugin
    with Stealth().use_sync(sync_playwright()) as p:
        browser = None # Initialize browser to None for proper cleanup in finally block
        try:
            print("Launching browser...")
            launch_args = {
                "headless": HEADLESS_MODE,
                "args": ["--no-sandbox", "--disable-setuid-sandbox"],
                "channel": "chrome" # Explicitly request the branded Chrome channel
            }

            browser = p.chromium.launch(**launch_args)
            page = browser.new_page()
            page.set_default_timeout(PLAYWRIGHT_TIMEOUT_MS)
            page.set_default_navigation_timeout(PLAYWRIGHT_TIMEOUT_MS)

            # Attach the request handler
            page.on("request", handle_request)

            # --- Step 1: Login to Libby ---
            print("Navigating to Libby login page...")
            page.goto("https://libbyapp.com/")
            page.wait_for_load_state('networkidle') # Wait for initial page load
            screenshot_path = os.path.join(config['DOWNLOAD_DIRECTORY'], "01_initial_load.png")
            page.screenshot(path=screenshot_path)

            # Click "Yes, I Have A Library Card" button
            print("Clicking 'Yes, I Have A Library Card' button...")
            try:
                # Using triple double quotes for robustness
                page.click("""button[role="button"]:has-text("Yes, I Have A Library Card")""")
                page.wait_for_load_state('networkidle')
                time.sleep(2) # Give a moment for the next page to load
                screenshot_path = os.path.join(config['DOWNLOAD_DIRECTORY'], "02_after_yes_card.png")
                page.screenshot(path=screenshot_path)
            except PlaywrightTimeoutError:
                print("Error: Could not find or click the 'Yes, I Have A Library Card' button. "
                      "The page might have changed or loaded unexpectedly.")
                return

            # Click "Search For A Library" button
            print("Clicking 'Search For A Library' button...")
            try:
                # Using triple double quotes for robustness
                page.click("""button[role="button"]:has-text("Search For A Library")""")
                page.wait_for_load_state('networkidle')
                time.sleep(2) # Give a moment for the next page to load
                screenshot_path = os.path.join(config['DOWNLOAD_DIRECTORY'], "03_after_search_library.png")
                page.screenshot(path=screenshot_path)
            except PlaywrightTimeoutError:
                print("Error: Could not find or click the 'Search For A Library' button. "
                      "The page might have changed or loaded unexpectedly.")
                return

            # Enter library name into search field
            print(f"Entering library name: '{config['LIBRARY']}' into search field...")
            try:
                # Using the provided HTML, the input has id="shibui-form-input-control-0001"
                # and placeholder="Search…". The ID is the most reliable selector.
                page.fill('#shibui-form-input-control-0001', config['LIBRARY'])
                page.wait_for_load_state('networkidle')
                time.sleep(3) # Give time for search results to load
                screenshot_path = os.path.join(config['DOWNLOAD_DIRECTORY'], "04_after_library_search_input.png")
                page.screenshot(path=screenshot_path)
            except PlaywrightTimeoutError:
                print("Error: Could not find or fill the library search input field. "
                      "Please inspect the selector.")
                return

            # --- Poll user for library selection from search results ---
            print("\nSearching for your library...")
            try:
                # Wait for search results to appear.
                # The HTML shows button.library-autocomplete-result elements.
                page.wait_for_selector('button.library-autocomplete-result', timeout=PLAYWRIGHT_TIMEOUT_MS)

                library_result_elements = page.locator('button.library-autocomplete-result').all()
                library_names = []
                for i, element in enumerate(library_result_elements):
                    # Extract the text from h2 (system name) and h3 (branch name)
                    system_name_element = element.locator('h2.library-branch-details-system-name').first
                    branch_name_element = element.locator('h3.library-branch-details-branch-name').first

                    full_name = ""
                    if system_name_element:
                        full_name += system_name_element.text_content().strip()
                    if branch_name_element:
                        branch_text = branch_name_element.text_content().strip()
                        if full_name and branch_text: # If both exist, combine with a separator
                            full_name += f" ({branch_text})"
                        elif branch_text: # If only branch name exists
                            full_name += branch_text

                    if full_name:
                        library_names.append(full_name)

                if not library_names:
                    print("No libraries found matching your search term.")
                    return

                print("Found the following libraries:")
                for i, name in enumerate(library_names):
                    print(f"{i+1}. {name}")

                # If the option index is not in config or invalid, prompt the user
                if 'LIBRARY_SEARCH_RESULT_INDEX' not in config or \
                   not (0 <= config['LIBRARY_SEARCH_RESULT_INDEX'] < len(library_names)):
                    while True:
                        try:
                            choice = input("Enter the number of your library from the list: ")
                            choice_index = int(choice) - 1
                            if 0 <= choice_index < len(library_names):
                                config['LIBRARY_SEARCH_RESULT_INDEX'] = choice_index
                                save_config(config) # Save the selected index
                                break
                            else:
                                print("Invalid choice. Please enter a number from the list.")
                        except ValueError:
                            print("Invalid input. Please enter a number.")
                else:
                    print(f"Using saved library search result option: {library_names[config['LIBRARY_SEARCH_RESULT_INDEX']]}")

                # Click the corresponding library result
                # We use the locator directly with nth() to click the specific element
                library_result_elements[config['LIBRARY_SEARCH_RESULT_INDEX']].click()
                page.wait_for_load_state('networkidle')
                time.sleep(3)
                screenshot_filename = f"05_after_select_library_from_results_{config['LIBRARY_SEARCH_RESULT_INDEX']+1}.png"
                screenshot_path = os.path.join(config['DOWNLOAD_DIRECTORY'], screenshot_filename)
                page.screenshot(path=screenshot_path)

            except PlaywrightTimeoutError:
                print("Error: Library search results did not appear in time or selector is incorrect.")
                return
            except Exception as e:
                print(f"An error occurred while selecting library from search results: {e}")
                return

            # Click "Sign In With My Card" button
            print("Clicking 'Sign In With My Card' button...")
            try:
                # Using triple double quotes for robustness
                page.click("""button[role="button"]:has-text("Sign In With My Card")""")
                page.wait_for_load_state('networkidle')
                time.sleep(2) # Give a moment for the next page to load
                screenshot_path = os.path.join(config['DOWNLOAD_DIRECTORY'], "06_after_sign_in_with_card.png")
                page.screenshot(path=screenshot_path)
            except PlaywrightTimeoutError:
                print("Error: Could not find or click the 'Sign In With My Card' button. "
                      "The page might have changed or loaded unexpectedly.")
                return

            # --- Handle library card usage option ---
            print("\nHandling 'Where do you use your library card?' option...")
            try:
                # Wait for the options to be visible
                page.wait_for_selector('.auth-ils-list button', timeout=PLAYWRIGHT_TIMEOUT_MS)

                # Get all library choice buttons
                library_choice_buttons = page.locator('.auth-ils-list button').all()
                options_text = []
                option_buttons = []
                for i, button in enumerate(library_choice_buttons):
                    # inner_text() keeps line breaks between child elements (e.g. the
                    # option name and a "Recommended choice." badge); join them visibly.
                    text = button.inner_text().strip()
                    if text: # Only add non-empty text
                        text = ' — '.join(line.strip() for line in text.splitlines() if line.strip())
                        options_text.append(text)
                        option_buttons.append(button)

                if not options_text:
                    print("No library card usage options found on the page.")
                    return

                # If the option index is not in config or invalid, list the options and prompt the user
                if 'LIBRARY_CARD_USAGE_OPTION_INDEX' not in config or \
                   not (0 <= config['LIBRARY_CARD_USAGE_OPTION_INDEX'] < len(options_text)):
                    print("Where do you use your library card?")
                    for i, option in enumerate(options_text):
                        print(f"{i+1}. {option}")
                    while True:
                        try:
                            choice = input("Enter the number of your choice: ")
                            choice_index = int(choice) - 1
                            if 0 <= choice_index < len(options_text):
                                config['LIBRARY_CARD_USAGE_OPTION_INDEX'] = choice_index
                                save_config(config) # Save the selected index
                                break
                            else:
                                print("Invalid choice. Please enter a number from the list.")
                        except ValueError:
                            print("Invalid input. Please enter a number.")
                else:
                    print(f"Using saved library card usage option: {options_text[config['LIBRARY_CARD_USAGE_OPTION_INDEX']]}")

                # Click the corresponding button based on the stored/selected index
                option_buttons[config['LIBRARY_CARD_USAGE_OPTION_INDEX']].click(timeout=PLAYWRIGHT_TIMEOUT_MS)
                try:
                    page.wait_for_load_state('networkidle', timeout=PLAYWRIGHT_TIMEOUT_MS)
                except PlaywrightTimeoutError:
                    print("Note: network did not go idle after selecting card usage option; continuing anyway.")
                time.sleep(3)
                # Fix: Separated f-string for filename from os.path.join
                filename = f"07_after_select_card_usage_{config['LIBRARY_CARD_USAGE_OPTION_INDEX']+1}.png"
                screenshot_path = os.path.join(config['DOWNLOAD_DIRECTORY'], filename)
                page.screenshot(path=screenshot_path)

            except PlaywrightTimeoutError as e:
                print(f"Error: timed out during library card usage option step: {e}")
                return
            except Exception as e:
                print(f"An error occurred while handling library card usage options: {e}")
                return

            # Enter library card number
            print(f"Entering library card number: '{config['LIBRARY_CARD_NUMBER']}' into Card Number field...")
            try:
                # Using the provided HTML, the input has id="shibui-form-input-control-0002"
                # and placeholder="Search…". The ID is the most reliable selector.
                page.fill('#shibui-form-input-control-0002', config['LIBRARY_CARD_NUMBER'])
                page.wait_for_load_state('networkidle')
                time.sleep(3) # Give time for search results to load (search happens automatically)
                screenshot_path = os.path.join(config['DOWNLOAD_DIRECTORY'], "08_after_card_number_input.png")
                page.screenshot(path=screenshot_path)
            except PlaywrightTimeoutError:
                print("Error: Could not find or fill the library card number field. "
                      "Please inspect the selector.")
                return

            # Click "Next" button
            print("Clicking 'Next' button...")
            try:
                # Using triple double quotes for robustness
                page.click("""button[role="button"]:has-text("Next")""")
                page.wait_for_load_state('networkidle')
                time.sleep(2) # Give a moment for the next page to load
                screenshot_path = os.path.join(config['DOWNLOAD_DIRECTORY'], "09_after_card_number_next.png")
                page.screenshot(path=screenshot_path)
            except PlaywrightTimeoutError:
                print("Error: Could not find or click the 'Next' button. "
                      "The page might have changed or loaded unexpectedly.")
                return

            # Enter PIN
            print(f"Entering PIN: *********** into PIN field...")
            try:
                # Using the provided HTML, the input has id="shibui-form-input-control-0003"
                # and placeholder="Search…". The ID is the most reliable selector.
                page.fill('#shibui-form-input-control-0003', config['LIBBY_PASSWORD'])
                page.wait_for_load_state('networkidle')
                time.sleep(3) # Give time for search results to load (search happens automatically)
                screenshot_path = os.path.join(config['DOWNLOAD_DIRECTORY'], "10_after_pin_input.png")
                page.screenshot(path=screenshot_path)
            except PlaywrightTimeoutError:
                print("Error: Could not find or fill the PIN field. "
                      "Please inspect the selector.")
                return

            # Click Sign In button
            print("Attempting to log in...")
            try:
                # Using triple double quotes for robustness
                page.click("""button[role="button"]:has-text("Sign In")""")
                page.wait_for_load_state('networkidle')
                time.sleep(2) # Give a moment for the next page to load
                screenshot_path = os.path.join(config['DOWNLOAD_DIRECTORY'], "11_after_final_sign_in.png")
                page.screenshot(path=screenshot_path)
            except PlaywrightTimeoutError:
                print("Error: Could not find or click the 'Sign In' button. "
                      "The page might have changed or loaded unexpectedly.")
                return

            # Click "Next" button (This might be a final confirmation after successful login)
            print("Clicking 'Next' button (post-login confirmation)...")
            try:
                # Using triple double quotes for robustness
                page.click("""button[role="button"]:has-text("Next")""", timeout=5000) # Shorter timeout for optional button
                page.wait_for_load_state('networkidle')
                time.sleep(2)
                screenshot_path = os.path.join(config['DOWNLOAD_DIRECTORY'], "12_after_post_login_next.png")
                page.screenshot(path=screenshot_path)
            except PlaywrightTimeoutError:
                print("No 'Next' button found after login, proceeding.")
            except Exception as e:
                print(f"An unexpected error occurred clicking post-login 'Next': {e}")


            page.wait_for_load_state('networkidle', timeout=PLAYWRIGHT_TIMEOUT_MS)
            print("Login attempt complete. Checking if logged in...")
            screenshot_path = os.path.join(config['DOWNLOAD_DIRECTORY'], "13_after_login_complete.png")
            page.screenshot(path=screenshot_path)

            # --- Step 2: Navigate to Audiobook ---
            print("Navigating to audiobook section...")

            # First, navigate to the "Shelf" or "Loans" section.
            print("Clicking 'Shelf' button in footer navigation...")
            try:
                page.click('#footer-nav-shelf')
                page.wait_for_load_state('networkidle')
                time.sleep(2) # Short pause for UI to settle
                screenshot_path = os.path.join(config['DOWNLOAD_DIRECTORY'], "14_on_shelf_page.png")
                page.screenshot(path=screenshot_path)
            except PlaywrightTimeoutError:
                print("Error: Could not find or click the 'Shelf' button. "
                      "The footer navigation might have changed or not loaded.")
                return

            # --- Prompt user for audiobook selection on the shelf ---
            print("\nAudiobooks on your Shelf:")
            save_snapshot(page, "shelf")
            try:
                # Only audiobook tiles can be opened in the Libby player. Ebook loans use
                # "Read With..." and are listed separately so users know why a title is missing.
                page.wait_for_selector('.title-list-tiles .title-tile', timeout=PLAYWRIGHT_TIMEOUT_MS)

                audiobook_titles, audiobook_authors = collect_shelf_audiobook_loans(page)
                hold_titles, _ = collect_shelf_audiobook_holds(page)
                ebook_titles, _ = collect_shelf_titles(page, 'data-title-tile-format_book')

                print(f"DEBUG: Parsed Audiobook Titles: {audiobook_titles}")
                print(f"DEBUG: Parsed Audiobook Authors: {audiobook_authors}")
                if hold_titles:
                    print(f"DEBUG: Audiobook holds on shelf (borrow first, not downloadable yet): {hold_titles}")
                if ebook_titles:
                    print(f"DEBUG: Ebook-only loans on shelf (not downloadable here): {ebook_titles}")

                if not audiobook_titles:
                    print("No borrowed audiobooks found on your shelf.")
                    preselect_title = normalize_text(os.environ.get('LIBBY_BOOK_TITLE', ''))
                    if preselect_title:
                        explain_missing_audiobook(page, preselect_title)
                    elif hold_titles:
                        print("\nYou do have audiobook holds, but none are borrowed yet:")
                        for title in hold_titles:
                            print(f"  - {title} (hold — tap Borrow in Libby first)")
                    elif ebook_titles:
                        print("\nYou do have ebook loans on your shelf, but this script only downloads audiobooks:")
                        for title in ebook_titles:
                            print(f"  - {title} (ebook)")
                    return

                # Prompt user for which audiobook on their shelf they want to download.
                for i, title in enumerate(audiobook_titles):
                    print(f"{i+1}. {title}")
                if hold_titles:
                    print("\nAudiobook holds on your shelf (borrow in Libby before downloading):")
                    for title in hold_titles:
                        print(f"  - {title}")
                if ebook_titles:
                    print("\nEbook loans on your shelf (not supported by this script):")
                    for title in ebook_titles:
                        print(f"  - {title}")

                # Non-interactive preselect by title (for unattended/testing runs):
                # LIBBY_BOOK_TITLE=Wicked picks the first shelf title containing the
                # string, case-insensitively. Shelf order is not stable, so piping a
                # number into stdin can select the wrong book.
                preselect_title = normalize_text(os.environ.get('LIBBY_BOOK_TITLE', ''))
                preselect_matches = [i for i, t in enumerate(audiobook_titles) if preselect_title and title_matches(preselect_title, t)]
                if preselect_matches:
                    choice_index = preselect_matches[0]
                    selected_title = audiobook_titles[choice_index]
                    print(f"Preselected via LIBBY_BOOK_TITLE={preselect_title!r}: '{selected_title}'")
                elif preselect_title:
                    explain_missing_audiobook(page, preselect_title)
                    return
                # Select audiobook (auto-select only when there's a single option)
                elif AUTO_SELECT_FIRST_AUDIOBOOK and len(audiobook_titles) == 1:
                    choice_index = 0
                    selected_title = audiobook_titles[choice_index]
                    print(f"Auto-selected the only audiobook on the shelf: '{selected_title}'")
                else:
                    # Loop until a valid choice is made
                    selected_title = None
                    while selected_title is None:
                        try:
                            choice = input("Enter the number of the audiobook to open: ")
                            choice_index = int(choice) - 1
                            if 0 <= choice_index < len(audiobook_titles):
                                selected_title = audiobook_titles[choice_index]
                                print(f"You selected: '{selected_title}'")
                            else:
                                print("Invalid choice. Please enter a number from the list.")
                        except ValueError:
                            print("Invalid input. Please enter a number.")

                # Fetch series/author metadata from title details before opening the player.
                selected_tile = page.locator(
                    '.title-list-tiles .title-tile.data-tile-class_loan.data-title-tile-format_audiobook'
                ).nth(choice_index)
                selected_author = audiobook_authors[choice_index] if choice_index < len(audiobook_authors) else ""
                if tile_is_hold(selected_tile):
                    explain_hold_not_borrowed(selected_title, hold_status_from_tile(selected_tile))
                    return

                title_metadata = fetch_title_metadata(
                    page,
                    selected_tile,
                    fallback_title=selected_title,
                    fallback_author=selected_author,
                )

                book_download_dir = build_book_download_dir(
                    config['DOWNLOAD_DIRECTORY'],
                    title_metadata,
                    fallback_title=selected_title,
                    fallback_author=selected_author,
                )
                print(f"Download directory for this book: {book_download_dir}")
                os.makedirs(book_download_dir, exist_ok=True)
                download_cover_image(title_metadata.get('cover_url'), book_download_dir)
                config['DOWNLOAD_DIRECTORY'] = book_download_dir
                book_folder_name = os.path.basename(book_download_dir)

                # Open the tile the user picked by index - not by title text. Libby titles
                # often contain non-breaking spaces that break :has-text() matching.
                open_audiobook_button_selector = """button[role="button"]:has-text("Open Audiobook")"""
                open_button = selected_tile.locator(open_audiobook_button_selector)
                if open_button.count() == 0:
                    explain_hold_not_borrowed(selected_title, hold_status_from_tile(selected_tile))
                    return
                open_button.click(timeout=PLAYWRIGHT_TIMEOUT_MS)
                try:
                    page.wait_for_load_state('networkidle', timeout=PLAYWRIGHT_TIMEOUT_MS)
                except PlaywrightTimeoutError:
                    pass  # Libby's SPA often never reaches networkidle; the player still loads
                time.sleep(3)
                filename = f"15_after_open_audiobook_button_{book_folder_name.replace(' ', '_')}.png"
                screenshot_path = os.path.join(config['DOWNLOAD_DIRECTORY'], filename)
                page.screenshot(path=screenshot_path)

            except PlaywrightTimeoutError as e:
                print(f"Error: Timed out while opening the selected audiobook on the shelf: {e}")
                return
            except Exception as e:
                print(f"An error occurred while listing/selecting audiobooks: {e}")
                return

            print("Audiobook player opened. Rewinding to beginning...")
            time.sleep(5)
            save_snapshot(page, "player_opened")
            screenshot_path = os.path.join(config['DOWNLOAD_DIRECTORY'], "16_after_audiobook_detail_load.png")
            page.screenshot(path=screenshot_path)

            # Navigate to the beginning of the book so the forward pass starts from Part 1.
            # Recording is gated to strict ascending order (see handle_request), so the
            # resume position that loaded above was ignored; Part 1 will be the first part
            # accepted once the rewind navigation triggers it.
            global downloads_enabled, _latest_libby_part_number_trigger
            try:
                player_frame_init = page.frame_locator('iframe[src*="listen.libbyapp.com"]')
                prev_btn = player_frame_init.locator('button[aria-label*="Previous Chapter"]')
                # Wait for the player to be ready before rewinding, keyed on the Next Chapter
                # button. That button is present throughout the book, whereas Previous Chapter
                # is hidden at the very start - so waiting on Previous Chapter would stall for
                # the full timeout whenever the book opens at/near the beginning.
                try:
                    player_frame_init.locator('button[aria-label*="Next Chapter"]').first.wait_for(state='visible', timeout=PLAYWRIGHT_TIMEOUT_MS)
                except PlaywrightTimeoutError:
                    print("Warning: player controls did not become visible within 60s; rewind may fail.")

                # The initial auto-load (resume position + manifest traffic) is done. Enable
                # the handler now so the rewind navigation's Part 1 trigger is the first
                # thing captured - not the mid-book resume position that loaded above.
                downloads_enabled = True
                print("Rewind step reached: request handler enabled, capturing from Part 1 onward.")

                # If Libby shows a "Recent place" history-back button pointing near the
                # start of the book, click it to jump straight there instead of stepping
                # back one chapter at a time.
                try:
                    back_btn = player_frame_init.locator('button.history-bar-back-button')
                    if back_btn.count() > 0 and back_btn.first.is_visible():
                        place_text = back_btn.first.locator('.place-phrase-visual').first.text_content().strip()
                        total_sec = 0
                        for segment in place_text.split(':'):
                            total_sec = total_sec * 60 + int(segment)
                        if total_sec == 0:
                            print(f"History-back button offers recent place {place_text}; jumping straight to it.")
                            back_btn.first.click(timeout=3000)
                            time.sleep(3)
                        else:
                            print(f"History-back button present but points to {place_text}; ignoring it.")
                except Exception as e:
                    print(f"History-back shortcut not used: {e}")

                for rewind_i in range(300):
                    try:
                        prev_btn.first.click(timeout=2000)
                        time.sleep(0.5)
                    except (PlaywrightTimeoutError, Exception):
                        print(f"Reached beginning of book after {rewind_i} Previous Chapter clicks.")
                        break
                time.sleep(3)
            except Exception as e:
                print(f"Error rewinding to beginning: {e}")

            # --- Step 3: Player Control and Forward Part Discovery ---
            initial_parts_count = len(downloaded_parts)
            no_new_parts_count = 0
            MAX_NO_NEW_PARTS_ITERATIONS = 10 # Stop if no new parts found for this many clicks
            MAX_FORWARD_CLICKS = 500 # Safety limit for forward clicks

            MAX_SEEK_PROBES = 20          # Binary-search probes per missing part (resolution ~1/2^20 of the slider)
            SEEK_TRIGGER_TIMEOUT_SEC = 12  # Max wait for a fresh part trigger after a seek probe (the player defers loading after rapid scrubs)
            MAX_GAP_SKIP_CLICKS = 200     # Fallback 15s-skip budget floor when no seek slider is found
            GAP_SKIP_WAIT_SEC = 2         # Wait after each seek/skip for the part trigger to fire

            for i in range(MAX_FORWARD_CLICKS):
                current_parts_count = len(downloaded_parts)
                expected_next_part = next_expected_part()  # Next part we still need, in order
                print(f"Forward pass iteration {i+1}. Current parts downloaded: {current_parts_count} (expect next part {expected_next_part})")

                # Advance with the "Next Chapter" button only (aria-label match). This has
                # proven reliable; the old fallback selectors (chapter-bar-next-button,
                # 15s-skip, JS clicks) were removed because they stay present-but-hidden at
                # the end of the book and kept the loop "advancing" forever.
                player_frame = page.frame_locator('iframe[src*="listen.libbyapp.com"]')
                next_chapter_btn = player_frame.locator('button[aria-label*="Next Chapter"]')

                # Log the button's state every iteration so its behaviour (especially at the
                # end of the book, where it becomes present-but-hidden) is always visible.
                nc_count = -1
                nc_visible = False
                nc_aria = None
                nc_bbox = None
                try:
                    nc_count = next_chapter_btn.count()
                    if nc_count > 0:
                        first_btn = next_chapter_btn.first
                        nc_visible = first_btn.is_visible()
                        nc_aria = first_btn.get_attribute('aria-label')
                        nc_bbox = first_btn.bounding_box()
                except Exception as e:
                    print(f"  [next-chapter] error reading button metadata: {e}")
                print(f"  [next-chapter] count={nc_count} visible={nc_visible} aria-label={nc_aria!r} bbox={nc_bbox}")

                # End of book: the Next Chapter button is gone or no longer visible (it stays
                # in the DOM but hidden on the last chapter). Playback never reaches the true
                # audio end via chapter skips, so this - not the play button's "The End"
                # text - is our stop signal.
                if nc_count == 0 or not nc_visible:
                    print("End of book detected: 'Next Chapter' button is not present/visible. Stopping forward pass.")
                    save_snapshot(page, "end_of_book")
                    break

                button_found = False
                try:
                    next_chapter_btn.first.click(timeout=2000)
                    print("  Clicked Next Chapter.")
                    button_found = True
                    time.sleep(5)
                except (PlaywrightTimeoutError, Exception) as e:
                    print(f"  Next Chapter click failed despite being visible: {e}")
                    break # Treat an unclickable button as end of book

                # Gap recovery: chapters can span multiple audio parts, so a Next
                # Chapter jump can skip over the start of a part entirely (e.g. it lands
                # on Part 6 while Part 5 was never played). Because recording is gated to
                # ascending order, the skipped part simply never got accepted: the next
                # expected part is still missing even though we've now seen a higher part.
                # When that happens, step back chapter-by-chapter until the player is
                # positioned BEFORE the missing part, then advance in 15s steps; each
                # skipped part triggers in order and is accepted as we pass through it.
                if button_found:
                    time.sleep(2)  # let the part trigger from the chapter jump arrive
                    landed = _last_seen_part  # highest part the player has reached so far
                    missing_part = expected_next_part
                    if landed > missing_part:
                        print(f"Gap detected: player reached Part {landed} but Part {missing_part} was skipped. Stepping back to before Part {missing_part}...")
                        save_snapshot(page, f"gap_recovery_part{missing_part}")
                        player_frame = page.frame_locator('iframe[src*="listen.libbyapp.com"]')
                        # We're sitting at the overshoot chapter's start right now: the
                        # missing part's boundary lies BEFORE this tape position. Record it
                        # as the upper bound for the seek search below.
                        gap_hi_px = tape_position_px(player_frame)
                        prev_chapter_btn = player_frame.locator('button[aria-label*="Previous Chapter"]')

                        # Step back one chapter at a time until the player lands on a part
                        # earlier than the one we're missing (a single chapter back is often
                        # not enough - the skipped part can start several chapters earlier).
                        MAX_BACK_CHAPTERS = 15
                        back_clicks = 0
                        while back_clicks < MAX_BACK_CHAPTERS:
                            try:
                                prev_chapter_btn.first.click(timeout=3000)
                            except (PlaywrightTimeoutError, Exception) as e:
                                print(f"  Reached start of book while stepping back ({e}).")
                                break
                            back_clicks += 1
                            time.sleep(2)
                            if _last_seen_part and _last_seen_part < missing_part:
                                print(f"  Stepped back {back_clicks} chapter(s) to Part {_last_seen_part}; now scanning forward for Part {missing_part}.")
                                break
                        else:
                            print(f"  Stepped back {back_clicks} chapters (cap reached); scanning forward from here.")

                        # Binary-search the seekometer tape for the missing part(s).
                        # The tape is time-linear over the whole book (2px/sec), so seeking
                        # to a tape position triggers that position's part request, and
                        # position -> part number is monotonic: each probe halves the
                        # interval. The order gate reads out the result: overshoot probes
                        # are ignored, and the probe that lands inside the target part is
                        # the expected one, so it's accepted and downloads.
                        # Search between here (known to be in a part < target after the
                        # step-back) and the overshoot chapter's start captured above.
                        lo = tape_position_px(player_frame)
                        hi = gap_hi_px
                        if hi is None:
                            try:
                                tape_w = player_frame.locator('.seekometer-tape').first.evaluate("el => el.getBoundingClientRect().width")
                                hi = float(tape_w)
                            except Exception as e:
                                print(f"  [tape-seek] could not read tape width ({e})")
                        seek_ok = lo is not None and hi is not None and hi > lo
                        if not seek_ok:
                            print(f"  [seek-search] no usable tape bounds (lo={lo}, hi={hi}); falling back to 15s skips.")
                        if seek_ok:
                            while next_expected_part() < landed and seek_ok:
                                target = next_expected_part()
                                s_lo, s_hi = lo, hi
                                probes = 0
                                while probes < MAX_SEEK_PROBES and next_expected_part() == target:
                                    if s_hi - s_lo < 30:  # interval down to ~15s of audio; boundary pinned
                                        print(f"  [seek-search] interval collapsed to {s_hi - s_lo:.0f}px without capturing Part {target}.")
                                        break
                                    mid = (s_lo + s_hi) / 2.0
                                    seq_before = _trigger_seq
                                    landed_px = seek_tape_to_px(page, player_frame, mid)
                                    if landed_px is None:
                                        print("  [seek-search] tape drag failed; falling back to 15s skips.")
                                        seek_ok = False
                                        break
                                    probes += 1
                                    # Wait for a FRESH trigger from this seek before classifying
                                    # the probe - a fixed short sleep can read a stale part number
                                    # and misclassify. Seeks within the already-loaded part fire
                                    # no new trigger, so on timeout the stale value IS correct.
                                    probe_deadline = time.time() + SEEK_TRIGGER_TIMEOUT_SEC
                                    while time.time() < probe_deadline and _trigger_seq == seq_before:
                                        time.sleep(0.25)
                                    fresh = _trigger_seq != seq_before
                                    if fresh:
                                        # Scrubbing fires triggers for parts merely passed over.
                                        # Wait for the stream to go quiet (2s) so we read the
                                        # trigger belonging to the tape's resting position.
                                        last_seq = _trigger_seq
                                        quiet_since = time.time()
                                        settle_deadline = time.time() + 8
                                        while time.time() < settle_deadline and (time.time() - quiet_since) < 2.0:
                                            time.sleep(0.25)
                                            if _trigger_seq != last_seq:
                                                last_seq = _trigger_seq
                                                quiet_since = time.time()
                                    cur = _last_seen_part
                                    # Classify against where the tape actually SETTLED, not the
                                    # requested midpoint - drags aren't pixel-accurate and the
                                    # tape can drift after release; narrowing by the wrong
                                    # coordinate corrupts the interval.
                                    settled_px = tape_position_px(player_frame)
                                    if settled_px is None:
                                        settled_px = landed_px
                                    print(f"  [seek-search] probe {probes}/{MAX_SEEK_PROBES}: tape_px={settled_px:.0f} (~{settled_px / TAPE_PX_PER_SEC / 60:.1f} min) -> part {cur} ({'fresh trigger' if fresh else 'no new trigger'}, target {target})")
                                    if cur < target:
                                        s_lo = max(s_lo, settled_px)
                                    elif cur > target:
                                        s_hi = min(s_hi, settled_px)
                                if not seek_ok:
                                    break
                                if next_expected_part() == target:
                                    print(f"  Seek search could not trigger Part {target} in {probes} probes; falling back to 15s skips.")
                                    break
                                print(f"  Seek search captured Part {target} in {probes} probes.")
                                lo = tape_position_px(player_frame) or lo  # continue from here for the next missing part

                        # Fallback when no slider was found or the search stalled: play
                        # through the chapter in 15s steps, budgeted from the chapter length
                        # in the Next Chapter label ("Next Chapter . 88 minutes ahead.").
                        if next_expected_part() < landed:
                            skip_budget = MAX_GAP_SKIP_CLICKS
                            try:
                                label = player_frame.locator('button[aria-label*="Next Chapter"]').first.get_attribute('aria-label') or ""
                                hours_m = re.search(r'(\d+)\s*hour', label)
                                minutes_m = re.search(r'(\d+)\s*minute', label)
                                total_min = (int(hours_m.group(1)) * 60 if hours_m else 0) + (int(minutes_m.group(1)) if minutes_m else 0)
                                if total_min:
                                    skip_budget = max(skip_budget, (total_min * 60) // 15 + 20)
                                    print(f"  Chapter ahead is ~{total_min} min of audio; skip budget set to {skip_budget}.")
                            except Exception as e:
                                print(f"  Could not read chapter length for skip budget ({e}); using default {skip_budget}.")

                            skip_btn = player_frame.locator('button[aria-label*="Advance 15 seconds"], button.mini-player-jump-ahead')
                            skip_clicks = 0
                            while next_expected_part() < landed and skip_clicks < skip_budget:
                                try:
                                    skip_btn.first.click(timeout=3000)
                                except (PlaywrightTimeoutError, Exception) as e:
                                    print(f"  15s advance failed during gap recovery: {e}")
                                    break
                                skip_clicks += 1
                                time.sleep(GAP_SKIP_WAIT_SEC)
                                if skip_clicks % 20 == 0:
                                    print(f"  Gap recovery: {skip_clicks}/{skip_budget} skips so far, next still-missing part is {next_expected_part()} (filling up to {landed - 1})")

                        if next_expected_part() < landed:
                            print(f"  Gap recovery gave up; Part {next_expected_part()} still missing (Step 4 will retry).")
                        else:
                            print(f"  Gap recovery done; parts up to {landed - 1} captured. Resuming chapter skips to reach Part {landed}.")

                        # An accepted trigger only marks the part; the browser's CDN audio
                        # fetch is still in flight. Clicking Next Chapter now can abort that
                        # fetch ("No Playwright response object"), losing the part until the
                        # Step 4 retry. Let pending downloads settle before navigating away.
                        pending = []
                        wait_deadline = time.time() + 20
                        while time.time() < wait_deadline:
                            with active_downloads_lock:
                                pending = sorted(p for p in found_parts if p not in downloaded_parts)
                            if not pending:
                                break
                            time.sleep(1)
                        if pending:
                            print(f"  Warning: recovered part(s) {pending} still not downloaded after 20s; Step 4 will retry them.")

                if len(downloaded_parts) == current_parts_count:
                    no_new_parts_count += 1
                    print(f"No new parts detected in this iteration ({no_new_parts_count}/{MAX_NO_NEW_PARTS_ITERATIONS}).")
                    if no_new_parts_count >= MAX_NO_NEW_PARTS_ITERATIONS:
                        print("Stopping forward pass: No new parts found for several iterations.")
                        break
                else:
                    no_new_parts_count = 0 # Reset counter if new parts were found

            print(f"Forward pass complete. Total unique parts found: {len(downloaded_parts)}")
            print(f"Highest part number downloaded: {max_part_number_found}; highest part number seen in any trigger: {max_part_number_seen}")

            # --- Wait for all active downloads to complete before proceeding ---
            print("Waiting for all active downloads to complete...")
            while True:
                with active_downloads_lock:
                    current_active = active_downloads_count
                if current_active == 0:
                    print("All downloads appear to be complete.")
                    break
                print(f"Still {current_active} downloads active. Waiting...")
                time.sleep(5) # Wait a bit before checking again

            # --- Step 4: Retrieve Missing Parts via Signed URL Extraction ---
            print("Checking for any missing parts and attempting to retrieve them...")
            # Range over the highest part SEEN, not just downloaded: parts skipped by
            # chapter jumps were never accepted, so max_part_number_found alone would
            # undercount and silently declare success with parts missing (e.g. a run
            # that downloaded 1-8 but saw triggers for 11 is missing 9-11, not "done").
            highest_known_part = max(max_part_number_found, max_part_number_seen)
            missing_parts = []
            for i in range(1, highest_known_part + 1):
                if i not in downloaded_parts:
                    missing_parts.append(i)

            if not missing_parts:
                print("No missing parts detected. All parts downloaded successfully!")
            else:
                print(f"Missing parts identified: {sorted(missing_parts)}")

                # These parts' triggers were accepted during the forward pass, but the
                # audio download never completed. The order gate in handle_request keys
                # on found_parts, so unless we evict them it would reject every re-trigger
                # as out-of-order (expecting a part past the end of the book) and the
                # retries below could never succeed.
                with active_downloads_lock:
                    for p in missing_parts:
                        found_parts.discard(p)
                    _latest_libby_part_number_trigger = None

                player_frame_obj = None
                for frame in page.frames:
                    if 'listen.libbyapp.com' in frame.url:
                        player_frame_obj = frame
                        break

                # Try to extract all signed spine URLs from the player's JavaScript
                if player_frame_obj and len(_signed_spine_urls) < highest_known_part:
                    print("Extracting signed spine URLs from player...")
                    try:
                        spine_data = player_frame_obj.evaluate(r"""
                            () => {
                                try {
                                    var results = {};
                                    var scripts = document.querySelectorAll('script');
                                    for (var s of scripts) {
                                        var text = s.textContent || '';
                                        var matches = text.matchAll(/Part(\d+)\.mp3\?cmpt=([A-Za-z0-9+\/=%]+--[a-f0-9]+)/g);
                                        for (var m of matches) {
                                            results[parseInt(m[1])] = m[2];
                                        }
                                    }
                                    function searchObj(obj, depth) {
                                        if (depth > 3 || !obj) return;
                                        try {
                                            if (typeof obj === 'string' && obj.includes('cmpt=') && obj.includes('Part')) {
                                                var m = obj.match(/Part(\d+)\.mp3\?cmpt=([A-Za-z0-9+\/=%]+--[a-f0-9]+)/);
                                                if (m) results[parseInt(m[1])] = m[2];
                                            }
                                            if (typeof obj === 'object') {
                                                for (var k in obj) {
                                                    try { searchObj(obj[k], depth + 1); } catch(e) {}
                                                }
                                            }
                                        } catch(e) {}
                                    }
                                    try { searchObj(window.__NEXT_DATA__, 0); } catch(e) {}
                                    try { searchObj(window.__STATE__, 0); } catch(e) {}
                                    try { searchObj(window.roster, 0); } catch(e) {}
                                    return {found: Object.keys(results).length, urls: results};
                                } catch(e) {
                                    return {error: e.message, found: 0, urls: {}};
                                }
                            }
                        """)
                        print(f"  Spine URL extraction: found {spine_data.get('found', 0)} signed URLs")
                        if spine_data.get('urls'):
                            for part_str, cmpt in spine_data['urls'].items():
                                part_num = int(part_str)
                                if part_num not in _signed_spine_urls and _libby_url_template:
                                    full_url = f"{_libby_url_template}Part{part_num:02d}.mp3?cmpt={cmpt}"
                                    _signed_spine_urls[part_num] = full_url
                    except Exception as e:
                        print(f"  Spine URL extraction failed: {e}")

                for missing_part in sorted(missing_parts):
                    if missing_part in downloaded_parts:
                        continue
                    print(f"Attempting to retrieve missing Part {missing_part} (spine {missing_part - 1})...")

                    # Method 1: Use stored signed URL if available
                    if missing_part in _signed_spine_urls and missing_part not in downloaded_parts:
                        signed_url = _signed_spine_urls[missing_part]
                        print(f"  Using signed URL for Part {missing_part}...")
                        try:
                            if player_frame_obj:
                                player_frame_obj.evaluate("""
                                    (url) => {
                                        var audio = new Audio();
                                        audio.src = url;
                                        audio.load();
                                    }
                                """, signed_url)
                                time.sleep(8)
                        except Exception as e:
                            print(f"  Signed URL fetch failed: {e}")

                    if missing_part in downloaded_parts:
                        print(f"  Successfully retrieved Part {missing_part}!")
                    else:
                        print(f"  Failed to retrieve Part {missing_part}.")

                # Method 2: Systematic forward scan from beginning for remaining missing parts
                still_missing = [p for p in missing_parts if p not in downloaded_parts]
                if still_missing:
                    print(f"\nSystematic scan for {len(still_missing)} remaining missing parts: {still_missing}")
                    player_fl = page.frame_locator('iframe[src*="listen.libbyapp.com"]')

                    print("  Rewinding to beginning...")
                    for _ in range(50):
                        try:
                            player_fl.locator('button[aria-label*="Previous Chapter"]').first.click(timeout=2000)
                            time.sleep(0.5)
                        except (PlaywrightTimeoutError, Exception):
                            break
                    time.sleep(3)

                    print("  Scanning forward through all chapters...")
                    for scan_i in range(100):
                        remaining = [p for p in still_missing if p not in downloaded_parts]
                        if not remaining:
                            print(f"  All missing parts found after {scan_i} chapter scans!")
                            break
                        try:
                            player_fl.locator('button[aria-label*="Next Chapter"]').first.click(timeout=3000)
                            time.sleep(3)
                            newly_found = [p for p in still_missing if p in downloaded_parts and p not in found_parts]
                        except (PlaywrightTimeoutError, Exception):
                            print(f"  End of book reached after {scan_i} chapter scans.")
                            break

                    final_missing = [p for p in missing_parts if p not in downloaded_parts]
                    if final_missing:
                        print(f"  Parts still missing after full scan: {final_missing}")
                    else:
                        print(f"  All parts successfully retrieved!")

            print("All download attempts complete.")

            downloaded_files = sorted(
                f for f in os.listdir(book_download_dir)
                if f.lower().endswith('.mp3')
            )
            title_display = title_metadata.get('title') or selected_title
            author_display = title_metadata.get('author_name') or selected_author
            print(f"\nDownloaded \"{title_display}\" by {author_display} ({len(downloaded_files)} parts) to: {book_download_dir}")

        except PlaywrightTimeoutError as e:
            print(f"Playwright operation timed out: {e}. This often means a selector was not found or a page took too long to load.")
            print("Please review your selectors and internet connection.")
        except Exception as e:
            print(f"An unexpected error occurred: {e}")
        finally:
            if browser:
                print("Closing browser...")
                browser.close()
            print("Script finished.")

# --- How to Run ---
if __name__ == "__main__":
    with open(RUN_LOG_FILE, 'w') as _log:
        _log_file = _log
        sys.stdout = _Tee(sys.__stdout__, _log)
        sys.stderr = _Tee(sys.__stderr__, _log)
        try:
            run()
        finally:
            sys.stdout = sys.__stdout__
            sys.stderr = sys.__stderr__
            _log_file = None
