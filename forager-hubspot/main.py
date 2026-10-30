import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

HS = "https://api.hubapi.com"
FG = f"https://api-v2.forager.ai/api/{os.environ['FORAGER_ACCOUNT_ID']}/datastorage"
DEFAULT_TITLES = [t.strip() for t in os.getenv("DEFAULT_TITLES", "CEO,CTO,CFO,VP,Head,Director").split(",")]
LIMIT = int(os.getenv("CONTACT_LIMIT", "5"))
EXTRAS = os.getenv("INCLUDE_EXTRAS") == "1"
ALIASES = {
    "vp": "vice president",
    "ceo": "chief executive officer",
    "cto": "chief technology officer",
    "cfo": "chief financial officer",
    "coo": "chief operating officer",
    "cmo": "chief marketing officer",
}
PROPS = {
    "contacts": ("contactinformation", ["employer_domain", "employer_linkedin_url", "forager_enriched"]),
    "companies": ("companyinformation", ["forager_enriched"]),
}

http = httpx.AsyncClient(timeout=60)
log = logging.getLogger("app")


async def hs(method, path, **kw):
    r = await http.request(method, HS + path, headers={"Authorization": f"Bearer {os.environ['HUBSPOT_TOKEN']}"}, **kw)
    if r.is_error and r.status_code != 409:
        log.error("HubSpot %s %s: %s", r.status_code, path, r.text)
    r.raise_for_status()
    return r.json() if r.content else None


async def fg(path, body):
    h = {"X-API-KEY": os.environ["FORAGER_API_KEY"]}
    r = await http.post(f"{FG}/{path}/", json=body, headers=h)
    if r.status_code == 405:
        r = await http.get(f"{FG}/{path}/", params=body, headers=h)
    if r.status_code == 404:
        return {}
    if r.status_code == 402:
        raise HTTPException(402, "Forager credits exhausted")
    if r.is_error:
        log.error("Forager %s %s: %s", r.status_code, path, r.text)
    r.raise_for_status()
    return r.json()


def clean(d):
    return {k: str(v) for k, v in d.items() if v not in (None, "")}


def li(x):
    return ((x or {}).get("linkedin_info") or {}).get("public_profile_url")


def place(loc):
    loc = loc or {}
    t = {o["place_type"]: o["name"].split(",")[0].strip() for o in loc.get("osm_locations", [])}
    n = [x.strip() for x in loc.get("name", "").split(",")]
    return {
        "city": t.get("city") or (n[0] if len(n) > 2 else ""),
        "state": t.get("state") or (n[1] if len(n) > 2 else ""),
        "country": t.get("country") or n[-1],
    }


def company_props(o):
    a = (o.get("addresses") or [{}])[0]
    return clean({
        "name": o["name"],
        "domain": o.get("domain"),
        "description": o.get("description"),
        "numberofemployees": o.get("employees_amount"),
        "annualrevenue": (o.get("finance_info") or {}).get("revenue"),
        "linkedin_company_page": li(o),
        "founded_year": (o.get("founded_date") or "")[:4],
        "city": a.get("city"),
        "state": a.get("state"),
        "country": a.get("country"),
        "forager_enriched": "true",
    })


def contact_props(r, email, phone):
    p, o = r["person"], r.get("organization") or {}
    return clean({
        "firstname": p["first_name"],
        "lastname": p.get("last_name"),
        "jobtitle": r.get("role_title"),
        "hs_linkedin_url": li(p),
        "email": email,
        "phone": phone,
        **place(p.get("location")),
        "company": o.get("name"),
        "employer_domain": o.get("domain"),
        "employer_linkedin_url": li(o),
        "linkedinbio": p.get("description") if EXTRAS else None,
        "forager_enriched": "true",
    })


def variants(title):
    t = title.strip().lower()
    out = {t}
    for short, full in ALIASES.items():
        if re.search(rf"\b{short}\b", t):
            out.add(re.sub(rf"\b{short}\b", full, t))
        if full in t:
            out.add(t.replace(full, short))
    return out


def title_ok(r, titles):
    t = (r.get("role_title") or "").lower()
    found = any(re.search(rf"\b{re.escape(v)}\b", t) for x in titles for v in variants(x))
    return found and not any(w in t for w in ("assistant", "intern"))


def extras(p):
    return {"headline": p.get("headline"), "about": p.get("description"), "skills": [s["name"] for s in p.get("skills", [])][:10]}


async def find(obj, prop, val):
    body = {"filterGroups": [{"filters": [{"propertyName": prop, "operator": "EQ", "value": val}]}], "limit": 1}
    res = (await hs("POST", f"/crm/v3/objects/{obj}/search", json=body))["results"]
    return res[0]["id"] if res else None


async def upsert(obj, props, *keys):
    for k in keys:
        if props.get(k) and (i := await find(obj, k, props[k])):
            await hs("PATCH", f"/crm/v3/objects/{obj}/{i}", json={"properties": props})
            return i
    return (await hs("POST", f"/crm/v3/objects/{obj}", json={"properties": props}))["id"]


async def link(cid, coid):
    await hs("PUT", f"/crm/v4/objects/contacts/{cid}/associations/default/companies/{coid}")


async def reveal(pid):
    async def rows(path):
        r = await fg(path, {"person_id": pid})
        data = r if isinstance(r, list) else r.get("data", [])
        if not data:
            log.warning("empty %s for %s: %s", path, pid, r)
        return data

    emails = sorted(await rows("person_contacts_lookup/work_emails"), key=lambda e: e.get("validation_status") != "valid")
    phones = await rows("person_contacts_lookup/phone_numbers")
    return (emails[0]["email"] if emails else None), (phones[0]["phone_number"] if phones else None)


async def company_id(domain):
    return await upsert("companies", {"domain": domain, "forager_enriched": "true"}, "domain")


async def enrich_company(cid):
    domain = (await hs("GET", f"/crm/v3/objects/companies/{cid}", params={"properties": "domain"}))["properties"].get("domain")
    if not domain:
        raise HTTPException(422, "company has no domain")
    res = (await fg("organization_search", {"domains": [domain]})).get("search_results")
    if not res:
        raise HTTPException(404, "company not found in Forager")
    best = [o for o in res if o.get("domain") == domain] or res
    props = company_props(max(best, key=lambda o: o.get("employees_amount") or 0))
    await hs("PATCH", f"/crm/v3/objects/companies/{cid}", json={"properties": props})
    return {"id": cid, "properties": props}


async def enrich_contact(cid):
    c = (await hs("GET", f"/crm/v3/objects/contacts/{cid}", params={"properties": "hs_linkedin_url,email"}))["properties"]
    if c.get("hs_linkedin_url"):
        slug = c["hs_linkedin_url"].rstrip("/").rsplit("/", 1)[-1]
        d = await fg("person_detail_lookup", {"linkedin_public_identifier": slug})
    elif c.get("email"):
        d = await fg("person_detail_reverse_lookup/by_email", {"email": c["email"]})
    else:
        raise HTTPException(422, "contact needs a LinkedIn URL or email")
    if not d:
        raise HTTPException(404, "person not found in Forager")
    role = next((x for x in d["roles"] if x["is_current"]), None) or next(iter(d["roles"]), {})
    r = {"person": d, "role_title": role.get("role_title"), "organization": role.get("organization")}
    email, phone = await reveal(d["id"])
    props = contact_props(r, email, phone)
    await hs("PATCH", f"/crm/v3/objects/contacts/{cid}", json={"properties": props})
    domain = (r["organization"] or {}).get("domain")
    if domain and (coid := await find("companies", "domain", domain)):
        await link(cid, coid)
    return {"id": cid, "properties": props, **extras(d)}


async def find_contacts(coid, titles, limit):
    domain = (await hs("GET", f"/crm/v3/objects/companies/{coid}", params={"properties": "domain"}))["properties"]["domain"]
    query = " OR ".join(f'"{v}"' for x in titles for v in sorted(variants(x)))
    body = {"organization_domains": [domain], "role_is_current": True, "role_title": query}
    out, seen = [], set()
    for page in range(1, 4):
        rows = (await fg("person_role_search", {**body, "page": page})).get("search_results", [])
        for r in rows:
            pid = r["person"]["id"]
            if len(out) == limit:
                break
            url = li(r["person"])
            if pid in seen or not title_ok(r, titles) or (url and await find("contacts", "hs_linkedin_url", url)):
                continue
            seen.add(pid)
            email, phone = await reveal(pid)
            props = contact_props(r, email, phone)
            cid = await upsert("contacts", props, "hs_linkedin_url", "email")
            await link(cid, coid)
            out.append({"id": cid, "properties": props, **extras(r["person"])})
        if not rows or len(out) == limit:
            break
    return out


async def handle(kind, oid):
    try:
        obj = "companies" if kind.startswith("company") else "contacts"
        if (await hs("GET", f"/crm/v3/objects/{obj}/{oid}", params={"properties": "forager_enriched"}))["properties"].get("forager_enriched"):
            return
        if obj == "contacts":
            await enrich_contact(oid)
            return
        await enrich_company(oid)
        assoc = await hs("GET", f"/crm/v4/objects/companies/{oid}/associations/contacts")
        if assoc["results"]:
            for a in assoc["results"]:
                await enrich_contact(a["toObjectId"])
        else:
            await find_contacts(oid, DEFAULT_TITLES, LIMIT)
    except Exception:
        log.exception("webhook failed: %s %s", kind, oid)


def verified(req, body):
    ts = req.headers.get("X-HubSpot-Request-Timestamp", "0")
    if abs(time.time() * 1000 - int(ts)) > 300_000:
        return False
    msg = (req.method + os.environ["WEBHOOK_URL"] + body.decode() + ts).encode()
    sig = base64.b64encode(hmac.new(os.environ["HUBSPOT_CLIENT_SECRET"].encode(), msg, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(sig, req.headers.get("X-HubSpot-Signature-v3", ""))


def auth(x_api_key: str = Header()):
    if not hmac.compare_digest(x_api_key, os.environ["API_KEY"]):
        raise HTTPException(401)


class Req(BaseModel):
    company_id: str | None = None
    contact_id: str | None = None
    domain: str | None = None
    titles: list[str] = DEFAULT_TITLES
    limit: int = LIMIT


api = APIRouter(dependencies=[Depends(auth)])


@api.post("/enrich/company")
async def enrich_company_route(r: Req):
    return await enrich_company(r.company_id or await company_id(r.domain))


@api.post("/enrich/contact")
async def enrich_contact_route(r: Req):
    return await enrich_contact(r.contact_id)


@api.post("/find-contacts")
async def find_contacts_route(r: Req):
    return await find_contacts(r.company_id or await company_id(r.domain), r.titles, r.limit)


@asynccontextmanager
async def lifespan(_):
    for obj, (group, names) in PROPS.items():
        for n in names:
            try:
                await hs("POST", f"/crm/v3/properties/{obj}", json={"name": n, "label": n, "type": "string", "fieldType": "text", "groupName": group})
            except httpx.HTTPStatusError as e:
                if e.response.status_code != 409:
                    raise
    yield


app = FastAPI(lifespan=lifespan)
app.include_router(api)


@app.exception_handler(httpx.HTTPStatusError)
async def upstream_error(_, e):
    return JSONResponse({"url": str(e.request.url), "status": e.response.status_code, "body": e.response.text}, 502)


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/webhooks/hubspot")
async def hubspot_webhook(req: Request, bg: BackgroundTasks):
    body = await req.body()
    if not verified(req, body):
        raise HTTPException(401)
    for e in json.loads(body):
        if e["subscriptionType"] in ("company.creation", "contact.creation"):
            bg.add_task(handle, e["subscriptionType"], str(e["objectId"]))
    return {"ok": True}