"""Modelos pydantic de request/response de la API.

Ninguno de estos modelos transporta bytes de imagen: las imágenes llegan
como `UploadFile` (multipart) y nunca se serializan de vuelta al cliente ni
a logs.
"""
from typing import Literal, Optional

from pydantic import BaseModel, Field


class LivenessResultSchema(BaseModel):
    """Resultado de la verificación de vida (liveness). Ver app/liveness.py."""

    status: Literal["skipped", "passed", "failed"]
    score: Optional[float] = None


class VerifyResponse(BaseModel):
    request_id: str
    match: bool
    similarity: float = Field(..., ge=0.0, le=1.0)
    distance: float
    threshold: float
    model: str
    liveness: LivenessResultSchema


class ErrorResponse(BaseModel):
    """Forma uniforme de error. `error` es un código estable para el cliente
    (Flutter), `message` es un texto en español apto para mostrar al usuario.
    """

    error: str
    message: str
    request_id: Optional[str] = None


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
