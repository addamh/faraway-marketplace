from __future__ import annotations

import hashlib
import hmac
import logging
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware

from . import jobs, scheduler
from .config import settings
from .db import get_db, init_db
from .geocode import GeocodeError, geocode
from .models import KM_PER_MILE, AppSetting, Listing, Match, ScrapeRun, Search, User, utcnow
from .security import csrf_token, hash_password, new_invite_code, verify_password
from .sources.facebook import normalize_fb_location

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
BASE = Path(__file__).parent


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(title="Faraway Finds", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    same_site="lax",
    https_only=settings.secure_cookies,
    max_age=60 * 60 * 24 * 30,
)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
app.mount("/images", StaticFiles(directory=settings.images_dir, check_dir=False), name="images")
templates = Jinja2Templates(directory=BASE / "templates")


# --------------------------------------------------------------------------- template helpers


def timeago(dt: datetime | None) -> str:
    if dt is None:
        return "never"
    secs = (utcnow() - dt).total_seconds()
    future = secs < 0
    secs = abs(secs)
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if secs >= size:
            n = int(secs // size)
            text = f"{n} {unit}{'s' if n != 1 else ''}"
            return f"in {text}" if future else f"{text} ago"
    return "just now"


def image_src(listing: Listing) -> str | None:
    if listing.image_file:
        return f"/images/{listing.image_file}"
    return listing.image_url


templates.env.filters["timeago"] = timeago
templates.env.globals["image_src"] = image_src
templates.env.globals["source_name"] = settings.source


class LoginRequired(Exception):
    pass


class Forbidden(Exception):
    pass


@app.exception_handler(LoginRequired)
async def _login_required(request: Request, _exc: LoginRequired):
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(Forbidden)
async def _forbidden(request: Request, _exc: Forbidden):
    return HTMLResponse("Forbidden", status_code=403)


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    uid = request.session.get("uid")
    user = db.get(User, uid) if uid else None
    if user is None:
        request.session.pop("uid", None)
        raise LoginRequired()
    return user


def admin_user(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise Forbidden()
    return user


def flash(request: Request, message: str, kind: str = "ok") -> None:
    request.session.setdefault("flash", []).append({"msg": message, "kind": kind})


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def safe_next(url: str | None, default: str = "/finds") -> str:
    """Only allow same-site relative paths as post-action destinations."""
    if url and url.startswith("/") and not url.startswith("//") and "\\" not in url:
        return url
    return default


async def read_form(request: Request) -> dict[str, str]:
    form = await request.form()
    sent = str(form.get("csrf", ""))
    expected = request.session.get("csrf", "")
    if not expected or not hmac.compare_digest(sent, expected):
        raise Forbidden()
    return {k: str(v) for k, v in form.items()}


def new_count(db: Session, user: User) -> int:
    return db.scalar(
        select(func.count(func.distinct(Match.listing_id)))
        .join(Search, Search.id == Match.search_id)
        .where(Search.user_id == user.id, Match.status == "new")
    ) or 0


def render(request: Request, name: str, db: Session | None = None, user: User | None = None, **ctx) -> HTMLResponse:
    ctx.update(
        user=user,
        flashes=request.session.pop("flash", []),
        csrf=csrf_token(request.session),
        nav_new=new_count(db, user) if (db is not None and user is not None) else 0,
        path=request.url.path,
    )
    return templates.TemplateResponse(request, name, ctx)


def get_setting(db: Session, key: str) -> str | None:
    row = db.get(AppSetting, key)
    return row.value if row else None


def set_setting(db: Session, key: str, value: str) -> None:
    row = db.get(AppSetting, key)
    if row:
        row.value = value
    else:
        db.add(AppSetting(key=key, value=value))
    db.commit()


def invite_code(db: Session) -> str:
    code = get_setting(db, "invite_code")
    if not code:
        code = settings.invite_code or new_invite_code()
        set_setting(db, "invite_code", code)
    return code


def _float(raw: str | None) -> float | None:
    raw = (raw or "").strip().replace(",", ".")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


# --------------------------------------------------------------------------- auth


@app.get("/healthz")
def healthz():
    return {"ok": True, "source": settings.source, "scrape_running": jobs.is_running()}


@app.get("/")
def home(request: Request):
    return redirect("/finds" if request.session.get("uid") else "/login")


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)):
    if db.scalar(select(func.count(User.id))) == 0:
        return redirect("/register")
    return render(request, "login.html")


@app.post("/login")
async def login(request: Request, db: Session = Depends(get_db)):
    form = await read_form(request)
    user = db.scalar(select(User).where(User.email == form.get("email", "").strip().lower()))
    if user is None or not verify_password(form.get("password", ""), user.password_hash):
        flash(request, "That email and password don't match.", "error")
        return redirect("/login")
    request.session["uid"] = user.id
    return redirect("/finds")


@app.post("/logout")
async def logout(request: Request):
    await read_form(request)
    request.session.clear()
    return redirect("/login")


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request, db: Session = Depends(get_db), code: str = ""):
    first = db.scalar(select(func.count(User.id))) == 0
    return render(request, "register.html", first=first, code=code)


@app.post("/register")
async def register(request: Request, db: Session = Depends(get_db)):
    form = await read_form(request)
    first = db.scalar(select(func.count(User.id))) == 0
    email = form.get("email", "").strip().lower()
    name = form.get("name", "").strip()
    password = form.get("password", "")
    code = form.get("invite", "").strip()
    back = f"/register?code={quote(code)}"
    if not first and not hmac.compare_digest(code, invite_code(db)):
        flash(request, "That invite code isn't right. Ask whoever runs this app for the current one.", "error")
        return redirect(back)
    if "@" not in email or not name:
        flash(request, "Name and a valid email are required.", "error")
        return redirect(back)
    if len(password) < 8:
        flash(request, "Use a password of at least 8 characters.", "error")
        return redirect(back)
    if db.scalar(select(User).where(User.email == email)):
        flash(request, "There's already an account with that email.", "error")
        return redirect(back)
    user = User(email=email, name=name[:120], password_hash=hash_password(password), is_admin=first)
    db.add(user)
    db.commit()
    request.session["uid"] = user.id
    flash(request, f"Welcome, {user.first_name}! Set up your pickup area, then add what you're hunting for.")
    return redirect("/profile")


# --------------------------------------------------------------------------- finds


STATUS_FILTERS = {"new": ["new"], "all": ["new", "seen", "saved"], "saved": ["saved"], "hidden": ["hidden"]}


@app.get("/finds", response_class=HTMLResponse)
def finds(
    request: Request,
    show: str = "all",
    search: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    statuses = STATUS_FILTERS.get(show, STATUS_FILTERS["all"])
    q = (
        select(Match)
        .join(Search, Search.id == Match.search_id)
        .where(Search.user_id == user.id, Match.status.in_(statuses))
        .options(selectinload(Match.listing), selectinload(Match.helper), selectinload(Match.search))
        .order_by(Match.first_seen_at.desc())
    )
    if search:
        q = q.where(Match.search_id == search)
    rows = db.scalars(q).all()

    # Group: helper -> listing -> card (a listing can match several of your searches).
    groups: OrderedDict[int, dict] = OrderedDict()
    for m in rows:
        g = groups.setdefault(m.helper_id, {"helper": m.helper, "cards": OrderedDict()})
        card = g["cards"].setdefault(
            m.listing_id,
            {
                "listing": m.listing,
                "queries": [],
                "status": m.status,
                "found": m.first_seen_at,
                "stale": (utcnow() - m.listing.last_seen_at).days >= 2,
            },
        )
        if m.search.query not in card["queries"]:
            card["queries"].append(m.search.query)
        if m.status == "new" or (m.status == "saved" and card["status"] != "new"):
            card["status"] = m.status
        card["found"] = max(card["found"], m.first_seen_at)
    for g in groups.values():
        g["cards"] = sorted(g["cards"].values(), key=lambda c: (c["status"] != "new", -c["found"].timestamp()))
    ordered = sorted(groups.values(), key=lambda g: (-sum(c["status"] == "new" for c in g["cards"]), g["helper"].name))

    last_run = db.scalar(select(ScrapeRun).order_by(ScrapeRun.started_at.desc()).limit(1))
    helper_count = sum(1 for h in db.scalars(select(User).where(User.is_helper.is_(True))) if h.has_area and h.id != user.id)
    return render(
        request,
        "finds.html",
        db,
        user,
        groups=ordered,
        show=show,
        search_id=search,
        searches=user.searches,
        last_run=last_run,
        next_run=scheduler.next_run_time(),
        helper_count=helper_count,
        total=sum(len(g["cards"]) for g in ordered),
    )


def _user_matches_for_listing(db: Session, user: User, listing_id: str):
    return db.scalars(
        select(Match).join(Search, Search.id == Match.search_id).where(
            Search.user_id == user.id, Match.listing_id == listing_id
        )
    ).all()


@app.post("/finds/{listing_id}/status")
async def set_status(listing_id: str, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    form = await read_form(request)
    status = form.get("status")
    if status not in {"seen", "saved", "hidden"}:
        raise Forbidden()
    for m in _user_matches_for_listing(db, user, listing_id):
        m.status = status
    db.commit()
    return redirect(safe_next(form.get("next")))


@app.post("/finds/mark-seen")
async def mark_seen(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    form = await read_form(request)
    matches = db.scalars(
        select(Match).join(Search, Search.id == Match.search_id).where(Search.user_id == user.id, Match.status == "new")
    ).all()
    for m in matches:
        m.status = "seen"
    db.commit()
    return redirect(safe_next(form.get("next")))


# --------------------------------------------------------------------------- searches


def _search_from_form(search: Search, form: dict[str, str]) -> str | None:
    query = " ".join(form.get("query", "").split())[:200]
    if len(query) < 2:
        return "Enter what you're looking for."
    lo, hi = _float(form.get("min_price")), _float(form.get("max_price"))
    if lo is not None and hi is not None and lo > hi:
        return "Min price is higher than max price."
    search.query = query
    search.min_price, search.max_price = lo, hi
    search.strict = form.get("strict") == "on"
    search.must_include = form.get("must_include", "").strip()[:300]
    search.exclude = form.get("exclude", "").strip()[:300]
    return None


@app.get("/searches", response_class=HTMLResponse)
def searches_page(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    counts = dict(
        db.execute(
            select(Match.search_id, func.count(func.distinct(Match.listing_id)))
            .join(Search, Search.id == Match.search_id)
            .where(Search.user_id == user.id, Match.status != "hidden")
            .group_by(Match.search_id)
        ).all()
    )
    helpers = [
        h for h in db.scalars(select(User).where(User.is_helper.is_(True)).order_by(User.name))
        if h.has_area and (h.id != user.id or settings.include_own_area)
    ]
    return render(request, "searches.html", db, user, searches=user.searches, counts=counts, helpers=helpers)


@app.post("/searches")
async def create_search(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    form = await read_form(request)
    search = Search(user_id=user.id)
    error = _search_from_form(search, form)
    if error:
        flash(request, error, "error")
        return redirect("/searches")
    db.add(search)
    db.commit()
    flash(request, f"Added “{search.query}”. It'll run in the next daily scan.")
    return redirect("/searches")


def _own_search(db: Session, user: User, search_id: int) -> Search:
    search = db.get(Search, search_id)
    if search is None or search.user_id != user.id:
        raise Forbidden()
    return search


@app.post("/searches/{search_id}")
async def update_search(search_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    form = await read_form(request)
    search = _own_search(db, user, search_id)
    action = form.get("action", "save")
    if action == "delete":
        db.delete(search)
        db.commit()
        flash(request, "Search deleted.")
    elif action == "toggle":
        search.active = not search.active
        db.commit()
    else:
        error = _search_from_form(search, form)
        if error:
            flash(request, error, "error")
            db.rollback()
        else:
            db.commit()
            flash(request, "Search updated.")
    return redirect("/searches")


# --------------------------------------------------------------------------- profile & people


@app.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    unit = user.radius_unit or "mi"
    radius_value = round(user.radius_km / KM_PER_MILE) if unit == "mi" else round(user.radius_km)
    return render(request, "profile.html", db, user, radius_value=radius_value, unit=unit)


@app.post("/profile")
async def save_profile(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    form = await read_form(request)
    name = form.get("name", "").strip()
    if name:
        user.name = name[:120]
    user.contact = form.get("contact", "").strip()[:200]
    user.is_helper = form.get("is_helper") == "on"

    unit = "km" if form.get("radius_unit") == "km" else "mi"
    radius = _float(form.get("radius")) or (20 if unit == "mi" else 30)
    radius_km = radius * KM_PER_MILE if unit == "mi" else radius
    user.radius_unit = unit
    user.radius_km = max(1.0, min(radius_km, 500.0))

    fb_loc_raw = form.get("fb_location", "").strip()
    if fb_loc_raw:
        fb_loc = normalize_fb_location(fb_loc_raw)
        if fb_loc is None:
            flash(request, "That doesn't look like a Marketplace location or URL; left it unchanged.", "error")
        else:
            user.fb_location = fb_loc
    elif "fb_location" in form:
        user.fb_location = None

    area_q = " ".join(form.get("area", "").split())
    if area_q and area_q != (user.area_query or ""):
        try:
            place = await run_in_threadpool(geocode, area_q)
        except GeocodeError as exc:
            flash(request, str(exc), "error")
            db.commit()
            return redirect("/profile")
        user.area_query, user.area_label, user.lat, user.lng = area_q, place.label, place.lat, place.lng
    elif not area_q:
        user.area_query = user.area_label = user.lat = user.lng = None

    if user.is_helper and not user.has_area:
        flash(request, "Add your city so we know where to search for others.", "error")
    else:
        flash(request, "Profile saved.")
    db.commit()
    return redirect("/profile")


@app.post("/profile/password")
async def change_password(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    form = await read_form(request)
    if not verify_password(form.get("current", ""), user.password_hash):
        flash(request, "Current password is wrong.", "error")
    elif len(form.get("new", "")) < 8:
        flash(request, "Use a password of at least 8 characters.", "error")
    else:
        user.password_hash = hash_password(form["new"])
        db.commit()
        flash(request, "Password changed.")
    return redirect("/profile")


@app.get("/people", response_class=HTMLResponse)
def people(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    users = db.scalars(select(User).options(selectinload(User.searches)).order_by(User.name)).all()
    return render(request, "people.html", db, user, people=users)


@app.get("/my-area", response_class=HTMLResponse)
def my_area(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = db.scalars(
        select(Match)
        .where(Match.helper_id == user.id, Match.status != "hidden")
        .options(selectinload(Match.listing), selectinload(Match.search).selectinload(Search.user))
        .order_by(Match.first_seen_at.desc())
        .limit(300)
    ).all()
    buyers: OrderedDict[int, dict] = OrderedDict()
    for m in rows:
        b = buyers.setdefault(m.search.user_id, {"buyer": m.search.user, "cards": OrderedDict()})
        card = b["cards"].setdefault(
            m.listing_id,
            {
                "listing": m.listing,
                "queries": [],
                "found": m.first_seen_at,
                "status": None,
                "stale": (utcnow() - m.listing.last_seen_at).days >= 2,
            },
        )
        if m.search.query not in card["queries"]:
            card["queries"].append(m.search.query)
    for b in buyers.values():
        b["cards"] = list(b["cards"].values())
    return render(request, "my_area.html", db, user, buyers=list(buyers.values()))


# --------------------------------------------------------------------------- admin


def source_status() -> dict:
    if settings.source == "facebook":
        has = settings.fb_storage_state is not None and settings.fb_storage_state.exists()
        return {"ok": has, "detail": f"Cookies file {'found' if has else 'missing'}: {settings.fb_storage_state}"}
    if settings.source == "serpapi":
        return {"ok": bool(settings.serpapi_key), "detail": "API key set" if settings.serpapi_key else "SERPAPI_KEY is not set"}
    return {"ok": True, "detail": "Fake listings for trying the app out. Set SOURCE to serpapi or facebook for real data."}


@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request, db: Session = Depends(get_db), user: User = Depends(admin_user)):
    runs = db.scalars(select(ScrapeRun).order_by(ScrapeRun.started_at.desc()).limit(20)).all()
    users = db.scalars(select(User).order_by(User.created_at)).all()
    tasks = jobs.plan_tasks(db, settings)
    return render(
        request,
        "admin.html",
        db,
        user,
        runs=runs,
        users=users,
        invite=invite_code(db),
        running=jobs.is_running(),
        next_run=scheduler.next_run_time(),
        source=source_status(),
        planned=len(tasks),
        cap=settings.max_requests_per_run,
        settings=settings,
        base_url=str(request.base_url).rstrip("/"),
    )


@app.post("/admin/run")
async def admin_run(request: Request, user: User = Depends(admin_user)):
    await read_form(request)
    if jobs.start_background_run("manual"):
        flash(request, "Scan started. Refresh this page to follow along.")
    else:
        flash(request, "A scan is already running.", "error")
    return redirect("/admin")


@app.post("/admin/invite")
async def admin_rotate_invite(request: Request, db: Session = Depends(get_db), user: User = Depends(admin_user)):
    await read_form(request)
    set_setting(db, "invite_code", new_invite_code())
    flash(request, "New invite code generated; the old one no longer works.")
    return redirect("/admin")


@app.post("/admin/users/{user_id}")
async def admin_user_action(user_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(admin_user)):
    form = await read_form(request)
    target = db.get(User, user_id)
    if target is None or target.id == user.id:
        flash(request, "You can't change your own account here.", "error")
        return redirect("/admin")
    action = form.get("action")
    if action == "delete":
        db.execute(Match.__table__.delete().where(Match.helper_id == target.id))
        db.delete(target)
        flash(request, f"Removed {target.name}.")
    elif action == "toggle-admin":
        target.is_admin = not target.is_admin
        flash(request, f"{target.name} is {'now' if target.is_admin else 'no longer'} an admin.")
    db.commit()
    return redirect("/admin")


# --------------------------------------------------------------------------- demo images


@app.get("/demo-image/{listing_id}.svg")
def demo_image(listing_id: str):
    h = hashlib.sha1(listing_id.encode()).digest()
    hue1, hue2 = h[0] * 360 // 256, (h[0] * 360 // 256 + 40 + h[1] % 80) % 360
    shape = h[2] % 3
    shapes = [
        '<rect x="70" y="60" width="100" height="150" rx="10" fill="#fff" fill-opacity=".85"/>'
        '<rect x="85" y="75" width="70" height="55" rx="4" fill="hsl({h},45%,30%)"/>'
        '<circle cx="140" cy="175" r="9" fill="hsl({h},45%,30%)"/><path d="M90 167h24M102 155v24" stroke="hsl({h},45%,30%)" stroke-width="7"/>',
        '<circle cx="120" cy="125" r="60" fill="#fff" fill-opacity=".85"/><circle cx="120" cy="125" r="18" fill="hsl({h},45%,30%)"/>',
        '<rect x="55" y="80" width="130" height="90" rx="14" fill="#fff" fill-opacity=".85"/><rect x="75" y="100" width="50" height="50" rx="6" fill="hsl({h},45%,30%)"/>'
        '<circle cx="155" cy="125" r="14" fill="hsl({h},45%,30%)"/>',
    ]
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 240 240"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
        f'<stop offset="0" stop-color="hsl({hue1},70%,62%)"/><stop offset="1" stop-color="hsl({hue2},70%,48%)"/></linearGradient></defs>'
        '<rect width="240" height="240" fill="url(#g)"/>' + shapes[shape].replace("{h}", str(hue1)) + "</svg>"
    )
    return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})
