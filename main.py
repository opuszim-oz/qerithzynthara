"""Opus Zim pipeline: generate_pdf | deliver_pdf | cleanup (24h deletion)."""
import os, io, re, json, smtplib, requests
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                PageBreak, Image, ListFlowable, ListItem)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

EVENT = os.environ["EVENT_TYPE"]
PAYLOAD = json.loads(os.environ.get("PAYLOAD") or "{}")
U1 = os.environ["SB_USERS1_URL"]; U1K = os.environ["SB_USERS1_SERVICE_KEY"]
KEYS_URL = os.environ["SB_KEYS_URL"]; KEYS_K = os.environ["SB_KEYS_SERVICE_KEY"]
LIBS = json.loads(os.environ["SB_LIBRARIES_JSON"])       # {"A-Sciences": {url,key}, ...}
PDFB = json.loads(os.environ["SB_PDF_BUCKETS_JSON"])     # [{url,key}, x4]

def sb(url, key, path, method="GET", body=None, headers=None):
    h = {"apikey": key, "Authorization": f"Bearer {key}",
         "Content-Type": "application/json", "Prefer": "return=representation"}
    if headers: h.update(headers)
    r = requests.request(method, f"{url}/rest/v1/{path}", json=body, headers=h, timeout=60)
    r.raise_for_status()
    return r.json() if r.text else None

# ---------- API key rotation ----------
def get_key(provider):
    rows = sb(KEYS_URL, KEYS_K,
              f"api_keys?provider=eq.{provider}&status=eq.active&order=use_count.asc&limit=5")
    if not rows: raise RuntimeError(f"No active {provider} keys")
    return rows

def count_use(row):  # only called on SUCCESS (errors don't deduct credits)
    new = (row["use_count"] or 0) + 1
    patch = {"use_count": new}
    if row.get("max_uses") and new >= row["max_uses"]:
        patch["status"] = "exhausted"
    sb(KEYS_URL, KEYS_K, f"api_keys?id=eq.{row['id']}", "PATCH", patch)

def call_gemini(prompt, files):
    for row in get_key("gemini"):
        try:
            parts = [{"text": prompt}] + [
                {"inline_data": {"mime_type": "application/pdf", "data": f}} for f in files]
            r = requests.post(
                "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent",
                params={"key": row["api_key"]},
                json={"contents": [{"parts": parts}]}, timeout=600)
            r.raise_for_status()
            count_use(row)
            return r.json()["candidates"][0]["content"]["parts"][0]["text"]
        except Exception:
            continue  # rotate to next key; failed call NOT counted
    raise RuntimeError("All Gemini keys failed")

def call_kie_image(prompt):
    for row in get_key("kie"):
        try:
            r = requests.post("https://api.kie.ai/api/v1/gpt4o-image/generate",
                headers={"Authorization": f"Bearer {row['api_key']}"},
                json={"prompt": prompt, "size": "1:1"}, timeout=60)
            r.raise_for_status()
            task = r.json()["data"]["taskId"]
            for _ in range(90):  # poll up to 15 min
                import time; time.sleep(10)
                s = requests.get("https://api.kie.ai/api/v1/gpt4o-image/record-info",
                    headers={"Authorization": f"Bearer {row['api_key']}"},
                    params={"taskId": task}, timeout=30).json()
                if s["data"]["status"] == "SUCCESS":
                    count_use(row)  # success only
                    return requests.get(s["data"]["response"]["resultUrls"][0], timeout=60).content
                if s["data"]["status"] == "FAILED":
                    raise RuntimeError("kie failed")
        except Exception:
            continue
    raise RuntimeError("All Kie keys failed")

# ---------- content extraction ----------
STAGE_RE = re.compile(r"\[STAGE(\d)\](.*?)\[/STAGE\1\]", re.S)
IMGP_RE  = re.compile(r"\[IMAGE_PROMPT_(\d)\](.*?)\[/IMAGE_PROMPT_\1\]", re.S)

def library_for(subject, level):
    cat = ("Sciences" if subject in ("Mathematics","Computer Science","Biology","Physics","Chemistry","Agriculture")
           else "Commercials" if subject in () else "Arts")
    if subject in ("Geography","History","Textile Technology and Design (TTD)"): cat = "Arts"
    return LIBS[f"{level}-{cat}"]

def fetch_library_files(subject, level):
    import base64
    lib = library_for(subject, level)
    h = {"apikey": lib["key"], "Authorization": f"Bearer {lib['key']}"}
    listing = requests.post(f"{lib['url']}/storage/v1/object/list/library",
        headers=h, json={"prefix": f"{subject}/", "limit": 7}, timeout=60).json()
    out = []
    for f in listing[:7]:
        data = requests.get(f"{lib['url']}/storage/v1/object/library/{subject}/{f['name']}",
                            headers=h, timeout=120).content
        out.append(base64.b64encode(data).decode())
    return out

# ---------- PDF building (professional typography, stage-per-page) ----------
def build_pdf(title, subject, level, stages, images):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=2.5*cm, bottomMargin=2.5*cm,
                            leftMargin=2.5*cm, rightMargin=2.5*cm)
    ss = getSampleStyleSheet()
    h1 = ParagraphStyle("H1", parent=ss["Heading1"], spaceAfter=14)
    h2 = ParagraphStyle("H2", parent=ss["Heading2"], spaceAfter=8)
    body = ParagraphStyle("Body", parent=ss["BodyText"], leading=16, spaceAfter=10)
    story = [Spacer(1, 6*cm),
             Paragraph(title.upper(), ParagraphStyle("T", parent=ss["Title"], fontSize=22)),
             Spacer(1, 1*cm),
             Paragraph(f"{subject} — {'Ordinary' if level=='O' else 'Advanced'} Level Project", h2),
             PageBreak()]
    names = ["INTRODUCTION / PROBLEM IDENTIFICATION", "RESEARCH / RELATED IDEAS",
             "POSSIBLE SOLUTIONS / CONCEPT DEVELOPMENT", "DEVELOPMENT",
             "PRESENTATION OF RESULTS", "EVALUATION, CONCLUSION & RECOMMENDATIONS"]
    for i, name in enumerate(names, 1):
        story.append(Paragraph(f"STAGE {i}: {name}", h1))
        text = stages.get(i, "")
        for block in text.split("\n\n"):
            block = block.strip()
            if not block: continue
            if block.startswith("## "):
                story.append(Paragraph(block[3:], h2))
            elif block.startswith("- "):
                story.append(ListFlowable(
                    [ListItem(Paragraph(li[2:].strip(), body)) for li in block.split("\n") if li.startswith("- ")],
                    bulletType="bullet"))
            else:
                story.append(Paragraph(block, body))
        if i == 5:
            for n, img in enumerate(images, 1):
                story.append(PageBreak())
                story.append(Image(io.BytesIO(img), width=12*cm, height=12*cm))
        story.append(PageBreak())
    if story and isinstance(story[-1], PageBreak): story.pop()
    doc.build(story)
    return buf.getvalue()

def make_previews(pdf_bytes):
    """Render page 1-2 previews. Use pypdf to split; upload as separate 2-page pdf."""
    from pypdf import PdfReader, PdfWriter
    r = PdfReader(io.BytesIO(pdf_bytes)); w = PdfWriter()
    for p in r.pages[:2]: w.add_page(p)
    out = io.BytesIO(); w.write(out)
    return out.getvalue()

def upload_pdf(path, data, content_type="application/pdf"):
    for idx, b in enumerate(PDFB):   # bucket failover 1→2→3→4
        r = requests.post(f"{b['url']}/storage/v1/object/pdfs/{path}",
            headers={"apikey": b["key"], "Authorization": f"Bearer {b['key']}",
                     "Content-Type": content_type}, data=data, timeout=120)
        if r.ok: return idx
    raise RuntimeError("All PDF buckets full/failed")

# ---------- event handlers ----------
def handle_generate():
    pid = PAYLOAD["project_id"]
    try:
        subject, level, title = PAYLOAD["subject"], PAYLOAD["level"], PAYLOAD["title"]
        mp = sb(U1, U1K, f"master_prompts?subject=eq.{subject}&level=eq.{level}&engine=eq.gemini")
        prompt = (mp[0]["prompt"] if mp else DEFAULT_GEMINI_PROMPT)
        prompt = prompt.replace("{{SUBJECT}}", subject).replace("{{LEVEL}}", level).replace("{{TITLE}}", title)
        files = fetch_library_files(subject, level)
        raw = call_gemini(prompt, files)
        stages = {int(m[0]): m[1].strip() for m in STAGE_RE.findall(raw)}
        img_prompts = [m[1].strip() for m in IMGP_RE.findall(raw)][:2]
        images = []
        if img_prompts:
            imp = sb(U1, U1K, f"master_prompts?subject=eq.{subject}&level=eq.{level}&engine=eq.kie_image")
            base = imp[0]["prompt"] if imp else "{{IMAGE_RULES}}"
            for p in img_prompts:
                images.append(call_kie_image(base.replace("{{IMAGE_RULES}}", p)))
        pdf = build_pdf(title, subject, level, stages, images)
        preview = make_previews(pdf)
        bucket = upload_pdf(f"{pid}.pdf", pdf)
        upload_pdf(f"{pid}_preview.pdf", preview)
        pb = PDFB[bucket]
        sb(U1, U1K, f"projects?id=eq.{pid}", "PATCH", {
            "status": "generated", "generated_at": datetime.now(timezone.utc).isoformat(),
            "pdf_bucket": str(bucket), "pdf_path": f"{pid}.pdf",
            "preview1_url": f"{pb['url']}/storage/v1/object/public/pdfs/{pid}_preview.pdf",
            "preview2_url": ""})
    except Exception as e:
        sb(U1, U1K, f"projects?id=eq.{pid}", "PATCH", {"status": "failed"})
        raise

def handle_deliver():
    pid = PAYLOAD["project_id"]
    p = sb(U1, U1K, f"projects?id=eq.{pid}")[0]
    b = PDFB[int(p["pdf_bucket"])]
    pdf = requests.get(f"{b['url']}/storage/v1/object/pdfs/{p['pdf_path']}",
        headers={"apikey": b["key"], "Authorization": f"Bearer {b['key']}"}, timeout=120).content
    msg = EmailMessage()
    msg["Subject"] = f"Your Opus Zim Project: {p['title']}"
    msg["From"] = os.environ["SMTP_USER"]; msg["To"] = p["email"]
    msg.set_content("Thank you for using Opus Zim! Your project PDF is attached.\n\n"
                    "Disclaimer: for reference and learning purposes only.")
    msg.add_attachment(pdf, maintype="application", subtype="pdf", filename="OpusZim_Project.pdf")
    with smtplib.SMTP_SSL(os.environ["SMTP_HOST"], 465) as s:
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"]); s.send_message(msg)

def handle_cleanup():
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    rows = sb(U1, U1K, f"projects?status=eq.downloaded&downloaded_at=lt.{cutoff}")
    for p in rows or []:
        b = PDFB[int(p["pdf_bucket"])]
        requests.delete(f"{b['url']}/storage/v1/object/pdfs/{p['pdf_path']}",
            headers={"apikey": b["key"], "Authorization": f"Bearer {b['key']}"}, timeout=60)
        requests.delete(f"{b['url']}/storage/v1/object/pdfs/{p['id']}_preview.pdf",
            headers={"apikey": b["key"], "Authorization": f"Bearer {b['key']}"}, timeout=60)
        sb(U1, U1K, f"projects?id=eq.{p['id']}", "PATCH", {"status": "deleted"})

DEFAULT_GEMINI_PROMPT = """You are an expert Zimbabwean {{LEVEL}}' Level {{SUBJECT}} teacher.
Using the attached textbooks and marking guide, write a complete 6-stage school project titled "{{TITLE}}".
Follow the marking guide structure exactly. Wrap each stage in [STAGE1]...[/STAGE1] through [STAGE6]...[/STAGE6].
Use "## " for sub-headings and "- " for bullet points. Do not number headings.
If Stage 5 needs images, include up to two prompts wrapped as [IMAGE_PROMPT_1]...[/IMAGE_PROMPT_1] and [IMAGE_PROMPT_2]...[/IMAGE_PROMPT_2]; otherwise include none and write text/poem content instead.
No table of contents, bibliography, references, or acknowledgements."""

if EVENT == "generate_pdf": handle_generate()
elif EVENT == "deliver_pdf": handle_deliver()
else: handle_cleanup()
