import os
from dotenv import load_dotenv

load_dotenv()

def _req(key: str) -> str:
    v = os.getenv(key)
    if not v:
        raise RuntimeError(f"Missing required env var: {key}")
    return v

RPC_URL = os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")
RPC_WS_URL = os.getenv("RPC_WS_URL", "wss://api.mainnet-beta.solana.com")

PRIVATE_KEY = _req("PRIVATE_KEY")

JITO_BLOCK_ENGINE_URL = os.getenv(
    "JITO_BLOCK_ENGINE_URL",
    "https://mainnet.block-engine.jito.labs.io/api/v1/bundles",
)
USE_JITO = os.getenv("USE_JITO", "true").lower() == "true"

# --- Sniper filters ---
MIN_MIGRATION_RATE = float(os.getenv("MIN_MIGRATION_RATE", "0.5"))
MIN_TOKENS_LAUNCHED = int(os.getenv("MIN_TOKENS_LAUNCHED", "3"))
REQUIRE_SOCIAL = os.getenv("REQUIRE_SOCIAL", "true").lower() == "true"
# Market cap limits in SOL (0 = disabled)
MIN_MARKET_CAP_SOL = float(os.getenv("MIN_MARKET_CAP_SOL", "0"))
MAX_MARKET_CAP_SOL = float(os.getenv("MAX_MARKET_CAP_SOL", "0"))

# --- Buy parameters ---
BUY_AMOUNT_SOL = float(os.getenv("BUY_AMOUNT_SOL", "0.1"))
SLIPPAGE = float(os.getenv("SLIPPAGE", "0.25"))
PRIORITY_FEE_MICROLAMPORTS = int(os.getenv("PRIORITY_FEE_MICROLAMPORTS", "500000"))
JITO_TIP_SOL = float(os.getenv("JITO_TIP_SOL", "0.003"))
MAX_CONCURRENT_SNIPES = int(os.getenv("MAX_CONCURRENT_SNIPES", "3"))

LAMPORTS_PER_SOL = 1_000_000_000
