from tortoise import fields
from tortoise.models import Model


class MarketBar(Model):
    """One OHLCV bar for a ticker at a given timestamp/timespan."""

    id = fields.IntField(pk=True)
    ticker = fields.CharField(max_length=20)
    timespan = fields.CharField(max_length=10)  # "day" | "hour" | "minute"
    multiplier = fields.IntField()  # e.g. 1 for "1 day"

    timestamp_ms = fields.BigIntField()  # Polygon's bar timestamp in ms
    bar_date = fields.DateField()  # date-only, used for range queries / dedupe

    open = fields.FloatField()
    high = fields.FloatField()
    low = fields.FloatField()
    close = fields.FloatField()
    volume = fields.FloatField()
    vwap = fields.FloatField(null=True)
    trade_count = fields.IntField(null=True)

    fetched_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "market_bars"
        # Composite uniqueness: one bar per (ticker, timespan, multiplier, ts)
        unique_together = [("ticker", "timespan", "multiplier", "timestamp_ms")]
        indexes = [
            ("ticker", "timespan", "multiplier", "bar_date"),
        ]
        ordering = ["-timestamp_ms"]

    def __str__(self) -> str:
        return (
            f"MarketBar(ticker={self.ticker}, timespan={self.timespan}, "
            f"ts={self.timestamp_ms}, close={self.close})"
        )