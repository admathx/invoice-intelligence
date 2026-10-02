from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import (
    accounts,
    activity,
    auth,
    costs,
    digest,
    distributors,
    exports,
    inbound,
    insights,
    invoice_review,
    invoices,
    negotiation,
    ops as ops_api,
    review,
    setup,
    skus,
    spending,
    tenants,
    users,
)
from app import ops
from app.auth import CSRF_HEADER, csrf_ok
from app.config import settings

app = FastAPI(title="Invoice Intelligence")


@app.exception_handler(Exception)
async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """Anything no endpoint handled: tell whoever runs the service (app.ops),
    and the person a plain sentence rather than a stack trace."""
    route = request.scope.get("route")
    where = f"{request.method} {getattr(route, 'path', request.url.path)}"
    ops.alert(f"api:{where}:{type(exc).__name__}", f"Error in {where}: {type(exc).__name__}", exc=exc)
    return JSONResponse(status_code=500, content={"detail": "Something went wrong on our side. Try again in a moment."})


@app.middleware("http")
async def require_csrf_header(request: Request, call_next):
    # See app.auth's module docstring for why a custom header is the check.
    if not csrf_ok(request):
        return JSONResponse(status_code=403, content={"detail": f"missing {CSRF_HEADER} header"})
    return await call_next(request)


# Added last so it is the outermost layer: a CSRF refusal above still carries
# CORS headers, so the browser shows the frontend the real 403 rather than an
# opaque CORS failure. The frontend normally reaches the API through its own
# /api proxy (same origin, no CORS at all); this is for pointing a dev
# frontend straight at the API. Explicit origins, never a pattern, because
# credentials are allowed: any origin admitted here can act as a signed-in
# user.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.frontend_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Page images used to be a public StaticFiles mount at /renders. Once there
# are logins that would leak every location's invoices to anyone with a URL,
# so they're served by an authorized endpoint (invoices.get_page_image).

app.include_router(auth.router)
app.include_router(digest.router)
app.include_router(invoices.router)
app.include_router(invoice_review.router)
app.include_router(activity.router)
app.include_router(distributors.router)
app.include_router(exports.router)
app.include_router(inbound.router)
app.include_router(skus.router)
app.include_router(spending.router)
app.include_router(costs.router)
app.include_router(review.router)
app.include_router(setup.router)
app.include_router(insights.router)
app.include_router(negotiation.router)
app.include_router(ops_api.router)
app.include_router(accounts.router)
app.include_router(tenants.router)
app.include_router(users.router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
