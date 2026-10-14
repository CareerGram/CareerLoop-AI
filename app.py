"""CareerLoop AI - combined version.

Features
- Resume upload (PDF) or pasted text, TF-IDF + cosine-similarity job matching
- Job tracker with URL, location, application date, follow-up date, notes
- Evidence-based write-up built only from sentences found in the resume
- Direct Gmail sending with explicit, content-specific confirmation
- Local email drafts (optionally copied to Gmail Drafts if supported)
- Email history
- Automatic, non-destructive migration of the older `applications` table
"""

import hashlib
import json
import re
import sqlite3
from collections import Counter
from datetime import date, datetime
from pathlib import Path

import fitz  # PyMuPDF
import pandas as pd
import streamlit as st
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv is optional
    pass

try:
    from gmail_service import GmailService, GmailAuthError
except ImportError:
    # Older gmail_service.py without GmailAuthError.
    from gmail_service import GmailService

    class GmailAuthError(Exception):
        pass


APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "careerloop.db"
STATUSES = ["Saved", "Preparing", "Applied", "Interview", "Offer", "Rejected", "Withdrawn"]
LEGACY_STATUS_MAP = {"Not applied": "Saved"}

st.set_page_config(page_title="CareerLoop AI", page_icon="🔁", layout="wide")


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _columns(conn, table):
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _table_exists(conn, table):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def init_db():
    """Create tables, add missing columns, and migrate legacy data."""
    with db_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL,
                job_url TEXT NOT NULL DEFAULT '',
                location TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL,
                match_score REAL NOT NULL DEFAULT 0,
                matching_terms TEXT NOT NULL DEFAULT '[]',
                missing_terms TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'Saved',
                date_added TEXT NOT NULL,
                date_applied TEXT,
                follow_up_date TEXT,
                notes TEXT NOT NULL DEFAULT ''
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS email_drafts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER,
                recipient TEXT NOT NULL DEFAULT '',
                subject TEXT NOT NULL,
                body TEXT NOT NULL,
                gmail_draft_id TEXT,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS email_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER,
                recipient TEXT NOT NULL,
                subject TEXT NOT NULL,
                body TEXT NOT NULL,
                status TEXT NOT NULL,
                gmail_message_id TEXT,
                created_at TEXT NOT NULL,
                sent_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        # Columns that older databases may be missing.
        if "legacy_application_id" not in _columns(conn, "jobs"):
            conn.execute("ALTER TABLE jobs ADD COLUMN legacy_application_id INTEGER")
        if "job_id" not in _columns(conn, "email_records"):
            conn.execute("ALTER TABLE email_records ADD COLUMN job_id INTEGER")

        _migrate_legacy_applications(conn)


def _migrate_legacy_applications(conn):
    """Copy rows from the old `applications` table into `jobs` (idempotent).

    The old table is left untouched, so nothing is lost if you roll back.
    """
    if not _table_exists(conn, "applications"):
        return

    already = {
        r["legacy_application_id"]
        for r in conn.execute(
            "SELECT legacy_application_id FROM jobs WHERE legacy_application_id IS NOT NULL"
        )
    }
    for app in conn.execute("SELECT * FROM applications ORDER BY id").fetchall():
        if app["id"] in already:
            continue
        status = LEGACY_STATUS_MAP.get(app["status"], app["status"])
        if status not in STATUSES:
            status = "Saved"
        added = (app["created_at"] or datetime.now().isoformat())[:10]
        conn.execute(
            """
            INSERT INTO jobs(company, role, description, match_score, status,
                             date_added, notes, legacy_application_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                app["company"] or "",
                app["role"] or "Untitled role",
                app["job_description"] or "",
                app["match_score"] or 0,
                status,
                added,
                app["notes"] or "",
                app["id"],
            ),
        )

    # Re-link old email records to the migrated jobs.
    if "application_id" in _columns(conn, "email_records"):
        conn.execute("""
            UPDATE email_records
            SET job_id = (
                SELECT j.id FROM jobs j
                WHERE j.legacy_application_id = email_records.application_id
            )
            WHERE job_id IS NULL AND application_id IS NOT NULL
        """)


def setting_get(key, default=""):
    with db_connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def setting_set(key, value):
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def setting_delete(key):
    with db_connect() as conn:
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))


# --------------------------------------------------------------------------
# Resume reading and matching
# --------------------------------------------------------------------------
def extract_pdf_text(uploaded_file):
    parts = []
    with fitz.open(stream=uploaded_file.getvalue(), filetype="pdf") as doc:
        for page in doc:
            parts.append(page.get_text())
    return "\n".join(parts).strip()


def clean_text(text):
    return re.sub(r"\s+", " ", text or "").strip()


STOP_WORDS = {
    "with", "from", "that", "this", "have", "will", "your", "you", "are",
    "for", "and", "the", "our", "their", "they", "who", "what", "when",
    "where", "which", "into", "about", "work", "role", "team", "years",
    "year", "using", "used", "such", "can", "able", "must", "should",
    "not", "all", "any", "more", "than", "also", "across", "based",
    "strong", "good", "excellent", "including", "required", "preferred",
    "responsibilities", "requirements", "experience", "knowledge",
}


def term_list(text):
    """Unique-term counter used for the matching / missing term lists."""
    words = re.findall(r"[A-Za-z][A-Za-z0-9+#.-]{1,}", (text or "").lower())
    words = [w.strip(".-") for w in words]
    return Counter(w for w in words if len(w) > 2 and w not in STOP_WORDS)


def calculate_match(resume_text, job_text):
    """TF-IDF (1-2 grams) cosine similarity, plus matching/missing term lists."""
    resume_text = clean_text(resume_text)
    job_text = clean_text(job_text)
    job_terms = term_list(job_text)
    ranked_job_terms = [t for t, _ in job_terms.most_common()]

    if not resume_text or not job_text:
        return 0.0, [], ranked_job_terms[:20]

    try:
        vectorizer = TfidfVectorizer(ngram_range=(1, 2), stop_words="english")
        matrix = vectorizer.fit_transform([resume_text, job_text])
        score = float(cosine_similarity(matrix[0:1], matrix[1:2])[0][0]) * 100
    except ValueError:
        score = 0.0

    resume_terms = set(term_list(resume_text))
    matching = [t for t in ranked_job_terms if t in resume_terms]
    missing = [t for t in ranked_job_terms if t not in resume_terms]
    return round(score, 1), matching[:20], missing[:20]


def resume_evidence_sentences(resume_text, job_text, limit=3):
    """Pick resume sentences most similar to the job description.

    Returns (sentences_in_resume_order, shared_terms). Only text that exists in
    the resume is returned; nothing is generated or embellished.
    """
    raw = re.split(r"(?<=[.!?])\s+|\n+|[•●▪◦■]", resume_text or "")
    candidates = []
    for s in raw:
        s = clean_text(s).lstrip("-–—* ").strip()
        words = s.split()
        if len(words) < 5 or len(s) < 25 or len(s) > 300:
            continue
        if "@" in s or "http" in s.lower() or re.search(r"\d{8,}", s.replace(" ", "")):
            continue  # contact details
        if s not in candidates:
            candidates.append(s)
    if not candidates or not clean_text(job_text):
        return [], []

    try:
        vec = TfidfVectorizer(stop_words="english")
        matrix = vec.fit_transform(candidates + [clean_text(job_text)])
        sims = cosine_similarity(matrix[-1], matrix[:-1])[0]
    except ValueError:
        return [], []

    job_vocab = set(term_list(job_text))
    scored = []
    for idx, (sentence, sim) in enumerate(zip(candidates, sims)):
        shared = job_vocab & set(term_list(sentence))
        if sim > 0 and len(shared) >= 2:
            scored.append((sim, idx, sentence, shared))
    scored.sort(reverse=True)
    top = sorted(scored[:limit], key=lambda x: x[1])  # restore resume order

    sentences = [t[2] for t in top]
    shared_terms = []
    for t in top:
        for term in sorted(t[3]):
            if term not in shared_terms:
                shared_terms.append(term)
    return sentences, shared_terms[:8]


def build_writeup(resume_text, job_text, role, company):
    """Evidence-based paragraph using only information found in the resume."""
    sentences, terms = resume_evidence_sentences(resume_text, job_text)
    if not sentences:
        return ""
    target = role.strip() or "this position"
    if company.strip():
        target += f" at {company.strip()}"
    evidence = "; ".join(s.rstrip(".;:, ") for s in sentences)
    paragraph = (
        f"For {target}, the parts of my background that relate most directly to "
        f"the job description are the following, as described in my resume: {evidence}."
    )
    if terms:
        paragraph += f" These connect to requirements such as {', '.join(terms)}."
    return paragraph


# --------------------------------------------------------------------------
# Job and email data access
# --------------------------------------------------------------------------
def add_job(company, role, url, location, description, score, matching, missing):
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO jobs(company, role, job_url, location, description, match_score,
                             matching_terms, missing_terms, status, date_added)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Saved', ?)
            """,
            (company, role, url, location, description, score,
             json.dumps(matching), json.dumps(missing), date.today().isoformat()),
        )


def fetch_jobs():
    with db_connect() as conn:
        return pd.read_sql_query(
            "SELECT * FROM jobs ORDER BY date_added DESC, id DESC", conn
        )


def fetch_job(job_id):
    if job_id is None:
        return None
    with db_connect() as conn:
        return conn.execute("SELECT * FROM jobs WHERE id = ?", (int(job_id),)).fetchone()


def update_job(job_id, **fields):
    allowed = {
        "company", "role", "job_url", "location", "status", "date_applied",
        "follow_up_date", "notes", "match_score", "matching_terms", "missing_terms",
    }
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    assignments = ", ".join(f"{k} = ?" for k in fields)
    with db_connect() as conn:
        conn.execute(
            f"UPDATE jobs SET {assignments} WHERE id = ?", (*fields.values(), int(job_id))
        )


def job_label(row):
    return f'#{row["id"]} · {row["role"] or "Untitled role"} — {row["company"] or "Company not entered"}'


def safe_json_list(value):
    try:
        data = json.loads(value or "[]")
        return data if isinstance(data, list) else []
    except (TypeError, ValueError):
        return []


def safe_date(value):
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def save_email_draft(job_id, recipient, subject, body, gmail_draft_id=None):
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO email_drafts(job_id, recipient, subject, body, gmail_draft_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (job_id, recipient, subject, body, gmail_draft_id,
             datetime.now().isoformat(timespec="seconds")),
        )


def fetch_drafts():
    with db_connect() as conn:
        return pd.read_sql_query(
            """
            SELECT e.*, j.role, j.company
            FROM email_drafts e LEFT JOIN jobs j ON e.job_id = j.id
            ORDER BY e.id DESC
            """,
            conn,
        )


def save_email_record(job_id, recipient, subject, body, message_id):
    now = datetime.now().isoformat(timespec="seconds")
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO email_records(job_id, recipient, subject, body, status,
                                      gmail_message_id, created_at, sent_at)
            VALUES (?, ?, ?, ?, 'Sent', ?, ?, ?)
            """,
            (job_id, recipient, subject, body, message_id, now, now),
        )


def fetch_email_records():
    with db_connect() as conn:
        return conn.execute(
            """
            SELECT r.*, j.role, j.company
            FROM email_records r LEFT JOIN jobs j ON r.job_id = j.id
            ORDER BY r.id DESC
            """
        ).fetchall()


def get_gmail_service():
    """Support both GmailService(APP_DIR) and GmailService() constructors."""
    try:
        return GmailService(APP_DIR)
    except TypeError:
        return GmailService()


def build_default_body(name, company, role, writeup):
    greeting = f"Hello {company.strip()} hiring team," if company.strip() else "Hello,"
    role_text = role.strip() or "the position"
    parts = [
        greeting,
        f"I am writing to express my interest in the {role_text} role.",
    ]
    if writeup:
        parts.append(writeup)
    parts.append(
        "I would be glad to share my resume or any additional information you need. "
        "Thank you for your time and consideration."
    )
    parts.append(f"Best regards,\n{name.strip() or 'Your Name'}")
    return "\n\n".join(parts)


# --------------------------------------------------------------------------
# App start
# --------------------------------------------------------------------------
init_db()

st.title("🔁 CareerLoop AI")
st.caption("By Team CareerGram · Job matching, tracking, and human-approved email")
st.info(
    "The match score is a rough TF-IDF text-similarity signal. It is not a hiring "
    "probability. Emails are sent only after you explicitly confirm the exact content."
)

if "last_send_message" in st.session_state:
    st.success(st.session_state.pop("last_send_message"))

# ---- Sidebar: profile and resume -----------------------------------------
with st.sidebar:
    st.header("Your profile")
    profile_name = st.text_input("Your name", value=setting_get("profile_name", ""))
    if st.button("Save profile name"):
        setting_set("profile_name", profile_name.strip())
        st.success("Profile name saved locally.")

    st.subheader("Resume")
    uploaded_resume = st.file_uploader("Upload resume (PDF)", type=["pdf"], key="resume_upload")
    pasted_resume = st.text_area("Or paste resume text", height=140, key="pasted_resume")

    new_text = ""
    if uploaded_resume:
        try:
            new_text = extract_pdf_text(uploaded_resume)
        except Exception as exc:
            st.error(f"Could not read this PDF: {exc}")
    if not new_text and pasted_resume.strip():
        new_text = pasted_resume.strip()

    saved_resume = setting_get("resume_text", "")
    if new_text and new_text != saved_resume:
        setting_set("resume_text", new_text)  # stored locally in careerloop.db
        saved_resume = new_text
    resume_text = new_text or saved_resume

    if resume_text:
        st.success(f"Resume text ready ({len(resume_text)} characters).")
        with st.expander("Preview resume text"):
            st.text_area("Resume text", resume_text[:6000], height=180, disabled=True)
        if st.button("Delete saved resume text"):
            setting_delete("resume_text")
            st.info("Saved resume text deleted. Clear the upload/paste box above too.")
            st.rerun()
        st.caption("Resume text is kept in your local careerloop.db and is not sent to any AI service.")
    else:
        st.warning("Upload a text-based PDF or paste your resume to calculate matches.")

tab_overview, tab_add, tab_tracker, tab_email, tab_history, tab_gmail = st.tabs([
    "Overview", "Add a job", "Application tracker",
    "Recruiter email", "Email history", "Gmail connection",
])

# ---- Overview --------------------------------------------------------------
with tab_overview:
    jobs_df = fetch_jobs()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Jobs tracked", len(jobs_df))
    c2.metric("Applied", int((jobs_df["status"] == "Applied").sum()) if not jobs_df.empty else 0)
    c3.metric("Interviews", int((jobs_df["status"] == "Interview").sum()) if not jobs_df.empty else 0)
    c4.metric("Offers", int((jobs_df["status"] == "Offer").sum()) if not jobs_df.empty else 0)

    st.subheader("Recent opportunities")
    if jobs_df.empty:
        st.write("No jobs yet. Open **Add a job** to start.")
    else:
        display = jobs_df[[
            "id", "company", "role", "location", "match_score",
            "status", "date_applied", "follow_up_date",
        ]].copy()
        display.columns = [
            "ID", "Company", "Role", "Location", "Match score",
            "Status", "Date applied", "Follow-up date",
        ]
        st.dataframe(display, hide_index=True)

    st.subheader("How the match score works")
    st.write(
        "Your resume and the job description are converted to TF-IDF vectors (single words "
        "and two-word phrases) and compared with cosine similarity. It ranks how similar "
        "the wording is. It does not verify skills or predict recruiter decisions."
    )

# ---- Add a job -------------------------------------------------------------
with tab_add:
    st.subheader("Add and assess a job")
    with st.form("add_job_form"):
        company = st.text_input("Company")
        role = st.text_input("Job title *")
        job_url = st.text_input("Job URL (optional)")
        location = st.text_input("Location (optional)")
        description = st.text_area("Paste the job description *", height=220)
        submitted = st.form_submit_button("Calculate match and save job")

    if submitted:
        if not role.strip() or not description.strip():
            st.error("Please enter a job title and job description.")
        elif not resume_text:
            st.error("Add your resume in the sidebar first.")
        else:
            score, matching, missing = calculate_match(resume_text, description)
            add_job(
                company.strip(), role.strip(), job_url.strip(), location.strip(),
                description.strip(), score, matching, missing,
            )
            st.success(f"Saved. Text-similarity score: {score:.1f}/100")
            st.write("**Terms found in both texts**")
            st.write(", ".join(matching) if matching else "No exact terms found.")
            st.write("**Job terms not found in the resume text**")
            st.write(", ".join(missing) if missing else "No unmatched terms found.")
            st.caption(
                "These are text overlaps, not verified skills. Only add skills and "
                "experience you genuinely have."
            )

# ---- Tracker ---------------------------------------------------------------
with tab_tracker:
    st.subheader("Application tracker")
    jobs_df = fetch_jobs()
    if jobs_df.empty:
        st.info("Add a job first.")
    else:
        options = {job_label(row): int(row["id"]) for _, row in jobs_df.iterrows()}
        selected_label = st.selectbox("Choose an application", list(options.keys()))
        selected_id = options[selected_label]
        job = fetch_job(selected_id)

        left, right = st.columns(2)
        with left:
            st.metric("Text similarity", f'{job["match_score"]:.1f}/100')
            st.write(f'**Added:** {job["date_added"]}')
            st.write(f'**URL:** {job["job_url"] or "Not provided"}')
            st.write(f'**Location:** {job["location"] or "Not provided"}')
            st.write("**Terms found in both texts**")
            st.write(", ".join(safe_json_list(job["matching_terms"])) or "None identified")
            st.write("**Terms not found in resume text**")
            st.write(", ".join(safe_json_list(job["missing_terms"])) or "None identified")
            if st.button("Re-run match with current resume", key=f"rematch_{selected_id}"):
                if not resume_text:
                    st.error("Add your resume in the sidebar first.")
                else:
                    score, matching, missing = calculate_match(resume_text, job["description"])
                    update_job(
                        selected_id, match_score=score,
                        matching_terms=json.dumps(matching),
                        missing_terms=json.dumps(missing),
                    )
                    st.rerun()

        with right:
            with st.form(f"update_job_{selected_id}"):
                u_company = st.text_input("Company", value=job["company"] or "")
                u_role = st.text_input("Job title", value=job["role"] or "")
                u_url = st.text_input("Job URL", value=job["job_url"] or "")
                u_location = st.text_input("Location", value=job["location"] or "")
                current_status = job["status"] if job["status"] in STATUSES else "Saved"
                status = st.selectbox("Status", STATUSES, index=STATUSES.index(current_status))
                applied_date = st.date_input(
                    "Date applied (optional)", value=safe_date(job["date_applied"])
                )
                follow_date = st.date_input(
                    "Follow-up date (optional)", value=safe_date(job["follow_up_date"])
                )
                notes = st.text_area("Notes", value=job["notes"] or "")
                update = st.form_submit_button("Save tracker changes")

            if update:
                update_job(
                    selected_id,
                    company=u_company.strip(),
                    role=u_role.strip() or job["role"],
                    job_url=u_url.strip(),
                    location=u_location.strip(),
                    status=status,
                    date_applied=applied_date.isoformat() if applied_date else None,
                    follow_up_date=follow_date.isoformat() if follow_date else None,
                    notes=notes.strip(),
                )
                st.success("Application updated.")
                st.rerun()

        with st.expander("Job description"):
            st.text(job["description"])

        today = date.today().isoformat()
        due = jobs_df[
            jobs_df["follow_up_date"].notna()
            & (jobs_df["follow_up_date"] <= today)
            & (~jobs_df["status"].isin(["Rejected", "Offer", "Withdrawn"]))
        ]
        if not due.empty:
            st.warning(
                f"{len(due)} application(s) have a follow-up date today or earlier: "
                + ", ".join(f'#{r["id"]} {r["role"]}' for _, r in due.iterrows())
                + ". Review before contacting anyone."
            )

# ---- Recruiter email (direct send) ----------------------------------------
with tab_email:
    st.subheader("Send an email to a recruiter")
    st.write(
        "Write the message, review it, tick the confirmation box, and send it through "
        "Gmail. Changing any field removes your confirmation."
    )

    jobs_df = fetch_jobs()
    job_rows = {int(r["id"]): r for _, r in jobs_df.iterrows()}
    job_choices = [None] + list(job_rows.keys())

    def format_job(job_id):
        return "No linked job" if job_id is None else job_label(job_rows[job_id])

    if st.session_state.get("em_job") not in job_choices:
        st.session_state["em_job"] = None

    selected_job_id = st.selectbox(
        "Link to a job (optional)", job_choices, format_func=format_job, key="em_job"
    )
    include_writeup = st.checkbox(
        "Include an evidence-based write-up from my resume",
        value=True,
        key="em_include",
        help="Built only from sentences that already exist in your resume.",
    )
    selected_job = job_rows.get(selected_job_id)

    writeup = ""
    if selected_job is not None and include_writeup and resume_text:
        writeup = build_writeup(
            resume_text, selected_job["description"],
            selected_job["role"], selected_job["company"],
        )
        if not writeup:
            st.caption("No resume sentences matched this job closely enough for a write-up.")

    # Reset the editable fields whenever the job or write-up option changes.
    context = (selected_job_id, include_writeup)
    if st.session_state.get("em_context") != context:
        st.session_state["em_context"] = context
        st.session_state["em_subject"] = (
            f"Application for {selected_job['role']}" if selected_job is not None else ""
        )
        st.session_state["em_body"] = build_default_body(
            setting_get("profile_name", ""),
            selected_job["company"] if selected_job is not None else "",
            selected_job["role"] if selected_job is not None else "",
            writeup,
        )

    if writeup:
        with st.expander("Suggested write-up (uses only text from your resume)", expanded=False):
            st.write(writeup)
            st.caption("Check that it reads correctly and that every statement is accurate.")

    recipient = st.text_input(
        "Recruiter's email address", placeholder="recruiter@company.com", key="em_recipient"
    )
    subject = st.text_input("Email subject", key="em_subject")
    body = st.text_area("Email message", height=280, key="em_body")

    st.caption(
        "Personalize the message. Check the name, company, role, and every claim before sending."
    )

    st.divider()
    st.subheader("Review before sending")
    st.write("**To:**", recipient.strip() or "Not entered")
    st.write("**Subject:**", subject.strip() or "Not entered")
    with st.expander("Preview the email"):
        st.text(body or "Your message will appear here.")

    st.warning(
        "An email accepted by Gmail cannot be recalled by this app. "
        "Gmail will receive exactly what is shown above."
    )

    # The confirmation key depends on the exact content, so editing anything
    # (or a successful send) produces a fresh, unticked checkbox.
    content_hash = hashlib.sha256(
        f"{recipient.strip()}|{subject.strip()}|{body.strip()}".encode("utf-8")
    ).hexdigest()[:12]
    confirm_key = f"em_confirm_{content_hash}_{st.session_state.get('send_nonce', 0)}"

    approved = st.checkbox(
        "I have checked the recipient, subject, and message, and I approve sending this email.",
        key=confirm_key,
    )

    mark_applied = False
    if selected_job is not None:
        mark_applied = st.checkbox(
            "After sending, mark the linked job as Applied (sets today as the date applied)",
            value=True,
            key="em_mark_applied",
            help="Only changes jobs that are currently Saved or Preparing. "
                 "Later stages such as Interview or Offer are never changed.",
        )

    btn_send, btn_draft = st.columns([1, 1])
    send_clicked = btn_send.button("📨 Send email", type="primary", disabled=not approved)
    draft_clicked = btn_draft.button("💾 Save draft locally")

    if draft_clicked:
        if not subject.strip() or not body.strip():
            st.error("Subject and body are required to save a draft.")
        else:
            save_email_draft(selected_job_id, recipient.strip(), subject.strip(), body.strip())
            st.success("Draft saved locally.")

    if send_clicked:
        email_ok = re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", recipient.strip())
        if not approved:
            st.error("Please confirm before sending.")
        elif not email_ok:
            st.error("Please enter a valid-looking email address.")
        elif not subject.strip():
            st.error("Please enter an email subject.")
        elif not body.strip():
            st.error("Please write your email message.")
        else:
            try:
                with st.spinner("Sending through Gmail..."):
                    gmail = get_gmail_service()
                    message_id = gmail.send_email(
                        recipient=recipient.strip(),
                        subject=subject.strip(),
                        body=body.strip(),
                    )
                if not message_id:
                    st.error(
                        "Gmail returned no message ID. Check your Sent folder before trying again."
                    )
                else:
                    save_email_record(
                        selected_job_id, recipient.strip(), subject.strip(),
                        body.strip(), message_id,
                    )
                    st.session_state["send_nonce"] = st.session_state.get("send_nonce", 0) + 1
                    status_note = ""
                    if (
                        mark_applied
                        and selected_job is not None
                        and selected_job["status"] in ("Saved", "Preparing")
                    ):
                        update_job(
                            selected_job_id,
                            status="Applied",
                            date_applied=selected_job["date_applied"] or date.today().isoformat(),
                        )
                        status_note = " The linked job was marked as Applied."
                    st.session_state["last_send_message"] = (
                        f"Gmail accepted the email (message ID: {message_id})."
                        f"{status_note} See the Email history tab."
                    )
                    # Rerun so Overview and Tracker (rendered earlier in the script)
                    # reload the new status.
                    st.rerun()
            except GmailAuthError as exc:
                st.error(str(exc))
            except Exception as exc:
                st.error(f"We could not confirm that the email was sent: {exc}")
                st.warning(
                    "Before retrying, check Gmail's Sent folder. A connection problem can "
                    "happen even after Gmail has accepted a message."
                )

    st.divider()
    st.subheader("Saved drafts")
    drafts_df = fetch_drafts()
    if drafts_df.empty:
        st.write("No drafts saved yet.")
    else:
        for _, d in drafts_df.iterrows():
            label = f'#{int(d["id"])} · {d["subject"]} · {d["recipient"] or "no recipient"}'
            with st.expander(label):
                st.text(d["body"])

                def load_draft(row=d):
                    jid = int(row["job_id"]) if pd.notna(row["job_id"]) else None
                    if jid not in job_choices:
                        jid = None
                    st.session_state["em_job"] = jid
                    st.session_state["em_context"] = (jid, st.session_state.get("em_include", True))
                    st.session_state["em_recipient"] = row["recipient"] or ""
                    st.session_state["em_subject"] = row["subject"]
                    st.session_state["em_body"] = row["body"]

                st.button(
                    "Load into the send form above",
                    key=f'load_draft_{int(d["id"])}',
                    on_click=load_draft,
                )

# ---- Email history --------------------------------------------------------
with tab_history:
    st.subheader("Email history")
    records = fetch_email_records()
    if not records:
        st.write("No email records yet.")
    else:
        for rec in records:
            heading = f'#{rec["id"]} · {rec["status"]} · {rec["recipient"]} · {rec["subject"]}'
            with st.expander(heading):
                st.write("**Recipient:**", rec["recipient"])
                st.write("**Subject:**", rec["subject"])
                st.write("**Status:**", rec["status"])
                if rec["role"]:
                    st.write("**Linked job:**", f'{rec["role"]} — {rec["company"] or "Company not entered"}')
                st.write("**Created:**", rec["created_at"])
                st.write("**Sent at:**", rec["sent_at"] or "Not recorded")
                st.write("**Gmail message ID:**", rec["gmail_message_id"] or "Not available")
                st.write("**Message:**")
                st.text(rec["body"])

# ---- Gmail connection -----------------------------------------------------
with tab_gmail:
    st.subheader("Gmail connection")
    st.write(
        "Gmail access uses Google's OAuth consent flow. credentials.json and token.json "
        "stay in this project folder and must never be committed to GitHub."
    )
    if not (APP_DIR / "credentials.json").exists():
        st.warning(
            "credentials.json not found. Create an OAuth Desktop App credential in Google "
            "Cloud and place the downloaded file next to app.py."
        )
    else:
        st.success("credentials.json found.")

    col1, col2 = st.columns(2)
    with col1:
        if st.button("Authorize / test Gmail access"):
            try:
                service = get_gmail_service()
                if hasattr(service, "get_profile_email"):
                    address = service.get_profile_email()
                    st.session_state["gmail_address"] = address
                    st.success(f"Connected as {address}")
                else:
                    st.success("Gmail authorization succeeded.")
                st.session_state["gmail_connected"] = True
            except GmailAuthError as exc:
                st.error(str(exc))
            except Exception as exc:
                st.error(f"Gmail connection failed: {exc}")
    with col2:
        if st.button("Forget connection status"):
            st.session_state.pop("gmail_connected", None)
            st.session_state.pop("gmail_address", None)
            st.info("Indicator cleared. To revoke access, use your Google Account security settings.")

    if st.session_state.get("gmail_connected"):
        st.success(f"Connected: {st.session_state.get('gmail_address', '')}")

    st.caption(
        "Sending requires a Gmail scope that allows it (gmail.send or gmail.compose). "
        "The app never reads your inbox."
    )

    # Optional: copy a local draft into Gmail Drafts (only if gmail_service supports it).
    st.divider()
    st.subheader("Copy a local draft into Gmail Drafts")
    drafts_df = fetch_drafts()
    unsynced = drafts_df[drafts_df["gmail_draft_id"].isna()] if not drafts_df.empty else drafts_df
    try:
        can_draft = hasattr(GmailService, "create_draft")
    except Exception:
        can_draft = False
    if not can_draft:
        st.caption("Your gmail_service.py has no create_draft method, so this option is hidden.")
    elif unsynced.empty:
        st.write("No unsynced local drafts.")
    else:
        choices = {f'#{int(r["id"])} · {r["subject"]}': int(r["id"]) for _, r in unsynced.iterrows()}
        picked = st.selectbox("Choose a local draft", list(choices.keys()))
        if st.button("Create draft in Gmail"):
            try:
                draft_id = choices[picked]
                with db_connect() as conn:
                    row = conn.execute(
                        "SELECT * FROM email_drafts WHERE id = ?", (draft_id,)
                    ).fetchone()
                gmail_id = get_gmail_service().create_draft(
                    row["recipient"], row["subject"], row["body"]
                )
                with db_connect() as conn:
                    conn.execute(
                        "UPDATE email_drafts SET gmail_draft_id = ? WHERE id = ?",
                        (gmail_id, draft_id),
                    )
                st.success(f"Gmail draft created (ID: {gmail_id}).")
            except GmailAuthError as exc:
                st.error(str(exc))
            except Exception as exc:
                st.error(f"Could not create Gmail draft: {exc}")

st.divider()
st.caption("CareerLoop AI · Team CareerGram · Review all AI-assisted content before use.")