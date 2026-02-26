import os
import json
import uuid
import logging
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone

from flask import Flask, request, jsonify
from flask_cors import CORS
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException
import anthropic
from google.cloud import firestore
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    RunReportRequest, DateRange, Metric, Dimension,
)
from googleapiclient.discovery import build
import google.auth
from datetime import timedelta


logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=False)


@app.after_request
def add_cors_headers(response):
    """Ensure CORS headers are present on EVERY response, including errors."""
    response.headers.setdefault("Access-Control-Allow-Origin", "*")
    response.headers.setdefault("Access-Control-Allow-Headers", "Content-Type, Authorization")
    response.headers.setdefault("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
    return response


# -----------------------------
# Config (ENV)
# -----------------------------
INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY")  # אופציונלי (לנעילת ה-endpoint)

GOOGLE_ADS_DEVELOPER_TOKEN = os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN")
GOOGLE_ADS_CLIENT_ID = os.getenv("GOOGLE_ADS_CLIENT_ID")
GOOGLE_ADS_CLIENT_SECRET = os.getenv("GOOGLE_ADS_CLIENT_SECRET")
GOOGLE_ADS_REFRESH_TOKEN = os.getenv("GOOGLE_ADS_REFRESH_TOKEN")

# זה ה-MCC (login customer id). לדוגמה: 44988665320 (בלי מקפים)
GOOGLE_ADS_MCC_ID = os.getenv("GOOGLE_ADS_MCC_ID")

# הגבלות כדי לא להיתקע על TIMEOUT
MAX_ACCOUNTS = int(os.getenv("MAX_ACCOUNTS", "10"))                 # כמה חשבונות למשוך מתחת ל-MCC
MAX_CAMPAIGNS_PER_ACCOUNT = int(os.getenv("MAX_CAMPAIGNS_PER_ACCOUNT", "30"))  # כמה קמפיינים לכל חשבון

# Claude AI (Anthropic)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")
CLAUDE_MAX_TOKENS = int(os.getenv("CLAUDE_MAX_TOKENS", "4096"))

CLAUDE_SYSTEM_PROMPT = """You are a senior marketing strategist and growth expert. You specialize in:
- Google Ads campaign strategy, optimization, and performance analysis
- SEO and organic search performance (Google Search Console data)
- Google Tag Manager configuration analysis (tracking audit, conversion setup, remarketing tags)
- Lead generation and CRM strategies
- Conversion rate optimization (CRO)
- Cross-channel marketing analysis (comparing Paid vs. Organic performance)

When provided with data context (Google Ads, Google Analytics, Google Search Console, Google Tag Manager), analyze it deeply to provide actionable insights. If you see organic search data, use it to inform your paid strategy. If you see GTM data, identify tracking gaps and suggest improvements (e.g., missing conversion tags, remarketing opportunities).
Always answer in the language the user speaks to you (usually Hebrew).
Keep answers concise, professional, and directly useful."""

# Firestore
db = firestore.Client()
CONVERSATIONS_COLLECTION = "conversations"

# Google Analytics
# Optional comma-separated list of GA4 property IDs for quick access
GA_PROPERTY_IDS = [p.strip() for p in os.getenv("GA_PROPERTY_IDS", "").split(",") if p.strip()]


def _require_auth() -> bool:
    """Simple header auth (optional)."""
    if not INTERNAL_API_KEY:
        return True
    return request.headers.get("auth-key") == INTERNAL_API_KEY


def _build_googleads_client() -> GoogleAdsClient:
    missing = [
        k for k, v in {
            "GOOGLE_ADS_DEVELOPER_TOKEN": GOOGLE_ADS_DEVELOPER_TOKEN,
            "GOOGLE_ADS_CLIENT_ID": GOOGLE_ADS_CLIENT_ID,
            "GOOGLE_ADS_CLIENT_SECRET": GOOGLE_ADS_CLIENT_SECRET,
            "GOOGLE_ADS_REFRESH_TOKEN": GOOGLE_ADS_REFRESH_TOKEN,
        }.items() if not v
    ]
    if missing:
        raise RuntimeError(f"Missing ENV vars: {', '.join(missing)}")

    config = {
        "developer_token": GOOGLE_ADS_DEVELOPER_TOKEN,
        "client_id": GOOGLE_ADS_CLIENT_ID,
        "client_secret": GOOGLE_ADS_CLIENT_SECRET,
        "refresh_token": GOOGLE_ADS_REFRESH_TOKEN,
        "use_proto_plus": True,
    }
    # login_customer_id = ה-MCC
    if GOOGLE_ADS_MCC_ID:
        # GoogleAdsClient מצפה ל-int / str של מספר בלבד
        config["login_customer_id"] = str(GOOGLE_ADS_MCC_ID).replace("-", "").strip()

    return GoogleAdsClient.load_from_dict(config)


def _list_direct_child_accounts(client: GoogleAdsClient) -> List[Dict[str, Any]]:
    """
    מחזיר רשימת חשבונות (Customer) שנמצאים ישירות תחת ה-MCC (Level 1),
    כדי לא להיתקע על כל עץ ההיררכיה.
    """
    if not GOOGLE_ADS_MCC_ID:
        raise RuntimeError("Missing GOOGLE_ADS_MCC_ID (your MCC id)")

    mcc_id = str(GOOGLE_ADS_MCC_ID).replace("-", "").strip()

    ga_service = client.get_service("GoogleAdsService")

    query = """
        SELECT
          customer_client.client_customer,
          customer_client.id,
          customer_client.descriptive_name,
          customer_client.level,
          customer_client.manager,
          customer_client.status
        FROM customer_client
        WHERE customer_client.level = 1
    """

    accounts: List[Dict[str, Any]] = []
    stream = ga_service.search_stream(customer_id=mcc_id, query=query)

    for batch in stream:
        for row in batch.results:
            cc = row.customer_client
            # status יכול להיות ENABLED/DISABLED/CANCELED וכו'
            accounts.append({
                "customer_id": str(cc.id),
                "resource_name": cc.client_customer,
                "name": cc.descriptive_name,
                "status": cc.status.name if hasattr(cc.status, "name") else str(cc.status),
                "is_manager": bool(cc.manager),
                "level": int(cc.level),
            })

    # תעדוף חשבונות פעילים (אם קיים)
    accounts_sorted = sorted(accounts, key=lambda a: (a["status"] != "ENABLED", a["is_manager"], a["customer_id"]))
    return accounts_sorted[:MAX_ACCOUNTS]


def _fetch_campaigns_for_account(client: GoogleAdsClient, customer_id: str) -> List[Dict[str, Any]]:
    ga_service = client.get_service("GoogleAdsService")

    # מדדים בסיסיים ב-30 ימים אחרונים — רק קמפיינים עם הוצאה בפועל
    query = f"""
        SELECT
          customer.id,
          customer.descriptive_name,
          campaign.id,
          campaign.name,
          campaign.status,
          campaign.advertising_channel_type,
          metrics.impressions,
          metrics.clicks,
          metrics.cost_micros,
          metrics.conversions
        FROM campaign
        WHERE segments.date DURING LAST_30_DAYS
          AND metrics.cost_micros > 0
        ORDER BY metrics.cost_micros DESC
        LIMIT {MAX_CAMPAIGNS_PER_ACCOUNT}
    """

    rows: List[Dict[str, Any]] = []
    try:
        # search() מחזיר iterator עם pagination; יותר פשוט מ-stream כאן
        for r in ga_service.search(customer_id=customer_id, query=query):
            rows.append({
                "customer_id": str(r.customer.id),
                "customer_name": r.customer.descriptive_name,
                "campaign_id": str(r.campaign.id),
                "campaign_name": r.campaign.name,
                "campaign_status": r.campaign.status.name,
                "channel_type": r.campaign.advertising_channel_type.name,
                "impressions": int(r.metrics.impressions),
                "clicks": int(r.metrics.clicks),
                "cost": float(r.metrics.cost_micros) / 1_000_000.0,
                "conversions": float(r.metrics.conversions),
            })
    except GoogleAdsException as ex:
        # מחזירים "שגיאה רכה" ברמת החשבון כדי שלא יפיל את כל הקריאה
        logging.exception("GoogleAdsException on customer_id=%s", customer_id)
        return [{
            "customer_id": customer_id,
            "error": {
                "message": ex.error.message if ex.error else str(ex),
                "request_id": getattr(ex, "request_id", None),
            }
        }]
    except Exception as ex:
        logging.exception("Unexpected error on customer_id=%s", customer_id)
        return [{
            "customer_id": customer_id,
            "error": {"message": str(ex)}
        }]

    return rows


@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.get("/campaigns")
def campaigns():
    # if not _require_auth():
    #     return jsonify({"error": "Unauthorized"}), 401

    try:
        gads_client = _build_googleads_client()
        accounts = _list_direct_child_accounts(gads_client)

        all_results: List[Dict[str, Any]] = []
        for acc in accounts:
            cid = acc["customer_id"]
            # אם זה חשבון manager, עדיין אפשר שיהיו לו קמפיינים (לרוב לא),
            # אבל נשאיר; אם תרצה לדלג על managers תגיד.
            campaigns_rows = _fetch_campaigns_for_account(gads_client, cid)
            # מוסיפים מעט metadata של החשבון
            for row in campaigns_rows:
                row.setdefault("account_meta", {
                    "customer_id": cid,
                    "name": acc.get("name"),
                    "status": acc.get("status"),
                    "is_manager": acc.get("is_manager"),
                    "level": acc.get("level"),
                })
            all_results.extend(campaigns_rows)

        return jsonify({
            "ok": True,
            "login_customer_id": str(GOOGLE_ADS_MCC_ID).replace("-", "").strip() if GOOGLE_ADS_MCC_ID else None,
            "accounts_count": len(accounts),
            "row_count": len(all_results),
            "rows": all_results,
        })

    except Exception as ex:
        logging.exception("Failed /campaigns")
        return jsonify({"ok": False, "error": str(ex)}), 500


# -----------------------------------------------
# Google Analytics Endpoints
# -----------------------------------------------

def _get_ga_scoped_credentials():
    """Get default credentials with explicit analytics scopes."""
    import google.auth
    scopes = [
        "https://www.googleapis.com/auth/analytics.readonly",
    ]
    credentials, project = google.auth.default(scopes=scopes)
    return credentials


def _get_ga_data_client() -> BetaAnalyticsDataClient:
    """Returns a GA Data client with explicit analytics scopes."""
    credentials = _get_ga_scoped_credentials()
    return BetaAnalyticsDataClient(credentials=credentials)


def _run_ga_report(property_id: str, days: int = 30) -> Dict[str, Any]:
    """
    Run a summary report for a GA4 property.
    Returns sessions, active users, screen page views, conversions,
    bounce rate — grouped by date.
    """
    data_client = _get_ga_data_client()

    req = RunReportRequest(
        property=f"properties/{property_id}",
        date_ranges=[DateRange(start_date=f"{days}daysAgo", end_date="today")],
        dimensions=[Dimension(name="date")],
        metrics=[
            Metric(name="sessions"),
            Metric(name="activeUsers"),
            Metric(name="screenPageViews"),
            Metric(name="conversions"),
            Metric(name="bounceRate"),
        ],
    )

    response = data_client.run_report(req)

    rows = []
    metric_headers = [h.name for h in response.metric_headers]
    for row in response.rows:
        row_data = {"date": row.dimension_values[0].value}
        for i, metric_val in enumerate(row.metric_values):
            row_data[metric_headers[i]] = metric_val.value
        rows.append(row_data)

    # Sort by date ascending
    rows.sort(key=lambda r: r["date"])

    # Calculate totals from the response totals
    totals = {}
    if response.totals:
        for i, metric_val in enumerate(response.totals[0].metric_values):
            totals[metric_headers[i]] = metric_val.value

    return {
        "property_id": property_id,
        "date_range": f"last {days} days",
        "row_count": len(rows),
        "totals": totals,
        "rows": rows,
    }


def _get_ga_context_for_ai(property_id: str) -> str:
    """Fetch GA report data and format it as text context for Claude."""
    try:
        report = _run_ga_report(property_id, days=30)
        totals = report.get("totals", {})

        context_lines = [
            f"=== Google Analytics Data (Property {property_id}, Last 30 Days) ===",
            f"Total Sessions: {totals.get('sessions', 'N/A')}",
            f"Total Active Users: {totals.get('activeUsers', 'N/A')}",
            f"Total Page Views: {totals.get('screenPageViews', 'N/A')}",
            f"Total Conversions: {totals.get('conversions', 'N/A')}",
            f"Average Bounce Rate: {totals.get('bounceRate', 'N/A')}",
            "",
            "Daily breakdown (last 10 days):",
        ]

        # Show last 10 days for brevity
        recent_rows = report["rows"][-10:]
        for row in recent_rows:
            date_str = row['date']
            formatted_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
            context_lines.append(
                f"  {formatted_date}: "
                f"Sessions={row.get('sessions', '?')}, "
                f"Users={row.get('activeUsers', '?')}, "
                f"PageViews={row.get('screenPageViews', '?')}, "
                f"Conversions={row.get('conversions', '?')}, "
                f"BounceRate={row.get('bounceRate', '?')}"
            )

        context_lines.append("=== End of GA Data ===")
        return "\n".join(context_lines)

    except Exception as ex:
        logging.exception("Failed to fetch GA context for property %s", property_id)
        return f"[Could not fetch GA data for property {property_id}: {str(ex)}]"


@app.route("/analytics/accounts", methods=["GET", "OPTIONS"])
def ga_accounts():
    """Returns the hardcoded GA4 property IDs from GA_PROPERTY_IDS env var."""
    if request.method == "OPTIONS":
        return "", 204
    return jsonify({
        "ok": True,
        "property_ids": GA_PROPERTY_IDS,
    })


@app.route("/analytics/report/<property_id>", methods=["GET", "OPTIONS"])
def ga_report(property_id):
    if request.method == "OPTIONS":
        return "", 204
    try:
        days = int(request.args.get("days", "30"))
        days = max(1, min(days, 365))  # clamp to 1-365
        report = _run_ga_report(property_id, days=days)
        return jsonify({"ok": True, **report})
    except Exception as ex:
        logging.exception("Failed /analytics/report/%s", property_id)
        return jsonify({"ok": False, "error": str(ex)}), 500


# -----------------------------------------------
# Google Search Console Context for AI
# -----------------------------------------------

def _get_gsc_context_for_ai(site_url: str) -> str:
    """
    Fetch Google Search Console performance data and format it for Claude.
    Extracts Top 5 Queries, Pages, Query+Page Matrix, and Country break-downs.
    """
    try:
        credentials, project = google.auth.default(scopes=['https://www.googleapis.com/auth/webmasters.readonly'])
        # Build the GSC service
        service = build('searchconsole', 'v1', credentials=credentials)

        # Calculate dates (last 30 days)
        end_date = datetime.now(timezone.utc).date() - timedelta(days=2) # GSC data typically has a 2-day lag
        start_date = end_date - timedelta(days=30)
        
        start_date_str = start_date.strftime('%Y-%m-%d')
        end_date_str = end_date.strftime('%Y-%m-%d')

        context_lines = [
            f"=== Google Search Console (SEO) Data for {site_url} ===",
            f"Date Range: {start_date_str} to {end_date_str}",
            ""
        ]

        def _query_gsc(dimensions: List[str], row_limit: int = 5) -> List[Dict]:
            request = {
                'startDate': start_date_str,
                'endDate': end_date_str,
                'dimensions': dimensions,
                'rowLimit': row_limit
            }
            response = service.searchanalytics().query(siteUrl=site_url, body=request).execute()
            return response.get('rows', [])

        # 1. Top 15 Queries
        top_queries = _query_gsc(['query'], 15)
        if top_queries:
            context_lines.append("--- Top 15 Organic Search Queries ---")
            for r in top_queries:
                context_lines.append(f"Query: '{r['keys'][0]}' | Clicks: {r['clicks']} | Imp: {r['impressions']} | CTR: {r['ctr'] * 100:.1f}% | Pos: {r['position']:.1f}")
            context_lines.append("")

        # 2. Top 5 Pages
        top_pages = _query_gsc(['page'], 5)
        if top_pages:
            context_lines.append("--- Top 5 Organic Landing Pages ---")
            for r in top_pages:
                context_lines.append(f"Page: {r['keys'][0]} | Clicks: {r['clicks']} | Imp: {r['impressions']} | CTR: {r['ctr'] * 100:.1f}% | Pos: {r['position']:.1f}")
            context_lines.append("")

        # 3. Top 5 Query + Page Matrix
        top_matrix = _query_gsc(['query', 'page'], 5)
        if top_matrix:
            context_lines.append("--- Top 5 Query + Page Combinations ---")
            for r in top_matrix:
                context_lines.append(f"Query: '{r['keys'][0]}' -> Page: {r['keys'][1]} | Clicks: {r['clicks']} | Pos: {r['position']:.1f}")
            context_lines.append("")

        # 4. Device x Country breakdown (Top 5 per device)
        device_country = _query_gsc(['device', 'country'], 10)
        if device_country:
            context_lines.append("--- Organic Traffic by Device & Country (Top Segments) ---")
            for r in device_country:
                context_lines.append(f"Device: {r['keys'][0].upper()} | Country: {r['keys'][1].upper()} | Clicks: {r['clicks']} | Imp: {r['impressions']}")
            context_lines.append("")

        context_lines.append("=== End of GSC Data ===")
        return "\n".join(context_lines)

    except Exception as ex:
        logging.exception("Failed to fetch GSC context for property %s", site_url)
        return f"[Could not fetch GSC data for property {site_url}: {str(ex)}]"


# -----------------------------------------------
# Google Tag Manager Context for AI
# -----------------------------------------------

def _get_gtm_context_for_ai(account_id: str, public_id: str) -> str:
    """
    Fetch Google Tag Manager tags, triggers, and variables for a container
    and format them as text context for Claude.
    account_id: GTM account ID (numeric)
    public_id: Public container ID like 'GTM-NCJ9SK9Z'
    """
    try:
        credentials, project = google.auth.default(scopes=['https://www.googleapis.com/auth/tagmanager.readonly'])
        service = build('tagmanager', 'v2', credentials=credentials)

        # Step 1: List containers under this account to find the numeric container ID
        account_path = f"accounts/{account_id}"
        containers_response = service.accounts().containers().list(parent=account_path).execute()
        containers = containers_response.get('container', [])

        # Find the container matching the public ID (e.g. GTM-NCJ9SK9Z)
        container_path = None
        for c in containers:
            if c.get('publicId', '').upper() == public_id.upper():
                container_path = c.get('path')  # e.g. accounts/123/containers/456
                break

        if not container_path:
            return f"[GTM container {public_id} not found under account {account_id}. Available: {[c.get('publicId') for c in containers]}]"

        # Step 2: Get the live (published) version of the container
        live = service.accounts().containers().versions().live(
            parent=container_path
        ).execute()

        context_lines = [
            f"=== Google Tag Manager Data ({public_id}) ===",
            f"Container Version: {live.get('containerVersionId', 'N/A')}",
            ""
        ]

        # --- 1. Tags ---
        tags = live.get('tag', [])
        triggers = live.get('trigger', [])
        variables = live.get('variable', [])

        # Build trigger ID -> name lookup
        trigger_map = {}
        for t in triggers:
            trigger_map[t.get('triggerId', '')] = t.get('name', 'Unknown')

        if tags:
            context_lines.append(f"--- All Tags ({len(tags)}) ---")
            for tag in tags:
                tag_name = tag.get('name', 'Unnamed')
                tag_type = tag.get('type', 'Unknown')
                paused = ' [PAUSED]' if tag.get('paused') else ''

                # Get firing triggers
                firing_ids = tag.get('firingTriggerId', [])
                firing_names = [trigger_map.get(tid, f'ID:{tid}') for tid in firing_ids]
                fires_on = ', '.join(firing_names) if firing_names else 'No trigger'

                context_lines.append(f"Tag: {tag_name} | Type: {tag_type}{paused} | Fires on: {fires_on}")
            context_lines.append("")

        # --- 2. Triggers ---
        if triggers:
            context_lines.append(f"--- All Triggers ({len(triggers)}) ---")
            for t in triggers:
                t_name = t.get('name', 'Unnamed')
                t_type = t.get('type', 'Unknown')
                # Get filter conditions if present
                filters = t.get('filter', [])
                conditions = []
                for f in filters:
                    param = f.get('parameter', [])
                    if len(param) >= 2:
                        conditions.append(f"{param[0].get('value', '?')} {f.get('type', '?')} {param[1].get('value', '?')}")
                cond_str = f" | Conditions: {'; '.join(conditions)}" if conditions else ''
                context_lines.append(f"Trigger: {t_name} | Type: {t_type}{cond_str}")
            context_lines.append("")

        # --- 3. Custom Variables ---
        if variables:
            context_lines.append(f"--- Custom Variables ({len(variables)}) ---")
            for v in variables:
                v_name = v.get('name', 'Unnamed')
                v_type = v.get('type', 'Unknown')
                context_lines.append(f"Variable: {v_name} | Type: {v_type}")
            context_lines.append("")

        context_lines.append("=== End of GTM Data ===")
        return "\n".join(context_lines)

    except Exception as ex:
        logging.exception("Failed to fetch GTM context for %s / %s", account_id, public_id)
        return f"[Could not fetch GTM data for {public_id}: {str(ex)}]"


# -----------------------------------------------
# Google Ads Context for AI
# -----------------------------------------------

def _get_google_ads_context_for_ai(customer_ids: Optional[List[str]] = None) -> str:
    """
    Fetch Google Ads campaign data and format it as text context for Claude.
    If customer_ids is provided, fetch only those accounts.
    Otherwise, fetch all child accounts under the MCC.
    """
    try:
        gads_client = _build_googleads_client()

        # Determine which accounts to fetch
        if customer_ids:
            accounts = [{"customer_id": cid.replace("-", "").strip(), "name": None, "status": None} for cid in customer_ids]
        else:
            accounts = _list_direct_child_accounts(gads_client)

        if not accounts:
            return "[No Google Ads accounts found under this MCC.]"

        context_lines = [
            "=== Google Ads Campaign Data (Last 30 Days) ===",
            f"Accounts loaded: {len(accounts)}",
            "",
        ]

        grand_totals = {"impressions": 0, "clicks": 0, "cost": 0.0, "conversions": 0.0}

        for acc in accounts:
            cid = acc["customer_id"]
            acc_name = acc.get("name") or cid
            context_lines.append(f"--- Account: {acc_name} (ID: {cid}) ---")

            campaigns = _fetch_campaigns_for_account(gads_client, cid)

            # Check for error responses
            if campaigns and "error" in campaigns[0]:
                err_msg = campaigns[0]["error"].get("message", "Unknown error")
                context_lines.append(f"  [Error fetching campaigns: {err_msg}]")
                context_lines.append("")
                continue

            if not campaigns:
                context_lines.append("  No campaigns found.")
                context_lines.append("")
                continue

            acc_totals = {"impressions": 0, "clicks": 0, "cost": 0.0, "conversions": 0.0}

            for c in campaigns:
                impressions = c.get("impressions", 0)
                clicks = c.get("clicks", 0)
                cost = c.get("cost", 0.0)
                conversions = c.get("conversions", 0.0)
                ctr = (clicks / impressions * 100) if impressions > 0 else 0.0
                cpc = (cost / clicks) if clicks > 0 else 0.0

                context_lines.append(
                    f"  Campaign: {c.get('campaign_name', '?')} | "
                    f"Status: {c.get('campaign_status', '?')} | "
                    f"Type: {c.get('channel_type', '?')} | "
                    f"Impr: {impressions:,} | "
                    f"Clicks: {clicks:,} | "
                    f"CTR: {ctr:.2f}% | "
                    f"Cost: ₪{cost:,.2f} | "
                    f"CPC: ₪{cpc:.2f} | "
                    f"Conv: {conversions:.1f}"
                )

                acc_totals["impressions"] += impressions
                acc_totals["clicks"] += clicks
                acc_totals["cost"] += cost
                acc_totals["conversions"] += conversions

            # Account totals
            acc_ctr = (acc_totals["clicks"] / acc_totals["impressions"] * 100) if acc_totals["impressions"] > 0 else 0.0
            acc_cpc = (acc_totals["cost"] / acc_totals["clicks"]) if acc_totals["clicks"] > 0 else 0.0
            context_lines.append(
                f"  ACCOUNT TOTAL: Impr: {acc_totals['impressions']:,} | "
                f"Clicks: {acc_totals['clicks']:,} | "
                f"CTR: {acc_ctr:.2f}% | "
                f"Cost: ₪{acc_totals['cost']:,.2f} | "
                f"CPC: ₪{acc_cpc:.2f} | "
                f"Conv: {acc_totals['conversions']:.1f}"
            )
            context_lines.append("")

            # Add to grand totals
            for k in grand_totals:
                grand_totals[k] += acc_totals[k]

        # Grand totals across all accounts
        grand_ctr = (grand_totals["clicks"] / grand_totals["impressions"] * 100) if grand_totals["impressions"] > 0 else 0.0
        grand_cpc = (grand_totals["cost"] / grand_totals["clicks"]) if grand_totals["clicks"] > 0 else 0.0
        context_lines.append(
            f"GRAND TOTAL (All Accounts): Impr: {grand_totals['impressions']:,} | "
            f"Clicks: {grand_totals['clicks']:,} | "
            f"CTR: {grand_ctr:.2f}% | "
            f"Cost: ₪{grand_totals['cost']:,.2f} | "
            f"CPC: ₪{grand_cpc:.2f} | "
            f"Conv: {grand_totals['conversions']:.1f}"
        )
        context_lines.append("=== End of Google Ads Data ===")
        return "\n".join(context_lines)

    except Exception as ex:
        logging.exception("Failed to fetch Google Ads context for AI")
        return f"[Could not fetch Google Ads data: {str(ex)}]"


# -----------------------------------------------
# Claude AI Chat Endpoints
# -----------------------------------------------

def _get_anthropic_client() -> anthropic.Anthropic:
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("Missing ANTHROPIC_API_KEY environment variable")
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def _get_conversation_ref(conversation_id: str):
    return db.collection(CONVERSATIONS_COLLECTION).document(conversation_id)


def _create_conversation(title: str = "New Conversation", conv_id: Optional[str] = None) -> Dict[str, Any]:
    if not conv_id:
        conv_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    doc = {
        "id": conv_id,
        "title": title,
        "created_at": now,
        "updated_at": now,
        "messages": [],
    }
    _get_conversation_ref(conv_id).set(doc)
    return doc


def _auto_title(message: str) -> str:
    """Create a short title from the first user message."""
    title = message.strip().replace("\n", " ")
    return title[:80] + ("..." if len(title) > 80 else "")


@app.route("/ai-chat", methods=["POST", "OPTIONS"])
def ai_chat():
    if request.method == "OPTIONS":
        return "", 204

    try:
        body = request.get_json(force=True)
        user_message = body.get("message", "").strip()
        conversation_id = body.get("conversation_id")
        ga_property_id = body.get("ga_property_id")  # optional GA4 property
        include_google_ads = body.get("include_google_ads", False)  # pull Google Ads data
        google_ads_customer_ids = body.get("google_ads_customer_ids")  # optional list of account IDs

        if not user_message:
            return jsonify({"ok": False, "error": "'message' is required"}), 400

        # --- Build data context blocks ---
        context_parts = []

        # Google Ads context
        if include_google_ads or google_ads_customer_ids:
            ads_context = _get_google_ads_context_for_ai(
                customer_ids=google_ads_customer_ids if google_ads_customer_ids else None
            )
            if ads_context:
                context_parts.append(ads_context)

        # Google Analytics context
        if ga_property_id:
            ga_context = _get_ga_context_for_ai(str(ga_property_id))
            if ga_context:
                context_parts.append(ga_context)

        # Google Search Console context
        gsc_property_url = body.get("gsc_property_url")
        if gsc_property_url:
            gsc_context = _get_gsc_context_for_ai(gsc_property_url)
            if gsc_context:
                context_parts.append(gsc_context)

        # Google Tag Manager context
        gtm_account_id = body.get("gtm_account_id")
        gtm_public_id = body.get("gtm_public_id")
        if gtm_account_id and gtm_public_id:
            gtm_context = _get_gtm_context_for_ai(gtm_account_id, gtm_public_id)
            if gtm_context:
                context_parts.append(gtm_context)

        # Get or create conversation
        if conversation_id:
            conv_ref = _get_conversation_ref(conversation_id)
            conv_doc = conv_ref.get()
            if not conv_doc.exists:
                conv_data = _create_conversation(title=_auto_title(user_message), conv_id=conversation_id)
            else:
                conv_data = conv_doc.to_dict()
        else:
            conv_data = _create_conversation(title=_auto_title(user_message))
            conversation_id = conv_data["id"]
            conv_ref = _get_conversation_ref(conversation_id)

        # Build messages array for Claude (from history)
        messages_for_claude = []
        for msg in conv_data.get("messages", []):
            messages_for_claude.append({
                "role": msg["role"],
                "content": msg["content"],
            })

        # Add the new user message, enriched with data context if available
        enriched_message = user_message
        if context_parts:
            context_block = "\n\n".join(context_parts)
            enriched_message = f"{context_block}\n\nUser question: {user_message}"
        messages_for_claude.append({"role": "user", "content": enriched_message})

        # Call Claude
        client = _get_anthropic_client()
        total_chars = sum(len(m["content"]) for m in messages_for_claude)
        logging.info("Calling Claude model=%s, messages=%d, total_chars=%d", CLAUDE_MODEL, len(messages_for_claude), total_chars)
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=CLAUDE_MAX_TOKENS,
            system=CLAUDE_SYSTEM_PROMPT,
            messages=messages_for_claude,
            timeout=120.0,  # 120s timeout for the API call
        )

        assistant_message = response.content[0].text

        # Update Firestore with both messages
        now = datetime.now(timezone.utc).isoformat()
        conv_ref.update({
            "updated_at": now,
            "messages": firestore.ArrayUnion([
                {"role": "user", "content": user_message, "timestamp": now},
                {"role": "assistant", "content": assistant_message, "timestamp": now},
            ]),
        })

        return jsonify({
            "ok": True,
            "conversation_id": conversation_id,
            "response": assistant_message,
            "model": CLAUDE_MODEL,
            "usage": {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            },
        })

    except Exception as ex:
        logging.exception("Failed /ai-chat: %s", str(ex))
        resp = jsonify({"ok": False, "error": str(ex)})
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp, 500


@app.route("/ai-chat/conversations", methods=["GET", "OPTIONS"])
def list_conversations():
    if request.method == "OPTIONS":
        return "", 204

    try:
        query = (
            db.collection(CONVERSATIONS_COLLECTION)
            .order_by("updated_at", direction=firestore.Query.DESCENDING)
            .limit(50)
        )
        docs = query.stream()

        conversations = []
        for doc in docs:
            d = doc.to_dict()
            conversations.append({
                "id": d.get("id"),
                "title": d.get("title"),
                "created_at": d.get("created_at"),
                "updated_at": d.get("updated_at"),
                "message_count": len(d.get("messages", [])),
            })

        return jsonify({"ok": True, "conversations": conversations})

    except Exception as ex:
        logging.exception("Failed /ai-chat/conversations")
        return jsonify({"ok": False, "error": str(ex)}), 500


@app.route("/ai-chat/history/<conversation_id>", methods=["GET", "OPTIONS"])
def get_conversation_history(conversation_id):
    if request.method == "OPTIONS":
        return "", 204

    try:
        doc = _get_conversation_ref(conversation_id).get()
        if not doc.exists:
            return jsonify({"ok": False, "error": "Conversation not found"}), 404

        d = doc.to_dict()
        return jsonify({
            "ok": True,
            "conversation": {
                "id": d.get("id"),
                "title": d.get("title"),
                "created_at": d.get("created_at"),
                "updated_at": d.get("updated_at"),
                "messages": d.get("messages", []),
            },
        })

    except Exception as ex:
        logging.exception("Failed /ai-chat/history")
        return jsonify({"ok": False, "error": str(ex)}), 500


@app.route("/ai-chat/conversations/<conversation_id>", methods=["DELETE", "OPTIONS"])
def delete_conversation(conversation_id):
    if request.method == "OPTIONS":
        return "", 204

    try:
        ref = _get_conversation_ref(conversation_id)
        doc = ref.get()
        if not doc.exists:
            return jsonify({"ok": False, "error": "Conversation not found"}), 404

        ref.delete()
        return jsonify({"ok": True, "deleted": conversation_id})

    except Exception as ex:
        logging.exception("Failed DELETE /ai-chat/conversations")
        return jsonify({"ok": False, "error": str(ex)}), 500


# Cloud Run will use PORT env var
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
