"""Logging estructurado que NUNCA registra bytes de imagen, base64 ni datos
biométricos crudos.

Regla dura: cualquier llamada a estos loggers debe pasar únicamente
metadatos (request_id, tamaños en bytes, tiempos, códigos de decisión). Si en
algún momento se necesita depurar contenido de imagen, hazlo fuera de estos
loggers y nunca lo commitees.
"""
import logging
import sys
from typing import Any, Mapping

_CONFIGURED = False


def configure_logging(level: int = logging.INFO) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    handler = logging.StreamHandler(stream=sys.stdout)
    formatter = logging.Formatter(
        fmt="%(asctime)s level=%(levelname)s logger=%(name)s %(message)s"
    )
    handler.setFormatter(formatter)

    root = logging.getLogger("kyc_service")
    root.setLevel(level)
    root.addHandler(handler)
    root.propagate = False
    _CONFIGURED = True


def get_logger(name: str = "kyc_service") -> logging.Logger:
    configure_logging()
    if not name.startswith("kyc_service"):
        name = f"kyc_service.{name}"
    return logging.getLogger(name)


# Claves explícitamente prohibidas en cualquier `extra=` o mensaje formateado
# pasado a estos loggers. Sirve como recordatorio/documentación, no como
# enforcement automático (no interceptamos el contenido de los mensajes).
FORBIDDEN_LOG_KEYS = frozenset(
    {
        "image",
        "image_bytes",
        "id_document_bytes",
        "selfie_bytes",
        "base64",
        "raw_bytes",
        "file_content",
    }
)


def safe_extra(**fields: Any) -> Mapping[str, Any]:
    """Helper opcional para construir un dict de metadatos de log, que falla
    ruidosamente si alguien intenta colar una clave prohibida (defensa en
    profundidad, no reemplaza la disciplina de no pasar bytes de imagen).
    """
    offending = FORBIDDEN_LOG_KEYS.intersection(fields.keys())
    if offending:
        raise ValueError(
            f"Intento de loggear campos prohibidos (posibles datos biométricos): {offending}"
        )
    return fields
