# Libby Audiobook Downloader Script

This Python script automates the process of logging into Libby (libbyapp.com), navigating to your bookshelf, selecting an audiobook, and downloading its audio parts. It uses Playwright for browser automation.

## Features

* Automated login to Libby.
* Dynamic selection of your library card usage option (stored for future runs).
* Lists audiobooks on your shelf for interactive selection.
* Downloads individual MP3 parts of the selected audiobook.
* Attempts to retrieve any missing parts by navigating backward in the player.
* Configuration details (library card, PIN, library name, download directory) are stored securely in a `libby_config.json` file after the first run.

## Prerequisites

Before running the script, ensure you have the following installed:

* **Python 3.8+**: Download from [python.org](https://www.python.org/downloads/).
* **`pip`**: Python's package installer (usually comes with Python).
* **`playwright`**: For browser automation.
* **`requests`**: For handling HTTP requests (though direct MP3 downloads are handled by Playwright's response).
* **`playwright-stealth`**: To help evade bot detection.
* **Chromium browser drivers**: Required by Playwright.

## Installation

1.  **Clone or download** the `libby_download.py` script to your local machine.

2.  **Open your terminal or command prompt** and navigate to the directory where you saved the script.

3.  **(Recommended) Create a Python virtual environment** to manage dependencies:

    ```bash
    python -m venv libby_env
    ```

4.  **Activate the virtual environment**:

    * **On Windows:**

        ```bash
        .\libby_env\Scripts\activate
        ```

    * **On macOS/Linux:**

        ```bash
        source libby_env/bin/activate
        ```

5.  **Install the required Python packages**:

    ```bash
    pip install playwright requests playwright-stealth
    ```

6.  **Install Playwright's browser drivers**:

    ```bash
    playwright install --with-deps
    ```

    This command will install Chromium and its necessary dependencies.

## Usage

1.  **Run the script from your activated virtual environment**:

    ```bash
    python libby_download.py
    ```

2.  **First Run Configuration**:
    * The first time you run the script, it will prompt you for your `LIBRARY_CARD_NUMBER`, `LIBBY_PASSWORD` (PIN), `LIBRARY` name (e.g., "Boston Public Library"), and `DOWNLOAD_DIRECTORY`.
    * It will also dynamically fetch and list options for "Where do you use your library card?" and ask you to select one by number.
    * This information will be saved to `libby_config.json` in the same directory as the script. **Do not share this file.**

3.  **Subsequent Runs**:
    * On subsequent runs, the script will load the saved configuration from `libby_config.json`.
    * It will then list the audiobooks currently on your Libby shelf and prompt you to enter the number corresponding to the audiobook you wish to download.

## Troubleshooting & Debugging

* **`HEADLESS_MODE = False`**: In the `libby_download.py` script, set `HEADLESS_MODE = False` at the top of the file. This will make Playwright launch a visible browser window, allowing you to observe the automation steps and identify where issues might occur.

* **Network Inspection**: While the browser is open (in non-headless mode), press `F12` to open Developer Tools and navigate to the "Network" tab. This can help you see which requests are failing (e.g., `403 Forbidden` responses) and compare them to successful manual browser interactions.

* **Libby Account Access**: If you encounter `403 Forbidden` errors even when manually interacting with the Playwright-launched browser, or if you cannot access Libby audiobooks on the web in any browser, the issue might be a server-side block on your account's web access. In such cases, you will need to contact Libby/OverDrive support directly to resolve the issue.

* **Selector Changes**: Websites can change their HTML structure. If the script stops working, it's possible that a button or input field's selector has changed. You might need to inspect the Libby website's elements using browser developer tools (F12) and update the corresponding selectors in the `libby_download.py` script.
