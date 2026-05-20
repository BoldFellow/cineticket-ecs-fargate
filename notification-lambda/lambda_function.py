"""
CineTicket — Notification Lambda
Triggered by SQS (which is subscribed to the OrderPlaced SNS topic).
Sends a booking confirmation email via SES for every successful booking.

Why Lambda and not Fargate here?
  This work is purely event-driven: short, stateless, triggered once per booking.
  Lambda is the right tool — Fargate is for always-on HTTP services.

Environment variables:
  SENDER_EMAIL  - SES-verified sender address (must be verified in SES sandbox)
  AWS_REGION    - AWS region (default: us-east-1)
"""

import json
import logging
import os

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

ses          = boto3.client("ses", region_name=os.environ.get("AWS_REGION", "us-east-1"))
SENDER_EMAIL = os.environ["SENDER_EMAIL"]


def lambda_handler(event, context):
    for record in event["Records"]:
        # SQS record body contains the raw SNS notification JSON
        body    = json.loads(record["body"])
        message = json.loads(body["Message"])

        booking_id     = message.get("booking_id", "N/A")
        movie_id       = message.get("movie_id", "N/A")
        seat_id        = message.get("seat_id", "N/A")
        customer_email = message.get("customer_email")

        if not customer_email:
            log.warning("no customer_email in message, skipping: %s", message)
            continue

        ses.send_email(
            Source=SENDER_EMAIL,
            Destination={"ToAddresses": [customer_email]},
            Message={
                "Subject": {"Data": "Your CineTicket booking is confirmed!"},
                "Body": {
                    "Text": {
                        "Data": (
                            f"Your seat is booked!\n\n"
                            f"Booking ID : {booking_id}\n"
                            f"Movie      : {movie_id}\n"
                            f"Seat       : {seat_id}\n\n"
                            f"Enjoy the show!"
                        )
                    }
                },
            },
        )
        log.info("confirmation sent to %s for booking %s", customer_email, booking_id)

    return {"statusCode": 200}
