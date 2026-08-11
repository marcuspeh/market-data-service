from tortoise import fields
from tortoise.models import Model


class MarketBar(Model):
    """One daily OHLCV bar for a ticker."""

    id = fields.IntField(pk=True)
    ticker = fields.CharField(max_length=20)

    # Daily bar open time (UTC midnight). Single source of truth for the
    # bar's date — date-specific filtering is done by range queries on
    # this column.
    timestamp = fields.BigIntField()

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
        # One row per ticker per bar open time.
        unique_together = [("ticker", "timestamp")]
        indexes = [
            ("ticker", "timestamp"),
        ]
        ordering = ["-timestamp"]

    def __str__(self) -> str:
        return (
            f"MarketBar(ticker={self.ticker}, "
            f"ts={self.timestamp}, close={self.close})"
        )