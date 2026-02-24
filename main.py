import os
import json
import logging
from typing import Dict, Any, List

from flask import Flask, request, jsonify
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)

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
MAX_ACCOUNTS = int(os.getenv("MAX_ACCOUNTS", "50"))                 # כמה חשבונות למשוך מתחת ל-MCC
MAX_CAMPAIGNS_PER_ACCOUNT = int(os.getenv("MAX_CAMPAIGNS_PER_ACCOUNT", "200"))  # כמה קמפיינים לכל חשבון


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

    # מדדים בסיסיים ב-30 ימים אחרונים (LAST_30_DAYS)
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
        ORDER BY metrics.impressions DESC
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


# Cloud Run will use PORT env var
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
