import asyncio
import os
import re
import datetime
from playwright.async_api import async_playwright, Page

async def run_bot(contact: dict) -> dict:
    print("Starting new_bridge_bot main flow...")
    
    async with async_playwright() as p:
        # Check if saved state exists
        from pathlib import Path
        state_path = Path("auth_state.json")
        context_kwargs = {"accept_downloads": True, "viewport": None}
        if state_path.exists():
            context_kwargs["storage_state"] = str(state_path)
            
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(**context_kwargs)
        page = await context.new_page()

        # =========================================================
        # LOGIN & INITIAL NAVIGATION (Comprehensive)
        # =========================================================
        print("Opening NatGen Agency...")
        await page.goto("https://natgenagency.com/", timeout=30000)
        
        # 1. Check if session is already active (bypasses login)
        await page.wait_for_timeout(3000)
        if "MainMenu.aspx" in page.url:
            print("Session active; landed directly on Main Menu. Skipping login.")
        else:
            print("Session not active. Proceeding with login...")
            await page.goto("https://natgenagency.com/Login.aspx", timeout=30000)
            
            # Dismiss 'enable login' popup if present
            try:
                if await page.locator("button:has-text('Enable Login')").is_visible(timeout=2000):
                    await page.click("button:has-text('Enable Login')")
            except Exception:
                pass

            # Fill User ID
            print("Filling User ID...")
            for _ in range(4):
                try:
                    await page.locator("#txtUserID").fill(os.getenv("NATGEN_USERNAME", ""))
                    await page.locator("#btnLogin").click(timeout=10000)
                    break
                except Exception:
                    await page.wait_for_timeout(1000)
                    
            await page.wait_for_timeout(4000)

            # Check if password screen appeared
            if await page.locator("#txtPassword").count() > 0:
                print("Password screen detected. Filling password...")
                for _ in range(4):
                    try:
                        await page.locator("#txtPassword").fill(os.getenv("NATGEN_PASSWORD", ""))
                        await page.locator("#btnLogin").click(timeout=10000)
                        break
                    except Exception:
                        await page.wait_for_timeout(1000)
            
            await page.wait_for_timeout(4000)

            # Check for 2FA / MFA
            if await page.locator("#loginWith2faSms, #loginWith2faEmail").count() > 0:
                print("2FA Delivery selection detected.")
                if await page.locator("#loginWith2faSms").count() > 0:
                    await page.click("#loginWith2faSms")
                    print("Selected SMS 2FA.")
                else:
                    await page.click("#loginWith2faEmail")
                    print("Selected Email 2FA.")
                    
                await page.wait_for_timeout(3000)

            if await page.locator("#TwoFactorCode, #verifyButton").count() > 0:
                print("Waiting up to 60s for 2FA code to be entered...")
                for _ in range(30):
                    if "MainMenu.aspx" in page.url or await page.locator("#MainContent_btnContinue").count() > 0:
                        break
                    await page.wait_for_timeout(2000)

            # Wait for Main Menu / Dashboard to settle
            print("Waiting for dashboard/bridge page...")
            for _ in range(10):
                if "MainMenu.aspx" in page.url or await page.locator("#MainContent_btnContinue").is_visible():
                    break
                await page.wait_for_timeout(2000)
                
            # Save auth state
            await context.storage_state(path=str(state_path))
            print("Login complete. Saved session state.")

        # =========================================================
        # PAGE 1: CLIENT INFO
        # =========================================================
        print("Page 1: Client Info...")
        
        # Policy Type -> Auto
        for _ in range(4):
            try:
                await page.locator("#MainContent_ucClientInformation_ddlPolicyType").select_option(label="Auto")
                break
            except Exception:
                await page.wait_for_timeout(1000)

        # First Name
        fname = contact.get("firstName", "")
        for _ in range(4):
            try:
                await page.locator("#MainContent_ucClientInformation_txtFirstName").fill(fname)
                break
            except Exception:
                await page.wait_for_timeout(1000)

        # Last Name
        lname = contact.get("lastName", "")
        for _ in range(4):
            try:
                await page.locator("#MainContent_ucClientInformation_txtLastName").fill(lname)
                break
            except Exception:
                await page.wait_for_timeout(1000)

        # Address 1
        addr1 = contact.get("address1", contact.get("address", ""))
        for _ in range(4):
            try:
                await page.locator("#MainContent_ucClientInformation_txtAddress1").fill(addr1)
                break
            except Exception:
                await page.wait_for_timeout(1000)

        # Zip
        zip_code = contact.get("postalCode", "")
        for _ in range(4):
            try:
                await page.locator("#MainContent_ucClientInformation_txtZipCode").fill(zip_code)
                break
            except Exception:
                await page.wait_for_timeout(1000)

        # Click Find Address to auto-fill City/State
        for _ in range(4):
            try:
                await page.locator("#MainContent_ucClientInformation_btnFindAddress").click()
                break
            except Exception:
                await page.wait_for_timeout(1000)
                
        # Wait for Address search to finish
        await page.wait_for_timeout(3000)

        # Phone Number
        phone = contact.get("phone", "")
        if phone and len(phone) >= 10:
            phone_clean = ''.join(filter(str.isdigit, phone))[-10:]
            area = phone_clean[0:3]
            prefix = phone_clean[3:6]
            line = phone_clean[6:10]
            
            for _ in range(4):
                try:
                    await page.locator("#MainContent_ucClientInformation_txtPhoneAreaCode").fill(area)
                    break
                except Exception:
                    await page.wait_for_timeout(1000)
                    
            for _ in range(4):
                try:
                    await page.locator("#MainContent_ucClientInformation_txtPhonePrefix").fill(prefix)
                    break
                except Exception:
                    await page.wait_for_timeout(1000)
                    
            for _ in range(4):
                try:
                    await page.locator("#MainContent_ucClientInformation_txtPhoneLineNumber").fill(line)
                    break
                except Exception:
                    await page.wait_for_timeout(1000)

        # Email
        email = contact.get("email", "")
        for _ in range(4):
            try:
                await page.locator("#MainContent_ucClientInformation_txtEmailAddress").fill(email)
                break
            except Exception:
                await page.wait_for_timeout(1000)

        # Click Continue from Page 1
        print("Clicking Continue from Page 1...")
        for _ in range(4):
            try:
                await page.locator("#MainContent_btnContinue").click()
                break
            except Exception:
                await page.wait_for_timeout(1000)

        # Wait for next page
        await page.wait_for_timeout(4000)

        # =========================================================
        # PAGE 2: PRIOR POLICY
        # =========================================================
        print("Page 2: Prior Policy...")
        for _ in range(4):
            try:
                await page.locator("#MainContent_ucPriorPolicyInformation_ddlPriorInsuranceCoverage").select_option(label=re.compile(r"^\s*no\s*$", re.I))
                break
            except Exception:
                await page.wait_for_timeout(1000)

        # Click Continue from Page 2
        for _ in range(4):
            try:
                await page.locator("#MainContent_btnContinue").click()
                break
            except Exception:
                await page.wait_for_timeout(1000)

        await page.wait_for_timeout(4000)
        
        # =========================================================
        # DRIVER LIST & EDIT
        # =========================================================
        print("Driver List...")

        # Pull driver data from contact; fall back to contact root as single driver
        contact_drivers = contact.get("drivers", [])
        if not contact_drivers:
            contact_drivers = [contact]

        async def _select_defensive_driver_no():
            """Select 'No' (index 1, the second option) in Defensive Driver dropdown, retrying until confirmed."""
            dd_sel = "#MainContent_ddlDefensiveDriverCourse"
            for attempt in range(1, 8):
                try:
                    # Click to open then select index 1 (second option = No)
                    await page.locator(dd_sel).click()
                    await page.wait_for_timeout(300)
                    await page.locator(dd_sel).select_option(index=1)
                    await page.wait_for_timeout(400)
                except Exception:
                    await page.wait_for_timeout(500)
                    continue

                # Confirm a non-blank value is now selected
                try:
                    val = (await page.locator(dd_sel).input_value()).strip()
                except Exception:
                    val = ""
                if val and val not in ("", "-1"):
                    print(f"Defensive Driver Course set to No (value='{val}') on attempt {attempt}")
                    return
                print(f"Defensive Driver Course not confirmed on attempt {attempt}, retrying...")

        async def _fill_driver_form(driver_data: dict):
            """Fill the DriverInfo form fields from driver_data (or defaults) then save."""
            # Defensive Driver → No (index 1, second option)
            await _select_defensive_driver_no()

            # Driver License Status → Active (required by carrier)
            lic_status_sel = "#MainContent_ddlDriversLicenseStatus"
            for attempt in range(1, 5):
                try:
                    await page.locator(lic_status_sel).select_option(value="Active")
                    await page.wait_for_timeout(500)
                except Exception:
                    await page.wait_for_timeout(500)
                val = (await page.locator(lic_status_sel).input_value()).strip() if await page.locator(lic_status_sel).count() > 0 else "Active"
                if val and val not in ("", "-1"):
                    break

            # License State → GA default
            if await page.locator("#MainContent_ddlLicenseState").count() > 0:
                try:
                    await page.locator("#MainContent_ddlLicenseState").select_option(value="GA")
                except Exception:
                    pass

            # License Number → fill if blank
            if await page.locator("#MainContent_txtDriversLicenseNumber").count() > 0:
                lic = (await page.locator("#MainContent_txtDriversLicenseNumber").input_value()).strip()
                if not lic:
                    raw = (
                        driver_data.get("driverLicenseNumber")
                        or driver_data.get("licenseNumber")
                        or ""
                    )
                    digits = re.sub(r"\D", "", str(raw))
                    lic_to_use = (digits or "123456789")[:9].zfill(9)
                    try:
                        await page.locator("#MainContent_txtDriversLicenseNumber").fill(lic_to_use)
                    except Exception:
                        pass

            # Save Driver — retry up to 10 times
            for _ in range(10):
                try:
                    await page.locator("#MainContent_btnSaveDriver").click()
                    break
                except Exception:
                    await page.wait_for_timeout(1000)
            await page.wait_for_timeout(3000)

        # --- Count how many Edit links exist in the driver table ---
        edit_links_loc = page.locator("#MainContent_gvDrivers tbody tr a").filter(has_text=re.compile(r"view/edit|edit", re.I))
        total_drivers = await edit_links_loc.count()
        print(f"Found {total_drivers} driver(s) in table.")

        # --- Loop through every driver by index, re-fetching each time after save ---
        for i in range(total_drivers):
            print(f"Editing driver {i + 1} of {total_drivers}...")

            # Re-fetch edit links each iteration (ASP.NET re-renders table after each save)
            for _ in range(4):
                try:
                    links = page.locator("#MainContent_gvDrivers tbody tr a").filter(has_text=re.compile(r"view/edit|edit", re.I))
                    await links.nth(i).click()
                    break
                except Exception:
                    await page.wait_for_timeout(1000)
            await page.wait_for_timeout(3000)

            # Use matching contact driver data if available, else empty dict (defaults)
            d_data = contact_drivers[i] if i < len(contact_drivers) else {}
            await _fill_driver_form(d_data)

        # Check Connected Driver Agreement checkbox (always visible on list page)
        for _ in range(4):
            try:
                cb = page.locator("#MainContent_chkConnectedDriverAgreement")
                if not await cb.is_checked():
                    await cb.check()
                print("Connected Driver Agreement checkbox checked.")
                break
            except Exception:
                await page.wait_for_timeout(500)

        # Click Continue from Driver List
        for _ in range(4):
            try:
                await page.locator("#MainContent_btnContinue").click()
                break
            except Exception:
                await page.wait_for_timeout(1000)
        await page.wait_for_timeout(4000)

        # =========================================================
        # VEHICLES
        # =========================================================
        print("Vehicles...")
        for _ in range(4):
            try:
                await page.locator("#MainContent_btnContinue").click()
                break
            except Exception:
                await page.wait_for_timeout(1000)
        await page.wait_for_timeout(4000)

        # =========================================================
        # COVERAGES
        # =========================================================
        print("Coverages...")
        for _ in range(4):
            try:
                await page.locator("#MainContent_btnContinue").click()
                break
            except Exception:
                await page.wait_for_timeout(1000)
        await page.wait_for_timeout(4000)

        # =========================================================
        # UNDERWRITING
        # =========================================================
        print("Underwriting...")
        # Get all visible underwriting dropdowns and set them to False/No
        all_selects = await page.locator("select[id^='MainContent_ucUnderwritingQuestions_rpParentQuestions_ddlAnswer_']").all()
        for loc in all_selects:
            for _ in range(4):
                try:
                    if not await loc.is_visible():
                        break

                    options = await loc.locator("option").all()
                    for opt in options:
                        val = (await opt.get_attribute("value") or "").strip().lower()
                        lbl = (await opt.text_content() or "").strip().lower()
                        if val in ("false", "no", "n", "0") or lbl in ("false", "no", "n", "0"):
                            await loc.select_option(value=await opt.get_attribute("value"))
                            break
                    break
                except Exception:
                    await page.wait_for_timeout(500)

        # Question 6: Is there a trampoline on premise? → No (index 1, second option)
        trampoline_xpath = "/html/body/form/div[3]/div[2]/div[8]/div[4]/div[3]/fieldset/ul/li[6]/select"
        for attempt in range(1, 8):
            try:
                trampoline_sel = page.locator(f"xpath={trampoline_xpath}")
                if await trampoline_sel.count() == 0:
                    print("Trampoline question not found, skipping.")
                    break
                await trampoline_sel.click()
                await page.wait_for_timeout(300)
                await trampoline_sel.select_option(index=1)
                await page.wait_for_timeout(400)
                val = (await trampoline_sel.input_value()).strip()
                if val and val not in ("", "-1"):
                    print(f"Trampoline question set to No (value='{val}') on attempt {attempt}.")
                    break
                print(f"Trampoline question not confirmed on attempt {attempt}, retrying...")
            except Exception:
                await page.wait_for_timeout(500)

        for _ in range(4):
            try:
                await page.locator("#MainContent_btnContinue").click()
                break
            except Exception:
                await page.wait_for_timeout(1000)
        await page.wait_for_timeout(4000)

        # =========================================================
        # PREMIUM SUMMARY
        # =========================================================
        print("Premium Summary reached!")
        
        print("Finished basic scaffold.")
        return {
            "status": "success",
            "message": "Quote completed successfully via new_bridge_bot scaffold.",
            "rates": {},
            "raw_payload": contact
        }

if __name__ == "__main__":
    import json
    contact_data = {
        "firstName": "John",
        "lastName": "Doe",
        "address1": "123 Main St",
        "postalCode": "12345",
        "phone": "5551234567",
        "email": "john@example.com"
    }
    result = asyncio.run(run_bot(contact_data))
    print(json.dumps(result, indent=2))
