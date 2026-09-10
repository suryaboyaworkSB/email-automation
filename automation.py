import base64
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import quote
import pandas as pd
import os

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]

def get_flow(client_secrets_file: str | None = None, redirect_uri: str | None = None) -> Flow:
    """
    Backward-compatible OAuth Flow creator.
    - If called without args (as in your app.py), it uses sensible defaults.
    - You can override via env vars or by passing arguments explicitly.
    """
    # Default to your existing hardcoded path/URI if not provided
    default_secrets = r"C:\Users\sboya\OneDrive - dvrpc.org\Documents\email-automation\email-automation-web\credentials.json"
    default_redirect = "https://localhost:5000/callback"

    client_secrets_file = (
        client_secrets_file
        or os.environ.get("GOOGLE_CLIENT_SECRETS_FILE")
        or default_secrets
    )
    redirect_uri = (
        redirect_uri
        or os.environ.get("GOOGLE_REDIRECT_URI")
        or default_redirect
    )

    return Flow.from_client_secrets_file(
        client_secrets_file,
        scopes=SCOPES,
        redirect_uri=redirect_uri
    )


def build_gmaps_email_body(group: pd.DataFrame, mcd: str,
                           mcd_display: str | None = None) -> str:
    """
    Returns an HTML body string listing:
      - One 'Open in Google Maps' link per coordinate
      - A 'View all points together' link (Directions view with waypoints)
    Expects columns 'LATITUDE' and 'LONGITUDE' in `group`.

    `mcd` is the numeric MCD code (used internally for grouping). `mcd_display`
    is the human-readable township name that appears in the email body; if not
    provided, falls back to the MCD code so old callers still work.
    """
    name = mcd_display if mcd_display else mcd

    coords = []
    if "LATITUDE" in group.columns and "LONGITUDE" in group.columns:
        for lat, lon in zip(group["LATITUDE"], group["LONGITUDE"]):
            if pd.notna(lat) and pd.notna(lon):
                coords.append((str(lat), str(lon)))

    if not coords:
        return f"<p>No coordinates found for <b>{name}</b>.</p>"

    # Per-point links
    per_point_links = []
    for i, (lat, lon) in enumerate(coords, 1):
        url = f"https://www.google.com/maps/search/?api=1&query={quote(lat)},{quote(lon)}"
        per_point_links.append(f'<li><a href="{url}">Point {i}: {lat}, {lon}</a></li>')

    # “All points” link (Directions view with waypoints). First point = destination.
    destination = f"{coords[0][0]},{coords[0][1]}"
    waypoints = "%7C".join([f"{lat},{lon}" for (lat, lon) in coords[1:]])
    if waypoints:
        all_points_url = (
            f"https://www.google.com/maps/dir/?api=1"
            f"&destination={quote(destination)}&waypoints={waypoints}"
        )
    else:
        # Only one point → open that point directly
        all_points_url = f"https://www.google.com/maps/search/?api=1&query={quote(destination)}"

    html = f"""
<p>Good Morning,</p>

<p>Beginning next week, the Delaware Valley Regional Planning Commission’s (DVRPC)
<b>Office of Travel Monitoring (OTM)</b> will be conducting traffic data collection
activities in <b>{name}</b>. These activities may include vehicle volume and
classification counts, as well as bicycle and pedestrian counts, using automatic
traffic recording equipment.</p>

<p>The selected locations are part of DVRPC’s ongoing regional data collection 
program and may be situated on state, county, or local roadway facilities. 
Equipment will be installed and retrieved over the next several weeks. Typical 
collection durations are 24–48 hours for vehicle volume and classification counts, 
and up to 8 days for bicycle and pedestrian counts.</p>

<p>All DVRPC equipment will be clearly labeled. If any questions or concerns arise 
regarding the equipment or field activities, please feel free to contact us.</p>

<p><b>Google Maps Links to Count Locations:</b></p>
<ul>
    {''.join(per_point_links)}
</ul>

<p><a href="{all_points_url}"><b>View all locations together in Google Maps</b></a></p>

<p>For questions or additional information, please contact:</p>

<p>
Jonathan Ferullo – <a href="mailto:jferullo@dvrpc.org">jferullo@dvrpc.org</a><br>
Joshua Rocks – Manager, Office of Travel Monitoring –
<a href="mailto:jrocks@dvrpc.org">jrocks@dvrpc.org</a>
</p>

<p>Thank you for your cooperation and support.</p>

<p>Best regards,<br>
DVRPC Office of Travel Monitoring Team</p>
""" 
    return html


def send_emails_with_oauth(bodies_by_recipient: dict, sender_email: str, credentials):
    """
    Send Gmail messages with an HTML body (Google Maps links), no attachments.
    bodies_by_recipient: dict[email] = html_body

    SAFETY: if the env var TEST_REDIRECT_TO is set, every outgoing email is
    rerouted to that single address with a [TEST] banner instead of going to
    the real township. The OTM CC list is also suppressed in test mode. Unset
    the variable to send for real.
    """
    gmail = build("gmail", "v1", credentials=credentials)
    results = []

    # If set, everything goes here instead of the real recipients.
    test_redirect = os.environ.get("TEST_REDIRECT_TO")

    for recipient, html_body in bodies_by_recipient.items():
        try:
            msg = MIMEMultipart("alternative")

            if test_redirect:
                msg["to"] = test_redirect
                msg["subject"] = (
                    f"[TEST -> would have gone to {recipient}] "
                    f"Traffic counting in your municipality"
                )
                banner = (
                    f"<div style='background:#fff3cd;border:1px solid #ffc107;"
                    f"padding:12px;margin-bottom:16px;"
                    f"font-family:Arial,sans-serif;font-size:13px'>"
                    f"<b>TEST MODE</b> &mdash; this email was redirected from "
                    f"<code>{recipient}</code>. "
                    f"Unset <code>TEST_REDIRECT_TO</code> to send for real."
                    f"</div>"
                )
                html_body = banner + html_body
            else:
                msg["to"] = recipient
                # CC the OTM contacts on every notification.
                msg["cc"] = "jferullo@dvrpc.org, jrocks@dvrpc.org"
                msg["subject"] = "Traffic counting in your municipality"

            msg["reply-to"] = sender_email

            # Plain text fallback
            plain_fallback = (
                "Please open this email in an HTML-capable client to view the Google Maps links."
            )

            msg.attach(MIMEText(plain_fallback, "plain"))
            msg.attach(MIMEText(html_body, "html"))

            raw = {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}
            gmail.users().messages().send(userId="me", body=raw).execute()

            if test_redirect:
                results.append(
                    f"[TEST] {recipient} -> redirected to {test_redirect}"
                )
            else:
                results.append(f"Sent to {recipient} (HTML with Google Maps links)")
        except Exception as e:
            results.append(f"{recipient}: send failed -> {e}")

    return results
