from tortoise import fields
from tortoise.models import Model


class ETFConstituents(Model):
    id = fields.IntField(pk=True, auto_now_add=True)
    etf_symbol = fields.CharField(max_length=24, index=True)
    ticker = fields.CharField(max_length=24)
    name = fields.CharField(max_length=255)
    weight = fields.FloatField()
    fetched_at = fields.DatetimeField(db_default=fields.Now())

    class Meta:
        table = "etf_constituents"
        ordering = ["-fetched_at"]

    def __str__(self):
        return (
            f"ETFConstituents(id={self.id}, etf_symbol={self.etf_symbol}, "
            f"ticker={self.ticker}, weight={self.weight})"
        )
