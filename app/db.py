import urllib.parse
from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base

from app.config import settings

def get_async_db_url_and_args(raw_url: str):
    parsed = urllib.parse.urlsplit(raw_url)
    scheme = parsed.scheme
    if scheme in ("postgres", "postgresql"):
        scheme = "postgresql+asyncpg"
    elif scheme != "postgresql+asyncpg":
        scheme = "postgresql+asyncpg"

    query_params = urllib.parse.parse_qs(parsed.query)
    
    # Check if SSL should be enabled
    use_ssl = False
    if "sslmode" in query_params:
        ssl_val = query_params["sslmode"][0].lower()
        if ssl_val in ("require", "verify-ca", "verify-full"):
            use_ssl = True
    elif "neon.tech" in parsed.netloc:
        use_ssl = True

    # Strip params asyncpg does not recognize
    query_params.pop("sslmode", None)
    query_params.pop("channel_binding", None)

    new_query = urllib.parse.urlencode(query_params, doseq=True)
    clean_url = urllib.parse.urlunsplit((scheme, parsed.netloc, parsed.path, new_query, parsed.fragment))

    connect_args = {}
    if use_ssl:
        connect_args["ssl"] = True

    return clean_url, connect_args

db_url, connect_args = get_async_db_url_and_args(settings.DATABASE_URL)

engine = create_async_engine(
    db_url,
    connect_args=connect_args,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=2,
    future=True,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)

Base = declarative_base()

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()
