import logging
import os
import re
from bot import build_application

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("main")


def read_token() -> str:
    return (os.getenv("TOKEN") or os.getenv("TELEGRAM") or "").strip()


def validate_token(token: str) -> None:
    if not token:
        raise RuntimeError("TOKEN vazio. Configure a variável TOKEN no Railway.")
    if token.lower() == "token":
        raise RuntimeError("TOKEN está como 'token' (placeholder). Cole o token real do @BotFather.")
    if not re.match(r"^\d+:[A-Za-z0-9_-]{30,}$", token):
        raise RuntimeError(f"TOKEN inválido (formato inesperado). Caracteres lidos: {len(token)}")


def main() -> None:
    token = read_token()
    validate_token(token)

    app = build_application(token)
    log.info("Bot iniciando (polling). Token lido com %s caracteres.", len(token))
    app.run_polling(drop_pending_updates=True, allowed_updates=["message"])


if __name__ == "__main__":
    main()
