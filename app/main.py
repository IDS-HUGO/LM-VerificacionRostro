import time
import uuid
from typing import Optional

from fastapi import FastAPI, File, Header, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.logging_config import get_logger
from app.pipeline import (
    InvalidImageError,
    NoFaceOnIdError,
    NoFaceOnSelfieError,
    run_verification,
)
from app.schemas import ErrorResponse, HealthResponse, LivenessResultSchema, VerifyResponse

logger = get_logger(__name__)

app = FastAPI(
    title="YO FREE-LANCER KYC Service",
    description=(
        "Verificación facial (selfie vs identificación oficial) para el registro "
        "de usuarios de YO FREE-LANCER. Procesa las imágenes en memoria y nunca "
        "las persiste. Fail-closed: cualquier error, timeout o respuesta no-2xx "
        "debe tratarse como NO verificado por quien consuma esta API."
    ),
    version="0.1.0",
)

_settings = get_settings()
_allowed_origins = _settings.allowed_origins_list
if _allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_allowed_origins,
        allow_credentials=True,
        allow_methods=["POST", "GET", "OPTIONS"],
        allow_headers=["Content-Type", "X-Request-Id", "Authorization"],
    )
    logger.info("cors_enabled", extra={"allowed_origins": _allowed_origins})
else:
    logger.info("cors_disabled_no_origins_configured")


class PayloadTooLargeError(Exception):
    """El archivo subido excede `MAX_UPLOAD_SIZE_MB`. Se levanta ANTES de
    intentar decodificar la imagen, para evitar procesar uploads
    desproporcionadamente grandes (protección tipo decompression-bomb)."""

    def __init__(self, field_name: str, limit_mb: int):
        self.field_name = field_name
        self.limit_mb = limit_mb
        super().__init__(f"'{field_name}' excede el límite de {limit_mb} MB")


def _resolve_request_id(explicit: Optional[str]) -> str:
    """Usa el `X-Request-Id` recibido si viene, si no genera uno nuevo. Nunca
    lanza; siempre devuelve un string usable para correlacionar logs."""
    return explicit if explicit else str(uuid.uuid4())


async def _read_upload_within_limit(upload: UploadFile, field_name: str) -> bytes:
    """Lee el upload completo a memoria y valida su tamaño contra
    `MAX_UPLOAD_SIZE_MB` ANTES de que el pipeline intente decodificarlo con
    OpenCV/DeepFace. Este orden es intencional: rechazar por tamaño debe ser
    más barato que cualquier intento de decodificación/detección de rostro.
    """
    settings = get_settings()
    data = await upload.read()
    if len(data) > settings.max_upload_size_bytes:
        raise PayloadTooLargeError(field_name, settings.MAX_UPLOAD_SIZE_MB)
    return data


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Readiness probe.

    IMPORTANTE: un 200 aquí NO significa "hay un verificador de respaldo
    disponible". Este servicio no tiene fallback propio: si está caído, la
    verificación KYC simplemente no puede completarse. Ese es el
    comportamiento correcto (fail-closed) y NO debe interpretarse como una
    señal para activar un verificador alterno o saltarse la verificación.
    """
    return HealthResponse()


@app.post(
    "/verify",
    response_model=VerifyResponse,
    responses={
        400: {"model": ErrorResponse, "description": "Imagen inválida/corrupta"},
        413: {"model": ErrorResponse, "description": "Archivo demasiado grande"},
        422: {"model": ErrorResponse, "description": "No se detectó rostro en ID o selfie"},
        503: {"model": ErrorResponse, "description": "Verificación no disponible"},
    },
)
async def verify(
    id_document: UploadFile = File(..., description="Foto de INE o pasaporte"),
    selfie: UploadFile = File(..., description="Selfie tomada en el momento"),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
) -> VerifyResponse:
    request_id = _resolve_request_id(x_request_id)

    id_bytes = await _read_upload_within_limit(id_document, "id_document")
    selfie_bytes = await _read_upload_within_limit(selfie, "selfie")

    logger.info(
        "verify_request_received",
        extra={
            "request_id": request_id,
            "id_document_size_bytes": len(id_bytes),
            "selfie_size_bytes": len(selfie_bytes),
        },
    )

    start = time.monotonic()
    settings = get_settings()
    result = run_verification(id_bytes, selfie_bytes, settings)
    elapsed_ms = round((time.monotonic() - start) * 1000, 1)

    logger.info(
        "verify_request_completed",
        extra={
            "request_id": request_id,
            "match": result.match,
            "distance": result.distance,
            "threshold": result.threshold,
            "elapsed_ms": elapsed_ms,
        },
    )

    return VerifyResponse(
        request_id=request_id,
        match=result.match,
        similarity=result.similarity,
        distance=result.distance,
        threshold=result.threshold,
        model=result.model,
        liveness=LivenessResultSchema(status=result.liveness.status, score=result.liveness.score),
    )


def _request_id_from_request(request: Request) -> str:
    return _resolve_request_id(request.headers.get("x-request-id"))


@app.exception_handler(PayloadTooLargeError)
async def handle_payload_too_large(request: Request, exc: PayloadTooLargeError) -> JSONResponse:
    request_id = _request_id_from_request(request)
    logger.warning(
        "upload_rejected_too_large",
        extra={"request_id": request_id, "field": exc.field_name, "limit_mb": exc.limit_mb},
    )
    return JSONResponse(
        status_code=413,
        content=ErrorResponse(
            error="PAYLOAD_TOO_LARGE",
            message=f"El archivo '{exc.field_name}' excede el límite de {exc.limit_mb} MB.",
            request_id=request_id,
        ).model_dump(),
    )


@app.exception_handler(InvalidImageError)
async def handle_invalid_image(request: Request, exc: InvalidImageError) -> JSONResponse:
    request_id = _request_id_from_request(request)
    logger.warning("invalid_image_upload", extra={"request_id": request_id})
    return JSONResponse(
        status_code=400,
        content=ErrorResponse(
            error="INVALID_IMAGE",
            message="No pudimos procesar una de las imágenes enviadas. Verifica el formato e intenta de nuevo.",
            request_id=request_id,
        ).model_dump(),
    )


@app.exception_handler(NoFaceOnIdError)
async def handle_no_face_on_id(request: Request, exc: NoFaceOnIdError) -> JSONResponse:
    request_id = _request_id_from_request(request)
    logger.info("no_face_on_id", extra={"request_id": request_id})
    return JSONResponse(
        status_code=422,
        content=ErrorResponse(
            error="NO_FACE_ON_ID",
            message=(
                "No pudimos detectar un rostro en tu identificación. "
                "Verifica que la foto sea clara y vuelve a intentarlo."
            ),
            request_id=request_id,
        ).model_dump(),
    )


@app.exception_handler(NoFaceOnSelfieError)
async def handle_no_face_on_selfie(request: Request, exc: NoFaceOnSelfieError) -> JSONResponse:
    request_id = _request_id_from_request(request)
    logger.info("no_face_on_selfie", extra={"request_id": request_id})
    return JSONResponse(
        status_code=422,
        content=ErrorResponse(
            error="NO_FACE_ON_SELFIE",
            message=(
                "No pudimos detectar un rostro en tu selfie. "
                "Asegúrate de tener buena iluminación y vuelve a intentarlo."
            ),
            request_id=request_id,
        ).model_dump(),
    )


@app.exception_handler(Exception)
async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """Red de seguridad fail-closed: cualquier excepción no anticipada
    (incluyendo `TransientModelError` ya reintentado y agotado) termina en un
    503 explícito, nunca en un match silencioso ni en un 500 con stack trace
    expuesto al cliente."""
    request_id = _request_id_from_request(request)
    logger.error(
        "unexpected_verification_failure",
        extra={"request_id": request_id, "exception_type": type(exc).__name__},
    )
    return JSONResponse(
        status_code=503,
        content=ErrorResponse(
            error="VERIFICATION_UNAVAILABLE",
            message="El servicio de verificación no está disponible en este momento. Intenta de nuevo más tarde.",
            request_id=request_id,
        ).model_dump(),
    )


if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    uvicorn.run("app.main:app", host="0.0.0.0", port=settings.PORT)