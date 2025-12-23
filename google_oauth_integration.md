# External App Google OAuth Integration Guide

This guide explains how to integrate Google Calendar OAuth with Vomeet from your external application.

## Overview

Vomeet handles the OAuth token exchange and storage. Your app:
1. Gets a calendar auth token from Vomeet
2. Builds the Google OAuth URL
3. Receives the callback and forwards to Vomeet
4. Vomeet stores tokens and redirects to your success/error URLs

## Prerequisites

1. **Vomeet Account** with API key
2. **Google Cloud Console** project with OAuth 2.0 credentials configured on your Vomeet account
3. **Redirect URI** registered in Google Cloud Console

---

## Step 1: Get Calendar Auth Token (Backend)

Call Vomeet to get a signed auth token for your user.

```bash
curl -X POST https://vomeet.io/google/calendar/auth_token \
  -H "X-API-Key: YOUR_VOMEET_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"external_user_id": "your-user-123"}'
```

**Response:**
```json
{
  "calendar_auth_token": "YXU6MTIzOjE3MDM...",
  "account_user_id": 456,
  "expires_in": 600
}
```

> ⚠️ Token expires in 10 minutes. Generate a new one if the user doesn't complete OAuth in time.

---

## Step 2: Build Google OAuth URL (Frontend)

Construct the Google OAuth URL with a JSON state parameter:

```javascript
// Frontend JavaScript
function initiateGoogleOAuth(calendarAuthToken) {
  const state = JSON.stringify({
    vomeet_calendar_auth_token: calendarAuthToken,  // from step 1
    google_oauth_redirect_uri: "https://your-app.com/oauth/callback",
    success_url: "https://your-app.com/calendar/success",
    error_url: "https://your-app.com/calendar/error"
  });

  const params = new URLSearchParams({
    client_id: "YOUR_GOOGLE_CLIENT_ID",  // same as configured in Vomeet account
    redirect_uri: "https://your-app.com/oauth/callback",
    response_type: "code",
    scope: "https://www.googleapis.com/auth/calendar.events.readonly https://www.googleapis.com/auth/userinfo.email",
    access_type: "offline",
    prompt: "consent",
    state: state
  });

  window.location.href = `https://accounts.google.com/o/oauth2/v2/auth?${params}`;
}
```

### State Parameter Fields

| Field | Required | Description |
|-------|----------|-------------|
| `vomeet_calendar_auth_token` | ✅ | Token from Step 1 |
| `google_oauth_redirect_uri` | ✅ | Your callback URL (must match `redirect_uri`) |
| `success_url` | ❌ | Redirect here on success |
| `error_url` | ❌ | Redirect here on error |

---

## Step 3: Handle OAuth Callback (Backend)

Google redirects to your `redirect_uri` with `code` and `state` parameters. Forward these to Vomeet:

### Python (FastAPI)
```python
from fastapi import FastAPI, Query
from fastapi.responses import RedirectResponse
from urllib.parse import urlencode

app = FastAPI()

@app.get("/oauth/callback")
async def oauth_callback(
    code: str = Query(None),
    state: str = Query(None),
    error: str = Query(None),
    error_description: str = Query(None),
):
    # Forward all params to Vomeet
    params = {}
    if code:
        params["code"] = code
    if state:
        params["state"] = state
    if error:
        params["error"] = error
    if error_description:
        params["error_description"] = error_description
    
    vomeet_url = f"https://vomeet.io/google/calendar/google_oauth_callback?{urlencode(params)}"
    return RedirectResponse(url=vomeet_url)
```

### Node.js (Express)
```javascript
app.get('/oauth/callback', (req, res) => {
  const { code, state, error, error_description } = req.query;
  
  const params = new URLSearchParams();
  if (code) params.append('code', code);
  if (state) params.append('state', state);
  if (error) params.append('error', error);
  if (error_description) params.append('error_description', error_description);
  
  res.redirect(`https://vomeet.io/google/calendar/google_oauth_callback?${params}`);
});
```

---

## Step 4: Handle Success/Error Redirects

### Success Redirect
Vomeet redirects to `success_url` with these query params:
- `email` - Connected Google account email
- `name` - User's display name
- `account_user_id` - Vomeet's internal user ID

```javascript
// https://your-app.com/calendar/success?email=user@gmail.com&name=John&account_user_id=456

app.get('/calendar/success', (req, res) => {
  const { email, name, account_user_id } = req.query;
  // Show success message, update UI, etc.
  res.render('success', { email, name });
});
```

### Error Redirect
Vomeet redirects to `error_url` with these query params:
- `error` - Error code
- `error_description` - Human-readable message

```javascript
// https://your-app.com/calendar/error?error=access_denied&error_description=User%20denied%20access

app.get('/calendar/error', (req, res) => {
  const { error, error_description } = req.query;
  res.render('error', { error, error_description });
});
```

---

## Google Cloud Console Setup

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Select your project
3. Navigate to **APIs & Services → Credentials**
4. Edit your **OAuth 2.0 Client ID**
5. Add your redirect URI to **Authorized redirect URIs**:
   ```
   https://your-app.com/oauth/callback
   ```

---

## Complete Flow Diagram

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│   Your App   │     │    Vomeet    │     │    Google    │     │   Your App   │
│   Frontend   │     │     API      │     │    OAuth     │     │   Backend    │
└──────┬───────┘     └──────┬───────┘     └──────┬───────┘     └──────┬───────┘
       │                    │                    │                    │
       │ 1. Get auth token  │                    │                    │
       │───────────────────>│                    │                    │
       │                    │                    │                    │
       │ calendar_auth_token│                    │                    │
       │<───────────────────│                    │                    │
       │                    │                    │                    │
       │ 2. Redirect to Google OAuth             │                    │
       │────────────────────────────────────────>│                    │
       │                    │                    │                    │
       │                    │    3. User grants  │                    │
       │                    │       permission   │                    │
       │                    │                    │                    │
       │                    │    4. Redirect to your callback         │
       │                    │                    │───────────────────>│
       │                    │                    │                    │
       │                    │ 5. Forward to Vomeet                    │
       │                    │<───────────────────────────────────────│
       │                    │                    │                    │
       │                    │ 6. Exchange code,  │                    │
       │                    │    store tokens    │                    │
       │                    │                    │                    │
       │ 7. Redirect to success_url              │                    │
       │<───────────────────│                    │                    │
       │                    │                    │                    │
```

---

## API Endpoints After Connection

Once a user is connected, use these endpoints:

### Check Integration Status
```bash
curl https://vomeet.io/google/users/{external_user_id}/status \
  -H "X-API-Key: YOUR_API_KEY"
```

### Get Calendar Events
```bash
curl https://vomeet.io/google/users/{external_user_id}/calendar/events \
  -H "X-API-Key: YOUR_API_KEY"
```

### Get Upcoming Google Meets
```bash
curl "https://vomeet.io/google/users/{external_user_id}/calendar/upcoming-meets?hours=24" \
  -H "X-API-Key: YOUR_API_KEY"
```

### Disconnect Integration
```bash
curl -X DELETE https://vomeet.io/google/users/{external_user_id}/disconnect \
  -H "X-API-Key: YOUR_API_KEY"
```

---

## Error Codes

| Error Code | Description |
|------------|-------------|
| `access_denied` | User denied calendar permissions |
| `missing_code` | No authorization code from Google |
| `missing_state` | Missing state parameter |
| `missing_token` | Missing vomeet_calendar_auth_token in state |
| `invalid_token` | Token expired or invalid signature |
| `user_not_found` | Account user not found |
| `account_disabled` | Vomeet account disabled |
| `not_configured` | Google OAuth not configured on account |
| `token_exchange_failed` | Failed to exchange code for tokens |
| `userinfo_failed` | Failed to get Google user info |
