"""
CineTicket — Booking Service
Handles seat reservations. Owns the bookings RDS table and the DynamoDB seat-map writes.

Booking flow (POST /bookings):
  1. Verify the movie exists  — sync HTTP call to Movie Service (Service Discovery)
  2. Reserve the seat         — DynamoDB conditional write (ConditionalCheckFailedException → 409)
  3. Record the booking       — INSERT into RDS PostgreSQL (transactional record)
  4. Invalidate cache         — POST to Movie Service /invalidate-cache (non-fatal)
  5. Notify async             — Publish to SNS OrderPlaced topic (non-fatal)
  6. Return 201 + booking_id

Why Fargate and not Lambda?
  This is a long-lived HTTP service that holds a database connection and handles
  sustained traffic. Lambda's ephemeral model causes connection exhaustion on RDS.

Environment variables:
  DB_SECRET_NAME     - Secrets Manager secret (JSON: username, password, host, port, dbname)
  MOVIE_SERVICE_URL  - Base URL for Movie Service (default: http://movies.cineticket.local)
  SNS_TOPIC_ARN      - ARN of the OrderPlaced SNS topic
  DYNAMODB_TABLE_SEATS - DynamoDB table name for seat maps (PK: movie_id, SK: seat_id)
  AWS_REGION         - AWS region (default: us-east-1)
"""

import json
import logging
import os
import uuid

import boto3
import psycopg2
import requests
from botocore.exceptions import ClientError
from flask import Flask, jsonify, request
from flask_cors import CORS
from psycopg2.extras import RealDictCursor

# ---------------------------------------------------------------------------
# 1. CONFIG
# ---------------------------------------------------------------------------

AWS_REGION        = os.environ.get("AWS_REGION", "us-east-1")
DB_SECRET_NAME    = os.environ["DB_SECRET_NAME"]
MOVIE_SERVICE_URL = os.environ.get("MOVIE_SERVICE_URL", "http://movies.cineticket.local")
SNS_TOPIC_ARN     = os.environ["SNS_TOPIC_ARN"]
TABLE_SEATS       = os.environ["DYNAMODB_TABLE_SEATS"]

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

# ---------------------------------------------------------------------------
# 2. DATABASE CONNECTION
# ---------------------------------------------------------------------------

def _load_secret():
    client = boto3.client("secretsmanager", region_name=AWS_REGION)
    resp   = client.get_secret_value(SecretId=DB_SECRET_NAME)
    return json.loads(resp["SecretString"])

# Fetch once at container startup — matches Logistics app pattern
_secret = _load_secret()


def _db():
    """Open and return a fresh psycopg2 connection."""
    return psycopg2.connect(
        host=_secret["host"],
        port=int(_secret.get("port", 5432)),
        dbname=_secret["dbname"],
        user=_secret["username"],
        password=_secret["password"],
        connect_timeout=5,
    )


# ---------------------------------------------------------------------------
# 3. AWS CLIENTS
# ---------------------------------------------------------------------------

dynamodb   = boto3.resource("dynamodb", region_name=AWS_REGION)
seats_tbl  = dynamodb.Table(TABLE_SEATS)
sns        = boto3.client("sns", region_name=AWS_REGION)

# ---------------------------------------------------------------------------
# 4. ROUTES
# ---------------------------------------------------------------------------

@app.route("/health")
def health():
    """Health check — verifies RDS connectivity. ALB uses this for target health."""
    try:
        conn = _db()
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        conn.close()
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        log.error("health check failed: %s", e)
        return jsonify({"status": "unhealthy", "error": str(e)}), 503


@app.route("/bookings", methods=["POST"])
def create_booking():
    body           = request.get_json(silent=True) or {}
    movie_id       = body.get("movie_id", "").strip()
    seat_id        = body.get("seat_id", "").strip()
    customer_email = body.get("customer_email", "").strip()

    if not movie_id or not seat_id or not customer_email:
        return jsonify({"error": "movie_id, seat_id, and customer_email are required"}), 400

    # --- Step 1: verify the movie exists (sync call to Movie Service) ----------
    # Teaching point: this is synchronous because we need the answer NOW
    # before we commit to reserving a seat.
    try:
        resp = requests.get(f"{MOVIE_SERVICE_URL}/movies/{movie_id}", timeout=5)
        if resp.status_code == 404:
            return jsonify({"error": "movie not found"}), 404
        resp.raise_for_status()
    except requests.RequestException as e:
        log.error("Movie Service unreachable: %s", e)
        return jsonify({"error": "could not reach Movie Service"}), 502

    # --- Step 2: reserve the seat (DynamoDB conditional write) ----------------
    # Teaching point: ConditionExpression guarantees only ONE booking wins
    # when two users hit "Book" at the same time — this is the race condition fix.
    try:
        seats_tbl.update_item(
            Key={"movie_id": movie_id, "seat_id": seat_id},
            UpdateExpression="SET #s = :booked",
            ConditionExpression="#s = :available",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":booked": "booked", ":available": "available"},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            # Another request already booked this seat — deterministic 409
            return jsonify({"error": "seat is not available"}), 409
        log.error("DynamoDB error: %s", e)
        return jsonify({"error": "failed to reserve seat"}), 500

    # --- Step 3: insert booking record into RDS --------------------------------
    booking_id = str(uuid.uuid4())
    try:
        conn = _db()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO bookings (booking_id, movie_id, seat_id, customer_email) "
                "VALUES (%s, %s, %s, %s)",
                (booking_id, movie_id, seat_id, customer_email),
            )
        conn.commit()
        conn.close()
    except Exception as e:
        log.error("RDS insert failed: %s", e)
        return jsonify({"error": "booking record could not be saved"}), 500

    # --- Step 4: invalidate Movie Service cache --------------------------------
    # Teaching point: the cache must be busted NOW so the next GET /movies/{id}
    # shows the seat as 'booked', not stale 'available'.
    try:
        requests.post(f"{MOVIE_SERVICE_URL}/movies/{movie_id}/invalidate-cache", timeout=3)
    except requests.RequestException as e:
        log.warning("cache invalidation failed (non-fatal): %s", e)

    # --- Step 5: publish OrderPlaced event to SNS (async) ---------------------
    # Teaching point: the email notification does NOT need to block the HTTP
    # response. We publish the event and return immediately — Lambda picks it up.
    try:
        sns.publish(
            TopicArn=SNS_TOPIC_ARN,
            Message=json.dumps({
                "booking_id":     booking_id,
                "movie_id":       movie_id,
                "seat_id":        seat_id,
                "customer_email": customer_email,
            }),
            Subject="OrderPlaced",
        )
    except Exception as e:
        log.warning("SNS publish failed (non-fatal): %s", e)

    return jsonify({
        "booking_id": booking_id,
        "movie_id":   movie_id,
        "seat_id":    seat_id,
        "status":     "confirmed",
    }), 201


@app.route("/bookings/<booking_id>")
def get_booking(booking_id):
    try:
        conn = _db()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM bookings WHERE booking_id = %s", (booking_id,))
            row = cur.fetchone()
        conn.close()
    except Exception as e:
        log.error("RDS query failed: %s", e)
        return jsonify({"error": "could not fetch booking"}), 500

    if not row:
        return jsonify({"error": "booking not found"}), 404

    result = dict(row)
    if result.get("booked_at"):
        result["booked_at"] = result["booked_at"].isoformat()
    return jsonify(result), 200


@app.route("/bookings/<booking_id>", methods=["DELETE"])
def cancel_booking(booking_id):
    # --- Fetch the booking so we know which seat to release --------------------
    try:
        conn = _db()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM bookings WHERE booking_id = %s", (booking_id,))
            row = cur.fetchone()
        conn.close()
    except Exception as e:
        log.error("RDS query failed: %s", e)
        return jsonify({"error": "could not fetch booking"}), 500

    if not row:
        return jsonify({"error": "booking not found"}), 404

    movie_id = row["movie_id"]
    seat_id  = row["seat_id"]

    # --- Release the seat in DynamoDB (unconditional — cancellation wins) ------
    # Teaching point: no condition here because we OWN this seat; the booking
    # record in RDS is our source of truth that we're allowed to release it.
    try:
        seats_tbl.update_item(
            Key={"movie_id": movie_id, "seat_id": seat_id},
            UpdateExpression="SET #s = :available",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":available": "available"},
        )
    except ClientError as e:
        log.error("DynamoDB release failed: %s", e)
        return jsonify({"error": "could not release seat"}), 500

    # --- Delete the booking record from RDS ------------------------------------
    try:
        conn = _db()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM bookings WHERE booking_id = %s", (booking_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        log.error("RDS delete failed: %s", e)
        return jsonify({"error": "could not delete booking record"}), 500

    # --- Invalidate Movie Service cache ----------------------------------------
    try:
        requests.post(f"{MOVIE_SERVICE_URL}/movies/{movie_id}/invalidate-cache", timeout=3)
    except requests.RequestException as e:
        log.warning("cache invalidation failed (non-fatal): %s", e)

    return jsonify({
        "message":    "booking cancelled",
        "booking_id": booking_id,
        "movie_id":   movie_id,
        "seat_id":    seat_id,
        "status":     "seat returned to pool",
    }), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
