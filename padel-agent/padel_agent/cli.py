"""Chat por terminal con el agente: python -m padel_agent.cli"""

from __future__ import annotations

from .agent import PadelAgent
from .app import build_service_from_env


def _load_dotenv(path: str = ".env") -> None:
    import os
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip())
    except FileNotFoundError:
        pass


def main() -> None:
    _load_dotenv()
    service = build_service_from_env()
    agent = PadelAgent(service)
    print(f"Asistente de {service.club.name}. Escribe 'salir' para terminar.\n")
    while True:
        try:
            text = input("Tú: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if text.lower() in ("salir", "exit", "quit"):
            break
        if text:
            print(f"\nAsistente: {agent.ask(text)}\n")


if __name__ == "__main__":
    main()
