import os
import re
import time
import requests
import json # Import the json library for config file handling
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from playwright_stealth import Stealth # Import the stealth library

# --- Configuration File Path ---
CONFIG_FILE = 'libby_config.json'

# --- Global Variables for Tracking Download Progress ---
downloaded_parts = set()
max_part_number_found = 0
part_number = 00
# Note: In a single-threaded sync_playwright script, this might not be strictly necessary
# for these simple variables, but it's good practice for shared state in async contexts
# or if more complex threading was introduced. For this script, we'll rely on the
# sequential nature of request handling within the Playwright event loop.

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
                config[field] = input(f"Please enter your {field.replace('_', ' ')} (e.g., Boston Public Library): ")
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

# --- Network Request Handler ---
def handle_request(request):
    """
    Callback function to process intercepted network requests.
    Identifies and downloads audiobook MP3 parts directly using Playwright's response.
    """
    global downloaded_parts, max_part_number_found, part_number

    url = request.url

    # if this is the initial request URL, extract the part number.
    # This URL contains the "PartXX.mp3" string before any redirects.
    part_match = re.search(r"Part(\d+).mp3", url, re.IGNORECASE)
    
    if part_match:
        part_number = int(part_match.group(1))
        return

    # Get the response object for the intercepted request.
    response = request.response()

    if not response:
        print(f"No response object for request (Part {part_number}): {request.url}")
        return

    # Otherwise, check if the FINAL response URL is from the CDN.
    # This confirms it's the actual audio content.
    if "audioclips.cdn.overdrive.com" in url:
        if part_number not in downloaded_parts:
            print(f"Detected new part from CDN: {url} (Part {part_number}) [Original Request: {request.url}]")
            file_name = f"Part_{part_number:02d}.mp3" # e.g., Part_01.mp3, Part_10.mp3
            file_path = os.path.join(config['DOWNLOAD_DIRECTORY'], file_name)

            try:
                print(f"  Response Status: {response.status}")
                print(f"  Response Headers: {response.headers}")
                
                content = response.body()
                content_length = len(content)
                print(f"  Response Body Size: {content_length} bytes")
                if response.status == 200 and content_length > 0:
                    with open(file_path, "wb") as f:
                        f.write(content)

                    print(f"Successfully downloaded {file_name} ({content_length} bytes)")
                    downloaded_parts.add(part_number)
                    max_part_number_found = max(max_part_number_found, part_number)
                else:
                    print(f"Failed to download {file_name}. Status: {response.status}, Body Size: {content_length} bytes.")
                    if response.status == 403:
                        print("  (403 Forbidden: Access denied. This might indicate an issue with session or token.)")
                    elif content_length == 0:
                        print("  (Empty response body received. This is unexpected for an actual audio part.)")
            except Exception as e:
                print(f"An unexpected error occurred during download of {file_name} from {url}: {e}")
        else:
            # print(f"Part {part_number} already downloaded, skipping.") # Uncomment for verbose logging
            pass
    # else:
        # print(f"Skipping non-CDN audio part request (Part {part_number}): {url}") # Uncomment for verbose logging


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

            # Path to your Chrome executable.
            # When using channel="chrome", Playwright will attempt to find your installed Google Chrome.
            # You typically don't need to specify executable_path when using 'channel'.
            # Keeping it commented out for clarity.
            # CHROME_EXECUTABLE_PATH = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'
            # If CHROME_EXECUTABLE_PATH was defined and uncommented, it would be used here.
            # However, for 'channel' to work effectively, it's often best not to specify
            # executable_path unless absolutely necessary for a custom build.
            # Playwright will attempt to find the 'chrome' channel installation.
            # if 'CHROME_EXECUTABLE_PATH' in globals() and CHROME_EXECUTABLE_PATH:
            #     print(f"Note: CHROME_EXECUTABLE_PATH is set but 'channel' is also used. "
            #           f"Playwright will prioritize 'channel' to find the branded Chrome.")
            #     launch_args["executable_path"] = CHROME_EXECUTABLE_PATH


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
                for i, button in enumerate(library_choice_buttons):
                    # Extract text, stripping whitespace and filtering out empty strings
                    text = button.text_content().strip()
                    if text: # Only add non-empty text
                        options_text.append(text)

                if not options_text:
                    print("No library card usage options found on the page.")
                    return

                # If the option index is not in config or invalid, prompt the user
                if 'LIBRARY_CARD_USAGE_OPTION_INDEX' not in config or \
                   not (0 <= config['LIBRARY_CARD_USAGE_OPTION_INDEX'] < len(options_text)):
                    print("Please select your library card usage option:")
                    for i, text in enumerate(options_text):
                        print(f"{i+1}. {text}")
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
                selected_option_text = options_text[config['LIBRARY_CARD_USAGE_OPTION_INDEX']]
                # Using triple double quotes for robustness in f-string
                page.click(f"""button:has-text("{selected_option_text}") >> nth={config['LIBRARY_CARD_USAGE_OPTION_INDEX']}""")
                page.wait_for_load_state('networkidle')
                time.sleep(3)
                # Fix: Separated f-string for filename from os.path.join
                filename = f"07_after_select_card_usage_{config['LIBRARY_CARD_USAGE_OPTION_INDEX']+1}.png"
                screenshot_path = os.path.join(config['DOWNLOAD_DIRECTORY'], filename)
                page.screenshot(path=screenshot_path)

            except PlaywrightTimeoutError:
                print("Error: Library card usage options did not appear in time.")
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
            print(f"Entering PIN: '{config['LIBBY_PASSWORD']}' into PIN field...")
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


            # Add a generic wait after login, adjust as needed
            page.wait_for_load_state('networkidle', timeout=60000) # Give more time for login redirect
            print("Login attempt complete. Checking if logged in...")
            screenshot_path = os.path.join(config['DOWNLOAD_DIRECTORY'], "13_after_login_complete.png")
            page.screenshot(path=screenshot_path)

            # You might want to add a check here to ensure login was successful,
            # e.g., by looking for an element that only appears after login.
            # For now, we'll proceed assuming success.

            # --- Step 2: Navigate to Audiobook ---
            # Removed hardcoded AUDIOBOOK_TITLE reference
            print("Navigating to audiobook section...")

            # First, navigate to the "Shelf" or "Loans" section.
            # Using the provided HTML, the button has id="footer-nav-shelf"
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
                for i, tile in enumerate(audiobook_tiles):
                    # Extract the title text from within the tile
                    title_element = tile.locator('.title-tile-title').first
                    if title_element:
                        title_text = title_element.text_content().strip().replace('&nbsp;', ' ')
                        audiobook_titles.append(title_text)

                # Debug print: Show what titles were parsed
                print(f"DEBUG: Parsed Audiobook Titles: {audiobook_titles}")

                if not audiobook_titles:
                    print("No audiobooks found on your shelf.")
                    return

                # Prompt user for which book on their shelf they want to download.
                # Print numbered list for user selection
                for i, title in enumerate(audiobook_titles):
                    print(f"{i+1}. {title}")

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

                # Locate the specific audiobook tile using the selected title
                # Using triple double quotes for robustness in f-string
                audiobook_tile_locator = page.locator(f"""div.title-tile:has-text("{selected_title}")""").first
                # Click the "Open Audiobook" button within that tile
                # Using triple double quotes for robustness
                open_audiobook_button_selector = """button[role="button"]:has-text("Open Audiobook")"""
                audiobook_tile_locator.locator(open_audiobook_button_selector).click()
                page.wait_for_load_state('networkidle')
                time.sleep(3)
                # Fix: Separated f-string for filename from os.path.join
                filename = f"15_after_open_audiobook_button_{selected_title.replace(' ', '_')}.png"
                screenshot_path = os.path.join(config['DOWNLOAD_DIRECTORY'], filename)
                page.screenshot(path=screenshot_path)

            except PlaywrightTimeoutError:
                print("Error: Audiobook titles did not appear in time on the shelf.")
                return
            except Exception as e:
                print(f"An error occurred while listing/selecting audiobooks: {e}")
                return

            # Removed the explicit "Click Listen or Play button" step as the user confirmed
            # the first part loads automatically after navigating to the audiobook detail page.
            print("Audiobook player opened. Starting part discovery...")
            time.sleep(5) # Give player time to load initial parts and for network requests to fire
            screenshot_path = os.path.join(config['DOWNLOAD_DIRECTORY'], "16_after_audiobook_detail_load.png")
            page.screenshot(path=screenshot_path)


            # --- Step 3: Player Control and Forward Part Discovery ---
            # Continuously click "Next Chapter" or "Skip Forward" to trigger new part loads.
            initial_parts_count = len(downloaded_parts)
            no_new_parts_count = 0
            MAX_NO_NEW_PARTS_ITERATIONS = 10 # Stop if no new parts found for this many clicks
            MAX_FORWARD_CLICKS = 500 # Safety limit for forward clicks

            # Selector for the "Next Chapter" button
            NEXT_CHAPTER_SELECTOR = 'button.chapter-bar-next-button'

            for i in range(MAX_FORWARD_CLICKS):
                current_parts_count = len(downloaded_parts)
                print(f"Forward pass iteration {i+1}. Current parts downloaded: {current_parts_count}")

                try:
                    # Wait for the next chapter button to be visible and enabled
                    page.wait_for_selector(NEXT_CHAPTER_SELECTOR, timeout=5000)
                    page.click(NEXT_CHAPTER_SELECTOR)
                    time.sleep(30) # Give time for network requests to fire and new parts to be detected
                except PlaywrightTimeoutError:
                    print("No 'Next Chapter' button found or end of audiobook reached in forward pass.")
                    break # Exit loop if button is not found (likely end of book)
                except Exception as e:
                    print(f"Error clicking 'Next Chapter' button: {e}")
                    break # Exit loop on unexpected error

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

            # --- Step 4: Handling Missing Parts (Backward Seeking) ---
            print("Checking for any missing parts and attempting to retrieve them...")
            missing_parts = []
            for i in range(1, max_part_number_found + 1):
                if i not in downloaded_parts:
                    missing_parts.append(i)

            if not missing_parts:
                print("No missing parts detected. All parts downloaded successfully!")
            else:
                print(f"Missing parts identified: {sorted(missing_parts)}")
                # This is the most challenging part due to dynamic player behavior.
                # The strategy here is to attempt to go back to a chapter that *might*
                # contain the missing part and let it play briefly.
                # This often involves repeatedly clicking "Previous Chapter".

                # You MUST inspect the "Previous Chapter" button's selector.
                # Using triple double quotes for robustness
                PREV_CHAPTER_SELECTOR = """button[aria-label*="Previous chapter"]"""
                # --- END PLACEHOLDER ---

                for missing_part in sorted(missing_parts):
                    print(f"Attempting to retrieve missing Part {missing_part}...")
                    retries = 3
                    for attempt in range(retries):
                        try:
                            # A heuristic: Go back a few chapters to try and trigger the part.
                            # This might need adjustment based on how Libby loads parts.
                            # For example, if Part 5 is missing, go back to a chapter that
                            # would contain Part 4 or 3.
                            for _ in range(2): # Click 'previous chapter' a couple of times
                                try:
                                    page.click(PREV_CHAPTER_SELECTOR, timeout=2000)
                                    time.sleep(1)
                                except PlaywrightTimeoutError:
                                    print("Reached beginning of audiobook while seeking backwards.")
                                    break # Can't go back further

                            print(f"Attempt {attempt + 1} to trigger Part {missing_part} download...")
                            # Now, play forward a bit or just wait, hoping the missing part loads
                            # as the player re-buffers.
                            time.sleep(5) # Give ample time for network requests

                            if missing_part in downloaded_parts:
                                print(f"Successfully retrieved missing Part {missing_part}!")
                                break # Move to the next missing part
                            else:
                                print(f"Part {missing_part} not found after attempt {attempt + 1}.")
                        except Exception as e:
                            print(f"Error during backward seeking for Part {missing_part}: {e}")
                    if missing_part not in downloaded_parts:
                        print(f"Failed to retrieve Part {missing_part} after {retries} attempts.")

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
    # Before running:
    # 1. Install Playwright and requests:
    #    pip install playwright requests
    # 2. Install playwright-stealth:
    #    pip install playwright-stealth
    # 3. Ensure Playwright's browser drivers are installed and up-to-date:
    #    playwright install --with-deps
    # 4. The script will now prompt you for your LIBRARY_CARD_NUMBER, LIBBY_PASSWORD,
    #    LIBRARY, DOWNLOAD_DIRECTORY, and LIBRARY_CARD_USAGE_OPTION_INDEX the first time
    #    it runs, and save them to 'libby_config.json'.
    # 5. IMPORTANT: Run the script initially with HEADLESS_MODE = False.
    #    Observe the browser actions and use your browser's developer tools (F12)
    #    to find the correct CSS selectors or XPath expressions if any issues arise.
    # 6. The script will continue to prompt you for the AUDIOBOOK_TITLE to download
    #    each time it is run.

    run()
