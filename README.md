# CDG_IA_SYNC_TEST

Prueba aislada para validar **MVSEP Parakeet v3** antes de integrarlo al panel CDG de producción.

## Objetivo

1. Subir una pista de **voces** (`wav`, `mp3`, `flac`, `m4a`, etc.).
2. Crear un trabajo MVSEP con:
   - `sep_type=64` → Parakeet
   - `add_opt1=0` → usar el audio tal cual
   - `add_opt2=1` → Parakeet v3
3. Consultar el `hash` hasta `done`.
4. Descargar/inspeccionar la salida textual.
5. Intentar convertir timestamps a una lista normalizada:

```json
[
  {"word": "SIEMPRE", "start": 31.240, "end": 31.700},
  {"word": "QUE", "start": 31.700, "end": 31.910}
]
```

**Importante:** el parser es deliberadamente tolerante porque el primer trabajo real nos permitirá confirmar el formato exacto del archivo que MVSEP genera para Parakeet. La UI conserva también la respuesta cruda, así que no perdemos información.

## Configuración

```bash
cp .env.example .env
```

Editar `.env`:

```env
MVSEP_API_TOKEN=TU_TOKEN_REAL
MVSEP_API_BASE=https://de.mvsep.com/api
```

El token **no debe subirse a GitHub**. `.env` ya está incluido en `.gitignore`.

## Ejecutar con Docker

```bash
docker compose up -d --build
```

Abrir:

```text
http://TU_SERVIDOR:8091
```

## Ejecutar sin Docker

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

En Windows PowerShell, la activación del entorno es:

```powershell
.venv\Scripts\Activate.ps1
```

## Qué queremos observar en la primera prueba

- Exactitud de la letra cantada en español.
- Si la salida contiene timestamps por palabra o por segmento.
- Formato exacto de la salida (`JSON`, `TXT`, `SRT`, etc.).
- Desfase real de los tiempos contra la waveform.
- Qué correcciones necesita Valeria antes de exportar CDG.

## Siguiente paso si la prueba sale bien

No mover este código directamente a producción. Primero convertir el resultado a la estructura interna del editor CDG y agregar un estado **IA PRE-SINCRONIZADA → REVISADA → CREAR CDG**.
