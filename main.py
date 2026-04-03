import qrcode
import secrets
import string
import os
import io
import base64
from fastapi import FastAPI, HTTPException, Header, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from supabase import create_client
from datetime import datetime
from urllib.parse import urlparse
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

supabase_url = os.getenv("SUPABASE_URL")
supabase_key = os.getenv("SUPABASE_KEY")
supabase = create_client(supabase_url, supabase_key)
session_secret = os.getenv("SESSION_SECRET", secrets.token_hex(32))

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=session_secret)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# ─── MCP Server ───────────────────────────────────────────────────────────────

mcp = FastMCP(
    name="QR Code Generator",
    instructions=(
        "Generate QR codes from URLs. "
        "Use your API key (obtained from the dashboard) to authenticate. "
        "Generated QR codes are saved to your account history."
    ),
)


@mcp.tool()
def generate_qr(url: str, api_key: str) -> dict:
    """Generate a QR code for a URL and save it to the user's account.

    Args:
        url: The URL to encode in the QR code (must start with http:// or https://).
        api_key: Your QR Generator API key from the dashboard (starts with 'qr_').

    Returns:
        A dict with image_url (public URL of the QR image) and target_url.
    """
    if not url or not url.strip():
        raise ValueError("url is required")

    url = url.strip()

    # Validate API key against Supabase
    key_data = (
        supabase.table("api_keys")
        .select("user_id")
        .eq("api_key", api_key)
        .eq("is_active", True)
        .execute()
    )
    if not key_data.data:
        raise ValueError("Invalid or inactive API key")

    user_id = key_data.data[0]["user_id"]

    # Generate QR
    img_bytes = make_qr_bytes(url)
    filename = get_unique_name(url)
    storage_path = f"generated/{filename}"

    supabase.storage.from_("qr-codes").upload(
        path=storage_path,
        file=img_bytes,
        file_options={"content-type": "image/png"},
    )
    public_url = supabase.storage.from_("qr-codes").get_public_url(storage_path)

    supabase.table("qr_codes").insert({
        "user_id": user_id,
        "target_url": url,
        "file_name": filename,
        "image_url": public_url,
    }).execute()

    return {"image_url": public_url, "target_url": url}


app.mount("/mcp", mcp.streamable_http_app())


# ─── Helpers ──────────────────────────────────────────────────────────────────

def get_current_user(request: Request):
    return request.session.get("user")


def make_qr_bytes(url: str) -> bytes:
    img = qrcode.make(url)
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


def get_unique_name(url: str) -> str:
    domain = urlparse(url).netloc.replace(".", "_") or "link"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{domain}_{timestamp}.png"


QR_LIMIT = 5


def check_qr_limit(user_id: str):
    result = supabase.table("qr_codes").select("id").eq("user_id", user_id).execute()
    if len(result.data) >= QR_LIMIT:
        raise HTTPException(
            status_code=429,
            detail=f"Limit reached ({QR_LIMIT} QR codes). Delete one to generate more.",
        )


# ─── Pages ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def home(request: Request, error: str = None, message: str = None):
    user = get_current_user(request)
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"user": user, "error": error, "message": message},
    )


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/?error=Please+sign+in+to+access+the+dashboard", status_code=302)

    qr_result = (
        supabase.table("qr_codes")
        .select("*")
        .eq("user_id", user["id"])
        .order("created_at", desc=True)
        .execute()
    )
    key_result = (
        supabase.table("api_keys")
        .select("api_key")
        .eq("user_id", user["id"])
        .eq("is_active", True)
        .execute()
    )

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "user": user,
            "qr_codes": qr_result.data,
            "api_key": key_result.data[0]["api_key"] if key_result.data else None,
        },
    )


# ─── Auth ─────────────────────────────────────────────────────────────────────

@app.post("/auth/register")
async def register(request: Request, email: str = Form(...), password: str = Form(...)):
    try:
        result = supabase.auth.sign_up({"email": email, "password": password})
        if result.user and result.session:
            request.session["user"] = {"id": result.user.id, "email": result.user.email}
            return RedirectResponse("/dashboard", status_code=302)
        # Supabase may require email confirmation
        return RedirectResponse(
            "/?message=Check+your+email+to+confirm+your+account", status_code=302
        )
    except Exception as e:
        return RedirectResponse(f"/?error={str(e)}", status_code=302)


@app.post("/auth/login")
async def login(request: Request, email: str = Form(...), password: str = Form(...)):
    try:
        result = supabase.auth.sign_in_with_password({"email": email, "password": password})
        if result.user:
            request.session["user"] = {"id": result.user.id, "email": result.user.email}
            return RedirectResponse("/dashboard", status_code=302)
        return RedirectResponse("/?error=Invalid+credentials", status_code=302)
    except Exception:
        return RedirectResponse("/?error=Invalid+credentials", status_code=302)


@app.get("/auth/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=302)


# ─── QR Generation ────────────────────────────────────────────────────────────

@app.post("/api/generate")
async def generate_qr_anonymous(request: Request):
    """Anonymous – returns base64 image only, nothing is saved."""
    body = await request.json()
    url = body.get("url", "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL is required")
    img_bytes = make_qr_bytes(url)
    return {"image": base64.b64encode(img_bytes).decode()}


@app.post("/api/generate/save")
async def generate_qr_save(request: Request):
    """Authenticated – saves QR to Supabase storage and logs to DB."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    check_qr_limit(user["id"])

    body = await request.json()
    url = body.get("url", "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL is required")

    img_bytes = make_qr_bytes(url)
    filename = get_unique_name(url)
    storage_path = f"generated/{filename}"

    supabase.storage.from_("qr-codes").upload(
        path=storage_path,
        file=img_bytes,
        file_options={"content-type": "image/png"},
    )
    public_url = supabase.storage.from_("qr-codes").get_public_url(storage_path)

    insert_result = supabase.table("qr_codes").insert({
        "user_id": user["id"],
        "target_url": url,
        "file_name": filename,
        "image_url": public_url,
    }).execute()

    return {
        "id": insert_result.data[0]["id"],
        "image": base64.b64encode(img_bytes).decode(),
        "image_url": public_url,
        "target_url": url,
        "file_name": filename,
    }


@app.post("/api/generate/text")
async def generate_qr_text(request: Request):
    """Authenticated – generates a plain-text QR code, saves to Supabase."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    check_qr_limit(user["id"])

    body = await request.json()
    text = body.get("text", "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Text is required")
    if len(text) > 500:
        raise HTTPException(status_code=400, detail="Text must be 500 characters or fewer")

    img_bytes = make_qr_bytes(text)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"text_{timestamp}.png"
    storage_path = f"generated/{filename}"

    supabase.storage.from_("qr-codes").upload(
        path=storage_path,
        file=img_bytes,
        file_options={"content-type": "image/png"},
    )
    public_url = supabase.storage.from_("qr-codes").get_public_url(storage_path)

    insert_result = supabase.table("qr_codes").insert({
        "user_id": user["id"],
        "target_url": text,
        "file_name": filename,
        "image_url": public_url,
    }).execute()

    return {
        "id": insert_result.data[0]["id"],
        "image": base64.b64encode(img_bytes).decode(),
        "image_url": public_url,
        "target_url": text,
        "file_name": filename,
    }


@app.post("/api/generate/wifi")
async def generate_qr_wifi(request: Request):
    """Authenticated – generates a WiFi QR code, saves to Supabase."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    check_qr_limit(user["id"])

    body = await request.json()
    ssid = body.get("ssid", "").strip()
    password = body.get("password", "").strip()
    auth = body.get("auth", "WPA").strip()
    hidden = body.get("hidden", False)

    if not ssid:
        raise HTTPException(status_code=400, detail="SSID is required")
    if auth not in ("WPA", "WEP", "nopass"):
        raise HTTPException(status_code=400, detail="Invalid security type")

    hidden_str = "true" if hidden else "false"
    wifi_string = f"WIFI:T:{auth};S:{ssid};P:{password};H:{hidden_str};;"

    img_bytes = make_qr_bytes(wifi_string)
    safe_ssid = "".join(c if c.isalnum() else "_" for c in ssid)
    filename = f"wifi_{safe_ssid}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    storage_path = f"generated/{filename}"

    supabase.storage.from_("qr-codes").upload(
        path=storage_path,
        file=img_bytes,
        file_options={"content-type": "image/png"},
    )
    public_url = supabase.storage.from_("qr-codes").get_public_url(storage_path)

    insert_result = supabase.table("qr_codes").insert({
        "user_id": user["id"],
        "target_url": wifi_string,
        "file_name": filename,
        "image_url": public_url,
    }).execute()

    return {
        "id": insert_result.data[0]["id"],
        "image": base64.b64encode(img_bytes).decode(),
        "image_url": public_url,
        "target_url": wifi_string,
        "file_name": filename,
        "ssid": ssid,
    }


# ─── QR Delete ────────────────────────────────────────────────────────────────

@app.delete("/api/qr/{qr_id}")
async def delete_qr(qr_id: str, request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    result = supabase.table("qr_codes").select("file_name, user_id").eq("id", qr_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="QR code not found")
    if result.data[0]["user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="Not authorized")

    file_name = result.data[0]["file_name"]
    try:
        supabase.storage.from_("qr-codes").remove([f"generated/{file_name}"])
    except Exception:
        pass  # Continue even if storage delete fails

    supabase.table("qr_codes").delete().eq("id", qr_id).execute()
    return {"status": "deleted"}


# ─── API Key Management ───────────────────────────────────────────────────────

@app.post("/api/keys/generate")
async def generate_api_key_endpoint(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Deactivate any existing keys for this user
    supabase.table("api_keys").update({"is_active": False}).eq("user_id", user["id"]).execute()

    alphabet = string.ascii_letters + string.digits
    new_key = "qr_" + "".join(secrets.choice(alphabet) for _ in range(32))

    supabase.table("api_keys").insert({
        "user_id": user["id"],
        "api_key": new_key,
        "is_active": True,
    }).execute()

    return {"api_key": new_key}


# ─── Public API (external use with user API key) ──────────────────────────────

@app.post("/api/v1/generate")
async def public_api_generate(request: Request, x_api_key: str = Header(...)):
    """External API endpoint. Pass your API key in the X-Api-Key header."""
    key_data = (
        supabase.table("api_keys")
        .select("user_id")
        .eq("api_key", x_api_key)
        .eq("is_active", True)
        .execute()
    )
    if not key_data.data:
        raise HTTPException(status_code=401, detail="Invalid or inactive API key")

    user_id = key_data.data[0]["user_id"]
    body = await request.json()
    url = body.get("url", "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL is required")

    img_bytes = make_qr_bytes(url)
    filename = get_unique_name(url)
    storage_path = f"generated/{filename}"

    supabase.storage.from_("qr-codes").upload(
        path=storage_path,
        file=img_bytes,
        file_options={"content-type": "image/png"},
    )
    public_url = supabase.storage.from_("qr-codes").get_public_url(storage_path)

    supabase.table("qr_codes").insert({
        "user_id": user_id,
        "target_url": url,
        "file_name": filename,
        "image_url": public_url,
    }).execute()

    return {"status": "success", "image_url": public_url, "target_url": url}
