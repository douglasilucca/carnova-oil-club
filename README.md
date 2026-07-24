# Carnova Oil Club — Online Edition

This version is prepared for deployment on Render with PostgreSQL.

## Deploy

1. Create a new GitHub repository.
2. Upload every file from this folder to the repository.
3. In Render, choose **New > Blueprint**.
4. Connect the GitHub repository.
5. Render detects `render.yaml`.
6. Enter a strong value for `ADMIN_PASSWORD`.
7. Deploy.
8. Open the generated `onrender.com` address.

## Temporary administrator

- Email: `admin@carnovaoil.com`
- Password: the value entered during deployment.

## Stripe webhook

After deployment, the webhook URL is:

`https://YOUR-APP.onrender.com/stripe/webhook`

In Stripe Workbench / Webhooks:

1. Add an event destination.
2. Use the URL above.
3. Subscribe to `checkout.session.completed`.
4. Copy the signing secret.
5. In Render, set `STRIPE_WEBHOOK_SECRET`.
6. Redeploy the app.

## Custom domain

Add `club.carnovaoil.com` in the Render Custom Domains area, then copy the DNS record Render provides into the domain provider.
