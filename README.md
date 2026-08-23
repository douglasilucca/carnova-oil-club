# Carnova Oil Club V5 — Online Appointments

## New feature
- Customer self-service appointment booking
- Available time slots for the next 30 days
- Sundays automatically closed
- Duplicate bookings prevented
- Vehicle selection during booking
- Customer appointment confirmation page
- Customer can cancel an upcoming appointment
- Admin appointment calendar
- Confirm, complete, cancel or mark no-show
- Upcoming appointments on the dashboard
- Optional SMTP email confirmations
- Existing Stripe, members, vehicles, service history and PostgreSQL data preserved

## Default schedule
- Monday through Saturday
- 9:00 AM through 5:00 PM
- 60-minute appointment slots
- 30-day booking window

## Optional Render variables
- APPOINTMENT_START_HOUR=9
- APPOINTMENT_END_HOUR=17
- APPOINTMENT_SLOT_MINUTES=60
- APPOINTMENT_BOOKING_DAYS=30

## Optional email variables
- SMTP_HOST
- SMTP_PORT
- SMTP_USERNAME
- SMTP_PASSWORD
- SMTP_FROM_EMAIL

The appointment system works without email configuration.
Auto deploy test 2026

## Google Wallet pass design

The Generic Pass uses the existing Carnova logo, a dark graphite background, premium membership labels, a QR code, and an object-level `Schedule Oil Change` link. The pass also publishes the member's remaining changes and earliest upcoming appointment; booking, cancellation, and staff status changes sync the saved object.

Google Wallet controls the Android card shell, typography, spacing, truncation, and final layout. Carnova controls the supported pass data: logo, background color, text modules, barcode value, links, app link, validity, and class row template. Generic Pass does not provide a reliable per-field text-color control, so the digital membership card's aqua balance accent cannot be guaranteed on Android and pixel-perfect parity is not claimed.

The implementation follows the [GenericObject REST reference](https://developers.google.com/wallet/reference/rest/v1/genericobject) and [GenericClass REST reference](https://developers.google.com/wallet/reference/rest/v1/genericclass).
