"""Lógica de extracción de rostro y comparación facial (KYC).

Contrato de fallo cerrado ("fail closed"): cualquier ambigüedad, error de
decodificación, ausencia de rostro o fallo inesperado debe terminar en una
excepción específica que `app/main.py` traduce a una respuesta 4xx/5xx — este
módulo nunca debe devolver un resultado "match=True" por default ni tragarse
silenciosamente un error.

`deepface` se importa de forma perezosa (dentro de las funciones, no a nivel
de módulo) a propósito: en entornos sin los pesos del modelo descargados (o
sin `tensorflow` instalable, p.ej. Python muy nuevo) el resto de la app y los
tests que mockean DeepFace deben poder cargar sin requerir la librería real.
"""
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, TypeVar

import cv2
import numpy as np

from app.config import Settings, get_settings
from app.liveness import LivenessResult, check_liveness
from app.logging_config import get_logger

logger = get_logger(__name__)

T = TypeVar("T")

# Backoff en segundos entre reintentos del *invocación al modelo* (nunca de
# la request completa). Un máximo de 2 reintentos == hasta 3 intentos totales.
_RETRY_BACKOFFS_SECONDS = (0.5, 1.5)

# Fragmentos de mensaje (en minúsculas) que, si aparecen en una excepción
# lanzada por DeepFace/TensorFlow/OpenCV, se consideran indicativos de un
# problema transitorio de infraestructura (p.ej. el modelo todavía se está
# descargando o cargando a disco) y no de una imagen sin rostro. Es una
# heurística deliberada: DeepFace no expone una jerarquía de excepciones
# propia y consistente para distinguir estos casos.
_TRANSIENT_INFRA_MARKERS = (
    "model file not found",
    "confirm that",  # DeepFace loguea variantes de "confirm that x.h5 exists"
    "downloading",
    "download the model weights",
    "connection",
    "timed out",
    "temporarily unavailable",
    "resource temporarily unavailable",
    "file exists",  # carrera al escribir el cache de pesos del modelo
    "read-only file system",
)

# Fragmentos que indican, de forma determinística, que no se detectó ningún
# rostro en la imagen (no es un problema de infraestructura: reintentar con
# la misma imagen produciría el mismo resultado).
_NO_FACE_MARKERS = (
    "face could not be detected",
    "could not be detected",
    "no face",
    "face detector",
)


class InvalidImageError(Exception):
    """Los bytes recibidos no pudieron decodificarse como imagen válida."""


class NoFaceOnIdError(Exception):
    """No se detectó ningún rostro en la imagen de identificación."""


class NoFaceOnSelfieError(Exception):
    """No se detectó ningún rostro en la selfie."""


class TransientModelError(Exception):
    """Fallo transitorio de infraestructura al invocar el modelo (p.ej. pesos
    aún no cargados). Se reintenta un número acotado de veces. NUNCA se debe
    lanzar para el caso determinístico de "no hay rostro en la imagen".
    """


@dataclass(frozen=True)
class VerificationResult:
    match: bool
    similarity: float
    distance: float
    threshold: float
    model: str
    liveness: LivenessResult


def decode_image(data: bytes) -> np.ndarray:
    """Decodifica bytes de imagen directamente a un arreglo numpy en memoria.

    Nunca escribe a disco. Para bytes corruptos/no-imagen, `cv2.imdecode`
    normalmente devuelve None, pero para algunos casos límite (p. ej. buffer
    vacío) puede lanzar directamente un `cv2.error` en vez de devolver None.
    Cubrimos ambos casos aquí y siempre los traducimos a `InvalidImageError`
    para mantener el contrato de fallo cerrado (nunca debe escapar un
    `cv2.error` crudo hacia la capa HTTP).
    """
    if not data:
        raise InvalidImageError("No se recibieron bytes de imagen.")
    buffer = np.frombuffer(data, dtype=np.uint8)
    try:
        image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
    except cv2.error as exc:
        raise InvalidImageError("No se pudo decodificar la imagen recibida.") from exc
    if image is None:
        raise InvalidImageError("No se pudo decodificar la imagen recibida.")
    return image


def decide_match(distance: float, threshold: float) -> bool:
    """Función pura de decisión: aísla la regla de negocio del resto del
    pipeline para poder probarla sin invocar DeepFace."""
    return distance <= threshold


def _distance_to_similarity(distance: float) -> float:
    """Convierte distancia coseno a un score de similitud amigable en [0,1].
    Solo informativo: la decisión de match usa `distance`/`threshold`
    directamente, no este valor derivado."""
    return max(0.0, min(1.0, 1.0 - distance))


def _matches_any(message: str, markers: tuple) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in markers)


def _classify_exception(exc: Exception) -> Exception:
    """Reclasifica una excepción cruda de DeepFace en una de nuestras
    excepciones tipadas, según su mensaje. Si no matchea ningún patrón
    conocido, se devuelve tal cual (fallará el request con 503 más arriba)."""
    message = str(exc)
    if _matches_any(message, _TRANSIENT_INFRA_MARKERS):
        return TransientModelError(message)
    return exc


def _with_retry(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Ejecuta `func` reintentando SOLO cuando la excepción (tras
    reclasificar) es un `TransientModelError`. Ausencia de rostro u otros
    errores fallan rápido, sin reintento, porque son determinísticos dado el
    mismo par de imágenes."""
    attempt = 0
    while True:
        try:
            return func(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - reclasificamos explícitamente
            classified = _classify_exception(exc)
            if isinstance(classified, TransientModelError) and attempt < len(_RETRY_BACKOFFS_SECONDS):
                logger.warning(
                    "Fallo transitorio del modelo, reintentando",
                    extra={"attempt": attempt + 1, "max_attempts": len(_RETRY_BACKOFFS_SECONDS) + 1},
                )
                time.sleep(_RETRY_BACKOFFS_SECONDS[attempt])
                attempt += 1
                continue
            raise classified from exc


def _deepface_extract_faces(image: np.ndarray, detector_backend: str) -> list:
    from deepface import DeepFace  # import perezoso a propósito, ver docstring del módulo

    return DeepFace.extract_faces(
        img_path=image,
        detector_backend=detector_backend,
        enforce_detection=True,
        align=True,
    )


def _deepface_verify(
    img1: np.ndarray,
    img2: np.ndarray,
    model_name: str,
    detector_backend: str,
    distance_metric: str,
) -> Dict[str, Any]:
    from deepface import DeepFace  # import perezoso a propósito, ver docstring del módulo

    return DeepFace.verify(
        img1_path=img1,
        img2_path=img2,
        model_name=model_name,
        detector_backend=detector_backend,
        distance_metric=distance_metric,
        enforce_detection=True,
    )


def _face_crop_to_bgr_uint8(face: np.ndarray) -> np.ndarray:
    """`DeepFace.extract_faces` devuelve el rostro recortado como float64 en
    rango [0,1] y canales en orden RGB. `DeepFace.verify` espera imágenes en
    el mismo formato que produce `cv2.imread`/`cv2.imdecode` (BGR, uint8), así
    que convertimos aquí antes de usarlo como `img1_path`."""
    face_uint8 = np.clip(face * 255.0, 0, 255).astype(np.uint8)
    return cv2.cvtColor(face_uint8, cv2.COLOR_RGB2BGR)


def extract_id_face(id_image: np.ndarray, settings: Settings) -> np.ndarray:
    """Extrae el rostro de mayor confianza de la imagen de identificación.

    Lanza `NoFaceOnIdError` (fail-closed, no reintentable) si no se detecta
    ningún rostro. Lanza `TransientModelError` (reintentable, manejado por
    `_with_retry`) si el fallo parece de infraestructura.
    """
    try:
        faces = _with_retry(_deepface_extract_faces, id_image, settings.DETECTOR_BACKEND)
    except TransientModelError:
        raise
    except Exception as exc:
        if _matches_any(str(exc), _NO_FACE_MARKERS):
            raise NoFaceOnIdError(str(exc)) from exc
        raise

    if not faces:
        raise NoFaceOnIdError("No se detectó ningún rostro en la identificación.")

    best_face = max(faces, key=lambda f: f.get("confidence", 0.0))
    return _face_crop_to_bgr_uint8(best_face["face"])


def verify_faces(id_face_crop_bgr: np.ndarray, selfie_image: np.ndarray, settings: Settings) -> Dict[str, Any]:
    """Compara el recorte del rostro de la ID contra la selfie.

    Como `id_face_crop_bgr` ya es un recorte de rostro validado por
    `extract_id_face`, cualquier error de "rostro no detectado" que surja
    aquí se atribuye a la selfie (limitación conocida y aceptada del diseño:
    en teoría un recorte extremo podría fallar la re-detección interna de
    DeepFace.verify y mal-atribuirse a la selfie; no se maneja de forma
    especial porque el diseño del pipeline ya fue decidido).
    """
    try:
        return _with_retry(
            _deepface_verify,
            id_face_crop_bgr,
            selfie_image,
            settings.MODEL_NAME,
            settings.DETECTOR_BACKEND,
            settings.DISTANCE_METRIC,
        )
    except TransientModelError:
        raise
    except Exception as exc:
        if _matches_any(str(exc), _NO_FACE_MARKERS):
            raise NoFaceOnSelfieError(str(exc)) from exc
        raise


def run_verification(
    id_document_bytes: bytes,
    selfie_bytes: bytes,
    settings: Settings | None = None,
) -> VerificationResult:
    """Orquesta el pipeline completo: decodificar -> extraer rostro de la ID
    -> verificar contra la selfie -> aplicar umbral -> liveness (stub).

    No escribe nada a disco en ningún punto. Las excepciones lanzadas aquí
    (`InvalidImageError`, `NoFaceOnIdError`, `NoFaceOnSelfieError`,
    `TransientModelError` u otras) deben ser traducidas por la capa HTTP
    (`app/main.py`) a respuestas fail-closed (4xx/503), nunca a un match
    implícito.
    """
    settings = settings or get_settings()

    id_image = decode_image(id_document_bytes)
    selfie_image = decode_image(selfie_bytes)

    id_face_crop = extract_id_face(id_image, settings)
    verify_result = verify_faces(id_face_crop, selfie_image, settings)

    distance = float(verify_result["distance"])
    threshold = settings.SIMILARITY_THRESHOLD
    match = decide_match(distance, threshold)
    similarity = _distance_to_similarity(distance)
    liveness = check_liveness(selfie_image)

    return VerificationResult(
        match=match,
        similarity=similarity,
        distance=distance,
        threshold=threshold,
        model=settings.MODEL_NAME,
        liveness=liveness,
    )
