"""
Canonical GHL custom field IDs — sourced from ghl_custom_fields.json export.

``services/ghl_client.py`` imports these constants (single source of truth).

Verified against location: Czwg7VWYU6myocqsb86R
"""

# ---------------------------------------------------------------------------
# Output fields — written back to GHL after quote completes
# ---------------------------------------------------------------------------
#
# Contractor brief uses ng_* names; this location uses existing GHL fields instead:
#
#   Brief field          GHL field (actual)                      Constant
#   ----------------     ------------------------------------    ---------------------------
#   ng_price             fire_price                              FIELD_ID_PRICE
#   ng_quote_status      fire_quote_status                       FIELD_ID_QUOTE_STATUS
#   (auto status)        auto_quote_status                       FIELD_ID_AUTO_QUOTE_STATUS
#   ng_price (auto)      national_general_quote_price            FIELD_ID_NG_QUOTE_PRICE
#   ng_quote_url         auto_quote_url                          FIELD_ID_AUTO_QUOTE_URL
#   (PDF upload)         upload_national_general_auto_quote      FIELD_ID_NG_QUOTE_PDF
#   ng_home_premium      (no output field — Slack/console only)
#   ng_pay_plan          (no field ID yet)
#   ng_quote_date        (no field ID yet)
#
# Tags (not custom fields): ng-quote-success, ng-quote-failed, etc. in ghl_client.py

# Shared total premium + quote status (re-used from HOA bot — keep as-is).
FIELD_ID_PRICE        = "FbUGPnB3rSRHDU52RV2d"  # contact.fire_price         ✓ VERIFIED
FIELD_ID_QUOTE_STATUS = "WxZtUOwNYitB1ZRKxzgY"  # contact.fire_quote_status  ✓ VERIFIED
FIELD_ID_NOT_ELIGIBLE = "Ni7UAcQDhxsWG6OwnBdh"  # contact.not_eligible       ✓ VERIFIED

# NG360 quote output — wired in record_successful_quote()
FIELD_ID_NG_QUOTE_PRICE  = "0hirh8xfnKhXkh2PZtro"  # contact.national_general_quote_price
FIELD_ID_NG_QUOTE_PDF    = "5ZZDrU61kWXa0Am4mHDg"  # contact.upload_national_general_auto_quote
FIELD_ID_AUTO_QUOTE_STATUS = "toQnMxk4xZZEZETomWbH"  # contact.auto_quote_status
FIELD_ID_AUTO_QUOTE_URL    = "TVq7CxkxXumd1kD9v1Rm"  # contact.auto_quote_url

# ---------------------------------------------------------------------------
# Input fields — read from GHL contact to feed bridge_bot
# ---------------------------------------------------------------------------

# Core property
FIELD_ID_YEAR_BUILT   = "oGHsIqmSaJUdYs3QHtOI"  # contact.year_built         ✓ VERIFIED

# Vehicles — Vehicle #1
FIELD_ID_VEH1_YEAR      = "auVJAvWpnnYYYzTo3oW9"  # contact.vehicle_1_year
FIELD_ID_VEH1_MAKE      = "C7istMeqbBJT06ZmM76P"  # contact.vehicle_1_make
FIELD_ID_VEH1_MODEL     = "seNja9CEi7TLhXew64WI"  # contact.vehicle_1_model
FIELD_ID_VEH1_SUBMODEL  = "IVPWhlbMASu1hAr6wd6H"  # contact.vehicle_1_submodel
FIELD_ID_VEH1_VIN       = "DXe8dPAy1NzqkRYGsBw2"  # contact.vehicle_1_vin_prefix
FIELD_ID_VEH1_OWNERSHIP = "44z8qQkPDktQiBiiUOiw"  # contact.vehicle_1_owned__leased
FIELD_ID_VEH1_USE       = "WmXgvU3uyJaqYAVuUNy4"  # contact.vehicle_1_use
FIELD_ID_VEH1_ANNUAL_MI = "qclrLfVxDmRh3uv6bMUi"  # contact.vehicle_1_annual_distance_mi
FIELD_ID_VEH1_DIST_MI   = "7wN4SnWh5d0FjilriNrd"  # contact.vehicle_1_istance_driven_mi  (typo in GHL preserved)
FIELD_ID_VEH1_PARKED    = "Xhyrwzgzndv908OWd8Jd"  # contact.vehicle_1_location_parked

# Vehicles — Vehicle #2
FIELD_ID_VEH2_YEAR      = "DdpYg3Lo5ioWcPyBnRgJ"  # contact.vehicle_2_year
FIELD_ID_VEH2_MAKE      = "341K8Jgel1X5J5OrJm4m"  # contact.vehicle_2_make
FIELD_ID_VEH2_MODEL     = "f4sPNa61EW2yFgEOnLgF"  # contact.vehicle_2_model
FIELD_ID_VEH2_SUBMODEL  = "aTgHXTOzecDIgTm94dtJ"  # contact.vehicle_2_submodel
FIELD_ID_VEH2_VIN       = "2vUp4BAImmIo8a8dsK7N"  # contact.vehicle_2_vin_prefix
FIELD_ID_VEH2_OWNERSHIP = "MWLLTPYeXjWbYvtl2sVV"  # contact.vehicle_2_owned__leased
FIELD_ID_VEH2_USE       = "nklRSHs53FVK43manawu"   # contact.vehicle_2_use
FIELD_ID_VEH2_ANNUAL_MI = "L3q4eh1qN5dAibZCbUlm"  # contact.vehicle_2_annual_distance_mi
FIELD_ID_VEH2_DIST_MI   = "bHNx2ZBVrio8qURwWtex"  # contact.vehicle_2_distance_driven_mi
FIELD_ID_VEH2_PARKED    = "Q4TYXP4bENKFXBj2HT5Q"  # contact.vehicle_2_location_parked

# Total vehicles
FIELD_ID_TOTAL_VEHICLES  = "zAJbxgQsMSzsgrMawqow"  # contact.total_vehicles
FIELD_ID_NUM_AUTO        = "aZHbdJXTOjdIssPV7sMa"  # contact.# of auto
FIELD_ID_ANNUAL_MILEAGE  = "MTfxFP3mYXa8bRr6IsLr"  # contact.annual_mileage
FIELD_ID_ANNUAL_MILEAGE1 = "KXEo63Au66hZjenqSZAi"  # contact.annual_mileage_1
FIELD_ID_ANNUAL_MILEAGE2 = "UEkFlv5LO65nFASGdHpf"  # contact.annual_mileage_2

# Drivers — Driver #1
FIELD_ID_DRV1_FIRST      = "ncWuqpGqeNBZg2bnpJ6a"  # contact.driver_1_first_name
FIELD_ID_DRV1_LAST       = "WwLY87sIdm2e00u56z0h"  # contact.driver_1_last_name
FIELD_ID_DRV1_DOB        = "0zTbyVEssvnAInozzBdz"  # contact.driver_1_date_of_birth
FIELD_ID_DRV1_GENDER     = "AWMNPex2qSSme7YK5e4G"  # contact.driver_1_gender
FIELD_ID_DRV1_MARITAL    = "GbIVaeAE4oOe6xygsCp2"  # contact.driver_1_marital_status
FIELD_ID_DRV1_LIC_STATUS = "angsInSy7gZlI1xoFz6E"  # contact.driver_1_license_status
FIELD_ID_DRV1_LIC_NUM    = "s1kiQ4QAMzZUND5pmslt"  # contact.driver_1_drivers_license_number
FIELD_ID_DRV1_OCCUPATION = "z9r6fDjPLXxMHOyD2avg"  # contact.driver_1_occupation
FIELD_ID_DRV1_CREDIT     = "8JFsLV5CMbvhzGyRNA5y"  # contact.driver_1_credit_rating
FIELD_ID_DRV1_PRIMARY_VEH = "dossMc26YL4QV3d7pOY6"  # contact.driver_1_primary_vehicle
FIELD_ID_DRV1_SR22       = "QGRdSXfdWFn6q9fix965"  # contact.driver_1_needs_sr22
FIELD_ID_DRV1_SUSPENDED  = "PVJRHrIH2xH1Emzj0jr8"  # contact.driver_1_license_ever_suspended
FIELD_ID_DRV1_EDUCATION  = "LsH1Z3CBECGk8SEWE6cR"  # contact.driver_1_education_level

# Drivers — Driver #2
FIELD_ID_DRV2_FIRST      = "JEoeGMthQLekTFXAi1L1"  # contact.driver_2_first_name
FIELD_ID_DRV2_LAST       = "N0ZY3gBOQ92qbEzp2ciT"  # contact.driver_2_last_name
FIELD_ID_DRV2_DOB        = "kJh3P1fT6gGQbu9qMFHz"  # contact.driver_2_date_of_birth
FIELD_ID_DRV2_GENDER     = "sYoBRuqkLH0xkSww1mdt"  # contact.driver_2_gender
FIELD_ID_DRV2_MARITAL    = "wJfi4ovSirG6ddixmrow"  # contact.driver_2_marital_status
FIELD_ID_DRV2_LIC_STATUS = "gZIsBJk4OIjdGkeRwuLr"  # contact.driver_2_license_status
FIELD_ID_DRV2_OCCUPATION = "m7T7VG450e5eRgiooCmv"  # contact.driver_2_occupation
FIELD_ID_DRV2_PRIMARY_VEH = "Tp2Brc7JfuzeHLq0X0z1"  # contact.driver_2_primary_vehicle

# Policy / carrier info (input only — do not overwrite on quote success)
FIELD_ID_CURRENT_AUTO_CARRIER  = "tIRadWH8kH71Wuc3JhE0"  # contact.current_auto_carrier
FIELD_ID_CURRENT_HOME_CARRIER  = "aYB5OycrkqiwFgn8Dw3i"  # contact.current_home_carrier
FIELD_ID_CURRENT_INSURER       = "MY5HS0NaK05DraTN2MZY"   # contact.current_insurer
FIELD_ID_COVERAGE_A            = "axsIxJgVILl3PdiX5wUQ"   # contact.coverage_a
FIELD_ID_AUTO_POLICY_NUMBER    = "bDTA6zupa5ByZmuaqSBn"   # contact.auto_policy_number
FIELD_ID_AUTO_POLICY_PREMIUM   = "rXUgFslrbsQbAiDB5jNo"   # contact.auto_policy_premium (existing policy, not NG quote)
