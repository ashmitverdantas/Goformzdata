# Two small code changes required in `formsDataDownload.py`

Your script is 95% container-ready, but two things must be adjusted so it runs
headless inside the image. Make these edits before building.

---

## 1. Read GoFormz credentials from environment variables

The script calls `GOFORMZ_EMAIL` / `GOFORMZ_PASSWORD` in `login()` but never
defines them. Add this to the **CONFIG** section (near the Azure DI config):

```python
# --- GoFormz login (from environment / Container App secrets) ---
GOFORMZ_EMAIL    = os.getenv("GOFORMZ_EMAIL", "umella@verdantas.com")
GOFORMZ_PASSWORD = os.getenv("GOFORMZ_PASSWORD", "")
```

---

## 2. Point Selenium at the container's Chromium binary

In `build_driver()`, tell Chrome where the system Chromium lives (installed by
the Dockerfile). Replace the driver creation lines:

```python
    opts.page_load_strategy = "eager"

    # --- add these two lines ---
    chrome_bin = os.getenv("CHROME_BIN")
    if chrome_bin:
        opts.binary_location = chrome_bin
    # ---------------------------

    prefs = { ... }                       # unchanged
    opts.add_experimental_option("prefs", prefs)

    driver_path = os.getenv("CHROMEDRIVER_PATH")
    if driver_path:
        from selenium.webdriver.chrome.service import Service
        driver = webdriver.Chrome(service=Service(driver_path), options=opts)
    else:
        driver = webdriver.Chrome(options=opts)   # local fallback (Selenium Manager)

    driver.set_page_load_timeout(60)
    return driver
```

That's it — everything else in your script works unchanged inside the container.

---

## Note on the workbook (`goformz_dates.xlsx`)

Your script reads/writes a **local** `goformz_dates.xlsx`. Inside a container the
filesystem is ephemeral, so results are lost when the job ends. Pick one:

- **Simplest:** mount an Azure Files share to the job and keep the workbook there.
- **Recommended for you:** at the end of `main()`, upload the workbook to your
  **Fabric Lakehouse / ADLS** using `azure-storage-file-datalake`
  (already in `requirements.txt`). I can wire this in if you want.
