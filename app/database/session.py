from tortoise import Tortoise

from app.config.settings import get_settings


async def init_db():
    await Tortoise.init(
        db_url=get_settings().database_url,
        modules={"models": ["app.database.models"]},
        _enable_global_fallback=True,
    )
    await Tortoise.generate_schemas()


async def close_db():
    await Tortoise.close_connections()
