TRUSTWELL INSURANCE  |  NG360 Bot Contractor Brief  |  CONFIDENTIAL

# TRUSTWELL INSURANCE AGENCY

## NG360 Bot

National General PKGProtect2 Quote Automation

Contractor Project Brief

| **STATUS: NEW BUILD** | **PRIORITY: HIGH** |
| --- | --- |

Trustwell Insurance Agency  |  Confidential  |  March 30, 2026

# 1. Engagement Overview

This document defines the complete build of the NG360 Bot — a production automation system that generates bundled homeowners + auto insurance quotes (PKGProtect2 product) on the National General carrier portal for Georgia customers of Trustwell Insurance Agency. This is a full build engagement. Every section in this brief represents a required deliverable. The engagement is not considered complete until every section — architecture, portal flow, GHL integration, error handling, environment configuration, and all acceptance criteria — has been fully implemented, tested, and operational in production.

**STARTING POINT: The contractor should use the existing HOA Bot codebase as a shell. The HOA Bot is a production-proven system with the same four-layer architecture (FastAPI webhook server, queue manager, Playwright bridge bot, integration services), the same GHL integration pattern, the same Google Drive upload logic, the same Slack notification layer, and the same watchdog service. The ONLY thing that changes is the carrier portal automation — instead of the HOA portal, the NG360 Bot targets the National General portal (natgenagency.com) running the PKGProtect2 bundled home + auto product. Clone the HOA Bot, swap the portal automation layer, update the GHL custom fields and port assignment, and adapt for the bundled product flow. Do not rebuild the infrastructure from scratch.**

**SCOPE: Even though the HOA Bot is the starting shell, this is still a COMPLETE BUILD of the NG360 Bot. The contractor is responsible for delivering a fully functional, production-ready bot that meets every specification in this document. Partial delivery is not acceptable. Every section below is a mandatory build requirement, not a reference or suggestion.**

| **Field** | **Detail** |
| --- | --- |
| Document Date | March 30, 2026 |
| System | NG360 Bot — National General PKGProtect2 Bundled Quote Automation |
| Engagement Type | COMPLETE BUILD — Using HOA Bot as shell; only the portal automation layer changes |
| Starting Shell | HOA Bot (port 8002, SC/TX) — clone and adapt for National General portal |
| Owner | Trustwell Insurance Agency — Desmond Thomas (CEO) |
| Target Machine | Mac Mini M4 or Mac Studio (macOS) |
| Portal URL | https://natgenagency.com/ (login) → https://ho.natgenagency.com/ (quote flow) |
| Product | PKGProtect2 — Bundled Home + Auto Package |
| State Coverage | Georgia (GA) — expand to additional states as appointments are secured |
| Source Material | Chrome Recorder JSON (NG360_BOT.json, 249 steps) — reference recording of a manual quote session |
| Production Status | NEW BUILD — Bot must be built, tested, and deployed. HOA Bot shell accelerates but does not eliminate the work. |
| Confidentiality | Strictly confidential. Do not distribute. |

## 1.1  What Carries Over from the HOA Bot (Do Not Rebuild)

The following components are production-proven in the HOA Bot and should be cloned directly into the NG360 Bot with minimal modification. The contractor should not rewrite these from scratch.

| **Component** | **What to Change** |
| --- | --- |
| FastAPI Webhook Server | Change port from 8002 to 8004. Update state validation from SC/TX to GA. Update field checks from fire_price to ng_price. |
| Queue Manager | Update queue persistence filename from hoa_queue.json to ng360_queue.json. No structural changes. |
| Google Drive Uploader | Update parent folder ID to a new NG360 quotes folder. No structural changes. |
| GHL Integration Service | Update custom field names (fire_* → ng_*). Update tag prefixes (hoa-quote-* → ng-quote-*). Same API pattern. |
| Slack Notification Service | Update message templates to reference NG360/National General. Same channels and webhook pattern. |
| Watchdog Service | Increase timeout from 480s to 600s (longer flow). No structural changes. |
| PostgreSQL Logging | Add ng360_jobs table mirroring hoa_jobs schema. Same connection pattern. |

## 1.2  What Must Be Built New (The Actual Work)

The core new development is the Playwright portal automation — the bridge bot that navigates the National General PKGProtect2 quote flow. This is a significantly larger automation than the HOA Bot because it handles a bundled home + auto product in a single session across 14 portal pages with 73 form fields. The contractor receives a Chrome Recorder JSON file (NG360_BOT.json, 249 steps) that documents every click, field entry, and navigation. This JSON is a reference recording only — it is not executable code. The contractor must translate the recorded steps into a robust Playwright automation with proper error handling, retry logic, and dynamic data injection from GHL contact records.

**CREDENTIAL SECURITY: The Chrome Recorder JSON contains a recorded password. This password must be externalized to environment variables in the production build. Never hardcode credentials.**

# 2. System Architecture

## 2.1  Four-Layer Architecture

The NG360 Bot follows the same four-layer architecture established by the NS Bot and HOA Bot in the Trustwell infrastructure. Each layer operates independently with clear boundaries.

| **Layer** | **Responsibility** |
| --- | --- |
| GHL Webhook Server (FastAPI) | Receives contact-updated webhooks from GHL. Validates state (GA only for initial launch). Checks ng_price and not_eligible fields to prevent duplicate or ineligible processing. Assigns queue priority by tag. Returns HTTP 200 immediately — never blocks GHL. |
| Queue Manager (FIFO, Priority Levels) | Manages job lifecycle from PENDING through PROCESSING to COMPLETED or FAILED. Handles retries (3 max, 5-second delay). Persists state to quote_queue.json so the queue survives crashes. On restart, any PROCESSING jobs auto-reset to PENDING. Runs one job at a time. |
| NG360 Bridge Bot (Playwright) | The main automation engine. Launches a visible Chrome browser. Executes the full PKGProtect2 quote flow against the National General portal — from login through premium summary and PDF capture. Captures screenshots per step for debugging. Returns a result dict to the queue manager. |
| Integration Services | Google Drive Uploader creates customer-named folders, uploads quote PDFs, and returns shareable URLs. GHL Integration Service updates ng_price, ng_quote_status, ng_quote_url, and ng_quote_date on the contact. Slack notifies #insurance-quotes on success and #bot-alerts on failure. |

## 2.2  Technology Stack

| **Component** | **Technology** |
| --- | --- |
| Language | Python 3.11+ |
| Browser Automation | Playwright (Chromium, headed mode for debugging) |
| Web Framework | FastAPI (webhook receiver) |
| Queue | asyncio.PriorityQueue with JSON persistence |
| Database | PostgreSQL on Mac Studio (primary), replica on Mac Mini |
| File Storage | Google Drive API (customer folders, PDF uploads) |
| CRM Integration | GHL API v2 (contact updates, custom fields) |
| Notifications | Slack (success/failure channels), Pushover (mobile alerts) |
| Networking | Tailscale mesh (inter-machine communication) |
| Source Spec | Chrome Recorder JSON (NG360_BOT.json, 249 steps) |

## 2.3  Watchdog Service

A watchdog coroutine runs every 60 seconds. Any job that remains in PROCESSING status for more than 600 seconds (10 minutes — longer than other bots due to the combined home+auto flow) is rescued: the job is re-queued and retry_count is incremented. After 3 total retries the job is marked FAILED, the contact is tagged in GHL, and a critical alert is sent via Slack and Pushover.

# 3. Portal Flow — Page-by-Page Breakdown

The National General PKGProtect2 portal flow navigates through 14 distinct pages. The contractor must convert the raw Chrome Recorder JSON into a robust Playwright automation with proper waits, error handling, and screenshot capture at each stage. The following table documents every page in sequence.

| **#** | **Portal Page** | **URL Path** | **Fields** | **Key Actions** |
| --- | --- | --- | --- | --- |
| 1 | Landing / Sign In | natgenagency.com | 2 | Navigate to portal, click SIGN IN, enter credentials (from env vars), submit login |
| 2 | Main Menu | natgenagency.com/MainMenu.aspx | 2 | Select State (GA), select Product (PKGProtect2), click Begin |
| 3 | Client Search | ho.../ContentPages/ClientSearch | 3 | Enter First Name, Last Name, Zip Code from GHL contact data, click Search, then Add New Customer |
| 4 | Client Info (Page 1) | ho.../ContentPages/ClientInfo | 14 | DOB, Gender, Marital Status, Occupation, Phone (type + 3 fields), Address, City, Years at Residence, Email Option, Email Address, Confirm Email |
| 5 | Client Info (Page 2) | ho.../ContentPages/ClientInfo | 1 | Input By (Agent ID: 20050264) |
| 6 | Prefill Verification | ho.../ContentPages/Prefill | 4 | Review prefilled residents. Accept or Reject each. Rejection requires Reason + Comment. Check license boxes. |
| 7 | Property Info | ho.../ContentPages/PropertyInfo | 10 | Residence Class, Occupancy, Named Insured Type, Effective Date (calendar picker), Roof Type, Primary Heat Type, Roof Shape, Hail Resistant, Year of Roof Replacement, Oil Tank |
| 8 | Underwriting (Home) | ho.../ContentPages/Underwriting | 5 | Prior Insurance status, Prior Carrier, Expiration Date, Years of Continuous Property Insurance |
| 9 | Loss History | ho.../ContentPages/LossHistory | 0 | Review-only page. Click Next if no losses to report. |
| 10 | Coverage Info | ho.../ContentPages/CoverageInfoD | 3 | All Perils Deductible, Named Storm Deductible, Windstorm/Hail Deductible |
| 11 | Driver Info | ho.../ContentPages/DriverInfo | 7 | Losses in past 5 years, Defensive Driver Course, Program Participant, Dynamic Drive Program checkbox, Driver License Status. Must handle Terms & Conditions checkbox. |
| 12 | Driver Violations | ho.../ContentPages/DriverViolations | 0 | Review-only page. Click Next. |
| 13 | Vehicle Info (per vehicle) | ho.../ContentPages/VehicleInfo | 12+ | For EACH vehicle: Rented/Leased, Weight check, Camper Unit, Ownership Status, Annual Mileage, Purchased Date. Then vehicle coverages: Comprehensive, Roadside, Customizing Equipment, Custom Audio, Transportation Expense. Save each vehicle individually. |
| 14 | Auto Underwriting | ho.../ContentPages/AutoUnderwriting | 5 | Named Insured Type, Company car, Losses in 5 years, Years with Prior Auto Carrier. Click Next. |
| 15 | Premium Summary | ho.../ContentPages/PremiumSummary | 2 | Select Pay Method (AS = Agency Sweep), select Pay Plan, click Re-Rate, then Quote Proposal to generate PDF. |

**MULTI-VEHICLE HANDLING: Steps 180–231 in the JSON show the bot processing TWO vehicles. The production bot must dynamically handle 1–6 vehicles per quote. Each vehicle requires its own edit/save/coverage cycle. Vehicle count comes from GHL contact data.**

**DYNAMIC DRIVE PROGRAM: The portal requires accepting Terms & Conditions via a checkbox before proceeding past Driver Info. This is an interactive modal — the bot must click the T&C link, then check the agreement box. Failure to handle this blocks the entire flow.**

# 4. GHL Integration

## 4.1  Inbound Data — GHL Contact Fields Required

The bot pulls all customer data from the GHL contact record via webhook payload. The following fields must be populated on the contact before the bot can process a quote. If any required field is missing, the bot must reject the job and tag the contact with ng-quote-missing-data.

| **GHL Field** | **Maps To Portal Field** | **Required** | **Notes** |
| --- | --- | --- | --- |
| first_name | First Name | Yes | Client Search + Client Info |
| last_name | Last Name | Yes | Client Search + Client Info |
| zip | Zip Code | Yes | Client Search |
| date_of_birth | Date of Birth | Yes | Format: MMDDYYYY (no separators) |
| gender | Gender | Yes | M or F |
| marital_status | Marital Status | Yes | Single, Married, Divorced, Widowed, Separated |
| occupation | Occupation | Yes | Defaults to Other if not mapped |
| phone | Phone Number | Yes | Split into area code (3), prefix (3), line (4) |
| address1 | Street Address 1 | Yes | Full street address |
| city | City | Yes |  |
| email | Email Address | Yes | Entered twice (Email + Confirm Email) |
| years_at_residence | Years at Residence | No | Defaults to 5 if missing |
| prior_carrier_home | Prior Carrier | No | Defaults to None/Other if missing |
| prior_expiration | Expiration Date | No | Prior policy expiration |
| years_continuous_ins | Years Continuous Ins | No | Defaults to 1 if missing |
| vehicles | Vehicle Info array | Yes | Array of objects: year, make, model, VIN. Each vehicle processed separately. |
| num_vehicles | Vehicle count | Yes | Determines how many Vehicle Info loops to execute |

## 4.2  Outbound Data — GHL Custom Fields Updated

After a successful quote, the bot writes the following custom fields back to the GHL contact record. These fields must be created in GHL Settings before the bot goes live.

| **GHL Custom Field** | **Type** | **Value** |
| --- | --- | --- |
| ng_price | Text | Combined premium amount extracted from Premium Summary page |
| ng_home_premium | Text | Homeowners portion of bundled premium |
| ng_auto_premium | Text | Auto portion of bundled premium |
| ng_quote_status | Dropdown | Quoted / Failed / Missing Data / Not Eligible |
| ng_quote_url | Text | Google Drive shareable URL for quote PDF |
| ng_quote_date | Date | Timestamp of successful quote generation |
| ng_pay_plan | Text | Selected payment plan from Premium Summary |
| ng_carrier | Text | National General (static value) |

## 4.3  GHL Tags

The bot applies tags to contacts at various stages to enable filtering, reporting, and workflow triggers in GHL.

| **Tag** | **When Applied** | **Purpose** |
| --- | --- | --- |
| ng-quote-success | Quote completed successfully | Triggers quote delivery workflow in GHL |
| ng-quote-failed | Quote failed after 3 retries | Triggers manual review workflow |
| ng-quote-missing-data | Required fields missing on contact | Triggers data collection workflow |
| ng-quote-processing | Job enters PROCESSING state | Prevents duplicate submissions |
| ng-quote-not-eligible | Portal rejects the application | Logs ineligibility reason |

# 5. Portal-Specific Logic — Field Mapping Rules

## 5.1  Phone Number Splitting

The National General portal requires the phone number to be entered across three separate fields: Area Code (3 digits), Prefix (3 digits), and Line Number (4 digits). The GHL contact stores the phone as a single string. The bot must parse and split it. Phone type defaults to Cell.

## 5.2  Date Fields

All date fields in the NG portal use MMDDYYYY format with no separators. The bot must convert GHL date formats accordingly. Date of Birth, Prior Policy Expiration, Effective Date (calendar picker), and Vehicle Purchased Date all require this conversion. The Effective Date field uses a calendar picker UI — the bot must click the calendar icon, select the month dropdown, then click the target day.

## 5.3  Prefill Verification (Page 6)

After entering client info, the portal runs a prefill check and may return additional household members. The bot must handle three scenarios:

| **Scenario** | **Action** | **Portal Steps** |
| --- | --- | --- |
| No prefill results | Click Next to proceed | Single click |
| Prefill matches found | Accept matching residents, reject others | For each: select Accept/Reject. If Reject, select Rejection Reason and enter Comment |
| License checkbox required | Check the license verification boxes | Steps 107–108 in JSON: two checkboxes must be checked before Next |

## 5.4  Vehicle Processing Loop

The NG360 Bot must handle multiple vehicles dynamically. The Chrome Recorder JSON shows a flow for two vehicles, but production quotes may have 1 to 6 vehicles. For each vehicle, the bot must:

| **Step** | **Action** | **Portal Element** |
| --- | --- | --- |
| 1 | Click View/Edit on the vehicle row | btnViewEdit_N (N = vehicle index, 0-based) |
| 2 | Answer rental/leased question | Is vehicle ever rented/leased to others for a fee? = False |
| 3 | Answer weight question | Is the vehicle weight between 14k-16k? = False |
| 4 | Answer camper question | Is a Camper Unit included? = False |
| 5 | Set ownership status | Ownership Status (1=Owned, 2=Financed, 3=Leased) |
| 6 | Enter annual mileage | Annual Mileage (default 10000 if not provided) |
| 7 | Click Save Vehicle | Save Vehicle button → page reload |
| 8 | Enter purchased date | Purchased Date in MM/DD/YYYY format |
| 9 | Click Save Vehicle again | Save Vehicle button → page reload |
| 10 | Set vehicle coverages | Comprehensive deductible, Roadside, Customizing Equipment, Custom Audio, Transportation Expense |
| 11 | Click Save Vehicle Coverages | Save Vehicle Coverages button → page reload |

**PAGE RELOADS: The Vehicle Info page reloads after every Save action. The bot must wait for the page to fully reload before proceeding to the next action. Use Playwright waitForNavigation or waitForLoadState('networkidle'). This is the highest-risk page for timing failures.**

# 6. Premium Summary and PDF Capture

The final page of the quote flow displays the combined home + auto premium. The bot must perform the following steps to extract pricing and capture the quote proposal PDF.

| **Step** | **Action** | **Details** |
| --- | --- | --- |
| 1 | Select Pay Method | Set to AS (Agency Sweep) — this is Trustwell's standard payment method |
| 2 | Select Pay Plan | Choose appropriate pay plan from dropdown |
| 3 | Click Re-Rate | Recalculates premium with selected payment options. Wait for page reload. |
| 4 | Extract Premium Values | Scrape the combined premium, home premium, and auto premium from the summary table |
| 5 | Click Quote Proposal | Generates the PDF quote document. This may open in a new tab or trigger a download. |
| 6 | Capture PDF | Use Chrome DevTools Protocol (CDP) to intercept the PDF, or download and read from disk |
| 7 | Upload to Google Drive | Create customer folder, upload PDF, generate shareable URL |
| 8 | Update GHL Contact | Write ng_price, ng_home_premium, ng_auto_premium, ng_quote_url, ng_quote_date, ng_quote_status |

**QUOTE PROPOSAL POPUP: Step 245 in the JSON shows the Quote Proposal button navigating to a Canny changelog widget URL, followed by a Chrome extension authorization. This is an artifact of the recording environment. In production, the Quote Proposal button should either open a PDF in a new tab or trigger a download. Test this behavior carefully in a clean browser profile.**

# 7. Error Handling and Failure Modes

The NG360 Bot operates against a third-party portal that can change without notice. Robust error handling is non-negotiable. The following table documents known failure modes and required mitigation strategies.

| **Failure Mode** | **Detection** | **Mitigation** | **Priority** |
| --- | --- | --- | --- |
| Login failure | No redirect to MainMenu.aspx | Retry once with fresh browser context. If still fails, alert Slack and pause queue. | Critical |
| Session timeout | Unexpected redirect to login page | Detect mid-flow login redirects. Re-authenticate and resume from last known page. | Critical |
| Prefill modal unexpected | Unknown elements on Prefill page | Screenshot, log, attempt default Accept All. If fails, reject job. | High |
| Vehicle page reload timeout | Page does not reload within 30s after Save | Retry the Save click. If still no reload after 60s, fail the job. | High |
| Calendar picker failure | Date not selected after click sequence | Fallback: type date directly into the input field via keyboard. | Medium |
| Missing dropdown option | Dropdown value not found | Log the expected vs available values. Use closest match or default. | Medium |
| Portal maintenance/downtime | HTTP 5xx or connection timeout | Pause queue for 15 minutes, retry. After 3 pauses, alert and stop. | High |
| PDF generation failure | Quote Proposal button does not produce PDF | Retry Re-Rate then Quote Proposal. If still fails, screenshot summary page as fallback. | High |
| Rate rejection / not eligible | Portal displays error or ineligibility msg | Capture error text, tag contact ng-quote-not-eligible, update GHL status. | Medium |

# 8. Environment Configuration

The following environment variables must be configured before the bot can run. Store these in a .env file in the project root. Never commit this file to version control.

| **Variable** | **Description** | **Example** |
| --- | --- | --- |
| NATGEN_USERNAME | National General portal login username | trustwell0 |
| NATGEN_PASSWORD | National General portal login password | (from secure vault) |
| NATGEN_AGENT_ID | Agent ID for Input By field | 20050264 |
| GHL_API_KEY | GHL API v2 key | (from GHL Settings) |
| GHL_LOCATION_ID | GHL location ID for contact updates | (from GHL Settings) |
| WEBHOOK_PORT | Port for FastAPI webhook server | 8004 (avoid 8000/8002/8003 conflicts) |
| GDRIVE_FOLDER_ID | Google Drive parent folder for quotes | (folder ID) |
| SLACK_WEBHOOK_QUOTES | Slack webhook for #insurance-quotes | (Slack URL) |
| SLACK_WEBHOOK_ALERTS | Slack webhook for #bot-alerts | (Slack URL) |
| PUSHOVER_TOKEN | Pushover API token for mobile alerts | (token) |
| PUSHOVER_USER | Pushover user key | (user key) |
| DB_HOST | PostgreSQL host (Mac Studio via Tailscale) | 100.x.x.x |
| DB_NAME | Database name | trustwell_bots |
| DB_USER | Database user | bot_user |
| DB_PASSWORD | Database password | (from secure vault) |

# 9. Bot Fleet Context — Do Not Conflict

The NG360 Bot joins an existing fleet of carrier-specific bots. Each bot runs independently on its own port with its own queue. The contractor must ensure the NG360 Bot does not conflict with existing services.

| **Bot** | **Carrier** | **Port** | **States** | **Product** | **Status** |
| --- | --- | --- | --- | --- | --- |
| NS Bot | National Summit | 8000 | GA, TN | Homeowners | Active |
| HOA Bot | Homeowners of America | 8002 | SC, TX | Homeowners | Active |
| AI Bot | American Integrity | 8003 | FL | Homeowners | Disabled |
| NG360 Bot | National General | 8004 | GA | PKGProtect2 (Home + Auto) | New Build |
| PL Bot | PolicyLynx (Multi) | 8001 | Multi | Auto (Comparative) | Active |

**PORT ASSIGNMENT: The NG360 Bot MUST use port 8004. Ports 8000, 8001, 8002, and 8003 are already allocated. Verify no port conflict exists before starting the service.**

# 10. Recommended File Structure

| **Path** | **Purpose** |
| --- | --- |
| ng360_bot/ | Project root |
| ng360_bot/main.py | Entry point — starts FastAPI server and queue processor |
| ng360_bot/config.py | Environment variable loader and validation |
| ng360_bot/webhook_server.py | FastAPI webhook receiver (port 8004) |
| ng360_bot/queue_manager.py | Job queue with JSON persistence and priority levels |
| ng360_bot/ng360_bridge.py | Core Playwright automation — the 14-page quote flow |
| ng360_bot/ghl_service.py | GHL API integration (read contacts, update custom fields, apply tags) |
| ng360_bot/gdrive_service.py | Google Drive upload (customer folders, PDF storage) |
| ng360_bot/slack_service.py | Slack notification service (success + failure channels) |
| ng360_bot/watchdog.py | Watchdog coroutine for stuck job detection |
| ng360_bot/models.py | Data models (Job, Contact, QuoteResult, Vehicle) |
| ng360_bot/utils.py | Phone parsing, date formatting, retry decorators |
| ng360_bot/.env | Environment variables (NEVER commit) |
| ng360_bot/.env.example | Template with all required variables (no values) |
| ng360_bot/quote_queue.json | Persistent queue state file |
| ng360_bot/screenshots/ | Debug screenshots organized by job ID |
| ng360_bot/NG360_BOT.json | Source Chrome Recorder JSON (reference only) |

# 11. Acceptance Criteria

The contractor's deliverable will be evaluated against the following acceptance criteria. All items must pass before the engagement is considered complete.

| **#** | **Criterion** | **Measurement** |
| --- | --- | --- |
| 1 | End-to-end quote generation | Bot completes a full PKGProtect2 quote for a test GA contact with valid data, producing a PDF and updating all GHL custom fields. |
| 2 | Multi-vehicle support | Bot correctly processes quotes with 1, 2, and 3 vehicles in separate test runs. |
| 3 | Error recovery | Bot recovers from a simulated session timeout mid-flow without manual intervention. |
| 4 | Queue persistence | Bot process is killed mid-quote. On restart, the interrupted job is re-queued and completed. |
| 5 | GHL field validation | Bot rejects a contact with missing required fields, tags it ng-quote-missing-data, and does not attempt a portal session. |
| 6 | Slack notifications | Success and failure notifications appear in correct Slack channels with job details. |
| 7 | Google Drive upload | PDF is uploaded to a correctly named customer folder with a shareable URL written to GHL. |
| 8 | No port conflicts | Bot runs on port 8004 concurrently with NS Bot (8000), PL Bot (8001), and HOA Bot (8002) without issues. |
| 9 | Credential externalization | No hardcoded credentials in source code. All secrets loaded from environment variables. |
| 10 | Screenshot capture | Debug screenshots captured at each page transition, stored in job-specific subdirectory. |
| 11 | Watchdog recovery | Simulated stuck job (>600s) is automatically rescued and re-queued by watchdog. |
| 12 | Target success rate | 90%+ success rate across 10 consecutive test quotes with valid data. |

# 12. Operational Standards

## 12.1  Bot Design Principles

All bots in the Trustwell fleet follow these non-negotiable design principles. The contractor must adhere to these standards.

| **Principle** | **Description** |
| --- | --- |
| Small bots, local to their machine | Each bot runs independently on its assigned Mac. No cross-machine dependencies for mission-critical operations. |
| No Docker | All bots run natively on macOS. Docker is not used in the Trustwell infrastructure. |
| PostgreSQL primary on Mac Studio | All persistent data writes go to Mac Studio. Replicas on Minis are read-only. |
| Google Drive backup | All generated documents (PDFs, quotes) are uploaded to Google Drive as the backup layer. |
| Slack notifications only | No SMS notifications to Desmond. All alerts go to Slack channels. |
| Python only | No Zapier, Make, or n8n. All automation is Python-native. |
| GHL sends complete data | If fields appear empty in the webhook payload, it is a parsing/extraction bug in the bot, never a GHL data quality issue. |

## 12.2  Startup and Monitoring

The contractor must provide clear startup commands and a monitoring approach in the project README. At minimum:

| **Command** | **Purpose** |
| --- | --- |
| python main.py | Start the bot (webhook server + queue processor + watchdog) |
| curl http://localhost:8004/health | Health check endpoint returning queue depth, last job status, uptime |
| curl http://localhost:8004/queue | Queue status endpoint showing all pending/processing/completed jobs |
| python main.py --test-quote <contact_id> | Manual test: run a single quote for a specific GHL contact |
| python main.py --drain | Graceful shutdown: finish current job, stop accepting new webhooks |

# 13. Deliverables

The contractor must deliver the following items upon completion of the engagement.

| **#** | **Deliverable** | **Format** |
| --- | --- | --- |
| 1 | Complete source code for NG360 Bot | Python project in the file structure specified in Section 10 |
| 2 | README with setup instructions | Markdown file covering installation, env config, startup, and testing |
| 3 | .env.example template | All required environment variables with descriptions, no values |
| 4 | Test results log | JSON or CSV log of 10+ successful test quotes with timestamps and contact IDs |
| 5 | Screenshot evidence of multi-vehicle support | Screenshots showing 1-vehicle, 2-vehicle, and 3-vehicle successful completions |
| 6 | GHL custom field setup documentation | List of all custom fields that must be created in GHL before go-live |
| 7 | Known issues and limitations document | Any portal behaviors, edge cases, or limitations discovered during development |

JSON Mapping

```json
{
    "title": "NG360 BOT",
    "steps": [
        {
            "type": "setViewport",
            "width": 943,
            "height": 1194,
            "deviceScaleFactor": 1,
            "isMobile": false,
            "hasTouch": false,
            "isLandscape": false
        },
        {
            "type": "navigate",
            "url": "https://natgenagency.com/",
            "assertedEvents": [
                {
                    "type": "navigation",
                    "url": "https://natgenagency.com/",
                    "title": "National General Insurance, Inc."
                }
            ]
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/SIGN IN",
                    "aria/[role=\"generic\"]"
                ],
                [
                    "span span"
                ],
                [
                    "xpath///*[@id=\"btnLogin\"]/span"
                ],
                [
                    "pierce/span span"
                ]
            ],
            "offsetY": 3,
            "offsetX": 49,
            "assertedEvents": [
                {
                    "type": "navigation",
                    "url": "https://login.natgenagency.com/Account/Login?ReturnUrl=%2Fconnect%2Fauthorize%2Fcallback%3Fclient_id%3D587fa794-cb46-4c08-918a-08db5092fa05%26redirect_uri%3Dhttps%253A%252F%252Fnatgenagency.com%252FOAuthCallback%26response_type%3Dcode%26scope%3Dopenid%2520profile%2520email%2520offline_access%26code_challenge%3DdQ-bXKj1Z-OXBGt1L4Q7XeRmnag53Ttt_nEuzHnzRKk%26code_challenge_method%3DS256%26state%3DOpenIdConnect.AuthenticationProperties%253DqtLaHH89iJo5Eboegabi_FZoPegYatoPpeMlcW2WUZns_6Lct-G-0dXk9VvGICyNwZuUPd4ZJGxh9x6MQejawksg1yY1a6EKYaQ9zvkh6fG5KlCuKLzV-l5CG36EAo9vznsPWp7MIuZgUQAl6BdGre9coQZKRbjdbgBX1rOMNdn04jCjGIPuoh6jKqoOoYe0i6xh_cWHHRaJFZNtik0Bul-Eq26p8rj2UbhBzfUojMesNJzLrETibBMM3EK3YS3MPLQfM74Q0Hr_z-CeKLtbA7kmyZ6jGnu0V1njCPJTh8C6exauSSwvLUPnPOQhHjiO7PmVKBGP_sjslsJZejf-EtJQjYJe5o0-tkbjVZ496HN3n5hnQi9D5uY7lGYD1vE8V031-3sA-zzmpjjZru-upmERy-Uybe1yt4xlhd6JqyZr1P4ht4YjudCKcVRqNqYPrYUmWkfuCsNtKxAd1KlLJUcULlYXTtCYTmNti0geKNc4atPfX0cOsC-TrRvcQAG8Ir1vCVvpblpej2bOoFrzNUQE9dhFlbZxoQNiejUSS50%26login_hint%3Dtrustwell0%26acr_values%3Dbranding%253ANatGenAgency%26x-client-SKU%3DID_NET462%26x-client-ver%3D7.3.0.0",
                    "title": ""
                }
            ]
        },
        {
            "type": "change",
            "value": "Dteezy$2026$2034",
            "selectors": [
                [
                    "aria/PASSWORD"
                ],
                [
                    "#Password"
                ],
                [
                    "xpath///*[@id=\"Password\"]"
                ],
                [
                    "pierce/#Password"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/SIGN IN "
                ],
                [
                    "div:nth-of-type(3) button"
                ],
                [
                    "xpath///*[@id=\"login-form\"]/form/div[3]/div/button"
                ],
                [
                    "pierce/div:nth-of-type(3) button"
                ],
                [
                    "text/SIGN IN"
                ]
            ],
            "offsetY": 11.328125,
            "offsetX": 231.5,
            "assertedEvents": [
                {
                    "type": "navigation",
                    "url": "https://natgenagency.com/MainMenu.aspx",
                    "title": ""
                }
            ]
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "#ctl00_MainContent_wgtMainMenuNewQuote_ddlState"
                ],
                [
                    "xpath///*[@id=\"ctl00_MainContent_wgtMainMenuNewQuote_ddlState\"]"
                ],
                [
                    "pierce/#ctl00_MainContent_wgtMainMenuNewQuote_ddlState"
                ]
            ],
            "offsetY": 6,
            "offsetX": 110.5
        },
        {
            "type": "change",
            "value": "GA",
            "selectors": [
                [
                    "#ctl00_MainContent_wgtMainMenuNewQuote_ddlState"
                ],
                [
                    "xpath///*[@id=\"ctl00_MainContent_wgtMainMenuNewQuote_ddlState\"]"
                ],
                [
                    "pierce/#ctl00_MainContent_wgtMainMenuNewQuote_ddlState"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "#ctl00_MainContent_wgtMainMenuNewQuote_ddlProduct"
                ],
                [
                    "xpath///*[@id=\"ctl00_MainContent_wgtMainMenuNewQuote_ddlProduct\"]"
                ],
                [
                    "pierce/#ctl00_MainContent_wgtMainMenuNewQuote_ddlProduct"
                ]
            ],
            "offsetY": 9,
            "offsetX": 100.5
        },
        {
            "type": "change",
            "value": "PKGProtect2",
            "selectors": [
                [
                    "#ctl00_MainContent_wgtMainMenuNewQuote_ddlProduct"
                ],
                [
                    "xpath///*[@id=\"ctl00_MainContent_wgtMainMenuNewQuote_ddlProduct\"]"
                ],
                [
                    "pierce/#ctl00_MainContent_wgtMainMenuNewQuote_ddlProduct"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Begin"
                ],
                [
                    "#ctl00_MainContent_wgtMainMenuNewQuote_btnContinue"
                ],
                [
                    "xpath///*[@id=\"ctl00_MainContent_wgtMainMenuNewQuote_btnContinue\"]"
                ],
                [
                    "pierce/#ctl00_MainContent_wgtMainMenuNewQuote_btnContinue"
                ],
                [
                    "text/Begin"
                ]
            ],
            "offsetY": 12,
            "offsetX": 33.84375,
            "assertedEvents": [
                {
                    "type": "navigation",
                    "url": "https://ho.natgenagency.com/ContentPages/ClientSearch",
                    "title": ""
                }
            ]
        },
        {
            "type": "navigate",
            "url": "chrome://new-tab-page/",
            "assertedEvents": [
                {
                    "type": "navigation",
                    "url": "chrome://new-tab-page/",
                    "title": "New Tab"
                }
            ]
        },
        {
            "type": "click",
            "target": "chrome://new-tab-page/",
            "selectors": [
                [
                    "aria/TrustwellONE®"
                ],
                [
                    "ntp-app",
                    "#mostVisited",
                    "div:nth-of-type(6) > a"
                ],
                [
                    "pierce/div:nth-of-type(6) > a"
                ]
            ],
            "offsetY": 19,
            "offsetX": 76
        },
        {
            "type": "navigate",
            "url": "https://app.trustwellinsurance.com/",
            "assertedEvents": [
                {
                    "type": "navigation",
                    "url": "https://app.trustwellinsurance.com/",
                    "title": ""
                }
            ]
        },
        {
            "type": "click",
            "target": "https://app.trustwellinsurance.com/",
            "selectors": [
                [
                    "aria/Sub-Accounts icon Sub-Accounts"
                ],
                [
                    "a:nth-of-type(4)"
                ],
                [
                    "xpath///*[@id=\"sb_agency-accounts\"]"
                ],
                [
                    "pierce/a:nth-of-type(4)"
                ]
            ],
            "offsetY": 25.443191528320312,
            "offsetX": 134
        },
        {
            "type": "click",
            "target": "https://app.trustwellinsurance.com/",
            "selectors": [
                [
                    "#loc-Czwg7VWYU6myocqsb86R__switch"
                ],
                [
                    "xpath///*[@id=\"loc-Czwg7VWYU6myocqsb86R__switch\"]"
                ],
                [
                    "pierce/#loc-Czwg7VWYU6myocqsb86R__switch"
                ]
            ],
            "offsetY": 8.17047119140625,
            "offsetX": 17.6534423828125
        },
        {
            "type": "click",
            "target": "https://app.trustwellinsurance.com/",
            "selectors": [
                [
                    "aria/Contacts icon Contacts",
                    "aria/[role=\"generic\"]"
                ],
                [
                    "#sb_contacts > span"
                ],
                [
                    "xpath///*[@id=\"sb_contacts\"]/span"
                ],
                [
                    "pierce/#sb_contacts > span"
                ]
            ],
            "offsetY": 15.46307373046875,
            "offsetX": 38.00852584838867
        },
        {
            "type": "click",
            "target": "https://app.trustwellinsurance.com/",
            "selectors": [
                [
                    "aria/Matthew Mgbeke"
                ],
                [
                    "div:nth-of-type(10) a"
                ],
                [
                    "xpath///*[@id=\"table-container\"]/div[2]/div[2]/div/div[10]/div[2]/div/div[2]/a"
                ],
                [
                    "pierce/div:nth-of-type(10) a"
                ],
                [
                    "text/Matthew Mgbeke"
                ]
            ],
            "offsetY": 6.09661865234375,
            "offsetX": 39.10797119140625
        },
        {
            "type": "click",
            "target": "https://app.trustwellinsurance.com/",
            "selectors": [
                [
                    "div.gap-2 > button:nth-of-type(1) svg"
                ],
                [
                    "xpath///*[@id=\"sidebar-activities-icon\"]/svg"
                ],
                [
                    "pierce/div.gap-2 > button:nth-of-type(1) svg"
                ]
            ],
            "offsetY": 11.005691528320312,
            "offsetX": 14.1846923828125
        },
        {
            "type": "click",
            "target": "https://app.trustwellinsurance.com/",
            "selectors": [
                [
                    "div.gap-2 > button:nth-of-type(1) svg"
                ],
                [
                    "xpath///*[@id=\"sidebar-activities-icon\"]/svg"
                ],
                [
                    "pierce/div.gap-2 > button:nth-of-type(1) svg"
                ]
            ],
            "offsetY": 14.005691528320312,
            "offsetX": 11.1846923828125
        },
        {
            "type": "click",
            "target": "https://app.trustwellinsurance.com/",
            "selectors": [
                [
                    "div.hr-tabs-rail > div:nth-of-type(2) span"
                ],
                [
                    "xpath///*[@id=\"record-details-tabs\"]/div[1]/div[1]/div[2]/div/span"
                ],
                [
                    "pierce/div.hr-tabs-rail > div:nth-of-type(2) span"
                ],
                [
                    "text/All fields"
                ]
            ],
            "offsetY": 3.622161865234375,
            "offsetX": 9.35797119140625
        },
        {
            "type": "doubleClick",
            "target": "https://app.trustwellinsurance.com/",
            "selectors": [
                [
                    "div.hover div:nth-of-type(8) input"
                ],
                [
                    "xpath///*[@data-testid=\"hl-input\"]/div[1]/div[1]/input"
                ],
                [
                    "pierce/div.hover div:nth-of-type(8) input"
                ],
                [
                    "text/230 Hickory Pointe"
                ]
            ],
            "offsetY": 7.2728271484375,
            "offsetX": 49.116485595703125
        },
        {
            "type": "doubleClick",
            "target": "https://app.trustwellinsurance.com/",
            "selectors": [
                [
                    "div.hover div:nth-of-type(8) input"
                ],
                [
                    "xpath///*[@data-testid=\"hl-input\"]/div[1]/div[1]/input"
                ],
                [
                    "pierce/div.hover div:nth-of-type(8) input"
                ],
                [
                    "text/230 Hickory Pointe"
                ]
            ],
            "offsetY": 7.2728271484375,
            "offsetX": 49.116485595703125
        },
        {
            "type": "click",
            "target": "https://app.trustwellinsurance.com/",
            "selectors": [
                [
                    "div.hover div:nth-of-type(8) input"
                ],
                [
                    "xpath///*[@data-testid=\"hl-input\"]/div[1]/div[1]/input"
                ],
                [
                    "pierce/div.hover div:nth-of-type(8) input"
                ],
                [
                    "text/230 Hickory Pointe"
                ]
            ],
            "offsetY": 7.2728271484375,
            "offsetX": 49.116485595703125
        },
        {
            "type": "click",
            "target": "https://www.google.com/search?q=230+Hickory+Pointe+Dr&rlz=1C5OZZY_enUS1160US1160&sourceid=chrome&ie=UTF-8",
            "selectors": [
                [
                    "#rcnt"
                ],
                [
                    "xpath///*[@id=\"rcnt\"]"
                ],
                [
                    "pierce/#rcnt"
                ]
            ],
            "offsetY": 112,
            "offsetX": 1203
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "#main"
                ],
                [
                    "xpath///*[@id=\"main\"]"
                ],
                [
                    "pierce/#main"
                ]
            ],
            "offsetY": 428,
            "offsetX": 170.5
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/First Name"
                ],
                [
                    "#MainContent_txtFirstName"
                ],
                [
                    "xpath///*[@id=\"MainContent_txtFirstName\"]"
                ],
                [
                    "pierce/#MainContent_txtFirstName"
                ]
            ],
            "offsetY": 16,
            "offsetX": 36.25
        },
        {
            "type": "click",
            "target": "https://www.google.com/search?q=230+Hickory+Pointe+Dr&rlz=1C5OZZY_enUS1160US1160&sourceid=chrome&ie=UTF-8",
            "selectors": [
                [
                    "#rcnt"
                ],
                [
                    "xpath///*[@id=\"rcnt\"]"
                ],
                [
                    "pierce/#rcnt"
                ]
            ],
            "offsetY": 109,
            "offsetX": 1352
        },
        {
            "type": "navigate",
            "url": "chrome://new-tab-page/",
            "assertedEvents": [
                {
                    "type": "navigation",
                    "url": "chrome://new-tab-page/",
                    "title": "New Tab"
                }
            ]
        },
        {
            "type": "click",
            "target": "chrome://new-tab-page/",
            "selectors": [
                [
                    "ntp-app",
                    "#mostVisited",
                    "div:nth-of-type(5) > cr-icon-button.icon-more-vert",
                    "#maskedImage"
                ],
                [
                    "pierce/div:nth-of-type(5) > cr-icon-button.icon-more-vert",
                    "pierce/#maskedImage"
                ]
            ],
            "offsetY": 10,
            "offsetX": 26
        },
        {
            "type": "click",
            "target": "chrome://new-tab-page/",
            "selectors": [
                [
                    "ntp-app",
                    "#mostVisited",
                    "#actionMenu",
                    "#dialog"
                ],
                [
                    "pierce/#mostVisited",
                    "pierce/#actionMenu",
                    "pierce/#dialog"
                ]
            ],
            "offsetY": 30,
            "offsetX": 204
        },
        {
            "type": "click",
            "target": "chrome://new-tab-page/",
            "selectors": [
                [
                    "aria/TrustwellONE®"
                ],
                [
                    "ntp-app",
                    "#mostVisited",
                    "div:nth-of-type(6) > a"
                ],
                [
                    "pierce/div:nth-of-type(6) > a"
                ]
            ],
            "offsetY": 57,
            "offsetX": 60
        },
        {
            "type": "navigate",
            "url": "https://app.trustwellinsurance.com/",
            "assertedEvents": [
                {
                    "type": "navigation",
                    "url": "https://app.trustwellinsurance.com/",
                    "title": ""
                }
            ]
        },
        {
            "type": "click",
            "target": "https://app.trustwellinsurance.com/",
            "selectors": [
                [
                    "aria/Sub-Accounts icon Sub-Accounts",
                    "aria/[role=\"generic\"]"
                ],
                [
                    "a:nth-of-type(4) > span"
                ],
                [
                    "xpath///*[@id=\"sb_agency-accounts\"]/span"
                ],
                [
                    "pierce/a:nth-of-type(4) > span"
                ],
                [
                    "text/Sub-Accounts"
                ]
            ],
            "offsetY": 3.4460296630859375,
            "offsetX": 84.00852584838867
        },
        {
            "type": "click",
            "target": "https://app.trustwellinsurance.com/",
            "selectors": [
                [
                    "#loc-Czwg7VWYU6myocqsb86R__switch"
                ],
                [
                    "xpath///*[@id=\"loc-Czwg7VWYU6myocqsb86R__switch\"]"
                ],
                [
                    "pierce/#loc-Czwg7VWYU6myocqsb86R__switch"
                ]
            ],
            "offsetY": 18.80682373046875,
            "offsetX": 21.6534423828125
        },
        {
            "type": "click",
            "target": "https://app.trustwellinsurance.com/",
            "selectors": [
                [
                    "aria/Contacts icon Contacts",
                    "aria/[role=\"generic\"]"
                ],
                [
                    "#sb_contacts > span"
                ],
                [
                    "xpath///*[@id=\"sb_contacts\"]/span"
                ],
                [
                    "pierce/#sb_contacts > span"
                ]
            ],
            "offsetY": 15.46307373046875,
            "offsetX": 35.00852584838867
        },
        {
            "type": "click",
            "target": "https://app.trustwellinsurance.com/",
            "selectors": [
                [
                    "aria/Matthew Mgbeke"
                ],
                [
                    "div:nth-of-type(10) a"
                ],
                [
                    "xpath///*[@id=\"table-container\"]/div[2]/div[2]/div/div[10]/div[2]/div/div[2]/a"
                ],
                [
                    "pierce/div:nth-of-type(10) a"
                ],
                [
                    "text/Matthew Mgbeke"
                ]
            ],
            "offsetY": 8.914794921875,
            "offsetX": 39.10797119140625
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/First Name"
                ],
                [
                    "#MainContent_txtFirstName"
                ],
                [
                    "xpath///*[@id=\"MainContent_txtFirstName\"]"
                ],
                [
                    "pierce/#MainContent_txtFirstName"
                ]
            ],
            "offsetY": 11,
            "offsetX": 67.25
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/First Name"
                ],
                [
                    "#MainContent_txtFirstName"
                ],
                [
                    "xpath///*[@id=\"MainContent_txtFirstName\"]"
                ],
                [
                    "pierce/#MainContent_txtFirstName"
                ]
            ],
            "offsetY": 10,
            "offsetX": 47.25
        },
        {
            "type": "change",
            "value": "M",
            "selectors": [
                [
                    "aria/First Name"
                ],
                [
                    "#MainContent_txtFirstName"
                ],
                [
                    "xpath///*[@id=\"MainContent_txtFirstName\"]"
                ],
                [
                    "pierce/#MainContent_txtFirstName"
                ]
            ],
            "target": "main"
        },
        {
            "type": "keyUp",
            "key": "m",
            "target": "main"
        },
        {
            "type": "change",
            "value": "Matthew",
            "selectors": [
                [
                    "aria/First Name"
                ],
                [
                    "#MainContent_txtFirstName"
                ],
                [
                    "xpath///*[@id=\"MainContent_txtFirstName\"]"
                ],
                [
                    "pierce/#MainContent_txtFirstName"
                ]
            ],
            "target": "main"
        },
        {
            "type": "keyDown",
            "target": "main",
            "key": "Tab"
        },
        {
            "type": "keyUp",
            "key": "Tab",
            "target": "main"
        },
        {
            "type": "change",
            "value": "Mgbeke",
            "selectors": [
                [
                    "aria/Last Name"
                ],
                [
                    "#MainContent_txtLastName"
                ],
                [
                    "xpath///*[@id=\"MainContent_txtLastName\"]"
                ],
                [
                    "pierce/#MainContent_txtLastName"
                ]
            ],
            "target": "main"
        },
        {
            "type": "keyDown",
            "target": "main",
            "key": "Tab"
        },
        {
            "type": "keyUp",
            "key": "Tab",
            "target": "main"
        },
        {
            "type": "change",
            "value": "30101",
            "selectors": [
                [
                    "aria/Zip Code"
                ],
                [
                    "#MainContent_txtZipCode"
                ],
                [
                    "xpath///*[@id=\"MainContent_txtZipCode\"]"
                ],
                [
                    "pierce/#MainContent_txtZipCode"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Search"
                ],
                [
                    "#MainContent_btnSearch"
                ],
                [
                    "xpath///*[@id=\"MainContent_btnSearch\"]"
                ],
                [
                    "pierce/#MainContent_btnSearch"
                ]
            ],
            "offsetY": 19,
            "offsetX": 49.25,
            "assertedEvents": [
                {
                    "type": "navigation",
                    "url": "https://ho.natgenagency.com/ContentPages/ClientSearch",
                    "title": ""
                }
            ]
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Add New Customer"
                ],
                [
                    "#MainContent_btnAddNewClient"
                ],
                [
                    "xpath///*[@id=\"MainContent_btnAddNewClient\"]"
                ],
                [
                    "pierce/#MainContent_btnAddNewClient"
                ]
            ],
            "offsetY": 17,
            "offsetX": 55.796875,
            "assertedEvents": [
                {
                    "type": "navigation",
                    "url": "https://ho.natgenagency.com/ContentPages/ClientInfo",
                    "title": ""
                }
            ]
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Date of Birth"
                ],
                [
                    "#MainContent_ucNamedInsured_txtDateOfBirth"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucNamedInsured_txtDateOfBirth\"]"
                ],
                [
                    "pierce/#MainContent_ucNamedInsured_txtDateOfBirth"
                ]
            ],
            "offsetY": 5,
            "offsetX": 54.875
        },
        {
            "type": "change",
            "value": "06071958",
            "selectors": [
                [
                    "aria/Date of Birth"
                ],
                [
                    "#MainContent_ucNamedInsured_txtDateOfBirth"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucNamedInsured_txtDateOfBirth\"]"
                ],
                [
                    "pierce/#MainContent_ucNamedInsured_txtDateOfBirth"
                ]
            ],
            "target": "main"
        },
        {
            "type": "keyDown",
            "target": "main",
            "key": "Tab"
        },
        {
            "type": "keyUp",
            "key": "Tab",
            "target": "main"
        },
        {
            "type": "keyDown",
            "target": "main",
            "key": "Tab"
        },
        {
            "type": "keyUp",
            "key": "Tab",
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Gender"
                ],
                [
                    "#MainContent_ucNamedInsured_ddlGender"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucNamedInsured_ddlGender\"]"
                ],
                [
                    "pierce/#MainContent_ucNamedInsured_ddlGender"
                ]
            ],
            "offsetY": 20,
            "offsetX": 105.875
        },
        {
            "type": "change",
            "value": "M",
            "selectors": [
                [
                    "aria/Gender"
                ],
                [
                    "#MainContent_ucNamedInsured_ddlGender"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucNamedInsured_ddlGender\"]"
                ],
                [
                    "pierce/#MainContent_ucNamedInsured_ddlGender"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Marital Status"
                ],
                [
                    "#MainContent_ucNamedInsured_ddlMaritalStatus"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucNamedInsured_ddlMaritalStatus\"]"
                ],
                [
                    "pierce/#MainContent_ucNamedInsured_ddlMaritalStatus"
                ]
            ],
            "offsetY": 9,
            "offsetX": 79.875
        },
        {
            "type": "change",
            "value": "Single",
            "selectors": [
                [
                    "aria/Marital Status"
                ],
                [
                    "#MainContent_ucNamedInsured_ddlMaritalStatus"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucNamedInsured_ddlMaritalStatus\"]"
                ],
                [
                    "pierce/#MainContent_ucNamedInsured_ddlMaritalStatus"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Occupation"
                ],
                [
                    "#MainContent_ucNamedInsured_ddlOccupation"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucNamedInsured_ddlOccupation\"]"
                ],
                [
                    "pierce/#MainContent_ucNamedInsured_ddlOccupation"
                ]
            ],
            "offsetY": 9,
            "offsetX": 73.875
        },
        {
            "type": "change",
            "value": "Other",
            "selectors": [
                [
                    "aria/Occupation"
                ],
                [
                    "#MainContent_ucNamedInsured_ddlOccupation"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucNamedInsured_ddlOccupation\"]"
                ],
                [
                    "pierce/#MainContent_ucNamedInsured_ddlOccupation"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "#MainContent_ucContactInfo_ucPhoneNumber_ddlPhoneType"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucContactInfo_ucPhoneNumber_ddlPhoneType\"]"
                ],
                [
                    "pierce/#MainContent_ucContactInfo_ucPhoneNumber_ddlPhoneType"
                ]
            ],
            "offsetY": 15,
            "offsetX": 60
        },
        {
            "type": "change",
            "value": "Cell",
            "selectors": [
                [
                    "#MainContent_ucContactInfo_ucPhoneNumber_ddlPhoneType"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucContactInfo_ucPhoneNumber_ddlPhoneType\"]"
                ],
                [
                    "pierce/#MainContent_ucContactInfo_ucPhoneNumber_ddlPhoneType"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "#MainContent_ucContactInfo_ucPhoneNumber_txtAreaCode"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucContactInfo_ucPhoneNumber_txtAreaCode\"]"
                ],
                [
                    "pierce/#MainContent_ucContactInfo_ucPhoneNumber_txtAreaCode"
                ]
            ],
            "offsetY": 24,
            "offsetX": 15
        },
        {
            "type": "change",
            "value": "662",
            "selectors": [
                [
                    "#MainContent_ucContactInfo_ucPhoneNumber_txtAreaCode"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucContactInfo_ucPhoneNumber_txtAreaCode\"]"
                ],
                [
                    "pierce/#MainContent_ucContactInfo_ucPhoneNumber_txtAreaCode"
                ]
            ],
            "target": "main"
        },
        {
            "type": "change",
            "value": "607",
            "selectors": [
                [
                    "#MainContent_ucContactInfo_ucPhoneNumber_txtPrefix"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucContactInfo_ucPhoneNumber_txtPrefix\"]"
                ],
                [
                    "pierce/#MainContent_ucContactInfo_ucPhoneNumber_txtPrefix"
                ]
            ],
            "target": "main"
        },
        {
            "type": "change",
            "value": "6394",
            "selectors": [
                [
                    "#MainContent_ucContactInfo_ucPhoneNumber_txtLineNumber"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucContactInfo_ucPhoneNumber_txtLineNumber\"]"
                ],
                [
                    "pierce/#MainContent_ucContactInfo_ucPhoneNumber_txtLineNumber"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Street Address 1"
                ],
                [
                    "#MainContent_ucResidentialAddress_txtAddress"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucResidentialAddress_txtAddress\"]"
                ],
                [
                    "pierce/#MainContent_ucResidentialAddress_txtAddress"
                ]
            ],
            "offsetY": 11,
            "offsetX": 19.875
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Residential Address"
                ],
                [
                    "#MainContent_pnlResidentialAddress > fieldset"
                ],
                [
                    "xpath///*[@id=\"MainContent_pnlResidentialAddress\"]/fieldset"
                ],
                [
                    "pierce/#MainContent_pnlResidentialAddress > fieldset"
                ]
            ],
            "offsetY": 15,
            "offsetX": 245.875
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Residential Address"
                ],
                [
                    "#MainContent_pnlResidentialAddress > fieldset"
                ],
                [
                    "xpath///*[@id=\"MainContent_pnlResidentialAddress\"]/fieldset"
                ],
                [
                    "pierce/#MainContent_pnlResidentialAddress > fieldset"
                ]
            ],
            "offsetY": 44,
            "offsetX": 230.875
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Street Address 1"
                ],
                [
                    "#MainContent_ucResidentialAddress_txtAddress"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucResidentialAddress_txtAddress\"]"
                ],
                [
                    "pierce/#MainContent_ucResidentialAddress_txtAddress"
                ]
            ],
            "offsetY": 3,
            "offsetX": 22.875
        },
        {
            "type": "click",
            "target": "https://app.trustwellinsurance.com/",
            "selectors": [
                [
                    "div.hover div:nth-of-type(8) span"
                ],
                [
                    "xpath///*[@id=\"vx0fWyKHn1lyPBGromGE-form-item\"]/label/span"
                ],
                [
                    "pierce/div.hover div:nth-of-type(8) span"
                ],
                [
                    "text/Street address"
                ]
            ],
            "offsetY": 13.35516357421875,
            "offsetX": 92.11648559570312
        },
        {
            "type": "doubleClick",
            "target": "https://app.trustwellinsurance.com/",
            "selectors": [
                [
                    "div.hover div:nth-of-type(8) input"
                ],
                [
                    "xpath///*[@data-testid=\"hl-input\"]/div[1]/div[1]/input"
                ],
                [
                    "pierce/div.hover div:nth-of-type(8) input"
                ],
                [
                    "text/230 Hickory Pointe"
                ]
            ],
            "offsetY": 9.36370849609375,
            "offsetX": 85.11648559570312
        },
        {
            "type": "doubleClick",
            "target": "https://app.trustwellinsurance.com/",
            "selectors": [
                [
                    "div.hover div:nth-of-type(8) input"
                ],
                [
                    "xpath///*[@data-testid=\"hl-input\"]/div[1]/div[1]/input"
                ],
                [
                    "pierce/div.hover div:nth-of-type(8) input"
                ],
                [
                    "text/230 Hickory Pointe"
                ]
            ],
            "offsetY": 8.36370849609375,
            "offsetX": 86.11648559570312
        },
        {
            "type": "click",
            "target": "https://app.trustwellinsurance.com/",
            "selectors": [
                [
                    "div.hover div:nth-of-type(8) input"
                ],
                [
                    "xpath///*[@data-testid=\"hl-input\"]/div[1]/div[1]/input"
                ],
                [
                    "pierce/div.hover div:nth-of-type(8) input"
                ],
                [
                    "text/230 Hickory Pointe"
                ]
            ],
            "offsetY": 8.36370849609375,
            "offsetX": 86.11648559570312
        },
        {
            "type": "click",
            "target": "https://app.trustwellinsurance.com/",
            "selectors": [
                [
                    "div.hover div:nth-of-type(8) input"
                ],
                [
                    "xpath///*[@data-testid=\"hl-input\"]/div[1]/div[1]/input"
                ],
                [
                    "pierce/div.hover div:nth-of-type(8) input"
                ],
                [
                    "text/230 Hickory Pointe"
                ]
            ],
            "offsetY": 8.36370849609375,
            "offsetX": 86.11648559570312
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Street Address 1"
                ],
                [
                    "#MainContent_ucResidentialAddress_txtAddress"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucResidentialAddress_txtAddress\"]"
                ],
                [
                    "pierce/#MainContent_ucResidentialAddress_txtAddress"
                ]
            ],
            "offsetY": 6,
            "offsetX": 39.875
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Street Address 1"
                ],
                [
                    "#MainContent_ucResidentialAddress_txtAddress"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucResidentialAddress_txtAddress\"]"
                ],
                [
                    "pierce/#MainContent_ucResidentialAddress_txtAddress"
                ]
            ],
            "offsetY": 8,
            "offsetX": 22.875,
            "button": "secondary"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Street Address 1"
                ],
                [
                    "#MainContent_ucResidentialAddress_txtAddress"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucResidentialAddress_txtAddress\"]"
                ],
                [
                    "pierce/#MainContent_ucResidentialAddress_txtAddress"
                ]
            ],
            "offsetY": 12,
            "offsetX": 15.875
        },
        {
            "type": "change",
            "value": "230 Hickory Pointe Dr",
            "selectors": [
                [
                    "aria/Street Address 1"
                ],
                [
                    "#MainContent_ucResidentialAddress_txtAddress"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucResidentialAddress_txtAddress\"]"
                ],
                [
                    "pierce/#MainContent_ucResidentialAddress_txtAddress"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/City"
                ],
                [
                    "#MainContent_ucResidentialAddress_txtCity"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucResidentialAddress_txtCity\"]"
                ],
                [
                    "pierce/#MainContent_ucResidentialAddress_txtCity"
                ]
            ],
            "offsetY": 15,
            "offsetX": 16.875
        },
        {
            "type": "change",
            "value": "acworth",
            "selectors": [
                [
                    "aria/City"
                ],
                [
                    "#MainContent_ucResidentialAddress_txtCity"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucResidentialAddress_txtCity\"]"
                ],
                [
                    "pierce/#MainContent_ucResidentialAddress_txtCity"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Years at residence:"
                ],
                [
                    "#MainContent_ddlYearsAtAddress"
                ],
                [
                    "xpath///*[@id=\"MainContent_ddlYearsAtAddress\"]"
                ],
                [
                    "pierce/#MainContent_ddlYearsAtAddress"
                ]
            ],
            "offsetY": 17,
            "offsetX": 33.875
        },
        {
            "type": "change",
            "value": "99",
            "selectors": [
                [
                    "aria/Years at residence:"
                ],
                [
                    "#MainContent_ddlYearsAtAddress"
                ],
                [
                    "xpath///*[@id=\"MainContent_ddlYearsAtAddress\"]"
                ],
                [
                    "pierce/#MainContent_ddlYearsAtAddress"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Email Option"
                ],
                [
                    "#MainContent_ucContactInfo_ucEmailAddress_ddlEmailOption"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucContactInfo_ucEmailAddress_ddlEmailOption\"]"
                ],
                [
                    "pierce/#MainContent_ucContactInfo_ucEmailAddress_ddlEmailOption"
                ]
            ],
            "offsetY": 12,
            "offsetX": 35
        },
        {
            "type": "change",
            "value": "Provided",
            "selectors": [
                [
                    "aria/Email Option"
                ],
                [
                    "#MainContent_ucContactInfo_ucEmailAddress_ddlEmailOption"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucContactInfo_ucEmailAddress_ddlEmailOption\"]"
                ],
                [
                    "pierce/#MainContent_ucContactInfo_ucEmailAddress_ddlEmailOption"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "https://app.trustwellinsurance.com/",
            "selectors": [
                [
                    "div.hover div.hr-collapse-item__content-wrapper > div > div > div > div"
                ],
                [
                    "xpath///*[@id=\"aFC9EmbLEIPoKN3QwKb0\"]/div[2]/div/div/div/div"
                ],
                [
                    "pierce/div.hover div.hr-collapse-item__content-wrapper > div > div > div > div"
                ]
            ],
            "offsetY": 304.2301025390625,
            "offsetX": 106.11648559570312
        },
        {
            "type": "doubleClick",
            "target": "https://app.trustwellinsurance.com/",
            "selectors": [
                [
                    "div.hover div > div > div > div > div:nth-of-type(5) input"
                ],
                [
                    "xpath///*[@data-testid=\"hl-input\"]/div[1]/div[1]/input"
                ],
                [
                    "pierce/div.hover div > div > div > div > div:nth-of-type(5) input"
                ],
                [
                    "text/mattmgb2005@yahoo.com"
                ]
            ],
            "offsetY": 17.3125,
            "offsetX": 103.11648559570312
        },
        {
            "type": "click",
            "target": "https://app.trustwellinsurance.com/",
            "selectors": [
                [
                    "div.hover div > div > div > div > div:nth-of-type(5) input"
                ],
                [
                    "xpath///*[@data-testid=\"hl-input\"]/div[1]/div[1]/input"
                ],
                [
                    "pierce/div.hover div > div > div > div > div:nth-of-type(5) input"
                ],
                [
                    "text/mattmgb2005@yahoo.com"
                ]
            ],
            "offsetY": 17.3125,
            "offsetX": 103.11648559570312
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "#contentBlock ul ul"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucContactInfo_dvContactInfoResizer\"]/ul/li[4]/ul"
                ],
                [
                    "pierce/#contentBlock ul ul"
                ]
            ],
            "offsetY": 56,
            "offsetX": 283
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Email Address"
                ],
                [
                    "#MainContent_ucContactInfo_ucEmailAddress_txtEmailAddress"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucContactInfo_ucEmailAddress_txtEmailAddress\"]"
                ],
                [
                    "pierce/#MainContent_ucContactInfo_ucEmailAddress_txtEmailAddress"
                ]
            ],
            "offsetY": 13,
            "offsetX": 67
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Email Address"
                ],
                [
                    "#MainContent_ucContactInfo_ucEmailAddress_txtEmailAddress"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucContactInfo_ucEmailAddress_txtEmailAddress\"]"
                ],
                [
                    "pierce/#MainContent_ucContactInfo_ucEmailAddress_txtEmailAddress"
                ]
            ],
            "offsetY": 15,
            "offsetX": 62,
            "button": "secondary"
        },
        {
            "type": "change",
            "value": "mattmgb2005@yahoo.com",
            "selectors": [
                [
                    "aria/Email Address"
                ],
                [
                    "#MainContent_ucContactInfo_ucEmailAddress_txtEmailAddress"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucContactInfo_ucEmailAddress_txtEmailAddress\"]"
                ],
                [
                    "pierce/#MainContent_ucContactInfo_ucEmailAddress_txtEmailAddress"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Confirm Email Address"
                ],
                [
                    "#MainContent_ucContactInfo_ucEmailAddress_txtEmailAddressConfirmation"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucContactInfo_ucEmailAddress_txtEmailAddressConfirmation\"]"
                ],
                [
                    "pierce/#MainContent_ucContactInfo_ucEmailAddress_txtEmailAddressConfirmation"
                ]
            ],
            "offsetY": 18,
            "offsetX": 38
        },
        {
            "type": "change",
            "value": "mattmgb2005@yahoo.com",
            "selectors": [
                [
                    "aria/Confirm Email Address"
                ],
                [
                    "#MainContent_ucContactInfo_ucEmailAddress_txtEmailAddressConfirmation"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucContactInfo_ucEmailAddress_txtEmailAddressConfirmation\"]"
                ],
                [
                    "pierce/#MainContent_ucContactInfo_ucEmailAddress_txtEmailAddressConfirmation"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Next"
                ],
                [
                    "#MainContent_btnContinue"
                ],
                [
                    "xpath///*[@id=\"MainContent_btnContinue\"]"
                ],
                [
                    "pierce/#MainContent_btnContinue"
                ],
                [
                    "text/Next"
                ]
            ],
            "offsetY": 21,
            "offsetX": 40.1875,
            "assertedEvents": [
                {
                    "type": "navigation",
                    "url": "https://ho.natgenagency.com/ContentPages/ClientInfo",
                    "title": ""
                }
            ]
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Input By"
                ],
                [
                    "#MainContent_ucGeneralInformation_ddlInputBy"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucGeneralInformation_ddlInputBy\"]"
                ],
                [
                    "pierce/#MainContent_ucGeneralInformation_ddlInputBy"
                ]
            ],
            "offsetY": 16,
            "offsetX": 93.375
        },
        {
            "type": "change",
            "value": "20050264",
            "selectors": [
                [
                    "aria/Input By"
                ],
                [
                    "#MainContent_ucGeneralInformation_ddlInputBy"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucGeneralInformation_ddlInputBy\"]"
                ],
                [
                    "pierce/#MainContent_ucGeneralInformation_ddlInputBy"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Next"
                ],
                [
                    "#MainContent_btnContinue"
                ],
                [
                    "xpath///*[@id=\"MainContent_btnContinue\"]"
                ],
                [
                    "pierce/#MainContent_btnContinue"
                ]
            ],
            "offsetY": 2,
            "offsetX": 13.6875,
            "assertedEvents": [
                {
                    "type": "navigation",
                    "url": "https://ho.natgenagency.com/ContentPages/Prefill",
                    "title": ""
                }
            ]
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Select an option -- Select --",
                    "aria/[role=\"combobox\"]"
                ],
                [
                    "tr:nth-of-type(3) select"
                ],
                [
                    "xpath///*[@id=\"ddlDriverStatus\"]"
                ],
                [
                    "pierce/tr:nth-of-type(3) select"
                ],
                [
                    "text/-1"
                ]
            ],
            "offsetY": 14,
            "offsetX": 52.46875
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Select an option -- Select --",
                    "aria/[role=\"combobox\"]"
                ],
                [
                    "tr:nth-of-type(3) select"
                ],
                [
                    "xpath///*[@id=\"ddlDriverStatus\"]"
                ],
                [
                    "pierce/tr:nth-of-type(3) select"
                ],
                [
                    "text/-1"
                ]
            ],
            "offsetY": 0,
            "offsetX": 33.46875
        },
        {
            "type": "change",
            "value": "R",
            "selectors": [
                [
                    "aria/Select an option Reject",
                    "aria/[role=\"combobox\"]"
                ],
                [
                    "tr:nth-of-type(3) select"
                ],
                [
                    "xpath///*[@id=\"ddlDriverStatus\"]"
                ],
                [
                    "pierce/tr:nth-of-type(3) select"
                ],
                [
                    "text/-1"
                ]
            ],
            "target": "main",
            "assertedEvents": [
                {
                    "type": "navigation",
                    "url": "https://ho.natgenagency.com/ContentPages/Prefill",
                    "title": ""
                }
            ]
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "tr:nth-of-type(3) > td:nth-of-type(5) li:nth-of-type(1)"
                ],
                [
                    "xpath///*[@id=\"gvPrefillDriver\"]/tbody/tr[3]/td[5]/ul/li[1]"
                ],
                [
                    "pierce/tr:nth-of-type(3) > td:nth-of-type(5) li:nth-of-type(1)"
                ],
                [
                    "text/Rejection Reason\n "
                ]
            ],
            "offsetY": 61,
            "offsetX": 121.890625
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Rejection Reason[role=\"combobox\"]"
                ],
                [
                    "#ddlRejectionReason"
                ],
                [
                    "xpath///*[@id=\"ddlRejectionReason\"]"
                ],
                [
                    "pierce/#ddlRejectionReason"
                ],
                [
                    "text/-1"
                ]
            ],
            "offsetY": 17,
            "offsetX": 98.953125
        },
        {
            "type": "change",
            "value": "Not household member",
            "selectors": [
                [
                    "aria/Rejection Reason[role=\"combobox\"]"
                ],
                [
                    "#ddlRejectionReason"
                ],
                [
                    "xpath///*[@id=\"ddlRejectionReason\"]"
                ],
                [
                    "pierce/#ddlRejectionReason"
                ],
                [
                    "text/-1"
                ]
            ],
            "target": "main",
            "assertedEvents": [
                {
                    "type": "navigation",
                    "url": "https://ho.natgenagency.com/ContentPages/Prefill",
                    "title": ""
                }
            ]
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Comment"
                ],
                [
                    "#txtRejectionAddInfo"
                ],
                [
                    "xpath///*[@id=\"txtRejectionAddInfo\"]"
                ],
                [
                    "pierce/#txtRejectionAddInfo"
                ]
            ],
            "offsetY": 12,
            "offsetX": 40.953125
        },
        {
            "type": "change",
            "value": "Not in the house",
            "selectors": [
                [
                    "aria/Comment"
                ],
                [
                    "#txtRejectionAddInfo"
                ],
                [
                    "xpath///*[@id=\"txtRejectionAddInfo\"]"
                ],
                [
                    "pierce/#txtRejectionAddInfo"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "tr:nth-of-type(2) > td:nth-of-type(4) input"
                ],
                [
                    "xpath///*[@id=\"rbAccept\"]"
                ],
                [
                    "pierce/tr:nth-of-type(2) > td:nth-of-type(4) input"
                ]
            ],
            "offsetY": 12,
            "offsetX": 10.265625
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "tr:nth-of-type(3) > td:nth-of-type(4) input"
                ],
                [
                    "xpath///*[@id=\"rbAccept\"]"
                ],
                [
                    "pierce/tr:nth-of-type(3) > td:nth-of-type(4) input"
                ]
            ],
            "offsetY": 9,
            "offsetX": 5.265625
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Next"
                ],
                [
                    "#MainContent_btnContinue"
                ],
                [
                    "xpath///*[@id=\"MainContent_btnContinue\"]"
                ],
                [
                    "pierce/#MainContent_btnContinue"
                ],
                [
                    "text/Next"
                ]
            ],
            "offsetY": 8,
            "offsetX": 30.1875,
            "assertedEvents": [
                {
                    "type": "navigation",
                    "url": "https://ho.natgenagency.com/ContentPages/PropertyInfo",
                    "title": ""
                }
            ]
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "#MainContent_pnlDwellingInfoLeftCol > ul"
                ],
                [
                    "xpath///*[@id=\"MainContent_pnlDwellingInfoLeftCol\"]/ul"
                ],
                [
                    "pierce/#MainContent_pnlDwellingInfoLeftCol > ul"
                ]
            ],
            "offsetY": 90,
            "offsetX": 302.75
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Residence Class"
                ],
                [
                    "#MainContent_ddlResidenceClass"
                ],
                [
                    "xpath///*[@id=\"MainContent_ddlResidenceClass\"]"
                ],
                [
                    "pierce/#MainContent_ddlResidenceClass"
                ]
            ],
            "offsetY": 9,
            "offsetX": 87.75
        },
        {
            "type": "change",
            "value": "Primary",
            "selectors": [
                [
                    "aria/Residence Class"
                ],
                [
                    "#MainContent_ddlResidenceClass"
                ],
                [
                    "xpath///*[@id=\"MainContent_ddlResidenceClass\"]"
                ],
                [
                    "pierce/#MainContent_ddlResidenceClass"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "#MainContent_ddlOccupancy2"
                ],
                [
                    "xpath///*[@id=\"MainContent_ddlOccupancy2\"]"
                ],
                [
                    "pierce/#MainContent_ddlOccupancy2"
                ]
            ],
            "offsetY": 11,
            "offsetX": 68.75
        },
        {
            "type": "change",
            "value": "OwnerOccupied",
            "selectors": [
                [
                    "#MainContent_ddlOccupancy2"
                ],
                [
                    "xpath///*[@id=\"MainContent_ddlOccupancy2\"]"
                ],
                [
                    "pierce/#MainContent_ddlOccupancy2"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Structure"
                ],
                [
                    "#MainContent_ddlType"
                ],
                [
                    "xpath///*[@id=\"MainContent_ddlType\"]"
                ],
                [
                    "pierce/#MainContent_ddlType"
                ]
            ],
            "offsetY": 17,
            "offsetX": 63.75
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Named Insured Type"
                ],
                [
                    "#MainContent_ddlNamedInsuredType"
                ],
                [
                    "xpath///*[@id=\"MainContent_ddlNamedInsuredType\"]"
                ],
                [
                    "pierce/#MainContent_ddlNamedInsuredType"
                ]
            ],
            "offsetY": 10,
            "offsetX": 39
        },
        {
            "type": "change",
            "value": "Owner",
            "selectors": [
                [
                    "aria/Named Insured Type"
                ],
                [
                    "#MainContent_ddlNamedInsuredType"
                ],
                [
                    "xpath///*[@id=\"MainContent_ddlNamedInsuredType\"]"
                ],
                [
                    "pierce/#MainContent_ddlNamedInsuredType"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Calendar/Date Picker"
                ],
                [
                    "fieldset > ul:nth-of-type(1) img"
                ],
                [
                    "xpath///*[@id=\"MainContent_liDatePurchased\"]/img"
                ],
                [
                    "pierce/fieldset > ul:nth-of-type(1) img"
                ]
            ],
            "offsetY": 5,
            "offsetX": 14
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Change the month"
                ],
                [
                    "div.datepick-popup select:nth-of-type(1)"
                ],
                [
                    "xpath//html/body/div[5]/div/div[2]/div/div/select[1]"
                ],
                [
                    "pierce/div.datepick-popup select:nth-of-type(1)"
                ]
            ],
            "offsetY": 5.421875,
            "offsetX": 12.015625
        },
        {
            "type": "change",
            "value": "1/2026",
            "selectors": [
                [
                    "aria/Change the month"
                ],
                [
                    "div.datepick-popup select:nth-of-type(1)"
                ],
                [
                    "xpath//html/body/div[5]/div/div[2]/div/div/select[1]"
                ],
                [
                    "pierce/div.datepick-popup select:nth-of-type(1)"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/13[role=\"link\"]"
                ],
                [
                    "tr:nth-of-type(3) > td:nth-of-type(3) > a"
                ],
                [
                    "xpath//html/body/div[5]/div/div[2]/div/table/tbody/tr[3]/td[3]/a"
                ],
                [
                    "pierce/tr:nth-of-type(3) > td:nth-of-type(3) > a"
                ]
            ],
            "offsetY": 7.171875,
            "offsetX": 18.953125
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Roof Type"
                ],
                [
                    "#MainContent_ddlRoofType"
                ],
                [
                    "xpath///*[@id=\"MainContent_ddlRoofType\"]"
                ],
                [
                    "pierce/#MainContent_ddlRoofType"
                ]
            ],
            "offsetY": 17,
            "offsetX": 35
        },
        {
            "type": "change",
            "value": "AS",
            "selectors": [
                [
                    "aria/Roof Type"
                ],
                [
                    "#MainContent_ddlRoofType"
                ],
                [
                    "xpath///*[@id=\"MainContent_ddlRoofType\"]"
                ],
                [
                    "pierce/#MainContent_ddlRoofType"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Primary Heat Type"
                ],
                [
                    "#MainContent_ddlPrimaryHeat"
                ],
                [
                    "xpath///*[@id=\"MainContent_ddlPrimaryHeat\"]"
                ],
                [
                    "pierce/#MainContent_ddlPrimaryHeat"
                ]
            ],
            "offsetY": 15,
            "offsetX": 44
        },
        {
            "type": "change",
            "value": "Electric",
            "selectors": [
                [
                    "aria/Primary Heat Type"
                ],
                [
                    "#MainContent_ddlPrimaryHeat"
                ],
                [
                    "xpath///*[@id=\"MainContent_ddlPrimaryHeat\"]"
                ],
                [
                    "pierce/#MainContent_ddlPrimaryHeat"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Roof Shape"
                ],
                [
                    "#MainContent_ddlRoofShape"
                ],
                [
                    "xpath///*[@id=\"MainContent_ddlRoofShape\"]"
                ],
                [
                    "pierce/#MainContent_ddlRoofShape"
                ]
            ],
            "offsetY": 12,
            "offsetX": 41
        },
        {
            "type": "change",
            "value": "Gable, Slight Pitch",
            "selectors": [
                [
                    "aria/Roof Shape"
                ],
                [
                    "#MainContent_ddlRoofShape"
                ],
                [
                    "xpath///*[@id=\"MainContent_ddlRoofShape\"]"
                ],
                [
                    "pierce/#MainContent_ddlRoofShape"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Is Roof Hail Resistant?"
                ],
                [
                    "#MainContent_ddlRoofHail"
                ],
                [
                    "xpath///*[@id=\"MainContent_ddlRoofHail\"]"
                ],
                [
                    "pierce/#MainContent_ddlRoofHail"
                ]
            ],
            "offsetY": 11,
            "offsetX": 48
        },
        {
            "type": "change",
            "value": "False",
            "selectors": [
                [
                    "aria/Is Roof Hail Resistant?"
                ],
                [
                    "#MainContent_ddlRoofHail"
                ],
                [
                    "xpath///*[@id=\"MainContent_ddlRoofHail\"]"
                ],
                [
                    "pierce/#MainContent_ddlRoofHail"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Year of Complete Roof Replacement"
                ],
                [
                    "#MainContent_txtYearRoofRenovation"
                ],
                [
                    "xpath///*[@id=\"MainContent_txtYearRoofRenovation\"]"
                ],
                [
                    "pierce/#MainContent_txtYearRoofRenovation"
                ]
            ],
            "offsetY": 15,
            "offsetX": 47
        },
        {
            "type": "change",
            "value": "2020",
            "selectors": [
                [
                    "aria/Year of Complete Roof Replacement"
                ],
                [
                    "#MainContent_txtYearRoofRenovation"
                ],
                [
                    "xpath///*[@id=\"MainContent_txtYearRoofRenovation\"]"
                ],
                [
                    "pierce/#MainContent_txtYearRoofRenovation"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Oil Tank on Premises"
                ],
                [
                    "#MainContent_ddlOilTank"
                ],
                [
                    "xpath///*[@id=\"MainContent_ddlOilTank\"]"
                ],
                [
                    "pierce/#MainContent_ddlOilTank"
                ]
            ],
            "offsetY": 22,
            "offsetX": 19
        },
        {
            "type": "change",
            "value": "None",
            "selectors": [
                [
                    "aria/Oil Tank on Premises"
                ],
                [
                    "#MainContent_ddlOilTank"
                ],
                [
                    "xpath///*[@id=\"MainContent_ddlOilTank\"]"
                ],
                [
                    "pierce/#MainContent_ddlOilTank"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Next"
                ],
                [
                    "#MainContent_btnContinue"
                ],
                [
                    "xpath///*[@id=\"MainContent_btnContinue\"]"
                ],
                [
                    "pierce/#MainContent_btnContinue"
                ],
                [
                    "text/Next"
                ]
            ],
            "offsetY": 3,
            "offsetX": 24.6875,
            "assertedEvents": [
                {
                    "type": "navigation",
                    "url": "https://ho.natgenagency.com/ContentPages/Underwriting",
                    "title": ""
                }
            ]
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Prior Insurance"
                ],
                [
                    "#MainContent_ucPriorPolicyInformation_ddlPriorInsuranceCoverage"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucPriorPolicyInformation_ddlPriorInsuranceCoverage\"]"
                ],
                [
                    "pierce/#MainContent_ucPriorPolicyInformation_ddlPriorInsuranceCoverage"
                ]
            ],
            "offsetY": 4,
            "offsetX": 70.5
        },
        {
            "type": "change",
            "value": "Prior standard insurance",
            "selectors": [
                [
                    "aria/Prior Insurance"
                ],
                [
                    "#MainContent_ucPriorPolicyInformation_ddlPriorInsuranceCoverage"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucPriorPolicyInformation_ddlPriorInsuranceCoverage\"]"
                ],
                [
                    "pierce/#MainContent_ucPriorPolicyInformation_ddlPriorInsuranceCoverage"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Prior Carrier"
                ],
                [
                    "#MainContent_ucPriorPolicyInformation_ddlPriorInsuranceCompany"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucPriorPolicyInformation_ddlPriorInsuranceCompany\"]"
                ],
                [
                    "pierce/#MainContent_ucPriorPolicyInformation_ddlPriorInsuranceCompany"
                ]
            ],
            "offsetY": 11,
            "offsetX": 60.5
        },
        {
            "type": "change",
            "value": "Allstate Ins Co",
            "selectors": [
                [
                    "aria/Prior Carrier"
                ],
                [
                    "#MainContent_ucPriorPolicyInformation_ddlPriorInsuranceCompany"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucPriorPolicyInformation_ddlPriorInsuranceCompany\"]"
                ],
                [
                    "pierce/#MainContent_ucPriorPolicyInformation_ddlPriorInsuranceCompany"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Expiration Date"
                ],
                [
                    "#MainContent_ucPriorPolicyInformation_txtExpirationDate"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucPriorPolicyInformation_txtExpirationDate\"]"
                ],
                [
                    "pierce/#MainContent_ucPriorPolicyInformation_txtExpirationDate"
                ]
            ],
            "offsetY": 10,
            "offsetX": 60.5
        },
        {
            "type": "change",
            "value": "3/31/2026",
            "selectors": [
                [
                    "aria/Expiration Date"
                ],
                [
                    "#MainContent_ucPriorPolicyInformation_txtExpirationDate"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucPriorPolicyInformation_txtExpirationDate\"]"
                ],
                [
                    "pierce/#MainContent_ucPriorPolicyInformation_txtExpirationDate"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Years of Continuous Property Insurance"
                ],
                [
                    "#MainContent_ucPriorPolicyInformation_txtContinuousInsurance"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucPriorPolicyInformation_txtContinuousInsurance\"]"
                ],
                [
                    "pierce/#MainContent_ucPriorPolicyInformation_txtContinuousInsurance"
                ]
            ],
            "offsetY": 14,
            "offsetX": 122.5
        },
        {
            "type": "change",
            "value": "1",
            "selectors": [
                [
                    "aria/Years of Continuous Property Insurance"
                ],
                [
                    "#MainContent_ucPriorPolicyInformation_txtContinuousInsurance"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucPriorPolicyInformation_txtContinuousInsurance\"]"
                ],
                [
                    "pierce/#MainContent_ucPriorPolicyInformation_txtContinuousInsurance"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "#layout2v2"
                ],
                [
                    "xpath///*[@id=\"layout2v2\"]"
                ],
                [
                    "pierce/#layout2v2"
                ]
            ],
            "offsetY": 153,
            "offsetX": 700.5
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "#MainContent_ucPriorPolicyInformation_pnlPriorPolicyInformationRightCol > ul"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucPriorPolicyInformation_pnlPriorPolicyInformationRightCol\"]/ul"
                ],
                [
                    "pierce/#MainContent_ucPriorPolicyInformation_pnlPriorPolicyInformationRightCol > ul"
                ]
            ],
            "offsetY": 38,
            "offsetX": 176.5,
            "duration": 500.90000000596046
        },
        {
            "type": "change",
            "value": "4",
            "selectors": [
                [
                    "aria/Years of Continuous Property Insurance"
                ],
                [
                    "#MainContent_ucPriorPolicyInformation_txtContinuousInsurance"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucPriorPolicyInformation_txtContinuousInsurance\"]"
                ],
                [
                    "pierce/#MainContent_ucPriorPolicyInformation_txtContinuousInsurance"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "#MainContent_ucPriorPolicyInformation_pnlPriorPolicyInformationRightCol > ul"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucPriorPolicyInformation_pnlPriorPolicyInformationRightCol\"]/ul"
                ],
                [
                    "pierce/#MainContent_ucPriorPolicyInformation_pnlPriorPolicyInformationRightCol > ul"
                ]
            ],
            "offsetY": 41,
            "offsetX": 157.5
        },
        {
            "type": "change",
            "value": "1",
            "selectors": [
                [
                    "aria/Years of Continuous Property Insurance"
                ],
                [
                    "#MainContent_ucPriorPolicyInformation_txtContinuousInsurance"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucPriorPolicyInformation_txtContinuousInsurance\"]"
                ],
                [
                    "pierce/#MainContent_ucPriorPolicyInformation_txtContinuousInsurance"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "#layout2v2"
                ],
                [
                    "xpath///*[@id=\"layout2v2\"]"
                ],
                [
                    "pierce/#layout2v2"
                ]
            ],
            "offsetY": 761,
            "offsetX": 850.5
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Next"
                ],
                [
                    "#MainContent_btnContinue"
                ],
                [
                    "xpath///*[@id=\"MainContent_btnContinue\"]"
                ],
                [
                    "pierce/#MainContent_btnContinue"
                ],
                [
                    "text/Next"
                ]
            ],
            "offsetY": 7,
            "offsetX": 28.1875,
            "assertedEvents": [
                {
                    "type": "navigation",
                    "url": "https://ho.natgenagency.com/ContentPages/LossHistory",
                    "title": ""
                }
            ]
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Next"
                ],
                [
                    "#MainContent_btnContinue"
                ],
                [
                    "xpath///*[@id=\"MainContent_btnContinue\"]"
                ],
                [
                    "pierce/#MainContent_btnContinue"
                ],
                [
                    "text/Next"
                ]
            ],
            "offsetY": 6,
            "offsetX": 32.1875,
            "assertedEvents": [
                {
                    "type": "navigation",
                    "url": "https://ho.natgenagency.com/ContentPages/CoverageInfoD",
                    "title": ""
                }
            ]
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/All Perils Deductible"
                ],
                [
                    "#MainContent_ddlPerils"
                ],
                [
                    "xpath///*[@id=\"MainContent_ddlPerils\"]"
                ],
                [
                    "pierce/#MainContent_ddlPerils"
                ]
            ],
            "offsetY": 19,
            "offsetX": 20
        },
        {
            "type": "change",
            "value": "0.010",
            "selectors": [
                [
                    "aria/All Perils Deductible"
                ],
                [
                    "#MainContent_ddlPerils"
                ],
                [
                    "xpath///*[@id=\"MainContent_ddlPerils\"]"
                ],
                [
                    "pierce/#MainContent_ddlPerils"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Named Storm Deductible"
                ],
                [
                    "#MainContent_ddlNamedStormDeductible"
                ],
                [
                    "xpath///*[@id=\"MainContent_ddlNamedStormDeductible\"]"
                ],
                [
                    "pierce/#MainContent_ddlNamedStormDeductible"
                ]
            ],
            "offsetY": 12,
            "offsetX": 40
        },
        {
            "type": "change",
            "value": "0.020",
            "selectors": [
                [
                    "aria/Named Storm Deductible"
                ],
                [
                    "#MainContent_ddlNamedStormDeductible"
                ],
                [
                    "xpath///*[@id=\"MainContent_ddlNamedStormDeductible\"]"
                ],
                [
                    "pierce/#MainContent_ddlNamedStormDeductible"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Windstorm or Hail Deductible"
                ],
                [
                    "#MainContent_ddlWindstormDeductible"
                ],
                [
                    "xpath///*[@id=\"MainContent_ddlWindstormDeductible\"]"
                ],
                [
                    "pierce/#MainContent_ddlWindstormDeductible"
                ]
            ],
            "offsetY": 12,
            "offsetX": 37
        },
        {
            "type": "change",
            "value": "0.020",
            "selectors": [
                [
                    "aria/Windstorm or Hail Deductible"
                ],
                [
                    "#MainContent_ddlWindstormDeductible"
                ],
                [
                    "xpath///*[@id=\"MainContent_ddlWindstormDeductible\"]"
                ],
                [
                    "pierce/#MainContent_ddlWindstormDeductible"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Coverages"
                ],
                [
                    "#MainContent_pnlCoverage fieldset"
                ],
                [
                    "xpath///*[@id=\"MainContent_pnlCoverageTop\"]/fieldset"
                ],
                [
                    "pierce/#MainContent_pnlCoverage fieldset"
                ]
            ],
            "offsetY": 335,
            "offsetX": 642
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Next"
                ],
                [
                    "#MainContent_btnContinue"
                ],
                [
                    "xpath///*[@id=\"MainContent_btnContinue\"]"
                ],
                [
                    "pierce/#MainContent_btnContinue"
                ],
                [
                    "text/Next"
                ]
            ],
            "offsetY": 8,
            "offsetX": 32.6875,
            "assertedEvents": [
                {
                    "type": "navigation",
                    "url": "https://ho.natgenagency.com/ContentPages/DriverInfo",
                    "title": ""
                }
            ]
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Have you had any losses in the past 5 years?"
                ],
                [
                    "#MainContent_ddlLossesIn5Years"
                ],
                [
                    "xpath///*[@id=\"MainContent_ddlLossesIn5Years\"]"
                ],
                [
                    "pierce/#MainContent_ddlLossesIn5Years"
                ]
            ],
            "offsetY": 10,
            "offsetX": 63
        },
        {
            "type": "change",
            "value": "False",
            "selectors": [
                [
                    "aria/Have you had any losses in the past 5 years?"
                ],
                [
                    "#MainContent_ddlLossesIn5Years"
                ],
                [
                    "xpath///*[@id=\"MainContent_ddlLossesIn5Years\"]"
                ],
                [
                    "pierce/#MainContent_ddlLossesIn5Years"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Have you completed a Defensive Driver Course?"
                ],
                [
                    "#MainContent_ddlDefensiveDriverCourse"
                ],
                [
                    "xpath///*[@id=\"MainContent_ddlDefensiveDriverCourse\"]"
                ],
                [
                    "pierce/#MainContent_ddlDefensiveDriverCourse"
                ]
            ],
            "offsetY": 12,
            "offsetX": 55
        },
        {
            "type": "change",
            "value": "False",
            "selectors": [
                [
                    "aria/Have you completed a Defensive Driver Course?"
                ],
                [
                    "#MainContent_ddlDefensiveDriverCourse"
                ],
                [
                    "xpath///*[@id=\"MainContent_ddlDefensiveDriverCourse\"]"
                ],
                [
                    "pierce/#MainContent_ddlDefensiveDriverCourse"
                ]
            ],
            "target": "main",
            "assertedEvents": [
                {
                    "type": "navigation",
                    "url": "https://ho.natgenagency.com/ContentPages/DriverInfo",
                    "title": "Driver Information"
                }
            ]
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Program Participant?"
                ],
                [
                    "#MainContent_ddlConnectedDriverOptIn"
                ],
                [
                    "xpath///*[@id=\"MainContent_ddlConnectedDriverOptIn\"]"
                ],
                [
                    "pierce/#MainContent_ddlConnectedDriverOptIn"
                ]
            ],
            "offsetY": 5,
            "offsetX": 32.3125
        },
        {
            "type": "change",
            "value": "False",
            "selectors": [
                [
                    "aria/Program Participant?"
                ],
                [
                    "#MainContent_ddlConnectedDriverOptIn"
                ],
                [
                    "xpath///*[@id=\"MainContent_ddlConnectedDriverOptIn\"]"
                ],
                [
                    "pierce/#MainContent_ddlConnectedDriverOptIn"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Dynamic Drive Program"
                ],
                [
                    "fieldset fieldset"
                ],
                [
                    "xpath///*[@id=\"MainContent_pnlConnectedDriver\"]/fieldset"
                ],
                [
                    "pierce/fieldset fieldset"
                ]
            ],
            "offsetY": 32,
            "offsetX": 294.3125
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "#lblFooterServerNode"
                ],
                [
                    "xpath///*[@id=\"lblFooterServerNode\"]"
                ],
                [
                    "pierce/#lblFooterServerNode"
                ],
                [
                    "text/Node PWV01"
                ]
            ],
            "offsetY": 0,
            "offsetX": 628.5
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Drivers License Status:"
                ],
                [
                    "#MainContent_ddlDriversLicenseStatus"
                ],
                [
                    "xpath///*[@id=\"MainContent_ddlDriversLicenseStatus\"]"
                ],
                [
                    "pierce/#MainContent_ddlDriversLicenseStatus"
                ]
            ],
            "offsetY": 6,
            "offsetX": 44
        },
        {
            "type": "change",
            "value": "Active",
            "selectors": [
                [
                    "aria/Drivers License Status:"
                ],
                [
                    "#MainContent_ddlDriversLicenseStatus"
                ],
                [
                    "xpath///*[@id=\"MainContent_ddlDriversLicenseStatus\"]"
                ],
                [
                    "pierce/#MainContent_ddlDriversLicenseStatus"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "#MainContent_lblLossesIn5Years"
                ],
                [
                    "xpath///*[@id=\"MainContent_lblLossesIn5Years\"]"
                ],
                [
                    "pierce/#MainContent_lblLossesIn5Years"
                ],
                [
                    "text/Have you had"
                ]
            ],
            "offsetY": 25,
            "offsetX": 95
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Save"
                ],
                [
                    "#MainContent_btnSaveDriver"
                ],
                [
                    "xpath///*[@id=\"MainContent_btnSaveDriver\"]"
                ],
                [
                    "pierce/#MainContent_btnSaveDriver"
                ]
            ],
            "offsetY": 10,
            "offsetX": 30.359375,
            "assertedEvents": [
                {
                    "type": "navigation",
                    "url": "https://ho.natgenagency.com/ContentPages/DriverInfo",
                    "title": "Driver Information"
                }
            ]
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Dynamic Drive Program Terms and Conditions",
                    "aria/[role=\"paragraph\"]"
                ],
                [
                    "#contentBlock p"
                ],
                [
                    "xpath///*[@id=\"MainContent_dvConnectedDriverAgreement\"]/p"
                ],
                [
                    "pierce/#contentBlock p"
                ]
            ],
            "offsetY": 38,
            "offsetX": 529.25
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Dynamic Drive Program Terms and Conditions"
                ],
                [
                    "fieldset"
                ],
                [
                    "xpath///*[@id=\"MainContent_pnlConnectedDriverAgreement\"]/fieldset"
                ],
                [
                    "pierce/fieldset"
                ]
            ],
            "offsetY": 145,
            "offsetX": 245.25
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Check box to note that the above Terms and Conditions have been read and agreed to by the Insured."
                ],
                [
                    "#MainContent_chkConnectedDriverAgreement"
                ],
                [
                    "xpath///*[@id=\"MainContent_chkConnectedDriverAgreement\"]"
                ],
                [
                    "pierce/#MainContent_chkConnectedDriverAgreement"
                ]
            ],
            "offsetY": 9,
            "offsetX": 7,
            "assertedEvents": [
                {
                    "type": "navigation",
                    "url": "https://ho.natgenagency.com/ContentPages/DriverInfo",
                    "title": ""
                }
            ]
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "#contentBlock"
                ],
                [
                    "xpath///*[@id=\"contentBlock\"]"
                ],
                [
                    "pierce/#contentBlock"
                ]
            ],
            "offsetY": 362,
            "offsetX": 874.5
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Next"
                ],
                [
                    "#MainContent_btnContinue"
                ],
                [
                    "xpath///*[@id=\"MainContent_btnContinue\"]"
                ],
                [
                    "pierce/#MainContent_btnContinue"
                ],
                [
                    "text/Next"
                ]
            ],
            "offsetY": 17,
            "offsetX": 29.1875,
            "assertedEvents": [
                {
                    "type": "navigation",
                    "url": "https://ho.natgenagency.com/ContentPages/DriverViolations",
                    "title": ""
                }
            ]
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Next"
                ],
                [
                    "#MainContent_btnContinue"
                ],
                [
                    "xpath///*[@id=\"MainContent_btnContinue\"]"
                ],
                [
                    "pierce/#MainContent_btnContinue"
                ],
                [
                    "text/Next"
                ]
            ],
            "offsetY": 6,
            "offsetX": 15.1875,
            "assertedEvents": [
                {
                    "type": "navigation",
                    "url": "https://ho.natgenagency.com/ContentPages/VehicleInfo",
                    "title": ""
                }
            ]
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Uninsured/Underinsured Motorist Property Damage - Reduced By[role=\"combobox\"]"
                ],
                [
                    "#MainContent_ucAutoPolicy_rptCoreCoverages_ddlCovUMUIMPD_7"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucAutoPolicy_rptCoreCoverages_ddlCovUMUIMPD_7\"]"
                ],
                [
                    "pierce/#MainContent_ucAutoPolicy_rptCoreCoverages_ddlCovUMUIMPD_7"
                ]
            ],
            "offsetY": 12,
            "offsetX": 64.6875
        },
        {
            "type": "change",
            "value": "25000/250",
            "selectors": [
                [
                    "aria/Uninsured/Underinsured Motorist Property Damage - Reduced By[role=\"combobox\"]"
                ],
                [
                    "#MainContent_ucAutoPolicy_rptCoreCoverages_ddlCovUMUIMPD_7"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucAutoPolicy_rptCoreCoverages_ddlCovUMUIMPD_7\"]"
                ],
                [
                    "pierce/#MainContent_ucAutoPolicy_rptCoreCoverages_ddlCovUMUIMPD_7"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Next"
                ],
                [
                    "#MainContent_btnContinue"
                ],
                [
                    "xpath///*[@id=\"MainContent_btnContinue\"]"
                ],
                [
                    "pierce/#MainContent_btnContinue"
                ],
                [
                    "text/Next"
                ]
            ],
            "offsetY": 10,
            "offsetX": 21.1875,
            "assertedEvents": [
                {
                    "type": "navigation",
                    "url": "https://ho.natgenagency.com/ContentPages/VehicleInfo",
                    "title": ""
                }
            ]
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "#MainContent_rpVehicles_btnViewEdit_0"
                ],
                [
                    "xpath///*[@id=\"MainContent_rpVehicles_btnViewEdit_0\"]"
                ],
                [
                    "pierce/#MainContent_rpVehicles_btnViewEdit_0"
                ]
            ],
            "offsetY": 8,
            "offsetX": 21.609375,
            "assertedEvents": [
                {
                    "type": "navigation",
                    "url": "https://ho.natgenagency.com/ContentPages/VehicleInfo",
                    "title": "Vehicle Information"
                }
            ]
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Is vehicle ever rented/leased to others for a fee?"
                ],
                [
                    "#MainContent_ucVehicleInfo_ddlAutoRentedToOthers"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucVehicleInfo_ddlAutoRentedToOthers\"]"
                ],
                [
                    "pierce/#MainContent_ucVehicleInfo_ddlAutoRentedToOthers"
                ]
            ],
            "offsetY": 11,
            "offsetX": 72.25
        },
        {
            "type": "change",
            "value": "False",
            "selectors": [
                [
                    "aria/Is vehicle ever rented/leased to others for a fee?"
                ],
                [
                    "#MainContent_ucVehicleInfo_ddlAutoRentedToOthers"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucVehicleInfo_ddlAutoRentedToOthers\"]"
                ],
                [
                    "pierce/#MainContent_ucVehicleInfo_ddlAutoRentedToOthers"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Is the vehicle weight between 14k -16k and used to service a farm/residence premises?"
                ],
                [
                    "#MainContent_ucVehicleInfo_ddlVehicleWeightBetween14kAnd16k"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucVehicleInfo_ddlVehicleWeightBetween14kAnd16k\"]"
                ],
                [
                    "pierce/#MainContent_ucVehicleInfo_ddlVehicleWeightBetween14kAnd16k"
                ]
            ],
            "offsetY": 9,
            "offsetX": 66.25
        },
        {
            "type": "change",
            "value": "False",
            "selectors": [
                [
                    "aria/Is the vehicle weight between 14k -16k and used to service a farm/residence premises?"
                ],
                [
                    "#MainContent_ucVehicleInfo_ddlVehicleWeightBetween14kAnd16k"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucVehicleInfo_ddlVehicleWeightBetween14kAnd16k\"]"
                ],
                [
                    "pierce/#MainContent_ucVehicleInfo_ddlVehicleWeightBetween14kAnd16k"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Is a Camper Unit included with this vehicle?"
                ],
                [
                    "#MainContent_ucVehicleInfo_ddlCamperIncluded"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucVehicleInfo_ddlCamperIncluded\"]"
                ],
                [
                    "pierce/#MainContent_ucVehicleInfo_ddlCamperIncluded"
                ]
            ],
            "offsetY": 9,
            "offsetX": 125.25
        },
        {
            "type": "change",
            "value": "False",
            "selectors": [
                [
                    "aria/Is a Camper Unit included with this vehicle?"
                ],
                [
                    "#MainContent_ucVehicleInfo_ddlCamperIncluded"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucVehicleInfo_ddlCamperIncluded\"]"
                ],
                [
                    "pierce/#MainContent_ucVehicleInfo_ddlCamperIncluded"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "#MainContent_ucVehicleInfo_dvVehicleInput"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucVehicleInfo_dvVehicleInput\"]"
                ],
                [
                    "pierce/#MainContent_ucVehicleInfo_dvVehicleInput"
                ]
            ],
            "offsetY": 343,
            "offsetX": 535.25
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "ul:nth-of-type(6)"
                ],
                [
                    "xpath///*[@id=\"ulWrapper\"]/ul[6]"
                ],
                [
                    "pierce/ul:nth-of-type(6)"
                ]
            ],
            "offsetY": 100,
            "offsetX": 441.25
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Annual Mileage"
                ],
                [
                    "#MainContent_ucVehicleInfo_txtAnnualMileage"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucVehicleInfo_txtAnnualMileage\"]"
                ],
                [
                    "pierce/#MainContent_ucVehicleInfo_txtAnnualMileage"
                ]
            ],
            "offsetY": 2,
            "offsetX": 135.25
        },
        {
            "type": "change",
            "value": "10000",
            "selectors": [
                [
                    "aria/Annual Mileage"
                ],
                [
                    "#MainContent_ucVehicleInfo_txtAnnualMileage"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucVehicleInfo_txtAnnualMileage\"]"
                ],
                [
                    "pierce/#MainContent_ucVehicleInfo_txtAnnualMileage"
                ]
            ],
            "target": "main",
            "assertedEvents": [
                {
                    "type": "navigation",
                    "url": "https://ho.natgenagency.com/ContentPages/VehicleInfo",
                    "title": ""
                }
            ]
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Save Vehicle"
                ],
                [
                    "#MainContent_btnSaveVehicle"
                ],
                [
                    "xpath///*[@id=\"MainContent_btnSaveVehicle\"]"
                ],
                [
                    "pierce/#MainContent_btnSaveVehicle"
                ],
                [
                    "text/Save Vehicle"
                ]
            ],
            "offsetY": 12,
            "offsetX": 63.890625,
            "assertedEvents": [
                {
                    "type": "navigation",
                    "url": "https://ho.natgenagency.com/ContentPages/VehicleInfo",
                    "title": ""
                }
            ]
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Is vehicle ever rented/leased to others for a fee?"
                ],
                [
                    "#MainContent_ucVehicleInfo_ddlAutoRentedToOthers"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucVehicleInfo_ddlAutoRentedToOthers\"]"
                ],
                [
                    "pierce/#MainContent_ucVehicleInfo_ddlAutoRentedToOthers"
                ]
            ],
            "offsetY": 0,
            "offsetX": 76.25
        },
        {
            "type": "change",
            "value": "-1",
            "selectors": [
                [
                    "aria/Is vehicle ever rented/leased to others for a fee?"
                ],
                [
                    "#MainContent_ucVehicleInfo_ddlAutoRentedToOthers"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucVehicleInfo_ddlAutoRentedToOthers\"]"
                ],
                [
                    "pierce/#MainContent_ucVehicleInfo_ddlAutoRentedToOthers"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Purchased Date"
                ],
                [
                    "#MainContent_ucVehicleInfo_txtPurchaseDate"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucVehicleInfo_txtPurchaseDate\"]"
                ],
                [
                    "pierce/#MainContent_ucVehicleInfo_txtPurchaseDate"
                ]
            ],
            "offsetY": 11,
            "offsetX": 125.25
        },
        {
            "type": "change",
            "value": "03/01/2024",
            "selectors": [
                [
                    "aria/Purchased Date"
                ],
                [
                    "#MainContent_ucVehicleInfo_txtPurchaseDate"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucVehicleInfo_txtPurchaseDate\"]"
                ],
                [
                    "pierce/#MainContent_ucVehicleInfo_txtPurchaseDate"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Is vehicle ever rented/leased to others for a fee?"
                ],
                [
                    "#MainContent_ucVehicleInfo_ddlAutoRentedToOthers"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucVehicleInfo_ddlAutoRentedToOthers\"]"
                ],
                [
                    "pierce/#MainContent_ucVehicleInfo_ddlAutoRentedToOthers"
                ]
            ],
            "offsetY": 22,
            "offsetX": 42.25
        },
        {
            "type": "change",
            "value": "False",
            "selectors": [
                [
                    "aria/Is vehicle ever rented/leased to others for a fee?"
                ],
                [
                    "#MainContent_ucVehicleInfo_ddlAutoRentedToOthers"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucVehicleInfo_ddlAutoRentedToOthers\"]"
                ],
                [
                    "pierce/#MainContent_ucVehicleInfo_ddlAutoRentedToOthers"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "ul:nth-of-type(6)"
                ],
                [
                    "xpath///*[@id=\"ulWrapper\"]/ul[6]"
                ],
                [
                    "pierce/ul:nth-of-type(6)"
                ]
            ],
            "offsetY": 52,
            "offsetX": 330.25
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Ownership Status"
                ],
                [
                    "#MainContent_ucVehicleInfo_ddlOwnershipStatus"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucVehicleInfo_ddlOwnershipStatus\"]"
                ],
                [
                    "pierce/#MainContent_ucVehicleInfo_ddlOwnershipStatus"
                ]
            ],
            "offsetY": 8,
            "offsetX": 61.25
        },
        {
            "type": "change",
            "value": "3",
            "selectors": [
                [
                    "aria/Ownership Status"
                ],
                [
                    "#MainContent_ucVehicleInfo_ddlOwnershipStatus"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucVehicleInfo_ddlOwnershipStatus\"]"
                ],
                [
                    "pierce/#MainContent_ucVehicleInfo_ddlOwnershipStatus"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Save Vehicle"
                ],
                [
                    "#MainContent_btnSaveVehicle"
                ],
                [
                    "xpath///*[@id=\"MainContent_btnSaveVehicle\"]"
                ],
                [
                    "pierce/#MainContent_btnSaveVehicle"
                ],
                [
                    "text/Save Vehicle"
                ]
            ],
            "offsetY": 13,
            "offsetX": 52.890625,
            "assertedEvents": [
                {
                    "type": "navigation",
                    "url": "https://ho.natgenagency.com/ContentPages/VehicleInfo",
                    "title": ""
                }
            ]
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Comprehensive"
                ],
                [
                    "#MainContent_ucVehicleCoverages_rptOptionalCoverages_ddlCovCOMP_1"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucVehicleCoverages_rptOptionalCoverages_ddlCovCOMP_1\"]"
                ],
                [
                    "pierce/#MainContent_ucVehicleCoverages_rptOptionalCoverages_ddlCovCOMP_1"
                ]
            ],
            "offsetY": 12,
            "offsetX": 77.25
        },
        {
            "type": "change",
            "value": "1000",
            "selectors": [
                [
                    "aria/Comprehensive"
                ],
                [
                    "#MainContent_ucVehicleCoverages_rptOptionalCoverages_ddlCovCOMP_1"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucVehicleCoverages_rptOptionalCoverages_ddlCovCOMP_1\"]"
                ],
                [
                    "pierce/#MainContent_ucVehicleCoverages_rptOptionalCoverages_ddlCovCOMP_1"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Roadside Assistance"
                ],
                [
                    "#MainContent_ucVehicleCoverages_rptOptionalCoverages_ddlCovRoadside_3"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucVehicleCoverages_rptOptionalCoverages_ddlCovRoadside_3\"]"
                ],
                [
                    "pierce/#MainContent_ucVehicleCoverages_rptOptionalCoverages_ddlCovRoadside_3"
                ]
            ],
            "offsetY": 10,
            "offsetX": 11.25
        },
        {
            "type": "change",
            "value": "-1",
            "selectors": [
                [
                    "aria/Roadside Assistance"
                ],
                [
                    "#MainContent_ucVehicleCoverages_rptOptionalCoverages_ddlCovRoadside_3"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucVehicleCoverages_rptOptionalCoverages_ddlCovRoadside_3\"]"
                ],
                [
                    "pierce/#MainContent_ucVehicleCoverages_rptOptionalCoverages_ddlCovRoadside_3"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Customizing Equipment"
                ],
                [
                    "#MainContent_ucVehicleCoverages_rptOptionalCoverages_ddlCovCUST_4"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucVehicleCoverages_rptOptionalCoverages_ddlCovCUST_4\"]"
                ],
                [
                    "pierce/#MainContent_ucVehicleCoverages_rptOptionalCoverages_ddlCovCUST_4"
                ]
            ],
            "offsetY": 4,
            "offsetX": 56.25
        },
        {
            "type": "change",
            "value": "-1",
            "selectors": [
                [
                    "aria/Customizing Equipment"
                ],
                [
                    "#MainContent_ucVehicleCoverages_rptOptionalCoverages_ddlCovCUST_4"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucVehicleCoverages_rptOptionalCoverages_ddlCovCUST_4\"]"
                ],
                [
                    "pierce/#MainContent_ucVehicleCoverages_rptOptionalCoverages_ddlCovCUST_4"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Custom Audio System"
                ],
                [
                    "#MainContent_ucVehicleCoverages_rptOptionalCoverages_ddlCovAV_5"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucVehicleCoverages_rptOptionalCoverages_ddlCovAV_5\"]"
                ],
                [
                    "pierce/#MainContent_ucVehicleCoverages_rptOptionalCoverages_ddlCovAV_5"
                ]
            ],
            "offsetY": 7,
            "offsetX": 63.25
        },
        {
            "type": "change",
            "value": "-1",
            "selectors": [
                [
                    "aria/Custom Audio System"
                ],
                [
                    "#MainContent_ucVehicleCoverages_rptOptionalCoverages_ddlCovAV_5"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucVehicleCoverages_rptOptionalCoverages_ddlCovAV_5\"]"
                ],
                [
                    "pierce/#MainContent_ucVehicleCoverages_rptOptionalCoverages_ddlCovAV_5"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Transportation Expense"
                ],
                [
                    "#MainContent_ucVehicleCoverages_rptOptionalCoverages_ddlCovRENT_6"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucVehicleCoverages_rptOptionalCoverages_ddlCovRENT_6\"]"
                ],
                [
                    "pierce/#MainContent_ucVehicleCoverages_rptOptionalCoverages_ddlCovRENT_6"
                ],
                [
                    "text/20/600"
                ]
            ],
            "offsetY": 23,
            "offsetX": 37.25
        },
        {
            "type": "change",
            "value": "-1",
            "selectors": [
                [
                    "aria/Transportation Expense"
                ],
                [
                    "#MainContent_ucVehicleCoverages_rptOptionalCoverages_ddlCovRENT_6"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucVehicleCoverages_rptOptionalCoverages_ddlCovRENT_6\"]"
                ],
                [
                    "pierce/#MainContent_ucVehicleCoverages_rptOptionalCoverages_ddlCovRENT_6"
                ],
                [
                    "text/20/600"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/2020 JEEP WRANGLER UNLIMITED SAHARA Coverages",
                    "aria/[role=\"list\"]"
                ],
                [
                    "#MainContent_pnlVehicleCoverages ul"
                ],
                [
                    "xpath///*[@id=\"MainContent_pnlVehicleCoverages\"]/fieldset/ul"
                ],
                [
                    "pierce/#MainContent_pnlVehicleCoverages ul"
                ]
            ],
            "offsetY": 256,
            "offsetX": 480.25
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Save Vehicle Coverages"
                ],
                [
                    "#MainContent_btnSaveVehicleCov"
                ],
                [
                    "xpath///*[@id=\"MainContent_btnSaveVehicleCov\"]"
                ],
                [
                    "pierce/#MainContent_btnSaveVehicleCov"
                ],
                [
                    "text/Save Vehicle"
                ]
            ],
            "offsetY": 0,
            "offsetX": 66.25,
            "assertedEvents": [
                {
                    "type": "navigation",
                    "url": "https://ho.natgenagency.com/ContentPages/VehicleInfo",
                    "title": ""
                }
            ]
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Next"
                ],
                [
                    "#MainContent_btnContinue"
                ],
                [
                    "xpath///*[@id=\"MainContent_btnContinue\"]"
                ],
                [
                    "pierce/#MainContent_btnContinue"
                ],
                [
                    "text/Next"
                ]
            ],
            "offsetY": 7,
            "offsetX": 15.1875,
            "assertedEvents": [
                {
                    "type": "navigation",
                    "url": "https://ho.natgenagency.com/ContentPages/VehicleInfo",
                    "title": "Vehicle Information"
                }
            ]
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "#MainContent_rpVehicles_btnViewEdit_1"
                ],
                [
                    "xpath///*[@id=\"MainContent_rpVehicles_btnViewEdit_1\"]"
                ],
                [
                    "pierce/#MainContent_rpVehicles_btnViewEdit_1"
                ]
            ],
            "offsetY": 5,
            "offsetX": 35.609375,
            "assertedEvents": [
                {
                    "type": "navigation",
                    "url": "https://ho.natgenagency.com/ContentPages/VehicleInfo",
                    "title": "Vehicle Information"
                }
            ]
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Is vehicle ever rented/leased to others for a fee?"
                ],
                [
                    "#MainContent_ucVehicleInfo_ddlAutoRentedToOthers"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucVehicleInfo_ddlAutoRentedToOthers\"]"
                ],
                [
                    "pierce/#MainContent_ucVehicleInfo_ddlAutoRentedToOthers"
                ]
            ],
            "offsetY": 20,
            "offsetX": 91.25
        },
        {
            "type": "change",
            "value": "False",
            "selectors": [
                [
                    "aria/Is vehicle ever rented/leased to others for a fee?"
                ],
                [
                    "#MainContent_ucVehicleInfo_ddlAutoRentedToOthers"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucVehicleInfo_ddlAutoRentedToOthers\"]"
                ],
                [
                    "pierce/#MainContent_ucVehicleInfo_ddlAutoRentedToOthers"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Is the vehicle weight between 14k -16k and used to service a farm/residence premises?"
                ],
                [
                    "#MainContent_ucVehicleInfo_ddlVehicleWeightBetween14kAnd16k"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucVehicleInfo_ddlVehicleWeightBetween14kAnd16k\"]"
                ],
                [
                    "pierce/#MainContent_ucVehicleInfo_ddlVehicleWeightBetween14kAnd16k"
                ]
            ],
            "offsetY": 6,
            "offsetX": 93.25
        },
        {
            "type": "change",
            "value": "False",
            "selectors": [
                [
                    "aria/Is the vehicle weight between 14k -16k and used to service a farm/residence premises?"
                ],
                [
                    "#MainContent_ucVehicleInfo_ddlVehicleWeightBetween14kAnd16k"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucVehicleInfo_ddlVehicleWeightBetween14kAnd16k\"]"
                ],
                [
                    "pierce/#MainContent_ucVehicleInfo_ddlVehicleWeightBetween14kAnd16k"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Is a Camper Unit included with this vehicle?"
                ],
                [
                    "#MainContent_ucVehicleInfo_ddlCamperIncluded"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucVehicleInfo_ddlCamperIncluded\"]"
                ],
                [
                    "pierce/#MainContent_ucVehicleInfo_ddlCamperIncluded"
                ]
            ],
            "offsetY": 9,
            "offsetX": 61.25
        },
        {
            "type": "change",
            "value": "False",
            "selectors": [
                [
                    "aria/Is a Camper Unit included with this vehicle?"
                ],
                [
                    "#MainContent_ucVehicleInfo_ddlCamperIncluded"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucVehicleInfo_ddlCamperIncluded\"]"
                ],
                [
                    "pierce/#MainContent_ucVehicleInfo_ddlCamperIncluded"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Ownership Status"
                ],
                [
                    "#MainContent_ucVehicleInfo_ddlOwnershipStatus"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucVehicleInfo_ddlOwnershipStatus\"]"
                ],
                [
                    "pierce/#MainContent_ucVehicleInfo_ddlOwnershipStatus"
                ]
            ],
            "offsetY": 19,
            "offsetX": 41.25
        },
        {
            "type": "change",
            "value": "3",
            "selectors": [
                [
                    "aria/Ownership Status"
                ],
                [
                    "#MainContent_ucVehicleInfo_ddlOwnershipStatus"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucVehicleInfo_ddlOwnershipStatus\"]"
                ],
                [
                    "pierce/#MainContent_ucVehicleInfo_ddlOwnershipStatus"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Annual Mileage"
                ],
                [
                    "#MainContent_ucVehicleInfo_txtAnnualMileage"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucVehicleInfo_txtAnnualMileage\"]"
                ],
                [
                    "pierce/#MainContent_ucVehicleInfo_txtAnnualMileage"
                ]
            ],
            "offsetY": 9,
            "offsetX": 53.25
        },
        {
            "type": "change",
            "value": "10000",
            "selectors": [
                [
                    "aria/Annual Mileage"
                ],
                [
                    "#MainContent_ucVehicleInfo_txtAnnualMileage"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucVehicleInfo_txtAnnualMileage\"]"
                ],
                [
                    "pierce/#MainContent_ucVehicleInfo_txtAnnualMileage"
                ]
            ],
            "target": "main",
            "assertedEvents": [
                {
                    "type": "navigation",
                    "url": "https://ho.natgenagency.com/ContentPages/VehicleInfo",
                    "title": "Vehicle Information"
                }
            ]
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Save Vehicle"
                ],
                [
                    "#MainContent_btnSaveVehicle"
                ],
                [
                    "xpath///*[@id=\"MainContent_btnSaveVehicle\"]"
                ],
                [
                    "pierce/#MainContent_btnSaveVehicle"
                ],
                [
                    "text/Save Vehicle"
                ]
            ],
            "offsetY": 11,
            "offsetX": 57.890625,
            "assertedEvents": [
                {
                    "type": "navigation",
                    "url": "https://ho.natgenagency.com/ContentPages/VehicleInfo",
                    "title": "Vehicle Information"
                }
            ]
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Purchased Date"
                ],
                [
                    "#MainContent_ucVehicleInfo_txtPurchaseDate"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucVehicleInfo_txtPurchaseDate\"]"
                ],
                [
                    "pierce/#MainContent_ucVehicleInfo_txtPurchaseDate"
                ]
            ],
            "offsetY": 18,
            "offsetX": 90.25
        },
        {
            "type": "change",
            "value": "03/01/2024",
            "selectors": [
                [
                    "aria/Purchased Date"
                ],
                [
                    "#MainContent_ucVehicleInfo_txtPurchaseDate"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucVehicleInfo_txtPurchaseDate\"]"
                ],
                [
                    "pierce/#MainContent_ucVehicleInfo_txtPurchaseDate"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Save Vehicle"
                ],
                [
                    "#MainContent_btnSaveVehicle"
                ],
                [
                    "xpath///*[@id=\"MainContent_btnSaveVehicle\"]"
                ],
                [
                    "pierce/#MainContent_btnSaveVehicle"
                ],
                [
                    "text/Save Vehicle"
                ]
            ],
            "offsetY": 9,
            "offsetX": 61.890625,
            "assertedEvents": [
                {
                    "type": "navigation",
                    "url": "https://ho.natgenagency.com/ContentPages/VehicleInfo",
                    "title": ""
                }
            ]
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Save Vehicle Coverages"
                ],
                [
                    "#MainContent_btnSaveVehicleCov"
                ],
                [
                    "xpath///*[@id=\"MainContent_btnSaveVehicleCov\"]"
                ],
                [
                    "pierce/#MainContent_btnSaveVehicleCov"
                ],
                [
                    "text/Save Vehicle"
                ]
            ],
            "offsetY": 7,
            "offsetX": 89.25,
            "assertedEvents": [
                {
                    "type": "navigation",
                    "url": "https://ho.natgenagency.com/ContentPages/VehicleInfo",
                    "title": ""
                }
            ]
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Next"
                ],
                [
                    "#MainContent_btnContinue"
                ],
                [
                    "xpath///*[@id=\"MainContent_btnContinue\"]"
                ],
                [
                    "pierce/#MainContent_btnContinue"
                ],
                [
                    "text/Next"
                ]
            ],
            "offsetY": 13,
            "offsetX": 19.1875,
            "assertedEvents": [
                {
                    "type": "navigation",
                    "url": "https://ho.natgenagency.com/ContentPages/AutoUnderwriting",
                    "title": ""
                }
            ]
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/1:  Named Insured Type"
                ],
                [
                    "#MainContent_ucAutoQuestions_rpParentQuestions_ddlAnswer_0"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucAutoQuestions_rpParentQuestions_ddlAnswer_0\"]"
                ],
                [
                    "pierce/#MainContent_ucAutoQuestions_rpParentQuestions_ddlAnswer_0"
                ]
            ],
            "offsetY": 12,
            "offsetX": 28.75
        },
        {
            "type": "change",
            "value": "Individual",
            "selectors": [
                [
                    "aria/1:  Named Insured Type"
                ],
                [
                    "#MainContent_ucAutoQuestions_rpParentQuestions_ddlAnswer_0"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucAutoQuestions_rpParentQuestions_ddlAnswer_0\"]"
                ],
                [
                    "pierce/#MainContent_ucAutoQuestions_rpParentQuestions_ddlAnswer_0"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/3:  Do any operators have a Company car?"
                ],
                [
                    "#MainContent_ucAutoQuestions_rpParentQuestions_ddlAnswer_2"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucAutoQuestions_rpParentQuestions_ddlAnswer_2\"]"
                ],
                [
                    "pierce/#MainContent_ucAutoQuestions_rpParentQuestions_ddlAnswer_2"
                ]
            ],
            "offsetY": 17,
            "offsetX": 38.75
        },
        {
            "type": "change",
            "value": "False",
            "selectors": [
                [
                    "aria/3:  Do any operators have a Company car?"
                ],
                [
                    "#MainContent_ucAutoQuestions_rpParentQuestions_ddlAnswer_2"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucAutoQuestions_rpParentQuestions_ddlAnswer_2\"]"
                ],
                [
                    "pierce/#MainContent_ucAutoQuestions_rpParentQuestions_ddlAnswer_2"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/4:  Have you had any losses in the previous 5 years?"
                ],
                [
                    "#MainContent_ucAutoQuestions_rpParentQuestions_ddlAnswer_3"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucAutoQuestions_rpParentQuestions_ddlAnswer_3\"]"
                ],
                [
                    "pierce/#MainContent_ucAutoQuestions_rpParentQuestions_ddlAnswer_3"
                ]
            ],
            "offsetY": 11,
            "offsetX": 30.75
        },
        {
            "type": "change",
            "value": "False",
            "selectors": [
                [
                    "aria/4:  Have you had any losses in the previous 5 years?"
                ],
                [
                    "#MainContent_ucAutoQuestions_rpParentQuestions_ddlAnswer_3"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucAutoQuestions_rpParentQuestions_ddlAnswer_3\"]"
                ],
                [
                    "pierce/#MainContent_ucAutoQuestions_rpParentQuestions_ddlAnswer_3"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/5:  Years with Prior Auto Carrier"
                ],
                [
                    "#MainContent_ucAutoQuestions_rpParentQuestions_txtAnswer_4"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucAutoQuestions_rpParentQuestions_txtAnswer_4\"]"
                ],
                [
                    "pierce/#MainContent_ucAutoQuestions_rpParentQuestions_txtAnswer_4"
                ]
            ],
            "offsetY": 8,
            "offsetX": 33.75
        },
        {
            "type": "change",
            "value": "1",
            "selectors": [
                [
                    "aria/5:  Years with Prior Auto Carrier"
                ],
                [
                    "#MainContent_ucAutoQuestions_rpParentQuestions_txtAnswer_4"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucAutoQuestions_rpParentQuestions_txtAnswer_4\"]"
                ],
                [
                    "pierce/#MainContent_ucAutoQuestions_rpParentQuestions_txtAnswer_4"
                ]
            ],
            "target": "main"
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Next"
                ],
                [
                    "#MainContent_btnContinue"
                ],
                [
                    "xpath///*[@id=\"MainContent_btnContinue\"]"
                ],
                [
                    "pierce/#MainContent_btnContinue"
                ],
                [
                    "text/Next"
                ]
            ],
            "offsetY": 6,
            "offsetX": 38.1875,
            "assertedEvents": [
                {
                    "type": "navigation",
                    "url": "https://ho.natgenagency.com/ContentPages/PremiumSummary",
                    "title": ""
                }
            ]
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Pay Method"
                ],
                [
                    "#MainContent_ucPaymentMethod_ddlPayMethod"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucPaymentMethod_ddlPayMethod\"]"
                ],
                [
                    "pierce/#MainContent_ucPaymentMethod_ddlPayMethod"
                ]
            ],
            "offsetY": 11,
            "offsetX": 107.375
        },
        {
            "type": "change",
            "value": "AS",
            "selectors": [
                [
                    "aria/Pay Method"
                ],
                [
                    "#MainContent_ucPaymentMethod_ddlPayMethod"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucPaymentMethod_ddlPayMethod\"]"
                ],
                [
                    "pierce/#MainContent_ucPaymentMethod_ddlPayMethod"
                ]
            ],
            "target": "main",
            "assertedEvents": [
                {
                    "type": "navigation",
                    "url": "https://ho.natgenagency.com/ContentPages/PremiumSummary",
                    "title": ""
                }
            ]
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Pay Plan"
                ],
                [
                    "#MainContent_ucPaymentMethod_ddlPayPlan"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucPaymentMethod_ddlPayPlan\"]"
                ],
                [
                    "pierce/#MainContent_ucPaymentMethod_ddlPayPlan"
                ],
                [
                    "text/PIF"
                ]
            ],
            "offsetY": 5,
            "offsetX": 146.375
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Re-Rate"
                ],
                [
                    "#MainContent_ucRater_btnRate"
                ],
                [
                    "xpath///*[@id=\"MainContent_ucRater_btnRate\"]"
                ],
                [
                    "pierce/#MainContent_ucRater_btnRate"
                ],
                [
                    "text/Re-Rate"
                ]
            ],
            "offsetY": 1,
            "offsetX": 52.3125,
            "assertedEvents": [
                {
                    "type": "navigation",
                    "url": "https://ho.natgenagency.com/ContentPages/PremiumSummary",
                    "title": ""
                }
            ]
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Quote Proposal"
                ],
                [
                    "#MainContent_btnQuoteProposal"
                ],
                [
                    "xpath///*[@id=\"MainContent_btnQuoteProposal\"]"
                ],
                [
                    "pierce/#MainContent_btnQuoteProposal"
                ],
                [
                    "text/Quote Proposal"
                ]
            ],
            "offsetY": 12,
            "offsetX": 50,
            "assertedEvents": [
                {
                    "type": "navigation",
                    "url": "https://changelog-widget.canny.io/612f76b3a11bf815b4eb024b?theme",
                    "title": "Premium Summary"
                }
            ]
        },
        {
            "type": "click",
            "target": "https://claude.ai/oauth/authorize?client_id=dae2cad8-15c5-43d2-9046-fcaecc135fa4&response_type=code&scope=user%3Aprofile+user%3Ainference+user%3Achat&redirect_uri=chrome-extension%3A%2F%2Ffcoeoabgfenejglbffodgkkbkcdhcgfn%2Foauth_callback.html&state=GkBd-BhpElde5eD9jIMwviniZP08bbMO2eFER9zFdKU&code_challenge=KSPdyjAAxB8G5HnseVM_Iz2k_e0Ybr_fEvZ_2QlfL_A&code_challenge_method=S256",
            "selectors": [
                [
                    "aria/Authorize"
                ],
                [
                    "button.font-base-bold"
                ],
                [
                    "xpath//html/body/div[2]/div/div[1]/div/div[4]/button[1]"
                ],
                [
                    "pierce/button.font-base-bold"
                ],
                [
                    "text/Authorize"
                ]
            ],
            "offsetY": 11.83001708984375,
            "offsetX": 179.27499389648438,
            "assertedEvents": [
                {
                    "type": "navigation",
                    "url": "https://claude.ai/chrome/installed",
                    "title": ""
                }
            ]
        },
        {
            "type": "click",
            "target": "main",
            "selectors": [
                [
                    "aria/Quote Proposal"
                ],
                [
                    "#MainContent_btnQuoteProposal"
                ],
                [
                    "xpath///*[@id=\"MainContent_btnQuoteProposal\"]"
                ],
                [
                    "pierce/#MainContent_btnQuoteProposal"
                ],
                [
                    "text/Quote Proposal"
                ]
            ],
            "offsetY": 6,
            "offsetX": 39,
            "assertedEvents": [
                {
                    "type": "navigation",
                    "url": "https://ho.natgenagency.com/ContentPages/PremiumSummary",
                    "title": ""
                }
            ]
        },
        {
            "type": "click",
            "target": "chrome-extension://mhjfbmdgcfjbbpaeojofohoefgiehjai/index.html",
            "selectors": [
                [
                    "#viewer",
                    "#toolbar",
                    "#downloads",
                    "#save",
                    "#icon"
                ],
                [
                    "pierce/#downloads",
                    "pierce/#icon"
                ]
            ],
            "offsetY": 19,
            "offsetX": 27
        }
    ]
}
```

**END OF CONTRACTOR BRIEF**

Trustwell Insurance Agency  |  Confidential  |  March 30, 2026
