# 🪪 KYC Service — Verificación facial (YO FREE-LANCER)

Microservicio Python autoalojado que compara el rostro de una identificación
oficial (INE o pasaporte) contra una selfie, para el flujo de registro de
**YO FREE-LANCER** (aplica a YOER y a Cliente por igual). Usa FastAPI +
[DeepFace](https://github.com/serengil/deepface) (modelo `ArcFace`) +
`opencv-python-headless`. No tiene base de datos propia ni persiste ninguna
imagen: todo se procesa en memoria.

Este servicio **falla cerrado**: cualquier error, timeout, detección de baja
confianza o ausencia de rostro se traduce en una respuesta explícita de error
(4xx/503), nunca en un "match" implícito. Quien consuma esta API (la app
Flutter, fuera del alcance de este directorio) debe tratar cualquier
respuesta no-2xx o timeout como **NO verificado**.

---

## 🚀 Setup en 5 pasos

### 1. Crear el entorno virtual
```bash
cd kyc-service
python3 -m venv .venv
```

> ⚠️ **Requiere Python 3.9–3.12.** `deepface` depende de `tensorflow`, que al
> momento de escribir esto no publica wheels para Python 3.13/3.14. El
> `Dockerfile` de este servicio ya fija `python:3.11-slim` para evitar este
> problema en producción/CI. Si tu Python local es más nuevo, usa Docker
> (paso 5) o instala una versión de Python compatible.

### 2. Instalar dependencias
```bash
.venv/bin/pip install -r requirements.txt
# Para correr los tests, además:
.venv/bin/pip install -r requirements-dev.txt
```

### 3. Configurar variables de entorno (opcional, todas tienen default)
```bash
export SIMILARITY_THRESHOLD=0.65
export DETECTOR_BACKEND=retinaface
export MODEL_NAME=ArcFace
export MAX_UPLOAD_SIZE_MB=8
export PORT=8000
```

### 4. Correr el servicio
```bash
.venv/bin/uvicorn app.main:app --reload --port 8000
```

### 5. (Alternativa) Correr con Docker
```bash
docker compose up --build
```

---

## 🔎 Endpoints

### `GET /health`
Readiness probe simple:
```json
{"status": "ok"}
```
**Importante:** un `200` aquí **no** significa "hay un verificador de
respaldo disponible". Este servicio no tiene fallback propio — si está
caído, la verificación KYC simplemente no puede completarse. Eso es el
comportamiento correcto (fail-closed) y no debe interpretarse como señal
para activar un verificador alterno ni para saltarse la verificación.

### `POST /verify`
Multipart form-data:
| Campo | Tipo | Descripción |
|---|---|---|
| `id_document` | file | Foto de INE o pasaporte |
| `selfie` | file | Selfie tomada en el momento |

Header opcional `X-Request-Id` (si no se envía, el servicio genera uno).

**Respuesta exitosa (200):**
```json
{
  "request_id": "...",
  "match": true,
  "similarity": 0.81,
  "distance": 0.19,
  "threshold": 0.65,
  "model": "ArcFace",
  "liveness": {"status": "skipped", "score": null}
}
```
`distance`/`threshold` son la fuente de verdad de la decisión (`match =
distance <= threshold`); `similarity` (`1 - distance`, acotado a `[0,1]`) es
solo un campo informativo más amigable.

**Respuestas de error (todas fail-closed, nunca un 200 con match ambiguo):**
| Código | `error` | Cuándo |
|---|---|---|
| 400 | `INVALID_IMAGE` | Los bytes recibidos no son una imagen válida |
| 413 | `PAYLOAD_TOO_LARGE` | El archivo excede `MAX_UPLOAD_SIZE_MB` (se valida ANTES de decodificar) |
| 422 | `NO_FACE_ON_ID` | No se detectó rostro en la identificación |
| 422 | `NO_FACE_ON_SELFIE` | No se detectó rostro en la selfie |
| 503 | `VERIFICATION_UNAVAILABLE` | Cualquier otro error inesperado (incluye fallos transitorios de infraestructura que agotaron sus reintentos) |

---

## 🔒 Política de retención (sin persistencia)

- Las imágenes subidas se decodifican **directamente a memoria**
  (`cv2.imdecode(np.frombuffer(...))`) y nunca se escriben a disco por
  código propio de este servicio.
- Ninguna imagen, recorte de rostro, ni embedding facial se guarda en base
  de datos ni en logs.
- `app/logging_config.py` solo registra metadatos (request_id, tamaños en
  bytes, tiempos, resultado de la decisión) — nunca bytes de imagen, base64,
  ni contenido de archivo.
- El único registro persistente del resultado de una verificación vive
  **fuera de este servicio** (lado Flutter/Node, tabla `kyc_verifications`),
  y solo guarda `request_id, user_id, timestamp, match, similarity, model,
  threshold` — nunca imágenes.
- Nota de honestidad técnica: Starlette (el framework ASGI sobre el que
  corre FastAPI) usa internamente `SpooledTemporaryFile` para parsear
  uploads `multipart/form-data`, que puede volcarse a un archivo temporal del
  sistema operativo si el archivo supera ~1MB antes de que este servicio lo
  lea a memoria. Ese archivo temporal es efímero, gestionado por el SO/la
  librería estándar de Python (no por código de este servicio) y se elimina
  automáticamente al cerrarse. Mantener `MAX_UPLOAD_SIZE_MB` bajo (default 8)
  acota la ventana de este comportamiento. Si se requiere una garantía más
  estricta de "cero bytes tocan disco bajo ninguna circunstancia", habría que
  reemplazar el parseo multipart por un lector de stream a medida — fuera de
  alcance de esta primera versión.

---

## 🎯 Umbral de similitud

`SIMILARITY_THRESHOLD` (default `0.65`) se aplica sobre `distance` (métrica
coseno de `ArcFace`): `match = distance <= SIMILARITY_THRESHOLD`.

**Este valor es un punto de partida razonable, NO un valor calibrado con
datos reales de identificaciones mexicanas (INE/pasaporte).** Antes de usar
este servicio en producción, se debe recalibrar con un conjunto de pruebas
representativo (pares genuinos vs. impostores con fotos reales de INE y
selfies), midiendo FAR/FRR y ajustando el umbral según el apetito de riesgo
del producto.

---

## 🫥 Liveness (detección de vida)

`app/liveness.py` expone `check_liveness(selfie_array) -> LivenessResult`,
que **hoy siempre devuelve `status="skipped"`** — es un punto de extensión,
no una implementación real. No se verifica que la selfie provenga de una
persona viva presente (podría ser una foto de una foto, una pantalla, etc.).

Ruta de mejora recomendada (no implementada): activar el parámetro real de
DeepFace `anti_spoofing=True` en `DeepFace.extract_faces(...)`, que devuelve
`is_real`/`antispoof_score` por rostro detectado. Ver el docstring de
`app/liveness.py` para más detalle y las decisiones de producto pendientes
(p. ej. si un liveness "failed" debe bloquear el match aunque la similitud
sea alta).

---

## 🧪 Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -v
```

Para excluir la prueba lenta (real, sin mocks) si no tienes los pesos de
DeepFace descargados:
```bash
.venv/bin/pytest -v -m "not slow"
```

- `tests/test_pipeline.py`: prueba pura y tabulada de `decide_match`
  (distancia vs. umbral, casos justo en el límite), decodificación de
  imágenes (válida/corrupta/vacía), la heurística de reclasificación de
  errores transitorios, y el mecanismo de reintento (`_with_retry`) con un
  reloj falseado (sin esperar tiempo real). Incluye una prueba
  `@pytest.mark.slow` que ejercitaría el DeepFace real; se **auto-omite**
  (`pytest.importorskip`) si `deepface` no está instalado.
- `tests/test_api.py`: prueba la API con `fastapi.testclient.TestClient`,
  mockeando `DeepFace.extract_faces`/`DeepFace.verify` (nunca depende de
  pesos reales descargados). Cubre: `/health` 200, imagen corrupta → 4xx (no
  500), upload de más de `MAX_UPLOAD_SIZE_MB` → 413 **antes** de invocar
  DeepFace (se verifica con un mock que falla la prueba si se le llega a
  invocar), casos de match/no-match, ausencia de rostro en ID/selfie, y que
  una excepción inesperada cualquiera termine en 503 sin fugar el mensaje
  interno.

### 🧯 Estado real de la instalación de DeepFace en este entorno

En el sandbox donde se desarrolló este servicio, **solo había disponible
Python 3.14**, y `tensorflow` (dependencia de `deepface`) todavía no publica
wheels para esa versión — `pip install deepface` falla ahí con
`ResolutionImpossible`. `fastapi`, `uvicorn`, `pydantic`, `numpy` y
`opencv-python-headless` sí instalan y funcionan correctamente en Python
3.14.

Por eso `app/pipeline.py` importa `deepface` de forma **perezosa** (dentro
de las funciones que lo usan, no a nivel de módulo): la app FastAPI, el
`TestClient` y toda la suite de tests (excepto la marcada `slow`) corren y
pasan sin que `deepface` esté instalado. Se corrió `pytest` realmente en
este entorno: **25 pruebas pasaron, 1 se omitió automáticamente** (la
`@pytest.mark.slow` que requiere DeepFace real). Para producción, usa el
`Dockerfile` incluido (`python:3.11-slim`), donde `deepface`/`tensorflow` sí
instalan normalmente.

---

## 📁 Estructura del proyecto

```
kyc-service/
├── requirements.txt          # Dependencias de runtime
├── requirements-dev.txt      # + pytest/httpx/pytest-mock para tests
├── Dockerfile                 # python:3.11-slim (evita el problema de wheels de tensorflow)
├── docker-compose.yml         # Levanta el servicio de forma standalone
├── pytest.ini
├── app/
│   ├── main.py                # FastAPI: POST /verify, GET /health, exception handlers fail-closed
│   ├── pipeline.py            # Decodificación + extracción de rostro + comparación (DeepFace import perezoso)
│   ├── liveness.py            # Stub de liveness (siempre "skipped"), punto de extensión documentado
│   ├── config.py              # Settings vía pydantic-settings (env-driven)
│   ├── schemas.py             # Modelos pydantic de request/response
│   └── logging_config.py      # Logging estructurado, nunca loguea bytes de imagen
└── tests/
    ├── conftest.py             # Fixtures: imágenes sintéticas de color sólido, bytes corruptos
    ├── test_pipeline.py        # Unit tests puros + 1 prueba slow con DeepFace real
    ├── test_api.py             # Tests de API con TestClient, DeepFace mockeado
    └── fixtures/
        └── README.md           # Por qué no se commitean imágenes de rostro aquí
```

---

## ⛔ Pendiente / fuera de alcance de esta versión

- **Calibración real del umbral** con datasets de INE/pasaporte mexicanos
  (ver sección "Umbral de similitud").
- **Liveness real** (hoy solo el stub `skipped`) — requiere decisión de
  producto sobre cómo interactúa con el match (ver `app/liveness.py`).
- **Auditoría persistente** (`kyc_verifications`): este servicio no la
  implementa; es responsabilidad del backend Flutter/Node consumidor,
  guardando solo campos no biométricos.
- **Garantía de "cero bytes en disco" a nivel de framework ASGI**: ver nota
  en la sección de retención sobre `SpooledTemporaryFile` de Starlette.
- **Rate limiting / autenticación de este endpoint**: no implementado aquí;
  se asume que se despliega detrás de una red interna o gateway que ya
  autentica/limita quién puede llamar a `/verify`.
- **Instalación real de `deepface` verificada en CI**: en este entorno de
  desarrollo (Python 3.14) no fue posible instalar `deepface`/`tensorflow`;
  queda pendiente correr la suite completa (incluida la prueba `slow`) en un
  entorno con Python 3.9–3.12 antes de considerar esto probado de extremo a
  extremo contra el modelo real.
- **Métricas/observabilidad** (p. ej. Prometheus) más allá del logging
  estructurado básico.
