# React + TypeScript + Vite + shadcn/ui

This is a template for a new Vite project with React, TypeScript, and shadcn/ui.

## GitHub App login

Set `VITE_BACKEND_URL=http://localhost:8000` locally and configure the backend's
`FRONTEND_URL=http://localhost:5173`. Use the same hostname throughout; do not mix
`localhost` and `127.0.0.1`. No GitHub secrets belong in frontend environment variables.

Login keeps the application bearer JWT in the existing local storage location.
Profile, installation access, and reconnect state come from `/auth/me`, not JWT
profile claims. `/auth/success` consumes the token, removes it from the URL, and
routes using verified access: `/projects` or `/install`.

The `/install` page starts an authenticated request with Axios `withCredentials`
for the temporary OAuth cookie, then navigates to GitHub's own installation screen.
It also supports reconnecting expired GitHub authorization and checking access again.
Protected dashboard routes require usable installation access; backend failures
show retry controls instead of incorrectly claiming no installation exists.

Repository discovery preserves installation IDs, full names, ingestion status,
and separate GitHub/internal repository IDs. **Project creation and chat remain
mocked intentionally; these changes do not start repository ingestion.**

Run `npm test`, `npm run build`, and `npm run lint` to verify the frontend.
Tests use Node's built-in runner and Vite's module loader; no live GitHub calls.

## Adding components

To add components to your app, run the following command:

```bash
npx shadcn@latest add button
```

This will place the ui components in the `src/components` directory.

## Using components

To use the components in your app, import them as follows:

```tsx
import { Button } from "@/components/ui/button"
```
