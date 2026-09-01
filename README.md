# CDG_IA_SYNC_TEST

Prueba aislada para comparar dos caminos antes de integrar IA al editor CDG de producción.

## Flujo A — Parakeet / MVSEP

- Parakeet v3
- `sep_type=64`
- `add_opt1=0`
- `add_opt2=1`
- Resultado real observado: MVSEP devuelve `data.transcription.txt` y `data.transcription.srt`.
- El SRT sirve como referencia temporal por segmento/frase.

## Flujo B — Qwen3

Worker GPU separado:

- `Qwen/Qwen3-ASR-1.7B`
- `Qwen/Qwen3-ForcedAligner-0.6B`

Regla:

1. Si el audio trae letra embebida, usarla como **letra maestra** y saltar ASR.
2. Si no hay letra, Qwen3-ASR genera una primera transcripción.
3. Forced Aligner intenta devolver palabra + inicio + fin.
4. La WebApp normaliza el resultado para comparar contra Parakeet.

> El Forced Aligner oficial está documentado para alineación texto–voz y soporta español. Esta repo existe precisamente para medir qué tan bien se comporta con **voz cantada** antes de llevarlo a producción.

## Arquitectura

```text
Navegador
   |
   v
OVH / FastAPI
   |----------------------> MVSEP / Parakeet
   |
   +----------------------> Qwen GPU Worker
                              |-- Qwen3-ASR 1.7B
                              +-- ForcedAligner 0.6B
```

El OVH no carga los modelos Qwen. Las credenciales/endpoints se guardan en `/runtime` y no se suben a GitHub.

## WebApp

En producción de prueba:

```text
https://panel.kitkaraoke.com/cdg-ia-test/
```

La pantalla permite:

- elegir una pista de voces;
- buscar letra embebida;
- editar/pegar letra maestra;
- ejecutar Parakeet;
- ejecutar Qwen cuando haya un worker GPU conectado;
- comparar resultados;
- ver logs persistentes.

## Worker GPU

Código en `gpu_worker/`.

Requiere GPU NVIDIA + Docker/NVIDIA Container Toolkit.

```bash
export WORKER_TOKEN='token-largo'
docker compose -f gpu_worker/docker-compose.yml up -d --build
```

Después se conecta la URL del worker desde la WebApp.

## Objetivo final

Conseguir una estructura:

```json
[
  {"word":"YA","start":23.360,"end":23.610},
  {"word":"TE","start":23.610,"end":23.790},
  {"word":"OLVIDÉ","start":23.790,"end":24.470}
]
```

y solo después integrar el ganador en el editor CDG como:

```text
IA PRE-SINCRONIZADA -> REVISAR -> APROBAR -> CREAR CDG
```
