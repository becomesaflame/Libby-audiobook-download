import os
import re
import time
import requests
import json # Import the json library for config file handling
import threading # Import threading for the lock
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from playwright_stealth import Stealth # Import the stealth library

# --- Configuration File Path ---
CONFIG_FILE = 'libby_config.json'

# --- Testing Configuration ---
AUTO_SELECT_FIRST_AUDIOBOOK = True  # Set to False for manual selection

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

def sanitize_filename(name):
    """Remove or replace characters that are invalid in directory/file names."""
    sanitized = re.sub(r'[<>:"/\\|?*]', '_', name)
    sanitized = sanitized.strip().strip('.')
    sanitized = re.sub(r'[_\s]+', ' ', sanitized)
    return sanitized.strip()

# --- Network Request Handler ---
def handle_request(request):
    """
    Callback function to process intercepted network requests.
    Identifies and downloads audiobook MP3 parts directly using requests library.
    """
    global downloaded_parts, found_parts, max_part_number_found, active_downloads_count, _latest_libby_part_number_trigger, _libby_url_template, _last_seen_part

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
            print(f"Skipping CDN request {request.url}: No preceding Libby part trigger found. This might be an unrelated CDN asset.")
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
                page.wait_for_selector('button.library-autocomplete-result', timeout=15000)

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
                page.wait_for_selector('.auth-ils-list button', timeout=10000)

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
                option_buttons[config['LIBRARY_CARD_USAGE_OPTION_INDEX']].click(timeout=10000)
                try:
                    page.wait_for_load_state('networkidle', timeout=15000)
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


            page.wait_for_load_state('networkidle', timeout=60000) # Give more time for login redirect
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
            try:
                # Wait for audiobook tiles to be visible
                page.wait_for_selector('.title-list-tiles .title-tile', timeout=15000)

                audiobook_tiles = page.locator('.title-list-tiles .title-tile').all()
                audiobook_titles = []
                audiobook_authors = []
                for i, tile in enumerate(audiobook_tiles):
                    title_element = tile.locator('.title-tile-title').first
                    if title_element:
                        title_text = title_element.text_content().strip().replace('&nbsp;', ' ')
                        audiobook_titles.append(title_text)

                    author_text = ""
                    for author_selector in ['.title-tile-author', '.title-tile-creator', '.title-tile-subtitle']:
                        try:
                            author_element = tile.locator(author_selector).first
                            if author_element and author_element.is_visible():
                                candidate = author_element.text_content().strip().replace('&nbsp;', ' ')
                                if candidate:
                                    author_text = candidate
                                    break
                        except Exception:
                            continue
                    audiobook_authors.append(author_text)

                print(f"DEBUG: Parsed Audiobook Titles: {audiobook_titles}")
                print(f"DEBUG: Parsed Audiobook Authors: {audiobook_authors}")

                if not audiobook_titles:
                    print("No audiobooks found on your shelf.")
                    return

                # Prompt user for which book on their shelf they want to download.
                # Print numbered list for user selection
                for i, title in enumerate(audiobook_titles):
                    print(f"{i+1}. {title}")

                # Select audiobook (auto-select only when there's a single option)
                if AUTO_SELECT_FIRST_AUDIOBOOK and len(audiobook_titles) == 1:
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

                # Create a book-specific download subdirectory (Author/Title or just Title)
                selected_author = audiobook_authors[choice_index] if choice_index < len(audiobook_authors) else ""
                book_folder_name = sanitize_filename(selected_title)
                if selected_author:
                    author_folder_name = sanitize_filename(selected_author)
                    book_download_dir = os.path.join(config['DOWNLOAD_DIRECTORY'], author_folder_name, book_folder_name)
                    print(f"Author: '{selected_author}' -> folder: '{author_folder_name}'")
                else:
                    book_download_dir = os.path.join(config['DOWNLOAD_DIRECTORY'], book_folder_name)
                    print("No author info found on shelf tile; using title-only folder.")

                os.makedirs(book_download_dir, exist_ok=True)
                print(f"Download directory for this book: {book_download_dir}")
                config['DOWNLOAD_DIRECTORY'] = book_download_dir

                # Locate the specific audiobook tile using the selected title
                audiobook_tile_locator = page.locator(f"""div.title-tile:has-text("{selected_title}")""").first
                # Click the "Open Audiobook" button within that tile
                open_audiobook_button_selector = """button[role="button"]:has-text("Open Audiobook")"""
                audiobook_tile_locator.locator(open_audiobook_button_selector).click()
                page.wait_for_load_state('networkidle')
                time.sleep(3)
                filename = f"15_after_open_audiobook_button_{selected_title.replace(' ', '_')}.png"
                screenshot_path = os.path.join(config['DOWNLOAD_DIRECTORY'], filename)
                page.screenshot(path=screenshot_path)

            except PlaywrightTimeoutError:
                print("Error: Audiobook titles did not appear in time on the shelf.")
                return
            except Exception as e:
                print(f"An error occurred while listing/selecting audiobooks: {e}")
                return

            print("Audiobook player opened. Rewinding to beginning...")
            time.sleep(5)
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
                    player_frame_init.locator('button[aria-label*="Next Chapter"]').first.wait_for(state='visible', timeout=60000)
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

            MAX_GAP_SKIP_CLICKS = 200  # ~50 minutes of audio (15s per click) to scan a chapter for a missed part
            GAP_SKIP_WAIT_SEC = 2      # Wait after each 15s skip for the part trigger to fire

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
                        player_frame = page.frame_locator('iframe[src*="listen.libbyapp.com"]')
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

                        skip_btn = player_frame.locator('button[aria-label*="Advance 15 seconds"], button.mini-player-jump-ahead')
                        skip_clicks = 0
                        # 15s-skip to fill the parts SKIPPED between expected and the part
                        # we overshot to (< landed). `landed` sits at a chapter boundary, so
                        # once the in-between parts are captured we stop and let the normal
                        # Next Chapter click reach it in the next iteration.
                        while next_expected_part() < landed and skip_clicks < MAX_GAP_SKIP_CLICKS:
                            try:
                                skip_btn.first.click(timeout=3000)
                            except (PlaywrightTimeoutError, Exception) as e:
                                print(f"  15s advance failed during gap recovery: {e}")
                                break
                            skip_clicks += 1
                            time.sleep(GAP_SKIP_WAIT_SEC)
                            if skip_clicks % 20 == 0:
                                print(f"  Gap recovery: {skip_clicks} skips so far, next still-missing part is {next_expected_part()} (filling up to {landed - 1})")
                        if next_expected_part() < landed:
                            print(f"  Gap recovery gave up after {skip_clicks} skips; Part {next_expected_part()} still missing (Step 4 will retry).")
                        else:
                            print(f"  Gap recovery done after {skip_clicks} 15s skips; parts up to {landed - 1} captured. Resuming chapter skips to reach Part {landed}.")

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
            print(f"Highest part number found: {max_part_number_found}")

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
            missing_parts = []
            for i in range(1, max_part_number_found + 1):
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
                if player_frame_obj and len(_signed_spine_urls) < max_part_number_found:
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
    run()
