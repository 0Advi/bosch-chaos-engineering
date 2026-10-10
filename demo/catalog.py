"""Fault catalog, user-facing endpoints and the service topology layout.

Every scenario is a bounded, reversible change inside the sandbox:
  flag        flagd feature flag built into the OpenTelemetry demo services
  kill        container crash (docker kill), restored with docker start
  pause       hung / unresponsive service (docker pause), restored with unpause
  partition   network partition (docker network disconnect), restored with connect
  cpu         CPU starvation (CFS quota), restored to unlimited
  db_overload expensive parallel queries inside PostgreSQL, restored by cancelling them
  traffic     sudden traffic spike from concurrent synthetic users
  combined    traffic spike while a service is down
"""
from __future__ import annotations

# Services the agent and operator may target with container faults. The proxy and
# the observability stack are excluded: breaking them would blind the experiment.
CONTAINER_TARGETS = ["ad", "cart", "checkout", "currency", "email", "frontend", "image-provider",
                     "payment", "product-catalog", "quote", "recommendation", "shipping",
                     "valkey-cart", "astronomy-db"]
EXCLUDED_TARGETS = {
    "frontend-proxy": "entry point used to observe the experiment",
    "otel-collector": "telemetry pipeline (would blind the evidence)",
    "prometheus": "metrics store (would blind the evidence)",
    "jaeger": "trace store (would blind the evidence)",
    "opensearch": "log store (would blind the evidence)",
    "grafana": "dashboards only", "opamp-server": "collector management only",
    "flagd": "fault-injection control plane", "flagd-ui": "flag editor only",
    "load-generator": "not used (synthetic workload is built in)",
    "telemetry-docs": "documentation only",
}

SCENARIOS: dict[str, dict] = {
    # ------------------------------------------------------------ flag faults
    "payment-failure": {
        "kind": "flag", "flag": "paymentFailure", "target": "payment", "category": "Errors",
        "title": "Payment service rejects charges", "variants": ["25%", "50%", "75%", "100%"], "default": "50%",
        "hypothesis": "If the payment service itself fails a share of charges, only checkout is affected; "
                      "customers whose valid card hit the outage get an honest, retryable server error (5xx, "
                      "'try again'), not a card-decline (4xx) message; the shop recovers within seconds."},
    "payment-unreachable": {
        "kind": "flag", "flag": "paymentUnreachable", "target": "payment", "category": "Failed dependency",
        "title": "Checkout cannot reach payment", "variants": ["on"], "default": "on",
        "hypothesis": "If checkout loses its payment connection, checkout fails fast with a server error "
                      "while browsing and cart stay healthy."},
    "cart-failure": {
        "kind": "flag", "flag": "cartFailure", "target": "cart", "category": "Errors",
        "title": "Cart service fails requests", "variants": ["25%", "50%", "100%"], "default": "50%",
        "hypothesis": "If cart fails, adding to cart and checkout fail but browsing, product pages and "
                      "recommendations keep working."},
    "product-catalog-failure": {
        "kind": "flag", "flag": "productCatalogFailure", "target": "product-catalog", "category": "Errors",
        "title": "Product catalog fails for one product", "variants": ["on"], "default": "on",
        "hypothesis": "If one product lookup fails, only that product page breaks; the catalog, other "
                      "products and checkout stay healthy."},
    "db-lock-contention": {
        "kind": "flag", "flag": "productCatalogLockContention", "target": "astronomy-db",
        "category": "Overloaded database", "title": "Database lock contention",
        "variants": ["on"], "default": "on",
        "hypothesis": "If the product database suffers lock contention, catalog reads slow down but stay "
                      "within a few hundred milliseconds and nothing fails."},
    "recommendation-cache-failure": {
        "kind": "flag", "flag": "recommendationCacheFailure", "target": "recommendation",
        "category": "Resource exhaustion", "title": "Recommendation cache failure",
        "variants": ["on"], "default": "on",
        "hypothesis": "If the recommendation cache fails, recommendations get slower but product pages "
                      "and checkout are unaffected."},
    "ad-failure": {
        "kind": "flag", "flag": "adFailure", "target": "ad", "category": "Errors",
        "title": "Ad service fails", "variants": ["on"], "default": "on",
        "hypothesis": "If ads fail, the shop degrades gracefully: only the ad slot is empty."},
    "ad-high-cpu": {
        "kind": "flag", "flag": "adHighCpu", "target": "ad", "category": "Slowdown",
        "title": "Ad service CPU saturation", "variants": ["on"], "default": "on",
        "hypothesis": "If the ad service saturates its CPU, ad latency rises but other pages are unaffected."},
    "ad-gc-pauses": {
        "kind": "flag", "flag": "adManualGc", "target": "ad", "category": "Slowdown",
        "title": "Ad service garbage-collection pauses", "variants": ["on"], "default": "on",
        "hypothesis": "If the ad JVM pauses for full GCs, ad latency spikes but nothing else is affected."},
    "image-slow-load": {
        "kind": "flag", "flag": "imageSlowLoad", "target": "image-provider", "category": "Slow network",
        "title": "Slow product images", "variants": ["5sec", "10sec"], "default": "5sec",
        "hypothesis": "If image delivery slows to seconds, only images are slow; APIs and checkout keep "
                      "their normal latency."},
    "intl-shipping-slowdown": {
        "kind": "flag", "flag": "intlShippingSlowdown", "target": "shipping", "category": "Slowdown",
        "title": "International shipping slowdown", "variants": ["5sec", "10sec"], "default": "5sec",
        "hypothesis": "If international shipping quotes are delayed, only international quotes slow down "
                      "and domestic checkout is unaffected."},
    "email-memory-leak": {
        "kind": "flag", "flag": "emailMemoryLeak", "target": "email", "category": "Resource exhaustion",
        "title": "Email service memory leak", "variants": ["100x", "1000x"], "default": "1000x",
        "hypothesis": "If the email service leaks memory, order confirmation may fail but orders are "
                      "still placed successfully."},
    # ------------------------------------------------------- container faults
    "service-crash": {
        "kind": "kill", "target": "currency", "targets": CONTAINER_TARGETS, "category": "Crash",
        "title": "Service crash", "variants": ["kill"], "default": "kill",
        "hypothesis": "If {target} crashes, only features that need it fail, and it recovers quickly "
                      "once restarted."},
    "service-freeze": {
        "kind": "pause", "target": "recommendation", "targets": CONTAINER_TARGETS, "category": "Hung dependency",
        "title": "Service hangs (frozen process)", "variants": ["pause"], "default": "pause",
        "hypothesis": "If {target} stops responding, callers time out quickly instead of hanging and "
                      "unrelated pages stay fast."},
    "network-partition": {
        "kind": "partition", "target": "cart", "targets": CONTAINER_TARGETS, "category": "Network",
        "title": "Network partition", "variants": ["disconnect"], "default": "disconnect",
        "hypothesis": "If {target} is cut off from the network, dependent features fail fast with clear "
                      "errors and recover when connectivity returns."},
    "cpu-starvation": {
        "kind": "cpu", "target": "product-catalog", "targets": CONTAINER_TARGETS, "category": "Resource exhaustion",
        "title": "CPU starvation", "variants": ["10%", "5%"], "default": "10%",
        "hypothesis": "If {target} only gets a fraction of a CPU, latency rises but requests still succeed."},
    "db-overload": {
        "kind": "db_overload", "target": "astronomy-db", "category": "Overloaded database",
        "title": "Overloaded database (heavy queries)", "variants": ["4 queries", "8 queries"], "default": "4 queries",
        "hypothesis": "If the product database is saturated by heavy queries, catalog latency rises but "
                      "requests keep succeeding."},
    "traffic-spike": {
        "kind": "traffic", "target": "frontend", "category": "Traffic spike",
        "title": "Sudden traffic spike", "variants": ["20 users", "40 users"], "default": "20 users",
        "hypothesis": "If traffic suddenly multiplies, latency rises moderately and no requests fail."},
    "spike-plus-crash": {
        "kind": "combined", "target": "recommendation", "targets": CONTAINER_TARGETS, "category": "Combined",
        "title": "Traffic spike while a service is down", "variants": ["20 users", "40 users"],
        "default": "20 users",
        "hypothesis": "If traffic spikes while {target} is down, the failure stays contained to features "
                      "that need {target} and the rest of the shop absorbs the load."},
}

# User-facing endpoints exercised by every synthetic journey, and the services they use.
ENDPOINTS: dict[str, dict] = {
    "home":            {"label": "Home page",           "services": ["frontend-proxy", "frontend"]},
    "catalog":         {"label": "Product list (EUR)",  "services": ["frontend-proxy", "frontend", "product-catalog", "astronomy-db", "currency"]},
    "product":         {"label": "Product page",        "services": ["frontend-proxy", "frontend", "product-catalog", "astronomy-db", "currency"]},
    "recommendations": {"label": "Recommendations",     "services": ["frontend-proxy", "frontend", "recommendation", "product-catalog", "astronomy-db"]},
    "ads":             {"label": "Ads",                 "services": ["frontend-proxy", "frontend", "ad"]},
    "image":           {"label": "Product image",       "services": ["frontend-proxy", "image-provider"]},
    "shipping":        {"label": "Intl shipping quote", "services": ["frontend-proxy", "frontend", "shipping", "quote", "currency"]},
    "cart_add":        {"label": "Add to cart",         "services": ["frontend-proxy", "frontend", "cart", "valkey-cart"]},
    "cart_get":        {"label": "View cart",           "services": ["frontend-proxy", "frontend", "cart", "valkey-cart", "product-catalog", "astronomy-db", "currency"]},
    "checkout":        {"label": "Checkout",            "services": ["frontend-proxy", "frontend", "checkout", "cart", "valkey-cart", "product-catalog", "astronomy-db", "currency", "shipping", "quote", "payment", "email"]},
}

# Fixed layout for the 25-service dependency graph (viewBox 0 0 1000 560).
LAYOUT = {
    "users": (60, 280), "load-generator": (60, 420),
    "frontend-proxy": (200, 280),
    "frontend": (350, 220), "image-provider": (350, 420),
    "ad": (530, 40), "recommendation": (530, 110), "product-catalog": (530, 190), "currency": (530, 270),
    "cart": (530, 350), "shipping": (530, 430), "checkout": (530, 510),
    "astronomy-db": (720, 150), "valkey-cart": (720, 350), "quote": (720, 430),
    "payment": (720, 500), "email": (720, 545),
    "flagd": (900, 40), "flagd-ui": (900, 95), "otel-collector": (900, 170), "prometheus": (900, 240),
    "jaeger": (900, 300), "opensearch": (900, 360), "grafana": (900, 420), "opamp-server": (900, 480),
    "telemetry-docs": (900, 535),
}
PLATFORM = ["flagd", "flagd-ui", "otel-collector", "prometheus", "jaeger", "opensearch", "grafana",
            "opamp-server", "telemetry-docs"]

# Interpreted services whose source can be hot-patched into the running container
# (bind-mounted over the image file) for the auto-fix demonstration.
HOT_PATCH = {
    "payment/charge.js": "/usr/src/app/charge.js",
    "payment/index.js": "/usr/src/app/index.js",
    "recommendation/recommendation_server.py": "/app/recommendation_server.py",
    "email/email_server.rb": "/email_server/email_server.rb",
}


def scenario_view(sid: str, target: str | None = None) -> dict:
    spec = dict(SCENARIOS[sid])
    spec["id"] = sid
    spec["target"] = target or spec["target"]
    spec["hypothesis"] = spec["hypothesis"].replace("{target}", spec["target"])
    return spec
