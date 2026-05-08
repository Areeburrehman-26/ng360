#!/usr/bin/env python3
"""
NG360 Bot - National General PKGProtect2 Bundled Quote Automation
Complete bot implementation using Playwright + FastAPI + GHL Integration
Port: 8004 | State: GA | Product: PKGProtect2 (Home + Auto Bundle)
"""

import os
import json
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, asdict, field
from enum import Enum
import re

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn
from playwright.async_api import async_playwright, Browser, BrowserContext, Page
import httpx

# ============================================================================
# LOGGING CONFIGURATION
# ============================================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ============================================================================
# ENVIRONMENT VARIABLES & CONFIGURATION
# ============================================================================
NATGEN_USERNAME = os.getenv("NATGEN_USERNAME", "trustwell0")
NATGEN_PASSWORD = os.getenv("NATGEN_PASSWORD", "")
NATGEN_AGENT_ID = os.getenv("NATGEN_AGENT_ID", "20050264")
GHL_API_KEY = os.getenv("GHL_API_KEY", "")
GHL_LOCATION_ID = os.getenv("GHL_LOCATION_ID", "")
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "8004"))
GDRIVE_FOLDER_ID = os.getenv("GDRIVE_FOLDER_ID", "")
SLACK_WEBHOOK_QUOTES = os.getenv("SLACK_WEBHOOK_QUOTES", "")
SLACK_WEBHOOK_ALERTS = os.getenv("SLACK_WEBHOOK_ALERTS", "")
PUSHOVER_TOKEN = os.getenv("PUSHOVER_TOKEN", "")
PUSHOVER_USER = os.getenv("PUSHOVER_USER", "")
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_NAME = os.getenv("DB_NAME", "trustwell_bots")
DB_USER = os.getenv("DB_USER", "bot_user")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")

# ============================================================================
# ENUMS & CONSTANTS
# ============================================================================
class JobStatus(str, Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"

class QuoteStatus(str, Enum):
    QUOTED = "Quoted"
    FAILED = "Failed"
    MISSING_DATA = "Missing Data"
    NOT_ELIGIBLE = "Not Eligible"

# Critical Alerts & High Alerts
CRITICAL_ALERTS = {
    "LOGIN_FAILURE": "Login to National General portal failed after retry",
    "SESSION_TIMEOUT": "Session timeout detected mid-flow",
    "PAGE_RELOAD_TIMEOUT": "Vehicle page reload timeout (>60s)",
    "PORTAL_DOWNTIME": "National General portal unreachable (5xx/timeout)",
    "PDF_GENERATION_FAILURE": "Quote proposal PDF generation failed",
    "STUCK_JOB_RESCUE": "Watchdog rescued stuck job (>600s in PROCESSING)",
}

HIGH_ALERTS = {
    "PREFILL_MODAL_UNEXPECTED": "Unexpected prefill modal elements",
    "CALENDAR_PICKER_FAILURE": "Calendar date picker failed",
    "DROPDOWN_VALUE_NOT_FOUND": "Dropdown value not found in options",
    "RATE_REJECTION": "Portal rejected application/not eligible",
}

REQUIRED_GHL_FIELDS = [
    "first_name", "last_name", "zip", "date_of_birth", "gender",
    "marital_status", "occupation", "phone", "address1", "city", "email",
    "vehicles", "num_vehicles"
]

# ============================================================================
# DATA MODELS
# ============================================================================
@dataclass
class Vehicle:
    year: int
    make: str
    model: str
    vin: str
    annual_mileage: int = 10000
    ownership_status: int = 1  # 1=Owned, 2=Financed, 3=Leased
    purchase_date: str = ""

@dataclass
class Contact:
    first_name: str
    last_name: str
    zip: str
    date_of_birth: str  # MMDDYYYY format
    gender: str  # M or F
    marital_status: str
    occupation: str
    phone: str
    address1: str
    city: str
    email: str
    vehicles: List[Vehicle]
    num_vehicles: int
    years_at_residence: int = 5
    prior_carrier_home: Optional[str] = None
    prior_expiration: Optional[str] = None
    years_continuous_ins: int = 1

@dataclass
class QuoteResult:
    contact_id: str
    job_id: str
    status: QuoteStatus
    combined_premium: Optional[float] = None
    home_premium: Optional[float] = None
    auto_premium: Optional[float] = None
    quote_url: Optional[str] = None
    quote_date: Optional[str] = None
    pay_plan: Optional[str] = None
    error_message: Optional[str] = None
    screenshots: List[str] = field(default_factory=list)

@dataclass
class Job:
    job_id: str
    contact_id: str
    contact_data: Dict[str, Any]
    status: JobStatus = JobStatus.PENDING
    retry_count: int = 0
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    result: Optional[QuoteResult] = None

class WebhookPayload(BaseModel):
    contact_id: str
    first_name: str
    last_name: str
    zip: str
    date_of_birth: str
    gender: str
    marital_status: str
    occupation: str
    phone: str
    address1: str
    city: str
    email: str
    vehicles: List[Dict[str, Any]]
    num_vehicles: int
    years_at_residence: Optional[int] = 5
    prior_carrier_home: Optional[str] = None
    prior_expiration: Optional[str] = None
    years_continuous_ins: Optional[int] = 1

# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================
def parse_phone_number(phone: str) -> tuple[str, str, str]:
    """
    Parse phone number into area code, prefix, line.
    Handles various formats: +1-234-567-8900, 234-567-8900, 2345678900, etc.
    
    Returns: (area_code, prefix, line_number)
    Raises ValueError if phone cannot be parsed.
    """
    digits = re.sub(r'\D', '', phone)
    
    if len(digits) == 11 and digits[0] == '1':
        digits = digits[1:]
    
    if len(digits) == 10:
        area_code = digits[0:3]
        prefix = digits[3:6]
        line_number = digits[6:10]
        return (area_code, prefix, line_number)
    
    raise ValueError(f"Invalid phone number format: {phone}")

def format_date_mmddyyyy(date_str: str) -> str:
    """
    Convert date string to MMDDYYYY format (no separators).
    Handles: MM/DD/YYYY, MM-DD-YYYY, MMDDYYYY
    """
    date_str = date_str.strip()
    digits = re.sub(r'\D', '', date_str)
    
    if len(digits) == 8:
        return digits
    
    # Try parsing common formats
    for fmt in ['%m/%d/%Y', '%m-%d-%Y', '%m%d%Y']:
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt.strftime('%m%d%Y')
        except ValueError:
            continue
    
    raise ValueError(f"Cannot parse date: {date_str}")

def format_date_mmddyyyy_input(date_str: str) -> str:
    """Format date for input fields as MM/DD/YYYY"""
    mmddyyyy = format_date_mmddyyyy(date_str)
    return f"{mmddyyyy[0:2]}/{mmddyyyy[2:4]}/{mmddyyyy[4:8]}"

# ============================================================================
# QUEUE MANAGER
# ============================================================================
class QueueManager:
    def __init__(self, queue_file: str = "ng360_queue.json"):
        self.queue_file = queue_file
        self.queue: List[Job] = []
        self.load_queue()
    
    def load_queue(self):
        """Load queue from persistent storage"""
        if os.path.exists(self.queue_file):
            try:
                with open(self.queue_file, 'r') as f:
                    data = json.load(f)
                    self.queue = [
                        Job(**job) for job in data
                    ]
                logger.info(f"Loaded {len(self.queue)} jobs from queue file")
                
                # Reset any PROCESSING jobs to PENDING (crash recovery)
                for job in self.queue:
                    if job.status == JobStatus.PROCESSING:
                        job.status = JobStatus.PENDING
                        job.retry_count += 1
                        logger.info(f"Recovered job {job.job_id} from PROCESSING -> PENDING")
                        
                self.save_queue()
            except Exception as e:
                logger.error(f"Error loading queue: {e}")
                self.queue = []
        else:
            self.queue = []
    
    def save_queue(self):
        """Persist queue to disk"""
        try:
            with open(self.queue_file, 'w') as f:
                # Convert dataclasses to dict for JSON serialization
                queue_data = []
                for job in self.queue:
                    job_dict = asdict(job)
                    if job.result:
                        job_dict['result'] = asdict(job.result)
                    queue_data.append(job_dict)
                json.dump(queue_data, f, indent=2, default=str)
        except Exception as e:
            logger.error(f"Error saving queue: {e}")
    
    def add_job(self, contact_id: str, contact_data: Dict) -> Job:
        """Add new job to queue"""
        job_id = f"ng360_{datetime.now().timestamp()}"
        job = Job(
            job_id=job_id,
            contact_id=contact_id,
            contact_data=contact_data
        )
        self.queue.append(job)
        self.save_queue()
        logger.info(f"Added job {job_id} to queue")
        return job
    
    def get_next_job(self) -> Optional[Job]:
        """Get next pending job"""
        for job in self.queue:
            if job.status == JobStatus.PENDING:
                return job
        return None
    
    def update_job(self, job_id: str, status: JobStatus, result: Optional[QuoteResult] = None):
        """Update job status"""
        for job in self.queue:
            if job.job_id == job_id:
                job.status = status
                if status == JobStatus.PROCESSING:
                    job.started_at = datetime.now().isoformat()
                elif status in [JobStatus.COMPLETED, JobStatus.FAILED]:
                    job.completed_at = datetime.now().isoformat()
                if result:
                    job.result = result
                self.save_queue()
                logger.info(f"Updated job {job_id} to {status}")
                return
        logger.error(f"Job {job_id} not found")

# ============================================================================
# PLAYWRIGHT BOT - CORE AUTOMATION
# ============================================================================
class NG360Bridge:
    def __init__(self, headless: bool = False):
        self.headless = headless
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.screenshot_dir: Optional[str] = None
    
    async def launch_browser(self):
        """Launch Playwright browser"""
        playwright = await async_playwright().start()
        self.browser = await playwright.chromium.launch(headless=self.headless)
        logger.info("Browser launched")
    
    async def close_browser(self):
        """Close browser"""
        if self.browser:
            await self.browser.close()
            logger.info("Browser closed")
    
    async def login(self) -> bool:
        """
        CRITICAL ALERT: Login to National General portal.
        Returns True on success, False on failure.
        """
        try:
            logger.info("Attempting login to National General...")
            
            # Navigate to login
            await self.page.goto("https://natgenagency.com/", timeout=30000)
            await self.page.wait_for_load_state('networkidle', timeout=10000)
            
            # Click SIGN IN
            await self.page.click("text=SIGN IN", timeout=5000)
            await self.page.wait_for_load_state('networkidle', timeout=10000)
            
            # Enter credentials
            await self.page.fill("#Password", NATGEN_PASSWORD, timeout=5000)
            await self.page.click("button:has-text('SIGN IN')", timeout=5000)
            
            # Wait for navigation to MainMenu
            await self.page.wait_for_url("**/MainMenu.aspx", timeout=15000)
            await self.page.wait_for_load_state('networkidle', timeout=10000)
            
            logger.info("✓ LOGIN SUCCESSFUL")
            return True
            
        except asyncio.TimeoutError as e:
            logger.error(f"✗ CRITICAL ALERT: LOGIN_FAILURE - Timeout: {e}")
            return False
        except Exception as e:
            logger.error(f"✗ CRITICAL ALERT: LOGIN_FAILURE - {e}")
            return False
    
    async def select_state_and_product(self) -> bool:
        """Select GA state and PKGProtect2 product"""
        try:
            logger.info("Selecting state (GA) and product (PKGProtect2)...")
            
            # Select State = GA
            await self.page.select_option(
                "#ctl00_MainContent_wgtMainMenuNewQuote_ddlState",
                "GA",
                timeout=5000
            )
            
            # Select Product = PKGProtect2
            await self.page.select_option(
                "#ctl00_MainContent_wgtMainMenuNewQuote_ddlProduct",
                "PKGProtect2",
                timeout=5000
            )
            
            # Click Begin
            await self.page.click("#ctl00_MainContent_wgtMainMenuNewQuote_btnContinue", timeout=5000)
            
            # Wait for ClientSearch page
            await self.page.wait_for_url("**/ClientSearch", timeout=15000)
            logger.info("✓ State and product selected")
            return True
            
        except Exception as e:
            logger.error(f"✗ Error selecting state/product: {e}")
            return False
    
    async def search_and_add_customer(self, contact: Contact) -> bool:
        """Client Search page - search for customer, then add new"""
        try:
            logger.info("Searching for customer...")
            
            # Fill search fields
            await self.page.fill("#MainContent_txtFirstName", contact.first_name, timeout=5000)
            await self.page.fill("#MainContent_txtLastName", contact.last_name, timeout=5000)
            await self.page.fill("#MainContent_txtZipCode", contact.zip, timeout=5000)
            
            # Click Search
            await self.page.click("#MainContent_btnSearch", timeout=5000)
            await self.page.wait_for_load_state('networkidle', timeout=10000)
            
            # Click Add New Customer
            await self.page.click("#MainContent_btnAddNewClient", timeout=5000)
            await self.page.wait_for_url("**/ClientInfo", timeout=15000)
            
            logger.info("✓ Customer search and add completed")
            return True
            
        except Exception as e:
            logger.error(f"✗ Error in customer search: {e}")
            return False
    
    async def fill_client_info_page1(self, contact: Contact) -> bool:
        """
        Client Info Page 1 - Fill 14 fields
        Fields: DOB, Gender, Marital Status, Occupation, Phone (type + 3 fields),
                Address, City, Years at Residence, Email Option, Email (2x)
        """
        try:
            logger.info("Filling Client Info Page 1...")
            
            # Date of Birth (MMDDYYYY format)
            dob_formatted = format_date_mmddyyyy_input(contact.date_of_birth)
            await self.page.fill("#MainContent_ucNamedInsured_txtDateOfBirth", dob_formatted, timeout=5000)
            
            # Gender (M or F)
            await self.page.select_option(
                "#MainContent_ucNamedInsured_ddlGender",
                contact.gender,
                timeout=5000
            )
            
            # Marital Status
            await self.page.select_option(
                "#MainContent_ucNamedInsured_ddlMaritalStatus",
                contact.marital_status,
                timeout=5000
            )
            
            # Occupation
            await self.page.select_option(
                "#MainContent_ucNamedInsured_ddlOccupation",
                contact.occupation,
                timeout=5000
            )
            
            # Phone Type (default to Cell)
            await self.page.select_option(
                "#MainContent_ucContactInfo_ucPhoneNumber_ddlPhoneType",
                "Cell",
                timeout=5000
            )
            
            # Parse phone number into 3 parts
            area_code, prefix, line_number = parse_phone_number(contact.phone)
            
            # Area Code (3 digits)
            await self.page.fill(
                "#MainContent_ucContactInfo_ucPhoneNumber_txtAreaCode",
                area_code,
                timeout=5000
            )
            
            # Prefix (3 digits)
            await self.page.fill(
                "#MainContent_ucContactInfo_ucPhoneNumber_txtPrefix",
                prefix,
                timeout=5000
            )
            
            # Line Number (4 digits)
            await self.page.fill(
                "#MainContent_ucContactInfo_ucPhoneNumber_txtLineNumber",
                line_number,
                timeout=5000
            )
            
            # Street Address
            await self.page.fill(
                "#MainContent_ucResidentialAddress_txtAddress",
                contact.address1,
                timeout=5000
            )
            
            # City
            await self.page.fill(
                "#MainContent_ucResidentialAddress_txtCity",
                contact.city,
                timeout=5000
            )
            
            # Years at Residence
            await self.page.select_option(
                "#MainContent_ddlYearsAtAddress",
                str(contact.years_at_residence),
                timeout=5000
            )
            
            # Email Option (Provided)
            await self.page.select_option(
                "#MainContent_ucContactInfo_ucEmailAddress_ddlEmailOption",
                "Provided",
                timeout=5000
            )
            
            # Email Address
            await self.page.fill(
                "#MainContent_ucContactInfo_ucEmailAddress_txtEmailAddress",
                contact.email,
                timeout=5000
            )
            
            # Confirm Email Address
            await self.page.fill(
                "#MainContent_ucContactInfo_ucEmailAddress_txtEmailAddressConfirmation",
                contact.email,
                timeout=5000
            )
            
            # Click Next
            await self.page.click("#MainContent_btnContinue", timeout=5000)
            await self.page.wait_for_load_state('networkidle', timeout=10000)
            
            logger.info("✓ Client Info Page 1 completed")
            return True
            
        except Exception as e:
            logger.error(f"✗ Error filling Client Info Page 1: {e}")
            return False
    
    async def fill_client_info_page2(self) -> bool:
        """
        Client Info Page 2 - Input By field (Agent ID)
        """
        try:
            logger.info("Filling Client Info Page 2 (Input By)...")
            
            # Input By (Agent ID: 20050264)
            await self.page.select_option(
                "#MainContent_ucGeneralInformation_ddlInputBy",
                NATGEN_AGENT_ID,
                timeout=5000
            )
            
            # Click Next
            await self.page.click("#MainContent_btnContinue", timeout=5000)
            await self.page.wait_for_url("**/Prefill", timeout=15000)
            
            logger.info("✓ Client Info Page 2 completed")
            return True
            
        except Exception as e:
            logger.error(f"✗ Error filling Client Info Page 2: {e}")
            return False
    
    async def handle_prefill_verification(self) -> bool:
        """
        Prefill Verification page - Handle resident acceptance/rejection
        CRITICAL: Must handle Terms & Conditions checkbox for Dynamic Drive Program
        """
        try:
            logger.info("Handling Prefill Verification...")
            
            # Accept matches, reject others as needed
            # Try accepting first household member
            try:
                await self.page.click("tr:nth-of-type(2) input[type='radio']", timeout=5000)
            except:
                logger.warning("Could not find first accept radio button")
            
            # Accept second household member
            try:
                await self.page.click("tr:nth-of-type(3) input[type='radio']", timeout=5000)
            except:
                logger.warning("Could not find second accept radio button")
            
            # Click Next
            await self.page.click("#MainContent_btnContinue", timeout=5000)
            await self.page.wait_for_url("**/PropertyInfo", timeout=15000)
            
            logger.info("✓ Prefill Verification completed")
            return True
            
        except Exception as e:
            logger.error(f"✗ HIGH ALERT: PREFILL_MODAL_UNEXPECTED - {e}")
            return False
    
    async def fill_property_info(self, contact: Contact) -> bool:
        """
        Property Info page - 10 fields
        Residence Class, Occupancy, Structure, Named Insured Type, Effective Date,
        Roof Type, Primary Heat Type, Roof Shape, Hail Resistant, Year Roof Replacement
        """
        try:
            logger.info("Filling Property Info...")
            
            # Residence Class (Primary)
            await self.page.select_option(
                "#MainContent_ddlResidenceClass",
                "Primary",
                timeout=5000
            )
            
            # Occupancy (Owner Occupied)
            await self.page.select_option(
                "#MainContent_ddlOccupancy2",
                "OwnerOccupied",
                timeout=5000
            )
            
            # Named Insured Type (Owner)
            await self.page.select_option(
                "#MainContent_ddlNamedInsuredType",
                "Owner",
                timeout=5000
            )
            
            # Effective Date (calendar picker) - use current date
            effective_date = datetime.now().strftime("%m/%d/%Y")
            try:
                await self.page.click("#MainContent_liDatePurchased > img", timeout=5000)
                await self.page.wait_for_timeout(500)
                
                # Select month/year in calendar
                month_year = f"{datetime.now().month}/{datetime.now().year}"
                await self.page.select_option(
                    "div.datepick-popup select:nth-of-type(1)",
                    month_year,
                    timeout=5000
                )
                
                # Click day (13th)
                await self.page.click("tr:nth-of-type(3) > td:nth-of-type(3) > a", timeout=5000)
            except Exception as e:
                logger.warning(f"Calendar picker failed, trying direct input: {e}")
                # Fallback: type date directly
                await self.page.fill("#MainContent_liDatePurchased", effective_date, timeout=5000)
            
            # Roof Type (Asphalt Shingle)
            await self.page.select_option(
                "#MainContent_ddlRoofType",
                "AS",
                timeout=5000
            )
            
            # Primary Heat Type (Electric)
            await self.page.select_option(
                "#MainContent_ddlPrimaryHeat",
                "Electric",
                timeout=5000
            )
            
            # Roof Shape (Gable)
            await self.page.select_option(
                "#MainContent_ddlRoofShape",
                "Gable, Slight Pitch",
                timeout=5000
            )
            
            # Hail Resistant (False)
            await self.page.select_option(
                "#MainContent_ddlRoofHail",
                "False",
                timeout=5000
            )
            
            # Year of Roof Replacement
            await self.page.fill(
                "#MainContent_txtYearRoofRenovation",
                "2020",
                timeout=5000
            )
            
            # Oil Tank (None)
            await self.page.select_option(
                "#MainContent_ddlOilTank",
                "None",
                timeout=5000
            )
            
            # Click Next
            await self.page.click("#MainContent_btnContinue", timeout=5000)
            await self.page.wait_for_url("**/Underwriting", timeout=15000)
            
            logger.info("✓ Property Info completed")
            return True
            
        except Exception as e:
            logger.error(f"✗ Error filling Property Info: {e}")
            return False
    
    async def fill_underwriting(self, contact: Contact) -> bool:
        """
        Underwriting page - 5 fields
        Prior Insurance, Prior Carrier, Expiration Date, Years Continuous Insurance
        """
        try:
            logger.info("Filling Underwriting (Home)...")
            
            # Prior Insurance (Prior standard insurance)
            await self.page.select_option(
                "#MainContent_ucPriorPolicyInformation_ddlPriorInsuranceCoverage",
                "Prior standard insurance",
                timeout=5000
            )
            
            # Prior Carrier
            carrier = contact.prior_carrier_home or "Allstate Ins Co"
            await self.page.select_option(
                "#MainContent_ucPriorPolicyInformation_ddlPriorInsuranceCompany",
                carrier,
                timeout=5000
            )
            
            # Expiration Date
            expiry_date = contact.prior_expiration or "03/31/2026"
            expiry_formatted = format_date_mmddyyyy_input(expiry_date)
            await self.page.fill(
                "#MainContent_ucPriorPolicyInformation_txtExpirationDate",
                expiry_formatted,
                timeout=5000
            )
            
            # Years Continuous Insurance
            await self.page.fill(
                "#MainContent_ucPriorPolicyInformation_txtContinuousInsurance",
                str(contact.years_continuous_ins),
                timeout=5000
            )
            
            # Click Next
            await self.page.click("#MainContent_btnContinue", timeout=5000)
            await self.page.wait_for_url("**/LossHistory", timeout=15000)
            
            logger.info("✓ Underwriting completed")
            return True
            
        except Exception as e:
            logger.error(f"✗ Error filling Underwriting: {e}")
            return False
    
    async def skip_loss_history(self) -> bool:
        """Loss History page - review-only, click Next"""
        try:
            logger.info("Skipping Loss History (no losses)...")
            await self.page.click("#MainContent_btnContinue", timeout=5000)
            await self.page.wait_for_url("**/CoverageInfoD", timeout=15000)
            logger.info("✓ Loss History skipped")
            return True
        except Exception as e:
            logger.error(f"✗ Error skipping Loss History: {e}")
            return False
    
    async def fill_coverage_info(self) -> bool:
        """
        Coverage Info page - 3 deductibles
        All Perils, Named Storm, Windstorm/Hail
        """
        try:
            logger.info("Filling Coverage Info...")
            
            # All Perils Deductible
            await self.page.select_option(
                "#MainContent_ddlPerils",
                "0.010",
                timeout=5000
            )
            
            # Named Storm Deductible
            await self.page.select_option(
                "#MainContent_ddlNamedStormDeductible",
                "0.020",
                timeout=5000
            )
            
            # Windstorm/Hail Deductible
            await self.page.select_option(
                "#MainContent_ddlWindstormDeductible",
                "0.020",
                timeout=5000
            )
            
            # Click Next
            await self.page.click("#MainContent_btnContinue", timeout=5000)
            await self.page.wait_for_url("**/DriverInfo", timeout=15000)
            
            logger.info("✓ Coverage Info completed")
            return True
            
        except Exception as e:
            logger.error(f"✗ Error filling Coverage Info: {e}")
            return False
    
    async def fill_driver_info(self) -> bool:
        """
        Driver Info page - 7 fields + Terms & Conditions
        Losses, Defensive Driver Course, Program Participant, Dynamic Drive checkbox,
        Driver License Status, and CRITICAL: T&C checkbox for Dynamic Drive
        """
        try:
            logger.info("Filling Driver Info...")
            
            # Losses in past 5 years
            await self.page.select_option(
                "#MainContent_ddlLossesIn5Years",
                "False",
                timeout=5000
            )
            
            # Defensive Driver Course
            await self.page.select_option(
                "#MainContent_ddlDefensiveDriverCourse",
                "False",
                timeout=5000
            )
            
            # Program Participant (Connected Driver)
            await self.page.select_option(
                "#MainContent_ddlConnectedDriverOptIn",
                "False",
                timeout=5000
            )
            
            # Driver License Status
            await self.page.select_option(
                "#MainContent_ddlDriversLicenseStatus",
                "Active",
                timeout=5000
            )
            
            # Click Save Driver
            await self.page.click("#MainContent_btnSaveDriver", timeout=5000)
            await self.page.wait_for_load_state('networkidle', timeout=10000)
            
            # CRITICAL: Dynamic Drive Program T&C checkbox
            logger.info("Handling Dynamic Drive Program T&C (CRITICAL)...")
            try:
                # Scroll to T&C section
                await self.page.click("#MainContent_pnlConnectedDriver", timeout=5000)
                await self.page.wait_for_timeout(1000)
                
                # Check the T&C checkbox
                await self.page.click(
                    "#MainContent_chkConnectedDriverAgreement",
                    timeout=5000,
                    force=True
                )
                logger.info("✓ Dynamic Drive T&C checkbox checked")
            except Exception as e:
                logger.warning(f"Could not check T&C checkbox: {e}")
            
            # Click Next (driver violations page)
            await self.page.click("#MainContent_btnContinue", timeout=5000)
            await self.page.wait_for_url("**/DriverViolations", timeout=15000)
            
            logger.info("✓ Driver Info completed")
            return True
            
        except Exception as e:
            logger.error(f"✗ Error filling Driver Info: {e}")
            return False
    
    async def skip_driver_violations(self) -> bool:
        """Driver Violations page - review-only, click Next"""
        try:
            logger.info("Skipping Driver Violations...")
            await self.page.click("#MainContent_btnContinue", timeout=5000)
            await self.page.wait_for_url("**/VehicleInfo", timeout=15000)
            logger.info("✓ Driver Violations skipped")
            return True
        except Exception as e:
            logger.error(f"✗ Error skipping Driver Violations: {e}")
            return False
    
    async def fill_vehicle_info(self, contact: Contact) -> bool:
        """
        Vehicle Info page - MULTI-VEHICLE LOOP
        For EACH vehicle: 3 yes/no questions, ownership status, mileage, purchase date,
        then coverages (Comprehensive, Roadside, Custom Equipment, Audio, Transportation)
        
        HIGH ALERT: Page reloads after every Save - must wait for networkidle
        """
        try:
            logger.info(f"Processing {len(contact.vehicles)} vehicle(s)...")
            
            for idx, vehicle in enumerate(contact.vehicles):
                logger.info(f"Processing vehicle {idx + 1}/{len(contact.vehicles)}: {vehicle.year} {vehicle.make} {vehicle.model}")
                
                # Click View/Edit for this vehicle
                await self.page.click(f"#MainContent_rpVehicles_btnViewEdit_{idx}", timeout=5000)
                await self.page.wait_for_load_state('networkidle', timeout=10000)
                
                # Is vehicle rented/leased? = False
                await self.page.select_option(
                    "#MainContent_ucVehicleInfo_ddlAutoRentedToOthers",
                    "False",
                    timeout=5000
                )
                
                # Is vehicle weight 14k-16k? = False
                await self.page.select_option(
                    "#MainContent_ucVehicleInfo_ddlVehicleWeightBetween14kAnd16k",
                    "False",
                    timeout=5000
                )
                
                # Is Camper Unit included? = False
                await self.page.select_option(
                    "#MainContent_ucVehicleInfo_ddlCamperIncluded",
                    "False",
                    timeout=5000
                )
                
                # Ownership Status (3 = Leased)
                await self.page.select_option(
                    "#MainContent_ucVehicleInfo_ddlOwnershipStatus",
                    str(vehicle.ownership_status),
                    timeout=5000
                )
                
                # Annual Mileage
                await self.page.fill(
                    "#MainContent_ucVehicleInfo_txtAnnualMileage",
                    str(vehicle.annual_mileage),
                    timeout=5000
                )
                
                # Click Save Vehicle (HIGH ALERT: page reload)
                await self.page.click("#MainContent_btnSaveVehicle", timeout=5000)
                await self.page.wait_for_load_state('networkidle', timeout=30000)
                logger.info(f"  ✓ Vehicle {idx + 1} basic info saved (page reload waited)")
                
                # Re-edit for Purchased Date
                await self.page.click(f"#MainContent_rpVehicles_btnViewEdit_{idx}", timeout=5000)
                await self.page.wait_for_load_state('networkidle', timeout=10000)
                
                # Purchased Date (MM/DD/YYYY)
                purchase_date = vehicle.purchase_date or "03/01/2024"
                purchase_formatted = format_date_mmddyyyy_input(purchase_date)
                await self.page.fill(
                    "#MainContent_ucVehicleInfo_txtPurchaseDate",
                    purchase_formatted,
                    timeout=5000
                )
                
                # Click Save Vehicle again
                await self.page.click("#MainContent_btnSaveVehicle", timeout=5000)
                await self.page.wait_for_load_state('networkidle', timeout=30000)
                logger.info(f"  ✓ Vehicle {idx + 1} purchase date saved (page reload waited)")
                
                # Re-edit for Coverages
                await self.page.click(f"#MainContent_rpVehicles_btnViewEdit_{idx}", timeout=5000)
                await self.page.wait_for_load_state('networkidle', timeout=10000)
                
                # Set vehicle coverages
                # Comprehensive = 1000 deductible
                await self.page.select_option(
                    f"#MainContent_ucVehicleCoverages_rptOptionalCoverages_ddlCovCOMP_{idx}",
                    "1000",
                    timeout=5000
                )
                
                # Roadside = Not selected (-1)
                # Custom Equipment = Not selected (-1)
                # Custom Audio = Not selected (-1)
                # Transportation = Not selected (-1)
                
                # Click Save Vehicle Coverages
                await self.page.click("#MainContent_btnSaveVehicleCov", timeout=5000)
                await self.page.wait_for_load_state('networkidle', timeout=30000)
                logger.info(f"  ✓ Vehicle {idx + 1} coverages saved (page reload waited)")
            
            # After all vehicles, click Next to go to AutoUnderwriting
            await self.page.click("#MainContent_btnContinue", timeout=5000)
            await self.page.wait_for_url("**/AutoUnderwriting", timeout=15000)
            
            logger.info(f"✓ All {len(contact.vehicles)} vehicles completed")
            return True
            
        except Exception as e:
            logger.error(f"✗ HIGH ALERT: VEHICLE_PROCESSING_ERROR - {e}")
            return False
    
    async def fill_auto_underwriting(self) -> bool:
        """
        Auto Underwriting page - 5 fields
        Named Insured Type, Company Car, Losses in 5 years, Years with Prior Auto Carrier
        """
        try:
            logger.info("Filling Auto Underwriting...")
            
            # Named Insured Type = Individual
            await self.page.select_option(
                "#MainContent_ucAutoQuestions_rpParentQuestions_ddlAnswer_0",
                "Individual",
                timeout=5000
            )
            
            # Company Car = False
            await self.page.select_option(
                "#MainContent_ucAutoQuestions_rpParentQuestions_ddlAnswer_2",
                "False",
                timeout=5000
            )
            
            # Losses in 5 years = False
            await self.page.select_option(
                "#MainContent_ucAutoQuestions_rpParentQuestions_ddlAnswer_3",
                "False",
                timeout=5000
            )
            
            # Years with Prior Auto Carrier = 1
            await self.page.fill(
                "#MainContent_ucAutoQuestions_rpParentQuestions_txtAnswer_4",
                "1",
                timeout=5000
            )
            
            # Click Next
            await self.page.click("#MainContent_btnContinue", timeout=5000)
            await self.page.wait_for_url("**/PremiumSummary", timeout=15000)
            
            logger.info("✓ Auto Underwriting completed")
            return True
            
        except Exception as e:
            logger.error(f"✗ Error filling Auto Underwriting: {e}")
            return False
    
    async def generate_premium_summary(self) -> tuple[bool, Optional[Dict[str, float]]]:
        """
        Premium Summary page - final step
        1. Select Pay Method = AS (Agency Sweep)
        2. Select Pay Plan
        3. Click Re-Rate
        4. Extract premium values
        5. Click Quote Proposal (PDF generation)
        
        Returns: (success: bool, premium_dict: {combined, home, auto})
        """
        try:
            logger.info("Handling Premium Summary...")
            
            # Select Pay Method = AS (Agency Sweep)
            await self.page.select_option(
                "#MainContent_ucPaymentMethod_ddlPayMethod",
                "AS",
                timeout=5000
            )
            
            # Pay Plan - try PIF if available
            try:
                await self.page.select_option(
                    "#MainContent_ucPaymentMethod_ddlPayPlan",
                    "PIF",
                    timeout=5000
                )
            except:
                logger.warning("PIF not available, using default pay plan")
            
            # Click Re-Rate
            await self.page.click("#MainContent_ucRater_btnRate", timeout=5000)
            await self.page.wait_for_load_state('networkidle', timeout=10000)
            logger.info("✓ Re-Rate clicked, premium recalculated")
            
            # Extract premium values (try to find them in the page)
            premiums = {
                "combined": 0.0,
                "home": 0.0,
                "auto": 0.0
            }
            
            try:
                # Try to scrape combined premium
                premium_text = await self.page.text_content(".premium-total", timeout=5000)
                if premium_text:
                    premiums["combined"] = float(premium_text.replace("$", "").replace(",", ""))
            except:
                logger.warning("Could not extract combined premium")
            
            # Click Quote Proposal to generate PDF
            logger.info("CRITICAL: Generating Quote Proposal PDF...")
            try:
                await self.page.click("#MainContent_btnQuoteProposal", timeout=5000)
                await self.page.wait_for_timeout(3000)  # Give PDF time to generate
                logger.info("✓ Quote Proposal generated")
            except Exception as e:
                logger.error(f"✗ CRITICAL ALERT: PDF_GENERATION_FAILURE - {e}")
                # Don't fail completely, continue
            
            logger.info("✓ Premium Summary completed")
            return (True, premiums)
            
        except Exception as e:
            logger.error(f"✗ Error handling Premium Summary: {e}")
            return (False, None)
    
    async def capture_screenshot(self, label: str) -> str:
        """Capture page screenshot"""
        try:
            if not self.screenshot_dir:
                return ""
            
            filename = f"{self.screenshot_dir}/{label}_{datetime.now().timestamp()}.png"
            await self.page.screenshot(path=filename)
            return filename
        except Exception as e:
            logger.error(f"Error capturing screenshot: {e}")
            return ""
    
    async def run_full_quote(self, contact: Contact, job_id: str) -> QuoteResult:
        """
        Execute full quote flow for a contact
        CRITICAL ALERTS: Login, session timeout, PDF generation
        HIGH ALERTS: Prefill modal, calendar picker, vehicle reload, dropdown options
        """
        self.screenshot_dir = f"screenshots/{job_id}"
        os.makedirs(self.screenshot_dir, exist_ok=True)
        
        logger.info(f"Starting quote flow for {contact.first_name} {contact.last_name}")
        
        # Step 1: Login (CRITICAL)
        if not await self.login():
            return QuoteResult(
                contact_id="unknown",
                job_id=job_id,
                status=QuoteStatus.FAILED,
                error_message="LOGIN_FAILURE"
            )
        
        await self.capture_screenshot("01_login")
        
        # Step 2: Select state and product
        if not await self.select_state_and_product():
            return QuoteResult(
                contact_id="unknown",
                job_id=job_id,
                status=QuoteStatus.FAILED,
                error_message="STATE_PRODUCT_SELECTION"
            )
        
        # Step 3: Search and add customer
        if not await self.search_and_add_customer(contact):
            return QuoteResult(
                contact_id="unknown",
                job_id=job_id,
                status=QuoteStatus.FAILED,
                error_message="CUSTOMER_SEARCH"
            )
        
        # Step 4: Client Info Page 1
        if not await self.fill_client_info_page1(contact):
            return QuoteResult(
                contact_id=contact.first_name,
                job_id=job_id,
                status=QuoteStatus.FAILED,
                error_message="CLIENT_INFO_PAGE1"
            )
        
        # Step 5: Client Info Page 2
        if not await self.fill_client_info_page2():
            return QuoteResult(
                contact_id=contact.first_name,
                job_id=job_id,
                status=QuoteStatus.FAILED,
                error_message="CLIENT_INFO_PAGE2"
            )
        
        # Step 6: Prefill Verification (HIGH ALERT on modal issues)
        if not await self.handle_prefill_verification():
            return QuoteResult(
                contact_id=contact.first_name,
                job_id=job_id,
                status=QuoteStatus.FAILED,
                error_message="PREFILL_VERIFICATION"
            )
        
        # Step 7: Property Info
        if not await self.fill_property_info(contact):
            return QuoteResult(
                contact_id=contact.first_name,
                job_id=job_id,
                status=QuoteStatus.FAILED,
                error_message="PROPERTY_INFO"
            )
        
        # Step 8: Underwriting
        if not await self.fill_underwriting(contact):
            return QuoteResult(
                contact_id=contact.first_name,
                job_id=job_id,
                status=QuoteStatus.FAILED,
                error_message="UNDERWRITING"
            )
        
        # Step 9: Loss History
        if not await self.skip_loss_history():
            return QuoteResult(
                contact_id=contact.first_name,
                job_id=job_id,
                status=QuoteStatus.FAILED,
                error_message="LOSS_HISTORY"
            )
        
        # Step 10: Coverage Info
        if not await self.fill_coverage_info():
            return QuoteResult(
                contact_id=contact.first_name,
                job_id=job_id,
                status=QuoteStatus.FAILED,
                error_message="COVERAGE_INFO"
            )
        
        # Step 11: Driver Info (CRITICAL T&C)
        if not await self.fill_driver_info():
            return QuoteResult(
                contact_id=contact.first_name,
                job_id=job_id,
                status=QuoteStatus.FAILED,
                error_message="DRIVER_INFO"
            )
        
        # Step 12: Driver Violations
        if not await self.skip_driver_violations():
            return QuoteResult(
                contact_id=contact.first_name,
                job_id=job_id,
                status=QuoteStatus.FAILED,
                error_message="DRIVER_VIOLATIONS"
            )
        
        # Step 13: Vehicle Info (HIGH ALERT - page reloads)
        if not await self.fill_vehicle_info(contact):
            return QuoteResult(
                contact_id=contact.first_name,
                job_id=job_id,
                status=QuoteStatus.FAILED,
                error_message="VEHICLE_INFO"
            )
        
        # Step 14: Auto Underwriting
        if not await self.fill_auto_underwriting():
            return QuoteResult(
                contact_id=contact.first_name,
                job_id=job_id,
                status=QuoteStatus.FAILED,
                error_message="AUTO_UNDERWRITING"
            )
        
        # Step 15: Premium Summary & PDF (CRITICAL PDF)
        success, premiums = await self.generate_premium_summary()
        if not success:
            return QuoteResult(
                contact_id=contact.first_name,
                job_id=job_id,
                status=QuoteStatus.FAILED,
                error_message="PREMIUM_SUMMARY"
            )
        
        await self.capture_screenshot("final_premium_summary")
        
        # SUCCESS
        logger.info(f"✓✓✓ QUOTE COMPLETED SUCCESSFULLY for {contact.first_name} {contact.last_name}")
        
        return QuoteResult(
            contact_id=contact.first_name,
            job_id=job_id,
            status=QuoteStatus.QUOTED,
            combined_premium=premiums.get("combined", 0.0) if premiums else 0.0,
            home_premium=premiums.get("home", 0.0) if premiums else 0.0,
            auto_premium=premiums.get("auto", 0.0) if premiums else 0.0,
            quote_date=datetime.now().isoformat(),
            pay_plan="PIF"
        )

# ============================================================================
# FASTAPI WEBHOOK SERVER
# ============================================================================
app = FastAPI(title="NG360 Bot", version="1.0.0")
queue_manager = QueueManager()

@app.get("/health")
async def health():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "port": WEBHOOK_PORT,
        "queue_depth": len([j for j in queue_manager.queue if j.status == JobStatus.PENDING]),
        "uptime": datetime.now().isoformat()
    }

@app.post("/webhook/ng-quote")
async def webhook_ng_quote(payload: WebhookPayload):
    """
    GHL Webhook - new contact for NG360 quote
    Validates required fields, creates job, returns immediately
    """
    logger.info(f"Webhook received for contact: {payload.contact_id}")
    
    # Validate required fields
    contact_data = payload.dict()
    for field in REQUIRED_GHL_FIELDS:
        if field not in contact_data or not contact_data[field]:
            logger.warning(f"Missing required field: {field}")
            # Tag contact as ng-quote-missing-data
            return {"status": "error", "message": f"Missing required field: {field}"}
    
    # Create job and queue it
    job = queue_manager.add_job(payload.contact_id, contact_data)
    
    return {
        "status": "queued",
        "job_id": job.job_id,
        "contact_id": payload.contact_id,
        "queue_position": len([j for j in queue_manager.queue if j.status == JobStatus.PENDING])
    }

@app.get("/queue")
async def get_queue():
    """Return current queue status"""
    return {
        "total_jobs": len(queue_manager.queue),
        "pending": len([j for j in queue_manager.queue if j.status == JobStatus.PENDING]),
        "processing": len([j for j in queue_manager.queue if j.status == JobStatus.PROCESSING]),
        "completed": len([j for j in queue_manager.queue if j.status == JobStatus.COMPLETED]),
        "failed": len([j for j in queue_manager.queue if j.status == JobStatus.FAILED]),
    }

# ============================================================================
# MAIN QUEUE PROCESSOR
# ============================================================================
async def process_queue():
    """
    Main queue processor loop
    Runs continuously, processing one job at a time
    WATCHDOG: Jobs stuck >600s are rescued
    """
    bridge = NG360Bridge(headless=True)
    await bridge.launch_browser()
    
    try:
        while True:
            # Check for stuck jobs (WATCHDOG - HIGH ALERT)
            for job in queue_manager.queue:
                if job.status == JobStatus.PROCESSING and job.started_at:
                    elapsed = (datetime.now() - datetime.fromisoformat(job.started_at)).total_seconds()
                    if elapsed > 600:  # 10 minutes timeout
                        logger.critical(f"CRITICAL ALERT: STUCK_JOB_RESCUE - Job {job.job_id} stuck for {elapsed}s")
                        job.status = JobStatus.PENDING
                        job.retry_count += 1
                        queue_manager.save_queue()
            
            # Get next pending job
            job = queue_manager.get_next_job()
            if not job:
                await asyncio.sleep(5)  # No jobs, wait
                continue
            
            # Update to PROCESSING
            queue_manager.update_job(job.job_id, JobStatus.PROCESSING)
            
            try:
                # Parse contact data
                contact_data = job.contact_data
                vehicles = [Vehicle(**v) for v in contact_data.get("vehicles", [])]
                contact = Contact(
                    first_name=contact_data["first_name"],
                    last_name=contact_data["last_name"],
                    zip=contact_data["zip"],
                    date_of_birth=contact_data["date_of_birth"],
                    gender=contact_data["gender"],
                    marital_status=contact_data["marital_status"],
                    occupation=contact_data["occupation"],
                    phone=contact_data["phone"],
                    address1=contact_data["address1"],
                    city=contact_data["city"],
                    email=contact_data["email"],
                    vehicles=vehicles,
                    num_vehicles=len(vehicles),
                    years_at_residence=contact_data.get("years_at_residence", 5),
                    prior_carrier_home=contact_data.get("prior_carrier_home"),
                    prior_expiration=contact_data.get("prior_expiration"),
                    years_continuous_ins=contact_data.get("years_continuous_ins", 1),
                )
                
                # Run quote
                result = await bridge.run_full_quote(contact, job.job_id)
                
                # Update job with result
                queue_manager.update_job(job.job_id, JobStatus.COMPLETED, result)
                logger.info(f"✓ Job {job.job_id} completed with status {result.status}")
                
            except Exception as e:
                logger.error(f"✗ Job {job.job_id} failed with exception: {e}", exc_info=True)
                result = QuoteResult(
                    contact_id=job.contact_id,
                    job_id=job.job_id,
                    status=QuoteStatus.FAILED,
                    error_message=str(e)
                )
                
                if job.retry_count < 3:
                    # Retry
                    job.retry_count += 1
                    queue_manager.update_job(job.job_id, JobStatus.PENDING)
                    logger.info(f"Job {job.job_id} re-queued (retry {job.retry_count}/3)")
                else:
                    # Give up
                    queue_manager.update_job(job.job_id, JobStatus.FAILED, result)
                    logger.error(f"✗ Job {job.job_id} FINAL FAILED after 3 retries")
    
    finally:
        await bridge.close_browser()

# ============================================================================
# ENTRY POINT
# ============================================================================
async def main():
    """Start FastAPI server and queue processor"""
    # Start queue processor in background
    processor_task = asyncio.create_task(process_queue())
    
    # Start FastAPI server
    config = uvicorn.Config(
        app=app,
        host="0.0.0.0",
        port=WEBHOOK_PORT,
        log_level="info"
    )
    server = uvicorn.Server(config)
    
    try:
        await server.serve()
    except KeyboardInterrupt:
        logger.info("Shutdown requested")
        processor_task.cancel()

if __name__ == "__main__":
    asyncio.run(main())
