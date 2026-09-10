from flask import Flask, render_template, request, redirect, url_for, flash, send_file, session
import pandas as pd
import pyodbc
import os
from werkzeug.utils import secure_filename
from automation import get_flow, send_emails_with_oauth, build_gmaps_email_body
from google.oauth2.credentials import Credentials

app = Flask(__name__)
# Require FLASK_SECRET_KEY to be set in the environment. Refuse to start otherwise
# so we never silently fall back to a well-known key that would let attackers
# forge session cookies.
_secret = os.environ.get('FLASK_SECRET_KEY')
if not _secret:
    raise RuntimeError(
        "FLASK_SECRET_KEY environment variable must be set. "
        "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
    )
app.secret_key = _secret

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app.config['UPLOAD_FOLDER'] = os.path.join(BASE_DIR, 'uploads')
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

EMAIL_LIST_PATH = r"C:\Users\sboya\OneDrive - dvrpc.org\Documents\email-automation\uploads\Final_MCD_Emai_excel.xlsx"
temp_data = {}

def creds_from_session(creds_dict):
    return Credentials(
        token=creds_dict.get('token'),
        refresh_token=creds_dict.get('refresh_token'),
        token_uri=creds_dict.get('token_uri'),
        client_id=creds_dict.get('client_id'),
        client_secret=creds_dict.get('client_secret'),
        scopes=creds_dict.get('scopes'),
    )

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        record_file = request.files.get('record_excel')
        sender_email = request.form.get('email')
        if not record_file or not sender_email:
            flash("Please upload the record‐numbers Excel and provide your email.")
            return redirect(url_for('index'))

        # Sanitize the user-supplied filename to prevent path traversal
        # (e.g. "../../etc/passwd"). secure_filename strips dangerous chars
        # and drops any directory component.
        safe_name = secure_filename(record_file.filename or '')
        if not safe_name:
            flash("Invalid filename for uploaded record file.")
            return redirect(url_for('index'))
        record_path = os.path.join(app.config['UPLOAD_FOLDER'], safe_name)
        record_file.save(record_path)

        try:
            df_records = pd.read_excel(record_path, dtype={'record_num': str})
        except Exception as e:
            flash(f"Failed to read the record Excel file: {e}")
            return redirect(url_for('index'))

        if 'record_num' not in df_records.columns:
            flash("Excel must contain a column named 'record_num'.")
            return redirect(url_for('index'))

        raw_ids = df_records['record_num'].astype(str).str.strip().tolist()
        recordnums = [r for r in raw_ids if r.isdigit()]
        recordnums = list(dict.fromkeys(recordnums))

        if not recordnums:
            flash("No valid numeric record IDs found in the Excel.")
            return redirect(url_for('index'))

        try:
            rec_ints = [int(r) for r in recordnums]
        except ValueError:
            flash("All values in 'record_num' column must be numeric.")
            return redirect(url_for('index'))

        if not os.path.exists(EMAIL_LIST_PATH):
            flash("Township emails file not found on server.")
            return redirect(url_for('index'))

        try:
            df_emails = pd.read_excel(EMAIL_LIST_PATH, dtype={'MCD': str, 'Email': str})
        except Exception as e:
            flash(f"Failed to read the local email list: {e}")
            return redirect(url_for('index'))

        if 'MCD' not in df_emails.columns or 'Email' not in df_emails.columns:
            flash("The local email list must have columns 'MCD' and 'Email'.")
            return redirect(url_for('index'))

        placeholders = ','.join('?' for _ in rec_ints)
        db_path = r"C:\Users\sboya\Downloads\DVRPCTC_NEW_READONLY\DVRPCTC_NEW_READONLY.accdb"
        conn_str = f"DRIVER={{Microsoft Access Driver (*.mdb, *.accdb)}};DBQ={db_path};"
        try:
            conn = pyodbc.connect(conn_str)
            cursor = conn.cursor()
            query = (
                "SELECT RECORDNUM, MCD, LATITUDE, LONGITUDE "
                f"FROM DVRPCTC_NEW_HEADER_VIEW WHERE RECORDNUM IN ({placeholders})"
            )
            cursor.execute(query, rec_ints)
            rows = cursor.fetchall()
            conn.close()
        except Exception as e:
            flash(f"Database query failed: {e}")
            return redirect(url_for('index'))

        df_db = pd.DataFrame([tuple(row) for row in rows],
                             columns=['RECORDNUM', 'MCD', 'LATITUDE', 'LONGITUDE'])

        if df_db.empty:
            flash("No matching records found in the database.")
            return redirect(url_for('index'))

        df_merged = pd.merge(df_db, df_emails, on='MCD', how='inner')

        if df_merged.empty:
            flash("No common MCDs between DB results and email list.")
            return redirect(url_for('index'))
        
        temp_data['df_merged'] = df_merged
        temp_data['sender_email'] = sender_email

        return redirect(url_for('authorize'))
    
    return render_template('index.html')

@app.route('/authorize')
def authorize():
    flow = get_flow()
    authorization_url, state = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true',
        prompt='consent'
    )
    session['oauth_state'] = state
    return redirect(authorization_url)

@app.route('/callback')
def callback():
    state = session.get('oauth_state')
    if not state or state != request.args.get('state'):
        flash("Authorization state mismatch or expired.")
        return redirect(url_for('index'))

    flow = get_flow()

    try:
        flow.fetch_token(authorization_response=request.url)
        credentials = flow.credentials
        
        session['credentials'] = {
            'token': credentials.token,
            'refresh_token': credentials.refresh_token,
            'token_uri': credentials.token_uri,
            'client_id': credentials.client_id,
            'client_secret': credentials.client_secret,
            'scopes': credentials.scopes
        }

        return redirect(url_for('send_emails_logic'))

    except Exception as e:
        flash(f"Authorization failed: {e}")
        return redirect(url_for('index'))

@app.route('/send-emails-logic')
def send_emails_logic():
    df_merged = temp_data.get('df_merged')
    sender_email = temp_data.get('sender_email')
    creds_dict = session.get('credentials')

    if df_merged is None or df_merged.empty or not sender_email or not creds_dict:
        flash("Email data or credentials not found. Please try again.")
        return redirect(url_for('index'))

    credentials = creds_from_session(creds_dict)

    bodies_by_recipient = {}
    skipped = []
    for mcd, group in df_merged.groupby('MCD'):
        email = group['Email'].iloc[0]
        if not email or pd.isna(email):
            skipped.append(mcd)
            continue

        # NOTE: These two lines must stay INSIDE the loop. Previously they sat
        # at the function indent level, so only the last MCD's body was kept
        # and only one email ever went out.
        html_body = build_gmaps_email_body(group, mcd)
        bodies_by_recipient[email] = html_body

    results = send_emails_with_oauth(bodies_by_recipient, sender_email, credentials)

    log_lines = ["=== Email Log ==="] + results
    if skipped:
        log_lines += ["\nSkipped MCDs (no email):"] + skipped
    log_path = os.path.join(app.config['UPLOAD_FOLDER'], 'email_log.txt')
    with open(log_path, 'w') as logf:
        logf.write("\n".join(log_lines))

    temp_data.clear()
    return redirect(url_for('download_log'))

@app.route('/download-log')
def download_log():
    path = os.path.join(app.config['UPLOAD_FOLDER'], 'email_log.txt')
    if os.path.exists(path):
        return send_file(path, as_attachment=True)
    flash('Log file not found.')
    return redirect(url_for('index'))

if __name__ == '__main__':
    # Debug mode is OFF by default. Werkzeug's debug mode exposes an
    # interactive Python console on tracebacks — anyone who can reach the
    # server can run arbitrary code. Only enable it locally by setting
    # FLASK_DEBUG=1 in your shell.
    debug_mode = os.environ.get('FLASK_DEBUG', '').lower() in ('1', 'true', 'yes')
    # Bind to localhost by default so the dev server isn't exposed to the
    # whole network. Override with FLASK_HOST=0.0.0.0 if you actually need
    # LAN access (e.g. for OAuth from another device).
    host = os.environ.get('FLASK_HOST', '127.0.0.1')
    port = int(os.environ.get('FLASK_PORT', '5000'))
    app.run(debug=debug_mode, host=host, port=port)
