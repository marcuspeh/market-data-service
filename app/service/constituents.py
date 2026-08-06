from app.database.repositories.etf_constituents import ETFConstituentsRepository


class ConstituentsService:
    def __init__(self):
        self.constituents_repository = ETFConstituentsRepository()

    def get_constituents(self, symbol: str) -> List[Dict]:
        return self.constituents_repository.get_cached(symbol.upper())

    