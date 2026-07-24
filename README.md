# Carnova Oil Club V3 — Phase 1

## New in this release
- Premium Carnova visual identity
- QR camera scanner with manual-search fallback
- Improved Stripe customer-name capture
- Optional Stripe Customer lookup using STRIPE_SECRET_KEY
- Program revenue and estimated outstanding-cost dashboard
- Membership utilization indicator
- Premium digital member card
- Existing members, webhook and PostgreSQL data preserved

## Optional Render variables
- STRIPE_SECRET_KEY: allows the webhook to retrieve a customer's name and phone when Stripe does not send them in the checkout event.
- ESTIMATED_COST_PER_CHANGE_CENTS: internal estimated cost per oil change. Default is 6500 ($65).

Upload the contents of this folder to the root of the existing GitHub repository and commit.
