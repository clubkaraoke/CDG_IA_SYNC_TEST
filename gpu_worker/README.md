# Qwen GPU Worker

Worker separado para la prueba CDG. Requiere una máquina con GPU NVIDIA y Docker/NVIDIA Container Toolkit.

Modelos:

- `Qwen/Qwen3-ASR-1.7B`
- `Qwen/Qwen3-ForcedAligner-0.6B`

## Ejecutar

```bash
export WORKER_TOKEN='elige-un-token-largo'
docker compose -f gpu_worker/docker-compose.yml up -d --build
```

Comprobar:

```bash
curl -H "Authorization: Bearer $WORKER_TOKEN" http://IP:8000/health
```

El OVH llama este worker; el navegador nunca recibe el token.

### Modos

- Si se envía `lyrics`: solo Forced Aligner.
- Sin `lyrics`: Qwen3-ASR 1.7B transcribe y luego Forced Aligner alinea.

La salida normalizada es:

```json
{
  "words": [
    {"word": "YA", "start": 23.36, "end": 23.61},
    {"word": "TE", "start": 23.61, "end": 23.79}
  ]
}
```
