-- CineTicket Bookings Schema
-- Run once against the RDS PostgreSQL instance before starting the Booking Service.

CREATE TABLE IF NOT EXISTS bookings (
    booking_id     UUID         PRIMARY KEY,
    movie_id       VARCHAR(64)  NOT NULL,
    seat_id        VARCHAR(16)  NOT NULL,
    customer_email VARCHAR(255) NOT NULL,
    booked_at      TIMESTAMPTZ  DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_bookings_movie ON bookings (movie_id);
