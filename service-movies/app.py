"""
CineTicket — Movie Service
Reads movie listings and seat availability from DynamoDB.
Caches results in ElastiCache Redis (cache-aside, 60s TTL).
Exposes an internal cache-invalidation endpoint called by Booking Service.

Environment variables:
  DYNAMODB_TABLE_MOVIES  - DynamoDB table name for movie records
  DYNAMODB_TABLE_SEATS   - DynamoDB table name for seat maps (PK: movie_id, SK: seat_id)
  REDIS_HOST             - ElastiCache Redis primary endpoint hostname
  REDIS_PORT             - Redis port (default: 6379)
  AWS_REGION             - AWS region (default: us-east-1)

Request path (via ALB):  /movies/*
Internal path (via ECS Service Discovery):  movies.cineticket.local/movies/*
"""

import json
import logging
import os

import boto3
from boto3.dynamodb.conditions import Key
from flask import Flask, jsonify
from flask_cors import CORS
import redis

# ---------------------------------------------------------------------------
# 1. CONFIG
# ---------------------------------------------------------------------------

AWS_REGION        = os.environ.get("AWS_REGION", "us-east-1")
TABLE_MOVIES      = os.environ["DYNAMODB_TABLE_MOVIES"]
TABLE_SEATS       = os.environ["DYNAMODB_TABLE_SEATS"]
REDIS_HOST        = os.environ["REDIS_HOST"]
REDIS_PORT        = int(os.environ.get("REDIS_PORT", "6379"))
CACHE_TTL_SECONDS = 60

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

dynamodb    = boto3.resource("dynamodb", region_name=AWS_REGION)
movies_tbl  = dynamodb.Table(TABLE_MOVIES)
seats_tbl   = dynamodb.Table(TABLE_SEATS)

cache = redis.Redis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    decode_responses=True,
    socket_connect_timeout=2,
)

# ---------------------------------------------------------------------------
# 2. HELPERS
# ---------------------------------------------------------------------------

def _seat_map(movie_id):
    """Return {seat_id: status} dict for a movie from DynamoDB."""
    resp = seats_tbl.query(KeyConditionExpression=Key("movie_id").eq(movie_id))
    return {item["seat_id"]: item["status"] for item in resp.get("Items", [])}


def _build_movie(item, include_seats=True):
    out = {
        "movie_id":    item["movie_id"],
        "title":       item["title"],
        "genre":       item.get("genre", ""),
        "showtimes":   item.get("showtimes", []),
        "description": item.get("description", ""),
    }
    if include_seats:
        out["seat_map"] = _seat_map(item["movie_id"])
    return out


def _cache_get(key):
    try:
        val = cache.get(key)
        if val:
            log.info("cache HIT: %s", key)
        else:
            log.info("cache MISS: %s", key)
        return val
    except Exception as e:
        log.warning("Redis GET failed (non-fatal): %s", e)
        return None


def _cache_set(key, value):
    try:
        cache.setex(key, CACHE_TTL_SECONDS, value)
    except Exception as e:
        log.warning("Redis SET failed (non-fatal): %s", e)


def _cache_delete(*keys):
    try:
        deleted = cache.delete(*keys)
        log.info("cache invalidated %d key(s): %s", deleted, keys)
        return deleted
    except Exception as e:
        log.warning("Redis DEL failed (non-fatal): %s", e)
        return 0


# ---------------------------------------------------------------------------
# 3. ROUTES
# ---------------------------------------------------------------------------

@app.route("/health")
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/movies")
def list_movies():
    """List all movies (no seat maps — listing page only)."""
    cache_key = "movies:all"
    cached = _cache_get(cache_key)
    if cached:
        return jsonify(json.loads(cached)), 200

    resp   = movies_tbl.scan()
    movies = [_build_movie(item, include_seats=False) for item in resp.get("Items", [])]
    _cache_set(cache_key, json.dumps(movies))
    return jsonify(movies), 200


@app.route("/movies/<movie_id>")
def get_movie(movie_id):
    """Get a single movie with its full seat map."""
    cache_key = f"movies:{movie_id}"
    cached = _cache_get(cache_key)
    if cached:
        return jsonify(json.loads(cached)), 200

    resp = movies_tbl.get_item(Key={"movie_id": movie_id})
    item = resp.get("Item")
    if not item:
        return jsonify({"error": "movie not found"}), 404

    movie = _build_movie(item, include_seats=True)
    _cache_set(cache_key, json.dumps(movie))
    return jsonify(movie), 200


@app.route("/movies/<movie_id>/invalidate-cache", methods=["POST"])
def invalidate_cache(movie_id):
    """
    Internal endpoint — called by Booking Service after a seat is booked.
    Deletes the per-movie and the listing cache so the next GET reflects
    the updated seat status immediately (no stale 'available' shown to users).
    """
    deleted = _cache_delete(f"movies:{movie_id}", "movies:all")
    return jsonify({"invalidated": deleted}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
